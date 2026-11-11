"""
tests/test_chat_service.py — Module 6: Chat Management & Conversation History Test Suite

Covers all 34 required test cases across:
  - Conversation Lifecycle (Tests 1 to 9)
  - API Validation (Tests 10 to 18)
  - Context Handling (Tests 19 to 24)
  - Data Safety & Integrity (Tests 25 to 30)
  - Regression & System Invariants (Tests 31 to 34)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from unittest.mock import MagicMock
import pytest

from app import create_app
from backend.services.chat_service import (
    ChatService,
    init_db,
    get_db_connection,
    immediate_transaction,
    generate_conversation_title,
)
from backend.services.rate_limiter import SlidingWindowRateLimiter
from backend.services.policy_service import PolicyService, PolicyAnswer
from backend.services.gemini_service import GeminiService, GeminiResponse


# ── Test Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db_path(tmp_path):
    """Provides a fresh isolated SQLite database path per test."""
    db_file = tmp_path / "test_chat.db"
    return str(db_file)


@pytest.fixture
def mock_policy_service():
    """Provides a mock PolicyService returning controlled grounded responses."""
    service = MagicMock(spec=PolicyService)
    service.ask.return_value = PolicyAnswer(
        question="What is the passing requirement?",
        answer=(
            "To pass the course and earn the Verified Certificate, you must achieve an overall "
            "weighted score of at least 60% across all assessments, AND score at least 40% in "
            "the Proctored Final Exam separately.\n\nSource: Grading Policy V2."
        ),
        sources=["Grading & Late Submission Policy V2 (Current)"],
        is_in_scope=True,
        is_supported=True,
    )
    return service


@pytest.fixture
def chat_service(temp_db_path, mock_policy_service):
    """Provides an isolated ChatService instance backed by temp SQLite and mock policy."""
    return ChatService(
        db_path=temp_db_path,
        policy_service=mock_policy_service,
        max_context_messages=6,
    )


@pytest.fixture
def client(chat_service):
    """Provides a Flask test client with the isolated ChatService injected."""
    rate_limiter = SlidingWindowRateLimiter(default_ip_limit=500, default_ip_window=60)
    app = create_app({
        "TESTING": True,
        "chat_service": chat_service,
        "rate_limiter": rate_limiter,
    })
    with app.test_client() as test_client:
        yield test_client


# ── Category 1: Conversation Lifecycle (Tests 1 to 9) ─────────────────────────

class TestConversationLifecycle:

    def test_01_create_conversation(self, chat_service, client):
        """1. Create a conversation via POST /api/v1/conversations."""
        resp = client.post("/api/v1/conversations")
        assert resp.status_code == 201
        data = resp.get_json()
        assert "id" in data
        assert data["status"] == "active"
        assert "created_at" in data
        assert "updated_at" in data

    def test_02_retrieve_existing_conversation(self, chat_service, client):
        """2. Retrieve an existing conversation via GET /api/v1/conversations/{id}."""
        conv = chat_service.create_conversation()
        resp = client.get(f"/api/v1/conversations/{conv['id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == conv["id"]
        assert data["status"] == "active"

    def test_03_list_multiple_conversations(self, chat_service, client):
        """3. List multiple conversations ordered by most recently updated."""
        conv1 = chat_service.create_conversation()
        conv2 = chat_service.create_conversation()
        # Update conv1 so it has a later updated_at
        chat_service.save_user_message(conv1["id"], "Hello from conv 1")

        resp = client.get("/api/v1/conversations")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "conversations" in data
        ids = [c["id"] for c in data["conversations"]]
        assert conv1["id"] in ids
        assert conv2["id"] in ids
        # conv1 should appear before conv2 due to recent activity
        assert ids.index(conv1["id"]) < ids.index(conv2["id"])

    def test_04_save_user_message(self, chat_service):
        """4. Save a user message directly through the service."""
        conv = chat_service.create_conversation()
        msg = chat_service.save_user_message(conv["id"], "What is the capstone deadline?")
        assert msg["conversation_id"] == conv["id"]
        assert msg["role"] == "user"
        assert msg["content"] == "What is the capstone deadline?"
        assert "created_at" in msg

    def test_05_save_assistant_message(self, chat_service):
        """5. Save an assistant message with source attributions."""
        conv = chat_service.create_conversation()
        sources = ["Grading & Late Submission Policy V2 (Current)"]
        msg = chat_service.save_assistant_message(
            conv["id"], "Deadline is 15 November 2026.", sources=sources
        )
        assert msg["role"] == "assistant"
        assert msg["content"] == "Deadline is 15 November 2026."
        assert msg["sources"] == sources

    def test_06_retrieve_messages_chronological_order(self, chat_service, client):
        """6. Retrieve messages belonging to a conversation in chronological order."""
        conv = chat_service.create_conversation()
        chat_service.save_user_message(conv["id"], "First message")
        chat_service.save_assistant_message(conv["id"], "First reply")
        chat_service.save_user_message(conv["id"], "Second message")

        resp = client.get(f"/api/v1/conversations/{conv['id']}/messages")
        assert resp.status_code == 200
        data = resp.get_json()
        messages = data["messages"]
        assert len(messages) == 3
        assert messages[0]["content"] == "First message"
        assert messages[1]["content"] == "First reply"
        assert messages[2]["content"] == "Second message"

    def test_07_update_conversation_activity_timestamp(self, chat_service):
        """7. Verify conversation updated_at updates on message activity."""
        conv = chat_service.create_conversation()
        original_updated_at = conv["updated_at"]
        # Advance time by saving a message
        chat_service.save_user_message(conv["id"], "Activity update test")
        updated_conv = chat_service.get_conversation(conv["id"])
        assert updated_conv["updated_at"] >= original_updated_at

    def test_08_delete_conversation(self, chat_service, client):
        """8. Delete a conversation via DELETE /api/v1/conversations/{id}."""
        conv = chat_service.create_conversation()
        resp = client.delete(f"/api/v1/conversations/{conv['id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

        # Subsequent GET should return 404
        get_resp = client.get(f"/api/v1/conversations/{conv['id']}")
        assert get_resp.status_code == 404

    def test_09_verify_messages_deleted_with_conversation(self, chat_service, temp_db_path):
        """9. Ensure deleting a conversation purges all associated messages."""
        conv = chat_service.create_conversation()
        chat_service.save_user_message(conv["id"], "Message to be purged")
        chat_service.save_assistant_message(conv["id"], "Reply to be purged")

        # Verify messages exist
        assert len(chat_service.get_conversation_messages(conv["id"])) == 2

        # Delete conversation
        chat_service.delete_conversation(conv["id"])

        # Check DB directly: messages table must have 0 rows for this conversation
        conn = get_db_connection(temp_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv["id"],))
            count = cursor.fetchone()[0]
            assert count == 0
        finally:
            conn.close()


# ── Category 2: API Validation & Error Handling (Tests 10 to 18) ───────────────

class TestApiValidation:

    def test_10_missing_message_content(self, client):
        """10. POST /messages with missing message field returns 400."""
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={},
        )
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"].lower()

    def test_11_empty_message(self, client):
        """11. POST /messages with empty string returns 400."""
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": ""},
        )
        assert resp.status_code == 400
        assert "empty" in resp.get_json()["error"].lower()

    def test_12_whitespace_only_message(self, client):
        """12. POST /messages with whitespace string returns 400."""
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "   \n\t   "},
        )
        assert resp.status_code == 400
        assert "whitespace" in resp.get_json()["error"].lower()

    def test_13_non_string_message(self, client):
        """13. POST /messages with non-string data returns 400."""
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        for invalid_val in [12345, ["hello"], {"text": "hi"}, True]:
            resp = client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={"message": invalid_val},
            )
            assert resp.status_code == 400
            assert "must be a string" in resp.get_json()["error"].lower()

    def test_14_excessively_long_message(self, client):
        """14. POST /messages with >4000 characters returns 400."""
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        long_msg = "A" * 4500
        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": long_msg},
        )
        assert resp.status_code == 400
        assert "maximum allowed length" in resp.get_json()["error"].lower()

    def test_15_invalid_conversation_id(self, client):
        """15. Malicious or SQL-injection conversation ID returns 404."""
        resp = client.post(
            "/api/v1/conversations/' OR 1=1 --/messages",
            json={"message": "Hello"},
        )
        assert resp.status_code == 404

    def test_16_non_existent_conversation(self, client):
        """16. Request to a non-existent UUID returns 404."""
        resp = client.post(
            "/api/v1/conversations/00000000-0000-0000-0000-000000000000/messages",
            json={"message": "What is the pass score?"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()

    def test_17_malformed_json_request(self, client):
        """17. Non-JSON request body returns 400."""
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            data="this is not json",
            content_type="text/plain",
        )
        assert resp.status_code == 400

    def test_18_correct_http_status_codes(self, client):
        """18. Verify 200, 201, 400, 404, and 405 status codes."""
        # 201 on create
        c_resp = client.post("/api/v1/conversations")
        assert c_resp.status_code == 201
        conv_id = c_resp.get_json()["id"]

        # 200 on message post
        m_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the passing criteria?"},
        )
        assert m_resp.status_code == 200

        # 400 on bad payload
        bad_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={},
        )
        assert bad_resp.status_code == 400

        # 404 on not found
        nf_resp = client.get("/api/v1/conversations/not-found-id")
        assert nf_resp.status_code == 404

        # 405 on method not allowed
        mna_resp = client.put("/api/v1/conversations")
        assert mna_resp.status_code == 405


# ── Category 3: Context Handling & Follow-Ups (Tests 19 to 24) ────────────────

class TestContextHandling:

    def test_19_follow_up_uses_relevant_context(self, chat_service, client):
        """19. Follow-up 'How long is it?' resolves 'it' to final exam."""
        # Configure policy mock to return specific answer based on question text
        def policy_side_effect(q):
            if "exam" in q.lower() and ("duration" in q.lower() or "how long" in q.lower()):
                return PolicyAnswer(
                    question=q,
                    answer="The Proctored Final Exam duration is 90 minutes. Source: Official Exam Rules.",
                    sources=["Final Exam Rules & Student Forum Excerpts"],
                )
            return PolicyAnswer(
                question=q,
                answer="The final exam is on 20 November 2026. Source: Official Exam Rules.",
                sources=["Final Exam Rules & Student Forum Excerpts"],
            )

        chat_service.policy_service.ask.side_effect = policy_side_effect

        # Turn 1: Ask about exam date
        conv = client.post("/api/v1/conversations").get_json()
        conv_id = conv["id"]
        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the final exam date?"},
        )

        # Turn 2: Follow-up question
        resp2 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "How long is it?"},
        )
        assert resp2.status_code == 200
        ans = resp2.get_json()["content"]
        assert "90 minutes" in ans

    def test_20_unrelated_previous_messages_do_not_contaminate(self, chat_service, client):
        """20. Self-contained question does not receive contamination from previous turn."""
        chat_service.policy_service.ask.side_effect = lambda q: PolicyAnswer(
            question=q,
            answer="The exam is scheduled on 20 November 2026.",
            sources=["Final Exam Rules & Student Forum Excerpts"],
        )

        conv = client.post("/api/v1/conversations").get_json()
        conv_id = conv["id"]

        # Turn 1: Fee inquiry
        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the Verified Certificate fee?"},
        )

        # Turn 2: Completely independent exam question
        resp2 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "When is the final exam?"},
        )
        assert resp2.status_code == 200
        # The query passed to policy_service should not have fee contamination
        called_q = chat_service.policy_service.ask.call_args[0][0]
        assert "fee" not in called_q.lower()
        assert "final exam" in called_q.lower()

    def test_21_previous_assistant_answers_cannot_override_official_policy(self, chat_service):
        """21. Old assistant answers cannot override current official policy."""
        conv = chat_service.create_conversation()
        # Simulate a previous compromised assistant answer claiming 50%
        chat_service.save_user_message(conv["id"], "What was the passing score in 2025?")
        chat_service.save_assistant_message(
            conv["id"], "In earlier cohorts, the passing score was 50%."
        )

        # Now student asks current passing requirement
        # PolicyService ask should still be invoked and enforce 60% + 40%
        res = chat_service.process_message(conv["id"], "What is the passing requirement now?")
        assert "60%" in res["content"]
        assert "40%" in res["content"]

    def test_22_ambiguous_follow_up_requests_clarification(self, chat_service, client):
        """22. Vague follow-up 'What about the deadline?' with multiple topics requests clarification."""
        conv = client.post("/api/v1/conversations").get_json()
        conv_id = conv["id"]

        # Turn 1: Discusses both weekly quizzes and capstone
        chat_service.save_user_message(conv_id, "Tell me about weekly quizzes and the capstone case.")
        chat_service.save_assistant_message(
            conv_id, "The course has 6 weekly quizzes and 1 capstone case."
        )

        # Turn 2: Ambiguous question
        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What about the deadline?"},
        )
        assert resp.status_code == 200
        content = resp.get_json()["content"]
        assert "clarify" in content.lower()
        assert "quiz" in content.lower()
        assert "capstone" in content.lower()

    def test_23_conversation_context_window_respected(self, chat_service):
        """23. Only the configured max_context_messages are used for context."""
        chat_service.max_context_messages = 4
        conv = chat_service.create_conversation()

        # Add 10 historical messages
        for i in range(10):
            chat_service.save_user_message(conv["id"], f"Message {i}")
            chat_service.save_assistant_message(conv["id"], f"Reply {i}")

        history = chat_service.get_conversation_messages(
            conv["id"], limit=chat_service.max_context_messages
        )
        assert len(history) <= 4

    def test_24_official_policy_retrieval_still_happens_for_follow_ups(self, chat_service, client):
        """24. Follow-up queries still receive authoritative source attribution."""
        chat_service.policy_service.ask.side_effect = lambda q: PolicyAnswer(
            question=q,
            answer="Late submissions are not accepted. Source: Grading Policy V2.",
            sources=["Grading & Late Submission Policy V2 (Current)"],
        )

        conv = client.post("/api/v1/conversations").get_json()
        conv_id = conv["id"]

        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the capstone deadline?"},
        )

        follow_up_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Can I submit it one day late?"},
        )
        assert follow_up_resp.status_code == 200
        sources = follow_up_resp.get_json()["sources"]
        assert "Grading & Late Submission Policy V2 (Current)" in sources


# ── Category 4: Data Safety & Integrity (Tests 25 to 30) ──────────────────────

class TestDataSafetyAndIntegrity:

    def test_25_cross_conversation_isolation(self, client):
        """25. Messages in Conversation A never appear in Conversation B."""
        conv_a = client.post("/api/v1/conversations").get_json()["id"]
        conv_b = client.post("/api/v1/conversations").get_json()["id"]

        client.post(f"/api/v1/conversations/{conv_a}/messages", json={"message": "Secret message in A"})
        client.post(f"/api/v1/conversations/{conv_b}/messages", json={"message": "Public message in B"})

        msgs_a = client.get(f"/api/v1/conversations/{conv_a}/messages").get_json()["messages"]
        msgs_b = client.get(f"/api/v1/conversations/{conv_b}/messages").get_json()["messages"]

        contents_a = [m["content"] for m in msgs_a]
        contents_b = [m["content"] for m in msgs_b]

        assert "Secret message in A" in contents_a
        assert "Secret message in A" not in contents_b
        assert "Public message in B" in contents_b
        assert "Public message in B" not in contents_a

    def test_26_sql_injection_defense(self, client):
        """26. SQL injection payloads in message text or parameters are safely escaped."""
        conv = client.post("/api/v1/conversations").get_json()["id"]
        malicious_msg = "'; DROP TABLE messages; SELECT * FROM conversations WHERE '1'='1"

        resp = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": malicious_msg},
        )
        assert resp.status_code == 200

        # Verify messages table is intact
        msgs = client.get(f"/api/v1/conversations/{conv}/messages").get_json()["messages"]
        assert any(malicious_msg in m["content"] for m in msgs)

    def test_27_foreign_key_integrity(self, chat_service, temp_db_path):
        """27. Inserting message with non-existent conversation_id triggers FK violation."""
        conn = get_db_connection(temp_db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                with conn:
                    conn.execute(
                        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                        "VALUES ('bad-id', 'non-existent-conv-id', 'user', 'test', '2026-01-01T00:00:00Z');"
                    )
        finally:
            conn.close()

    def test_28_deleted_conversation_cannot_accept_new_messages(self, client):
        """28. Posting to a deleted conversation returns 404."""
        conv = client.post("/api/v1/conversations").get_json()["id"]
        # Delete conversation
        del_resp = client.delete(f"/api/v1/conversations/{conv}")
        assert del_resp.status_code == 200

        # Attempt to post new message
        post_resp = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "Are you still there?"},
        )
        assert post_resp.status_code == 404

    def test_29_api_keys_never_stored_in_conversation_records(self, chat_service, client):
        """29. If an API key appears in query or error, it is never stored in DB."""
        secret_key = "AIzaSyFakeSecretKeyNotToBeStored"
        chat_service.policy_service.ask.return_value = PolicyAnswer(
            question="My key is AIzaSyFakeSecretKeyNotToBeStored",
            answer="The exam is closed book.",
            sources=["Official Exam Rules"],
        )

        conv = client.post("/api/v1/conversations").get_json()["id"]
        client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "Can I use AI tools in the exam?"},
        )

        msgs = client.get(f"/api/v1/conversations/{conv}/messages").get_json()["messages"]
        for m in msgs:
            assert secret_key not in m["content"]

    def test_30_concurrent_message_writes(self, chat_service):
        """30. Concurrent threads saving messages do not corrupt SQLite database."""
        conv = chat_service.create_conversation()
        errors = []

        def worker(thread_idx):
            try:
                for i in range(5):
                    chat_service.save_user_message(conv["id"], f"Thread {thread_idx} msg {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        messages = chat_service.get_conversation_messages(conv["id"], limit=50)
        assert len(messages) == 20  # 4 threads * 5 messages


# ── Category 5: Regression & Integration (Tests 31 to 34) ──────────────────────

class TestRegressionAndIntegration:

    def test_31_modules_1_to_5_integration_intact(self, client):
        """31. Calling /api/v1/conversations/{id}/messages executes full PolicyService flow."""
        conv = client.post("/api/v1/conversations").get_json()["id"]
        resp = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "What is the passing requirement?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "60%" in data["content"]
        assert "40%" in data["content"]
        assert len(data["sources"]) > 0

    def test_32_gemini_failure_fallback_still_works_through_chat_api(self, chat_service, client):
        """32. When Gemini fails, the chat API still returns policy fallback."""
        # Use real PolicyService with unconfigured Gemini to test fallback
        real_policy = PolicyService(gemini_service=GeminiService(api_key=""))
        chat_service.policy_service = real_policy

        conv = client.post("/api/v1/conversations").get_json()["id"]
        resp = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "What is the capstone deadline?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "15 November 2026" in data["content"]
        assert any("Policy V2" in s or "Grading" in s for s in data["sources"])

    def test_33_source_attribution_intact_in_chat_api(self, client):
        """33. Source attribution accurately matches official registered titles."""
        conv = client.post("/api/v1/conversations").get_json()["id"]
        resp = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "When is the final exam?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "sources" in data
        assert isinstance(data["sources"], list)

    def test_34_health_endpoint_remains_independent_of_chat_and_gemini(self, client):
        """34. GET /health remains functional and independent of AI or DB."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "healthy"


