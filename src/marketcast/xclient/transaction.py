"""x-client-transaction-id generator.

Some GraphQL endpoints (notably SearchTimeline) 404 unless the request carries a
valid `x-client-transaction-id` header. X computes it client-side in obfuscated
JS from data baked into the home page: a base64 "site verification" key, four
SVG keyframe animations, and a couple of byte indices pulled from the
`ondemand.s.*.js` chunk. This is a faithful pure-Python port of that algorithm
(based on the public reverse-engineering by iSarabjitDhiman/XClientTransaction).

What we had to fix vs. the upstream port: X changed its webpack manifest, so the
old `"ondemand.s":"<hash>"` regex no longer matches. The chunk hash now lives as
`<chunkId>:"ondemand.s"` + `<chunkId>:"<hash>"`, and the file is served at
`ondemand.s.<hash>a.js` (webpack appends a literal `a`). `_ondemand_url` handles
both the new and the legacy layout.

Usage:
    gen = TransactionIdGenerator(session, impersonate="chrome124")
    txid = gen.generate("GET", "/i/api/graphql/<qid>/SearchTimeline")

The home-page-derived state (key, animation key, indices) is parsed once and
reused; it is not auth-specific, so a process-wide cache with a TTL is shared
across clients. Everything is fail-open at the call site: if generation raises,
callers simply omit the header (restoring the old behaviour).
"""
from __future__ import annotations

import base64
import hashlib
import math
import random
import re
import time

import bs4

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# X's transaction epoch (2023-05-01 00:00:00 UTC, in ms) and obfuscation constants.
_EPOCH_MS = 1682924400 * 1000
_KEYWORD = "obfiowerehiring"
_ADDITIONAL_RANDOM_NUMBER = 3

_ABS = "https://abs.twimg.com/responsive-web/client-web"

# legacy:  "ondemand.s":"<hash>"   (kept as a first try in case X reverts)
_LEGACY_ONDEMAND_RE = re.compile(r"""["']ondemand\.s["']\s*:\s*["']([\w]+)["']""")
# current: <chunkId>:"ondemand.s"  then  <chunkId>:"<hash>"
_CHUNK_ID_RE = re.compile(r'(\d+):"ondemand\.s"')
_INDICES_RE = re.compile(r"\(\w\[(\d{1,2})\],\s*16\)")


def _is_odd(n: int) -> float:
    return -1.0 if n % 2 else 0.0


class _Cubic:
    """Cubic bezier solver used to sample the SVG keyframe animation."""

    def __init__(self, curves):
        self.curves = curves

    def _calc(self, a, b, m):
        return 3.0 * a * (1 - m) ** 2 * m + 3.0 * b * (1 - m) * m * m + m ** 3

    def value(self, t):
        c = self.curves
        start, end, mid = 0.0, 1.0, 0.0
        if t <= 0.0:
            g = 0.0
            if c[0] > 0.0:
                g = c[1] / c[0]
            elif c[1] == 0.0 and c[2] > 0.0:
                g = c[3] / c[2]
            return g * t
        if t >= 1.0:
            g = 0.0
            if c[2] < 1.0:
                g = (c[3] - 1.0) / (c[2] - 1.0)
            elif c[2] == 1.0 and c[0] < 1.0:
                g = (c[1] - 1.0) / (c[0] - 1.0)
            return 1.0 + g * (t - 1.0)
        while start < end:
            mid = (start + end) / 2
            x = self._calc(c[0], c[2], mid)
            if abs(t - x) < 1e-5:
                return self._calc(c[1], c[3], mid)
            if x < t:
                start = mid
            else:
                end = mid
        return self._calc(c[1], c[3], mid)


