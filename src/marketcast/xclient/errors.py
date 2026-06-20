"""Typed xclient errors.

Let callers react programmatically: a dead token -> rotate account, a rate
limit -> wait, a ban -> drop from the pool, etc. — instead of a bare
``RuntimeError(text)``.
"""

from __future__ import annotations


class TwitterError(RuntimeError):
    """Base error. Carries the HTTP status, X error code and a body snippet."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: int | None = None, response=None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.response = response


class RateLimited(TwitterError):
    """429. ``reset`` is the epoch-seconds window reset (if provided in headers)."""

    def __init__(self, message="rate limited", *, reset: int | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.reset = reset


class Unauthorized(TwitterError):
    """401 / dead or expired auth_token (X code 32/89)."""


class Suspended(TwitterError):
    """Account suspended/restricted (X code 64) or temporarily locked (326)."""


class Forbidden(TwitterError):
    """403 not classified as Suspended (e.g. read-only, action blocked)."""


class NotFound(TwitterError):
    """404 (X code 34) — missing resource / stale query_id."""


# X error codes that unambiguously mean "token/account is no good".
_DEAD_TOKEN_CODES = {32, 89, 99}
_SUSPENDED_CODES = {64, 141, 326}


def _extract(resp) -> tuple[int | None, str]:
    """(first X error code, message) from the JSON body, if present."""
    try:
        j = resp.json()
    except Exception:
        return None, (getattr(resp, "text", "") or "")[:300]
    errs = j.get("errors") if isinstance(j, dict) else None
    if isinstance(errs, list) and errs:
        e0 = errs[0]
        if isinstance(e0, dict):
            return e0.get("code"), str(e0.get("message") or errs)
        return None, str(errs)
    return None, (getattr(resp, "text", "") or "")[:300]


def raise_for_status(resp, op: str | None = None) -> None:
    """Raise a typed error if the HTTP status is >= 400; otherwise do nothing."""
    status = resp.status_code
    if status < 400:
        return
    code, msg = _extract(resp)
    prefix = f"{op}: " if op else ""
    kw = {"status": status, "code": code, "response": resp}

    if status == 429:
        reset = resp.headers.get("x-rate-limit-reset")
        try:
            reset = int(reset) if reset else None
        except ValueError:
            reset = None
        raise RateLimited(f"{prefix}rate limited (reset={reset})", reset=reset, **kw)
    if status == 401 or code in _DEAD_TOKEN_CODES:
        raise Unauthorized(f"{prefix}unauthorized (code={code}): {msg}", **kw)
    if code in _SUSPENDED_CODES:
        raise Suspended(f"{prefix}account restricted (code={code}): {msg}", **kw)
    if status == 403:
        raise Forbidden(f"{prefix}forbidden (code={code}): {msg}", **kw)
    if status == 404:
        raise NotFound(f"{prefix}not found (code={code}): {msg}", **kw)
    raise TwitterError(f"{prefix}HTTP {status} (code={code}): {msg}", **kw)


def raise_for_graphql(data: dict, op: str | None = None, resp=None) -> None:
    """Raise if a GraphQL response (HTTP 200) carries an ``errors[]`` array."""
    errs = data.get("errors") if isinstance(data, dict) else None
    if not errs:
        return
    e0 = errs[0] if isinstance(errs, list) and errs else {}
    code = e0.get("code") if isinstance(e0, dict) else None
    msg = e0.get("message") if isinstance(e0, dict) else str(errs)
    prefix = f"{op}: " if op else ""
    kw = {"status": getattr(resp, "status_code", None), "code": code, "response": resp}
    if code in _SUSPENDED_CODES:
        raise Suspended(f"{prefix}{msg}", **kw)
    if code in _DEAD_TOKEN_CODES:
        raise Unauthorized(f"{prefix}{msg}", **kw)
    raise TwitterError(f"{prefix}{op or 'graphql'} errors: {errs}", **kw)
