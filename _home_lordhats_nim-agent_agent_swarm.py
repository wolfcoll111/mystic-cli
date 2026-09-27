"""
Multi-Agent Swarm Orchestrator.
Coordinates Overseer (Llama 3), Architect (Kimi K2.5), Coder (Qwen 3.6 Plus), and Verifier (Minimax M2.5).
"""

import asyncio
import logging
import json
import re
import inspect
from contextlib import suppress
from pathlib import Path

from agent.config import Config
from agent.nim_client import NIMClient

logger = logging.getLogger("nim_agent.swarm")

class SwarmOrchestrator:
    # Default friendly display names for each swarm role
    ROLE_NAMES = {
        "meta/llama3-70b-instruct": "Llama 3 (Overseer)",
        "deepseek-ai/deepseek-v4-pro": "DeepSeek V4 Pro (Architect)",
        "z-ai/glm-5.1": "GLM 5.1 (Coder/Architect)",
        "minimaxai/minimax-m2.7": "Minimax M2.7 (Verifier)",
    }

    def __init__(self, config: Config, discord_notify_callback=None):
        self.config = config
        self.notify = discord_notify_callback
        
        # Load custom models config if it exists
        models_config_path = self.config.data_dir / "models_config.json"
        
        # Default models configuration
        self.models_config = {
            "overseer": "meta/llama3-70b-instruct",
            "architect": "z-ai/glm-5.1",
            "coder": "z-ai/glm-5.1",
            "verifier": "minimaxai/minimax-m2.7"
        }
        
        if models_config_path.exists():
            try:
                loaded = json.loads(models_config_path.read_text(encoding="utf-8"))
                self.models_config.update(loaded)
            except Exception as e:
                logger.error("Failed to load models_config.json: %s", e)
        else:
            # Save defaults
            try:
                models_config_path.write_text(json.dumps(self.models_config, indent=4), encoding="utf-8")
            except Exception as e:
                logger.error("Failed to write default models_config.json: %s", e)
        
        # Overseer
        self.overseer = NIMClient(
            api_key=config.nvidia_api_key,
            base_url=config.nim_base_url,
            model=self.models_config["overseer"], 
        )

        # Architect
        self.architect = NIMClient(
            api_key=config.nvidia_api_key,
            base_url=config.nim_base_url,
            model=self.models_config["architect"],
        )
        
        # Coder
        self.coder = NIMClient(
            api_key=config.nvidia_api_key,
            base_url=config.nim_base_url,
            model=self.models_config["coder"], 
        )
        
        # Verifier
        self.verifier = NIMClient(
            api_key=config.nvidia_api_key,
            base_url=config.nim_base_url,
            model=self.models_config["verifier"], 
        )

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

    def _display_name(self, client: NIMClient) -> str:
        """Get a human-friendly display name for a model."""
        return self.ROLE_NAMES.get(client.model, client.model)

    async def _robust_chat(self, client: NIMClient, prompt: str, chat_id: int) -> str:
        """Call LLM with retries, heartbeat updates, and a 3-minute timeout window per attempt."""
        max_attempts = 3
        total_timeout = 180.0   # 3 minutes total per attempt
        heartbeat = 20.0        # send progress every 20s
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
                            f"⏳ Waiting on **{display}**... {int(elapsed)}s elapsed (attempt {attempt}/{max_attempts})"
                        )

            except Exception as e:
                if not task.done():
                    task.cancel()
                    with suppress(Exception):
                        await task

                err_str = str(e).lower()
                repr_str = repr(e)

                if "404" in err_str or "not found" in err_str or "does not exist" in err_str:
                    logger.error("Fatal 404 API exception on %s: %s", client.model, e)
                    await self._send_update(chat_id, f"❌ Model error: **{display}** not found or blocked by privacy settings.")
                    raise ValueError(f"Model 404 Not Found: {display}")

                if "401" in err_str or "unauthorized" in err_str or "api_key" in err_str or "api key" in err_str:
                    logger.error("Fatal Auth exception on %s: %s", client.model, e)
                    await self._send_update(chat_id, f"❌ Auth error: **{display}**. Check API key.")
                    raise ValueError(f"Auth Error: {display}")

                logger.exception("Robust chat exception on %s (Attempt %s/%s)", client.model, attempt, max_attempts)

                if attempt == max_attempts:
                    await self._send_update(
                        chat_id,
                        f"❌ **{display}** failed permanently after {max_attempts} attempts: {repr_str}"
                    )
                    raise ValueError(f"Max retries exceeded for: {display}")

                await self._send_update(
                    chat_id,
                    f"⚠️ **{display}** failed on attempt {attempt}: {repr_str}. Retrying in 5s..."
                )
                await asyncio.sleep(5)

    # ──────────────────────────────────────────────
    # Query helpers (used by bot commands)
    # ──────────────────────────────────────────────

    def _workspace_path(self, project_name: str) -> Path:
        """Return the resolved workspace path for a project."""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name).strip("._") or "project"
        root = Path(getattr(self.config, "workspace_dir", "/data/workspace")).expanduser()
        return root / safe

    def get_checkpoint(self, project_name: str) -> dict | None:
        """Read checkpoint data for a project, or None if missing."""
        cp = self._workspace_path(project_name) / ".swarm_checkpoint.json"
        if not cp.exists():
            return None
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_project_files(self, project_name: str) -> list[dict]:
        """List all files in a project workspace with sizes."""
        ws = self._workspace_path(project_name)
        if not ws.exists():
            return []
        results = []
        for f in sorted(ws.iterdir()):
            if f.name.startswith("."):
                continue
            results.append({
                "name": f.name,
                "size": f.stat().st_size if f.is_file() else 0,
                "is_dir": f.is_dir(),
            })
        return results

    def read_project_file(self, project_name: str, filename: str) -> str | None:
        """Read a file from the project workspace."""
        ws = self._workspace_path(project_name)
        fp = ws / Path(filename).name  # sanitize
        if not fp.exists() or not fp.is_file():
            return None
        try:
            return fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

    def reset_checkpoint(self, project_name: str) -> bool:
        """Delete checkpoint so next swarm run starts fresh."""
        cp = self._workspace_path(project_name) / ".swarm_checkpoint.json"
        if cp.exists():
            cp.unlink()
            return True
        return False

    def get_workspace_disk_usage(self, project_name: str) -> int:
        """Total bytes used by workspace folder."""
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
                logger.info("Swarm cancelled explicitly by user.")
                raise
            except Exception as e:
                restart_count += 1
                logger.error("Swarm critical crash: %s", e, exc_info=True)
                await self._send_update(chat_id, f"❌ **Swarm Critical Crash!**\n`{type(e).__name__}: {e}`\n\n🔄 **Auto-restart triggered!** (Restart #{restart_count}). The swarm will rehydrate from the last checkpoint in 30 seconds to prevent silent hangs...")
                await asyncio.sleep(30)

    async def _run_pipeline_impl(self, chat_id: int, project_name: str, prompt: str):
        await self._send_update(chat_id, f"🐝 Swarm started for project: {project_name}")

        safe_project_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_name).strip("._") or "project"
        workspace_root = Path(getattr(self.config, "workspace_dir", "/data/workspace")).expanduser()
        workspace_path = workspace_root / safe_project_name

        try:
            workspace_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("Workspace creation failed")
            await self._send_update(chat_id, f"❌ Workspace creation failed: {workspace_path}")
            raise

        checkpoint_file = workspace_path / ".swarm_checkpoint.json"
        
        # 0. Checkpoint Rehydration
        plan = []
        completed_modules = []
        
        if checkpoint_file.exists():
            try:
                data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                plan = data.get("plan", [])
                completed_modules = data.get("completed_modules", [])
                
                if len(plan) > 0 and len(completed_modules) < len(plan):
                    await self._send_update(chat_id, f"🧠 **{self._display_name(self.overseer)}**: I found a previous save state! We are resuming the project `{project_name}` with {len(completed_modules)}/{len(plan)} modules completed.")
                elif len(plan) > 0 and len(completed_modules) == len(plan):
                    # Fully completed already, restart
                    plan = []
                    completed_modules = []
            except Exception as e:
                logger.warning("Checkpoint load failed: %s", e)
        
        # 1. Architect Phase (if no plan exists)
        if not plan:
            await self._send_update(chat_id, f"👋 **Greetings! I am {self._display_name(self.architect)}.** My purpose right now is to serve as your Lead Game Architect. I will analyze your request and break it down into a detailed roadmap of distinct modules and scripts. Let's design this!\n\n*(Starting architect design...)*")
            
            architect_prompt = (
                f"You are the Lead Game Architect building a Roblox game using Luau.\n"
                f"User request: {prompt}\n\n"
                f"Break this game design down into distinct scripts and components required to build it from scratch. "
                f"Respond ONLY with a valid JSON array of objects. Do not include markdown code ticks around the JSON. "
                f"Format example:\n"
                f"[{{\"filename\": \"Core.luau\", \"description\": \"Core game loop logic\"}}, "
                f"{{\"filename\": \"Weapon.luau\", \"description\": \"Gun shooting mechanics\"}}]"
            )
            
            arch_response = await self._robust_chat(self.architect, architect_prompt, chat_id)
            
            # Remove any <think>...</think> reasoning blocks cleanly
            cleaned_response = re.sub(r'<think>.*?</think>', '', arch_response, flags=re.DOTALL).strip()

            # Try to extract JSON if it was wrapped in markdown
            # Using non-greedy match .*? to avoid spanning across multiple independent brackets, 
            # but usually the plan is a single top-level array.
            js_match = re.search(r'\[.*\]', cleaned_response, re.DOTALL)
            if js_match:
                plan_text = js_match.group(0)
            else:
                plan_text = cleaned_response
                
            try:
                plan = json.loads(plan_text)
                if not isinstance(plan, list):
                    plan = [{"filename": "Game.luau", "description": prompt}]
            except Exception as e:
                plan = [{"filename": "GameLogic.luau", "description": prompt}]
            
            # Save initialized checkpoint
            checkpoint_file.write_text(json.dumps({"plan": plan, "completed_modules": completed_modules}), encoding="utf-8")
            
            # GLM-5 Checks in
            glm_prompt = f"React briefly with excitement to this Roblox game plan consisting of {len(plan)} files. Inform the user we are assigning the coding to {self._display_name(self.coder)}."
            overseer_msg = await self._robust_chat(self.overseer, glm_prompt, chat_id)
            await self._send_update(chat_id, f"🧠 **{self._display_name(self.overseer)}**:\n\n{overseer_msg}")

        # 2. Iterative Module Construction
        for i, module in enumerate(plan):
            filename = module.get("filename", "unknown.luau")
            desc = module.get("description", "")
            
            # Skip if already in checkpoint
            if filename in completed_modules:
                continue

            safe_file = Path(filename).name
            file_path = workspace_path / safe_file
            
            await self._send_update(chat_id, f"👋 **Hello! I am {self._display_name(self.coder)}.** My purpose right now is to act as your dedicated Coder. I will write the complete, functional Luau script for `{safe_file}` exactly as the Architect instructed. Time to write some code!\n\n*(Progress {len(completed_modules)}/{len(plan)} — coding `{safe_file}`...)*")
            
            coder_prompt = (
                f"You are an expert Roblox Luau Coder within a Multi-Agent Swarm.\n"
                f"Write the complete script for: {safe_file}\n"
                f"Component Description: {desc}\n\n"
                f"Requirements: Output ONLY the raw Luau code inside triple backticks (```luau ... ```). "
                f"Do NOT provide explanations or pleasantries."
            )
            
            max_attempts = 3
            final_code = ""
            for attempt in range(max_attempts):
                code_response = await self._robust_chat(self.coder, coder_prompt, chat_id)
                
                # Extract code
                code_blocks = re.findall(r'```(?:luau|lua)?\n([\s\S]*?)```', code_response, re.IGNORECASE)
                if code_blocks:
                    final_code = code_blocks[0].strip()
                else:
                    final_code = code_response.replace("```", "").strip()

                # Save immediately after coding so verifier crashes don't lose work
                if final_code:
                    file_path.write_text(final_code, encoding="utf-8")

                # Attempt verification (non-fatal — if verifier dies, we keep the code)
                try:
                    await self._send_update(chat_id, f"👋 **Hi there! I am {self._display_name(self.verifier)}.** My purpose right now is to serve as your QA Verifier. I am rigorously reviewing `{safe_file}` to ensure the Coder made no mistakes, hallucinates no invalid Roblox APIs, and completely fulfills the Architect's requirements.\n\n*(Reviewing `{safe_file}` - Attempt {attempt+1}/{max_attempts}...)*")
                    
                    verifier_prompt = (
                        f"You are an expert Roblox Quality Assurance Verifier.\n"
                        f"Review this Luau code for '{safe_file}' (Intended design: {desc}).\n"
                        f"Check for:\n1. Missing requirements\n2. Syntax errors\n3. Invalid Roblox API hallucination\n\n"
                        f"Code to review:\n```luau\n{final_code}\n```\n\n"
                        f"If the code is FLAWLESS and ready to deploy to Roblox Studio, respond with the exact word: VERIFIED\n"
                        f"If there are errors, describe the exact errors and how to fix them."
                    )
                    
                    ver_response = await self._robust_chat(self.verifier, verifier_prompt, chat_id)
                    ver_upper = ver_response.upper().strip()

                    # Accept if verifier explicitly says VERIFIED anywhere in the response
                    is_verified = "VERIFIED" in ver_upper

                    # Also accept clearly positive responses that don't use the magic word
                    _positive_signals = ("LOOKS GOOD", "NO ISSUES", "NO ERRORS", "WELL WRITTEN",
                                         "READY TO DEPLOY", "CODE IS CORRECT", "FLAWLESS",
                                         "NO PROBLEMS", "PASSES ALL CHECKS", "LGTM", "APPROVED")
                    is_positive = any(sig in ver_upper for sig in _positive_signals)

                    # Detect responses that contain real, actionable error feedback
                    _error_signals = ("ERROR", "BUG", "MISSING", "INCORRECT", "INVALID",
                                      "UNDEFINED", "FIX", "SHOULD BE", "REPLACE", "WRONG")
                    has_real_errors = any(sig in ver_upper for sig in _error_signals) and not is_verified

                    if is_verified or (is_positive and not has_real_errors):
                        await self._send_update(chat_id, f"✅ **{self._display_name(self.verifier)}:** Excellent work! `{safe_file}` is VERIFIED and flawless.")
                        break
                    elif has_real_errors and attempt < max_attempts - 1:
                        error_summary = ver_response if len(ver_response) <= 500 else ver_response[:500] + "\n...[truncated]"
                        await self._send_update(chat_id, f"⚠️ **{self._display_name(self.verifier)}** flagged issues in `{safe_file}`! Here is a summary of why it's wrong:\n```text\n{error_summary}\n```\nPassing back to **{self._display_name(self.coder)}** for a fix...")
                        coder_prompt += (
                            f"\n\nThe Verifier rejected your previous code with this feedback:\n"
                            f"{ver_response}\n\n"
                            f"Please fix the errors and output the full corrected code."
                        )
                    else:
                        # Ambiguous or final attempt — accept the code and move on
                        if attempt >= max_attempts - 1:
                            await self._send_update(chat_id, f"⚠️ **{self._display_name(self.verifier)}** couldn't perfect `{safe_file}` in {max_attempts} rounds. Saving current code.")
                        else:
                            # Verifier gave an ambiguous response with no clear errors — accept it
                            await self._send_update(chat_id, f"✅ `{safe_file}` accepted (verifier response was ambiguous, no clear errors detected).")
                        break
                except Exception as ver_err:
                    logger.warning("Verifier failed for %s: %s — keeping coder output", safe_file, ver_err)
                    await self._send_update(chat_id, f"⚠️ **{self._display_name(self.verifier)}** crashed! Keeping **{self._display_name(self.coder)}**'s code for `{safe_file}` and moving on.")
                    break
            
            # Update and Write Checkpoint
            completed_modules.append(filename)
            checkpoint_file.write_text(json.dumps({"plan": plan, "completed_modules": completed_modules}), encoding="utf-8")
            
        await self._send_update(chat_id, f"🧠 **{self._display_name(self.overseer)}**:\n\n🎉 **Swarm Complete!**\nAll {len(plan)} scripts have been generated, verified, and saved to your project!")
