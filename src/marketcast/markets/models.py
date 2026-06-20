"""Shared data shapes for the marketcast pipeline.

These ``TypedDict`` definitions document the dictionaries that flow between the
``markets``, ``generation`` and ``llm`` layers. They are intentionally plain
dicts (not dataclasses) so the data stays JSON-serializable end to end — facts
go straight into prompts, generation logs and the Node recorder — while still
giving type checkers and editors full visibility into the shape.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

SubjectKind = Literal["trader", "event"]


class Subject(TypedDict):
    """A pickable, hyped Polymarket subject.

    ``facts`` always contains at least a ``url`` key; its remaining keys depend
    on ``kind`` (event facts vs. trader facts).
    """

    kind: SubjectKind
    hype: float
    facts: dict[str, Any]


class Research(TypedDict, total=False):
    """Distilled fresh-from-X research about a subject."""

    topic: str
    summary: str
    bullets: list[str]
    quotes: list[dict[str, Any]]
    tweets: list[dict[str, Any]]


class GeneratedPost(TypedDict, total=False):
    """The result of :func:`marketcast.generation.generate_post`."""

    kind: SubjectKind
    url: str
    post: str
    quotes: list[dict[str, Any]]
    facts: dict[str, Any]


class DashboardCopy(TypedDict, total=False):
    """AI-written hook/verdict/analysis copy injected into the recorder."""

    hook: str
    verdict: str
    analysis: str
