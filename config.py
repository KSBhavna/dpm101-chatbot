"""
config.py — Configuration module for DPM-101 Helpdesk Chatbot

Loads all settings from environment variables via python-dotenv.
Never hardcodes API keys or secrets.
All paths are derived from the project root so they work
regardless of the working directory Gunicorn uses.

Updated in Module 3: added DOC_REGISTRY, TOPIC_PRECEDENCE, chunking config,
and corrected DOCUMENTS_DIR to data/documents/.
"""

import os
from dotenv import load_dotenv

# Load .env file if it exists (development only).
# In production (Render) the environment variables are set in the dashboard.
load_dotenv()


class Config:
    def __init__(self) -> None:
        # Reload/read from environment dynamically on instantiation
        self.GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
        self.GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.GEMINI_TEMPERATURE: float = float(os.getenv("GEMINI_TEMPERATURE", "0.2"))
        self.GEMINI_MAX_OUTPUT_TOKENS: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "1024"))
        self.GEMINI_REQUEST_TIMEOUT: float = float(os.getenv("GEMINI_REQUEST_TIMEOUT", "30.0"))
        self.GEMINI_MAX_RETRIES: int = int(os.getenv("GEMINI_MAX_RETRIES", "2"))

        self.FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "False").lower() == "true"
        self.SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")

        self.BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
        self.DATA_DIR: str = os.path.join(self.BASE_DIR, "data")
        self.DB_PATH: str = os.path.join(self.DATA_DIR, "chatbot.db")
        self.DOCUMENTS_DIR: str = os.path.join(self.BASE_DIR, "data", "documents")

        self.CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "1000"))
        self.CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "150"))
        self.SIMILARITY_THRESHOLD: float = float(os.getenv("SIMILARITY_THRESHOLD", "0.90"))
        self.PROMPT_VERSION: str = os.getenv("PROMPT_VERSION", "v1")

        # Module 7: SQLite Caching Configuration
        self.CACHE_ENABLED: bool = os.getenv("CACHE_ENABLED", "True").lower() == "true"
        self.CACHE_DEFAULT_TTL: int = int(os.getenv("CACHE_DEFAULT_TTL", "86400"))  # 24 hours
        self.CACHE_MAX_ENTRIES: int = int(os.getenv("CACHE_MAX_ENTRIES", "1000"))
        self.POLICY_VERSION: str = os.getenv("POLICY_VERSION", "v2")

        # Module 8: Request Management & Duplicate Prevention
        self.RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "True").lower() == "true"
        self.RATE_LIMIT_MAX_REQUESTS: int = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "20"))
        self.RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
        self.RATE_LIMIT_CONVERSATION_MAX: int = int(os.getenv("RATE_LIMIT_CONVERSATION_MAX", "15"))
        self.RATE_LIMIT_CONVERSATION_WINDOW: int = int(os.getenv("RATE_LIMIT_CONVERSATION_WINDOW", "60"))
        self.DUPLICATE_WINDOW_SECONDS: int = int(os.getenv("DUPLICATE_WINDOW_SECONDS", "10"))
        self.IDEMPOTENCY_KEY_TTL: int = int(os.getenv("IDEMPOTENCY_KEY_TTL", "300"))
        self.CONVERSATION_LOCK_TIMEOUT: float = float(os.getenv("CONVERSATION_LOCK_TIMEOUT", "5.0"))
        self.TRUSTED_PROXIES_COUNT: int = int(os.getenv("TRUSTED_PROXIES_COUNT", "1"))

    # Class-level defaults for backwards compatibility
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "False").lower() == "true"
    SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")
    BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR: str = os.path.join(BASE_DIR, "data")
    DB_PATH: str = os.path.join(DATA_DIR, "chatbot.db")
    DOCUMENTS_DIR: str = os.path.join(BASE_DIR, "data", "documents")
    CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "1000"))
    CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "150"))
    SIMILARITY_THRESHOLD: float = float(os.getenv("SIMILARITY_THRESHOLD", "0.90"))
    PROMPT_VERSION: str = os.getenv("PROMPT_VERSION", "v1")
    CACHE_ENABLED: bool = os.getenv("CACHE_ENABLED", "True").lower() == "true"
    CACHE_DEFAULT_TTL: int = int(os.getenv("CACHE_DEFAULT_TTL", "86400"))
    CACHE_MAX_ENTRIES: int = int(os.getenv("CACHE_MAX_ENTRIES", "1000"))
    POLICY_VERSION: str = os.getenv("POLICY_VERSION", "v2")
    RATE_LIMIT_ENABLED: bool = os.getenv("RATE_LIMIT_ENABLED", "True").lower() == "true"
    RATE_LIMIT_MAX_REQUESTS: int = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "20"))
    RATE_LIMIT_WINDOW_SECONDS: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    RATE_LIMIT_CONVERSATION_MAX: int = int(os.getenv("RATE_LIMIT_CONVERSATION_MAX", "15"))
    RATE_LIMIT_CONVERSATION_WINDOW: int = int(os.getenv("RATE_LIMIT_CONVERSATION_WINDOW", "60"))
    DUPLICATE_WINDOW_SECONDS: int = int(os.getenv("DUPLICATE_WINDOW_SECONDS", "10"))
    IDEMPOTENCY_KEY_TTL: int = int(os.getenv("IDEMPOTENCY_KEY_TTL", "300"))
    CONVERSATION_LOCK_TIMEOUT: float = float(os.getenv("CONVERSATION_LOCK_TIMEOUT", "5.0"))
    TRUSTED_PROXIES_COUNT: int = int(os.getenv("TRUSTED_PROXIES_COUNT", "1"))


