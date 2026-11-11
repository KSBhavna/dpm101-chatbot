# MODULE 10 — FRONTEND CHAT INTERFACE
## Refined Architecture Analysis and Implementation Plan (Phase 1C)

**Project:** IIMBx DPM-101 Course Helpdesk Chatbot  
**Module:** 10 of 12 (Frontend Chat Interface)  
**Status:** Planning & Architectural Analysis ONLY (Phase 1C Final Plan Refinement)  
**Baseline Test Suite:** 313 / 313 backend tests passing (Modules 1–9 approved)  
**Application Source Changes:** NONE (Planning phase only)  

---

## 1. Executive Summary & Traceability

Module 10 implements the official student-facing **Frontend Chat Interface** for the IIMBx Digital Product Management (DPM-101) Course Helpdesk. The objective is to deliver an accessible, responsive, academic, and secure web interface using pure vanilla HTML5, CSS3, and ES6+ JavaScript that seamlessly connects to the existing Flask backend.

### Traceability to Original Specifications & Approved Modules

| Requirement ID & Source | Original Requirement & Specification | Planned Frontend Implementation | Verification Method |
|---|---|---|---|
| **Roadmap §15 (Module 10)** | `index.html`, `main.css`, `chat.css`, `chat.js`, `ui.js`. Loading states, source citations, follow-up indicator, error display. | Modular HTML/CSS/JS architecture served directly by Flask from `frontend/`. | Browser functional testing across desktop, tablet, and mobile. |
| **Security §11.3 (XSS Defense)** | Safe text rendering via `textContent` or safe DOM text nodes; strictly **no `innerHTML`** insertion of model-generated output. | Safe DOM tree builder that formats paragraphs, bullet lists, and bold text using `document.createElement` and `document.createTextNode`. | Security inspection & automated XSS payload test suite. |
| **Chat Management (Module 6)** | Full lifecycle: conversation creation, switching, history retrieval, deletion, and context-aware follow-ups. | Complete sidebar conversation list, active conversation switching, chronological message feed, and explicit delete action. Conversation IDs follow UUID v4 contract. | E2E functional test of conversation flows. |
| **Request Management (Module 8 & Phase 1C)** | Idempotency keys (`X-Request-ID`), duplicate detection, duplicate replay, and per-IP / per-conversation rate limiting. Backend validates request IDs via `^[a-zA-Z0-9_\-]{1,128}$`. | Frontend generates unique request IDs satisfying `^[a-zA-Z0-9_\-]{1,128}$` (UUID v4 format via `crypto.randomUUID()` or fallback), keeps header and body `request_id` identical, reuses ID on retry, generates fresh ID for new turns, and handles live 429 countdown. | Rapid double-click and simulated duplicate/rate-limit verification. |
| **Statistics & Counters (Module 9)** | Read-only integration of `GET /api/stats` without disrupting chat. | Accessible modal/drawer fetching all 6 operational fields every 30s only while open, with non-interfering graceful degradation. | Stats modal inspection and network traffic analysis. |
| **Policy Accuracy (Module 5)** | Attribution to official documents (Doc 01, Doc 03, Doc 04 §1–§2) and clear unsupported topic rejection. | Dynamic source pills rendered only when returned by backend; multi-source support; distinct styling for unsupported answers. | Visual and schema inspection across official question set. |

---

## 2. Verified Backend API Contracts

Every endpoint below has been verified directly against its active implementation in `backend/routes/chat_routes.py`, `backend/routes/stats_routes.py`, `backend/services/request_service.py`, and `app.py`.

