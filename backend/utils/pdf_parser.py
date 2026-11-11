"""
backend/utils/pdf_parser.py — Low-level PDF text extraction for DPM-101 Chatbot

Responsibilities:
  - Open PDF files using pdfplumber and extract per-page text.
  - Clean whitespace and formatting artifacts without altering meaning.
  - Detect Doc04 section boundaries to separate official exam rules
    from unmoderated student forum comments.
  - Return structured data only — no business logic, no chunking here.

This module must NOT:
  - Import or call the Gemini API.
  - Perform TF-IDF indexing or retrieval.
  - Access the SQLite database.
  - Fabricate or assume document content not present in the PDF.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PageData:
    """
    Represents one extracted page from a PDF.

    page_number is 1-indexed (matches the visible page number in the PDF viewer).
    is_empty is True when pdfplumber returned None or only whitespace for the page.
    extraction_error contains the error message if pdfplumber threw an exception on this page.
    section_type is populated only during Doc04 processing; None for all other docs.
    """
    page_number: int
    text: str           # cleaned text; empty string when page has no extractable text
    raw_text: str       # original text from pdfplumber before cleaning
    is_empty: bool
    extraction_error: Optional[str] = None
    section_type: Optional[str] = None   # 'official_exam_rules' | 'forum' | None
    section_name: Optional[str] = None   # e.g. 'Section 1', 'Section 2', 'Forum'


@dataclass
class ExtractionResult:
    """
    Result of extracting text from a single PDF file.

    success is False when the file is missing, unreadable, or raises an error.
    When success is False, pages will be empty and error will explain the reason.
    page_count is the number of pages in the PDF (even if some are empty).
    empty_page_count is the number of pages with no extractable text.
    failed_page_count is the number of pages where extraction raised an error.
    """
    doc_id: str
    filepath: str
    success: bool
    pages: list[PageData] = field(default_factory=list)
    error: Optional[str] = None
    page_count: int = 0
    empty_page_count: int = 0
    failed_page_count: int = 0


# ── Custom exception ──────────────────────────────────────────────────────────

class Doc04SectionError(ValueError):
    """
    Raised when Doc04 section-boundary detection cannot classify any page
    as 'official_exam_rules'. This prevents silent ingestion of a document
    whose structure does not match expectations.
    """


# ── Text cleaning ─────────────────────────────────────────────────────────────

def clean_text(raw: str) -> str:
    """
    Normalise whitespace and remove common PDF formatting artifacts
    without altering the semantic content of the text.

    Operations performed (in order):
      1. Replace form-feed characters (page breaks in some PDFs) with a newline.
      2. Replace carriage-return + newline pairs with a plain newline.
      3. Collapse runs of three or more consecutive blank lines into two blank lines
         (preserves paragraph breaks while removing excessive whitespace).
      4. Strip leading and trailing whitespace from each line.
      5. Strip leading and trailing whitespace from the whole string.

    Does NOT:
      - Remove hyphens, dashes, or punctuation (which carry meaning).
      - Merge lines (which would destroy heading detection).
      - Alter numbers, dates, or percentages.
    """
    if not raw:
        return ""

    text = raw
    text = text.replace("\f", "\n")          # form-feed → newline
    text = text.replace("\r\n", "\n")        # Windows line endings
    text = text.replace("\r", "\n")          # bare carriage returns
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing whitespace from each line (but keep the newline)
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    return text.strip()


# ── Core extraction ───────────────────────────────────────────────────────────

def extract_pages(pdf_path: str | Path) -> ExtractionResult:
    """
    Extract text from a document (PDF or UTF-8 text file).

    If the file has a .txt extension, reads the text file directly with UTF-8 encoding.
    If the file has a .pdf extension (or other), extracts per-page text using pdfplumber.

    Returns an ExtractionResult. If the file is missing, empty, or unreadable,
    success=False is returned with a descriptive error message.
    No exception is propagated to the caller.

    Args:
        pdf_path: Absolute or relative path to the PDF or TXT file.

    Returns:
        ExtractionResult with per-page text and metadata.
    """
    path = Path(pdf_path)

    if not path.exists():
        return ExtractionResult(
            doc_id="unknown",
            filepath=str(path),
            success=False,
            error=f"File not found: {path}",
        )

    if not path.is_file():
        return ExtractionResult(
            doc_id="unknown",
            filepath=str(path),
            success=False,
            error=f"Path exists but is not a file: {path}",
        )

    # ── Handle plain text files (.txt) ────────────────────────────────────────
    if path.suffix.lower() == ".txt":
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError as u_err:
            logger.error("UTF-8 decoding failed for %s: %s", path.name, u_err)
            return ExtractionResult(
                doc_id="unknown",
                filepath=str(path),
                success=False,
                error=f"UTF-8 decoding failed for '{path.name}': {u_err}",
            )
        except Exception as read_err:
            logger.error("Failed to read text file %s: %s", path.name, read_err)
            return ExtractionResult(
                doc_id="unknown",
                filepath=str(path),
                success=False,
                error=f"Failed to read file '{path.name}': {read_err}",
            )

        cleaned = clean_text(content)
        if not cleaned:
            logger.error("Text file %s contains no extractable text.", path.name)
            return ExtractionResult(
                doc_id="unknown",
                filepath=str(path),
                success=False,
                error=f"No extractable text found in '{path.name}'. File is empty or whitespace-only.",
                pages=[PageData(page_number=1, text="", raw_text=content, is_empty=True)],
                page_count=1,
                empty_page_count=1,
            )

        logger.info("Loaded text document %s (%d chars).", path.name, len(cleaned))
        return ExtractionResult(
            doc_id="unknown",
            filepath=str(path),
            success=True,
            pages=[PageData(page_number=1, text=cleaned, raw_text=content, is_empty=False)],
            page_count=1,
            empty_page_count=0,
        )

    # ── Handle PDF files (.pdf) ───────────────────────────────────────────────
    # Import pdfplumber lazily so the rest of the module is importable even if
    # pdfplumber is not installed (e.g. during lightweight unit tests that mock this).
    try:
        import pdfplumber
    except ImportError:
        return ExtractionResult(
            doc_id="unknown",
            filepath=str(pdf_path),
            success=False,
            error="pdfplumber is not installed. Run: pip install pdfplumber",
        )

    pages: list[PageData] = []
    empty_count = 0
    failed_page_count = 0
    failed_page_errors: list[str] = []

    try:
        with pdfplumber.open(str(path)) as pdf:
            total_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                page_num = i + 1   # 1-indexed
                page_extraction_error = None
                try:
                    raw = page.extract_text() or ""
                except Exception as page_err:
                    failed_page_count += 1
                    page_extraction_error = str(page_err)
                    failed_page_errors.append(f"page {page_num}: {page_err}")
                    logger.error(
                        "pdfplumber failed to extract page %d of %s: %s",
                        page_num, path.name, page_err,
                    )
                    raw = ""

                cleaned = clean_text(raw)
                is_empty = len(cleaned) == 0
                if is_empty:
                    empty_count += 1

                pages.append(PageData(
                    page_number=page_num,
                    text=cleaned,
                    raw_text=raw,
                    is_empty=is_empty,
                    extraction_error=page_extraction_error,
                ))

        if total_pages == 0:
            logger.error("PDF has 0 pages: %s", path.name)
            return ExtractionResult(
                doc_id="unknown",
                filepath=str(path),
                success=False,
                error=f"PDF file has 0 pages: {path.name}",
                page_count=0,
                empty_page_count=0,
                failed_page_count=0,
            )

        # Unreadable pages fail-safe: if any page threw an extraction error,
        # do not silently ignore it, as crucial official information could be omitted.
        if failed_page_count > 0:
            err_summary = "; ".join(failed_page_errors)
            logger.error(
                "PDF %s had extraction errors on %d of %d page(s): %s",
                path.name, failed_page_count, total_pages, err_summary,
            )
            return ExtractionResult(
                doc_id="unknown",
                filepath=str(path),
                success=False,
                error=(
                    f"Extraction failed on {failed_page_count} page(s) of '{path.name}': "
                    f"{err_summary}. Unreadable pages cannot be silently omitted from official course documents."
                ),
                pages=pages,
                page_count=total_pages,
                empty_page_count=empty_count,
                failed_page_count=failed_page_count,
            )

        if empty_count == total_pages:
            logger.error(
                "PDF %s contains no extractable text across all %d page(s). "
                "The file may be image-only (without OCR), empty, or corrupted.",
                path.name, total_pages,
            )
            return ExtractionResult(
                doc_id="unknown",
                filepath=str(path),
                success=False,
                error=(
                    f"No extractable text found in '{path.name}' across all {total_pages} page(s). "
                    "The PDF may be empty, image-only (requiring OCR), or corrupted."
                ),
                pages=pages,
                page_count=total_pages,
                empty_page_count=empty_count,
                failed_page_count=0,
            )

        logger.info(
            "Extracted %d pages from %s (%d empty).",
            total_pages, path.name, empty_count,
        )
        return ExtractionResult(
            doc_id="unknown",         # caller sets doc_id from DOC_REGISTRY
            filepath=str(path),
            success=True,
            pages=pages,
            page_count=total_pages,
            empty_page_count=empty_count,
            failed_page_count=0,
        )

    except Exception as err:
        err_msg = str(err)
        err_repr = repr(err)
        combined_err = f"{err_msg} {err_repr}".lower()
        if "password" in combined_err or "encrypt" in combined_err:
            error_reason = (
                f"PDF is encrypted or password-protected and cannot be read without credentials: "
                f"{err_repr}"
            )
        else:
            error_reason = f"PDF extraction failed: {err_msg or err_repr}"

        logger.error("Failed to open or read %s: %s", path, error_reason)
        return ExtractionResult(
            doc_id="unknown",
            filepath=str(path),
            success=False,
            error=error_reason,
        )


# ── Doc04 section detection ───────────────────────────────────────────────────

def detect_doc04_sections(
    pages: list[PageData],
    section_config: dict,
) -> list[PageData]:
    """
    Classify content of Doc04 as either 'official_exam_rules' or 'forum'.

    Supports both multi-page PDFs (where each page is a PageData) and
    single-file text documents (where page_count == 1, containing all sections).
    For single-page documents, if multiple sections are present in that page's text,
    it splits the page into sub-pages/sections so that official rules and forum
    comments are properly isolated.

    Raises:
        Doc04SectionError: if no page/section could be classified as 'official_exam_rules'.

    Args:
        pages: List of PageData extracted from Doc04 by extract_pages().
        section_config: The 'section_config' dict from DOC_REGISTRY['doc04'],
                        containing 'official_patterns' and 'forum_patterns' lists.

    Returns:
        A list of PageData with section_type and section_name set on every item.
    """
    if not pages:
        raise Doc04SectionError(
            "Doc04 produced no pages; cannot verify official exam rule sections."
        )

    official_patterns = [
        re.compile(p) for p in section_config.get("official_patterns", [])
    ]
    forum_patterns = [
        re.compile(p) for p in section_config.get("forum_patterns", [])
    ]

    # Special handling for single-page documents (such as .txt files or single-page PDFs)
    # that contain all sections in one text block.
    if len(pages) == 1 and pages[0].text:
        single_page = pages[0]
        lines = single_page.text.split("\n")
        sub_pages: list[PageData] = []
        current_type = "official_exam_rules"
        current_name = "Section 1"
        current_lines: list[str] = []
        sub_page_idx = 1

        for line in lines:
            line_str = line.strip()
            new_type = None
            new_name = None

            for pattern in official_patterns:
                match = pattern.search(line_str)
                if match:
                    new_type = "official_exam_rules"
                    new_name = _extract_section_name(line_str, match)
                    break

            if new_type is None:
                for pattern in forum_patterns:
                    match = pattern.search(line_str)
                    if match:
                        new_type = "forum"
                        new_name = _extract_section_name(line_str, match)
                        break

            # If section boundary detected and we have accumulated lines, flush
            if new_type is not None and current_lines:
                chunk_text = "\n".join(current_lines).strip()
                if chunk_text:
                    sub_pages.append(PageData(
                        page_number=sub_page_idx,
                        text=clean_text(chunk_text),
                        raw_text=chunk_text,
                        is_empty=False,
                        section_type=current_type,
                        section_name=current_name,
                    ))
                    sub_page_idx += 1
                current_lines = [line]
                current_type = new_type
                current_name = new_name or current_name
            else:
                current_lines.append(line)
                if new_type is not None:
                    current_type = new_type
                    current_name = new_name or current_name

        if current_lines:
            chunk_text = "\n".join(current_lines).strip()
            if chunk_text:
                sub_pages.append(PageData(
                    page_number=sub_page_idx,
                    text=clean_text(chunk_text),
                    raw_text=chunk_text,
                    is_empty=False,
                    section_type=current_type,
                    section_name=current_name,
                ))

        if sub_pages:
            official_pages = [p for p in sub_pages if p.section_type == "official_exam_rules"]
            if not official_pages:
                raise Doc04SectionError(
                    "Doc04 section detection found zero pages classified as 'official_exam_rules'."
                )
            return sub_pages

    # Multi-page documents (PDFs)
    current_section_type = "official_exam_rules"
    current_section_name = "Section 1"
    classified: list[PageData] = []

    for page in pages:
        first_lines = page.text[:300]
        new_type = None
        new_name = None

        for pattern in official_patterns:
            match = pattern.search(first_lines)
            if match:
                new_type = "official_exam_rules"
                new_name = _extract_section_name(first_lines, match)
                break

        if new_type is None:
            for pattern in forum_patterns:
                match = pattern.search(first_lines)
                if match:
                    new_type = "forum"
                    new_name = _extract_section_name(first_lines, match)
                    break

        if new_type is not None:
            current_section_type = new_type
            current_section_name = new_name or current_section_name

        classified.append(PageData(
            page_number=page.page_number,
            text=page.text,
            raw_text=page.raw_text,
            is_empty=page.is_empty,
            section_type=current_section_type,
            section_name=current_section_name,
        ))

    official_pages = [p for p in classified if p.section_type == "official_exam_rules"]
    if not official_pages:
        raise Doc04SectionError(
            "Doc04 section detection found zero pages classified as "
            "'official_exam_rules'. The document structure may differ from "
            "expectations. Inspect the actual headings and update "
            "DOC_REGISTRY['doc04']['section_config']['official_patterns'] in config.py."
        )

    return classified


def _extract_section_name(text_snippet: str, match: re.Match) -> str:
    """
    Extract a human-readable section name from the matched text.
    Returns the matched line, truncated at 60 characters.
    """
    start = max(0, match.start() - 1)
    line_end = text_snippet.find("\n", start)
    if line_end == -1:
        line_end = len(text_snippet)
    line = text_snippet[start:line_end].strip()
    return line[:60]
