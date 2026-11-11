"""
tests/test_health.py — Module 2: Health endpoint tests

Uses Flask's built-in test client (no live server needed).
Run with:  pytest tests/ -v
"""

import pytest
from app import create_app


@pytest.fixture
def client():
    """
    Create a fresh Flask test client for each test function.
    TESTING=True makes Flask propagate exceptions instead of swallowing them.
    """
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


class TestHealthEndpoint:
    """Tests for GET /health"""

    def test_returns_http_200(self, client):
        """Health endpoint must return HTTP 200 OK."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_content_type_is_json(self, client):
        """Response must be JSON."""
        response = client.get("/health")
        assert response.content_type == "application/json"

    def test_status_field_is_healthy(self, client):
        """Response body must contain status = 'healthy'."""
        response = client.get("/health")
        data = response.get_json()
        assert data is not None, "Response body is not valid JSON"
        assert data["status"] == "healthy"

    def test_service_field_matches(self, client):
        """Response body must identify the service correctly."""
        response = client.get("/health")
        data = response.get_json()
        assert data["service"] == "DPM-101 Helpdesk Chatbot"

    def test_full_response_structure(self, client):
        """Response must contain exactly the expected keys and values."""
        response = client.get("/health")
        data = response.get_json()
        expected = {
            "status": "healthy",
            "service": "DPM-101 Helpdesk Chatbot",
        }
        assert data == expected

    def test_health_does_not_accept_post(self, client):
        """Health endpoint is GET only; POST must return 405 Method Not Allowed."""
        response = client.post("/health")
        assert response.status_code == 405
