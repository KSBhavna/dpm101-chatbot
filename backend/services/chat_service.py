"""
backend/services/chat_service.py — Persistent Chat Management & Conversation History

Module 6: Chat Management & Conversation History
Responsibilities:
  - Database schema management: SQLite tables `conversations` and `messages`.
  - Conversation lifecycle: create, get, list, delete, verify existence.
  - Message management: save user message, save assistant message, list messages.
  - Context-aware follow-up resolution: disambiguate pronouns and follow-up queries.
  - Safe transaction-based operations, parameterized queries, and UTC timestamps.
  - Decoupled from direct Gemini calls; orchestrates via Module 5 PolicyService.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, Any

from config import config
from backend.services.policy_service import PolicyService, PolicyAnswer
from backend.services.cache_service import CacheService, init_cache_table

logger = logging.getLogger(__name__)


# ── Database Initialization & Connection Helper ───────────────────────────────

@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """
    Context manager that explicitly executes BEGIN IMMEDIATE to acquire
    a SQLite write lock immediately at transaction start.
    Guarantees that no concurrent writer can modify the database between
    precondition checks and writes, eliminating isolation races and upgrade deadlocks.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Open an isolated SQLite connection with autocommit mode (isolation_level=None),
    foreign keys enabled, WAL mode, and busy timeout.
    """
    path = db_path or config.DB_PATH
    # Ensure parent directory exists for file-based DBs
    if path != ":memory:" and not path.startswith("file:"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    conn = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 15000;")
    # WAL mode improves concurrency in multi-request environments (ignore error for in-memory DB)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize SQLite schema for conversations and messages if not already present.
    Includes backward-compatible migration for the title column.
    """
    conn = get_db_connection(db_path)
    try:
        with immediate_transaction(conn):
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                );
            """)

            # Backward-compatible column migration for existing SQLite databases
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN title TEXT;")
            except sqlite3.OperationalError:
                pass  # Column already exists

            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sources TEXT,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
                ON messages(conversation_id, created_at ASC);
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                ON conversations(updated_at DESC);
            """)

            # Module 7: Cache table initialization
            init_cache_table(conn)
        logger.info("Chat database schema initialized successfully at %s", db_path or config.DB_PATH)
    finally:
        conn.close()


# ── Title Generation Helper ───────────────────────────────────────────────────

