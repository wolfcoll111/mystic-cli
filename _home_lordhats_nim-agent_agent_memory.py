"""
SQLite-based conversation memory for NIM Agent.
Lightweight, async, and optimized for Raspberry Pi.
"""

import json
import time
import logging
import aiosqlite
from pathlib import Path

logger = logging.getLogger("nim_agent.memory")

# Keep last N messages in active context to stay within token limits
MAX_CONTEXT_MESSAGES = 50


class Memory:
    """Async SQLite conversation memory."""

    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def initialize(self):
        """Create tables if they don't exist."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                project_name TEXT DEFAULT 'main',
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                name TEXT,
                timestamp REAL NOT NULL
            )
        """)
        # Safely migrate existing DB to support projects
        try:
            await self._db.execute("ALTER TABLE messages ADD COLUMN project_name TEXT DEFAULT 'main'")
        except Exception:
            pass

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                chat_id INTEGER PRIMARY KEY,
                summary TEXT DEFAULT '',
                last_activity REAL,
                message_count INTEGER DEFAULT 0
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_chat
            ON messages(chat_id, timestamp)
        """)
        await self._db.commit()
        logger.info("Memory initialized at %s", self.db_path)

    async def close(self):
        if self._db:
            await self._db.close()

    async def add_message(self, chat_id: int, role: str, content: str | None = None,
                          tool_calls: list | None = None, tool_call_id: str | None = None,
                          name: str | None = None, project_name: str = "main"):
        """Store a message in the conversation history."""
        await self._db.execute(
            """INSERT INTO messages (chat_id, project_name, role, content, tool_calls, tool_call_id, name, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, project_name, role, content,
             json.dumps(tool_calls) if tool_calls else None,
             tool_call_id, name, time.time())
        )
        # Update metadata
        await self._db.execute(
            """INSERT INTO metadata (chat_id, last_activity, message_count)
               VALUES (?, ?, 1)
               ON CONFLICT(chat_id) DO UPDATE SET
                   last_activity = excluded.last_activity,
                   message_count = message_count + 1""",
            (chat_id, time.time())
        )
        await self._db.commit()

    async def get_history(self, chat_id: int, project_name: str = "main", limit: int = MAX_CONTEXT_MESSAGES) -> list[dict]:
        """Retrieve recent messages for a chat as OpenAI-format dicts."""
        cursor = await self._db.execute(
            """SELECT role, content, tool_calls, tool_call_id, name
               FROM messages
               WHERE chat_id = ? AND project_name = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (chat_id, project_name, limit)
        )
        rows = await cursor.fetchall()
        messages = []
        for role, content, tool_calls_json, tool_call_id, name in reversed(rows):
            msg = {"role": role}
            if content is not None:
                msg["content"] = content
            if tool_calls_json:
                msg["tool_calls"] = json.loads(tool_calls_json)
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if name:
                msg["name"] = name
            messages.append(msg)
        return messages

    async def clear_history(self, chat_id: int, project_name: str = "main"):
        """Clear all messages for a specific project in a chat."""
        await self._db.execute("DELETE FROM messages WHERE chat_id = ? AND project_name = ?", (chat_id, project_name))
        await self._db.commit()
        logger.info("Cleared history for chat %d project %s", chat_id, project_name)

    async def get_projects(self, chat_id: int) -> list[str]:
        """Get all active project names for a chat."""
        cursor = await self._db.execute("SELECT DISTINCT project_name FROM messages WHERE chat_id = ?", (chat_id,))
        rows = await cursor.fetchall()
        projects = [r[0] for r in rows if r[0]]
        return projects if projects else ["main"]

    async def get_last_activity(self, chat_id: int) -> float | None:
        """Get timestamp of last activity for a chat."""
        cursor = await self._db.execute(
            "SELECT last_activity FROM metadata WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def get_all_chat_ids(self) -> list[int]:
        """Return all known chat IDs."""
        cursor = await self._db.execute("SELECT chat_id FROM metadata")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_global_last_activity(self) -> float | None:
        """Get the most recent activity across all chats."""
        cursor = await self._db.execute(
            "SELECT MAX(last_activity) FROM metadata"
        )
        row = await cursor.fetchone()
        return row[0] if row else None
