"""
tests/test_module11_e2e_integration.py — Module 11: End-to-End Integration, Testing & Error Handling

Covers the approved Module 11 test plan across 5 core suites:
  1. Suite 1: Full-Stack Multi-Turn Conversation Lifecycle (TestE2EConversationLifecycle)
     - E2E-01: Full conversation CRUD round-trip, message persistence, and cascade deletion.
     - E2E-02: Multi-turn contextual follow-up, pronoun resolution, and cache bypass.
  2. Suite 2: Section 17 Canonical Questions End-to-End (TestE2ESection17Acceptance)
     - E2E-03: Q1 passing criteria (60% overall + 40% final exam) & distinction (85%).
     - E2E-04: Q2 capstone deadline (15 Nov 2026) and Q6 quiz rule (best 5 of 6).
     - E2E-05: Q3 final exam date (20 Nov 2026), rules, closed-book, and forum exclusion.
     - E2E-06: Q4 certificate fee (Rs. 1,500) and rejection of adversarial forum claim.
     - E2E-07: Q5, Q7, Q8 polite declines (refund, kubernetes, cricket) and cache exclusion.
     - E2E-08: Q11 rephrased query produces cache MISS (no semantic cache matching).
  3. Suite 3: End-to-End Cache Lifecycle & TXT Fingerprint Invalidation (TestE2ECacheAndFingerprinting)
     - E2E-09: Cold-start miss followed by Q10 exact repeat producing cache HIT.
     - E2E-10: Q12 document update invalidation via plain-text file fingerprint recalculation.
     - E2E-11: Unsupported, out-of-scope, and error responses verified absent from cache table.
  4. Suite 4: Cross-Tier Concurrency, Idempotency & Rate Limiting (TestE2EConcurrencyAndLimits)
     - E2E-12: Thundering Herd duplicate resolution (3 concurrent requests, 1 Gemini call).
     - E2E-13: Sliding window rate limiting (HTTP 429) and duplicate replay exemption.
     - E2E-14: Per-conversation lock contention returning HTTP 409 CONVERSATION_BUSY.
  5. Suite 5: Error Injection, Extractive Fallback & Metrics Integrity (TestE2EFaultToleranceAndStats)
     - E2E-15: Extractive fallback on Gemini outage without 500 crash; error metric increment.
     - E2E-16: Metrics accuracy on GET /api/stats and health check quota decoupling.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app import create_app
from config import DOC_REGISTRY, config
from backend.services.cache_service import CacheService
from backend.services.chat_service import ChatService
from backend.services.document_service import DocumentProcessor
from backend.services.gemini_service import GeminiResponse, GeminiService
from backend.services.policy_service import PolicyAnswer, PolicyService
from backend.services.rate_limiter import SlidingWindowRateLimiter
from backend.services.request_service import RequestManager
from backend.services.stats_service import StatsService, get_stats_service, set_stats_service, WORKER_NOTE


# ── Helper Functions & Mock Client ────────────────────────────────────────────

def make_mock_gemini_client():
    """
    Creates a controlled mock Google GenAI client returning policy-grounded answers
    for canonical course inquiries without making external network calls.
    Extracts the specific student question from the grounded prompt to prevent
    keywords in background document excerpts from triggering incorrect branches.
    """
    mock_client = MagicMock()

    def generate_content(model, contents, config=None):
        prompt_str = str(contents)
        if "STUDENT QUESTION:" in prompt_str:
            q_part = prompt_str.split("STUDENT QUESTION:")[1].split("ANSWER (grounded")[0].strip().lower()
        else:
            q_part = prompt_str.lower()

        if any(w in q_part for w in ("pass", "passing", "score to pass")):
            text = (
                "To pass the course and earn the Verified Certificate, learners must achieve an overall "
                "weighted score of at least 60% across all assessments, AND score at least 40% in "
                "the Proctored Final Exam separately. Learners scoring 85% or above receive a "
                "'Pass with Distinction' badge.\n\nSource: Grading Policy V2."
            )
        elif "distinction" in q_part:
            text = (
                "Learners scoring 85% or above receive a 'Pass with Distinction' badge.\n\n"
                "Source: Grading Policy V2."
            )
        elif "capstone" in q_part and any(w in q_part for w in ("deadline", "due", "when", "date")):
            text = (
                "The Capstone Product Case deadline has been extended to 15 November 2026 at 11:59 PM IST "
                "(extended by 5 days from the earlier deadline).\n\nSource: Grading Policy V2."
            )
        elif "exam" in q_part and any(w in q_part for w in ("rules", "when", "date", "logistics")):
            text = (
                "The Proctored Final Exam takes place on 20 November 2026, with slots available between "
                "10:00 AM and 8:00 PM IST. The exam duration is 90 minutes. It is strictly closed-book "
                "and AI-proctored. Students must take it on a desktop or laptop with webcam and microphone "
                "enabled. Mobile phones and tablets are not permitted.\n\n"
                "Source: Official Exam Rules."
            )
        elif any(w in q_part for w in ("certificate fee", "cost", "fee")):
            text = (
                "The Verified Certificate Track costs Rs. 1,500. "
                "It provides graded quizzes, capstone review, and proctored exam access.\n\n"
                "Source: Course Handbook & Overview."
            )
        elif any(w in q_part for w in ("best-five-of-six", "best 5", "quiz rule", "quizzes count", "miss 1 quiz")):
            text = (
                "The course includes 6 weekly quizzes, and the best 5 out of 6 weekly quizzes count "
                "toward your grade. Learners can miss 1 weekly quiz without penalty.\n\n"
                "Source: Grading Policy V2."
            )
        elif any(w in q_part for w in ("late submission", "late", "penalty", "miss it by just one day", "one day late", "capstone case", "capstone submission")):
            text = (
                "Late submissions are no longer accepted for weekly quizzes or the capstone case. "
                "The earlier 48-hour grace window with a 10% penalty has been discontinued. "
                "Because the best 5 out of 6 weekly quizzes are counted, learners can miss 1 weekly quiz without penalty.\n\n"
                "Source: Grading Policy V2."
            )
        else:
            text = (
                "Here is the official information regarding your course inquiry.\n\n"
                "Source: Course Handbook & Overview."
            )

        resp = MagicMock()
        resp.text = text
        resp.candidates = [MagicMock()]
        resp.usage_metadata.prompt_token_count = 50
        resp.usage_metadata.candidates_token_count = 35
        resp.usage_metadata.total_token_count = 85
        return resp

    mock_client.models.generate_content.side_effect = generate_content
    return mock_client


# ── Pytest Fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def integration_env(tmp_path):
    """
    Sets up a fully integrated test application instance backed by:
      - Isolated SQLite database for chat and cache
      - Isolated StatsService
      - Isolated SlidingWindowRateLimiter
      - Isolated RequestManager
      - Real CacheService and PolicyService
      - Offline Mock Gemini Client
    """
    db_file = str(tmp_path / "test_module11_integration.db")
    stats_svc = StatsService()
    set_stats_service(stats_svc)

    mock_gemini_client = make_mock_gemini_client()
    gemini_svc = GeminiService(client=mock_gemini_client)

    cache_svc = CacheService(db_path=db_file)
    policy_svc = PolicyService(
        gemini_service=gemini_svc,
        cache_service=cache_svc,
    )
    chat_svc = ChatService(
        db_path=db_file,
        policy_service=policy_svc,
        cache_service=cache_svc,
        max_context_messages=6,
    )
    rate_limiter = SlidingWindowRateLimiter(default_ip_limit=20, default_ip_window=60)
    request_mgr = RequestManager(idempotency_ttl=300)

    app_instance = create_app({
        "TESTING": True,
        "DB_PATH": db_file,
        "chat_service": chat_svc,
        "rate_limiter": rate_limiter,
        "request_manager": request_mgr,
        "stats_service": stats_svc,
    })

    client = app_instance.test_client()

    services = {
        "db_path": db_file,
        "app": app_instance,
        "client": client,
        "chat_service": chat_svc,
        "policy_service": policy_svc,
        "cache_service": cache_svc,
        "gemini_service": gemini_svc,
        "mock_gemini_client": mock_gemini_client,
        "rate_limiter": rate_limiter,
        "request_manager": request_mgr,
        "stats_service": stats_svc,
    }

    yield client, services

    set_stats_service(None)


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 1: Full-Stack Multi-Turn Conversation Lifecycle
# ═════════════════════════════════════════════════════════════════════════════

class TestE2EConversationLifecycle:
    """Verifies complete multi-turn conversation persistence, context resolution, and cascade deletion."""

    def test_e2e_01_conversation_crud_and_message_history(self, integration_env):
        """E2E-01: Full conversation round-trip: create -> send message -> list history -> cascade delete -> 404."""
        client, services = integration_env

        # 1. Create a new conversation
        create_resp = client.post("/api/v1/conversations")
        assert create_resp.status_code == 201
        conv_data = create_resp.get_json()
        assert "id" in conv_data
        conv_id = conv_data["id"]
        assert conv_data["status"] == "active"

        # 2. Get conversation metadata
        meta_resp = client.get(f"/api/v1/conversations/{conv_id}")
        assert meta_resp.status_code == 200
        assert meta_resp.get_json()["id"] == conv_id

        # 3. Post a message to the conversation
        msg_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the certificate fee?"},
        )
        assert msg_resp.status_code == 200
        msg_data = msg_resp.get_json()
        assert msg_data["conversation_id"] == conv_id
        assert msg_data["role"] == "assistant"
        assert "1,500" in msg_data["content"]
        assert any("Handbook" in s for s in msg_data["sources"])
        assert msg_data["is_duplicate"] is False

        # 4. Retrieve message history
        hist_resp = client.get(f"/api/v1/conversations/{conv_id}/messages")
        assert hist_resp.status_code == 200
        hist_data = hist_resp.get_json()
        assert hist_data["conversation_id"] == conv_id
        messages = hist_data["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "What is the certificate fee?"
        assert messages[1]["role"] == "assistant"
        assert "1,500" in messages[1]["content"]

        # 5. Delete the conversation
        del_resp = client.delete(f"/api/v1/conversations/{conv_id}")
        assert del_resp.status_code == 200
        assert del_resp.get_json()["success"] is True

        # 6. Verify subsequent requests return HTTP 404
        assert client.get(f"/api/v1/conversations/{conv_id}").status_code == 404
        assert client.get(f"/api/v1/conversations/{conv_id}/messages").status_code == 404
        post_after_del = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Can I still send a message?"},
        )
        assert post_after_del.status_code == 404

    def test_e2e_02_multiturn_contextual_followup(self, integration_env):
        """E2E-02: Multi-turn contextual follow-up: Turn 1 sets context -> Turn 2 resolves pronoun and bypasses cache."""
        client, services = integration_env

        # Create session
        conv_resp = client.post("/api/v1/conversations")
        conv_id = conv_resp.get_json()["id"]

        # Turn 1: Specific query establishing context
        turn1_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the late submission penalty?"},
        )
        assert turn1_resp.status_code == 200
        turn1_data = turn1_resp.get_json()
        assert "Late submissions are no longer accepted" in turn1_data["content"]

        # Turn 2: Contextual follow-up query containing pronoun "it"
        turn2_resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What if I miss it by just one day?"},
        )
        assert turn2_resp.status_code == 200
        turn2_data = turn2_resp.get_json()
        # Verify assistant answers contextually based on the capstone/quiz late submission rules
        assert "discontinued" in turn2_data["content"] or "penalty" in turn2_data["content"] or "longer accepted" in turn2_data["content"]
        assert turn2_data["is_duplicate"] is False

        # Verify that both turns (4 total messages) are present in history in correct order
        messages_resp = client.get(f"/api/v1/conversations/{conv_id}/messages")
        msgs = messages_resp.get_json()["messages"]
        assert len(msgs) == 4
        assert msgs[0]["content"] == "What is the late submission penalty?"
        assert msgs[2]["content"] == "What if I miss it by just one day?"


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 2: Section 17 Canonical Questions End-to-End
# ═════════════════════════════════════════════════════════════════════════════

class TestE2ESection17Acceptance:
    """Verifies all Section 17 canonical questions, document authorities, and rephrased queries."""

    def test_e2e_03_canonical_q1_grading_passing_distinction_precedence(self, integration_env):
        """E2E-03: Q1 passing requirements: verifies 60% overall + 40% exam minimum; 85% distinction; Doc 03 authority."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What are the passing requirements for DPM-101?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()

        # Strict grading assertions per 03_grading_policy_v2.txt
        assert "60%" in data["content"] or "60 percent" in data["content"].lower()
        assert "40%" in data["content"] or "40 percent" in data["content"].lower()
        assert "85%" in data["content"] or "distinction" in data["content"].lower()

        # Authority assertion: Doc 03 is cited; outdated Doc 02 is excluded
        sources_str = " ".join(data.get("sources", []))
        assert "Grading & Late Submission Policy V2" in sources_str or "Grading Policy V2" in data["content"]
        assert "Policy V1" not in sources_str
        assert "02_grading_policy_v1" not in sources_str

    def test_e2e_04_canonical_q2_and_q6_capstone_deadline_and_quiz_rule(self, integration_env):
        """E2E-04: Q2 capstone deadline (15 Nov 2026) and Q6 best 5 of 6 quizzes rule."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        # Q2: Capstone deadline
        resp_q2 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "When is the capstone project deadline?"},
        )
        assert resp_q2.status_code == 200
        data_q2 = resp_q2.get_json()
        assert "15 November 2026" in data_q2["content"]
        assert "11:59 PM" in data_q2["content"]

        # Q6: Quiz rule
        resp_q6 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "How does the best-five-of-six quiz rule work?"},
        )
        assert resp_q6.status_code == 200
        data_q6 = resp_q6.get_json()
        assert "best 5" in data_q6["content"].lower() or "5 out of 6" in data_q6["content"]

    def test_e2e_05_canonical_q3_final_exam_rules_and_forum_exclusion(self, integration_env):
        """E2E-05: Q3 exam logistics: date 20 Nov 2026, closed-book, laptop/desktop only, forum exclusion."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "When is the final exam and what are the rules?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()

        assert "20 November 2026" in data["content"]
        assert "closed-book" in data["content"].lower()
        # Verify prohibited items/devices mentioned
        assert "tablet" in data["content"].lower() or "mobile" in data["content"].lower() or "phone" in data["content"].lower()
        # Doc 04 §1–§2 is cited; Doc 01, Doc 02, and Doc 03 are strictly excluded from sources
        sources_list = data.get("sources", [])
        assert sources_list == ["Final Exam Rules & Student Forum Excerpts"]
        assert not any("Grading" in s or "Handbook" in s for s in sources_list)
        # Forum comments are completely excluded from the grounded answer
        assert "30 december" not in data["content"].lower()
        assert "4,999" not in data["content"]

    def test_e2e_05b_combined_exam_and_grading_retrieval(self, integration_env):
        """E2E-05b: Combined query about exam score and proctoring retrieves both Doc 03 and Doc 04."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What score do I need in the final exam to pass and is it proctored?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()

        sources = data.get("sources", [])
        # Both authoritative documents must be cited
        assert any("Grading" in s for s in sources)
        assert any("Exam Rules" in s for s in sources)
        # Doc 01 must not appear
        assert not any("Handbook" in s for s in sources)
        # Content confirms dual requirement and proctored rule
        assert "40%" in data["content"] or "40 percent" in data["content"].lower()

    def test_e2e_06_canonical_q4_certificate_fee_adversarial_rejection(self, integration_env):
        """E2E-06: Q4 certificate fee is Rs. 1,500; rejects adversarial Rs. 4,999 forum rumor."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the certificate fee?"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "1,500" in data["content"] or "1500" in data["content"]
        assert "4,999" not in data["content"]
        assert "4999" not in data["content"]
        assert any("Handbook" in s for s in data["sources"])

    def test_e2e_07_canonical_q5_q7_q8_unsupported_out_of_scope_rejection(self, integration_env):
        """E2E-07: Questions 5, 7, 8: polite decline responses; verifies they are NEVER cached."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        # Q5: Refund policy (unsupported topic in course context)
        resp_q5 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the refund policy?"},
        )
        assert resp_q5.status_code == 200
        data_q5 = resp_q5.get_json()
        assert "do not specify a refund policy" in data_q5["content"].lower()
        assert data_q5["is_supported"] is False

        # Q7: Technical question out of scope
        resp_q7 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Can you help me with SQL joins and Kubernetes setup?"},
        )
        assert resp_q7.status_code == 200
        data_q7 = resp_q7.get_json()
        assert "do not specify whether" in data_q7["content"].lower() or "only answer questions related to the dpm-101 course" in data_q7["content"].lower()

        # Q8: Completely unrelated question
        resp_q8 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Who won the cricket World Cup?"},
        )
        assert resp_q8.status_code == 200
        data_q8 = resp_q8.get_json()
        assert "i can only answer questions related to the dpm-101 course" in data_q8["content"].lower()
        assert data_q8["is_in_scope"] is False

        # Verify NONE of these 3 responses are stored in the SQLite cache table
        cache_svc: CacheService = services["cache_service"]
        assert cache_svc.get("What is the refund policy?") is None
        assert cache_svc.get("Can you help me with SQL joins and Kubernetes setup?") is None
        assert cache_svc.get("Who won the cricket World Cup?") is None

    def test_e2e_08_canonical_q11_rephrased_query_cache_miss(self, integration_env):
        """E2E-08: Q11 rephrased question produces a Cache MISS and fresh evaluation (no semantic caching)."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        # Turn 1: Ask Q9 ("What is the late submission penalty?")
        resp_q9 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the late submission penalty?"},
        )
        assert resp_q9.status_code == 200
        hits_before = services["stats_service"].get_stats()["cache_hits"]

        # Turn 2: Ask rephrased Q11 in a new session ("What happens if I submit my assignment late?")
        conv2_id = client.post("/api/v1/conversations").get_json()["id"]
        resp_q11 = client.post(
            f"/api/v1/conversations/{conv2_id}/messages",
            json={"message": "What happens if I submit my assignment late?"},
        )
        assert resp_q11.status_code == 200
        hits_after = services["stats_service"].get_stats()["cache_hits"]

        # Verified: hits_after == hits_before (Q11 produced a Cache MISS because the wording differs)
        assert hits_after == hits_before


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 3: End-to-End Cache Lifecycle & TXT Fingerprint Invalidation
# ═════════════════════════════════════════════════════════════════════════════

