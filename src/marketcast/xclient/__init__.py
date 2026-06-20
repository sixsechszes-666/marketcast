"""Self-contained x.com GraphQL/REST client (curl_cffi + auth_token cookie).

Faithful port of the original ``traders/xclient`` library. Auth is a single
``auth_token`` cookie (no API keys); transport runs on ``curl_cffi`` so it
behaves like a real Chrome session.

Public surface:
    * ``Twitter`` — high-level facade (``.read`` / ``.write`` / identity).
    * ``TwitterClient`` — transport/auth.
    * ``TwitterReader`` / ``TwitterWriter`` — reads / actions.
    * ``Telemetry`` — client-event ("scribe") beacons for account realism.
    * ``AccountPool`` / ``Account`` — multi-account rotation.
    * ``ActionPacer`` / ``LimitReached`` — opt-in action throttling + daily caps.
    * typed errors (``TwitterError`` and subclasses).
"""
from __future__ import annotations

from . import errors
from .accounts import Account, AccountPool
from .api import Twitter, load_creds
from .client import TwitterClient
from .errors import (
    Forbidden,
    NotFound,
    RateLimited,
    Suspended,
    TwitterError,
    Unauthorized,
)
from .pacer import ActionPacer, LimitReached
from .reader import TwitterReader
from .telemetry import Telemetry
from .writer import TwitterWriter

__all__ = [
    "Twitter", "TwitterClient", "TwitterReader", "TwitterWriter",
    "Telemetry", "AccountPool", "Account", "ActionPacer", "LimitReached",
    "load_creds", "errors",
    "TwitterError", "RateLimited", "Unauthorized", "Suspended", "Forbidden", "NotFound",
]
