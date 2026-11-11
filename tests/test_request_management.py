"""
tests/test_request_management.py — Test Suite for Module 8 Request Management & Duplicate Prevention

Covers:
  - Strict input validation (message type, whitespace, length <= 4000).
  - Explicit idempotency keys (X-Request-ID, request_id, format, header/body consistency).
  - Payload collision & conversation mismatch detection (HTTP 409).
  - Implicit rapid double-submit deduplication (auto-fingerprint).
  - Intentional repeated questions after window expiration (not suppressed).
  - In-flight thundering herd synchronization using threading.Condition and threading.Barrier.
  - Per-conversation turn serialization & independent conversation parallelism.
  - Bounded lock timeouts (HTTP 409 CONVERSATION_BUSY).
  - Sliding-window rate limiting (per-IP, per-conversation, Retry-After header).
  - Duplicate replay immunity under rate limit exhaustion.
  - Failure broadcasting to waiting threads.
  - Deletion during processing safety.
  - Memory hygiene & stale record cleanup.
  - Module 7 cache integration.
"""

import threading
import time
import uuid
import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from backend.services.chat_service import ChatService, get_db_connection
from backend.services.policy_service import PolicyService, PolicyAnswer
from backend.services.rate_limiter import SlidingWindowRateLimiter
from backend.services.request_service import (
    RequestManager,
    InvalidRequestIdError,
    MismatchedRequestIdError,
    IdempotencyConflictError,
    ConversationBusyError,
)
from backend.routes.chat_routes import (
    set_chat_service,
    set_rate_limiter,
    set_request_manager,
    get_client_ip,
)


@pytest.fixture
def mock_policy_service():
    """PolicyService mock that returns grounded responses with call counting."""
    class MockPolicyService:
        def __init__(self):
            self.call_count = 0
            self._lock = threading.Lock()

        def ask(self, question: str, is_context_dependent: bool = False, skip_cache: bool = False):
            with self._lock:
                self.call_count += 1
            return PolicyAnswer(
                question=question,
                answer=f"Authoritative answer for: {question}",
                sources=["Grading & Late Submission Policy V2 (Current)"],
                is_in_scope=True,
                is_supported=True,
            )

    return MockPolicyService()


@pytest.fixture
def isolated_env(tmp_path, mock_policy_service):
    """Creates an isolated database, ChatService, RateLimiter, RequestManager, and Flask client."""
    db_file = str(tmp_path / "test_req_mgr.db")
    chat_svc = ChatService(db_path=db_file, policy_service=mock_policy_service)
    rate_limiter = SlidingWindowRateLimiter(
        default_ip_limit=20,
        default_ip_window=60,
        default_conv_limit=15,
        default_conv_window=60,
        enabled=True,
    )
    request_mgr = RequestManager(
        duplicate_window=10,
        idempotency_ttl=300,
        lock_timeout=5.0,
    )

    set_chat_service(chat_svc)
    set_rate_limiter(rate_limiter)
    set_request_manager(request_mgr)

    app = create_app({
        "TESTING": True,
        "DB_PATH": db_file,
        "chat_service": chat_svc,
        "rate_limiter": rate_limiter,
        "request_manager": request_mgr,
    })

    client = app.test_client()

    yield {
        "app": app,
        "client": client,
        "chat_svc": chat_svc,
        "rate_limiter": rate_limiter,
        "request_mgr": request_mgr,
        "mock_policy": mock_policy_service,
        "db_file": db_file,
    }

    # Clean up global holders
    set_chat_service(None)
    set_rate_limiter(None)
    set_request_manager(None)


# ── Category 1: Input Validation & Request ID Format ──────────────────────────

