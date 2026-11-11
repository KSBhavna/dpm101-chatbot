"""
backend/services/gemini_service.py — Dedicated Gemini API Integration Service

Module 4: Gemini API Integration
Integrates Google's official Gemini API using the official `google-genai` SDK (`google.genai`).

Key responsibilities:
  - Initialize the Gemini client securely from configuration.
  - Expose `generate_text()` to send prompts and receive grounded text answers.
  - Extract text cleanly and safely from responses.
  - Handle transient errors (e.g. rate limits, 503 server errors) with exponential backoff retries.
  - Fail immediately without retries on non-transient errors (invalid API key, 400 Bad Request).
  - Never leak or log API keys or secrets in logs, exceptions, or error responses.
  - Return structured, sanitized results via `GeminiResponse`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional, Any
from backend.services.stats_service import get_stats_service

logger = logging.getLogger(__name__)


@dataclass
class GeminiResponse:
    """
    Standard response object returned by GeminiService.

    success: True if text was generated successfully; False on error.
    text: Generated text string (empty on failure).
    model: Model name used for generation.
    error: Sanitized error description if success=False; None otherwise.
    error_type: Classification of error ('auth', 'quota', 'timeout', 'network', 'invalid_request', 'unknown').
    prompt_tokens: Prompt token count if provided by provider metadata.
    candidates_tokens: Generated candidate token count if provided.
    total_tokens: Total token count if provided.
    """
    success: bool
    text: str = ""
    model: str = ""
    error: Optional[str] = None
    error_type: Optional[str] = None
    prompt_tokens: Optional[int] = None
    candidates_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class GeminiConfigurationError(ValueError):
    """Raised when Gemini configuration (e.g. missing API key) is invalid."""


class GeminiService:
    """
    Encapsulates interaction with the Google Gemini API using google-genai SDK.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
        request_timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        client: Optional[Any] = None,
    ) -> None:
        """
        Initialize GeminiService.

        If parameters are omitted, values are pulled from config.py.
        An explicit `client` can be injected for testing.
        """
        # Delay importing config to avoid circular imports
        from config import config

        self.api_key = api_key if api_key is not None else config.GEMINI_API_KEY
        self.model = model if model is not None else config.GEMINI_MODEL
        self.temperature = (
            temperature if temperature is not None else config.GEMINI_TEMPERATURE
        )
        self.max_output_tokens = (
            max_output_tokens if max_output_tokens is not None else config.GEMINI_MAX_OUTPUT_TOKENS
        )
        self.request_timeout = (
            request_timeout if request_timeout is not None else config.GEMINI_REQUEST_TIMEOUT
        )
        self.max_retries = (
            max_retries if max_retries is not None else config.GEMINI_MAX_RETRIES
        )

        self._client = client

    def get_client(self) -> Any:
        """
        Get or initialize the google.genai.Client instance.

        Raises:
            GeminiConfigurationError if API key is missing or empty.
        """
        if self._client is not None:
            return self._client

        if not self.api_key or not self.api_key.strip():
            raise GeminiConfigurationError(
                "Gemini API key is not configured. Set GEMINI_API_KEY in your environment or .env."
            )

        try:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
            return self._client
        except Exception as e:
            sanitized = self._sanitize_message(str(e))
            logger.error("Failed to initialize Gemini Client: %s", sanitized)
            raise RuntimeError(f"Failed to initialize Gemini Client: {sanitized}") from e

    def generate_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = None,
        max_output_tokens: Optional[int] = None,
    ) -> GeminiResponse:
        """
        Send a text generation request to Gemini with retry logic for transient errors.

        Args:
            prompt: User/context prompt text to send.
            system_instruction: Optional system instruction for grounding / persona.
            temperature: Optional override for temperature.
            max_output_tokens: Optional override for max output tokens.

        Returns:
            GeminiResponse object containing success, text, and sanitized error info.
        """
        if not prompt or not prompt.strip():
            return GeminiResponse(
                success=False,
                model=self.model,
                error="Prompt cannot be empty.",
                error_type="invalid_request",
            )

        try:
            client = self.get_client()
        except GeminiConfigurationError as cfg_err:
            return GeminiResponse(
                success=False,
                model=self.model,
                error=str(cfg_err),
                error_type="auth",
            )
        except Exception as init_err:
            return GeminiResponse(
                success=False,
                model=self.model,
                error=self._sanitize_message(str(init_err)),
                error_type="unknown",
            )

        # Build GenerateContentConfig
        eff_temp = temperature if temperature is not None else self.temperature
        eff_tokens = max_output_tokens if max_output_tokens is not None else self.max_output_tokens

        try:
            from google.genai import types
            config_kwargs: dict[str, Any] = {
                "temperature": eff_temp,
                "max_output_tokens": eff_tokens,
            }
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction

            config_obj = types.GenerateContentConfig(**config_kwargs)
        except Exception as cfg_build_err:
            return GeminiResponse(
                success=False,
                model=self.model,
                error=f"Failed to build configuration: {self._sanitize_message(str(cfg_build_err))}",
                error_type="invalid_request",
            )

        retries = 0
        backoff_delay = 1.0

        while True:
            try:
                get_stats_service().increment_gemini_calls()

                response = client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config_obj,
                )
                return self._parse_response(response)

            except Exception as exc:
                is_transient, error_type, clean_msg = self._classify_exception(exc)

                if is_transient and retries < self.max_retries:
                    retries += 1
                    logger.warning(
                        "Gemini API transient failure (attempt %d/%d, retrying in %.1fs): %s",
                        retries, self.max_retries, backoff_delay, clean_msg,
                    )
                    time.sleep(backoff_delay)
                    backoff_delay *= 2.0
                    continue

                logger.error(
                    "Gemini API error (error_type=%s, permanent or retries exhausted): %s",
                    error_type, clean_msg,
                )
                return GeminiResponse(
                    success=False,
                    model=self.model,
                    error=clean_msg,
                    error_type=error_type,
                )

    def _parse_response(self, response: Any) -> GeminiResponse:
        """
        Safely extract text and metadata from google.genai GenerateContentResponse.
        """
        if response is None:
            return GeminiResponse(
                success=False,
                model=self.model,
                error="Empty response received from Gemini API.",
                error_type="malformed_response",
            )

        # Attempt standard .text property
        generated_text = ""
        try:
            if hasattr(response, "text") and response.text:
                generated_text = response.text
            elif hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, "content") and hasattr(candidate.content, "parts"):
                    parts_text = [
                        p.text for p in candidate.content.parts if hasattr(p, "text") and p.text
                    ]
                    generated_text = "".join(parts_text)
        except Exception as parse_err:
            logger.warning("Error parsing Gemini response candidates: %s", parse_err)
            generated_text = ""

        # Extract usage metadata if available
        prompt_tokens = None
        candidates_tokens = None
        total_tokens = None

        if hasattr(response, "usage_metadata") and response.usage_metadata:
            meta = response.usage_metadata
            prompt_tokens = getattr(meta, "prompt_token_count", None)
            candidates_tokens = getattr(meta, "candidates_token_count", None)
            total_tokens = getattr(meta, "total_token_count", None)

        if not generated_text or not generated_text.strip():
            # Check for safety ratings / finish reason
            finish_reason = "empty"
            if hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                finish_reason = getattr(candidate, "finish_reason", "empty")

            return GeminiResponse(
                success=False,
                model=self.model,
                error=f"Gemini returned an empty response (finish_reason: {finish_reason}).",
                error_type="empty_response",
                prompt_tokens=prompt_tokens,
                candidates_tokens=candidates_tokens,
                total_tokens=total_tokens,
            )

        return GeminiResponse(
            success=True,
            text=generated_text.strip(),
            model=self.model,
            prompt_tokens=prompt_tokens,
            candidates_tokens=candidates_tokens,
            total_tokens=total_tokens,
        )

    def _classify_exception(self, exc: Exception) -> tuple[bool, str, str]:
        """
        Classify an exception into (is_transient, error_type, sanitized_message).
        """
        msg = str(exc)
        repr_str = repr(exc)
        lower = f"{msg} {repr_str}".lower()
        sanitized = self._sanitize_message(msg or repr_str)

        # 1. Authentication / Permission errors (Non-transient)
        if any(w in lower for w in ("api_key_invalid", "api key not valid", "unauthenticated", "permission_denied", "invalid api key")):
            return False, "auth", f"Authentication failed: {sanitized}"

        # 2. Quota / Rate limit (Transient if rate limit, Non-transient if hard quota exceeded)
        if any(w in lower for w in ("resource_exhausted", "quota exceeded", "rate limit", "429")):
            return True, "quota", f"Gemini quota or rate limit exceeded: {sanitized}"

        # 3. Timeouts (Transient)
        if any(w in lower for w in ("timeout", "timed out", "deadline_exceeded", "connecttimeouterror")):
            return True, "timeout", f"Request timed out: {sanitized}"

        # 4. Network / Connection errors (Transient)
        if any(w in lower for w in ("connection refused", "connection error", "connection reset", "broken pipe", "failed to establish a new connection", "remotedisconnected")):
            return True, "network", f"Network connectivity error: {sanitized}"

        # 5. Service unavailable / Server errors (Transient)
        if any(w in lower for w in ("503", "502", "504", "unavailable", "server error", "internal server error")):
            return True, "server_error", f"Gemini server error: {sanitized}"

        # 6. Invalid request / bad model (Non-transient)
        if any(w in lower for w in ("400", "invalid_argument", "model not found", "not supported for model")):
            return False, "invalid_request", f"Invalid request or model configuration: {sanitized}"

        # Default fallback (Non-transient to prevent infinite loops)
        return False, "unknown", f"Gemini API request failed: {sanitized}"

    def _sanitize_message(self, text: str) -> str:
        """
        Remove API key or sensitive credentials from error strings.
        """
        if not text:
            return ""
        sanitized = text
        if self.api_key and len(self.api_key) > 5:
            sanitized = sanitized.replace(self.api_key, "[REDACTED_API_KEY]")
        # Redact generic Google API key patterns (AIzaSy...)
        sanitized = re.sub(r"AIza[0-9A-Za-z\-_]{30,}", "[REDACTED_API_KEY]", sanitized)
        return sanitized
