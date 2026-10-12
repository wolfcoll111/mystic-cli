"""Central configuration. No I/O at import time — call Config.load() explicitly."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent.parent)
    nvidia_api_key: str = ""
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    model_name: str = "z-ai/glm5"
    max_tokens: int = 4096
    temperature: float = 0.7
    openrouter_api_key: str = ""
    discord_token: str = ""
    allowed_users: list[int] = field(default_factory=list)
    data_dir: Path = field(default_factory=Path)
    skills_dir: Path = field(default_factory=Path)
    custom_skills_dir: Path = field(default_factory=Path)
    workspace_dir: Path = field(default_factory=Path)
    db_path: Path = field(default_factory=Path)
    improvements_dir: Path = field(default_factory=Path)
    mcp_config_path: Path = field(default_factory=Path)
    providers_config_path: Path = field(default_factory=Path)
    discord_sync_channel_id: int | None = None
    max_tool_iterations: int = 50
    code_timeout: int = 30
    self_improve_interval_hours: float = 6.0

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        # Load .env lazily here (not at import) so tests/CLI control ordering.
        try:
            from dotenv import load_dotenv
            candidates = []
            if env_file:
                candidates.append(Path(env_file))
            candidates += [
                Path.cwd() / ".env",
                Path(__file__).resolve().parent.parent.parent / ".env",
                Path("/data/.env"),
            ]
            for p in candidates:
                if p.exists():
                    load_dotenv(p, override=False)
                    break
        except ImportError:
            pass

        root = Path(__file__).resolve().parent.parent.parent
        data_dir = Path(os.getenv("DATA_DIR", str(root / "data")))
        allowed = [
            int(uid.strip())
            for uid in os.getenv("ALLOWED_USERS", "").split(",")
            if uid.strip().isdigit()
        ]
        sync_raw = os.getenv("DISCORD_SYNC_CHANNEL_ID", "").strip()
        cfg = cls(
            project_root=root,
            nvidia_api_key=os.getenv("NVIDIA_API_KEY", ""),
            nim_base_url=os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            model_name=os.getenv("MODEL_NAME", "z-ai/glm5"),
            max_tokens=_int("MAX_TOKENS", 4096),
            temperature=_float("TEMPERATURE", 0.7),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            discord_token=os.getenv("DISCORD_BOT_TOKEN", ""),
            allowed_users=allowed,
            data_dir=data_dir,
            skills_dir=root / "skills",
            custom_skills_dir=Path(os.getenv("CUSTOM_SKILLS_DIR", str(root / "custom_skills"))),
            workspace_dir=Path(os.getenv("WORKSPACE_DIR", str(data_dir / "workspace"))),
            db_path=Path(os.getenv("DB_PATH", str(data_dir / "memory.db"))),
            improvements_dir=Path(os.getenv("IMPROVEMENTS_DIR", str(data_dir / "improvements"))),
            mcp_config_path=Path(os.getenv("MCP_CONFIG_PATH", str(data_dir / "mcp_servers.json"))),
            providers_config_path=Path(os.getenv("PROVIDERS_CONFIG_PATH", str(data_dir / "providers_config.json"))),
            discord_sync_channel_id=int(sync_raw) if sync_raw.isdigit() else None,
            max_tool_iterations=_int("MAX_TOOL_ITERATIONS", 50),
            code_timeout=_int("CODE_TIMEOUT", 30),
            self_improve_interval_hours=_float("SELF_IMPROVE_INTERVAL", 6.0),
        )
        return cfg

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.workspace_dir, self.custom_skills_dir, self.improvements_dir):
            p.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        errors = []
        if not self.nvidia_api_key:
            errors.append("NVIDIA_API_KEY is not set")
        if not self.discord_token:
            errors.append("DISCORD_BOT_TOKEN is not set")
        if not self.allowed_users:
            errors.append("ALLOWED_USERS is not set (need at least one Discord user ID)")
        return errors