### 2.1 Route Map & Endpoint Specifications

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                             BACKEND API CONTRACT SPECIFICATION                                        │
├────────────────────────────────┬─────────┬─────────────────────────────────────┬───────────────────┬───────────────────┤
│ Endpoint                       │ Method  │ Request Body / Query Params         │ Success Response  │ Error Responses   │
├────────────────────────────────┼─────────┼─────────────────────────────────────┼───────────────────┼───────────────────┤
│ /health                        │ GET     │ None                                │ 200 OK            │ None              │
│ /api/v1/conversations          │ POST    │ None (or empty JSON)                │ 201 Created       │ 500               │
│ /api/v1/conversations          │ GET     │ Query: limit (def 50), offset (0)   │ 200 OK            │ 500               │
│ /api/v1/conversations/{id}     │ GET     │ Path: id (UUID v4)                  │ 200 OK            │ 404, 500          │
│ /api/v1/conversations/{id}     │ DELETE  │ Path: id (UUID v4)                  │ 200 OK            │ 404, 500          │
│ /api/v1/conversations/{id}/msgs│ GET     │ Query: limit (def 100), offset (0)  │ 200 OK            │ 404, 500          │
│ /api/v1/conversations/{id}/msgs│ POST    │ Body: {message, request_id}         │ 200 OK            │ 400, 404, 409,    │
│                                │         │ Header: X-Request-ID                │                   │ 429, 500, 504     │
│ /api/stats                     │ GET     │ None                                │ 200 OK            │ 405, 503          │
└────────────────────────────────┴─────────┴─────────────────────────────────────┴───────────────────┴───────────────────┘
```

### 2.2 Deep Endpoint Specifications

#### 1. System Health Check
- **Route:** `GET /health`
- **Headers:** None
- **Success Response (HTTP 200 OK):**
  ```json
  {
    "status": "healthy",
    "service": "DPM-101 Helpdesk Chatbot"
  }
  ```
- **Error Behavior:** Server down or reverse proxy error (502/503/504).
- **Frontend Use:** Verifies backend connectivity on initial page load; displays subtle status dot (`● Online` / `○ Offline`).

#### 2. Create Conversation
- **Route:** `POST /api/v1/conversations`
- **Request Body:** None (or `{}`)
- **Success Response (HTTP 201 Created):**
  ```json
  {
    "id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d",
    "created_at": "2026-11-11T09:15:30.123456+00:00",
    "updated_at": "2026-11-11T09:15:30.123456+00:00",
    "status": "active"
  }
  ```
- **Error Response:** HTTP 500 `{"error": "Internal server error"}`.
- **Frontend Use:** Called when user clicks "+ New Chat", or on initial visit if no active conversation exists in `localStorage`.

#### 3. List Conversations
- **Route:** `GET /api/v1/conversations?limit={limit}&offset={offset}`
- **Query Parameters:**
  - `limit`: Integer, default `50`, clamped between `1` and `200`.
  - `offset`: Integer, default `0`, clamped $\ge 0$.
- **Success Response (HTTP 200 OK):**
  ```json
  {
    "conversations": [
      {
        "id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d",
        "created_at": "2026-11-11T09:15:30.123456+00:00",
        "updated_at": "2026-11-11T09:20:10.654321+00:00",
        "status": "active"
      }
    ]
  }
  ```
- **Ordering:** Sorted chronologically descending by `updated_at DESC`.
- **Frontend Use:** Populates the sidebar conversation history list.

#### 4. Get Conversation Details
- **Route:** `GET /api/v1/conversations/{conversation_id}`
- **Path Parameter:** `conversation_id` (pre-validated UUID v4 string).
- **Success Response (HTTP 200 OK):**
  ```json
  {
    "id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d",
    "created_at": "2026-11-11T09:15:30.123456+00:00",
    "updated_at": "2026-11-11T09:20:10.654321+00:00",
    "status": "active"
  }
  ```
- **Error Response (HTTP 404 Not Found):**
  ```json
  {
    "error": "Conversation not found"
  }
  ```

#### 5. Delete Conversation
- **Route:** `DELETE /api/v1/conversations/{conversation_id}`
- **Path Parameter:** `conversation_id` (pre-validated UUID v4 string).
- **Success Response (HTTP 200 OK):**
  ```json
  {
    "success": true,
    "message": "Conversation deleted successfully",
    "conversation_id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d"
  }
  ```
- **Error Response (HTTP 404 Not Found):**
  ```json
  {
    "error": "Conversation not found"
  }
  ```
- **Frontend Use:** Triggered by the delete button on sidebar chat items; removes conversation from UI and purges its stored messages in SQLite.

#### 6. Get Message History
- **Route:** `GET /api/v1/conversations/{conversation_id}/messages?limit={limit}&offset={offset}`
- **Query Parameters:**
  - `limit`: Integer, default `100`, clamped between `1` and `500`.
  - `offset`: Integer, default `0`, clamped $\ge 0$.
- **Success Response (HTTP 200 OK):**
  ```json
  {
    "conversation_id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d",
    "messages": [
      {
        "id": "msg-001",
        "conversation_id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d",
        "role": "user",
        "content": "What is the passing score?",
        "created_at": "2026-11-11T09:15:35.000000+00:00",
        "sources": null
      },
      {
        "id": "msg-002",
        "conversation_id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d",
        "role": "assistant",
        "content": "To pass the course, you must achieve an overall weighted score of at least 60% across all assessments, AND score at least 40% in the proctored final exam.",
        "created_at": "2026-11-11T09:15:37.000000+00:00",
        "sources": ["Grading & Late Submission Policy V2 (Current)"]
      }
    ]
  }
  ```
- **Error Response (HTTP 404 Not Found):**
  ```json
  {
    "error": "Conversation not found"
  }
  ```
- **Key Schema Details:**
  - Messages are returned in deterministic chronological order (`created_at ASC, rowid ASC`).
  - User messages have `sources: null`.
  - Assistant messages have `sources: list[str]` (or `null` if no sources were recorded).

#### 7. Send Message & Receive Response
- **Route:** `POST /api/v1/conversations/{conversation_id}/messages`
- **Request Headers:**
  - `Content-Type: application/json`
  - `X-Request-ID: <string>` (optional, 1–128 chars matching `^[a-zA-Z0-9_\-]{1,128}$`)
- **Request Body:**
  ```json
  {
    "message": "What is the penalty for late submission?",
    "request_id": "<string>"
  }
  ```
  *(Backend Rule: The backend validates request IDs against `^[a-zA-Z0-9_\-]{1,128}$`. If both `X-Request-ID` header and body `request_id` are provided, they must be strictly identical. The frontend generates UUID v4 strings, which cleanly satisfy this backend regex.)*
- **Success Response (HTTP 200 OK — Normal Processing):**
  ```json
  {
    "conversation_id": "c1f7a8b2-3e4d-4a1b-9f2c-5d8e7b6a9c0d",
    "message_id": "msg-004",
    "role": "assistant",
    "content": "Late submissions are no longer accepted for weekly quizzes or the capstone case...",
    "sources": ["Grading & Late Submission Policy V2 (Current)"],
    "created_at": "2026-11-11T09:16:10.000000+00:00",
    "is_in_scope": true,
    "is_supported": true,
    "is_duplicate": false
  }
  ```
- **Duplicate Replay Response (HTTP 200 OK — Idempotent Duplicate Replay):**
  - Response Header: `X-Duplicate-Request: true`
  - Response Body: Same structure as above, with `"is_duplicate": true`.
- **Error Responses:**
  - **HTTP 400 Bad Request:**
    - Non-JSON body: `{"error": "Request body must be valid JSON"}`
    - Missing message: `{"error": "Field 'message' is required"}`
    - Empty message: `{"error": "Message cannot be empty or whitespace only"}`
    - Message > 4000 characters: `{"error": "Message exceeds maximum allowed length of 4000 characters"}`
    - Invalid request ID format: `{"error": "...", "code": "INVALID_REQUEST_ID"}` *(when request ID does not match `^[a-zA-Z0-9_\-]{1,128}$`)*
    - Header/body mismatch: `{"error": "...", "code": "MISMATCHED_REQUEST_ID"}` *(when header and body IDs differ)*
  - **HTTP 404 Not Found:**
    - `{"error": "Conversation not found"}` (or deleted)
  - **HTTP 409 Conflict:**
    - In-flight lock: `{"error": "A message is already being processed for this conversation. Please wait.", "code": "CONVERSATION_BUSY"}`
    - Idempotency conflict: `{"error": "...", "code": "IDEMPOTENCY_CONFLICT"}` (same request ID used with different query)
  - **HTTP 429 Too Many Requests (Rate Limited):**
    - Response Header: `Retry-After: <integer-seconds>` (e.g. `15`)
    - Response Body:
      ```json
      {
        "error": "Rate limit exceeded. Please wait before sending another message.",
        "code": "RATE_LIMITED",
        "retry_after": 15
      }
      ```
  - **HTTP 504 Gateway Timeout:**
    - `{"error": "Timed out waiting for in-flight request", "code": "IN_FLIGHT_TIMEOUT"}`
  - **HTTP 500 Internal Server Error:**
    - `{"error": "Internal server error processing message"}`

#### 8. Operational Statistics
- **Route:** `GET /api/stats`
- **Method Restriction:** Strictly `GET` (non-GET returns HTTP 405 Method Not Allowed).
- **Success Response (HTTP 200 OK):**
  ```json
  {
    "total_requests": 142,
    "cache_hits": 58,
    "gemini_calls": 84,
    "errors": 1,
    "uptime_seconds": 18450,
    "worker_note": "Counters reflect the current worker process only. Reset on restart."
  }
  ```
- **Error Response (HTTP 503 Service Unavailable):**
  ```json
  {
    "error": "Failed to retrieve statistics",
    "code": "STATS_UNAVAILABLE"
  }
  ```

---

## 3. Refined Conversation Recovery (Correction 1)

### 3.1 Strict Prohibition of Silent Auto-Resending
The v1 proposal to automatically create a new conversation and resend the user's message upon HTTP 404 is **completely removed**.
- **Reason:** Silent resubmission creates unexpected conversations, risks duplicate billing/processing, and masks server resets or state loss from the student.

### 3.2 Corrected Recovery Flow
When a message submission or message history load returns **HTTP 404**:

```
Student Sends Message
         │
         ▼
