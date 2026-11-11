"""
backend/services/cache_service.py — SQLite-Based Query & Answer Caching System

Module 7: SQLite Caching System
Responsibilities:
  - Cache table DDL & indexing for persistent SQLite caching.
  - Deterministic composite cache key generation:
      SHA-256(normalized_query + doc_fingerprint + policy_version + model_version)
  - Policy & context safety:
      - Only complete, verified, policy-grounded answers are cached.
      - Never caches API failures, fallbacks, unsupported topics, or clarification requests.
      - Never caches context-dependent answers or conversation-dependent history.
      - Never caches hallucinated fees (₹4,999) or answers citing only superseded Doc02.
  - Safe cache retrieval with expiration checks, source validation, and LRU eviction.
  - Capacity bounds: Enforces CACHE_MAX_ENTRIES using LRU eviction.
  - Expired-entry cleanup and cache statistics.
  - Fault tolerance: Any SQLite error degrades gracefully to cache-miss without failing chat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any

from config import DOC_REGISTRY, config

logger = logging.getLogger(__name__)


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class CachedAnswer:
    """
    Structured representation of a valid, cached policy answer.
    """
    answer: str
    sources: list[str] = field(default_factory=list)
    cache_key: str = ""
    created_at: str = ""
    expires_at: str = ""
    last_accessed_at: str = ""
    access_count: int = 1
    confidence: float = 1.0
    model_used: Optional[str] = None
    prompt_tokens: Optional[int] = None
    candidates_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


# ── Cache Table Initialization ────────────────────────────────────────────────

def init_cache_table(conn: sqlite3.Connection) -> None:
    """
    Initialize SQLite schema for query_cache if not already present.
    Designed to be called inside an immediate transaction or schema setup.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_cache (
            id TEXT PRIMARY KEY,
            cache_key TEXT UNIQUE NOT NULL,
            normalized_query TEXT NOT NULL,
            answer TEXT NOT NULL,
            sources TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 1,
            confidence REAL DEFAULT 1.0,
            is_in_scope INTEGER NOT NULL DEFAULT 1,
            is_supported INTEGER NOT NULL DEFAULT 1,
            model_used TEXT,
            prompt_tokens INTEGER,
            candidates_tokens INTEGER,
            total_tokens INTEGER
        );
    """)

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_query_cache_key
        ON query_cache(cache_key);
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_query_cache_expires
        ON query_cache(expires_at);
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_query_cache_accessed
        ON query_cache(last_accessed_at DESC);
    """)


# ── CacheService Implementation ───────────────────────────────────────────────

