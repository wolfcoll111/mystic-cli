"""
Agent Core — the brain of NIM.
Manages the conversation loop: user message → system prompt → NIM API → tool calls → response.
Also runs the self-improvement cycle.
"""

import json
import time
import logging
import asyncio
from datetime import datetime
from pathlib import Path

from agent.config import Config
from agent.nim_client import NIMClient
from agent.memory import Memory
from agent.master_prompt import build_master_prompt
from agent.mcp_client import MCPManager
from skills.loader import SkillLoader

logger = logging.getLogger("nim_agent.core")


class Agent:
    """The NIM Agent — orchestrates everything."""

    def __init__(self, config: Config):
        self.config = config
        self.nim = NIMClient(
            api_key=config.nvidia_api_key,
            base_url=config.nim_base_url,
            model=config.model_name,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
        self.memory = Memory(config.db_path)
        self.skills = SkillLoader(config.skills_dir, config.custom_skills_dir)
        self.mcp = MCPManager(config.mcp_config_path)
        self._last_task_time: float = time.time()
        self._improvement_task: asyncio.Task | None = None
        self._discord_notify_callback = None  # Set by discord bot

    async def start(self):
        """Initialize all subsystems."""
        await self.memory.initialize()
        self.skills.load_all()
        logger.info("Agent started — %d skills loaded: %s",
                     len(self.skills.skills),
                     [s.name for s in self.skills.skills])
        # Connect enabled MCP servers
        try:
            await self.mcp.connect_all_enabled()
            mcp_tool_count = len(self.mcp.get_all_tools())
            if mcp_tool_count:
                logger.info("MCP: %d tools available from %d servers",
                            mcp_tool_count, len(self.mcp.connections))
        except Exception as e:
            logger.warning("MCP startup error (non-fatal): %s", e)

    async def stop(self):
        """Shut down cleanly."""
        if self._improvement_task and not self._improvement_task.done():
            self._improvement_task.cancel()
        await self.mcp.disconnect_all()
        await self.memory.close()
        logger.info("Agent stopped")

    def set_notify_callback(self, callback):
        """Set the Discord notification callback for self-improvement proposals."""
        self._discord_notify_callback = callback

    def _build_system_prompt(self) -> str:
        """Build the system prompt with current skill descriptions."""
        skill_desc = self.skills.get_skill_descriptions()

        # Load recent improvement summaries if any
        imp_summary = ""
        imp_dir = self.config.improvements_dir
        if imp_dir.exists():
            files = sorted(imp_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[:3]:  # Last 3 improvements
                imp_summary += f"\n- {f.stem}: {f.read_text(encoding='utf-8')[:200]}...\n"

        return build_master_prompt(skill_desc, imp_summary, model_name=self.config.model_name)

    async def process_message(self, chat_id: int, user_message: str,
                              project_name: str = "main",
                              return_trace: bool = False) -> str | dict:
        """
        Process a user message and return the agent's response.
        This is the main entry point for Discord and dashboard messages.

        If return_trace=True, returns {"response": str, "trace": list}
        instead of just the response string.
        """
        self._last_task_time = time.time()
        activity_trace = []  # Collect trace of all agent activity

        # Store user message
        await self.memory.add_message(chat_id, "user", user_message, project_name=project_name)

        # Build context
        history = await self.memory.get_history(chat_id, project_name=project_name)
        system_prompt = self._build_system_prompt()
        messages = [{"role": "system", "content": system_prompt}] + history

        # Get tool definitions — merge built-in skills + MCP tools
        tools = self.skills.get_tool_definitions()
        mcp_tools = self.mcp.get_all_tools()
        if mcp_tools:
            tools = (tools or []) + mcp_tools

        # Agent loop — up to N tool-call iterations
        final_response = ""
        for iteration in range(self.config.max_tool_iterations):
            logger.info("Iteration %d for chat %d", iteration + 1, chat_id)

            response = await self.nim.chat(messages, tools=tools if tools else None)
            choice = response.choices[0]
            message = choice.message

            # Record reasoning if the model included text alongside tool calls
            if message.content and message.tool_calls:
                activity_trace.append({
                    "type": "reasoning",
                    "content": message.content,
                    "iteration": iteration + 1,
                    "timestamp": datetime.now().isoformat(),
                })

            # If the model wants to call tools
            if message.tool_calls:
                # Store assistant message with tool calls
                tool_calls_data = []
                for tc in message.tool_calls:
                    tool_calls_data.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    })

                await self.memory.add_message(
                    chat_id, "assistant", content=message.content,
                    tool_calls=tool_calls_data, project_name=project_name
                )
                messages.append({
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": tool_calls_data,
                })

                # Execute each tool call
                for tc in message.tool_calls:
                    skill_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    logger.info("Executing skill: %s(%s)", skill_name, args)

                    # Record tool call in trace
                    activity_trace.append({
                        "type": "tool_call",
                        "name": skill_name,
                        "arguments": args,
                        "iteration": iteration + 1,
                        "timestamp": datetime.now().isoformat(),
                    })

                    # Route to MCP or built-in skills
                    if skill_name.startswith("mcp_"):
                        result = await self.mcp.call_tool(skill_name, args)
                    else:
                        result = await self.skills.execute(
                            skill_name, _context={"project_name": project_name}, **args
                        )

                    # Record tool result in trace
                    result_preview = result[:500] + "..." if len(result) > 500 else result
                    activity_trace.append({
                        "type": "tool_result",
                        "name": skill_name,
                        "result": result_preview,
                        "iteration": iteration + 1,
                        "timestamp": datetime.now().isoformat(),
                    })

                    # Store tool result
                    await self.memory.add_message(
                        chat_id, "tool", content=result,
                        tool_call_id=tc.id, name=skill_name, project_name=project_name
                    )
                    messages.append({
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tc.id,
                        "name": skill_name,
                    })

                # Continue loop — model will see tool results and decide next action
                continue

            # No tool calls — model gave a final response
            final_response = message.content or "(no response)"
            await self.memory.add_message(chat_id, "assistant", final_response, project_name=project_name)
            break
        else:
            final_response = "⚠️ I hit my tool-call limit for this message. Here's what I have so far — let me know if you want me to continue."

        if return_trace:
            return {"response": final_response, "trace": activity_trace}
        return final_response

    # ──────────────────────────────────────────────
    # Self-Improvement Cycle
    # ──────────────────────────────────────────────

    async def start_improvement_loop(self):
        """Background task: every 6 hours of idle time, propose an improvement."""
        self._improvement_task = asyncio.create_task(self._improvement_loop())

    async def _improvement_loop(self):
        """The actual improvement loop. Runs forever in the background."""
        interval = self.config.self_improve_interval_hours * 3600  # Convert to seconds

        while True:
            try:
                await asyncio.sleep(60)  # Check every minute

                idle_time = time.time() - self._last_task_time
                if idle_time < interval:
                    continue

                logger.info("Idle for %.1f hours — starting self-improvement cycle",
                            idle_time / 3600)

                await self._run_improvement_cycle()

                # Reset timer so we don't immediately re-trigger
                self._last_task_time = time.time()

            except asyncio.CancelledError:
                logger.info("Improvement loop cancelled")
                break
            except Exception as e:
                logger.error("Error in improvement loop: %s", e, exc_info=True)
                await asyncio.sleep(300)  # Wait 5 min on error

    async def _run_improvement_cycle(self):
        """Analyze the system and propose one improvement."""
        # Gather context about current state
        context_parts = []

        # List current skills
        skill_names = [s.name for s in self.skills.skills]
        context_parts.append(f"Current skills: {skill_names}")

        # Check recent errors from log (if log file exists)
        log_file = self.config.data_dir / "nim_agent.log"
        if log_file.exists():
            try:
                lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                error_lines = [l for l in lines[-500:] if "ERROR" in l or "WARNING" in l]
                if error_lines:
                    context_parts.append(f"Recent errors/warnings:\n" + "\n".join(error_lines[-20:]))
            except Exception:
                pass

        # List existing improvements
        existing = []
        for f in self.config.improvements_dir.glob("*.md"):
            existing.append(f.stem)
        if existing:
            context_parts.append(f"Already proposed improvements: {existing}")

        # List custom skills
        custom = list(self.config.custom_skills_dir.glob("*.py"))
        context_parts.append(f"Custom skills installed: {[f.stem for f in custom]}")

        context = "\n\n".join(context_parts)

        # Ask GLM-5 to propose an improvement
        analysis_prompt = f"""You are NIM, an AI agent running on a Raspberry Pi 4. You are in self-improvement mode.

Analyze your current state and propose ONE small, concrete improvement.

Your current state:
{context}

Think about:
- Missing skills that would be useful (e.g., reminder/timer, note-taking, weather, news)
- Performance optimizations (caching, less memory usage)
- Better error handling or recovery
- New integrations (e.g., CasaOS API, network tools)
- Prompt refinements

Write your proposal in this EXACT format:

# [Title of Improvement]

## Problem
[What limitation or issue does this address?]

## Solution
[Exactly what would change — include the code or config diff]

## Benefit
[Why this makes you better at serving your owner]

## Risk
[Any potential downside — be honest]

Keep it SMALL and SAFE. One file change max. Your owner must approve before implementation."""

        try:
            proposal = await self.nim.simple_chat(analysis_prompt)
        except Exception as e:
            logger.error("Failed to generate improvement proposal: %s", e)
            return

        if not proposal or len(proposal.strip()) < 50:
            logger.info("Improvement cycle produced no meaningful proposal")
            return

        # Save proposal to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Extract title from proposal
        title = "improvement"
        for line in proposal.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip().lower().replace(" ", "_")[:40]
                break

        filename = f"{timestamp}_{title}.md"
        filepath = self.config.improvements_dir / filename
        filepath.write_text(proposal, encoding="utf-8")
        logger.info("Saved improvement proposal: %s", filepath)

        # Notify owner via Discord
        if self._discord_notify_callback:
            summary = proposal[:1500]  # Discord message limit consideration
            notification = (
                "🔧 **Self-Improvement Proposal**\n\n"
                f"{summary}\n\n"
                f"📄 Full proposal saved to: `{filename}`\n\n"
                "Reply with ✅ to approve or ❌ to reject."
            )
            try:
                await self._discord_notify_callback(notification)
            except Exception as e:
                logger.error("Failed to send improvement notification: %s", e)
