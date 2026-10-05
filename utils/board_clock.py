# path: utils/board_clock.py
"""Countdowns on the stats boards that match what people see.

Discord draws <t:...:R> ("in 8 seconds", "3 seconds ago") against each viewer's
own clock, so a timestamp written from a server whose clock is off shows up
wrong by exactly that much. One test server ran almost four minutes slow; a fixed
"latency offset" of 96 seconds papered over that until the clock drifted again.

So the boards keep time by Discord's clock instead: every edit Discord confirms
carries its own edited_at, which gives the gap between this server and Discord.
Each board also learns how long its update takes from starting to landing (Plex
and Discord round trips), so "next update" points at when the new numbers
actually appear rather than when the next one merely starts.
"""
import time
from datetime import datetime
from typing import Dict, Optional

#: Weight of the newest measurement in the running averages.
SMOOTHING = 0.3
#: Until a board has been timed once, assume its update takes this long.
DEFAULT_TAKES = 2.0


class BoardClock:
    def __init__(self) -> None:
        self.skew: Optional[float] = None     # Discord's clock minus ours, in seconds
        self.takes: Dict[str, float] = {}     # board -> seconds from start to landing

    def _blend(self, old: Optional[float], new: float) -> float:
        return new if old is None else (1 - SMOOTHING) * old + SMOOTHING * new

    def landed(self, board: str, started: float, message) -> None:
        """Note an edit Discord confirmed. `started` is time.monotonic() at the update's start."""
        self.takes[board] = self._blend(self.takes.get(board), time.monotonic() - started)
        edited = getattr(message, "edited_at", None)
        if isinstance(edited, datetime):
            self.skew = self._blend(self.skew, edited.timestamp() - time.time())

    def now(self) -> int:
        """Now, by Discord's clock."""
        return round(time.time() + (self.skew or 0.0))

    def next_update(self, board: str, next_start: Optional[datetime], interval: float) -> int:
        """When the next update will show, by Discord's clock."""
        start = next_start.timestamp() if next_start else time.time() + interval
        return round(start + (self.skew or 0.0) + self.takes.get(board, DEFAULT_TAKES))
