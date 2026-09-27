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
from agent.config_manager import ConfigManager

logger = logging.getLogger("nim_agent.discord")


def _project_name_from_channel(channel) -> str:
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
        self.cfg_mgr = ConfigManager(config)
        self._owner_chat_id: int | None = None
        self._active_projects: dict[int, str] = {}
        self._active_tasks: dict[int, asyncio.Task] = {}
        self._all_tasks: set[asyncio.Task] = set()
        self._progress_messages: dict[int, discord.Message] = {}
        self.swarm = SwarmOrchestrator(config, self._send_swarm_update)
        self._start_time = time.monotonic()

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        self.bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)
        self._setup_events()
        self._setup_commands()

        self._bot_task = None

    async def start(self):
        self.agent.set_notify_callback(self._send_improvement_notification)
        logger.info("Discord bot starting...")
        self._bot_task = asyncio.create_task(self.bot.start(self.config.discord_token))
        logger.info("Discord bot started")

    async def stop(self):
        if self.bot:
            await self.bot.close()
        if self._bot_task:
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass

    def _is_allowed(self, user_id: int) -> bool:
        if not self.config.allowed_users:
            return True
        return user_id in self.config.allowed_users

    def _resolve_project(self, ctx_or_message) -> str:
        channel = getattr(ctx_or_message, 'channel', ctx_or_message)
        chat_id = channel.id
        if isinstance(channel, discord.TextChannel):
            proj = _project_name_from_channel(channel)
            self._active_projects[chat_id] = proj
            return proj
        return self._active_projects.get(chat_id, "main")

    # ── Events ──

    def _setup_events(self):
        @self.bot.event
        async def on_ready():
            logger.info(f"NIM Agent connected as {self.bot.user}")

        @self.bot.event
        async def on_guild_channel_create(channel):
            if not isinstance(channel, discord.TextChannel):
                return
            proj = _project_name_from_channel(channel)
            ws = self.swarm._workspace_path(proj)
            ws.mkdir(parents=True, exist_ok=True)
            logger.info("Auto-created workspace for #%s", channel.name)
            try:
                await channel.send(
                    f":file_folder: **Workspace Ready**\n"
                    f"Project `{proj}` auto-provisioned.\n"
                    f"Use `/swarm <prompt>` to start building!"
                )
            except Exception:
                pass

        @self.bot.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                return
            if not self._is_allowed(message.author.id):
                if isinstance(message.channel, discord.DMChannel):
                    await message.channel.send(":no_entry: Unauthorized.")
                return
            self._owner_chat_id = message.author.id
            ctx = await self.bot.get_context(message)
            if ctx.valid:
                await self.bot.invoke(ctx)
                return
            await self._handle_text_message(message)

    # ── Commands ──

    def _setup_commands(self):

        @self.bot.command(name="start")
        async def cmd_start(ctx: commands.Context):
            await ctx.send(
                ":robot: **NIM Agent Online**\n\n"
                "I'm your AI companion on Raspberry Pi 4.\n"
                "Powered by multi-model swarm via NVIDIA NIM + OpenRouter.\n\n"
                "Send me a message or use `/help`."
            )

        @self.bot.command(name="help")
        async def cmd_help(ctx: commands.Context):
            skills_list = "\n".join(
                f"  \u2022 `{s.name}` \u2014 {s.description[:60]}"
                for s in self.agent.skills.skills
            )
            await ctx.send(
                ":blue_book: **NIM Agent Help**\n\n"
                "**Talk to me** \u2014 just send a message!\n\n"
                "**General:**\n"
                "  `/help` \u2014 This message\n"
                "  `/status` \u2014 System stats\n"
                "  `/ping` \u2014 Latency\n"
                "  `/uptime` \u2014 Bot uptime\n"
                "  `/models` \u2014 Show model lineup\n"
                "  `/providers` \u2014 Show providers config\n"
                "  `/logs [n]` \u2014 Last N log lines\n\n"
                "**Configuration:**\n"
                "  `/model <role> <model_name>` \u2014 Change model for a role\n"
                "  `/add model <role> <model_name>` \u2014 Add/override model\n"
                "  `/apikey <name_or_id> <key>` \u2014 Update provider API key\n"
                "  `/endpoint <name_or_id> <url>` \u2014 Update provider endpoint\n"
                "  `/reload` \u2014 Reload all config from disk\n\n"
                "**Swarm (Multi-Agent Coding):**\n"
                "  `/swarm <prompt>` \u2014 Launch coding swarm\n"
                "  `/swarm resume` \u2014 Resume swarm\n"
                "  `/swarm status` \u2014 Check progress\n"
                "  `/swarm files` \u2014 List files\n"
                "  `/swarm cat <file>` \u2014 View file\n"
                "  `/swarm cancel` \u2014 Cancel swarm\n"
                "  `/swarm reset` \u2014 Clear checkpoint\n"
                "  `/swarm diff` \u2014 Files vs checkpoint\n"
                "  `/stopswarm` \u2014 Quick cancel\n\n"
                "**Project Management:**\n"
                "  `/project` \u2014 Show current project\n"
                "  `/project list` \u2014 List all projects\n"
                "  `/project new <name>` \u2014 Create/switch\n"
                "  `/project switch <name>` \u2014 Switch\n"
                "  `/project delete <name>` \u2014 Delete\n"
                "  `/project files` \u2014 List files\n"
                "  `/project info` \u2014 Stats\n"
                "  `/project export` \u2014 Download ZIP\n\n"
                "**Utilities:**\n"
                "  `/skills` \u2014 List skills\n"
                "  `/install <url>` \u2014 Install skill\n"
                "  `/clear` \u2014 Clear history\n"
                "  `/backup` \u2014 Snapshot workspace\n"
                "  `/purge [n]` \u2014 Delete bot messages\n"
                "  `/improvements` \u2014 View proposals\n\n"
                f"**Loaded Skills:**\n{skills_list}"
            )

        @self.bot.command(name="ping")
        async def cmd_ping(ctx: commands.Context):
            start = time.monotonic()
            msg = await ctx.send("Pinging...")
            rtt = (time.monotonic() - start) * 1000
            ws_lat = self.bot.latency * 1000
            await msg.edit(content=f":ping_pong: **Pong!**  RTT: `{rtt:.0f}ms` | WS: `{ws_lat:.0f}ms`")

        @self.bot.command(name="uptime")
        async def cmd_uptime(ctx: commands.Context):
            elapsed = time.monotonic() - self._start_time
            delta = timedelta(seconds=int(elapsed))
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            mins, secs = divmod(rem, 60)
            parts = []
            if days: parts.append(f"{days}d")
            if hours: parts.append(f"{hours}h")
            if mins: parts.append(f"{mins}m")
            parts.append(f"{secs}s")
            await ctx.send(f":clock1: **Uptime:** {' '.join(parts)}")

        # ── /model ─────────────────────────────
        @self.bot.command(name="model")
        async def cmd_model(ctx: commands.Context, role: str = None, *, model_name: str = None):
            """Change the model for a swarm role. Usage: /model <role> <model_name>"""
            valid_roles = ("overseer", "architect", "coder", "verifier", "agent")
            if not role or not model_name:
                current = self.cfg_mgr.get_models()
                lines = [f":brain: **Current Models**\n"]
                for r in valid_roles:
                    lines.append(f"  `{r}`: `{current.get(r, '?')}`")
                await ctx.send("\n".join(lines))
                return

            role = role.lower()
            if role not in valid_roles:
                await ctx.send(f":warning: Invalid role: `{role}`. Valid: {', '.join(valid_roles)}")
                return

            self.cfg_mgr.update_models({role: model_name})
            self.swarm.reload_config()
            await ctx.send(f":white_check_mark: Model for `{role}` set to `{model_name}`\nSwarm clients reloaded.")

        # ── /add model ─────────────────────────
        @self.bot.command(name="add")
        async def cmd_add(ctx: commands.Context, model_role: str = None, *, model_name: str = None):
            """Add or override a model. Usage: /add model <role> <model_name>"""
            if not model_role or not model_name:
                await ctx.send("Usage: `/add model <role> <model_name>`\nExample: `/add model coder my-custom-model`")
                return
            if model_role.lower() == "model" and model_name:
                # /add model <role> <name> - shift arguments
                parts = model_name.split(None, 1)
                if len(parts) >= 2:
                    role = parts[0].lower()
                    name = parts[1]
                else:
                    await ctx.send("Usage: `/add model <role> <model_name>`")
                    return
            else:
                role = model_role.lower()
                name = model_name

            valid_roles = ("overseer", "architect", "coder", "verifier", "agent")
            if role not in valid_roles:
                await ctx.send(f"Invalid role: `{role}`. Valid: {', '.join(valid_roles)}")
                return

            self.cfg_mgr.update_models({role: name})
            self.swarm.reload_config()
            await ctx.send(f":white_check_mark: Model `{role}` set to `{name}`\nSwarm reloaded.")

        # ── /apikey ────────────────────────────
        @self.bot.command(name="apikey")
        async def cmd_apikey(ctx: commands.Context, provider_id_or_name: str = None, *, new_key: str = None):
            """Update a provider's API key. Usage: /apikey <name_or_id> <new_key>"""
            if not provider_id_or_name or not new_key:
                await ctx.send("Usage: `/apikey <provider_name_or_id> <new_api_key>`\nSee `/providers` for names.")
                return

            success = self.cfg_mgr.update_provider_key(provider_id_or_name, new_key)
            if success:
                self.swarm.reload_config()
                masked = new_key[:6] + "..." + new_key[-4:] if len(new_key) > 10 else "***"
                await ctx.send(f":white_check_mark: API key for `{provider_id_or_name}` updated to `{masked}`\nSwarm reloaded.")
            else:
                await ctx.send(f":warning: Provider `{provider_id_or_name}` not found. Use `/providers` to list them.")

        # ── /endpoint ──────────────────────────
        @self.bot.command(name="endpoint")
        async def cmd_endpoint(ctx: commands.Context, provider_id_or_name: str = None, *, new_url: str = None):
            """Update a provider's endpoint URL. Usage: /endpoint <name_or_id> <url>"""
            if not provider_id_or_name or not new_url:
                await ctx.send("Usage: `/endpoint <provider_name_or_id> <new_url>`\nExample: `/endpoint OpenGateway https://my-new-url.com/v1`")
                return

            success = self.cfg_mgr.update_provider_endpoint(provider_id_or_name, new_url)
            if success:
                self.swarm.reload_config()
                await ctx.send(f":white_check_mark: Endpoint for `{provider_id_or_name}` set to `{new_url}`\nSwarm reloaded.")
            else:
                await ctx.send(f":warning: Provider `{provider_id_or_name}` not found.")

        # ── /providers ─────────────────────────
        @self.bot.command(name="providers")
        async def cmd_providers(ctx: commands.Context):
            """List all configured providers with their status."""
            providers = self.cfg_mgr.get_providers()
            mapping = self.cfg_mgr.get_model_providers()
            if not providers:
                await ctx.send("No providers configured. Add one via the dashboard or edit `providers_config.json`.")
                return
            lines = [":globe_with_meridians: **Configured Providers**\n"]
            for p in providers:
                pid = p.get("id", "?")
                name = p.get("name", "Unnamed")
                url = p.get("base_url", "?")
                key = p.get("api_key", "")
                masked = key[:6] + "..." + key[-4:] if len(key) > 10 else "***" if key else "No key"
                assigned = [role for role, pid2 in mapping.items() if pid2 == pid]
                roles_str = (", ".join(assigned)) if assigned else "(not assigned)"
                lines.append(f"  **{name}** (`{pid}`)")
                lines.append(f"    URL: `{url}`")
                lines.append(f"    Key: `{masked}`")
                lines.append(f"    Roles: {roles_str}")
            await ctx.send("\n".join(lines))

        # ── /reload ────────────────────────────
        @self.bot.command(name="reload")
        async def cmd_reload(ctx: commands.Context):
            """Reload all configs (models, providers, mappings) from disk."""
            self.cfg_mgr = ConfigManager(self.config)
            self.swarm.reload_config()
            await ctx.send(":arrows_counterclockwise: Config reloaded from disk. Swarm clients refreshed.")

        # ── /models ────────────────────────────
        @self.bot.command(name="models")
        async def cmd_models(ctx: commands.Context):
            models = self.cfg_mgr.get_models()
            lines = [":brain: **Model Lineup**\n"]
            roster = [
                ("Overseer", models.get("overseer", "?")),
                ("Architect", models.get("architect", "?")),
                ("Coder", models.get("coder", "?")),
                ("Verifier", models.get("verifier", "?")),
                ("Agent", models.get("agent", self.config.model_name)),
            ]
            for label, m in roster:
                resolved = self.cfg_mgr.resolve_provider_for_role(label.lower())
                prov_name = resolved.get("name", "default") if resolved else "default"
                lines.append(f"  **{label}**")
                lines.append(f"    Model: `{m}`")
                lines.append(f"    Provider: `{prov_name}`")
            await ctx.send("\n".join(lines))

        @self.bot.command(name="logs")
        async def cmd_logs(ctx: commands.Context, count: int = 20):
            log_file = self.config.data_dir / "nim_agent.log"
            if not log_file.exists():
                await ctx.send("No log file found.")
                return
            try:
                all_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = all_lines[-min(count, 50):]
                text = "\n".join(tail)
                if len(text) > 1800:
                    text = text[-1800:]
                await ctx.send(f"```\n{text}\n```")
            except Exception as e:
                await ctx.send(f"Failed: {e}")

        @self.bot.command(name="skills")
        async def cmd_skills(ctx: commands.Context):
            lines = []
            for s in self.agent.skills.skills:
                params = ", ".join(s.parameters.get("properties", {}).keys())
                lines.append(f":wrench: **{s.name}**\n   {s.description}\n   Params: `{params}`")
            text = "\n\n".join(lines) if lines else "No skills loaded."
            for i in range(0, len(text), 1900):
                await ctx.send(text[i:i+1900])

        @self.bot.command(name="install")
        async def cmd_install(ctx: commands.Context, url: str = None):
            if not url:
                await ctx.send("Usage: `/install <url-to-skill.py>`")
                return
            if not url.endswith(".py"):
                await ctx.send("URL must point to a `.py` file.")
                return
            msg = await ctx.send("Downloading skill...")
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            await msg.edit(content=f"Download failed: HTTP {resp.status}")
                            return
                        content = await resp.text()
                filename = url.split("/")[-1]
                filepath = self.config.custom_skills_dir / filename
                filepath.write_text(content, encoding="utf-8")
                self.agent.skills.load_all()
                await msg.edit(content=f"Skill installed: `{filename}`\nTotal: {len(self.agent.skills.skills)}")
            except Exception as e:
                await msg.edit(content=f"Install failed: {e}")

        @self.bot.command(name="clear")
        async def cmd_clear(ctx: commands.Context):
            chat_id = ctx.channel.id
            current = self._resolve_project(ctx)
            await self.agent.memory.clear_history(chat_id, project_name=current)
            await ctx.send(f"History for `{current}` cleared.")

        @self.bot.command(name="status")
        async def cmd_status(ctx: commands.Context):
            async with ctx.typing():
                result = await self.agent.skills.execute("system_info", category="all")
                for i in range(0, len(result), 1900):
                    await ctx.send(f"```\n{result[i:i+1900]}\n```")

        @self.bot.command(name="improvements")
        async def cmd_improvements(ctx: commands.Context):
            imp_dir = self.config.improvements_dir
            files = sorted(imp_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                await ctx.send("No improvement proposals yet.")
                return
            lines = [":wrench: **Improvement Proposals**\n"]
            for f in files[:10]:
                content = f.read_text(encoding="utf-8")
                title = f.stem
                for line in content.split("\n"):
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
                lines.append(f"\u2022 `{f.name}`\n  {title}")
            await ctx.send("\n".join(lines))

        @self.bot.command(name="purge")
        async def cmd_purge(ctx: commands.Context, count: int = 5):
            if count > 50: count = 50
            deleted = 0
            async for msg in ctx.channel.history(limit=200):
                if msg.author == self.bot.user and deleted < count:
                    try:
                        await msg.delete()
                        deleted += 1
                    except Exception:
                        pass
            await ctx.send(f"Purged {deleted} bot messages.", delete_after=5)

        @self.bot.command(name="backup")
        async def cmd_backup(ctx: commands.Context):
            current = self._resolve_project(ctx)
            ws = self.swarm._workspace_path(current)
            if not ws.exists() or not any(ws.iterdir()):
                await ctx.send(f"Project `{current}` workspace is empty.")
                return
            msg = await ctx.send(f"Creating backup of `{current}`...")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = self.config.data_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            zip_path = backup_dir / f"{current}_{ts}.zip"
            try:
                import zipfile
                with zipfile.ZipFile(str(zip_path), 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in ws.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(ws))
                size = _human_bytes(zip_path.stat().st_size)
                if zip_path.stat().st_size < 8 * 1024 * 1024:
                    await msg.edit(content=f"Backup: `{zip_path.name}` ({size})")
                    await ctx.send(file=discord.File(str(zip_path), filename=zip_path.name))
                else:
                    await msg.edit(content=f"Backup on Pi: `{zip_path}` ({size})\n(Too large for Discord)")
            except Exception as e:
                await msg.edit(content=f"Backup failed: {e}")

        # ── /project ──
        @self.bot.command(name="project")
        async def cmd_project(ctx: commands.Context, action: str = None, proj_name: str = None):
            chat_id = ctx.channel.id
            is_group = isinstance(ctx.channel, discord.TextChannel)
            current = self._resolve_project(ctx)

            if not action:
                ws = self.swarm._workspace_path(current)
                exists = ws.exists()
                await ctx.send(
                    f":file_folder: **Current Project:** `{current}`\n"
                    f"Workspace: `{ws}` {'Exists' if exists else 'Not created'}\n\n"
                    "`/project list` \u2014 All projects\n"
                    "`/project new <name>` \u2014 Create/switch\n"
                    "`/project switch <name>` \u2014 Switch\n"
                    "`/project delete <name>` \u2014 Delete\n"
                    "`/project files` \u2014 List files\n"
                    "`/project info` \u2014 Stats\n"
                    "`/project export` \u2014 Download ZIP"
                )
                return

            action = action.lower()

            if action == "list":
                projects = await self.agent.memory.get_projects(chat_id)
                ws_root = Path(getattr(self.config, "workspace_dir", "/data/workspace"))
                ws_projects = set()
                if ws_root.exists():
                    for d in ws_root.iterdir():
                        if d.is_dir(): ws_projects.add(d.name)
                all_projects = sorted(set(projects) | ws_projects)
                if not all_projects:
                    await ctx.send("No projects found.")
                    return
                lines = []
                for p in all_projects:
                    marker = " \u2190 active" if p == current else ""
                    has_ws = ":file_folder:" if (ws_root / p).exists() else ":thought_balloon:"
                    cp = self.swarm.get_checkpoint(p)
                    if cp:
                        done = len(cp.get("completed_modules", []))
                        total = len(cp.get("plan", []))
                        lines.append(f"{has_ws} `{p}`{marker} \u2014 swarm {done}/{total}")
                    else:
                        lines.append(f"{has_ws} `{p}`{marker}")
                await ctx.send(":file_folder: **All Projects:**\n" + "\n".join(lines))
                return

            if action == "files":
                files = self.swarm.list_project_files(current)
                if not files:
                    await ctx.send(f"Project `{current}` has no files.")
                    return
                lines = []
                for f in files:
                    icon = ":file_folder:" if f["is_dir"] else ":page_facing_up:"
                    size = f" ({_human_bytes(f['size'])})" if not f["is_dir"] else ""
                    lines.append(f"{icon} `{f['name']}`{size}")
                await ctx.send(f"**Files in `{current}`:**\n" + "\n".join(lines))
                return

            if action == "info":
                usage = self.swarm.get_workspace_disk_usage(current)
                files = self.swarm.list_project_files(current)
                cp = self.swarm.get_checkpoint(current)
                cp_info = "No swarm checkpoint"
                if cp:
                    done = len(cp.get("completed_modules", []))
                    total = len(cp.get("plan", []))
                    cp_info = f"Swarm: {done}/{total}"
                await ctx.send(
                    f":bar_chart: **Project: `{current}`**\n"
                    f"Files: {len(files)}\n"
                    f"Disk: {_human_bytes(usage)}\n"
                    f"{cp_info}"
                )
                return

            if action == "export":
                ws = self.swarm._workspace_path(current)
                if not ws.exists() or not any(ws.iterdir()):
                    await ctx.send(f"Project `{current}` is empty.")
                    return
                tmp_zip = self.config.data_dir / f"_export_{current}.zip"
                try:
                    with zipfile.ZipFile(str(tmp_zip), 'w', zipfile.ZIP_DEFLATED) as zf:
                        for f in ws.rglob("*"):
                            if f.is_file() and not f.name.startswith("."):
                                zf.write(f, f.relative_to(ws))
                    if tmp_zip.stat().st_size < 8 * 1024 * 1024:
                        await ctx.send(f"**Export: `{current}`**", file=discord.File(str(tmp_zip), filename=f"{current}.zip"))
                    else:
                        await ctx.send(f"Export too large. Saved at: `{tmp_zip}`")
                except Exception as e:
                    await ctx.send(f"Export failed: {e}")
                finally:
                    try: tmp_zip.unlink(missing_ok=True)
                    except Exception: pass
                return

            if is_group and action in ("new", "switch", "delete"):
                await ctx.send("In servers, projects are locked to the channel name. Create a new text channel.")
                return

            if not proj_name:
                await ctx.send("Provide a project name. Example: `/project new website`")
                return
            proj_name = proj_name.lower().strip()

            if action in ("new", "switch"):
                self._active_projects[chat_id] = proj_name
                ws = self.swarm._workspace_path(proj_name)
                ws.mkdir(parents=True, exist_ok=True)
                await ctx.send(f"Switched to project: `{proj_name}`")
            elif action == "delete":
                await self.agent.memory.clear_history(chat_id, project_name=proj_name)
                ws = self.swarm._workspace_path(proj_name)
                if ws.exists(): shutil.rmtree(ws, ignore_errors=True)
                if current == proj_name:
                    self._active_projects[chat_id] = "main"
                    await ctx.send(f"Deleted `{proj_name}`. Reverted to `main`.")
                else:
                    await ctx.send(f"Deleted `{proj_name}`.")
            else:
                await ctx.send("Unknown action. Use: list, new, switch, delete, files, info, export.")

        # ── /swarm ──
        @self.bot.command(name="swarm")
        async def cmd_swarm(ctx: commands.Context, *, prompt: str = None):
            chat_id = ctx.channel.id
            current = self._resolve_project(ctx)

            if not prompt:
                await ctx.send(
                    ":honeybee: **Swarm Commands:**\n"
                    "`/swarm <prompt>` \u2014 Launch\n"
                    "`/swarm resume` \u2014 Resume\n"
                    "`/swarm status` \u2014 Progress\n"
                    "`/swarm files` \u2014 List files\n"
                    "`/swarm cat <filename>` \u2014 View file\n"
                    "`/swarm cancel` \u2014 Cancel\n"
                    "`/swarm reset` \u2014 Clear checkpoint\n"
                    "`/swarm diff` \u2014 Show changes"
                )
                return

            subcmd = prompt.strip().split()[0].lower()

            if subcmd == "status":
                cp = self.swarm.get_checkpoint(current)
                if not cp:
                    await ctx.send(f"No swarm data for `{current}`.")
                    return
                plan = cp.get("plan", [])
                done = cp.get("completed_modules", [])
                running = chat_id in self._active_tasks and not self._active_tasks[chat_id].done()
                status = "Running" if running else ("Complete" if len(done) == len(plan) else "Paused")
                lines = [f":bar_chart: **Swarm \u2014 `{current}`**\n{status}\n{len(done)}/{len(plan)} modules\n"]
                for m in plan:
                    fn = m.get("filename", "?")
                    icon = "\u2705" if fn in done else ("\U0001f504" if running else "\u2b1c")
                    lines.append(f"{icon} `{fn}` \u2014 {m.get('description', '')[:60]}")
                await ctx.send("\n".join(lines))
                return

            if subcmd == "files":
                files = self.swarm.list_project_files(current)
                if not files:
                    await ctx.send(f"No files in `{current}`.")
                    return
                lines = []
                for f in files:
                    icon = ":file_folder:" if f["is_dir"] else ":page_facing_up:"
                    size = f" ({_human_bytes(f['size'])})" if not f["is_dir"] else ""
                    lines.append(f"{icon} `{f['name']}`{size}")
                await ctx.send("\n".join(lines))
                return

            if subcmd == "cat":
                parts = prompt.strip().split(maxsplit=1)
                if len(parts) < 2:
                    await ctx.send("Usage: `/swarm cat <filename>`")
                    return
                filename = parts[1].strip()
                content = self.swarm.read_project_file(current, filename)
                if content is None:
                    await ctx.send(f"File `{filename}` not found.")
                    return
                ext = Path(filename).suffix.lstrip(".")
                lang = ext if ext in ("luau", "lua", "py", "json", "js", "ts", "sh") else ""
                header = f":page_facing_up: **`{filename}`** ({_human_bytes(len(content))})\n"
                if len(content) > 1700: content = content[:1700] + "\n\n... (truncated)"
                await ctx.send(f"{header}```{lang}\n{content}\n```")
                return

            if subcmd == "cancel":
                task = self._active_tasks.get(chat_id)
                if task and not task.done():
                    task.cancel()
                    await ctx.send("Swarm cancelled. Use `/swarm resume` to continue.")
                else:
                    await ctx.send("No active swarm.")
                return

            if subcmd == "reset":
                removed = self.swarm.reset_checkpoint(current)
                await ctx.send("Checkpoint cleared." if removed else "No checkpoint found.")
                return

            if subcmd == "resume":
                cp = self.swarm.get_checkpoint(current)
                if not cp or not cp.get("plan"):
                    await ctx.send(f"No checkpoint for `{current}`.")
                    return
                done = len(cp.get("completed_modules", []))
                total = len(cp.get("plan", []))
                if done >= total:
                    await ctx.send(f"Project `{current}` already complete. Use `/swarm reset` to start over.")
                    return
                await ctx.send(f"Resuming `{current}` \u2014 {done}/{total} modules.")
                task = asyncio.create_task(self.swarm.run_pipeline(chat_id, current, "(resumed)"))
                self._active_tasks[chat_id] = task
                self._all_tasks.add(task)
                task.add_done_callback(self._all_tasks.discard)
                return

            if subcmd == "diff":
                cp = self.swarm.get_checkpoint(current)
                files = self.swarm.list_project_files(current)
                fnames = {f["name"] for f in files if not f["is_dir"]}
                if not cp:
                    if files:
                        await ctx.send(f"No checkpoint, {len(files)} files exist:\n" + "\n".join(f"  `{f['name']}`" for f in files if not f["is_dir"]))
                    else:
                        await ctx.send(f"No data for `{current}`.")
                    return
                planned = {m.get("filename", "?") for m in cp.get("plan", [])}
                done_set = set(cp.get("completed_modules", []))
                extra = fnames - planned - {".swarm_checkpoint.json"}
                lines = [f":bar_chart: **Diff \u2014 `{current}`**\n"]
                for fn in sorted(planned):
                    if fn in done_set:
                        lines.append(f"\u2705 `{fn}` \u2014 completed {'& on disk' if fn in fnames else '(missing)'}")
                    else:
                        lines.append(f"\u2b1c `{fn}` \u2014 pending {'(partial on disk)' if fn in fnames else ''}")
                for fn in sorted(extra):
                    lines.append(f"\u2795 `{fn}` \u2014 extra")
                await ctx.send("\n".join(lines))
                return

            if chat_id in self._active_tasks:
                existing = self._active_tasks[chat_id]
                if not existing.done():
                    await ctx.send("Swarm already running! Use `/swarm cancel` first.")
                    return

            await ctx.send(f"Deploying Swarm into `{current}`...")
            task = asyncio.create_task(self.swarm.run_pipeline(chat_id, current, prompt))
            self._active_tasks[chat_id] = task
            self._all_tasks.add(task)
            task.add_done_callback(self._all_tasks.discard)

        @self.bot.command(name="stopswarm")
        async def cmd_stopswarm(ctx: commands.Context):
            chat_id = ctx.channel.id
            task = self._active_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
                await ctx.send("Swarm cancelled.")
            else:
                await ctx.send("No active swarm.")

    # ── Text Message Handling ──

    async def _handle_text_message(self, message: discord.Message):
        chat_id = message.channel.id
        current_project = self._resolve_project(message)
        user_message = message.content
        thinking_msg = await message.channel.send(f"Thinking... [Project: `{current_project}`]")
        async with message.channel.typing():
            try:
                response = await self.agent.process_message(chat_id, user_message, project_name=current_project)
                if len(response) <= 1900:
                    try: await thinking_msg.edit(content=response)
                    except: await message.channel.send(response)
                else:
                    try: await thinking_msg.edit(content=response[:1900])
                    except: await message.channel.send(response[:1900])
                    for i in range(1900, len(response), 1900):
                        await message.channel.send(response[i:i+1900])
            except Exception as e:
                logger.error("Error: %s", e, exc_info=True)
                try: await thinking_msg.edit(content=f"Error: {e}")
                except: await message.channel.send(f"Error: {e}")

    # ── Callbacks ──

    async def _send_improvement_notification(self, message: str):
        if not self._owner_chat_id:
            if self.config.allowed_users:
                self._owner_chat_id = self.config.allowed_users[0]
            else:
                return
        try:
            user = await self.bot.fetch_user(self._owner_chat_id)
            if not user: return
            for i in range(0, len(message), 1900):
                await user.send(message[i:i+1900])
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    def _is_progress_message(self, message: str) -> bool:
        return message.startswith("[WAIT]") or message.startswith("[WAIT")

    async def _send_swarm_update(self, chat_id: int, message: str):
        try:
            channel = self.bot.get_channel(chat_id)
            if not channel:
                user = self.bot.get_user(chat_id) or await self.bot.fetch_user(chat_id)
                if user: await user.send(message)
                return
            is_progress = self._is_progress_message(message)
            if is_progress:
                prev = self._progress_messages.get(chat_id)
                if prev is not None:
                    try:
                        await prev.edit(content=message)
                        return
                    except (discord.NotFound, discord.HTTPException):
                        pass
                msg = await channel.send(message)
                self._progress_messages[chat_id] = msg
            else:
                self._progress_messages.pop(chat_id, None)
                for i in range(0, len(message), 1900):
                    await channel.send(message[i:i+1900])
        except Exception as e:
            logger.error(f"Swarm update failed: {e}")
