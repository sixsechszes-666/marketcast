"""Central configuration for marketcast.

All runtime configuration is environment-driven and resolved once, here, so the
rest of the codebase never touches ``os.environ`` directly. Values are loaded
from the process environment, falling back to a ``.env`` file in the project
root (see ``.env.example``).

Usage::

    from marketcast.config import settings

    token = settings.auth_token
    path = settings.data_dir / "generations.jsonl"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Project root = three levels up from this file (src/marketcast/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """Populate ``os.environ`` from a project-root ``.env`` if present.

    Uses python-dotenv when available, otherwise a tiny built-in parser so the
    package has no hard dependency on it.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:  # pragma: no cover - thin wrapper
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except Exception:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved, read-only application settings."""

    # --- X / Twitter -------------------------------------------------------
    auth_token: str | None = field(default_factory=lambda: os.getenv("AUTH_TOKEN"))
    proxy: str | None = field(default_factory=lambda: os.getenv("PROXY"))

    # --- LLM ---------------------------------------------------------------
    nvidia_keys_file: str = field(
        default_factory=lambda: os.getenv("NVIDIA_KEYS_FILE", "nvidia_keys.txt")
    )
    kimi_model: str = field(
        default_factory=lambda: os.getenv("KIMI_MODEL", "moonshotai/kimi-k2.6")
    )
    node_bin: str = field(default_factory=lambda: os.getenv("NODE_BIN", "node"))
    disable_duck: bool = field(default_factory=lambda: _env_bool("DISABLE_DUCK"))

    # --- pipeline toggles --------------------------------------------------
    disable_research: bool = field(default_factory=lambda: _env_bool("DISABLE_RESEARCH"))

    # --- paths -------------------------------------------------------------
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("MARKETCAST_DATA_DIR", PROJECT_ROOT / "data"))
    )
    recorder_dir: Path = field(
        default_factory=lambda: Path(os.getenv("RECORDER_DIR", PROJECT_ROOT / "recorder"))
    )

    def __post_init__(self) -> None:
        # data_dir must exist for history / generation logs.
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _build_settings() -> Settings:
    _load_dotenv()
    return Settings()


# Import-time singleton used across the package.
settings = _build_settings()
