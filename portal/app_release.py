# path: portal/app_release.py
"""The app's latest version, for members: the app's "new version" card, the
website's download card, and one alert to app users per release.

The files live in config/app/: the signed APK, the iPhone .ipa and a
latest.json naming them. The maintainer's release script (plexbie-app's
scripts/release.sh) puts them there; any other install can copy the same three
files from the app's GitHub release. Downloads are for signed-in members: the app or the website asks
for a link, signed and good for LINK_SECONDS, which the phone's browser then
opens (the browser already may install apps; the app needs no permission).

iPhones: the same release carries an unsigned .ipa (latest.json's "ios"), which
members install with SideStore or AltStore under their own Apple ID. Each member
gets their own source address (a signed token, good for SOURCE_DAYS), which those
apps check for updates; the source and its download re-check, every time, that
the person is still a member, so removing someone from Plex ends their updates.
A member can also replace their address (a copy got out, say): it carries a
version kept per member, and replacing it bumps that, so every older one stops.
"""
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.blocking import run_blocking
from core.logging import get_logger
from database.kv_store import kv_get, kv_set
from portal.auth import sign, unsign

logger = get_logger(__name__)

APP_DIR = Path("config/app")
LINK_SECONDS = 600
APK_NAME = re.compile(r"plexbie-\d{1,3}\.\d{1,3}\.\d{1,3}\.apk")
IPA_NAME = re.compile(r"plexbie-\d{1,3}\.\d{1,3}\.\d{1,3}\.ipa")
SOURCE_DAYS = 3 * 365                          # a member's SideStore/AltStore source address
MIN_IOS = "16.4"                               # Expo SDK 57's deployment target
VERSION = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
SHA256 = re.compile(r"[0-9a-f]{64}")
BUNDLE_ID = "com.plexbie.app"
ANNOUNCED = ("app_release", "announced")      # the versionCode app users were last told about
SOURCES = "app_release"                        # "source:<via>:<id>": a member's source version


