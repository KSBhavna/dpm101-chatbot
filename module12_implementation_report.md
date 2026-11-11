# Module 12 — Production Deployment & Final Submission Report

**Project:** IIMBx DPM-101 Course Helpdesk Chatbot  
**Module:** 12 of 12 (Production Deployment & Final Delivery)  
**Status:** ✅ **Production Ready & Locally Verified**  
**Test Suite:** **353 / 353 Passing (100%)**  

---

## 1. Executive Summary

Module 12 completes the 12-module development roadmap for the IIMBx DPM-101 Course Helpdesk Chatbot. The application has been hardened for production deployment, verified against all policy invariants, packaged with production WSGI serving via Gunicorn, and configured with Infrastructure-as-Code via Render Blueprint (`render.yaml`).

A comprehensive local production verification was executed, confirming the liveness check, static asset delivery, conversation lifecycle, duplicate request idempotency, grounded policy answers, and thread-safe operational metrics without requiring unverified external credentials.

---

## 2. Production Architecture & Deployment Preparation

### 2.1 Technology Stack & Runtime

| Component | Technology | Specification |
|---|---|---|
| **Python Runtime** | CPython 3.11.9 | Specified in `runtime.txt` (`python-3.11.9`) and `render.yaml` |
| **Web Framework** | Flask 3.0.3 | Application factory pattern (`create_app()`) |
| **WSGI Server** | Gunicorn 22.0.0 | High-concurrency production HTTP server |
| **Concurrency Model** | 1 Worker, 4 Threads | `--workers 1 --threads 4` guarantees shared in-process state for deduplication and rate limiting |
| **Static Serving** | Native Flask `send_static_file` | Direct delivery of CSS/JS/HTML without CORS issues |
| **Database** | SQLite 3 | WAL mode, busy timeout 15.0s, autocommit isolation |
| **AI SDK** | `google-genai==3.0.0` | Official Google GenAI SDK (deprecated `google-generativeai` excluded) |
| **Retrieval Engine** | TF-IDF Vector Space Model | Pure Python, zero-external-dependency lexical retrieval |

### 2.2 Entry Point Hardening (`app.py`)

The application entry point was updated to support both WSGI conventions seamlessly:
1. `gunicorn "app:create_app()"` (Application factory invocation)
2. `gunicorn app:app` (Module-level WSGI instance)
3. Dynamic host/port binding: binds to `0.0.0.0` and respects Render's `$PORT` environment variable (`os.environ.get("PORT", 5000)`).

```python
# Module-level application instance for WSGI runners (e.g. gunicorn app:app)
app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=config.FLASK_DEBUG)
```

### 2.3 Dependencies (`requirements.txt`)

`gunicorn==22.0.0` was added to `requirements.txt`. All 353 tests continue to pass cleanly.

---

## 3. Render Deployment Specification

### 3.1 Declarative Blueprint (`render.yaml`)

A Render Blueprint specification was created at the repository root:

```yaml
services:
  - type: web
    name: dpm101-chatbot
    runtime: python
    plan: free
    region: oregon
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn "app:create_app()" --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
    healthCheckPath: /health
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.9
      - key: FLASK_DEBUG
        value: "False"
      - key: FLASK_SECRET_KEY
        generateValue: true
      - key: GEMINI_API_KEY
        sync: false
      - key: GEMINI_MODEL
        value: gemini-2.5-flash
      - key: CACHE_ENABLED
        value: "True"
      - key: RATE_LIMIT_ENABLED
        value: "True"
```

### 3.2 Environment Variables Reference

| Variable | Type | Required | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Secret | **Yes** | Google Gemini API key from Google AI Studio. Never committed to Git. |
| `GEMINI_MODEL` | String | No | Model name (default: `gemini-2.5-flash` or `gemini-3.8-flash`). |
| `FLASK_SECRET_KEY` | Secret | **Yes** | 256-bit secret string generated automatically by Render. |
| `FLASK_DEBUG` | Boolean | No | Must be `False` in production. |
| `CACHE_ENABLED` | Boolean | No | `True` enables SQLite response caching. |
| `CACHE_DEFAULT_TTL` | Integer | No | `86400` (24 hours). |
| `RATE_LIMIT_ENABLED` | Boolean | No | `True` enables in-memory sliding window rate limiter. |
| `PYTHON_VERSION` | String | No | Pinned to `3.11.9`. |

---

## 4. SQLite Storage & Free Tier Persistence Invariants

> [!WARNING]
> **Ephemeral Filesystem on Render Free Tier**:
> - Render free web services run inside ephemeral containers that spin down after 15 minutes of inactivity.
> - Official course text documents (`data/documents/*.txt`) persist permanently because they are tracked in version control.
> - The SQLite database file (`data/chatbot.db`), conversation history, cached query keys, and in-process statistics (`GET /api/stats`) are stored on the ephemeral filesystem and reset when the service restarts, wakes from sleep, or redeploys.
> - This behavior is fully documented in `README.md` and expected on free container tiers. For permanent cross-restart persistence, a Render Persistent Disk or external PostgreSQL database is required.

---

## 5. Live Verification Results

A comprehensive verification was conducted against the production server running under production configuration (`FLASK_DEBUG=False`, dynamic port binding):

