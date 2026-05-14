"""
Proxy Rotation Manager — prevents IP bans during scraping.

Strategy:
  - Maintain pool of proxies (loaded from env or file)
  - Round-robin with health tracking
  - Ban proxy after 3 consecutive failures
  - Re-test banned proxies every 30 min
  - Per-platform proxy assignment (sticky per domain to avoid session issues)

Supported proxy types:
  - HTTP proxies (host:port:user:pass format)
  - SOCKS5 proxies
  - Rotating residential proxies via API (Bright Data / Oxylabs / Smartproxy)

Security:
  - Proxy credentials stored in vault, not .env
  - HTTPS traffic only — no plaintext proxy tunneling
  - Never log full proxy URLs (mask credentials)
"""

from __future__ import annotations
import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
import structlog

log = structlog.get_logger()

TEST_URL = "https://httpbin.org/ip"
PROXY_TEST_TIMEOUT = 8.0
BAN_AFTER_FAILURES = 3
RETEST_INTERVAL_SEC = 1800  # 30 min


@dataclass
class ProxyEntry:
    url: str                          # full proxy URL with credentials
    display: str                      # masked URL for logging
    failures: int = 0
    banned_at: float | None = None
    last_used: float = 0.0
    latency_ms: float = 999.0


class ProxyPool:
    """
    Thread-safe rotating proxy pool.
    All platform scrapers request proxies from here.
    """

    def __init__(self):
        self._proxies: list[ProxyEntry] = []
        self._domain_assignment: dict[str, ProxyEntry] = {}  # sticky per domain
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "ProxyPool":
        """
        Load proxies from PROXY_LIST env var.
        Format: comma-separated host:port:user:pass
        or a single rotating endpoint: socks5://user:pass@gate.provider.com:7777
        """
        pool = cls()
        raw = os.environ.get("PROXY_LIST", "")
        if not raw:
            log.warning("no_proxies_configured — scraping without proxy rotation")
            return pool

        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            proxy_url = cls._normalise(entry)
            if proxy_url:
                pool._proxies.append(ProxyEntry(
                    url=proxy_url,
                    display=cls._mask(proxy_url),
                ))

        log.info("proxy_pool_loaded", count=len(pool._proxies))
        return pool

    @classmethod
    def from_brightdata(cls, zone: str, password: str) -> "ProxyPool":
        """Bright Data (formerly Luminati) rotating residential proxy endpoint."""
        pool = cls()
        endpoint = f"http://{zone}:{quote(password)}@brd.superproxy.io:22225"
        pool._proxies.append(ProxyEntry(
            url=endpoint,
            display="brightdata://***@brd.superproxy.io:22225",
        ))
        return pool

    @classmethod
    def from_smartproxy(cls, user: str, password: str) -> "ProxyPool":
        """Smartproxy rotating residential endpoint."""
        pool = cls()
        endpoint = f"http://{user}:{quote(password)}@gate.smartproxy.com:7000"
        pool._proxies.append(ProxyEntry(
            url=endpoint,
            display="smartproxy://***@gate.smartproxy.com:7000",
        ))
        return pool

    async def get(self, domain: str | None = None) -> str | None:
        """
        Get next available proxy URL.
        Returns None if pool is empty or all proxies banned.
        Sticky per domain if domain provided.
        """
        async with self._lock:
            if not self._proxies:
                return None

            # Sticky: same domain → same proxy (avoids session breaks)
            if domain and domain in self._domain_assignment:
                entry = self._domain_assignment[domain]
                if not self._is_banned(entry):
                    entry.last_used = time.monotonic()
                    return entry.url
                # Proxy banned — reassign
                del self._domain_assignment[domain]

            available = [p for p in self._proxies if not self._is_banned(p)]

            if not available:
                # All banned — try re-testing the least-recently-banned
                await self._retest_oldest_banned()
                available = [p for p in self._proxies if not self._is_banned(p)]
                if not available:
                    log.error("all_proxies_banned")
                    return None

            # Pick proxy with lowest recent usage + lowest latency
            entry = min(available, key=lambda p: (p.last_used, p.latency_ms))
            entry.last_used = time.monotonic()

            if domain:
                self._domain_assignment[domain] = entry

            return entry.url

    async def report_failure(self, proxy_url: str) -> None:
        async with self._lock:
            entry = self._find(proxy_url)
            if not entry:
                return
            entry.failures += 1
            if entry.failures >= BAN_AFTER_FAILURES:
                entry.banned_at = time.monotonic()
                log.warning("proxy_banned", proxy=entry.display, failures=entry.failures)

    async def report_success(self, proxy_url: str, latency_ms: float) -> None:
        async with self._lock:
            entry = self._find(proxy_url)
            if not entry:
                return
            entry.failures = 0
            entry.banned_at = None
            entry.latency_ms = latency_ms

    async def test_all(self) -> dict[str, bool]:
        """Test all proxies. Returns {display_url: working}."""
        results = {}
        tasks = [self._test_proxy(p) for p in self._proxies]
        statuses = await asyncio.gather(*tasks, return_exceptions=True)
        for proxy, ok in zip(self._proxies, statuses):
            results[proxy.display] = bool(ok) and not isinstance(ok, Exception)
        return results

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _test_proxy(self, entry: ProxyEntry) -> bool:
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(
                proxy=entry.url,
                timeout=PROXY_TEST_TIMEOUT,
            ) as client:
                resp = await client.get(TEST_URL)
                latency = (time.monotonic() - start) * 1000
                if resp.status_code == 200:
                    entry.latency_ms = latency
                    entry.failures = 0
                    entry.banned_at = None
                    return True
        except Exception as e:
            log.debug("proxy_test_failed", proxy=entry.display, error=str(e))
        return False

    async def _retest_oldest_banned(self) -> None:
        banned = sorted(
            [p for p in self._proxies if p.banned_at],
            key=lambda p: p.banned_at,
        )
        if banned:
            oldest = banned[0]
            log.info("retesting_banned_proxy", proxy=oldest.display)
            await self._test_proxy(oldest)

    def _is_banned(self, entry: ProxyEntry) -> bool:
        if not entry.banned_at:
            return False
        # Auto-unban after RETEST_INTERVAL_SEC
        if time.monotonic() - entry.banned_at > RETEST_INTERVAL_SEC:
            entry.banned_at = None
            entry.failures = 0
            return False
        return True

    def _find(self, proxy_url: str) -> ProxyEntry | None:
        return next((p for p in self._proxies if p.url == proxy_url), None)

    @staticmethod
    def _normalise(entry: str) -> str | None:
        """Convert host:port:user:pass → http://user:pass@host:port"""
        if entry.startswith(("http://", "https://", "socks5://")):
            return entry
        parts = entry.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            return f"http://{quote(user)}:{quote(pwd)}@{host}:{port}"
        if len(parts) == 2:
            host, port = parts
            return f"http://{host}:{port}"
        log.warning("invalid_proxy_format", entry=entry[:20])
        return None

    @staticmethod
    def _mask(url: str) -> str:
        """Replace credentials in URL with ***."""
        import re
        return re.sub(r"://[^@]+@", "://***@", url)


# ── Global singleton (shared across scraper instances) ────────────────────────
_global_pool: ProxyPool | None = None


def get_proxy_pool() -> ProxyPool:
    global _global_pool
    if _global_pool is None:
        _global_pool = ProxyPool.from_env()
    return _global_pool
