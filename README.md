# Discord Archiver (Postgres Edition)

This repository contains the Discord crawler/bot and the Flask-based viewer that read/write from the shared **Postgres** database. All legacy SQLite helpers, backups, and FTS scripts have been deleted so the working tree only reflects the production stack.

## Prerequisites

- Python 3.11+
- A running Postgres instance (e.g., `postgresql://archiver:password@127.0.0.1:5432/discord_archiver`)
- Discord bot token with access to the source guild

## Local Setup

```bash
git clone <repo-url>
cd discord-archiver
python -m venv .venv
source .venv/bin/activate        # (Windows PowerShell: .\.venv\Scripts\Activate.ps1)
pip install --upgrade pip
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```ini
DISCORD_TOKEN=your_bot_token
DATABASE_URL=postgresql://archiver:password@localhost:5432/discord_archiver
```

## Running the Bot

```bash
PYTHONPATH=. python -m archiver.main
```

The crawler now backfills ~12 hours of history on startup and streams new GM posts into Postgres.

## Running the Viewer

```bash
PYTHONPATH=. python -m archiver.viewer --host 0.0.0.0 --port 8080
```

The viewer uses the same `DATABASE_URL` and no longer depends on SQLite.

## Notes

- `.gitignore` prevents committing local databases, virtual environments, and secrets.
- The removed files (`db_sqlite.py`, `setup_fts.py`, `viewer.txt`, etc.) are intentionally gone; any remaining SQLite references in the dependency tree are from third-party packages only.
- For production deployments (Fly.io, Contabo, etc.) reuse the same source tree—the bot and viewer now expect Postgres everywhere.
