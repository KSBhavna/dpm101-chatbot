# IIMBx DPM-101 Course Helpdesk Chatbot

A context-aware chatbot that answers student questions strictly using official
DPM-101 course documents. Built as a Technical Internship project (Module 2 of 12).

---

## Project Overview

| Component | Technology |
|---|---|
| Frontend | HTML, CSS, JavaScript |
| Backend | Python Flask |
| AI | Google Gemini (`google-genai` SDK) |
| Retrieval | TF-IDF (RAG pattern) |
| Database | SQLite |
| Deployment | Render |

The chatbot answers questions **only from four official course PDFs** — it does not
use Gemini's general knowledge.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11 or higher |
| pip | bundled with Python |
| Git | any recent version |

---

## Setup

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd dpm101-chatbot
```

### 2. Create and activate the virtual environment

**Windows (PowerShell)**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**macOS / Linux**
```bash
python3 -m venv venv
source venv/bin/activate
```

You should see `(venv)` in your prompt when it is active.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Open `.env` and fill in your values:

```env
GEMINI_API_KEY=your_actual_api_key_here
GEMINI_MODEL=gemini-3.8-flash
FLASK_DEBUG=True
FLASK_SECRET_KEY=any-long-random-string
```

> **Important:** Never commit `.env` to Git. It is excluded by `.gitignore`.

---

## Running the Application

```bash
python app.py
```

The Flask development server starts on `http://localhost:5000`.

---

## Testing the Health Endpoint

**Using curl**
```bash
curl http://localhost:5000/health
```

**Expected response (HTTP 200)**
```json
{
  "status": "healthy",
  "service": "DPM-101 Helpdesk Chatbot"
}
```

**Using pytest** (Flask test client — no server needed)
```bash
pytest tests/ -v
```

---

## Project Structure

```
dpm101-chatbot/
├── app.py               # Flask application factory + /health endpoint
├── config.py            # Configuration via python-dotenv
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template (safe to commit)
├── .gitignore
├── README.md
│
├── backend/             # Application logic (implemented in later modules)
│   ├── __init__.py
│   ├── routes/          # Flask blueprints
│   ├── services/        # Business logic (Gemini, cache, retrieval, session)
│   └── utils/           # PDF parser, ingestion, TF-IDF index, text utilities
│
├── frontend/            # Static files served by Flask
│   ├── index.html       # Chat UI (implemented in Module 10)
│   ├── css/
│   └── js/
│
├── data/                # Runtime SQLite database (gitignored, rebuilt on startup)
│   └── .gitkeep
│
└── tests/               # pytest test suite
    └── test_health.py
```

---

## Module Roadmap

| Module | Title | Status |
|---|---|---|
| 1 | Project Setup & Architecture | ✅ Complete |
| 2 | Environment & Repository Setup | ✅ Complete |
| 3 | Course Document Management | ✅ Complete |
| 4 | Gemini API Integration | ✅ Complete |
| 5 | Policy Management & Answer Accuracy | ✅ Complete |
| 6 | Chat Management & Conversation History | ✅ Complete |
| 7 | SQLite Caching System | ✅ Complete |
| 8 | Request Management & Duplicate Prevention | ✅ Complete |
| 9 | Statistics & Live Counters | ✅ Complete |
| 10 | Frontend Chat Interface | ⬜ Planned |
| 11 | Integration, Testing & Error Handling | ⬜ Planned |
| 12 | Deployment & Final Submission | ⬜ Planned |

---

## Module 6: Chat Management & Conversation History

Module 6 adds persistent conversation management, message history, and context-aware follow-up question handling backed by SQLite.

### 1. Database Schema

The database uses SQLite with WAL mode and foreign-key constraints enabled:

#### Table `conversations`
| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | UUID string identifying the conversation |
| `created_at` | `TEXT` | `NOT NULL` | ISO 8601 UTC creation timestamp |
| `updated_at` | `TEXT` | `NOT NULL` | ISO 8601 UTC last activity timestamp |
| `status` | `TEXT` | `NOT NULL DEFAULT 'active'` | `'active'` or `'deleted'` |