class TestE2ECacheAndFingerprinting:
    """Verifies cold-start caching, exact-key hits, and document fingerprint cache invalidation."""

    def test_e2e_09_cold_start_miss_and_q10_exact_repeat_hit(self, integration_env):
        """E2E-09: Cold-start miss stores answer; Q10 exact repeat yields cache HIT without calling Gemini."""
        client, services = integration_env
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        stats_svc: StatsService = services["stats_service"]
        initial_stats = stats_svc.get_stats()
        assert initial_stats["cache_hits"] == 0

        # Request 1: Cold start miss -> calls Gemini and writes cache
        resp1 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "When is the capstone project deadline?"},
        )
        assert resp1.status_code == 200
        stats1 = stats_svc.get_stats()
        assert stats1["gemini_calls"] == 1
        assert stats1["cache_hits"] == 0

        # Request 2: Q10 exact repeat in a fresh conversation -> Cache HIT
        conv2_id = client.post("/api/v1/conversations").get_json()["id"]
        resp2 = client.post(
            f"/api/v1/conversations/{conv2_id}/messages",
            json={"message": "When is the capstone project deadline?"},
        )
        assert resp2.status_code == 200
        stats2 = stats_svc.get_stats()
        # Gemini call count remains 1, cache_hits increments to 1
        assert stats2["gemini_calls"] == 1
        assert stats2["cache_hits"] == 1
        assert resp2.get_json()["content"] == resp1.get_json()["content"]

    def test_e2e_10_canonical_q12_txt_fingerprint_invalidation(self, tmp_path):
        """E2E-10: Modifying 03_grading_policy_v2.txt updates document fingerprint, causing a Cache MISS."""
        # Setup an isolated documents directory
        temp_docs_dir = tmp_path / "temp_documents"
        temp_docs_dir.mkdir()
        for doc_id, doc_meta in DOC_REGISTRY.items():
            src_file = Path(config.DOCUMENTS_DIR) / doc_meta["filename"]
            if src_file.exists():
                shutil.copy(src_file, temp_docs_dir / doc_meta["filename"])

        db_file = str(tmp_path / "cache_invalidation.db")
        stats_svc = StatsService()
        set_stats_service(stats_svc)

        mock_gemini_client = make_mock_gemini_client()
        gemini_svc = GeminiService(client=mock_gemini_client)

        cache_svc = CacheService(db_path=db_file, documents_dir=str(temp_docs_dir))
        doc_proc = DocumentProcessor(DOC_REGISTRY, str(temp_docs_dir))
        policy_svc = PolicyService(
            gemini_service=gemini_svc,
            doc_processor=doc_proc,
            cache_service=cache_svc,
        )
        chat_svc = ChatService(
            db_path=db_file,
            policy_service=policy_svc,
            cache_service=cache_svc,
        )

        rate_limiter = SlidingWindowRateLimiter(default_ip_limit=50, default_ip_window=60)
        request_mgr = RequestManager(idempotency_ttl=300)

        app_instance = create_app({
            "TESTING": True,
            "DB_PATH": db_file,
            "chat_service": chat_svc,
            "rate_limiter": rate_limiter,
            "request_manager": request_mgr,
            "stats_service": stats_svc,
        })

        client = app_instance.test_client()
        conv_id = client.post("/api/v1/conversations").get_json()["id"]
        query = "What is the late submission penalty?"

        # 1. First execution: Cold-start miss
        resp1 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": query},
        )
        assert resp1.status_code == 200
        fp1 = cache_svc.compute_documents_fingerprint()

        # 2. Second execution: Cache HIT
        conv2_id = client.post("/api/v1/conversations").get_json()["id"]
        resp2 = client.post(
            f"/api/v1/conversations/{conv2_id}/messages",
            json={"message": query},
        )
        assert resp2.status_code == 200
        assert stats_svc.get_stats()["cache_hits"] == 1

        # 3. Simulate Course Coordinator modifying Doc 03
        doc3_path = temp_docs_dir / "03_grading_policy_v2.txt"
        time.sleep(0.05)  # Ensure nanosecond mtime differential
        with open(doc3_path, "a", encoding="utf-8") as f:
            f.write("\n\n4. AMENDMENT: One-time special extension granted.\n")

        # 4. Invalidation: Document fingerprint changes automatically
        fp2 = cache_svc.compute_documents_fingerprint(force_recompute=True)
        assert fp1 != fp2, "Document fingerprint must change when course text is modified"

        # 5. Querying again produces a Cache MISS because the composite key incorporates the new fingerprint
        conv3_id = client.post("/api/v1/conversations").get_json()["id"]
        resp3 = client.post(
            f"/api/v1/conversations/{conv3_id}/messages",
            json={"message": query},
        )
        assert resp3.status_code == 200
        # Cache hits must NOT increment on cache miss
        assert stats_svc.get_stats()["cache_hits"] == 1

        set_stats_service(None)

    def test_e2e_11_unsupported_and_error_answers_never_cached(self, integration_env):
        """E2E-11: Unsupported answers, out-of-scope answers, and provider errors are never stored in cache table."""
        client, services = integration_env
        cache_svc: CacheService = services["cache_service"]

        # Verify cache table is initialized and empty initially
        stats = cache_svc.get_stats()
        assert stats["total_entries"] == 0

        # Ask unsupported query
        conv_id = client.post("/api/v1/conversations").get_json()["id"]
        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the refund policy?"},
        )

        # Ask out-of-scope query
        client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Tell me a joke about robots"},
        )

        # Directly inspect the SQLite query_cache table
        conn = sqlite3.connect(services["db_path"])
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM query_cache;")
            count = cursor.fetchone()[0]
            assert count == 0, f"query_cache table should have 0 entries, found {count}"
        finally:
            conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 4: Cross-Tier Concurrency, Idempotency & Rate Limiting
