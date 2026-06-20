"""Self-healing GraphQL query_id resolver.

X rotates the per-operation query_ids embedded in its web bundle every week or
two, which makes hardcoded ids 404. This module keeps last-known-good defaults,
caches discovered ids to disk, and can re-scrape the current ids from x.com's
main.<hash>.js on demand (reader.py calls refresh() automatically on a 404).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

_CACHE = Path(__file__).with_name("query_ids_cache.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# Operations we use + last-known-good ids (scraped from main.js on 2026-05-24).
DEFAULTS = {
    "ListLatestTweetsTimeline": "us9jUxCKI9355JaG6q7sVw",
    "BlueVerifiedFollowers": "mtFzhZWVIMar04RMPbXn0Q",
    "UserByScreenName": "IGgvgiOx4QZndDHuD3x9TQ",
    "HomeTimeline": "7zlnp2TxC044W4C1ZUJMHw",
    "SearchTimeline": "099UqLkXma7fhT81Jv4n9g",
    "UserTweets": "3AS73VJOTCg8ePuvJndFew",
    # No hardcoded id (rotates): refresh() scrapes these from main.js on first
    # use / on a 404. Reads:
    "Followers": "",
    "Following": "",
    "TweetDetail": "",
    "UserByRestId": "",
    "Likes": "",
    "ListMembers": "",
    "ExploreSidebar": "",
    # Writes (GraphQL mutations):
    "CreateTweet": "",
    "DeleteTweet": "",
    "FavoriteTweet": "",
    "UnfavoriteTweet": "",
    "CreateRetweet": "",
    "DeleteRetweet": "",
    "CreateBookmark": "",
    "DeleteBookmark": "",
    "CreateList": "",
    "ListAddMember": "",
    "ListRemoveMember": "",
    "DeleteList": "",
}


def _load() -> dict:
    ids = dict(DEFAULTS)
    try:
        if _CACHE.exists():
            cached = json.loads(_CACHE.read_text(encoding="utf-8")).get("ids", {})
            ids.update({k: v for k, v in cached.items() if v})
    except Exception:
        pass
    return ids


IDS = _load()


def get(name: str) -> str | None:
    """Return the current query_id for an operation name (or None)."""
    return IDS.get(name)


def refresh(session, impersonate: str = "chrome124", timeout: int = 40) -> dict:
    """Scrape current query_ids from x.com's main bundle. Updates IDS + cache."""
    try:
        html = session.get(
            "https://x.com/home", headers={"user-agent": UA},
            impersonate=impersonate, timeout=timeout,
        ).text
        m = re.search(
            r"https://abs\.twimg\.com/responsive-web/client-web/main\.[0-9a-f]+\.js",
            html,
        )
        if not m:
            return IDS
        js = session.get(
            m.group(0), headers={"user-agent": UA},
            impersonate=impersonate, timeout=timeout,
        ).text
        found = {}
        for op in DEFAULTS:
            mm = (re.search(r'queryId:"([a-zA-Z0-9_-]+)",operationName:"' + op + '"', js)
                  or re.search(r'operationName:"' + op + r'"[^}]*?queryId:"([a-zA-Z0-9_-]+)"', js))
            if mm:
                found[op] = mm.group(1)
        if found:
            IDS.update(found)
            try:
                _CACHE.write_text(
                    json.dumps({"ids": IDS, "updated": time.time()}, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
    except Exception:
        pass
    return IDS
