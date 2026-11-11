"""
backend/services/rate_limiter.py — Sliding-Window In-Process Rate Limiter

Module 8: Request Management & Duplicate Prevention
Responsibilities:
  - Thread-safe sliding-window rate limiting using sub-second monotonic timestamps.
  - Per-IP rate limiting (default: 20 requests per 60 seconds).
  - Per-conversation rate limiting (default: 15 requests per 60 seconds).
  - Accurate Retry-After calculation based on oldest timestamp in window.
  - Atomic dual-key check (IP + conversation) so a conversation reject does not consume IP quota.
  - Dynamic cleanup of stale timestamps and memory hygiene.
  - Reset capability for deterministic unit and concurrency testing.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict, deque
from typing import Optional, Any

from config import config

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    """
    In-memory thread-safe sliding window rate limiter.
    Designed for single-worker multi-threaded Gunicorn deployments.
    """

    def __init__(
        self,
        default_ip_limit: Optional[int] = None,
        default_ip_window: Optional[int] = None,
        default_conv_limit: Optional[int] = None,
        default_conv_window: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.enabled: bool = (
            enabled if enabled is not None else getattr(config, "RATE_LIMIT_ENABLED", True)
        )
        self.ip_limit: int = (
            default_ip_limit
            if default_ip_limit is not None
            else getattr(config, "RATE_LIMIT_MAX_REQUESTS", 20)
        )
        self.ip_window: int = (
            default_ip_window
            if default_ip_window is not None
            else getattr(config, "RATE_LIMIT_WINDOW_SECONDS", 60)
        )
        self.conv_limit: int = (
            default_conv_limit
            if default_conv_limit is not None
            else getattr(config, "RATE_LIMIT_CONVERSATION_MAX", 15)
        )
        self.conv_window: int = (
            default_conv_window
            if default_conv_window is not None
            else getattr(config, "RATE_LIMIT_CONVERSATION_WINDOW", 60)
        )

        self._lock = threading.Lock()
        # Maps key -> deque of monotonic float timestamps
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._total_requests_checked: int = 0
        self._total_requests_rejected: int = 0

    def is_enabled(self) -> bool:
        """Return True if rate limiting is enabled."""
        return self.enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable rate limiting dynamically."""
        with self._lock:
            self.enabled = enabled

    def _prune_window(self, queue: deque[float], cutoff: float) -> None:
        """Remove timestamps older than cutoff from the left of the deque."""
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def is_allowed(
        self,
        key: str,
        max_requests: Optional[int] = None,
        window_seconds: Optional[int] = None,
        consume: bool = True,
    ) -> tuple[bool, int]:
        """
        Check if a single key is allowed under sliding window.
        Returns:
          (is_allowed: bool, retry_after: int)
        """
        if not self.enabled:
            return True, 0

        limit = max_requests if max_requests is not None else self.ip_limit
        window = window_seconds if window_seconds is not None else self.ip_window

        now = time.monotonic()
        cutoff = now - window

        with self._lock:
            self._total_requests_checked += 1
            queue = self._windows[key]
            self._prune_window(queue, cutoff)

            if len(queue) >= limit:
                # Rate limit exceeded; calculate retry_after from oldest timestamp in window
                oldest = queue[0]
                retry_after = max(1, int(math.ceil(oldest + window - now)))
                self._total_requests_rejected += 1
                return False, retry_after

            if consume:
                queue.append(now)

            return True, 0

    def check_and_consume(
        self,
        client_ip: str,
        conversation_id: Optional[str] = None,
    ) -> tuple[bool, str, int]:
        """
        Atomically check and consume quota for both client IP and conversation ID.
        Ensures all-or-nothing consumption so a conversation limit rejection
        does not prematurely penalize the client's IP quota.

        Returns:
          (allowed: bool, error_reason: str, retry_after: int)
        """
        if not self.enabled:
            return True, "", 0

        now = time.monotonic()
        ip_cutoff = now - self.ip_window
        conv_cutoff = now - self.conv_window

        ip_key = f"ip:{client_ip}"
        conv_key = f"conv:{conversation_id}" if conversation_id else None

        with self._lock:
            self._total_requests_checked += 1

            # 1. Evaluate IP quota
            ip_queue = self._windows[ip_key]
            self._prune_window(ip_queue, ip_cutoff)
            if len(ip_queue) >= self.ip_limit:
                oldest_ip = ip_queue[0]
                retry_after = max(1, int(math.ceil(oldest_ip + self.ip_window - now)))
                self._total_requests_rejected += 1
                return False, f"IP rate limit exceeded ({self.ip_limit} requests per {self.ip_window}s)", retry_after

            # 2. Evaluate conversation quota (if conversation_id provided)
            if conv_key:
                conv_queue = self._windows[conv_key]
                self._prune_window(conv_queue, conv_cutoff)
                if len(conv_queue) >= self.conv_limit:
                    oldest_conv = conv_queue[0]
                    retry_after = max(1, int(math.ceil(oldest_conv + self.conv_window - now)))
                    self._total_requests_rejected += 1
                    return False, f"Conversation rate limit exceeded ({self.conv_limit} requests per {self.conv_window}s)", retry_after

            # 3. Both allowed: consume atomically
            ip_queue.append(now)
            if conv_key:
                self._windows[conv_key].append(now)

            return True, "", 0

    def reset(self) -> None:
        """Reset all rate limiting windows and counters (useful for unit tests)."""
        with self._lock:
            self._windows.clear()
            self._total_requests_checked = 0
            self._total_requests_rejected = 0

    def cleanup_stale(self) -> int:
        """
        Evict expired timestamps across all tracked keys.
        Returns the count of pruned keys that are now completely empty.
        """
        now = time.monotonic()
        max_window = max(self.ip_window, self.conv_window)
        cutoff = now - max_window

        with self._lock:
            empty_keys = []
            for key, queue in list(self._windows.items()):
                self._prune_window(queue, cutoff)
                if not queue:
                    empty_keys.append(key)
            for k in empty_keys:
                del self._windows[k]
            return len(empty_keys)

    def get_stats(self) -> dict[str, Any]:
        """Return rate limiter operational metrics."""
        with self._lock:
            active_keys = len(self._windows)
            active_timestamps = sum(len(q) for q in self._windows.values())
            return {
                "enabled": self.enabled,
                "ip_limit": self.ip_limit,
                "ip_window_seconds": self.ip_window,
                "conversation_limit": self.conv_limit,
                "conversation_window_seconds": self.conv_window,
                "active_keys_tracked": active_keys,
                "active_timestamps_tracked": active_timestamps,
                "total_requests_checked": self._total_requests_checked,
                "total_requests_rejected": self._total_requests_rejected,
            }
