"""
Cloudflare Quick Tunnel — free cloud access for Mystic Dashboard.
Uses 'cloudflared' quick tunnels (trycloudflare.com) — 100% free, no account needed.
Downloads the binary automatically on first use.
"""

import os
import sys
import json
import uuid
import shutil
import asyncio
import logging
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger("nim_agent.tunnel")


class TunnelManager:
    """Manages Cloudflare Quick Tunnels and API key authentication."""

    def __init__(self, data_dir: Path, port: int = 8080):
        self.data_dir = data_dir
        self.port = port
        self._process: asyncio.subprocess.Process | None = None
        self._public_url: str | None = None
        self._api_key: str = self._load_or_create_key()

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def public_url(self) -> str | None:
        return self._public_url

    @property
    def full_url(self) -> str | None:
        """Public URL with API key appended for easy sharing."""
        if self._public_url:
            return f"{self._public_url}/?key={self._api_key}"
        return None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _key_path(self) -> Path:
        return self.data_dir / "mystic_api_key.txt"

    def _load_or_create_key(self) -> str:
        """Load existing API key or generate a new one."""
        kp = self._key_path()
        if kp.exists():
            key = kp.read_text(encoding="utf-8").strip()
            if len(key) >= 16:
                return key

        key = uuid.uuid4().hex[:32]
        kp.write_text(key, encoding="utf-8")
        logger.info("Generated new Mystic API key")
        return key

    def validate_key(self, provided_key: str) -> bool:
        """Check if a provided API key matches."""
        return provided_key == self._api_key

    def _get_cloudflared_path(self) -> Path:
        """Get path to cloudflared binary."""
        bin_dir = self.data_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "windows":
            return bin_dir / "cloudflared.exe"

        return bin_dir / "cloudflared"

    def _get_download_url(self) -> str:
        """Get the correct cloudflared download URL for this platform."""
        system = platform.system().lower()
        machine = platform.machine().lower()

        base = "https://github.com/cloudflare/cloudflared/releases/latest/download"

        if system == "linux":
            if machine in ("armv7l", "armv6l", "arm"):
                return f"{base}/cloudflared-linux-arm"
            elif machine in ("aarch64", "arm64"):
                return f"{base}/cloudflared-linux-arm64"
            elif machine in ("x86_64", "amd64"):
                return f"{base}/cloudflared-linux-amd64"
            else:
                return f"{base}/cloudflared-linux-amd64"
        elif system == "darwin":
            if machine in ("arm64", "aarch64"):
                return f"{base}/cloudflared-darwin-amd64.tgz"
            return f"{base}/cloudflared-darwin-amd64.tgz"
        elif system == "windows":
            return f"{base}/cloudflared-windows-amd64.exe"

        return f"{base}/cloudflared-linux-arm"  # Default to Pi

    async def _ensure_cloudflared(self) -> Path:
        """Download cloudflared if not present."""
        cf_path = self._get_cloudflared_path()

        # Check if already exists or in PATH
        if cf_path.exists():
            return cf_path

        if shutil.which("cloudflared"):
            return Path(shutil.which("cloudflared"))

        logger.info("Downloading cloudflared...")
        url = self._get_download_url()

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c",
                f"import urllib.request; urllib.request.urlretrieve('{url}', '{cf_path}')",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

            if platform.system().lower() != "windows":
                os.chmod(str(cf_path), 0o755)

            logger.info("cloudflared downloaded to %s", cf_path)
            return cf_path

        except Exception as e:
            logger.error("Failed to download cloudflared: %s", e)
            raise RuntimeError(
                f"Could not download cloudflared. Install manually: {url}"
            ) from e

    async def start(self) -> str:
        """Start a Cloudflare Quick Tunnel. Returns the public URL with API key."""
        if self.is_running:
            return self.full_url

        cf_path = await self._ensure_cloudflared()

        logger.info("Starting Cloudflare tunnel on port %d...", self.port)

        self._process = await asyncio.create_subprocess_exec(
            str(cf_path), "tunnel", "--url", f"http://localhost:{self.port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Read stderr to find the tunnel URL (cloudflared logs to stderr)
        url = await self._wait_for_url(timeout=30)

        if url:
            self._public_url = url
            logger.info("🌐 Tunnel active: %s", self.full_url)
            return self.full_url
        else:
            raise RuntimeError("Tunnel started but no URL was detected within 30s")

    async def _wait_for_url(self, timeout: float = 30) -> str | None:
        """Wait for cloudflared to print the tunnel URL."""
        import re

        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            if self._process.stderr is None:
                await asyncio.sleep(0.5)
                continue

            try:
                line = await asyncio.wait_for(
                    self._process.stderr.readline(), timeout=2.0
                )
                if not line:
                    if self._process.returncode is not None:
                        break
                    continue

                text = line.decode("utf-8", errors="replace").strip()
                logger.debug("cloudflared: %s", text)

                # Look for the trycloudflare.com URL
                match = re.search(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", text)
                if match:
                    return match.group(1)

            except asyncio.TimeoutError:
                continue

        return None

    async def stop(self):
        """Stop the tunnel."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
            logger.info("Tunnel stopped")
        self._process = None
        self._public_url = None

    def get_status(self) -> dict:
        """Get tunnel status info."""
        return {
            "running": self.is_running,
            "public_url": self._public_url,
            "full_url": self.full_url,
            "api_key": self._api_key,
            "port": self.port,
        }
