"""Offline unit tests for the pure xclient parsers.

These exercise ``parse_user`` and ``parse_tweet`` against small, hand-built
fixtures shaped like the real x.com GraphQL ``result`` objects. No network.
"""

from __future__ import annotations

from marketcast.xclient.parsers import (
    parse_followers_response,
    parse_tweet,
    parse_user,
    parse_user_result,
)


# --- fixtures (trimmed but real-shaped GraphQL "result" objects) ---

def _user_result() -> dict:
    """A GraphQL ``user_results.result`` like UserByScreenName returns."""
    return {
        "rest_id": "1540806109744840704",
        "is_blue_verified": True,
        "core": {
            "screen_name": "poly_enjoyer",
            "name": "OMEGA",
            "created_at": "Tue Jun 28 12:00:00 +0000 2022",
        },
        "legacy": {
            "verified": False,
            "protected": False,
            "followers_count": 1743,
            "friends_count": 565,
            "statuses_count": 3188,
            "media_count": 200,
            "favourites_count": 5000,
            "location": "internet",
            "description": "markets are cope",
        },
        "relationship_perspectives": {"following": False, "followed_by": True},
    }


def _tweet_result() -> dict:
    """A GraphQL tweet ``result`` like a timeline entry carries."""
    return {
        "rest_id": "1790000000000000000",
        "core": {"user_results": {"result": _user_result()}},
        "views": {"count": "5000"},
        "legacy": {
            "id_str": "1790000000000000000",
            "created_at": "Tue Jun 28 12:00:00 +0000 2022",
            "full_text": "gm to the polymarket degens",
            "favorite_count": 10,
            "retweet_count": 2,
            "reply_count": 1,
            "quote_count": 0,
            "bookmark_count": 3,
            "is_quote_status": False,
        },
    }


# --- parse_user ---

def test_parse_user_flattens_core_and_legacy():
    out = parse_user(_user_result())
    assert out is not None
    assert out["id"] == "1540806109744840704"
    assert out["screen_name"] == "poly_enjoyer"
    assert out["name"] == "OMEGA"
    assert out["is_blue_verified"] is True
    assert out["verified"] is False
    assert out["followers_count"] == 1743
    assert out["friends_count"] == 565
    assert out["location"] == "internet"
    assert out["url"] == "https://x.com/poly_enjoyer"
    # relationship perspective is surfaced flat
    assert out["following"] is False
    assert out["followed_by"] is True
    # created_at gets an ISO sibling
    assert out["created_at_iso"] == "2022-06-28T12:00:00+00:00"


def test_parse_user_requires_id_and_handle():
    assert parse_user({}) is None
    assert parse_user(None) is None
    # missing rest_id -> None
    assert parse_user({"core": {"screen_name": "x"}}) is None
    # missing screen_name -> None
    assert parse_user({"rest_id": "1", "core": {}}) is None


def test_parse_user_result_unwraps_envelope():
    data = {"data": {"user": {"result": _user_result()}}}
    out = parse_user_result(data)
    assert out is not None
    assert out["screen_name"] == "poly_enjoyer"


def test_parse_followers_response_walks_timeline():
    data = {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "itemType": "TimelineUser",
                                                    "user_results": {
                                                        "result": _user_result()
                                                    },
                                                }
                                            }
                                        },
                                        # a non-user entry is ignored
                                        {"content": {"itemContent": {"itemType": "TimelineCursor"}}},
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    users = parse_followers_response(data)
    assert len(users) == 1
    assert users[0]["screen_name"] == "poly_enjoyer"


# --- parse_tweet ---

def test_parse_tweet_flattens_metrics_and_author():
    out = parse_tweet(_tweet_result())
    assert out is not None
    assert out["id"] == "1790000000000000000"
    assert out["author"] == "poly_enjoyer"
    assert out["author_name"] == "OMEGA"
    assert out["text"] == "gm to the polymarket degens"
    assert out["likes"] == 10
    assert out["retweets"] == 2
    assert out["replies"] == 1
    assert out["bookmarks"] == 3
    assert out["views"] == 5000
    assert out["is_quote"] is False
    assert out["quoted"] is None
    assert out["url"] == "https://x.com/poly_enjoyer/status/1790000000000000000"
    # views_per_hour is derived from views + age (positive here, old tweet)
    assert out["views_per_hour"] is not None and out["views_per_hour"] > 0


def test_parse_tweet_skips_retweets_by_default():
    rt = _tweet_result()
    rt["legacy"]["full_text"] = "RT @someone: original"
    assert parse_tweet(rt) is None
    # but not when skip_retweets=False
    assert parse_tweet(rt, skip_retweets=False) is not None


def test_parse_tweet_unwraps_visibility_results():
    wrapped = {
        "__typename": "TweetWithVisibilityResults",
        "tweet": _tweet_result(),
    }
    # parse_tweet reads top-level legacy first; the inner tweet is unwrapped by
    # _basic_tweet, so the author/id still resolve.
    out = parse_tweet(wrapped, skip_retweets=False)
    assert out is not None
    assert out["author"] == "poly_enjoyer"
    assert out["id"] == "1790000000000000000"


def test_parse_tweet_longform_note_overrides_full_text():
    tw = _tweet_result()
    tw["note_tweet"] = {
        "note_tweet_results": {"result": {"text": "a much longer longform body"}}
    }
    out = parse_tweet(tw)
    assert out["text"] == "a much longer longform body"
