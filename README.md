# Mystic CLI

KI-Agent mit Gespraechsschleife (System-Prompt, Tool-Calls, Antwort), Skill-System
(Code-Ausfuehrung Python/Bash, Dateiverwaltung), Konfigurations-Sync zwischen
Dashboard und Discord-Bot, Web-Dashboard (HTML/CSS/JS), Mehr-Modell-Setup.

- 22 Python-Dateien + 2 JS-Dateien, ca. 6.300 Zeilen
- Sprachen: Python, JavaScript (Dashboard), HTML/CSS
- KI-CLI-Workflow als Partner: planen, Schritt fuer Schritt umsetzen

## Schnellstart

```bash
python3 setup.py        # fragt alles ab, schreibt lokale .env (nie im Repo)
python3 setup_github.py # GitHub-Token nachtragen (Hermes + gh-CLI)
```

Die `.env` steht in `.gitignore` und wird nie gepusht. Vorlage: `.env.example`.

## Hinweise

- API-Keys nur ueber Umgebungsvariablen (`os.getenv`), nie im Code.
- Discord-Bot + Dashboard teilen sich die Konfiguration ueber `config_manager.py`.
