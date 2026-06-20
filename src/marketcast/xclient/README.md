# xclient — Twitter/X AIO (read + write)

Self-contained x.com client over the private GraphQL/REST API. Auth is a single
`auth_token` cookie (no API keys). Built on `curl_cffi` (TLS-impersonation) so it
behaves like a real Chrome session.

> **For the LLM reading this:** this is a ready-to-use library. To build a script,
> import `Twitter`, call `Twitter.from_env()` (or `from_token`), then use
> `x.read.*` for data and `x.write.*` for actions. Every method below is
> live-tested. Jump to **API Reference** for exact signatures and **Data shapes**
> for return dicts. Don't re-implement HTTP/auth/query-id logic — it's handled.

---

## Install / requirements

```
pip install curl_cffi
# optional: python-dotenv (env loading falls back to a builtin parser if absent)
# optional: pillow (only for generating test images for upload_media)
```

Python 3.10+ (uses `str | None` unions).

## Auth setup

You need an `auth_token` (the cookie value from a logged-in x.com session,
DevTools → Application → Cookies → `auth_token`).

Provide it one of three ways (checked in this order by `from_env`):
1. Env var: `AUTH_TOKEN=...` (and optional `PROXY=...`)
2. `.env` in the current working dir
3. `.env` in the project root or `req_example/.env`

`.env` format:
```
AUTH_TOKEN=abcdef0123...
PROXY=http://user:pass@host:port      # optional
```

---

## Quick start

```python
from marketcast.xclient import Twitter

x = Twitter.from_env()                       # or Twitter.from_token("auth_token...")
print(x.me["screen_name"])                   # who am I (identity, cached)

# READ
user    = x.read.get_user("poly_enjoyer")            # profile dict
tweets  = x.read.get_user_tweets(screen_name="poly_enjoyer", count=40)
hits    = x.read.search("solana", count=20, product="Latest")
fans    = x.read.get_all_followers(screen_name="poly_enjoyer")  # ALL, auto-paginates
thread  = x.read.get_tweet("1790000000000000000")    # {"tweet":..., "replies":[...]}

# WRITE  (acts as the logged-in account)
t = x.write.create_tweet("gm")                       # returns the new tweet dict
x.write.reply(t["id"], "first")
x.write.like(t["id"]); x.write.retweet(t["id"]); x.write.bookmark(t["id"])
x.write.follow(user["id"])
x.write.delete_tweet(t["id"])
```

`Twitter.from_env(**client_kwargs)` forwards kwargs to `TwitterClient`
(`proxy`, `impersonate`, `lang`, `timeout`, `username`).

---

## Architecture

```
xclient/
  client.py     TwitterClient  — session, auth_token, ct0, transport (.get/.post)
  reader.py     TwitterReader  — all reads (timelines, search, users, followers...)
  writer.py     TwitterWriter  — all actions (tweet, like, follow, lists, DM...)
  api.py        Twitter        — facade: .read + .write + identity (.me/.my_id)
  parsers.py    pure functions raw JSON -> flat dicts
  query_ids.py  self-healing GraphQL query_id resolver (see below)
  features.py   GraphQL feature-flag sets
  transaction.py x-client-transaction-id generator (some endpoints need it)
```

`Twitter` is the entry point. If you need lower-level control:
`TwitterClient(auth_token=...)` → `TwitterReader(client)` / `TwitterWriter(client)`.

### query_id self-healing (important)

X rotates the per-operation GraphQL `query_id` every week or two, which 404s
hardcoded ids. `query_ids.py` keeps last-known-good ids, **scrapes the current
ones from x.com's `main.js` on first use and again on any 404, then caches them
to `query_ids_cache.json`.** You never manage these manually for the built-in
methods.

Caveat: a few rarely-used operations (bookmarks, retweeters, favoriters) live in
lazy JS chunks not in `main.js`, so they can't be auto-resolved. Call them via
the **escape hatch** with an explicit `query_id` (see below).

---

## API Reference

### `Twitter` (facade — `xclient.Twitter`)
| Member | Returns | Notes |
|---|---|---|
| `Twitter.from_env(**kw)` | `Twitter` | reads `AUTH_TOKEN`/`PROXY` from env or `.env` |
| `Twitter.from_token(token, *, proxy=None, **kw)` | `Twitter` | explicit token |
| `.read` | `TwitterReader` | all read methods |
| `.write` | `TwitterWriter` | all write methods |
| `.me` | user dict\|None | logged-in profile (cached) |
| `.my_id` | str\|None | own numeric id (from `twid` cookie) |
| `.whoami()` | user dict | force-refresh `.me` |
| `.client` | `TwitterClient` | raw transport |

### `TwitterReader` (`x.read`)
Each has a `*_raw` variant returning the unparsed JSON dict.