# Single shared instance imported by the rest of the application.
config = Config()


# ── Document Registry ─────────────────────────────────────────────────────────
# Defines every official DPM-101 source document.
# Authorities and precedence rules are encoded here, not inferred from filenames.
#
# policy_status values:
#   "current"                 — authoritative and in effect
#   "superseded"              — retained for audit only; grading content overridden by doc03
#   "official_exam_information" — authoritative for exam logistics only
#
# is_required:
#   True  — missing or failed document raises a DEGRADED startup status
#           and blocks POST /api/chat (enforced in Module 5+)
#   False — missing document is logged but does not block startup
#
# is_outdated:
#   True  — all grading/submission content is superseded; retrieval filter excludes it
#   False — content is authoritative for its declared topic scope
#
# section_config (doc04 only):
#   Controls which sections are ingested as official and which are excluded as forum.
#   Patterns are matched against the beginning of text blocks (page-level headings).
#   These are DESIGN-TIME defaults; must be verified against the actual PDF in Module 3.
# ─────────────────────────────────────────────────────────────────────────────

DOC_REGISTRY = {
    "doc01": {
        "doc_id":        "doc01",
        "filename":      "01_course_handbook.txt",
        "title":         "Course Handbook & Overview",
        "last_updated":  "2026-08-15",
        "policy_status": "current",
        "is_required":   True,
        "is_outdated":   False,
        "section_config": None,          # no section filtering needed
        "topic_scope": [
            "course_structure", "schedule", "faculty",
            "objective", "certificate",
        ],
    },
    "doc02": {
        "doc_id":        "doc02",
        "filename":      "02_grading_policy_v1.txt",
        "title":         "Grading & Late Submission Policy V1 (Historical)",
        "last_updated":  "2026-01-10",
        "policy_status": "superseded",
        # is_required=True: Doc02 must be present for complete four-document
        # processing. It is superseded by Doc03 on conflicting grading and
        # late-submission rules, but it is not optional — its absence means
        # the knowledge base is incomplete and the system must report DEGRADED.
        "is_required":   True,
        "is_outdated":   True,           # ALL grading/submission content overridden by doc03
        "section_config": None,
        "topic_scope": [
            "grading", "assessment", "passing_requirement",
            "distinction", "late_submission", "penalty",
            "assignment_deadline", "quiz_deadline",
        ],
    },
    "doc03": {
        "doc_id":        "doc03",
        "filename":      "03_grading_policy_v2.txt",
        "title":         "Grading & Late Submission Policy V2 (Current)",
        "last_updated":  "2026-10-05",
        "policy_status": "current",
        "is_required":   True,
        "is_outdated":   False,
        "section_config": None,
        "topic_scope": [
            "grading", "assessment", "passing_requirement",
            "distinction", "late_submission", "penalty",
            "assignment_deadline", "quiz_deadline",
        ],
    },
    "doc04": {
        "doc_id":        "doc04",
        "filename":      "04_exam_rules_and_faq.txt",
        "title":         "Final Exam Rules & Student Forum Excerpts",
        "last_updated":  "2026-10-25",
        "policy_status": "official_exam_information",
        "is_required":   True,
        "is_outdated":   False,
        "section_config": {
            # Patterns matched against section header lines or block tops.
            # Case-insensitive. Section 1 & 2 are official; Section 3+ / Forum are excluded.
            "official_patterns": [
                r"(?i)^section\s+[12]\b",
                r"(?i)^1[\.\)]\s+official\s+proctored",
                r"(?i)^1[\.\)]\s+final\s+exam",
                r"(?i)^2[\.\)]\s+technical\s+requirements",
                r"(?i)^2[\.\)]\s+",
                r"(?i)^official\s+proctored\s+final\s+exam\s+rules",
                r"(?i)^final\s+exam\s+rules",
                r"(?i)^examination\s+rules",
            ],
            "forum_patterns": [
                r"(?i)^section\s+[3-9]\b",
                r"(?i)^3[\.\)]\s+unmoderated\s+student\s+discussion",
                r"(?i)^3[\.\)]\s+",
                r"(?i)^student\s+forum",
                r"(?i)^forum\s+excerpt",
                r"(?i)^student\s+comment",
                r"(?i)^discussion\s+forum",
                r"(?i)^unmoderated",
            ],
        },
        "topic_scope": [
            "final_exam", "exam_format", "exam_device",
            "proctoring", "exam_date",
        ],
    },
}


