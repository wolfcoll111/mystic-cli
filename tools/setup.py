#!/usr/bin/env python3
"""Mystic Ersteinrichtung: fragt alles ab, schreibt LOKALE .env (nie ins Repo).
Start: python3 setup.py  (GitHub-Token auch einzeln: python3 setup_github.py)
Der Token wird versteckt getippt, nie angezeigt/geloggt, nur lokal gespeichert:
~/.hermes/.env + gh-CLI. Niemals ./env, niemals Git-Repo, niemals Chat."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_part1 import ask, ask_secret, clean_token, store_github_token, TOKEN_RE
ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
def main():
    print("=== Mystic Ersteinrichtung ===\n")
    if ENV_FILE.exists():
        if ask("Es gibt schon .env. Ueberschreiben? (ja/nein)", "nein").lower() not in ("ja","j","yes","y"):
            print("Abgebrochen."); return
    print("--- 1/5 Anzeigename ---")
    agent_name = ask("Name deines Agents", "Mystic")
    print("\n--- 2/5 Anbieter (NVIDIA NIM und/oder OpenRouter) ---")
    print("NVIDIA Keys: https://build.nvidia.com (Get API Key)")
    print("OpenRouter Keys: https://openrouter.ai/keys")
    nvidia_key = nvidia_base = ""
    if ask("NVIDIA NIM nutzen? (ja/nein)", "ja").lower() in ("ja","j","yes","y"):
        nvidia_base = ask("NVIDIA Base-URL", "https://integrate.api.nvidia.com/v1")
        nvidia_key = ask_secret("NVIDIA API Key")
    or_key = or_base = ""
    if ask("OpenRouter nutzen? (ja/nein)", "nein").lower() in ("ja","j","yes","y"):
        or_base = ask("OpenRouter Base-URL", "https://openrouter.ai/api/v1")
        or_key = ask_secret("OpenRouter API Key")
    print("\n--- 3/5 Modell ---")
    model = ask("Modellname", "meta/llama3-70b-instruct")
    max_tokens = ask("Antwort-Laenge (max_tokens)", "4096")
    temperature = ask("Kreativitaet 0.0-1.0", "0.7")
    print("\n--- 4/5 Discord ---")
    print("Bot-Token: discord.com/developers/applications -> Bot -> Reset Token")
    discord_token = ask_secret("Discord Bot Token")
    allowed = ask("Erlaubte Discord User-IDs (Komma-getrennt)")
    print("\n--- 5/5 GitHub-Token (optional, Enter = spaeter via setup_github.py) ---")
    print("Erstellen: github.com -> Settings -> Developer settings -> Fine-grained tokens")
    gh_token = clean_token(ask_secret("GitHub-Token (Enter = ueberspringen)") or "")
    gh_saved = (False, False)
    if gh_token:
        if not TOKEN_RE.match(gh_token):
            print("Kein gueltiges Token-Format. Uebersprungen."); gh_token = ""
        else:
            gh_saved = store_github_token(gh_token)
            print(f"Gespeichert (Hermes: {'ja' if gh_saved[0] else 'nein'}, gh-CLI: {'ja' if gh_saved[1] else 'nein'}).")
    env = ["# Mystic lokale Einstellungen (NIEMALS committen!)", "# Erstellt mit: python3 setup.py", "",
        f"AGENT_NAME={agent_name}", "", "# NVIDIA NIM", f"NIM_BASE_URL={nvidia_base}", f"NVIDIA_API_KEY={nvidia_key}",
        "", "# OpenRouter", f"OPENROUTER_BASE_URL={or_base}", f"OPENROUTER_API_KEY={or_key}",
        "", "# Modell", f"MODEL_NAME={model}", f"MAX_TOKENS={max_tokens}", f"TEMPERATURE={temperature}",
        "", "# Discord", f"DISCORD_BOT_TOKEN={discord_token}", f"ALLOWED_USERS={allowed}",
        "", "# GitHub-Token steht ABSICHTLICH NICHT hier (nur ~/.hermes/.env + gh-CLI)."]
    ENV_FILE.write_text("\n".join(env) + "\n")
    os.chmod(ENV_FILE, 0o600)
    print(f"\nFertig! .env nach {ENV_FILE} (nur du lesbar). Siehe README.")
if __name__ == "__main__":
    main()