POST /conversations/{id}/messages
         │
   HTTP 404 Received
         │
         ├────────────────────────────────────────────────────────────────────────┐
         ▼                                                                        ▼
1. Preserve User Text                                                  2. Display Prominent Banner
   - Keep message intact in input composer                               - Clear, honest alert message:
   - Ensure student does NOT lose their typing                             "This conversation is no longer available
                                                                            on the server. It may have expired or been
                                                                            reset."
                                                                         - Offer explicit action:
                                                                           [Start New Chat] button
         │
         ▼
3. Student Clicks [Start New Chat]
   - Frontend calls POST /api/v1/conversations
   - Saves new conversation ID to state and localStorage
   - Composer retains the student's text, ready for user review and manual send
   - Never implies deleted conversation history can be recovered
```

### 3.3 UI Implementation Details
1. **Preserved Input State:** If `POST /messages` fails with 404:
   - The textarea content is **not** cleared.
   - The composer is re-enabled so the student can edit or keep the text.
2. **Error Banner:**
   ```html
   <div class="alert-banner alert-warning" role="alert">
     <span class="alert-icon">⚠️</span>
     <div class="alert-content">
       <strong>Conversation Unavailable</strong>
       <p>This conversation is no longer active on the server. Your message has been preserved below.</p>
     </div>
     <button type="button" class="btn btn-secondary btn-sm" id="btn-recover-new-chat">Start New Chat</button>
   </div>
   ```
3. **No Silent Network Calls:** No background `createConversation` or automatic message re-dispatch is executed. The user retains full control.

---

## 4. Grounded Source Attribution (Correction 2)

### 4.1 Schema Inspection from Backend
From `backend/services/policy_service.py` (`PolicyAnswer`) and `backend/services/chat_service.py` (`save_assistant_message`):
- `sources` is a Python `list[str]` containing unique document titles extracted from evidence chunks via `_get_unique_sources()`.
- Example authoritative sources:
  - `"Grading & Late Submission Policy V2 (Current)"` (Doc 03)
  - `"Course Handbook & Overview"` (Doc 01)
  - `"Final Exam Rules & Student Forum Excerpts"` (Doc 04 §1–§2)
- Unsupported answers (e.g. questions regarding refund policies, faculty offices, or out-of-scope subjects):
  - `sources` is `[]` (empty list)
  - `is_supported` is `false`
  - `is_in_scope` is `true` or `false`

### 4.2 Rendering Rules

```
                       Assistant Response Received
                                   │
                                   ▼
                       Check `sources` property
                                   │
         ┌─────────────────────────┴─────────────────────────┐
         ▼                                                   ▼