#### Table `messages`
| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | UUID string identifying the message |
| `conversation_id` | `TEXT` | `NOT NULL, FK` | Foreign key referencing `conversations(id) ON DELETE CASCADE` |
| `role` | `TEXT` | `NOT NULL, CHECK` | Role: `'user'` or `'assistant'` |
| `content` | `TEXT` | `NOT NULL` | Message text content |
| `created_at` | `TEXT` | `NOT NULL` | ISO 8601 UTC creation timestamp |
| `sources` | `TEXT` | `NULL` | JSON-serialized array of document sources (assistant only) |

#### Indexes
- `idx_messages_conversation_created` on `messages(conversation_id, created_at ASC)`
- `idx_conversations_updated` on `conversations(updated_at DESC)`

---

### 2. Chat API Endpoints

All chat endpoints use the `/api/v1` prefix.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/conversations` | Create a new active conversation. |
| `GET` | `/api/v1/conversations` | List active conversations ordered by `updated_at DESC`. |
| `GET` | `/api/v1/conversations/{id}` | Get metadata for a specific conversation. |
| `GET` | `/api/v1/conversations/{id}/messages` | Retrieve messages for a conversation in chronological order. |
| `POST` | `/api/v1/conversations/{id}/messages` | Send a user message and receive the policy-grounded assistant reply. |
| `DELETE` | `/api/v1/conversations/{id}` | Delete a conversation and purge its message history. |

---

### 3. Example Request & Response

#### Send Message
`POST /api/v1/conversations/4a1c6e1e-2895-46aa-a4d1-419b48f654b1/messages`

**Headers:**
`Content-Type: application/json`

**Request Body:**
```json
{
  "message": "What is the passing criteria?"
}
```

**Response (HTTP 200 OK):**
```json
{
  "conversation_id": "4a1c6e1e-2895-46aa-a4d1-419b48f654b1",
  "message_id": "e2c36c1d-10fb-4d32-9cb1-c30d95955a02",
  "role": "assistant",
  "content": "To pass the course and earn the Verified Certificate, you must achieve an overall weighted score of at least 60% across all assessments, AND score at least 40% in the Proctored Final Exam separately.\n\nSource: Grading Policy V2.",
  "sources": [
    "Grading & Late Submission Policy V2 (Current)"
  ],
  "created_at": "2026-11-10T22:50:00.000000+00:00",
  "is_in_scope": true,
  "is_supported": true
}
```

---

### 4. Context-Aware Follow-Up Handling

The system inspects recent conversation turns (up to the configured context window limit, default 6 messages) to disambiguate follow-up queries:
- **Pronoun Resolution**: `"How long is it?"` after discussing the final exam resolves to `"What is the duration of the final exam?"` and answers 90 minutes.
- **Assignment Context**: `"Can I submit it one day late?"` after discussing the capstone resolves to capstone late submission rules.
- **Ambiguity Detection**: If a student asks a vague question (*"What about the deadline?"*) when multiple topics (quizzes and capstone) were discussed, the chatbot prompts the user for clarification.
- **Contamination Prevention**: Self-contained queries never inherit unrelated context from prior turns.
- **Policy Invariance**: Old assistant answers in chat history are treated as conversation context only, never as authoritative policy documents.

---

---

### 5. Module 7: SQLite-Based Query & Answer Caching System

The chatbot incorporates a persistent SQLite caching subsystem (`backend/services/cache_service.py`) designed to reduce repeated Gemini API calls, eliminate latency for frequent questions, and strictly preserve policy accuracy.

#### Table `query_cache`
| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | UUID string identifying the cache entry |
| `cache_key` | `TEXT` | `UNIQUE NOT NULL` | Composite SHA-256 hash |
| `normalized_query` | `TEXT` | `NOT NULL` | Standardized lowercase query without trailing punctuation |
| `answer` | `TEXT` | `NOT NULL` | Policy-validated answer text |
| `sources` | `TEXT` | `NOT NULL` | JSON-serialized array of official source document titles |
| `created_at` | `TEXT` | `NOT NULL` | ISO 8601 UTC creation timestamp |
| `expires_at` | `TEXT` | `NOT NULL` | ISO 8601 UTC expiration timestamp |
| `last_accessed_at` | `TEXT` | `NOT NULL` | ISO 8601 UTC timestamp of last cache hit |
| `access_count` | `INTEGER` | `NOT NULL DEFAULT 1` | Cumulative counter of cache lookups/hits |
| `confidence` | `REAL` | `DEFAULT 1.0` | Confidence score |
| `is_in_scope` | `INTEGER` | `NOT NULL DEFAULT 1` | 1 if question is relevant to course |
| `is_supported` | `INTEGER` | `NOT NULL DEFAULT 1` | 1 if answer is grounded in official docs |
| `model_used` | `TEXT` | `NULL` | Model identifier used for generation |
| `prompt_tokens` | `INTEGER` | `NULL` | Token counts from generation metadata |
| `candidates_tokens` | `INTEGER` | `NULL` | Token counts from generation metadata |
| `total_tokens` | `INTEGER` | `NULL` | Token counts from generation metadata |

#### Indexes
- `idx_query_cache_key` UNIQUE on `query_cache(cache_key)`
- `idx_query_cache_expires` on `query_cache(expires_at)`
- `idx_query_cache_accessed` on `query_cache(last_accessed_at DESC)`

#### Composite SHA-256 Key Strategy
The composite cache key is generated from four independent dimensions:
1. **Normalized Query**: Lowercase, stripped punctuation, normalized whitespace.
2. **Document Fingerprint**: SHA-256 combination of registered document IDs, versions, and file hashes. Any change to official documents invalidates stale keys.
3. **Policy Version**: Prompt version and policy rule revision (`v1`, `v2`).
4. **Model Configuration**: Target model (`gemini-3.8-flash`) and temperature.

$$\text{Key} = \text{SHA-256}(\text{"query:"} + Q_{\text{norm}} + \text{"|docs:"} + \text{FP}_{\text{docs}} + \text{"|policy:"} + V_{\text{policy}} + \text{"|model:"} + M_{\text{config}})$$

#### Policy & Context Safety Rules
- **No Context-Dependent Caching**: Answers derived from conversation pronouns ("How long is it?") are never cached as standalone answers.
- **No Clarification Caching**: Ambiguity prompts are never cached.
- **No Fallback / Error Caching**: Extractive fallback answers and temporary provider errors are never stored.
- **No Out-of-Scope / Unsupported Caching**: Off-topic rejections and unsupported topic messages are never cached.
- **Fault-Tolerant Degrade**: Any SQLite read/write failure degrades gracefully to a cache miss; chat requests never fail due to cache errors.

---

### 6. Known Limitations & Ephemeral Storage Disclosure

- **SQLite Ephemeral Storage**: On free cloud platforms like Render, local files (including SQLite databases) are ephemeral and reset whenever the container restarts or re-deploys. Persistent production storage requires an external volume mount or a managed PostgreSQL database.
- **Identifier vs. Authentication**: Conversation IDs are unique operational identifiers, not proof of user identity or authenticated session ownership. Authentication is not implemented in this module.
- **Context Window**: Follow-up resolution operates over the most recent 6 messages (`MAX_CONTEXT_MESSAGES = 6`) to protect processing latency and prevent context drift.

---

## Module 8: Request Management & Duplicate Prevention

Module 8 adds in-process sliding-window rate limiting, per-conversation turn serialization, explicit idempotency key support, rapid double-click deduplication, and in-flight thundering herd suppression.

### 1. Key Features

- **Sliding-Window Rate Limiting (`rate_limiter.py`)**:
  - Enforces 20 requests per 60 seconds per client IP and 15 requests per 60 seconds per conversation using high-precision `time.monotonic()` timestamps.
  - Returns `HTTP 429 Too Many Requests` with an accurate `Retry-After` header when thresholds are reached.
  - Safe proxy client IP extraction supporting Render's `X-Forwarded-For` proxy header.
- **Duplicate Prevention & Idempotency (`request_service.py`)**:
  - Supports client-supplied `X-Request-ID` header and body `request_id` (300s TTL).
  - Automatically fingerprints rapid submissions within 10 seconds: `SHA-256(conversation_id + "::" + normalized_query)`.
  - Replays completed responses directly from memory with `is_duplicate: true` and `X-Duplicate-Request: true` without inserting redundant database rows.
  - Duplicate retries do not consume rate limit quota.
  - Intentional repeated questions asked after the 10-second deduplication window are processed normally as fresh turns.
- **Turn Serialization & Concurrency Control**:
  - Per-conversation thread locks ensure turns do not interleave within the same conversation.
  - Bounded 5.0-second lock acquisition timeout returns `HTTP 409 Conflict` (`CONVERSATION_BUSY`) rather than blocking indefinitely.
  - In-flight identical requests coordinate using condition variables (`threading.Condition`), eliminating redundant Gemini calls.
  - Independent conversations execute concurrently in parallel.

### 2. Operational Security

- **Safe Proxy Header Inspection**: Uses configured trusted proxy counts (`TRUSTED_PROXIES_COUNT = 1`) to extract authentic client IPs from `X-Forwarded-For` without trusting attacker-spoofed header prefixes.
- **Zero Public Admin Endpoints**: No public administrative reset or internal state dumping endpoints are exposed, preventing rate-limit bypass and information disclosure attacks.

---

## Module 9: Statistics & Live Counters

Module 9 adds thread-safe in-process counters and a dedicated public statistics endpoint (`GET /api/stats`) providing real-time visibility into system throughput, cache effectiveness, AI generation activity, errors, and process uptime.

### 1. In-Process Counters Architecture (`backend/services/stats_service.py`)

- **Thread-Safe Synchronization**: Uses an instance-level `threading.Lock` to guarantee atomic counter updates and consistent snapshot retrieval across concurrent worker threads (`--workers 1 --threads 2`).
- **Zero Gemini Quota Consumption**: Fetching statistics operates entirely in-memory, never calling the Gemini API or consuming LLM quota.
- **Strict Metric Definitions**:
  - `total_requests`: Counts valid incoming chat message requests received at `POST /api/v1/conversations/{id}/messages`. Includes duplicate replays and rate-limited requests, but excludes malformed inputs (HTTP 400) or nonexistent conversations (HTTP 404).
  - `cache_hits`: Incremented only when `cache_service.get()` returns a valid, unexpired policy answer.
  - `gemini_calls`: Incremented only when the Google Gemini API generation is actually invoked.
  - `errors`: Incremented on server-side operational or generation failures (e.g. Gemini provider failure or unhandled exception). Captured with exactly-once counting per failed request.
  - `uptime_seconds`: Computed as `int(time.time() - start_time)` from process initialization.
  - `worker_note`: Required architectural disclaimer: `"Counters reflect the current worker process only. Reset on restart."`

### 2. Operational Statistics API

#### Endpoint
`GET /api/stats`

#### Response (`HTTP 200 OK`)
```json
{
  "total_requests": 847,
  "cache_hits": 503,
  "gemini_calls": 344,
  "errors": 12,
  "uptime_seconds": 7200,
  "worker_note": "Counters reflect the current worker process only. Reset on restart."
}
```

#### Security & Access
- **Read-Only**: Non-GET methods (`POST`, `PUT`, `DELETE`, `PATCH`) return `HTTP 405 Method Not Allowed`.
- **Zero Information Leakage**: Exposes only the 6 required numerical counters and the worker disclaimer. Contains no IP addresses, user identifiers, session IDs, request IDs, prompts, or secrets.
- **No Public Reset Controls**: Does not expose reset endpoints, preventing unauthorized metric manipulation.

### 3. Deployment & Lifecycle Disclosures

- **Single-Worker Scope**: Counters reside in the memory of the single Gunicorn worker process (`--workers 1 --threads 2`). They accurately reflect process-wide metrics without cross-process inconsistency.
- **Ephemeral Container Restart Loss**: On free-tier cloud platforms like Render, container restarts and redeployments wipe in-memory state. All counters reset to zero upon worker process boot.

---

## Module 10: Frontend Chat Interface

Module 10 introduces the complete student-facing web interface for the IIMBx Digital Product Management (DPM-101) Course Helpdesk. Built entirely with vanilla HTML5, modern CSS3, and ES6+ JavaScript, it natively integrates with the Flask backend without requiring external frontend build steps or heavy JavaScript frameworks.

### 1. Architecture & Component Structure

The frontend is served directly by the Flask backend from `frontend/`:

```
frontend/
├── index.html              # Semantic HTML5 markup, ARIA accessibility landmarks, dialogs
├── css/
│   ├── main.css            # Academic design tokens, typography, CSS grid layout, buttons, modals
│   └── chat.css            # Message feed, user & assistant bubbles, source pills, composer, stats grid
└── js/
    ├── storage.js          # LocalStorage state persistence with private browsing fallback
    ├── api.js              # REST client wrapping all 8 backend endpoints, UUID v4 generator
    ├── ui.js               # Safe DOM renderer (zero innerHTML), auto-scroll, modals, countdowns
    └── app.js              # Main coordinator, keyboard navigation, 404 recovery, stats polling
