"""
tests/test_stats_service.py — Module 9: Statistics & Live Counters Test Suite

Tests:
  1. Counter initialization and correct default values
  2. Individual atomic increments for requests, hits, Gemini calls, and errors
  3. Reset functionality for test isolation
  4. Uptime calculation across simulated intervals
  5. Thread safety and race-condition prevention under 20 concurrent threads
  6. Concurrent snapshot reads while increments are in-flight
  7. API endpoint GET /api/stats schema compliance (exact 6 fields, no internal state)
  8. API endpoint HTTP method restrictions (405 for POST, PUT, DELETE, PATCH)
  9. Zero Gemini API calls triggered during stats endpoint requests
 10. Graceful degradation (HTTP 503) on unexpected operational failure
 11. Pipeline instrumentation: valid chat message increments total_requests
 12. Pipeline instrumentation: cache hit increments cache_hits without calling Gemini
 13. Pipeline instrumentation: duplicate request increments total_requests without calling Gemini or cache
 14. Pipeline instrumentation: rate-limited request increments total_requests without calling Gemini or cache
 15. Pipeline instrumentation: Gemini failure increments errors exactly once
 16. Empirical latency benchmark measuring p50 and p95 across 100 iterations
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from app import create_app
from backend.services.stats_service import StatsService, get_stats_service, set_stats_service, WORKER_NOTE
from backend.services.chat_service import ChatService
from backend.services.policy_service import PolicyService, PolicyAnswer
from backend.services.gemini_service import GeminiResponse


@pytest.fixture(autouse=True)
def clean_stats():
    """Ensure a clean, isolated StatsService instance for each test."""
    service = StatsService()
    set_stats_service(service)
    yield service
    set_stats_service(None)


@pytest.fixture
def test_app(clean_stats, tmp_path):
    """Create Flask application configured for isolated testing."""
    db_file = str(tmp_path / "test_stats.db")
    chat_svc = ChatService(db_path=db_file)
    app = create_app({
        "TESTING": True,
        "DB_PATH": db_file,
        "chat_service": chat_svc,
        "stats_service": clean_stats,
    })
    return app


@pytest.fixture
def client(test_app):
    """Flask test client."""
    return test_app.test_client()


# ── 1. Unit Tests for StatsService ───────────────────────────────────────────

class TestStatsServiceUnit:
    def test_initialization(self, clean_stats):
        """Counters must initialize to 0 and uptime must be non-negative."""
        data = clean_stats.get_stats()
        assert data["total_requests"] == 0
        assert data["cache_hits"] == 0
        assert data["gemini_calls"] == 0
        assert data["errors"] == 0
        assert data["uptime_seconds"] >= 0
        assert data["worker_note"] == WORKER_NOTE
        assert len(data) == 6

    def test_counter_increments(self, clean_stats):
        """Each increment method must increase the corresponding counter."""
        clean_stats.increment_requests(3)
        clean_stats.increment_cache_hits(2)
        clean_stats.increment_gemini_calls(5)
        clean_stats.increment_errors(1)

        data = clean_stats.get_stats()
        assert data["total_requests"] == 3
        assert data["cache_hits"] == 2
        assert data["gemini_calls"] == 5
        assert data["errors"] == 1

    def test_reset_counters(self, clean_stats):
        """Reset must restore all counters to 0 without altering start time."""
        start_time = clean_stats.start_time
        clean_stats.increment_requests(10)
        clean_stats.increment_cache_hits(5)
        clean_stats.increment_gemini_calls(5)
        clean_stats.increment_errors(2)

        clean_stats.reset()
        data = clean_stats.get_stats()
        assert data["total_requests"] == 0
        assert data["cache_hits"] == 0
        assert data["gemini_calls"] == 0
        assert data["errors"] == 0
        assert clean_stats.start_time == start_time

    def test_uptime_calculation(self):
        """Uptime must accurately reflect elapsed seconds from start_time."""
        fake_start = time.time() - 3600  # 1 hour ago
        svc = StatsService(start_time=fake_start)
        data = svc.get_stats()
        assert data["uptime_seconds"] >= 3600
        assert data["uptime_seconds"] <= 3605


# ── 2. Concurrency & Thread-Safety Tests ──────────────────────────────────────

class TestStatsServiceConcurrency:
    def test_concurrent_counter_increments(self, clean_stats):
        """
        20 concurrent threads each performing 100 increments across all counters.
        Must result in exactly 2,000 for each counter with zero lost updates.
        """
        num_threads = 20
        increments_per_thread = 100

        def worker():
            for _ in range(increments_per_thread):
                clean_stats.increment_requests()
                clean_stats.increment_cache_hits()
                clean_stats.increment_gemini_calls()
                clean_stats.increment_errors()

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = clean_stats.get_stats()
        expected = num_threads * increments_per_thread
        assert data["total_requests"] == expected
        assert data["cache_hits"] == expected
        assert data["gemini_calls"] == expected
        assert data["errors"] == expected

    def test_concurrent_reads_during_writes(self, clean_stats):
        """
        Simultaneous read and write threads must execute without deadlock
        or inconsistent state exceptions.
        """
        stop_event = threading.Event()
        read_results = []

        def writer():
            while not stop_event.is_set():
                clean_stats.increment_requests()
                clean_stats.increment_cache_hits()
                time.sleep(0.001)

        def reader():
            while not stop_event.is_set():
                stats = clean_stats.get_stats()
                read_results.append(stats["total_requests"])
                time.sleep(0.001)

        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)

        writer_thread.start()
        reader_thread.start()
        time.sleep(0.1)
        stop_event.set()

        writer_thread.join()
        reader_thread.join()

        assert len(read_results) > 0
        assert all(isinstance(r, int) for r in read_results)


# ── 3. API Route Tests: GET /api/stats ────────────────────────────────────────

class TestStatsEndpoint:
    def test_get_stats_schema_and_status(self, client, clean_stats):
        """GET /api/stats must return HTTP 200 with the exact 6 required fields."""
        clean_stats.increment_requests(10)
        clean_stats.increment_cache_hits(6)
        clean_stats.increment_gemini_calls(4)
        clean_stats.increment_errors(1)

        resp = client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.is_json

        data = resp.get_json()
        assert set(data.keys()) == {
            "total_requests",
            "cache_hits",
            "gemini_calls",
            "errors",
            "uptime_seconds",
            "worker_note",
        }
        assert data["total_requests"] == 10
        assert data["cache_hits"] == 6
        assert data["gemini_calls"] == 4
        assert data["errors"] == 1
        assert isinstance(data["uptime_seconds"], int)
        assert data["worker_note"] == WORKER_NOTE

    def test_http_method_restrictions(self, client):
        """Endpoints must reject POST, PUT, DELETE, PATCH with 405 Method Not Allowed."""
        assert client.post("/api/stats").status_code == 405
        assert client.put("/api/stats").status_code == 405
        assert client.delete("/api/stats").status_code == 405
        assert client.patch("/api/stats").status_code == 405

    def test_stats_does_not_call_gemini(self, client):
        """Querying /api/stats must NEVER invoke Gemini API or consume quota."""
        with patch("backend.services.gemini_service.GeminiService.generate_text") as mock_gemini:
            resp = client.get("/api/stats")
            assert resp.status_code == 200
            mock_gemini.assert_not_called()

    def test_stats_graceful_failure(self, client, test_app):
        """If StatsService raises an unhandled error, endpoint returns HTTP 503."""
        mock_service = MagicMock()
        mock_service.get_stats.side_effect = RuntimeError("Database locked or memory corrupted")
        test_app.extensions["stats_service"] = mock_service

        resp = client.get("/api/stats")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["code"] == "STATS_UNAVAILABLE"


# ── 4. Pipeline Instrumentation & Metric Accuracy Tests ───────────────────────

class TestPipelineInstrumentation:
    def test_chat_message_increments_total_requests(self, client, clean_stats):
        """Valid chat message must increment total_requests."""
        # 1. Create conversation
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        initial_requests = clean_stats.get_stats()["total_requests"]

        # 2. Send message
        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the final exam date?"},
        )

        final_requests = clean_stats.get_stats()["total_requests"]
        assert final_requests == initial_requests + 1

    def test_cache_hit_increments_cache_hits_only(self, clean_stats):
        """A cache hit must increment cache_hits without incrementing gemini_calls."""
        mock_cache = MagicMock()
        mock_cached_answer = MagicMock()
        mock_cached_answer.answer = "The final exam is Nov 20, 2026."
        mock_cached_answer.sources = ["Doc 04"]
        mock_cached_answer.confidence = 1.0
        mock_cached_answer.model_used = "gemini-3.8-flash"
        mock_cached_answer.prompt_tokens = 10
        mock_cached_answer.candidates_tokens = 5
        mock_cached_answer.total_tokens = 15
        mock_cache.is_enabled.return_value = True
        mock_cache.get.return_value = mock_cached_answer

        policy_svc = PolicyService(cache_service=mock_cache)
        ans = policy_svc.ask("When is the final exam?")

        stats = clean_stats.get_stats()
        assert stats["cache_hits"] == 1
        assert stats["gemini_calls"] == 0
        assert ans.answer == "The final exam is Nov 20, 2026."

    def test_gemini_call_increments_gemini_calls_only(self, clean_stats):
        """A cache miss that invokes Gemini must increment gemini_calls and not cache_hits."""
        from backend.services.gemini_service import GeminiService
        mock_cache = MagicMock()
        mock_cache.is_enabled.return_value = True
        mock_cache.get.return_value = None  # Cache miss

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "To earn the Verified Certificate, learners must achieve an overall weighted score of at least 60% and 40% on the final exam. Source: Grading Policy V2."
        mock_resp.candidates = [MagicMock()]
        mock_client.models.generate_content.return_value = mock_resp

        real_gemini = GeminiService(client=mock_client)
        policy_svc = PolicyService(gemini_service=real_gemini, cache_service=mock_cache)
        policy_svc.ask("What is the passing grade?")

        stats = clean_stats.get_stats()
        assert stats["gemini_calls"] == 1
        assert stats["cache_hits"] == 0

    def test_gemini_failure_increments_errors_exactly_once(self, clean_stats):
        """When Gemini fails, errors is incremented exactly once."""
        from backend.services.gemini_service import GeminiService
        mock_cache = MagicMock()
        mock_cache.is_enabled.return_value = True
        mock_cache.get.return_value = None

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("API key invalid")

        real_gemini = GeminiService(client=mock_client, max_retries=0)
        policy_svc = PolicyService(gemini_service=real_gemini, cache_service=mock_cache)
        ans = policy_svc.ask("What is the passing grade?")

        stats = clean_stats.get_stats()
        assert stats["gemini_calls"] == 1
        assert stats["errors"] == 1
        # Fallback answer should still be returned
        assert ans.answer is not None

    def test_gemini_retries_increment_network_attempts(self, clean_stats):
        """
        When Gemini encounters a transient error and retries,
        gemini_calls increments for each actual network attempt.
        """
        from backend.services.gemini_service import GeminiService
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "Success on retry"
        mock_resp.candidates = [MagicMock()]

        # Fail with transient 429 once, then succeed on attempt 2
        mock_client.models.generate_content.side_effect = [
            Exception("429 Resource Exhausted rate limit"),
            mock_resp,
        ]

        with patch("time.sleep", return_value=None):
            real_gemini = GeminiService(client=mock_client, max_retries=2)
            resp = real_gemini.generate_text("test prompt")

        assert resp.success is True
        stats = clean_stats.get_stats()
        # 1 initial attempt + 1 retry = 2 total network calls
        assert stats["gemini_calls"] == 2
        assert stats["errors"] == 0

    def test_gemini_exhausted_retries_increments_errors_once(self, clean_stats):
        """
        When Gemini encounters transient errors and exhausts all retries,
        gemini_calls tracks each attempt and errors is incremented exactly once.
        """
        from backend.services.gemini_service import GeminiService
        mock_client = MagicMock()
        # Always fail with transient 503
        mock_client.models.generate_content.side_effect = Exception("503 Service Unavailable")

        with patch("time.sleep", return_value=None):
            real_gemini = GeminiService(client=mock_client, max_retries=2)
            policy_svc = PolicyService(gemini_service=real_gemini)
            ans = policy_svc.ask("What is the passing grade?")

        stats = clean_stats.get_stats()
        # 1 initial attempt + 2 retries = 3 actual network attempts
        assert stats["gemini_calls"] == 3
        # Failed generation request increments errors exactly once
        assert stats["errors"] == 1
        assert ans.answer is not None

    def test_invalid_requests_do_not_increment_total_requests(self, client, clean_stats):
        """
        Malformed JSON (400) and nonexistent conversation (404) requests
        must NOT increment total_requests.
        """
        initial_requests = clean_stats.get_stats()["total_requests"]

        # 1. Invalid JSON body
        r1 = client.post(
            "/api/v1/conversations/non-existent-uuid/messages",
            data="not a json",
            content_type="text/plain",
        )
        assert r1.status_code == 400

        # 2. Missing message field
        r2 = client.post(
            "/api/v1/conversations/non-existent-uuid/messages",
            json={"wrong_field": "hello"},
        )
        assert r2.status_code == 400

        # 3. Nonexistent conversation ID
        r3 = client.post(
            "/api/v1/conversations/00000000-0000-0000-0000-000000000000/messages",
            json={"message": "hello"},
        )
        assert r3.status_code == 404

        stats = clean_stats.get_stats()
        assert stats["total_requests"] == initial_requests

    def test_duplicate_request_increments_requests_not_gemini(self, client, clean_stats):
        """Duplicate request replay must increment total_requests but not gemini_calls or cache_hits."""
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        # First request
        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the certificate fee?"},
            headers={"X-Request-ID": "dup-test-uuid-1"},
        )
        stats1 = clean_stats.get_stats()
        reqs1 = stats1["total_requests"]
        gemini1 = stats1["gemini_calls"]
        hits1 = stats1["cache_hits"]

        # Duplicate request with same idempotency key
        dup_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the certificate fee?"},
            headers={"X-Request-ID": "dup-test-uuid-1"},
        )
        assert dup_resp.status_code == 200
        assert dup_resp.headers.get("X-Duplicate-Request") == "true"

        stats2 = clean_stats.get_stats()
        assert stats2["total_requests"] == reqs1 + 1
        assert stats2["gemini_calls"] == gemini1  # Gemini NOT called on duplicate
        assert stats2["cache_hits"] == hits1      # Cache NOT called on duplicate

    def test_rate_limited_request_increments_requests_not_gemini(self, client, clean_stats):
        """Rate limited request (429) increments total_requests but not gemini_calls."""
        from backend.services.rate_limiter import SlidingWindowRateLimiter
        strict_limiter = SlidingWindowRateLimiter(default_ip_limit=1, default_ip_window=60)
        client.application.extensions["rate_limiter"] = strict_limiter

        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        # Request 1: allowed
        r1 = client.post(f"/api/v1/conversations/{conv_id}/messages", json={"message": "Hello 1"})
        assert r1.status_code == 200

        stats1 = clean_stats.get_stats()
        gemini1 = stats1["gemini_calls"]

        # Request 2: rate limited
        r2 = client.post(f"/api/v1/conversations/{conv_id}/messages", json={"message": "Hello 2"})
        assert r2.status_code == 429

        stats2 = clean_stats.get_stats()
        assert stats2["total_requests"] == stats1["total_requests"] + 1
        assert stats2["gemini_calls"] == gemini1  # Gemini NOT called on rate limited request


# ── 5. Empirical Latency Benchmark ────────────────────────────────────────────

class TestStatsLatencyBenchmark:
    def test_stats_endpoint_latency(self, client):
        """
        Empirically measure latency of 100 sequential GET /api/stats requests.
        Calculates min, max, avg, p50, and p95 latency.
        """
        latencies_ms = []
        for _ in range(100):
            t0 = time.perf_counter()
            resp = client.get("/api/stats")
            t1 = time.perf_counter()
            assert resp.status_code == 200
            latencies_ms.append((t1 - t0) * 1000.0)

        latencies_sorted = sorted(latencies_ms)
        min_lat = min(latencies_sorted)
        max_lat = max(latencies_sorted)
        avg_lat = sum(latencies_sorted) / len(latencies_sorted)
        p50_lat = latencies_sorted[49]
        p95_lat = latencies_sorted[94]

        # Log measured performance metrics
        print(f"\n[GET /api/stats Benchmark (100 reqs)] Min: {min_lat:.2f}ms | Avg: {avg_lat:.2f}ms | p50: {p50_lat:.2f}ms | p95: {p95_lat:.2f}ms | Max: {max_lat:.2f}ms")

        # Sanity assertion for in-memory read inside test harness (< 50ms)
        assert p95_lat < 50.0, f"p95 latency was higher than expected: {p95_lat:.2f}ms"