def _read(folder: Path) -> Optional[Dict[str, Any]]:
    """latest.json, checked, with the APK it names present; None otherwise."""
    try:
        info = json.loads((folder / "latest.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    file, version, code = info.get("file"), info.get("version"), info.get("versionCode")
    if not (isinstance(file, str) and APK_NAME.fullmatch(file) and isinstance(version, str) and VERSION.fullmatch(version)
            and isinstance(code, int) and code > 0 and isinstance(info.get("sha256"), str) and SHA256.fullmatch(info["sha256"])):
        logger.warning("config/app/latest.json isn't valid; no app update is offered")
        return None
    apk = folder / file
    if not apk.is_file():
        logger.warning(f"config/app/latest.json names {file}, which isn't there")
        return None
    notes = info.get("notes") if isinstance(info.get("notes"), str) else ""
    cert = info.get("cert") if isinstance(info.get("cert"), str) and SHA256.fullmatch(info["cert"]) else None
    return {"version": version, "versionCode": code, "file": file, "sha256": info["sha256"], "cert": cert,
            "size": apk.stat().st_size, "notes": notes[:2000], "publishedAt": info.get("publishedAt"),
            "ios": _read_ios(folder, info.get("ios"))}


def _read_ios(folder: Path, ios: Any) -> Optional[Dict[str, Any]]:
    """The release's iPhone build, if it has one that's there and checks out."""
    if ios is None:
        return None
    file = ios.get("file") if isinstance(ios, dict) else None
    if not (isinstance(file, str) and IPA_NAME.fullmatch(file) and isinstance(ios.get("sha256"), str)
            and SHA256.fullmatch(ios["sha256"])):
        logger.warning("config/app/latest.json has an iPhone build that isn't valid; it isn't offered")
        return None
    ipa = folder / file
    if not ipa.is_file():
        logger.warning(f"config/app/latest.json names {file}, which isn't there")
        return None
    return {"file": file, "sha256": ios["sha256"], "size": ipa.stat().st_size}


async def latest(folder: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    return await run_blocking(_read, folder or APP_DIR)


def public(info: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What members are told: no file names, which only a signed link uses."""
    if not info:
        return None
    out = {k: info[k] for k in ("version", "versionCode", "sha256", "size", "notes", "publishedAt")}
    out["ios"] = {"size": info["ios"]["size"]} if info.get("ios") else None
    return out


def link(secret: str, info: Dict[str, Any], now: Optional[float] = None) -> str:
    """The path of a download link for this release, good for LINK_SECONDS."""
    token = sign(secret, {"p": "apk", "f": info["file"], "exp": int((now or time.time()) + LINK_SECONDS)})
    return f"/download/app/{token}"


def apk_for(secret: str, token: str, folder: Optional[Path] = None) -> Optional[Path]:
    """The APK a download link names, if the link is good and the file is there."""
    payload = unsign(secret, token)
    if not payload or payload.get("p") != "apk" or not APK_NAME.fullmatch(str(payload.get("f") or "")):
        return None
    apk = (folder or APP_DIR) / payload["f"]
    return apk if apk.is_file() else None


def source_token(secret: str, user: Dict[str, Any], version: int = 0, now: Optional[float] = None) -> str:
    """A member's own SideStore/AltStore source address (its token part). It names the
    sign-in they asked with, so each fetch can check that person is still a member,
    and the member's source version (source_version), so replacing it ends this one."""
    u = user["user"]
    return sign(secret, {"p": "iossrc", "typ": "iossrc", "via": u.get("via"), "id": str(u["id"]), "v": version,
                         "exp": int((now or time.time()) + SOURCE_DAYS * 86400)})


def source_session(secret: str, token: str) -> Optional[Dict[str, Any]]:
    """The sign-in and version a source token names, if its signature is good. An
    address made before versions existed counts as version 0."""
    payload = unsign(secret, token)
    if not payload or payload.get("p") != "iossrc" or payload.get("via") not in ("discord", "plex") or not payload.get("id"):
        return None
    version = payload.get("v", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    return {"via": payload["via"], "id": str(payload["id"]), "v": version}


def _source_key(via: Any, uid: Any) -> str:
    return f"source:{via}:{uid}"


async def source_version(user: Dict[str, Any], renew: bool = False) -> int:
    """A member's current source version (0 until they first replace their address);
    with renew, the next one, which ends every address made before it."""
    u = user["user"]
    key = _source_key(u.get("via"), u["id"])
    current = await kv_get(SOURCES, key)
    current = current if isinstance(current, int) else 0
    if renew:
        current += 1
        await kv_set(SOURCES, key, current)
    return current


async def source_check(secret: str, token: str) -> Optional[Dict[str, Any]]:
    """source_session, if the token is also the member's current version (not replaced)."""
    s = source_session(secret, token)
    if s is None:
        return None
    current = await kv_get(SOURCES, _source_key(s["via"], s["id"]))
    return s if s["v"] == (current if isinstance(current, int) else 0) else None


def ipa_for(token_ok: bool, file: str, folder: Optional[Path] = None) -> Optional[Path]:
    """The iPhone build a member's source points at, if their token was good."""
    if not token_ok or not IPA_NAME.fullmatch(file or ""):
        return None
    ipa = (folder or APP_DIR) / file
    return ipa if ipa.is_file() else None


def _plain(notes: str) -> str:
    """Release notes without Markdown, for SideStore/AltStore's "what's new"."""
    lines = []
    for line in (notes or "").splitlines():
        line = re.sub(r"[*_`#>]+", "", line).strip()
        if line.lower().startswith(("installing", "checks")):
            break
        lines.append(re.sub(r"^[-•]\s*", "• ", line))
    return "\n".join(lines).strip()


def altstore_source(info: Optional[Dict[str, Any]], site: str, token: str) -> Dict[str, Any]:
    """A SideStore/AltStore source for one member (both read the same format): Plexbie
    and its newest iPhone build, downloaded through their own address."""
    site = site.rstrip("/")
    icon = f"{site}/brand/plexbie-512.png"
    app = {
        "name": "Plexbie", "bundleIdentifier": BUNDLE_ID, "developerName": "Your household's Plexbie",
        "subtitle": "Ask for films, shows and books on your household's Plex.",
        "localizedDescription": ("Plexbie for your household's Plex: ask for films, shows and books, follow each one "
                                 "on its way, and see what just arrived. Signing in happens on your Plexbie's own page."),
        "iconURL": icon, "tintColor": "ffd1e4", "category": "entertainment", "screenshotURLs": [],
        "versions": [],
        "appPermissions": {"entitlements": [], "privacy": {}},
    }
    ios = (info or {}).get("ios")
    if info and ios:
        app["versions"].append({
            "version": info["version"], "buildVersion": str(info["versionCode"]),
            "date": info.get("publishedAt") or "", "localizedDescription": _plain(info["notes"]),
            "downloadURL": f"{site}/download/ios/{token}/{ios['file']}", "size": ios["size"],
            "minOSVersion": MIN_IOS,
        })
    return {"name": "Plexbie", "identifier": "com.plexbie.source", "subtitle": "Your household's Plexbie app",
            "website": site, "iconURL": icon, "tintColor": "ffd1e4", "apps": [app], "news": []}


def headline(notes: str) -> str:
    """The first line of the release notes, without Markdown, for an alert."""
    for line in (notes or "").splitlines():
        line = re.sub(r"[*_`#>]+", "", line).strip(" -•")
        if line and not line.lower().startswith("new in "):
            return line[:200]
    return "Open Plexbie to get it."


async def announce(folder: Optional[Path] = None) -> bool:
    """Tell app users, once per release, that a new version is out. The first
    release Plexbie sees is only noted, so a fresh install doesn't alert."""
    from core import notify
    info = await latest(folder)
    if not info:
        return False
    told = await kv_get(*ANNOUNCED)
    if isinstance(told, int) and told >= info["versionCode"]:
        return False
    await kv_set(*ANNOUNCED, info["versionCode"])
    if told is None:
        return False
    sent = await notify.push_app_to_everyone(title=f"Plexbie {info['version']} is out", body=headline(info["notes"]), url="/app/update")
    logger.info(f"App {info['version']} announced to {sent} phone(s)")
    return True

