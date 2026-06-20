"""Turn a selected Polymarket subject (trader or event) into a Twitter post in
the style of the reference template.

Numbers are computed upstream (``markets``) and handed to the model as a strict
FACTS block; the model only writes prose, it never invents a figure. Output is
validated: the exact referral link must be present and at least one real headline
number must survive. Generation uses the shared LLM client
(:func:`marketcast.llm.call_nvidia`), so key rotation on errors / 429 is
automatic.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime
from typing import Any

from marketcast.config import settings
from marketcast.llm import call_nvidia

_POST_DATA = settings.data_dir

# ── reference style the user gave us (trader variant) ────────────────────────
# NOTE: numbers here are PLACEHOLDERS in brackets on purpose — the model must
# pull every real figure from FACTS, never from this example.
_TEMPLATE_EXAMPLE = """+$[profit] PNL ON [MARKET NAME]

polymarket trader made $[profit] by betting only $[stake] on weather predictions

trading with focus only on weather markets: [N] predictions

in just [timeframe] of active trading he earned $[amount]

dozens of trades from [low]% to [high]% profit

check his profile: [the exact link from facts]"""

# ── second style: the long-form analytical "breakdown / teardown" (opt-in) ───
# A calmer, capitalized thread that explains HOW a result happened and what it
# teaches, building to a thesis (structured system vs slot machine). Opt in with
# style="breakdown" / make_post --style breakdown / the post_ui B toggle.
# Numbers are bracket PLACEHOLDERS — every real figure must come from FACTS.
_TEMPLATE_BREAKDOWN = """A Polymarket trader made $[profit] in a single day on [market type] in just [N] trades.

Total PnL $[total].

[wins] of those trades won. [losses] lost. Total volume across the day was $[volume].

The biggest winners did most of the work:

> $[stake1] turned into $[result1].
> $[stake2] turned into $[result2].
> $[stake3] turned into $[result3].

All his entries were in the [low] to [high] cent range. He held positions for minutes, not hours.

What stands out beyond the profit number is how he traded.

The first thing is position size. He was deploying several thousand dollars per trade. This is not a fifty-dollar lottery-ticket approach. You only put that kind of capital into a fast market if you trust your read and have a clear exit plan before you enter.

The second thing is hedging. On some trades he opened smaller opposite-side positions to cap his downside. Most retail traders either never hedge or hedge everything blindly. He hedged selectively.

The third thing is the fee math. [N] trades on $[volume] in volume cost him $[fees], under one percent of his total volume. Polymarket gives volume rebates as you move up tiers, so high-volume traders effectively pay less per trade than everyone else. That is its own form of edge.

The bigger lesson is what this is not. This is not gambling. It is directional trading with sized conviction and pre-planned hedges, on a market structure that rewards volume with lower friction.

Most retail traders run the opposite playbook. They trade tiny size, with no hedges, and burn their edge on fees.

That is the real divide on Polymarket. It is not between traders who pick winners and traders who pick losers. It is between traders who treat this as a structured system and traders who treat it as a slot machine.

[the exact link from facts]"""

SYSTEM_PROMPT = """You write short viral posts for X (Twitter) about Polymarket.
Your only goal is views and clicks. The post must read like a real person who
found something wild on Polymarket and is telling their followers about it.

VOICE:
- Like explaining it to a smart friend: clear and a little casual, but not sloppy. Moderately professional.
- Body text all lowercase, confident. No corporate tone. The ONE exception is an
  optional ALL-CAPS headline on the very first line, used only when the prompt asks
  for it; everything after that headline stays lowercase.
- ONE short idea per line. Never put two sentences on one line. If a line would
  contain a sentence-ending period in the MIDDLE (e.g. "x happened. y also happened"),
  split it into two separate lines instead. A line may hold commas, but never a
  full stop followed by more words. Keep lines simple and skimmable.
- Open with the most striking thing: a breaking real-world development if one is
  given, otherwise the most jaw-dropping real number.
- Plain everyday words. Short lines. No padding.

HARD RULES:
- English only.
- NO hashtags, NO emojis, NO @ mentions, NO $ tickers, NO markdown.
- NO em dashes (the "—" character). Use a comma, a line break, or nothing.
- Do NOT end a line with a period. Let the line break be the punctuation. Split a
  sentence that would need a period into its own short line instead. (Commas and
  decimal points mid-line are fine.)
