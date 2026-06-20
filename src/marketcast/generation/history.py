"""Subject history — remember which Polymarket subjects we've already turned into
a post, so auto-pick doesn't describe the same event/trader twice.

Stored in ``settings.data_dir/used_subjects.json`` as
``{"used": [ {id, kind, label, url, ts} ]}``. The id is stable and unique per
subject::

    event  -> "event:<slug>"
    trader -> "trader:<wallet>"

``pick_subject(exclude_used=True)`` drops any candidate whose id is already here;
the orchestrators call :func:`mark_used` once a post has been generated for it.
The whole feature is opt-in and every function is fail-open — a read/write error
never breaks generation.
"""
from __future__ import annotations

import json
import time
from typing import Any

from marketcast.config import settings

_FILE = settings.data_dir / "used_subjects.json"


def _facts(obj: Any) -> dict:
    """Accept either a subject dict ({kind,url,facts,...}) or a bare facts dict."""
    if isinstance(obj, dict):
        return obj.get("facts", obj)
    return {}


def subject_id(obj: Any) -> str:
    """Stable, unique id for a subject (``trader:<addr>`` / ``event:<slug>``)."""
    f = _facts(obj)
    if f.get("kind") == "trader":
        return "trader:" + (f.get("addr") or "").lower()
    return "event:" + (f.get("slug") or f.get("url") or "")


def load_used() -> dict:
    """Return ``{id: record}``. Empty on any problem (missing file, bad JSON)."""
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        return {r["id"]: r for r in data.get("used", []) if r.get("id")}
    except Exception:
        return {}


def used_ids() -> set:
    """Set of subject ids already marked used."""
    return set(load_used().keys())


def recent(n: int) -> list:
    """The most recently used records (newest first), up to ``n``. Used for the
    theme cooldown in pick_subject."""
    recs = sorted(load_used().values(), key=lambda r: r.get("ts", 0), reverse=True)
    return recs[:max(n, 0)]


def _save(used: dict) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        recs = sorted(used.values(), key=lambda r: r.get("ts", 0))
        _FILE.write_text(
            json.dumps({"used": recs}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[HISTORY]: could not save used subjects: {e}")


def mark_used(subject: Any) -> None:
    """Record a subject as already described. Idempotent (refreshes ts)."""
    sid = subject_id(subject)
    if not sid or sid in ("event:", "trader:"):
        return
    f = _facts(subject)
    used = load_used()
    used[sid] = {
        "id": sid,
        "kind": f.get("kind"),
        "label": f.get("title") or f.get("name") or "",
        "tag": f.get("tag"),
        "focus_topic": f.get("focus_topic"),
        "url": f.get("url"),
        "ts": time.time(),
    }
    _save(used)


def count() -> int:
    """Number of subjects currently marked used."""
    return len(load_used())
