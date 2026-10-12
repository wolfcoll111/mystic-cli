#!/usr/bin/env python3
"""Mystic Setup Teil 1: Helfer + GitHub-Token-Speicherung (lokal only)."""
import getpass, os, re, shutil, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
TOKEN_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|ghu_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,})$")
def ask(prompt, default=""):
    hint = f" [{default}]" if default else ""
    val = input(f"{prompt}{hint}: ").strip()
    return val if val else default
def ask_secret(prompt):
    try:
        return getpass.getpass(f"{prompt} (versteckt): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAbgebrochen."); sys.exit(1)
def clean_token(raw):
    t = raw.strip()
    if t[:7].lower() == "bearer ":
        t = t[7:].strip()
    return t.replace("\n", "").replace("\r", "").replace(" ", "")
def store_github_token(token):
    ok_h, ok_g = False, False
    henv = Path.home() / ".hermes" / ".env"
    try:
        lines = henv.read_text().splitlines() if henv.exists() else []
        lines = [l for l in lines if not l.startswith("GITHUB_PERSONAL_ACCESS_TOKEN=")]
        lines.append(f"GITHUB_PERSONAL_ACCESS_TOKEN={token}")
        henv.parent.mkdir(parents=True, exist_ok=True)
        henv.write_text("\n".join(lines) + "\n")
        os.chmod(henv, 0o600)
        ok_h = True
    except Exception as e:
        print(f"  Hinweis: Hermes-.env Fehler ({e})")
    gh = shutil.which("gh")
    if gh:
        try:
            p = subprocess.run([gh, "auth", "login", "--with-token"], input=token + "\n", text=True, capture_output=True, timeout=60)
            ok_g = p.returncode == 0
        except Exception as e:
            print(f"  Hinweis: gh-CLI Fehler ({e})")
    else:
        print("  Hinweis: gh-CLI fehlt - nur Hermes-.env gespeichert.")
    return ok_h, ok_g
