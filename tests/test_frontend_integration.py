"""
tests/test_frontend_integration.py — Integration Tests for Module 10 Frontend Chat Interface

Module 10: Frontend Chat Interface
Verifies:
  - Flask static file serving for index.html, CSS, and JS assets with correct MIME types.
  - Presence of semantic landmarks, accessibility attributes, and required UI controls.
  - End-to-end API integration with conversation lifecycle and message routes.
  - Idempotency key validation (header vs. body matching, regex syntax `^[a-zA-Z0-9_-]{1,128}$`).
  - Safe error handling for 404, 400, and 429 status codes.
  - Accurate schema and non-interference for GET /api/stats.
  - Security audit verifying zero innerHTML insertion of untrusted data in frontend JS.
"""

from __future__ import annotations

import re
import uuid
import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from backend.services.chat_service import ChatService
from backend.services.request_service import RequestManager
from backend.services.rate_limiter import SlidingWindowRateLimiter
from backend.services.stats_service import StatsService


@pytest.fixture
def app(tmp_path) -> Flask:
    """Create test application with isolated SQLite database and services."""
    test_db = str(tmp_path / "test_frontend.db")
    chat_svc = ChatService(db_path=test_db)
    rate_limiter = SlidingWindowRateLimiter(default_ip_limit=20, default_ip_window=60)
    request_mgr = RequestManager(idempotency_ttl=300)
    stats_svc = StatsService()

    app_instance = create_app({
        "TESTING": True,
        "DB_PATH": test_db,
        "chat_service": chat_svc,
        "rate_limiter": rate_limiter,
        "request_manager": request_mgr,
        "stats_service": stats_svc,
    })
    return app_instance


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


# ── 1. Static Asset Serving Tests ─────────────────────────────────────────────

class TestFrontendStaticServing:
    """Verify that Flask correctly serves all frontend files with expected headers."""

    def test_index_html_served_at_root(self, client: FlaskClient):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type
        content = resp.data.decode("utf-8")
        assert "<title>IIMBx DPM-101 Course Helpdesk</title>" in content
        assert 'id="sidebar"' in content
        assert 'id="message-feed"' in content
        assert 'id="message-input"' in content
        assert 'id="stats-dialog"' in content
        assert 'src="/js/storage.js"' in content
        assert 'src="/js/api.js"' in content
        assert 'src="/js/ui.js"' in content
        assert 'src="/js/app.js"' in content

    def test_css_main_served(self, client: FlaskClient):
        resp = client.get("/css/main.css")
        assert resp.status_code == 200
        assert "text/css" in resp.content_type
        content = resp.data.decode("utf-8")
        assert "--color-primary: #0B2545;" in content
        assert ".app-header" in content

    def test_css_chat_served(self, client: FlaskClient):
        resp = client.get("/css/chat.css")
        assert resp.status_code == 200
        assert "text/css" in resp.content_type
        content = resp.data.decode("utf-8")
        assert ".message-feed" in content
        assert ".source-pill" in content
        assert ".typing-dots" in content

    def test_js_storage_served(self, client: FlaskClient):
        resp = client.get("/js/storage.js")
        assert resp.status_code == 200
        assert "javascript" in resp.content_type
        content = resp.data.decode("utf-8")
        assert "ChatStorage" in content
        assert "dpm101_active_conv_id" in content

    def test_js_api_served(self, client: FlaskClient):
        resp = client.get("/js/api.js")
        assert resp.status_code == 200
        assert "javascript" in resp.content_type
        content = resp.data.decode("utf-8")
        assert "ChatApi" in content
        assert "generateRequestId" in content
        assert "isValidConversationId" in content

    def test_js_ui_served(self, client: FlaskClient):
        resp = client.get("/js/ui.js")
        assert resp.status_code == 200
        assert "javascript" in resp.content_type
        content = resp.data.decode("utf-8")
        assert "ChatUI" in content
        assert "renderSafeContent" in content
        assert "createMessageElement" in content

    def test_js_app_served(self, client: FlaskClient):
        resp = client.get("/js/app.js")
        assert resp.status_code == 200
        assert "javascript" in resp.content_type
        content = resp.data.decode("utf-8")
        assert "handleSubmitMessage" in content
        assert "openStatsModal" in content


# ── 2. API Contract & Integration Tests ───────────────────────────────────────