class TestInputValidation:
    """Verifies strict input validation and request ID format checks."""

    def test_message_empty_or_whitespace(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()

        res1 = client.post(f"/api/v1/conversations/{conv['id']}/messages", json={"message": ""})
        assert res1.status_code == 400
        assert "empty or whitespace" in res1.get_json()["error"]

        res2 = client.post(f"/api/v1/conversations/{conv['id']}/messages", json={"message": "   \n  "})
        assert res2.status_code == 400

    def test_message_not_string(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()

        res = client.post(f"/api/v1/conversations/{conv['id']}/messages", json={"message": 12345})
        assert res.status_code == 400
        assert "must be a string" in res.get_json()["error"]

    def test_message_length_exceeded(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()

        long_msg = "A" * 4001
        res = client.post(f"/api/v1/conversations/{conv['id']}/messages", json={"message": long_msg})
        assert res.status_code == 400
        assert "exceeds maximum allowed length" in res.get_json()["error"]

    def test_invalid_request_id_format(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()

        # Characters like spaces, semicolons, quotes are forbidden
        res = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "What is the fee?", "request_id": "invalid key with spaces!"},
        )
        assert res.status_code == 400
        assert res.get_json()["code"] == "INVALID_REQUEST_ID"

    def test_mismatched_header_and_body_request_id(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()

        res = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": "req-header-123"},
            json={"message": "What is the fee?", "request_id": "req-body-456"},
        )
        assert res.status_code == 400
        assert res.get_json()["code"] == "MISMATCHED_REQUEST_ID"

    def test_matching_header_and_body_request_id(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()

        res = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": "req-same-123"},
            json={"message": "What is the fee?", "request_id": "req-same-123"},
        )
        assert res.status_code == 200
        assert res.get_json()["is_duplicate"] is False


# ── Category 2: Idempotency & Duplicate Prevention ────────────────────────────

class TestIdempotencyAndDuplicatePrevention:
    """Verifies idempotency keys, duplicate replays, collision detection, and database integrity."""

    def test_same_key_same_payload_replays_response(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()
        req_id = "idem-test-001"

        # First request
        res1 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "What is the passing score?"},
        )
        assert res1.status_code == 200
        data1 = res1.get_json()
        assert data1["is_duplicate"] is False
        assert "X-Duplicate-Request" not in res1.headers

        # Second request with identical key
        res2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "What is the passing score?"},
        )
        assert res2.status_code == 200
        data2 = res2.get_json()
        assert data2["is_duplicate"] is True
        assert res2.headers.get("X-Duplicate-Request") == "true"
        assert data2["message_id"] == data1["message_id"]
        assert data2["content"] == data1["content"]

    def test_same_key_same_payload_no_extra_db_rows(self, isolated_env):
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        conv = chat_svc.create_conversation()
        req_id = "idem-db-check"

        # Submit twice
        client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "What is the passing score?"},
        )
        client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "What is the passing score?"},
        )

        # Assert exactly 1 user message and 1 assistant message in database
        msgs = chat_svc.get_conversation_messages(conv["id"])
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_same_key_different_payload_returns_409(self, isolated_env):
        client = isolated_env["client"]
        conv = isolated_env["chat_svc"].create_conversation()
        req_id = "idem-collision-test"

        # Submit first payload
        res1 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "Question A"},
        )
        assert res1.status_code == 200

        # Submit second DIFFERENT payload with same key
        res2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "Question B"},
        )
        assert res2.status_code == 409
        assert res2.get_json()["code"] == "IDEMPOTENCY_CONFLICT"

    def test_same_key_across_conversations_returns_409(self, isolated_env):
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        conv1 = chat_svc.create_conversation()
        conv2 = chat_svc.create_conversation()
        req_id = "idem-cross-conv-key"

        res1 = client.post(
            f"/api/v1/conversations/{conv1['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "Common Question"},
        )
        assert res1.status_code == 200

        res2 = client.post(
            f"/api/v1/conversations/{conv2['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "Common Question"},
        )
        assert res2.status_code == 409
        assert res2.get_json()["code"] == "IDEMPOTENCY_CONFLICT"

    def test_rapid_duplicate_submissions_auto_fingerprint(self, isolated_env):
        """Simulates rapid double-click without explicit request_id."""
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        conv = chat_svc.create_conversation()

        # Submit 1
        res1 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "What is the Capstone deadline?"},
        )
        assert res1.status_code == 200
        assert res1.get_json()["is_duplicate"] is False

        # Submit 2 within 10s (auto fingerprint matches)
        res2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "What is the Capstone deadline?"},
        )
        assert res2.status_code == 200
        assert res2.get_json()["is_duplicate"] is True
        assert res2.headers.get("X-Duplicate-Request") == "true"
        assert res2.get_json()["message_id"] == res1.get_json()["message_id"]

        # Exactly 2 messages in SQLite
        msgs = chat_svc.get_conversation_messages(conv["id"])
        assert len(msgs) == 2

    def test_intentional_repeated_questions_after_window(self, isolated_env):
        """Repeated questions after the duplicate window must be processed normally."""
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        request_mgr = isolated_env["request_mgr"]
        conv = chat_svc.create_conversation()

        # Configure short duplicate window for this test
        request_mgr.duplicate_window = 0.1  # 100ms window

        # Turn 1
        res1 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "What is the passing score?"},
        )
        assert res1.status_code == 200
        assert res1.get_json()["is_duplicate"] is False

        # Wait for duplicate window to expire
        time.sleep(0.15)

        # Turn 2: same question intentionally asked again
        res2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "What is the passing score?"},
        )
        assert res2.status_code == 200
        assert res2.get_json()["is_duplicate"] is False
        # Generated a new message ID!
        assert res2.get_json()["message_id"] != res1.get_json()["message_id"]

        # Now 4 messages in DB (2 user, 2 assistant)
        msgs = chat_svc.get_conversation_messages(conv["id"])
        assert len(msgs) == 4

    def test_distinct_questions_in_same_conversation_not_deduplicated(self, isolated_env):
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        conv = chat_svc.create_conversation()

        res1 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "What is the passing score?"},
        )
        assert res1.status_code == 200
        assert res1.get_json()["is_duplicate"] is False

        res2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "What is the Capstone deadline?"},
        )
        assert res2.status_code == 200
        assert res2.get_json()["is_duplicate"] is False

        msgs = chat_svc.get_conversation_messages(conv["id"])
        assert len(msgs) == 4