| Method | Returns | Notes |
|---|---|---|
| `get_user(screen_name)` | user dict | by @handle |
| `get_user_by_id(user_id)` | user dict | by numeric id |
| `resolve_user_id(screen_name)` | str | @handle → id |
| `get_user_tweets(screen_name=/user_id=, count=40, skip_retweets=True)` | list[tweet] | a user's tweets |
| `get_all_user_tweets(...)` / `get_all_likes(...)` / `get_all_list_members(list_id)` | list | scroll to end via cursor (`on_page` callback) |
| `get_dm_inbox()` / `get_dm_conversation(conversation_id)` | dict (raw) | read DMs (inbox + a thread) |
| `get_notifications(count=40)` | dict (raw) | notifications timeline (legacy globalObjects) |
| `get_tweet(tweet_id, count=40)` | `{"tweet": tweet, "replies": [tweet,...]}` | thread/replies |
| `search(raw_query, count=20, product="Latest", pages=1, skip_retweets=True)` | list[tweet] | `product`: Latest/Top/People/Photos/Videos; `pages>1` scrolls |
| `get_home_timeline(count=20, skip_retweets=True)` | list[tweet] | your "For you"/following feed |
| `get_list_timeline(list_id, count=20, skip_retweets=True)` | list[tweet] | tweets in a list |
| `get_list_members(list_id, count=100)` | list[user] | members of a list |
| `get_likes(screen_name=/user_id=, count=40)` | list[tweet] | **only visible for your own account** (likes are private) |
| `get_followers(screen_name=/user_id=, count=20, pages=1)` | list[user] | one or N pages |
| `get_all_followers(screen_name=/user_id=, count=100, on_page=None, wait_on_limit=True, on_wait=None, max_pages=1000)` | list[user] | **ALL** followers (GraphQL ~50 req/15m → slow, auto-waits) |
| `get_following(...)` / `get_all_following(...)` | list[user] | who a user follows (same signatures) |
| `get_all_followers_v11(screen_name=/user_id=, count=200, on_page=None)` | list[user] | ⚡ **FAST** ALL followers via REST 1.1 (~1000 req/15m, 200/page) — **prefer this for big lists** |
| `get_all_following_v11(...)` | list[user] | ⚡ FAST all following (REST 1.1); `on_page(new, total, total_count)` |
| `get_followers_v11(...)` / `get_following_v11(...)` | `{users, next_cursor, total_count}` | one fast page (cursor `"-1"` start, `"0"` end) |
| `get_blue_verified_followers(...)` | list[user] | verified-only followers |
| `get_trends()` | dict (raw) | Explore sidebar trends |
| `get_badge_count()` | dict | unread counts: `ntab_unread_count`, `dm_unread_count`, ... |
| `gql_get(operation, variables, *, feature_set=None, field_toggles=None, query_id=None, referer=...)` | dict (raw) | **escape hatch** for any read op |

### `Telemetry` (`x.telemetry`) — account warming / realism
Emulates the client-side "scribe" beacons a real browser fires constantly
(impressions, tweet opens, profile visits, video ad-checks). A bot that only
fires mutations and zero telemetry looks robotic; these make the activity
pattern look human.

| Method | Returns | Notes |
|---|---|---|
| `impressions(tweets, *, page="home", section="home", component="stream")` | bool | log views of tweets you actually fetched (dicts or ids) |
| `open_tweet(tweet, *, page="home")` | bool | opened a tweet's detail view |
| `profile_visit(user_id)` | bool | visited a profile |
| `video_preroll(tweet_ids, *, display_location="TIMELINE_HOME")` | int (HTTP status) | video ad-check beacon; batch the video tweet ids you saw |
| `badge_count()` / `fleetline()` | dict | background polls a real tab makes (unread counts / live Spaces) |
| `heartbeat()` | dict | one idle "tick" (badge + fleetline); call ~every 30s to look alive |
| `scribe(events)` / `event(namespace, items=None, **extra)` | bool / dict | low-level: build/send raw `client_event`s |

**Honesty / caveats (read before using):**
- Benefit to "account health" is *plausible but unproven* — no public confirmation
  of how X weighs these signals.
- **Inconsistent telemetry can backfire** (events for things you didn't do, future
  timestamps, wrong namespaces are themselves bot tells). **Tie events to real
  reads** — log `impressions` only for tweets you actually fetched.
- `jot/client_event.json` returns **200 empty** (works). It requires
  `x-client-transaction-id`; `Telemetry` adds it automatically.
- `video_preroll` posts the exact browser body (single form field `tweets` whose
  value is the whole `{"tweets":[...],"display_location":...}` JSON) and returns
  the HTTP status (200 on success; X may also 202/CachedForSync via a worker).

