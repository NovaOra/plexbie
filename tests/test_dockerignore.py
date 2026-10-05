# path: tests/test_dockerignore.py
""".dockerignore patterns for files that can appear at any depth must say so.

Regression coverage for: .dockerignore matches from the build-context root only,
unlike .gitignore. "__pycache__/" and "*.py[cod]" excluded nothing below the
root, and the running bot writes bytecode into its bind-mounted plugin
directories inside the build context - so 904 KB of it was in every image. The
*.bak / *.sync-conflict-* rules had the same hole for nested copies.

Root-anchored entries (config/, logs/, tests/, .git/ ...) are deliberate and
left alone; this only checks name-based patterns.
"""
import pathlib

import conftest  # noqa: F401

DOCKERIGNORE = pathlib.Path(conftest.PROJECT_ROOT) / ".dockerignore"


def _patterns():
    lines = DOCKERIGNORE.read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def test_name_based_patterns_match_at_any_depth():
    unanchored = [
        p for p in _patterns()
        if not p.startswith("**/")
        and (p.startswith("*") or p.rstrip("/") in ("__pycache__", ".DS_Store"))
    ]
    assert unanchored == [], (
        "these only match at the context root; prefix with **/: " + ", ".join(unanchored)
    )


def test_bytecode_is_excluded_everywhere():
    patterns = _patterns()
    assert "**/__pycache__/" in patterns or "**/__pycache__" in patterns
    assert "**/*.py[cod]" in patterns
