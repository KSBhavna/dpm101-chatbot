# MODULE 9 — STATISTICS & LIVE COUNTERS
## Implementation Plan (Phase 1B — Refined Architecture)

**Project:** IIMBx DPM-101 Course Helpdesk Chatbot  
**Module:** 9 of 12  
**Status:** Approved for Final Review (No source code modified)  
**Regression Baseline:** 293 / 293 tests passing in 13.00s  

---

## 1. Executive Summary & Exact Traceability

Module 9 implements **Statistics & Live Counters** to provide operational visibility into chatbot throughput, cache effectiveness, AI invocation volume, errors, and worker uptime.

### Traceability to Original Specification
Every aspect of this plan is directly derived from the approved project architecture (`implementation_plan.md`):

| Architecture Reference | Requirement Specification | Planned Implementation | Acceptance Verification |
|---|---|---|---|
| **Roadmap §15 (Module 9)** | `stats_service.py` — thread-safe in-process counters (`threading.Lock`); `GET /api/stats` with `worker_note` field explaining single-worker scope and restart loss. | `backend/services/stats_service.py` with `threading.Lock` coordinating all counter updates; `GET /api/stats` endpoint. | Unit and concurrency tests verify thread safety and exact `worker_note`. |
| **API Design §9 (`GET /api/stats`)** | JSON response must contain exactly: `total_requests`, `cache_hits`, `gemini_calls`, `errors`, `uptime_seconds`, and `worker_note`. | Endpoint returns the exact 6-field schema specified in §9. | Schema contract test matching §9 JSON specification. |
| **Worker Strategy §12.1** | Architecture uses a single Gunicorn worker process (`--workers 1 --threads 2`) so in-process statistics reflect application-wide totals without cross-worker drift. | Counters reside in the single worker process memory space; no distributed brokers (e.g. Redis) introduced. | Worker lifecycle documentation and single-process scope verification. |
| **Persistence Limitations §12.3** | Render's ephemeral filesystem wipes on restart; in-process stats reset to zero on worker restart. | Documented restart behavior; uptime measured from process start timestamp. | Service initialization test verifies counters start at zero. |
| **Zero Gemini Quota §9** | Stats endpoint must never invoke Gemini API or consume LLM quota. | Stats collection strictly queries memory and local SQLite; zero network calls. | Test verifies Gemini client mock is never called during stats requests. |

---

## 2. Scope Boundaries

### Strictly in Scope for Module 9:
1. `backend/services/stats_service.py`: Central thread-safe statistics service.
2. `backend/routes/stats_routes.py`: Dedicated route exposing `GET /api/stats`.
3. Application registration in `app.py`.
4. Instrumentation in `chat_routes.py`, `policy_service.py`, and `gemini_service.py`.
5. Comprehensive test suite in `tests/test_stats_service.py`.

### Explicitly Out of Scope for Module 9:
1. **Frontend Changes**: Per project roadmap, all frontend interface work (including HTML, CSS, JavaScript, and stats presentation) belongs to **Module 10: Frontend Chat Interface**. No frontend files will be created or modified in Module 9.
2. **Alternative Endpoints**: `/api/v1/stats` is excluded; only the specified primary endpoint `GET /api/stats` will be implemented.
3. **Reset Controls**: No public or private reset endpoint (`POST /reset`) will be exposed, preserving security against unauthorized counter manipulation.

---

## 3. Endpoint Contract Specification: `GET /api/stats`

### 3.1 Route Definition
- **Path:** `/api/stats`
- **Method:** `GET`
- **Authentication:** Publicly accessible (contains no sensitive internal state, PII, or tokens).
- **Allowed Methods:** `GET` only. Requests with `POST`, `PUT`, `DELETE`, etc. return `HTTP 405 Method Not Allowed`.

### 3.2 Response Payload (`HTTP 200 OK`)
The endpoint response strictly conforms to Section 9 of the architecture specification:

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

### 3.3 Field Definitions and Types
| Field | Type | Description |
|---|---|---|
| `total_requests` | `int` | Total student chat message requests received at the message endpoint. |
| `cache_hits` | `int` | Number of requests resolved directly via SQLite cache without calling Gemini. |
| `gemini_calls` | `int` | Number of actual API calls made to the Google Gemini model. |
| `errors` | `int` | Cumulative server-side operational and AI generation failures. |
| `uptime_seconds` | `int` | Seconds elapsed since the worker process started. |
| `worker_note` | `str` | Exact fixed string: `"Counters reflect the current worker process only. Reset on restart."` |

---

## 4. Metric Definitions and Instrumentation Strategy

