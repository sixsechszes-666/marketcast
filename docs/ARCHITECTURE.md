# marketcast — architecture & module contracts

This is the coordination spec for the clean rewrite. Every module is a faithful
port of code from the original `traders/` project, restructured into a proper
package with separation of concerns, type hints, docstrings and a config layer.

## Source of truth

Original (messy) code lives in:
a separate local prototype directory

The clean target is this repo. **Port behavior faithfully** — do not invent new
features or drop functional behavior beyond the agreed scope cuts below. The
point is a clean, presentable version of the *same* software.

### In scope (focused subset)
- `xclient/` — the X/Twitter API library.
- Polymarket data → subject picking → research.
- LLM provider (duck.ai primary + Kimi/NVIDIA fallback).
- AI post generation (post text, templates, dashboard copy, subject history).
- Node recorder: Polymarket dashboards → 1:1 MP4 video (+ optional music mux).

### Out of scope (do NOT port)
POV/lecture/AI-tools/remix content streams, the big `post_ui.py` TUI, clip
finder / uniquify, music library prep (`trim-tracks.js`), `social_signal.py`,
`competitor_filter.py`, `wallet_pool.py`, story/lecture sources & history.

## Layout

```
marketcast/
├── pyproject.toml          # packaging, deps, ruff/mypy/pytest config (DONE)
├── .env.example            # documented config (DONE)
├── README.md               # top-level (owned by orchestrator)
├── src/marketcast/
│   ├── __init__.py         # lazy re-exports (DONE)
│   ├── config.py           # `settings` singleton, env-driven (DONE)
│   ├── cli.py              # end-to-end orchestration (owned by orchestrator)
│   ├── xclient/            # Agent: xclient
│   ├── llm/                # Agent: llm+generation
│   ├── markets/            # Agent: markets   (models.py DONE)
│   └── generation/         # Agent: llm+generation
├── recorder/               # Agent: recorder (Node subproject)
└── tests/                  # each agent adds tests for its module
```

## Conventions (all Python modules)
- `from __future__ import annotations` at the top of every module.
- Full type hints on public functions; concise docstrings (module + public defs).
- No direct `os.environ` access — read from `marketcast.config.settings`.
- No secrets in code. Read keys from files/env per `config.py`.
- Persisted data (logs, history, caches) goes under `settings.data_dir`.
- Keep imports within the package absolute: `from marketcast.markets import ...`.
- Match the existing code's idiom; do not over-engineer.

## Module contracts (public interfaces other layers depend on)

These signatures are the integration boundary. Keep them stable; internals are
free to be reorganized.

### `marketcast.markets`
Re-export from `markets/__init__.py`:
- `pick_subject(mode="auto", *, exclude_used=False, **kw) -> Subject | None`
  (port of `polymarket_data.pick_subject`; `exclude_used` wires the history check)
- `research_subject(subject) -> Research | None` (port of `event_research.research_subject`)
- `research_as_block(research) -> str` (port of `event_research.as_block`)
- `top_quotes(research, n=3) -> list` (port of `event_research.top_quotes`)
- `Subject`, `Research` types from `markets/models.py` (DONE).
Files: `polymarket.py` (from `polymarket_data.py`), `research.py` (from
`event_research.py`), `models.py` (done).

### `marketcast.generation`
Re-export from `generation/__init__.py`:
- `generate_post(subject, *, templates=None, research=None, greentext=True,
  style="default", headline=True, max_retries=3) -> GeneratedPost`
  (from `post_generator.generate_post`)
- `format_quotes(quotes, width=70) -> str` (from `post_generator`)
- `fetch_templates(kind=None, *, source="search", ...) -> list | None`
  (from `tweet_sources.fetch_templates`)
- `generate_dashboard_copy(subject, *, research=None, max_retries=2) -> DashboardCopy`
  (from `dashboard_copy.generate_dashboard_copy`)
- history helpers `mark_used(subject)`, `used_ids() -> set`, `recent(n)`, `count()`
  (from `subject_history.py`)
Files: `generator.py`, `templates.py`, `dashboard_copy.py`, `history.py`.

### `marketcast.llm`
Re-export from `llm/__init__.py`:
- `call_llm(system_prompt, user_prompt, max_tokens=600, temperature=..., **kw) -> str`
  (port of `nvidia_client.call_nvidia`; keep a `call_nvidia` alias for parity).
  Same provider behavior: duck.ai primary (curl_cffi) + Kimi/NVIDIA fallback,
  duck.ai session refresh via the bundled `duck_capture.js`.
- `keys_loaded() -> int`.
Files: `provider.py` (from `nvidia_client.py`), `_duck.py`/`_kimi.py` if you want
to split providers, `chrome_identities.py` (port of identities helper),
plus the bundled `duck_capture.js` (copy from source; it is a Node helper the
Python client shells out to — keep it under `llm/`). Strip the agentrouter path
(already removed per project history) — duck.ai + Kimi only.

### `recorder/` (Node)
- `node record.js [--grid|--trader|--trader-classic] [--hide=ids] [--copy=file.json]
  [--no-music] <url|wallet>` → writes `recorder/videos/*.mp4`.
- Keep the 4 dashboards under `recorder/dashboards/` and update record.js paths.
- Keep `addmusic.js` (optional music mux). Drop `trim-tracks.js`, `launcher.js`
  unless launcher is a clean menu worth keeping — orchestrator decides; default: drop.
- Add `recorder/README.md` and `recorder/package.json` (playwright dep; ffmpeg
  required on PATH).

## Cross-layer data shape
`Subject` is a dict: `{"kind": "trader"|"event", "hype": float, "facts": {...}}`,
`facts` always has `"url"`. Keep this exact shape — the recorder and prompts read it.
