"""marketcast command-line interface.

End-to-end pipeline:

    1. pick a hyped Polymarket subject (event or trader)
    2. optionally pull reference tweets (templates) and fresh X research
    3. write a viral post about it with the LLM
    4. optionally record the matching 1:1 dashboard video (Node recorder)

The text post and the video are always about the *same* subject, so they can be
published together.

Examples::

    marketcast post                       # auto-pick, print the post
    marketcast post --mode trader         # force a trader story
    marketcast post --video --grid        # also record the grid-style video
    marketcast post --json                # machine-readable output
    marketcast post --out post.txt        # save the post text
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from marketcast.config import settings


def _make_utf8_stdout() -> None:
    """Windows consoles default to cp1251 here; force UTF-8 so non-ASCII event
    titles and post text print instead of crashing."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def record_video(
    subject: dict[str, Any],
    *,
    grid: bool = False,
    hide: list[str] | None = None,
    copy: dict[str, Any] | None = None,
) -> bool:
    """Fire the Node recorder on the same subject. Returns True on success.

    ``hide`` is an optional list of chart/panel ids to skip in the video.
    ``copy`` is the optional ``window.__aiCopy`` dict (hook/verdict/analysis).
    """
    record_js = settings.recorder_dir / "record.js"
    if not record_js.exists():
        print(f"[VIDEO]: record.js not found at {record_js} — skipping")
        return False

    url = subject["facts"]["url"]
    if subject["kind"] == "trader":
        flags = ["--trader"] if grid else ["--trader-classic"]
    else:
        flags = ["--grid"] if grid else []

    hide_ids = ",".join(sorted(hide)) if hide else ""
    if hide_ids:
        flags.append(f"--hide={hide_ids}")

    copy_path: str | None = None
    if copy:
        fd, copy_path = tempfile.mkstemp(suffix=".json", prefix="aicopy_")
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(copy, f, ensure_ascii=False)
        flags.append(f"--copy={copy_path}")

    cmd = [settings.node_bin, "record.js", *flags, url]
    print(f"[VIDEO]: {' '.join(cmd)}  (cwd={settings.recorder_dir})")
    try:
        result = subprocess.run(cmd, cwd=str(settings.recorder_dir))
        return result.returncode == 0
    except FileNotFoundError:
        print("[VIDEO]: node not found on PATH — skipping video")
        return False
    finally:
        if copy_path and Path(copy_path).exists():
            try:
                Path(copy_path).unlink()
            except OSError:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketcast", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    post = sub.add_parser("post", help="pick a subject and write a viral post")
    post.add_argument("--mode", choices=["auto", "trader", "event"], default="auto")
    post.add_argument("--video", action="store_true", help="also record the matching video")
    post.add_argument("--grid", action="store_true", help="use the grid video layout")
    post.add_argument(
        "--hide", metavar="ID,ID", default="",
        help="comma-separated chart/panel ids to skip in the video",
    )
    post.add_argument(
        "--no-templates", action="store_true",
        help="don't pull reference tweets from X for post structure",
    )
    post.add_argument(
        "--template-source", choices=["search", "accounts", "both"], default="search",
        help="where post structures come from",
    )
    post.add_argument(
        "--no-copy", action="store_true",
        help="don't AI-generate the video's hook/verdict/analysis copy",
    )
    post.add_argument(
        "--no-research", action="store_true",
        help="don't pull fresh X research for the post/video",
    )
    post.add_argument(
        "--no-greentext", action="store_true",
        help="don't format any fact lines as '> ' greentext",
    )
    post.add_argument(
        "--no-headline", action="store_true",
        help="don't open with the ALL-CAPS FOMO headline",
    )
    post.add_argument(
        "--style", choices=["default", "breakdown"], default="default",
        help="post voice: terse house style, or long-form analytical teardown",
    )
    post.add_argument(
        "--unique", action="store_true",
        help="skip subjects already used before (and remember this one)",
    )
    post.add_argument("--json", action="store_true", help="print machine-readable JSON")
    post.add_argument("--out", metavar="FILE", help="save the post text to a file")
    return parser


def _run_post(args: argparse.Namespace) -> int:
    # Imported lazily so `--help` and unrelated subcommands don't pay the cost
    # (and so a missing optional dep only bites when the feature is used).
    from marketcast.generation import (
        fetch_templates,
        format_quotes,
        generate_dashboard_copy,
        generate_post,
        mark_used,
    )
    from marketcast.markets import pick_subject, research_subject

    subject = pick_subject(mode=args.mode, exclude_used=args.unique)
    if not subject:
        if args.unique:
            print("No fresh subject left — all trending ones are already used.", file=sys.stderr)
        else:
            print("No subject found (no trending events returned).", file=sys.stderr)
        return 2

    if args.unique:
        try:
            mark_used(subject)
        except Exception as e:
            print(f"[HISTORY]: could not record subject ({e})")

    templates = None
    if not args.no_templates:
        try:
            templates = fetch_templates(kind=subject["kind"], source=args.template_source)
        except Exception as e:
            print(f"[X]: template fetch unavailable ({e}) — using default style")

    research = None
    if not args.no_research:
        try:
            research = research_subject(subject)
        except Exception as e:
            print(f"[RESEARCH]: unavailable ({e})")

    result = generate_post(
        subject,
        templates=templates,
        research=research,
        greentext=not args.no_greentext,
        style=args.style,
        headline=not args.no_headline,
    )

    if args.out:
        Path(args.out).write_text(result["post"], encoding="utf-8")
        print(f"[SAVED]: {args.out}")

    if args.json:
        print(json.dumps(
            {
                "kind": result["kind"],
                "url": result["url"],
                "hype": subject["hype"],
                "post": result["post"],
                "quotes": result.get("quotes", []),
                "facts": subject["facts"],
            },
            indent=2,
            ensure_ascii=False,
        ))
    else:
        bar = "-" * 60
        print(f"\n{bar}")
        print(f"  subject : {result['kind']}  (hype {subject['hype']})")
        print(f"  link    : {result['url']}")
        print(f"{bar}\n")
        print(result["post"])
        print(f"\n{bar}")
        quote_block = format_quotes(result.get("quotes"))
        if quote_block:
            print(f"\n{quote_block}")
            print(f"{bar}")

    if args.video:
        hide = [h.strip() for h in args.hide.split(",") if h.strip()]
        copy = None
        if not args.no_copy:
            try:
                copy = generate_dashboard_copy(subject, research=research)
            except Exception as e:
                print(f"[COPY]: unavailable ({e}) — dashboard uses built-in copy")
        record_video(subject, grid=args.grid, hide=hide, copy=copy)

    return 0


def main(argv: list[str] | None = None) -> int:
    _make_utf8_stdout()
    args = _build_parser().parse_args(argv)
    if args.command == "post":
        return _run_post(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
