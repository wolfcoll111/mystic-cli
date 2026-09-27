"""
Configuration loader for NIM Agent.
Reads from .env file and provides typed access to all settings.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root or /data/.env (Docker)
_env_paths = [
    Path(__file__).parent.parent / ".env",
    Path("/data/.env"),
]
for p in _env_paths:
    if p.exists():
        load_dotenv(p)
        break


class Config:
    """Central configuration for the NIM Agent."""

    # --- NVIDIA NIM ---
    nvidia_api_key: str = os.getenv("NVIDIA_API_KEY", "")
    nim_base_url: str = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    model_name: str = os.getenv("MODEL_NAME", "z-ai/glm5")
    max_tokens: int = int(os.getenv("MAX_TOKENS", "4096"))
    temperature: float = float(os.getenv("TEMPERATURE", "0.7"))

    # --- OpenRouter ---
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")

    # --- Discord ---
    discord_token: str = os.getenv("DISCORD_BOT_TOKEN", "")
    allowed_users: list[int] = [
        int(uid.strip())
        for uid in os.getenv("ALLOWED_USERS", "").split(",")
        if uid.strip().isdigit()
    ]

    # --- Paths ---
    project_root: Path = Path(__file__).parent.parent
    data_dir: Path = Path(os.getenv("DATA_DIR", str(project_root / "data")))
    skills_dir: Path = project_root / "skills"
    custom_skills_dir: Path = Path(os.getenv("CUSTOM_SKILLS_DIR", str(project_root / "custom_skills")))
    workspace_dir: Path = Path(os.getenv("WORKSPACE_DIR", str(data_dir / "workspace")))
    db_path: Path = Path(os.getenv("DB_PATH", str(data_dir / "memory.db")))
    improvements_dir: Path = Path(os.getenv("IMPROVEMENTS_DIR", str(data_dir / "improvements")))
    mcp_config_path: Path = Path(os.getenv("MCP_CONFIG_PATH", str(data_dir / "mcp_servers.json")))
    providers_config_path: Path = Path(os.getenv("PROVIDERS_CONFIG_PATH", str(data_dir / "providers_config.json")))

    # --- Discord Sync ---
    discord_sync_channel_id: int | None = (
        int(os.getenv("DISCORD_SYNC_CHANNEL_ID"))
        if os.getenv("DISCORD_SYNC_CHANNEL_ID", "").strip().isdigit()
        else None
    )

    # --- Agent Behaviour ---
    max_tool_iterations: int = int(os.getenv("MAX_TOOL_ITERATIONS", "50"))
    code_timeout: int = int(os.getenv("CODE_TIMEOUT", "30"))
    self_improve_interval_hours: float = float(os.getenv("SELF_IMPROVE_INTERVAL", "6"))

    def __init__(self):
        """Ensure required dirs exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.custom_skills_dir.mkdir(parents=True, exist_ok=True)
        self.improvements_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """Return list of missing required config values."""
        errors = []
        if not self.nvidia_api_key:
            errors.append("NVIDIA_API_KEY is not set")
        if not self.discord_token:
            errors.append("DISCORD_BOT_TOKEN is not set")
        if not self.allowed_users:
            errors.append("ALLOWED_USERS is not set (need at least one Discord user ID)")
        return errors
