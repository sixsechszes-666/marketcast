"""marketcast — Polymarket → AI-written viral X posts with dashboard videos.

Public surface:

    from marketcast import pick_subject, generate_post, research_subject

See ``marketcast.cli`` for the end-to-end orchestration.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "pick_subject",
    "generate_post",
    "research_subject",
    "__version__",
]


def __getattr__(name: str):
    # Lazy re-exports keep import-time cost (and optional deps) out of the way
    # until a symbol is actually used.
    if name == "pick_subject":
        from marketcast.markets import pick_subject

        return pick_subject
    if name == "research_subject":
        from marketcast.markets import research_subject

        return research_subject
    if name == "generate_post":
        from marketcast.generation import generate_post

        return generate_post
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
