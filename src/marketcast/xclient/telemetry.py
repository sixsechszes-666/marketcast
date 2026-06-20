"""Telemetry — emulates x.com client-side "scribe" events (jot/client_event).

The real web client constantly fires batches of client_event logs: tweet
impressions, tweet opens, profile visits, video views. An account that only ever
sends mutations and zero telemetry looks robotic — these events make the activity
pattern look more believable.

IMPORTANT / HONEST:
  * The benefit to "account health" is plausible but UNPROVEN and not guaranteed.
  * Broken/inconsistent telemetry can HARM (events for things you didn't do;
    future timestamps; a wrong namespace — all bot tells in their own right).
  * So tie events to REAL actions: send impressions for tweets you actually
    fetched via search/get_home_timeline/get_tweet.

The jot/client_event.json endpoint requires x-client-transaction-id (404 without
it) — this module adds it itself. The endpoint replies with an empty 200 body.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote, urlparse

from . import transaction
from .client import TwitterClient

JOT = "https://x.com/i/api/1.1/jot/client_event.json"
PREROLLS = "https://x.com/i/api/1.1/videoads/v2/prerolls.json"
BADGE = "https://x.com/i/api/2/badge_count/badge_count.json?supports_ntab_urt=1&include_xchat_count=1"
FLEETLINE = "https://x.com/i/api/fleets/v1/fleetline?only_spaces=true"
CLIENT_APP_ID = "3033300"  # web client
ITEM_TWEET = 0
ITEM_USER = 3


def _ms() -> int:
    return int(time.time() * 1000)


class Telemetry:
    """A client_event telemetry layer over :class:`TwitterClient`."""

    def __init__(self, client: TwitterClient) -> None:
        self.client = client
        self._seq = 0
        self._gen = None

    # ---- low-level ----

    def _txid(self) -> str:
        if self._gen is None:
            self._gen = transaction.get_generator(self.client.session, self.client.impersonate)
        return self._gen.generate("POST", urlparse(JOT).path)

    def event(self, namespace: dict, items: list | None = None, **extra) -> dict:
        """Build one client_event. namespace: {page, section, component, element,
        action, client}. items: a list of entities (tweets/users)."""
        self._seq += 1
        ev = {
            "_category_": "client_event",
            "format_version": 2,
            "triggered_on": _ms(),
            "items": items or [],
            "event_namespace": {"client": "m5", **namespace},
            "client_event_sequence_start_timestamp": _ms() - 500,
            "client_event_sequence_number": self._seq,
            "client_app_id": CLIENT_APP_ID,
        }
        ev.update(extra)
        return ev

    def scribe(self, events: list[dict]) -> bool:
        """Send a list of client_event entries to jot/client_event.json. 200 = accepted."""
        if not events:
            return True
        body = "debug=true&log=" + quote(json.dumps(events, separators=(",", ":")))
        resp = self.client.request(
            "POST", JOT, data=body, referer="https://x.com/home",
            extra_headers={"x-client-transaction-id": self._txid()},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"scribe {resp.status_code}: {resp.text[:200]}")
        return True

    # ---- high-level helpers (tied to real entities) ----

    @staticmethod
    def _tweet_item(tw, position: int | None = None) -> dict:
        """tw — a tweet dict (from the reader) or a string id."""
        if isinstance(tw, dict):
            item = {"item_type": ITEM_TWEET, "id": str(tw["id"])}
            if tw.get("author_id"):
                item["author_id"] = str(tw["author_id"])
        else:
            item = {"item_type": ITEM_TWEET, "id": str(tw)}
        if position is not None:
            item["position"] = position
        return item

    def impressions(
        self, tweets: list, *, page: str = "home", section: str = "home",
        component: str = "stream",
    ) -> bool:
        """Log impressions of tweets you actually fetched (one batch). tweets — a
        list of tweet dicts or ids."""
        items = [self._tweet_item(tw, i) for i, tw in enumerate(tweets)]
        if not items:
            return True
        ev = self.event(
            {"page": page, "section": section, "component": component,
             "element": "tweet", "action": "impression"},
            items=items,
        )
        return self.scribe([ev])

    def open_tweet(self, tweet, *, page: str = "home") -> bool:
        """Tweet open (navigation into the detail view)."""
        ev = self.event(
            {"page": page, "section": "tweet", "component": "tweet",
             "element": "tweet", "action": "open"},
            items=[self._tweet_item(tweet)],
        )
        return self.scribe([ev])

    def profile_visit(self, user_id: str) -> bool:
        """Visit to a user's profile."""
        ev = self.event(
            {"page": "profile", "section": "profile", "component": "user",
             "element": "user", "action": "profile_click"},
            items=[{"item_type": ITEM_USER, "id": str(user_id)}],
        )
        return self.scribe([ev])

    # ---- background polls (like an open idle tab) ----

    def badge_count(self) -> dict:
        """Poll unread counters (the client hits this periodically)."""
        r = self.client.get(BADGE, referer="https://x.com/home")
        return r.json() if r.status_code < 400 else {}

    def fleetline(self) -> dict:
        """Poll fleetline (live Spaces). Returns refresh_delay_secs and threads."""
        r = self.client.get(FLEETLINE, referer="https://x.com/home")
        return r.json() if r.status_code < 400 else {}

    def heartbeat(self) -> dict:
        """One background "tick" of an open idle tab: poll unread + fleetline. Call
        ~every 30s while idle to keep the session looking alive."""
        return {"badge": self.badge_count(), "fleetline": self.fleetline()}

    def video_preroll(
        self, tweet_ids: list, *, display_location: str = "TIMELINE_HOME"
    ) -> int:
        """Video-ad preroll check for a video tweet (like a player before start).

        The browser usually batches every visible video tweet here; it often goes
        through a service worker (202 CachedForSync). Format: a single form field
        `tweets` whose value is the whole JSON object. Returns the HTTP status
        (does not raise). tweet_ids — ids of video tweets you actually saw.
        """
        ids = [str(t["id"] if isinstance(t, dict) else t) for t in tweet_ids]
        if not ids:
            return 0
        payload = {
            "tweets": [{"tweet_id": i} for i in ids],
            "display_location": display_location,
        }
        body = {"tweets": json.dumps(payload, separators=(",", ":"))}
        resp = self.client.request(
            "POST", PREROLLS, data=body, referer="https://x.com/home",
            extra_headers={"x-client-transaction-id": self._txid()},
        )
        return resp.status_code