`sources` has 1 or more items                    `sources` is empty / null
         │                                                   │
         ▼                                                   ▼
Render Source Badges Container                 Check `is_supported` flag
For each source in `sources`:                                │
  - Create `<span class="source-badge">`        ┌────────────┴────────────┐
  - Text: `📄 Official Source: ${source}`      ▼                         ▼
                                       is_supported === false    is_supported === true
                                                │                         │
                                                ▼                         ▼
                                       Render Notice:            No badge rendered
                                       "Information not          (graceful omission)
                                       specified in official
                                       course documents."
```

### 4.3 Strict Formatting Standards
1. **Multi-Source Support:** If the backend references multiple documents (e.g. Doc 01 and Doc 03 for a combined question), each source gets its own badge.
2. **No Hardcoded Badges:** The UI never injects a default or fallback source badge if `sources` is empty.
3. **Never Fabricate Citations:** If a document title is not explicitly present in the `sources` array, the frontend will never invent or guess a citation.
4. **Unsupported Notice:** If `is_supported === false`, display a distinct status pill:
   ```html
   <div class="response-unsupported-note" role="note">
     <span class="note-icon">ℹ️</span>
     <span>Information not specified in official course documents.</span>
   </div>
   ```

---

## 5. Frontend Security & Safe Rendering (Correction 4)

### 5.1 Strict XSS Defense (Zero `innerHTML`)
To satisfy Architecture Specification §11.3 without exception:
- **Rule:** The frontend will **never** assign untrusted backend content (assistant responses, user messages, conversation titles, source titles, or error strings) to `element.innerHTML`.
- **Implementation:** All text is inserted via `document.createTextNode` or `element.textContent`.
- **Formatting Engine (`ui.js`):**
  ```javascript
  /**
   * Safely renders structured text into a container DOM element using
   * native DOM nodes only. Eliminates XSS risks completely.
   */
  function renderSafeContent(container, rawText) {
      container.replaceChildren(); // Safe DOM clear
      if (!rawText) return;

      // Split into paragraphs by blank lines
      const paragraphs = rawText.split(/\n\s*\n/);
      for (const para of paragraphs) {
          const trimmed = para.trim();
          if (!trimmed) continue;

          // Check if paragraph is a bullet list
          const lines = trimmed.split('\n');
          const isList = lines.every(l => /^\s*[-*•]\s+/.test(l));

          if (isList) {
              const ul = document.createElement('ul');
              ul.className = 'content-list';
              for (const line of lines) {
                  const li = document.createElement('li');
                  const itemText = line.replace(/^\s*[-*•]\s+/, '');
                  renderInlineFormatting(li, itemText);
                  ul.appendChild(li);
              }
              container.appendChild(ul);
          } else {
              const p = document.createElement('p');
              p.className = 'content-para';
              lines.forEach((line, idx) => {
                  if (idx > 0) p.appendChild(document.createElement('br'));
                  renderInlineFormatting(p, line);
              });
              container.appendChild(p);
          }
      }
  }

  /**
   * Safe inline tokenizer for bold (**text**) without innerHTML.
   */
  function renderInlineFormatting(parent, text) {
      const parts = text.split(/(\*\*[^*]+\*\*)/g);
      for (const part of parts) {
          if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
              const strong = document.createElement('strong');
              strong.textContent = part.slice(2, -2);
              parent.appendChild(strong);
          } else if (part) {
              parent.appendChild(document.createTextNode(part));
          }
      }
  }
  ```

### 5.2 Zero API Key Exposure
- No Gemini API key, secret token, or bearer credential is ever sent to or stored in the browser.
- All requests communicate with local Flask endpoints (`/api/...`).
- Environment variables remain server-side only.

### 5.3 URL Parameter Validation (Conversation IDs vs. Request IDs)
- **Conversation IDs:** Created by the backend via `uuid.uuid4()` and strictly validated against UUID v4 format. Before interpolating a `conversation_id` into a fetch path (e.g. `/api/v1/conversations/${id}/messages`), `api.js` verifies the ID against the standard UUID v4 format:
  ```javascript
  const UUID_V4_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

  function isValidUUID(id) {
      return typeof id === 'string' && UUID_V4_REGEX.test(id);
  }
  ```
  If invalid, the call is rejected immediately on the client side with a validation error, preventing path traversal or malformed URL requests.
- **Request IDs:** Validated separately by the backend via `^[a-zA-Z0-9_\-]{1,128}$` (see Section 6).

---

## 6. Request Management, Idempotency & Rate Limiting (Phase 1C Refinement)

### 6.1 Backend Validation Contract vs. Frontend ID Generation Strategy
- **Backend Validation Contract:** The approved Module 8 backend implementation does **not** mandate UUID v4 specifically for request IDs. Instead, `backend/services/request_service.py` defines:
  ```python
  REQUEST_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
  ```
  The backend accepts any string from 1 to 128 characters composed of ASCII alphanumeric characters, underscores, and hyphens.
- **Frontend ID Strategy:**
  - For high uniqueness, collision resistance, and clean traceability, the frontend generates RFC 4122 v4 UUID strings (e.g. `f47ac10b-58cc-4372-a567-0e02b2c3d479`).
  - Standard UUID v4 strings are 36 characters long and contain only lowercase hexadecimal digits (`0-9`, `a-f`) and hyphens (`-`).
  - Therefore, all generated IDs **cleanly and strictly satisfy the backend validation regex `^[a-zA-Z0-9_\-]{1,128}$`**.

### 6.2 ID Generator with Robust Environment Fallback
To ensure reliable operation across all modern browsers, legacy devices, and non-secure HTTP local testing environments:
```javascript
/**
 * Generate a request ID that satisfies backend regex ^[a-zA-Z0-9_\-]{1,128}$.
 * Generates an RFC 4122 v4 UUID string using native crypto when available,
 * with a high-entropy math fallback for non-secure contexts.
 */
