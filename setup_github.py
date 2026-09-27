#!/usr/bin/env python3
"""GitHub-Token nachtragen/austauschen (fuer Cline UND Hermes).
Du tippst den neuen Token versteckt ein (PASTE hier = nur in DEIN Terminal,
nicht in irgendeinen Chat). Gespeichert: ~/.hermes/.env + gh-CLI, nie Repo/Chat.
Start: python3 setup_github.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_part1 import ask_secret, clean_token, store_github_token, TOKEN_RE
print("=== GitHub-Token einrichten ===")
print("Neuer Token: github.com -> Settings -> Developer settings ->")
print("Personal access tokens -> Fine-grained tokens -> Generate new token")
print("(Rechte: Contents read/write nur fuer deine Repos.)\n")
raw = ask_secret("Neuen Token hier pasten")
token = clean_token(raw)
if not token:
    print("Leer. Abgebrochen."); sys.exit(1)
if not TOKEN_RE.match(token):
    print("Format falsch (erwartet github_pat_... oder ghp_...). Abgebrochen."); sys.exit(1)
ok_h, ok_g = store_github_token(token)
print(f"Fertig. Hermes: {'OK' if ok_h else 'FEHLER'}, gh-CLI: {'OK' if ok_g else 'FEHLER/fehlend'}.")
print("Danach: 'gh auth status' pruefen. Alten Token auf GitHub loeschen!")
