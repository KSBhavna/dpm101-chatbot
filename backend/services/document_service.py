"""
backend/services/document_service.py — Document processing for DPM-101 Chatbot

Responsibilities:
  - Coordinate loading and processing of all four official DPM-101 PDFs.
  - Apply document authority rules from DOC_REGISTRY.
  - Apply Doc04 section filtering (official vs. forum).
  - Produce overlapping, section-aware text chunks with full source metadata.
  - Report the processing status of every document clearly.

This module must NOT:
  - Import or call the Gemini API.
  - Perform TF-IDF indexing or retrieval.
  - Access the SQLite database (chunked data is returned in memory only;
    DB persistence is added in Module 5 as part of startup ingestion).
  - Fabricate document content.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.utils.pdf_parser import (
    ExtractionResult,
    Doc04SectionError,
    PageData,
    detect_doc04_sections,
    extract_pages,
)

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class ChunkData:
    """
    A single retrievable text chunk produced from an official course document.

    chunk_id is a stable, deterministic identifier:
      {doc_id}_p{page:03d}_c{index:04d}

    is_excluded: True  → chunk must never be returned by the retriever.
                 This flag is set for Doc04 forum sections.
    is_outdated: True  → chunk's grading/submission content is superseded by Doc03.
                 This flag is set for all Doc02 chunks.

    section_type values:
      'official'              — general content (Doc01, Doc02, Doc03)
      'official_exam_rules'   — Doc04 §1 and §2 (authoritative exam content)
      'forum'                 — Doc04 §3+ (student forum; always excluded)
    """
    chunk_id: str
    doc_id: str
    doc_title: str
    source_filename: str
    last_updated: str
    policy_status: str          # 'current' | 'superseded' | 'official_exam_information'
    page_number: int
    section_name: str
    section_type: str           # 'official' | 'official_exam_rules' | 'forum'
    is_excluded: bool           # True → retrieval SQL filters this out
    is_outdated: bool           # True → Doc02 grading content; not authoritative
    text: str
    char_start: int             # character offset within the page text
    char_end: int


@dataclass
class ProcessingResult:
    """
    Result of processing a single document from the DOC_REGISTRY.

    success:          PDF was found, read, and chunked without errors.
    chunks:           List of ChunkData produced. Empty on failure.
    skipped:          True when file is missing but document is not required.
    error:            Human-readable failure reason. None on success.
    official_chunks:  Count of chunks with is_excluded=False.
    excluded_chunks:  Count of chunks with is_excluded=True (forum content).
    outdated_chunks:  Count of chunks with is_outdated=True (Doc02).
    """
    doc_id: str
    success: bool
    chunks: list[ChunkData] = field(default_factory=list)
    skipped: bool = False
    error: Optional[str] = None
    page_count: int = 0
    empty_page_count: int = 0
    official_chunks: int = 0
    excluded_chunks: int = 0
    outdated_chunks: int = 0

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)


# ── DocumentProcessor ─────────────────────────────────────────────────────────

class DocumentProcessor:
    """
    Coordinates PDF extraction, section filtering, chunking, and status reporting
    for all documents registered in DOC_REGISTRY.

    Usage:
        from config import DOC_REGISTRY, config
        processor = DocumentProcessor(DOC_REGISTRY, config.DOCUMENTS_DIR,
                                      config.CHUNK_SIZE, config.CHUNK_OVERLAP)
        results = processor.process_all()
        report = processor.get_processing_report(results)
        print(report)

    The returned results dict is keyed by doc_id.
    """

    def __init__(
        self,
        doc_registry: dict,
        documents_dir: str,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
    ) -> None:
        self.doc_registry = doc_registry
        self.documents_dir = Path(documents_dir)
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    # ── Public API ────────────────────────────────────────────────────────────

    def process_all(self) -> dict[str, ProcessingResult]:
        """
        Process all documents in DOC_REGISTRY.

        Returns:
            Dict mapping doc_id → ProcessingResult.
            All four entries are always present, even when processing fails.
        """
        results: dict[str, ProcessingResult] = {}
        for doc_id, doc_config in self.doc_registry.items():
            logger.info("Processing document: %s (%s)", doc_id, doc_config["title"])
            results[doc_id] = self.process_document(doc_config)
        return results

    def process_document(self, doc_config: dict) -> ProcessingResult:
        """
        Extract, classify, and chunk a single PDF.

        Args:
            doc_config: Entry from DOC_REGISTRY for this document.

        Returns:
            ProcessingResult. Never raises — all errors are captured in the result.
        """
        doc_id = doc_config["doc_id"]
        filepath = self.documents_dir / doc_config["filename"]

        # ── 1. Check file exists ──────────────────────────────────────────────
        if not filepath.exists():
            if doc_config.get("is_required", True):
                msg = (
                    f"REQUIRED document '{doc_config['filename']}' is missing from "
                    f"{self.documents_dir}. Place the official PDF there and restart."
                )
            else:
                msg = (
                    f"Optional document '{doc_config['filename']}' is not present. "
                    "It is retained for audit purposes only; its absence is logged "
                    "but does not block startup."
                )
            logger.warning(msg)
            return ProcessingResult(
                doc_id=doc_id,
                success=False,
                skipped=True,
                error=msg,
            )

        # ── 2. Extract text ───────────────────────────────────────────────────
        result: ExtractionResult = extract_pages(filepath)
        result.doc_id = doc_id

        if not result.success:
            logger.error(
                "Extraction failed for %s: %s", doc_config["filename"], result.error
            )
            return ProcessingResult(
                doc_id=doc_id,
                success=False,
                error=result.error,
                page_count=result.page_count,
                empty_page_count=result.empty_page_count,
            )

        # ── 3. Doc04-specific: apply section-boundary detection ───────────────
        pages = result.pages
        if doc_id == "doc04":
            try:
                pages = detect_doc04_sections(pages, doc_config["section_config"])
            except Doc04SectionError as err:
                logger.error("Doc04 section detection failed: %s", err)
                return ProcessingResult(
                    doc_id=doc_id,
                    success=False,
                    error=str(err),
                    page_count=result.page_count,
                    empty_page_count=result.empty_page_count,
                )

        # ── 4. Chunk pages ────────────────────────────────────────────────────
        chunks = self._chunk_pages(pages, doc_config)

        # Fail-safe: A processed document must produce at least one meaningful chunk
        if not chunks:
            logger.error(
                "%s: Document produced 0 chunks after text extraction and chunking.",
                doc_id,
            )
            return ProcessingResult(
                doc_id=doc_id,
                success=False,
                error=(
                    f"Document '{doc_config['filename']}' produced 0 meaningful chunks. "
                    "Cannot add empty document to knowledge base."
                ),
                page_count=result.page_count,
                empty_page_count=result.empty_page_count,
            )

        official = sum(1 for c in chunks if not c.is_excluded)
        excluded = sum(1 for c in chunks if c.is_excluded)
        outdated = sum(1 for c in chunks if c.is_outdated)

        logger.info(
            "%s: %d chunks produced (%d official, %d excluded, %d outdated).",
            doc_id, len(chunks), official, excluded, outdated,
        )

        return ProcessingResult(
            doc_id=doc_id,
            success=True,
            chunks=chunks,
            page_count=result.page_count,
            empty_page_count=result.empty_page_count,
            official_chunks=official,
            excluded_chunks=excluded,
            outdated_chunks=outdated,
        )

    def get_processing_report(self, results: dict[str, ProcessingResult]) -> str:
        """
        Return a human-readable processing report covering all four documents.

        This report is logged at startup and exposed by the /api/health endpoint.
        """
        lines = ["=" * 60, "DPM-101 Document Processing Report", "=" * 60]

        for doc_id, result in results.items():
            doc_config = self.doc_registry.get(doc_id, {})
            title = doc_config.get("title", doc_id)
            lines.append(f"\n[{doc_id.upper()}] {title}")

            if result.success:
                lines.append(f"  Status  : OK")
                lines.append(f"  Pages   : {result.page_count} "
                             f"({result.empty_page_count} empty)")
                lines.append(f"  Chunks  : {result.total_chunks} total")
                lines.append(f"            {result.official_chunks} retrievable")
                if result.excluded_chunks:
                    lines.append(f"            {result.excluded_chunks} excluded "
                                 f"(forum — never served)")
                if result.outdated_chunks:
                    lines.append(f"            {result.outdated_chunks} outdated "
                                 f"(superseded by doc03)")
            elif result.skipped:
                req = doc_config.get("is_required", True)
                label = "MISSING (REQUIRED)" if req else "MISSING (optional)"
                lines.append(f"  Status  : {label}")
                lines.append(f"  Reason  : {result.error}")
            else:
                lines.append(f"  Status  : FAILED")
                lines.append(f"  Reason  : {result.error}")

        lines.append("\n" + "=" * 60)

        required_failed = [
            doc_id for doc_id, r in results.items()
            if not r.success
            and self.doc_registry.get(doc_id, {}).get("is_required", True)
        ]

        if required_failed:
            lines.append(
                f"⚠  DEGRADED — {len(required_failed)} required document(s) failed: "
                + ", ".join(required_failed)
            )
            lines.append(
                "   The application will not serve chat answers until these "
                "documents are loaded successfully."
            )
        else:
            lines.append("✓  All required documents loaded successfully.")

        return "\n".join(lines)

    def compute_pdf_hash(self, filepath: str | Path) -> Optional[str]:
        """
        Compute the SHA-256 hash of a PDF file's bytes.

        Used for idempotent startup ingestion: if the hash matches the stored
        hash in doc_versions, the document does not need to be re-ingested.

        Returns None if the file cannot be read.
        """
        path = Path(filepath)
        if not path.exists() or not path.is_file():
            return None
        try:
            sha256 = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(65536), b""):
                    sha256.update(block)
            return sha256.hexdigest()
        except OSError as err:
            logger.error("Cannot hash %s: %s", path, err)
            return None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _chunk_pages(
        self, pages: list[PageData], doc_config: dict
    ) -> list[ChunkData]:
        """
        Produce overlapping, section-aware text chunks from a list of pages.

        Each page is chunked independently to prevent chunks from spanning
        page boundaries. Within a page, chunks respect sentence boundaries
        where possible.

        Chunk metadata includes source document identity, page number,
        section type, and exclusion/outdated flags.
        """
        chunks: list[ChunkData] = []
        doc_id = doc_config["doc_id"]
        is_outdated = doc_config.get("is_outdated", False)
        policy_status = doc_config.get("policy_status", "current")
        doc_title = doc_config.get("title", doc_id)
        filename = doc_config.get("filename", "")
        last_updated = doc_config.get("last_updated", "")

        for page in pages:
            if page.is_empty:
                continue

            # Determine section metadata for this page
            section_type = page.section_type or "official"
            section_name = page.section_name or ""
            is_excluded = section_type == "forum"

            page_chunks = self._split_into_chunks(page.text)

            for chunk_index, (text, char_start, char_end) in enumerate(page_chunks):
                chunk_id = f"{doc_id}_p{page.page_number:03d}_c{chunk_index:04d}"
                chunks.append(ChunkData(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    doc_title=doc_title,
                    source_filename=filename,
                    last_updated=last_updated,
                    policy_status=policy_status,
                    page_number=page.page_number,
                    section_name=section_name,
                    section_type=section_type,
                    is_excluded=is_excluded,
                    is_outdated=is_outdated,
                    text=text,
                    char_start=char_start,
                    char_end=char_end,
                ))

        return chunks

    def _split_into_chunks(
        self, text: str
    ) -> list[tuple[str, int, int]]:
        """
        Split a page's text into overlapping chunks at sentence boundaries.

        Returns a list of (chunk_text, char_start, char_end) tuples.
        char_start and char_end are character offsets into the original page text.

        Strategy:
          1. Find candidate split points at sentence-ending punctuation followed by
             whitespace ('. ', '! ', '? ') or at double newlines (paragraph breaks).
          2. Accumulate text until the chunk would exceed CHUNK_SIZE.
          3. At that point, split at the most recent candidate split point.
          4. The next chunk begins CHUNK_OVERLAP characters before the current end
             (or at the last sentence boundary before that point).
          5. If no split point is found within CHUNK_SIZE characters (e.g. very long
             sentence), force-split at the nearest word boundary.

        Empty text produces an empty list.
        Text shorter than CHUNK_SIZE produces a single chunk.
        """
        if not text:
            return []

        size = self.chunk_size
        overlap = self.chunk_overlap

        # Sentence/paragraph boundary positions (end of sentence, inclusive)
        boundary_re = re.compile(r"(?<=[.!?])\s+|(?<=\n)\n")
        boundary_positions = [m.start() for m in boundary_re.finditer(text)]

        chunks: list[tuple[str, int, int]] = []
        start = 0
        text_len = len(text)

        while start < text_len:
            end = min(start + size, text_len)

            if end < text_len:
                # Find the latest sentence boundary at or before `end`
                best_boundary = None
                for pos in reversed(boundary_positions):
                    if start < pos <= end:
                        best_boundary = pos
                        break

                if best_boundary is not None:
                    end = best_boundary
                else:
                    # No sentence boundary: find nearest word boundary
                    while end > start and not text[end - 1].isspace():
                        end -= 1
                    if end == start:
                        end = min(start + size, text_len)   # force-split

            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append((chunk_text, start, end))

            if end >= text_len:
                break

            # Advance start with overlap (back up `overlap` chars from end,
            # but no further back than the last sentence boundary before end)
            overlap_start = max(start + 1, end - overlap)
            best_overlap_boundary = None
            for pos in reversed(boundary_positions):
                if overlap_start <= pos < end:
                    best_overlap_boundary = pos
                    break
            start = best_overlap_boundary if best_overlap_boundary is not None \
                else overlap_start

        return chunks
