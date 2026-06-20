"""Polymarket public-API fetchers, hype scoring and subject selection.

Everything here uses the same public, CORS-open endpoints the recorder
dashboards use (gamma / data-api / user-pnl-api / lb-api / clob). No keys.

Two subject kinds:
  - ``"event"``  : a trending market (gamma ``/events`` by 24h volume).
  - ``"trader"`` : a real wallet active on a hot market (data-api ``/holders`` ->
                   full per-wallet stats, same maths as the recorder dashboard).

:func:`pick_subject` scores candidates by a "will this pop on Twitter" heuristic
and returns the single best :class:`~marketcast.markets.models.Subject`. All
numbers in ``facts`` are real (computed here) so the LLM never invents a figure.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from typing import Any

import requests

from marketcast.markets.models import Subject

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
PNL = "https://user-pnl-api.polymarket.com"
LB = "https://lb-api.polymarket.com"

# Polymarket referral — applied to both profile and event links.
# Current signup-link format: ...?modal=signup&mt=1&via=<code>
REFERRAL_CODE = "007"
REFERRAL_PARAM = "via"
REFERRAL_QS = f"modal=signup&mt=1&{REFERRAL_PARAM}={REFERRAL_CODE}"

_TIMEOUT = 12


def _get(url: str, fallback: Any = None) -> Any:
    """GET a public Polymarket JSON endpoint, returning ``fallback`` on failure."""
    try:
        r = requests.get(url, headers={"Accept": "application/json"}, timeout=_TIMEOUT)
        if not r.ok:
            return fallback
        return r.json()
    except Exception as e:
        print(f"[API ERR]: {url[:80]} -> {e}")
        return fallback


def _num(v: Any) -> float:
    """Coerce ``v`` to a finite float, or ``0.0`` if it is not a real number."""
    try:
        n = float(v)
        return n if n == n and n not in (float("inf"), float("-inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_json(v: Any, fb: Any) -> Any:
    """Return ``v`` if it is already a list/dict, else JSON-parse it, else ``fb``."""
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return fb


def _clean_name(name: str | None, pseudo: str | None, wallet: str | None) -> str:
    """A human-readable display name — falls back to pseudonym, then short
    wallet, when ``name`` is itself an address or an over-long blob."""
    short = f"{wallet[:6]}..{wallet[-4:]}" if wallet else "-"
    if not name or re.match(r"^0x[a-f0-9]", name, re.I) or len(name) > 22:
        return pseudo or short
    return name


# ── EVENT SIDE ──────────────────────────────────────────────────────────────
def trending_events(limit: int = 12) -> list[dict]:
    """Hottest open events by 24h volume."""
    evs = _get(
        f"{GAMMA}/events?order=volume24hr&ascending=false&closed=false&limit={limit}",
        [],
    )
    return evs if isinstance(evs, list) else []


# Ranking axes merged for discovery. 24h volume alone clusters on a handful of
# mega-themes (the "always Iran" problem); adding the weekly and liquidity
# leaders pulls in markets that those axes surface but the 24h list never does.
_DISCOVERY_AXES = ("volume24hr", "volume1wk", "liquidity")


def discover_events(limit: int = 30) -> list[dict]:
    """Trending events merged across several ranking axes, deduped by slug.

    Each axis contributes its top slice, so the candidate pool reflects more than
    just 'what had the most dollars traded in the last 24h'. Falls back to plain
    24h trending if the multi-axis fetch turns up nothing.
    """
    per_axis = max(limit, 12)
    seen: set = set()
    merged: list[dict] = []
    for axis in _DISCOVERY_AXES:
        evs = _get(
            f"{GAMMA}/events?order={axis}&ascending=false&closed=false&limit={per_axis}",
            [],
        )
        if not isinstance(evs, list):
            continue
        for ev in evs:
            slug = ev.get("slug")
            if slug and slug not in seen:
                seen.add(slug)
                merged.append(ev)
    return merged or trending_events(limit)


def _clean_q(q: str | None) -> str:
    return re.sub(r"\?$", "", re.sub(r"^Will\s+", "", q or "", flags=re.I)).strip()


def event_facts(ev: dict) -> dict | None:
    """Compact, real fact set for one gamma event object (port of computeEvent)."""
    markets = []
    for m in ev.get("markets") or []:
        if not m.get("outcomes"):
            continue
        prices = [_num(x) for x in _safe_json(m.get("outcomePrices"), [])]
        markets.append({**m, "_yes": prices[0] if prices else 0.0})
    if not markets:
        return None
    markets.sort(key=lambda m: m["_yes"], reverse=True)
    primary = markets[0]
    is_multi = len(markets) > 1

    def field_name(m: dict) -> str:
        git = m.get("groupItemTitle")
        if git and not re.match(r"^\d", git):
            return git
        return _clean_q(m.get("question"))

    lead = field_name(primary) if is_multi else "YES"
    field = [
        {"name": field_name(m), "yes": m["_yes"], "chg24h": _num(m.get("oneDayPriceChange"))}
        for m in markets[:6]
    ]

    end_ms = 0
    for d in (primary.get("endDate"), ev.get("endDate")):
        if d:
            try:
                end_ms = int(time.mktime(time.strptime(d[:19], "%Y-%m-%dT%H:%M:%S"))) * 1000
                break
            except Exception:
                pass
    days_left = max(0, int((end_ms / 1000 - time.time()) / 86400)) if end_ms else 0

    # runner-up + gap for multi-outcome races
    runner_up = field[1] if (is_multi and len(field) >= 2) else None
    gap_pts = round((field[0]["yes"] - field[1]["yes"]) * 100, 1) if runner_up else 0.0

    # first meaningful category tag (politics / crypto / sports / ...)
    tag = None
    for tg in ev.get("tags") or []:
        lbl = (tg.get("label") or "").strip()
        if lbl and lbl.lower() not in ("all", "trending", "recurring"):
            tag = lbl
            break

    return {
        "kind": "event",
        "slug": ev.get("slug"),
        "title": ev.get("title"),
        "url": f"https://polymarket.com/event/{ev.get('slug')}?{REFERRAL_QS}",
        "is_multi": is_multi,
        "outcome_count": len(markets),
        "lead_name": lead,
        "lead_pct": round(primary["_yes"] * 100, 1),
        "runner_up_name": runner_up["name"] if runner_up else None,
        "runner_up_pct": round(runner_up["yes"] * 100, 1) if runner_up else 0.0,
        "gap_pts": gap_pts,
        "chg24h_pts": round(_num(primary.get("oneDayPriceChange")) * 100, 1),
        "chg1w_pts": round(_num(primary.get("oneWeekPriceChange")) * 100, 1),
        "volume": round(_num(ev.get("volume"))),
        "volume24h": round(_num(ev.get("volume24hr"))),
        "volume_1wk": round(_num(ev.get("volume1wk"))),
        "open_interest": round(_num(ev.get("openInterest"))),
        "comments": int(_num(ev.get("commentCount"))),
        "days_left": days_left,
        "tag": tag,
        "field": field,
        "question": primary.get("question"),
        "_cid": primary.get("conditionId"),
    }


def _event_hype(f: dict | None) -> float:
    """Higher = more likely to pull views. Volume + drama + recognizability."""
    if not f:
        return 0.0
    # already-decided markets are weak bait: a near-100% (or 0-days-left)
    # leader means the race is over — kill it unless it's a tight multi.
    decided = f["lead_pct"] >= 98 or (f["days_left"] == 0 and f["lead_pct"] >= 90)
    if f["is_multi"] and len(f["field"]) >= 2:
        # two-or-more outcomes already pinned at ~100% = degenerate / settled race
        pinned = sum(1 for o in f["field"] if o["yes"] >= 0.98)
        decided = pinned >= 2 or (decided and (f["field"][0]["yes"] - f["field"][1]["yes"]) > 0.06)
    if decided:
        return 0.0
    score = 0.0
    # raw size matters, but it shouldn't decide on its own — what goes viral is
    # MOVEMENT, not the perennial heavyweight.
    score += min(f["volume24h"], 3_000_000) / 60_000  # up to ~50
    # freshness: share of the market's entire lifetime volume that traded in the
    # last 24h. A market where most of the action is TODAY just caught fire.
    if f["volume"] > 10_000:
        fresh_ratio = f["volume24h"] / f["volume"]
        score += min(fresh_ratio, 1.0) * 60  # up to +60 if it's all today
    # drama only counts if people are actually trading it.
    liquid = f["volume24h"] >= 25_000
    if liquid and f["is_multi"] and len(f["field"]) >= 2:
        gap = f["field"][0]["yes"] - f["field"][1]["yes"]
        if gap < 0.06:
            score += 40  # neck-and-neck
    big_move = abs(f["chg24h_pts"])
    if liquid and big_move >= 5:
        score += big_move * 3  # something swung hard
    if liquid and 5 <= f["lead_pct"] <= 35:
        score += 25  # longshot in play
    score += min(f["comments"], 2000) / 40  # crowd attention
    return score


# ── TRADER SIDE ─────────────────────────────────────────────────────────────
def top_holders(condition_id: str, limit: int = 12) -> list[str]:
    """De-duplicated, order-preserving list of wallets holding a market."""
    rows = _get(f"{DATA}/holders?market={condition_id}&limit={limit}", [])
    out = []
    for grp in rows or []:
        for h in grp.get("holders") or []:
            w = h.get("proxyWallet")
            if w:
                out.append(w.lower())
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


def _pnl_since(series: list[dict], days: int) -> float:
    """Profit change over the trailing ``days`` of a PnL time series."""
    if not series:
        return 0.0
    last = series[-1]
    cutoff = last["t"] - days * 86400
    base = series[0]["p"]
    for pt in series:
        if pt["t"] <= cutoff:
            base = pt["p"]
        else:
            break
    return last["p"] - base


def trader_facts(addr: str) -> dict | None:
    """Real per-wallet stats (port of Dashboard.html loadData + computeStats)."""
    pos = _get(
        f"{DATA}/positions?user={addr}&sizeThreshold=.01&limit=500"
        f"&sortBy=CASHPNL&sortDirection=DESC",
        [],
    )
    trades = _get(f"{DATA}/trades?user={addr}&limit=200", [])
    pnl = _get(f"{PNL}/user-pnl?user_address={addr}&interval=all&fidelity=1d", [])
    value = _get(f"{DATA}/value?user={addr}", [])
    traded = _get(f"{DATA}/traded?user={addr}", None)
    vol = _get(f"{LB}/volume?window=all&limit=1&address={addr}", [])

    pos = pos if isinstance(pos, list) else []
    trades = trades if isinstance(trades, list) else []
    ser = [
        {"t": int(_num(p.get("t"))), "p": _num(p.get("p"))}
        for p in (pnl if isinstance(pnl, list) else [])
    ]

    first = trades[0] if trades else {}
    name = _clean_name(first.get("name"), first.get("pseudonym"), addr)

    total_pnl = ser[-1]["p"] if ser else 0.0
    pnl30 = _pnl_since(ser, 30)
    volume = _num(vol[0]["amount"]) if isinstance(vol, list) and vol else 0.0
    portfolio = _num(value[0]["value"]) if isinstance(value, list) and value else 0.0
    markets = (
        int(_num(traded.get("traded")))
        if isinstance(traded, dict) and traded
        else len({t.get("conditionId") for t in trades})
    )
    roi = (total_pnl / volume * 100) if volume > 1 else 0.0

    # per-position multipliers (winners) -> "from 500% to 12,300% profit"
    mults = []
    for p in pos:
        inv = _num(p.get("initialValue"))
        cp = _num(p.get("cashPnl"))
        if inv > 1 and cp > 0:
            mults.append(cp / inv * 100)
    mult_lo = round(min(mults)) if mults else 0
    mult_hi = round(max(mults)) if mults else 0

    # focus topic: most common keyword across position/trade titles
    titles = [(p.get("title") or "") for p in pos] + [(t.get("title") or "") for t in trades]
    stop = {
        "will", "the", "be", "to", "in", "on", "by", "of", "a", "an", "and",
        "or", "vs", "2024", "2025", "2026", "win", "market", "yes", "no", "this",
    }
    words: Counter = Counter()
    for t in titles:
        for w in re.findall(r"[A-Za-z]{3,}", t.lower()):
            if w not in stop:
                words[w] += 1
    focus = words.most_common(1)[0][0] if words else None

    # active-since (first pnl point)
    days_active = int((time.time() - ser[0]["t"]) / 86400) if ser else 0

    # recent momentum
    pnl7 = _pnl_since(ser, 7)
    per_day = round(pnl30 / 30) if pnl30 else 0

    # positions in profit (real, from cashPnl sign) — only count meaningful ones
    closed = [p for p in pos if abs(_num(p.get("cashPnl"))) > 0.5]
    pos_up = sum(1 for p in closed if _num(p.get("cashPnl")) > 0)
    pos_total = len(closed)

    # best position by $ profit, worst by $ loss, biggest single stake
    best = max(pos, key=lambda p: _num(p.get("cashPnl")), default=None)
    worst = min(pos, key=lambda p: _num(p.get("cashPnl")), default=None)
    biggest = max(pos, key=lambda p: _num(p.get("initialValue")), default=None)

    best_trade = None
    if best and _num(best.get("cashPnl")) > 0:
        bi = _num(best.get("initialValue"))
        best_trade = {
            "title": (best.get("title") or "").strip(),
            "pnl": round(_num(best.get("cashPnl"))),
            "mult_pct": round(_num(best.get("cashPnl")) / bi * 100) if bi > 1 else 0,
        }
    biggest_bet = None
    if biggest and _num(biggest.get("initialValue")) > 1:
        biggest_bet = {
            "usd": round(_num(biggest.get("initialValue"))),
            "title": (biggest.get("title") or "").strip(),
        }
    worst_loss = (
        round(_num(worst.get("cashPnl")))
        if worst and _num(worst.get("cashPnl")) < 0
        else 0
    )
    avg_bet = round(volume / len(trades)) if trades else 0

    return {
        "kind": "trader",
        "addr": addr,
        "name": name,
        "url": f"https://polymarket.com/@{addr}?{REFERRAL_QS}",
        "total_pnl": round(total_pnl),
        "pnl_30d": round(pnl30),
        "pnl_7d": round(pnl7),
        "profit_per_day": per_day,
        "roi_pct": round(roi, 1),
        "volume": round(volume),
        "portfolio": round(portfolio),
        "markets_traded": markets,
        "trade_count": len(trades),
        "open_positions": len(pos),
        "avg_bet": avg_bet,
        "positions_up": pos_up,
        "positions_total": pos_total,
        "mult_lo_pct": mult_lo,
        "mult_hi_pct": mult_hi,
        "best_trade": best_trade,
        "biggest_bet": biggest_bet,
        "worst_loss_usd": worst_loss,
        "focus_topic": focus,
        "days_active": days_active,
    }


def _trader_hype(f: dict | None) -> float:
    """Reward the 'tiny stake -> big result' story: high ROI, niche focus,
    eye-catching absolute profit, fast turnaround."""
    if not f or f["total_pnl"] <= 0:
        return 0.0
    score = 0.0
    score += min(f["total_pnl"], 500_000) / 5_000  # absolute profit
    score += min(max(f["roi_pct"], 0), 300) / 3  # return on volume
    if f["mult_hi_pct"] >= 1000:
        score += min(f["mult_hi_pct"], 20_000) / 200  # jaw-drop multiplier
    if f["focus_topic"]:
        score += 15  # clean niche angle
    if 0 < f["days_active"] <= 60 and f["total_pnl"] > 2000:
        score += 30  # "in a month he made..."
    return score


# ── SELECTION ────────────────────────────────────────────────────────────────
# Generic, non-distinctive words stripped before computing a subject's "theme".
_THEME_STOP = {
    "will", "the", "by", "in", "on", "of", "and", "or", "vs", "to", "be", "a",
    "an", "is", "are", "at", "for", "with", "winner", "win", "wins", "next",
    "when", "what", "which", "who", "how", "many", "price", "hit", "hits",
    "above", "below", "reach", "decision", "market", "markets", "this", "that",
    "new", "get", "gets", "day", "days", "week", "month", "year", "end", "ends",
    "close", "closes", "through", "continue", "continues", "world", "champion",
    "nominee", "election", "elections", "presidential", "2024", "2025", "2026",
    "2027", "2028", "2029", "2030",
}


def _theme_words(f: dict) -> set:
    """Distinctive words that identify a subject's TOPIC — used to keep one theme
    from monopolising the shortlist. Pulled from title, category tag, focus topic."""
    text = " ".join(
        str(x or "")
        for x in (f.get("title"), f.get("name"), f.get("tag"), f.get("focus_topic"))
    )
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)}
    # light stem (4-char prefix for longer words) so inflections cluster.
    words = {w[:4] if len(w) >= 6 else w for w in words}
    return words - _THEME_STOP


def _diversify(cands: list[tuple]) -> list[tuple]:
    """Reorder ``(facts, hype)`` candidates so the front is theme-diverse: greedily
    keep the highest-ranked subject of each theme, push theme-duplicates to the
    back. Front-loads variety without dropping anyone."""
    kept, dupes, seen = [], [], set()
    for item in cands:
        words = _theme_words(item[0])
        if words & seen:
            dupes.append(item)
        else:
            kept.append(item)
            seen |= words
    return kept + dupes


def pick_subject(
    mode: str = "auto",
    *,
    event_pool: int = 8,
    holders_per_event: int = 4,
    trader_events: int = 2,
    shortlist: int = 4,
    social_checks: int = 5,
    exclude_used: bool = False,
    theme_cooldown: int = 4,
    pool_wallets: int = 5,
) -> Subject | None:
    """Discover candidates, score them, return the single best subject dict.

    Three-stage pick:
      1. cheap numeric heuristic ranks all candidates;
      2. the top ``social_checks`` get a REAL X-chatter multiplier and are
         re-ranked by heuristic x social signal;
      3. an LLM judge chooses the most scroll-stopping one among the top
         ``shortlist``.
    Both stages 2 and 3 are fail-open — any problem falls back to the heuristic
    ranking, so behaviour never regresses below the old logic.

    ``exclude_used``: when True, drop any subject already used (via the
    ``generation.history`` layer) AND apply the theme cooldown. The pool is
    widened to leave room. Returns ``None`` if everything fresh is exhausted.

    ``mode``: ``"auto"`` (mix of trader/event), ``"trader"``, or ``"event"``.
    """
    used: set = set()
    cooldown_themes: set = set()
    _sid = None
    if exclude_used:
        try:
            # Lazy, guarded import of the history layer (owned by another agent)
            # — mirrors how the original wired subject_history optionally.
            from marketcast.generation.history import recent, subject_id as _sid, used_ids

            used = used_ids()
            # theme cooldown: bench the topics of the last `theme_cooldown` picks
            # so a dominant theme can't recur run after run.
            if theme_cooldown > 0:
                for r in recent(theme_cooldown):
                    cooldown_themes |= _theme_words(
                        {
                            "title": r.get("label"),
                            "tag": r.get("tag"),
                            "focus_topic": r.get("focus_topic"),
                        }
                    )
        except Exception as e:
            print(f"[HISTORY]: dedup unavailable ({e})")
            exclude_used = False
    # widen the pool when deduping so fresh, theme-varied options remain
    pool = max(event_pool, 30) if exclude_used else event_pool

    evs = discover_events(pool)
    if not evs:
        return None

    def _skip(f: dict) -> bool:
        if not exclude_used:
            return False
        if _sid is not None and _sid({"facts": f}) in used:
            return True
        return bool(cooldown_themes and (_theme_words(f) & cooldown_themes))

    candidates: list[tuple[dict, float]] = []  # list of (facts, hype)

    if mode in ("auto", "event"):
        for ev in evs:
            f = event_facts(ev)
            if f and not _skip(f):
                candidates.append((f, _event_hype(f)))

    if mode in ("auto", "trader"):
        tried: set = set()
        # (a) holders of the hottest few events — traders active on what's hot now
        for ev in evs[:trader_events]:
            markets = ev.get("markets") or []
            cid = markets[0].get("conditionId") if markets else None
            if not cid:
                continue
            for w in top_holders(cid, limit=holders_per_event * 2)[:holders_per_event]:
                if w in tried:
                    continue
                tried.add(w)
                tf = trader_facts(w)
                if tf and not _skip(tf):
                    candidates.append((tf, _trader_hype(tf)))
        # (b) remembered high-quality wallets — decoupled from the current event.
        # NOTE: the original wallet_pool persistence is out of scope for the
        # focused rewrite, so the optional pool is guarded and simply skipped if
        # the module is absent (behaviour: holder-discovery only).
        if pool_wallets > 0:
            try:
                from marketcast.markets.wallet_pool import top as _pool_top

                for w in _pool_top(pool_wallets, exclude=tried):
                    tried.add(w)
                    tf = trader_facts(w)
                    if tf and not _skip(tf):
                        candidates.append((tf, _trader_hype(tf)))
            except Exception as e:
                print(f"[WALLETS]: pool unavailable ({e})")
        try:
            from marketcast.markets.wallet_pool import remember as _remember_wallet

            for f, h in candidates:
                if f.get("kind") == "trader":
                    _remember_wallet(f, h)
        except Exception as e:
            print(f"[WALLETS]: could not update pool ({e})")

    if not candidates:
        return None

    # rank by heuristic; trader stories convert better, so give them a slight edge
    def _rank(item: tuple[dict, float]) -> float:
        f, h = item
        return h * 1.1 if f["kind"] == "trader" else h

    candidates.sort(key=_rank, reverse=True)

    # de-cluster: front-load distinct themes so the social pass and the judge
    # see variety, not 4 flavours of the same story.
    candidates = _diversify(candidates)

    # Idea A: re-rank the strongest contenders by REAL X chatter before the judge.
    pre = candidates[: max(shortlist, social_checks)]
    if len(pre) > 1:
        try:
            from marketcast.markets.social import social_multipliers

            mults = social_multipliers(pre, max_checks=social_checks)
        except Exception as e:
            print(f"[SOCIAL]: skipped ({e})")
            mults = [1.0] * len(pre)
        ranked = sorted(
            zip(pre, mults), key=lambda pm: _rank(pm[0]) * pm[1], reverse=True
        )
        candidates = [c for c, _m in ranked] + candidates[len(pre):]

    chosen = candidates[0]
    # only genuinely-hyped candidates are worth the judge's (and a viewer's) time
    hot = [c for c in candidates if c[1] > 0][:shortlist]
    if len(hot) > 1 and _llm_pick_enabled():
        picked = _llm_pick(hot)
        if picked:
            chosen = picked
    return _wrap(chosen[0], chosen[1])


def _llm_pick_enabled() -> bool:
    # No dedicated setting exists for this toggle, so it is read from the env
    # directly (kept identical to the original DISABLE_LLM_PICK behaviour).
    return os.getenv("DISABLE_LLM_PICK", "").strip().lower() not in ("1", "true", "yes")


def _pick_json(text: str | None) -> Any:
    """First parseable JSON object in ``text`` (the LLM may wrap in fences and emit
    several blocks). Brace-matches each candidate."""
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


def _extract_pick_index(raw: str | None, n: int) -> tuple[int | None, str | None]:
    """Best-effort ``(index, why)`` from the judge's reply. Tries strict JSON first,
    then a loose regex (the Kimi fallback routinely emits prose or half-formed
    JSON). Returns ``(None, None)`` when nothing usable is found."""
    obj = _pick_json(raw)
    idx = why = None
    if isinstance(obj, dict) and isinstance(obj.get("pick"), int):
        idx, why = obj["pick"], obj.get("why")
    else:
        m = re.search(r'pick["\s:=>]*?(\d{1,2})', raw or "", re.I)
        if m:
            idx = int(m.group(1))
            mw = re.search(r'why["\s:=>]*?["\']?([^"\'\n}]{2,60})', raw or "", re.I)
            why = mw.group(1).strip() if mw else None
    if not isinstance(idx, int) or not (0 <= idx < n):
        return None, None
    return idx, why


def _llm_pick(cands: list[tuple[dict, float]]) -> tuple[dict, float] | None:
    """Ask the LLM to choose the most scroll-stopping story among the shortlist.
    Returns the chosen ``(facts, hype)`` tuple, or ``None`` on any problem so the
    caller falls back to the heuristic top-1."""
    try:
        from marketcast.generation.generator import _facts_block
        from marketcast.llm import call_llm
    except Exception as e:
        print(f"[PICK]: judge unavailable ({e}) - using heuristic top-1")
        return None

    blocks = []
    for i, (f, _h) in enumerate(cands):
        blocks.append(
            f"[{i}] kind={f['kind']}\n{_facts_block({'kind': f['kind'], 'facts': f})}"
        )
    listing = "\n\n".join(blocks)

    system = (
        "You are a viral-content editor for a Polymarket short-form video and tweet "
        "account. You receive several real candidate stories with their true numbers. "
        "Pick the ONE that would most stop a scroll and pull views on X right now. "
        "Favor: a jaw-dropping figure, fresh drama or a big swing, an underdog or "
        "longshot in play, a topic people recognize and argue about. "
        "Avoid: stale or already-decided outcomes, and unremarkable numbers. "
        "Judge only the facts given; never assume data that is not shown. "
        "Output STRICT JSON only, no prose, no code fences: "
        '{"pick": <index int>, "why": "<max 8 words>"}'
    )
    last = len(cands) - 1
    user = (
        f"CANDIDATES:\n{listing}\n\n"
        f"Choose the single best index from 0 to {last}. JSON only."
    )
    retry_user = (
        f"CANDIDATES:\n{listing}\n\n"
        f"Reply with ONLY this, nothing else (no prose, no code fences), "
        f'picking an index from 0 to {last}:\n{{"pick": 0, "why": "short reason"}}'
    )

    for attempt, prompt in enumerate((user, retry_user)):
        try:
            raw = call_llm(system, prompt, max_tokens=120, temperature=0.2)
        except Exception as e:
            print(f"[PICK]: judge call failed ({e}) - using heuristic top-1")
            return None
        idx, why = _extract_pick_index(raw, len(cands))
        if idx is not None:
            f = cands[idx][0]
            why = re.sub(r"\s+", " ", str(why or "").strip())[:60]
            label = f.get("name") or (f.get("title") or "")[:40]
            note = " (retry)" if attempt else ""
            print(f'[PICK]: judge chose [{idx}] {f["kind"]} "{label}" - {why}{note}')
            return cands[idx]
        if attempt == 0:
            print("[PICK]: unparseable judge output - retrying with strict format")
    print("[PICK]: judge output unusable after retry - using heuristic top-1")
    return None


def _biggest_recent_trade(cid: str, hours: int = 24) -> dict | None:
    """Largest single trade on a market in the last ``hours`` — a 'whale just bet
    $X on Y' hook. One extra call, only for the chosen event."""
    rows = _get(f"{DATA}/trades?market={cid}&limit=100", [])
    if not isinstance(rows, list):
        return None
    cutoff = time.time() - hours * 3600
    best = None
    for t in rows:
        ts = _num(t.get("timestamp"))
        if ts > 1e12:  # normalise ms -> s if needed
            ts /= 1000
        usd = _num(t.get("size")) * _num(t.get("price"))
        if ts >= cutoff and usd > (best["usd"] if best else 0):
            best = {
                "usd": round(usd),
                "side": (t.get("side") or "").lower(),
                "outcome": t.get("outcome"),
                "trader": _clean_name(
                    t.get("name"), t.get("pseudonym"), t.get("proxyWallet", "")
                ),
            }
    return best if (best and best["usd"] >= 1000) else None


def _wrap(facts: dict | None, hype: float) -> Subject | None:
    if not facts:
        return None
    if facts.get("kind") == "event" and facts.get("_cid"):
        try:
            bt = _biggest_recent_trade(facts["_cid"])
            if bt:
                facts["biggest_trade"] = bt
        except Exception:
            pass
    return {
        "kind": facts["kind"],
        "url": facts["url"],
        "facts": facts,
        "hype": round(hype, 1),
    }


if __name__ == "__main__":
    import sys

    m = sys.argv[1] if len(sys.argv) > 1 else "auto"
    s = pick_subject(mode=m)
    print(json.dumps(s, indent=2, ensure_ascii=False))