```

### 2. How to Start and Access the Application

1. **Activate the virtual environment**:
   ```powershell
   # Windows
   .\venv\Scripts\Activate.ps1
   ```
2. **Start the Flask server**:
   ```bash
   python app.py
   ```
3. **Open the Chatbot in your browser**:
   Navigate to `http://localhost:5000` (or `http://127.0.0.1:5000`).

### 3. Key Features

- **Academic IIMBx Identity**: Branded with deep academic navy (`#0B2545`), gold accents (`#C59B27`), and high-contrast typography satisfying WCAG 2.1 AA standards.
- **Responsive Layout**:
  - **Desktop (>900px)**: 2-column view with permanent conversation sidebar and spacious chat workspace.
  - **Tablet (601px–900px)**: Collapsible sidebar drawer with mobile hamburger toggle and tap-outside backdrop.
  - **Mobile (≤600px)**: Compact header, full-screen touch-friendly feed, and auto-expanding input composer.
- **Conversation Management**:
  - Create new conversations with one click.
  - Switch between past conversations with instantaneous history loading.
  - Delete conversations with confirmation prompt.
  - Welcome state with 4 clickable FAQ prompt chips for rapid student onboarding.
- **Strict Safe Text Rendering (Zero `innerHTML`)**:
  - Model responses, user messages, conversation titles, and source citations are rendered strictly using native DOM text nodes (`document.createElement`, `document.createTextNode`, `replaceChildren`).
  - Custom safe tokenizer renders bold text (`**text**`), bullet lists, and paragraphs without HTML string injection, eliminating XSS vulnerabilities.
