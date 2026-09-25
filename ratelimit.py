"""
Optional per-IP rate limiting for the Telegram Stremio addon.

- Disabled by default. The instance owner enables it (env or /configure wizard)
  when sharing the addon publicly.
- Two buckets:
    * "api"    -> JSON endpoints (manifest excluded): catalog / meta / stream resolve / subtitles / configure
    * "stream" -> media range endpoints (/stream/file, /stream/split, /stream/zip)
  Media players fire many small range requests, so the stream bucket has a much
  higher default allowance so normal playback is never interrupted.
- Sliding window implemented in memory (per worker process).
"""

import time
import asyncio
import logging
from collections import defaultdict, deque

from config import Config

logger = logging.getLogger("ratelimit")

_hits: dict = defaultdict(deque)
_last_cleanup = time.time()
_lock = asyncio.Lock()


def _client_ip(request) -> str:
    # Behind common reverse proxies (Render/Heroku/Nginx) use X-Forwarded-For
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _cleanup(window: int):
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 30:
        return
    _last_cleanup = now
    stale = []
    for key, dq in _hits.items():
        while dq and dq[0] < now - window:
            dq.popleft()
        if not dq:
            stale.append(key)
    for key in stale:
        _hits.pop(key, None)


def _limit_status(ip: str, bucket: str) -> tuple:
    """Returns (allowed: bool, retry_after: int, remaining: int)."""
    if bucket == "stream":
        limit = Config.RATE_LIMIT_STREAM_REQUESTS
    else:
        limit = Config.RATE_LIMIT_REQUESTS
    window = max(1, Config.RATE_LIMIT_WINDOW)
    now = time.time()

    key = (ip, bucket)
    dq = _hits[key]
    while dq and dq[0] < now - window:
        dq.popleft()

    if len(dq) >= limit:
        retry_after = max(1, int(dq[0] + window - now) + 1)
        return False, retry_after, 0

    dq.append(now)
    _cleanup(window)
    return True, 0, limit - len(dq)


async def check_rate_limit(request, bucket: str = "api") -> tuple:
    """
    Returns (allowed, retry_after, headers).
    When rate limiting is disabled this always returns (True, 0, {}).
    """
    if not Config.RATE_LIMIT_ENABLED:
        return True, 0, {}

    ip = _client_ip(request)
    async with _lock:
        allowed, retry_after, remaining = _limit_status(ip, bucket)

    headers = {
        "X-RateLimit-Limit": str(Config.RATE_LIMIT_STREAM_REQUESTS if bucket == "stream" else Config.RATE_LIMIT_REQUESTS),
        "X-RateLimit-Remaining": str(max(0, remaining)),
    }
    if not allowed:
        headers["Retry-After"] = str(retry_after)
        logger.warning(f"Rate limit exceeded for {ip} on bucket '{bucket}' (retry after {retry_after}s)")
    return allowed, retry_after, headers


def enabled() -> bool:
    return bool(Config.RATE_LIMIT_ENABLED)
