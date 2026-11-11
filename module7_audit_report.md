# Module 7: SQLite Caching System — Independent Implementation Audit Report

**Project:** IIMBx DPM-101 Course Helpdesk Chatbot  
**Module Audited:** Module 7 — SQLite Caching System  
**Audit Date:** 2026-11-11  
**Environment:** Python 3.11.9 on Windows (`win32`)  
**Test Framework:** `pytest-8.3.2` (plugins: `anyio-4.16.0`, `flask-1.3.0`)  
**Audit Scope:** Independent forensic code inspection, composite key hashing, document fingerprinting, LRU capacity bounds enforcement, policy and context safety, SQLite transaction isolation, error resilience, and full regression verification.  
**Test Result:** **265 passed in 14.39s (100% passing across all 6 test suites)**  
**Recommendation:** **PASS — Module 7 Complete**

---

## 1. Audit Requirements & Verification Matrix

| # | Requirement | Specific Verification Area | Audit Evidence & Code Location | Result |
|---|---|---|---|:---:|
| 1 | **Database Cache Design** | SQLite `query_cache` schema, primary key, composite SHA-256 key, timestamps, access stats, indexing. | `backend/services/cache_service.py` — Unique index on `cache_key`, indexes on `expires_at` and `last_accessed_at DESC`. Autocommit (`isolation_level=None`), WAL mode, busy timeout (`15000`). | **PASS** |
| 2 | **Composite Key Generation** | Multi-factor SHA-256 hash: query + doc fingerprint + policy version + model config. | `backend/services/cache_service.py` — Verified 64-char hex key. Normalization standardizes casing, whitespace, and punctuation. | **PASS** |
| 3 | **Document Fingerprint Accuracy** | Content hashing of official documents to invalidate stale entries. | `backend/services/cache_service.py` — File mtime memoization + SHA-256 hashing. Automatically invalidates on file edit (`test_35`). | **PASS** |
| 4 | **Policy & Model Invalidation** | Changes in prompt/policy revision or Gemini model/temp produce new keys. | Verified in `test_policy_version_change_invalidates_key` and `test_model_version_change_invalidates_key`. | **PASS** |
| 5 | **Context Safety & Follow-up Exclusion** | Context-dependent questions (pronouns/ellipses) never cached as standalone. | `backend/services/cache_service.py` and `chat_service.py` — Verified in `test_19`, `test_30`. | **PASS** |
| 6 | **Source & Moderation Validation** | Doc02 cannot be sole authority; unmoderated forum evicted; fee ₹4,999 rejected. | `backend/services/cache_service.py` — Verified in `test_33` (Doc02 eviction), `test_34` (fee rejection), `test_25` (forum eviction). | **PASS** |
| 7 | **TTL & Capacity Bounds (LRU)** | Configurable TTL expiration and maximum entry enforcement (`CACHE_MAX_ENTRIES`). | `backend/services/cache_service.py` — Purges expired entries, then evicts least recently accessed (LRU). Verified in `test_32`. | **PASS** |
| 8 | **SQLite Concurrency & Lock Contention** | Write operations use `immediate_transaction`; read access stats update without lock. | Read updates execute atomic standalone `UPDATE` on autocommit connection without write lock contention. Tested under 8 concurrent threads (`test_28`). | **PASS** |
| 9 | **Fault Tolerance & Graceful Degradation** | SQLite unavailability degrades to cache miss without raising or failing chat. | Handled via `try...except` logging warning. Verified in `test_cache_database_failure_degrades_gracefully` with simulated `OperationalError`. | **PASS** |
| 10 | **Gemini Call Elimination on Hits** | Cache hits return validated answers and skip Gemini API completely. | Verified in `test_first_question_misses_then_second_hits_skipping_gemini` (`mock_gemini.generate_text.call_count == 1` across 2 calls). | **PASS** |
| 11 | **Modules 1–6 Regression Integrity** | Existing 229 tests across Modules 1–6 continue passing without regressions. | Verified: 229/229 existing tests pass. Combined total: **265/265 passed**. | **PASS** |

---

## 2. Identified Defects & Corrections Performed

During this strict independent implementation audit, five potential defects were identified and corrected:

1. **Defect 1: Lack of `CACHE_MAX_ENTRIES` Capacity Bounds & LRU Eviction**: Implemented `self.max_entries` in `CacheService` and `_enforce_max_entries(conn)`. Uses LRU eviction (`ORDER BY last_accessed_at ASC LIMIT excess`). Verified in `test_32`.
2. **Defect 2: Disk I/O Overhead on Every Cache Lookup**: Added file mtime and size checking (`st_mtime_ns`, `st_size`). Avoids re-reading 4 files from disk on every query while still automatically detecting file edits. Verified in `test_35`.
3. **Defect 3: Lock Contention During Read Operations (`get()`)**: Refactored `_update_access_stats` to execute a single atomic `UPDATE` without `immediate_transaction` write lock.
4. **Defect 4: Potential Caching of Hallucinated ₹4,999 Fee**: Added explicit rejection in `validate_cacheable()` for any answer discussing certificate fees that mentions ₹4,999. Verified in `test_34`.
5. **Defect 5: Potential Acceptance of Superseded Doc02 as Sole Source**: Added rule in `_validate_stored_sources()` rejecting any cached entry where `len(sources) == 1` and the source is historical/superseded Doc02. Verified in `test_33`.

---

## 3. Dedicated HTTP Management Endpoints

In `backend/routes/chat_routes.py`:
- `GET /api/v1/cache/stats`: Returns JSON runtime metrics (`total_entries`, `active_entries`, `expired_entries`, `runtime_hits`, `runtime_misses`, `runtime_hit_rate`, `max_entries`, `default_ttl_seconds`).
- `POST /api/v1/cache/cleanup`: Triggers safe transaction purge of expired records and returns `cleaned_entries` count.
- Verified in `test_36_cache_stats_and_cleanup_endpoints`.

---

## 4. Complete Test Suite Execution Breakdown

```powershell
Set-Location "c:\Users\ACHYUTHA\OneDrive\Desktop\API M&H Dashboard\dpm101-chatbot"
.\venv\Scripts\python.exe -m pytest tests\ -v --tb=short
```

```
============================ 265 passed in 14.39s =============================
```

| Test Suite File | Module | Total Tests | Mocked | Real Integration | Status |
|---|:---:|:---:|:---:|:---:|:---:|
| `tests/test_cache_service.py` | **Module 7** | **36** | 3 | 33 | **36 Passed** |
| `tests/test_chat_service.py` | **Module 6** | **49** | 16 | 33 | **49 Passed** |
| `tests/test_document_service.py` | **Module 3** | **114** | 0 | 114 | **114 Passed** |
| `tests/test_policy_service.py` | **Module 5** | **39** | 22 | 17 | **39 Passed** |
| `tests/test_gemini_service.py` | **Module 4** | **21** | 19 | 2 | **21 Passed** |
| `tests/test_health.py` | **Module 2** | **6** | 0 | 6 | **6 Passed** |
| **Combined Regression Suite** | **Modules 1–7** | **265** | **60** | **205** | **265 Passed** |

---

## 5. Audit Verdict

# **AUDIT RESULT: FULL PASS**

Module 7 meets every requirement specified in the prompt. All five identified defects have been remediated with dedicated regression tests. All 265 tests pass cleanly in Python 3.11.9.

**Module 8 has NOT been started.** Awaiting your final review and approval.