To ensure absolute accuracy without double-counting, each counter is defined with exact trigger conditions and hook locations:

```
[Incoming Request: POST /api/v1/conversations/{id}/messages]
       │
       ▼
1. Validate JSON & Conversation Active
       │
       ├── (Validation fails: HTTP 400/404) ──> Return Error (Not counted in total_requests)
       │
       ▼
2. request_mgr.start_request()
       │
       ├──> INCREMENT total_requests (+1)
       │
       ├── (Duplicate completed replay: HTTP 200) ──> Return replayed response (No Gemini, No Cache Hit)
       ├── (Rate limited: HTTP 429) ───────────────> Return 429 (No Gemini, No Cache Hit)
       │
       ▼
3. policy_service.ask(query)
       │
       ├── Cache lookup succeeds ──> INCREMENT cache_hits (+1) ──> Return answer
       │
       └── Cache miss ─────────────> Proceed to live retrieval
                                           │
                                           ▼
                                    gemini_service.generate_text()
                                           │
                                           ├──> INCREMENT gemini_calls (+1)
                                           │
                                           ├── Gemini succeeds ──> Cache answer & return
                                           │
                                           └── Gemini throws / fails ──> INCREMENT errors (+1)
                                                                       ──> Return fallback or 500
```

### 4.1 Detailed Metric Rules

1. **`total_requests`**:
   - **Definition:** Counts every valid chat message request arriving at `POST /api/v1/conversations/<id>/messages` that passes initial JSON validation and active conversation verification.
   - **Duplicates:** Included in `total_requests` because a client sent an incoming request, but duplicates do **not** increment `gemini_calls` or `cache_hits`.
   - **Rate-Limited Requests:** Included in `total_requests` because an incoming request was received and processed by the rate limiter, but does **not** increment `gemini_calls` or `cache_hits`.
   - **Malformed Requests (HTTP 400 / 404):** Malformed payloads (missing fields, invalid JSON) and requests to nonexistent/deleted conversations are rejected before request processing begins and are not counted as processed chat requests.

2. **`cache_hits`**:
   - **Definition:** Incremented **only** when `cache_service.get()` returns a valid, unexpired, policy-grounded `CachedAnswer`.
   - **Exclusions:** Context-dependent queries that bypass cache, cache misses, and expired entries do not increment this counter.
   - **Instrumentation Hook:** Directly inside `PolicyService.ask()` upon receiving a non-None result from `self.cache_service.get()`.

3. **`gemini_calls`**:
   - **Definition:** Incremented **only** when an actual network API request to Google Gemini (`generate_text`) is initiated.
   - **Exclusions:** Never incremented on cache hits, out-of-scope query declines, unsupported topic declines, or extractive fallbacks where the API is not called.
   - **Instrumentation Hook:** Inside `GeminiService.generate_text()` or immediately prior to the client invocation in `PolicyService`.

4. **`errors`**:
   - **Definition:** Tracks server-side operational and generation failures during message processing (e.g. Gemini API failure, SQLite operational error, or unhandled exception resulting in HTTP 500/503).
   - **Exclusions:** Client errors (HTTP 400 Bad Request, HTTP 404 Not Found, HTTP 409 Conflict) and rate limits (HTTP 429) are normal operational rejections, not server errors.
   - **Double-Counting Prevention:** Captured exclusively in the exception handler or fallback trigger path. Each failed request increments `errors` exactly once.

5. **`uptime_seconds`**:
   - **Definition:** Calculated as `int(time.time() - self._start_time)` where `_start_time` is recorded using `time.time()` when `StatsService` is instantiated at worker startup.

---

## 5. Verified Database Schema & Aggregation Strategy

While the primary contract of `GET /api/stats` requires the 6 in-process metrics above, `StatsService` also provides an internal aggregation utility to verify persistent database health.

### 5.1 Verification of Actual SQLite Schema
Inspection of `backend/services/chat_service.py` and `backend/services/cache_service.py` reveals the exact database structures:

1. **`conversations` Table**:
   - Columns: `id TEXT PRIMARY KEY`, `created_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`, `status TEXT NOT NULL DEFAULT 'active'`.
   - **Status Values:**
     - Active conversations: `status = 'active'`.
     - Soft-deleted conversations: `status = 'deleted'` (via `update conversations set status = 'deleted'`).
     - Hard-deleted conversations: row is removed entirely (`DELETE FROM conversations`).
     - *Conclusion:* A query checking `status = 'active'` is valid; counting total vs. active conversations must account for both soft and hard deletions.

