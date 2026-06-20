"""Offline unit tests for the ``markets`` layer.

These exercise ONLY the pure helper / scoring / text functions with hardcoded
inputs. Nothing here touches the Polymarket or X network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``src/`` importable when running pytest from the repo root.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from marketcast.markets import polymarket as pm  # noqa: E402
from marketcast.markets import research as rs  # noqa: E402


# ── polymarket._num ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value, expected",
    [
        (3, 3.0),
        ("3.5", 3.5),
        ("1,000", 0.0),       # comma -> not parseable
        (None, 0.0),
        ("abc", 0.0),
        (float("inf"), 0.0),
        (float("nan"), 0.0),
        ("-2", -2.0),
    ],
)
def test_num(value, expected):
    assert pm._num(value) == expected


# ── polymarket._safe_json ─────────────────────────────────────────────────────
def test_safe_json_passthrough_and_parse():
    assert pm._safe_json([1, 2], "fb") == [1, 2]
    assert pm._safe_json({"a": 1}, "fb") == {"a": 1}
    assert pm._safe_json('["0.6","0.4"]', None) == ["0.6", "0.4"]
    assert pm._safe_json("not json", "fb") == "fb"
    assert pm._safe_json(None, "fb") == "fb"


# ── polymarket._clean_name ────────────────────────────────────────────────────
def test_clean_name_uses_real_name():
    assert pm._clean_name("Satoshi", "pseudo", "0xabc123def4567890") == "Satoshi"


def test_clean_name_falls_back_to_pseudo_for_address():
    assert pm._clean_name("0xdeadbeef", "WhaleGuy", "0xabc123def4567890") == "WhaleGuy"


def test_clean_name_short_wallet_when_nothing_else():
    out = pm._clean_name("", None, "0xabcdef1234567890")
    assert out == "0xabcd..7890"


def test_clean_name_rejects_overlong_blob():
    blob = "x" * 30
    assert pm._clean_name(blob, "Nick", "0xabcdef1234567890") == "Nick"


# ── polymarket._clean_q ───────────────────────────────────────────────────────
def test_clean_q_strips_will_and_qmark():
    assert pm._clean_q("Will Bitcoin hit 100k?") == "Bitcoin hit 100k"
    assert pm._clean_q(None) == ""


# ── polymarket._pnl_since ─────────────────────────────────────────────────────
def test_pnl_since_window():
    day = 86400
    series = [
        {"t": 0, "p": 0.0},
        {"t": 10 * day, "p": 100.0},
        {"t": 20 * day, "p": 250.0},
        {"t": 30 * day, "p": 400.0},
    ]
    # last point is at t=30d, p=400. 7 days back -> base at t=20d (p=250) -> 150
    assert pm._pnl_since(series, 7) == 150.0
    # whole-series change
    assert pm._pnl_since(series, 365) == 400.0
    assert pm._pnl_since([], 7) == 0.0


# ── polymarket._event_hype ────────────────────────────────────────────────────
def _base_event_facts(**over):
    f = {
        "lead_pct": 50.0,
        "days_left": 30,
        "is_multi": False,
        "field": [{"name": "YES", "yes": 0.5, "chg24h": 0.0}],
        "volume": 1_000_000,
        "volume24h": 0,
        "chg24h_pts": 0.0,
        "comments": 0,
    }
    f.update(over)
    return f


def test_event_hype_none_is_zero():
    assert pm._event_hype(None) == 0.0


def test_event_hype_decided_market_is_zero():
    # near-certain leader -> already decided -> killed
    assert pm._event_hype(_base_event_facts(lead_pct=99.0)) == 0.0


def test_event_hype_rewards_volume_and_freshness():
    fresh = _base_event_facts(volume=100_000, volume24h=100_000)
    stale = _base_event_facts(volume=10_000_000, volume24h=100_000)
    # same 24h volume, but the fresh one (all of lifetime volume is today) scores higher
    assert pm._event_hype(fresh) > pm._event_hype(stale)


def test_event_hype_neck_and_neck_bonus():
    tight = _base_event_facts(
        is_multi=True,
        volume24h=50_000,
        field=[
            {"name": "A", "yes": 0.51, "chg24h": 0.0},
            {"name": "B", "yes": 0.49, "chg24h": 0.0},
        ],
        lead_pct=51.0,
    )
    blowout = _base_event_facts(
        is_multi=True,
        volume24h=50_000,
        field=[
            {"name": "A", "yes": 0.80, "chg24h": 0.0},
            {"name": "B", "yes": 0.20, "chg24h": 0.0},
        ],
        lead_pct=80.0,
    )
    assert pm._event_hype(tight) > pm._event_hype(blowout)


# ── polymarket._trader_hype ───────────────────────────────────────────────────
def _base_trader_facts(**over):
    f = {
        "total_pnl": 0,
        "roi_pct": 0.0,
        "mult_hi_pct": 0,
        "focus_topic": None,
        "days_active": 0,
    }
    f.update(over)
    return f


def test_trader_hype_zero_when_no_profit():
    assert pm._trader_hype(_base_trader_facts(total_pnl=0)) == 0.0
    assert pm._trader_hype(_base_trader_facts(total_pnl=-500)) == 0.0
    assert pm._trader_hype(None) == 0.0


def test_trader_hype_scales_with_profit_and_niche():
    plain = pm._trader_hype(_base_trader_facts(total_pnl=50_000))
    niche = pm._trader_hype(_base_trader_facts(total_pnl=50_000, focus_topic="iran"))
    assert niche > plain  # +15 for clean niche
    assert plain > 0


def test_trader_hype_fast_turnaround_bonus():
    fast = _base_trader_facts(total_pnl=5000, days_active=20)
    slow = _base_trader_facts(total_pnl=5000, days_active=400)
    assert pm._trader_hype(fast) > pm._trader_hype(slow)


# ── polymarket._theme_words / _diversify ──────────────────────────────────────
def test_theme_words_stems_and_drops_stopwords():
    words = pm._theme_words({"title": "Will Iran strike Israel", "tag": "Politics"})
    # "iran" kept, "israel"->"isra" stem, "strike"->"stri" stem, "will" stop-dropped
    assert "iran" in words
    assert "will" not in words


def test_diversify_pushes_duplicate_theme_to_back():
    a = ({"title": "Iran nuclear deal"}, 100.0)
    b = ({"title": "Bitcoin to 100k"}, 90.0)
    c = ({"title": "Iran airspace closed"}, 80.0)  # same theme as a
    out = pm._diversify([a, b, c])
    # first two are distinct themes; the iran duplicate is benched to the end
    assert out[0] is a
    assert out[1] is b
    assert out[2] is c


# ── polymarket._pick_json / _extract_pick_index ───────────────────────────────
def test_pick_json_first_object():
    assert pm._pick_json('garbage {"pick": 2} more') == {"pick": 2}
    assert pm._pick_json("no json here") is None
    assert pm._pick_json("") is None


def test_extract_pick_index_strict_json():
    idx, why = pm._extract_pick_index('{"pick": 1, "why": "big swing"}', 3)
    assert idx == 1
    assert why == "big swing"


def test_extract_pick_index_loose_regex():
    idx, why = pm._extract_pick_index('pick=2, why="longshot in play"', 3)
    assert idx == 2
    assert why == "longshot in play"


def test_extract_pick_index_out_of_range():
    idx, why = pm._extract_pick_index('{"pick": 9}', 3)
    assert idx is None and why is None


# ── research._topic_from_title ─────────────────────────────────────────────────
def test_topic_from_title():
    assert rs._topic_from_title("Will Trump win by Dec 31, 2026?") == "Trump win"
    assert rs._topic_from_title(None) is None
    assert rs._topic_from_title("   ") is None


# ── research._build_query ─────────────────────────────────────────────────────
def test_build_query_basic():
    q = rs._build_query("iran strike", 2, 0)
    assert "iran strike" in q
    assert "within_time:2d" in q
    assert "lang:en" in q
    assert "min_faves" not in q


def test_build_query_with_min_faves():
    q = rs._build_query("topic", 3, 10)
    assert "within_time:3d" in q
    assert "min_faves:10" in q


# ── research._clean / _clean_text ─────────────────────────────────────────────
def test_clean_strips_markdown_and_truncates():
    assert rs._clean("**hello** `world` foo bar baz", max_words=3) == "hello world foo"
    assert rs._clean(None) == ""


def test_clean_text_removes_urls_and_collapses_ws():
    out = rs._clean_text("check   this https://x.com/a/b out")
    assert out == "check this out"


# ── research._extract_json ────────────────────────────────────────────────────
def test_extract_json_from_fenced_text():
    raw = '```json\n{"summary": "hi", "points": []}\n```'
    obj = rs._extract_json(raw)
    assert obj == {"summary": "hi", "points": []}
    assert rs._extract_json("nothing") is None


# ── research._trader_name ─────────────────────────────────────────────────────
def test_trader_name_accepts_real_name():
    assert rs._trader_name({"name": "WhaleKing"}) == "WhaleKing"


@pytest.mark.parametrize("name", ["0xabc..def", "ab", "-", "", "0xdeadbeef"])
def test_trader_name_rejects_aliases(name):
    assert rs._trader_name({"name": name}) is None


# ── research.research_as_block ────────────────────────────────────────────────
def test_research_as_block_empty():
    assert rs.research_as_block(None) == ""
    assert rs.research_as_block({}) == ""


def test_research_as_block_renders_sections():
    research = {
        "keywords": ["iran"],
        "days": 2,
        "as_of": "2026-06-18",
        "summary": "tensions rising",
        "points": ["airspace closed", "talks stalled"],
        "sentiment": "heating up",
    }
    block = rs.research_as_block(research)
    assert "CURRENT CONTEXT" in block
    assert "now: tensions rising" in block
    assert "- airspace closed" in block
    assert "momentum: heating up" in block


# ── research.top_quotes ───────────────────────────────────────────────────────
def test_top_quotes_sorts_dedupes_limits():
    research = {
        "sources": [
            {"author": "a", "likes": 5, "text": "t1", "url": "u1"},
            {"author": "b", "likes": 50, "text": "t2", "url": "u2"},
            {"author": "c", "likes": 99, "text": "t3", "url": ""},   # no url -> skipped
            {"author": "d", "likes": 30, "text": "t4", "url": "u2"},  # dup url -> skipped
            {"author": "e", "likes": 10, "text": "t5", "url": "u5"},
        ]
    }
    out = rs.top_quotes(research, n=2)
    assert [q["url"] for q in out] == ["u2", "u5"]  # top-2 by likes: u2(50), u5(10)
    assert out[0]["likes"] == 50


def test_top_quotes_empty():
    assert rs.top_quotes(None) == []
    assert rs.top_quotes({"sources": []}) == []
