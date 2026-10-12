#!/usr/bin/env python3
"""Plain-script rebuild verifier (no pytest needed). Exit 0 = all pass."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

passed, failed = [], []


def check(name, fn):
    try:
        fn()
        passed.append(name)
        print(f"PASS {name}")
    except Exception as e:
        failed.append(name)
        print(f"FAIL {name}: {type(e).__name__}: {e}")


def t_imports():
    import mystic.config, mystic._store, mystic.config_manager  # noqa
    import mystic.nim_client, mystic.memory  # noqa
    import mystic.skills.file_manager, mystic.skills.code_executor  # noqa
    import mystic.dashboard.chat_store, mystic.dashboard.tunnel  # noqa
    import mystic.dashboard.app, mystic.swarm  # noqa


def t_config():
    from mystic import config as cmod
    assert hasattr(cmod.Config, "load")
    cfg = cmod.Config.load()
    assert cfg.data_dir.name == "data"


def t_safe_join():
    from mystic._store import safe_join
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "ws"
        root.mkdir()
        for evil in ("../../etc/passwd", "../x", "a/../../b"):
            try:
                safe_join(root, evil)
            except ValueError:
                pass
            else:
                raise AssertionError(f"no raise for {evil}")
        ok = safe_join(root, "notes/todo.txt")
        assert str(ok).startswith(str(root.resolve()))


def t_safe_filename():
    from mystic._store import safe_filename
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("c.png") == "c.png"


def t_chat_ids():
    from mystic.dashboard.chat_store import ChatStore
    with tempfile.TemporaryDirectory() as td:
        cs = ChatStore(Path(td))
        for bad in ("../../etc/passwd", "a/b", ""):
            try:
                cs._thread_path(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"no raise for {bad!r}")
        t = cs.create_thread("hi")
        assert cs.get_thread(t["id"]) is not None


def t_tunnel_masked():
    from mystic.dashboard.tunnel import TunnelManager
    with tempfile.TemporaryDirectory() as td:
        tm = TunnelManager(Path(td), port=8080)
        pub = tm.get_public_status()
        assert "api_key" not in pub, "key leaked in public status"
        assert pub["has_key"] is True


def t_no_secrets():
    for fp in (ROOT / "src").rglob("*.py"):
        text = fp.read_text(encoding="utf-8", errors="ignore")
        for marker in ("ogw_live_", "ghp_", "github_pat_"):
            assert marker not in text, f"leak in {fp}"


def t_nim_client():
    import inspect
    from mystic.nim_client import NIMClient
    assert hasattr(NIMClient, "close")
    assert "timeout" in inspect.signature(NIMClient.__init__).parameters


for name, fn in [
    ("imports", t_imports), ("config", t_config), ("safe_join", t_safe_join),
    ("safe_filename", t_safe_filename), ("chat_ids", t_chat_ids),
    ("tunnel_masked", t_tunnel_masked), ("no_secrets", t_no_secrets),
    ("nim_client", t_nim_client),
]:
    check(name, fn)

print(f"\n{len(passed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
