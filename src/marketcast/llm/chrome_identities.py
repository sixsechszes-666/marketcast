"""A small pool of coherent Chrome 145 identities used for BOTH the duck.ai
browser capture AND the subsequent curl-cffi calls.

One identity is chosen at session start and used end-to-end so duck.ai sees a
single consistent client (no UA / JS / HTTP mismatch).

Each entry bundles a User-Agent with the matching high-entropy ``Sec-CH-UA-*``
client hints (Full-Version-List, Platform, Platform-Version, Arch, Bitness,
Wow64, Model, Mobile) — duck's anti-replay checks include those, and
inconsistent values stand out. All Windows variants because the real machine IS
Windows; lying about the OS would create a JS-side ``navigator.platform``
mismatch inside the captured browser. Variation is in Windows build + Chrome
patch number, the kind of spread you'd see across real users.

curl-cffi (0.14.0) tops out at TLS impersonation ``chrome142``; ja3/ja4 don't
visibly drift between 142 and 145, so a chrome142 handshake under a Chrome/145
UA is normal (lots of real users run a Chrome a few versions ahead of any given
fingerprint library's profile).
"""
from __future__ import annotations

import random

_BRANDS_LOW = '"Google Chrome";v="145", "Chromium";v="145", "Not?A_Brand";v="24"'
_FVL = '"Google Chrome";v="{v}", "Chromium";v="{v}", "Not?A_Brand";v="24.0.0.0"'
# UA on Windows is locked by UA-reduction — every real Chrome 145 on Windows
# sends this exact string, so all 20 entries share it. The variation lives in
# the high-entropy Sec-CH-UA-* hints below (which is where real client diversity
# now lives post-UA-reduction).
_WIN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

# curl-cffi TLS impersonation target — keep close to Chrome 145.
TLS_IMPERSONATE = "chrome142"


def _ident(label: str, full_v: str, plat_v: str, *,
           arch: str = "x86", bits: str = "64", wow: str = "?0") -> dict:
    """Build one identity entry so the 20-entry pool below reads like a table."""
    return {
        "label": label,
        "ua": _WIN_UA,
        "sec_ch_ua": _BRANDS_LOW,
        "sec_ch_ua_full_version_list": _FVL.format(v=full_v),
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_platform_version": f'"{plat_v}"',
        "sec_ch_ua_arch": f'"{arch}"',
        "sec_ch_ua_bitness": f'"{bits}"',
        "sec_ch_ua_wow64": wow,
        "sec_ch_ua_model": '""',
    }


# 20 plausible Chrome 145 Windows identities — mix of Win11 24H2 / 23H2 / 22H2
# and Win10 22H2 reported via Sec-CH-UA-Platform-Version ("15.0.0" / "14.0.0" /
# "13.0.0" / "10.0.0"), across two Chrome 145 build trains (.7400.* and .7432.*).
# One Windows-on-ARM variant for natural minority.
IDENTITIES = [
    _ident("win11-24h2-145.7400.85",     "145.0.7400.85",  "15.0.0"),
    _ident("win11-23h2-145.7400.102",    "145.0.7400.102", "14.0.0"),
    _ident("win10-22h2-145.7400.68",     "145.0.7400.68",  "10.0.0"),
    _ident("win11-24h2-145.7432.42",     "145.0.7432.42",  "15.0.0"),
    _ident("win11-23h2-145.7432.59",     "145.0.7432.59",  "14.0.0"),
    _ident("win11-22h2-145.7400.119",    "145.0.7400.119", "13.0.0"),
    _ident("win11-24h2-145.7400.136",    "145.0.7400.136", "15.0.0"),
    _ident("win11-24h2-145.7432.93",     "145.0.7432.93",  "15.0.0"),
    _ident("win11-23h2-145.7432.76",     "145.0.7432.76",  "14.0.0"),
    _ident("win10-22h2-145.7400.85",     "145.0.7400.85",  "10.0.0"),
    _ident("win11-24h2-145.7432.15",     "145.0.7432.15",  "15.0.0"),
    _ident("win11-23h2-145.7400.50",     "145.0.7400.50",  "14.0.0"),
    _ident("win11-22h2-145.7400.102",    "145.0.7400.102", "13.0.0"),
    _ident("win11-24h2-145.7432.76",     "145.0.7432.76",  "15.0.0"),
    _ident("win11-23h2-145.7432.119",    "145.0.7432.119", "14.0.0"),
    _ident("win10-22h2-145.7432.42",     "145.0.7432.42",  "10.0.0"),
    _ident("win11-24h2-145.7400.50",     "145.0.7400.50",  "15.0.0"),
    _ident("win11-24h2-145.7432.59-arm", "145.0.7432.59",  "15.0.0", arch="arm"),
    _ident("win11-23h2-145.7400.85",     "145.0.7400.85",  "14.0.0"),
    _ident("win10-22h2-145.7432.93",     "145.0.7432.93",  "10.0.0"),
]


# python-side keys mapped to canonical HTTP header names
_HEADER_MAP = {
    "ua":                          "User-Agent",
    "sec_ch_ua":                   "Sec-CH-UA",
    "sec_ch_ua_full_version_list": "Sec-CH-UA-Full-Version-List",
    "sec_ch_ua_mobile":            "Sec-CH-UA-Mobile",
    "sec_ch_ua_platform":          "Sec-CH-UA-Platform",
    "sec_ch_ua_platform_version":  "Sec-CH-UA-Platform-Version",
    "sec_ch_ua_arch":              "Sec-CH-UA-Arch",
    "sec_ch_ua_bitness":           "Sec-CH-UA-Bitness",
    "sec_ch_ua_wow64":             "Sec-CH-UA-Wow64",
    "sec_ch_ua_model":             "Sec-CH-UA-Model",
}


def pick_identity() -> dict:
    """Return a random identity from the pool.

    The caller is expected to cache the result for the session — rotation is a
    safety lever, not a per-request thing.
    """
    return random.choice(IDENTITIES)


def headers_for(identity: dict) -> dict:
    """Render an HTTP-headers dict from an identity (ready for curl-cffi)."""
    return {h: identity[k] for k, h in _HEADER_MAP.items() if identity.get(k) is not None}