function generateRequestId() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
        try {
            return crypto.randomUUID();
        } catch (_) {
            // Fall through if restricted by browser security policy
        }
    }
    // RFC 4122 v4 compliant math fallback (36 characters: hex and hyphens)
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        const r = (Math.random() * 16) | 0;
        const v = c === 'x' ? r : (r & 0x3) | 0x8;
        return v.toString(16);
    });
}
```

### 6.3 Idempotency Key Consistency, Retries, and New Messages
- **Header & Body Match:** For every message submission, `api.js` includes the request ID in **both**:
  1. HTTP Header: `X-Request-ID: <id>`
  2. JSON Body: `{"message": "...", "request_id": "<id>"}`
  Both values are guaranteed to be identical, preventing `MISMATCHED_REQUEST_ID` errors.
- **Retry Reuse:** If a request fails due to a network drop or transient error, retrying that same logical turn reuses the **exact same `request_id`**. This allows the backend `RequestManager` to safely recognize the retry, coordinate with in-flight tasks, or replay cached results without creating duplicate assistant turns.
- **Fresh Turns:** Every genuinely new message typed by the student generates a brand-new request ID.

### 6.4 Duplicate Replay Deduplication
- When the backend returns a duplicate response:
  - Header: `X-Duplicate-Request: true`
  - Body: `is_duplicate: true`
- The frontend inspects `message_id`:
  - If a message bubble with this `message_id` already exists in the DOM, the UI does **not** append a second bubble.
  - If it is not yet rendered (e.g. page refreshed during in-flight processing), it renders the message with a subtle pill: `⚡ Replayed Response`.

### 6.5 HTTP 429 Rate-Limit Handling (Countdown Timer)
- When the backend responds with HTTP 429:
  - The client parses `retry_after` from the JSON response body (`data.retry_after`) or falls back to parsing the `Retry-After` header as an integer.
  - The UI locks the input composer and starts an active second-by-second countdown timer:
    ```
    ⏳ Rate limit active. Please wait 15 seconds before sending another message.
    ```
  - When the countdown reaches `0`, the timer automatically clears, the warning banner disappears, and the composer is re-enabled.

---

## 7. Live Statistics Integration (Correction 6)

### 7.1 Verified Operational Fields
The live statistics modal fetches `GET /api/stats` and presents the exact 6 fields defined in the approved Module 9 contract:

| Key | Type | Label in UI | Description |
|---|---|---|---|
| `total_requests` | `int` | Total Queries | Total valid chat message requests processed. |
| `cache_hits` | `int` | Cache Hits | Queries resolved instantly from SQLite cache. |
| `gemini_calls` | `int` | AI Invocations | Real Gemini model generation calls made. |
| `errors` | `int` | System Errors | Server or model error count. |
| `uptime_seconds` | `int` | Server Uptime | Formatted as `Xh Ym Zs` or `Xd Xh`. |
| `worker_note` | `str` | Operational Note | Displayed verbatim: *"Counters reflect the current worker process only. Reset on restart."* |

### 7.2 Non-Interference Architecture
1. **Isolated Execution:** Stats fetching executes independently via `api.fetchStats()`. It never shares state with the chat message pipeline or conversation store.
2. **Open-Only Polling:**
   - Stats are fetched only when the user opens the stats modal/drawer.
   - An interval polls every 30 seconds **only** while the modal is visible.
   - If the modal is closed or the document becomes hidden (`document.visibilityState !== 'visible'`), the interval is immediately cleared via `clearInterval()`.
3. **Graceful Degradation:**
   - If `/api/stats` returns HTTP 503 or network fails, an inline message is shown inside the modal:  
     `"Statistics temporarily unavailable"`.
   - The error is isolated to the modal; it never triggers alerts or affects chat functionality.

---

## 8. Frontend Component & File Architecture

```
dpm101-chatbot/
└── frontend/
    ├── index.html              # Semantic HTML5 markup, ARIA landmarks, dialogs
    ├── css/
    │   ├── main.css            # Base styles, color variables, typography, responsive grid
    │   └── chat.css            # Message feed, bubbles, sources, composer, stats modal
    └── js/
        ├── api.js              # Verified REST client (handles all 8 endpoints, request IDs, regex validation)
        ├── storage.js          # LocalStorage manager (active conversation + preferences)
        ├── ui.js               # Safe DOM renderer (zero innerHTML), auto-scroll, modals
        └── app.js              # Application controller, event orchestration, keyboard bindings
