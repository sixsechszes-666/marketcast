"""Shared LLM client.

PRIMARY provider is duck.ai (free DuckDuckGo AI chat), called DIRECTLY from
Python via curl-cffi with Chrome TLS impersonation; Kimi K2.6 over NVIDIA
endpoints is the automatic FALLBACK.

:func:`call_llm` (aliased :func:`call_nvidia` for backward compat with the
generation layer) is the public entry point. It tries duck.ai first and, on any
failure (rate limit / challenge / network), transparently falls back to Kimi.

Providers
---------
* **duck.ai (PRIMARY)** — no API key. Talks to ``https://duck.ai/duckchat/v1/chat``
  directly via ``curl_cffi.requests`` with ``impersonate="chrome142"`` and a
  Chrome 145 UA + high-entropy ``Sec-CH-UA-*`` client hints — the bare Node
  ``fetch`` path was being rate-limited (429 / ERR_USER_LIMIT) at the TLS
  fingerprint layer regardless of IP or session freshness. Auth is the captured
  ``x-vqd-hash-1``; when the token goes stale (HTTP 418) or anything non-200
  hits, the client spawns ``duck_capture.js`` (headless CloakBrowser) to mint a
  fresh hash and retries once. Browser + curl-cffi use the SAME randomly-picked
  identity from :data:`chrome_identities.IDENTITIES` (rotation on every refresh
  as a safety lever). Model via ``DUCK_MODEL`` env (default = the captured
  session's model, typically claude-haiku-4-5).
  NOTE: duck.ai ignores temperature / top_p / max_tokens, and does not accept a
  "system" role — we fold the system prompt into the user message. Requires a
  session file (``duck_capture.json`` under ``settings.data_dir``): run
  ``node duck_capture.js`` once, or let the auto-refresh do it on first call.
  Set ``DISABLE_DUCK=1`` (``settings.disable_duck``) to skip duck.ai.
* **NVIDIA/Kimi (FALLBACK)** — keys from ``settings.nvidia_keys_file`` (one per
  line) or ``NVIDIA_API_KEYS`` / ``NVIDIA_API_KEY`` env vars, with 40 RPM
  per-key limiting + rotation on 429/idle/error.
"""
from __future__ import annotations

import json
import os
import time
import subprocess
from collections import deque
from pathlib import Path

import requests

from marketcast.config import settings

from .chrome_identities import IDENTITIES, TLS_IMPERSONATE, headers_for, pick_identity

KIMI_MODEL = settings.kimi_model
NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
RPM_LIMIT = 40

# --- shared HTTP timeouts (connect, idle) -------------------------------
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 90

# --- primary provider: duck.ai via curl-cffi (Python) -------------------
# duck_capture.js is bundled next to this module; the session JSON lives under
# settings.data_dir so it never pollutes the package source tree.
DUCK_CAPTURE_JS = str(Path(__file__).parent / "duck_capture.js")
DUCK_CAPTURE_JSON = str(settings.data_dir / "duck_capture.json")
DUCK_CHAT_URL = "https://duck.ai/duckchat/v1/chat"
# Empty = use whatever model the captured session was created with (the model is
# picked in the browser at capture time, e.g. "Claude Haiku 4.5"). Set DUCK_MODEL
# to a raw API id only to override that.
DUCK_MODEL = os.getenv("DUCK_MODEL", "").strip()
# Empty = use the session's reasoningEffort (model-specific: "none" for Claude,
# "minimal" for gpt-5). Set DUCK_REASONING only to override.
DUCK_REASONING = os.getenv("DUCK_REASONING", "").strip()
NODE_BIN = settings.node_bin
# generous: a stale-token refresh spins up a headless browser (~10-20s)
DUCK_TIMEOUT = int(os.getenv("DUCK_TIMEOUT", "120"))

# headers fetch/undici/curl will set itself — never replay these from capture
_STRIP_HEADERS = {"content-length", "accept-encoding", "connection", "host"}
# the current Chrome identity (UA + Sec-CH-UA-*) used for both the captured
# browser AND the curl-cffi calls. Picked at first use; rotated on every refresh.
_current_identity: dict | None = None
# after duck.ai reports a 429 (per-user rate cap), stop hitting it for a while and
# serve from Kimi directly — a single "generate" fires many LLM calls in a burst,
# and hammering a rate-limited endpoint just wastes a round-trip on each one.
DUCK_COOLDOWN_S = int(os.getenv("DUCK_COOLDOWN", "180"))
_duck_cooldown_until = 0.0


