"""
MCP (Model Context Protocol) Client for Mystic.
Manages connections to MCP servers and exposes their tools to the agent.
Supports stdio transport (subprocess) and SSE transport (HTTP).
"""

import json
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("nim_agent.mcp_client")


class MCPServerConfig:
    """Configuration for a single MCP server."""

    def __init__(self, data: dict):
        self.id = data.get("id", str(uuid.uuid4())[:8])
        self.name = data.get("name", "Unnamed Server")
        self.transport = data.get("transport", "stdio")  # "stdio" or "sse"
        self.command = data.get("command", "")          # for stdio
        self.args = data.get("args", [])                # for stdio
        self.env = data.get("env", {})                  # for stdio
        self.url = data.get("url", "")                  # for sse
        self.enabled = data.get("enabled", True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "url": self.url,
            "enabled": self.enabled,
        }


class MCPConnection:
    """A live connection to an MCP server via stdio."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.tools: list[dict] = []
        self.connected = False
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    async def connect(self):
        """Start the MCP server subprocess and initialize."""
        if self.config.transport != "stdio":
            logger.warning("Only stdio transport is currently supported. Server: %s", self.config.name)
            return

        if not self.config.command:
            raise ValueError(f"No command specified for MCP server '{self.config.name}'")

        cmd_parts = [self.config.command] + self.config.args
        env = None
        if self.config.env:
            import os
            env = {**os.environ, **self.config.env}

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            logger.info("MCP server '%s' process started (PID: %d)", self.config.name, self.process.pid)

            # Start reading responses
            self._reader_task = asyncio.create_task(self._read_responses())

            # Send initialize request
            init_result = await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mystic", "version": "1.0.0"},
            })

            if init_result:
                # Send initialized notification
                await self._send_notification("notifications/initialized", {})
                self.connected = True
                logger.info("MCP server '%s' initialized successfully", self.config.name)

                # Discover tools
                await self.refresh_tools()
            else:
                logger.error("MCP server '%s' initialization failed", self.config.name)

        except FileNotFoundError:
            logger.error("MCP server command not found: %s", self.config.command)
            raise
        except Exception as e:
            logger.error("Failed to connect to MCP server '%s': %s", self.config.name, e)
            raise

    async def disconnect(self):
        """Stop the MCP server."""
        self.connected = False
        if self._reader_task:
            self._reader_task.cancel()
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.tools = []
        logger.info("MCP server '%s' disconnected", self.config.name)

    async def refresh_tools(self):
        """Fetch the list of tools from the MCP server."""
        result = await self._send_request("tools/list", {})
        if result and "tools" in result:
            self.tools = result["tools"]
            logger.info("MCP server '%s' has %d tools: %s",
                        self.config.name, len(self.tools),
                        [t["name"] for t in self.tools])

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool on the MCP server and return the result as a string."""
        result = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

        if result is None:
            return f"Error: MCP tool '{name}' returned no result"

        # Extract text content from MCP response
        content_parts = result.get("content", [])
        texts = []
        for part in content_parts:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image":
                texts.append(f"[Image: {part.get('mimeType', 'image')}]")
            else:
                texts.append(json.dumps(part))

        return "\n".join(texts) if texts else json.dumps(result)

    def get_openai_tools(self) -> list[dict]:
        """Convert MCP tools to OpenAI function-calling format."""
        openai_tools = []
        for tool in self.tools:
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": f"mcp_{self.config.id}_{tool['name']}",
                    "description": tool.get("description", f"MCP tool: {tool['name']}"),
                    "parameters": schema,
                },
            })
        return openai_tools

    async def _send_request(self, method: str, params: dict, timeout: float = 30.0) -> dict | None:
        """Send a JSON-RPC request and wait for the response."""
        if not self.process or not self.process.stdin:
            return None

        self._request_id += 1
        req_id = self._request_id

        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            data = json.dumps(request) + "\n"
            self.process.stdin.write(data.encode())
            await self.process.stdin.drain()

            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.error("MCP request timed out: %s (server: %s)", method, self.config.name)
            self._pending.pop(req_id, None)
            return None
        except Exception as e:
            logger.error("MCP request failed: %s — %s", method, e)
            self._pending.pop(req_id, None)
            return None

    async def _send_notification(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        if not self.process or not self.process.stdin:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        data = json.dumps(notification) + "\n"
        self.process.stdin.write(data.encode())
        await self.process.stdin.drain()

    async def _read_responses(self):
        """Read JSON-RPC responses from the MCP server stdout."""
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break

                line = line.decode().strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Handle response
                if "id" in msg and msg["id"] in self._pending:
                    future = self._pending.pop(msg["id"])
                    if "error" in msg:
                        logger.error("MCP error: %s", msg["error"])
                        future.set_result(None)
                    else:
                        future.set_result(msg.get("result", {}))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("MCP reader error for '%s': %s", self.config.name, e)


class MCPManager:
    """Manages multiple MCP server connections."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.servers: dict[str, MCPServerConfig] = {}
        self.connections: dict[str, MCPConnection] = {}
        self._load_config()

    def _load_config(self):
        """Load MCP server configurations from JSON file."""
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                for srv_data in data.get("servers", []):
                    cfg = MCPServerConfig(srv_data)
                    self.servers[cfg.id] = cfg
                logger.info("Loaded %d MCP server configs", len(self.servers))
            except Exception as e:
                logger.error("Failed to load MCP config: %s", e)
        else:
            self._save_config()

    def _save_config(self):
        """Save MCP server configurations to JSON file."""
        data = {
            "servers": [s.to_dict() for s in self.servers.values()]
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add_server(self, server_data: dict) -> MCPServerConfig:
        """Add a new MCP server configuration."""
        cfg = MCPServerConfig(server_data)
        if not cfg.id:
            cfg.id = str(uuid.uuid4())[:8]
        self.servers[cfg.id] = cfg
        self._save_config()
        logger.info("Added MCP server: %s (%s)", cfg.name, cfg.id)
        return cfg

    def remove_server(self, server_id: str) -> bool:
        """Remove an MCP server configuration."""
        if server_id in self.connections:
            # Will be disconnected by caller
            pass
        if server_id in self.servers:
            del self.servers[server_id]
            self._save_config()
            logger.info("Removed MCP server: %s", server_id)
            return True
        return False

    async def connect_server(self, server_id: str) -> bool:
        """Connect to an MCP server."""
        if server_id not in self.servers:
            return False

        # Disconnect existing connection if any
        if server_id in self.connections:
            await self.connections[server_id].disconnect()

        cfg = self.servers[server_id]
        conn = MCPConnection(cfg)
        try:
            await conn.connect()
            self.connections[server_id] = conn
            return True
        except Exception as e:
            logger.error("Failed to connect MCP server '%s': %s", cfg.name, e)
            return False

    async def disconnect_server(self, server_id: str) -> bool:
        """Disconnect from an MCP server."""
        if server_id in self.connections:
            await self.connections[server_id].disconnect()
            del self.connections[server_id]
            return True
        return False

    async def connect_all_enabled(self):
        """Connect to all enabled MCP servers."""
        for server_id, cfg in self.servers.items():
            if cfg.enabled:
                try:
                    await self.connect_server(server_id)
                except Exception as e:
                    logger.error("Failed to auto-connect MCP server '%s': %s", cfg.name, e)

    async def disconnect_all(self):
        """Disconnect all MCP servers."""
        for server_id in list(self.connections.keys()):
            await self.disconnect_server(server_id)

    def get_all_tools(self) -> list[dict]:
        """Get all tools from all connected MCP servers in OpenAI format."""
        tools = []
        for conn in self.connections.values():
            if conn.connected:
                tools.extend(conn.get_openai_tools())
        return tools

    async def call_tool(self, full_name: str, arguments: dict) -> str:
        """Call an MCP tool by its full name (mcp_{server_id}_{tool_name})."""
        # Parse the full name
        parts = full_name.split("_", 2)
        if len(parts) < 3 or parts[0] != "mcp":
            return f"Error: Invalid MCP tool name format: {full_name}"

        server_id = parts[1]
        tool_name = parts[2]

        if server_id not in self.connections:
            return f"Error: MCP server '{server_id}' is not connected"

        conn = self.connections[server_id]
        if not conn.connected:
            return f"Error: MCP server '{conn.config.name}' is not connected"

        return await conn.call_tool(tool_name, arguments)

    def get_status(self) -> list[dict]:
        """Get status of all MCP servers."""
        status = []
        for server_id, cfg in self.servers.items():
            conn = self.connections.get(server_id)
            status.append({
                "id": cfg.id,
                "name": cfg.name,
                "transport": cfg.transport,
                "command": cfg.command,
                "enabled": cfg.enabled,
                "connected": conn.connected if conn else False,
                "tool_count": len(conn.tools) if conn and conn.connected else 0,
                "tools": [t["name"] for t in conn.tools] if conn and conn.connected else [],
            })
        return status
