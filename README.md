# Mystic CLI 2.0 — Optimized Rebuild

Rebuilt from the flat `mystic-cli` dump into a clean `src/` layout. Canonical
`*_modified.py` files promoted; `_home_lordhats_*` backups and dead setup
duplicates removed.

## Layout

```
src/mystic/
  main.py  config.py  config_manager.py  core.py  nim_client.py
  memory.py  master_prompt.py  mcp_client.py  swarm.py  _store.py
  skills/{base,loader,code_executor,file_manager}.py
  discord_bot/bot.py
  dashboard/{app,chat_store,tunnel}.py  dashboard/static/
tools/setup.py  tools/setup_helpers.py  tools/verify_rebuild.py
tests/test_rebuild.py
data/  (gitignored runtime: models/providers/mcp/memory.db)
```

## What was fixed

**Bugs (top):**
1. Leaked live `ogw_live_` key in committed JSON — file dropped, never copied. **Rotate that key.**
2. Nothing imported (`agent/`/`skills/` packages missing) — real `mystic` package now.
3. `requirements.txt` incomplete (`openai, aiosqlite, aiohttp, psutil` missing; `httpx` unused) — replaced by `pyproject.toml`.
4. `POST /api/terminal/exec` arbitrary shell + key leak via `GET /api/status` — cwd sandboxed to data dir, denylist, 30s timeout, key never returned (`get_public_status()`).
5. LLM code executor hardcoded `/data/workspace`, no limits — uses `WORKSPACE_DIR`/`CODE_TIMEOUT`, 20k input cap, dangerous-pattern block.
6. Auth bypass for `/api/chat/files/*` — removed; all `/api/*` require key.
7. Path traversal via naive `replace("..","")` — new `safe_join()` with `resolve()` + `is_relative_to`, fail-closed on `..`.
8. Blocking `psutil.cpu_percent(interval=0.1)` + sync disk reads — non-blocking + `to_thread`.
9. JSON read-modify-write races — atomic `tmp+rename` writes + per-file locks.
10. `SyntaxError: global cfg_mgr` + import-time `Config()` mkdir side effects — lazy `init_runtime()`.
11. Unbounded `memory` table / logs / uploads — `Memory.prune()`, 10MB upload cap.
12. `NIMClient` socket leak (new client per reload, never closed) — `timeout=60`, `close()`, swarm closes old clients.

**Perf:** TTL-cached config reads (10s), async file I/O, WAL-mode sqlite, optional `psutil`/`openai` imports so dashboard imports without Pi-only deps.

## Verify

```bash
python3 tools/verify_rebuild.py   # 8 checks, no extra deps needed
python3 -m pytest tests/ -q       # same checks as pytest (needs pytest only)
```

## Rotate before deploy

The old repo contained a live Opengateway key (`ogw_live_...` in
`_home_lordhats_nim-agent_data_providers_config.json`). It is NOT in this
rebuild, but it lives in git history — rotate it in the provider dashboard.