- **Grounded Source Attribution**:
  - Dynamically renders source badges based on the backend's `sources` list.
  - Supports multiple authoritative sources per response.
  - Displays distinct `ℹ️ Information not specified in official course documents` banner for unsupported queries (`is_supported: false`).
  - Never hardcodes or fabricates document citations.
- **Request Management & Duplicate Prevention**:
  - Generates RFC 4122 v4 UUID request IDs satisfying `^[a-zA-Z0-9_\-]{1,128}$`.
  - Sends matching request IDs in both `X-Request-ID` header and JSON body `request_id`.
  - Reuses request ID when retrying a failed turn; generates a fresh ID for every new turn.
  - Deduplicates replayed responses (`is_duplicate: true` / `X-Duplicate-Request: true`) to prevent duplicate bubbles in the DOM.
- **HTTP 429 Rate-Limit Countdown**:
  - Catches HTTP 429 and parses `retry_after` from response body and header.
  - Disables composer and displays an active second-by-second countdown banner (`15s... 14s... 0s`).
  - Automatically re-enables composer when the cooldown expires.
- **404 Conversation Recovery (No Silent Resending)**:
  - If a conversation is no longer active on the server, the student's typed message is preserved in the composer.
  - Displays an honest alert: *"This conversation is no longer active on the server."*
  - Provides an explicit `[Start New Chat]` action button for the student.
