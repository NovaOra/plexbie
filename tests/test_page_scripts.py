# path: tests/test_page_scripts.py
"""The scripts inside the pages the bot serves itself (portal/*.html) parse.

A page's script that declares the same name twice at the top stops with a
SyntaxError before it runs any of it, and the page shows only its headings: the
first-time setup page did exactly that. The website's tests compile these scripts
with Node (web/src/portalPages.test.ts); this catches the same mistake here, where
there's no JavaScript engine.
"""
import pathlib
import re
from collections import Counter

import conftest  # noqa: F401

PORTAL = pathlib.Path(__file__).resolve().parent.parent / "portal"
SCRIPT = re.compile(r"<script(\s[^>]*)?>(.*?)</script>", re.S | re.I)
# Declarations at the start of a line with no indent: the page's top level.
TOP = re.compile(r"^(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _scripts(path: pathlib.Path):
    for m in SCRIPT.finditer(path.read_text()):
        attrs = m.group(1) or ""
        if re.search(r"\ssrc=|type=[\"']?(module|application/json|application/ld\+json|importmap)", attrs):
            continue
        yield m.group(2)


def test_the_bot_serves_pages_with_scripts():
    assert any(any(True for _ in _scripts(p)) for p in PORTAL.glob("*.html")), "portal/*.html with a script"


def test_no_page_script_declares_a_name_twice_at_the_top():
    for page in sorted(PORTAL.glob("*.html")):
        for script in _scripts(page):
            twice = sorted(name for name, n in Counter(TOP.findall(script)).items() if n > 1)
            assert not twice, f"{page.name} declares {', '.join(twice)} more than once, so its script never runs"


def test_the_setup_page_keeps_its_one_escape_helper():
    # The one that also escapes ' (the setup page puts text in attributes too).
    script = "".join(_scripts(PORTAL / "setup.html"))
    assert TOP.findall(script).count("esc") == 1
    assert "\"'\": \"&#39;\"" in script
