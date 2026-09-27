"""
Mystic Dashboard — Backend API Server.
Serves the Codex-style web UI and provides APIs for chat, file management,
model configuration, skills listing, system status, cloud tunnel control,
terminal execution, MCP server management, and provider configuration.
"""

import os
import json
import time
import shutil
import logging
import platform
import psutil
import asyncio
from aiohttp import web
from pathlib import Path
from datetime import datetime, timezone

from agent.config import Config
from dashboard.chat_store import ChatStore
from dashboard.tunnel import TunnelManager

logger = logging.getLogger("nim_agent.dashboard")

config = Config()
chat_store = ChatStore(config.data_dir)
tunnel_mgr = TunnelManager(config.data_dir, port=8080)
models_config_path = config.data_dir / "models_config.json"
providers_config_path = config.providers_config_path
_start_time = time.time()

# ─── Auth Middleware ──────────────────────────────────────────────

@web.middleware
async def auth_middleware(request, handler):
    """Check API key for all routes except the login endpoint and static assets."""
    path = request.path

    # Always allow: login page, static assets, favicon, the auth check endpoint
    if path in ("/api/auth/check", "/api/auth/login", "/api/tunnel/status"):
        return await handler(request)
    if path.startswith("/static/"):
        return await handler(request)
    # Allow serving uploaded chat files (they're behind a unique filename)
    if path.startswith("/api/chat/files/"):
        return await handler(request)

    # Check for valid session via cookie, query param, or header
    api_key = (
        request.cookies.get("mystic_session")
        or request.query.get("key")
        or request.headers.get("X-Mystic-Key")
    )

    # For the root page, always serve it (the JS handles auth state)
    if path == "/":
        # If key is in query, set cookie and redirect clean
        if request.query.get("key") and tunnel_mgr.validate_key(request.query["key"]):
            resp = web.HTTPFound("/")
            resp.set_cookie(
                "mystic_session", request.query["key"],
                max_age=86400 * 30, httponly=True, samesite="Lax"
            )
            return resp
        return await handler(request)

    # API routes need auth
    if path.startswith("/api/"):
        if api_key and tunnel_mgr.validate_key(api_key):
            return await handler(request)
        return web.json_response({"error": "unauthorized"}, status=401)

    return await handler(request)


# ─── Auth Endpoints ──────────────────────────────────────────────

async def auth_check(request):
    """Check if the current session is authenticated."""
    key = (
        request.cookies.get("mystic_session")
        or request.query.get("key")
        or request.headers.get("X-Mystic-Key")
    )
    if key and tunnel_mgr.validate_key(key):
        return web.json_response({"authenticated": True})
    return web.json_response({"authenticated": False})


async def auth_login(request):
    """Authenticate with API key. Sets a session cookie."""
    try:
        body = await request.json()
        key = body.get("key", "")
    except Exception:
        key = request.query.get("key", "")

    if tunnel_mgr.validate_key(key):
        resp = web.json_response({"authenticated": True})
        resp.set_cookie(
            "mystic_session", key,
            max_age=86400 * 30, httponly=True, samesite="Lax"
        )
        return resp
    return web.json_response({"authenticated": False, "error": "Invalid API key"}, status=401)


# ─── Models Endpoints ────────────────────────────────────────────

async def get_models(request):
    """Get current models configuration."""
    default_config = {
        "overseer": "meta/llama3-70b-instruct",
        "architect": "z-ai/glm-5.1",
        "coder": "z-ai/glm-5.1",
        "verifier": "minimaxai/minimax-m2.7",
        "agent": config.model_name,
    }

    if models_config_path.exists():
        try:
            loaded = json.loads(models_config_path.read_text(encoding="utf-8"))
            default_config.update(loaded)
        except Exception as e:
            logger.error("Error reading models_config.json: %s", e)

    return web.json_response(default_config)


