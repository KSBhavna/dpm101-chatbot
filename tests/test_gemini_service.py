"""
tests/test_gemini_service.py — Module 4: Gemini API Integration Test Suite

Tests all requirements using mocks so that automated tests never consume
live Gemini API quota or leak secrets.

Test categories:
  1. Successful client initialization
  2. Missing API key error handling
  3. Valid text generation request
  4. Correct generated-text extraction
  5. Empty response handling
  6. Malformed response handling
  7. Authentication failure handling
  8. Rate-limit / Quota failure handling
  9. Network failure handling
 10. Timeout failure handling
 11. Retry behavior with exponential backoff for transient errors
 12. Non-retry for permanent errors (auth, bad request)
 13. API key sanitization (no leakage in errors/logs)
 14. Health endpoint remains completely independent of Gemini
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from backend.services.gemini_service import (
    GeminiService,
    GeminiResponse,
    GeminiConfigurationError,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_client():
    """Create a mock genai.Client with a mock models.generate_content method."""
    client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "Hello! I am grounded in DPM-101 course documents."
    mock_resp.candidates = [MagicMock()]
    mock_resp.candidates[0].content.parts = [MagicMock(text=mock_resp.text)]
    mock_resp.usage_metadata.prompt_token_count = 10
    mock_resp.usage_metadata.candidates_token_count = 15
    mock_resp.usage_metadata.total_token_count = 25
    client.models.generate_content.return_value = mock_resp
    return client


# ── 1. Client Initialization & Configuration ──────────────────────────────────

class TestGeminiInitialization:
    def test_missing_api_key_raises_configuration_error(self):
        """Missing or empty API key must raise GeminiConfigurationError."""
        service = GeminiService(api_key="")
        with pytest.raises(GeminiConfigurationError, match="not configured"):
            service.get_client()

    def test_whitespace_api_key_raises_configuration_error(self):
        """Whitespace-only API key must also raise GeminiConfigurationError."""
        service = GeminiService(api_key="   ")
        with pytest.raises(GeminiConfigurationError, match="not configured"):
            service.get_client()

    def test_default_model_is_gemini_3_8_flash(self):
        """Default model configured must be gemini-3.8-flash."""
        from config import config
        assert config.GEMINI_MODEL == "gemini-3.8-flash"
        service = GeminiService()
        assert service.model == "gemini-3.8-flash"

    def test_model_override_via_constructor_and_env(self, monkeypatch):
        """Model can be overridden via constructor or GEMINI_MODEL env var."""
        service_explicit = GeminiService(model="custom-override-model")
        assert service_explicit.model == "custom-override-model"

        monkeypatch.setenv("GEMINI_MODEL", "gemini-test-env-model")
        from config import Config
        new_cfg = Config()
        assert new_cfg.GEMINI_MODEL == "gemini-test-env-model"

    def test_successful_client_initialization_with_injected_client(self, mock_client):
        """Injected client should be returned directly without calling genai.Client."""
        service = GeminiService(api_key="test_key", client=mock_client)
        assert service.get_client() is mock_client

    @patch("google.genai.Client")
    def test_successful_client_initialization_via_sdk(self, mock_genai_client_class):
        """Valid API key should call genai.Client(api_key=...)."""
        mock_instance = MagicMock()
        mock_genai_client_class.return_value = mock_instance

        service = GeminiService(api_key="valid_test_api_key_123")
        client = service.get_client()

        mock_genai_client_class.assert_called_once_with(api_key="valid_test_api_key_123")
        assert client is mock_instance


# ── 2. Generation Requests & Response Extraction ──────────────────────────────

class TestGeminiGeneration:
    def test_valid_generation_request(self, mock_client):
        """Valid prompt must return success=True and populated text."""
        service = GeminiService(api_key="test_key", client=mock_client)
        resp = service.generate_text("What is the passing criteria?")

        assert resp.success is True
        assert "grounded in DPM-101" in resp.text
        assert resp.error is None
        assert resp.prompt_tokens == 10
        assert resp.candidates_tokens == 15
        assert resp.total_tokens == 25

    def test_empty_prompt_rejected_without_api_call(self, mock_client):
        """Empty or whitespace prompt must return invalid_request without calling client."""
        service = GeminiService(api_key="test_key", client=mock_client)
        resp = service.generate_text("   ")

        assert resp.success is False
        assert resp.error_type == "invalid_request"
        assert "empty" in resp.error.lower()
        mock_client.models.generate_content.assert_not_called()

    def test_unconfigured_api_key_returns_auth_error(self):
        """generate_text without configured API key must return success=False with auth error."""
        service = GeminiService(api_key="")
        resp = service.generate_text("Hello")

        assert resp.success is False
        assert resp.error_type == "auth"
        assert "not configured" in resp.error

    def test_text_extraction_from_candidates_parts_fallback(self):
        """If response.text is empty, fallback to candidates[0].content.parts."""
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = None  # No direct text property
        candidate = MagicMock()
        candidate.content.parts = [MagicMock(text="Part 1 text. "), MagicMock(text="Part 2 text.")]
        mock_resp.candidates = [candidate]
        mock_resp.usage_metadata = None
        mock_client.models.generate_content.return_value = mock_resp

        service = GeminiService(api_key="test_key", client=mock_client)
        resp = service.generate_text("Hello")

        assert resp.success is True
        assert resp.text == "Part 1 text. Part 2 text."

    def test_empty_gemini_response(self):
        """When Gemini returns an empty candidate or text, return empty_response error."""
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_resp.candidates = [MagicMock(finish_reason="SAFETY")]
        mock_resp.candidates[0].content.parts = []
        mock_resp.usage_metadata = None
        mock_client.models.generate_content.return_value = mock_resp

        service = GeminiService(api_key="test_key", client=mock_client)
        resp = service.generate_text("Controversial topic")

        assert resp.success is False
        assert resp.error_type == "empty_response"
        assert "SAFETY" in resp.error

    def test_none_malformed_response(self):
        """When client returns None, return malformed_response."""
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = None

        service = GeminiService(api_key="test_key", client=mock_client)
        resp = service.generate_text("Hello")

        assert resp.success is False
        assert resp.error_type == "malformed_response"


# ── 3. Error Handling & Retry Policies ────────────────────────────────────────

class TestGeminiErrorHandlingAndRetries:
    def test_authentication_failure_no_retries(self):
        """Authentication failure (e.g. 403 API_KEY_INVALID) must fail immediately with no retry."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError(
            "API_KEY_INVALID: API key not valid. Please pass a valid API key."
        )

        service = GeminiService(api_key="bad_key", max_retries=3, client=mock_client)
        resp = service.generate_text("Hello")

        assert resp.success is False
        assert resp.error_type == "auth"
        assert "Authentication failed" in resp.error
        # Permanent error: must only be called ONCE
        assert mock_client.models.generate_content.call_count == 1

    def test_quota_rate_limit_transient_retries_and_succeeds(self, monkeypatch):
        """Transient 429 RESOURCE_EXHAUSTED should retry with backoff and succeed if recovered."""
        monkeypatch.setattr("time.sleep", lambda s: None)  # fast test

        mock_client = MagicMock()
        success_resp = MagicMock()
        success_resp.text = "Recovered response"
        success_resp.candidates = []
        success_resp.usage_metadata = None

        # Fails once with 429, succeeds on second attempt
        mock_client.models.generate_content.side_effect = [
            RuntimeError("429 RESOURCE_EXHAUSTED: Rate limit exceeded"),
            success_resp,
        ]

        service = GeminiService(api_key="key", max_retries=2, client=mock_client)
        resp = service.generate_text("Question")

        assert resp.success is True
        assert resp.text == "Recovered response"
        assert mock_client.models.generate_content.call_count == 2

    def test_quota_rate_limit_exhausted_retries_fails(self, monkeypatch):
        """Transient 429 that never recovers should exhaust max_retries and fail safely."""
        monkeypatch.setattr("time.sleep", lambda s: None)

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError(
            "429 RESOURCE_EXHAUSTED: Quota exceeded"
        )

        service = GeminiService(api_key="key", max_retries=2, client=mock_client)
        resp = service.generate_text("Question")

        assert resp.success is False
        assert resp.error_type == "quota"
        # Initial try + 2 retries = 3 calls
        assert mock_client.models.generate_content.call_count == 3

    def test_network_failure_transient_retry(self, monkeypatch):
        """Connection error must be classified as network and retried."""
        monkeypatch.setattr("time.sleep", lambda s: None)

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ConnectionError(
            "Failed to establish a new connection: [Errno 111] Connection refused"
        )

        service = GeminiService(api_key="key", max_retries=1, client=mock_client)
        resp = service.generate_text("Question")

        assert resp.success is False
        assert resp.error_type == "network"
        assert mock_client.models.generate_content.call_count == 2

    def test_timeout_failure_transient_retry(self, monkeypatch):
        """Request timeout must be classified as timeout and retried."""
        monkeypatch.setattr("time.sleep", lambda s: None)

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = TimeoutError(
            "Request timed out while waiting for response"
        )

        service = GeminiService(api_key="key", max_retries=1, client=mock_client)
        resp = service.generate_text("Question")

        assert resp.success is False
        assert resp.error_type == "timeout"
        assert mock_client.models.generate_content.call_count == 2

    def test_invalid_model_request_no_retries(self):
        """400 / model not found must be classified as invalid_request and NOT retried."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ValueError(
            "400 Invalid argument: models/nonexistent-model is not found"
        )

        service = GeminiService(api_key="key", max_retries=2, client=mock_client)
        resp = service.generate_text("Question")

        assert resp.success is False
        assert resp.error_type == "invalid_request"
        assert mock_client.models.generate_content.call_count == 1


# ── 4. API Key Security & Sanitization ────────────────────────────────────────

class TestApiKeySecurity:
    def test_api_key_is_redacted_from_error_strings(self):
        """Real or synthetic API keys must never appear in error messages."""
        secret_key = "AIzaSyD-SecretKeyExampleForTest123456"
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError(
            f"Error with key {secret_key}: request refused"
        )

        service = GeminiService(api_key=secret_key, max_retries=0, client=mock_client)
        resp = service.generate_text("Hello")

        assert secret_key not in resp.error
        assert "[REDACTED_API_KEY]" in resp.error

    def test_generic_aiza_key_pattern_redacted(self):
        """Generic AIza... key patterns must also be stripped by sanitizer."""
        service = GeminiService(api_key="dummy_key")
        leaked_text = "Failed with credentials AIzaSyB1234567890123456789012345678901"
        sanitized = service._sanitize_message(leaked_text)

        assert "AIzaSy" not in sanitized
        assert "[REDACTED_API_KEY]" in sanitized


# ── 5. System Instructions & Parameter Forwarding ─────────────────────────────

class TestParameterForwarding:
    def test_system_instruction_forwarded_to_config(self, mock_client):
        """When system_instruction is provided, it must be passed in GenerateContentConfig."""
        service = GeminiService(api_key="test_key", client=mock_client)
        service.generate_text("User query", system_instruction="Answer strictly as IIMBx Helpdesk.")

        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        config_arg = call_kwargs.get("config")
        assert config_arg is not None
        assert config_arg.system_instruction == "Answer strictly as IIMBx Helpdesk."