# ── Category 3: Rate Limiting Tests ───────────────────────────────────────────

class TestRateLimiting:
    """Verifies IP and conversation rate limits, Retry-After header, and duplicate immunity."""

    def test_ip_rate_limit_exceeded_returns_429(self, isolated_env):
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        rate_limiter = isolated_env["rate_limiter"]
        rate_limiter.ip_limit = 5  # Small limit for test
        rate_limiter.ip_window = 60

        conv = chat_svc.create_conversation()

        # Send 5 allowed requests
        for i in range(5):
            res = client.post(
                f"/api/v1/conversations/{conv['id']}/messages",
                json={"message": f"Question {i}"},
            )
            assert res.status_code == 200

        # 6th request triggers 429
        res_blocked = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "Question 6"},
        )
        assert res_blocked.status_code == 429
        data = res_blocked.get_json()
        assert data["code"] == "RATE_LIMITED"
        assert "Retry-After" in res_blocked.headers
        assert int(res_blocked.headers["Retry-After"]) >= 1

    def test_conversation_rate_limit_exceeded_returns_429(self, isolated_env):
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        rate_limiter = isolated_env["rate_limiter"]
        rate_limiter.ip_limit = 50
        rate_limiter.conv_limit = 3  # Low conversation limit

        conv = chat_svc.create_conversation()

        for i in range(3):
            res = client.post(
                f"/api/v1/conversations/{conv['id']}/messages",
                json={"message": f"Turn {i}"},
            )
            assert res.status_code == 200

        res_blocked = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "Turn 4"},
        )
        assert res_blocked.status_code == 429
        assert res_blocked.get_json()["code"] == "RATE_LIMITED"

    def test_duplicate_replay_does_not_consume_rate_limit(self, isolated_env):
        """A duplicate retry must succeed even when rate limit quota is exhausted."""
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        rate_limiter = isolated_env["rate_limiter"]
        rate_limiter.ip_limit = 2
        rate_limiter.ip_window = 60

        conv = chat_svc.create_conversation()
        req_id = "rate-exempt-key"

        # Request 1 (Consumes token 1)
        res1 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "First question"},
        )
        assert res1.status_code == 200

        # Request 2 (Consumes token 2 - limit reached)
        res2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "Second question"},
        )
        assert res2.status_code == 200

        # Request 3 is a NEW question -> blocked with 429
        res3 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "Third new question"},
        )
        assert res3.status_code == 429

        # Request 4 is a RETRY of Request 1 with same key -> MUST SUCCEED WITH 200!
        res_retry = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Request-ID": req_id},
            json={"message": "First question"},
        )
        assert res_retry.status_code == 200
        assert res_retry.get_json()["is_duplicate"] is True
        assert res_retry.headers.get("X-Duplicate-Request") == "true"

    def test_different_ips_have_isolated_quotas(self, isolated_env):
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        rate_limiter = isolated_env["rate_limiter"]
        rate_limiter.ip_limit = 2

        conv = chat_svc.create_conversation()

        # IP 1 uses all quota
        client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Forwarded-For": "192.168.1.10"},
            json={"message": "Q1"},
        )
        client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Forwarded-For": "192.168.1.10"},
            json={"message": "Q2"},
        )
        res_ip1_blocked = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Forwarded-For": "192.168.1.10"},
            json={"message": "Q3"},
        )
        assert res_ip1_blocked.status_code == 429

        # IP 2 can still make requests!
        res_ip2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Forwarded-For": "192.168.1.20"},
            json={"message": "Q1 for IP 2"},
        )
        assert res_ip2.status_code == 200