```

### 8.1 Detailed File Responsibilities

1. **`frontend/index.html`**:
   - Header with course title, health status indicator, `[Live Stats]` button, and `[+ New Chat]` button.
   - Collapsible sidebar with list of conversations and delete buttons.
   - Main message feed with `role="log"` and `aria-live="polite"`.
   - Composer form with expanding `<textarea>`, character counter (`0 / 4000`), and `[Send]` button.
   - Accessible `<dialog id="stats-modal">` for operational statistics.
2. **`frontend/css/main.css`**:
   - IIMBx academic color system (`#0B2545` deep navy, `#C59B27` gold accent, `#F4F7FB` background).
   - Responsive layout rules (desktop 2-column grid, tablet collapsible sidebar, mobile overlay drawer).
   - High-contrast focus rings (`:focus-visible`) and reduced motion rules (`prefers-reduced-motion`).
3. **`frontend/css/chat.css`**:
   - Distinct styling for user bubbles (navy, right-aligned) and assistant bubbles (soft blue-gray, left-aligned).
   - Source badge cards, unsupported topic notes, and duplicate replay badges.
   - Three-dot pulsing typing indicator animation.
   - Countdown rate-limit warning banner and error toast alerts.
4. **`frontend/js/api.js`**:
   - Encapsulates `fetch` calls to `/health`, `/api/v1/conversations`, `/api/v1/conversations/{id}/messages`, and `/api/stats`.
   - Generates request IDs conforming to `^[a-zA-Z0-9_\-]{1,128}$` (UUID v4 format).
   - Normalizes errors and extracts `retry_after` from 429 responses.
   - Validates conversation ID format (UUID v4) before interpolating into URLs.
