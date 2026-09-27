"""
Multi-Agent Swarm Orchestrator.
Coordinates Overseer, Architect, Coder, and Verifier using dynamic provider config.
"""

import asyncio
import logging
import json
import re
import inspect
from contextlib import suppress
from pathlib import Path

from agent.config import Config
from agent.config_manager import ConfigManager

logger = logging.getLogger("nim_agent.swarm")


class SwarmOrchestrator:
    ROLE_NAMES = {
        "meta/llama3-70b-instruct": "Llama 3 (Overseer)",
        "deepseek-ai/deepseek-v4-pro": "DeepSeek V4 Pro (Architect)",
        "z-ai/glm-5.1": "GLM 5.1 (Coder/Architect)",
        "minimaxai/minimax-m2.7": "Minimax M2.7 (Verifier)",
    }

    def __init__(self, config: Config, discord_notify_callback=None):
        self.config = config
        self.notify = discord_notify_callback
        self.cfg_mgr = ConfigManager(config)

        self.overseer = self.cfg_mgr.create_client_for_role("overseer")
        self.architect = self.cfg_mgr.create_client_for_role("architect")
        self.coder = self.cfg_mgr.create_client_for_role("coder")
        self.verifier = self.cfg_mgr.create_client_for_role("verifier")

    def reload_config(self):
        """Reload all models and providers from disk and recreate clients."""
        self.cfg_mgr.apply_to_swarm(self)
        logger.info("Swarm config reloaded")

    async def _send_update(self, chat_id: int, msg: str):
        logger.info("Swarm update [%s]: %s", chat_id, msg)
        if not self.notify:
            return
        try:
            result = self.notify(chat_id, msg)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Swarm notify error")

    def _display_name(self, client) -> str:
        return self.ROLE_NAMES.get(client.model, client.model)

    async def _robust_chat(self, client, prompt: str, chat_id: int) -> str:
        max_attempts = 3
        total_timeout = 180.0
        heartbeat = 20.0
        display = self._display_name(client)

        for attempt in range(1, max_attempts + 1):
            task = asyncio.create_task(client.simple_chat(prompt))
            elapsed = 0.0

            try:
                while True:
                    try:
                        result = await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat)
                        if not isinstance(result, str) or not result.strip():
                            raise ValueError(f"Empty response from {display}")
                        return result
                    except asyncio.TimeoutError:
                        elapsed += heartbeat
                        if elapsed >= total_timeout:
                            task.cancel()
                            with suppress(Exception):
                                await task
                            raise TimeoutError(f"{display} timed out after {int(total_timeout)}s")
                        await self._send_update(
                            chat_id,
                            f"[WAIT] Waiting on **{display}**... {int(elapsed)}s (attempt {attempt}/{max_attempts})"
                        )
            except Exception as e:
                if not task.done():
                    task.cancel()
                    with suppress(Exception):
                        await task
                err_str = str(e).lower()
                repr_str = repr(e)
                if "404" in err_str or "not found" in err_str or "does not exist" in err_str:
                    logger.error("Fatal 404 on %s: %s", client.model, e)
                    await self._send_update(chat_id, f"[FAIL] Model error: **{display}** not found.")
                    raise ValueError(f"Model 404 Not Found: {display}")
                if "401" in err_str or "unauthorized" in err_str or "api_key" in err_str or "api key" in err_str:
                    logger.error("Fatal Auth on %s: %s", client.model, e)
                    await self._send_update(chat_id, f"[FAIL] Auth error: **{display}**. Check API key.")
                    raise ValueError(f"Auth Error: {display}")
                logger.exception("Robust chat exception on %s (Attempt %s/%s)", client.model, attempt, max_attempts)
                if attempt == max_attempts:
                    await self._send_update(chat_id, f"[FAIL] **{display}** failed after {max_attempts} attempts: {repr_str}")
                    raise ValueError(f"Max retries: {display}")
                await self._send_update(chat_id, f"[RETRY] **{display}** failed attempt {attempt}: {repr_str}. Retrying...")
                await asyncio.sleep(5)

    def _workspace_path(self, project_name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name).strip("._") or "project"
        root = Path(getattr(self.config, "workspace_dir", "/data/workspace")).expanduser()
        return root / safe

    def get_checkpoint(self, project_name: str) -> dict | None:
        cp = self._workspace_path(project_name) / ".swarm_checkpoint.json"
        if not cp.exists():
            return None
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_project_files(self, project_name: str) -> list[dict]:
        ws = self._workspace_path(project_name)
        if not ws.exists():
            return []
        results = []
        for f in sorted(ws.iterdir()):
            if f.name.startswith("."):
                continue
            results.append({"name": f.name, "size": f.stat().st_size if f.is_file() else 0, "is_dir": f.is_dir()})
        return results

    def read_project_file(self, project_name: str, filename: str) -> str | None:
        ws = self._workspace_path(project_name)
        fp = ws / Path(filename).name
        if not fp.exists() or not fp.is_file():
            return None
        try:
            return fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    def reset_checkpoint(self, project_name: str) -> bool:
        cp = self._workspace_path(project_name) / ".swarm_checkpoint.json"
        if cp.exists():
            cp.unlink()
            return True
        return False

    def get_workspace_disk_usage(self, project_name: str) -> int:
        ws = self._workspace_path(project_name)
        if not ws.exists():
            return 0
        return sum(f.stat().st_size for f in ws.rglob("*") if f.is_file())

    async def run_pipeline(self, chat_id: int, project_name: str, prompt: str):
        restart_count = 0
        while True:
            try:
                await self._run_pipeline_impl(chat_id, project_name, prompt)
                break
            except asyncio.CancelledError:
                logger.info("Swarm cancelled.")
                raise
            except Exception as e:
                restart_count += 1
                logger.error("Swarm crash: %s", e, exc_info=True)
                await self._send_update(chat_id, f"[CRASH] **Swarm crashed!** `{e}`. Auto-restart #{restart_count} in 30s...")
                await asyncio.sleep(30)

    async def _run_pipeline_impl(self, chat_id: int, project_name: str, prompt: str):
        await self._send_update(chat_id, f"[SWARM] Swarm started for project: {project_name}")
        safe_pn = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name).strip("._") or "project"
        workspace_root = Path(getattr(self.config, "workspace_dir", "/data/workspace")).expanduser()
        workspace_path = workspace_root / safe_pn
        workspace_path.mkdir(parents=True, exist_ok=True)
        checkpoint_file = workspace_path / ".swarm_checkpoint.json"

        plan = []
        completed = []

        if checkpoint_file.exists():
            try:
                data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                plan = data.get("plan", [])
                completed = data.get("completed_modules", [])
                if plan and len(completed) < len(plan):
                    await self._send_update(chat_id, f"[RESUME] Resuming `{project_name}` ({len(completed)}/{len(plan)})")
                elif plan and len(completed) == len(plan):
                    plan = []
                    completed = []
            except Exception:
                pass

        if not plan:
            await self._send_update(chat_id, f"[ARCHITECT] **{self._display_name(self.architect)}**: Designing architecture...")
            arch_prompt = (
                f"You are a Lead Game Architect building a Roblox game in Luau.\n"
                f"User request: {prompt}\n\n"
                f"Break this into distinct scripts. Respond ONLY with a JSON array:\n"
                f'[{{"filename": "Core.luau", "description": "Core logic"}}, ...]'
            )
            arch_resp = await self._robust_chat(self.architect, arch_prompt, chat_id)
            arch_resp = re.sub(r'<think>.*?</think>', '', arch_resp, flags=re.DOTALL).strip()
            m = re.search(r'\[.*\]', arch_resp, re.DOTALL)
            try:
                plan = json.loads(m.group(0) if m else arch_resp)
                if not isinstance(plan, list):
                    plan = [{"filename": "Game.luau", "description": prompt}]
            except Exception:
                plan = [{"filename": "GameLogic.luau", "description": prompt}]
            checkpoint_file.write_text(json.dumps({"plan": plan, "completed_modules": completed}), encoding="utf-8")
            msg = await self._robust_chat(self.overseer, f"React to this plan with {len(plan)} files.", chat_id)
            await self._send_update(chat_id, f"[OVERSEER] {msg}")

        for i, mod in enumerate(plan):
            fn = mod.get("filename", "unknown.luau")
            desc = mod.get("description", "")
            if fn in completed:
                continue
            sf = Path(fn).name
            fp = workspace_path / sf
            await self._send_update(chat_id, f"[CODER] Coding `{sf}` ({len(completed)}/{len(plan)})...")
            coder_prompt = (
                f"You are a Roblox Luau Coder. Write the complete script for: {sf}\n"
                f"Description: {desc}\n\nOutput ONLY code inside ```luau ... ```"
            )
            final_code = ""
            for attempt in range(3):
                code_resp = await self._robust_chat(self.coder, coder_prompt, chat_id)
                blocks = re.findall(r'```(?:luau|lua)?\n([\s\S]*?)```', code_resp, re.IGNORECASE)
                final_code = blocks[0].strip() if blocks else code_resp.replace("```", "").strip()
                if final_code:
                    fp.write_text(final_code, encoding="utf-8")
                try:
                    ver_prompt = f"Review this Luau for '{sf}'. Code:\n```luau\n{final_code}\n```\nIf flawless, respond: VERIFIED"
                    ver_resp = await self._robust_chat(self.verifier, ver_prompt, chat_id)
                    if "VERIFIED" in ver_resp.upper():
                        await self._send_update(chat_id, f"[VERIFIER] `{sf}` verified!")
                        break
                    elif attempt < 2:
                        await self._send_update(chat_id, f"[VERIFIER] Issues found. Fixing...")
                        coder_prompt += f"\n\nFix errors:\n{ver_resp}"
                    else:
                        await self._send_update(chat_id, f"[VERIFIER] Max attempts. Keeping code for `{sf}`.")
                except Exception:
                    await self._send_update(chat_id, f"[VERIFIER] Crashed. Keeping code for `{sf}`.")
                    break
            completed.append(fn)
            checkpoint_file.write_text(json.dumps({"plan": plan, "completed_modules": completed}), encoding="utf-8")

        await self._send_update(chat_id, f"[DONE] **Swarm Complete!** {len(plan)} files generated for `{project_name}`.")
