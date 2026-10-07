# path: tests/test_python_pins.py
"""Every Python package the image and CI install comes at an exact version.

requirements.txt pins what Plexbie asks for and constraints.txt pins what those
pull in. The image's pip, setuptools and wheel are pinned in the Dockerfile, and
pytest's own dependencies (CI's pytest job) are pinned in constraints.txt, so no
build or test run picks up whatever the registry serves that day.
"""
import pathlib
import re

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)

# What pytest (requirements-dev.txt) needs on Python 3.11 on Linux.
PYTEST_DEPS = ("iniconfig", "packaging", "pluggy", "pygments")


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _constraints():
    pins = {}
    for line in (ROOT / "constraints.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, sep, version = line.partition("==")
            assert sep and version.strip(), f"constraints.txt has a range, not a version: {line}"
            pins[_norm(name)] = version.strip()
    return pins


def _image_pip_installs():
    text = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    return re.findall(r"pip install ([^&\n]+)", text)


def test_the_image_pins_pip_setuptools_and_wheel():
    installs = _image_pip_installs()
    assert installs, "the Dockerfile installs no Python packages"
    named = " ".join(installs)
    for tool in ("pip", "setuptools", "wheel"):
        assert re.search(rf"(?<![\w-]){tool}==\d+(\.\d+)*(?![\w.])", named), f"{tool} is not pinned in the Dockerfile"


def test_the_image_upgrades_nothing_to_latest():
    for args in _image_pip_installs():
        assert "--upgrade" not in args.split() and "-U" not in args.split(), f"unpinned upgrade: pip install {args}"
        assert "-c constraints.txt" in args, f"installed without the constraints: pip install {args}"


def test_pytests_dependencies_are_pinned():
    pins = _constraints()
    missing = [name for name in PYTEST_DEPS if name not in pins]
    assert missing == [], "not pinned in constraints.txt: " + ", ".join(missing)
