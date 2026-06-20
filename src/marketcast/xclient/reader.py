"""TwitterReader — all read methods (timelines, search, followers, etc.).

Takes a TwitterClient (transport/auth). Returns either the raw response or
parsed tweets — your choice.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote

from . import errors, features, query_ids
from .client import TwitterClient
from .parsers import (
    extract_bottom_cursor,
    extract_followers_cursor,
    extract_list_members_cursor,
    extract_user_tweets_cursor,
    parse_followers_response,
    parse_home_response,
    parse_list_members_response,
    parse_list_response,
    parse_search_response,
    parse_tweet_detail_response,
    parse_user_result,
    parse_user_tweets_response,
    parse_users_list_v11,
)

GRAPHQL = "https://x.com/i/api/graphql"


def _gql_url(name: str) -> str:
    qid = query_ids.get(name)
    return f"{GRAPHQL}/{qid}/{name}"


def _encode(obj: dict) -> str:
    return quote(json.dumps(obj, separators=(",", ":")), safe="")


class TwitterReader:
    """All read operations over a :class:`TwitterClient`."""

    def __init__(self, client: TwitterClient) -> None:
        self.client = client

    # ---- Lists ----

    def get_list_timeline_raw(self, list_id: str, count: int = 20) -> dict:
        """Raw ListLatestTweetsTimeline response for a list."""
        variables = {"listId": list_id, "count": count}
        url = (
            f"{_gql_url('ListLatestTweetsTimeline')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.LIST_LATEST_TWEETS_TIMELINE)}"
        )
        resp = self.client.get(url, referer=f"https://x.com/i/lists/{list_id}")
        errors.raise_for_status(resp)
        return resp.json()

    def get_list_timeline(
        self, list_id: str, count: int = 20, *, skip_retweets: bool = True
    ) -> list[dict]:
        """Parsed tweets in a list."""
        data = self.get_list_timeline_raw(list_id, count)
        return parse_list_response(data, skip_retweets=skip_retweets)

    # ---- Users ----

    def get_user_by_screen_name_raw(self, screen_name: str) -> dict:
        """Raw UserByScreenName response."""
        variables = {"screen_name": screen_name}
        url = (
            f"{_gql_url('UserByScreenName')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.USER_BY_SCREEN_NAME)}"
            f"&fieldToggles={_encode(features.USER_BY_SCREEN_NAME_FIELD_TOGGLES)}"
        )
        resp = self.client.get(url, referer=f"https://x.com/{screen_name}")
        errors.raise_for_status(resp)
        return resp.json()

    def resolve_user_id(self, screen_name: str) -> str:
        """screen_name -> rest_id."""
        data = self.get_user_by_screen_name_raw(screen_name)
        result = data.get("data", {}).get("user", {}).get("result") or {}
        user_id = result.get("rest_id")
        if not user_id:
            raise RuntimeError(f"Failed to resolve user_id for @{screen_name}")
        return user_id

    def get_user(self, screen_name: str) -> dict | None:
        """Parsed profile by @screen_name."""
        return parse_user_result(self.get_user_by_screen_name_raw(screen_name))

    def get_user_by_id_raw(self, user_id: str, _retried: bool = False) -> dict:
        """UserByRestId. Self-heals query_id on a 404."""
        if not query_ids.get("UserByRestId"):
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
        variables = {"userId": user_id, "withGrokTranslatedBio": True}
        url = (
            f"{_gql_url('UserByRestId')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.USER_BY_SCREEN_NAME)}"
        )
        resp = self.client.get(url, referer="https://x.com/")
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_user_by_id_raw(user_id, _retried=True)
        errors.raise_for_status(resp)
        return resp.json()

    def get_user_by_id(self, user_id: str) -> dict | None:
        """Parsed profile by numeric user_id."""
        return parse_user_result(self.get_user_by_id_raw(user_id))

    # ---- Followers ----

    def get_blue_verified_followers_raw(
        self,
        user_id: str | None = None,
        count: int = 20,
        *,
        screen_name: str | None = None,
    ) -> dict:
        """Raw BlueVerifiedFollowers response."""
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withGrokTranslatedBio": True,
        }
        url = (
            f"{_gql_url('BlueVerifiedFollowers')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.BLUE_VERIFIED_FOLLOWERS)}"
        )
        referer = (
            f"https://x.com/{screen_name}/verified_followers"
            if screen_name
            else "https://x.com/"
        )
        resp = self.client.get(url, referer=referer)
        errors.raise_for_status(resp)
        return resp.json()

    def get_blue_verified_followers(
        self,
        user_id: str | None = None,
        count: int = 20,
        *,
        screen_name: str | None = None,
    ) -> list[dict]:
        """Parsed verified-only followers."""
        data = self.get_blue_verified_followers_raw(
            user_id, count, screen_name=screen_name
        )
        return parse_followers_response(data)

    def get_followers_raw(
        self,
        user_id: str | None = None,
        count: int = 20,
        *,
        screen_name: str | None = None,
        cursor: str | None = None,
        _retried: bool = False,
    ) -> dict:
        """A profile's regular (all) followers. Self-heals query_id on a 404.

        Followers has no hardcoded query_id — we fetch the current one from the
        x.com bundle (refresh) on first use and rebuild on a 404.
        """
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        if not query_ids.get("Followers"):
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withGrokTranslatedBio": True,
        }
        if cursor:
            variables["cursor"] = cursor
        url = (
            f"{_gql_url('Followers')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.FOLLOWERS)}"
        )
        referer = (
            f"https://x.com/{screen_name}/followers"
            if screen_name
            else "https://x.com/"
        )
        resp = self.client.get(url, referer=referer)
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_followers_raw(
                user_id, count, screen_name=screen_name, cursor=cursor, _retried=True
            )
        errors.raise_for_status(resp)
        return resp.json()

    def get_followers(
        self,
        user_id: str | None = None,
        count: int = 20,
        *,
        screen_name: str | None = None,
        cursor: str | None = None,
        pages: int = 1,
    ) -> list[dict]:
        """List of followers. pages>1 scrolls by the bottom cursor and
        concatenates pages (deduped by id), stopping when a page adds nothing or
        the cursor stops moving."""
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        data = self.get_followers_raw(user_id, count, cursor=cursor)
        users = parse_followers_response(data)
        if pages <= 1:
            return users
        seen = {u["id"] for u in users}
        cur = extract_followers_cursor(data)
        fetched = 1
        while cur and fetched < pages:
            fetched += 1
            data = self.get_followers_raw(user_id, count, cursor=cur)
            new = [u for u in parse_followers_response(data) if u["id"] not in seen]
            if not new:
                break
            seen.update(u["id"] for u in new)
            users.extend(new)
            nxt = extract_followers_cursor(data)
            if not nxt or nxt == cur:   # no further progress
                break
            cur = nxt
        return users

    def _page_with_limit(self, fetch, *, wait_on_limit: bool, max_wait: int, on_wait) -> dict:
        """Run fetch() with 429 handling.

        The followers/following endpoints are limited (~50 requests per 15-min
        window). On a 429, if wait_on_limit=True, wait for the window to reset
        (x-rate-limit-reset, but no longer than max_wait) and retry the same call.
        """
        while True:
            try:
                return fetch()
            except Exception:
                resp = self.client.last_response
                if not resp or resp.status_code != 429 or not wait_on_limit:
                    raise
                reset = resp.headers.get("x-rate-limit-reset")
                try:
                    wait = int(reset) - int(time.time()) + 3 if reset else 60
                except ValueError:
                    wait = 60
                wait = max(1, min(wait, max_wait))
                if on_wait:
                    on_wait(wait)
                time.sleep(wait)

    def _collect_all_users(
        self,
        raw_fetch,
        *,
        count: int,
        max_pages: int,
        on_page,
        wait_on_limit: bool,
        max_wait: int,
        on_wait,
    ) -> list[dict]:
        """Scroll a user timeline (followers/following) to the end by cursor.

        raw_fetch(count, cursor) -> raw dict. Deduped by id; stop on an empty page
        or a frozen cursor."""
        def fetch(cursor):
            return self._page_with_limit(
                lambda: raw_fetch(count, cursor),
                wait_on_limit=wait_on_limit, max_wait=max_wait, on_wait=on_wait,
            )

        data = fetch(None)
        users = parse_followers_response(data)
        seen = {u["id"] for u in users}
        if on_page:
            on_page(users, len(users))
        cur = extract_followers_cursor(data)
        pages = 1
        while cur and pages < max_pages:
            pages += 1
            data = fetch(cur)
            raw = parse_followers_response(data)
            new = [u for u in raw if u["id"] not in seen]
            if new:
                seen.update(u["id"] for u in new)
                users.extend(new)
                if on_page:
                    on_page(new, len(users))
            nxt = extract_followers_cursor(data)
            # Stop only when X returns an empty page or the cursor stops moving —
            # a page of pure duplicates is not itself a reason to stop (pages can
            # overlap).
            if not raw or not nxt or nxt == cur:
                break
            cur = nxt
        return users

    def get_all_followers(
        self,
        user_id: str | None = None,
        *,
        screen_name: str | None = None,
        count: int = 100,
        max_pages: int = 1000,
        on_page=None,
        wait_on_limit: bool = True,
        max_wait: int = 960,
        on_wait=None,
    ) -> list[dict]:
        """Scrape ALL of a profile's followers: scrolls the timeline by the bottom
        cursor to the end (or until max_pages — a guard against infinite loops).

        count — page size (X returns up to ~100 at a time, ~50 in practice).
        on_page(new_users, total) — callback after each page (progress).
        wait_on_limit — on a 429, wait for the rate-limit window to reset and
        continue (the endpoint is limited to ~50 requests / 15 min). max_wait — the
        cap on a single wait in seconds. on_wait(seconds) — callback before waiting.
        Note: for private/deleted/banned accounts X returns fewer than
        followers_count, so the total is usually a bit below the profile number.
        """
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        return self._collect_all_users(
            lambda c, cur: self.get_followers_raw(user_id, c, cursor=cur),
            count=count, max_pages=max_pages, on_page=on_page,
            wait_on_limit=wait_on_limit, max_wait=max_wait, on_wait=on_wait,
        )

    # ---- Following ----

    def get_following_raw(
        self,
        user_id: str | None = None,
        count: int = 20,
        *,
        screen_name: str | None = None,
        cursor: str | None = None,
        _retried: bool = False,
    ) -> dict:
        """Who a profile follows (Following). Self-heals query_id on a 404."""
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        if not query_ids.get("Following"):
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withGrokTranslatedBio": True,
        }
        if cursor:
            variables["cursor"] = cursor
        url = (
            f"{_gql_url('Following')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.FOLLOWERS)}"
        )
        referer = (
            f"https://x.com/{screen_name}/following"
            if screen_name
            else "https://x.com/"
        )
        resp = self.client.get(url, referer=referer)
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_following_raw(
                user_id, count, screen_name=screen_name, cursor=cursor, _retried=True
            )
        errors.raise_for_status(resp)
        return resp.json()

    def get_following(
        self,
        user_id: str | None = None,
        count: int = 20,
        *,
        screen_name: str | None = None,
        cursor: str | None = None,
    ) -> list[dict]:
        """One page of a profile's "following" (who they follow)."""
        data = self.get_following_raw(
            user_id, count, screen_name=screen_name, cursor=cursor
        )
        return parse_followers_response(data)

    def get_all_following(
        self,
        user_id: str | None = None,
        *,
        screen_name: str | None = None,
        count: int = 100,
        max_pages: int = 1000,
        on_page=None,
        wait_on_limit: bool = True,
        max_wait: int = 960,
        on_wait=None,
    ) -> list[dict]:
        """Scrape EVERYONE a profile follows. See get_all_followers."""
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        return self._collect_all_users(
            lambda c, cur: self.get_following_raw(user_id, c, cursor=cur),
            count=count, max_pages=max_pages, on_page=on_page,
            wait_on_limit=wait_on_limit, max_wait=max_wait, on_wait=on_wait,
        )

    # ---- Home timeline ----

    def get_home_timeline_raw(
        self,
        count: int = 20,
        *,
        seen_tweet_ids: list[str] | None = None,
    ) -> dict:
        """HomeTimeline — a POST with a JSON body (variables/features/queryId)."""
        body = {
            "variables": {
                "count": count,
                "includePromotedContent": True,
                "requestContext": "launch",
                "withCommunity": True,
                "seenTweetIds": seen_tweet_ids or [],
            },
            "features": features.HOME_TIMELINE,
            "queryId": query_ids.get("HomeTimeline"),
        }
        resp = self.client.post(
            _gql_url("HomeTimeline"),
            json_body=body,
            referer="https://x.com/home",
        )
        errors.raise_for_status(resp)
        return resp.json()

    def get_home_timeline(
        self,
        count: int = 20,
        *,
        seen_tweet_ids: list[str] | None = None,
        skip_retweets: bool = True,
    ) -> list[dict]:
        """Parsed home ("For you"/following) timeline."""
        data = self.get_home_timeline_raw(count, seen_tweet_ids=seen_tweet_ids)
        return parse_home_response(data, skip_retweets=skip_retweets)

    # ---- Search ----

    def get_search_raw(
        self,
        raw_query: str,
        count: int = 20,
        *,
        product: str = "Latest",
        cursor: str | None = None,
        _retried: bool = False,
    ) -> dict:
        """SearchTimeline. product: Latest | Top | People | Photos | Videos.

        On a 404 (X rotated the query_id) we re-scrape the current id from the
        web bundle once and retry, so the call self-heals.
        """
        variables = {
            "rawQuery": raw_query,
            "count": count,
            "querySource": "typed_query",
            "product": product,
            "withGrokTranslatedBio": False,
            "withQuickPromoteEligibilityTweetFields": False,
        }
        if cursor:
            variables["cursor"] = cursor
        url = (
            f"{_gql_url('SearchTimeline')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.SEARCH_TIMELINE)}"
        )
        from urllib.parse import quote as _q
        referer = (
            f"https://x.com/search?q={_q(raw_query)}"
            f"&src=typed_query&f={product.lower()}"
        )
        resp = self.client.get(url, referer=referer)
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_search_raw(
                raw_query, count, product=product, cursor=cursor, _retried=True
            )
        errors.raise_for_status(resp, "SearchTimeline")
        return resp.json()

    def search(
        self,
        raw_query: str,
        count: int = 20,
        *,
        product: str = "Latest",
        cursor: str | None = None,
        skip_retweets: bool = True,
        pages: int = 1,
    ) -> list[dict]:
        """Search tweets. With pages>1 it scrolls: follows the bottom cursor and
        concatenates the next pages (deduped by tweet id), stopping early when a
        page adds nothing or the cursor stops advancing."""
        data = self.get_search_raw(raw_query, count, product=product, cursor=cursor)
        tweets = parse_search_response(data, skip_retweets=skip_retweets)
        if pages <= 1:
            return tweets
        seen = {t["id"] for t in tweets}
        cur = extract_bottom_cursor(data)
        fetched = 1
        while cur and fetched < pages:
            fetched += 1
            data = self.get_search_raw(raw_query, count, product=product, cursor=cur)
            new = [t for t in parse_search_response(data, skip_retweets=skip_retweets)
                   if t["id"] not in seen]
            if not new:
                break
            seen.update(t["id"] for t in new)
            tweets.extend(new)
            nxt = extract_bottom_cursor(data)
            if not nxt or nxt == cur:   # no further progress
                break
            cur = nxt
        return tweets

    # ---- User tweets ----

    def get_user_tweets_raw(
        self,
        user_id: str | None = None,
        count: int = 40,
        *,
        screen_name: str | None = None,
        cursor: str | None = None,
        _retried: bool = False,
    ) -> dict:
        """UserTweets timeline. Self-heals query_id on a 404."""
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": False,
            "withVoice": True,
        }
        if cursor:
            variables["cursor"] = cursor
        url = (
            f"{_gql_url('UserTweets')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.SEARCH_TIMELINE)}"
            f"&fieldToggles={_encode({'withArticlePlainText': False})}"
        )
        referer = f"https://x.com/{screen_name}" if screen_name else "https://x.com/"
        resp = self.client.get(url, referer=referer)
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_user_tweets_raw(
                user_id, count, screen_name=screen_name, cursor=cursor, _retried=True
            )
        errors.raise_for_status(resp)
        return resp.json()

    def get_user_tweets(
        self,
        user_id: str | None = None,
        count: int = 40,
        *,
        screen_name: str | None = None,
        skip_retweets: bool = True,
    ) -> list[dict]:
        """Parsed tweets of a user."""
        data = self.get_user_tweets_raw(user_id, count, screen_name=screen_name)
        return parse_user_tweets_response(data, skip_retweets=skip_retweets)

    def _collect_tweets(self, raw_fetch, *, skip_retweets, max_pages, on_page) -> list[dict]:
        """Scroll a tweet timeline (UserTweets/Likes) by cursor to the end."""
        tweets, seen, cursor, pages = [], set(), None, 0
        while pages < max_pages:
            pages += 1
            data = raw_fetch(cursor)
            page = parse_user_tweets_response(data, skip_retweets=skip_retweets)
            new = [t for t in page if t["id"] not in seen]
            if new:
                seen.update(t["id"] for t in new)
                tweets.extend(new)
                if on_page:
                    on_page(new, len(tweets))
            nxt = extract_user_tweets_cursor(data)
            # empty tweet page or frozen cursor -> end
            if not page or not nxt or nxt == cursor:
                break
            cursor = nxt
        return tweets

    def get_all_user_tweets(
        self, user_id: str | None = None, *, screen_name: str | None = None,
        count: int = 100, skip_retweets: bool = True, max_pages: int = 1000, on_page=None,
    ) -> list[dict]:
        """ALL of a user's tweets (scroll by cursor). ~a few hundred most recent
        are available — X limits profile depth."""
        if not user_id:
            user_id = self.resolve_user_id(screen_name)
        return self._collect_tweets(
            lambda cur: self.get_user_tweets_raw(user_id, count, cursor=cur),
            skip_retweets=skip_retweets, max_pages=max_pages, on_page=on_page,
        )

    # ---- Tweet detail (thread/replies) ----

    def get_tweet_detail_raw(
        self, tweet_id: str, count: int = 40, *, cursor: str | None = None,
        _retried: bool = False,
    ) -> dict:
        """TweetDetail — the focal tweet + replies. Self-heals query_id on a 404."""
        if not query_ids.get("TweetDetail"):
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
        variables = {
            "focalTweetId": tweet_id,
            "with_rux_injections": False,
            "rankingMode": "Relevance",
            "includePromotedContent": False,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
        }
        if cursor:
            variables["cursor"] = cursor
        _toggles = {"withArticleRichContentState": True, "withArticlePlainText": False}
        url = (
            f"{_gql_url('TweetDetail')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.SEARCH_TIMELINE)}"
            f"&fieldToggles={_encode(_toggles)}"
        )
        resp = self.client.get(url, referer=f"https://x.com/i/status/{tweet_id}")
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_tweet_detail_raw(
                tweet_id, count, cursor=cursor, _retried=True
            )
        errors.raise_for_status(resp)
        return resp.json()

    def get_tweet(self, tweet_id: str, count: int = 40) -> dict:
        """``{'tweet': focal, 'replies': [...]}`` for a tweet id."""
        return parse_tweet_detail_response(self.get_tweet_detail_raw(tweet_id, count))

    # ---- Likes (a profile's liked tweets) ----

    def get_likes_raw(
        self,
        user_id: str | None = None,
        count: int = 40,
        *,
        screen_name: str | None = None,
        cursor: str | None = None,
        _retried: bool = False,
    ) -> dict:
        """Likes timeline — a profile's likes (only visible for your own account).
        Self-heals query_id on a 404."""
        if not user_id:
            if not screen_name:
                raise ValueError("provide user_id or screen_name")
            user_id = self.resolve_user_id(screen_name)
        if not query_ids.get("Likes"):
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
        variables = {
            "userId": user_id,
            "count": count,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withVoice": True,
        }
        if cursor:
            variables["cursor"] = cursor
        url = (
            f"{_gql_url('Likes')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.SEARCH_TIMELINE)}"
            f"&fieldToggles={_encode({'withArticlePlainText': False})}"
        )
        referer = f"https://x.com/{screen_name}/likes" if screen_name else "https://x.com/"
        resp = self.client.get(url, referer=referer)
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_likes_raw(
                user_id, count, screen_name=screen_name, cursor=cursor, _retried=True
            )
        errors.raise_for_status(resp)
        return resp.json()

    def get_likes(
        self,
        user_id: str | None = None,
        count: int = 40,
        *,
        screen_name: str | None = None,
        skip_retweets: bool = False,
    ) -> list[dict]:
        """A profile's liked tweets (timeline_v2 — same format as UserTweets)."""
        data = self.get_likes_raw(user_id, count, screen_name=screen_name)
        return parse_user_tweets_response(data, skip_retweets=skip_retweets)

    def get_all_likes(
        self, user_id: str | None = None, *, screen_name: str | None = None,
        count: int = 100, max_pages: int = 1000, on_page=None,
    ) -> list[dict]:
        """ALL of a profile's likes (scroll; only for your own account — likes are private)."""
        if not user_id:
            user_id = self.resolve_user_id(screen_name)
        return self._collect_tweets(
            lambda cur: self.get_likes_raw(user_id, count, cursor=cur),
            skip_retweets=False, max_pages=max_pages, on_page=on_page,
        )

    # ---- List members ----

    def get_list_members_raw(
        self, list_id: str, count: int = 100, *, cursor: str | None = None,
        _retried: bool = False,
    ) -> dict:
        """ListMembers — members of a list. Self-heals query_id on a 404."""
        if not query_ids.get("ListMembers"):
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
        variables = {"listId": list_id, "count": count, "withSafetyModeUserFields": True}
        if cursor:
            variables["cursor"] = cursor
        url = (
            f"{_gql_url('ListMembers')}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(features.SEARCH_TIMELINE)}"
        )
        resp = self.client.get(url, referer=f"https://x.com/i/lists/{list_id}/members")
        if resp.status_code == 404 and not _retried:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.get_list_members_raw(list_id, count, cursor=cursor, _retried=True)
        errors.raise_for_status(resp)
        return resp.json()

    def get_list_members(self, list_id: str, count: int = 100) -> list[dict]:
        """Members of a list (one page)."""
        return parse_list_members_response(self.get_list_members_raw(list_id, count))

    def get_all_list_members(
        self, list_id: str, *, count: int = 100, max_pages: int = 1000, on_page=None,
    ) -> list[dict]:
        """ALL members of a list (scroll by cursor)."""
        users, seen, cursor, pages = [], set(), None, 0
        while pages < max_pages:
            pages += 1
            data = self.get_list_members_raw(list_id, count, cursor=cursor)
            page = parse_list_members_response(data)
            new = [u for u in page if u["id"] not in seen]
            if new:
                seen.update(u["id"] for u in new)
                users.extend(new)
                if on_page:
                    on_page(new, len(users))
            nxt = extract_list_members_cursor(data)
            if not page or not nxt or nxt == cursor:
                break
            cursor = nxt
        return users

    # ---- DMs / notifications ----

    def get_dm_inbox(self) -> dict:
        """Initial DM state (conversations + recent events). Raw."""
        url = (
            "https://x.com/i/api/1.1/dm/inbox_initial_state.json"
            "?nsfw_filtering_enabled=false&include_ext_is_blue_verified=1"
            "&dm_secret_conversations_enabled=false&krs_registration_enabled=true"
            "&filter_low_quality=true&include_quality=all&dm_users=true"
        )
        resp = self.client.get(url, referer="https://x.com/messages")
        errors.raise_for_status(resp, "dm_inbox")
        return resp.json()

    def get_dm_conversation(self, conversation_id: str, count: int = 50) -> dict:
        """Messages of a specific conversation (id like '<a>-<b>'). Raw."""
        url = (
            f"https://x.com/i/api/1.1/dm/conversation/{conversation_id}.json"
            f"?context=FETCH_DM_CONVERSATION&include_ext_is_blue_verified=1&max_id=&count={count}"
        )
        resp = self.client.get(url, referer="https://x.com/messages")
        errors.raise_for_status(resp, "dm_conversation")
        return resp.json()

    def get_notifications(self, count: int = 40) -> dict:
        """Notifications feed (legacy 2/notifications/all.json). Raw globalObjects+timeline."""
        resp = self.client.get(
            f"https://x.com/i/api/2/notifications/all.json?count={count}",
            referer="https://x.com/notifications",
        )
        errors.raise_for_status(resp, "notifications")
        return resp.json()

    # ---- FAST followers/following via legacy REST 1.1 ----
    # friends/list.json (following) and followers/list.json: ~1000 req / 15min,
    # 200 users/page — an order of magnitude above GraphQL (~50 req). They return
    # legacy user objects. These endpoints do NOT return total_count (it'll be None).

    _V11_USER_FIELDS = (
        "include_profile_interstitial_type=1&include_blocking=1&include_blocked_by=1"
        "&include_followed_by=1&include_want_retweets=1&include_mute_edge=1"
        "&include_can_dm=1&include_can_media_tag=1&include_ext_is_blue_verified=1"
        "&include_ext_verified_type=1&include_ext_profile_image_shape=1&skip_status=1"
    )

    def _users_list_v11_raw(
        self, kind: str, user_id: str, count: int, cursor: str,
    ) -> dict:
        path = "friends/list.json" if kind == "following" else "followers/list.json"
        url = (
            f"https://x.com/i/api/1.1/{path}?{self._V11_USER_FIELDS}"
            f"&cursor={cursor}&user_id={user_id}&count={count}"
        )
        resp = self.client.get(url, referer="https://x.com/home")
        errors.raise_for_status(resp)
        return resp.json()

    def get_followers_v11(
        self, user_id: str | None = None, count: int = 200, *,
        screen_name: str | None = None, cursor: str = "-1",
    ) -> dict:
        """One page of followers via the fast REST 1.1 (~1000/15min). Returns
        ``{'users': [...], 'next_cursor': str, 'total_count': None}``."""
        if not user_id:
            user_id = self.resolve_user_id(screen_name)
        return parse_users_list_v11(
            self._users_list_v11_raw("followers", user_id, count, cursor)
        )

    def get_following_v11(
        self, user_id: str | None = None, count: int = 200, *,
        screen_name: str | None = None, cursor: str = "-1",
    ) -> dict:
        """One page of following via the fast REST 1.1 (~1000/15min). Returns
        ``{'users': [...], 'next_cursor': str, 'total_count': None}``."""
        if not user_id:
            user_id = self.resolve_user_id(screen_name)
        return parse_users_list_v11(
            self._users_list_v11_raw("following", user_id, count, cursor)
        )

    def _collect_all_v11(
        self, kind, user_id, count, max_pages, on_page,
    ) -> list[dict]:
        users, seen, cursor, pages = [], set(), "-1", 0
        while pages < max_pages:
            pages += 1
            page = parse_users_list_v11(
                self._users_list_v11_raw(kind, user_id, count, cursor)
            )
            new = [u for u in page["users"] if u["id"] not in seen]
            if new:
                seen.update(u["id"] for u in new)
                users.extend(new)
                if on_page:
                    on_page(new, len(users), page["total_count"])
            cursor = page["next_cursor"]
            if not page["users"] or cursor in ("0", "", None):
                break
        return users

    def get_all_followers_v11(
        self, user_id: str | None = None, *, screen_name: str | None = None,
        count: int = 200, max_pages: int = 10000, on_page=None,
    ) -> list[dict]:
        """ALL followers via the fast REST 1.1 (1000/15min — no long pauses).
        on_page(new, total, total_count)."""
        if not user_id:
            user_id = self.resolve_user_id(screen_name)
        return self._collect_all_v11("followers", user_id, count, max_pages, on_page)

    def get_all_following_v11(
        self, user_id: str | None = None, *, screen_name: str | None = None,
        count: int = 200, max_pages: int = 10000, on_page=None,
    ) -> list[dict]:
        """ALL following via the fast REST 1.1 (12000/15min). on_page(new, total, total_count)."""
        if not user_id:
            user_id = self.resolve_user_id(screen_name)
        return self._collect_all_v11("following", user_id, count, max_pages, on_page)

    # ---- Trends / notifications ----

    def get_trends(self) -> dict:
        """Explore sidebar trends (ExploreSidebar). Raw JSON."""
        return self.gql_get("ExploreSidebar", {}, feature_set=features.HOME_TIMELINE)

    def get_badge_count(self) -> dict:
        """Unread counters: ``{'ntab_unread_count', 'dm_unread_count', ...}``."""
        resp = self.client.get(
            "https://x.com/i/api/2/badge_count/badge_count.json"
            "?supports_ntab_urt=1&include_xchat_count=1",
            referer="https://x.com/home",
        )
        errors.raise_for_status(resp)
        return resp.json()

    # ---- Generic GraphQL escape hatch ----

    def gql_get(
        self,
        operation: str,
        variables: dict,
        *,
        feature_set: dict | None = None,
        field_toggles: dict | None = None,
        query_id: str | None = None,
        referer: str = "https://x.com/",
        _retried: bool = False,
    ) -> dict:
        """Call ANY x.com GraphQL read operation (GET).

        An escape hatch for operations without a dedicated method. query_id comes
        from the resolver (if the operation is in query_ids) or is passed
        explicitly — the id is easy to grab from DevTools (Network tab,
        .../graphql/<ID>/<Operation>). feature_set defaults to SEARCH_TIMELINE
        (broad, fits almost everything).
        """
        qid = query_id or query_ids.get(operation)
        if not qid:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            qid = query_ids.get(operation)
        if not qid:
            raise ValueError(
                f"no query_id for {operation}: pass query_id=... "
                f"(grab it from DevTools: .../graphql/<ID>/{operation})"
            )
        feats = features.SEARCH_TIMELINE if feature_set is None else feature_set
        url = (
            f"{GRAPHQL}/{qid}/{operation}"
            f"?variables={_encode(variables)}"
            f"&features={_encode(feats)}"
        )
        if field_toggles is not None:
            url += f"&fieldToggles={_encode(field_toggles)}"
        resp = self.client.get(url, referer=referer)
        if resp.status_code == 404 and not _retried and not query_id:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self.gql_get(
                operation, variables, feature_set=feature_set,
                field_toggles=field_toggles, referer=referer, _retried=True,
            )
        errors.raise_for_status(resp)
        return resp.json()
