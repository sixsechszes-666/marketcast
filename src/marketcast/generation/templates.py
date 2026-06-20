"""Pull real, high-performing Polymarket tweets from X so the generator can
borrow a fresh *structure* (not the words) each run, instead of producing
near-identical posts.

Two streams are available (one X client per thread, curl_cffi sessions are not
shareable). A ``source`` switch picks which run:
  * SEARCH   — advanced keyword search: Polymarket + hype keywords, min_faves,
               last N days. Structures from the whole Polymarket conversation,
               not a fixed handful of accounts. (DEFAULT.)
  * ACCOUNTS — recent posts from the curated reference accounts (REF_ACCOUNTS).
  * BOTH     — run both in parallel and merge.
Select with the ``source=`` argument, the ``TEMPLATE_SOURCE`` env var, or the
caller's flag.

Everything fetched is saved under ``settings.data_dir`` (raw query meta + parsed
tweets) so the queries and the prompt can be tuned later.

Auth: reuses the existing X ``auth_token`` cookie via ``settings.auth_token`` /
``settings.proxy``.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from marketcast.config import settings

POST_DATA = settings.data_dir

# Reference accounts whose Polymarket posts we mine for structure (variant 1).
REF_ACCOUNTS = ["PolymarketStory", "mahera777"]

# Hype keywords for the advanced search (variant 2) — mirrors the search URL.
SEARCH_KEYWORDS = '(screenshot OR chart OR mover OR volume OR whale OR "smart money" OR profit OR odds)'

# Where post structures are mined from. "search" = advanced keyword search only
# (the default — the whole Polymarket conversation, not a fixed account list);
# "accounts" = REF_ACCOUNTS only; "both" = merge the two.
_VALID_SOURCES = ("search", "accounts", "both")
DEFAULT_SOURCE = "search"


def _resolve_source(source: str) -> str:
    """Pick the effective source. The ``TEMPLATE_SOURCE`` env var overrides the
    argument when set to a valid value; otherwise the argument is used; an
    unknown value falls back to the default."""
    env = (os.getenv("TEMPLATE_SOURCE") or "").strip().lower()
    if env in _VALID_SOURCES:
        return env
    return source if source in _VALID_SOURCES else DEFAULT_SOURCE


def _load_creds() -> tuple[str, str | None]:
    """Return ``(auth_token, proxy)`` from configured settings."""
    token = (settings.auth_token or "").strip()
    proxy = (settings.proxy or "").strip() or None
    return token, proxy


def _since(days: int) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _is_transient(e: Exception) -> bool:
    """True for the intermittent TLS/connection resets x.com throws (curl 35),
    which succeed on a fresh attempt — worth retrying."""
    s = str(e).lower()
    return ("curl: (35)" in s or "connection was reset" in s or "recv failure" in s
            or "tls connect error" in s or "curl: (56)" in s or "timed out" in s)


def _x_attempts(default: int = 4) -> int:
    """Retry count for transient X resets. Raise X_RETRIES on a flaky network."""
    try:
        return min(max(int(os.getenv("X_RETRIES", str(default))), 1), 10)
    except ValueError:
        return default


def _x_retry(fn, *args, attempts=None, label="", **kwargs):
    """Call an X request, retrying transient curl-35 resets with backoff. ~17% of
    requests reset, so 4 tries drops the failure odds to ~0.1% (tune with X_RETRIES).
    Re-raises the last error if every attempt is a NON-transient failure or all
    transient ones fail."""
    if attempts is None:
        attempts = _x_attempts()
    dbg = os.getenv("X_DEBUG", "").strip().lower() in ("1", "true", "yes")
    tag = f"{label} " if label else ""
    last = None
    for i in range(max(1, attempts)):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if not _is_transient(e):
                if dbg:
                    print(f"[X]: {tag}non-transient error: {e!r}")
                raise
            if dbg:
                print(f"[X]: {tag}transient (try {i + 1}/{attempts}): {e}")
            if i < attempts - 1:
                time.sleep(0.8 * (i + 1))            # 0.8s, 1.6s, 2.4s
    # full (untruncated) error when we finally give up, for diagnosis
    print(f"[X]: {tag}gave up after {attempts} tries — {last}")
    raise last


def _new_client(token: str, proxy: str | None, retries=None):
    """X client factory. Does a FRESH ct0 warmup handshake every time — exactly the
    behaviour that worked 100% before the multi-mode rework. (An earlier version
    cached/reused the session and skipped the handshake; firing a cold GraphQL call
    without that warmup correlated with curl-35 resets, so reuse was removed.) The
    only addition over the original is a transient-reset retry with backoff."""
    # lazy import: xclient is a sibling layer ported by another agent; importing it
    # here keeps this module importable standalone (e.g. for the text helpers / tests).
    from marketcast.xclient import TwitterClient

    if retries is None:
        retries = _x_attempts()
    last = None
    for i in range(max(1, retries)):
        try:
            c = TwitterClient(auth_token=token, proxy=proxy)
            c.ensure_ct0()                          # real warmup GET to x.com, like the old code
            return c
        except Exception as e:
            last = e
            if not _is_transient(e):
                raise
            if i < retries - 1:
                time.sleep(0.8 * (i + 1))           # 0.8s, 1.6s, 2.4s backoff
    print(f"[X]: client init gave up after {retries} tries — {last}")
    raise last if last else RuntimeError("could not init X client")


def _fetch_search(token, proxy, raw_query, count):
    """Advanced-search stream. X currently gates SearchTimeline behind extra
    anti-bot checks, so this often 404s — we degrade gracefully."""
    from marketcast.xclient import TwitterReader

    try:
        reader = TwitterReader(_new_client(token, proxy))
        return _x_retry(reader.search, raw_query, count=count, product="Top", skip_retweets=True)
    except Exception as e:
        print(f"[X]: search stream unavailable ({str(e)[:80]})")
        return []


def _fetch_accounts(token, proxy, accounts, per_account, days, min_faves):
    """Reference-accounts stream via UserTweets. Works today."""
    from marketcast.xclient import TwitterReader

    out = []
    try:
        reader = TwitterReader(_new_client(token, proxy))
    except Exception as e:
        print(f"[X]: accounts stream client failed ({str(e)[:80]})")
        return out
    cutoff = days * 86400
    for handle in accounts:
        try:
            tweets = _x_retry(reader.get_user_tweets, screen_name=handle,
                              count=per_account, skip_retweets=True)
        except Exception as e:
            print(f"[X]: @{handle} skipped ({str(e)[:60]})")
            continue
        time.sleep(0.4)                              # gentle pacing between accounts
        for t in tweets:
            age = t.get("age_seconds")
            if age is not None and age > cutoff:
                continue
            if (t.get("likes") or 0) < min_faves:
                continue
            out.append(t)
    return out


# ── tweet quality / dedupe ───────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")


def _strip_for_len(text: str) -> str:
    return _WS_RE.sub(" ", _URL_RE.sub("", text or "")).strip()


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", _URL_RE.sub("", (text or "").lower())).strip()


def _is_usable(t: dict) -> bool:
    body = _strip_for_len(t.get("text"))
    if len(body) < 40:                      # too short to carry a structure
        return False
    if body.count("@") >= 4:                # reply pile / mention spam
        return False
    return True


def _dedupe(tweets):
    seen, out = set(), []
    for t in tweets:
        key = _norm(t.get("text"))[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _slim(t: dict, source: str) -> dict:
    return {
        "text": t.get("text"),
        "author": t.get("author"),
        "likes": t.get("likes"),
        "views": t.get("views"),
        "views_per_hour": t.get("views_per_hour"),
        "url": t.get("url"),
        "source": source,
    }


def fetch_templates(kind=None, *, source=DEFAULT_SOURCE, days=2, min_faves=30,
                    per_query=30, account_days=7, account_min_faves=15,
                    per_account=40, keep=12):
    """Fetch + rank real Polymarket tweets to use as structure references.

    source: 'search' (advanced keyword search only, the default), 'accounts'
        (REF_ACCOUNTS only), or 'both' (run both in parallel and merge). The
        ``TEMPLATE_SOURCE`` env var overrides this when set.
    kind: 'trader' | 'event' | None — lightly biases the search keywords.
    Returns a list of slim tweet dicts (best first). Always saves what it got to
    ``settings.data_dir``. Returns [] (never raises) so callers can fall back.
    """
    token, proxy = _load_creds()
    if not token:
        print("[X]: no AUTH_TOKEN found (settings.auth_token) — skipping tweet templates")
        return []

    source = _resolve_source(source)
    run_search = source in ("search", "both")
    run_accounts = source in ("accounts", "both")

    since = _since(days)
    extra = ""
    if kind == "trader":
        extra = " (trader OR whale OR wallet OR profit)"
    elif kind == "event":
        extra = " (odds OR market OR resolve OR volume)"
    search_q = (
        f"Polymarket {SEARCH_KEYWORDS}{extra} "
        f"min_faves:{min_faves} since:{since} lang:en -filter:replies"
    )

    # only spin up the stream(s) the chosen source needs
    results = {"accounts": [], "search": []}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {}
        if run_accounts:
            futs["accounts"] = ex.submit(_fetch_accounts, token, proxy, REF_ACCOUNTS,
                                         per_account, account_days, account_min_faves)
        if run_search:
            futs["search"] = ex.submit(_fetch_search, token, proxy, search_q, per_query)
        for name, fut in futs.items():
            results[name] = fut.result()

    # merge, drop junk, dedupe
    merged = []
    for src, tweets in results.items():
        for t in tweets:
            if _is_usable(t):
                merged.append(_slim(t, src))
    merged = _dedupe(merged)

    # rank: likes first, then velocity
    merged.sort(key=lambda t: (t.get("likes") or 0, t.get("views_per_hour") or 0), reverse=True)
    top = merged[:keep]

    _save({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "source": source,
        "since": since,
        "accounts": REF_ACCOUNTS if run_accounts else [],
        "search_query": search_q if run_search else None,
        "counts": {src: len(v) for src, v in results.items()},
        "kept": len(top),
        "templates": top,
    }, prefix="templates")

    print(f"[X]: templates [{source}] — accounts={len(results['accounts'])} "
          f"search={len(results['search'])} -> kept {len(top)}")
    return top


def _save(obj, *, prefix):
    try:
        POST_DATA.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = POST_DATA / f"{prefix}_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return path
    except Exception as e:
        print(f"[X]: could not save {prefix}: {e}")
        return None