- Do NOT sound like AI: no "in conclusion", no "let's dive in", no "game changer",
  no "this is huge", no rhetorical questions to the reader.
- Every POLYMARKET metric (odds, %, volume, profit, dates) MUST come from the
  FACTS block — never invent or alter one. Real-world details may come from a
  CURRENT CONTEXT block; keep its "reportedly/claimed" hedging for unverified claims.
- If you lack data for an angle, skip that angle. Do not fill gaps with guesses.
- The LAST line must be a call to action followed by the EXACT link from FACTS,
  copied character for character.

LENGTH: length follows the story — a quick stat is a few lines, a breaking-news
post runs longer. No limit, but cut every line that isn't real news or a key number.

Output ONLY the post text. No preamble, no labels, no explanation."""


# ── alternate voice for style="breakdown" — long-form analytical teardown ─────
SYSTEM_BREAKDOWN = """You write longer-form analytical "breakdown" posts for X about Polymarket — the kind that reads like a sharp trader calmly explaining what just happened and what it teaches.

VOICE:
- Normal capitalization and full sentences, like a thoughtful thread. Confident and analytical, never hyped.
- Structure it as a teardown: open with the headline result, then break down 2 to 4 concrete observations about HOW it happened (position size, timing, hedging, fee math, conviction, edge), each as its own short paragraph that names the thing and then explains it.
- Build toward a thesis: what this REALLY is versus what it looks like. End on the divide or the lesson, not just the number.
- Short paragraphs, one idea each, a blank line between them. Skimmable.
- Calm authority. No exclamation marks, no rhetorical questions to the reader.

