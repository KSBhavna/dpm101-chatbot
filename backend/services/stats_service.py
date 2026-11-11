"""
backend/services/stats_service.py — Thread-Safe Statistics & Live Counters Service

Module 9: Statistics & Live Counters
Maintains real-time in-process operational metrics for the single Gunicorn worker:
  - total_requests: chat message requests received
  - cache_hits: queries served directly from SQLite cache
  - gemini_calls: actual API invocations to Google Gemini
  - errors: server-side operational or generation failures
  - uptime_seconds: elapsed seconds since worker process start
  - worker_note: disclaimer explaining single-worker scope and restart loss

All metrics are synchronized via threading.Lock to guarantee thread-safe
atomic updates and consistent snapshot retrieval across concurrent worker threads.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Any
from flask import current_app

logger = logging.getLogger(__name__)

WORKER_NOTE = "Counters reflect the current worker process only. Reset on restart."


class StatsService:
    """
    Thread-safe in-process counter service for operational monitoring.
    """

    def __init__(self, start_time: Optional[float] = None) -> None:
        self._lock = threading.Lock()
        self._start_time = start_time if start_time is not None else time.time()
        self._total_requests = 0
        self._cache_hits = 0
        self._gemini_calls = 0
        self._errors = 0

    @property
    def start_time(self) -> float:
        """Process start timestamp."""
        return self._start_time

    def increment_requests(self, count: int = 1) -> None:
        """Increment total_requests atomically."""
        with self._lock:
            self._total_requests += count

    def increment_cache_hits(self, count: int = 1) -> None:
        """Increment cache_hits atomically."""
        with self._lock:
            self._cache_hits += count

    def increment_gemini_calls(self, count: int = 1) -> None:
        """Increment gemini_calls atomically."""
        with self._lock:
            self._gemini_calls += count

    def increment_errors(self, count: int = 1) -> None:
        """Increment errors atomically."""
        with self._lock:
            self._errors += count

    def get_stats(self) -> dict[str, Any]:
        """
        Retrieve a thread-safe snapshot of public operational statistics.
        Returns exactly the 6 fields specified in Architecture Specification §9:
          - total_requests: int
          - cache_hits: int
          - gemini_calls: int
          - errors: int
          - uptime_seconds: int
          - worker_note: str
        """
        with self._lock:
            uptime = int(time.time() - self._start_time)
            return {
                "total_requests": self._total_requests,
                "cache_hits": self._cache_hits,
                "gemini_calls": self._gemini_calls,
                "errors": self._errors,
                "uptime_seconds": max(0, uptime),
                "worker_note": WORKER_NOTE,
            }

    def reset(self) -> None:
        """
        Reset runtime counters to zero (primarily for unit test isolation).
        Does not alter worker start time.
        """
        with self._lock:
            self._total_requests = 0
            self._cache_hits = 0
            self._gemini_calls = 0
            self._errors = 0


# Module-level singleton instance
_stats_service_instance: Optional[StatsService] = None


def get_stats_service() -> StatsService:
    """
    Retrieve configured StatsService instance.
    Prefers current_app.extensions["stats_service"] if inside an active Flask app context,
    otherwise uses the module-level singleton instance.
    """
    global _stats_service_instance
    try:
        if current_app:
            app_service = current_app.extensions.get("stats_service")
            if app_service:
                return app_service
    except (RuntimeError, AttributeError):
        pass

    if _stats_service_instance is None:
        _stats_service_instance = StatsService()
    return _stats_service_instance


def set_stats_service(service: Optional[StatsService]) -> None:
    """Explicitly inject a StatsService instance (useful for unit test isolation)."""
    global _stats_service_instance
    _stats_service_instance = service
