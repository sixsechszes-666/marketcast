"""Offline round-trip test for the subject-history helpers.

We point ``MARKETCAST_DATA_DIR`` at a temp dir BEFORE importing the package, so
the frozen, cached ``settings`` singleton (and the module-level history file
path derived from it) resolve to throwaway storage. No LLM / network.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _fresh_history(tmp_path: Path):
    """Import marketcast.generation.history bound to a temp data dir."""
    import os

    os.environ["MARKETCAST_DATA_DIR"] = str(tmp_path)
    # drop any already-imported marketcast modules so config re-reads the env
    for name in list(sys.modules):
        if name == "marketcast" or name.startswith("marketcast."):
            del sys.modules[name]
    return importlib.import_module("marketcast.generation.history")


def test_history_roundtrip(tmp_path):
    hist = _fresh_history(tmp_path)

    assert hist.count() == 0
    assert hist.used_ids() == set()

    trader = {"kind": "trader",
              "facts": {"kind": "trader", "addr": "0xABC123", "name": "whale",
                        "url": "https://polymarket.com/p/whale"}}
    event = {"kind": "event",
             "facts": {"kind": "event", "slug": "election-2026", "title": "Election",
                       "url": "https://polymarket.com/event/election-2026"}}

    hist.mark_used(trader)
    hist.mark_used(event)

    assert hist.count() == 2
    # trader ids are lowercased
    assert hist.used_ids() == {"trader:0xabc123", "event:election-2026"}


def test_history_idempotent_and_recent(tmp_path):
    hist = _fresh_history(tmp_path)

    trader = {"kind": "trader",
              "facts": {"kind": "trader", "addr": "0xDead", "name": "t",
                        "url": "https://polymarket.com/p/t"}}
    hist.mark_used(trader)
    hist.mark_used(trader)  # idempotent: same id, refreshes ts only
    assert hist.count() == 1

    event = {"kind": "event",
             "facts": {"kind": "event", "slug": "newest", "title": "Newest",
                       "url": "https://polymarket.com/event/newest"}}
    hist.mark_used(event)

    rec = hist.recent(1)
    assert len(rec) == 1
    assert rec[0]["id"] == "event:newest"  # most-recently used first


def test_history_skips_empty_id(tmp_path):
    hist = _fresh_history(tmp_path)
    # no addr / slug -> id is "trader:" / "event:" -> must be ignored
    hist.mark_used({"kind": "trader", "facts": {"kind": "trader"}})
    assert hist.count() == 0
