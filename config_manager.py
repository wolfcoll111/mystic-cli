"""
ConfigManager — shared sync layer between Dashboard & Discord.
Reads/writes models_config.json, providers_config.json, model_providers.json.
Both dashboard and discord use this to stay in sync.
"""

import json
import logging
import uuid
from pathlib import Path

from agent.config import Config

logger = logging.getLogger("nim_agent.config_manager")


class ConfigManager:
    """
    Central config management.
    Both the dashboard API and Discord bot use this to read/write
    models, providers, and the model-to-provider mapping.
    """

    def __init__(self, config: Config):
        self._config = config
        self.models_config_path = config.data_dir / "models_config.json"
        self.providers_config_path = config.providers_config_path
        self.model_providers_path = config.data_dir / "model_providers.json"

    # ── Models ──────────────────────────────────────────────

    def get_models(self) -> dict:
        default = {
            "overseer": "meta/llama3-70b-instruct",
            "architect": "z-ai/glm-5.1",
            "coder": "z-ai/glm-5.1",
            "verifier": "minimaxai/minimax-m2.7",
            "agent": self._config.model_name,
        }
        if self.models_config_path.exists():
            try:
                loaded = json.loads(self.models_config_path.read_text(encoding="utf-8"))
                default.update(loaded)
            except Exception:
                pass
        return default

    def update_models(self, updates: dict) -> dict:
        current = {}
        if self.models_config_path.exists():
            try:
                current = json.loads(self.models_config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        current.update(updates)
        self.models_config_path.write_text(json.dumps(current, indent=4), encoding="utf-8")
        return current

    # ── Model -> Provider Mapping ───────────────────────────

    def get_model_providers(self) -> dict:
        default = {}
        if self.model_providers_path.exists():
            try:
                return json.loads(self.model_providers_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return default

    def set_model_provider(self, role: str, provider_id: str):
        mapping = self.get_model_providers()
        mapping[role] = provider_id
        self.model_providers_path.write_text(json.dumps(mapping, indent=4), encoding="utf-8")

    def remove_model_provider(self, role: str):
        mapping = self.get_model_providers()
        mapping.pop(role, None)
        self.model_providers_path.write_text(json.dumps(mapping, indent=4), encoding="utf-8")

    # ── Providers ───────────────────────────────────────────

    def get_providers(self) -> list:
        if self.providers_config_path.exists():
            try:
                return json.loads(self.providers_config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def save_provider(self, data: dict) -> dict:
        providers = self.get_providers()
        provider_id = data.get("id")
        if provider_id:
            for i, p in enumerate(providers):
                if p.get("id") == provider_id:
                    providers[i] = {**p, **data}
                    break
            else:
                data["id"] = str(uuid.uuid4())[:8]
                providers.append(data)
        else:
            data["id"] = str(uuid.uuid4())[:8]
            providers.append(data)
        self._save_providers(providers)
        return data

    def delete_provider(self, provider_id: str) -> bool:
        providers = self.get_providers()
        new_list = [p for p in providers if p.get("id") != provider_id]
        if len(new_list) == len(providers):
            return False
        self._save_providers(new_list)
        return True

    def update_provider_key(self, provider_id_or_name: str, new_key: str) -> bool:
        providers = self.get_providers()
        for p in providers:
            if p.get("id") == provider_id_or_name or p.get("name", "").lower() == provider_id_or_name.lower():
                p["api_key"] = new_key
                self._save_providers(providers)
                return True
        return False

    def update_provider_endpoint(self, provider_id_or_name: str, new_url: str) -> bool:
        providers = self.get_providers()
        for p in providers:
            if p.get("id") == provider_id_or_name or p.get("name", "").lower() == provider_id_or_name.lower():
                p["base_url"] = new_url
                self._save_providers(providers)
                return True
        return False

    def _save_providers(self, providers: list):
        self.providers_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.providers_config_path.write_text(json.dumps(providers, indent=2), encoding="utf-8")

    # ── Resolve Provider for a Role ─────────────────────────

    def resolve_provider_for_role(self, role: str) -> dict | None:
        """Find the provider config that should serve this role."""
        mapping = self.get_model_providers()
        provider_id = mapping.get(role)
        if not provider_id:
            return None
        for p in self.get_providers():
            if p.get("id") == provider_id:
                return p
        return None

    def create_client_for_role(self, role: str, model_name: str | None = None):
        """
        Create an NIMClient for a given swarm role.
        Uses the provider mapping if available, otherwise falls back
        to the default Config values.
        """
        from agent.nim_client import NIMClient

        provider = self.resolve_provider_for_role(role)
        if provider:
            api_key = provider.get("api_key", self._config.nvidia_api_key)
            base_url = provider.get("base_url", self._config.nim_base_url)
        else:
            api_key = self._config.nvidia_api_key
            base_url = self._config.nim_base_url

        models = self.get_models()
        model = model_name or models.get(role, self._config.model_name)

        return NIMClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
        )

    # ── Reload swarm clients in-place ───────────────────────

    def apply_to_swarm(self, swarm):
        """Recreate all swarm NIMClients from current config."""
        swarm.overseer = self.create_client_for_role("overseer")
        swarm.architect = self.create_client_for_role("architect")
        swarm.coder = self.create_client_for_role("coder")
        swarm.verifier = self.create_client_for_role("verifier")
        logger.info("Swarm clients refreshed from ConfigManager")