- **Live Operational Statistics Modal**:
  - Access via `[📊 Live Stats]` button in header.
  - Displays the 6 exact counters from `GET /api/stats`: Total Requests, Cache Hits, Gemini Calls, Errors, Process Uptime, and Worker Note.
  - Polls automatically every 30 seconds strictly while the dialog is open.
  - Pauses polling immediately when closed or when the browser tab is hidden.

### 4. Testing Instructions

Run frontend integration tests:
```bash
pytest tests/test_frontend_integration.py -v
```

Run Module 11 End-to-End Integration & Error Handling suite:
```bash
pytest tests/test_module11_e2e_integration.py -v
```

Run the complete regression suite across all 11 modules:
```bash
pytest -v
```
*(Total: 353 / 353 tests passing with zero regressions).*

---

## Module 11: End-to-End Integration, Testing & Error Handling

Module 11 unifies all previous subsystems (Modules 1–10) into a hardened, production-ready pipeline.

### Core Verified Behaviors:
1. **Full-Stack Conversation Lifecycle**:
   - Complete round-trip creation, message persistence, chronological message retrieval with sources, and cascade deletion.
   - Multi-turn contextual follow-up resolving implicit pronouns (`"it"`, `"this"`, `"miss it by one day"`) to prior topics while bypassing cache storage for context-dependent turns.
