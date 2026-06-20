"""Kimi/duck.ai-written on-screen copy for the video dashboards.

The dashboards hardcode their hook line, verdict, and strengths/risks, so every
video reads the same. This generates fresh PROSE for those slots from the same
real facts; numbers stay data-driven inside the dashboard, the model only writes
words. Output is a small dict injected as ``window.__aiCopy`` by record.js::

    {
      "hook":      {"kicker": str, "headline": str, "sub": str},
      "verdict":   {"line1": str, "line2": str},
      "strengths": [str, str, str],
      "risks":     [str, str, str]
    }

If anything fails (no key, bad JSON), returns None and the dashboard falls back
to its built-in copy. Every result is saved under ``settings.data_dir`` for tuning.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from marketcast.config import settings
from marketcast.llm import call_nvidia

from .generator import _facts_block

_POST_DATA = settings.data_dir

_SYSTEM = """You write punchy on-screen copy for a vertical short-form video about Polymarket.
The video already shows the real numbers as big graphics; your job is the WORDS around them.

RULES:
- English. Confident, scroll-stopping, but not cringe. No emojis, no hashtags, no @handles, no markdown.
- Never state a number, percent, dollar figure or date that is not in FACTS. Prefer words over numbers; the screen shows the numbers.
- Keep it tight. A hook headline is a few words. Strengths/risks are short phrases.
- Output STRICT JSON only, no preamble, no code fences."""


def _user_prompt(subject: dict, research_block: str = "") -> str:
    facts = _facts_block(subject)
    if subject["kind"] == "trader":
        flavour = (
            "This is a video about ONE polymarket trader's track record. "
            "Make the hook bait people into watching the breakdown. "
            "strengths = what's impressive about this trader; risks = honest red flags."
        )
    else:
        flavour = (
            "This is a video about ONE polymarket market/event and its drama. "
            "Make the hook bait people into watching the odds breakdown. "
            "strengths = the bullish/for case; risks = the bearish/against case."
        )
    schema = (
        '{"hook":{"kicker":"1-3 WORD UPPERCASE LABEL","headline":"<=6 words, the bait",'
        '"sub":"<=10 words, why keep watching"},'
        '"verdict":{"line1":"<=8 words punchy takeaway","line2":"<=12 words follow-up"},'
        '"strengths":["<=9 words","<=9 words","<=9 words"],'
        '"risks":["<=9 words","<=9 words","<=9 words"]}'
    )
    ctx = f"{research_block}\n\n" if research_block else ""
    return (
        f"{flavour}\n\n{ctx}FACTS (the only real data you may reference):\n{facts}\n\n"
        f"Return EXACTLY this JSON shape, filled in:\n{schema}\n\n"
        "Polymarket metrics (odds, volume, profit, dates) come only from FACTS. If a "
        "CURRENT CONTEXT block is present, lean on it so the copy feels current and "
        "specific about the real-world story. No invented Polymarket numbers, no "
        "emojis. JSON only."
    )


def _extract_json(text):
    """Return the first parseable JSON object in ``text``.

    Models (esp. Claude via duck.ai) often wrap the JSON in ```json fences and
    emit several blocks with prose in between. A greedy {.*} grabs the whole span
    and fails to parse, so instead we brace-match each candidate object and
    return the first one that loads."""
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
                        return json.loads(text[start:j + 1])
                    except Exception:
                        break  # not valid from this start; try the next "{"
    return None


def _clean_line(s, max_words=14):
    s = re.sub(r"[#*`]", "", str(s or "")).strip().strip('"').strip()
    s = re.sub(r"\s+", " ", s)
    words = s.split()
    return " ".join(words[:max_words])


def _validate(d):
    """Coerce/validate the model JSON into the window.__aiCopy shape, or None."""
    if not isinstance(d, dict):
        return None
    hook = d.get("hook") or {}
    verdict = d.get("verdict") or {}
    strengths = [_clean_line(x, 10) for x in (d.get("strengths") or []) if str(x).strip()][:4]
    risks = [_clean_line(x, 10) for x in (d.get("risks") or []) if str(x).strip()][:4]
    out = {
        "hook": {
            "kicker": _clean_line(hook.get("kicker"), 4).upper()[:24] or None,
            "headline": _clean_line(hook.get("headline"), 8),
            "sub": _clean_line(hook.get("sub"), 14),
        },
        "verdict": {
            "line1": _clean_line(verdict.get("line1"), 10),
            "line2": _clean_line(verdict.get("line2"), 16),
        },
        "strengths": strengths,
        "risks": risks,
    }
    # require at least the hook headline to consider it usable
    if not out["hook"]["headline"]:
        return None
    return out


def _save(record):
    try:
        os.makedirs(_POST_DATA, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(_POST_DATA, f"copy_{ts}.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[COPY]: could not save: {e}")


def generate_dashboard_copy(subject: dict, *, research: dict | None = None,
                            max_retries: int = 2):
    """Return the ``window.__aiCopy`` dict for this subject, or None on failure.

    research: optional research digest — rendered into a CURRENT CONTEXT block so
    the video copy reflects what's happening right now; numbers still come only
    from FACTS."""
    research_block = ""
    if research:
        try:
            from marketcast.markets import research_as_block
            research_block = research_as_block(research)
        except Exception:
            research_block = ""
    user = _user_prompt(subject, research_block)
    for attempt in range(1, max_retries + 1):
        try:
            raw = call_nvidia(_SYSTEM, user, max_tokens=500,
                              temperature=0.9 + 0.05 * (attempt - 1))
        except Exception as e:
            print(f"[COPY]: model call failed ({e})")
            return None
        copy = _validate(_extract_json(raw))
        if copy:
            _save({"kind": subject["kind"], "url": subject["facts"].get("url"),
                   "used_research": bool(research_block), "research": research or None,
                   "prompt": user, "raw": raw, "copy": copy})
            return copy
        print(f"[COPY RETRY {attempt}/{max_retries}]: unparseable, retrying")
    return None
