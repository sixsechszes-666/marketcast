"""TwitterWriter — write actions on x.com: tweets, likes, retweets, bookmarks,
follows, mutes, blocks, media upload.

Takes a TwitterClient (transport/auth). GraphQL mutations go via POST with a body
``{variables, features?, queryId}``; the query_id comes from the self-healing
resolver (query_ids) — no hardcoding, with auto-refresh on a 404. Follow/mute/block
live on the legacy REST 1.1 (form-urlencoded).
"""

from __future__ import annotations

import base64
import time
import uuid
from urllib.parse import unquote

from . import errors, features, query_ids
from .client import TwitterClient
from .parsers import parse_tweet

GRAPHQL = "https://x.com/i/api/graphql"
API_11 = "https://x.com/i/api/1.1"
UPLOAD = "https://upload.x.com/i/media/upload.json"


def _gql_url(name: str) -> str:
    return f"{GRAPHQL}/{query_ids.get(name)}/{name}"


class TwitterWriter:
    """All write operations over a :class:`TwitterClient`."""

    def __init__(self, client: TwitterClient, *, pacer=None) -> None:
        self.client = client
        self.pacer = pacer

    def _pace(self, action: str | None) -> None:
        if self.pacer and action:
            self.pacer.gate(action)

    # ---- low-level ----

    def _mutation(
        self,
        op: str,
        variables: dict,
        *,
        feature_set: dict | None = None,
        query_id: str | None = None,
        referer: str = "https://x.com/home",
        _retried: bool = False,
    ) -> dict:
        """GraphQL mutation via POST. Self-heals query_id on a 404."""
        qid = query_id or query_ids.get(op)
        if not qid:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            qid = query_ids.get(op)
        if not qid:
            raise ValueError(
                f"no query_id for {op}: pass query_id=... (from DevTools)"
            )
        body: dict = {"variables": variables, "queryId": qid}
        if feature_set is not None:
            body["features"] = feature_set
        resp = self.client.post(f"{GRAPHQL}/{qid}/{op}", json_body=body, referer=referer)
        if resp.status_code == 404 and not _retried and not query_id:
            query_ids.refresh(self.client.session, impersonate=self.client.impersonate)
            return self._mutation(
                op, variables, feature_set=feature_set, referer=referer, _retried=True
            )
        errors.raise_for_status(resp, op)
        data = resp.json()
        errors.raise_for_graphql(data, op, resp)
        return data

    def mutate(
        self, operation: str, variables: dict, *,
        feature_set: dict | None = None, query_id: str | None = None,
        referer: str = "https://x.com/home",
    ) -> dict:
        """Call ANY GraphQL write operation (escape hatch). query_id from the
        resolver or passed explicitly (DevTools: .../graphql/<ID>/<Operation>)."""
        return self._mutation(
            operation, variables, feature_set=feature_set,
            query_id=query_id, referer=referer,
        )

    def _v11(self, path: str, params: dict, *, referer: str = "https://x.com/home") -> dict:
        """Legacy REST 1.1 POST (form-urlencoded)."""
        resp = self.client.request(
            "POST", f"{API_11}/{path}", data=params, referer=referer
        )
        errors.raise_for_status(resp, path)
        return resp.json()

    # ---- tweets ----

    def create_tweet(
        self,
        text: str = "",
        *,
        reply_to: str | None = None,
        quote_tweet_id: str | None = None,
        media_ids: list[str] | None = None,
        possibly_sensitive: bool = False,
    ) -> dict | None:
        """Publish a tweet. reply_to — parent tweet id (reply); quote_tweet_id —
        a quote; media_ids — previously uploaded media (see upload_media). Returns
        the parsed new tweet."""
        variables: dict = {
            "tweet_text": text,
            "dark_request": False,
            "media": {
                "media_entities": [
                    {"media_id": str(m), "tagged_users": []} for m in (media_ids or [])
                ],
                "possibly_sensitive": possibly_sensitive,
            },
            "semantic_annotation_ids": [],
        }
        if reply_to:
            variables["reply"] = {
                "in_reply_to_tweet_id": str(reply_to),
                "exclude_reply_user_ids": [],
            }
        if quote_tweet_id:
            variables["attachment_url"] = f"https://x.com/i/status/{quote_tweet_id}"
        self._pace("reply" if reply_to else "tweet")
        data = self._mutation("CreateTweet", variables, feature_set=features.CREATE_TWEET)
        result = (
            data.get("data", {})
            .get("create_tweet", {})
            .get("tweet_results", {})
            .get("result")
        )
        return parse_tweet(result, skip_retweets=False) if result else None

    def reply(self, tweet_id: str, text: str, **kw) -> dict | None:
        """Reply to a tweet."""
        return self.create_tweet(text, reply_to=tweet_id, **kw)

    def quote(self, tweet_id: str, text: str, **kw) -> dict | None:
        """Quote a tweet."""
        return self.create_tweet(text, quote_tweet_id=tweet_id, **kw)

    def delete_tweet(self, tweet_id: str) -> bool:
        """Delete one of your tweets."""
        self._mutation(
            "DeleteTweet", {"tweet_id": str(tweet_id), "dark_request": False}
        )
        return True

    # ---- reactions ----

    def like(self, tweet_id: str) -> bool:
        """Like a tweet."""
        self._pace("like")
        data = self._mutation("FavoriteTweet", {"tweet_id": str(tweet_id)})
        return data.get("data", {}).get("favorite_tweet") == "Done"

    def unlike(self, tweet_id: str) -> bool:
        """Remove a like."""
        data = self._mutation("UnfavoriteTweet", {"tweet_id": str(tweet_id)})
        return data.get("data", {}).get("unfavorite_tweet") == "Done"

    def retweet(self, tweet_id: str) -> str | None:
        """Retweet. Returns the id of the created retweet."""
        self._pace("retweet")
        data = self._mutation(
            "CreateRetweet", {"tweet_id": str(tweet_id), "dark_request": False}
        )
        return (
            data.get("data", {})
            .get("create_retweet", {})
            .get("retweet_results", {})
            .get("result", {})
            .get("rest_id")
        )

    def unretweet(self, tweet_id: str) -> bool:
        """Undo a retweet (by the original tweet id)."""
        self._mutation(
            "DeleteRetweet", {"source_tweet_id": str(tweet_id), "dark_request": False}
        )
        return True

    def bookmark(self, tweet_id: str) -> bool:
        """Bookmark a tweet."""
        self._pace("bookmark")
        data = self._mutation("CreateBookmark", {"tweet_id": str(tweet_id)})
        return data.get("data", {}).get("tweet_bookmark_put") == "Done"

    def unbookmark(self, tweet_id: str) -> bool:
        """Remove a bookmark."""
        data = self._mutation("DeleteBookmark", {"tweet_id": str(tweet_id)})
        return data.get("data", {}).get("tweet_bookmark_delete") == "Done"

    # ---- relationships (REST 1.1) ----

    def follow(self, user_id: str) -> dict:
        """Follow a user (by numeric user_id)."""
        self._pace("follow")
        return self._v11(
            "friendships/create.json",
            {
                "include_profile_interstitial_type": "1",
                "include_blocking": "1",
                "include_blocked_by": "1",
                "skip_status": "1",
                "user_id": str(user_id),
            },
        )

    def unfollow(self, user_id: str) -> dict:
        """Unfollow a user (by numeric user_id)."""
        self._pace("unfollow")
        return self._v11(
            "friendships/destroy.json",
            {"include_profile_interstitial_type": "1", "skip_status": "1",
             "user_id": str(user_id)},
        )

    def mute(self, user_id: str) -> dict:
        """Mute a user."""
        return self._v11("mutes/users/create.json", {"user_id": str(user_id)})

    def unmute(self, user_id: str) -> dict:
        """Unmute a user."""
        return self._v11("mutes/users/destroy.json", {"user_id": str(user_id)})

    def block(self, user_id: str) -> dict:
        """Block a user."""
        return self._v11("blocks/create.json", {"user_id": str(user_id)})

    def unblock(self, user_id: str) -> dict:
        """Unblock a user."""
        return self._v11("blocks/destroy.json", {"user_id": str(user_id)})

    # ---- lists ----

    def get_my_lists(self, count: int = 100) -> list[dict]:
        """Lists owned by the logged-in account (id/name/private)."""
        mine = self._my_id()
        resp = self.client.get(
            f"{API_11}/lists/ownerships.json?user_id={mine}&count={count}",
            referer="https://x.com/home",
        )
        resp.raise_for_status()
        return [
            {"id": lst.get("id_str"), "name": lst.get("name"),
             "private": lst.get("mode") == "private"}
            for lst in resp.json().get("lists", [])
        ]

    def create_list(
        self, name: str, *, description: str = "", private: bool = False
    ) -> str | None:
        """Create a list. Returns the list_id.

        On accounts with no banner X creates the list but chokes serializing the
        response (DecodeException on default_banner_media_results) — in that case
        we pull the freshly created list's id from lists/ownerships.json by name."""
        try:
            data = self._mutation(
                "CreateList",
                {"isPrivate": private, "name": name, "description": description},
                feature_set=features.SEARCH_TIMELINE,
            )
            return (
                data.get("data", {}).get("list", {}).get("id_str")
                or data.get("data", {}).get("create_list", {}).get("id_str")
            )
        except RuntimeError as e:
            if "DecodeException" not in str(e) and "214" not in str(e):
                raise
            for lst in self.get_my_lists():
                if lst["name"] == name:
                    return lst["id"]
            raise

    def _list_member_op(self, op: str, list_id: str, user_id: str) -> bool:
        """ListAddMember/ListRemoveMember. On accounts with no banner X applies the
        change but chokes serializing the response (DecodeException) — we treat
        that as success."""
        try:
            self._mutation(
                op,
                {"listId": str(list_id), "userId": str(user_id)},
                feature_set=features.SEARCH_TIMELINE,
            )
        except RuntimeError as e:
            if "DecodeException" not in str(e):
                raise
        return True

    def add_list_member(self, list_id: str, user_id: str) -> bool:
        """Add a user to a list."""
        return self._list_member_op("ListAddMember", list_id, user_id)

    def remove_list_member(self, list_id: str, user_id: str) -> bool:
        """Remove a user from a list."""
        return self._list_member_op("ListRemoveMember", list_id, user_id)

    def delete_list(self, list_id: str) -> bool:
        """Delete a list."""
        self._mutation("DeleteList", {"listId": str(list_id)})
        return True

    # ---- direct messages ----

    def _my_id(self) -> str | None:
        twid = self.client.session.cookies.get("twid")
        return unquote(twid).split("u=")[-1] if twid else None

    def send_dm(self, recipient_id: str, text: str) -> dict:
        """Send a DM to a user by their user_id."""
        self._pace("dm")
        mine = self._my_id()
        conv = (
            f"{recipient_id}-{mine}" if mine and int(mine) > int(recipient_id)
            else f"{mine}-{recipient_id}"
        )
        body = {
            "conversation_id": conv,
            "recipient_ids": False,
            "request_id": str(uuid.uuid4()),
            "text": text,
            "cards_platform": "Web-12",
            "include_cards": 1,
            "include_quote_count": True,
            "dm_users": False,
        }
        resp = self.client.post(
            f"{API_11}/dm/new2.json", json_body=body, referer="https://x.com/messages"
        )
        errors.raise_for_status(resp, "send_dm")
        return resp.json()

    # ---- media (chunked upload to upload.x.com) ----

    def upload_media(
        self, path: str | None = None, *, data: bytes | None = None,
        media_category: str | None = None, mime_type: str = "image/jpeg",
    ) -> str:
        """Upload an image/video and return the media_id (for create_tweet).

        path — a file, or data — raw bytes. Implements the chunked protocol
        (INIT -> APPEND -> FINALIZE) + STATUS wait for video."""
        if data is None:
            if not path:
                raise ValueError("provide path or data")
            with open(path, "rb") as f:
                data = f.read()
        total = len(data)
        if media_category is None:
            media_category = "tweet_video" if mime_type.startswith("video") else "tweet_image"

        # INIT
        init = self.client.request(
            "POST", UPLOAD,
            data={
                "command": "INIT",
                "total_bytes": str(total),
                "media_type": mime_type,
                "media_category": media_category,
            },
            referer="https://x.com/home",
        )
        if init.status_code >= 400:
            raise RuntimeError(f"media INIT {init.status_code}: {init.text[:300]}")
        media_id = init.json()["media_id_string"]

        # APPEND (4 MB chunks; we send base64 in media_data — form-urlencoded, to
        # avoid depending on curl_cffi's multipart handling)
        chunk = 4 * 1024 * 1024
        for idx, off in enumerate(range(0, total, chunk)):
            part = data[off:off + chunk]
            r = self.client.request(
                "POST", UPLOAD,
                data={
                    "command": "APPEND",
                    "media_id": media_id,
                    "segment_index": str(idx),
                    "media_data": base64.b64encode(part).decode("ascii"),
                },
                referer="https://x.com/home",
            )
            if r.status_code >= 400:
                raise RuntimeError(f"media APPEND {r.status_code}: {r.text[:300]}")

        # FINALIZE
        fin = self.client.request(
            "POST", UPLOAD,
            data={"command": "FINALIZE", "media_id": media_id},
            referer="https://x.com/home",
        )
        if fin.status_code >= 400:
            raise RuntimeError(f"media FINALIZE {fin.status_code}: {fin.text[:300]}")
        info = fin.json()

        # video is processed asynchronously — wait for STATUS=succeeded
        if info.get("processing_info"):
            self._await_media(media_id)
        return media_id

    def _await_media(self, media_id: str, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.client.request(
                "GET", f"{UPLOAD}?command=STATUS&media_id={media_id}",
                referer="https://x.com/home",
            )
            pi = r.json().get("processing_info") or {}
            state = pi.get("state")
            if state == "succeeded":
                return
            if state == "failed":
                raise RuntimeError(f"media processing failed: {pi}")
            time.sleep(pi.get("check_after_secs", 2))
        raise RuntimeError("media processing timeout")