HARD RULES:
- English only.
- NO hashtags, NO emojis, NO @ mentions, NO $ tickers (cashtags), NO markdown. Dollar amounts on numbers are fine.
- NO em dashes (the "—" character). Use a period, a comma, or a line break.
- Full sentences and sentence-ending periods ARE expected here (unlike the terse style). Write real prose, not clipped fragments.
- Every POLYMARKET metric (profit, volume, fees, %, dates, trade sizes) MUST come from the FACTS block — never invent or alter one. Real-world details may come from a CURRENT CONTEXT block; keep its "reportedly/claimed" hedging.
- If a breakdown angle has no supporting fact (e.g. you don't know the fees or whether he hedged), make the point as a general principle WITHOUT inventing a specific figure, or skip that angle. Never fabricate numbers to fill the structure.
- The LAST line must be a short call to action followed by the EXACT link from FACTS, copied character for character.

LENGTH: a real breakdown — roughly 12 to 20 lines across several short paragraphs plus the link. Long enough to teach, tight enough that every line earns its place.

Output ONLY the post text. No preamble, no labels, no explanation."""


# ── shared hard-ban list (from the reference style guide) ────────────────────
# Appended to BOTH system prompts so neither voice ever uses these. A few are
# also caught post-hoc (see _scrub / the regenerate check) as a safety net.
_BANNED_BLOCK = """

BANNED WORDS — never output any of these, no exceptions:
noise, quietly, quiet, bet, bets, betting, bettor, bettors, game changer, mind-blowing, deep dive, buckle up, grab your popcorn, let that sink in, unlock, leverage, straightforward, genuinely, honestly.
- Never use the word "bet" in any form (bet, bets, betting, bettor). Say "trade", "trades", "trading", "position", "back/backing", or "prediction" instead.

BANNED PHRASINGS — never use these shapes:
- "he didn't X. he Y" or "this isn't X, it's Y" — no AI contrast templates.
- "no X, no Y, no Z" — no stacked-negation ad copy.
- "here's why" / "here's how" — generic AI opener.
- "what happened next surprised everyone" — clickbait.
- "check this out" / "you need to see this" — generic CTA.
- "watch it above" / "watch it below" — never point at the video's position; write a smart, specific call to action instead.
- "guaranteed" / "will pump" / "next 100x" — no promises or price predictions."""

SYSTEM_PROMPT = SYSTEM_PROMPT + _BANNED_BLOCK
SYSTEM_BREAKDOWN = SYSTEM_BREAKDOWN + _BANNED_BLOCK


def _money(n: Any) -> str:
    """Format a number as a compact dollar figure (``$1.2m`` / ``$3.4k`` / ``$50``)."""
    n = float(n)
    a = abs(n)
    if a >= 1e6:
        return f"${n / 1e6:.1f}m".replace(".0m", "m")
    if a >= 1e3:
        return f"${n / 1e3:.1f}k".replace(".0k", "k")
    return f"${n:.0f}"


def _clip(s: Any, n: int = 80) -> str:
    """Trim a string to ``n`` chars with an ellipsis."""
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _facts_block(subject: dict) -> str:
    """Render a subject's numbers into the strict FACTS block the model reads."""
    f = subject["facts"]
    lines: list[str] = []
    if subject["kind"] == "trader":
        lines.append(f"subject: a polymarket trader named {f['name']}")
        lines.append(f"total profit: {_money(f['total_pnl'])}")
        if f.get("pnl_30d"):
            lines.append(f"profit in the last 30 days: {_money(f['pnl_30d'])}")
        if f.get("pnl_7d"):
            lines.append(f"profit in the last 7 days: {_money(f['pnl_7d'])}")
        if f.get("profit_per_day"):
            lines.append(f"averaging about {_money(f['profit_per_day'])} profit per day this month")
        if f["roi_pct"]:
            lines.append(f"return on volume: {f['roi_pct']}%")
        if f["volume"]:
            lines.append(f"total trading volume: {_money(f['volume'])}")
        if f.get("avg_bet"):
            lines.append(f"average bet size: {_money(f['avg_bet'])}")
        if f.get("portfolio"):
            lines.append(f"current portfolio value: {_money(f['portfolio'])}")
        if f["markets_traded"]:
            lines.append(f"markets traded: {f['markets_traded']:,}")
        if f.get("open_positions"):
            lines.append(f"open positions right now: {f['open_positions']:,}")
        # only surface when genuinely strong — these are mark-to-market on OPEN
        # positions, so a low ratio understates a profitable trader
        if f.get("positions_total", 0) >= 5 and f["positions_up"] / f["positions_total"] >= 0.6:
            lines.append(f"{f['positions_up']} of his {f['positions_total']} open positions are currently in profit")
        if f["mult_hi_pct"] >= 200 and f["mult_lo_pct"]:
            lines.append(f"winning trades ranged from {f['mult_lo_pct']}% to {f['mult_hi_pct']}% profit")
        elif f["mult_hi_pct"] >= 200:
            lines.append(f"best winning trade: {f['mult_hi_pct']}% profit")
        bt = f.get("best_trade")
        if bt and bt.get("pnl"):
            extra = f" ({bt['mult_pct']}% return)" if bt.get("mult_pct") else ""
            lines.append(f"his best single position: {_money(bt['pnl'])} on \"{_clip(bt['title'])}\"{extra}")
        bb = f.get("biggest_bet")
        if bb and bb.get("usd"):
            lines.append(f"biggest single bet: {_money(bb['usd'])} on \"{_clip(bb['title'])}\"")
        if f.get("worst_loss_usd"):
            lines.append(f"biggest losing position: {_money(f['worst_loss_usd'])}")
        if f["focus_topic"]:
            lines.append(f"trades a lot around: {f['focus_topic']}")
        if f["days_active"]:
            lines.append(f"active for about {f['days_active']} days")
        lines.append(f"EXACT link (use verbatim on the last line): {f['url']}")
    else:  # event
        lines.append(f"subject: a polymarket event titled \"{(f['title'] or '').strip()}\"")
        if f.get("tag"):
            lines.append(f"category: {f['tag']}")
        if f["is_multi"]:
            lines.append(f"it has {f['outcome_count']} possible outcomes")
            top = f["field"][:3]
            lines.append("leading outcomes: " + ", ".join(
                f"{o['name']} at {round(o['yes'] * 100)}%" for o in top if o["yes"] > 0.01))
            if f.get("gap_pts") and f.get("runner_up_name"):
                lines.append(f"the top two are only {f['gap_pts']} points apart "
                             f"({f['lead_name']} vs {f['runner_up_name']})")
        else:
            lines.append(f"current odds of YES: {f['lead_pct']}%")
        if abs(f["chg24h_pts"]) >= 3:
            d = "up" if f["chg24h_pts"] > 0 else "down"
            lines.append(f"the leader moved {d} {abs(f['chg24h_pts'])} points in 24 hours")
        if abs(f.get("chg1w_pts", 0)) >= 8:
            d = "up" if f["chg1w_pts"] > 0 else "down"
            lines.append(f"over the past week the leader moved {d} {abs(f['chg1w_pts'])} points")
        bt = f.get("biggest_trade")
        if bt and bt.get("usd"):
            who = bt.get("trader") or "a trader"
            lines.append(f"in the last 24h {who} placed a single {_money(bt['usd'])} bet "
                         f"on {bt.get('outcome') or 'an outcome'}")
        if f["volume24h"]:
            lines.append(f"volume in the last 24 hours: {_money(f['volume24h'])}")
        if f.get("volume_1wk"):
            lines.append(f"volume in the last week: {_money(f['volume_1wk'])}")
        if f["volume"]:
            lines.append(f"total volume: {_money(f['volume'])}")
        if f.get("open_interest"):
            lines.append(f"open interest (live money at stake): {_money(f['open_interest'])}")
        if f["comments"]:
            lines.append(f"people arguing in the comments: {f['comments']:,}")
        if f["days_left"]:
            lines.append(f"resolves in about {f['days_left']} days")
        lines.append(f"EXACT link (use verbatim on the last line): {f['url']}")
    return "\n".join(lines)


# things that scream "AI wrote this" — stripped post-hoc as a safety net
_AI_TELLS = [
    "let's dive", "game changer", "game-changer", "this is huge", "buckle up",
    "in conclusion", "to sum up", "needless to say", "the bottom line",
    "folks", "ladies and gentlemen", "without further ado",
]
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")

# one-idea-per-line guard: a sentence boundary inside a line is a period preceded
# by >=2 lowercase letters and followed by a space + a word/number. The 2-letter
# lookbehind spares decimals ("1.26") and 1-letter abbreviations ("u.s." -> the
# final '.' is preceded by 's' but before that is '.', not a letter), so those
# never split. Commas stay (commas mid-line are allowed; only full stops break).
_SENT_SPLIT_RE = re.compile(r"(?<=[a-z]{2})\.[ \t]+(?=[a-z0-9$])")

# hard-banned "bet" word family -> guaranteed swap to the "trade" family post-hoc,
# since this is the one the user cares most about. Whole-word, case-preserving so a
# CAPS headline ("$8K BET" -> "$8K TRADE") and lowercase body both come out right.
_BET_MAP = {
    "bet": "trade", "bets": "trades", "betting": "trading",
    "bettor": "trader", "bettors": "traders",
}
_BET_RE = re.compile(r"\b(" + "|".join(sorted(_BET_MAP, key=len, reverse=True)) + r")\b", re.I)


def _deban_bet(m: re.Match) -> str:
    w = m.group(1)
    repl = _BET_MAP[w.lower()]
    if w.isupper():
        return repl.upper()
    if w[:1].isupper():
        return repl.capitalize()
    return repl


# cliché single words/phrases with no clean inline replacement: if one survives
# the prompt, the post is regenerated (see the ok-check) rather than mangled.
_BANNED_REGEN_RE = re.compile(
    r"\b(noise|quietly|quiet|game[ -]?changer|mind[ -]?blowing|deep dive|buckle up|"
    r"grab your popcorn|let that sink in|unlock|leverage|straightforward|"
    r"genuinely|honestly)\b", re.I)


def _scrub(text: str, allow_periods: bool = False) -> str:
    """Clean model output. ``allow_periods=True`` (the breakdown style) keeps full
    sentences intact — no sentence-splitting, no terminal-period stripping — so
    the analytical prose reads naturally; the terse default still enforces one
    idea per line with no full stops."""
    text = _EMOJI_RE.sub("", text)
    text = _BET_RE.sub(_deban_bet, text)            # hard-banned "bet"/"bets" -> "trade"/"trades"
    text = re.sub(r"\s*—\s*", ", ", text)           # em dash -> comma
    text = re.sub(r"^#+\s*", "", text, flags=re.M)  # markdown headers
    text = re.sub(r"#\w+", "", text)                # hashtags
    text = text.replace("**", "").replace("`", "")
    out: list[str] = []
    for ln in text.splitlines():
        low = ln.lower().strip()
        if any(t in low for t in _AI_TELLS):
            continue
        ln = ln.rstrip()
        if "http" in ln or allow_periods:
            out.append(ln)
            continue
        # one idea per line: split a line that glued two sentences with '. ',
        # then drop any single sentence-ending period at each piece's end (leave
        # ellipses alone). Mid-line commas/decimals stay. An empty line splits to
        # [""] and is preserved, keeping paragraph breaks intact.
        for piece in _SENT_SPLIT_RE.split(ln):
            piece = re.sub(r"(?<!\.)\.\s*$", "", piece.rstrip())
            out.append(piece)
    # collapse 3+ blank lines to one
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _templates_block(templates: list[dict]) -> str:
    """Render real tweets as numbered STRUCTURE references for the prompt."""
    lines: list[str] = []
    for i, t in enumerate(templates, 1):
        body = (t.get("text") or "").strip()
        body = re.sub(r"https?://\S+", "<link>", body)   # strip their links
        meta = f"@{t.get('author', '?')}, {t.get('likes') or 0} likes"
        lines.append(f"[{i}] ({meta})\n{body}")
    return "\n\n".join(lines)


def _event_metric_angle(f: dict) -> str:
    """Pick ONE applicable market metric to lead the market tie-in with, chosen at
    random each run so successive posts about the same kind of event don't keep
    opening on the same number (the 'every tweet uses the same metrics' problem)."""
    angles: list[str] = []
    if abs(f.get("chg24h_pts", 0)) >= 3 or abs(f.get("chg1w_pts", 0)) >= 5:
        angles.append("lead the market tie-in with the biggest price swing")
    if f.get("volume24h") or f.get("open_interest"):
        angles.append("lead the market tie-in with the scale of money flowing in (volume / open interest)")
    if f.get("comments", 0) >= 200:
        angles.append("lead the market tie-in with how many people are arguing in the comments")
    if 0 < f.get("days_left", 0) <= 21:
        angles.append("lead the market tie-in with how soon it resolves")
    bt = f.get("biggest_trade")
    if bt and bt.get("usd"):
        angles.append("lead the market tie-in with the single big whale trade")
    return random.choice(angles) if angles else ""


def _greentext_directive(enabled: bool = True) -> str:
    """The trendy X 'greentext' look — a few fact/news lines prefixed with '> '.
    On by default for every post (controlled by the post_ui toggle / make_post
    flag); ``DISABLE_GREENTEXT=1`` is a hard env override. Never the hook or the
    link line."""
    if not enabled or os.getenv("DISABLE_GREENTEXT", "").strip().lower() in ("1", "true", "yes"):
        return ""
    return (
        "- Style: format 2 to 4 of the punchiest fact / stat / news lines as "
        "'greentext' — start those lines with '> ' (the X meme look). Do NOT "
        "greentext the opening hook or the final call-to-action/link line, and "
        "don't prefix every line.\n"
    )


def _headline_directive(enabled: bool = True, is_event: bool = False) -> str:
    """The viral FOMO hook: an ALL-CAPS headline as the FIRST line, built from the
    single biggest real number in FACTS. On by default (toggle via post_ui /
    make_post --no-headline); HARD off with ``DISABLE_HEADLINE=1``. Default style
    only — the breakdown voice never uses it."""
    if not enabled or os.getenv("DISABLE_HEADLINE", "").strip().lower() in ("1", "true", "yes"):
        return ""
    if is_event:
        shape = (
            "  Pick the most dramatic TRUE angle for the headline, in this priority:\n"
            "  1) If the market CONTRADICTS the real-world situation, or the leader just "
            "flipped or swung hard, lead with the key person or outcome BY NAME plus that "
            "jarring fact, e.g. \"[NAME] IS LOSING BY [N] POINTS\" or \"POLYMARKET HAS "
            "[NAME] WINNING ANYWAY\". Then on the SECOND line (lowercase, house style) add "
            "a short twist that flips it into a question or the stakes, e.g. \"so why is "
            "the market still favoring him\" — that twist line is the hook into the post.\n"
            "  2) Otherwise lead with the biggest number: money pouring in, the live odds, "
            "or the single biggest whale trade, market name in caps "
            "(e.g. \"$2.4M FLOODING INTO [MARKET]\" or \"[NN]% ON [MARKET]\").\n"
            "  Names, odds and volumes come from FACTS; a real-world figure (like a vote "
            "margin) may come from CURRENT CONTEXT — keep the body's reportedly/claimed "
            "hedging for any contested claim, and never invent a number.\n"
        )
    else:
        shape = (
            "  Shape: \"+$[BIGGEST NUMBER] PNL ON [MARKET NAME]\" using his best single "
            "trade's profit and that market's name in caps. If there is no standout single "
            "trade in FACTS, use his total profit instead: \"+$[TOTAL] PNL ON POLYMARKET\".\n"
        )
    return (
        "- FIRST LINE = a punchy ALL-CAPS headline engineered to trigger instant FOMO "
        "the second someone sees it. This caps line is the ONLY line in caps; every line "
        "after it stays in the lowercase house style.\n"
        f"{shape}"
        "  Keep the headline short enough to fit one line — shorten a long name to its key "
        "words. Any number in it must be a real figure from FACTS (for an event, a "
        "real-world figure from CURRENT CONTEXT is also allowed). Dollar amounts on numbers "
        "are fine, but no $TICKER cashtags, no period at the end. After the headline (and "
        "its twist line, if any) leave a blank line, then the rest of the post.\n"
    )


def _build_prompt(subject: dict, facts: str, templates: list[dict] | None,
                  research: str = "", greentext: bool = True, headline: bool = True) -> str:
    """Build the user prompt. Template-driven (varied) when we have real tweets,
    otherwise an example-driven prompt. ``research`` is an optional pre-rendered
    CURRENT CONTEXT block: when present, the post is built AROUND that fresh
    real-world news, with the market as the tie-in."""
    is_event = subject.get("kind") == "event"
    has_research = bool(research)
    ctx = f"{research}\n\n" if has_research else ""

    if has_research:
        story_rules = (
            "- This is BREAKING. The CURRENT CONTEXT above is fresh real-world news that is "
            "moving this market — build the post AROUND it, not around the stats:\n"
            "  * Open with a HOOK on the real-world situation right now (what just happened).\n"
            "  * Weave in SEVERAL of the most important, freshest developments from CURRENT "
            "CONTEXT (multiple lines of real news, not one) — this is the substance.\n"
            "  * Keep any 'reportedly'/'claimed' hedging; never state a contested claim as confirmed.\n"
            "- THEN tie it to the market with a couple of metrics (see the angle below). "
            "Do NOT dump every number — pick the 2-3 that carry the story.\n"
        )
    else:
        story_rules = (
            "- Open with the most jaw-dropping real number, then a couple of supporting beats. "
            "Do NOT list every metric — pick the 2-3 that best carry the story.\n"
        )

    angle = _event_metric_angle(subject["facts"]) if is_event else ""
    metric_rules = (
        "- All POLYMARKET metrics (odds, %, volume, profit, dates) come ONLY from FACTS — "
        "never invent or alter them.\n"
        + (f"- {angle}.\n" if angle else "")
        + ("- If it's a multi-outcome race, make clear the figures are competing outcomes/dates, "
           "not a yes/no; never call two sub-outcomes 'neck and neck' as if it were the whole market.\n"
           if is_event else "")
        + "- Cut vague filler. No 'tension keeps shifting', 'no consensus', 'still anyone's "
        "game' padding — show a concrete fact instead, or drop the line.\n"
    )
    length_rule = (
        "- Keep it tight and skimmable: about 6 to 9 short lines plus the link. Lead "
        "with the news, hit a few key beats, stop. No rambling paragraphs, no line "
        "with more than one idea.\n"
        if has_research else
        "- 4 to 7 short lines plus the link. Tight beats long.\n"
    )
    gt = _greentext_directive(greentext)
    hd = _headline_directive(headline, is_event)

    if templates:
        block = _templates_block(templates)
        user = (
            "Below are REAL high-performing Polymarket tweets from the last couple "
            "of days. Study only their STRUCTURE: the hook, the line breaks, the "
            "rhythm, where the number lands, how the call-to-action is phrased.\n\n"
            f"REFERENCE TWEETS (structure only):\n{block}\n\n"
            f"{ctx}"
            "Pick the ONE whose structure best fits the subject below, then write "
            "MY post using that skeleton. Hard requirements:\n"
            "- Rewrite every word. Do NOT reuse their phrases, their names, their "
            "numbers, or their links. Borrow only the shape.\n"
            f"{hd}{story_rules}{metric_rules}{gt}{length_rule}"
            "- Keep my style from the system prompt (lowercase, no emojis, no "
            "hashtags, no @ handles).\n"
            "- Last line: a short call to action then the EXACT link from FACTS.\n\n"
            f"FACTS:\n{facts}\n\n"
            "Now write the post, and nothing else."
        )
    else:
        user = (
            "Write MY post from these FACTS in the style/rhythm of the EXAMPLE "
            "(different subject, so never copy its wording or numbers).\n\n"
            f"{ctx}"
            "Requirements:\n"
            f"{hd}{story_rules}{metric_rules}{gt}{length_rule}"
            "- Last line: a short call to action then the EXACT link from FACTS.\n\n"
            f"FACTS:\n{facts}\n\n"
            f"EXAMPLE (style only, NOT the data):\n{_TEMPLATE_EXAMPLE}\n\n"
            "Now write the post."
        )
    return user


def _build_breakdown_prompt(subject: dict, facts: str, research: str = "",
                            greentext: bool = True) -> str:
    """User prompt for the long-form analytical breakdown style. Always skeleton-
    driven by _TEMPLATE_BREAKDOWN (ignores fetched tweet templates — their terse
    shape contradicts this voice); research still flows in as CURRENT CONTEXT."""
    has_research = bool(research)
    ctx = f"{research}\n\n" if has_research else ""
    gt = _greentext_directive(greentext)
    research_rule = (
        "- CURRENT CONTEXT above is fresh real-world news on this market — open the "
        "breakdown on it so the piece feels timely, and keep its reportedly/claimed hedging.\n"
        if has_research else ""
    )
    return (
        "Write MY analytical breakdown post from these FACTS, in the STRUCTURE and "
        "rhythm of the EXAMPLE below. It is a DIFFERENT subject, so never copy the "
        "example's wording, names, numbers, or link — its figures are placeholders.\n\n"
        f"{ctx}"
        "Requirements:\n"
        "- Open with the single most striking real result from FACTS.\n"
        "- Then break it down into 2 to 4 concrete observations about HOW it happened, "
        "each its own short paragraph that names the thing, then explains it.\n"
        "- Build to a thesis: what this really is versus what it looks like, and end on "
        "the divide or lesson, not just a number.\n"
        "- All POLYMARKET numbers come ONLY from FACTS. If an angle (hedging, fees, hold "
        "time) has no fact behind it, make the point as a general principle without a "
        "specific figure, or drop it. Never fabricate a number to fill the shape.\n"
        f"{research_rule}{gt}"
        "- Last line: a short call to action then the EXACT link from FACTS.\n\n"
        f"FACTS:\n{facts}\n\n"
        f"EXAMPLE (style/structure only, NOT the data):\n{_TEMPLATE_BREAKDOWN}\n\n"
        "Now write the post."
    )


def format_quotes(quotes: list[dict] | None, width: int = 70) -> str:
    """Render the quote-tweet suggestions as a plain text block to print under the
    post. Returns "" when there are none."""
    if not quotes:
        return ""
    lines = ["quote one of these popular tweets on the topic:"]
    for q in quotes:
        author = q.get("author") or "?"
        likes = q.get("likes") or 0
        snippet = (q.get("text") or "").strip().replace("\n", " ")
        if len(snippet) > width:
            snippet = snippet[: width - 1].rstrip() + "…"
        lines.append(f"  @{author} ({likes} likes): {snippet}")
        lines.append(f"  {q.get('url', '')}")
    return "\n".join(lines)


def _save_generation(record: dict) -> None:
    try:
        os.makedirs(_POST_DATA, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(_POST_DATA, f"post_{ts}.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[POST]: could not save generation log: {e}")


def generate_post(subject: dict, *, templates: list[dict] | None = None,
                  research: dict | None = None, greentext: bool = True,
                  style: str = "default", headline: bool = True,
                  max_retries: int = 3) -> dict:
    """Generate a viral post for ``subject``.

    Returns ``{"post": str, "url": str, "kind": str, "quotes": list}`` or raises
    RuntimeError. ``quotes`` is up to 3 of the most-liked real tweets the research
    was built on (each {author, likes, text, url}); it's [] for traders and when
    no research/links are available.

    templates: optional list of real tweet dicts (from
        :func:`marketcast.generation.fetch_templates`). When given, the model
        borrows one tweet's structure and heavily rephrases, which keeps
        auto-mode posts from all looking the same. Every attempt's input + output
        is saved under ``settings.data_dir`` for prompt tuning.
    research: optional research digest dict (current real-world chatter). It's
        rendered into a CURRENT CONTEXT block so the post can be timely; numbers
        still come only from FACTS.
    greentext: when True (default), a few fact/news lines are formatted as X-style
        '> ' greentext.
    style: "default" (terse lowercase house style) or "breakdown" (long-form
        analytical teardown with full sentences). breakdown ignores tweet
        templates and uses its own system prompt + skeleton.
    headline: when True (default) the default style opens with an ALL-CAPS FOMO
        headline built from the biggest real number in FACTS. Ignored by breakdown.
    """
    breakdown = style == "breakdown"
    system = SYSTEM_BREAKDOWN if breakdown else SYSTEM_PROMPT
    url = subject["facts"]["url"]
    facts = _facts_block(subject)
    research_block = ""
    quotes: list[dict] = []
    if research:
        try:
            from marketcast.markets import research_as_block, top_quotes
            research_block = research_as_block(research)
            quotes = top_quotes(research, n=3)   # real tweets to quote under the post
        except Exception:
            research_block = ""
            quotes = []
    if breakdown:
        user_prompt = _build_breakdown_prompt(subject, facts, research_block, greentext)
    else:
        user_prompt = _build_prompt(subject, facts, templates, research_block, greentext, headline)

    attempts_log: list[dict] = []
    last = None
    for attempt in range(1, max_retries + 1):
        raw = call_nvidia(system, user_prompt, max_tokens=900 if breakdown else 500,
                          temperature=0.85 + 0.05 * (attempt - 1))
        post = _scrub(raw, allow_periods=breakdown)

        # the exact referral link must survive; if mangled or missing, fix it
        if url not in post:
            post = re.sub(r"https?://polymarket\.com/\S+", "", post).rstrip()
            post = f"{post}\n\ncheck it out: {url}"

        # at least one real headline number must be present (anti-empty / anti-hallucination)
        digits = re.findall(r"\d", post.replace(url, ""))
        # a surviving banned cliché word -> regenerate rather than ship it
        clean = not _BANNED_REGEN_RE.search(post)
        ok = len(digits) >= 2 and url in post and len(post.split()) >= 8 and clean
        attempts_log.append({"attempt": attempt, "raw": raw, "post": post, "ok": ok})

        if ok:
            _save_generation({
                "kind": subject["kind"], "url": url, "style": style,
                "headline": headline and not breakdown,
                "used_templates": bool(templates) and not breakdown,
                "used_research": bool(research_block), "research": research or None,
                "facts": facts, "prompt": user_prompt, "templates": templates or [],
                "attempts": attempts_log, "final": post,
            })
            return {"post": post, "url": url, "kind": subject["kind"], "quotes": quotes}

        last = post
        print(f"[POST RETRY {attempt}/{max_retries}]: weak output, regenerating")

    # graceful fallback: return last attempt with a guaranteed link
    if last and url not in last:
        last = f"{last}\n\ncheck it out: {url}"
    _save_generation({
        "kind": subject["kind"], "url": url, "style": style,
        "headline": headline and not breakdown,
        "used_templates": bool(templates) and not breakdown,
        "used_research": bool(research_block), "research": research or None,
        "facts": facts, "prompt": user_prompt, "templates": templates or [],
        "attempts": attempts_log, "final": last, "note": "fell back to last attempt",
    })
    if not last:
        raise RuntimeError("post generation failed — no usable output")
    return {"post": last, "url": url, "kind": subject["kind"], "quotes": quotes}
