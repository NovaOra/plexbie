# path: tests/test_formatting.py
"""Episode label formatting must tolerate missing season/episode numbers.

Regression coverage for: plexapi returns None for parentIndex/index on episodes
with incomplete season metadata, and four call sites formatted them directly with
:02d - which raises TypeError. The failures were silent rather than loud: inside
watch_tracking's 10-second loop the exception was swallowed, so the Now Watching
display stopped updating for as long as such an episode was playing.
"""
import conftest  # noqa: F401

from utils.formatting import UNKNOWN_MARKER, episode_label, season_episode


def test_both_numbers_present():
    assert season_episode(1, 2) == "S01E02"


def test_zero_padding():
    assert season_episode(3, 7) == "S03E07"


def test_large_numbers_are_not_truncated():
    assert season_episode(12, 345) == "S12E345"


def test_season_zero_is_valid():
    """Specials live in season 0, which is falsey - must not be treated as absent."""
    assert season_episode(0, 1) == "S00E01"
    assert UNKNOWN_MARKER not in season_episode(0, 0)


def test_missing_season():
    assert season_episode(None, 2) == f"S{UNKNOWN_MARKER}E02"


def test_missing_episode():
    assert season_episode(1, None) == f"S01E{UNKNOWN_MARKER}"


def test_both_missing():
    assert season_episode(None, None) == f"S{UNKNOWN_MARKER}E{UNKNOWN_MARKER}"


def test_no_typeerror_for_any_combination():
    """The actual crash: f"S{None:02d}" raises TypeError."""
    for season in (None, 0, 1, 99):
        for episode in (None, 0, 1, 99):
            season_episode(season, episode)  # must not raise


def test_full_label():
    assert episode_label("The Expanse", 3, 7, "Hard Vacuum") == \
        "The Expanse - S03E07: Hard Vacuum"


def test_label_without_title():
    assert episode_label("The Expanse", 3, 7) == "The Expanse - S03E07"


def test_label_with_missing_numbers_does_not_render_none():
    label = episode_label("The Expanse", None, None, "Unknown")
    assert "None" not in label
    assert "The Expanse" in label


def test_label_with_missing_show():
    assert "None" not in episode_label(None, 1, 1, "Pilot")


def test_no_call_site_formats_plex_values_with_02d():
    """Guard against the pattern being reintroduced."""
    import pathlib
    import re

    root = pathlib.Path(conftest.PROJECT_ROOT)
    offenders = []
    for path in sorted(root.glob("plugins/*/cog.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if re.search(r"\{[a-zA-Z_.\[\]0-9]*(season|episode|Index|index)[a-zA-Z_.\[\]0-9]*:02d\}", line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert offenders == [], (
        "use utils.formatting.season_episode/episode_label instead: " + str(offenders)
    )