```python
# "humanized" read: fetch, then emit the telemetry a browser would
tweets = x.read.get_home_timeline(count=20)
x.telemetry.impressions(tweets)                 # saw them
x.telemetry.open_tweet(tweets[0])               # opened one
x.write.like(tweets[0]["id"])                   # then acted
```

### `TwitterWriter` (`x.write`)
All act as the logged-in account. Mutations self-heal query_id.

| Method | Returns | Notes |
|---|---|---|
| `create_tweet(text="", *, reply_to=None, quote_tweet_id=None, media_ids=None, possibly_sensitive=False)` | tweet dict | the new tweet |
| `reply(tweet_id, text, **kw)` | tweet dict | = create_tweet(reply_to=) |
| `quote(tweet_id, text, **kw)` | tweet dict | = create_tweet(quote_tweet_id=) |
| `delete_tweet(tweet_id)` | bool | |
| `like(tweet_id)` / `unlike(tweet_id)` | bool | |
| `retweet(tweet_id)` | str (retweet id) | |
| `unretweet(tweet_id)` | bool | by original tweet id |
| `bookmark(tweet_id)` / `unbookmark(tweet_id)` | bool | |
| `follow(user_id)` / `unfollow(user_id)` | dict | REST 1.1; takes **user_id**, not handle |
| `mute/unmute/block/unblock(user_id)` | dict | REST 1.1 |
| `upload_media(path=None, *, data=None, mime_type="image/jpeg", media_category=None)` | str (media_id) | pass `media_ids=[id]` to create_tweet |
| `create_list(name, *, description="", private=False)` | str (list_id) | |
| `add_list_member(list_id, user_id)` / `remove_list_member(...)` | bool | |
| `delete_list(list_id)` | bool | |
| `get_my_lists(count=100)` | list[{id,name,private}] | |
| `send_dm(recipient_id, text)` | dict (raw) | recipient is **user_id** |
| `mutate(operation, variables, *, feature_set=None, query_id=None, referer=...)` | dict (raw) | **escape hatch** for any write op |

---

## Data shapes

**user dict** (from `get_user`, `get_followers`, `get_list_members`, ...):
```python
{
  "id": "1540806109744840704", "screen_name": "poly_enjoyer", "name": "...",
  "created_at": "...", "created_at_iso": "2022-06-...T...",
  "is_blue_verified": True, "verified": False, "protected": False,
  "followers_count": 1743, "friends_count": 565, "statuses_count": 3188,
  "media_count": 200, "favourites_count": 5000, "location": "", "description": "...",
  "url": "https://x.com/poly_enjoyer",
  "following": False,   # do I follow them
  "followed_by": False, # do they follow me
}
```

**tweet dict** (from `search`, `get_user_tweets`, `get_home_timeline`, `create_tweet`, ...):
```python
{
  "id": "1790...", "created_at": "...", "created_at_iso": "...",
  "age_seconds": 1234.0, "views_per_hour": 50.0,
  "author": "poly_enjoyer", "author_name": "OMEGA",
  "followers_count": 1743, "is_blue_verified": True,
  "following": False, "followed_by": False,
  "url": "https://x.com/poly_enjoyer/status/1790...",
  "text": "full tweet text (longform-aware)",
  "likes": 10, "retweets": 2, "replies": 1, "quotes": 0, "bookmarks": 3, "views": 500,
  "is_quote": False, "quoted": None,   # quoted -> a nested basic-tweet dict if quote
}
```
`get_tweet` returns `{"tweet": <focal tweet>, "replies": [<tweet>, ...]}`.

---

## Gotchas & limits (learned from live testing)

- **Rate limits & the FAST path.** The GraphQL `Followers`/`Following` ops allow
  only ~**50 requests / 15-min window**, so `get_all_followers` (GraphQL) can stall
  on a 15-min wait for large accounts (it auto-waits via `x-rate-limit-reset`,
  capped by `max_wait`; `wait_on_limit=False` to raise instead). **Prefer the REST
  1.1 fast path `get_all_followers_v11` / `get_all_following_v11`** (~1000 req/15m,
  200 users/page): e.g. 1705 followers in ~5s vs ~640s on GraphQL. Same flat user
  dicts. Use GraphQL only if you specifically need `get_blue_verified_followers`.
- **Parsed followers < profile count.** X omits suspended/deactivated/protected
  accounts, so e.g. 1705 parsed vs 1743 shown is normal.
- **Likes are private.** `get_likes` only returns data for *your own* account.
- **Long tweets / long quotes need Premium.** `create_tweet` supports any length
  (no client-side cap), but without Premium X rejects >280 chars with
  `code 186 AuthorizationError`. Works unchanged on a Premium account.
- **follow/mute/block take a numeric `user_id`**, not a @handle. Resolve first:
  `x.read.resolve_user_id("handle")`.