def generate_conversation_title(query: Optional[str]) -> str:
    """
    Generate a concise, readable, and meaningful conversation title based on the first user query.
    Deterministic, offline, zero API quota consumption.
    """
    if not query or not isinstance(query, str):
        return "New Chat"

    q = query.strip()
    if not q:
        return "New Chat"

    q_lower = q.lower()

    # 1. Direct topic/intent mappings for official DPM-101 course inquiries
    if re.search(r"\b(passing (requirement|requirements|criteria|score|marks)|pass(ing)? the course|pass criteria)\b", q_lower):
        return "Passing Requirements"
    if re.search(r"\b(distinction|pass with distinction)\b", q_lower):
        return "Pass with Distinction"
    if re.search(r"\b(when is (the )?final exam|final exam (date|schedule|timing|time|slot)|exam schedule)\b", q_lower):
        return "Final Exam Schedule"
    if re.search(r"\b(final exam (rules|guidelines|instructions)|proctored (final )?exam|exam rules)\b", q_lower):
        return "Final Exam Rules"
    if re.search(r"\b(capstone (deadline|due)|deadline for capstone|when is (the )?capstone)\b", q_lower):
        return "Capstone Deadline"
    if re.search(r"\b(capstone (submission|extension|late)|submit capstone)\b", q_lower):
        return "Capstone Submission"
    if re.search(r"\b(quiz|quizzes)\b.*\b(deadline|deadlines|due|timing|schedule)\b|\b(deadline|deadlines|due)\b.*\b(quiz|quizzes)\b", q_lower):
        return "Quiz Deadlines"
    if re.search(r"\b(quiz|quizzes|weekly quizzes)\b", q_lower):
        return "Weekly Quizzes"
    if re.search(r"\b(certificate fee|cost of certificate|verified certificate fee)\b", q_lower):
        return "Certificate Fee"
    if re.search(r"\b(certificate|verified track|verified certificate)\b", q_lower):
        return "Verified Certificate"
    if re.search(r"\b(refund|money back|fee refund)\b", q_lower):
        return "Refund Policy"
    if re.search(r"\b(grading (policy|scheme|breakdown|weightage)|grade calculation)\b", q_lower):
        return "Grading Policy"
    if re.search(r"\b(course duration|how long is the course|course schedule|weekly schedule)\b", q_lower):
        return "Course Schedule"
    if re.search(r"\b(calculator|rough paper|scratch paper)\b", q_lower):
        return "Exam Materials"
    if re.search(r"\b(laptop|desktop|webcam|microphone|browser|chrome|edge)\b", q_lower):
        return "Technical Requirements"

    # 2. General extraction fallback: strip question prefixes
    cleaned = re.sub(
        r"^(what (is|are|about) (the|a|an)?|when (is|are) (the|a|an)?|where (is|are) (the|a|an)?|how (do|does|can) (i|we)?|can (i|we)?|is (there|it) (a|an|the)?|tell me about|please explain|could you tell me)\s+",
        "",
        q,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip("? .!\t\r\n")
    if not cleaned:
        cleaned = q.strip("? .!\t\r\n")

    words = cleaned.split()
    if not words:
        return "New Chat"

    # Limit to max 4 words for concise title
    if len(words) > 4:
        words = words[:4]

    title = " ".join(w.capitalize() if not w.isupper() else w for w in words)
    if len(title) > 30:
        title = title[:27].rstrip() + "..."
    return title


# ── Context Resolution Helper ─────────────────────────────────────────────────

# Ambiguous follow-ups that require clarification if multiple candidate topics exist
AMBIGUOUS_PATTERNS = [
    r"^(what about the deadline\??|when is the deadline\??|deadline\??)$",
    r"^(can i get an extension\??|is there an extension\??)$",
    r"^(is that allowed\??|can i do that\??)$",
    r"^(what about it\??|how does that work\??)$",
]

# Patterns that indicate an anaphoric follow-up question
FOLLOW_UP_PRONOUNS = [
    r"\b(it|this|that|they|them)\b",
    r"^how long\b",
    r"^what date\b",
    r"^can i take it\b",
    r"^can i submit (it )?late\b",
    r"^what device\b",
    r"^is it open[- ]book\b",
    r"^what can i bring\b",
]


class ChatService:
    """
    Service managing conversation persistence, message ordering, and contextual follow-up.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        policy_service: Optional[PolicyService] = None,
        cache_service: Optional[CacheService] = None,
        max_context_messages: int = 6,
    ) -> None:
        self.db_path = db_path or config.DB_PATH
        self.cache_service = cache_service or CacheService(db_path=self.db_path)
        self.policy_service = policy_service or PolicyService(cache_service=self.cache_service)
        self.max_context_messages = max_context_messages
        # Ensure schema is ready
        init_db(self.db_path)

    def _get_connection(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    # ── Conversation Lifecycle ────────────────────────────────────────────────

    def create_conversation(self, title: Optional[str] = None) -> dict[str, Any]:
        """
        Create a new conversation record with status='active'.
        """
        conv_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        conv_title = title or "New Chat"
        conn = self._get_connection()
        try:
            with immediate_transaction(conn):
                conn.execute(
                    "INSERT INTO conversations (id, title, created_at, updated_at, status) VALUES (?, ?, ?, ?, 'active');",
                    (conv_id, conv_title, now_iso, now_iso),
                )
            return {
                "id": conv_id,
                "title": conv_title,
                "created_at": now_iso,
                "updated_at": now_iso,
                "status": "active",
            }
        finally:
            conn.close()

    def get_conversation(
        self, conversation_id: str, include_deleted: bool = False
    ) -> Optional[dict[str, Any]]:
        """
        Retrieve conversation metadata by ID.
        Returns None if non-existent or deleted (unless include_deleted=True).
        Backfills meaningful title from first user message if title is missing.
        """
        if not conversation_id or not isinstance(conversation_id, str):
            return None

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            status_clause = "" if include_deleted else " AND status = 'active'"
            cursor.execute(
                f"""
                SELECT id, title, created_at, updated_at, status,
                       (SELECT content FROM messages WHERE conversation_id = conversations.id AND role = 'user' ORDER BY created_at ASC LIMIT 1) AS first_message
                FROM conversations
                WHERE id = ?{status_clause};
                """,
                (conversation_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            title = row["title"]
            if not title or title == "New Chat":
                first_msg = row["first_message"]
                if first_msg:
                    title = generate_conversation_title(first_msg)
                    try:
                        with immediate_transaction(conn):
                            conn.execute("UPDATE conversations SET title = ? WHERE id = ?;", (title, row["id"]))
                    except Exception:
                        pass
                else:
                    title = "New Chat"

            return {
                "id": row["id"],
                "title": title,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "status": row["status"],
            }
        finally:
            conn.close()

    def list_conversations(
        self, include_deleted: bool = False, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """
        List conversations ordered by most recently updated.
        Backfills meaningful title from first user message if title is missing.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            status_clause = "" if include_deleted else "WHERE status = 'active'"
            cursor.execute(
                f"""
                SELECT id, title, created_at, updated_at, status,
                       (SELECT content FROM messages WHERE conversation_id = conversations.id AND role = 'user' ORDER BY created_at ASC LIMIT 1) AS first_message
                FROM conversations
                {status_clause}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?;
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
            results = []
            backfills = []
            for row in rows:
                title = row["title"]
                first_msg = row["first_message"]
                if not title or title == "New Chat":
                    if first_msg:
                        title = generate_conversation_title(first_msg)
                        backfills.append((title, row["id"]))
                    else:
                        title = "New Chat"

                results.append({
                    "id": row["id"],
                    "title": title,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "status": row["status"],
                })

            if backfills:
                try:
                    with immediate_transaction(conn):
                        cursor.executemany("UPDATE conversations SET title = ? WHERE id = ?;", backfills)
                except Exception:
                    pass

            return results
        finally:
            conn.close()

    def verify_conversation_active(self, conversation_id: str) -> tuple[bool, Optional[str]]:
        """
        Check if a conversation exists and is active.
        Returns (True, None) or (False, error_message).
        """
        if not conversation_id or not isinstance(conversation_id, str):
            return False, "Invalid conversation ID format"

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM conversations WHERE id = ?;",
                (conversation_id,),
            )
            row = cursor.fetchone()
            if not row:
                return False, "Conversation not found"
            if row["status"] != "active":
                return False, "Conversation has been deleted and cannot accept new messages"
            return True, None
        finally:
            conn.close()

    def delete_conversation(self, conversation_id: str, hard_delete: bool = False) -> bool:
        """
        Delete a conversation and its messages safely and atomically.
        Uses BEGIN IMMEDIATE to acquire the write lock before checking status,
        preventing concurrent message insertions during the deletion window.
        Returns False if the conversation does not exist or was already deleted.
        """
        if not conversation_id or not isinstance(conversation_id, str):
            return False

        conn = self._get_connection()
        try:
            with immediate_transaction(conn):
                cursor = conn.cursor()
                cursor.execute("SELECT id, status FROM conversations WHERE id = ?;", (conversation_id,))
                row = cursor.fetchone()
                if not row or row["status"] != "active":
                    return False

                now_iso = datetime.now(timezone.utc).isoformat()
                # Purge messages first within the immediate transaction
                cursor.execute("DELETE FROM messages WHERE conversation_id = ?;", (conversation_id,))
                if hard_delete:
                    cursor.execute("DELETE FROM conversations WHERE id = ?;", (conversation_id,))
                else:
                    cursor.execute(
                        "UPDATE conversations SET status = 'deleted', updated_at = ? WHERE id = ?;",
                        (now_iso, conversation_id),
                    )
            return True
        finally:
            conn.close()

    def update_conversation_activity(
        self, conversation_id: str, timestamp: Optional[str] = None
    ) -> None:
        """
        Update the updated_at timestamp of a conversation.
        """
        now_iso = timestamp or datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        try:
            with immediate_transaction(conn):
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ? AND status = 'active';",
                    (now_iso, conversation_id),
                )
        finally:
            conn.close()

    # ── Message Operations ────────────────────────────────────────────────────

    def save_user_message(self, conversation_id: str, content: str) -> dict[str, Any]:
        """
        Save a user message atomically and advance the conversation's updated_at timestamp.
        Uses BEGIN IMMEDIATE write transaction + atomic conditional INSERT.
        Raises ValueError if the conversation is not found or has been deleted.
        """
        msg_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._get_connection()
        try:
            with immediate_transaction(conn):
                cursor = conn.cursor()
                # Atomic conditional insert: only inserts if conversation exists and is active
                cursor.execute(
                    """
                    INSERT INTO messages (id, conversation_id, role, content, created_at, sources)
                    SELECT ?, id, 'user', ?, ?, NULL
                    FROM conversations
                    WHERE id = ? AND status = 'active';
                    """,
                    (msg_id, content, now_iso, conversation_id),
                )
                if cursor.rowcount == 0:
                    raise ValueError("Conversation not found or has been deleted")

                # Check if conversation needs a title generated from this first user message
                cursor.execute("SELECT title FROM conversations WHERE id = ?;", (conversation_id,))
                conv_row = cursor.fetchone()
                current_title = conv_row["title"] if conv_row else None
                if not current_title or current_title == "New Chat":
                    new_title = generate_conversation_title(content)
                    cursor.execute(
                        "UPDATE conversations SET updated_at = ?, title = ? WHERE id = ? AND status = 'active';",
                        (now_iso, new_title, conversation_id),
                    )
                else:
                    cursor.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ? AND status = 'active';",
                        (now_iso, conversation_id),
                    )
            return {
                "id": msg_id,
                "conversation_id": conversation_id,
                "role": "user",
                "content": content,
                "created_at": now_iso,
                "sources": None,
            }
        finally:
            conn.close()

    def save_assistant_message(
        self, conversation_id: str, content: str, sources: Optional[list[str]] = None
    ) -> dict[str, Any]:
        """
        Save an assistant message atomically with optional source attributions and update timestamp.
        Uses BEGIN IMMEDIATE write transaction + atomic conditional INSERT.
        Raises ValueError if the conversation is not found or has been deleted.
        """
        msg_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        sources_json = json.dumps(sources) if sources else None
        conn = self._get_connection()
        try:
            with immediate_transaction(conn):
                cursor = conn.cursor()
                # Atomic conditional insert: only inserts if conversation exists and is active
                cursor.execute(
                    """
                    INSERT INTO messages (id, conversation_id, role, content, created_at, sources)
                    SELECT ?, id, 'assistant', ?, ?, ?
                    FROM conversations
                    WHERE id = ? AND status = 'active';
                    """,
                    (msg_id, content, now_iso, sources_json, conversation_id),
                )
                if cursor.rowcount == 0:
                    raise ValueError("Conversation not found or has been deleted")

                cursor.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ? AND status = 'active';",
                    (now_iso, conversation_id),
                )
            return {
                "id": msg_id,
                "conversation_id": conversation_id,
                "role": "assistant",
                "content": content,
                "created_at": now_iso,
                "sources": sources or [],
            }
        finally:
            conn.close()

    def get_conversation_messages(
        self, conversation_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """
        Retrieve messages for a conversation ordered chronologically (created_at ASC, rowid ASC).
        Guarantees stable, deterministic ordering even when timestamps are identical.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, conversation_id, role, content, created_at, sources
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ? OFFSET ?;
                """,
                (conversation_id, limit, offset),
            )
            rows = cursor.fetchall()
            messages: list[dict[str, Any]] = []
            for r in rows:
                raw_sources = r["sources"]
                parsed_sources = json.loads(raw_sources) if raw_sources else None
                messages.append({
                    "id": r["id"],
                    "conversation_id": r["conversation_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "created_at": r["created_at"],
                    "sources": parsed_sources,
                })
            return messages
        finally:
            conn.close()

    def get_recent_messages(
        self, conversation_id: str, limit: int = 6
    ) -> list[dict[str, Any]]:
        """
        Retrieve the most recent N messages for a conversation,
        returned in chronological order (oldest to newest among the recent N).
        Uses deterministic rowid tie-breaking to ensure the absolute latest messages are selected.
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, conversation_id, role, content, created_at, sources
                FROM (
                    SELECT id, conversation_id, role, content, created_at, sources, rowid
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, rowid ASC;
                """,
                (conversation_id, limit),
            )
            rows = cursor.fetchall()
            messages: list[dict[str, Any]] = []
            for r in rows:
                raw_sources = r["sources"]
                parsed_sources = json.loads(raw_sources) if raw_sources else None
                messages.append({
                    "id": r["id"],
                    "conversation_id": r["conversation_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "created_at": r["created_at"],
                    "sources": parsed_sources,
                })
            return messages
        finally:
            conn.close()

    # ── Context-Aware Follow-Up Resolution ────────────────────────────────────

    def resolve_context(
        self, user_text: str, history: list[dict[str, Any]]
    ) -> tuple[str, bool, Optional[str]]:
        """
        Evaluate user_text against previous history context.
        Returns:
          (resolved_query, is_ambiguous, clarification_prompt)

        Rules:
          - If the user query is self-contained, return as-is without context contamination.
          - If it contains pronouns ("it", "this") or short ellipses ("how long"), resolve entity.
          - If multiple conflicting candidates exist or query is vague, flag as ambiguous.
        """
        clean_q = user_text.strip()
        q_lower = clean_q.lower()

        if not history:
            return clean_q, False, None

        # 1. Check for genuine ambiguity (e.g. "What about the deadline?" with multiple past topics)
        is_ambiguous_phrase = any(re.search(pat, q_lower) for pat in AMBIGUOUS_PATTERNS)
        if is_ambiguous_phrase:
            # Check what was discussed in history
            history_text = " ".join(m["content"].lower() for m in history)
            has_quizzes = "quiz" in history_text
            has_capstone = "capstone" in history_text
            if has_quizzes and has_capstone:
                return (
                    clean_q,
                    True,
                    "Could you please clarify whether you are referring to the weekly quiz deadlines or the Capstone Product Case deadline?",
                )

        # 2. Check if the query has follow-up pronouns or ellipses
        needs_context = False
        for pat in FOLLOW_UP_PRONOUNS:
            if re.search(pat, q_lower):
                needs_context = True
                break

        # Also check for short questions without subject (e.g., "How long?", "Duration?", "Can I take it on mobile?")
        if len(q_lower.split()) <= 4 and any(w in q_lower for w in ("how long", "duration", "date", "when", "format")):
            needs_context = True

        if not needs_context:
            # Self-contained question — do not contaminate with previous turn
            return clean_q, False, None

        # 3. Find the most recent core course entity discussed in history
        # Reverse iterate through history to find the latest turn's topic
        recent_topic = None
        for m in reversed(history):
            content_lower = m["content"].lower()
            if any(w in content_lower for w in ("final exam", "proctored exam", "proctored final", "exam")):
                recent_topic = "final exam"
                break
            elif any(w in content_lower for w in ("capstone", "product case", "capstone case")):
                recent_topic = "capstone"
                break
            elif any(w in content_lower for w in ("quiz", "quizzes", "weekly quiz")):
                recent_topic = "quiz"
                break
            elif any(w in content_lower for w in ("verified certificate", "verified track", "certificate fee", "cost")):
                recent_topic = "certificate"
                break
            elif any(w in content_lower for w in ("course duration", "workload", "overview")):
                recent_topic = "course"
                break

        if not recent_topic:
            return clean_q, False, None

        # 4. Resolve query based on entity
        resolved = clean_q
        if recent_topic == "final exam":
            # "How long is it?" -> "How long is the final exam?"
            if re.search(r"\b(how long|duration)\b", q_lower):
                resolved = "What is the duration of the final exam?"
            elif re.search(r"\b(device|laptop|desktop|mobile|tablet|phone)\b", q_lower):
                resolved = f"{clean_q} (referring to the final exam)"
            elif re.search(r"\b(open[- ]book|closed[- ]book|notes|ai|chatgpt)\b", q_lower):
                resolved = f"{clean_q} (referring to the final exam)"
            elif re.search(r"\b(it|this|that)\b", q_lower):
                resolved = re.sub(r"\b(it|this|that)\b", "the final exam", clean_q, flags=re.IGNORECASE)
        elif recent_topic == "capstone":
            # "Can I submit it one day late?" -> "Can I submit the capstone submission one day late?"
            # "What if I miss it by just one day?" -> "What if I miss the capstone submission by just one day?"
            if re.search(r"\b(submit|submission|late|extension|deadline|due|miss|missed)\b", q_lower):
                if re.search(r"\b(it|this|that)\b", q_lower):
                    resolved = re.sub(r"\b(it|this|that)\b", "the capstone submission", clean_q, flags=re.IGNORECASE)
                else:
                    resolved = f"{clean_q} for the capstone submission"
        elif recent_topic == "quiz":
            if re.search(r"\b(submit|submission|late|miss|missed)\b", q_lower):
                if re.search(r"\b(it|this|that|one)\b", q_lower):
                    resolved = re.sub(r"\b(it|this|that)\b", "a weekly quiz", clean_q, flags=re.IGNORECASE)
                else:
                    resolved = f"{clean_q} for weekly quizzes"

        return resolved, False, None

    # ── High-Level Message Processing Pipeline ────────────────────────────────

    def process_message(self, conversation_id: str, user_text: str) -> dict[str, Any]:
        """
        Complete message handling pipeline:
          1. Verify conversation exists and is active.
          2. Save user message.
          3. Retrieve recent conversation history (last N messages).
          4. Resolve context for follow-up questions.
          5. Call Module 5 PolicyService.
          6. Save assistant message and source attribution.
          7. Update conversation activity timestamp.
          8. Return structured assistant payload.
        """
        # 1. Verification
        is_active, err_msg = self.verify_conversation_active(conversation_id)
        if not is_active:
            raise ValueError(err_msg)

        clean_text = user_text.strip()

        # 2. Save User Message
        user_msg = self.save_user_message(conversation_id, clean_text)

        # 3. Retrieve recent history for context (fetch latest N+1 messages to exclude current)
        recent_history = self.get_recent_messages(
            conversation_id, limit=self.max_context_messages + 1
        )
        # Exclude the newly saved user message from historical context window
        history_before = [m for m in recent_history if m["id"] != user_msg["id"]][-self.max_context_messages:]

        # 4. Context Resolution
        resolved_q, is_ambiguous, clarification = self.resolve_context(
            clean_text, history_before
        )

        if is_ambiguous and clarification:
            # Re-check active status before saving clarification
            is_active_amb, err_amb = self.verify_conversation_active(conversation_id)
            if not is_active_amb:
                raise ValueError(err_amb or "Conversation was deleted during processing")

            assistant_msg = self.save_assistant_message(
                conversation_id, clarification, sources=[]
            )
            return {
                "conversation_id": conversation_id,
                "message_id": assistant_msg["id"],
                "role": "assistant",
                "content": clarification,
                "sources": [],
                "created_at": assistant_msg["created_at"],
                "is_in_scope": True,
                "is_supported": True,
                "is_duplicate": False,
            }

        # 5. Determine context dependency for safe caching (Module 7)
        is_context_dependent = (resolved_q.strip().lower() != clean_text.strip().lower()) or bool(
            history_before and any(re.search(pat, clean_text.lower()) for pat in FOLLOW_UP_PRONOUNS)
        )

        # 6. Call Module 5 Policy Pipeline
        try:
            policy_ans: PolicyAnswer = self.policy_service.ask(
                resolved_q, is_context_dependent=is_context_dependent
            )
        except TypeError:
            policy_ans = self.policy_service.ask(resolved_q)

        # Re-check active status before saving assistant reply in case conversation
        # was deleted during AI generation
        is_active_after, err_msg_after = self.verify_conversation_active(conversation_id)
        if not is_active_after:
            raise ValueError(err_msg_after or "Conversation was deleted during processing")

        # 6. Save Assistant Message
        assistant_msg = self.save_assistant_message(
            conversation_id=conversation_id,
            content=policy_ans.answer,
            sources=policy_ans.sources,
        )

        # 7. Return Result Payload
        return {
            "conversation_id": conversation_id,
            "message_id": assistant_msg["id"],
            "role": "assistant",
            "content": policy_ans.answer,
            "sources": policy_ans.sources,
            "created_at": assistant_msg["created_at"],
            "is_in_scope": policy_ans.is_in_scope,
            "is_supported": policy_ans.is_supported,
            "is_duplicate": False,
        }
