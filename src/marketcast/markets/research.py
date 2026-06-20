"""Live X research: pull fresh real-world news about a chosen subject from X and
distill it into a compact digest, so the generated tweet and the video copy can
be timely and grounded in what is actually happening right now (not just the
static Polymarket numbers).

Pipeline:
  1. ask the LLM for focused X-search keyword phrases about the event's
     real-world topic (the people/places/events behind the market);
  2. search X for each keyword (advanced-search builder), scrolling a few pages
     deep, then merge + dedupe;
  3. ask the LLM to distill the freshest tweets into a tiny digest:
        ``{"summary": str, "points": [str, ...], "sentiment": str}``;
  4. hand that digest to the generators as CURRENT CONTEXT.

Design (mirrors the rest of the pipeline):
- EVENTS get topical news research (keywords from the market's real-world story);
  TRADERS get reputation research (X chatter about that whale, scoped by name AND
  'polymarket'). :func:`research_subject` dispatches by kind. A trader with only
  a wallet-alias name (not searchable on X) gets ``None``.
- Fail-open: no token / no fresh tweets / bad JSON -> ``None``, and the generators
  just skip the context block. Never raises.
- The digest is QUALITATIVE. The tweet still takes every number from FACTS; the
  context only steers angle/relevance.
- Toggle off with ``DISABLE_RESEARCH=1`` (``settings.disable_research``).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from marketcast.config import settings
from marketcast.markets.models import Research

# Research artifacts are persisted under the configured data dir (was post_data/).
_RESEARCH_DIR = settings.data_dir / "research"


def enabled() -> bool:
    """Whether live research is on (``DISABLE_RESEARCH`` unset/false)."""
    return not settings.disable_research


def _days() -> int:
    try:
        d = int(os.getenv("RESEARCH_DAYS", "2"))
        return d if d >= 1 else 2
    except ValueError:
        return 2


def _pages() -> int:
    """How many pages to scroll PER keyword (default 2, capped at 6)."""
    try:
        return min(max(int(os.getenv("RESEARCH_PAGES", "2")), 1), 6)
    except ValueError:
        return 2


def _keyword_count() -> int:
    """How many keyword phrases the LLM generates / we search (default 5)."""
    try:
        return min(max(int(os.getenv("RESEARCH_KEYWORDS", "5")), 1), 8)
    except ValueError:
        return 5


def _min_faves() -> int:
    """Engagement floor so the digest is built from tweets people reacted to."""
    try:
        return max(int(os.getenv("RESEARCH_MIN_FAVES", "5")), 0)
    except ValueError:
        return 5


def _max_tweets() -> int:
    """Cap on how many (merged, top-engagement) tweets feed the distiller."""
    try:
        return min(max(int(os.getenv("RESEARCH_MAX_TWEETS", "16")), 3), 40)
    except ValueError:
        return 16


# ── X search query helpers (ported from social_signal, kept local) ────────────
_STOP = {
    "will", "the", "by", "in", "on", "of", "a", "an", "and", "or", "vs", "x",
    "to", "be", "get", "reach", "hit", "before", "after", "this", "for",
    "2024", "2025", "2026", "2027", "2028",
}


def _topic_from_title(title: str | None) -> str | None:
    """Core topic phrase from a market title (drop the '... by <date>?' tail, a
    leading 'Will', stopwords; keep ~6 meaningful words)."""
    title = (title or "").strip()
    title = re.sub(r"\bby\b.*$", "", title, flags=re.I)  # "... by Dec 31, 2026?"
    title = re.sub(r"^will\s+", "", title, flags=re.I)
    title = re.sub(r'[?"]', "", title).strip()
    words = re.findall(r"[A-Za-z0-9']+", title)
    core = [w for w in words if w.lower() not in _STOP][:6]
    q = " ".join(core).strip() if core else title.strip()
    return q or None


def _event_query(f: dict) -> str | None:
    return _topic_from_title(f.get("title"))


def _build_query(topic: str, days: int, min_faves: int = 0) -> str:
    """Advanced-search query for measuring a topic's current heat.

    Operators verified against SearchTimeline:
    - ``within_time:Nd``  precise rolling window
    - ``-filter:replies`` original posts, not reply piles
    - ``-filter:nullcast`` drop promoted/ad tweets
    - ``lang:en``         our posting audience
    - ``min_faves:N``     optional engagement floor
    """
    parts = [topic, f"within_time:{days}d", "lang:en", "-filter:replies", "-filter:nullcast"]
    if min_faves > 0:
        parts.append(f"min_faves:{min_faves}")
    return " ".join(parts)


# ── X search ─────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")


def _clean_text(text: str | None) -> str:
    return _WS_RE.sub(" ", _URL_RE.sub("", text or "")).strip()


_KW_SYSTEM = """You generate search keywords for X (Twitter) to surface the LATEST real-world \
news behind a prediction market. Return short phrases (1-3 words each) that target the \
underlying real-world story — the people, places, organizations and events involved — NOT \
the betting market itself. Each phrase should chase a DIFFERENT angle so together they cover \
the story. Do NOT use the words Polymarket, odds, bet, market or prediction. \
Output STRICT JSON only, no preamble, no code fences: {"keywords":["..","..","..","..",".."]}"""

_KW_BANNED = {
    "polymarket", "odds", "bet", "bets", "betting", "market", "markets",
    "prediction", "predictions", "yes", "no",
}


def _keywords(facts: dict) -> list[str]:
    """Ask the LLM for N focused X-search keyword phrases about the event's
    real-world topic. Falls back to the title-derived phrase on any failure."""
    n = _keyword_count()
    fallback = [k for k in [_event_query(facts)] if k]

    title = (facts.get("title") or "").strip()
    if not title:
        return fallback
    try:
        from marketcast.llm import call_llm
    except Exception:
        return fallback

    parts = [f"EVENT: {title}"]
    if facts.get("tag"):
        parts.append(f"category: {facts['tag']}")
    field = [o.get("name", "") for o in (facts.get("field") or [])[:4] if o.get("name")]
    if field:
        parts.append("outcomes in play: " + ", ".join(field))
    user = "\n".join(parts) + f"\n\nGive {n} search phrases. JSON only."

    try:
        raw = call_llm(_KW_SYSTEM, user, max_tokens=180, temperature=0.5)
    except Exception as e:
        print(f"[RESEARCH]: keyword gen failed ({str(e)[:50]}) — using title")
        return fallback

    obj = _extract_json(raw)
    kws: list[str] = []
    if isinstance(obj, dict):
        for k in obj.get("keywords") or []:
            k = _clean(k, 4)
            if not k:
                continue
            # drop betting-meta tokens that creep in (we want real-world chatter)
            k = " ".join(w for w in k.split() if w.lower() not in _KW_BANNED).strip()
            if k and k.lower() not in {x.lower() for x in kws}:
                kws.append(k)
    kws = kws[:n]
    if kws:
        print(f"[RESEARCH]: keywords -> {kws}")
    return kws or fallback


def _collect(queries: list[str], days: int, pages: int, min_faves: int) -> list[dict]:
    """Run each X advanced-search query on one shared client, merge + dedupe by
    text, return tweets highest-engagement first within the ``days`` window.
    Shared by event (keyword) and trader (name) research. ``[]`` on any problem."""
    try:
        from marketcast.xclient import Twitter
    except Exception as e:
        print(f"[RESEARCH]: unavailable ({e})")
        return []

    token = (settings.auth_token or "").strip()
    proxy = (settings.proxy or "").strip() or None
    if not token:
        print("[RESEARCH]: no AUTH_TOKEN — skipping")
        return []

    window_s = days * 86400
    try:
        tw = Twitter.from_token(token, proxy=proxy)
    except Exception as e:
        print(f"[RESEARCH]: client init failed ({str(e)[:60]})")
        return []

    by_key: dict[str, dict] = {}
    for q in queries:
        try:
            tweets = tw.read.search(
                q, count=30, product="Top", skip_retweets=True, pages=pages
            )
        except Exception as e:
            print(f"[RESEARCH]: search '{q[:24]}' failed ({str(e)[:40]})")
            continue
        for t in tweets:
            age = t.get("age_seconds")
            if age is not None and age > window_s:
                continue
            body = _clean_text(t.get("text"))
            if len(body) < 40:  # too thin to be a real headline
                continue
            key = body.lower()[:120]
            if key in by_key:
                continue
            by_key[key] = {
                "text": body,
                "author": t.get("author"),
                "likes": t.get("likes") or 0,
                "age_seconds": age,
                "url": t.get("url"),
            }
    return sorted(by_key.values(), key=lambda t: t["likes"], reverse=True)


def _gather_tweets(facts: dict) -> Any:
    """EVENT path: generate keywords, search X for each, merge + dedupe. Returns
    ``(tweets, keywords, queries, days)`` — freshest/highest-engagement first — or
    ``[]`` on any problem."""
    keywords = _keywords(facts)
    if not keywords:
        return []

    days, pages, min_faves = _days(), _pages(), _min_faves()
    queries = [_build_query(kw, days, min_faves) for kw in keywords]
    tweets = _collect(queries, days, pages, min_faves)
    return tweets[: _max_tweets()], keywords, queries, days


# ── LLM distillation ──────────────────────────────────────────────────────────
_SYSTEM = """You are a news desk analyst. You receive recent real tweets about a topic and \
distill ONLY what they actually say into a factual brief for a content creator.