| Test Item | Endpoint / Action | Expected Result | Actual Result | Status |
|---|---|---|---|---|
| **Liveness Check** | `GET /health` | HTTP 200, zero Gemini quota | `{"service": "DPM-101 Helpdesk Chatbot", "status": "healthy"}` | ✅ PASS |
| **Web UI** | `GET /` | HTTP 200, semantic HTML5 document | HTTP 200 (15,587 bytes loaded) | ✅ PASS |
| **Static Assets** | `GET /css/main.css` | HTTP 200, CSS typography tokens | HTTP 200 (14,462 bytes loaded) | ✅ PASS |
| **Operational Stats** | `GET /api/stats` | HTTP 200, exact 6 fields | `{"total_requests": 0, "cache_hits": 0, "gemini_calls": 0, "errors": 0, "uptime_seconds": 2, "worker_note": "..."}` | ✅ PASS |
| **Conversation Create** | `POST /api/v1/conversations` | HTTP 201, UUID id | HTTP 201, UUID returned | ✅ PASS |
| **Conversation Fetch** | `GET /api/v1/conversations/<id>` | HTTP 200, active status | HTTP 200, active status confirmed | ✅ PASS |
| **Policy Question** | `POST /api/v1/conversations/<id>/messages` | HTTP 200, Doc 03 answer | HTTP 200, weighted 60% overall + 40% proctored final cited | ✅ PASS |
| **Source Attribution** | `sources` field | Human-readable document titles | `['Grading & Late Submission Policy V2 (Current)', ...]` | ✅ PASS |
| **Duplicate Idempotency** | Replay identical request ID | HTTP 200, `X-Duplicate-Request: true` | HTTP 200, header present, zero duplicate generation | ✅ PASS |
| **Live Stats Update** | `GET /api/stats` | `total_requests` incremented | Counters incremented accurately | ✅ PASS |
| **Secrets Protection** | Payload & log inspection | Zero API keys or secrets leaked | Zero secrets present in responses or logs | ✅ PASS |

---

## 6. Repository Cleanliness & Git State

### 6.1 Git Commits
The `dpm101-chatbot` directory was initialized as an independent Git repository with clean history:
- `bfaa471`: `Release: IIMBx DPM-101 Course Helpdesk Chatbot - Production Ready`
- `8f0df8c`: `Docs: Update README with Module 12 production deployment specifications and submission checklist`

### 6.2 Exclusions & Secret Safety
- `.gitignore` correctly excludes `.env`, `*.env.local`, `venv/`, `__pycache__/`, `.pytest_cache/`, `data/*.db`, and logs.
- `.env.example` contains only placeholder values (`your_api_key_here`).
- Rigorous regex audit for Google API keys (`AIza...`) confirmed zero hardcoded credentials in the repository.

---

## 7. Instructions for Final GitHub & Render Push

To complete public hosting on your Render account:

```bash
# 1. Inside dpm101-chatbot, add your GitHub repository remote
git remote add origin https://github.com/<your-username>/<your-repo-name>.git

# 2. Push to GitHub
git branch -M main
git push -u origin main

# 3. Deploy on Render
# Option A: Go to https://dashboard.render.com/blueprints -> New Blueprint Instance -> Select repository -> Set GEMINI_API_KEY
# Option B: Go to https://dashboard.render.com/web/new -> Connect repository -> Build: pip install -r requirements.txt -> Start: gunicorn "app:create_app()" --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

---

## 8. Final Test Count & Audit Summary

All 353 test cases pass with zero failures and zero warnings:

```
============================= 353 passed in 19.49s =============================
```

### Test Suite Breakdown

| Suite | File | Tests | Focus Area |
|---|---|---|---|
| **Health Check** | `test_health.py` | 6 | Liveness probe, no Gemini quota |
| **Document Processor** | `test_document_service.py` | 27 | TXT parsing, metadata, section isolation |
| **Gemini Integration** | `test_gemini_service.py` | 32 | SDK configuration, retry, timeout, fallback |
| **Policy Engine** | `test_policy_service.py` | 43 | Topic precedence, document authority, Section 17 |
| **Conversation Store** | `test_chat_service.py` | 44 | SQLite transactions, message history, follow-ups |
| **Caching Layer** | `test_cache_service.py` | 55 | Composite SHA-256 keys, TTL, fingerprint invalidation |
| **Concurrency & Limits** | `test_request_management.py` | 58 | Rate limiter, Thundering Herd, idempotency |
| **Operational Stats** | `test_stats_service.py` | 30 | Thread-safe in-process metrics |
| **Frontend Integration** | `test_frontend_integration.py` | 41 | Native DOM nodes, zero innerHTML, XSS safety |
| **End-to-End Pipeline** | `test_module11_e2e_integration.py` | 17 | Full round-trip scenarios, Section 17 canonical Q1-Q12 |
| **TOTAL** | **10 Test Files** | **353** | **100% Pass Rate** |

---

## 9. Final Submission Checklist

- [x] **Modules 1–12 Completed**: All 12 project modules implemented according to design specifications.
- [x] **Test Baseline**: 353 / 353 tests passing cleanly.
- [x] **Zero Regressions**: All previous module behaviors intact.
- [x] **Security**: No secrets or API keys committed to Git.
- [x] **DOM Safety**: Zero `innerHTML` across frontend (`ui.js`).
- [x] **Production WSGI**: Gunicorn 22.0.0 added and verified.
- [x] **Infrastructure-as-Code**: `render.yaml` and `runtime.txt` authored and committed.
- [x] **Clean Documentation**: `README.md` completely updated with deployment instructions and architecture details.
- [x] **Report Generated**: Comprehensive Module 12 final report delivered.
