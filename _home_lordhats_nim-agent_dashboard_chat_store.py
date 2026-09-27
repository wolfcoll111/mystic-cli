"""
Chat Thread Storage — lightweight JSON-file-based persistence.
Each thread is stored as a separate JSON file in data/dashboard_threads/.
Supports Discord sync mapping and file attachments.
"""

import json
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("nim_agent.chat_store")


class ChatStore:
    """Manages chat threads for the Mystic dashboard."""

    def __init__(self, data_dir: Path):
        self.threads_dir = data_dir / "dashboard_threads"
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir = data_dir / "chat_uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    def _thread_path(self, thread_id: str) -> Path:
        safe = thread_id.replace("/", "").replace("\\", "").replace("..", "")
        return self.threads_dir / f"{safe}.json"

    def create_thread(self, title: str = "New thread",
                      discord_channel_id: int | None = None,
                      agent_chat_id: int | None = None) -> dict:
        """Create a new chat thread and return its metadata."""
        thread_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        if agent_chat_id is None:
            agent_chat_id = hash(thread_id) % 1_000_000 + 900_000

        thread = {
            "id": thread_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "discord_channel_id": discord_channel_id,
            "agent_chat_id": agent_chat_id,
        }
        self._save(thread_id, thread)
        logger.info("Created thread: %s (%s)", thread_id, title)
        return {
            "id": thread_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "discord_channel_id": discord_channel_id,
            "agent_chat_id": agent_chat_id,
        }

    def list_threads(self) -> list[dict]:
        """List all threads sorted by last update (newest first)."""
        threads = []
        for fp in self.threads_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                msg_count = len(data.get("messages", []))
                preview = ""
                if msg_count > 0:
                    last = data["messages"][-1]
                    preview = (last.get("content", "") or "")[:80]
                threads.append({
                    "id": data["id"],
                    "title": data.get("title", "Untitled"),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "message_count": msg_count,
                    "preview": preview,
                    "discord_channel_id": data.get("discord_channel_id"),
                })
            except Exception as e:
                logger.warning("Failed to read thread %s: %s", fp.name, e)
        threads.sort(key=lambda t: t.get("updated_at", ""), reverse=True)
        return threads

    def get_thread(self, thread_id: str) -> dict | None:
        """Get full thread data including messages."""
        fp = self._thread_path(thread_id)
        if not fp.exists():
            return None
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            # Ensure new fields exist for older threads
            data.setdefault("discord_channel_id", None)
            data.setdefault("agent_chat_id", hash(thread_id) % 1_000_000 + 900_000)
            return data
        except Exception as e:
            logger.error("Failed to read thread %s: %s", thread_id, e)
            return None

    def add_message(self, thread_id: str, role: str, content: str,
                    attachments: list[dict] | None = None,
                    source: str = "dashboard",
                    trace: list[dict] | None = None) -> dict | None:
        """Add a message to a thread. Returns the message dict."""
        thread = self.get_thread(thread_id)
        if thread is None:
            return None

        now = datetime.now(timezone.utc).isoformat()
        message = {
            "id": str(uuid.uuid4())[:8],
            "role": role,
            "content": content,
            "timestamp": now,
            "source": source,
        }
        if attachments:
            message["attachments"] = attachments
        if trace:
            message["trace"] = trace

        thread["messages"].append(message)
        thread["updated_at"] = now

        # Auto-title from first user message
        if thread["title"] == "New thread" and role == "user":
            thread["title"] = content[:50] + ("..." if len(content) > 50 else "")

        self._save(thread_id, thread)
        return message

    def set_discord_channel(self, thread_id: str, discord_channel_id: int):
        """Link a dashboard thread to a Discord channel/thread."""
        thread = self.get_thread(thread_id)
        if thread is None:
            return
        thread["discord_channel_id"] = discord_channel_id
        self._save(thread_id, thread)
        logger.info("Linked thread %s → Discord channel %d", thread_id, discord_channel_id)

    def find_thread_by_discord_channel(self, discord_channel_id: int) -> dict | None:
        """Find a dashboard thread linked to a specific Discord channel."""
        for fp in self.threads_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                if data.get("discord_channel_id") == discord_channel_id:
                    data.setdefault("agent_chat_id",
                                    hash(data["id"]) % 1_000_000 + 900_000)
                    return data
            except Exception:
                continue
        return None

    def delete_thread(self, thread_id: str) -> bool:
        """Delete a thread."""
        fp = self._thread_path(thread_id)
        if fp.exists():
            fp.unlink()
            logger.info("Deleted thread: %s", thread_id)
            return True
        return False

    def _save(self, thread_id: str, data: dict):
        fp = self._thread_path(thread_id)
        fp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
