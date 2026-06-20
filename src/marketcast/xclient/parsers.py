"""Parsers for x.com GraphQL responses.

Separation:
  * tweet-level: ``parse_tweet`` — turns a raw tweet result into a flat dict.
  * timeline-level: ``iter_list_tweets``, ``iter_home_tweets``, ... — walk the
    various timeline-response shapes and yield raw tweets.

Parsers are pure functions: they never make network requests.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

# ---- tweet ----

def _unwrap(tw: dict) -> dict:
    if tw.get("__typename") == "TweetWithVisibilityResults":
        return tw.get("tweet", {})
    return tw


def _best_video(legacy: dict) -> str | None:
    """Return the direct URL of the highest-bitrate mp4 in the tweet's media
    (native video / GIF), or None. Used by the POV pipeline to download the
    original viral clip straight from the source tweet."""
    media = (legacy.get("extended_entities") or {}).get("media") or []
    best_url, best_br = None, -1
    for m in media:
        if m.get("type") not in ("video", "animated_gif"):
            continue
        for v in ((m.get("video_info") or {}).get("variants") or []):
            if v.get("content_type") != "video/mp4" or not v.get("url"):
                continue
            br = v.get("bitrate") or 0          # GIF variants come with bitrate 0
            if br > best_br:
                best_br, best_url = br, v["url"]
    return best_url


def _basic_tweet(tweet: dict) -> dict | None:
    tweet = _unwrap(tweet)
    legacy = tweet.get("legacy") or {}
    note = tweet.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
    text = note.get("text") or legacy.get("full_text") or ""

    tweet_id = tweet.get("rest_id") or legacy.get("id_str")
    user_result = tweet.get("core", {}).get("user_results", {}).get("result", {})
    user_core = user_result.get("core") or {}
    user_legacy = user_result.get("legacy") or {}
    rel = user_result.get("relationship_perspectives") or {}
    screen_name = user_core.get("screen_name")
    if not tweet_id or not screen_name:
        return None

    views_raw = (tweet.get("views") or {}).get("count")
    views = int(views_raw) if views_raw and views_raw.isdigit() else None

    created_at = legacy.get("created_at")
    created_at_iso = None
    age_seconds = None
    if created_at:
        try:
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            created_at_iso = dt.isoformat()
            age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            pass

    views_per_hour = None
    if views and age_seconds and age_seconds > 0:
        views_per_hour = round(views / (age_seconds / 3600), 1)

    return {
        "id": tweet_id,
        "created_at": created_at,
        "created_at_iso": created_at_iso,
        "age_seconds": age_seconds,
        "views_per_hour": views_per_hour,
        "author": screen_name,
        "author_name": user_core.get("name"),
        "followers_count": user_legacy.get("followers_count"),
        "is_blue_verified": user_result.get("is_blue_verified"),
        "following": rel.get("following"),
        "followed_by": rel.get("followed_by"),
        "url": f"https://x.com/{screen_name}/status/{tweet_id}",
        "text": text,
        "likes": legacy.get("favorite_count"),
        "retweets": legacy.get("retweet_count"),
        "replies": legacy.get("reply_count"),
        "quotes": legacy.get("quote_count"),
        "bookmarks": legacy.get("bookmark_count"),
        "views": views,
        "video": _best_video(legacy),
    }


def parse_tweet(tweet: dict, *, skip_retweets: bool = True) -> dict | None:
    """Parse a tweet: text, author, metrics, quoted tweet. None for retweets."""
    legacy = tweet.get("legacy") or {}
    if skip_retweets and (
        legacy.get("retweeted_status_result")
        or (legacy.get("full_text") or "").startswith("RT @")
    ):
        return None

    info = _basic_tweet(tweet)
    if not info:
        return None

    info["is_quote"] = bool(legacy.get("is_quote_status"))
    quoted_raw = tweet.get("quoted_status_result", {}).get("result")
    info["quoted"] = _basic_tweet(quoted_raw) if (info["is_quote"] and quoted_raw) else None
    return info


# ---- timeline iterators ----

def _iter_timeline_entries(instructions: list) -> Iterator[dict]:
    for instr in instructions or []:
        yield from instr.get("entries", []) or []


def _entry_to_tweet(entry: dict) -> dict | None:
    content = entry.get("content", {})
    item = content.get("itemContent")
    if not item or item.get("itemType") != "TimelineTweet":
        return None
    result = item.get("tweet_results", {}).get("result")
    if not result:
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet", {})
    return result


def iter_list_tweets(data: dict) -> Iterator[dict]:
    """Raw tweets from ListLatestTweetsTimeline."""
    instructions = (
        data.get("data", {})
        .get("list", {})
        .get("tweets_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    for entry in _iter_timeline_entries(instructions):
        tw = _entry_to_tweet(entry)
        if tw:
            yield tw


def parse_list_response(data: dict, *, skip_retweets: bool = True) -> list[dict]:
    """A ready list of parsed tweets from a ListLatestTweetsTimeline response."""
    out = []
    for raw in iter_list_tweets(data):
        info = parse_tweet(raw, skip_retweets=skip_retweets)
        if info:
            out.append(info)
    return out


def iter_home_tweets(data: dict) -> Iterator[dict]:
    """Raw tweets from HomeTimeline."""
    instructions = (
        data.get("data", {})
        .get("home", {})
        .get("home_timeline_urt", {})
        .get("instructions", [])
    )
    for entry in _iter_timeline_entries(instructions):
        tw = _entry_to_tweet(entry)
        if tw:
            yield tw


def parse_home_response(data: dict, *, skip_retweets: bool = True) -> list[dict]:
    """A ready list of parsed tweets from a HomeTimeline response."""
    out = []
    for raw in iter_home_tweets(data):
        info = parse_tweet(raw, skip_retweets=skip_retweets)
        if info:
            out.append(info)
    return out


def iter_search_tweets(data: dict) -> Iterator[dict]:
    """Raw tweets from SearchTimeline."""
    instructions = (
        data.get("data", {})
        .get("search_by_raw_query", {})
        .get("search_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    for entry in _iter_timeline_entries(instructions):
        tw = _entry_to_tweet(entry)
        if tw:
            yield tw


def parse_search_response(data: dict, *, skip_retweets: bool = True) -> list[dict]:
    """A ready list of parsed tweets from a SearchTimeline response."""
    out = []
    for raw in iter_search_tweets(data):
        info = parse_tweet(raw, skip_retweets=skip_retweets)
        if info:
            out.append(info)
    return out


def _bottom_cursor_from_instructions(instructions: list) -> str | None:
    """Find the bottom-cursor value in a list of timeline instructions."""
    for instr in instructions or []:
        for entry in instr.get("entries", []) or []:
            content = entry.get("content", {})
            is_cursor = (
                content.get("entryType") == "TimelineTimelineCursor"
                or str(entry.get("entryId", "")).startswith("cursor-bottom")
            )
            if is_cursor and content.get("cursorType", "Bottom") == "Bottom":
                value = content.get("value")
                if value:
                    return value
    return None


def extract_bottom_cursor(data: dict) -> str | None:
    """The "down" cursor from SearchTimeline — for loading the next page (scroll).
    None if there is no cursor (end of results)."""
    instructions = (
        data.get("data", {})
        .get("search_by_raw_query", {})
        .get("search_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    return _bottom_cursor_from_instructions(instructions)


def extract_followers_cursor(data: dict) -> str | None:
    """The "down" cursor from a Followers / BlueVerifiedFollowers timeline."""
    instructions = (
        data.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    return _bottom_cursor_from_instructions(instructions)


def _entries_with_modules(entry: dict) -> Iterator[dict]:
    """Yield tweet-results from an entry, unwrapping TimelineModule items
    (profile timelines wrap conversations in modules)."""
    tw = _entry_to_tweet(entry)
    if tw:
        yield tw
        return
    for it in (entry.get("content", {}).get("items") or []):
        item = (it.get("item") or {}).get("itemContent")
        if not item or item.get("itemType") != "TimelineTweet":
            continue
        result = item.get("tweet_results", {}).get("result")
        if not result:
            continue
        if result.get("__typename") == "TweetWithVisibilityResults":
            result = result.get("tweet", {})
        yield result


def iter_user_tweets(data: dict) -> Iterator[dict]:
    """Raw tweets from UserTweets (timeline_v2 / timeline)."""
    result = data.get("data", {}).get("user", {}).get("result", {})
    tl = result.get("timeline_v2") or result.get("timeline") or {}
    instructions = tl.get("timeline", {}).get("instructions", [])
    for entry in _iter_timeline_entries(instructions):
        yield from _entries_with_modules(entry)


def parse_user_tweets_response(data: dict, *, skip_retweets: bool = True) -> list[dict]:
    """A ready list of parsed tweets from a UserTweets response."""
    out = []
    for raw in iter_user_tweets(data):
        info = parse_tweet(raw, skip_retweets=skip_retweets)
        if info:
            out.append(info)
    return out


# ---- users ----

def parse_user(user_result: dict) -> dict | None:
    """A flat user dict from a GraphQL ``user_results.result``."""
    if not user_result:
        return None
    core = user_result.get("core") or {}
    legacy = user_result.get("legacy") or {}
    rel = user_result.get("relationship_perspectives") or {}
    screen_name = core.get("screen_name")
    user_id = user_result.get("rest_id")
    if not screen_name or not user_id:
        return None

    created_at = core.get("created_at") or legacy.get("created_at")
    created_at_iso = None
    if created_at:
        try:
            created_at_iso = datetime.strptime(
                created_at, "%a %b %d %H:%M:%S %z %Y"
            ).isoformat()
        except ValueError:
            pass

    return {
        "id": user_id,
        "screen_name": screen_name,
        "name": core.get("name"),
        "created_at": created_at,
        "created_at_iso": created_at_iso,
        "is_blue_verified": user_result.get("is_blue_verified"),
        "verified": legacy.get("verified"),
        "protected": legacy.get("protected"),
        "followers_count": legacy.get("followers_count"),
        "friends_count": legacy.get("friends_count"),
        "statuses_count": legacy.get("statuses_count"),
        "media_count": legacy.get("media_count"),
        "favourites_count": legacy.get("favourites_count"),
        "location": legacy.get("location"),
        "description": legacy.get("description"),
        "url": f"https://x.com/{screen_name}",
        "following": rel.get("following"),
        "followed_by": rel.get("followed_by"),
    }


def iter_followers(data: dict) -> Iterator[dict]:
    """Raw ``user_results.result`` from a BlueVerifiedFollowers / Followers* timeline."""
    instructions = (
        data.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    for entry in _iter_timeline_entries(instructions):
        content = entry.get("content", {})
        item = content.get("itemContent")
        if not item or item.get("itemType") != "TimelineUser":
            continue
        result = item.get("user_results", {}).get("result")
        if result:
            yield result


def parse_followers_response(data: dict) -> list[dict]:
    """List of users from a BlueVerifiedFollowers / Followers / Following response."""
    out = []
    for raw in iter_followers(data):
        info = parse_user(raw)
        if info:
            out.append(info)
    return out


def parse_user_result(data: dict) -> dict | None:
    """A flat profile from a UserByScreenName / UserByRestId response."""
    result = data.get("data", {}).get("user", {}).get("result") or {}
    return parse_user(result)


def parse_user_legacy(u: dict) -> dict | None:
    """A flat profile from a legacy REST 1.1 user object (friends/followers list).

    Returns the same shape as ``parse_user`` (GraphQL) for compatibility."""
    if not u:
        return None
    screen_name = u.get("screen_name")
    user_id = u.get("id_str") or (str(u["id"]) if u.get("id") else None)
    if not screen_name or not user_id:
        return None
    created_at = u.get("created_at")
    created_at_iso = None
    if created_at:
        try:
            created_at_iso = datetime.strptime(
                created_at, "%a %b %d %H:%M:%S %z %Y"
            ).isoformat()
        except ValueError:
            pass
    return {
        "id": user_id,
        "screen_name": screen_name,
        "name": u.get("name"),
        "created_at": created_at,
        "created_at_iso": created_at_iso,
        "is_blue_verified": u.get("ext_is_blue_verified", u.get("is_blue_verified")),
        "verified": u.get("verified"),
        "protected": u.get("protected"),
        "followers_count": u.get("followers_count"),
        "friends_count": u.get("friends_count"),
        "statuses_count": u.get("statuses_count"),
        "media_count": u.get("media_count"),
        "favourites_count": u.get("favourites_count"),
        "location": u.get("location"),
        "description": u.get("description"),
        "url": f"https://x.com/{screen_name}",
        "following": u.get("following"),
        "followed_by": u.get("followed_by"),
    }


def parse_users_list_v11(data: dict) -> dict:
    """friends/following/list.json | followers/list.json response ->
    ``{'users': [flat...], 'next_cursor': str, 'total_count': int|None}``."""
    users = []
    for u in data.get("users", []) or []:
        info = parse_user_legacy(u)
        if info:
            users.append(info)
    return {
        "users": users,
        "next_cursor": data.get("next_cursor_str") or str(data.get("next_cursor", "0")),
        "total_count": data.get("total_count"),
    }


def _iter_timeline_users(instructions: list) -> Iterator[dict]:
    """Raw ``user_results.result`` from TimelineUser entries (any user timeline)."""
    for entry in _iter_timeline_entries(instructions):
        item = entry.get("content", {}).get("itemContent")
        if not item or item.get("itemType") != "TimelineUser":
            continue
        result = item.get("user_results", {}).get("result")
        if result:
            yield result


def parse_list_members_response(data: dict) -> list[dict]:
    """List members from a ListMembers response."""
    instructions = (
        data.get("data", {})
        .get("list", {})
        .get("members_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    out = []
    for raw in _iter_timeline_users(instructions):
        info = parse_user(raw)
        if info:
            out.append(info)
    return out


def extract_user_tweets_cursor(data: dict) -> str | None:
    """The "down" cursor from UserTweets / Likes (timeline_v2)."""
    result = data.get("data", {}).get("user", {}).get("result", {})
    tl = result.get("timeline_v2") or result.get("timeline") or {}
    return _bottom_cursor_from_instructions(tl.get("timeline", {}).get("instructions", []))


def extract_list_members_cursor(data: dict) -> str | None:
    """The "down" cursor from a ListMembers timeline."""
    instructions = (
        data.get("data", {})
        .get("list", {})
        .get("members_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    return _bottom_cursor_from_instructions(instructions)


# ---- tweet detail (conversation) ----

def iter_tweet_detail(data: dict) -> Iterator[dict]:
    """Raw tweets from TweetDetail: the focal tweet + replies (in response order)."""
    instructions = (
        data.get("data", {})
        .get("threaded_conversation_with_injections_v2", {})
        .get("instructions", [])
    )
    for entry in _iter_timeline_entries(instructions):
        yield from _entries_with_modules(entry)


def parse_tweet_detail_response(data: dict) -> dict:
    """Parse TweetDetail into ``{'tweet': focal, 'replies': [...]}``.

    The first tweet in the thread is the focal one (the one you opened); the rest
    are replies.
    """
    tweets = []
    for raw in iter_tweet_detail(data):
        info = parse_tweet(raw, skip_retweets=False)
        if info:
            tweets.append(info)
    if not tweets:
        return {"tweet": None, "replies": []}
    return {"tweet": tweets[0], "replies": tweets[1:]}
