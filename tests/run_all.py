#!/usr/bin/env python3
# path: tests/run_all.py
"""Run the test suite without pytest.

The production image intentionally carries no dev dependencies, so this discovers
and runs every `test_*` function in every `test_*.py` beside it using only the
standard library. The tests are ordinary asserts, so `pytest tests/` works too
once pytest is installed - see requirements-dev.txt.

Usage:
    python tests/run_all.py            # everything
    python tests/run_all.py config kv  # only modules whose name matches
"""
import importlib
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))              # conftest / helpers
sys.path.insert(0, str(TESTS_DIR.parent))       # the project itself

import conftest  # noqa: F401,E402  (sys.path + dummy token)


def discover(filters):
    """Yield (name, module_or_None, import_error_or_None).

    A module that cannot be imported is reported rather than aborting the run -
    one broken import should not hide every other module's results.
    """
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if filters and not any(f in path.stem for f in filters):
            continue
        try:
            yield path.stem, importlib.import_module(path.stem), None
        except Exception:
            yield path.stem, None, traceback.format_exc()


def main(argv):
    filters = argv[1:]
    passed = failed = 0
    failures = []

    for module_name, module, import_error in discover(filters):
        if import_error is not None:
            failed += 1
            failures.append((module_name, "<import>", import_error))
            print(f"\n{module_name}")
            print("  FAIL  <could not import module>")
            continue

        names = [n for n in dir(module) if n.startswith("test_")]
        tests = [(n, getattr(module, n)) for n in names]
        tests = [(n, fn) for n, fn in tests if callable(fn)]
        if not tests:
            continue

        print(f"\n{module.__name__}  ({len(tests)} tests)")
        for name, fn in tests:
            try:
                fn()
            except Exception:
                failed += 1
                failures.append((module.__name__, name, traceback.format_exc()))
                print(f"  FAIL  {name}")
            else:
                passed += 1
                print(f"  ok    {name}")

    print("\n" + "=" * 68)
    if failures:
        for mod, name, tb in failures:
            print(f"\n--- {mod}.{name} ---")
            print(tb.rstrip())
        print("\n" + "=" * 68)
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
