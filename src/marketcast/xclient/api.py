"""Twitter — a single facade over TwitterClient + TwitterReader + TwitterWriter.

Goal: one line to get a ready object exposing reads (``.read``), actions
(``.write``) and information about your own account (``.me``).

    from marketcast.xclient import Twitter
    x = Twitter.from_env()              # picks up AUTH_TOKEN from env or .env
    print(x.me["screen_name"])          # who is logged in
    tweets = x.read.search("solana", count=20)
    x.write.like(tweets[0]["id"])
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import unquote

from .client import TwitterClient
from .pacer import ActionPacer
from .reader import TwitterReader
from .telemetry import Telemetry
from .writer import TwitterWriter

# .env files tried (in priority order) when a token isn't passed and isn't in env.
_ENV_CANDIDATES = [
    Path.cwd() / ".env",
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parents[3] / ".env",
]


def _read_env_file(path: Path) -> dict:
    """Minimal ``KEY=value`` .env parser (no external dependency)."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def load_creds() -> tuple[str, str | None]:
    """Return ``(auth_token, proxy)``. env (AUTH_TOKEN/PROXY) > .env files."""
    token = (os.getenv("AUTH_TOKEN") or "").strip()
    proxy = (os.getenv("PROXY") or "").strip() or None
    if token:
        return token, proxy
    for path in _ENV_CANDIDATES:
        if path.exists():
            vals = _read_env_file(path)
            token = (vals.get("AUTH_TOKEN") or "").strip()
            if token:
                return token, (vals.get("PROXY") or "").strip() or None
    return "", proxy


class Twitter:
    """High-level facade. Holds client/reader/writer and caches identity."""

    def __init__(
        self, client: TwitterClient, *, cache_path: Path | None = None, pacer=None,
    ) -> None:
        self.client = client
        self.read = TwitterReader(client)
        self.write = TwitterWriter(client, pacer=pacer)
        self.telemetry = Telemetry(client)
        self.pacer = pacer
        self._cache_path = cache_path
        self._me: dict | None = None

    # ---- constructors ----

    @staticmethod
    def _resolve_cache(auth_token: str, cache) -> Path | None:
        """cache: False -> none; True -> ~/.xclient/<token8>.json; str/Path -> it."""
        if not cache:
            return None
        if cache is True:
            h = hashlib.sha1(auth_token.encode()).hexdigest()[:8]
            return Path.home() / ".xclient" / f"{h}.json"
        return Path(cache)

    @classmethod
    def from_token(
        cls, auth_token: str, *, proxy: str | None = None, cache=False,
        safe_mode=False, pacer=None, **client_kwargs
    ) -> Twitter:
        """Build from an explicit token.

        ``cache=True`` caches cookies/ct0 in ``~/.xclient/<token8>.json`` (fast
        start, fewer login requests). ``safe_mode=True`` enables an ActionPacer
        (jitter pauses + daily action caps; state in
        ``~/.xclient/pacer_<token8>.json``). You may also pass your own pacer.
        """
        client = TwitterClient(auth_token=auth_token, proxy=proxy, **client_kwargs)
        cache_path = cls._resolve_cache(auth_token, cache)
        if cache_path:
            client.load_session(cache_path)
        client.ensure_ct0()
        if cache_path:
            client.save_session(cache_path)
        if pacer is None and safe_mode:
            h = hashlib.sha1(auth_token.encode()).hexdigest()[:8]
            pacer = ActionPacer(state_file=Path.home() / ".xclient" / f"pacer_{h}.json")
        return cls(client, cache_path=cache_path, pacer=pacer)

    @classmethod
    def from_env(cls, *, cache=False, safe_mode=False, pacer=None, **client_kwargs) -> Twitter:
        """Build from AUTH_TOKEN/PROXY in the environment or a .env file."""
        token, proxy = load_creds()
        if not token:
            raise RuntimeError(
                "AUTH_TOKEN not found (neither in env nor .env). "
                "Put AUTH_TOKEN=... in .env or pass Twitter.from_token(token)."
            )
        return cls.from_token(
            token, proxy=proxy, cache=cache, safe_mode=safe_mode, pacer=pacer,
            **client_kwargs,
        )

    def save_session(self) -> None:
        """Persist the current session to the cache (if caching is enabled)."""
        if self._cache_path:
            self.client.save_session(self._cache_path)

    # ---- identity ----

    @property
    def my_id(self) -> str | None:
        """The numeric user_id of the logged-in account (from the ``twid`` cookie)."""
        twid = self.client.session.cookies.get("twid")
        if not twid:
            self.client.ensure_ct0()
            twid = self.client.session.cookies.get("twid")
        if not twid:
            return None
        return unquote(twid).split("u=")[-1] or None

    @property
    def me(self) -> dict | None:
        """The logged-in account's profile (cached)."""
        if self._me is None:
            uid = self.my_id
            if uid:
                self._me = self.read.get_user_by_id(uid)
        return self._me

    def whoami(self) -> dict | None:
        """Force-refresh your own profile."""
        self._me = None
        return self.me

    def __repr__(self) -> str:
        who = (self._me or {}).get("screen_name")
        return f"<Twitter @{who}>" if who else "<Twitter (identity not loaded)>"