2. **`messages` Table**:
   - Columns: `id TEXT PRIMARY KEY`, `conversation_id TEXT NOT NULL`, `role TEXT NOT NULL CHECK(role IN ('user', 'assistant'))`, `content TEXT NOT NULL`, `created_at TEXT NOT NULL`, `sources TEXT`.
   - Foreign key references `conversations(id) ON DELETE CASCADE`.

3. **`query_cache` Table**:
   - Columns: `id TEXT PRIMARY KEY`, `cache_key TEXT UNIQUE NOT NULL`, `normalized_query TEXT NOT NULL`, `answer TEXT NOT NULL`, `sources TEXT NOT NULL`, `created_at TEXT NOT NULL`, `expires_at TEXT NOT NULL`, `last_accessed_at TEXT NOT NULL`, `access_count INTEGER NOT NULL DEFAULT 1`, etc.
   - **Timestamp Format:** Stored as UTC ISO 8601 strings (`datetime.now(timezone.utc).isoformat()`, e.g. `'2026-11-11T03:10:45.123456+00:00'`).
   - *Expiration Comparison:* To compare against `expires_at` accurately in SQLite, the query must pass `datetime.now(timezone.utc).isoformat()` as a parameter (`expires_at > ?`), matching `CacheService.cleanup_expired()`. Do **not** use SQLite's native `datetime('now')` because SQLite uses space separators rather than `'T'`, causing lexicographical comparison discrepancies.

### 5.2 Safe Read-Only Aggregation Query
```python
def get_database_counts(self) -> dict[str, int]:
    """Execute non-blocking read query across SQLite tables using parameter binding."""
    now_iso = datetime.now(timezone.utc).isoformat()
    query = """
        SELECT
            (SELECT COUNT(*) FROM conversations) AS total_conversations,
            (SELECT COUNT(*) FROM conversations WHERE status = 'active') AS active_conversations,
            (SELECT COUNT(*) FROM messages) AS total_messages,
            (SELECT COUNT(*) FROM query_cache) AS total_cache_entries,
            (SELECT COUNT(*) FROM query_cache WHERE expires_at > ?) AS active_cache_entries;
    """
    # Executed with autocommit connection (isolation_level=None) under WAL mode
```

---

## 6. In-Memory Counter Design & Thread Safety

### 6.1 Synchronization
In Gunicorn running with `--workers 1 --threads 2`, two worker threads process requests concurrently. In Python, operations like `self._total_requests += 1` are not atomic at the bytecode level.

To guarantee zero race conditions:
```python
class StatsService:
    def __init__(self, db_path: Optional[str] = None):
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._total_requests = 0
        self._cache_hits = 0
        self._gemini_calls = 0
        self._errors = 0
        self.db_path = db_path

    def increment_requests(self) -> None:
        with self._lock:
            self._total_requests += 1

    def increment_cache_hits(self) -> None:
        with self._lock:
            self._cache_hits += 1

    def increment_gemini_calls(self) -> None:
        with self._lock:
            self._gemini_calls += 1

    def increment_errors(self) -> None:
        with self._lock:
            self._errors += 1

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            total_reqs = self._total_requests
            hits = self._cache_hits
            gemini = self._gemini_calls
            errs = self._errors
            uptime = int(time.time() - self._start_time)

        return {
            "total_requests": total_reqs,
            "cache_hits": hits,
            "gemini_calls": gemini,
            "errors": errs,
            "uptime_seconds": uptime,
            "worker_note": "Counters reflect the current worker process only. Reset on restart.",
        }
```

### 6.2 Ephemeral Lifecycle & Single Worker
- **Scope:** In-process counters reside strictly in the worker's RAM.
- **Worker Reset:** When the container restarts or redeploys on Render, memory is wiped and counters reset to zero.
- **Single Worker Alignment:** Because Gunicorn is configured with `--workers 1`, all requests are handled by this single process, eliminating cross-worker count discrepancy.

---

## 7. Security & Information Protection

1. **Strict Field Whitelist:** `GET /api/stats` exposes **only** the 6 mandatory numerical counters and worker note.
2. **Zero Internal State Leakage:**
   - No IP addresses or client identifiers.
   - No session UUIDs or conversation keys.
   - No prompt texts, question contents, or cached answer snippets.
   - No API keys or environment secrets.
3. **No Administrative Controls:**
   - No `/api/stats/reset` or modification endpoints.
   - The route accepts `GET` requests only; all other HTTP methods return `405 Method Not Allowed`.

---

## 8. Empirical Performance Verification Plan

Rather than making unsubstantiated latency claims, the implementation will measure actual response times empirically:

