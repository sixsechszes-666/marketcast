"""LLM provider layer.

duck.ai primary (curl_cffi TLS impersonation) + Kimi/NVIDIA fallback, with a
self-refreshing duck.ai session via the bundled ``duck_capture.js``.

Public surface:
    * ``call_llm(system_prompt, user_prompt, ...)`` — the entry point.
    * ``call_nvidia`` — backward-compat alias of ``call_llm``.
    * ``keys_loaded()`` — number of NVIDIA fallback keys loaded.
"""
from __future__ import annotations

from .provider import call_llm, call_nvidia, keys_loaded

__all__ = ["call_llm", "call_nvidia", "keys_loaded"]
