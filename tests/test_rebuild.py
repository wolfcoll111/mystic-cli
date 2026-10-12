"""Rebuild regression tests: imports, safe paths, no secret leaks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest


def test_imports():
    import mystic.config
    import mystic._store
    import mystic.config_manager
    import mystic.nim_client
    import mystic.memory
    import mystic.skills.file_manager
    import mystic.skills.code_executor
    import mystic.dashboard.chat_store
    import mystic.dashboard.tunnel
    import mystic.dashboard.app  # must not do I/O at import
    import mystic.swarm
    assert True


def test_config_has_explicit_load():
    # Import must expose explicit Config.load() (no I/O at import time).
    from mystic import config as cmod
    assert hasattr(cmod.Config, "load")


def test_safe_join_blocks_traversal(tmp_path):
    from mystic._store import safe_join
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_join(root, "../../etc/passwd")
    # dotted dirname without separator is a literal name, stays inside root
    inside = safe_join(root, "....")
    assert str(inside).startswith(str(root.resolve()))
    ok = safe_join(root, "notes/todo.txt")
    assert str(ok).startswith(str(root.resolve()))


def test_safe_filename():
    from mystic._store import safe_filename
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("c.png") == "c.png"
    # backslash is a valid POSIX filename char -> sanitized to underscore
    assert safe_filename("a/b\\c.png") in ("c.png", "b_c.png")


def test_chat_store_rejects_bad_id(tmp_path):
    from mystic.config import Config
    from mystic.dashboard.chat_store import ChatStore
    cs = ChatStore(tmp_path)
    with pytest.raises(ValueError):
        cs._thread_path("../../etc/passwd")
    with pytest.raises(ValueError):
        cs._thread_path("a/b")


def test_tunnel_status_masked(tmp_path):
    from mystic.dashboard.tunnel import TunnelManager
    tm = TunnelManager(tmp_path, port=8080)
    pub = tm.get_public_status()
    assert "api_key" not in pub
    assert "full_url" not in pub or "key=" not in str(pub.get("full_url", ""))
    assert pub["has_key"] is True
    # key file locked down
    import os
    assert oct((tmp_path / "mystic_api_key.txt").stat().st_mode)[-3:] in ("600", "400")


def test_no_hardcoded_secrets():
    root = Path(__file__).resolve().parent.parent / "src"
    bad = []
    for fp in root.rglob("*.py"):
        text = fp.read_text(encoding="utf-8", errors="ignore")
        for marker in ("ogw_live_", "ghp_", "github_pat_"):
            if marker in text:
                bad.append(f"{fp}:{marker}")
    assert not bad, f"leaked secrets: {bad}"


def test_nim_client_has_close_and_timeout():
    import inspect
    from mystic.nim_client import NIMClient
    assert hasattr(NIMClient, "close")
    sig = inspect.signature(NIMClient.__init__)
    assert "timeout" in sig.parameters
