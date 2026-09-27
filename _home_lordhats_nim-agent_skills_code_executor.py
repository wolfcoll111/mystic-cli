"""
Code Executor Skill — runs Python or Bash code in a subprocess.
"""

import asyncio
import tempfile
import os
from pathlib import Path
from skills.base import Skill


class CodeExecutorSkill(Skill):
    name = "execute_code"
    description = (
        "Execute Python or Bash code on the system and return the output. "
        "Use this for calculations, data processing, installing packages, "
        "running system commands, or any task that requires code execution."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The code to execute"
            },
            "language": {
                "type": "string",
                "enum": ["python", "bash"],
                "description": "Programming language: 'python' or 'bash'"
            }
        },
        "required": ["code", "language"]
    }

    async def execute(self, **kwargs) -> str:
        ctx = kwargs.pop("_context", {})
        project_name = ctx.get("project_name", "main")
        
        cwd_path = Path("/data/workspace") / project_name
        cwd_path.mkdir(parents=True, exist_ok=True)
        
        code = kwargs.get("code", "")
        language = kwargs.get("language", "python")
        timeout = int(os.getenv("CODE_TIMEOUT", "30"))

        if not code.strip():
            return "Error: No code provided."

        try:
            if language == "python":
                result = await self._run_python(code, timeout, cwd_path.as_posix())
            elif language == "bash":
                result = await self._run_bash(code, timeout, cwd_path.as_posix())
            else:
                return f"Error: Unsupported language '{language}'. Use 'python' or 'bash'."

            return result

        except asyncio.TimeoutError:
            return f"Error: Code execution timed out after {timeout}s."
        except Exception as e:
            return f"Error executing code: {type(e).__name__}: {e}"

    async def _run_python(self, code: str, timeout: int, cwd: str) -> str:
        """Run Python code via subprocess."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            output = ""
            if stdout:
                output += stdout.decode(errors="replace")
            if stderr:
                output += "\n[STDERR]\n" + stderr.decode(errors="replace")
            if proc.returncode != 0:
                output += f"\n[Exit code: {proc.returncode}]"

            return output.strip() or "(no output)"

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _run_bash(self, code: str, timeout: int, cwd: str) -> str:
        """Run Bash commands via subprocess."""
        proc = await asyncio.create_subprocess_shell(
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        output = ""
        if stdout:
            output += stdout.decode(errors="replace")
        if stderr:
            output += "\n[STDERR]\n" + stderr.decode(errors="replace")
        if proc.returncode != 0:
            output += f"\n[Exit code: {proc.returncode}]"

        # Truncate long output
        if len(output) > 4000:
            output = output[:3900] + f"\n\n... (truncated, {len(output)} chars total)"

        return output.strip() or "(no output)"
