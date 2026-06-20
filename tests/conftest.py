"""Shared test fixtures.

Tests are offline-only — no network, no real credentials. This redirects the
package's data directory to a temp folder so history/log writes don't touch the
real ``data/`` directory during a run.
"""
from __future__ import annotations

import os

# Point persisted data at a per-session temp dir before marketcast.config is
# imported anywhere (config resolves data_dir at import time).
import tempfile

os.environ.setdefault("MARKETCAST_DATA_DIR", tempfile.mkdtemp(prefix="marketcast-test-"))
