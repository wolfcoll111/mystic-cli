"""
Discord Bot for NIM Agent.
Handles user messages, commands, and serves as the primary interface.
"""

import logging
import asyncio
import time
import re
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord.ext import commands

from agent.core import Agent
from agent.config import Config
from agent.swarm import SwarmOrchestrator

logger = logging.getLogger("nim_agent.discord")

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _project_name_from_channel(channel) -> str:
    """Derive a safe project name from a text channel."""
    raw = channel.name.lower()
    return re.sub(r'[^a-z0-9_-]', '_', raw).strip('_') or "default_group_project"


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class NIMBot:
    """Discord bot wrapping the NIM Agent."""

    def __init__(self, agent: Agent, config: Config):
        self.agent = agent
        self.config = config
        self._owner_chat_id: int | None = None
        self._active_projects: dict[int, str] = {}
        self._active_tasks: dict[int, asyncio.Task] = {}  # chat_id → task (for cancel)
        self._all_tasks: set[asyncio.Task] = set()
        self._progress_messages: dict[int, discord.Message] = {}  # chat_id → last progress msg (for edit-in-place)
        self.swarm = SwarmOrchestrator(config, self._send_swarm_update)
        self._start_time = time.monotonic()

        # Initialize discord Bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        self.bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)
        self._setup_events()
        self._setup_commands()

        self._bot_task = None

    async def start(self):
        """Build and start the Discord bot in the background."""
        self.agent.set_notify_callback(self._send_improvement_notification)
        logger.info("Discord bot starting...")
        self._bot_task = asyncio.create_task(self.bot.start(self.config.discord_token))
        logger.info("Discord bot started — polling for messages")

    async def stop(self):
        """Gracefully stop the bot."""
        if self.bot:
            await self.bot.close()
        if self._bot_task:
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass

    def _is_allowed(self, user_id: int) -> bool:
        """Check if a user is in the whitelist."""
        if not self.config.allowed_users:
            return True
        return user_id in self.config.allowed_users

    def _resolve_project(self, ctx_or_message) -> str:
        """Resolve the current project name for a channel."""
        channel = ctx_or_message.channel if hasattr(ctx_or_message, 'channel') else ctx_or_message
        chat_id = channel.id
        if isinstance(channel, discord.TextChannel):
            proj = _project_name_from_channel(channel)
            self._active_projects[chat_id] = proj
            return proj
        return self._active_projects.get(chat_id, "main")

    # ──────────────────────────────────────────────
    # Events
    # ──────────────────────────────────────────────

    def _setup_events(self):
        @self.bot.event
        async def on_ready():
            logger.info(f"NIM Agent connected as {self.bot.user}")

        @self.bot.event
        async def on_guild_channel_create(channel):
            """Auto-provision workspace when a new text channel is created."""
            if not isinstance(channel, discord.TextChannel):
                return
            proj = _project_name_from_channel(channel)
            ws = self.swarm._workspace_path(proj)
            ws.mkdir(parents=True, exist_ok=True)
            logger.info("Auto-created workspace for new channel #%s → %s", channel.name, ws)
            try:
                await channel.send(
                    f"📁 **Workspace Ready**\n"
                    f"Project `{proj}` has been auto-provisioned for this channel.\n"
                    f"Use `/swarm <prompt>` to start building, or just chat with me!"
                )
            except Exception:
                pass  # missing perms

        @self.bot.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                return
            if not self._is_allowed(message.author.id):
                if isinstance(message.channel, discord.DMChannel):
                    await message.channel.send("⛔ Unauthorized.")
                return

            self._owner_chat_id = message.author.id

            ctx = await self.bot.get_context(message)
            if ctx.valid:
                await self.bot.invoke(ctx)
                return

            await self._handle_text_message(message)

    # ──────────────────────────────────────────────
    # Commands
    # ──────────────────────────────────────────────

    def _setup_commands(self):

        # ─── /start ──────────────────────────────
        @self.bot.command(name="start")
        async def cmd_start(ctx: commands.Context):
            await ctx.send(
                "🤖 **NIM Agent Online**\n\n"
                "I'm your AI companion running on your Raspberry Pi 4.\n"
                "Powered by a multi-model swarm via NVIDIA NIM + OpenRouter.\n\n"
                "Just send me a message and I'll help you out!\n"
                "Use `/help` to see everything I can do."
            )

        # ─── /help ───────────────────────────────
        @self.bot.command(name="help")
        async def cmd_help(ctx: commands.Context):
            skills_list = "\n".join(
                f"  • `{s.name}` — {s.description[:60]}"
                for s in self.agent.skills.skills
            )

            await ctx.send(
                "📖 **NIM Agent Help**\n\n"
                "**Talk to me** — just send any message!\n\n"
                "**General:**\n"
                "  `/help` — This message\n"
                "  `/status` — System stats (CPU, RAM, disk, temp)\n"
                "  `/ping` — Latency check\n"
                "  `/uptime` — How long the bot has been running\n"
                "  `/models` — Show the swarm model lineup\n"
                "  `/logs [n]` — Show last N log lines (default 20)\n\n"
                "**Swarm (Multi-Agent Coding):**\n"
                "  `/swarm <prompt>` — Launch a coding swarm\n"
                "  `/swarm resume` — Resume an interrupted swarm run\n"
                "  `/swarm status` — Check swarm progress\n"
                "  `/swarm files` — List generated files\n"
                "  `/swarm cat <file>` — View a generated file\n"
                "  `/swarm cancel` — Cancel a running swarm\n"
                "  `/swarm reset` — Wipe checkpoint, start fresh\n"
                "  `/swarm diff` — Show files changed since last checkpoint\n"
                "  `/stopswarm` — Quick alias for `/swarm cancel`\n\n"
                "**Project Management:**\n"
                "  `/project` — Show current project\n"
                "  `/project list` — List all projects\n"
                "  `/project new <name>` — Create/switch project\n"
                "  `/project switch <name>` — Switch project\n"
                "  `/project delete <name>` — Delete project memory\n"
                "  `/project files` — List workspace files\n"
                "  `/project info` — Workspace disk usage + stats\n"
                "  `/project export` — Download workspace as ZIP\n\n"
                "**Utilities:**\n"
                "  `/skills` — List loaded skills\n"
                "  `/install <url>` — Install a custom skill\n"
                "  `/clear` — Clear conversation history\n"
                "  `/backup` — Snapshot current project workspace\n"
                "  `/purge [n]` — Delete last N bot messages\n"
                "  `/improvements` — View self-improvement proposals\n\n"
                f"**Loaded Skills:**\n{skills_list}"
            )

        # ─── /ping ───────────────────────────────
        @self.bot.command(name="ping")
        async def cmd_ping(ctx: commands.Context):
            start = time.monotonic()
            msg = await ctx.send("🏓 Pinging...")
            rtt = (time.monotonic() - start) * 1000
            ws_lat = self.bot.latency * 1000
            await msg.edit(content=f"🏓 **Pong!**\nRoundtrip: `{rtt:.0f}ms`\nWebSocket: `{ws_lat:.0f}ms`")

        # ─── /uptime ─────────────────────────────
        @self.bot.command(name="uptime")
        async def cmd_uptime(ctx: commands.Context):
            elapsed = time.monotonic() - self._start_time
            delta = timedelta(seconds=int(elapsed))
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            mins, secs = divmod(rem, 60)
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            if mins:
                parts.append(f"{mins}m")
            parts.append(f"{secs}s")
            await ctx.send(f"⏱️ **Uptime:** {' '.join(parts)}")

        # ─── /models ─────────────────────────────
        @self.bot.command(name="models")
        async def cmd_models(ctx: commands.Context):
            lines = []
            roster = [
                ("🧠 Overseer", self.swarm.overseer, "Strategic oversight & narration"),
                ("🏗️ Architect", self.swarm.architect, "Game design & module planning"),
                ("💻 Coder", self.swarm.coder, "Luau script generation"),
                ("🔎 Verifier", self.swarm.verifier, "QA review & validation"),
            ]
            for emoji_role, client, job in roster:
                display = self.swarm._display_name(client)
                lines.append(f"{emoji_role}\n  Model: `{client.model}`\n  Name: **{display}**\n  Job: {job}")

            main_model = self.config.model_name
            lines.insert(0, f"💬 **Chat Agent** (main)\n  Model: `{main_model}`\n  Base: `{self.config.nim_base_url}`\n")

            await ctx.send("🤖 **Model Lineup**\n\n" + "\n\n".join(lines))

        # ─── /logs ───────────────────────────────
        @self.bot.command(name="logs")
        async def cmd_logs(ctx: commands.Context, count: int = 20):
            log_file = self.config.data_dir / "nim_agent.log"
            if not log_file.exists():
                await ctx.send("📄 No log file found yet.")
                return
            try:
                all_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = all_lines[-min(count, 50):]
                text = "\n".join(tail)
                if len(text) > 1800:
                    text = text[-1800:]
                await ctx.send(f"```\n{text}\n```")
            except Exception as e:
                await ctx.send(f"❌ Failed to read logs: {e}")

        # ─── /skills ─────────────────────────────
        @self.bot.command(name="skills")
        async def cmd_skills(ctx: commands.Context):
            lines = []
            for s in self.agent.skills.skills:
                params = ", ".join(s.parameters.get("properties", {}).keys())
                lines.append(f"🛠️ **{s.name}**\n   {s.description}\n   Params: `{params}`")
            text = "\n\n".join(lines) if lines else "No skills loaded."
            for i in range(0, len(text), 1900):
                await ctx.send(text[i:i+1900])

        # ─── /install ────────────────────────────
        @self.bot.command(name="install")
        async def cmd_install(ctx: commands.Context, url: str = None):
            if not url:
                await ctx.send("Usage: `/install <url-to-skill.py>`")
                return
            if not url.endswith(".py"):
                await ctx.send("⚠️ URL must point to a `.py` file.")
                return
            msg = await ctx.send("📦 Downloading skill...")
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            await msg.edit(content=f"❌ Download failed: HTTP {resp.status}")
                            return
                        content = await resp.text()
                filename = url.split("/")[-1]
                filepath = self.config.custom_skills_dir / filename
                filepath.write_text(content, encoding="utf-8")
                self.agent.skills.load_all()
                await msg.edit(content=f"✅ Skill installed: `{filename}`\nTotal skills loaded: {len(self.agent.skills.skills)}")
            except Exception as e:
                await msg.edit(content=f"❌ Install failed: {e}")

        # ─── /clear ──────────────────────────────
        @self.bot.command(name="clear")
        async def cmd_clear(ctx: commands.Context):
            chat_id = ctx.channel.id
            current = self._resolve_project(ctx)
            await self.agent.memory.clear_history(chat_id, project_name=current)
            await ctx.send(f"🗑️ Conversation history for project `{current}` cleared.")

        # ─── /status ─────────────────────────────
        @self.bot.command(name="status")
        async def cmd_status(ctx: commands.Context):
            async with ctx.typing():
                result = await self.agent.skills.execute("system_info", category="all")
                for i in range(0, len(result), 1900):
                    await ctx.send(f"```\n{result[i:i+1900]}\n```")

        # ─── /improvements ───────────────────────
        @self.bot.command(name="improvements")
        async def cmd_improvements(ctx: commands.Context):
            imp_dir = self.config.improvements_dir
            files = sorted(imp_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                await ctx.send("No improvement proposals yet.")
                return
            lines = ["🔧 **Improvement Proposals**\n"]
            for f in files[:10]:
                content = f.read_text(encoding="utf-8")
                title = f.stem
                for line in content.split("\n"):
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                lines.append(f"• `{f.name}`\n  {title}")
            await ctx.send("\n".join(lines))

        # ─── /purge ──────────────────────────────
        @self.bot.command(name="purge")
        async def cmd_purge(ctx: commands.Context, count: int = 5):
            """Delete the last N messages sent by the bot in this channel."""
            if count > 50:
                count = 50
            deleted = 0
            async for msg in ctx.channel.history(limit=200):
                if msg.author == self.bot.user and deleted < count:
                    try:
                        await msg.delete()
                        deleted += 1
                    except Exception:
                        pass
            await ctx.send(f"🗑️ Purged {deleted} bot messages.", delete_after=5)

        # ─── /backup ─────────────────────────────
        @self.bot.command(name="backup")
        async def cmd_backup(ctx: commands.Context):
            """Zip the current project workspace and upload it."""
            current = self._resolve_project(ctx)
            ws = self.swarm._workspace_path(current)
            if not ws.exists() or not any(ws.iterdir()):
                await ctx.send(f"⚠️ Project `{current}` workspace is empty. Nothing to backup.")
                return

            msg = await ctx.send(f"📦 Creating backup of `{current}`...")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = self.config.data_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            zip_path = backup_dir / f"{current}_{ts}.zip"

            try:
                with zipfile.ZipFile(str(zip_path), 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in ws.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(ws))

                size = _human_bytes(zip_path.stat().st_size)

                # Try to upload to Discord (8MB limit)
                if zip_path.stat().st_size < 8 * 1024 * 1024:
                    await msg.edit(content=f"📦 Backup created: `{zip_path.name}` ({size})")
                    await ctx.send(file=discord.File(str(zip_path), filename=zip_path.name))
                else:
                    await msg.edit(content=f"📦 Backup saved on Pi: `{zip_path}` ({size})\n(Too large to upload to Discord)")
            except Exception as e:
                await msg.edit(content=f"❌ Backup failed: {e}")

        # ══════════════════════════════════════════
        # /project — Expanded
        # ══════════════════════════════════════════

        @self.bot.command(name="project")
        async def cmd_project(ctx: commands.Context, action: str = None, proj_name: str = None):
            chat_id = ctx.channel.id
            is_group = isinstance(ctx.channel, discord.TextChannel)
            current = self._resolve_project(ctx)

            if not action:
                ws = self.swarm._workspace_path(current)
                exists = ws.exists()
                await ctx.send(
                    f"📁 **Current Project:** `{current}`\n"
                    f"Workspace: `{ws}` {'✅' if exists else '❌ (not created yet)'}\n\n"
                    "**Usage:**\n"
                    "`/project list` — All projects\n"
                    "`/project new <name>` — Create/switch\n"
                    "`/project switch <name>` — Switch\n"
                    "`/project delete <name>` — Delete memory\n"
                    "`/project files` — List workspace files\n"
                    "`/project info` — Disk usage & stats\n"
                    "`/project export` — Download as ZIP"
                )
                return

            action = action.lower()

            if action == "list":
                projects = await self.agent.memory.get_projects(chat_id)
                # Also scan workspace dirs
                ws_root = Path(getattr(self.config, "workspace_dir", "/data/workspace"))
                ws_projects = set()
                if ws_root.exists():
                    for d in ws_root.iterdir():
                        if d.is_dir():
                            ws_projects.add(d.name)
                all_projects = sorted(set(projects) | ws_projects)
                if not all_projects:
                    await ctx.send("📁 No projects found.")
                    return
                lines = []
                for p in all_projects:
                    marker = " ← active" if p == current else ""
                    has_ws = "📂" if (ws_root / p).exists() else "💭"
                    cp = self.swarm.get_checkpoint(p)
                    if cp:
                        done = len(cp.get("completed_modules", []))
                        total = len(cp.get("plan", []))
                        lines.append(f"{has_ws} `{p}`{marker} — swarm {done}/{total}")
                    else:
                        lines.append(f"{has_ws} `{p}`{marker}")
                await ctx.send("📁 **All Projects:**\n" + "\n".join(lines))
                return

            if action == "files":
                files = self.swarm.list_project_files(current)
                if not files:
                    await ctx.send(f"📁 Project `{current}` has no files yet.")
                    return
                lines = []
                for f in files:
                    icon = "📁" if f["is_dir"] else "📄"
                    size = f" ({_human_bytes(f['size'])})" if not f["is_dir"] else ""
                    lines.append(f"{icon} `{f['name']}`{size}")
                await ctx.send(f"📁 **Files in `{current}`:**\n" + "\n".join(lines))
                return

            if action == "info":
                usage = self.swarm.get_workspace_disk_usage(current)
                files = self.swarm.list_project_files(current)
                cp = self.swarm.get_checkpoint(current)
                cp_info = "No swarm checkpoint"
                if cp:
                    done = len(cp.get("completed_modules", []))
                    total = len(cp.get("plan", []))
                    cp_info = f"Swarm progress: {done}/{total} modules"
                await ctx.send(
                    f"📊 **Project Info: `{current}`**\n"
                    f"Files: {len(files)}\n"
                    f"Disk usage: {_human_bytes(usage)}\n"
                    f"{cp_info}"
                )
                return

            if action == "export":
                ws = self.swarm._workspace_path(current)
                if not ws.exists() or not any(ws.iterdir()):
                    await ctx.send(f"⚠️ Project `{current}` is empty.")
                    return
                tmp_zip = self.config.data_dir / f"_export_{current}.zip"
                try:
                    with zipfile.ZipFile(str(tmp_zip), 'w', zipfile.ZIP_DEFLATED) as zf:
                        for f in ws.rglob("*"):
                            if f.is_file() and not f.name.startswith("."):
                                zf.write(f, f.relative_to(ws))
                    if tmp_zip.stat().st_size < 8 * 1024 * 1024:
                        await ctx.send(
                            f"📦 **Export: `{current}`**",
                            file=discord.File(str(tmp_zip), filename=f"{current}.zip")
                        )
                    else:
                        await ctx.send(f"📦 Export too large for Discord. Saved at: `{tmp_zip}`")
                except Exception as e:
                    await ctx.send(f"❌ Export failed: {e}")
                finally:
                    try:
                        tmp_zip.unlink(missing_ok=True)
                    except Exception:
                        pass
                return

            if is_group and action in ("new", "switch", "delete"):
                await ctx.send("🔒 In servers, projects are locked to the channel name. Create a new text channel for a new project.")
                return

            if not proj_name:
                await ctx.send("⚠️ Provide a project name. Example: `/project new website`")
                return

            proj_name = proj_name.lower().strip()

            if action in ("new", "switch"):
                self._active_projects[chat_id] = proj_name
                # Auto-create workspace dir
                ws = self.swarm._workspace_path(proj_name)
                ws.mkdir(parents=True, exist_ok=True)
                await ctx.send(f"🔄 Switched to project: `{proj_name}`\nWorkspace ready at `{ws}`")

            elif action == "delete":
                await self.agent.memory.clear_history(chat_id, project_name=proj_name)
                ws = self.swarm._workspace_path(proj_name)
                if ws.exists():
                    shutil.rmtree(ws, ignore_errors=True)
                if current == proj_name:
                    self._active_projects[chat_id] = "main"
                    await ctx.send(f"🗑️ Deleted project `{proj_name}` (memory + workspace). Reverted to `main`.")
                else:
                    await ctx.send(f"🗑️ Deleted project `{proj_name}` (memory + workspace).")
            else:
                await ctx.send("⚠️ Unknown action. Use: list, new, switch, delete, files, info, export.")

        # ══════════════════════════════════════════
        # /swarm — Expanded with subcommands
        # ══════════════════════════════════════════

        @self.bot.command(name="swarm")
        async def cmd_swarm(ctx: commands.Context, *, prompt: str = None):
            chat_id = ctx.channel.id
            current = self._resolve_project(ctx)

            if not prompt:
                await ctx.send(
                    "🐝 **Swarm Commands:**\n"
                    "`/swarm <prompt>` — Launch coding swarm\n"
                    "`/swarm resume` — Resume from checkpoint\n"
                    "`/swarm status` — Show progress\n"
                    "`/swarm files` — List generated files\n"
                    "`/swarm cat <filename>` — View a file\n"
                    "`/swarm cancel` — Cancel running swarm\n"
                    "`/swarm reset` — Clear checkpoint (fresh start)\n"
                    "`/swarm diff` — Show files vs checkpoint"
                )
                return

            subcmd = prompt.strip().split()[0].lower()

            # ── /swarm status ────────────────────
            if subcmd == "status":
                cp = self.swarm.get_checkpoint(current)
                if not cp:
                    await ctx.send(f"📊 No swarm data for project `{current}`. Run `/swarm <prompt>` to start.")
                    return
                plan = cp.get("plan", [])
                done = cp.get("completed_modules", [])
                running = chat_id in self._active_tasks and not self._active_tasks[chat_id].done()
                status_icon = "🟢 Running" if running else ("✅ Complete" if len(done) == len(plan) else "⏸️ Paused/Crashed")

                lines = [f"📊 **Swarm Status — `{current}`**\n{status_icon}\nProgress: {len(done)}/{len(plan)} modules\n"]
                for m in plan:
                    fn = m.get("filename", "?")
                    icon = "✅" if fn in done else ("🔄" if running and fn not in done else "⬜")
                    lines.append(f"{icon} `{fn}` — {m.get('description', '')[:60]}")
                text = "\n".join(lines)
                for i in range(0, len(text), 1900):
                    await ctx.send(text[i:i+1900])
                return

            # ── /swarm files ─────────────────────
            if subcmd == "files":
                files = self.swarm.list_project_files(current)
                if not files:
                    await ctx.send(f"📁 No files in `{current}` workspace.")
                    return
                lines = []
                for f in files:
                    icon = "📁" if f["is_dir"] else "📄"
                    size = f" ({_human_bytes(f['size'])})" if not f["is_dir"] else ""
                    lines.append(f"{icon} `{f['name']}`{size}")
                await ctx.send(f"📁 **Swarm Files — `{current}`:**\n" + "\n".join(lines))
                return

            # ── /swarm cat <file> ────────────────
            if subcmd == "cat":
                parts = prompt.strip().split(maxsplit=1)
                if len(parts) < 2:
                    await ctx.send("Usage: `/swarm cat <filename>`")
                    return
                filename = parts[1].strip()
                content = self.swarm.read_project_file(current, filename)
                if content is None:
                    await ctx.send(f"❌ File `{filename}` not found in project `{current}`.")
                    return
                # Detect extension for syntax highlight
                ext = Path(filename).suffix.lstrip(".")
                lang = ext if ext in ("luau", "lua", "py", "json", "js", "ts", "sh") else ""
                header = f"📄 **`{filename}`** ({_human_bytes(len(content))})\n"
                if len(content) > 1700:
                    content = content[:1700] + "\n\n... (truncated)"
                await ctx.send(f"{header}```{lang}\n{content}\n```")
                return

            # ── /swarm cancel ────────────────────
            if subcmd == "cancel":
                task = self._active_tasks.get(chat_id)
                if task and not task.done():
                    task.cancel()
                    await ctx.send("🛑 Swarm task cancelled. Progress is saved — use `/swarm resume` to continue later.")
                else:
                    await ctx.send("⚠️ No active swarm running in this channel.")
                return

            # ── /swarm reset ─────────────────────
            if subcmd == "reset":
                removed = self.swarm.reset_checkpoint(current)
                if removed:
                    await ctx.send(f"🔄 Checkpoint cleared for `{current}`. Next `/swarm` will start fresh.\n(Files are still on disk — use `/project delete {current}` to wipe everything.)")
                else:
                    await ctx.send(f"⚠️ No checkpoint found for `{current}`.")
                return

            # ── /swarm resume ────────────────────
            if subcmd == "resume":
                cp = self.swarm.get_checkpoint(current)
                if not cp or not cp.get("plan"):
                    await ctx.send(f"⚠️ No checkpoint to resume for `{current}`. Use `/swarm <prompt>` to start.")
                    return
                done = len(cp.get("completed_modules", []))
                total = len(cp.get("plan", []))
                if done >= total:
                    await ctx.send(f"✅ Project `{current}` is already complete ({total}/{total} modules). Use `/swarm reset` to start over.")
                    return

                await ctx.send(f"🐝 Resuming swarm for `{current}` — {done}/{total} modules done.\n(Picking up where we left off!)")

                # Resume uses a dummy prompt since plan already exists in checkpoint
                task = asyncio.create_task(
                    self.swarm.run_pipeline(chat_id, current, "(resumed)")
                )
                self._active_tasks[chat_id] = task
                self._all_tasks.add(task)
                task.add_done_callback(self._all_tasks.discard)
                return

            # ── /swarm diff ──────────────────────
            if subcmd == "diff":
                cp = self.swarm.get_checkpoint(current)
                files = self.swarm.list_project_files(current)
                file_names = {f["name"] for f in files if not f["is_dir"]}

                if not cp:
                    if files:
                        await ctx.send(f"📊 No checkpoint, but {len(files)} files exist in workspace:\n" + "\n".join(f"  📄 `{f['name']}`" for f in files if not f["is_dir"]))
                    else:
                        await ctx.send(f"📊 No checkpoint and no files for `{current}`.")
                    return

                planned = {m.get("filename", "?") for m in cp.get("plan", [])}
                completed = set(cp.get("completed_modules", []))
                extra = file_names - planned - {".swarm_checkpoint.json"}

                lines = [f"📊 **Diff — `{current}`**\n"]
                for fn in sorted(planned):
                    if fn in completed:
                        exists = fn in file_names
                        lines.append(f"✅ `{fn}` — completed {'& on disk' if exists else '(missing from disk!)'}")
                    else:
                        exists = fn in file_names
                        lines.append(f"⬜ `{fn}` — pending {'(partial on disk)' if exists else ''}")
                for fn in sorted(extra):
                    lines.append(f"➕ `{fn}` — extra file (not in plan)")

                await ctx.send("\n".join(lines))
                return

            # ── /swarm <prompt> — Launch ──────────
            if chat_id in self._active_tasks:
                existing = self._active_tasks[chat_id]
                if not existing.done():
                    await ctx.send("⚠️ A swarm is already running in this channel! Use `/swarm cancel` first, or `/swarm status` to check progress.")
                    return

            await ctx.send(f"🐝 Deploying Swarm into project `{current}`...\n(I'll message you live progress right here!)")

            task = asyncio.create_task(
                self.swarm.run_pipeline(chat_id, current, prompt)
            )
            self._active_tasks[chat_id] = task
            self._all_tasks.add(task)
            task.add_done_callback(self._all_tasks.discard)

        # ─── /stopswarm — top-level alias for /swarm cancel ──
        @self.bot.command(name="stopswarm")
        async def cmd_stopswarm(ctx: commands.Context):
            """Top-level alias: stop a running swarm (same as /swarm cancel)."""
            chat_id = ctx.channel.id
            task = self._active_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
                await ctx.send("🛑 Swarm task cancelled. Progress is saved — use `/swarm resume` to continue later.")
            else:
                await ctx.send("⚠️ No active swarm running in this channel.")

    # ──────────────────────────────────────────────
    # Text Message Handling
    # ──────────────────────────────────────────────

    async def _handle_text_message(self, message: discord.Message):
        """Handle incoming text messages — the main conversation flow."""
        chat_id = message.channel.id
        current_project = self._resolve_project(message)
        user_message = message.content

        thinking_msg = await message.channel.send(f"🧠 Thinking... [Project: `{current_project}`]")

        async with message.channel.typing():
            try:
                response = await self.agent.process_message(chat_id, user_message, project_name=current_project)

                if len(response) <= 1900:
                    try:
                        await thinking_msg.edit(content=response)
                    except Exception:
                        await message.channel.send(response)
                else:
                    try:
                        await thinking_msg.edit(content=response[:1900])
                    except Exception:
                        await message.channel.send(response[:1900])
                    for i in range(1900, len(response), 1900):
                        await message.channel.send(response[i:i+1900])

            except Exception as e:
                logger.error("Error processing message: %s", e, exc_info=True)
                try:
                    await thinking_msg.edit(content=f"❌ Error: {e}")
                except Exception:
                    await message.channel.send(f"❌ Error: {e}")

    # ──────────────────────────────────────────────
    # Callbacks
    # ──────────────────────────────────────────────

    async def _send_improvement_notification(self, message: str):
        """Send a self-improvement proposal to the owner via Discord DM."""
        if not self._owner_chat_id:
            if self.config.allowed_users:
                self._owner_chat_id = self.config.allowed_users[0]
            else:
                logger.warning("No owner chat ID available for improvement notification")
                return
        try:
            user = await self.bot.fetch_user(self._owner_chat_id)
            if not user:
                return
            for i in range(0, len(message), 1900):
                await user.send(message[i:i+1900])
        except Exception as e:
            logger.error("Failed to send improvement notification: %s", e)

    def _is_progress_message(self, message: str) -> bool:
        """Detect transient progress messages that should be edited in-place."""
        return message.startswith("⏳ Waiting on") or message.startswith("⏳ **Waiting on")

    async def _send_swarm_update(self, chat_id: int, message: str):
        """Callback for Swarm to send messages into the active channel.

        Transient progress messages (heartbeats like "Waiting on X... 40s")
        are edited in-place on a single Discord message to prevent spam.
        All other messages (milestones, errors, completions) are sent as new messages.
        """
        try:
            channel = self.bot.get_channel(chat_id)
            if not channel:
                user = self.bot.get_user(chat_id)
                if not user:
                    user = await self.bot.fetch_user(chat_id)
                if user:
                    await user.send(message)
                return

            is_progress = self._is_progress_message(message)

            if is_progress:
                # Try to edit the existing progress message in-place
                prev = self._progress_messages.get(chat_id)
                if prev is not None:
                    try:
                        await prev.edit(content=message)
                        return
                    except (discord.NotFound, discord.HTTPException):
                        # Message was deleted or too old; send a new one
                        pass

                # No existing progress message — create one and track it
                msg = await channel.send(message)
                self._progress_messages[chat_id] = msg
            else:
                # Milestone / non-progress message: send as new message
                # Clear the tracked progress message so the next heartbeat starts fresh
                self._progress_messages.pop(chat_id, None)
                for i in range(0, len(message), 1900):
                    await channel.send(message[i:i+1900])

        except Exception as e:
            logger.error(f"Swarm update failed to send to Discord (chat_id: {chat_id}): {e}")
