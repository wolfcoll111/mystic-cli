"""
Mystic Dashboard — Backend API Server.
Serves the Codex-style web UI and provides APIs for chat, file management,
model configuration, skills listing, system status, cloud tunnel control,
terminal execution, MCP server management, provider configuration, and commands.
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
from agent.config_manager import ConfigManager
from dashboard.chat_store import ChatStore
from dashboard.tunnel import TunnelManager

logger = logging.getLogger("nim_agent.dashboard")

config = Config()
cfg_mgr = ConfigManager(config)
chat_store = ChatStore(config.data_dir)
tunnel_mgr = TunnelManager(config.data_dir, port=8080)
models_config_path = config.data_dir / "models_config.json"
providers_config_path = config.providers_config_path
_start_time = time.time()

# ─── Auth Middleware ──────────────────────────────────────────────

@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if path in ("/api/auth/check", "/api/auth/login", "/api/tunnel/status"):
        return await handler(request)
    if path.startswith("/static/"):
        return await handler(request)
    if path.startswith("/api/chat/files/"):
        return await handler(request)

    api_key = (
        request.cookies.get("mystic_session")
        or request.query.get("key")
        or request.headers.get("X-Mystic-Key")
    )

    if path == "/":
        if request.query.get("key") and tunnel_mgr.validate_key(request.query["key"]):
            resp = web.HTTPFound("/")
            resp.set_cookie("mystic_session", request.query["key"], max_age=86400*30, httponly=True, samesite="Lax")
            return resp
        return await handler(request)

    if path.startswith("/api/"):
        if api_key and tunnel_mgr.validate_key(api_key):
            return await handler(request)
        return web.json_response({"error": "unauthorized"}, status=401)

    return await handler(request)


# ─── Auth Endpoints ──────────────────────────────────────────────

async def auth_check(request):
    key = (request.cookies.get("mystic_session") or request.query.get("key") or request.headers.get("X-Mystic-Key"))
    if key and tunnel_mgr.validate_key(key):
        return web.json_response({"authenticated": True})
    return web.json_response({"authenticated": False})


async def auth_login(request):
    try:
        body = await request.json()
        key = body.get("key", "")
    except Exception:
        key = request.query.get("key", "")
    if tunnel_mgr.validate_key(key):
        resp = web.json_response({"authenticated": True})
        resp.set_cookie("mystic_session", key, max_age=86400*30, httponly=True, samesite="Lax")
        return resp
    return web.json_response({"authenticated": False, "error": "Invalid API key"}, status=401)


# ─── Models Endpoints ────────────────────────────────────────────

async def get_models(request):
    return web.json_response(cfg_mgr.get_models())


async def update_models(request):
    try:
        new_config = await request.json()
        result = cfg_mgr.update_models(new_config)
        # Reload swarm if agent is available
        agent = request.app.get("agent")
        if agent and hasattr(agent, 'swarm'):
            agent.swarm.reload_config()
        return web.json_response({"status": "success", "models": result})
    except Exception as e:
        logger.error("Error updating models: %s", e)
        return web.json_response({"status": "error", "message": str(e)}, status=400)


# ─── Model-Provider Mapping Endpoints ────────────────────────────

async def get_model_providers(request):
    return web.json_response(cfg_mgr.get_model_providers())


async def set_model_provider(request):
    try:
        body = await request.json()
        role = body.get("role")
        provider_id = body.get("provider_id")
        if not role:
            return web.json_response({"error": "role required"}, status=400)
        cfg_mgr.set_model_provider(role, provider_id or "")
        agent = request.app.get("agent")
        if agent and hasattr(agent, 'swarm'):
            agent.swarm.reload_config()
        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


# ─── Command Endpoint (for web slash commands) ───────────────────

async def handle_command(request):
    """
    Process slash commands from the web dashboard.
    Supports: /model, /add, /apikey, /endpoint, /providers, /reload, /models, /help, /status
    """
    try:
        body = await request.json()
        message = body.get("message", "").strip()
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    if not message.startswith("/"):
        return web.json_response({"error": "Not a command"}, status=400)

    parts = message[1:].split()
    cmd = parts[0].lower() if parts else ""

    response_parts = []

    if cmd == "help":
        response_parts.append("**Available Web Commands:**")
        response_parts.append("`/help` \u2014 Show this help")
        response_parts.append("`/models` \u2014 Show current model lineup")
        response_parts.append("`/model <role> <name>` \u2014 Set model for a role")
        response_parts.append("`/add model <role> <name>` \u2014 Add/update model")
        response_parts.append("`/providers` \u2014 List providers")
        response_parts.append("`/apikey <name_or_id> <key>` \u2014 Update API key")
        response_parts.append("`/endpoint <name_or_id> <url>` \u2014 Update endpoint")
        response_parts.append("`/reload` \u2014 Reload config from disk")
        response_parts.append("`/status` \u2014 System status")

    elif cmd == "models":
        models = cfg_mgr.get_models()
        response_parts.append("**Model Lineup**\n")
        for role, model in models.items():
            resolved = cfg_mgr.resolve_provider_for_role(role)
            prov_name = resolved.get("name", "default") if resolved else "default"
            response_parts.append(f"\u2022 **{role}**: `{model}` (provider: `{prov_name}`)")

    elif cmd == "model":
        if len(parts) < 3:
            response_parts.append("Usage: `/model <role> <model_name>`\nRoles: overseer, architect, coder, verifier, agent")
        else:
            role = parts[1].lower()
            model_name = " ".join(parts[2:])
            valid = ("overseer", "architect", "coder", "verifier", "agent")
            if role not in valid:
                response_parts.append(f"Invalid role: `{role}`. Valid: {', '.join(valid)}")
            else:
                cfg_mgr.update_models({role: model_name})
                agent = request.app.get("agent")
                if agent and hasattr(agent, 'swarm'):
                    agent.swarm.reload_config()
                response_parts.append(f"Model for `{role}` set to `{model_name}`")

    elif cmd == "add":
        if len(parts) < 4 or parts[1].lower() != "model":
            response_parts.append("Usage: `/add model <role> <model_name>`")
        else:
            role = parts[2].lower()
            model_name = " ".join(parts[3:])
            valid = ("overseer", "architect", "coder", "verifier", "agent")
            if role not in valid:
                response_parts.append(f"Invalid role: `{role}`. Valid: {', '.join(valid)}")
            else:
                cfg_mgr.update_models({role: model_name})
                agent = request.app.get("agent")
                if agent and hasattr(agent, 'swarm'):
                    agent.swarm.reload_config()
                response_parts.append(f"Model `{role}` set to `{model_name}`")

    elif cmd == "providers":
        providers = cfg_mgr.get_providers()
        if not providers:
            response_parts.append("No providers configured.")
        else:
            response_parts.append("**Configured Providers**\n")
            for p in providers:
                pid = p.get("id", "?")
                name = p.get("name", "Unnamed")
                url = p.get("base_url", "?")
                key = p.get("api_key", "")
                masked = key[:6] + "..." + key[-4:] if len(key) > 10 else "***" if key else "No key"
                response_parts.append(f"\u2022 **{name}** (`{pid}`)")
                response_parts.append(f"  URL: `{url}`  Key: `{masked}`")

    elif cmd == "apikey":
        if len(parts) < 3:
            response_parts.append("Usage: `/apikey <provider_name_or_id> <new_key>`")
        else:
            provider = parts[1]
            new_key = " ".join(parts[2:])
            success = cfg_mgr.update_provider_key(provider, new_key)
            if success:
                agent = request.app.get("agent")
                if agent and hasattr(agent, 'swarm'):
                    agent.swarm.reload_config()
                masked = new_key[:6] + "..." + new_key[-4:] if len(new_key) > 10 else "***"
                response_parts.append(f"API key for `{provider}` updated to `{masked}`")
            else:
                response_parts.append(f"Provider `{provider}` not found.")

    elif cmd == "endpoint":
        if len(parts) < 3:
            response_parts.append("Usage: `/endpoint <provider_name_or_id> <url>`")
        else:
            provider = parts[1]
            new_url = " ".join(parts[2:])
            success = cfg_mgr.update_provider_endpoint(provider, new_url)
            if success:
                agent = request.app.get("agent")
                if agent and hasattr(agent, 'swarm'):
                    agent.swarm.reload_config()
                response_parts.append(f"Endpoint for `{provider}` set to `{new_url}`")
            else:
                response_parts.append(f"Provider `{provider}` not found.")

    elif cmd == "reload":
        global cfg_mgr
        cfg_mgr = ConfigManager(config)
        agent = request.app.get("agent")
        if agent and hasattr(agent, 'swarm'):
            agent.swarm.reload_config()
        response_parts.append("Config reloaded from disk.")

    elif cmd == "status":
        status_data = await _system_status_data()
        response_parts.append(f"**System Status**")
        response_parts.append(f"Uptime: {status_data.get('uptime', '?')}")
        response_parts.append(f"CPU: {status_data.get('cpu_percent', '?')}%")
        if status_data.get("memory"):
            response_parts.append(f"Memory: {status_data['memory']['used_mb']}/{status_data['memory']['total_mb']} MB")
        if status_data.get("disk"):
            response_parts.append(f"Disk: {status_data['disk']['used_gb']}/{status_data['disk']['total_gb']} GB")

    else:
        response_parts.append(f"Unknown command: `/{cmd}`. Try `/help`.")

    return web.json_response({"response": "\n".join(response_parts), "command": cmd})


# ─── Chat Endpoints ──────────────────────────────────────────────

async def list_threads(request):
    return web.json_response(chat_store.list_threads())


async def create_thread(request):
    try:
        body = await request.json()
        title = body.get("title", "New thread")
    except Exception:
        title = "New thread"
    thread = chat_store.create_thread(title)
    return web.json_response(thread)


async def get_thread(request):
    thread_id = request.match_info["thread_id"]
    thread = chat_store.get_thread(thread_id)
    if thread is None:
        return web.json_response({"error": "Thread not found"}, status=404)
    return web.json_response(thread)


async def delete_thread(request):
    thread_id = request.match_info["thread_id"]
    if chat_store.delete_thread(thread_id):
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Thread not found"}, status=404)


async def chat_upload(request):
    reader = await request.multipart()
    saved = []
    async for part in reader:
        if part.name in ("file", "files"):
            filename = part.filename
            if not filename:
                continue
            ext = Path(filename).suffix
            safe_name = f"{int(time.time()*1000)}_{filename}"
            file_path = chat_store.uploads_dir / safe_name
            with open(file_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk: break
                    f.write(chunk)
            img_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
            is_image = ext.lower() in img_exts
            saved.append({
                "name": filename, "stored_name": safe_name, "url": f"/api/chat/files/{safe_name}",
                "size": file_path.stat().st_size, "is_image": is_image,
            })
    return web.json_response({"status": "success", "attachments": saved})


async def chat_serve_file(request):
    filename = request.match_info["filename"]
    safe = Path(filename).name
    file_path = chat_store.uploads_dir / safe
    if not file_path.exists() or not file_path.is_file():
        return web.json_response({"error": "File not found"}, status=404)
    return web.FileResponse(file_path)


async def chat_send(request):
    app = request.app
    agent = app.get("agent")
    bot = app.get("bot")

    try:
        body = await request.json()
        thread_id = body.get("thread_id")
        message = body.get("message", "").strip()
        attachments = body.get("attachments")
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)

    if not thread_id or not message:
        return web.json_response({"error": "thread_id and message required"}, status=400)

    # Check if this is a slash command
    if message.startswith("/"):
        cmd_result = await handle_command(request)
        cmd_data = json.loads(cmd_result.body.decode())
        # Return command result as a one-shot response
        thread = chat_store.get_thread(thread_id)
        if thread:
            chat_store.add_message(thread_id, "user", message, source="dashboard")
            chat_store.add_message(thread_id, "assistant", cmd_data.get("response", ""), source="dashboard")
        return web.json_response({
            "status": "done",
            "user_message": {"role": "user", "content": message},
            "assistant_message": {"role": "assistant", "content": cmd_data.get("response", "")},
        })

    thread = chat_store.get_thread(thread_id)
    if thread is None:
        return web.json_response({"error": "Thread not found"}, status=404)

    user_msg = chat_store.add_message(thread_id, "user", message, attachments=attachments, source="dashboard")

    ai_message = message
    if attachments:
        file_list = ", ".join(a["name"] for a in attachments)
        ai_message += f"\n\n[Attached files: {file_list}]"

    agent_chat_id = thread.get("agent_chat_id", hash(thread_id) % 1_000_000 + 900_000)

    async def _process_in_background():
        if agent:
            try:
                result = await agent.process_message(agent_chat_id, ai_message, project_name=f"dashboard_{thread_id}", return_trace=True)
                if isinstance(result, dict):
                    response_text = result["response"]
                    trace_data = result.get("trace", [])
                else:
                    response_text = result
                    trace_data = []
            except Exception as e:
                logger.error("Agent error: %s", e)
                response_text = f"Agent error: {type(e).__name__}: {e}"
                trace_data = []
        else:
            response_text = "Agent is not connected. Start via `main.py`."
            trace_data = []

        assistant_msg = chat_store.add_message(thread_id, "assistant", response_text, source="dashboard", trace=trace_data if trace_data else None)

        if bot:
            try:
                await _sync_to_discord(bot, thread, thread_id, message, response_text, attachments)
            except Exception as e:
                logger.error("Discord sync failed: %s", e)

    asyncio.create_task(_process_in_background())

    return web.json_response({"status": "processing", "user_message": user_msg}, status=202)


async def _sync_to_discord(bot, thread, thread_id, user_message, ai_response, attachments=None):
    import discord
    import re
    discord_bot = bot.bot
    if not discord_bot.is_ready() or not discord_bot.guilds:
        return
    discord_channel_id = thread.get("discord_channel_id")
    if discord_channel_id:
        channel = discord_bot.get_channel(discord_channel_id)
        if not channel:
            try: channel = await discord_bot.fetch_channel(discord_channel_id)
            except Exception: return
    else:
        guild = discord_bot.guilds[0]
        raw_title = thread.get("title", "dashboard-chat")[:80]
        safe_name = re.sub(r'[^a-z0-9\-]', '-', raw_title.lower()).strip('-') or "dashboard-chat"
        safe_name = f"mystic-{safe_name}"
        try:
            channel = await guild.create_text_channel(name=safe_name, topic=f"Mystic sync (thread: {thread_id})")
            chat_store.set_discord_channel(thread_id, channel.id)
        except Exception:
            return
    try:
        user_text = f"**[Dashboard]** {user_message}"
        if len(user_text) > 1900: user_text = user_text[:1900] + "..."
        files = []
        if attachments:
            for att in attachments:
                fp = chat_store.uploads_dir / att.get("stored_name", "")
                if fp.exists(): files.append(discord.File(str(fp), filename=att["name"]))
        await channel.send(user_text, files=files if files else None)
    except Exception as e:
        logger.error("Failed to send to Discord: %s", e)
    try:
        if len(ai_response) <= 1900:
            await channel.send(ai_response)
        else:
            for i in range(0, len(ai_response), 1900):
                await channel.send(ai_response[i:i+1900])
    except Exception as e:
        logger.error("Failed to send AI to Discord: %s", e)


# ─── Skills Endpoint ─────────────────────────────────────────────

async def list_skills(request):
    agent = request.app.get("agent")
    skills = []
    if agent:
        for s in agent.skills.skills:
            params = [{"name": k, "type": v.get("type", "string"), "description": v.get("description", "")} for k, v in s.parameters.get("properties", {}).items()]
            skills.append({"name": s.name, "description": s.description, "parameters": params})
    else:
        for skill_file in ["code_executor", "file_manager", "web_fetcher", "system_info"]:
            skills.append({"name": skill_file, "description": f"Built-in: {skill_file.replace('_', ' ').title()}", "parameters": []})
    return web.json_response(skills)


# ─── File Management (Cloud Storage) ─────────────────────────────

def _cloud_root() -> Path:
    root = config.data_dir / "cloud"
    root.mkdir(parents=True, exist_ok=True)
    return root

def _safe_path(rel_path: str) -> Path | None:
    root = _cloud_root()
    try:
        target = (root / rel_path).resolve()
        if not str(target).startswith(str(root.resolve())):
            return None
        return target
    except Exception:
        return None

async def list_files(request):
    rel_path = request.query.get("path", "")
    target = _safe_path(rel_path) if rel_path else _cloud_root()
    if target is None or not target.exists():
        return web.json_response({"error": "Path not found"}, status=404)
    if not target.is_dir():
        return web.json_response({"error": "Not a directory"}, status=400)
    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."): continue
            rel = str(entry.relative_to(_cloud_root())).replace("\\", "/")
            item = {"name": entry.name, "path": rel, "is_dir": entry.is_dir()}
            if entry.is_file():
                stat = entry.stat()
                item["size"] = stat.st_size
                item["modified"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            elif entry.is_dir():
                item["children_count"] = sum(1 for _ in entry.iterdir() if not _.name.startswith("."))
            items.append(item)
    except PermissionError:
        return web.json_response({"error": "Permission denied"}, status=403)
    total_bytes = sum(f.stat().st_size for f in _cloud_root().rglob("*") if f.is_file())
    return web.json_response({"path": rel_path or "/", "items": items, "total_storage_bytes": total_bytes})


async def upload_file(request):
    reader = await request.multipart()
    rel_dir = ""
    saved_files = []
    async for part in reader:
        if part.name == "path":
            rel_dir = (await part.text()).strip()
        elif part.name in ("file", "files"):
            filename = part.filename
            if not filename: continue
            target_dir = _safe_path(rel_dir) if rel_dir else _cloud_root()
            if target_dir is None: continue
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_name = Path(filename).name
            file_path = target_dir / safe_name
            with open(file_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk: break
                    f.write(chunk)
            saved_files.append({"name": safe_name, "path": str(file_path.relative_to(_cloud_root())).replace("\\", "/"), "size": file_path.stat().st_size})
    return web.json_response({"status": "success", "files": saved_files})


async def download_file(request):
    rel_path = request.query.get("path", "")
    if not rel_path:
        return web.json_response({"error": "path required"}, status=400)
    target = _safe_path(rel_path)
    if target is None or not target.exists() or not target.is_file():
        return web.json_response({"error": "File not found"}, status=404)
    return web.FileResponse(target, headers={"Content-Disposition": f'attachment; filename="{target.name}"'})


async def delete_file(request):
    try:
        body = await request.json()
        rel_path = body.get("path", "")
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)
    target = _safe_path(rel_path)
    if target is None or not target.exists():
        return web.json_response({"error": "Not found"}, status=404)
    if target.resolve() == _cloud_root().resolve():
        return web.json_response({"error": "Cannot delete root"}, status=400)
    if target.is_dir(): shutil.rmtree(target)
    else: target.unlink()
    return web.json_response({"status": "deleted"})


async def create_folder(request):
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
        return web.json_response({"error": "Invalid parent"}, status=400)
    new_dir = parent / name
    new_dir.mkdir(parents=True, exist_ok=True)
    return web.json_response({"status": "created", "path": str(new_dir.relative_to(_cloud_root())).replace("\\", "/")})


async def rename_file(request):
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
    return web.json_response({"status": "renamed", "new_path": str(dest.relative_to(_cloud_root())).replace("\\", "/")})


# ─── Terminal Endpoint ───────────────────────────────────────────

async def terminal_exec(request):
    try:
        body = await request.json()
        command = body.get("command", "").strip()
        cwd = body.get("cwd", str(config.data_dir / "workspace"))
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)
    if not command:
        return web.json_response({"error": "command required"}, status=400)
    cwd_path = Path(cwd)
    if not cwd_path.exists(): cwd_path.mkdir(parents=True, exist_ok=True)
    try:
        process = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(cwd_path))
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return web.json_response({"exit_code": -1, "stdout": "", "stderr": "Timed out (120s)", "command": command})
        return web.json_response({"exit_code": process.returncode, "stdout": stdout.decode("utf-8", errors="replace"), "stderr": stderr.decode("utf-8", errors="replace"), "command": command})
    except Exception as e:
        return web.json_response({"exit_code": -1, "stdout": "", "stderr": str(e), "command": command})


# ─── MCP Server Endpoints ────────────────────────────────────────

async def mcp_list_servers(request):
    agent = request.app.get("agent")
    if agent and hasattr(agent, "mcp"):
        return web.json_response({"servers": agent.mcp.get_status()})
    return web.json_response({"servers": []})


async def mcp_add_server(request):
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)
    for field in ("name", "command"):
        if not body.get(field):
            return web.json_response({"error": f"{field} is required"}, status=400)
    cfg = agent.mcp.add_server(body)
    return web.json_response({"status": "added", "server": cfg.to_dict()})


async def mcp_remove_server(request):
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)
    server_id = request.match_info["server_id"]
    await agent.mcp.disconnect_server(server_id)
    if agent.mcp.remove_server(server_id):
        return web.json_response({"status": "removed"})
    return web.json_response({"error": "Server not found"}, status=404)


async def mcp_connect_server(request):
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)
    server_id = request.match_info["server_id"]
    try:
        success = await agent.mcp.connect_server(server_id)
        if success: return web.json_response({"status": "connected"})
        return web.json_response({"error": "Connection failed"}, status=500)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def mcp_disconnect_server(request):
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)
    server_id = request.match_info["server_id"]
    success = await agent.mcp.disconnect_server(server_id)
    if success: return web.json_response({"status": "disconnected"})
    return web.json_response({"error": "Server not connected"}, status=404)


async def mcp_server_tools(request):
    agent = request.app.get("agent")
    if not agent or not hasattr(agent, "mcp"):
        return web.json_response({"error": "Agent not available"}, status=503)
    server_id = request.match_info["server_id"]
    conn = agent.mcp.connections.get(server_id)
    if not conn or not conn.connected:
        return web.json_response({"error": "Server not connected"}, status=404)
    tools = [{"name": t["name"], "description": t.get("description", ""), "schema": t.get("inputSchema", {})} for t in conn.tools]
    return web.json_response({"tools": tools})


# ─── Provider Endpoints ──────────────────────────────────────────

async def get_providers(request):
    return web.json_response(cfg_mgr.get_providers())


async def save_provider(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid body"}, status=400)
    result = cfg_mgr.save_provider(body)
    return web.json_response({"status": "saved", "provider": result})


async def delete_provider(request):
    provider_id = request.match_info["provider_id"]
    if cfg_mgr.delete_provider(provider_id):
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Not found"}, status=404)


# ─── System Status ────────────────────────────────────────────────

async def _system_status_data() -> dict:
    try:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        cpu_pct = psutil.cpu_percent(interval=0.1)
    except Exception:
        mem = disk = None; cpu_pct = 0
    uptime_seconds = int(time.time() - _start_time)
    hours, rem = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return {
        "status": "online", "uptime": f"{hours}h {minutes}m {seconds}s", "uptime_seconds": uptime_seconds,
        "platform": platform.platform(), "python": platform.python_version(),
        "cpu_percent": cpu_pct,
        "memory": {"total_mb": round(mem.total/1048576), "used_mb": round(mem.used/1048576), "percent": mem.percent} if mem else None,
        "disk": {"total_gb": round(disk.total/1073741824, 1), "used_gb": round(disk.used/1073741824, 1), "percent": disk.percent} if disk else None,
        "tunnel": tunnel_mgr.get_status(),
    }


async def system_status(request):
    return web.json_response(await _system_status_data())


# ─── Tunnel Control ──────────────────────────────────────────────

async def tunnel_status(request):
    return web.json_response(tunnel_mgr.get_status())


async def tunnel_start(request):
    try:
        url = await tunnel_mgr.start()
        return web.json_response({"status": "started", "url": url})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def tunnel_stop(request):
    await tunnel_mgr.stop()
    return web.json_response({"status": "stopped"})


# ─── App Factory ──────────────────────────────────────────────────

def create_app(agent=None, bot=None):
    app = web.Application(middlewares=[auth_middleware], client_max_size=0)
    if agent: app["agent"] = agent
    if bot: app["bot"] = bot

    static_dir = Path(__file__).parent / "static"

    # Auth
    app.router.add_get("/api/auth/check", auth_check)
    app.router.add_post("/api/auth/login", auth_login)

    # Models
    app.router.add_get("/api/models", get_models)
    app.router.add_post("/api/models", update_models)

    # Model-Provider Mapping
    app.router.add_get("/api/model_providers", get_model_providers)
    app.router.add_post("/api/model_providers", set_model_provider)

    # Commands
    app.router.add_post("/api/command", handle_command)

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
