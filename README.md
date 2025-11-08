# Discord Archiver

A comprehensive archiving system for Discord messages with full-text search and web-based viewing interface.

## Overview

Discord Archiver captures and stores Discord server messages in a PostgreSQL database, providing a powerful search interface and message browser. Built specifically for the GemStone IV Discord community.

**Key Features:**
- Real-time message capture as they're posted
- Historical message backfill (crawls past messages)
- Full-text search with PostgreSQL
- Web-based viewer with REST API
- Game Master (GM) message aggregation and reposting
- Edit/delete tracking with revision history

## Architecture

The system consists of two independent services:

- **Bot** (`bot/`) - Discord client that captures messages and crawls history
- **Viewer** (`source/`) - Flask web application for searching and viewing archived messages

Both services connect to a shared PostgreSQL database.

## Quick Start

### Prerequisites

- Python 3.10+
- PostgreSQL 12+
- Discord bot token

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/Nisugi/discord_archiver.git
   cd discord_archiver
   ```

2. **Install dependencies**
   ```bash
   # For the bot
   cd bot
   pip install -r requirements.txt

   # For the viewer (optional)
   cd ../source
   pip install -r requirements.txt
   ```

3. **Set up PostgreSQL**
   ```bash
   createdb discord_archiver
   ```

4. **Configure environment variables**

   Create a `.env` file in both `bot/` and `source/` directories:
   ```
   DISCORD_TOKEN=your_discord_bot_token_here
   DATABASE_URL=postgresql://user:password@localhost:5432/discord_archiver
   ```

5. **Initialize the database**
   ```bash
   cd bot
   python ../scripts/init_db.py
   ```

6. **Run the bot**
   ```bash
   python -m archiver.main
   ```

7. **Run the viewer (optional)**
   ```bash
   cd ../source
   flask --app archiver.viewer run --port 8080
   ```

## Configuration

Key configuration options are in `bot/archiver/config.py` and `source/archiver/config.py`:

- `SOURCE_GUILD_ID` - Discord server to archive
- `CRAWL_BACKFILL_DAYS` - How far back to crawl on startup (default: 0.5 days)
- `REPOST_DELAY_SECONDS` - Delay before reposting GM messages (default: 300s)
- `VIEWER_PAGE_SIZE` - Results per page in web viewer (default: 25)

## Database Backups

A backup script is provided for production use:

```bash
./scripts/pg_backup.sh
```

This script:
- Runs VACUUM ANALYZE before backup
- Creates timestamped PostgreSQL dumps
- Retains the last 2 backups

## Project Structure

```
discord-archiver/
├── bot/                  # Discord bot service
│   ├── archiver/
│   │   ├── main.py      # Bot entry point
│   │   ├── crawler.py   # Historical message backfill
│   │   ├── repost.py    # GM message reposting
│   │   ├── db.py        # Database schema & operations
│   │   └── config.py    # Configuration
│   └── requirements.txt
├── source/              # Web viewer service
│   ├── archiver/
│   │   ├── viewer.py    # Flask REST API
│   │   ├── db.py        # Database operations
│   │   └── config.py    # Configuration
│   └── requirements.txt
└── scripts/
    ├── init_db.py       # Database initialization
    └── pg_backup.sh     # PostgreSQL backup script
```

## API Endpoints

The viewer provides a REST API (default: `http://localhost:8080/api/`):

- `GET /api/search` - Full-text message search
- `GET /api/gms` - List GM posts
- `GET /api/channels` - Channel hierarchy
- `GET /api/stats` - Database statistics
- `GET /stream` - Server-sent events for real-time updates (;blue-tracker)

## Development

For detailed development guidance, see [CLAUDE.md](CLAUDE.md).

## License

This project is provided as-is for the GemStone IV community.

## Support

For issues or questions, please open an issue on GitHub.
