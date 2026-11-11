OFFICIAL DPM-101 COURSE DOCUMENTS — REQUIRED BUT MISSING
==========================================================

The following four PDF files must be placed in this directory before the
chatbot's knowledge base can be built.

Expected filenames
------------------
  document_01.pdf   — Course Handbook & Overview (updated 2026-08-15)
  document_02.pdf   — Grading & Late Submission Policy V1 (2026-01-10, audit only)
  document_03.pdf   — Grading & Late Submission Policy V2 (updated 2026-10-05)
  document_04.pdf   — Final Exam Rules & Student Forum Excerpts (updated 2026-10-25)

IMPORTANT NOTES
---------------
  • document_02.pdf is OPTIONAL for startup but must be retained for audit history.
  • document_01.pdf, document_03.pdf, and document_04.pdf are REQUIRED.
    If any of these are missing, the application will start in DEGRADED status,
    the /api/health endpoint will return HTTP 503, and /api/chat will be blocked.

  • document_04.pdf contains BOTH official exam rules (Sections 1–2) AND
    unmoderated student forum comments (Section 3+). The document management
    system will automatically separate them. Only official sections are used
    for answers.

What happens without the PDFs
------------------------------
  The Module 3 document management system (backend/services/document_service.py)
  will detect missing files and report them. All module tests run correctly using
  synthetic test PDFs created by the pytest fixtures (tests/conftest.py).
  No production functionality is available without the real PDFs.

How to obtain the files
-----------------------
  Obtain the official DPM-101 PDF files from the IIMBx course portal or your
  internship supervisor. Place them in this directory with the exact filenames
  listed above.

This file is tracked by Git. The PDFs themselves are excluded by .gitignore
to avoid committing large binary files to source control.