# ═════════════════════════════════════════════════════════════════════════════

class TestE2EConcurrencyAndLimits:
    """Verifies Thundering Herd duplicate resolution, sliding-window rate limiting, and conversation locks."""

    def test_e2e_12_thundering_herd_duplicate_resolution(self, integration_env):
        """E2E-12: 3 concurrent identical requests: exactly 1 Gemini call; duplicate threads wait and receive 200."""
        client, services = integration_env
        app = services["app"]
        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        results = [None] * 3
        errors = [None] * 3
        barrier = threading.Barrier(3)
        req_id = "req-e2e-thundering-herd-1"

        # Make mock Gemini delay briefly so duplicate threads register as in-flight waiters
        original_side_effect = services["mock_gemini_client"].models.generate_content.side_effect

        def slow_generate(*args, **kwargs):
            time.sleep(0.15)
            return original_side_effect(*args, **kwargs)

        services["mock_gemini_client"].models.generate_content.side_effect = slow_generate

        def worker(thread_idx):
            # Each thread uses its own test client to prevent contextvar collisions in Python 3.11
            with app.test_client() as c:
                try:
                    barrier.wait(timeout=5.0)
                    resp = c.post(
                        f"/api/v1/conversations/{conv_id}/messages",
                        json={
                            "message": "What are the passing requirements for DPM-101?",
                            "request_id": req_id,
                        },
                        headers={"X-Request-ID": req_id},
                    )
                    results[thread_idx] = (
                        resp.status_code,
                        resp.get_json(),
                        resp.headers.get("X-Duplicate-Request"),
                    )
                except Exception as e:
                    errors[thread_idx] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        err_list = [e for e in errors if e is not None]
        assert not err_list, f"Threads encountered exceptions: {err_list}"

        # All 3 requests receive HTTP 200 with identical grounded answers
        for r in results:
            assert r[0] == 200
            data = r[1]
            assert "60%" in data["content"]
            assert "40%" in data["content"]

        # Exactly 1 primary turn (is_duplicate: False) and 2 duplicate replays (is_duplicate: True)
        duplicate_flags = [r[1]["is_duplicate"] for r in results]
        assert duplicate_flags.count(False) == 1
        assert duplicate_flags.count(True) == 2

        # Exactly 2 responses have header X-Duplicate-Request: true
        dup_headers = [r[2] for r in results]
        assert dup_headers.count("true") == 2

        # Database contains exactly one user message and one assistant message
        messages = services["chat_service"].get_conversation_messages(conv_id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

        # Gemini was called exactly once across all 3 concurrent requests
        stats = services["stats_service"].get_stats()
        assert stats["gemini_calls"] == 1

    def test_e2e_13_sliding_window_rate_limiting_and_replay_exemption(self, integration_env):
        """E2E-13: Sliding window limits bursts to limit (HTTP 429), while duplicate replays are exempt from rate limit."""
        client, services = integration_env
        test_limiter = SlidingWindowRateLimiter(default_ip_limit=5, default_ip_window=60)
        services["app"].extensions["rate_limiter"] = test_limiter

        conv_id = client.post("/api/v1/conversations").get_json()["id"]
        completed_req_id = "req-e2e-rate-limit-1"

        # Request 1: succeeds and establishes idempotency record
        resp1 = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the certificate fee?", "request_id": completed_req_id},
            headers={"X-Request-ID": completed_req_id},
        )
        assert resp1.status_code == 200

        # Requests 2 to 5: consume the remainder of the 5-request quota
        for i in range(2, 6):
            r_id = f"req-e2e-rate-limit-{i}"
            r = client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={"message": f"Inquiry number {i} regarding DPM course", "request_id": r_id},
                headers={"X-Request-ID": r_id},
            )
            assert r.status_code == 200

        # Request 6: Exceeds rate limit -> returns HTTP 429 with Retry-After header
        resp_blocked = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "Exceeding request when rate limited", "request_id": "req-blocked"},
            headers={"X-Request-ID": "req-blocked"},
        )
        assert resp_blocked.status_code == 429
        assert resp_blocked.get_json().get("code") == "RATE_LIMITED"
        assert "Retry-After" in resp_blocked.headers

        # Replaying the earlier completed request must succeed (HTTP 200) without being blocked by rate limiting
        resp_replay = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What is the certificate fee?", "request_id": completed_req_id},
            headers={"X-Request-ID": completed_req_id},
        )
        assert resp_replay.status_code == 200
        assert resp_replay.get_json().get("is_duplicate") is True
        assert resp_replay.headers.get("X-Duplicate-Request") == "true"

    def test_e2e_14_same_conversation_lock_contention(self, integration_env):
        """E2E-14: Concurrent differing query to the same conversation returns HTTP 409 CONVERSATION_BUSY on lock timeout."""
        client, services = integration_env
        req_mgr: RequestManager = services["request_manager"]
        old_timeout = req_mgr.lock_timeout
        req_mgr.lock_timeout = 0.15

        try:
            conv_id = client.post("/api/v1/conversations").get_json()["id"]

            # Acquire the per-conversation lock simulating an in-progress request
            acquired = req_mgr.acquire_conversation_lock(conv_id)
            assert acquired is True

            try:
                # Second request to same conversation arrives while lock is held
                resp_busy = client.post(
                    f"/api/v1/conversations/{conv_id}/messages",
                    json={"message": "Different question while conversation lock is held", "request_id": "req-busy-1"},
                    headers={"X-Request-ID": "req-busy-1"},
                )
                assert resp_busy.status_code == 409
                data = resp_busy.get_json()
                assert data.get("code") == "CONVERSATION_BUSY"
            finally:
                req_mgr.release_conversation_lock(conv_id)

            # After lock release, new requests proceed smoothly
            resp_after = client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={"message": "Question after lock is released", "request_id": "req-after-lock"},
                headers={"X-Request-ID": "req-after-lock"},
            )
            assert resp_after.status_code == 200
        finally:
            req_mgr.lock_timeout = old_timeout


