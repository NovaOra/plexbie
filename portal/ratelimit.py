# path: portal/ratelimit.py
"""Per-visitor limits for routes anyone can reach (sign-in, invite links, images).

The visitor's address is the connection's own, unless the connection comes from a
trusted proxy (TRUSTED_PROXIES: by default this machine and Docker's networks,
where cloudflared and Nginx Proxy Manager run - not the rest of the LAN). Then
X-Forwarded-For is read from the right, skipping trusted proxies: each proxy
appends the address it saw, so the right-most untrusted entry is the visitor,
whatever the visitor wrote further left. CF-Connecting-IP is only a fallback when
there's no X-Forwarded-For: a proxy other than Cloudflare passes a visitor's own
copy of it straight through.
"""
import ipaddress
import json
import os
import time
from collections import deque
from typing import Deque, Dict, Optional

from aiohttp import web


#: Loopback and Docker's default address pools (where cloudflared and NPM run).
DEFAULT_TRUSTED = "127.0.0.0/8,::1/128,172.16.0.0/12"

_warned: set = set()
#: LAN proxies seen forwarding visitors without being trusted (Manage > Health lists them).
#: Capped: any LAN device can send the headers, and one that keeps changing address
#: mustn't grow it without end.
_untrusted: set = set()
_UNTRUSTED_MAX = 8


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        from core.logging import get_logger
        get_logger(__name__).warning(message)


def _lan_net() -> Optional[ipaddress.IPv4Network]:
    """This machine's own LAN (as a /24), when it can be found."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return ipaddress.ip_network(f"{s.getsockname()[0]}/24", strict=False)
    except (OSError, ValueError):
        return None


_cache: dict = {}


def _networks() -> list:
    raw = (os.getenv("TRUSTED_PROXIES") or "").strip()
    if raw not in _cache:
        _cache.clear()
        _cache[raw] = _parse(raw)
    return _cache[raw]


def _parse(raw: str) -> list:
    out = []
    for part in (raw or DEFAULT_TRUSTED).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            _warn_once(f"bad:{part}", f"TRUSTED_PROXIES: {part!r} isn't an address or range, so it's ignored "
                                      f"(names like 'npm' don't work; use the proxy's address)")
    if not raw:
        # The default trusts Docker's 172.16-31.x pools; a home network that itself
        # uses those addresses must not make every machine on it a "proxy".
        lan = _lan_net()
        if lan is not None and any(lan.subnet_of(n) for n in out if n.version == 4):
            out = [x for n in out for x in ((n.address_exclude(lan)) if n.version == 4 and lan.subnet_of(n) else (n,))]
            _warn_once("lan", f"This network ({lan}) is inside Docker's address range, so it's not trusted as a "
                              f"proxy; set TRUSTED_PROXIES to your proxy's address if one runs there")
    return out


def trusted(address: Optional[str]) -> bool:
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    return any(ip in net for net in _networks())


def _visitor(address: str) -> str:
    """The key a visitor is counted under: an IPv6 address by its /64, which one
    home or phone gets whole, so cycling through it doesn't make a new visitor."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address[:64]
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def visitor_scheme(request: web.Request) -> Optional[str]:
    """The scheme the visitor used, from Cloudflare's CF-Visitor header, and only
    when a trusted proxy (cloudflared) passed it on. X-Forwarded-Proto can't say:
    Nginx Proxy Manager overwrites it with its own plain-http hop."""
    if not trusted(request.remote or ""):
        return None
    try:
        scheme = json.loads(request.headers.get("CF-Visitor") or "{}").get("scheme")
    except (ValueError, AttributeError):
        return None
    return scheme if scheme in ("http", "https") else None


def untrusted_proxies() -> list:
    """LAN proxies seen forwarding visitors that TRUSTED_PROXIES doesn't list."""
    return sorted(_untrusted)


def client_ip(request: web.Request) -> str:
    remote = request.remote or "?"
    if not trusted(remote):
        if request.headers.get("X-Forwarded-For") or request.headers.get("CF-Connecting-IP"):
            try:
                private = ipaddress.ip_address(remote).is_private
            except ValueError:
                private = False
            if private:
                if len(_untrusted) < _UNTRUSTED_MAX:
                    _untrusted.add(remote)
                _warn_once(f"untrusted:{remote}",
                           f"A proxy at {remote} forwards visitors but isn't in TRUSTED_PROXIES, so every "
                           f"visitor through it shares one set of sign-in limits, and unless WEB_PUBLIC_URL "
                           f"is the site's https address, sign-in cookies go out without Secure and no HSTS "
                           f"is sent; add the proxy's address to TRUSTED_PROXIES and set WEB_PUBLIC_URL")
        return remote
    hops = [h.strip() for h in (request.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
    for hop in reversed(hops):
        if not trusted(hop):
            return hop[:64]
    if not hops:
        # cloudflared always sends both headers; this is only a fallback for a
        # proxy that sends CF-Connecting-IP alone.
        cf = (request.headers.get("CF-Connecting-IP") or "").strip()
        if cf:
            return cf[:64]
    return remote


def visitor(request: web.Request) -> str:
    """The key the limits below count this request's visitor under."""
    return _visitor(client_ip(request))


class Limiter:
    """At most `cap` hits per key in `window` seconds. Keys that go quiet are
    dropped, so a flood of made-up addresses can't grow it without bound."""

    def __init__(self, cap: int, window: float, max_keys: int = 10_000):
        self.cap, self.window, self.max_keys = cap, window, max_keys
        self._hits: Dict[str, Deque[float]] = {}

    def hit(self, key: str) -> bool:
        """Count a hit; True if it's over the limit (and so refused)."""
        now = time.monotonic()
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self.max_keys:
                self._prune(now)
            hits = self._hits[key] = deque()
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.cap:
            return True
        hits.append(now)
        return False

    def _prune(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] > self.window]:
            del self._hits[key]
        while len(self._hits) >= self.max_keys:          # all busy: drop the oldest
            self._hits.pop(next(iter(self._hits)))

    def over(self, request: web.Request) -> bool:
        return self.hit(visitor(request))
