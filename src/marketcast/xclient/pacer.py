"""ActionPacer — action throttling and daily caps for "account health".

Why: X restricts/bans accounts for bursts of automated actions. The pacer holds
a jittered pause between actions (like a human) and caps daily volumes
(follow/like/tweet/dm...). Counters persist (rolling 24h) so caps survive across
script runs.

Opt-in: by default the writer runs without a pacer (behaviour unchanged). Enable
via ``Twitter.from_env(safe_mode=True)`` or pass your own ``ActionPacer``.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from pathlib import Path

from .errors import TwitterError


class LimitReached(TwitterError):
    """Daily cap for an action was reached — try again later."""

    def __init__(self, action: str, limit: int) -> None:
        super().__init__(f"daily limit reached for '{action}' ({limit}/24h)")
        self.action = action
        self.limit = limit


# Conservative daily ceilings (overridable). None = no limit.
DEFAULT_LIMITS = {
    "follow": 400,
    "unfollow": 400,
    "like": 800,
    "retweet": 400,
    "tweet": 300,
    "reply": 300,
    "dm": 250,
    "bookmark": 800,
}

WINDOW = 24 * 3600  # daily limit window


class ActionPacer:
    """Jittered pacing + rolling-24h daily caps, optionally persisted to disk."""

    def __init__(
        self,
        *,
        limits: dict | None = None,
        jitter: tuple[float, float] = (3.0, 8.0),
        state_file=None,
    ) -> None:
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.jitter = jitter
        self.state_file = Path(state_file) if state_file else None
        self._events: dict[str, list[float]] = {}
        self._last_ts = 0.0
        if self.state_file:
            self._load()

    # ---- persistence ----

    def _load(self) -> None:
        try:
            self._events = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            self._events = {}

    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self._events), encoding="utf-8")
        except Exception:
            pass

    def _prune(self, action: str) -> list[float]:
        cutoff = time.time() - WINDOW
        kept = [t for t in self._events.get(action, []) if t >= cutoff]
        self._events[action] = kept
        return kept

    # ---- API ----

    def used(self, action: str) -> int:
        """How many times the action ran in the last 24h."""
        return len(self._prune(action))

    def remaining(self, action: str) -> int | None:
        """Remaining allowance for an action (None if uncapped)."""
        limit = self.limits.get(action)
        return None if limit is None else max(0, limit - self.used(action))

    def gate(self, action: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
        """Check the cap and wait the jitter pause. Raises ``LimitReached``."""
        recent = self._prune(action)
        limit = self.limits.get(action)
        if limit is not None and len(recent) >= limit:
            raise LimitReached(action, limit)
        # jitter: pause measured since the previous action of any type
        lo, hi = self.jitter
        if hi > 0:
            target = random.uniform(lo, hi)
            elapsed = time.time() - self._last_ts
            if elapsed < target:
                sleep(target - elapsed)
        self._record(action)

    def _record(self, action: str) -> None:
        self._events.setdefault(action, []).append(time.time())
        self._last_ts = time.time()
        self._save()