- **List mutations on bannerless accounts.** X applies the change but may return a
  `DecodeException` serializing the response; `create_list`/`add_list_member`/
  `remove_list_member` already tolerate this and recover (verified via
  `get_list_members`). On normal accounts there's no issue.
- **Errors.** Methods raise `RuntimeError` with the HTTP status + body snippet on
  failure; GraphQL-level errors raise `RuntimeError("<Op> errors: [...]")`.

---

## Production: errors, sessions, pacing, multi-account

**Typed errors** (`from marketcast.xclient import errors` or the classes directly). Every
method raises a precise type so scripts can branch:
```python
from marketcast.xclient import RateLimited, Unauthorized, Suspended, NotFound, TwitterError
try:
    x.write.follow(uid)
except Unauthorized:      # auth_token dead/expired -> rotate account
    ...
except Suspended:         # account restricted/locked -> drop from pool
    ...
except RateLimited as e:  # e.reset = epoch when window resets
    ...
except NotFound: ...      # gone / rotated query_id
# all inherit TwitterError(.status, .code, .response)
```

**Session persistence** — skip re-auth between runs:
```python
x = Twitter.from_env(cache=True)   # caches cookies+ct0 in ~/.xclient/<token8>.json
# 2nd run starts in ~0s (ct0 reused, no login fetch). x.save_session() to force-write.
```
Pass `cache="/path/file.json"` for a custom location.

**Action pacing & daily caps** (`safe_mode`) — account-health throttle:
```python
x = Twitter.from_env(safe_mode=True)   # ActionPacer: jitter pause + daily limits
x.write.like(id); x.write.follow(uid)  # auto-sleeps between, enforces caps
# raises LimitReached when a daily cap is hit. Counts persist (rolling 24h) in
# ~/.xclient/pacer_<token8>.json. Defaults: follow 400, like 800, tweet 300, dm 250...
# customise: Twitter.from_env(pacer=ActionPacer(limits={"follow":100}, jitter=(5,15)))
```
Pacing applies to writes only (follow/unfollow/like/retweet/bookmark/tweet/reply/dm).

**Multi-account pool** (`AccountPool`):
```python
from marketcast.xclient import AccountPool
pool = AccountPool.from_file("accounts.txt", cache=True, safe_mode=True)
#   accounts.txt: one per line, "auth_token" or "auth_token, http://user:pass@host:port"
pool = AccountPool.from_tokens([(tok1, proxy1), tok2])   # or inline

acc = pool.next()                 # round-robin over healthy accounts
acc = pool.random()               # random healthy
acc = pool.get("some_handle")     # by @screen_name
for a in pool.alive():            # vets each (authed check), flags dead/suspended
    a.tw.write.like(some_id)      # a.tw is a full Twitter facade
pool.map(lambda a: a.tw.write.follow(uid))  # runs on all; dead ones auto-skipped
```
Each account is lazy (builds its session on first use), has its own cache + proxy +
pacer, and is auto-flagged dead on `Unauthorized`/`Suspended`.

## Escape hatch (any operation not wrapped)

If an operation has no dedicated method, call it directly. If its `query_id` is
in `main.js` it auto-resolves; otherwise grab the id from DevTools (Network tab:
`.../graphql/<ID>/<OperationName>`) and pass `query_id=`.

```python
# read example
data = x.read.gql_get(
    "Bookmarks",
    {"count": 20, "includePromotedContent": False},
    query_id="QUERY_ID_FROM_DEVTOOLS",   # only needed if not auto-resolvable
)

# write example
x.write.mutate("PinTweet", {"tweet_id": "1790..."}, query_id="QUERY_ID_FROM_DEVTOOLS")
```

---

## Recipes

```python
# 1) Dump all followers to JSON, FAST (REST 1.1, ~5s for ~1700)
import json
from marketcast.xclient import Twitter
x = Twitter.from_env()
fans = x.read.get_all_followers_v11(
    screen_name="poly_enjoyer", count=200,
    on_page=lambda new, total, total_count: print("collected", total),
)
json.dump(fans, open("followers.json", "w"), ensure_ascii=False, indent=2)

# 1b) Keep a session looking alive while you work (idle heartbeat)
import time
for _ in range(10):
    x.telemetry.heartbeat()      # badge_count + fleetline, like an open tab
    time.sleep(30)

# 2) Auto-reply to recent search hits
for tw in x.read.search("airdrop", count=10, product="Latest"):
    if tw["followers_count"] > 1000:
        x.write.reply(tw["id"], "interesting 👀")

# 3) Tweet with an image
mid = x.write.upload_media("banner.png", mime_type="image/png")
x.write.create_tweet("with media", media_ids=[mid])

# 4) Build a list and fill it
lid = x.write.create_list("alpha", private=True)
for handle in ["poly_enjoyer", "elonmusk"]:
    x.write.add_list_member(lid, x.read.resolve_user_id(handle))
```
