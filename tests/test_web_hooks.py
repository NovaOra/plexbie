# path: tests/test_web_hooks.py
"""React's rule: no hook after an early return in a component.

Breaking it renders fine when data is already there and blanks the page when it
arrives a moment later ("Rendered more hooks than during the previous render").
Manage → People did exactly that when opened by its link. Twice now, so it's
checked here rather than remembered.
"""
import pathlib
import re

import conftest  # noqa: F401

WEB = pathlib.Path(conftest.PROJECT_ROOT) / "web" / "src"


def test_no_hook_follows_an_early_return():
    bad = []
    for path in sorted(WEB.rglob("*.tsx")):
        in_component, returned = False, None
        for number, line in enumerate(path.read_text().split("\n"), 1):
            if re.match(r"^(export )?(default )?function [A-Z]\w*\(", line):
                in_component, returned = True, None
                continue
            if in_component and line.startswith("}"):
                in_component = False
                continue
            if in_component and returned is None and re.match(r"^  (if \(.*\) )?return\b", line):
                returned = number
            if in_component and returned and re.match(r"^  (const|let) .*\buse[A-Z]\w*\(", line):
                bad.append(f"{path.relative_to(WEB)}:{number} (early return at {returned})")
    assert not bad, "hooks after an early return:\n  " + "\n  ".join(bad)