class CacheService:
    """
    Persistent SQLite-based caching service for DPM-101 course policy answers.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        default_ttl: Optional[int] = None,
        enabled: Optional[bool] = None,
        max_entries: Optional[int] = None,
        documents_dir: Optional[str] = None,
        doc_registry: Optional[dict] = None,
        policy_version: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> None:
        self.db_path = db_path or config.DB_PATH
        self.default_ttl = default_ttl if default_ttl is not None else config.CACHE_DEFAULT_TTL
        self.enabled = enabled if enabled is not None else config.CACHE_ENABLED
        self.max_entries = max_entries if max_entries is not None else config.CACHE_MAX_ENTRIES
        self.documents_dir = documents_dir or config.DOCUMENTS_DIR
        self.doc_registry = doc_registry or DOC_REGISTRY
        self.policy_version = policy_version or config.POLICY_VERSION
        self.model_name = model_name or config.GEMINI_MODEL

        # In-memory runtime metrics & microsecond fingerprint cache
        self._hits = 0
        self._misses = 0
        self._cached_fingerprint: Optional[str] = None
        self._cached_mtimes: list[tuple[str, int, int]] = []

        # Initialize schema if enabled
        if self.enabled:
            self._ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        """Import connection utility from chat_service to ensure uniform WAL/pragmas."""
        from backend.services.chat_service import get_db_connection
        return get_db_connection(self.db_path)

    def _ensure_schema(self) -> None:
        """Create cache table and indexes safely."""
        try:
            from backend.services.chat_service import immediate_transaction
            conn = self._get_connection()
            try:
                with immediate_transaction(conn):
                    init_cache_table(conn)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("CacheService could not initialize schema: %s", exc)

    def is_enabled(self) -> bool:
        """Check if caching is globally enabled."""
        return bool(self.enabled)

    # ── Query Normalization & Fingerprinting ──────────────────────────────────

    @staticmethod
    def normalize_query(query: str) -> str:
        """
        Normalize user query string to guarantee identical representations:
          - Trim outer whitespace
          - Convert to lowercase
          - Strip punctuation marks (e.g. trailing ?, !, ., ,, ;)
          - Collapse internal multiple spaces and newlines to single space
        """
        if not query or not isinstance(query, str):
            return ""
        # Strip common outer punctuation
        cleaned = query.strip().lower()
        cleaned = re.sub(r"[?!.,;:\"']+$", "", cleaned)
        cleaned = re.sub(r"^[?!.,;:\"']+", "", cleaned)
        # Collapse multiple whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def compute_documents_fingerprint(
        self,
        documents_dir: Optional[str] = None,
        doc_registry: Optional[dict] = None,
        force_recompute: bool = False,
    ) -> str:
        """
        Compute a SHA-256 fingerprint combining document registry metadata
        and file content hashes. Checks file mtimes for microsecond caching,
        and recomputes automatically whenever any document file is modified.
        """
        registry = doc_registry or self.doc_registry
        doc_dir = Path(documents_dir or self.documents_dir)

        # Check mtimes and sizes of registry files
        current_mtimes: list[tuple[str, int, int]] = []
        for doc_id in sorted(registry.keys()):
            filepath = doc_dir / registry[doc_id].get("filename", "")
            if filepath.exists() and filepath.is_file():
                try:
                    st = filepath.stat()
                    current_mtimes.append((doc_id, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_size))
                except OSError:
                    current_mtimes.append((doc_id, 0, 0))
            else:
                current_mtimes.append((doc_id, 0, 0))

        if not force_recompute and self._cached_fingerprint and current_mtimes == self._cached_mtimes:
            return self._cached_fingerprint

        components = []
        for doc_id in sorted(registry.keys()):
            doc_meta = registry[doc_id]
            filename = doc_meta.get("filename", "")
            last_updated = doc_meta.get("last_updated", "")
            policy_status = doc_meta.get("policy_status", "")

            # Check physical file hash if available
            file_hash = ""
            filepath = doc_dir / filename
            if filepath.exists() and filepath.is_file():
                try:
                    h = hashlib.sha256()
                    with open(filepath, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    file_hash = h.hexdigest()[:16]
                except OSError:
                    file_hash = "unreadable"

            components.append(f"{doc_id}:{filename}:{last_updated}:{policy_status}:{file_hash}")

        raw_fingerprint = "|".join(components)
        fp = hashlib.sha256(raw_fingerprint.encode("utf-8")).hexdigest()[:32]
        self._cached_fingerprint = fp
        self._cached_mtimes = current_mtimes
        return fp

    def generate_cache_key(
        self,
        query: str,
        doc_fingerprint: Optional[str] = None,
        policy_version: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> str:
        """
        Generate a composite SHA-256 cache key accounting for:
          1. Normalized user query
          2. Relevant course-document content fingerprint
          3. Policy and prompt version
          4. Gemini model configuration
        """
        norm_q = self.normalize_query(query)
        dfp = doc_fingerprint or self.compute_documents_fingerprint()
        pver = policy_version or f"{config.PROMPT_VERSION}:{self.policy_version}"
        mver = model_version or f"{self.model_name}:temp={config.GEMINI_TEMPERATURE}"

        composite = f"query:{norm_q}|docs:{dfp}|policy:{pver}|model:{mver}"
        return hashlib.sha256(composite.encode("utf-8")).hexdigest()

    # ── Policy & Storage Safety Validation ────────────────────────────────────

    @staticmethod
    def validate_cacheable(
        answer: str,
        sources: list[str],
        is_in_scope: bool = True,
        is_supported: bool = True,
        is_context_dependent: bool = False,
        is_fallback: bool = False,
        is_clarification: bool = False,
        error: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Strict validation gate before caching:
        Only complete, valid, official-policy answers may be cached.
        """
        if error is not None:
            return False, "Cannot cache error response"
        if not is_in_scope:
            return False, "Cannot cache out-of-scope response"
        if not is_supported:
            return False, "Cannot cache unsupported topic response"
        if is_context_dependent:
            return False, "Cannot cache context-dependent answer"
        if is_clarification:
            return False, "Cannot cache clarification prompt"
        if is_fallback:
            return False, "Cannot cache extractive or fallback answer"
        if not answer or not answer.strip():
            return False, "Cannot cache empty answer"
        if not sources or not isinstance(sources, list) or len(sources) == 0:
            return False, "Cannot cache answer without source attribution"

        # Check for fallback or error substrings in answer
        lower_ans = answer.lower()
        if "please contact the course administration" in lower_ans:
            return False, "Cannot cache unsupported fallback message"
        if "i am unable to process your request at this time" in lower_ans:
            return False, "Cannot cache temporary error message"

        # Check for invalid fee hallucination (e.g. ₹4,999)
        if ("certificate" in lower_ans or "fee" in lower_ans) and ("4,999" in answer or "4999" in answer):
            return False, "Cannot cache invalid certificate fee ₹4,999; official fee is Rs. 1,500"

        return True, "Valid"

    def _validate_stored_sources(self, sources: list[str]) -> bool:
        """
        Verify that cached source attributions remain valid and compliant:
          - No unmoderated forum sources
          - Sources must be non-empty strings
          - Sources must correspond to recognized registered documents
          - Historical Doc02 (superseded) cannot be the sole source
        """
        if not sources:
            return False

        # Build list of recognized document titles and filenames
        valid_titles = {d["title"].lower() for d in self.doc_registry.values()}
        valid_filenames = {d["filename"].lower() for d in self.doc_registry.values()}
        valid_ids = {doc_id.lower() for doc_id in self.doc_registry.keys()}

        # Disallow superseded Doc02 as sole authority
        if len(sources) == 1:
            sole_s = sources[0].lower()
            if "v1" in sole_s or "superseded" in sole_s or "02_grading_policy_v1" in sole_s:
                logger.warning("Rejecting cache entry citing only superseded Doc02: %s", sources[0])
                return False

        for s in sources:
            s_clean = s.strip()
            if not s_clean:
                return False
            s_lower = s_clean.lower()
            # Explicitly reject unmoderated student forum comments/sections
            if "unmoderated" in s_lower or "student comment" in s_lower or "student discussion" in s_lower:
                return False

            # Check if source matches any registered title, filename, or doc_id
            matched = any(
                vt in s_lower or s_lower in vt for vt in valid_titles
            ) or any(
                vf in s_lower for vf in valid_filenames
            ) or any(
                vid == s_lower for vid in valid_ids
            ) or "handbook" in s_lower or "grading" in s_lower or "exam" in s_lower

            if not matched:
                return False

        return True

    # ── Cache Lookup & Read Operations ────────────────────────────────────────

    def get(
        self,
        query: str,
        doc_fingerprint: Optional[str] = None,
        policy_version: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> Optional[CachedAnswer]:
        """
        Retrieve a cached answer for a query.
        Returns CachedAnswer on hit, or None on miss, expiration, or invalid data.
        Fault-tolerant: never raises SQLite errors.
        """
        if not self.enabled:
            return None

        norm_q = self.normalize_query(query)
        if not norm_q:
            return None

        cache_key = self.generate_cache_key(
            norm_q,
            doc_fingerprint=doc_fingerprint,
            policy_version=policy_version,
            model_version=model_version,
        )

        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, cache_key, answer, sources, created_at, expires_at,
                           last_accessed_at, access_count, confidence, is_in_scope,
                           is_supported, model_used, prompt_tokens, candidates_tokens, total_tokens
                    FROM query_cache
                    WHERE cache_key = ?;
                    """,
                    (cache_key,),
                )
                row = cursor.fetchone()
                if not row:
                    self._misses += 1
                    return None

                # Check expiration
                now_utc = datetime.now(timezone.utc)
                expires_at_dt = datetime.fromisoformat(row["expires_at"])
                if expires_at_dt <= now_utc:
                    logger.debug("Cache entry %s expired at %s", cache_key[:12], row["expires_at"])
                    self._misses += 1
                    # Lazy cleanup of this expired entry
                    self._delete_by_key(conn, cache_key)
                    return None

                # Parse and validate stored sources
                try:
                    sources = json.loads(row["sources"]) if row["sources"] else []
                except (json.JSONDecodeError, TypeError):
                    self._misses += 1
                    return None

                if not self._validate_stored_sources(sources):
                    logger.warning("Cache entry %s failed source validation; evicting", cache_key[:12])
                    self._delete_by_key(conn, cache_key)
                    self._misses += 1
                    return None

                # Update access stats safely without locking readers
                now_iso = now_utc.isoformat()
                self._update_access_stats(conn, cache_key, now_iso)

                self._hits += 1
                return CachedAnswer(
                    answer=row["answer"],
                    sources=sources,
                    cache_key=row["cache_key"],
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    last_accessed_at=now_iso,
                    access_count=row["access_count"] + 1,
                    confidence=row["confidence"] or 1.0,
                    model_used=row["model_used"],
                    prompt_tokens=row["prompt_tokens"],
                    candidates_tokens=row["candidates_tokens"],
                    total_tokens=row["total_tokens"],
                )
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("CacheService lookup error for key %s: %s", cache_key[:12], exc)
            self._misses += 1
            return None

    def _delete_by_key(self, conn: sqlite3.Connection, cache_key: str) -> None:
        """Helper to delete an entry by cache key."""
        try:
            from backend.services.chat_service import immediate_transaction
            with immediate_transaction(conn):
                conn.execute("DELETE FROM query_cache WHERE cache_key = ?;", (cache_key,))
        except Exception as exc:
            logger.debug("Failed to delete expired cache key %s: %s", cache_key[:12], exc)

    def _update_access_stats(self, conn: sqlite3.Connection, cache_key: str, now_iso: str) -> None:
        """Increment access count and last accessed timestamp without write lock contention."""
        try:
            conn.execute(
                """
                UPDATE query_cache
                SET access_count = access_count + 1, last_accessed_at = ?
                WHERE cache_key = ?;
                """,
                (now_iso, cache_key),
            )
        except Exception as exc:
            logger.debug("Failed to update access stats for cache key %s: %s", cache_key[:12], exc)

    # ── Capacity Bounds & LRU Eviction ────────────────────────────────────────

    def _enforce_max_entries(self, conn: sqlite3.Connection) -> int:
        """
        Enforce max_entries capacity constraint using LRU eviction.
        First removes expired entries, then evicts the least recently accessed entries.
        Must be called within an immediate transaction.
        """
        if self.max_entries <= 0:
            return 0

        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM query_cache;")
        count = cursor.fetchone()[0]
        if count < self.max_entries:
            return 0

        # 1. Clean expired entries first
        now_iso = datetime.now(timezone.utc).isoformat()
        cursor.execute("DELETE FROM query_cache WHERE expires_at <= ?;", (now_iso,))
        purged = cursor.rowcount

        cursor.execute("SELECT COUNT(*) FROM query_cache;")
        count = cursor.fetchone()[0]
        if count < self.max_entries:
            return purged

        # 2. Evict least recently accessed entries to make space
        excess = count - self.max_entries + 1
        cursor.execute(
            """
            DELETE FROM query_cache
            WHERE id IN (
                SELECT id FROM query_cache
                ORDER BY last_accessed_at ASC
                LIMIT ?
            );
            """,
            (excess,),
        )
        return purged + cursor.rowcount

    # ── Cache Write Operations ────────────────────────────────────────────────

    def set(
        self,
        query: str,
        answer: str,
        sources: list[str],
        ttl: Optional[int] = None,
        is_in_scope: bool = True,
        is_supported: bool = True,
        is_context_dependent: bool = False,
        is_fallback: bool = False,
        is_clarification: bool = False,
        error: Optional[str] = None,
        confidence: float = 1.0,
        model_used: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        candidates_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        doc_fingerprint: Optional[str] = None,
        policy_version: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> bool:
        """
        Store a validated policy answer in SQLite cache.
        Returns True on successful write, False if rejected or failed.
        """
        if not self.enabled:
            return False

        # Validate suitability for caching
        valid, reason = self.validate_cacheable(
            answer=answer,
            sources=sources,
            is_in_scope=is_in_scope,
            is_supported=is_supported,
            is_context_dependent=is_context_dependent,
            is_fallback=is_fallback,
            is_clarification=is_clarification,
            error=error,
        )
        if not valid:
            logger.debug("Skipping caching for query '%s': %s", query[:30], reason)
            return False

        norm_q = self.normalize_query(query)
        if not norm_q:
            return False

        cache_key = self.generate_cache_key(
            norm_q,
            doc_fingerprint=doc_fingerprint,
            policy_version=policy_version,
            model_version=model_version,
        )

        entry_id = str(uuid.uuid4())
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        effective_ttl = ttl if ttl is not None else self.default_ttl
        expires_iso = (now_dt + timedelta(seconds=effective_ttl)).isoformat()
        sources_json = json.dumps(sources)

        try:
            from backend.services.chat_service import immediate_transaction
            conn = self._get_connection()
            try:
                with immediate_transaction(conn):
                    # Enforce max_entries bounds via LRU eviction before insertion
                    self._enforce_max_entries(conn)

                    conn.execute(
                        """
                        INSERT INTO query_cache (
                            id, cache_key, normalized_query, answer, sources,
                            created_at, expires_at, last_accessed_at, access_count,
                            confidence, is_in_scope, is_supported, model_used,
                            prompt_tokens, candidates_tokens, total_tokens
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, 1, ?, ?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            answer = excluded.answer,
                            sources = excluded.sources,
                            expires_at = excluded.expires_at,
                            last_accessed_at = excluded.last_accessed_at,
                            access_count = query_cache.access_count + 1,
                            confidence = excluded.confidence,
                            model_used = excluded.model_used,
                            prompt_tokens = excluded.prompt_tokens,
                            candidates_tokens = excluded.candidates_tokens,
                            total_tokens = excluded.total_tokens;
                        """,
                        (
                            entry_id,
                            cache_key,
                            norm_q,
                            answer,
                            sources_json,
                            now_iso,
                            expires_iso,
                            now_iso,
                            confidence,
                            model_used,
                            prompt_tokens,
                            candidates_tokens,
                            total_tokens,
                        ),
                    )
                return True
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("CacheService write error for key %s: %s", cache_key[:12], exc)
            return False

    # ── Maintenance, Cleanup & Invalidation ───────────────────────────────────

    def invalidate(
        self,
        query: Optional[str] = None,
        cache_key: Optional[str] = None,
    ) -> bool:
        """
        Invalidate a single cache entry by query or cache key.
        Returns True if an entry was removed, False otherwise.
        """
        if not self.enabled:
            return False

        target_key = cache_key
        if not target_key and query:
            target_key = self.generate_cache_key(query)

        if not target_key:
            return False

        try:
            from backend.services.chat_service import immediate_transaction
            conn = self._get_connection()
            try:
                with immediate_transaction(conn):
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM query_cache WHERE cache_key = ?;", (target_key,))
                    return cursor.rowcount > 0
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("CacheService invalidation error: %s", exc)
            return False

    def clear(self) -> int:
        """
        Invalidate all entries in query_cache.
        Returns the number of deleted entries.
        """
        try:
            from backend.services.chat_service import immediate_transaction
            conn = self._get_connection()
            try:
                with immediate_transaction(conn):
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM query_cache;")
                    count = cursor.rowcount
                self._hits = 0
                self._misses = 0
                return count
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("CacheService clear error: %s", exc)
            return 0

    def cleanup_expired(self) -> int:
        """
        Safely purge all expired entries from query_cache in a short transaction.
        Returns the number of deleted records.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            from backend.services.chat_service import immediate_transaction
            conn = self._get_connection()
            try:
                with immediate_transaction(conn):
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM query_cache WHERE expires_at <= ?;", (now_iso,))
                    return cursor.rowcount
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("CacheService cleanup error: %s", exc)
            return 0

    def get_stats(self) -> dict[str, Any]:
        """
        Retrieve cache metrics and performance statistics.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        total_entries = 0
        active_entries = 0
        expired_entries = 0
        total_accesses = 0

        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM query_cache;")
                total_entries = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM query_cache WHERE expires_at > ?;", (now_iso,))
                active_entries = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM query_cache WHERE expires_at <= ?;", (now_iso,))
                expired_entries = cursor.fetchone()[0]

                cursor.execute("SELECT COALESCE(SUM(access_count), 0) FROM query_cache;")
                total_accesses = cursor.fetchone()[0]
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("CacheService get_stats error: %s", exc)

        total_lookups = self._hits + self._misses
        hit_rate = (self._hits / total_lookups) if total_lookups > 0 else 0.0

        return {
            "enabled": self.enabled,
            "max_entries": self.max_entries,
            "total_entries": total_entries,
            "active_entries": active_entries,
            "expired_entries": expired_entries,
            "total_accesses": total_accesses,
            "runtime_hits": self._hits,
            "runtime_misses": self._misses,
            "runtime_hit_rate": round(hit_rate, 4),
            "default_ttl_seconds": self.default_ttl,
        }
