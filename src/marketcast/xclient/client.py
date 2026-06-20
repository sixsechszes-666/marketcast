"""Base client: curl_cffi session, auth_token, auto ct0 acquisition, low-level request."""

from __future__ import annotations

from curl_cffi import requests

from .log import get_logger

log = get_logger("twitter.client")


# Public x.com web-client bearer token (shipped to every browser; not a secret
# and not account-specific). Required to talk to the private API.
BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "authorization": f"Bearer {BEARER}",
    "content-type": "application/json",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "user-agent": USER_AGENT,
    "x-twitter-active-user": "yes",
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-client-language": "ru",
}


class TwitterClient:
    """Low-level x.com client. Holds the session, auth_token, ct0 and shared headers."""

    def __init__(
        self,
        auth_token: str,
        proxy: str | None = None,
        impersonate: str = "chrome124",
        lang: str = "ru",
        timeout: int = 30,
        username: str | None = None,
        extra_cookies: dict | None = None,
    ) -> None:
        if not auth_token:
            raise ValueError("auth_token is required")
        self.auth_token = auth_token
        self.proxy = proxy
        self.impersonate = impersonate
        self.timeout = timeout
        self.username = (username or "").lstrip("@") or None

        self.session = requests.Session()
        self.session.cookies.set("auth_token", auth_token, domain=".x.com")
        for name, value in (extra_cookies or {}).items():
            if value:
                self.session.cookies.set(name, value, domain=".x.com")

        self._ct0: str | None = None
        self._base_headers = {**BASE_HEADERS, "x-twitter-client-language": lang}
        self.last_response = None

    # ---- ct0 / auth ----

    def ensure_ct0(self) -> str:
        """Fetch (and cache) the ``ct0`` CSRF token from x.com if not already set."""
        if self._ct0:
            return self._ct0
        r = self.session.get(
            "https://x.com/i/release_notes",
            headers={"user-agent": USER_AGENT},
            impersonate=self.impersonate,
            timeout=self.timeout,
            allow_redirects=True,
        )
        ct0 = self.session.cookies.get("ct0")
        if not ct0:
            self.session.get(
                "https://x.com/home", impersonate=self.impersonate, timeout=self.timeout
            )
            ct0 = self.session.cookies.get("ct0")
        if not ct0:
            raise RuntimeError(f"Failed to acquire ct0 (status={r.status_code})")
        self._ct0 = ct0
        prefix = f"[@{self.username}] " if self.username else ""
        log.info(f"{prefix}ct0 acquired [grey70]{ct0[:16]}… len={len(ct0)}[/grey70]")
        return ct0

    # ---- transport ----

    def _headers(self, referer: str | None = None, extra: dict | None = None) -> dict:
        h = {**self._base_headers, "x-csrf-token": self.ensure_ct0()}
        if referer:
            h["referer"] = referer
        if extra:
            h.update(extra)
        return h

    def _kw(self) -> dict:
        kw = {"impersonate": self.impersonate, "timeout": self.timeout}
        if self.proxy:
            kw["proxy"] = self.proxy
        return kw

    def request(
        self,
        method: str,
        url: str,
        *,
        params=None,
        json_body=None,
        data=None,
        referer: str | None = None,
        extra_headers: dict | None = None,
    ):
        """Perform a single HTTP request, attaching auth/CSRF/transaction headers."""
        headers = self._headers(referer, extra_headers)
        if data is not None:
            headers["content-type"] = "application/x-www-form-urlencoded"
        # x-client-transaction-id: required by some GraphQL endpoints (SearchTimeline
        # 404s without it). Generated from the home page; fail-open so a breakage
        # here just restores the old header-less behaviour.
        if "x-client-transaction-id" not in headers and "/i/api/graphql/" in url:
            try:
                from urllib.parse import urlparse

                from . import transaction
                gen = transaction.get_generator(self.session, self.impersonate)
                headers["x-client-transaction-id"] = gen.generate(method, urlparse(url).path)
            except Exception as e:
                log.debug(f"x-client-transaction-id skipped: {e}")
        resp = self.session.request(
            method,
            url,
            params=params,
            json=json_body,
            data=data,
            headers=headers,
            **self._kw(),
        )
        self.last_response = resp
        status = resp.status_code
        rl = resp.headers.get("x-rate-limit-remaining")
        if status < 300:
            color = "green"
        elif status < 500:
            color = "yellow"
        else:
            color = "red"
        prefix = f"[@{self.username}] " if self.username else ""
        log.debug(
            f"{prefix}{method} [{color}]{status}[/{color}] "
            f"[dim]rl={rl} {url[:80]}[/dim]"
        )
        return resp

    def get(self, url, **kw):
        """GET shorthand for :meth:`request`."""
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        """POST shorthand for :meth:`request`."""
        return self.request("POST", url, **kw)

    # ---- session persistence ----

    def dump_cookies(self) -> dict:
        """All session cookies as a dict (including auth_token/ct0/twid)."""
        try:
            return dict(self.session.cookies.get_dict())
        except Exception:
            out = {}
            try:
                for c in self.session.cookies.jar:
                    out[c.name] = c.value
            except Exception:
                pass
            return out

    def load_cookies(self, cookies: dict) -> None:
        """Load cookies into the session (and reuse ct0, skipping a network call)."""
        for k, v in (cookies or {}).items():
            if v:
                try:
                    self.session.cookies.set(k, v, domain=".x.com")
                except Exception:
                    pass
        if cookies.get("ct0"):
            self._ct0 = cookies["ct0"]

    def save_session(self, path) -> None:
        """Persist cookies to disk (json) to avoid re-logging-in."""
        import json as _json
        from pathlib import Path as _Path
        p = _Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            _json.dumps({"auth_token": self.auth_token, "cookies": self.dump_cookies()}),
            encoding="utf-8",
        )

    def load_session(self, path) -> bool:
        """Load cookies from disk. True if a file existed and was applied."""
        import json as _json
        from pathlib import Path as _Path
        p = _Path(path)
        if not p.exists():
            return False
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.load_cookies(data.get("cookies") or {})
        return True