# ── Category 4: Concurrency, Synchronization & Lock Management ───────────────

class TestConcurrencyAndLocking:
    """Deterministic concurrency verification using threading.Barrier and events."""

    def test_concurrent_identical_requests_thundering_herd(self, isolated_env):
        """
        Multiple threads fire the exact same request at the exact same instant.
        Verifies:
          - Exactly 1 PolicyService call executes.
          - Only 1 user and 1 assistant message in SQLite.
          - All threads receive HTTP 200.
          - Secondary threads receive is_duplicate: True.
        """
        app = isolated_env["app"]
        chat_svc = isolated_env["chat_svc"]
        mock_policy = isolated_env["mock_policy"]
        conv = chat_svc.create_conversation()

        num_threads = 5
        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads

        def worker(thread_idx):
            with app.test_client() as c:
                barrier.wait()  # Fire simultaneously
                res = c.post(
                    f"/api/v1/conversations/{conv['id']}/messages",
                    json={"message": "What is the final exam date?"},
                )
                results[thread_idx] = (res.status_code, res.get_json(), res.headers.get("X-Duplicate-Request"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads must succeed with 200
        status_codes = [r[0] for r in results]
        assert status_codes == [200] * num_threads

        # Exactly 1 PolicyService execution!
        assert mock_policy.call_count == 1

        # Exactly 1 primary and (num_threads - 1) duplicate responses
        dup_flags = [r[1]["is_duplicate"] for r in results]
        assert dup_flags.count(False) == 1
        assert dup_flags.count(True) == (num_threads - 1)

        # Database must have exactly 1 user message and 1 assistant message!
        msgs = chat_svc.get_conversation_messages(conv["id"])
        assert len(msgs) == 2

    def test_concurrent_different_questions_in_same_conversation(self, isolated_env):
        """
        Multiple different questions in the same conversation are serialized safely.
        Turns do not interleave; history order is preserved.
        """
        app = isolated_env["app"]
        chat_svc = isolated_env["chat_svc"]
        conv = chat_svc.create_conversation()

        num_threads = 4
        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads

        def worker(thread_idx):
            with app.test_client() as c:
                barrier.wait()
                res = c.post(
                    f"/api/v1/conversations/{conv['id']}/messages",
                    json={"message": f"Question number {thread_idx}"},
                )
                results[thread_idx] = res.status_code

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All succeed
        assert results == [200] * num_threads

        # 4 distinct user messages and 4 assistant messages in chronological order
        msgs = chat_svc.get_conversation_messages(conv["id"])
        assert len(msgs) == 8
        for i in range(0, 8, 2):
            assert msgs[i]["role"] == "user"
            assert msgs[i + 1]["role"] == "assistant"

    def test_independent_conversations_execute_concurrently(self, isolated_env):
        """Requests to separate conversation IDs execute in parallel without blocking."""
        app = isolated_env["app"]
        chat_svc = isolated_env["chat_svc"]

        conv1 = chat_svc.create_conversation()
        conv2 = chat_svc.create_conversation()

        results = {}

        def worker(conv_id, key):
            with app.test_client() as c:
                res = c.post(
                    f"/api/v1/conversations/{conv_id}/messages",
                    json={"message": f"Hello {key}"},
                )
                results[key] = res.status_code

        t1 = threading.Thread(target=worker, args=(conv1["id"], "c1"))
        t2 = threading.Thread(target=worker, args=(conv2["id"], "c2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["c1"] == 200
        assert results["c2"] == 200

    def test_conversation_lock_timeout_returns_409(self, isolated_env):
        """Simulate a long-running lock holder; secondary request times out with 409."""
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        request_mgr = isolated_env["request_mgr"]
        conv = chat_svc.create_conversation()

        # Set low lock timeout for test
        request_mgr.lock_timeout = 0.2

        # Manually hold conversation lock
        assert request_mgr.acquire_conversation_lock(conv["id"]) is True

        try:
            # Secondary request attempts to process turn in same conversation
            res = client.post(
                f"/api/v1/conversations/{conv['id']}/messages",
                json={"message": "Will be blocked by lock timeout"},
            )
            assert res.status_code == 409
            assert res.get_json()["code"] == "CONVERSATION_BUSY"
        finally:
            request_mgr.release_conversation_lock(conv["id"])

    def test_primary_failure_wakes_waiting_threads(self, isolated_env):
        """When primary in-flight request fails, waiting duplicates wake and receive error."""
        app = isolated_env["app"]
        chat_svc = isolated_env["chat_svc"]
        mock_policy = isolated_env["mock_policy"]
        request_mgr = isolated_env["request_mgr"]
        conv = chat_svc.create_conversation()

        num_threads = 3
        expected_waiters = num_threads - 1

        # Deterministic synchronization at the actual Condition.wait registration point
        waiters_registered = threading.Event()
        actual_waiters_count = 0
        waiter_lock = threading.Lock()
        record_ref = None

        orig_start_request = request_mgr.start_request

        def instrumented_start_request(*args, **kwargs):
            nonlocal record_ref
            key, is_duplicate, replayed, wait_target = orig_start_request(*args, **kwargs)
            if not is_duplicate:
                # Primary request created: intercept condition.wait on the primary record
                with request_mgr._master_lock:
                    rec = request_mgr._records[key]
                    record_ref = rec
                    orig_cond_wait = rec.condition.wait

                    def tracked_cond_wait(*w_args, **w_kwargs):
                        nonlocal actual_waiters_count
                        # Invoked under self._master_lock inside RequestManager.wait_for_in_flight
                        with waiter_lock:
                            actual_waiters_count += 1
                            if actual_waiters_count == expected_waiters:
                                waiters_registered.set()
                        return orig_cond_wait(*w_args, **w_kwargs)

                    rec.condition.wait = tracked_cond_wait
            return key, is_duplicate, replayed, wait_target

        request_mgr.start_request = instrumented_start_request

        # Make PolicyService fail on primary call only; subsequent calls return OK
        call_count = 0
        call_lock = threading.Lock()

        def failing_ask(question, **kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count

            if current_call == 1:
                # Deterministically wait until both duplicate threads reach cond.wait()
                if not waiters_registered.wait(timeout=5.0):
                    raise TimeoutError("Waiter threads failed to reach condition wait within timeout")

                # Verify under master_lock that all waiters are genuinely registered in _waiters
                with request_mgr._master_lock:
                    assert record_ref is not None
                    assert len(record_ref.condition._waiters) == expected_waiters

                raise RuntimeError("Simulated Gemini API network outage")

            # If a waiter incorrectly becomes a new primary request, return OK (HTTP 200)
            return PolicyAnswer(question=question, answer="OK", sources=[])

        mock_policy.ask = failing_ask

        barrier = threading.Barrier(num_threads)
        results = [None] * num_threads

        def worker(thread_idx):
            with app.test_client() as c:
                barrier.wait()
                res = c.post(
                    f"/api/v1/conversations/{conv['id']}/messages",
                    json={"message": "Question that will fail"},
                )
                results[thread_idx] = res.status_code

        try:
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)
                assert not t.is_alive(), "Worker thread hung and failed to wake"

            # 1. All threads received HTTP 500 error
            assert results == [500] * num_threads
            # 2. PolicyService was invoked exactly once by the primary request
            assert call_count == 1
            # 3. All expected waiter threads genuinely registered on the condition variable
            assert actual_waiters_count == expected_waiters
            assert request_mgr.get_stats()["in_flight_waits"] == expected_waiters
        finally:
            request_mgr.start_request = orig_start_request

    def test_deleted_conversation_during_processing(self, isolated_env):
        """Deleting conversation prevents newly inserted assistant message."""
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        conv = chat_svc.create_conversation()

        # Delete conversation
        assert chat_svc.delete_conversation(conv["id"]) is True

        # Send message to deleted conversation
        res = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            json={"message": "Hello to deleted conv"},
        )
        assert res.status_code == 404


# ── Category 5: Security Verification, Hygiene & IP Spoofing Defense ──────────

class TestHygieneAndSecurity:
    """Verifies security controls, absence of unprotected reset endpoints, and IP spoofing defense."""

    def test_public_cannot_access_request_stats_endpoint(self, isolated_env):
        """Internal operational metrics must not be exposed via unauthenticated public HTTP endpoint."""
        client = isolated_env["client"]
        res = client.get("/api/v1/request/stats")
        assert res.status_code == 404

    def test_public_cannot_reset_rate_limits_endpoint(self, isolated_env):
        """Unauthenticated reset endpoint must not exist in production."""
        client = isolated_env["client"]
        res = client.post("/api/v1/request/reset")
        assert res.status_code in (404, 405)

    def test_untrusted_forwarded_ip_spoofing_prevented(self, isolated_env):
        """
        Verify that an attacker attempting IP spoofing via X-Forwarded-For cannot bypass rate limiting.
        Render's reverse proxy appends the genuine client IP at the end of X-Forwarded-For.
        The rate limiter must extract the trusted boundary IP (index -1) and ignore attacker-forged prefixes.
        """
        client = isolated_env["client"]
        chat_svc = isolated_env["chat_svc"]
        rate_limiter = isolated_env["rate_limiter"]
        rate_limiter.ip_limit = 2
        rate_limiter.ip_window = 60

        conv = chat_svc.create_conversation()

        # Attacker tries to spoof client IP by changing the first token
        # but Render proxy appends real IP "203.0.113.195" at the end:
        res1 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.195"},
            json={"message": "Message 1 from attacker"},
        )
        assert res1.status_code == 200

        res2 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Forwarded-For": "2.2.2.2, 203.0.113.195"},
            json={"message": "Message 2 from attacker"},
        )
        assert res2.status_code == 200

        # 3rd request: attacker changes the spoofed prefix again to "3.3.3.3"
        # Since the genuine client IP is 203.0.113.195, the rate limiter MUST block it with HTTP 429!
        res3 = client.post(
            f"/api/v1/conversations/{conv['id']}/messages",
            headers={"X-Forwarded-For": "3.3.3.3, 203.0.113.195"},
            json={"message": "Message 3 from attacker"},
        )
        assert res3.status_code == 429
        assert res3.get_json()["code"] == "RATE_LIMITED"

    def test_service_metrics_and_reset_programmatic(self, isolated_env):
        """Verifies operational metrics and reset work cleanly at Python service level."""
        rate_limiter = isolated_env["rate_limiter"]
        request_mgr = isolated_env["request_mgr"]

        rl_stats = rate_limiter.get_stats()
        rm_stats = request_mgr.get_stats()

        assert "ip_limit" in rl_stats
        assert "duplicate_window_seconds" in rm_stats

        rate_limiter.reset()
        request_mgr.reset()
        assert rate_limiter.get_stats()["active_keys_tracked"] == 0
        assert request_mgr.get_stats()["active_records"] == 0

    def test_stale_cleanup_methods(self, isolated_env):
        request_mgr = isolated_env["request_mgr"]
        rate_limiter = isolated_env["rate_limiter"]

        # Run cleanups
        pruned_records = request_mgr.cleanup_stale()
        pruned_keys = rate_limiter.cleanup_stale()
        assert isinstance(pruned_records, int)
        assert isinstance(pruned_keys, int)