class _DuckRateLimited(RuntimeError):
    """duck.ai returned 429 (rate limited) — distinct so call_llm can pause it."""


def _duck_enabled() -> bool:
    if settings.disable_duck:
        return False
    return os.path.exists(DUCK_CAPTURE_JS)


def _parse_sse(text: str) -> str:
    """Stitch a duck.ai SSE stream into the plain assistant text (mirrors the
    parseSSE in duck_client.js)."""
    msg: list[str] = []
    for line in (text or "").split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[line.index(":") + 1:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        m = obj.get("message")
        if isinstance(m, str):
            msg.append(m)
    return "".join(msg)


def _load_capture() -> dict:
    """Load the saved duck.ai session (captured headers, cookies, identity)."""
    with open(DUCK_CAPTURE_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def _refresh_duck_session(identity: dict) -> None:
    """Re-run duck_capture.js headlessly with the given Chrome identity so the
    fresh hash + the curl-cffi requests share one consistent client."""
    if not os.path.exists(DUCK_CAPTURE_JS):
        raise RuntimeError(f"duck_capture.js not found at {DUCK_CAPTURE_JS}")
    # carry the captured session's model label over so the picker re-selects it
    # (otherwise a fresh capture drifts back to duck.ai's default GPT-5 mini)
    model_label = "Claude Haiku 4.5"
    try:
        prev = _load_capture()
        if prev.get("modelLabel"):
            model_label = prev["modelLabel"]
    except Exception:
        pass
    env = {**os.environ,
           "CHROME_IDENTITY": json.dumps(identity),
           "DUCK_MODEL_LABEL": model_label}
    print(f"[DUCK]: refreshing session (identity={identity.get('label', '?')}, "
          f"model={model_label})…")
    try:
        proc = subprocess.run(
            [NODE_BIN, DUCK_CAPTURE_JS, "--out=" + DUCK_CAPTURE_JSON, "refresh ping"],
            env=env, cwd=str(Path(DUCK_CAPTURE_JS).parent), timeout=DUCK_TIMEOUT,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError:
        raise RuntimeError(f"node not found ({NODE_BIN}); install Node or set NODE_BIN")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"duck.ai refresh timed out after {DUCK_TIMEOUT}s")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        raise RuntimeError(f"duck_capture.js refresh failed (exit {proc.returncode}): "
                           f"{' | '.join(tail)}")


def _call_duck(system_prompt: str, user_prompt: str, _retried: bool = False) -> str:
    """PRIMARY provider: POST to duck.ai chat directly via curl-cffi with Chrome
    TLS impersonation + the captured x-vqd-hash-1. On non-200 we re-capture
    (rotating identity) and retry once; 429 raises _DuckRateLimited so the
    cooldown breaker in call_llm can pause duck briefly."""
    global _current_identity
    if _current_identity is None:
        _current_identity = pick_identity()

    # if no capture yet, refresh first (turns the very first call into a setup)
    if not os.path.exists(DUCK_CAPTURE_JSON):
        _refresh_duck_session(_current_identity)

    capture = _load_capture()
    src_hdrs = capture.get("requestHeaders") or {}
    hdrs = {k: v for k, v in src_hdrs.items()
            if not k.startswith(":") and k.lower() not in _STRIP_HEADERS}
    # OVERRIDE UA + Sec-CH-UA-* with the current identity so the curl-cffi call
    # presents the exact same client the captured browser did. (Headers from
    # the capture were already from this identity, but explicit override guards
    # against a stale capture from a different identity.)
    hdrs.update(headers_for(_current_identity))
    if isinstance(capture.get("cookies"), list) and capture["cookies"]:
        hdrs["cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in capture["cookies"])

    # duck.ai rejects role:"system" (400) — fold it into the first user message
    user_content = (f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt)
    sb = capture.get("bodyParsed") or {}
    body = {
        "model": DUCK_MODEL or sb.get("model") or "claude-haiku-4-5",
        "metadata": {"toolChoice": {"NewsSearch": False, "VideosSearch": False,
                                    "LocalSearch": False, "WeatherForecast": False}},
        "messages": [{"role": "user", "content": user_content}],
        "canUseTools": False,
        "reasoningEffort": DUCK_REASONING or sb.get("reasoningEffort") or "none",
        "canUseApproxLocation": None,
        "canDelegateImageGeneration": None,
    }

    # lazy import: curl_cffi pulls in a native binding, no need at module load
    from curl_cffi import requests as _cr

    t_start = time.monotonic()
    try:
        r = _cr.post(DUCK_CHAT_URL, json=body, headers=hdrs,
                     impersonate=TLS_IMPERSONATE, timeout=DUCK_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"duck.ai network error: {str(e)[:120]}")

    if r.status_code == 200:
        answer = _parse_sse(r.text)
        if not answer.strip():
            raise RuntimeError("duck.ai returned an empty answer")
        print(f"[PROVIDER OK]: {body['model']} via duck.ai "
              f"(t={time.monotonic() - t_start:.1f}s)")
        return answer

    # error path — get a useful detail snippet from the body
    body_text = ""
    try:
        body_text = (r.text or "")[:200].replace("\n", " ")
    except Exception:
        pass

    if r.status_code == 429:
        raise _DuckRateLimited(f"duck.ai 429 (rate limited): {body_text[:120]}")

    if _retried:
        raise RuntimeError(f"duck.ai status {r.status_code}: {body_text[:160]}")

    # 418 (stale hash) or other non-200, non-429 — re-capture with a fresh
    # identity (rotation as a safety lever) and try again ONCE.
    _current_identity = pick_identity()
    _refresh_duck_session(_current_identity)
    return _call_duck(system_prompt, user_prompt, _retried=True)


def _duck_session_model() -> str:
    """Best-effort read of the model id from the captured session, for logging."""
    try:
        return (_load_capture().get("bodyParsed", {}) or {}).get("model", "duck-session")
    except Exception:
        return "duck-session"


def _load_nvidia_keys() -> list[str]:
    keys: list[str] = []
    keys_file = settings.nvidia_keys_file
    if not os.path.isabs(keys_file):
        # resolve a bare filename relative to the configured data dir
        candidate = settings.data_dir / keys_file
        keys_file = str(candidate) if candidate.exists() else keys_file
    if os.path.exists(keys_file):
        with open(keys_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    keys.append(line)
    if not keys and os.getenv("NVIDIA_API_KEYS"):
        keys = [k.strip() for k in os.getenv("NVIDIA_API_KEYS", "").split(",") if k.strip()]
    if not keys and os.getenv("NVIDIA_API_KEY"):
        keys = [os.getenv("NVIDIA_API_KEY")]  # type: ignore[list-item]
    return keys


NVIDIA_API_KEYS: list[str] = _load_nvidia_keys()
_key_calls: dict[str, deque] = {k: deque() for k in NVIDIA_API_KEYS}
_key_cooldown_until: dict[str, float] = {k: 0.0 for k in NVIDIA_API_KEYS}
_current_key_idx = 0


def _key_label(key: str) -> str:
    try:
        return f"key#{NVIDIA_API_KEYS.index(key) + 1}"
    except ValueError:
        return "key?"


def _pick_nvidia_key() -> str:
    global _current_key_idx
    if not NVIDIA_API_KEYS:
        raise RuntimeError(
            "No NVIDIA API keys — set NVIDIA_API_KEY/NVIDIA_API_KEYS or create "
            "the nvidia keys file (settings.nvidia_keys_file)"
        )
    n = len(NVIDIA_API_KEYS)
    now = time.monotonic()
    best_key, best_wait = None, float("inf")
    for offset in range(n):
        idx = (_current_key_idx + offset) % n
        key = NVIDIA_API_KEYS[idx]
        dq = _key_calls[key]
        while dq and now - dq[0] >= 60.0:
            dq.popleft()
        cooldown_wait = max(0.0, _key_cooldown_until[key] - now)
        rpm_wait = 0.0 if len(dq) < RPM_LIMIT else (60.0 - (now - dq[0]) + 0.1)
        wait = max(cooldown_wait, rpm_wait)
        if wait <= 0:
            _current_key_idx = (idx + 1) % n
            dq.append(now)
            return key
        if wait < best_wait:
            best_wait, best_key = wait, key
    print(f"[RPM]: all {n} keys blocked, sleeping {best_wait:.1f}s")
    time.sleep(best_wait)
    return _pick_nvidia_key()


def _consume_sse(resp, label: str) -> str:
    """Read an OpenAI-style SSE chat stream from `resp`; return the concatenated
    assistant text. Raises RuntimeError if no tokens arrive. Always closes resp.
    May raise requests.RequestException on an idle/disconnect mid-stream."""
    chunks: list[str] = []
    first_token_logged = False
    t_start = time.monotonic()
    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            data = raw_line[5:].strip() if raw_line.startswith("data:") else raw_line.strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                obj = json.loads(data)
            except (ValueError, TypeError):
                continue
            try:
                delta = obj["choices"][0].get("delta", {}).get("content")
            except (KeyError, IndexError, TypeError):
                delta = None
            if delta:
                if not first_token_logged:
                    ttfb = time.monotonic() - t_start
                    print(f"[PROVIDER OK]: {label} (ttfb={ttfb:.1f}s)")
                    first_token_logged = True
                chunks.append(delta)
    finally:
        resp.close()
    if not chunks:
        raise RuntimeError("empty stream (no tokens received)")
    return "".join(chunks)


def _build_payload(model: str, system_prompt: str, user_prompt: str,
                   max_tokens: int, temperature: float, top_p: float) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": True,
    }


def _call_kimi_nvidia(system_prompt: str, user_prompt: str, max_tokens: int,
                      temperature: float, top_p: float) -> str:
    """FALLBACK provider: Kimi K2.6 via NVIDIA, with per-key RPM + rotation."""
    payload = _build_payload(KIMI_MODEL, system_prompt, user_prompt,
                             max_tokens, temperature, top_p)
    max_attempts = max(3, len(NVIDIA_API_KEYS) * 2)
    last_err = None
    for attempt in range(1, max_attempts + 1):
        key = _pick_nvidia_key()
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                NVIDIA_ENDPOINT, headers=headers, json=payload,
                timeout=(CONNECT_TIMEOUT, IDLE_TIMEOUT), stream=True,
            )
        except requests.RequestException as e:
            last_err = str(e)
            print(f"[NET ERR {_key_label(key)}]: {e} ({attempt}/{max_attempts})")
            continue

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("retry-after", "60"))
            _key_cooldown_until[key] = time.monotonic() + retry_after
            print(f"[429 {_key_label(key)}]: cooldown {retry_after}s, rotating")
            resp.close()
            continue
        if resp.status_code != 200:
            last_err = f"{resp.status_code}: {resp.text[:300]}"
            print(f"[HTTP {resp.status_code} {_key_label(key)}]: rotating ({attempt}/{max_attempts})")
            resp.close()
            continue

        try:
            return _consume_sse(resp, f"Kimi K2.6 via {_key_label(key)}")
        except requests.RequestException as e:
            last_err = f"stream idle/disconnect: {e}"
            print(f"[STREAM ERR {_key_label(key)}]: {e} ({attempt}/{max_attempts})")
            continue
        except RuntimeError:
            last_err = "empty stream (no tokens received)"
            print(f"[STREAM ERR {_key_label(key)}]: empty response, rotating")
            continue
    raise RuntimeError(f"NVIDIA API: exhausted {max_attempts} attempts. last={last_err}")


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 600,
             temperature: float = 0.8, top_p: float = 0.9) -> str:
    """Public entry point. Tries duck.ai (free) first; on any failure falls back
    to Kimi K2.6 via NVIDIA. Returns assistant text or raises RuntimeError if
    every provider is exhausted.

    NOTE: duck.ai ignores max_tokens / temperature / top_p — those only affect
    the Kimi fallback path."""
    global _duck_cooldown_until
    if _duck_enabled() and time.monotonic() >= _duck_cooldown_until:
        try:
            return _call_duck(system_prompt, user_prompt)
        except _DuckRateLimited as e:
            _duck_cooldown_until = time.monotonic() + DUCK_COOLDOWN_S
            print(f"[FALLBACK]: {e} — pausing duck.ai for {DUCK_COOLDOWN_S}s, "
                  f"serving from Kimi K2.6")
        except Exception as e:
            print(f"[FALLBACK]: duck.ai failed ({e}); falling back to Kimi K2.6")
    return _call_kimi_nvidia(system_prompt, user_prompt, max_tokens,
                             temperature, top_p)


# Backward-compat alias: the generation layer (and the original code) call this
# function ``call_nvidia``. Keep both names pointing at the same implementation.
call_nvidia = call_llm


def keys_loaded() -> int:
    """Number of NVIDIA/Kimi fallback API keys currently loaded."""
    return len(NVIDIA_API_KEYS)