5. **`frontend/js/storage.js`**:
   - Stores and retrieves `dpm101_active_conv_id` from `localStorage`.
   - Provides safe in-memory fallback if `localStorage` throws `SecurityError` (e.g. restricted private browsing).
6. **`frontend/js/ui.js`**:
   - Implements `renderSafeContent()` with strict DOM text node construction.
   - Manages auto-scroll behavior (maintains position if user has scrolled up to read earlier messages).
   - Controls dialog open/close states and live countdown timers.
7. **`frontend/js/app.js`**:
   - Initializes application on `DOMContentLoaded`.
   - Checks backend health.
   - Restores active conversation or shows welcome screen with suggested questions.
   - Binds `Enter` to send, `Shift+Enter` for newline.
   - Implements explicit conversation recovery on 404 without silent resending.

---

## 9. Implementation Checklist (Phase 2 Preview)

| Task ID | Task Description | Target File | Verification Method |
|---|---|---|---|
| **T10.1** | Create semantic HTML structure with ARIA roles, landmarks, sidebar, message feed, and stats dialog. | `frontend/index.html` | W3C validator & accessibility audit |
| **T10.2** | Build modern, accessible CSS design system: variables, reset, layout grid, typography, focus styles. | `frontend/css/main.css` | Multi-resolution layout inspection |
| **T10.3** | Build chat-specific CSS: message bubbles, source pills, typing animation, drawer, toast alerts. | `frontend/css/chat.css` | Visual component inspection |
| **T10.4** | Implement REST API client module wrapping all backend endpoints with error normalization & request IDs (`^[a-zA-Z0-9_\-]{1,128}$`). | `frontend/js/api.js` | Endpoint integration tests |
| **T10.5** | Implement LocalStorage state persistence for active conversation ID with private browsing fallback. | `frontend/js/storage.js` | Storage unit tests |
| **T10.6** | Implement safe DOM rendering engine (safe text formatting, no `innerHTML`, auto-scroll, modals). | `frontend/js/ui.js` | Automated XSS payload test suite |
| **T10.7** | Implement main controller: event handlers, lifecycle management, keyboard bindings, 404 recovery. | `frontend/js/app.js` | End-to-end conversation flow test |
| **T10.8** | Create frontend static serving and integration test suite. | `tests/test_frontend_integration.py` | Pytest execution (`pytest tests/test_frontend_integration.py`) |
| **T10.9** | Execute complete regression suite across Modules 1–10 (target: 313 + new frontend tests passing). | Full Test Run | Pytest execution (`pytest -v`) |
| **T10.10**| Update `README.md` with frontend usage instructions, responsive layout guide, and screenshots. | `README.md` | Documentation review |

