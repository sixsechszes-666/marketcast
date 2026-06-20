"""AccountPool — a pool of accounts over the Twitter facade.

Each account = its own auth_token + (optional) proxy + its own cached session.
Accounts are built lazily (``ensure_ct0`` on first use). The pool offers rotation
(round-robin/random), lookup by handle, and automatically retires dead/banned
tokens (via the typed ``Unauthorized``/``Suspended`` errors).

    from marketcast.xclient import AccountPool
    pool = AccountPool.from_file("accounts.txt", cache=True, safe_mode=True)
    for acc in pool.healthy():
        acc.tw.write.like(some_id)
    acc = pool.next()              # round-robin over live accounts
"""

from __future__ import annotations

import random as _random
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .api import Twitter
from .errors import Suspended, Unauthorized


class Account:
    """A single account: lazy Twitter build, flags dead tokens."""

    def __init__(self, token: str, proxy: str | None = None, **tw_kwargs: Any) -> None:
        self.token = token
        self.proxy = proxy
        self.tw_kwargs = tw_kwargs
        self._tw: Twitter | None = None
        self.dead = False
        self.error: str | None = None

    @property
    def tw(self) -> Twitter:
        """Lazily build the Twitter facade. Flags dead on Unauthorized/Suspended."""
        if self._tw is None:
            try:
                self._tw = Twitter.from_token(self.token, proxy=self.proxy, **self.tw_kwargs)
            except (Unauthorized, Suspended) as e:
                self.dead = True
                self.error = type(e).__name__
                raise
        return self._tw

    @property
    def handle(self) -> str | None:
        """The account's @screen_name, or None if it can't be loaded."""
        try:
            return (self.tw.me or {}).get("screen_name")
        except (Unauthorized, Suspended):
            return None

    def check(self) -> bool:
        """Probe liveness with a real authed request (``badge_count``, which 401s
        on a dead token). Flags dead. Returns True if alive."""
        try:
            self.tw.read.get_badge_count()
            return True
        except (Unauthorized, Suspended) as e:
            self.dead = True
            self.error = type(e).__name__
            return False
        except Exception:
            return True  # network blips don't count as token death

    def __repr__(self) -> str:
        tag = "dead" if self.dead else (self._tw and "ready" or "lazy")
        return f"<Account {self.token[:8]}… {tag}>"


class AccountPool:
    """A rotating pool of :class:`Account` objects."""

    def __init__(self, accounts: list[Account]) -> None:
        self.accounts = accounts
        self._rr = 0

    # ---- constructors ----

    @classmethod
    def from_tokens(cls, tokens, **tw_kwargs: Any) -> AccountPool:
        """tokens: a list of ``token`` or ``(token, proxy)``. tw_kwargs
        (cache/safe_mode/impersonate...) apply to all accounts."""
        accs = []
        for t in tokens:
            if isinstance(t, tuple | list):
                token, proxy = (t + (None,))[:2]
            else:
                token, proxy = t, None
            if token:
                accs.append(Account(token, proxy, **tw_kwargs))
        return cls(accs)

    @classmethod
    def from_file(cls, path, **tw_kwargs: Any) -> AccountPool:
        """File: one account per line, ``#`` = comment. Line format:
        ``auth_token`` or ``auth_token,proxy`` (comma or space separated)."""
        tokens = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            tokens.append((parts[0], parts[1] if len(parts) > 1 else None))
        return cls.from_tokens(tokens, **tw_kwargs)

    # ---- access / rotation ----

    def __len__(self) -> int:
        return len(self.accounts)

    def __iter__(self) -> Iterator[Account]:
        return iter(self.accounts)

    def __getitem__(self, i) -> Account:
        return self.accounts[i]

    def healthy(self) -> list[Account]:
        """Accounts not flagged dead (no network check)."""
        return [a for a in self.accounts if not a.dead]

    def alive(self) -> list[Account]:
        """Accounts that passed a real identity check (makes requests)."""
        return [a for a in self.accounts if a.check()]

    def next(self) -> Account | None:
        """The next live account, round-robin."""
        pool = self.healthy()
        if not pool:
            return None
        acc = pool[self._rr % len(pool)]
        self._rr += 1
        return acc

    def random(self) -> Account | None:
        """A random healthy account, or None."""
        pool = self.healthy()
        return _random.choice(pool) if pool else None

    def get(self, handle: str) -> Account | None:
        """Find an account by @screen_name (builds sessions while searching)."""
        h = handle.lstrip("@").lower()
        for a in self.healthy():
            if (a.handle or "").lower() == h:
                return a
        return None

    def map(self, fn: Callable[[Account], Any], *, skip_dead: bool = True) -> list:
        """Run ``fn(account)`` over all accounts; dead/banned ones are skipped."""
        out = []
        for a in self.accounts:
            if skip_dead and a.dead:
                continue
            try:
                out.append(fn(a))
            except (Unauthorized, Suspended):
                a.dead = True
        return out

    def __repr__(self) -> str:
        return f"<AccountPool {len(self.healthy())}/{len(self.accounts)} healthy>"
