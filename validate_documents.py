"""
validate_documents.py — Real PDF Validation Script for Module 3

Run this script AFTER placing the four official DPM-101 PDFs in data/documents/:
  document_01.pdf  — Course Handbook & Overview
  document_02.pdf  — Grading & Late Submission Policy V1 (Outdated)
  document_03.pdf  — Grading & Late Submission Policy V2 (Current)
  document_04.pdf  — Final Exam Rules & Student Forum Excerpts

Usage:
    venv\Scripts\python.exe validate_documents.py

This script does NOT use Gemini, TF-IDF, SQLite, or any AI — it validates
document extraction and precedence rules only (Module 3 scope).

Output: a detailed report of extraction results for each PDF.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Ensure project root is on path ───────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DOC_REGISTRY, config
from backend.services.document_service import DocumentProcessor

SEPARATOR = "=" * 70
THIN_SEP  = "-" * 70


def main() -> None:
    print(SEPARATOR)
    print("DPM-101 Module 3 — Real PDF Validation")
    print(SEPARATOR)

    docs_dir = Path(config.DOCUMENTS_DIR)
    print(f"\nDocuments directory : {docs_dir}")
    print(f"Chunk size          : {config.CHUNK_SIZE} chars")
    print(f"Chunk overlap       : {config.CHUNK_OVERLAP} chars\n")

    # ── 1. Check which PDFs exist ─────────────────────────────────────────────
    print(THIN_SEP)
    print("1. PDF AVAILABILITY CHECK")
    print(THIN_SEP)

    all_present = True
    for doc_id, doc_config in DOC_REGISTRY.items():
        path = docs_dir / doc_config["filename"]
        status = "✓ FOUND" if path.exists() else "✗ MISSING"
        required = "(required)" if doc_config["is_required"] else "(optional — audit only)"
        size = f"  [{path.stat().st_size:,} bytes]" if path.exists() else ""
        print(f"  {doc_id}: {doc_config['filename']:35s} {status}{size} {required}")
        if not path.exists() and doc_config["is_required"]:
            all_present = False

    if not all_present:
        print("\n⚠  One or more REQUIRED PDFs are missing.")
        print("   Place all four official PDFs in:", docs_dir)
        print("   Real-document validation cannot proceed until they are present.")
        print("\n" + SEPARATOR)
        sys.exit(1)

    # ── 2. Run DocumentProcessor on all four PDFs ─────────────────────────────
    print("\n" + THIN_SEP)
    print("2. DOCUMENT PROCESSING")
    print(THIN_SEP)

    processor = DocumentProcessor(
        DOC_REGISTRY,
        str(docs_dir),
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
    )
    results = processor.process_all()

    # ── 3. Per-document extraction report ────────────────────────────────────
    print("\n" + THIN_SEP)
    print("3. PER-DOCUMENT EXTRACTION RESULTS")
    print(THIN_SEP)

    for doc_id, result in results.items():
        doc_config = DOC_REGISTRY[doc_id]
        print(f"\n[{doc_id.upper()}] {doc_config['title']}")
        print(f"  File          : {doc_config['filename']}")
        print(f"  Policy status : {doc_config['policy_status']}")
        print(f"  Is outdated   : {doc_config['is_outdated']}")

        if result.success:
            print(f"  Pages         : {result.page_count} ({result.empty_page_count} empty)")
            print(f"  Chunks total  : {result.total_chunks}")
            print(f"  Retrievable   : {result.official_chunks}")
            if result.excluded_chunks:
                print(f"  Excluded      : {result.excluded_chunks} (forum — never served)")
            if result.outdated_chunks:
                print(f"  Outdated      : {result.outdated_chunks} (superseded by doc03)")

            # Show first chunk as extraction sample
            official_chunks = [c for c in result.chunks if not c.is_excluded]
            if official_chunks:
                sample = official_chunks[0]
                preview = sample.text[:200].replace("\n", " ")
                print(f"  First chunk   : [{sample.chunk_id}] p{sample.page_number}")
                print(f"  Preview       : {preview!r}...")

            # PDF hash
            pdf_path = docs_dir / doc_config["filename"]
            pdf_hash = processor.compute_pdf_hash(pdf_path)
            print(f"  SHA-256       : {pdf_hash[:16]}...{pdf_hash[-8:]}")

        else:
            print(f"  Status : FAILED — {result.error}")

    # ── 4. Document precedence verification ───────────────────────────────────
    print("\n" + THIN_SEP)
    print("4. DOCUMENT PRECEDENCE VERIFICATION")
    print(THIN_SEP)

    checks = [
        ("doc01", False, False, "Doc01 — general course info (current, not outdated)"),
        ("doc02", True,  True,  "Doc02 — superseded grading V1 (outdated=True, is_required=True, superseded)"),
        ("doc03", False, False, "Doc03 — current grading authority (current, not outdated)"),
        ("doc04", False, False, "Doc04 — official exam info (not outdated; forum filtered)"),
    ]

    for doc_id, expect_outdated, expect_superseded, description in checks:
        result = results.get(doc_id)
        config_entry = DOC_REGISTRY[doc_id]
        actual_outdated = config_entry["is_outdated"]
        actual_superseded = config_entry["policy_status"] == "superseded"
        ok = (actual_outdated == expect_outdated) and (actual_superseded == expect_superseded)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {description}")
        if not ok:
            print(f"    Expected is_outdated={expect_outdated}, superseded={expect_superseded}")
            print(f"    Got      is_outdated={actual_outdated}, superseded={actual_superseded}")

    # ── 5. Doc04 section filtering verification ───────────────────────────────
    print("\n" + THIN_SEP)
    print("5. DOC04 SECTION FILTERING VERIFICATION")
    print(THIN_SEP)

    doc04_result = results.get("doc04")
    if doc04_result and doc04_result.success:
        official = [c for c in doc04_result.chunks if not c.is_excluded]
        forum    = [c for c in doc04_result.chunks if c.is_excluded]
        print(f"  Official exam rule chunks : {len(official)}")
        print(f"  Forum chunks (excluded)   : {len(forum)}")

        if official:
            all_official_text = " ".join(c.text for c in official).lower()
        else:
            all_official_text = ""

        # Key exam facts to verify
        exam_checks = [
            ("20 November 2026",     "Exam date (20 November 2026)"),
            ("90",                   "90-minute duration"),
            ("closed",               "Closed-book rule"),
            ("proctor",              "AI-proctoring"),
            ("desktop",              "Desktop/laptop requirement"),
            ("webcam",               "Webcam requirement"),
            ("mobile",               "Mobile restriction"),
        ]
        print("\n  Exam fact extraction (from official chunks only):")
        for keyword, label in exam_checks:
            found = keyword.lower() in all_official_text
            mark = "✓" if found else "⚠ NOT FOUND"
            print(f"    {mark}  {label} ({keyword!r})")

        # Verify forum content is NOT in official chunks
        forum_text = " ".join(c.text for c in forum).lower()
        if forum:
            print(f"\n  Forum content excluded     : ✓ ({len(forum)} forum chunks found, none retrievable)")
        else:
            print(f"\n  Forum chunks found         : 0 (Doc04 may have no forum section — verify PDF)")

    elif doc04_result and not doc04_result.success:
        print(f"  ✗ Doc04 processing failed: {doc04_result.error}")
    else:
        print("  Doc04 result not available.")

    # ── 6. Final summary ──────────────────────────────────────────────────────
    print("\n" + SEPARATOR)
    print(processor.get_processing_report(results))


if __name__ == "__main__":
    main()
