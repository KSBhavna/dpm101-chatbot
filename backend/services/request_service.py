"""
backend/services/request_service.py — Request Manager, Idempotency & In-Flight Concurrency

Module 8: Request Management & Duplicate Prevention
Responsibilities:
  - Enforce per-conversation lock serialization to preserve turn order.
  - Atomic idempotency key tracking and rapid duplicate detection.
  - Automatic SHA-256 fingerprinting for double-click prevention.
  - In-flight request coordination via thread synchronization (Condition variables).
  - Deterministic response replay with `is_duplicate: true` and `X-Duplicate-Request: true`.
  - Detection and rejection of key collisions across different payloads or conversations (HTTP 409).
  - Bounded lock timeouts (5.0s default) returning HTTP 409 Conflict when conversation is busy.
  - Failure broadcasting: woke duplicate waiters on primary request failure.
  - Memory hygiene: periodic pruning of expired records and stale locks.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any

from config import config

logger = logging.getLogger(__name__)

# Valid request_id syntax: 1-128 chars, alphanumeric, underscore, hyphen
REQUEST_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")


class RequestState(Enum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RequestManagementError(Exception):
    """Base exception for request management errors."""
    pass


class InvalidRequestIdError(RequestManagementError):
    """Raised when an explicit request_id fails format validation."""
    pass


class MismatchedRequestIdError(RequestManagementError):
    """Raised when header X-Request-ID and body request_id do not match."""
    pass


class IdempotencyConflictError(RequestManagementError):
    """Raised when an idempotency key is reused for a different payload or conversation."""
    pass


class ConversationBusyError(RequestManagementError):
    """Raised when a per-conversation lock cannot be acquired within the timeout."""
    pass


@dataclass
class RequestRecord:
    key: str
    conversation_id: str
    query_hash: str
    normalized_query: str
    status: RequestState
    created_at: float  # monotonic timestamp
    expires_at: float  # monotonic timestamp
    is_explicit: bool
    response_payload: Optional[dict[str, Any]] = None
    error: Optional[Exception] = None
    condition: Optional[threading.Condition] = None


class RequestManager:
    """
    Coordinates per-conversation serialization, atomic idempotency checks,
    in-flight thundering herd suppression, and response replay.
    """

    def __init__(
        self,
        duplicate_window: Optional[int] = None,
        idempotency_ttl: Optional[int] = None,
        lock_timeout: Optional[float] = None,
    ) -> None:
        self.duplicate_window: int = (
            duplicate_window
            if duplicate_window is not None
            else getattr(config, "DUPLICATE_WINDOW_SECONDS", 10)
        )
        self.idempotency_ttl: int = (
            idempotency_ttl
            if idempotency_ttl is not None
            else getattr(config, "IDEMPOTENCY_KEY_TTL", 300)
        )
        self.lock_timeout: float = (
            lock_timeout
            if lock_timeout is not None
            else getattr(config, "CONVERSATION_LOCK_TIMEOUT", 5.0)
        )

        self._master_lock = threading.Lock()
        # Key -> RequestRecord
        self._records: dict[str, RequestRecord] = {}
        # Conversation_id -> threading.Lock
        self._conv_locks: dict[str, threading.Lock] = {}
        # Conversation_id -> reference count of active/waiting threads
        self._conv_lock_refs: dict[str, int] = {}

        self._total_requests: int = 0
        self._duplicate_replays: int = 0
        self._in_flight_waits: int = 0
        self._lock_timeouts: int = 0

    @staticmethod
    def normalize_query(query: str) -> str:
        """
        Normalize query string safely:
        - Strip leading and trailing whitespace.
        - Case-fold to lowercase.
        - Collapse multiple internal whitespace characters to a single space.
        - Preserves punctuation and word order to avoid false-positive collisions.
        """
        stripped = query.strip().lower()
        return re.sub(r"\s+", " ", stripped)

    @staticmethod
    def compute_query_hash(normalized_query: str) -> str:
        """Compute SHA-256 hash of the normalized query text alone."""
        return hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_implicit_key(conversation_id: str, normalized_query: str) -> str:
        """
        Compute deterministic implicit fingerprint for double-click protection:
        SHA-256(conversation_id + "::" + normalized_query)
        """
        payload = f"{conversation_id}::{normalized_query}".encode("utf-8")
        return f"auto_{hashlib.sha256(payload).hexdigest()}"

    @staticmethod
    def validate_request_id(key: Optional[str]) -> None:
        """Validate explicit idempotency key format."""
        if key is None:
            return
        if not isinstance(key, str) or not REQUEST_ID_REGEX.match(key):
            raise InvalidRequestIdError(
                "Invalid request_id format. Must be 1-128 alphanumeric characters, underscores, or hyphens."
            )

    @staticmethod
    def resolve_and_validate_keys(
        header_key: Optional[str], body_key: Optional[str]
    ) -> Optional[str]:
        """
        Resolve and validate client-supplied request keys.
        Raises MismatchedRequestIdError if both supplied and differ.
        Raises InvalidRequestIdError if syntax is invalid.
        """
        h_clean = header_key.strip() if header_key else None
        b_clean = body_key.strip() if body_key else None

        if h_clean and b_clean and h_clean != b_clean:
            raise MismatchedRequestIdError(
                "Mismatched X-Request-ID header and body request_id"
            )

        chosen = h_clean or b_clean
        if chosen:
            RequestManager.validate_request_id(chosen)
        return chosen

    # ── Per-Conversation Lock Management ──────────────────────────────────────

    def _get_conversation_lock(self, conversation_id: str) -> threading.Lock:
        """Get or create the per-conversation lock under master lock protection."""
        with self._master_lock:
            if conversation_id not in self._conv_locks:
                self._conv_locks[conversation_id] = threading.Lock()
                self._conv_lock_refs[conversation_id] = 0
            self._conv_lock_refs[conversation_id] += 1
            return self._conv_locks[conversation_id]

    def _release_conversation_lock_ref(self, conversation_id: str) -> None:
        """Decrement reference count and prune lock when no longer in use."""
        with self._master_lock:
            if conversation_id in self._conv_lock_refs:
                self._conv_lock_refs[conversation_id] -= 1
                if self._conv_lock_refs[conversation_id] <= 0:
                    self._conv_locks.pop(conversation_id, None)
                    self._conv_lock_refs.pop(conversation_id, None)

    def acquire_conversation_lock(
        self, conversation_id: str, timeout: Optional[float] = None
    ) -> bool:
        """
        Acquire per-conversation lock with bounded timeout.
        Returns True if acquired, False on timeout.
        """
        wait_time = timeout if timeout is not None else self.lock_timeout
        lock = self._get_conversation_lock(conversation_id)
        acquired = lock.acquire(timeout=wait_time)
        if not acquired:
            self._release_conversation_lock_ref(conversation_id)
            with self._master_lock:
                self._lock_timeouts += 1
        return acquired

    def release_conversation_lock(self, conversation_id: str) -> None:
        """Release per-conversation lock safely."""
        with self._master_lock:
            lock = self._conv_locks.get(conversation_id)
        if lock and lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass
        self._release_conversation_lock_ref(conversation_id)

    def get_completed_response(self, key: str) -> Optional[dict[str, Any]]:
        """
        Check if a completed response is currently cached for this key.
        Returns the response payload or None if not found or expired.
        """
        now = time.monotonic()
        with self._master_lock:
            record = self._records.get(key)
            if record and record.status == RequestState.COMPLETED:
                if now < record.expires_at:
                    self._duplicate_replays += 1
                    return record.response_payload
                else:
                    del self._records[key]
            return None

    # ── Atomic Request Registration & Duplicate Coordination ──────────────────

    def start_request(
        self,
        conversation_id: str,
        raw_query: str,
        client_request_id: Optional[str] = None,
    ) -> tuple[str, bool, Optional[dict[str, Any]], Optional[Any]]:
        """
        Atomically inspect idempotency records and register in-flight state.

        Returns:
          (key, is_duplicate, replayed_response, wait_target)

        Behaviors:
          - If a completed duplicate exists: returns (key, True, response, None).
          - If an in-flight duplicate exists: returns (key, True, None, record).
          - If new request: registers new record as PROCESSING, returns (key, False, None, None).
        """
        norm_q = self.normalize_query(raw_query)
        q_hash = self.compute_query_hash(norm_q)

        is_explicit = client_request_id is not None
        key = (
            client_request_id
            if is_explicit
            else self.compute_implicit_key(conversation_id, norm_q)
        )
        ttl = self.idempotency_ttl if is_explicit else self.duplicate_window
        now = time.monotonic()

        with self._master_lock:
            self._total_requests += 1

            # Check existing record
            existing = self._records.get(key)
            if existing is not None:
                # Check expiration
                if now >= existing.expires_at:
                    # Expired entry: prune it
                    del self._records[key]
                    existing = None

            if existing is not None:
                # Check for payload or conversation collision
                if (
                    existing.conversation_id != conversation_id
                    or existing.query_hash != q_hash
                ):
                    raise IdempotencyConflictError(
                        "Idempotency key already used for a different request payload or conversation"
                    )

                if existing.status == RequestState.COMPLETED:
                    self._duplicate_replays += 1
                    # Replay cached response
                    return key, True, existing.response_payload, None

                if existing.status == RequestState.PROCESSING:
                    self._in_flight_waits += 1
                    # Secondary thread receives direct record reference
                    return key, True, None, existing

                if existing.status == RequestState.FAILED:
                    # Previous attempt failed; remove failed record and allow fresh attempt
                    del self._records[key]

            # New request: atomically create record in PROCESSING state
            cond = threading.Condition(self._master_lock)
            record = RequestRecord(
                key=key,
                conversation_id=conversation_id,
                query_hash=q_hash,
                normalized_query=norm_q,
                status=RequestState.PROCESSING,
                created_at=now,
                expires_at=now + ttl,
                is_explicit=is_explicit,
                condition=cond,
            )
            self._records[key] = record
            return key, False, None, None

    def wait_for_in_flight(
        self,
        target: Any,
        condition: Optional[threading.Condition] = None,
        timeout: float = 35.0,
    ) -> dict[str, Any]:
        """
        Wait for an active in-flight request to complete.
        Accepts either:
          - (record: RequestRecord)
          - (key: str, condition: threading.Condition)

        Returns the completed response payload.
        Raises the underlying exception if the primary request failed.
        """
        deadline = time.monotonic() + timeout

        if isinstance(target, RequestRecord):
            record = target
            cond = record.condition
        else:
            key = str(target)
            cond = condition
            record = self._records.get(key)

        if not cond:
            raise RequestManagementError("In-flight request has no wait condition")

        with cond:
            while True:
                if isinstance(target, str):
                    record = self._records.get(target)

                if not record:
                    raise RequestManagementError("In-flight request record was pruned or lost")

                if record.status == RequestState.COMPLETED:
                    if record.response_payload is None:
                        raise RequestManagementError("In-flight request completed without response")
                    return record.response_payload

                if record.status == RequestState.FAILED:
                    if record.error:
                        raise record.error
                    raise RequestManagementError("In-flight request failed without specific exception")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for in-flight request to complete")

                # Bounded condition wait
                cond.wait(timeout=min(remaining, 1.0))

    def complete_request(self, key: str, response_payload: dict[str, Any]) -> None:
        """
        Mark an in-flight request as COMPLETED, store response payload,
        and notify all waiting threads.
        """
        with self._master_lock:
            record = self._records.get(key)
            if record:
                record.status = RequestState.COMPLETED
                record.response_payload = response_payload
                # Refresh expiration from completion time
                ttl = self.idempotency_ttl if record.is_explicit else self.duplicate_window
                record.expires_at = time.monotonic() + ttl
                if record.condition:
                    record.condition.notify_all()

    def fail_request(self, key: str, error: Exception) -> None:
        """
        Mark an in-flight request as FAILED, store error, and wake all waiting threads.
        """
        with self._master_lock:
            record = self._records.get(key)
            if record:
                record.status = RequestState.FAILED
                record.error = error
                record.expires_at = time.monotonic() + 5.0
                if record.condition:
                    record.condition.notify_all()
                record.error = error
                # Keep failed record briefly so concurrent waiters observe the error
                record.expires_at = time.monotonic() + 5.0
                if record.condition:
                    record.condition.notify_all()

    def cleanup_stale(self) -> int:
        """
        Prune expired idempotency records.
        Returns count of pruned records.
        """
        now = time.monotonic()
        with self._master_lock:
            stale_keys = [
                k for k, rec in self._records.items()
                if now >= rec.expires_at and rec.status != RequestState.PROCESSING
            ]
            for k in stale_keys:
                del self._records[k]
            return len(stale_keys)

    def reset(self) -> None:
        """Clear all in-flight and completed records and locks (for tests)."""
        with self._master_lock:
            self._records.clear()
            self._conv_locks.clear()
            self._conv_lock_refs.clear()
            self._total_requests = 0
            self._duplicate_replays = 0
            self._in_flight_waits = 0
            self._lock_timeouts = 0

    def get_stats(self) -> dict[str, Any]:
        """Return operational metrics for request management."""
        with self._master_lock:
            active_records = len(self._records)
            processing = sum(1 for r in self._records.values() if r.status == RequestState.PROCESSING)
            completed = sum(1 for r in self._records.values() if r.status == RequestState.COMPLETED)
            active_conv_locks = len(self._conv_locks)
            return {
                "active_records": active_records,
                "in_flight_processing": processing,
                "completed_cached": completed,
                "active_conversation_locks": active_conv_locks,
                "total_requests": self._total_requests,
                "duplicate_replays": self._duplicate_replays,
                "in_flight_waits": self._in_flight_waits,
                "lock_timeouts": self._lock_timeouts,
                "duplicate_window_seconds": self.duplicate_window,
                "idempotency_ttl_seconds": self.idempotency_ttl,
                "lock_timeout_seconds": self.lock_timeout,
            }
