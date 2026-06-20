"""Offline tests for the pure text helpers in generation.generator.

These exercise formatting / scrubbing logic only — no LLM or network. We set a
temp data dir before import so the module's ``settings``-derived paths resolve to
throwaway storage.
"""
from __future__ import annotations

import importlib
import os
import sys


def _generator(tmp_path):
    os.environ["MARKETCAST_DATA_DIR"] = str(tmp_path)
    for name in list(sys.modules):
        if name == "marketcast" or name.startswith("marketcast."):
            del sys.modules[name]
    return importlib.import_module("marketcast.generation.generator")


def test_money(tmp_path):
    g = _generator(tmp_path)
    assert g._money(50) == "$50"
    assert g._money(999) == "$999"
    assert g._money(1500) == "$1.5k"
    assert g._money(2000) == "$2k"          # trailing .0k stripped
    assert g._money(2_500_000) == "$2.5m"
    assert g._money(3_000_000) == "$3m"     # trailing .0m stripped


def test_clip(tmp_path):
    g = _generator(tmp_path)
    assert g._clip("short", 80) == "short"
    assert g._clip("  padded  ") == "padded"
    long = "x" * 100
    clipped = g._clip(long, 10)
    assert len(clipped) == 10
    assert clipped.endswith("…")
    assert g._clip(None) == ""


def test_scrub_default_strips_and_splits(tmp_path):
    g = _generator(tmp_path)
    # em dash -> comma; banned "bet"/"betting" -> trade family; emojis removed
    out = g._scrub("he made a big bet 🚀 — and kept betting")
    assert "—" not in out
    assert "🚀" not in out
    assert "bet" not in out.lower().split()  # whole word gone
    assert "trade" in out

    # one-idea-per-line: glued sentences split, trailing periods dropped
    out2 = g._scrub("he won big. she lost more")
    lines = out2.splitlines()
    assert lines == ["he won big", "she lost more"]


def test_scrub_keeps_decimals_and_links(tmp_path):
    g = _generator(tmp_path)
    out = g._scrub("profit was 1.26x today\ncheck it: https://polymarket.com/x")
    # decimal point must survive (not treated as a sentence split)
    assert "1.26x" in out
    # the link line is preserved verbatim
    assert "https://polymarket.com/x" in out


def test_scrub_breakdown_allows_periods(tmp_path):
    g = _generator(tmp_path)
    text = "He made money. He kept it."
    out = g._scrub(text, allow_periods=True)
    # full sentences and terminal periods preserved in breakdown mode
    assert out == "He made money. He kept it."


def test_case_preserving_bet_swap(tmp_path):
    g = _generator(tmp_path)
    assert "$8K TRADE" in g._scrub("$8K BET")
    assert g._scrub("Bets are open").startswith("Trades")


def test_format_quotes(tmp_path):
    g = _generator(tmp_path)
    assert g.format_quotes(None) == ""
    assert g.format_quotes([]) == ""

    quotes = [{"author": "alice", "likes": 12, "text": "wild stuff here",
               "url": "https://x.com/alice/1"}]
    block = g.format_quotes(quotes)
    assert "quote one of these popular tweets" in block
    assert "@alice (12 likes): wild stuff here" in block
    assert "https://x.com/alice/1" in block


def test_format_quotes_truncates(tmp_path):
    g = _generator(tmp_path)
    long_text = "a" * 200
    block = g.format_quotes([{"author": "b", "likes": 0, "text": long_text,
                              "url": "u"}], width=20)
    # snippet trimmed to width with ellipsis
    assert "…" in block
    assert "a" * 200 not in block
