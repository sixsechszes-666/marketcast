"""AI post-generation layer.

Turns a picked Polymarket :class:`~marketcast.markets.models.Subject` into post
text, video dashboard copy, structure templates mined from X, and tracks which
subjects have already been used.

Public surface:
    * ``generate_post`` / ``format_quotes`` — viral post text + quote suggestions.
    * ``fetch_templates`` — real high-performing tweets to borrow structure from.
    * ``generate_dashboard_copy`` — on-screen copy for the recorder.
    * ``mark_used`` / ``used_ids`` / ``recent`` / ``count`` — subject history.
"""
from __future__ import annotations

from .dashboard_copy import generate_dashboard_copy
from .generator import format_quotes, generate_post
from .history import count, mark_used, recent, used_ids
from .templates import fetch_templates

__all__ = [
    "generate_post",
    "format_quotes",
    "fetch_templates",
    "generate_dashboard_copy",
    "mark_used",
    "used_ids",
    "recent",
    "count",
]