# ── Topic-to-Document Precedence ──────────────────────────────────────────────
# Maps each topic category to the list of authoritative doc IDs (in priority order).
# Used by retriever.py (Module 5) to enforce document authority.
# "doc04_official" is a virtual alias for doc04 chunks with section_type='official_exam_rules'.
# ─────────────────────────────────────────────────────────────────────────────

TOPIC_PRECEDENCE = {
    # Grading and assessment — Doc 03 is sole current authority;
    # Doc 02 chunks are marked is_outdated=True and must not be used as current policy.
    "grading":              ["doc03"],
    "assessment":           ["doc03"],
    "passing_requirement":  ["doc03"],   # 60% overall required (Doc03)
    "distinction":          ["doc03"],   # 85% or above (Doc03)
    "late_submission":      ["doc03"],
    "penalty":              ["doc03"],

    # Submission deadlines — Doc 03 authority
    "assignment_deadline":  ["doc03"],
    "quiz_deadline":        ["doc03"],
    "capstone_deadline":    ["doc03"],   # November 15, 2026, 11:59 PM IST (Doc03)
    "quiz_submission":      ["doc03"],   # Best 5 of 6 weekly quizzes (Doc03)

    # Final exam logistics and date — Doc 04 official sections only.
    # "doc04_official" is a virtual alias: chunks WHERE section_type='official_exam_rules'
    # Forum chunks (is_excluded=True) are never served regardless of topic.
    "final_exam":           ["doc04_official"],
    "exam_format":          ["doc04_official"],
    "exam_device":          ["doc04_official"],
    "proctoring":           ["doc04_official"],
    "exam_date":            ["doc04_official"],   # November 20, 2026 (Doc04 §1)
    "exam_slots":           ["doc04_official"],   # 10 AM–8 PM IST (Doc04 §1)
    "exam_materials":       ["doc04_official"],   # rough paper, calculator (Doc04 §1)
    "exam_id":              ["doc04_official"],   # government photo ID (Doc04 §1)
    "exam_browser":         ["doc04_official"],   # Chrome/Edge, 2 Mbps (Doc04 §2)

    # General course information — Doc 01 sole authority
    "schedule":             ["doc01"],   # weekly schedule (NOT exam date)
    "faculty":              ["doc01"],
    "objective":            ["doc01"],
    "certificate":          ["doc01"],
    "certificate_fee":      ["doc01"],   # certificate fee is a Doc01 topic
    "course_structure":     ["doc01"],
    "course_duration":      ["doc01"],
    "audit_track":          ["doc01"],
    "verified_track":       ["doc01"],

    # Forum comments — structurally excluded, never an answer source
    "forum":                [],
}
