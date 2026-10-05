# path: tests/test_board_clock.py
"""Stats-board countdowns by Discord's clock (utils/board_clock)."""
import time
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

from utils.board_clock import DEFAULT_TAKES, BoardClock


class Msg:
    def __init__(self, edited_at):
        self.edited_at = edited_at


def test_a_slow_server_clock_is_learned_from_discord():
    clock = BoardClock()
    # Discord stamps the edit 228 s ahead of this server (a real drift seen in testing).
    clock.landed("now", time.monotonic(), Msg(datetime.now(timezone.utc) + timedelta(seconds=228)))
    assert abs(clock.skew - 228) < 1
    assert abs(clock.now() - (time.time() + 228)) < 1.5


def test_the_countdown_ends_when_the_update_lands_not_when_it_starts():
    clock = BoardClock()
    clock.skew = 0.0
    clock.takes["now"] = 4.0                       # Plex + Discord round trips
    start = datetime.now(timezone.utc) + timedelta(seconds=10)
    assert clock.next_update("now", start, 10) == round(start.timestamp() + 4.0)


def test_before_anything_is_measured_it_still_gives_a_sensible_time():
    clock = BoardClock()
    got = clock.next_update("leaderboard", None, 300)
    assert abs(got - (time.time() + 300 + DEFAULT_TAKES)) < 1.5


def test_measurements_are_smoothed_not_jumpy():
    clock = BoardClock()
    clock.landed("now", time.monotonic() - 2, Msg(None))
    clock.landed("now", time.monotonic() - 12, Msg(None))      # one slow update
    assert 2 < clock.takes["now"] < 6


def test_the_magic_offset_is_gone():
    from pathlib import Path
    text = (Path(conftest.PROJECT_ROOT) / "plugins/watch_tracking/cog.py").read_text()
    assert "TIMESTAMP_LATENCY_OFFSET" not in text
    assert text.count("self.clock.landed(") == 1, "one place edits a board, and times it"
    assert text.count("await self._edit_board(") == 3, "all three boards go through it"


def test_every_board_edit_has_its_start_time():
    """The streaks board is edited from a helper; it must be handed the update's start."""
    import ast
    from pathlib import Path
    tree = ast.parse((Path(conftest.PROJECT_ROOT) / "plugins/watch_tracking/cog.py").read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and "self.clock.landed(" in ast.unparse(fn):
            names = {a.arg for a in fn.args.args} | {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                                                      for t in n.targets if isinstance(t, ast.Name)}
            assert "started" in names, f"{fn.name} uses `started` without defining it"
