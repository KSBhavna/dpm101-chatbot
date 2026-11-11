"""
backend/routes/chat_routes.py — Flask Blueprint for Chat API Endpoints

Module 6: Chat Management & Conversation History
Endpoints:
  - POST   /api/v1/conversations                     -> Create conversation
  - GET    /api/v1/conversations                     -> List active conversations
  - GET    /api/v1/conversations/<conversation_id>   -> Get conversation metadata
  - GET    /api/v1/conversations/<conversation_id>/messages -> Get message history
  - POST   /api/v1/conversations/<conversation_id>/messages -> Send user message & get response
  - DELETE /api/v1/conversations/<conversation_id>   -> Delete conversation & messages
"""

from __future__ import annotations

import logging
from typing import Optional, Any
from flask import Blueprint, request, jsonify, current_app, Request

from backend.services.chat_service import ChatService
from backend.services.rate_limiter import SlidingWindowRateLimiter
from backend.services.request_service import (
    RequestManager,
    InvalidRequestIdError,
    MismatchedRequestIdError,
    IdempotencyConflictError,
    ConversationBusyError,
)
from backend.services.stats_service import get_stats_service

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__, url_prefix="/api/v1")

# Module-level service holders, overridable in tests or app factory
_service_instance: Optional[ChatService] = None
_rate_limiter_instance: Optional[SlidingWindowRateLimiter] = None
_request_manager_instance: Optional[RequestManager] = None


def get_chat_service() -> ChatService:
    """
    Get the configured ChatService instance.
    Prefers current_app.extensions if available, then _service_instance, or creates default.
    """
    global _service_instance
    if current_app:
        app_service = current_app.extensions.get("chat_service")
        if app_service:
            return app_service
    if _service_instance is None:
        _service_instance = ChatService()
    return _service_instance


def set_chat_service(service: Optional[ChatService]) -> None:
    """Explicitly inject a ChatService instance (useful for unit tests)."""
    global _service_instance
    _service_instance = service


def get_rate_limiter() -> SlidingWindowRateLimiter:
    """Get the configured SlidingWindowRateLimiter instance."""
    global _rate_limiter_instance
    if current_app:
        app_limiter = current_app.extensions.get("rate_limiter")
        if app_limiter:
            return app_limiter
    if _rate_limiter_instance is None:
        _rate_limiter_instance = SlidingWindowRateLimiter()
    return _rate_limiter_instance


def set_rate_limiter(limiter: Optional[SlidingWindowRateLimiter]) -> None:
    """Explicitly inject a SlidingWindowRateLimiter instance (useful for unit tests)."""
    global _rate_limiter_instance
    _rate_limiter_instance = limiter


def get_request_manager() -> RequestManager:
    """Get the configured RequestManager instance."""
    global _request_manager_instance
    if current_app:
        app_mgr = current_app.extensions.get("request_manager")
        if app_mgr:
            return app_mgr
    if _request_manager_instance is None:
        _request_manager_instance = RequestManager()
    return _request_manager_instance


def set_request_manager(manager: Optional[RequestManager]) -> None:
    """Explicitly inject a RequestManager instance (useful for unit tests)."""
    global _request_manager_instance
    _request_manager_instance = manager


def get_client_ip(req: Request) -> str:
    """
    Safely extract client IP behind trusted reverse proxy (Render).
    Render is configured with TRUSTED_PROXIES_COUNT = 1.
    Render appends the genuine remote client IP to the end of X-Forwarded-For.
    To prevent IP spoofing attacks that bypass rate limiting, we select the IP
    at index -TRUSTED_PROXIES_COUNT (the rightmost entry added by Render's trusted proxy boundary).
    """
    from config import config
    trusted_count = getattr(config, "TRUSTED_PROXIES_COUNT", 1)
    forwarded = req.headers.get("X-Forwarded-For")
    if forwarded and trusted_count > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= trusted_count:
            return parts[-trusted_count]
        elif parts:
            return parts[0]
    return req.remote_addr or "127.0.0.1"


def _parse_limit(raw_val: Any, default: int, max_limit: int) -> int:
    """Parse limit parameter safely, clamping between 1 and max_limit."""
    if raw_val is None:
        return default
    try:
        val = int(raw_val)
        return max(1, min(val, max_limit))
    except (ValueError, TypeError):
        return default


def _parse_offset(raw_val: Any) -> int:
    """Parse offset parameter safely, clamping to >= 0."""
    if raw_val is None:
        return 0
    try:
        val = int(raw_val)
        return max(0, val)
    except (ValueError, TypeError):
        return 0


# ── Conversation Lifecycle Endpoints ──────────────────────────────────────────

@chat_bp.route("/conversations", methods=["POST"])
def create_conversation():
    """
    Create a new conversation.
    Returns:
      HTTP 201 with {id, created_at, updated_at, status}
    """
    service = get_chat_service()
    conv = service.create_conversation()
    return jsonify(conv), 201