---

## 10. Acceptance Criteria Matrix

| Criterion | Requirement Description | Verification Method | Pass Condition |
|---|---|---|---|
| **AC10.1** | Interface is cleanly branded for IIMBx DPM-101 Course Helpdesk and responsive across mobile, tablet, and desktop. | Chrome DevTools device mode (375px, 768px, 1280px) | Layout adapts cleanly with zero horizontal scroll overflow. |
| **AC10.2** | Conversation management: user can create, list, switch, and delete conversations. Conversation IDs verified via UUID v4 regex. | Manual browser testing & automated test | All conversation actions succeed and update the DOM accurately. |
| **AC10.3** | Safe Text Rendering: No model-generated or user content is rendered via `innerHTML`. | Automated XSS injection test (`<script>`, `onerror`, `<iframe>`) | Malicious payloads render as harmless plain text. |
| **AC10.4** | Grounded Source Attribution: Badges render only when returned by backend; supports multi-sources; flags unsupported topics. | Test queries against Doc 01, Doc 03, Doc 04, and unsupported topics | Source badges match backend `sources` array exactly; no fabricated badges. |
| **AC10.5** | Conversation Recovery: On HTTP 404, user message is preserved in composer, explicit notice is shown, and explicit New Chat button is offered (no silent resending). | Simulated 404 response | User message remains intact; student is not stranded. |
| **AC10.6** | Idempotency & Rate Limiting: Request IDs satisfy `^[a-zA-Z0-9_\-]{1,128}$` (UUID v4 format); header & body match; retries reuse ID; new turns get fresh ID; duplicate bubble deduplication; 429 live countdown timer. | Rapid send clicks and simulated 429 response | Single turn sent; live countdown decrements and re-enables composer at 0. |
| **AC10.7** | Live Statistics: Modal presents exact 6 fields from `/api/stats` without disrupting ongoing chats; polls only while open. | Open modal, inspect network tab, verify 6 fields | 6 fields match schema; polling stops when modal is closed. |
| **AC10.8** | Regression Baseline: All 313 existing backend tests continue to pass without modification. | `pytest -v` | 313 / 313 tests pass. |

---

## 11. Confirmation of Planning Integrity

- **NO application source code files have been created or modified.**
- **NO backend routes, services, or models have been altered.**
- **NO dependencies or packages have been installed.**
- **The existing backend test suite remains 100% passing at 313 / 313 tests.**
- **This phase is strictly limited to architectural planning and specification refinement.**

---

**STOPPING HERE. Awaiting your review and explicit approval of Phase 1C before beginning Phase 2 (Implementation).**