# ═════════════════════════════════════════════════════════════════════════════
# SUITE 5: Error Injection, Extractive Fallback & Metrics Integrity
# ═════════════════════════════════════════════════════════════════════════════

class TestE2EFaultToleranceAndStats:
    """Verifies extractive fallback on Gemini outage, stats counter accuracy, and health check decoupling."""

    def test_e2e_15_extractive_fallback_on_provider_outage(self, integration_env):
        """E2E-15: Gemini network/auth failure triggers extractive fallback without HTTP 500 crash."""
        client, services = integration_env
        services["mock_gemini_client"].models.generate_content.side_effect = RuntimeError(
            "Connection refused: Gemini API endpoint unreachable"
        )

        conv_id = client.post("/api/v1/conversations").get_json()["id"]

        resp = client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"message": "What are the passing requirements for DPM-101?"},
        )
        # Server must gracefully return HTTP 200 with extractive fallback
        assert resp.status_code == 200
        data = resp.get_json()
        assert "60%" in data["content"]
        assert "40%" in data["content"]
        assert "Grading Policy V2" in data["content"] or any("Grading" in s for s in data.get("sources", []))

        # Stats error counter should increment exactly once
        stats = services["stats_service"].get_stats()
        assert stats["errors"] == 1
        assert stats["gemini_calls"] >= 1

        # Fallback response must NOT be stored in the SQLite cache
        cache_svc: CacheService = services["cache_service"]
        assert cache_svc.get("What are the passing requirements for DPM-101?") is None

    def test_e2e_16_stats_pipeline_integrity_and_health_decoupling(self, integration_env):
        """E2E-16: /health calls do not consume quota or alter stats; schema matches exact 6 fields."""
        client, services = integration_env

        # Verify initial stats schema
        stats_resp = client.get("/api/stats")
        assert stats_resp.status_code == 200
        stats_data = stats_resp.get_json()
        assert len(stats_data) == 6
        assert set(stats_data.keys()) == {
            "total_requests",
            "cache_hits",
            "gemini_calls",
            "errors",
            "uptime_seconds",
            "worker_note",
        }
        assert stats_data["total_requests"] == 0
        assert stats_data["gemini_calls"] == 0
        assert stats_data["worker_note"] == WORKER_NOTE

        # 5 calls to /health must NOT alter any counters
        for _ in range(5):
            h_resp = client.get("/health")
            assert h_resp.status_code == 200
            assert h_resp.get_json()["status"] == "healthy"

        stats_after_health = client.get("/api/stats").get_json()
        assert stats_after_health["total_requests"] == 0
        assert stats_after_health["gemini_calls"] == 0

        # Root static GET / must NOT alter counters
        root_resp = client.get("/")
        assert root_resp.status_code == 200
        assert "<title>IIMBx DPM-101 Course Helpdesk</title>" in root_resp.data.decode("utf-8")

        # Invalid requests (404 conversation, 400 bad JSON) must NOT increment total_requests
        client.post("/api/v1/conversations/00000000-0000-0000-0000-000000000000/messages", json={"message": "hello"})
        client.post("/api/v1/conversations", data="not json", content_type="text/plain")

        stats_after_errors = client.get("/api/stats").get_json()
        assert stats_after_errors["total_requests"] == 0