class TransactionIdGenerator:
    """Derives per-request ``x-client-transaction-id`` headers from x.com state."""

    def __init__(self, session, impersonate: str = "chrome124", timeout: int = 30):
        self.session = session
        self.impersonate = impersonate
        self.timeout = timeout
        self._ready = False
        self.key = None
        self.key_bytes = None
        self.animation_key = None
        self.row_index = None
        self.byte_indices = None

    # ---- one-time setup from the home page ----

    def _http_get(self, url: str) -> str:
        return self.session.get(
            url, headers={"user-agent": UA},
            impersonate=self.impersonate, timeout=self.timeout,
        ).text

    def _ondemand_url(self, html: str) -> str | None:
        m = _LEGACY_ONDEMAND_RE.search(html)
        if m:
            return f"{_ABS}/ondemand.s.{m.group(1)}a.js"
        cid = _CHUNK_ID_RE.search(html)
        if not cid:
            return None
        hm = re.search(rf'{cid.group(1)}:"([0-9a-f]+)"', html)
        if not hm:
            return None
        return f"{_ABS}/ondemand.s.{hm.group(1)}a.js"

    def _load_indices(self, html: str):
        url = self._ondemand_url(html)
        if not url:
            raise RuntimeError("could not locate ondemand.s chunk in home page")
        js = self._http_get(url)
        idx = [int(x) for x in _INDICES_RE.findall(js)]
        if len(idx) < 2:
            raise RuntimeError(f"no key-byte indices in {url}")
        return idx[0], idx[1:]

    @staticmethod
    def _elem_children(node):
        return [c for c in getattr(node, "children", []) if getattr(c, "name", None)]

    def _2d_array(self, soup, key_bytes):
        frames = soup.find_all("svg", id=re.compile(r"loading-x-anim"))
        if not frames:
            raise RuntimeError("no loading-x-anim frames in home page")
        frame = frames[key_bytes[5] % 4]
        g = self._elem_children(frame)[0]
        path = self._elem_children(g)[1]
        d = path.get("d")[9:]
        return [[int(x) for x in re.sub(r"[^\d]+", " ", part).strip().split()]
                for part in d.split("C")]

    @staticmethod
    def _solve(value, lo, hi, rounding):
        r = value * (hi - lo) / 255 + lo
        return math.floor(r) if rounding else round(r, 2)

    @staticmethod
    def _interp(a, b, f):
        return [a[i] * (1 - f) + b[i] * f for i in range(min(len(a), len(b)))]

    @staticmethod
    def _rotation_matrix(deg):
        rad = math.radians(deg)
        return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]

    @staticmethod
    def _float_to_hex(x: float) -> str:
        out = []
        quotient = int(x)
        fraction = x - quotient
        while quotient > 0:
            quotient = int(x / 16)
            remainder = int(x - float(quotient) * 16)
            out.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
            x = float(quotient)
        if fraction == 0:
            return "".join(out)
        out.append(".")
        while fraction > 0:
            fraction *= 16
            integer = int(fraction)
            fraction -= float(integer)
            out.append(chr(integer + 55) if integer > 9 else str(integer))
        return "".join(out)

    def _animate(self, row, target_time):
        from_color = [float(v) for v in (*row[:3], 1)]
        to_color = [float(v) for v in (*row[3:6], 1)]
        from_rot = [0.0]
        to_rot = [self._solve(float(row[6]), 60.0, 360.0, True)]
        curves = [self._solve(float(v), _is_odd(i), 1.0, False)
                  for i, v in enumerate(row[7:])]
        val = _Cubic(curves).value(target_time)

        color = [c if c > 0 else 0 for c in self._interp(from_color, to_color, val)]
        rot = self._interp(from_rot, to_rot, val)
        matrix = self._rotation_matrix(rot[0])

        parts = [format(round(c), "x") for c in color[:-1]]
        for v in matrix:
            r = round(v, 2)
            r = -r if r < 0 else r
            hx = self._float_to_hex(r)
            if hx.startswith("."):
                parts.append("0" + hx.lower())
            else:
                parts.append(hx or "0")
        parts.extend(["0", "0"])
        return re.sub(r"[.-]", "", "".join(parts))

    def _ensure_ready(self):
        if self._ready:
            return
        html = self._http_get("https://x.com")
        soup = bs4.BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "twitter-site-verification"})
        if not meta or not meta.get("content"):
            raise RuntimeError("no twitter-site-verification key in home page")
        self.key = meta["content"]
        self.key_bytes = list(base64.b64decode(self.key))
        self.row_index, self.byte_indices = self._load_indices(html)

        total_time = 4096
        row = self.key_bytes[self.row_index] % 16
        frame_time = 1
        for i in self.byte_indices:
            frame_time *= self.key_bytes[i] % 16
        frame_time = round(frame_time / 10) * 10
        arr = self._2d_array(soup, self.key_bytes)
        self.animation_key = self._animate(arr[row], frame_time / total_time)
        self._ready = True

    # ---- per-request id ----

    def generate(self, method: str, path: str) -> str:
        """Return a fresh ``x-client-transaction-id`` for ``method`` + ``path``."""
        self._ensure_ready()
        now = math.floor((time.time() * 1000 - _EPOCH_MS) / 1000)
        now_bytes = [(now >> (i * 8)) & 0xFF for i in range(4)]
        digest = hashlib.sha256(
            f"{method}!{path}!{now}{_KEYWORD}{self.animation_key}".encode()
        ).digest()
        rnd = random.randint(0, 255)
        payload = [*self.key_bytes, *now_bytes, *list(digest)[:16], _ADDITIONAL_RANDOM_NUMBER]
        out = bytes([rnd, *[b ^ rnd for b in payload]])
        return base64.b64encode(out).decode().rstrip("=")


# Process-wide cache: the home-page state is global, not per-account, and valid
# for a while, so we avoid re-fetching x.com on every client.
_CACHE: dict = {"gen": None, "ts": 0.0}
_TTL = 3 * 3600  # re-derive every few hours


def get_generator(session, impersonate: str = "chrome124") -> TransactionIdGenerator:
    """Return a cached, ready :class:`TransactionIdGenerator` (re-derived per TTL)."""
    now = time.time()
    gen = _CACHE["gen"]
    if gen is None or (now - _CACHE["ts"]) > _TTL:
        gen = TransactionIdGenerator(session, impersonate=impersonate)
        gen._ensure_ready()
        _CACHE["gen"] = gen
        _CACHE["ts"] = now
    return gen
