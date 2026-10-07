# path: tests/test_wrangler_pin.py
"""The Cloudflare Workers are deployed with a pinned, locked wrangler.

`npx wrangler` with nothing installed runs whatever the registry serves that day,
on the machine that holds the Cloudflare credentials and sets the Workers'
secrets. web/cloudflare/package.json pins one exact version under a lockfile, and
every deploy instruction goes through that copy. Its node_modules stays out of
the image like web/node_modules does.
"""
import json
import pathlib
import re

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)
WEB = ROOT / "web"
CLOUDFLARE = WEB / "cloudflare"

EXACT = re.compile(r"^\d+\.\d+\.\d+$")


def _manifest():
    path = CLOUDFLARE / "package.json"
    assert path.exists(), "web/cloudflare/package.json is missing"
    return json.loads(path.read_text())


def test_wrangler_is_pinned_to_an_exact_version():
    manifest = _manifest()
    deps = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
    assert "wrangler" in deps, "wrangler is not in web/cloudflare/package.json"
    assert EXACT.match(deps["wrangler"]), f"wrangler is a range, not a version: {deps['wrangler']}"


def test_the_lockfile_matches_the_pin():
    lock_path = CLOUDFLARE / "package-lock.json"
    assert lock_path.exists(), "web/cloudflare/package-lock.json is missing"
    lock = json.loads(lock_path.read_text())
    pinned = _manifest()["devDependencies"]["wrangler"]
    locked = lock["packages"]["node_modules/wrangler"]
    assert locked["version"] == pinned
    assert locked.get("integrity"), "the locked wrangler has no integrity hash"


def test_no_instruction_fetches_wrangler_from_the_registry():
    docs = [WEB / "README.md", *sorted(CLOUDFLARE.glob("*.js*")), *sorted(CLOUDFLARE.glob("*/*.js*"))]
    loose = []
    for path in docs:
        if "node_modules" in path.parts:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bnpx\s+wrangler\b", line):
                loose.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    assert loose == [], "use the pinned copy (cloudflare/node_modules/.bin/wrangler): " + ", ".join(loose)


def test_the_pinned_copy_stays_out_of_the_image():
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    patterns = {ln.strip().rstrip("/") for ln in lines if ln.strip() and not ln.strip().startswith("#")}
    assert "web/cloudflare/node_modules" in patterns
