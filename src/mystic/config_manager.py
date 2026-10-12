"""ConfigManager — shared sync layer. Cached + atomic + locked."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from mystic._store import TTLCache, read_json, write_json_atomic_async
from mystic.config import Config

logger = logging.getLogger("mystic.config_manager")


class ConfigManager:
    def __init__(self, config: Config, cache_ttl: float = 10.0):
        self._config = config
        self.models_config_path = config.data_dir / "models_config.json"
        self.providers_config_path = config.providers_config_path
        self.model_providers_path = config.data_dir / "model_providers.json"
        self._models_cache = TTLCache(cache_ttl)
        self._providers_cache = TTLCache(cache_ttl)
        self._mapping_cache = TTLCache(cache_ttl)
        self._lock = asyncio.Lock()

    # ── Models ──
    def get_models(self) -> dict:
        cached = self._models_cache.get()
        if cached is not None:
            return dict(cached)
        default = {
            "overseer": "meta/llama3-70b-instruct",
            "architect": "z-ai/glm-5.1",
            "coder": "z-ai/glm-5.1",
            "verifier": "minimaxai/minimax-m2.7",
            "agent": self._config.model_name,
        }
        loaded = read_json(self.models_config_path, {})
        if isinstance(loaded, dict):
            default.update(loaded)
        self._models_cache.set(default)
        return dict(default)

    def update_models(self, updates: dict) -> dict:
        current = read_json(self.models_config_path, {})
        if not isinstance(current, dict):
            current = {}
        current.update(updates)
        self.models_config_path.parent.mkdir(parents=True, exist_ok=True)
        # sync write here (callers are sync); async variant below for API paths
        from mystic._store import write_json_atomic
        write_json_atomic(self.models_config_path, current)
        self._models_cache.invalidate()
        return current

    async def aupdate_models(self, updates: dict) -> dict:
        async with self._lock:
            current = await self._read(self.models_config_path, {})
            current.update(updates)
            await write_json_atomic_async(self.models_config_path, current)
            self._models_cache.invalidate()
            return current

    # ── Mapping ──
    def get_model_providers(self) -> dict:
        cached = self._mapping_cache.get()
        if cached is not None:
            return dict(cached)
        data = read_json(self.model_providers_path, {})
        if not isinstance(data, dict):
            data = {}
        self._mapping_cache.set(data)
        return dict(data)

    def set_model_provider(self, role: str, provider_id: str):
        from mystic._store import write_json_atomic
        mapping = self.get_model_providers()
        mapping[role] = provider_id
        write_json_atomic(self.model_providers_path, mapping)
        self._mapping_cache.invalidate()

    def remove_model_provider(self, role: str):
        from mystic._store import write_json_atomic
        mapping = self.get_model_providers()
        mapping.pop(role, None)
        write_json_atomic(self.model_providers_path, mapping)
        self._mapping_cache.invalidate()

    # ── Providers ──
    def get_providers(self) -> list:
        cached = self._providers_cache.get()
        if cached is not None:
            return list(cached)
        data = read_json(self.providers_config_path, [])
        if not isinstance(data, list):
            data = []
        self._providers_cache.set(data)
        return list(data)

    def save_provider(self, data: dict) -> dict:
        from mystic._store import write_json_atomic
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
        self._save_providers_sync(providers)
        self._providers_cache.invalidate()
        return data

    def delete_provider(self, provider_id: str) -> bool:
        from mystic._store import write_json_atomic
        providers = self.get_providers()
        new_list = [p for p in providers if p.get("id") != provider_id]
        if len(new_list) == len(providers):
            return False
        self._save_providers_sync(new_list)
        self._providers_cache.invalidate()
        return True

    def update_provider_key(self, provider_id_or_name: str, new_key: str) -> bool:
        providers = self.get_providers()
        for p in providers:
            if p.get("id") == provider_id_or_name or p.get("name", "").lower() == provider_id_or_name.lower():
                p["api_key"] = new_key
                self._save_providers_sync(providers)
                self._providers_cache.invalidate()
                return True
        return False

    def update_provider_endpoint(self, provider_id_or_name: str, new_url: str) -> bool:
        providers = self.get_providers()
        for p in providers:
            if p.get("id") == provider_id_or_name or p.get("name", "").lower() == provider_id_or_name.lower():
                p["base_url"] = new_url
                self._save_providers_sync(providers)
                self._providers_cache.invalidate()
                return True
        return False

    def _save_providers_sync(self, providers: list):
        from mystic._store import write_json_atomic
        self.providers_config_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.providers_config_path, providers)

    async def _read(self, path: Path, default):
        return await asyncio.to_thread(read_json, path, default)

    # ── Resolve ──
    def resolve_provider_for_role(self, role: str) -> dict | None:
        mapping = self.get_model_providers()
        provider_id = mapping.get(role)
        if not provider_id:
            return None
        for p in self.get_providers():
            if p.get("id") == provider_id:
                return p
        return None

    def create_client_for_role(self, role: str, model_name: str | None = None):
        from mystic.nim_client import NIMClient
        provider = self.resolve_provider_for_role(role)
        if provider:
            api_key = provider.get("api_key", self._config.nvidia_api_key)
            base_url = provider.get("base_url", self._config.nim_base_url)
        else:
            api_key = self._config.nvidia_api_key
            base_url = self._config.nim_base_url
        models = self.get_models()
        model = model_name or models.get(role, self._config.model_name)
        return NIMClient(api_key=api_key, base_url=base_url, model=model,
                         max_tokens=self._config.max_tokens, temperature=self._config.temperature)

    def apply_to_swarm(self, swarm):
        """Recreate swarm clients, closing old ones to avoid socket leaks."""
        for attr in ("overseer", "architect", "coder", "verifier"):
            old = getattr(swarm, attr, None)
            if old is not None and hasattr(old, "close"):
                try:
                    # old clients are AsyncOpenAI wrappers; schedule close if loop running
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(old.close())
                except Exception:
                    pass
        swarm.overseer = self.create_client_for_role("overseer")
        swarm.architect = self.create_client_for_role("architect")
        swarm.coder = self.create_client_for_role("coder")
        swarm.verifier = self.create_client_for_role("verifier")
        logger.info("Swarm clients refreshed from ConfigManager")
