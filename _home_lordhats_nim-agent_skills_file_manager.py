"""
File Manager Skill — read, write, list, and delete files in the workspace.
"""

import os
from pathlib import Path
from skills.base import Skill

# Workspace root — all file ops are sandboxed to this directory
WORKSPACE = Path(os.getenv("WORKSPACE_DIR", "/data/workspace"))


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

    def _resolve_path(self, rel_path: str, project_name: str) -> Path:
        """Resolve a relative path, ensuring it stays strictly within the project's sandbox."""
        project_sandbox = WORKSPACE / project_name
        project_sandbox.mkdir(parents=True, exist_ok=True)
        # Normalize and prevent directory traversal
        clean = Path(rel_path).as_posix().replace("..", "").lstrip("/")
        full_path = project_sandbox / clean
        # Verify it's still under the project sandbox
        try:
            full_path.resolve().relative_to(project_sandbox.resolve())
        except ValueError:
            raise ValueError(f"Path '{rel_path}' is outside your sandboxed project '{project_name}'")
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

        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > 10000:
            content = content[:9800] + f"\n\n... (truncated, {len(content)} chars total)"
        return content

    async def _write(self, rel_path: str, content: str, project_name: str) -> str:
        path = self._resolve_path(rel_path, project_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {rel_path}"

    async def _list(self, rel_path: str, project_name: str) -> str:
        project_sandbox = WORKSPACE / project_name
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