RULES:
- Use ONLY information present in the tweets. Do not add outside knowledge, do not speculate.
- Be concrete and present-tense: what is happening right now around this topic.
- Prioritize the FRESHEST and most CONSEQUENTIAL developments; each point a distinct fact (no duplicates, no vague mood lines).
- PRESERVE attribution: if the tweets only "claim"/"report" something unverified, keep that hedge ("reportedly", "claims") — do not present a contested claim as confirmed.
- No hype, no opinion, no emojis, no hashtags, no @handles, no markdown.
- If the tweets are off-topic or say nothing substantive, return empty fields.
- Output STRICT JSON only, no preamble, no code fences."""


def _user_prompt(topic: str, tweets: list[dict]) -> str:
    listing = "\n".join(
        f"- (@{t['author']}, {t['likes']} likes) {t['text']}" for t in tweets
    )
    schema = (
        '{"summary":"2-3 sentence present-tense state of play, or empty string",'
        '"points":["<=18 word concrete development","..."],'
        '"sentiment":"one of: heating up | cooling off | mixed | quiet"}'
    )
    return (
        f"TOPIC: {topic}\n\nRECENT TWEETS:\n{listing}\n\n"
        f"Distill into EXACTLY this JSON shape:\n{schema}\n\n"
        "Give up to 7 points — the freshest, most important, distinct developments "
        "from the tweets, strongest first. JSON only."
    )


def _extract_json(text: str | None) -> Any:
    if not text:
        return None
    for start in (i for i, c in enumerate(text) if c == "{"):
        depth = 0
        for j in range(start, len(text)):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start: j + 1])
                    except Exception:
                        break
    return None


def _clean(s: Any, max_words: int = 18) -> str:
    s = re.sub(r"[#*`]", "", str(s or "")).strip().strip('"').strip()
    s = _WS_RE.sub(" ", s)
    return " ".join(s.split()[:max_words])


def _distill(topic: str, tweets: list[dict]) -> dict | None:
    """Ask the LLM for the digest. Returns ``{summary, points, sentiment}`` or None."""
    try:
        from marketcast.llm import call_llm
    except Exception as e:
        print(f"[RESEARCH]: model unavailable ({e})")
        return None
    try:
        raw = call_llm(_SYSTEM, _user_prompt(topic, tweets), max_tokens=400, temperature=0.3)
    except Exception as e:
        print(f"[RESEARCH]: distill call failed ({str(e)[:60]})")
        return None
    obj = _extract_json(raw)
    if not isinstance(obj, dict):
        print("[RESEARCH]: unparseable digest")
        return None
    summary = _clean(obj.get("summary"), 60)
    points = [_clean(p, 22) for p in (obj.get("points") or []) if str(p).strip()][:7]
    sentiment = _clean(obj.get("sentiment"), 4).lower()
    if not summary and not points:
        return None  # nothing substantive
    return {"summary": summary, "points": points, "sentiment": sentiment}


def _save(record: dict) -> None:
    """Persist a research run under ``settings.data_dir/research/`` (fail-open)."""
    try:
        _RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        (_RESEARCH_DIR / f"research_{ts}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[RESEARCH]: could not save: {e}")


def research_event(subject: dict) -> Research | None:
    """Gather + distill fresh news for an EVENT subject.

    Returns a digest dict or ``None`` (traders, disabled, no token, no fresh
    chatter, any failure)."""
    if not enabled():
        return None
    facts = (subject or {}).get("facts") or {}
    if facts.get("kind") != "event":
        return None

    got = _gather_tweets(facts)
    if not got or not got[0]:
        print("[RESEARCH]: no fresh chatter found — skipping")
        return None
    tweets, keywords, queries, days = got

    topic = ", ".join(keywords)
    digest = _distill(topic, tweets)
    if not digest:
        return None

    research = {
        "topic": keywords[0] if keywords else topic,
        "keywords": keywords,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "days": days,
        "summary": digest["summary"],
        "points": digest["points"],
        "sentiment": digest["sentiment"],
        "sources": [
            {"author": t["author"], "likes": t["likes"], "text": t["text"], "url": t.get("url")}
            for t in tweets[:8]
        ],
    }
    _save(
        {
            "keywords": keywords,
            "queries": queries,
            "tweet_count": len(tweets),
            "research": research,
        }
    )
    print(
        f"[RESEARCH]: {len(keywords)} keywords -> {len(tweets)} tweets -> "
        f"{len(digest['points'])} points, {digest['sentiment'] or 'n/a'}"
    )
    return research


# ── TRADER research ───────────────────────────────────────────────────────────
def _trader_name(facts: dict) -> str | None:
    """The trader's display name ONLY if it's actually searchable on X — i.e. a
    real handle/name, not a wallet-derived fallback like '0x12ab..cd' or '-'."""
    name = (facts.get("name") or "").strip()
    if not name or len(name) < 3:
        return None
    low = name.lower()
    if low.startswith("0x") or ".." in name or name == "-":
        return None  # wallet-derived alias -> not a real searchable name
    return name


def _gather_trader_tweets(facts: dict) -> Any:
    """TRADER path: search X for chatter about THIS trader, tightly scoped by name
    AND 'polymarket'. Returns ``(tweets, name, queries, days)`` or ``[]``."""
    name = _trader_name(facts)
    if not name:
        print("[RESEARCH]: trader name is a wallet alias, not searchable — skipping")
        return []

    days, pages, min_faves = _days(), _pages(), _min_faves()
    phrase = f'"{name}"' if " " in name else name
    queries = [
        _build_query(f"{phrase} polymarket", days, min_faves),
        _build_query(f"{phrase} polymarket (trader OR whale OR pnl OR profit)", days, min_faves),
    ]
    tweets = _collect(queries, days, pages, min_faves)
    return tweets[: _max_tweets()], name, queries, days


def research_trader(subject: dict) -> Research | None:
    """Gather + distill fresh X chatter ABOUT a TRADER subject. Mirrors
    :func:`research_event` and returns the same digest shape, or ``None``.

    NOTE: numbers in the post still come ONLY from FACTS — this digest is the
    qualitative reputation/news layer.
    """
    if not enabled():
        return None
    facts = (subject or {}).get("facts") or {}
    if facts.get("kind") != "trader":
        return None

    got = _gather_trader_tweets(facts)
    if not got or not got[0]:
        print("[RESEARCH]: no chatter about this trader — skipping")
        return None
    tweets, name, queries, days = got

    digest = _distill(f"the polymarket trader {name}", tweets)
    if not digest:
        return None

    research = {
        "topic": name,
        "keywords": [name],
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "days": days,
        "summary": digest["summary"],
        "points": digest["points"],
        "sentiment": digest["sentiment"],
        "sources": [
            {"author": t["author"], "likes": t["likes"], "text": t["text"], "url": t.get("url")}
            for t in tweets[:8]
        ],
    }
    _save(
        {
            "keywords": [name],
            "queries": queries,
            "tweet_count": len(tweets),
            "research": research,
        }
    )
    print(
        f"[RESEARCH]: trader '{name}' -> {len(tweets)} tweets -> "
        f"{len(digest['points'])} points, {digest['sentiment'] or 'n/a'}"
    )
    return research


def research_subject(subject: dict) -> Research | None:
    """Dispatch to the right research path by subject kind: events get topical news
    research, traders get reputation/chatter research. Returns a digest or
    ``None``. Callers can use this and not care which kind they have."""
    kind = ((subject or {}).get("facts") or {}).get("kind")
    if kind == "event":
        return research_event(subject)
    if kind == "trader":
        return research_trader(subject)
    return None


def research_as_block(research: Research | None) -> str:
    """Render the digest as a compact CURRENT CONTEXT block for a prompt. Both the
    tweet generator and the video-copy generator embed this verbatim."""
    if not research:
        return ""
    topic = ", ".join(research.get("keywords") or []) or research.get("topic", "")
    lines = [
        f'CURRENT CONTEXT — real-world news from X about "{topic}" '
        f"(last {research.get('days', 2)}d, as of {research.get('as_of', '')}). "
        f"Use it to make the piece timely and pick a relevant angle; you may "
        f"weave in one concrete development. It describes the real world, NOT "
        f"the market — all Polymarket metrics still come only from FACTS."
    ]
    if research.get("summary"):
        lines.append(f"now: {research['summary']}")
    for p in research.get("points") or []:
        lines.append(f"- {p}")
    if research.get("sentiment"):
        lines.append(f"momentum: {research['sentiment']}")
    return "\n".join(lines)


def top_quotes(research: Research | None, n: int = 3) -> list[dict]:
    """Pick the ``n`` highest-engagement REAL tweets behind this digest, with their
    links, so the user can quote-tweet them under the generated post. Skips any
    without a usable URL. Returns ``[]`` when there's no research."""
    if not research:
        return []
    out = []
    seen: set = set()
    for s in research.get("sources") or []:
        url = (s.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "author": s.get("author"),
                "likes": s.get("likes") or 0,
                "text": s.get("text", ""),
                "url": url,
            }
        )
    out.sort(key=lambda s: s["likes"], reverse=True)
    return out[:n]


if __name__ == "__main__":
    import sys

    from marketcast.markets.polymarket import pick_subject

    s = pick_subject(mode="event")
    if not s:
        print("no event subject")
        sys.exit(1)
    print(f"subject: {s['facts'].get('title')}\n")
    r = research_event(s)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if r:
        print("\n--- prompt block ---\n" + research_as_block(r))
