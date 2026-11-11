/**
 * api.js — REST API Client Module
 *
 * Module 10: Frontend Chat Interface
 *
 * Responsibilities:
 *   - Encapsulate all 8 verified backend API endpoints.
 *   - Generate request IDs satisfying `^[a-zA-Z0-9_\-]{1,128}$` (RFC 4122 v4 UUID).
 *   - Ensure `X-Request-ID` header and body `request_id` are strictly identical.
 *   - Support reusing the same request ID for retries of the same logical turn.
 *   - Validate conversation IDs against standard UUID v4 format.
 *   - Parse HTTP 429 `Retry-After` headers and `retry_after` JSON fields.
 *   - Normalize errors without leaking sensitive stack traces or internal details.
 */

(function (window) {
  'use strict';

  var CONV_UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  var REQUEST_ID_REGEX = /^[a-zA-Z0-9_\-]{1,128}$/;

  /**
   * Custom ApiError containing HTTP status, error code, and retry-after duration.
   */
  function ApiError(message, status, code, retryAfter, data) {
    this.name = 'ApiError';
    this.message = message || 'An unexpected server error occurred';
    this.status = typeof status === 'number' ? status : 0;
    this.code = code || 'UNKNOWN_ERROR';
    this.retryAfter = typeof retryAfter === 'number' ? retryAfter : null;
    this.data = data || null;
    this.stack = (new Error()).stack;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  /**
   * Validate a conversation ID against standard UUID v4 format.
   * @param {string} id
   * @returns {boolean}
   */
  function isValidConversationId(id) {
    return typeof id === 'string' && CONV_UUID_REGEX.test(id);
  }

  /**
   * Generate an RFC 4122 v4 UUID string that strictly satisfies `^[a-zA-Z0-9_\-]{1,128}$`.
   * Uses native crypto.randomUUID() when available, with high-entropy fallback.
   * @returns {string}
   */
  function generateRequestId() {
    if (typeof window.crypto !== 'undefined' && typeof window.crypto.randomUUID === 'function') {
      try {
        var nativeId = window.crypto.randomUUID();
        if (REQUEST_ID_REGEX.test(nativeId)) {
          return nativeId;
        }
      } catch (_) {
        // Fall through to fallback
      }
    }

    // RFC 4122 v4 compliant math fallback (36 chars: hex and hyphens)
    var fallbackId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = (c === 'x') ? r : ((r & 0x3) | 0x8);
      return v.toString(16);
    });

    return fallbackId;
  }

  /**
   * Safe fetch wrapper with JSON serialization and error parsing.
   */
  function request(url, options) {
    var opts = options || {};
    opts.headers = opts.headers || {};

    // Do not set Content-Type on GET requests without body
    if (opts.body && !opts.headers['Content-Type']) {
      opts.headers['Content-Type'] = 'application/json';
    }

    return window.fetch(url, opts)
      .catch(function (networkErr) {
        throw new ApiError(
          'Network connection error. Please verify your internet connection or server status.',
          0,
          'NETWORK_ERROR'
        );
      })
      .then(function (response) {
        var isDuplicateHeader = response.headers.get('X-Duplicate-Request') === 'true';
        var retryAfterHeader = response.headers.get('Retry-After');

        return response.text().then(function (text) {
          var data = null;
          if (text) {
            try {
              data = JSON.parse(text);
            } catch (_) {
              data = { raw: text };
            }
          } else {
            data = {};
          }

          if (!response.ok) {
            var errorMsg = (data && data.error) ? data.error : ('Request failed with status ' + response.status);
            var errorCode = (data && data.code) ? data.code : ('HTTP_' + response.status);
            var retryAfter = null;

            if (data && typeof data.retry_after === 'number') {
              retryAfter = data.retry_after;
            } else if (retryAfterHeader) {
              var parsedHeader = parseInt(retryAfterHeader, 10);
              if (!isNaN(parsedHeader)) {
                retryAfter = parsedHeader;
              }
            }

            throw new ApiError(errorMsg, response.status, errorCode, retryAfter, data);
          }

          // If duplicate header is present, annotate data
          if (isDuplicateHeader && data && typeof data === 'object') {
            data.is_duplicate = true;
          }

          return data;
        });
      });
  }

  var ChatApi = {
    ApiError: ApiError,
    isValidConversationId: isValidConversationId,
    generateRequestId: generateRequestId,

    /**
     * 1. Health check: GET /health
     */
    checkHealth: function () {
      return request('/health', { method: 'GET' });
    },

    /**
     * 2. Create conversation: POST /api/v1/conversations
     */
    createConversation: function () {
      return request('/api/v1/conversations', {
        method: 'POST',
        body: JSON.stringify({}),
      });
    },

    /**
     * 3. List conversations: GET /api/v1/conversations
     */
    listConversations: function (limit, offset) {
      var l = typeof limit === 'number' ? limit : 50;
      var o = typeof offset === 'number' ? offset : 0;
      return request('/api/v1/conversations?limit=' + encodeURIComponent(l) + '&offset=' + encodeURIComponent(o), {
        method: 'GET',
      });
    },

    /**
     * 4. Get conversation metadata: GET /api/v1/conversations/{id}
     */
    getConversation: function (conversationId) {
      if (!isValidConversationId(conversationId)) {
        return Promise.reject(new ApiError('Invalid conversation ID format', 400, 'INVALID_CONVERSATION_ID'));
      }
      return request('/api/v1/conversations/' + encodeURIComponent(conversationId), {
        method: 'GET',
      });
    },

    /**
     * 5. Delete conversation: DELETE /api/v1/conversations/{id}
     */
    deleteConversation: function (conversationId) {
      if (!isValidConversationId(conversationId)) {
        return Promise.reject(new ApiError('Invalid conversation ID format', 400, 'INVALID_CONVERSATION_ID'));
      }
      return request('/api/v1/conversations/' + encodeURIComponent(conversationId), {
        method: 'DELETE',
      });
    },

    /**
     * 6. Get message history: GET /api/v1/conversations/{id}/messages
     */
    getMessages: function (conversationId, limit, offset) {
      if (!isValidConversationId(conversationId)) {
        return Promise.reject(new ApiError('Invalid conversation ID format', 400, 'INVALID_CONVERSATION_ID'));
      }
      var l = typeof limit === 'number' ? limit : 100;
      var o = typeof offset === 'number' ? offset : 0;
      return request(
        '/api/v1/conversations/' + encodeURIComponent(conversationId) + '/messages?limit=' + encodeURIComponent(l) + '&offset=' + encodeURIComponent(o),
        { method: 'GET' }
      );
    },

    /**
     * 7. Send message: POST /api/v1/conversations/{id}/messages
     * Sends identical request ID in X-Request-ID header and body request_id.
     */
    sendMessage: function (conversationId, messageText, requestId) {
      if (!isValidConversationId(conversationId)) {
        return Promise.reject(new ApiError('Invalid conversation ID format', 400, 'INVALID_CONVERSATION_ID'));
      }

      var text = (messageText || '').trim();
      if (!text) {
        return Promise.reject(new ApiError('Message cannot be empty', 400, 'EMPTY_MESSAGE'));
      }
      if (text.length > 4000) {
        return Promise.reject(new ApiError('Message exceeds maximum 4000 characters', 400, 'MESSAGE_TOO_LONG'));
      }

      var reqId = requestId || generateRequestId();

      return request('/api/v1/conversations/' + encodeURIComponent(conversationId) + '/messages', {
        method: 'POST',
        headers: {
          'X-Request-ID': reqId,
        },
        body: JSON.stringify({
          message: text,
          request_id: reqId,
        }),
      });
    },

    /**
     * 8. Operational statistics: GET /api/stats
     * Returns total_requests, cache_hits, gemini_calls, errors, uptime_seconds, worker_note.
     */
    getStats: function () {
      return request('/api/stats', { method: 'GET' });
    },
  };

  // Attach to window namespace
  window.ChatApi = ChatApi;

})(window);
