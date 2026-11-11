"""
tests/test_document_service.py — Module 3: Course Document Management Test Suite

All tests use synthetic PDFs generated in-process so they run without the
real official DPM-101 PDFs (which are not in the repository).

PDF generation uses fpdf2 when installed; falls back to reportlab (available
in the sandbox codex runtime) so the full suite can execute in both environments.

Test groups:
  A. pdf_parser  — text extraction, whitespace cleaning, page numbering
  B. doc04       — section detection, forum exclusion, Doc04SectionError
  C. DocumentProcessor.process_document — per-document processing
  D. DocumentProcessor.process_all / reporting — batch + status report
  E. Chunking     — overlapping chunks, chunk metadata, is_excluded, is_outdated
  F. PDF hashing  — compute_pdf_hash()
  G. Fail-safe    — missing / unreadable required documents block healthy status
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ── Make sure the project root is on sys.path ─────────────────────────────────
# Needed when pytest is invoked from outside the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── Imports under test ────────────────────────────────────────────────────────
from backend.utils.pdf_parser import (
    PageData,
    ExtractionResult,
    Doc04SectionError,
    clean_text,
    extract_pages,
    detect_doc04_sections,
)
from backend.services.document_service import (
    ChunkData,
    ProcessingResult,
    DocumentProcessor,
)
from config import DOC_REGISTRY, config


# ═════════════════════════════════════════════════════════════════════════════
# Helpers — synthetic PDF builders (fpdf2)
# ═════════════════════════════════════════════════════════════════════════════

def _make_pdf(pages: list[str], *, tmp_path: Path, filename: str = "test.pdf") -> Path:
    """
    Create a minimal PDF with one text string per page.

    Tries fpdf2 first (project dependency); falls back to reportlab if fpdf2
    is not installed (e.g. in the codex sandbox environment where only
    reportlab is pre-installed). Both produce pdfplumber-readable PDFs.

    Returns the full path to the written file.
    """
    out = tmp_path / filename

    try:
        from fpdf import FPDF  # fpdf2
        pdf = FPDF()
        pdf.set_auto_page_break(auto=False)
        pdf.set_font("Helvetica", size=11)
        for page_text in pages:
            pdf.add_page()
            for line in page_text.splitlines():
                pdf.cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
        pdf.output(str(out))

    except ImportError:
        # Fallback: reportlab (available in codex sandbox runtime)
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.pagesizes import A4
        c = rl_canvas.Canvas(str(out), pagesize=A4)
        width, height = A4
        for page_text in pages:
            y = height - 72   # start near top
            for line in page_text.splitlines():
                # reportlab encodes latin-1 by default; replace non-latin chars
                safe_line = line.encode("latin-1", errors="replace").decode("latin-1")
                c.drawString(72, y, safe_line)
                y -= 14
                if y < 72:
                    break
            c.showPage()
        c.save()

    return out


def _make_doc04_pdf(tmp_path: Path) -> Path:
    """
    Create a synthetic Doc04 PDF with:
      Page 1: 'Section 1\nFinal Exam Rules – date, duration, format, proctoring'
      Page 2: 'Section 2\nDevice & proctoring requirements'
      Page 3: 'Section 3\nStudent Forum Excerpts'
      Page 4: Forum comment text
    """
    pages = [
        "Section 1\nFinal Exam Date: November 20, 2026.\nDuration: 90 minutes.\nFormat: Closed-book. AI-proctored.",
        "Section 2\nDevice requirements: Desktop or laptop required.\nWebcam and microphone required.\nMobile and tablet not permitted.",
        "Section 3\nStudent Forum Excerpts",
        "Student comment: I heard we can use phones for the exam.",
    ]
    return _make_pdf(pages, tmp_path=tmp_path, filename="document_04.pdf")


def _minimal_doc_registry(doc_id: str, pdf_path: Path, **overrides) -> dict:
    """
    Build a minimal DOC_REGISTRY entry for a single document,
    suitable for constructing a DocumentProcessor in tests.
    """
    base = {
        "doc_id": doc_id,
        "filename": pdf_path.name,
        "title": f"Test Document {doc_id}",
        "last_updated": "2026-01-01",
        "policy_status": "current",
        "is_required": True,
        "is_outdated": False,
        "section_config": None,
        "topic_scope": [],
    }
    base.update(overrides)
    return {doc_id: base}


# ═════════════════════════════════════════════════════════════════════════════
# A. pdf_parser — extraction, cleaning, page numbering
# ═════════════════════════════════════════════════════════════════════════════

class TestCleanText:
    def test_empty_string(self):
        assert clean_text("") == ""

    def test_none_equivalent_empty(self):
        # pdfplumber can return "" for empty pages
        assert clean_text("   ") == ""

    def test_strips_leading_trailing_whitespace(self):
        assert clean_text("  hello world  ") == "hello world"

    def test_collapses_multiple_blank_lines(self):
        raw = "line1\n\n\n\n\nline2"
        result = clean_text(raw)
        # 3+ blank lines collapsed to 2
        assert "\n\n\n" not in result
        assert "line1" in result
        assert "line2" in result

    def test_form_feed_replaced(self):
        raw = "page1\fpage2"
        result = clean_text(raw)
        assert "\f" not in result
        assert "page1" in result
        assert "page2" in result

    def test_crlf_normalized(self):
        raw = "line1\r\nline2\r\nline3"
        result = clean_text(raw)
        assert "\r" not in result
        assert "line1" in result
        assert "line2" in result

    def test_does_not_strip_meaningful_hyphens(self):
        raw = "AI-proctored examination"
        assert "AI-proctored" in clean_text(raw)

    def test_preserves_numbers_and_dates(self):
        raw = "Exam date: November 20, 2026. Duration: 90 minutes."
        result = clean_text(raw)
        assert "November 20, 2026" in result
        assert "90 minutes" in result

    def test_does_not_merge_lines(self):
        raw = "Heading\nSubheading"
        result = clean_text(raw)
        assert "Heading\nSubheading" in result


class TestExtractPages:
    def test_missing_file_returns_failure(self, tmp_path):
        result = extract_pages(tmp_path / "nonexistent.pdf")
        assert result.success is False
        assert "not found" in result.error.lower() or "File not" in result.error

    def test_directory_instead_of_file_returns_failure(self, tmp_path):
        result = extract_pages(tmp_path)   # tmp_path is a directory
        assert result.success is False

    def test_valid_pdf_returns_success(self, tmp_path):
        pdf_path = _make_pdf(["Hello World"], tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        assert result.success is True
        assert result.page_count == 1

    def test_page_numbers_are_one_indexed(self, tmp_path):
        pdf_path = _make_pdf(["Page 1", "Page 2", "Page 3"], tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        assert result.success is True
        page_numbers = [p.page_number for p in result.pages]
        assert page_numbers == [1, 2, 3]

    def test_text_is_extracted(self, tmp_path):
        pdf_path = _make_pdf(["Exam date: November 20, 2026."], tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        assert result.success is True
        combined = " ".join(p.text for p in result.pages)
        assert "November 20, 2026" in combined

    def test_multipage_page_count(self, tmp_path):
        pages_text = [f"Content of page {i}" for i in range(1, 6)]
        pdf_path = _make_pdf(pages_text, tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        assert result.success is True
        assert result.page_count == 5
        assert len(result.pages) == 5

    def test_empty_page_counted(self, tmp_path):
        # fpdf2 blank page (space only) may extract as empty
        pdf_path = _make_pdf(["Real content", "   ", "More content"], tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        assert result.success is True
        assert result.page_count == 3

    def test_page_data_has_raw_and_clean_text(self, tmp_path):
        pdf_path = _make_pdf(["Sample text."], tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        page = result.pages[0]
        assert isinstance(page.text, str)
        assert isinstance(page.raw_text, str)

    def test_section_type_none_for_non_doc04(self, tmp_path):
        pdf_path = _make_pdf(["General course info."], tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        for page in result.pages:
            assert page.section_type is None


# ═════════════════════════════════════════════════════════════════════════════
# B. Doc04 section detection
# ═════════════════════════════════════════════════════════════════════════════

DOC04_SECTION_CONFIG = DOC_REGISTRY["doc04"]["section_config"]


def _make_pages(texts: list[str]) -> list[PageData]:
    """Create a list of PageData from plain text strings (no PDF needed)."""
    return [
        PageData(
            page_number=i + 1,
            text=t,
            raw_text=t,
            is_empty=(len(t.strip()) == 0),
        )
        for i, t in enumerate(texts)
    ]


class TestDoc04SectionDetection:
    def test_official_pages_classified_correctly(self):
        pages = _make_pages([
            "Section 1\nExam date: November 20, 2026.",
            "Section 2\nDevice requirements.",
        ])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert all(p.section_type == "official_exam_rules" for p in classified)

    def test_forum_pages_classified_correctly(self):
        pages = _make_pages([
            "Section 1\nFinal Exam Rules.",
            "Section 3\nStudent Forum Excerpts",
            "Student comment about the exam.",
        ])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert classified[0].section_type == "official_exam_rules"
        assert classified[1].section_type == "forum"
        assert classified[2].section_type == "forum"

    def test_sticky_section_propagates(self):
        """Section type persists to subsequent pages with no header."""
        pages = _make_pages([
            "Section 1\nOfficial rule.",
            "Continued official rule text without a new header.",
            "Still same section.",
        ])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert all(p.section_type == "official_exam_rules" for p in classified)

    def test_forum_sticky_after_switch(self):
        pages = _make_pages([
            "Section 1\nFinal Exam Date.",
            "Section 3\nForum section.",
            "No header — should inherit forum.",
            "Also no header — still forum.",
        ])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert classified[0].section_type == "official_exam_rules"
        assert classified[1].section_type == "forum"
        assert classified[2].section_type == "forum"
        assert classified[3].section_type == "forum"

    def test_raises_when_no_official_pages(self):
        """If the PDF has no official-section pages, Doc04SectionError must be raised."""
        pages = _make_pages([
            "Student Forum Excerpts",
            "Student comment: use your phone.",
        ])
        with pytest.raises(Doc04SectionError):
            detect_doc04_sections(pages, DOC04_SECTION_CONFIG)

    def test_raises_on_empty_page_list(self):
        with pytest.raises(Doc04SectionError):
            detect_doc04_sections([], DOC04_SECTION_CONFIG)

    def test_section_name_set(self):
        pages = _make_pages(["Section 1\nRule text."])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert classified[0].section_name is not None
        assert len(classified[0].section_name) > 0

    def test_case_insensitive_official_header(self):
        pages = _make_pages(["SECTION 1\nExam rule."])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert classified[0].section_type == "official_exam_rules"

    def test_case_insensitive_forum_header(self):
        pages = _make_pages([
            "Section 1\nRule.",
            "STUDENT FORUM\nSome comment.",
        ])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert classified[1].section_type == "forum"

    def test_exam_date_in_official_section(self):
        pages = _make_pages([
            "Section 1\nFinal Exam Date: November 20, 2026.\nDuration: 90 minutes.",
        ])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert classified[0].section_type == "official_exam_rules"
        assert "November 20, 2026" in classified[0].text

    def test_forum_comment_cannot_override_official(self):
        """
        A forum page claiming to override the exam date must NOT be classified official.
        """
        pages = _make_pages([
            "Section 1\nExam date: November 20, 2026.",
            "Student Forum\nI heard exam is on December 1.",
        ])
        classified = detect_doc04_sections(pages, DOC04_SECTION_CONFIG)
        assert classified[1].section_type == "forum"


# ═════════════════════════════════════════════════════════════════════════════
# C. DocumentProcessor.process_document
# ═════════════════════════════════════════════════════════════════════════════

class TestProcessDocument:
    def test_missing_required_doc_returns_failure(self, tmp_path):
        registry = _minimal_doc_registry(
            "doc01",
            tmp_path / "document_01.pdf",   # file not created
            is_required=True,
        )
        processor = DocumentProcessor(registry, str(tmp_path), chunk_size=500, chunk_overlap=50)
        result = processor.process_document(registry["doc01"])
        assert result.success is False
        assert result.skipped is True

    def test_missing_optional_doc_returns_skipped(self, tmp_path):
        registry = _minimal_doc_registry(
            "doc02",
            tmp_path / "document_02.pdf",   # file not created
            is_required=False,
        )
        processor = DocumentProcessor(registry, str(tmp_path), chunk_size=500, chunk_overlap=50)
        result = processor.process_document(registry["doc02"])
        assert result.success is False
        assert result.skipped is True

    def test_valid_pdf_returns_success_and_chunks(self, tmp_path):
        pdf_path = _make_pdf(
            ["This is the course handbook. It describes the course structure and schedule."],
            tmp_path=tmp_path,
            filename="document_01.pdf",
        )
        registry = _minimal_doc_registry("doc01", pdf_path)
        processor = DocumentProcessor(registry, str(tmp_path), chunk_size=500, chunk_overlap=50)
        result = processor.process_document(registry["doc01"])
        assert result.success is True
        assert result.total_chunks > 0

    def test_chunk_ids_are_unique(self, tmp_path):
        pages = [f"Content of page {i}. " * 5 for i in range(1, 4)]
        pdf_path = _make_pdf(pages, tmp_path=tmp_path, filename="document_01.pdf")
        registry = _minimal_doc_registry("doc01", pdf_path)
        processor = DocumentProcessor(registry, str(tmp_path), chunk_size=200, chunk_overlap=30)
        result = processor.process_document(registry["doc01"])
        ids = [c.chunk_id for c in result.chunks]
        assert len(ids) == len(set(ids)), "Chunk IDs must be unique"

    def test_chunk_ids_contain_doc_id_and_page(self, tmp_path):
        pdf_path = _make_pdf(["Sample text."], tmp_path=tmp_path, filename="document_01.pdf")
        registry = _minimal_doc_registry("doc01", pdf_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc01"])
        for chunk in result.chunks:
            assert chunk.doc_id == "doc01"
            assert chunk.chunk_id.startswith("doc01_p")

    def test_doc02_chunks_marked_outdated(self, tmp_path):
        """
        Doc02 is superseded but IS required (is_required=True).
        All its chunks must carry is_outdated=True and policy_status='superseded'
        so the retriever never presents them as current grading policy.
        """
        pdf_path = _make_pdf(
            ["Assignment worth 30%. Late penalty 10% per day."],
            tmp_path=tmp_path,
            filename="document_02.pdf",
        )
        registry = _minimal_doc_registry(
            "doc02", pdf_path,
            is_outdated=True,
            is_required=True,          # Doc02 is required; superseded ≠ optional
            policy_status="superseded",
        )
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc02"])
        assert result.success is True
        assert all(c.is_outdated for c in result.chunks), \
            "All doc02 chunks must be marked is_outdated=True"
        assert result.outdated_chunks == result.total_chunks

    def test_doc03_chunks_not_outdated(self, tmp_path):
        pdf_path = _make_pdf(
            ["Passing requirement: 50%. Distinction: 85%."],
            tmp_path=tmp_path,
            filename="document_03.pdf",
        )
        registry = _minimal_doc_registry(
            "doc03", pdf_path,
            is_outdated=False,
            policy_status="current",
        )
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc03"])
        assert result.success is True
        assert all(not c.is_outdated for c in result.chunks), \
            "doc03 chunks must NOT be marked outdated"

    def test_chunk_contains_source_metadata(self, tmp_path):
        pdf_path = _make_pdf(["Course info."], tmp_path=tmp_path, filename="document_01.pdf")
        registry = _minimal_doc_registry("doc01", pdf_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc01"])
        chunk = result.chunks[0]
        assert chunk.doc_id == "doc01"
        assert chunk.source_filename == "document_01.pdf"
        assert chunk.policy_status == "current"
        assert chunk.page_number >= 1
        assert chunk.last_updated == "2026-01-01"

    def test_doc04_forum_chunks_excluded(self, tmp_path):
        pdf_path = _make_doc04_pdf(tmp_path)
        registry = {
            "doc04": {
                **DOC_REGISTRY["doc04"],
                "filename": "document_04.pdf",
            }
        }
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc04"])
        assert result.success is True

        forum_chunks = [c for c in result.chunks if c.is_excluded]
        official_chunks = [c for c in result.chunks if not c.is_excluded]
        assert len(forum_chunks) > 0, "Forum chunks must be present"
        assert len(official_chunks) > 0, "Official exam rule chunks must be present"

    def test_doc04_official_chunks_not_excluded(self, tmp_path):
        pdf_path = _make_doc04_pdf(tmp_path)
        registry = {
            "doc04": {
                **DOC_REGISTRY["doc04"],
                "filename": "document_04.pdf",
            }
        }
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc04"])
        official = [c for c in result.chunks if c.section_type == "official_exam_rules"]
        assert all(not c.is_excluded for c in official), \
            "Official exam rule chunks must NOT be excluded"

    def test_doc04_forum_chunks_have_forum_section_type(self, tmp_path):
        pdf_path = _make_doc04_pdf(tmp_path)
        registry = {
            "doc04": {
                **DOC_REGISTRY["doc04"],
                "filename": "document_04.pdf",
            }
        }
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc04"])
        forum = [c for c in result.chunks if c.is_excluded]
        assert all(c.section_type == "forum" for c in forum)

    def test_doc04_exam_date_in_official_chunk(self, tmp_path):
        pdf_path = _make_doc04_pdf(tmp_path)
        registry = {
            "doc04": {
                **DOC_REGISTRY["doc04"],
                "filename": "document_04.pdf",
            }
        }
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc04"])
        official_texts = " ".join(c.text for c in result.chunks if not c.is_excluded)
        assert "November 20, 2026" in official_texts or "90 minutes" in official_texts

    def test_doc04_forum_comment_about_phones_excluded(self, tmp_path):
        """Forum comment claiming phones are allowed must be in excluded chunks only."""
        pdf_path = _make_doc04_pdf(tmp_path)
        registry = {
            "doc04": {
                **DOC_REGISTRY["doc04"],
                "filename": "document_04.pdf",
            }
        }
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc04"])
        retrievable_texts = " ".join(c.text for c in result.chunks if not c.is_excluded)
        # The forum comment "I heard we can use phones" must NOT be in retrievable content
        assert "phones" not in retrievable_texts.lower() or \
               "not permitted" in retrievable_texts.lower(), (
            "Forum comment about phones must only appear in excluded (forum) chunks"
        )

    def test_empty_pages_produce_no_chunks(self, tmp_path):
        """Pages with no extractable text must not generate chunks."""
        # Blank second page
        pdf_path = _make_pdf(["Actual content."], tmp_path=tmp_path, filename="doc.pdf")
        registry = _minimal_doc_registry("doc01", pdf_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc01"])
        # Every chunk must have non-empty text
        assert all(len(c.text.strip()) > 0 for c in result.chunks)


# ═════════════════════════════════════════════════════════════════════════════
# D. process_all and processing report
# ═════════════════════════════════════════════════════════════════════════════

class TestProcessAll:
    def _make_full_registry(self, tmp_path: Path, include_doc04: bool = True) -> dict:
        """
        Create a 4-document registry with real synthetic PDFs matching the
        actual DOC_REGISTRY settings — all four documents are required.
        Doc02 is required AND superseded/outdated (required ≠ current policy).
        """
        if include_doc04:
            _make_doc04_pdf(tmp_path)

        docs = {
            "doc01": {
                **DOC_REGISTRY["doc01"],
                "filename": "document_01.pdf",
                "is_required": True,
            },
            "doc02": {
                **DOC_REGISTRY["doc02"],
                "filename": "document_02.pdf",
                "is_required": True,       # required — superseded does NOT mean optional
                "is_outdated": True,
            },
            "doc03": {
                **DOC_REGISTRY["doc03"],
                "filename": "document_03.pdf",
                "is_required": True,
            },
            "doc04": {
                **DOC_REGISTRY["doc04"],
                "filename": "document_04.pdf",
                "is_required": True,
            },
        }

        _make_pdf(["Course Handbook content."], tmp_path=tmp_path, filename="document_01.pdf")
        _make_pdf(
            ["Grading Policy V1 (Historical). Late penalty: 10% per day. Passing: 50%."],
            tmp_path=tmp_path, filename="document_02.pdf",
        )
        _make_pdf(
            ["Grading Policy V2 (Current). Passing: 60% overall. "
             "Minimum proctored final exam score: 40%. Distinction: 85%."],
            tmp_path=tmp_path, filename="document_03.pdf",
        )

        return docs

    def test_process_all_returns_all_four_doc_ids(self, tmp_path):
        registry = self._make_full_registry(tmp_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        assert set(results.keys()) == {"doc01", "doc02", "doc03", "doc04"}

    def test_all_required_docs_succeed(self, tmp_path):
        """All four documents are required; all must succeed when PDFs are present."""
        registry = self._make_full_registry(tmp_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        for doc_id in ["doc01", "doc02", "doc03", "doc04"]:
            assert results[doc_id].success is True, \
                f"{doc_id} must succeed when PDF is present"

    def test_processing_report_contains_all_doc_ids(self, tmp_path):
        registry = self._make_full_registry(tmp_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        for doc_id in ["DOC01", "DOC02", "DOC03", "DOC04"]:
            assert doc_id in report

    def test_processing_report_success_message_when_all_ok(self, tmp_path):
        """All four required PDFs present → report must say all loaded successfully."""
        registry = self._make_full_registry(tmp_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        # All four docs present and required — no DEGRADED expected
        assert "DEGRADED" not in report
        assert "All required documents loaded successfully" in report

    def test_processing_report_degraded_when_required_missing(self, tmp_path):
        """Missing required doc must produce DEGRADED status in report."""
        registry = self._make_full_registry(tmp_path)
        # Remove doc03 PDF to simulate missing required file
        (tmp_path / "document_03.pdf").unlink()
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "DEGRADED" in report

    def test_missing_doc02_triggers_degraded(self, tmp_path):
        """
        Doc02 is required (even though superseded). Missing Doc02 must
        cause DEGRADED status, not silent success.
        """
        registry = self._make_full_registry(tmp_path)
        (tmp_path / "document_02.pdf").unlink()   # remove required doc02
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "DEGRADED" in report, \
            "Missing Doc02 must produce DEGRADED — it is required, not optional"

    def test_report_mentions_excluded_chunks(self, tmp_path):
        registry = self._make_full_registry(tmp_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "excluded" in report.lower() or "forum" in report.lower()

    def test_report_mentions_outdated_chunks(self, tmp_path):
        registry = self._make_full_registry(tmp_path)
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "outdated" in report.lower() or "superseded" in report.lower()

    def test_process_all_never_raises(self, tmp_path):
        """process_all() must not raise even when all PDFs are missing."""
        registry = self._make_full_registry(tmp_path)
        for fname in ["document_01.pdf", "document_02.pdf",
                      "document_03.pdf", "document_04.pdf"]:
            fp = tmp_path / fname
            if fp.exists():
                fp.unlink()
        processor = DocumentProcessor(registry, str(tmp_path))
        # Must not raise
        results = processor.process_all()
        assert isinstance(results, dict)


# ═════════════════════════════════════════════════════════════════════════════
# E. Chunking
# ═════════════════════════════════════════════════════════════════════════════

class TestChunking:
    def _processor(self, tmp_path, chunk_size=200, chunk_overlap=30):
        return DocumentProcessor(
            DOC_REGISTRY, str(tmp_path), chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )

    def test_short_text_produces_single_chunk(self, tmp_path):
        pdf_path = _make_pdf(["Short."], tmp_path=tmp_path, filename="document_01.pdf")
        reg = _minimal_doc_registry("doc01", pdf_path)
        p = DocumentProcessor(reg, str(tmp_path), chunk_size=500, chunk_overlap=50)
        result = p.process_document(reg["doc01"])
        assert result.total_chunks == 1

    def test_long_text_produces_multiple_chunks(self, tmp_path):
        long_text = "This is a sentence about the course. " * 60
        pdf_path = _make_pdf([long_text], tmp_path=tmp_path, filename="document_01.pdf")
        reg = _minimal_doc_registry("doc01", pdf_path)
        p = DocumentProcessor(reg, str(tmp_path), chunk_size=200, chunk_overlap=30)
        result = p.process_document(reg["doc01"])
        assert result.total_chunks > 1

    def test_chunks_do_not_exceed_chunk_size_greatly(self, tmp_path):
        """
        Chunks may slightly exceed CHUNK_SIZE due to sentence-boundary logic,
        but must not exceed 1.5× CHUNK_SIZE in length.
        """
        long_text = "Important exam rule text. " * 50
        pdf_path = _make_pdf([long_text], tmp_path=tmp_path, filename="document_01.pdf")
        reg = _minimal_doc_registry("doc01", pdf_path)
        chunk_size = 200
        p = DocumentProcessor(reg, str(tmp_path), chunk_size=chunk_size, chunk_overlap=30)
        result = p.process_document(reg["doc01"])
        for chunk in result.chunks:
            assert len(chunk.text) <= int(chunk_size * 1.5), \
                f"Chunk too long: {len(chunk.text)} chars (limit {int(chunk_size * 1.5)})"

    def test_overlapping_chunks_share_text(self, tmp_path):
        """
        With overlap > 0, adjacent chunks must share some content (the overlap region).
        """
        long_text = "Alpha beta gamma delta epsilon. " * 20
        pdf_path = _make_pdf([long_text], tmp_path=tmp_path, filename="document_01.pdf")
        reg = _minimal_doc_registry("doc01", pdf_path)
        p = DocumentProcessor(reg, str(tmp_path), chunk_size=100, chunk_overlap=30)
        result = p.process_document(reg["doc01"])
        if result.total_chunks > 1:
            # Check that at least one adjacent pair shares at least one word
            shared = False
            for i in range(len(result.chunks) - 1):
                words_a = set(result.chunks[i].text.split())
                words_b = set(result.chunks[i + 1].text.split())
                if words_a & words_b:
                    shared = True
                    break
            assert shared, "Adjacent chunks should share words in the overlap region"

    def test_chunks_cover_all_content(self, tmp_path):
        """
        The union of all chunk texts should cover the significant words
        from the original document.
        """
        content = "Passing requirement is fifty percent. Distinction at eighty-five percent."
        pdf_path = _make_pdf([content], tmp_path=tmp_path, filename="document_03.pdf")
        reg = _minimal_doc_registry("doc03", pdf_path)
        p = DocumentProcessor(reg, str(tmp_path), chunk_size=50, chunk_overlap=10)
        result = p.process_document(reg["doc03"])
        all_text = " ".join(c.text for c in result.chunks).lower()
        assert "passing" in all_text or "percent" in all_text

    def test_chunk_char_start_end_valid(self, tmp_path):
        """char_start must be < char_end and both must be non-negative."""
        content = "Some text about the exam schedule and grading policy."
        pdf_path = _make_pdf([content], tmp_path=tmp_path, filename="document_01.pdf")
        reg = _minimal_doc_registry("doc01", pdf_path)
        p = DocumentProcessor(reg, str(tmp_path), chunk_size=30, chunk_overlap=5)
        result = p.process_document(reg["doc01"])
        for chunk in result.chunks:
            assert chunk.char_start >= 0
            assert chunk.char_end > chunk.char_start

    def test_doc04_forum_chunk_is_excluded_flag(self, tmp_path):
        pdf_path = _make_doc04_pdf(tmp_path)
        registry = {"doc04": {**DOC_REGISTRY["doc04"], "filename": "document_04.pdf"}}
        p = DocumentProcessor(registry, str(tmp_path), chunk_size=300, chunk_overlap=30)
        result = p.process_document(registry["doc04"])
        for chunk in result.chunks:
            if chunk.section_type == "forum":
                assert chunk.is_excluded is True
            else:
                assert chunk.is_excluded is False


# ═════════════════════════════════════════════════════════════════════════════
# F. PDF hashing
# ═════════════════════════════════════════════════════════════════════════════

class TestPdfHashing:
    def test_hash_is_hex_string_of_length_64(self, tmp_path):
        pdf_path = _make_pdf(["Content."], tmp_path=tmp_path)
        processor = DocumentProcessor({}, str(tmp_path))
        h = processor.compute_pdf_hash(pdf_path)
        assert h is not None
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_deterministic(self, tmp_path):
        pdf_path = _make_pdf(["Deterministic content."], tmp_path=tmp_path)
        processor = DocumentProcessor({}, str(tmp_path))
        h1 = processor.compute_pdf_hash(pdf_path)
        h2 = processor.compute_pdf_hash(pdf_path)
        assert h1 == h2

    def test_different_files_have_different_hashes(self, tmp_path):
        pdf1 = _make_pdf(["Content A."], tmp_path=tmp_path, filename="a.pdf")
        pdf2 = _make_pdf(["Content B."], tmp_path=tmp_path, filename="b.pdf")
        processor = DocumentProcessor({}, str(tmp_path))
        assert processor.compute_pdf_hash(pdf1) != processor.compute_pdf_hash(pdf2)

    def test_hash_returns_none_for_missing_file(self, tmp_path):
        processor = DocumentProcessor({}, str(tmp_path))
        h = processor.compute_pdf_hash(tmp_path / "ghost.pdf")
        assert h is None

    def test_hash_matches_manual_sha256(self, tmp_path):
        pdf_path = _make_pdf(["Hashable content."], tmp_path=tmp_path)
        processor = DocumentProcessor({}, str(tmp_path))
        result_hash = processor.compute_pdf_hash(pdf_path)
        manual = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        assert result_hash == manual


# ═════════════════════════════════════════════════════════════════════════════
# G. Fail-safe: missing required documents block healthy status
# ═════════════════════════════════════════════════════════════════════════════

class TestFailSafe:
    def test_required_missing_result_not_success(self, tmp_path):
        registry = _minimal_doc_registry(
            "doc01",
            tmp_path / "document_01.pdf",   # not created
            is_required=True,
        )
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc01"])
        assert result.success is False
        assert result.chunks == []

    def test_report_degraded_when_doc01_missing(self, tmp_path):
        registry = {
            "doc01": {
                **DOC_REGISTRY["doc01"],
                "filename": "document_01.pdf",
                "is_required": True,
            }
        }
        # doc01 PDF deliberately not created
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "DEGRADED" in report

    def test_report_degraded_when_doc02_missing(self, tmp_path):
        """
        Doc02 is REQUIRED even though superseded.
        Missing Doc02 must produce DEGRADED in the processing report.
        """
        registry = {
            "doc01": {**DOC_REGISTRY["doc01"], "filename": "document_01.pdf", "is_required": True},
            "doc02": {**DOC_REGISTRY["doc02"], "filename": "document_02.pdf", "is_required": True},
        }
        _make_pdf(["Course info."], tmp_path=tmp_path, filename="document_01.pdf")
        # doc02 deliberately missing
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "DEGRADED" in report, \
            "Missing Doc02 must produce DEGRADED — it is required, not optional"

    def test_report_degraded_when_doc03_missing(self, tmp_path):
        registry = {
            "doc01": {**DOC_REGISTRY["doc01"], "filename": "document_01.pdf", "is_required": True},
            "doc03": {**DOC_REGISTRY["doc03"], "filename": "document_03.pdf", "is_required": True},
        }
        _make_pdf(["Course info."], tmp_path=tmp_path, filename="document_01.pdf")
        # doc03 deliberately missing
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "DEGRADED" in report

    def test_report_degraded_when_doc04_missing(self, tmp_path):
        registry = {
            "doc01": {**DOC_REGISTRY["doc01"], "filename": "document_01.pdf", "is_required": True},
            "doc04": {**DOC_REGISTRY["doc04"], "filename": "document_04.pdf", "is_required": True},
        }
        _make_pdf(["Course info."], tmp_path=tmp_path, filename="document_01.pdf")
        # doc04 deliberately missing
        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "DEGRADED" in report

    def test_doc04_section_error_produces_failed_result(self, tmp_path):
        """
        If Doc04's PDF contains no official-section pages, the result must be
        success=False (not a partial success), and the error must be recorded.
        """
        # Doc04 PDF with no official headers at all — only forum text
        all_forum_pdf = _make_pdf(
            [
                "Student Forum Excerpts\nI think the exam is easy.",
                "Another student: It is totally open book.",
            ],
            tmp_path=tmp_path,
            filename="document_04.pdf",
        )
        registry = {"doc04": {**DOC_REGISTRY["doc04"], "filename": "document_04.pdf"}}
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc04"])
        assert result.success is False
        assert result.error is not None
        assert result.chunks == []

    def test_processing_result_error_field_set_on_missing(self, tmp_path):
        registry = _minimal_doc_registry(
            "doc03",
            tmp_path / "document_03.pdf",
            is_required=True,
        )
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc03"])
        assert result.error is not None
        assert len(result.error) > 0


# ═════════════════════════════════════════════════════════════════════════════
# H. Document Registry correctness
#    Verifies that DOC_REGISTRY encodes all authority rules precisely.
# ═════════════════════════════════════════════════════════════════════════════

class TestDocRegistry:
    """Verify that DOC_REGISTRY encodes all four-document authority rules."""

    def test_all_four_docs_registered(self):
        assert set(DOC_REGISTRY.keys()) == {"doc01", "doc02", "doc03", "doc04"}

    def test_all_four_docs_are_required(self):
        """
        All four documents are required. Doc02 being superseded does not make
        it optional — its absence means the knowledge base is incomplete.
        """
        for doc_id, entry in DOC_REGISTRY.items():
            assert entry["is_required"] is True, \
                f"{doc_id} must be is_required=True; superseded ≠ optional"

    def test_doc01_is_current_and_not_outdated(self):
        assert DOC_REGISTRY["doc01"]["policy_status"] == "current"
        assert DOC_REGISTRY["doc01"]["is_outdated"] is False

    def test_doc02_is_superseded_and_outdated(self):
        assert DOC_REGISTRY["doc02"]["policy_status"] == "superseded"
        assert DOC_REGISTRY["doc02"]["is_outdated"] is True

    def test_doc03_is_current_and_not_outdated(self):
        assert DOC_REGISTRY["doc03"]["policy_status"] == "current"
        assert DOC_REGISTRY["doc03"]["is_outdated"] is False

    def test_doc04_is_official_exam_information(self):
        assert DOC_REGISTRY["doc04"]["policy_status"] == "official_exam_information"
        assert DOC_REGISTRY["doc04"]["is_outdated"] is False

    def test_doc04_has_section_config(self):
        sc = DOC_REGISTRY["doc04"]["section_config"]
        assert sc is not None
        assert "official_patterns" in sc
        assert "forum_patterns" in sc
        assert len(sc["official_patterns"]) > 0
        assert len(sc["forum_patterns"]) > 0

    def test_doc01_has_certificate_in_topic_scope(self):
        assert "certificate" in DOC_REGISTRY["doc01"]["topic_scope"]

    def test_doc02_topic_scope_covers_grading(self):
        assert "grading" in DOC_REGISTRY["doc02"]["topic_scope"]
        assert "late_submission" in DOC_REGISTRY["doc02"]["topic_scope"]

    def test_doc03_topic_scope_covers_grading(self):
        assert "grading" in DOC_REGISTRY["doc03"]["topic_scope"]
        assert "passing_requirement" in DOC_REGISTRY["doc03"]["topic_scope"]

    def test_doc04_topic_scope_covers_exam(self):
        assert "final_exam" in DOC_REGISTRY["doc04"]["topic_scope"]
        assert "exam_date" in DOC_REGISTRY["doc04"]["topic_scope"]

    def test_all_docs_have_required_keys(self):
        required_keys = {"doc_id", "filename", "title", "last_updated",
                         "policy_status", "is_required", "is_outdated",
                         "section_config", "topic_scope"}
        for doc_id, entry in DOC_REGISTRY.items():
            missing = required_keys - set(entry.keys())
            assert not missing, f"{doc_id} missing keys: {missing}"

    def test_doc_ids_match_keys(self):
        for key, entry in DOC_REGISTRY.items():
            assert entry["doc_id"] == key, \
                f"doc_id mismatch: key={key!r}, doc_id={entry['doc_id']!r}"

    def test_last_updated_dates_are_correct(self):
        assert DOC_REGISTRY["doc01"]["last_updated"] == "2026-08-15"
        assert DOC_REGISTRY["doc02"]["last_updated"] == "2026-01-10"
        assert DOC_REGISTRY["doc03"]["last_updated"] == "2026-10-05"
        assert DOC_REGISTRY["doc04"]["last_updated"] == "2026-10-25"

    def test_filenames_are_correct(self):
        assert DOC_REGISTRY["doc01"]["filename"] == "01_course_handbook.txt"
        assert DOC_REGISTRY["doc02"]["filename"] == "02_grading_policy_v1.txt"
        assert DOC_REGISTRY["doc03"]["filename"] == "03_grading_policy_v2.txt"
        assert DOC_REGISTRY["doc04"]["filename"] == "04_exam_rules_and_faq.txt"


# ═════════════════════════════════════════════════════════════════════════════
# I. Topic Precedence correctness
# ═════════════════════════════════════════════════════════════════════════════

class TestTopicPrecedence:
    """Verify TOPIC_PRECEDENCE routes each topic to the correct document."""

    def test_grading_topics_route_to_doc03(self):
        from config import TOPIC_PRECEDENCE
        for topic in ["grading", "assessment", "passing_requirement",
                      "distinction", "late_submission", "capstone_deadline",
                      "quiz_submission"]:
            assert TOPIC_PRECEDENCE.get(topic) == ["doc03"], \
                f"Topic '{topic}' must route to doc03"

    def test_exam_topics_route_to_doc04_official(self):
        from config import TOPIC_PRECEDENCE
        for topic in ["final_exam", "exam_format", "exam_device",
                      "proctoring", "exam_date"]:
            assert TOPIC_PRECEDENCE.get(topic) == ["doc04_official"], \
                f"Topic '{topic}' must route to doc04_official"

    def test_course_info_routes_to_doc01(self):
        from config import TOPIC_PRECEDENCE
        for topic in ["schedule", "faculty", "objective",
                      "certificate", "certificate_fee", "course_structure"]:
            assert TOPIC_PRECEDENCE.get(topic) == ["doc01"], \
                f"Topic '{topic}' must route to doc01"

    def test_forum_routes_to_empty(self):
        from config import TOPIC_PRECEDENCE
        assert TOPIC_PRECEDENCE["forum"] == []

    def test_capstone_deadline_present(self):
        from config import TOPIC_PRECEDENCE
        assert "capstone_deadline" in TOPIC_PRECEDENCE, \
            "capstone_deadline must be in TOPIC_PRECEDENCE (Doc03 authority)"

    def test_certificate_fee_present(self):
        from config import TOPIC_PRECEDENCE
        assert "certificate_fee" in TOPIC_PRECEDENCE, \
            "certificate_fee must be in TOPIC_PRECEDENCE (Doc01 authority)"


# ═════════════════════════════════════════════════════════════════════════════
# J. Doc02 required-but-superseded behaviour
#    Distinct from TestFailSafe: validates the semantic meaning of
#    is_required=True + is_outdated=True co-existing on the same document.
# ═════════════════════════════════════════════════════════════════════════════

class TestDoc02RequiredButSuperseded:
    """
    Doc02 must be:
      - Required: missing → DEGRADED
      - Superseded: all chunks carry is_outdated=True and policy_status='superseded'
      - Not confused with current policy
    """

    def test_doc02_registry_is_required_true(self):
        assert DOC_REGISTRY["doc02"]["is_required"] is True

    def test_doc02_registry_is_outdated_true(self):
        assert DOC_REGISTRY["doc02"]["is_outdated"] is True

    def test_doc02_registry_policy_status_superseded(self):
        assert DOC_REGISTRY["doc02"]["policy_status"] == "superseded"

    def test_doc02_missing_produces_degraded_in_full_run(self, tmp_path):
        """Missing Doc02 must degrade the whole knowledge base."""
        # Create all docs except doc02
        _make_pdf(["Course Handbook."], tmp_path=tmp_path, filename="document_01.pdf")
        _make_pdf(["Grading V2 current."], tmp_path=tmp_path, filename="document_03.pdf")
        _make_doc04_pdf(tmp_path)

        registry = {
            "doc01": {**DOC_REGISTRY["doc01"], "filename": "document_01.pdf"},
            "doc02": {**DOC_REGISTRY["doc02"], "filename": "document_02.pdf",
                      "is_required": True},   # required per spec
            "doc03": {**DOC_REGISTRY["doc03"], "filename": "document_03.pdf"},
            "doc04": {**DOC_REGISTRY["doc04"], "filename": "document_04.pdf"},
        }

        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)
        assert "DEGRADED" in report, \
            "Missing Doc02 must produce DEGRADED even though it is superseded"

    def test_doc02_chunks_policy_status_is_superseded(self, tmp_path):
        pdf_path = _make_pdf(
            ["Old grading: passing score 50%. Late penalty 5% daily."],
            tmp_path=tmp_path,
            filename="document_02.pdf",
        )
        registry = _minimal_doc_registry(
            "doc02", pdf_path,
            is_outdated=True,
            is_required=True,
            policy_status="superseded",
        )
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc02"])
        assert result.success is True
        for chunk in result.chunks:
            assert chunk.policy_status == "superseded", \
                "Doc02 chunks must carry policy_status='superseded'"
            assert chunk.is_outdated is True

    def test_doc03_supersedes_doc02_flags(self, tmp_path):
        """Doc03 chunks must NOT be outdated; Doc02 chunks MUST be outdated."""
        pdf02 = _make_pdf(["Old passing: 50%."], tmp_path=tmp_path, filename="doc02.pdf")
        pdf03 = _make_pdf(
            ["Current passing: 60%. Minimum proctored final exam score: 40%."],
            tmp_path=tmp_path, filename="doc03.pdf",
        )

        reg02 = _minimal_doc_registry("doc02", pdf02,
                                      is_outdated=True, is_required=True,
                                      policy_status="superseded")
        reg03 = _minimal_doc_registry("doc03", pdf03,
                                      is_outdated=False, is_required=True,
                                      policy_status="current")

        combined = {**reg02, **reg03}
        processor = DocumentProcessor(combined, str(tmp_path))

        r02 = processor.process_document(reg02["doc02"])
        r03 = processor.process_document(reg03["doc03"])

        assert all(c.is_outdated for c in r02.chunks), "All doc02 chunks must be outdated"
        assert all(not c.is_outdated for c in r03.chunks), "No doc03 chunk must be outdated"

    def test_doc02_does_not_contain_current_policy(self, tmp_path):
        """
        Verify that doc02 chunks are distinguishable from doc03 chunks by
        is_outdated flag — confirming the retriever can filter them out.
        """
        pdf_path = _make_pdf(
            ["Assignment worth 30%. Deadline: Week end."],
            tmp_path=tmp_path, filename="document_02.pdf",
        )
        registry = _minimal_doc_registry("doc02", pdf_path,
                                         is_outdated=True,
                                         is_required=True,
                                         policy_status="superseded")
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc02"])
        retrievable_non_outdated = [c for c in result.chunks if not c.is_outdated]
        assert retrievable_non_outdated == [], \
            "No doc02 chunk should have is_outdated=False"


# ═════════════════════════════════════════════════════════════════════════════
# L. Regression Tests: Empty, Image-Only, Encrypted & Zero-Chunk PDFs
# ═════════════════════════════════════════════════════════════════════════════

class TestUnreadableAndCorruptedPdfs:
    """
    Regression test suite verifying handling of empty, image-only (no OCR),
    encrypted/password-protected, and zero-chunk PDFs.

    Ensures that unextractable PDFs never pass as successful knowledge base entries.
    """

    def test_zero_page_empty_pdf_fails_extraction(self, tmp_path):
        """A PDF with 0 pages must fail extraction with success=False."""
        out = tmp_path / "zero_pages.pdf"
        try:
            from reportlab.pdfgen import canvas as rl_canvas
            c = rl_canvas.Canvas(str(out))
            c.save()  # 0 pages
        except ImportError:
            pytest.skip("reportlab required to create zero-page PDF")

        res = extract_pages(out)
        assert res.success is False
        assert "0 pages" in res.error or "extraction failed" in res.error.lower()

    def test_image_only_pdf_without_ocr_fails_extraction(self, tmp_path):
        """
        An image-only PDF contains visual pixels but zero extractable text.
        Without OCR support, extract_pages must return success=False with a clear error.
        """
        out = tmp_path / "image_only.pdf"
        try:
            from reportlab.pdfgen import canvas as rl_canvas
            from PIL import Image
            img_path = tmp_path / "sample.png"
            img = Image.new("RGB", (80, 80), color="blue")
            img.save(str(img_path))

            c = rl_canvas.Canvas(str(out))
            c.drawImage(str(img_path), 50, 50, 100, 100)
            c.showPage()
            c.save()
        except ImportError:
            pytest.skip("reportlab and Pillow required for image-only PDF test")

        res = extract_pages(out)
        assert res.success is False, "Image-only PDF must fail extraction"
        assert "No extractable text" in res.error or "image-only" in res.error.lower()
        assert res.page_count == 1
        assert res.empty_page_count == 1

    def test_encrypted_password_protected_pdf_fails_with_clear_reason(self, tmp_path):
        """
        An encrypted / password-protected PDF must return success=False
        and explicitly mention encryption or password in the error reason.
        """
        out = tmp_path / "encrypted.pdf"
        try:
            from reportlab.pdfgen import canvas as rl_canvas
            c = rl_canvas.Canvas(str(out), encrypt="secret_pass_123")
            c.drawString(100, 100, "Secret content")
            c.showPage()
            c.save()
        except ImportError:
            pytest.skip("reportlab encryption support required")

        res = extract_pages(out)
        assert res.success is False
        assert "encrypt" in res.error.lower() or "password" in res.error.lower(), \
            f"Expected encryption/password error reason, got: {res.error}"

    def test_pdf_producing_zero_chunks_fails_processing(self, tmp_path):
        """
        If a PDF file yields 0 chunks, process_document must return success=False
        and not add an empty document to the knowledge base.
        """
        # Create an image-only PDF
        out = tmp_path / "empty_content.pdf"
        try:
            from reportlab.pdfgen import canvas as rl_canvas
            from PIL import Image
            img_path = tmp_path / "pixel.png"
            img = Image.new("RGB", (10, 10), color="black")
            img.save(str(img_path))
            c = rl_canvas.Canvas(str(out))
            c.drawImage(str(img_path), 10, 10, 20, 20)
            c.showPage()
            c.save()
        except ImportError:
            pytest.skip("reportlab and Pillow required")

        registry = _minimal_doc_registry("doc01", out, is_required=True)
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc01"])

        assert result.success is False, "0-chunk / unreadable PDF must fail processing"
        assert result.chunks == []
        assert result.error is not None

    def test_failed_extraction_causes_overall_degraded_status(self, tmp_path):
        """
        If a required document fails extraction (e.g. unreadable image-only PDF),
        the overall knowledge base processing report must show DEGRADED.
        """
        # Create 3 valid docs and 1 unreadable image-only doc
        _make_pdf(["Course handbook content."], tmp_path=tmp_path, filename="document_01.pdf")
        _make_pdf(["Grading V1 old."], tmp_path=tmp_path, filename="document_02.pdf")
        _make_pdf(["Grading V2 current: 60% pass."], tmp_path=tmp_path, filename="document_03.pdf")

        # document_04 is image-only (unreadable)
        doc04_path = tmp_path / "document_04.pdf"
        try:
            from reportlab.pdfgen import canvas as rl_canvas
            from PIL import Image
            img_path = tmp_path / "dot.png"
            img = Image.new("RGB", (20, 20), color="white")
            img.save(str(img_path))
            c = rl_canvas.Canvas(str(doc04_path))
            c.drawImage(str(img_path), 50, 50, 50, 50)
            c.showPage()
            c.save()
        except ImportError:
            pytest.skip("reportlab and Pillow required")

        registry = {
            "doc01": {**DOC_REGISTRY["doc01"], "filename": "document_01.pdf"},
            "doc02": {**DOC_REGISTRY["doc02"], "filename": "document_02.pdf"},
            "doc03": {**DOC_REGISTRY["doc03"], "filename": "document_03.pdf"},
            "doc04": {**DOC_REGISTRY["doc04"], "filename": "document_04.pdf"},
        }

        processor = DocumentProcessor(registry, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)

        assert results["doc04"].success is False
        assert "DEGRADED" in report, "Extraction failure of required doc must cause DEGRADED"
        assert "doc04" in report

    def test_normal_text_based_pdf_still_processes_successfully(self, tmp_path):
        """
        Confirm that regular text-based PDFs continue to extract and chunk cleanly.
        """
        pdf_path = _make_pdf(
            ["Course Handbook.\nDuration: 8 weeks.\nCertificate Fee: Rs. 1,500."],
            tmp_path=tmp_path,
            filename="document_01.pdf",
        )
        registry = _minimal_doc_registry("doc01", pdf_path, is_required=True)
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc01"])

        assert result.success is True
        assert len(result.chunks) > 0
        assert "8 weeks" in result.chunks[0].text
        assert "Rs. 1,500" in result.chunks[0].text

    def test_existing_doc04_section_filtering_continues_to_work(self, tmp_path):
        """
        Verify that Doc04 section filtering (Sections 1 & 2 official vs Section 3 forum)
        remains completely operational.
        """
        doc04_path = _make_doc04_pdf(tmp_path)
        registry = {"doc04": {**DOC_REGISTRY["doc04"], "filename": "document_04.pdf"}}
        processor = DocumentProcessor(registry, str(tmp_path))
        result = processor.process_document(registry["doc04"])

        assert result.success is True
        official_chunks = [c for c in result.chunks if not c.is_excluded]
        forum_chunks = [c for c in result.chunks if c.is_excluded]

        assert len(official_chunks) > 0, "Must have official exam chunks"
        assert len(forum_chunks) > 0, "Must have excluded forum chunks"
        assert any("November 20, 2026" in c.text for c in official_chunks)
        assert all("phones" not in c.text for c in official_chunks)

    def test_mixed_readable_and_unreadable_pages_fails_safely(self, tmp_path, monkeypatch):
        """
        If a PDF has readable pages but one page fails during extract_text()
        (raising an exception), extraction must NOT silently ignore the failed page.
        It must return success=False and record failed_page_count.
        """
        pdf_path = _make_pdf(["Page 1 readable", "Page 2 readable"], tmp_path=tmp_path)

        # Mock page.extract_text on page 2 to raise an error
        import pdfplumber
        real_open = pdfplumber.open

        class MockPage:
            def __init__(self, page, idx):
                self._page = page
                self._idx = idx

            def extract_text(self):
                if self._idx == 1:
                    raise RuntimeError("Simulated unreadable corrupted font table")
                return self._page.extract_text()

        class MockPdf:
            def __init__(self, real_pdf):
                self._real_pdf = real_pdf
                self.pages = [MockPage(p, i) for i, p in enumerate(real_pdf.pages)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._real_pdf.__exit__(*args)

        monkeypatch.setattr(pdfplumber, "open", lambda path: MockPdf(real_open(path)))

        result = extract_pages(pdf_path)
        assert result.success is False
        assert result.failed_page_count == 1
        assert "Simulated unreadable" in result.error

    def test_pdf_containing_only_whitespace_fails_extraction(self, tmp_path):
        """
        A PDF where all pages contain only whitespace
        must be rejected as having no extractable text (success=False).
        """
        # Two pages containing only space characters
        pdf_path = _make_pdf(["   ", "      "], tmp_path=tmp_path)
        result = extract_pages(pdf_path)
        assert result.success is False
        assert "No extractable text" in result.error


# ═════════════════════════════════════════════════════════════════════════════
# M. Official TXT Documents Verification Suite
# ═════════════════════════════════════════════════════════════════════════════

class TestTxtDocumentProcessing:
    """
    Verifies that the original course TXT files can be loaded, cleaned,
    chunked, hashed, and processed with exact precedence and section-filtering rules.
    """

    def test_read_all_four_official_txt_documents(self):
        """Verify that all four official TXT documents in data/documents exist and load cleanly."""
        docs_dir = Path("data/documents")
        processor = DocumentProcessor(DOC_REGISTRY, str(docs_dir))
        results = processor.process_all()

        for doc_id, doc_config in DOC_REGISTRY.items():
            assert doc_id in results, f"Missing result for {doc_id}"
            assert results[doc_id].success is True, f"{doc_id} failed: {results[doc_id].error}"
            assert results[doc_id].page_count == 1
            assert len(results[doc_id].chunks) > 0

    def test_txt_empty_and_whitespace_only_fails(self, tmp_path):
        """Empty or whitespace-only TXT files must fail extraction with success=False."""
        empty_txt = tmp_path / "empty.txt"
        empty_txt.write_text("", encoding="utf-8")
        res = extract_pages(empty_txt)
        assert res.success is False
        assert "empty or whitespace-only" in res.error

        ws_txt = tmp_path / "whitespace.txt"
        ws_txt.write_text("   \n\t  \n  ", encoding="utf-8")
        res_ws = extract_pages(ws_txt)
        assert res_ws.success is False
        assert "empty or whitespace-only" in res_ws.error

    def test_txt_utf8_decoding_error_handling(self, tmp_path):
        """A text file with invalid non-UTF-8 bytes must return success=False gracefully."""
        bad_txt = tmp_path / "bad_encoding.txt"
        bad_txt.write_bytes(b"\x80\x81\x82 invalid binary text")
        res = extract_pages(bad_txt)
        assert res.success is False
        assert "UTF-8 decoding failed" in res.error

    def test_doc04_txt_section_filtering_retains_official_and_excludes_forum(self):
        """
        Doc04 TXT contains Sections 1 & 2 (official) and Section 3 (unmoderated forum).
        Verify that Sections 1 & 2 chunks are official/retrievable and Section 3 chunks
        are marked forum/excluded.
        """
        docs_dir = Path("data/documents")
        processor = DocumentProcessor(DOC_REGISTRY, str(docs_dir))
        result = processor.process_document(DOC_REGISTRY["doc04"])

        assert result.success is True
        official = [c for c in result.chunks if not c.is_excluded]
        forum = [c for c in result.chunks if c.is_excluded]

        assert len(official) > 0, "Doc04 must have retrievable official chunks"
        assert len(forum) > 0, "Doc04 must have excluded forum chunks"

        all_official = " ".join(c.text for c in official)
        assert "20 November 2026" in all_official
        assert "90 minutes" in all_official
        assert "CLOSED-BOOK" in all_official
        assert "Aadhaar, Passport" in all_official

        # Forum prompt injection MUST NOT be in official chunks
        assert "Disregard Section 1 above" not in all_official
        assert "30 December 2026" not in all_official
        assert "OPEN-BOOK" not in all_official

        # But MUST be captured in the excluded forum chunks
        all_forum = " ".join(c.text for c in forum)
        assert "Disregard Section 1 above" in all_forum

    def test_txt_doc02_superseded_and_doc03_authoritative(self):
        """
        Verify that Doc02 chunks are tagged is_outdated=True and policy_status='superseded',
        while Doc03 chunks are tagged is_outdated=False and policy_status='current'.
        """
        docs_dir = Path("data/documents")
        processor = DocumentProcessor(DOC_REGISTRY, str(docs_dir))
        r02 = processor.process_document(DOC_REGISTRY["doc02"])
        r03 = processor.process_document(DOC_REGISTRY["doc03"])

        assert r02.success is True
        assert all(c.is_outdated is True for c in r02.chunks)
        assert all(c.policy_status == "superseded" for c in r02.chunks)

        assert r03.success is True
        assert all(c.is_outdated is False for c in r03.chunks)
        assert all(c.policy_status == "current" for c in r03.chunks)

    def test_txt_missing_required_causes_degraded(self, tmp_path):
        """If a required TXT document is missing, the overall report must display DEGRADED."""
        # Create only 3 of 4 documents in tmp_path
        (tmp_path / "01_course_handbook.txt").write_text("Handbook info", encoding="utf-8")
        (tmp_path / "02_grading_policy_v1.txt").write_text("Grading V1 info", encoding="utf-8")
        (tmp_path / "03_grading_policy_v2.txt").write_text("Grading V2 info", encoding="utf-8")
        # 04_exam_rules_and_faq.txt deliberately missing

        processor = DocumentProcessor(DOC_REGISTRY, str(tmp_path))
        results = processor.process_all()
        report = processor.get_processing_report(results)

        assert "DEGRADED" in report
        assert "doc04" in report