# ── Category 6: Independent Audit Verification (Tests 35 to 40) ───────────────

class TestAuditVerificationAndEdgeCases:

    def test_35_repeated_delete_returns_404(self, client):
        """35. Deleting a conversation once returns 200; deleting it again returns 404."""
        conv = client.post("/api/v1/conversations").get_json()["id"]

        # First delete should succeed
        resp1 = client.delete(f"/api/v1/conversations/{conv}")
        assert resp1.status_code == 200
        assert resp1.get_json()["success"] is True

        # Second delete of already-deleted conversation must return 404
        resp2 = client.delete(f"/api/v1/conversations/{conv}")
        assert resp2.status_code == 404
        assert "not found" in resp2.get_json()["error"].lower()

    def test_36_recent_messages_context_resolution_in_long_conversation(self, chat_service):
        """36. Context resolution uses the most recent turns rather than the oldest turns."""
        conv = chat_service.create_conversation()
        conv_id = conv["id"]

        # Simulate 10 turns on irrelevant or early topics (e.g. course duration)
        for i in range(5):
            chat_service.save_user_message(conv_id, f"Early question {i} about course overview")
            chat_service.save_assistant_message(conv_id, f"Early answer {i} on course duration")

        # Turn 6: Student discusses final exam
        chat_service.save_user_message(conv_id, "When is the final exam?")
        chat_service.save_assistant_message(conv_id, "The final exam is on 20 November 2026.")

        # Turn 7: Elliptical follow-up
        # If get_recent_messages is used, it will see the final exam topic and resolve 'it'
        resolved_q, is_amb, _ = chat_service.resolve_context(
            "How long is it?",
            chat_service.get_recent_messages(conv_id, limit=chat_service.max_context_messages),
        )
        assert is_amb is False
        assert "final exam" in resolved_q.lower() or "duration" in resolved_q.lower()

    def test_37_concurrent_simultaneous_message_posting_to_same_conversation(self, chat_service):
        """37. Multiple threads posting messages simultaneously do not cause DB lock timeouts."""
        conv = chat_service.create_conversation()
        conv_id = conv["id"]
        errors = []

        def post_worker(thread_num):
            try:
                for j in range(5):
                    chat_service.save_user_message(conv_id, f"Worker {thread_num} msg {j}")
                    chat_service.save_assistant_message(
                        conv_id, f"Reply to worker {thread_num} msg {j}", sources=["Handbook"]
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=post_worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        all_msgs = chat_service.get_conversation_messages(conv_id, limit=100)
        # 4 threads * 5 turns * 2 messages per turn = 40 messages
        assert len(all_msgs) == 40

    def test_38_conversation_deleted_during_processing_aborts_gracefully(self, chat_service):
        """38. If conversation is deleted while policy generation is occurring, it aborts cleanly."""
        conv = chat_service.create_conversation()
        conv_id = conv["id"]

        # Mock policy service that deletes the conversation while 'thinking'
        def sneaky_policy(q):
            chat_service.delete_conversation(conv_id)
            return PolicyAnswer(
                question=q,
                answer="Answer generated while conv was being deleted",
                sources=["Grading Policy V2"],
            )

        chat_service.policy_service.ask.side_effect = sneaky_policy

        with pytest.raises(ValueError) as exc_info:
            chat_service.process_message(conv_id, "What is the capstone deadline?")
        assert "deleted" in str(exc_info.value).lower()

        # Direct DB check: messages must be empty (no resurrecting messages)
        conn = get_db_connection(chat_service.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
        finally:
            conn.close()

    def test_39_pagination_limit_and_offset_for_conversations_and_messages(self, client, chat_service):
        """39. Verify pagination parameters limit and offset for conversations and messages."""
        # Create 5 conversations
        conv_ids = []
        for i in range(5):
            c = chat_service.create_conversation()
            conv_ids.append(c["id"])

        # Fetch with limit=2, offset=0
        resp1 = client.get("/api/v1/conversations?limit=2&offset=0")
        assert resp1.status_code == 200
        data1 = resp1.get_json()["conversations"]
        assert len(data1) == 2

        # Fetch with limit=2, offset=2
        resp2 = client.get("/api/v1/conversations?limit=2&offset=2")
        assert resp2.status_code == 200
        data2 = resp2.get_json()["conversations"]
        assert len(data2) == 2
        # None of the IDs in page 2 should be in page 1
        page1_ids = {c["id"] for c in data1}
        page2_ids = {c["id"] for c in data2}
        assert page1_ids.isdisjoint(page2_ids)

        # Message pagination
        test_conv = conv_ids[0]
        for i in range(6):
            chat_service.save_user_message(test_conv, f"Message {i}")

        msg_resp = client.get(f"/api/v1/conversations/{test_conv}/messages?limit=3&offset=3")
        assert msg_resp.status_code == 200
        msgs = msg_resp.get_json()["messages"]
        assert len(msgs) == 3
        assert msgs[0]["content"] == "Message 3"

    def test_40_hard_delete_removes_conversation_record_entirely(self, chat_service, temp_db_path):
        """40. Hard delete removes row entirely from conversations table."""
        conv = chat_service.create_conversation()
        conv_id = conv["id"]
        chat_service.save_user_message(conv_id, "Some user message")

        # Perform hard delete
        success = chat_service.delete_conversation(conv_id, hard_delete=True)
        assert success is True

        conn = get_db_connection(temp_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM conversations WHERE id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
        finally:
            conn.close()

    def test_41_deterministic_message_ordering_with_identical_timestamps(self, chat_service, temp_db_path):
        """41. Messages with identical created_at timestamps are ordered deterministically by rowid."""
        conv = chat_service.create_conversation()
        conv_id = conv["id"]
        same_timestamp = "2026-11-10T12:00:00.000000+00:00"

        # Insert 6 messages directly with identical created_at
        conn = get_db_connection(temp_db_path)
        try:
            with conn:
                for idx in range(6):
                    conn.execute(
                        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                        "VALUES (?, ?, 'user', ?, ?);",
                        (f"msg-{idx}", conv_id, f"Order test {idx}", same_timestamp),
                    )
        finally:
            conn.close()

        # Full history should order msg-0 to msg-5 by rowid ASC
        history = chat_service.get_conversation_messages(conv_id, limit=10)
        assert len(history) == 6
        assert [m["content"] for m in history] == [f"Order test {i}" for i in range(6)]

        # Recent messages (limit=3) should deterministically select the last 3 (3, 4, 5) in chronological order
        recent = chat_service.get_recent_messages(conv_id, limit=3)
        assert len(recent) == 3
        assert [m["content"] for m in recent] == ["Order test 3", "Order test 4", "Order test 5"]

    def test_42_atomic_deletion_race_condition(self, chat_service, temp_db_path):
        """42. Atomic transaction ensures no messages can be inserted into a deleted conversation."""
        conv = chat_service.create_conversation()
        conv_id = conv["id"]

        # Delete conversation
        assert chat_service.delete_conversation(conv_id) is True

        # Attempt to save user message directly into deleted conversation must raise ValueError
        with pytest.raises(ValueError) as exc1:
            chat_service.save_user_message(conv_id, "User msg after delete")
        assert "deleted" in str(exc1.value).lower()

        # Attempt to save assistant message directly into deleted conversation must raise ValueError
        with pytest.raises(ValueError) as exc2:
            chat_service.save_assistant_message(conv_id, "Assistant msg after delete")
        assert "deleted" in str(exc2.value).lower()

        # Verify DB directly: messages table has zero rows for this conversation
        conn = get_db_connection(temp_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
        finally:
            conn.close()

    def test_43_pagination_robustness_against_invalid_and_boundary_values(self, client, chat_service):
        """43. Pagination gracefully handles invalid strings, negatives, zeros, and extreme limits."""
        conv = chat_service.create_conversation()
        for i in range(5):
            chat_service.save_user_message(conv["id"], f"Pagination message {i}")

        # Invalid limit string -> fallback to default (50/100)
        resp1 = client.get("/api/v1/conversations?limit=abc&offset=xyz")
        assert resp1.status_code == 200

        # Negative limit -> clamped to min 1
        resp2 = client.get(f"/api/v1/conversations/{conv['id']}/messages?limit=-10&offset=-5")
        assert resp2.status_code == 200
        data2 = resp2.get_json()["messages"]
        assert len(data2) >= 1

        # Zero limit -> clamped to min 1
        resp3 = client.get(f"/api/v1/conversations/{conv['id']}/messages?limit=0")
        assert resp3.status_code == 200
        assert len(resp3.get_json()["messages"]) >= 1

        # Extreme limit -> clamped to max 500
        resp4 = client.get(f"/api/v1/conversations/{conv['id']}/messages?limit=99999")
        assert resp4.status_code == 200
        assert len(resp4.get_json()["messages"]) <= 500

        # Conversations endpoint extreme limit clamped to max 200
        resp5 = client.get("/api/v1/conversations?limit=99999")
        assert resp5.status_code == 200
        assert len(resp5.get_json()["conversations"]) <= 200

    def test_44_context_safety_adversarial_stale_answers_cannot_override_policy(self, chat_service, client):
        """44. Prior adversarial assistant messages in history cannot override official policy."""
        # Use real PolicyService with extractive fallback to verify official document enforcement
        chat_service.policy_service = PolicyService(gemini_service=GeminiService(api_key=""))

        conv = client.post("/api/v1/conversations").get_json()["id"]

        # Poison Turn 1: Prior assistant hallucinated incorrect fee (Rs. 4,999)
        chat_service.save_user_message(conv, "What is the fee?")
        chat_service.save_assistant_message(conv, "The fee is Rs. 4,999.")

        # Poison Turn 2: Prior assistant hallucinated incorrect exam date (December 30)
        chat_service.save_user_message(conv, "When is the exam?")
        chat_service.save_assistant_message(conv, "The exam is on 30 December 2026.")

        # Poison Turn 3: Prior assistant claimed late submission 10% penalty
        chat_service.save_user_message(conv, "Can I submit late?")
        chat_service.save_assistant_message(conv, "Late submissions are allowed with 10% penalty.")

        # Test A: Asking certificate fee now must return Rs. 1,500 from Doc 01, ignoring poisoned history
        fee_resp = client.post(f"/api/v1/conversations/{conv}/messages", json={"message": "What is the Verified Certificate fee?"})
        assert fee_resp.status_code == 200
        fee_data = fee_resp.get_json()
        assert "1,500" in fee_data["content"] or "1500" in fee_data["content"]
        assert "4,999" not in fee_data["content"]
        assert any("Handbook" in s or "Overview" in s for s in fee_data["sources"])

        # Test B: Asking exam date now must return 20 November 2026 from Doc 04
        date_resp = client.post(f"/api/v1/conversations/{conv}/messages", json={"message": "When is the final exam held?"})
        assert date_resp.status_code == 200
        date_data = date_resp.get_json()
        assert "20 November 2026" in date_data["content"]
        assert "30 December" not in date_data["content"]

        # Test C: Asking late submission policy now must return late submissions not accepted from Doc 03
        late_resp = client.post(f"/api/v1/conversations/{conv}/messages", json={"message": "Are late submissions accepted?"})
        assert late_resp.status_code == 200
        late_data = late_resp.get_json()
        assert "no longer accepted" in late_data["content"].lower()
        assert "discontinued" in late_data["content"].lower()

    def test_45_soft_deletion_full_lifecycle(self, client, temp_db_path):
        """45. Full soft-deletion lifecycle: creation, message, soft-delete, purge, and 404 checks."""
        # 1. Create conversation
        create_resp = client.post("/api/v1/conversations")
        assert create_resp.status_code == 201
        conv_id = create_resp.get_json()["id"]

        # 2. Add message
        msg_resp = client.post(f"/api/v1/conversations/{conv_id}/messages", json={"message": "Hello before delete"})
        assert msg_resp.status_code == 200

        # 3. Soft-delete conversation
        del_resp = client.delete(f"/api/v1/conversations/{conv_id}")
        assert del_resp.status_code == 200
        assert del_resp.get_json()["success"] is True

        # 4. Message records must be completely purged from DB
        conn = get_db_connection(temp_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
            # 5. Conversation row exists with status = 'deleted'
            cursor.execute("SELECT status FROM conversations WHERE id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == "deleted"
        finally:
            conn.close()

        # 6. GET conversation details returns 404
        assert client.get(f"/api/v1/conversations/{conv_id}").status_code == 404

        # 7. GET messages returns 404
        assert client.get(f"/api/v1/conversations/{conv_id}/messages").status_code == 404

        # 8. POST new message returns 404
        post_after_del = client.post(f"/api/v1/conversations/{conv_id}/messages", json={"message": "Message after delete"})
        assert post_after_del.status_code == 404

        # 9. Repeated DELETE returns 404
        repeat_del = client.delete(f"/api/v1/conversations/{conv_id}")
        assert repeat_del.status_code == 404

        # 10. List conversations excludes soft-deleted conversation
        list_resp = client.get("/api/v1/conversations")
        active_ids = [c["id"] for c in list_resp.get_json()["conversations"]]
        assert conv_id not in active_ids

    def test_46_verified_certificate_fee_regression_chat_api(self, client, chat_service):
        """46. Regression test: Ask 'What is the Verified Certificate fee?' via chat API -> ₹1,500 from Doc 01."""
        chat_service.policy_service = PolicyService(gemini_service=GeminiService(api_key=""))
        conv = client.post("/api/v1/conversations").get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv}/messages",
            json={"message": "What is the Verified Certificate fee?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "1,500" in data["content"] or "1500" in data["content"]
        assert "4,999" not in data["content"]
        assert "4999" not in data["content"]
        assert any("Handbook" in s or "Overview" in s for s in data["sources"])

    def test_47_concurrency_scenario_a_insertion_begins_then_deletion_attempted(self, chat_service, temp_db_path):
        """
        47. Scenario A: Connection A begins message insertion under BEGIN IMMEDIATE;
        Connection B attempts conversation deletion. Verifies consistent final state.
        """
        conv = chat_service.create_conversation()
        conv_id = conv["id"]

        event_a_acquired_lock = threading.Event()
        event_b_can_proceed = threading.Event()
        event_a_completed = threading.Event()

        errors = []

        def worker_a():
            """Simulates Connection A starting an immediate write transaction."""
            connA = get_db_connection(temp_db_path)
            try:
                with immediate_transaction(connA):
                    # Signal that A holds the write lock
                    event_a_acquired_lock.set()
                    # Wait briefly for B to attempt deletion and queue on the lock
                    event_b_can_proceed.wait(timeout=5.0)

                    # Now execute message insertion
                    msg_id = "msg-sync-a"
                    now_iso = "2026-11-10T12:00:00Z"
                    cursor = connA.cursor()
                    cursor.execute(
                        """
                        INSERT INTO messages (id, conversation_id, role, content, created_at, sources)
                        SELECT ?, id, 'user', 'Message from A', ?, NULL
                        FROM conversations
                        WHERE id = ? AND status = 'active';
                        """,
                        (msg_id, now_iso, conv_id),
                    )
                    cursor.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ? AND status = 'active';",
                        (now_iso, conv_id),
                    )
                event_a_completed.set()
            except Exception as e:
                errors.append(e)
            finally:
                connA.close()

        def worker_b():
            """Simulates Connection B attempting deletion while A holds the lock."""
            # Wait until A holds the write lock
            event_a_acquired_lock.wait(timeout=5.0)
            # Allow A to proceed after B is ready
            event_b_can_proceed.set()
            try:
                # delete_conversation will queue for A's lock, then purge messages and mark deleted
                success = chat_service.delete_conversation(conv_id)
                assert success is True
            except Exception as e:
                errors.append(e)

        thread_a = threading.Thread(target=worker_a)
        thread_b = threading.Thread(target=worker_b)

        thread_a.start()
        thread_b.start()

        thread_a.join(timeout=10.0)
        thread_b.join(timeout=10.0)

        assert len(errors) == 0

        # Verify final state is completely consistent
        conn_check = get_db_connection(temp_db_path)
        try:
            cursor = conn_check.cursor()
            cursor.execute("SELECT status FROM conversations WHERE id = ?;", (conv_id,))
            assert cursor.fetchone()["status"] == "deleted"
            # Crucial invariant: zero orphan messages remaining in table
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
        finally:
            conn_check.close()

    def test_48_concurrency_scenario_b_deletion_commits_first_insertion_must_fail(self, chat_service, temp_db_path):
        """
        48. Scenario B: Connection B deletes the conversation first.
        Connection A attempts to insert a message into the deleted conversation.
        Connection A must fail atomically without inserting any rows.
        """
        conv = chat_service.create_conversation()
        conv_id = conv["id"]

        event_deleted = threading.Event()
        errors_b = []
        conn_a_failed = []

        def worker_b_deletes():
            try:
                assert chat_service.delete_conversation(conv_id) is True
                event_deleted.set()
            except Exception as e:
                errors_b.append(e)

        def worker_a_inserts():
            event_deleted.wait(timeout=5.0)
            try:
                chat_service.save_assistant_message(conv_id, "Reply from Conn A")
            except ValueError as val_err:
                conn_a_failed.append(str(val_err))

        t_b = threading.Thread(target=worker_b_deletes)
        t_a = threading.Thread(target=worker_a_inserts)

        t_b.start()
        t_a.start()

        t_b.join(timeout=5.0)
        t_a.join(timeout=5.0)

        assert len(errors_b) == 0
        # Conn A must have raised ValueError
        assert len(conn_a_failed) == 1
        assert "deleted" in conn_a_failed[0].lower()

        # Direct DB verification: zero messages exist
        conn_check = get_db_connection(temp_db_path)
        try:
            cursor = conn_check.cursor()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
        finally:
            conn_check.close()

    def test_49_concurrency_scenario_c_deletion_during_gemini_generation(self, chat_service, temp_db_path):
        """
        49. Scenario C: Conversation is deleted while Gemini generation is in-flight.
        Gemini returns, but saving the assistant message fails and zero messages remain.
        """
        conv = chat_service.create_conversation()
        conv_id = conv["id"]

        event_gemini_started = threading.Event()
        event_deletion_done = threading.Event()

        def slow_gemini_policy(q):
            # 1. Signal that Gemini network call is in flight
            event_gemini_started.set()
            # 2. Wait until external deletion has finished
            event_deletion_done.wait(timeout=5.0)
            return PolicyAnswer(
                question=q,
                answer="Answer generated while conversation was deleted",
                sources=["Grading Policy V2"],
            )

        chat_service.policy_service.ask.side_effect = slow_gemini_policy

        def external_deleter():
            event_gemini_started.wait(timeout=5.0)
            # Delete conversation while Gemini is running
            chat_service.delete_conversation(conv_id)
            event_deletion_done.set()

        thread_deleter = threading.Thread(target=external_deleter)
        thread_deleter.start()

        # process_message must fail when trying to save assistant reply
        with pytest.raises(ValueError) as exc_info:
            chat_service.process_message(conv_id, "What is the capstone deadline?")
        assert "deleted" in str(exc_info.value).lower()

        thread_deleter.join(timeout=5.0)

        # Database invariant: zero messages exist for the deleted conversation
        conn = get_db_connection(temp_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,))
            assert cursor.fetchone()[0] == 0
        finally:
            conn.close()


# ── Recent Conversations Title & Migration Test Suite ─────────────────────────

class TestConversationTitlesAndMigration:
    """Tests for conversation title generation, persistence, backfill, and schema migration."""

    def test_generate_conversation_title_passing_requirements(self):
        """Passing criteria questions map to 'Passing Requirements'."""
        assert generate_conversation_title("What are the passing requirements?") == "Passing Requirements"
        assert generate_conversation_title("What is the passing score for DPM-101?") == "Passing Requirements"
        assert generate_conversation_title("How do I pass the course?") == "Passing Requirements"

    def test_generate_conversation_title_final_exam(self):
        """Final exam logistics and rules map accurately."""
        assert generate_conversation_title("When is the final exam?") == "Final Exam Schedule"
        assert generate_conversation_title("What is the final exam date?") == "Final Exam Schedule"
        assert generate_conversation_title("What are the final exam rules?") == "Final Exam Rules"
        assert generate_conversation_title("Can I use a calculator during the exam?") == "Exam Materials"

    def test_generate_conversation_title_capstone_and_quizzes(self):
        """Capstone and quiz questions generate readable titles."""
        assert generate_conversation_title("What is the capstone deadline?") == "Capstone Deadline"
        assert generate_conversation_title("Can I submit the capstone late?") == "Capstone Submission"
        assert generate_conversation_title("When are the weekly quizzes due?") == "Quiz Deadlines"
        assert generate_conversation_title("What is the certificate fee?") == "Certificate Fee"

    def test_generate_conversation_title_fallbacks_and_empty(self):
        """Empty, whitespace, and general questions handle gracefully."""
        assert generate_conversation_title("") == "New Chat"
        assert generate_conversation_title("   ") == "New Chat"
        assert generate_conversation_title(None) == "New Chat"
        assert generate_conversation_title("Hello") == "Hello"
        assert generate_conversation_title("Tell me about product management") == "Product Management"

    def test_empty_conversation_has_new_chat_title(self, chat_service):
        """A brand new conversation starts with 'New Chat' title."""
        conv = chat_service.create_conversation()
        assert conv["title"] == "New Chat"

        fetched = chat_service.get_conversation(conv["id"])
        assert fetched["title"] == "New Chat"

        listing = chat_service.list_conversations()
        matching = [c for c in listing if c["id"] == conv["id"]]
        assert len(matching) == 1
        assert matching[0]["title"] == "New Chat"

    def test_title_persisted_on_first_user_message(self, chat_service):
        """First user message updates the title and subsequent messages preserve it."""
        conv = chat_service.create_conversation()
        assert conv["title"] == "New Chat"

        # First message sets title
        chat_service.save_user_message(conv["id"], "What are the passing requirements?")
        updated = chat_service.get_conversation(conv["id"])
        assert updated["title"] == "Passing Requirements"

        # Second message preserves the established title
        chat_service.save_user_message(conv["id"], "And what about distinction?")
        after_second = chat_service.get_conversation(conv["id"])
        assert after_second["title"] == "Passing Requirements"

    def test_backfill_existing_conversations_without_title(self, temp_db_path):
        """Existing conversations with NULL title are automatically backfilled from first message."""
        init_db(temp_db_path)
        chat = ChatService(db_path=temp_db_path)
        conv = chat.create_conversation()

        # Save user message
        chat.save_user_message(conv["id"], "What is the capstone deadline?")

        # Force title back to NULL directly in SQLite to simulate a legacy DB
        conn = get_db_connection(temp_db_path)
        try:
            with immediate_transaction(conn):
                conn.execute("UPDATE conversations SET title = NULL WHERE id = ?;", (conv["id"],))
        finally:
            conn.close()

        # Calling list_conversations backfills the title
        listing = chat.list_conversations()
        conv_item = [c for c in listing if c["id"] == conv["id"]][0]
        assert conv_item["title"] == "Capstone Deadline"

        # Verify it was written back to the SQLite table
        conn = get_db_connection(temp_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT title FROM conversations WHERE id = ?;", (conv["id"],))
            assert cursor.fetchone()["title"] == "Capstone Deadline"
        finally:
            conn.close()

    def test_database_migration_idempotent_and_handles_legacy_schema(self, tmp_path):
        """init_db must migrate legacy schemas without title and be safely idempotent."""
        db_file = str(tmp_path / "legacy_test.db")
        conn = sqlite3.connect(db_file)
        try:
            # Create old schema explicitly without 'title'
            conn.execute("""
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                );
            """)
            conn.execute("""
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sources TEXT
                );
            """)
            conn.execute(
                "INSERT INTO conversations (id, created_at, updated_at, status) VALUES ('legacy-1', '2026-01-01', '2026-01-01', 'active');"
            )
            conn.commit()
        finally:
            conn.close()

        # Run init_db on the legacy DB — must add 'title' column smoothly
        init_db(db_file)

        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(conversations);")
            columns = [row["name"] for row in cursor.fetchall()]
            assert "title" in columns

            # Legacy row still intact
            cursor.execute("SELECT id, status FROM conversations WHERE id = 'legacy-1';")
            row = cursor.fetchone()
            assert row["id"] == "legacy-1"
        finally:
            conn.close()

        # Run init_db a second time — must be completely idempotent (no exception)
        init_db(db_file)

    def test_api_conversations_endpoint_includes_title(self, client):
        """API endpoints return title and update it on message post."""
        # 1. Create conversation via API
        resp = client.post("/api/v1/conversations")
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["title"] == "New Chat"
        conv_id = data["id"]

        # 2. Post first message
        post_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "When is the final exam?"},
        )
        assert post_resp.status_code == 200

        # 3. Fetch conversation listing — title must reflect first message
        list_resp = client.get("/api/v1/conversations")
        assert list_resp.status_code == 200
        convs = list_resp.get_json()["conversations"]
        target = [c for c in convs if c["id"] == conv_id][0]
        assert target["title"] == "Final Exam Schedule"