@chat_bp.route("/conversations", methods=["GET"])
def list_conversations():
    """
    List conversations ordered by most recently updated.
    Query params:
      - limit: int (default 50, range 1 to 200)
      - offset: int (default 0, min 0)
    """
    service = get_chat_service()
    limit = _parse_limit(request.args.get("limit"), default=50, max_limit=200)
    offset = _parse_offset(request.args.get("offset"))
    convs = service.list_conversations(limit=limit, offset=offset)
    return jsonify({"conversations": convs}), 200


@chat_bp.route("/conversations/<conversation_id>", methods=["GET"])
def get_conversation_details(conversation_id: str):
    """
    Get conversation metadata.
    Returns:
      HTTP 200 with {id, created_at, updated_at, status}
      HTTP 404 if not found or deleted.
    """
    service = get_chat_service()
    conv = service.get_conversation(conversation_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify(conv), 200


@chat_bp.route("/conversations/<conversation_id>", methods=["DELETE"])
def delete_conversation(conversation_id: str):
    """
    Delete a conversation and all its messages.
    Returns:
      HTTP 200 with success confirmation
      HTTP 404 if conversation not found or already deleted
    """
    service = get_chat_service()
    success = service.delete_conversation(conversation_id)
    if not success:
        return jsonify({"error": "Conversation not found"}), 404
    return jsonify({
        "success": True,
        "message": "Conversation deleted successfully",
        "conversation_id": conversation_id,
    }), 200


# ── Message History & Sending Endpoints ────────────────────────────────────────

@chat_bp.route("/conversations/<conversation_id>/messages", methods=["GET"])
def get_messages(conversation_id: str):
    """
    Get message history for a conversation in chronological order.
    Returns:
      HTTP 200 with {conversation_id, messages: [...]}
      HTTP 404 if conversation does not exist or is deleted
    """
    service = get_chat_service()
    conv = service.get_conversation(conversation_id)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    limit = _parse_limit(request.args.get("limit"), default=100, max_limit=500)
    offset = _parse_offset(request.args.get("offset"))
    messages = service.get_conversation_messages(conversation_id, limit=limit, offset=offset)
    return jsonify({
        "conversation_id": conversation_id,
        "messages": messages,
    }), 200


@chat_bp.route("/conversations/<conversation_id>/messages", methods=["POST"])
def send_message(conversation_id: str):
    """
    Send a message within an existing conversation and receive the assistant response.
    Supports Module 8 Request Management:
      - Validates JSON, message content, length bounds, and idempotency key.
      - Enforces per-IP and per-conversation sliding-window rate limiting.
      - Serializes concurrent turns per conversation with a 5.0s bounded lock timeout.
      - Atomically coordinates identical in-flight requests (Thundering Herd protection).
      - Replays completed responses for duplicates with X-Duplicate-Request: true.
      - Never consumes rate limit quota for legitimate duplicate replays.
    """
    service = get_chat_service()
    rate_limiter = get_rate_limiter()
    request_mgr = get_request_manager()

    # 1. Validate JSON
    if not request.is_json:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        return jsonify({"error": "Request body must be a valid JSON object"}), 400

    # 2. Validate message presence and type
    if "message" not in data:
        return jsonify({"error": "Field 'message' is required"}), 400

    raw_message = data["message"]
    if not isinstance(raw_message, str):
        return jsonify({"error": "Field 'message' must be a string"}), 400

    clean_message = raw_message.strip()
    if not clean_message:
        return jsonify({"error": "Message cannot be empty or whitespace only"}), 400

    if len(clean_message) > 4000:
        return jsonify({"error": "Message exceeds maximum allowed length of 4000 characters"}), 400

    # 3. Validate idempotency keys (header vs body consistency and format)
    header_key = request.headers.get("X-Request-ID")
    body_key = data.get("request_id")
    try:
        resolved_key = RequestManager.resolve_and_validate_keys(header_key, body_key)
    except MismatchedRequestIdError as m_err:
        return jsonify({"error": str(m_err), "code": "MISMATCHED_REQUEST_ID"}), 400
    except InvalidRequestIdError as inv_err:
        return jsonify({"error": str(inv_err), "code": "INVALID_REQUEST_ID"}), 400

    # 4. Validate conversation status (fail fast if non-existent or deleted)
    is_active, err_msg = service.verify_conversation_active(conversation_id)
    if not is_active:
        return jsonify({"error": err_msg or "Conversation not found"}), 404

    # Module 9: Count valid incoming message request
    get_stats_service().increment_requests()

    # 5. Check idempotency store and coordinate in-flight requests (atomic check-and-register)
    try:
        req_key, is_duplicate, replayed_payload, wait_target = request_mgr.start_request(
            conversation_id=conversation_id,
            raw_query=clean_message,
            client_request_id=resolved_key,
        )
    except IdempotencyConflictError as conflict_err:
        return jsonify({
            "error": str(conflict_err),
            "code": "IDEMPOTENCY_CONFLICT",
        }), 409

    # 6. If it is a completed duplicate: replay immediately without consuming rate limit or DB writes
    if is_duplicate and replayed_payload is not None:
        payload_copy = dict(replayed_payload)
        payload_copy["is_duplicate"] = True
        resp = jsonify(payload_copy)
        resp.headers["X-Duplicate-Request"] = "true"
        return resp, 200

    # 7. If it is an in-flight duplicate: wait on primary thread (no extra rate limit consumed)
    if is_duplicate and wait_target is not None:
        try:
            completed_payload = request_mgr.wait_for_in_flight(wait_target, timeout=35.0)
            payload_copy = dict(completed_payload)
            payload_copy["is_duplicate"] = True
            resp = jsonify(payload_copy)
            resp.headers["X-Duplicate-Request"] = "true"
            return resp, 200
        except TimeoutError:
            return jsonify({
                "error": "Timed out waiting for in-flight request",
                "code": "IN_FLIGHT_TIMEOUT",
            }), 504
        except ConversationBusyError as busy_err:
            return jsonify({
                "error": str(busy_err),
                "code": "CONVERSATION_BUSY",
            }), 409
        except ValueError as val_err:
            return jsonify({"error": str(val_err)}), 404
        except Exception as exc:
            get_stats_service().increment_errors()
            logger.exception("In-flight request failed: %s", exc)
            return jsonify({"error": "Internal server error processing message"}), 500

    # 8. For genuinely NEW requests: check rate limits
    client_ip = get_client_ip(request)
    allowed, rate_err, retry_after = rate_limiter.check_and_consume(client_ip, conversation_id)
    if not allowed:
        request_mgr.fail_request(req_key, Exception("Rate limit exceeded"))
        resp = jsonify({
            "error": "Rate limit exceeded. Please wait before sending another message.",
            "code": "RATE_LIMITED",
            "retry_after": retry_after,
        })
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    # 9. Acquire per-conversation lock with bounded timeout
    lock_acquired = request_mgr.acquire_conversation_lock(conversation_id)
    if not lock_acquired:
        request_mgr.fail_request(req_key, ConversationBusyError("Conversation busy"))
        return jsonify({
            "error": "A message is already being processed for this conversation. Please wait.",
            "code": "CONVERSATION_BUSY",
        }), 409

    # 10. Execute message processing pipeline under lock
    try:
        # Re-verify conversation active (in case it was deleted while waiting for the lock)
        is_active_lock, err_lock = service.verify_conversation_active(conversation_id)
        if not is_active_lock:
            val_err = ValueError(err_lock or "Conversation not found")
            request_mgr.fail_request(req_key, val_err)
            return jsonify({"error": err_lock or "Conversation not found"}), 404

        # Recheck duplicate state after lock acquisition to prevent race conditions
        completed_after_lock = request_mgr.get_completed_response(req_key)
        if completed_after_lock is not None:
            payload_copy = dict(completed_after_lock)
            payload_copy["is_duplicate"] = True
            resp = jsonify(payload_copy)
            resp.headers["X-Duplicate-Request"] = "true"
            return resp, 200

        response_payload = service.process_message(conversation_id, clean_message)
        response_payload["is_duplicate"] = False
        request_mgr.complete_request(req_key, response_payload)
        return jsonify(response_payload), 200
    except ValueError as val_err:
        request_mgr.fail_request(req_key, val_err)
        return jsonify({"error": str(val_err)}), 404
    except Exception as exc:
        request_mgr.fail_request(req_key, exc)
        get_stats_service().increment_errors()
        logger.exception("Error processing message in conversation %s: %s", conversation_id, exc)
        return jsonify({"error": "Internal server error processing message"}), 500
    finally:
        request_mgr.release_conversation_lock(conversation_id)


# ── Cache Management Endpoints (Module 7) ─────────────────────────────────────

@chat_bp.route("/cache/stats", methods=["GET"])
def get_cache_stats():
    """
    Get cache runtime statistics and capacity metrics.
    Returns:
      HTTP 200 with cache statistics JSON
    """
    service = get_chat_service()
    if hasattr(service, "cache_service") and service.cache_service:
        stats = service.cache_service.get_stats()
        return jsonify(stats), 200
    return jsonify({"error": "Cache service not configured"}), 503


@chat_bp.route("/cache/cleanup", methods=["POST"])
def cleanup_cache():
    """
    Manually trigger cleanup of expired cache entries.
    Returns:
      HTTP 200 with cleanup count
    """
    service = get_chat_service()
    if hasattr(service, "cache_service") and service.cache_service:
        cleaned = service.cache_service.cleanup_expired()
        return jsonify({"success": True, "cleaned_entries": cleaned}), 200
    return jsonify({"error": "Cache service not configured"}), 503
    return jsonify({"error": "Cache service not configured"}), 503
