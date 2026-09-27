#!/usr/bin/env python3
"""GitHub-Token EINMAL pasten -> landet in Hermes-MCP UND Cline-MCP.
Du tippst versteckt in DEIN Terminal (nichts geht in einen Chat).
Ziele: 1) ~/.hermes/.env (Hermes liest GITHUB_PERSONAL_ACCESS_TOKEN),
2) Cline MCP-Settings (github-Server mit env GITHUB_PERSONAL_ACCESS_TOKEN),
3) gh-CLI Login (wenn gh installiert). Nie Ausgabe/Logs vom Token."""
import getpass, json, os, re, shutil, subprocess, sys
from pathlib import Path
TOKEN_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|ghu_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,})$")
CLINE_MCP = Path.home() / ".config" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
HERMES_ENV = Path.home() / ".hermes" / ".env"
def clean(raw):
    t = raw.strip()
    if t[:7].lower() == "bearer ":
        t = t[7:].strip()
    return t.replace("\n", "").replace("\r", "").replace(" ", "")
print("=== GitHub-Token fuer Hermes + Cline ===")
print("Neu erstellen: github.com -> Settings -> Developer settings ->")
print("Fine-grained tokens -> Generate new token (Contents read/write).")
print("Alten Token danach auf GitHub LOESCHEN.\n")
try:
    raw = getpass.getpass("Neuen Token hier pasten (versteckt, Enter): ")
except (KeyboardInterrupt, EOFError):
    print("\nAbgebrochen."); sys.exit(1)
token = clean(raw)
if not token or not TOKEN_RE.match(token):
    print("Falsches/leeres Format (github_pat_... oder ghp_...). Abgebrochen."); sys.exit(1)
# 1) Hermes .env
try:
    lines = HERMES_ENV.read_text().splitlines() if HERMES_ENV.exists() else []
    lines = [l for l in lines if not l.startswith("GITHUB_PERSONAL_ACCESS_TOKEN=")]
    lines.append(f"GITHUB_PERSONAL_ACCESS_TOKEN={token}")
    HERMES_ENV.parent.mkdir(parents=True, exist_ok=True)
    HERMES_ENV.write_text("\n".join(lines) + "\n")
    os.chmod(HERMES_ENV, 0o600)
    print("1) Hermes .env: OK")
except Exception as e:
    print(f"1) Hermes .env FEHLER: {e}")
# 2) Cline MCP settings (Referenz ${...}, Token selbst NIE in Datei)
d = json.loads(CLINE_MCP.read_text()) if CLINE_MCP.exists() else {}
servers = d.setdefault("mcpServers", {})
g = servers.setdefault("github", {})
g["command"] = "npx"
g["args"] = ["-y", "@modelcontextprotocol/server-github"]
g["env"] = {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"}
g["disabled"] = False
g["timeout"] = 60
CLINE_MCP.parent.mkdir(parents=True, exist_ok=True)
CLINE_MCP.write_text(json.dumps(d, indent=2) + "\n")
print("2) Cline MCP-Settings: OK (github-Server eingetragen)")
print("   HINWEIS: Cline liest ${...} evtl. nicht auf - falls der GitHub-MCP")
print("   in Cline keine Verbindung bekommt: VS Code neustarten; notfalls")
print("   stattdessen 'gh auth login' nutzen (Schritt 3) und mir Bescheid sagen.")
# 3) gh CLI
gh = shutil.which("gh")
if gh:
    try:
        p = subprocess.run([gh, "auth", "login", "--with-token"], input=token + "\n", text=True, capture_output=True, timeout=60)
        print(f"3) gh-CLI: {'OK' if p.returncode == 0 else 'FEHLER (pruefe gh auth status)'}")
    except Exception as e:
        print(f"3) gh-CLI FEHLER: {e}")
else:
    print("3) gh-CLI: nicht installiert (optional: sudo pacman -S github-cli)")
print("\nFertig. VS Code einmal neustarten, dann haben Hermes UND Cline GitHub-Zugang.")
print("Alten Token auf GitHub loeschen nicht vergessen!")