async def update_models(request):
    """Update models configuration."""
    try:
        new_config = await request.json()

        current = {}
        if models_config_path.exists():
            try:
                current = json.loads(models_config_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        current.update(new_config)
        models_config_path.write_text(json.dumps(current, indent=4), encoding="utf-8")

        return web.json_response({"status": "success", "models": current})
    except Exception as e:
        logger.error("Error updating models: %s", e)
        return web.json_response({"status": "error", "message": str(e)}, status=400)


# ─── Chat Endpoints ──────────────────────────────────────────────

async def list_threads(request):
    """List all chat threads."""
    return web.json_response(chat_store.list_threads())


async def create_thread(request):
    """Create a new chat thread."""
    try:
        body = await request.json()
        title = body.get("title", "New thread")
    except Exception:
        title = "New thread"
    thread = chat_store.create_thread(title)
    return web.json_response(thread)


async def get_thread(request):
    """Get a thread with all messages."""
    thread_id = request.match_info["thread_id"]
    thread = chat_store.get_thread(thread_id)
    if thread is None:
        return web.json_response({"error": "Thread not found"}, status=404)
    return web.json_response(thread)


async def delete_thread(request):
    """Delete a thread."""
    thread_id = request.match_info["thread_id"]
    if chat_store.delete_thread(thread_id):
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Thread not found"}, status=404)


async def chat_upload(request):
    """Upload files for chat messages. Returns list of attachment metadata."""
    reader = await request.multipart()
    saved = []

    async for part in reader:
        if part.name in ("file", "files"):
            filename = part.filename
            if not filename:
                continue
            # Generate unique filename to avoid collisions
            ext = Path(filename).suffix
            safe_name = f"{int(time.time()*1000)}_{filename}"
            file_path = chat_store.uploads_dir / safe_name

            with open(file_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    f.write(chunk)

            # Determine type
            img_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
            is_image = ext.lower() in img_exts

            saved.append({
                "name": filename,
                "stored_name": safe_name,
                "url": f"/api/chat/files/{safe_name}",
                "size": file_path.stat().st_size,
                "is_image": is_image,
            })
            logger.info("Chat upload: %s (%d bytes)", filename, file_path.stat().st_size)

    return web.json_response({"status": "success", "attachments": saved})


async def chat_serve_file(request):
    """Serve an uploaded chat file."""
    filename = request.match_info["filename"]
    # Sanitize
    safe = Path(filename).name
    file_path = chat_store.uploads_dir / safe
    if not file_path.exists() or not file_path.is_file():
        return web.json_response({"error": "File not found"}, status=404)
    return web.FileResponse(file_path)


async def chat_send(request):
    """Send a message and get AI response."""
    app = request.app
    agent = app.get("agent")
    bot = app.get("bot")

    try:
        body = await request.json()
        thread_id = body.get("thread_id")
        message = body.get("message", "").strip()
        attachments = body.get("attachments")  # list of attachment dicts
    except Exception:
        return web.json_response({"error": "Invalid request body"}, status=400)

    if not thread_id or not message:
        return web.json_response({"error": "thread_id and message required"}, status=400)

    # Verify thread exists
    thread = chat_store.get_thread(thread_id)
    if thread is None:
        return web.json_response({"error": "Thread not found"}, status=404)

    # Add user message
    user_msg = chat_store.add_message(thread_id, "user", message,
                                      attachments=attachments,
                                      source="dashboard")

    # Build the message to send to the AI (include attachment info)
    ai_message = message
    if attachments:
        file_list = ", ".join(a["name"] for a in attachments)
        ai_message += f"\n\n[Attached files: {file_list}]"

    # Get AI response
    agent_chat_id = thread.get("agent_chat_id",
                               hash(thread_id) % 1_000_000 + 900_000)

    async def _process_in_background():
        if agent:
            try:
                result = await agent.process_message(
                    agent_chat_id, ai_message,
                    project_name=f"dashboard_{thread_id}",
                    return_trace=True
                )
                if isinstance(result, dict):
                    response_text = result["response"]
                    trace_data = result.get("trace", [])
                else:
                    response_text = result
                    trace_data = []
            except Exception as e:
                logger.error("Agent error: %s", e)
                response_text = f"⚠️ Agent error: {type(e).__name__}: {e}"
                trace_data = []
        else:
            response_text = ("🔌 Agent is not connected to the dashboard. "
                             "The chat will work once the full system is started via `main.py`.")
            trace_data = []

        # Add assistant message with trace
        assistant_msg = chat_store.add_message(thread_id, "assistant", response_text,
                                               source="dashboard",
                                               trace=trace_data if trace_data else None)

        # ─── Discord Sync: push messages to Discord channel (on first message) ───
        if bot:
            try:
                await _sync_to_discord(bot, thread, thread_id,
                                       message, response_text, attachments)
            except Exception as e:
                logger.error("Discord sync failed: %s", e)

    # Launch processing as a background task to avoid HTTP timeout (e.g. Cloudflare 524)
    asyncio.create_task(_process_in_background())

    return web.json_response({
        "status": "processing",
        "user_message": user_msg,
    }, status=202)


async def _sync_to_discord(bot, thread, thread_id, user_message,
                           ai_response, attachments=None):
    """Sync a dashboard chat exchange to Discord by creating a dedicated channel."""
    import discord
    import re

    discord_bot = bot.bot  # The discord.py Bot instance
    if not discord_bot.is_ready() or not discord_bot.guilds:
        return

    discord_channel_id = thread.get("discord_channel_id")

    if discord_channel_id:
        # Channel already linked — send to existing Discord channel
        channel = discord_bot.get_channel(discord_channel_id)
        if not channel:
            try:
                channel = await discord_bot.fetch_channel(discord_channel_id)
            except Exception:
                logger.warning("Discord channel %d not found", discord_channel_id)
                return
    else:
        # Create a new dedicated text channel at top level (no category)
        guild = discord_bot.guilds[0]

        # Sanitize channel name (Discord allows lowercase, numbers, hyphens)
        raw_title = thread.get("title", "dashboard-chat")[:80]
        safe_name = re.sub(r'[^a-z0-9\-]', '-', raw_title.lower()).strip('-') or "dashboard-chat"
        safe_name = f"mystic-{safe_name}"

        try:
            channel = await guild.create_text_channel(
                name=safe_name,
                topic=f"Mystic Dashboard chat — synced from web UI (thread: {thread_id})",
            )
            # Store the mapping
            chat_store.set_discord_channel(thread_id, channel.id)
            logger.info("Created Discord channel #%s (%d) for dashboard thread %s",
                        channel.name, channel.id, thread_id)
        except Exception as e:
            logger.error("Failed to create Discord channel: %s", e)
            return

    # Send user message
    try:
        user_text = f"**[Dashboard]** {user_message}"
        if len(user_text) > 1900:
            user_text = user_text[:1900] + "..."

        # Handle attachments
        files = []
        if attachments:
            for att in attachments:
                fp = chat_store.uploads_dir / att.get("stored_name", "")
                if fp.exists():
                    files.append(discord.File(str(fp), filename=att["name"]))

        await channel.send(user_text, files=files if files else None)
    except Exception as e:
        logger.error("Failed to send user message to Discord: %s", e)

    # Send AI response
    try:
        if len(ai_response) <= 1900:
            await channel.send(ai_response)
        else:
            for i in range(0, len(ai_response), 1900):
                await channel.send(ai_response[i:i+1900])
    except Exception as e:
        logger.error("Failed to send AI response to Discord: %s", e)


# ─── Skills Endpoint ─────────────────────────────────────────────

async def list_skills(request):
    """List all loaded skills."""
    agent = request.app.get("agent")
    skills = []

    if agent:
        for s in agent.skills.skills:
            params = []
            for k, v in s.parameters.get("properties", {}).items():
                params.append({
                    "name": k,
                    "type": v.get("type", "string"),
                    "description": v.get("description", ""),
                })
            skills.append({
                "name": s.name,
                "description": s.description,
                "parameters": params,
            })
    else:
        # Fallback: list skill files
        for skill_file in ["code_executor", "file_manager", "web_fetcher", "system_info"]:
            skills.append({
                "name": skill_file,
                "description": f"Built-in skill: {skill_file.replace('_', ' ').title()}",
                "parameters": [],
            })

    return web.json_response(skills)


# ─── File Management (Cloud Storage) ─────────────────────────────

def _cloud_root() -> Path:
    """Root directory for cloud file storage."""
    root = config.data_dir / "cloud"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_path(rel_path: str) -> Path | None:
    """Resolve a relative path safely within the cloud root."""
    root = _cloud_root()
    try:
        target = (root / rel_path).resolve()
        if not str(target).startswith(str(root.resolve())):
            return None
        return target
    except Exception:
        return None


async def list_files(request):
    """List files in a directory within cloud storage."""
    rel_path = request.query.get("path", "")
    target = _safe_path(rel_path) if rel_path else _cloud_root()

    if target is None or not target.exists():
        return web.json_response({"error": "Path not found"}, status=404)

    if not target.is_dir():
        return web.json_response({"error": "Not a directory"}, status=400)

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            rel = str(entry.relative_to(_cloud_root())).replace("\\", "/")
            item = {
                "name": entry.name,
                "path": rel,
                "is_dir": entry.is_dir(),
            }
            if entry.is_file():
                stat = entry.stat()
                item["size"] = stat.st_size
                item["modified"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            elif entry.is_dir():
                item["children_count"] = sum(1 for _ in entry.iterdir() if not _.name.startswith("."))
            items.append(item)
    except PermissionError:
        return web.json_response({"error": "Permission denied"}, status=403)

    # Calculate total storage used
    total_bytes = sum(f.stat().st_size for f in _cloud_root().rglob("*") if f.is_file())

    return web.json_response({
        "path": rel_path or "/",
        "items": items,
        "total_storage_bytes": total_bytes,
    })


async def upload_file(request):
    """Upload a file to cloud storage."""
    reader = await request.multipart()
    rel_dir = ""
    saved_files = []

    async for part in reader:
        if part.name == "path":
            rel_dir = (await part.text()).strip()
        elif part.name == "file" or part.name == "files":
            filename = part.filename
            if not filename:
                continue

            target_dir = _safe_path(rel_dir) if rel_dir else _cloud_root()
            if target_dir is None:
                continue
            target_dir.mkdir(parents=True, exist_ok=True)

            # Sanitize filename
            safe_name = Path(filename).name
            file_path = target_dir / safe_name

            # Read and save
            with open(file_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk:
                        break
                    f.write(chunk)

            saved_files.append({
                "name": safe_name,
                "path": str(file_path.relative_to(_cloud_root())).replace("\\", "/"),
                "size": file_path.stat().st_size,
            })
            logger.info("Uploaded file: %s", file_path)

    return web.json_response({"status": "success", "files": saved_files})


async def download_file(request):
    """Download a file from cloud storage."""
    rel_path = request.query.get("path", "")
    if not rel_path:
        return web.json_response({"error": "path required"}, status=400)

    target = _safe_path(rel_path)
    if target is None or not target.exists() or not target.is_file():
        return web.json_response({"error": "File not found"}, status=404)

    return web.FileResponse(target, headers={
        "Content-Disposition": f'attachment; filename="{target.name}"'
    })


async def delete_file(request):
    """Delete a file or directory from cloud storage."""
    try:
        body = await request.json()
        rel_path = body.get("path", "")
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    target = _safe_path(rel_path)
    if target is None or not target.exists():
        return web.json_response({"error": "Not found"}, status=404)

    # Prevent deleting root
    if target.resolve() == _cloud_root().resolve():
        return web.json_response({"error": "Cannot delete root"}, status=400)

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    logger.info("Deleted: %s", target)
    return web.json_response({"status": "deleted"})


async def create_folder(request):
    """Create a new folder in cloud storage."""
    try:
        body = await request.json()
        rel_path = body.get("path", "")
        name = body.get("name", "").strip()
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    if not name:
        return web.json_response({"error": "name required"}, status=400)

    parent = _safe_path(rel_path) if rel_path else _cloud_root()
    if parent is None:
        return web.json_response({"error": "Invalid parent path"}, status=400)

    new_dir = parent / name
    new_dir.mkdir(parents=True, exist_ok=True)
    return web.json_response({"status": "created", "path": str(new_dir.relative_to(_cloud_root())).replace("\\", "/")})


async def rename_file(request):
    """Rename a file or folder."""
    try:
        body = await request.json()
        old_path = body.get("path", "")
        new_name = body.get("new_name", "").strip()
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    if not old_path or not new_name:
        return web.json_response({"error": "path and new_name required"}, status=400)

    source = _safe_path(old_path)
    if source is None or not source.exists():
        return web.json_response({"error": "Not found"}, status=404)

    dest = source.parent / Path(new_name).name
    source.rename(dest)
    return web.json_response({
        "status": "renamed",
        "new_path": str(dest.relative_to(_cloud_root())).replace("\\", "/"),
    })


# ─── Terminal Endpoint ───────────────────────────────────────────

async def terminal_exec(request):
    """Execute a shell command and return the output."""
    try:
        body = await request.json()
        command = body.get("command", "").strip()
        cwd = body.get("cwd", str(config.data_dir / "workspace"))
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    if not command:
        return web.json_response({"error": "command required"}, status=400)

    # Ensure cwd exists
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        cwd_path.mkdir(parents=True, exist_ok=True)

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd_path),
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return web.json_response({
                "exit_code": -1,
                "stdout": "",
                "stderr": "Command timed out (120s limit)",
                "command": command,
            })

        return web.json_response({
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "command": command,
        })
    except Exception as e:
        logger.error("Terminal exec error: %s", e)
        return web.json_response({
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "command": command,
        })


# ─── MCP Server Endpoints ────────────────────────────────────────

async def mcp_list_servers(request):
    """List all MCP server configurations and their status."""
    agent = request.app.get("agent")
    if agent and hasattr(agent, "mcp"):
        return web.json_response({"servers": agent.mcp.get_status()})
    return web.json_response({"servers": []})


async def mcp_add_server(request):
    """Add a new MCP server configuration."""
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    required = ["name", "command"]
    for field in required:
        if not body.get(field):
            return web.json_response({"error": f"{field} is required"}, status=400)

    cfg = agent.mcp.add_server(body)
    return web.json_response({"status": "added", "server": cfg.to_dict()})


async def mcp_remove_server(request):
    """Remove an MCP server configuration."""
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)

    server_id = request.match_info["server_id"]

    # Disconnect first if connected
    await agent.mcp.disconnect_server(server_id)

    if agent.mcp.remove_server(server_id):
        return web.json_response({"status": "removed"})
    return web.json_response({"error": "Server not found"}, status=404)


async def mcp_connect_server(request):
    """Connect to an MCP server."""
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)

    server_id = request.match_info["server_id"]
    try:
        success = await agent.mcp.connect_server(server_id)
        if success:
            return web.json_response({"status": "connected"})
        return web.json_response({"error": "Connection failed"}, status=500)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def mcp_disconnect_server(request):
    """Disconnect from an MCP server."""
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)

    server_id = request.match_info["server_id"]
    success = await agent.mcp.disconnect_server(server_id)
    if success:
        return web.json_response({"status": "disconnected"})
    return web.json_response({"error": "Server not connected"}, status=404)


