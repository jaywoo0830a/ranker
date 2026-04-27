"""Network-level resource blocking + cross-Job disk cache.

Plays the role of an in-browser adblocker: at the BrowserContext layer,
every request goes through a route handler that aborts the ones the
manifest opted to drop. Requests that abort here never leave Chromium,
so they're free from a bandwidth (= proxy traffic) standpoint.

The 3rd-party-tracker list is intentionally conservative: only well-known
ad/analytics hosts. Anything embedded by Naver itself (``*.naver.com``,
``*.naver.net``, ``*.pstatic.net``) is always allowed so the session still
emits the 1st-party telemetry a real user would.

When ``Resources.cache`` is configured, an additional layer kicks in:
``script`` requests to whitelisted CDN domains are served from a shared
on-disk cache, so peer Jobs (and later runs) reuse one network fetch
instead of paying for it N times. The cache strictly respects the
response's own ``Cache-Control: max-age`` and refuses anything that
looks personalized — see :class:`DiskCache`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Request, Response, Route

from .manifest import Cache, Resources


# Substring-matched against the request hostname. Kept short and stable —
# we'd rather pass an unknown 3rd-party request than risk a false-positive
# on something Naver actually depends on.
_TRACKER_HOSTS: frozenset[str] = frozenset({
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "facebook.net",
    "scorecardresearch.com",
    "criteo.com",
    "criteo.net",
    "adsrvr.org",
    "adnxs.com",
    "moatads.com",
    "rubiconproject.com",
    "taboola.com",
    "outbrain.com",
})


def is_tracker_host(url: str) -> bool:
    """True if ``url``'s hostname matches a known 3rd-party tracker.

    Substring match — picks up subdomains (``analytics.google.com``,
    ``connect.facebook.net``, etc.) without an explicit list per variant.
    """
    host = urlparse(url).hostname or ""
    return any(t in host for t in _TRACKER_HOSTS)


@dataclass
class BlockCounter:
    """Per-Job tally — read at end-of-Job for the savings log line."""
    allowed: int = 0
    blocked: int = 0

    @property
    def total(self) -> int:
        return self.allowed + self.blocked

    def summary(self) -> str:
        if self.total == 0:
            return ""
        pct = self.blocked * 100 / self.total
        return f"blocked {self.blocked} of {self.total} requests ({pct:.0f}%)"


@dataclass
class CacheCounter:
    """Per-Job cache tally — hits avoided a network roundtrip entirely."""
    hits: int = 0
    misses: int = 0
    stored: int = 0
    bytes_saved: int = 0

    def summary(self) -> str:
        looked_up = self.hits + self.misses
        if looked_up == 0:
            return ""
        kb = self.bytes_saved / 1024
        return (
            f"cache: {self.hits}/{looked_up} hits, "
            f"{self.stored} stored, {kb:.0f}KB saved"
        )


# ────────────────────────────────────────────────
# Disk cache — conservative, read-mostly
# ────────────────────────────────────────────────


def _is_cacheable_request(request: Request, domains: frozenset[str]) -> bool:
    """Gate at the request side: only safe-by-construction shapes proceed.

    GET-only (no POSTed payload to vary on); ``script`` only (HTML/XHR
    carry rank data we must always re-fetch); domain whitelist matches a
    substring of the hostname.
    """
    if request.method != "GET":
        return False
    if request.resource_type != "script":
        return False
    host = urlparse(request.url).hostname or ""
    return any(d in host for d in domains)


_MAX_AGE_RE = re.compile(r"max-age\s*=\s*(\d+)")


def _is_cacheable_response(headers: dict[str, str]) -> tuple[bool, int]:
    """Gate at the response side. Returns ``(cacheable, ttl_seconds)``.

    Strictly respects HTTP caching semantics:
    - ``no-store``/``no-cache``/``private``/``must-revalidate`` → refuse
    - require explicit ``max-age=N``; no heuristic freshness
    - ``Set-Cookie`` → refuse (response is user-specific)
    - ``Vary`` other than ``Accept-Encoding`` → refuse (we'd need to key
      on the varying header, which we don't)
    """
    # Header lookup is case-insensitive; Playwright's all_headers() returns
    # lowercased keys, but we normalize defensively in case that changes.
    norm = {k.lower(): v for k, v in headers.items()}

    cc = norm.get("cache-control", "").lower()
    for forbidden in ("no-store", "no-cache", "private", "must-revalidate"):
        if forbidden in cc:
            return False, 0

    m = _MAX_AGE_RE.search(cc)
    if not m:
        return False, 0
    ttl = int(m.group(1))

    if "set-cookie" in norm:
        return False, 0

    vary = norm.get("vary", "").strip().lower()
    if vary:
        parts = [p.strip() for p in vary.split(",") if p.strip()]
        if any(p != "accept-encoding" for p in parts):
            return False, 0

    return True, ttl


class DiskCache:
    """Best-effort on-disk cache shared across Jobs and runs.

    Layout::

        <root>/<sha256(url)[:2]>/<sha256(url)>.bin    # decoded body
        <root>/<sha256(url)[:2]>/<sha256(url)>.meta   # JSON: expires_at, content_type, url

    Concurrency: same URL fetched by N parallel Jobs may all miss and
    all write — content is byte-identical so last-writer-wins is safe.
    Writes go to a uuid-suffixed tmp path then ``os.replace`` for
    atomicity, so readers never see a partial file. Read errors of any
    kind degrade to "miss" — caching is best-effort and must never
    break a run.
    """

    def __init__(
        self,
        root: Path,
        domains: list[str],
        min_ttl_seconds: int,
        max_ttl_seconds: int,
        max_body_bytes: int,
    ) -> None:
        self._root = Path(root)
        self._domains = frozenset(domains)
        self._min_ttl = min_ttl_seconds
        self._max_ttl = max_ttl_seconds
        self._max_body = max_body_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def domains(self) -> frozenset[str]:
        return self._domains

    def _paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        bucket = self._root / digest[:2]
        return bucket / f"{digest}.bin", bucket / f"{digest}.meta"

    def has_fresh(self, url: str) -> bool:
        """Cheap probe — used by the response listener to skip re-storing
        what we already have, which would otherwise extend TTL forever
        on every cache hit."""
        _, meta_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        return time.time() < meta.get("expires_at", 0)

    def get(self, url: str) -> tuple[bytes, str] | None:
        """Return ``(body, content_type)`` if a fresh entry exists."""
        body_path, meta_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if time.time() >= meta.get("expires_at", 0):
            return None
        try:
            body = body_path.read_bytes()
        except OSError:
            return None
        return body, meta.get("content_type", "application/octet-stream")

    def put(self, url: str, body: bytes, ttl_seconds: int, content_type: str) -> bool:
        """Store under server-stated TTL, capped at ``max_ttl``.

        Returns True if stored, False if skipped. Skip cases:
        - ``ttl_seconds < min_ttl`` — short TTLs aren't worth the
          bookkeeping; clamping UP would silently extend stale data
          past the server's intent, which we never want.
        - oversize body (above ``max_body_bytes``) — guards against
          a misclassified response chewing disk.
        - any underlying IO failure — caching is best-effort.
        """
        if ttl_seconds < self._min_ttl:
            return False
        if len(body) > self._max_body:
            return False
        ttl = min(ttl_seconds, self._max_ttl)
        body_path, meta_path = self._paths(url)
        try:
            body_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_body = body_path.with_name(f"{body_path.name}.{uuid.uuid4().hex}.tmp")
            tmp_body.write_bytes(body)
            os.replace(tmp_body, body_path)
            meta = {
                "url": url,
                "expires_at": int(time.time()) + ttl,
                "content_type": content_type,
            }
            tmp_meta = meta_path.with_name(f"{meta_path.name}.{uuid.uuid4().hex}.tmp")
            tmp_meta.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(tmp_meta, meta_path)
            return True
        except OSError:
            return False


def build_cache(spec: Cache) -> DiskCache:
    """Build a DiskCache from the manifest config. Caller is responsible
    for guarding on ``spec.enabled`` — this just materializes the policy."""
    return DiskCache(
        root=spec.dir,
        domains=spec.domains,
        min_ttl_seconds=spec.min_ttl_seconds,
        max_ttl_seconds=spec.max_ttl_seconds,
        max_body_bytes=spec.max_body_kb * 1024,
    )


def make_route_handler(
    resources: Resources,
    counter: BlockCounter,
    cache: DiskCache | None = None,
    cache_counter: CacheCounter | None = None,
):
    """Build a route handler bound to one Resources policy + counters.

    The closure captures the policy as a frozenset for fast membership
    checks per request — the handler is on the request hot-path, so we
    avoid re-deriving it on every call.

    Order of operations per request:
      1. blocked resource type → abort
      2. tracker (if enabled) → abort
      3. cache hit (if cache enabled and request is cacheable) → fulfill
      4. otherwise → continue to network
    """
    blocked_types: frozenset[str] = frozenset(rt.value for rt in resources.block)
    block_trackers = resources.block_third_party_trackers

    async def handler(route: Route, request: Request) -> None:
        if request.resource_type in blocked_types:
            counter.blocked += 1
            await route.abort()
            return
        if block_trackers and is_tracker_host(request.url):
            counter.blocked += 1
            await route.abort()
            return
        if cache is not None and _is_cacheable_request(request, cache.domains):
            assert cache_counter is not None
            hit = cache.get(request.url)
            if hit is not None:
                body, content_type = hit
                cache_counter.hits += 1
                cache_counter.bytes_saved += len(body)
                # Serve the decoded body without Content-Encoding — we
                # stored the post-decompression bytes, and route.fulfill
                # will set Content-Length to match.
                await route.fulfill(
                    status=200, body=body, content_type=content_type,
                )
                counter.allowed += 1
                return
            cache_counter.misses += 1
        counter.allowed += 1
        await route.continue_()

    return handler


def _make_response_listener(cache: DiskCache, counter: CacheCounter):
    """Build the response listener that populates the cache.

    Runs after each successful response. We re-check the request-side
    gate (in case domains list changed mid-context — defensive) and the
    response-side gate (Cache-Control / Set-Cookie / Vary) before storing.

    ``has_fresh`` short-circuits when this response was itself served
    from our cache via ``route.fulfill`` — those responses re-fire the
    response event, and re-storing them would extend the TTL on every
    hit and break the "respect server max-age" guarantee.

    All errors are swallowed: caching is best-effort. A transient failure
    here must not kill a search.
    """
    async def on_response(response: Response) -> None:
        try:
            request = response.request
            if not _is_cacheable_request(request, cache.domains):
                return
            if response.status != 200:
                return
            if cache.has_fresh(request.url):
                return
            headers = await response.all_headers()
            cacheable, ttl = _is_cacheable_response(headers)
            if not cacheable:
                return
            body = await response.body()
            content_type = headers.get("content-type", "application/octet-stream")
            if cache.put(request.url, body, ttl, content_type):
                counter.stored += 1
        except Exception:
            # Caching must never break a run. Swallow any Playwright
            # transient (closed page, missing body, etc.) and move on.
            pass

    return on_response


async def install_blocking(
    context: BrowserContext,
    resources: Resources,
    counter: BlockCounter,
    cache: DiskCache | None = None,
    cache_counter: CacheCounter | None = None,
) -> None:
    """Register the blocking route on ``context``. ``counter`` is shared
    across rotations within the same Job so end-of-Job totals reflect the
    full Job, not just the most recent context.

    When ``cache`` is provided, also installs the response listener that
    populates it on cache misses. Both objects (cache, cache_counter)
    persist across context rotations within a Job — the cache itself is
    cross-Job, the counter is per-Job for end-of-Job reporting.
    """
    handler = make_route_handler(resources, counter, cache, cache_counter)
    await context.route("**/*", handler)
    if cache is not None:
        assert cache_counter is not None
        context.on("response", _make_response_listener(cache, cache_counter))