2. **Canonical Section 17 Policy Invariant Verification**:
   - **Q1 (Grading Policy)**: Strict enforcement of at least 60% overall score AND at least 40% in Proctored Final Exam separately; 85% or above for Pass with Distinction (Doc 03 authority; Doc 02 excluded).
   - **Q2 (Capstone Deadline)**: Authoritative deadline of 15 November 2026, 11:59 PM IST (extended by 5 days).
   - **Q3 (Final Exam Rules)**: Nov 20, 2026, 90 mins, closed-book, laptop/desktop + webcam/mic; unmoderated forum rumors and Doc 03 excluded.
   - **Q4 (Certificate Fee)**: Strictly ₹1,500 from Doc 01; adversarial ₹4,999 forum claims rejected.
   - **Q5, Q7, Q8 (Honest & Polite Declines)**: Unsupported queries (refund), out-of-scope queries (Kubernetes/SQL), and unrelated queries (Cricket World Cup) return polite declines and are **never stored in SQLite cache**.
   - **Q11 (Rephrased Query Cache Miss)**: Different query wording generates different SHA-256 composite keys, producing Cache MISS and evaluating freshly (no semantic similarity cache matching).
3. **Cache Lifecycle & TXT Fingerprint Invalidation**:
   - Exact repeat queries hit cache without calling Gemini (`cache_hits` increments; `gemini_calls` unchanged).
   - On-the-fly modification of course plain-text files (`03_grading_policy_v2.txt`) recalculates the document fingerprint, immediately invalidating old cached keys and guaranteeing fresh evaluation against updated text.
4. **Concurrency & Rate Limit Guarantees**:
   - Thundering herd protection: 3 concurrent identical requests coordinate on condition variables; exactly 1 Gemini synthesis call executes; duplicate threads wait and receive HTTP 200 with `X-Duplicate-Request: true`.
   - Sliding-window rate limiting returns HTTP 429 with `Retry-After` header when limit is reached, while replaying completed requests remains exempt from quota consumption.
   - Per-conversation locks return HTTP 409 `CONVERSATION_BUSY` when lock acquisition times out.
5. **Fault Tolerance & Extractive Fallback**:
   - Gemini API connection/authentication errors gracefully degrade to grounded extractive text fallbacks from course documents without crashing or returning HTTP 500.
   - Real-time in-process operational metrics on `GET /api/stats` maintain 100% thread safety and accuracy.
   - `GET /health` and static asset serving are decoupled from AI quota and do not increment request counters.

### 5. Remaining Limitations

- **Ephemeral Storage**: Conversation history, cache entries, and in-process statistics are stored locally in SQLite and memory; they reset when the server container restarts or redeploys.
- **No User Authentication**: Conversations are tracked via anonymous client-side UUIDs without authenticated student accounts.
- **Cloud Deployment**: Production deployment on Render with Gunicorn will be completed in Module 12.

---

## Next Steps

Module 12 will implement Production Deployment & Final Project Delivery on Render.
