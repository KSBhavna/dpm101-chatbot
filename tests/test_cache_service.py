"""
tests/test_cache_service.py — Module 7: SQLite Caching System Test Suite

Comprehensive tests verifying:
  1. Cache initialization and schema integrity.
  2. Composite SHA-256 key generation (query, doc fingerprint, policy version, model version).
  3. Query normalization (casing, whitespace, punctuation).
  4. Cache hit, cache miss, access count, and last accessed timestamp updates.
  5. Configurable TTL, expiration detection, and safe expired-entry cleanup.
  6. Cache update and cache invalidation (by query, by key, clear all).
  7. Policy & context safety exclusion:
     - Context-dependent follow-up exclusions.
     - Clarification prompt exclusions.
     - Unsupported and out-of-scope answer exclusions.
     - Extractive/API error fallback exclusions.
     - Stored source validation and eviction.
  8. Fault tolerance & error resilience (database error handling, SQL injection safety).
  9. High-concurrency multithreaded read/write safety.
 10. End-to-end integration with PolicyService and ChatService pipelines.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest

from config import DOC_REGISTRY, config
from backend.services.cache_service import CacheService, CachedAnswer, init_cache_table
from backend.services.chat_service import ChatService, get_db_connection
from backend.services.policy_service import PolicyService, PolicyAnswer
from backend.services.gemini_service import GeminiService, GeminiResponse


# ── Test Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def temp_cache_db(tmp_path):
    """Provides a fresh isolated SQLite database path per test."""
    db_file = tmp_path / "test_cache.db"
    return str(db_file)


@pytest.fixture
def cache_service(temp_cache_db):
    """Provides an isolated CacheService instance."""
    return CacheService(
        db_path=temp_cache_db,
        default_ttl=3600,
        enabled=True,
    )


# ── Category 1: Cache Initialization & Schema ─────────────────────────────────

class TestCacheInitialization:

    def test_init_cache_creates_table_and_indexes(self, temp_cache_db):
        """1. Schema DDL creates query_cache table and all 3 required indexes."""
        service = CacheService(db_path=temp_cache_db, enabled=True)
        conn = get_db_connection(temp_cache_db)
        try:
            cursor = conn.cursor()
            # Verify table existence
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='query_cache';")
            assert cursor.fetchone() is not None

            # Verify indexes
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='query_cache';")
            idx_names = {row["name"] for row in cursor.fetchall()}
            assert "idx_query_cache_key" in idx_names
            assert "idx_query_cache_expires" in idx_names
            assert "idx_query_cache_accessed" in idx_names
        finally:
            conn.close()

    def test_init_cache_idempotent(self, temp_cache_db):
        """2. Calling initialization multiple times does not raise or duplicate tables."""
        service = CacheService(db_path=temp_cache_db, enabled=True)
        service._ensure_schema()
        service._ensure_schema()
        assert service.is_enabled() is True

    def test_cache_disabled_mode(self, temp_cache_db):
        """3. When caching is disabled, get and set return None/False immediately."""
        disabled_service = CacheService(db_path=temp_cache_db, enabled=False)
        assert disabled_service.is_enabled() is False
        assert disabled_service.get("What is the passing score?") is None
        assert disabled_service.set(
            "What is the passing score?",
            "Passing is 60%.",
            sources=["Grading Policy V2"],
        ) is False


# ── Category 2: Cache Key Generation ──────────────────────────────────────────

class TestCacheKeyGeneration:

    def test_same_query_same_key_under_identical_config(self, cache_service):
        """4. Exact same query produces identical 64-character SHA-256 hex key."""
        k1 = cache_service.generate_cache_key("What is the passing requirement?")
        k2 = cache_service.generate_cache_key("What is the passing requirement?")
        assert k1 == k2
        assert len(k1) == 64
        int(k1, 16)  # Verifies valid hex

    def test_normalization_whitespace_casing_punctuation_produces_identical_key(self, cache_service):
        """5. Whitespace, casing, and trailing punctuation variations produce the same key."""
        q1 = "What is the passing score?"
        q2 = "  what is the passing score?  "
        q3 = "WHAT IS THE PASSING SCORE"
        q4 = "what   is   the   passing   score???"

        k1 = cache_service.generate_cache_key(q1)
        k2 = cache_service.generate_cache_key(q2)
        k3 = cache_service.generate_cache_key(q3)
        k4 = cache_service.generate_cache_key(q4)

        assert k1 == k2 == k3 == k4

    def test_different_queries_produce_different_keys(self, cache_service):
        """6. Substantially different questions produce distinct keys."""
        k1 = cache_service.generate_cache_key("What is the passing score?")
        k2 = cache_service.generate_cache_key("When is the final exam?")
        k3 = cache_service.generate_cache_key("What is the capstone deadline?")
        assert len({k1, k2, k3}) == 3

    def test_document_version_change_invalidates_key(self, cache_service):
        """7. Changes in official document versions produce a completely different cache key."""
        k_v1 = cache_service.generate_cache_key("What is the passing score?", doc_fingerprint="doc_fp_v1")
        k_v2 = cache_service.generate_cache_key("What is the passing score?", doc_fingerprint="doc_fp_v2")
        assert k_v1 != k_v2

    def test_policy_version_change_invalidates_key(self, cache_service):
        """8. Changes in policy version (e.g. prompt v1 -> v2) produce a different cache key."""
        k_p1 = cache_service.generate_cache_key("What is the passing score?", policy_version="policy_v1")
        k_p2 = cache_service.generate_cache_key("What is the passing score?", policy_version="policy_v2")
        assert k_p1 != k_p2

    def test_model_version_change_invalidates_key(self, cache_service):
        """9. Changes in LLM model or temperature produce a different cache key."""
        k_m1 = cache_service.generate_cache_key("What is the passing score?", model_version="gemini-3.8-flash:temp=0.1")
        k_m2 = cache_service.generate_cache_key("What is the passing score?", model_version="gemini-pro:temp=0.1")
        assert k_m1 != k_m2


# ── Category 3: Cache Hit, Miss, and Updates ──────────────────────────────────

class TestCacheHitAndMiss:

    def test_cache_miss_on_empty_cache(self, cache_service):
        """10. Uncached query returns None and increments runtime misses."""
        res = cache_service.get("What is the passing score?")
        assert res is None
        stats = cache_service.get_stats()
        assert stats["runtime_misses"] == 1
        assert stats["runtime_hits"] == 0

    def test_cache_hit_after_storing(self, cache_service):
        """11. Storing an answer then querying returns CachedAnswer and increments hits."""
        q = "What is the passing score?"
        ans = "The passing score is 60% overall and 40% in the final exam."
        sources = ["Grading & Late Submission Policy V2 (Current)"]

        stored = cache_service.set(query=q, answer=ans, sources=sources, model_used="gemini-3.8-flash")
        assert stored is True

        cached: CachedAnswer = cache_service.get(q)
        assert cached is not None
        assert cached.answer == ans
        assert cached.sources == sources
        assert cached.model_used == "gemini-3.8-flash"
        assert cached.access_count == 2  # 1 initial + 1 on get

        stats = cache_service.get_stats()
        assert stats["runtime_hits"] == 1
        assert stats["active_entries"] == 1

    def test_last_accessed_at_updates_on_hit(self, cache_service):
        """12. Accessing a cached entry updates its last_accessed_at timestamp."""
        q = "What is the distinction threshold?"
        ans = "Distinction requires 85% or higher."
        cache_service.set(q, ans, sources=["Grading Policy V2"])

        cached1 = cache_service.get(q)
        time1 = cached1.last_accessed_at

        # Access again after brief pause
        time.sleep(0.01)
        cached2 = cache_service.get(q)
        time2 = cached2.last_accessed_at

        assert cached2.access_count == 3
        assert time2 >= time1

    def test_source_attribution_preserved_on_hit(self, cache_service):
        """13. Stored sources are returned exactly without data loss."""
        q = "When is the final exam?"
        sources = ["Final Exam Rules & Student Forum Excerpts", "Course Handbook & Overview"]
        cache_service.set(q, "Exam is 20 November 2026.", sources=sources)

        cached = cache_service.get(q)
        assert cached.sources == sources


# ── Category 4: TTL, Expiration & Maintenance ─────────────────────────────────

class TestCacheExpirationAndCleanup:

    def test_expired_entry_returns_none_miss(self, cache_service):
        """14. An entry with an expired timestamp is treated as a cache miss."""
        q = "What is the capstone deadline?"
        ans = "Deadline is 15 November 2026."
        # Set with 0-second TTL (expires immediately)
        cache_service.set(q, ans, sources=["Grading Policy V2"], ttl=-1)

        cached = cache_service.get(q)
        assert cached is None
        stats = cache_service.get_stats()
        assert stats["runtime_misses"] == 1

    def test_cleanup_expired_removes_only_expired_entries(self, cache_service, temp_cache_db):
        """15. cleanup_expired safely removes expired records while keeping active ones."""
        # Active entry (TTL 3600)
        cache_service.set("Active query", "Active answer", sources=["Handbook"], ttl=3600)
        # Expired entry (TTL -10)
        cache_service.set("Expired query 1", "Old answer 1", sources=["Handbook"], ttl=-10)
        cache_service.set("Expired query 2", "Old answer 2", sources=["Handbook"], ttl=-10)

        cleaned_count = cache_service.cleanup_expired()
        assert cleaned_count == 2

        stats = cache_service.get_stats()
        assert stats["active_entries"] == 1
        assert stats["total_entries"] == 1

        # Active entry is still retrievable
        assert cache_service.get("Active query") is not None
        assert cache_service.get("Expired query 1") is None

    def test_cache_update_overwrites_existing_answer(self, cache_service):
        """16. Calling set with the same query updates the answer and refreshes TTL."""
        q = "What is the passing score?"
        cache_service.set(q, "Initial answer 60%", sources=["Policy V2"], ttl=100)
        cache_service.set(q, "Updated answer 60% with 40% exam min", sources=["Policy V2"], ttl=500)

        cached = cache_service.get(q)
        assert "Updated answer" in cached.answer

    def test_invalidate_by_query_and_key(self, cache_service):
        """17. Cache invalidation by query or explicit cache key removes entry."""
        cache_service.set("Question A", "Answer A", sources=["Handbook"])
        cache_service.set("Question B", "Answer B", sources=["Handbook"])

        # Invalidate A by query
        assert cache_service.invalidate(query="Question A") is True
        assert cache_service.get("Question A") is None
        assert cache_service.get("Question B") is not None

        # Invalidate B by key
        key_b = cache_service.generate_cache_key("Question B")
        assert cache_service.invalidate(cache_key=key_b) is True
        assert cache_service.get("Question B") is None

    def test_clear_removes_all_entries(self, cache_service):
        """18. clear() flushes the entire query_cache table."""
        cache_service.set("Q1", "A1", sources=["Handbook"])
        cache_service.set("Q2", "A2", sources=["Handbook"])
        cache_service.set("Q3", "A3", sources=["Handbook"])

        deleted = cache_service.clear()
        assert deleted == 3
        stats = cache_service.get_stats()
        assert stats["total_entries"] == 0


# ── Category 5: Policy & Context Safety ───────────────────────────────────────

class TestPolicyAndContextSafety:

    def test_context_dependent_answer_excluded_from_cache(self, cache_service):
        """19. Context-dependent follow-up answers are explicitly rejected by set()."""
        stored = cache_service.set(
            query="How long is it?",
            answer="The final exam is 90 minutes.",
            sources=["Exam Rules"],
            is_context_dependent=True,
        )
        assert stored is False
        assert cache_service.get("How long is it?") is None

    def test_clarification_prompt_excluded_from_cache(self, cache_service):
        """20. Clarification requests for ambiguous queries are never cached."""
        stored = cache_service.set(
            query="What about the deadline?",
            answer="Could you please clarify whether you mean the quiz or capstone deadline?",
            sources=[],
            is_clarification=True,
        )
        assert stored is False
        assert cache_service.get("What about the deadline?") is None

    def test_unsupported_answer_excluded_from_cache(self, cache_service):
        """21. Unsupported topic responses (e.g. refunds) are not cached."""
        stored = cache_service.set(
            query="Can I get a refund?",
            answer="The course documents do not specify a refund policy.",
            sources=["Course Handbook"],
            is_supported=False,
        )
        assert stored is False
        assert cache_service.get("Can I get a refund?") is None

    def test_out_of_scope_answer_excluded_from_cache(self, cache_service):
        """22. Out-of-scope queries (e.g. sports scores) are not cached."""
        stored = cache_service.set(
            query="Who won the cricket match?",
            answer="I can only answer questions related to DPM-101.",
            sources=[],
            is_in_scope=False,
        )
        assert stored is False
        assert cache_service.get("Who won the cricket match?") is None

    def test_extractive_fallback_excluded_from_cache(self, cache_service):
        """23. Extractive fallback answers generated during Gemini outages are not cached."""
        stored = cache_service.set(
            query="What is the passing score?",
            answer="Passing requires 60% overall.",
            sources=["Grading Policy V2"],
            is_fallback=True,
        )
        assert stored is False
        assert cache_service.get("What is the passing score?") is None

    def test_empty_or_invalid_answer_excluded_from_cache(self, cache_service):
        """24. Empty answers, empty sources, or errors are rejected."""
        assert cache_service.set("Q", "", sources=["Doc01"]) is False
        assert cache_service.set("Q", "Answer", sources=[]) is False
        assert cache_service.set("Q", "Answer", sources=["Doc01"], error="Timeout") is False

    def test_unmoderated_forum_source_evicted_from_cache(self, cache_service, temp_cache_db):
        """25. Cached entries referencing unmoderated forum sources are evicted on lookup."""
        # Manually inject an adversarial entry referencing forbidden forum content
        conn = get_db_connection(temp_cache_db)
        key = cache_service.generate_cache_key("Is the exam open book?")
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            future_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            conn.execute(
                """
                INSERT INTO query_cache (
                    id, cache_key, normalized_query, answer, sources, created_at,
                    expires_at, last_accessed_at, access_count, confidence, is_in_scope, is_supported
                ) VALUES ('bad-id', ?, 'is the exam open book', 'Yes open book', ?, ?, ?, ?, 1, 1.0, 1, 1);
                """,
                (key, json.dumps(["Doc04 Unmoderated Student Forum"]), now_iso, future_iso, now_iso),
            )
        finally:
            conn.close()

        # Lookup must detect invalid forum source, evict it, and return None
        result = cache_service.get("Is the exam open book?")
        assert result is None

        # Verify entry was evicted from DB
        conn = get_db_connection(temp_cache_db)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM query_cache WHERE cache_key = ?;", (key,))
            assert cursor.fetchone()[0] == 0
        finally:
            conn.close()


# ── Category 6: Fault Tolerance, Concurrency & Security ───────────────────────

class TestFaultToleranceAndConcurrency:

    def test_cache_database_failure_degrades_gracefully(self, temp_cache_db, monkeypatch):
        """26. Database disconnection or file corruption returns None instead of raising."""
        service = CacheService(db_path=temp_cache_db, enabled=True)

        def raise_db_error():
            raise sqlite3.OperationalError("Simulated disk I/O error or DB corruption")

        monkeypatch.setattr(service, "_get_connection", raise_db_error)

        # Must degrade gracefully without raising exception
        assert service.get("What is the passing score?") is None
        assert service.set("What is the passing score?", "60%", sources=["Grading Policy V2"]) is False
        assert service.invalidate("What is the passing score?") is False
        assert service.clear() == 0
        assert service.cleanup_expired() == 0

    def test_sql_injection_defense_in_query(self, cache_service):
        """27. SQL injection payloads in queries are safely parameterized and hashed."""
        malicious_query = "What is passing score?'; DROP TABLE query_cache; --"
        cache_service.set(malicious_query, "Safe answer 60%", sources=["Grading Policy V2"])

        cached = cache_service.get(malicious_query)
        assert cached is not None
        assert cached.answer == "Safe answer 60%"

        # Verify query_cache table still exists and is healthy
        stats = cache_service.get_stats()
        assert stats["total_entries"] >= 1

    def test_concurrent_cache_reads_and_writes(self, cache_service):
        """28. 8 concurrent threads simultaneously reading and writing do not deadlock or crash."""
        errors = []

        def worker(thread_idx: int):
            try:
                for i in range(10):
                    q = f"Query from thread {thread_idx} turn {i}"
                    a = f"Answer from thread {thread_idx} turn {i}"
                    cache_service.set(q, a, sources=["Course Handbook"])
                    hit = cache_service.get(q)
                    if hit is None or hit.answer != a:
                        errors.append(f"Mismatch in thread {thread_idx} turn {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        stats = cache_service.get_stats()
        # 8 threads * 10 entries = 80 entries written
        assert stats["total_entries"] == 80


# ── Category 7: Pipeline Integration (PolicyService & ChatService) ────────────

class TestPipelineIntegration:

    def test_first_question_misses_then_second_hits_skipping_gemini(self, temp_cache_db):
        """
        29. End-to-end integration:
            1st standalone ask -> Gemini API is called -> answer stored in cache.
            2nd identical ask  -> Cache HIT -> Gemini API is NOT called!
        """
        cache = CacheService(db_path=temp_cache_db, enabled=True)
        mock_gemini = MagicMock(spec=GeminiService)
        mock_gemini.generate_text.return_value = GeminiResponse(
            success=True,
            text="The Verified Certificate fee is Rs. 1,500. Source: Course Handbook & Overview.",
            model="gemini-3.8-flash",
        )

        policy = PolicyService(gemini_service=mock_gemini, cache_service=cache)

        # First ask: Cache miss
        ans1 = policy.ask("What is the Verified Certificate fee?")
        assert ans1.is_supported is True
        assert "1,500" in ans1.answer
        assert mock_gemini.generate_text.call_count == 1
        assert "cached" not in (ans1.model_used or "").lower()

        # Second ask: Cache hit -> Gemini must NOT be called again!
        ans2 = policy.ask("What is the Verified Certificate fee?")
        assert ans2.is_supported is True
        assert "1,500" in ans2.answer
        assert mock_gemini.generate_text.call_count == 1  # Still 1! Gemini skipped!
        assert "cached" in (ans2.model_used or "").lower()

    def test_context_dependent_follow_up_does_not_pollute_cache(self, temp_cache_db):
        """
        30. Follow-up question resolving 'it' to final exam is evaluated dynamically,
            and does not cache the bare pronoun or pollute standalone questions.
        """
        cache = CacheService(db_path=temp_cache_db, enabled=True)
        mock_gemini = MagicMock(spec=GeminiService)
        mock_gemini.generate_text.return_value = GeminiResponse(
            success=True,
            text="The Proctored Final Exam is 90 minutes. Source: Final Exam Rules & Student Forum Excerpts.",
            model="gemini-3.8-flash",
        )

        policy = PolicyService(gemini_service=mock_gemini, cache_service=cache)
        chat = ChatService(db_path=temp_cache_db, policy_service=policy, cache_service=cache)

        conv = chat.create_conversation()
        conv_id = conv["id"]

        # Turn 1: Exam date
        chat.process_message(conv_id, "When is the final exam?")
        # Turn 2: Follow-up pronoun 'How long is it?' (context-dependent)
        resp2 = chat.process_message(conv_id, "How long is it?")
        assert "90 minutes" in resp2["content"]

        # Verify: 'How long is it?' was NOT stored as a standalone cached question!
        assert cache.get("How long is it?") is None

    def test_changed_doc_fingerprint_causes_cache_miss_and_fresh_eval(self, temp_cache_db):
        """
        31. When document fingerprint changes, previous cached answer is missed
            and fresh Gemini evaluation is triggered.
        """
        cache = CacheService(db_path=temp_cache_db, enabled=True)
        mock_gemini = MagicMock(spec=GeminiService)
        mock_gemini.generate_text.return_value = GeminiResponse(
            success=True,
            text="Initial answer with doc fingerprint 1. Source: Handbook.",
            model="gemini-3.8-flash",
        )

        policy = PolicyService(gemini_service=mock_gemini, cache_service=cache)

        # Store entry under fingerprint 1
        q = "What is the course duration?"
        cache.set(q, "Old answer under FP1", sources=["Handbook"], doc_fingerprint="fp_version_1")

        # Querying under fingerprint 2 must miss and call Gemini
        ans = policy.ask(q)
        assert mock_gemini.generate_text.call_count == 1
        assert "Initial answer" in ans.answer

    def test_32_max_entries_enforcement_evicts_lru(self, temp_cache_db):
        """
        32. Strict capacity bound: When max_entries is set (e.g. 3), storing 4 entries
            evicts the least recently accessed entry so table size never exceeds 3.
        """
        cache = CacheService(db_path=temp_cache_db, max_entries=3, enabled=True)

        cache.set("Q1", "Answer 1", sources=["Handbook"])
        time.sleep(0.01)
        cache.set("Q2", "Answer 2", sources=["Handbook"])
        time.sleep(0.01)
        cache.set("Q3", "Answer 3", sources=["Handbook"])
        time.sleep(0.01)

        # Access Q1 to make Q2 the least recently accessed
        assert cache.get("Q1") is not None
        time.sleep(0.01)

        # Insert 4th entry -> must trigger LRU eviction of Q2
        cache.set("Q4", "Answer 4", sources=["Handbook"])

        stats = cache.get_stats()
        assert stats["total_entries"] == 3
        assert cache.get("Q1") is not None  # Recently accessed
        assert cache.get("Q3") is not None  # Active
        assert cache.get("Q4") is not None  # Newly added
        assert cache.get("Q2") is None      # Evicted (LRU)

    def test_33_adversarial_superseded_doc02_as_sole_source_evicted(self, cache_service, temp_cache_db):
        """
        33. Any cached answer citing only superseded Doc02 is detected,
            evicted, and returns None on lookup.
        """
        key = cache_service.generate_cache_key("What is the passing score?")
        conn = get_db_connection(temp_cache_db)
        now_iso = datetime.now(timezone.utc).isoformat()
        future_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        try:
            conn.execute(
                """
                INSERT INTO query_cache (
                    id, cache_key, normalized_query, answer, sources, created_at,
                    expires_at, last_accessed_at, access_count, confidence, is_in_scope, is_supported
                ) VALUES ('doc02-bad', ?, 'what is the passing score', '50% passing', ?, ?, ?, ?, 1, 1.0, 1, 1);
                """,
                (key, json.dumps(["Grading & Late Submission Policy V1 (Historical)"]), now_iso, future_iso, now_iso),
            )
        finally:
            conn.close()

        # Must reject and evict superseded Doc02 entry
        assert cache_service.get("What is the passing score?") is None

    def test_34_adversarial_4999_fee_hallucination_rejected(self, cache_service):
        """
        34. Attempting to cache an answer with hallucinated fee ₹4,999 is rejected.
        """
        stored = cache_service.set(
            query="What is the Verified Certificate fee?",
            answer="The fee for the Verified Certificate Track is ₹4,999.",
            sources=["Course Handbook & Overview"],
        )
        assert stored is False
        assert cache_service.get("What is the Verified Certificate fee?") is None

    def test_35_cached_doc_fingerprint_auto_invalidates_on_file_mtime_change(self, tmp_path):
        """
        35. File mtime changes trigger instant automatic recalculation of doc fingerprint.
        """
        doc_file = tmp_path / "01_course_handbook.txt"
        doc_file.write_text("Version 1 content", encoding="utf-8")

        mock_registry = {
            "doc01": {
                "doc_id": "doc01",
                "filename": "01_course_handbook.txt",
                "title": "Handbook",
                "last_updated": "2026-08-15",
                "policy_status": "current",
            }
        }

        db_path = str(tmp_path / "cache_mtime.db")
        service = CacheService(db_path=db_path, documents_dir=str(tmp_path), doc_registry=mock_registry, enabled=True)

        fp1 = service.compute_documents_fingerprint()
        # Immediate second call reuses cached fingerprint
        assert service.compute_documents_fingerprint() == fp1

        # Touch/modify file
        time.sleep(0.02)
        doc_file.write_text("Version 2 updated content", encoding="utf-8")

        fp2 = service.compute_documents_fingerprint()
        assert fp1 != fp2

    def test_36_cache_stats_and_cleanup_endpoints(self, temp_cache_db):
        """
        36. Flask API endpoints GET /api/v1/cache/stats and POST /api/v1/cache/cleanup work cleanly.
        """
        from app import create_app
        cache = CacheService(db_path=temp_cache_db, enabled=True)
        chat = ChatService(db_path=temp_cache_db, cache_service=cache)
        app = create_app({"TESTING": True, "chat_service": chat})

        with app.test_client() as client:
            # 1. GET /api/v1/cache/stats
            stats_resp = client.get("/api/v1/cache/stats")
            assert stats_resp.status_code == 200
            data = stats_resp.get_json()
            assert data["enabled"] is True
            assert "total_entries" in data

            # 2. Add expired entry and call POST /api/v1/cache/cleanup
            cache.set("Expired Q", "Ans", sources=["Handbook"], ttl=-10)
            clean_resp = client.post("/api/v1/cache/cleanup")
            assert clean_resp.status_code == 200
            assert clean_resp.get_json()["cleaned_entries"] == 1