class TestFrontendApiIntegration:
    """Verify backend API endpoints as consumed by the frontend client."""

    def test_health_check_returns_200(self, client: FlaskClient):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert data["service"] == "DPM-101 Helpdesk Chatbot"

    def test_conversation_lifecycle_integration(self, client: FlaskClient):
        # 1. Create Conversation
        post_resp = client.post("/api/v1/conversations", json={})
        assert post_resp.status_code == 201
        conv_data = post_resp.get_json()
        conv_id = conv_data["id"]
        assert uuid.UUID(conv_id)  # valid UUID v4
        assert conv_data["status"] == "active"

        # 2. List Conversations
        list_resp = client.get("/api/v1/conversations?limit=10&offset=0")
        assert list_resp.status_code == 200
        list_data = list_resp.get_json()
        assert any(c["id"] == conv_id for c in list_data["conversations"])

        # 3. Get Specific Conversation
        get_resp = client.get(f"/api/v1/conversations/{conv_id}")
        assert get_resp.status_code == 200
        assert get_resp.get_json()["id"] == conv_id

        # 4. Delete Conversation
        del_resp = client.delete(f"/api/v1/conversations/{conv_id}")
        assert del_resp.status_code == 200
        assert del_resp.get_json()["success"] is True

        # 5. Subsequent Get Returns 404
        post_del_resp = client.get(f"/api/v1/conversations/{conv_id}")
        assert post_del_resp.status_code == 404

    def test_message_send_and_history_retrieval(self, client: FlaskClient):
        # Create conversation
        conv = client.post("/api/v1/conversations", json={}).get_json()
        conv_id = conv["id"]

        req_id = str(uuid.uuid4())
        msg_payload = {
            "message": "What is the passing score for DPM-101?",
            "request_id": req_id,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": req_id,
        }

        # Send message
        send_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json=msg_payload,
            headers=headers,
        )
        assert send_resp.status_code == 200
        data = send_resp.get_json()
        assert data["conversation_id"] == conv_id
        assert "message_id" in data
        assert data["role"] == "assistant"
        assert "60%" in data["content"] or "passing" in data["content"].lower()
        assert isinstance(data["sources"], list)
        assert data["is_duplicate"] is False

        # Retrieve message history
        hist_resp = client.get(f"/api/v1/conversations/{conv_id}/messages")
        assert hist_resp.status_code == 200
        hist_data = hist_resp.get_json()
        assert hist_data["conversation_id"] == conv_id
        messages = hist_data["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "What is the passing score for DPM-101?"
        assert messages[0]["sources"] is None
        assert messages[1]["role"] == "assistant"
        assert messages[1]["sources"] is not None

    def test_duplicate_request_idempotent_replay(self, client: FlaskClient):
        conv = client.post("/api/v1/conversations", json={}).get_json()
        conv_id = conv["id"]

        req_id = str(uuid.uuid4())
        payload = {"message": "When is the final exam?", "request_id": req_id}
        headers = {"X-Request-ID": req_id}

        # First request
        resp1 = client.post(f"/api/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
        assert resp1.status_code == 200
        data1 = resp1.get_json()
        assert data1["is_duplicate"] is False

        # Duplicate replay (same request_id and payload)
        resp2 = client.post(f"/api/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
        assert resp2.status_code == 200
        assert resp2.headers.get("X-Duplicate-Request") == "true"
        data2 = resp2.get_json()
        assert data2["is_duplicate"] is True
        assert data2["message_id"] == data1["message_id"]

    def test_request_id_header_body_mismatch_returns_400(self, client: FlaskClient):
        conv = client.post("/api/v1/conversations", json={}).get_json()
        conv_id = conv["id"]

        payload = {"message": "Test question", "request_id": "client-id-001"}
        headers = {"X-Request-ID": "client-id-002"}

        resp = client.post(f"/api/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["code"] == "MISMATCHED_REQUEST_ID"

    def test_invalid_request_id_syntax_returns_400(self, client: FlaskClient):
        conv = client.post("/api/v1/conversations", json={}).get_json()
        conv_id = conv["id"]

        # Disallowed characters like spaces or symbols
        invalid_id = "bad id with spaces!"
        payload = {"message": "Test question", "request_id": invalid_id}
        headers = {"X-Request-ID": invalid_id}

        resp = client.post(f"/api/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["code"] == "INVALID_REQUEST_ID"

    def test_message_to_nonexistent_conversation_returns_404(self, client: FlaskClient):
        nonexistent_id = str(uuid.uuid4())
        payload = {"message": "Hello?"}

        resp = client.post(f"/api/v1/conversations/{nonexistent_id}/messages", json=payload)
        assert resp.status_code == 404
        data = resp.get_json()
        assert "Conversation not found" in data["error"]

    def test_stats_endpoint_schema_and_counters(self, client: FlaskClient):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.get_json()

        expected_keys = {
            "total_requests",
            "cache_hits",
            "gemini_calls",
            "errors",
            "uptime_seconds",
            "worker_note",
        }
        assert set(data.keys()) == expected_keys
        assert isinstance(data["total_requests"], int)
        assert isinstance(data["cache_hits"], int)
        assert isinstance(data["gemini_calls"], int)
        assert isinstance(data["errors"], int)
        assert isinstance(data["uptime_seconds"], int)
        assert data["worker_note"] == "Counters reflect the current worker process only. Reset on restart."

        # Non-GET methods return 405
        post_resp = client.post("/api/stats", json={})
        assert post_resp.status_code == 405

    def test_unsupported_topic_response_contract(self, client: FlaskClient):
        """Verify that unsupported questions return is_supported: false and clear explanation."""
        conv = client.post("/api/v1/conversations", json={}).get_json()
        conv_id = conv["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Can I get a refund if I drop DPM-101?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_supported"] is False
        assert "not specify" in data["content"].lower() or "refund" in data["content"].lower()

    def test_message_validation_bounds(self, client: FlaskClient):
        """Verify empty message and oversized message return 400 Bad Request."""
        conv = client.post("/api/v1/conversations", json={}).get_json()
        conv_id = conv["id"]

        # Empty message
        resp1 = client.post(f"/api/v1/conversations/{conv_id}/messages", json={"message": "   "})
        assert resp1.status_code == 400
        assert "cannot be empty" in resp1.get_json()["error"].lower()

        # Message > 4000 characters
        oversized = "a" * 4001
        resp2 = client.post(f"/api/v1/conversations/{conv_id}/messages", json={"message": oversized})
        assert resp2.status_code == 400
        assert "exceeds maximum" in resp2.get_json()["error"].lower()

    def test_rate_limiting_429_response_contract(self, client: FlaskClient, app: Flask):
        """Verify 429 returns Retry-After header and retry_after integer."""
        limiter = app.extensions.get("rate_limiter")
        if not limiter:
            pytest.skip("Rate limiter not configured")

        conv = client.post("/api/v1/conversations", json={}).get_json()
        conv_id = conv["id"]

        # Exhaust rate limit directly on the limiter
        client_ip = "127.0.0.1"
        for _ in range(25):
            limiter.check_and_consume(client_ip, conv_id)

        # Next message should trigger 429
        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Should be rate limited"},
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        data = resp.get_json()
        assert data["code"] == "RATE_LIMITED"
        assert isinstance(data["retry_after"], int)
        assert data["retry_after"] > 0


# ── 3. Frontend Security Audit (Static Code Inspection) ───────────────────────

class TestFrontendSecurityAudit:
    """Verify strict XSS protections and absence of innerHTML on untrusted content."""

    def test_dom_markup_has_all_required_landmarks_and_roles(self):
        with open("frontend/index.html", "r", encoding="utf-8") as f:
            html = f.read()

        assert 'role="banner"' in html
        assert 'role="main"' in html
        assert 'role="log"' in html
        assert 'aria-live="polite"' in html
        assert 'id="stats-dialog"' in html
        assert 'id="rate-limit-banner"' in html
        assert 'id="recovery-banner"' in html
        assert 'id="message-input"' in html
        assert 'id="btn-send"' in html
        assert 'id="char-counter"' in html

    def test_js_api_exports_and_methods(self):
        with open("frontend/js/api.js", "r", encoding="utf-8") as f:
            js = f.read()

        methods = [
            "checkHealth",
            "createConversation",
            "listConversations",
            "getConversation",
            "deleteConversation",
            "getMessages",
            "sendMessage",
            "getStats",
            "generateRequestId",
            "isValidConversationId",
        ]
        for m in methods:
            assert m in js, f"Missing ChatApi method: {m}"

    def test_no_inner_html_on_untrusted_content_in_ui_js(self):
        with open("frontend/js/ui.js", "r", encoding="utf-8") as f:
            code = f.read()

        # Check renderSafeContent implementation uses safe DOM nodes
        assert "replaceChildren" in code or "removeChild" in code
        assert "document.createTextNode" in code
        assert "document.createElement" in code

        # Verify no direct innerHTML assignments on dynamic assistant or user content
        dangerous_patterns = [
            r"contentDiv\.innerHTML\s*=",
            r"bubble\.innerHTML\s*=",
            r"row\.innerHTML\s*=",
            r"container\.innerHTML\s*=",
            r"insertAdjacentHTML",
        ]
        for pat in dangerous_patterns:
            matches = re.findall(pat, code)
            assert len(matches) == 0, f"Found forbidden innerHTML/insertAdjacentHTML pattern: {pat}"

    def test_no_api_keys_in_frontend_files(self):
        import os
        frontend_dir = "frontend"
        for root, _, files in os.walk(frontend_dir):
            for file in files:
                if file.endswith((".html", ".js", ".css")):
                    path = os.path.join(root, file)
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    assert "AIzaSy" not in content, f"Potential Gemini API key leak found in {path}"
                    assert "GEMINI_API_KEY" not in content or "GEMINI_API_KEY" in path
