"""
File Manager Skill — read, write, list, and delete files in the workspace.
"""

import asyncio
import os
from pathlib import Path
from mystic.skills.base import Skill
from mystic._store import safe_join

# Workspace root resolved lazily (respects Config/WORKSPACE_DIR, not hardcoded import-time env)
_DEF_WORKSPACE = Path(os.getenv("WORKSPACE_DIR", "/data/workspace"))


class FileManagerSkill(Skill):
    name = "file_manager"
    description = (
        "Manage files in the workspace. You can read, write, list, and delete files. "
        "All paths are relative to the workspace directory (/data/workspace/)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write", "list", "delete", "exists"],
                "description": "The file operation to perform"
            },
            "path": {
                "type": "string",
                "description": "Relative file path within the workspace (e.g., 'notes/todo.txt')"
            },
            "content": {
                "type": "string",
                "description": "Content to write (only for 'write' action)"
            }
        },
        "required": ["action", "path"]
    }

    def _workspace(self) -> Path:
        # Re-read env at call time so Config.load() ordering works
        return Path(os.getenv("WORKSPACE_DIR", str(_DEF_WORKSPACE)))

    def _resolve_path(self, rel_path: str, project_name: str) -> Path:
        """Resolve safely: blocks .., absolute escapes, and symlink TOCTOU."""
        project_sandbox = self._workspace() / Path(project_name).name
        project_sandbox.mkdir(parents=True, exist_ok=True)
        if not rel_path or rel_path == ".":
            return project_sandbox.resolve()
        full_path = safe_join(project_sandbox, rel_path)
        return full_path

    async def execute(self, **kwargs) -> str:
        ctx = kwargs.pop("_context", {})
        project_name = ctx.get("project_name", "main")
        action = kwargs.get("action", "")
        rel_path = kwargs.get("path", "")
        content = kwargs.get("content", "")

        if not action:
            return "Error: No action specified."

        try:
            if action == "read":
                return await self._read(rel_path, project_name)
            elif action == "write":
                return await self._write(rel_path, content, project_name)
            elif action == "list":
                return await self._list(rel_path, project_name)
            elif action == "delete":
                return await self._delete(rel_path, project_name)
            elif action == "exists":
                path = self._resolve_path(rel_path, project_name)
                return f"{'exists' if path.exists() else 'not found'}: {rel_path}"
            else:
                return f"Error: Unknown action '{action}'"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    async def _read(self, rel_path: str, project_name: str) -> str:
        path = self._resolve_path(rel_path, project_name)
        if not path.exists():
            return f"Error: File not found: {rel_path}"
        if not path.is_file():
            return f"Error: Not a file: {rel_path}"

        content = await asyncio.to_thread(path.read_text, "utf-8", "replace")
        if len(content) > 10000:
            content = content[:9800] + f"\n\n... (truncated, {len(content)} chars total)"
        return content

    async def _write(self, rel_path: str, content: str, project_name: str) -> str:
        path = self._resolve_path(rel_path, project_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {rel_path}"

    async def _list(self, rel_path: str, project_name: str) -> str:
        project_sandbox = self._workspace() / Path(project_name).name
        path = self._resolve_path(rel_path, project_name) if rel_path and rel_path != "." else project_sandbox
        if not path.exists():
            return f"Error: Directory not found: {rel_path}"
        if not path.is_dir():
            return f"Error: Not a directory: {rel_path}"

        items = []
        for child in sorted(path.iterdir()):
            prefix = "📁" if child.is_dir() else "📄"
            size = f" ({child.stat().st_size} bytes)" if child.is_file() else ""
            rel = child.relative_to(project_sandbox)
            items.append(f"{prefix} {rel}{size}")

        if not items:
            return f"(empty directory: {rel_path})"
        return "\n".join(items[:100])  # Cap at 100 entries

    async def _delete(self, rel_path: str, project_name: str) -> str:
        path = self._resolve_path(rel_path, project_name)
        if not path.exists():
            return f"Error: Not found: {rel_path}"
        if path.is_file():
            path.unlink()
            return f"Deleted file: {rel_path}"
        else:
            import shutil
            shutil.rmtree(path)
            return f"Deleted directory: {rel_path}"
