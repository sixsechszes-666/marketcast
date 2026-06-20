"""The ``markets`` layer: Polymarket data fetching, subject picking and live X
research.

Public API (the integration boundary other layers depend on):

- :func:`pick_subject` — discover, score and return the best Polymarket subject.
- :func:`research_subject` — gather + distill fresh X news/chatter about a subject.
- :func:`research_as_block` — render a research digest as a prompt context block.
- :func:`top_quotes` — the highest-engagement source tweets behind a digest.
- :class:`Subject`, :class:`Research` — the cross-layer data shapes.
"""
from __future__ import annotations

from marketcast.markets.models import Research, Subject
from marketcast.markets.polymarket import pick_subject
from marketcast.markets.research import research_as_block, research_subject, top_quotes

__all__ = [
    "pick_subject",
    "research_subject",
    "research_as_block",
    "top_quotes",
    "Subject",
    "Research",
]