async def mcp_server_tools(request):
    """List tools from a connected MCP server."""
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)

    server_id = request.match_info["server_id"]
    conn = agent.mcp.connections.get(server_id)
    if not conn or not conn.connected:
        return web.json_response({"error": "Server not connected"}, status=404)

    tools = []
    for t in conn.tools:
        tools.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "schema": t.get("inputSchema", {}),
        })
    return web.json_response({"tools": tools})


# ─── Provider Endpoints ──────────────────────────────────────────

def _load_providers() -> list[dict]:
    """Load providers from config file."""
    if providers_config_path.exists():
        try:
            return json.loads(providers_config_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_providers(providers: list[dict]):
    """Save providers to config file."""
    providers_config_path.parent.mkdir(parents=True, exist_ok=True)
    providers_config_path.write_text(json.dumps(providers, indent=2), encoding="utf-8")


async def get_providers(request):
    """Get all provider configurations."""
    return web.json_response(_load_providers())


async def save_provider(request):
    """Add or update a provider."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    providers = _load_providers()

    provider_id = body.get("id")
    if provider_id:
        # Update existing
        for i, p in enumerate(providers):
            if p.get("id") == provider_id:
                providers[i] = {**p, **body}
                break
        else:
            providers.append(body)
    else:
        import uuid
        body["id"] = str(uuid.uuid4())[:8]
        providers.append(body)

    _save_providers(providers)
    return web.json_response({"status": "saved", "provider": body})


async def delete_provider(request):
    """Delete a provider."""
    provider_id = request.match_info["provider_id"]
    providers = _load_providers()
    new_list = [p for p in providers if p.get("id") != provider_id]
    if len(new_list) == len(providers):
        return web.json_response({"error": "Provider not found"}, status=404)
    _save_providers(new_list)
    return web.json_response({"status": "deleted"})


# ─── System Status ────────────────────────────────────────────────

async def system_status(request):
    """Get system status information."""
    try:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        cpu_pct = psutil.cpu_percent(interval=0.1)
    except Exception:
        mem = None
        disk = None
        cpu_pct = 0

    uptime_seconds = int(time.time() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return web.json_response({
        "status": "online",
        "uptime": f"{hours}h {minutes}m {seconds}s",
        "uptime_seconds": uptime_seconds,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_percent": cpu_pct,
        "memory": {
            "total_mb": round(mem.total / 1048576) if mem else 0,
            "used_mb": round(mem.used / 1048576) if mem else 0,
            "percent": mem.percent if mem else 0,
        } if mem else None,
        "disk": {
            "total_gb": round(disk.total / 1073741824, 1) if disk else 0,
            "used_gb": round(disk.used / 1073741824, 1) if disk else 0,
            "percent": disk.percent if disk else 0,
        } if disk else None,
        "tunnel": tunnel_mgr.get_status(),
    })


# ─── Tunnel Control ──────────────────────────────────────────────

async def tunnel_status(request):
    """Get tunnel status (no auth required — used by login screen)."""
    return web.json_response(tunnel_mgr.get_status())


async def tunnel_start(request):
    """Start the cloud tunnel."""
    try:
        url = await tunnel_mgr.start()
        return web.json_response({"status": "started", "url": url})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def tunnel_stop(request):
    """Stop the cloud tunnel."""
    await tunnel_mgr.stop()
    return web.json_response({"status": "stopped"})


# ─── App Factory ──────────────────────────────────────────────────

def create_app(agent=None, bot=None):
    """Create the aiohttp web application.

    Args:
        agent: Optional Agent instance for chat functionality.
        bot: Optional NIMBot instance for Discord sync.
    """
    app = web.Application(middlewares=[auth_middleware],
                          client_max_size=0)  # No upload size limit

    # Store references
    if agent:
        app["agent"] = agent
    if bot:
        app["bot"] = bot

    static_dir = Path(__file__).parent / "static"

    # Auth
    app.router.add_get("/api/auth/check", auth_check)
    app.router.add_post("/api/auth/login", auth_login)

    # Models
    app.router.add_get("/api/models", get_models)
    app.router.add_post("/api/models", update_models)

    # Chat
    app.router.add_get("/api/threads", list_threads)
    app.router.add_post("/api/threads", create_thread)
    app.router.add_get("/api/threads/{thread_id}", get_thread)
    app.router.add_delete("/api/threads/{thread_id}", delete_thread)
    app.router.add_post("/api/chat", chat_send)
    app.router.add_post("/api/chat/upload", chat_upload)
    app.router.add_get("/api/chat/files/{filename}", chat_serve_file)

    # Skills
    app.router.add_get("/api/skills", list_skills)

    # Files (Cloud Storage)
    app.router.add_get("/api/files", list_files)
    app.router.add_post("/api/files/upload", upload_file)
    app.router.add_get("/api/files/download", download_file)
    app.router.add_post("/api/files/delete", delete_file)
    app.router.add_post("/api/files/folder", create_folder)
    app.router.add_post("/api/files/rename", rename_file)

    # Terminal
    app.router.add_post("/api/terminal/exec", terminal_exec)

    # MCP
    app.router.add_get("/api/mcp/servers", mcp_list_servers)
    app.router.add_post("/api/mcp/servers", mcp_add_server)
    app.router.add_delete("/api/mcp/servers/{server_id}", mcp_remove_server)
    app.router.add_post("/api/mcp/servers/{server_id}/connect", mcp_connect_server)
    app.router.add_post("/api/mcp/servers/{server_id}/disconnect", mcp_disconnect_server)
    app.router.add_get("/api/mcp/servers/{server_id}/tools", mcp_server_tools)

    # Providers
    app.router.add_get("/api/providers", get_providers)
    app.router.add_post("/api/providers", save_provider)
    app.router.add_delete("/api/providers/{provider_id}", delete_provider)

    # System
    app.router.add_get("/api/status", system_status)

    # Tunnel
    app.router.add_get("/api/tunnel/status", tunnel_status)
    app.router.add_post("/api/tunnel/start", tunnel_start)
    app.router.add_post("/api/tunnel/stop", tunnel_stop)

    # Serve index.html for root
    async def index_handler(request):
        return web.FileResponse(static_dir / "index.html")

    app.router.add_get("/", index_handler)
    app.router.add_static("/static/", path=static_dir, name="static")

    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8080)