1. **Benchmark Test (`test_stats_endpoint_latency`):**
   - Execute 100 sequential requests to `GET /api/stats` using Flask's test client.
   - Record response times using high-precision `time.perf_counter()`.
   - Calculate p50, p95, and maximum latency.
   - Assert that p95 response time is well within an acceptable threshold for an in-memory/WAL read ($< 25\,\text{ms}$ in test harness).
2. **Zero Block on Chat Pipeline:**
   - Counter increments inside the lock execute $< 1\,\mu\text{s}$, introducing negligible overhead to chat message handling.

---

## 9. Comprehensive Testing Plan

A dedicated test file `tests/test_stats_service.py` will be created with minimum 12 focused tests:

### 9.1 Unit Tests for `StatsService`
1. `test_stats_initialization`: Verify initial counters are 0, `uptime_seconds >= 0`, and `worker_note` matches specification.
2. `test_counter_increments`: Verify atomic increments of requests, cache hits, Gemini calls, and errors.
3. `test_reset_runtime_counters`: Verify reset helper restores counters to 0 (used for test isolation).

### 9.2 Concurrency & Thread-Safety Tests
4. `test_concurrent_increments`: Launch 20 concurrent threads each performing 100 increments across all counters; assert final values are exactly 2,000 with zero lost updates.
5. `test_concurrent_reads_during_increments`: Concurrently call `get_stats()` while write threads increment counters, verifying consistent snapshot reads without exceptions.

### 9.3 API Route Tests (`GET /api/stats`)
6. `test_api_stats_endpoint_schema`: Assert `GET /api/stats` returns `HTTP 200` with the exact 6 keys: `total_requests`, `cache_hits`, `gemini_calls`, `errors`, `uptime_seconds`, `worker_note`.
7. `test_api_stats_read_only_methods`: Verify `POST /api/stats`, `PUT /api/stats`, and `DELETE /api/stats` return `HTTP 405 Method Not Allowed`.
8. `test_stats_does_not_call_gemini`: Verify Gemini API client is never called when querying `/api/stats`.
9. `test_stats_endpoint_latency_benchmark`: Measure 100 iterations of `/api/stats` using `time.perf_counter()` to record empirical p50/p95 response times.

### 9.4 Pipeline Integration Tests
10. `test_chat_pipeline_increments_total_requests`: Send a message via chat endpoint; verify `total_requests` increments.
11. `test_cache_hit_increments_cache_hits_only`: Query a cached question; verify `cache_hits` increments while `gemini_calls` does not.
12. `test_gemini_call_increments_gemini_calls`: Query an uncached question; verify `gemini_calls` increments.
13. `test_regression_all_modules`: Verify that all 293 existing tests continue to pass with zero regressions.

---

## 10. File-by-File Change Plan

| Action | File Path | Scope & Responsibilities |
|---|---|---|
| **CREATE** | `backend/services/stats_service.py` | `StatsService` class with `threading.Lock`, atomic increments, snapshot reading, and singleton accessors (`get_stats_service()`). |
| **CREATE** | `backend/routes/stats_routes.py` | Flask Blueprint `stats_bp` registering the single primary route `GET /api/stats`. |
| **MODIFY** | `app.py` | Register `stats_bp` and initialize `app.extensions["stats_service"]`. |
| **MODIFY** | `backend/routes/chat_routes.py` | Hook `total_requests` on incoming valid messages and errors on unhandled exceptions. |
| **MODIFY** | `backend/services/policy_service.py` | Hook `cache_hits` on cache hit and `gemini_calls` on model invocation. |
| **CREATE** | `tests/test_stats_service.py` | 12+ comprehensive unit, concurrency, route schema, and integration tests. |
| **MODIFY** | `README.md` | Document Module 9 completion, `GET /api/stats` specification, and single-worker reset behavior. |

*Note: Zero frontend files are modified in Module 9.*

---

## 11. Remaining Limitations & Honest Architectural Assessment

1. **Worker-Bound Lifecycle**: Counters are held in memory. Restarting Gunicorn or redeploying the Render service resets all counters to zero. This is a known, accepted trade-off for single-worker in-memory architectures.
2. **Single-Worker Constraint**: If the application were scaled to multiple Gunicorn workers (`--workers > 1`), in-process counters would represent only the worker handling the request. For multi-worker deployments, a shared store (e.g. Redis) would be required. The current single-worker design (`--workers 1 --threads 2`) makes in-process counters accurate for the whole instance.
3. **No Historical Rollups**: Counters represent cumulative totals since the last restart. Time-series rollups (e.g. requests per minute over the last 24 hours) are not stored in SQLite to avoid unbounded database growth.
