# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Discord-Archiver is a two-component system for archiving Discord messages from the GemStone IV server into PostgreSQL and providing a web interface for searching/viewing them.

**Components:**
- **`bot/`** - Discord bot that captures messages in real-time and crawls historical messages
- **`source/`** - Flask web viewer with search API
- **`scripts/`** - Utility scripts (database backups)

The bot and viewer are **separate deployable services** that share the same PostgreSQL database.

## Running the Project

### Prerequisites
```bash
# PostgreSQL must be running and accessible
# Create database: createdb discord_archiver

# Install dependencies (in each directory)
cd bot && pip install -r requirements.txt
cd source && pip install -r requirements.txt
```

### Environment Setup
Both bot and viewer require a `.env` file with:
```
DISCORD_TOKEN=your_token_here
DATABASE_URL=postgresql://user:pass@host:port/discord_archiver
```

### Database Initialization (First-Time Setup)
Before running the bot for the first time, initialize the database schema:
```bash
cd bot
python ../scripts/init_db.py
```

This will:
1. Create all required tables, indexes, and views
2. Seed GM user IDs (100+ pre-configured)
3. Verify the setup

### Running the Bot
```bash
cd bot
python -m archiver.main
```

The bot will:
1. Connect to Discord and start real-time message capture
2. Launch background crawler for historical messages (default: 0.5 days backfill)
3. Start GM message repost task (5-minute delay before reposting)

### Running the Viewer
```bash
cd source
flask --app archiver.viewer run --port 8080
```

The viewer provides REST API endpoints at `http://localhost:8080/api/`.

### Database Backups
```bash
# Run manually or schedule via cron
./scripts/pg_backup.sh
```

Backups are stored at `/opt/discord-archiver/backups` (production path). Retains last 2 backups.

## Architecture

### Two-Service Design

```
Discord Server → Bot (bot/) → PostgreSQL ← Viewer (source/)
                   ↓
              Aggregator Guild (reposted GM messages)
```

**Bot responsibilities:**
- Real-time event handlers (on_message, on_message_edit, on_message_delete)
- Historical backfill via crawler
- GM message reposting to aggregator guild
- Notifies viewer of new GM posts via HTTP

**Viewer responsibilities:**
- Stateless Flask API for search and browsing
- Full-text search with query parsing
- Response caching (5-minute TTL)
- Statistics and channel hierarchy endpoints

### Shared Modules Pattern

`bot/archiver/config.py` and `source/archiver/config.py` are **identical copies**. Same for `db.py`. This allows the bot and viewer to be deployed independently while sharing:
- Database schema definitions
- GM user ID seeds (100+ pre-configured)
- Discord configuration (guild IDs, channel filters)
- Connection pooling logic

**When modifying shared modules:** Update both `bot/archiver/` and `source/archiver/` versions to maintain consistency.

### Database Schema Highlights

8 tables + 1 view:
- `posts` - Messages with full-text search index (tsvector on content)
- `post_revisions` - Edit/delete history (soft deletes via `deleted` flag)
- `members` - Users with `is_gm` flag
- `channels` - Discord channels/threads with parent hierarchy
- `crawl_progress` - Tracks crawler position per channel (resume on restart)
- `gm_posts_view` - Filtered view of GM-authored, non-deleted posts

Key indexes:
- `idx_posts_tsv` - GIN index for full-text search
- `idx_posts_chan_ts` - Channel browsing queries
- `idx_posts_author_ts` - Author filtering

### Async Architecture

The bot uses asyncio extensively:
- **Discord event loop** - Handles real-time messages
- **Background tasks** - Crawler and repost run concurrently without blocking
- **Connection pooling** - asyncpg pool (min=1, max=10 connections)

The viewer uses **psycopg** (synchronous) since Flask is blocking-based.

## Key Configuration

All configuration is in `config.py` (exists in both bot/ and source/):

**Performance tuning:**
- `CRAWL_BACKFILL_DAYS = 0.5` - How far back to crawl on startup
- `REQ_PAUSE = 1.5` - Rate limiting between Discord API calls (seconds)
- `PAGE_SIZE = 100` - Messages fetched per API request
- `REPOST_DELAY_SECONDS = 300` - Delay before reposting GM messages
- `VIEWER_MAX_RESULTS = 10000` - Prevents memory exhaustion on large queries

**Discord IDs:**
- `SOURCE_GUILD_ID` - The GemStone IV server being archived
- `AGGREGATOR_GUILD_ID` - Destination for GM message reposts
- `SEED_BLUE_IDS` - List of 100+ GM user IDs
- `PRIVATE_CHANNELS`, `INACCESSIBLE_CHANNELS` - Channels to skip

## Important Workflows

### Real-Time Message Capture
1. Discord event fires → `main.on_message()`
2. `save_message(db, message)` persists to PostgreSQL
3. If author is GM → add to repost queue + notify viewer via HTTP POST to `/api/notify_gm_post`
4. Full-text search index updated automatically (PostgreSQL trigger)

### Historical Backfill (Crawler)
1. On bot startup, `crawler.slow_crawl()` launches as background task
2. Iterates all channels/threads in guild
3. Fetches messages before `CRAWL_BACKFILL_DAYS` cutoff
4. Saves new messages; tracks progress in `crawl_progress` table
5. Handles 403/429 errors gracefully (caches inaccessible channels)
6. Runs continuously with configurable sleep intervals

### GM Message Reposting
1. Background task `delayed_repost_task()` checks queue every 30 seconds
2. Gets messages ready to repost (>5 minutes old)
3. `ensure_mirror()` creates mirrored channel hierarchy in aggregator guild
4. Sends message via webhook with retry logic (exponential backoff)
5. Marks message as reposted in database

### Web Search
1. HTTP GET `/api/search?q=query&channel=...&author=...`
2. `parse_search_query()` tokenizes and filters query
3. PostgreSQL full-text search using `@@` operator on `tsv` column
4. Results paginated (25 per page, max 10,000 total)
5. Response cached for 5 minutes

## Common Modifications

### Adding a new Discord event handler
Edit `bot/archiver/main.py` and add `@bot.event` decorated async function. Use `save_message()` or direct database calls via `db` module.

### Modifying database schema
1. Edit `bot/archiver/db.py` and `source/archiver/db.py` (both copies)
2. Update the `SCHEMA_SQL` constant with new table/index definitions
3. Run `python ../scripts/init_db.py` on a test database to verify
4. For production: Write a migration script (schema changes require manual migration, not handled automatically)

### Changing search behavior
Edit `source/archiver/viewer.py` → `parse_search_query()` for query parsing logic, or modify the SQL query in the search endpoint.

### Adjusting crawler behavior
Edit `bot/archiver/crawler.py`:
- `CRAWL_BACKFILL_DAYS` in config.py changes how far back to crawl
- `slow_crawl()` controls iteration logic and sleep intervals
- Inaccessible channel handling in `crawl_channel()`

## Deployment Notes

Production deployment appears to use `/opt/discord-archiver/` as the base directory (based on backup script path). The bot and viewer run as separate processes, likely managed by systemd or similar.

**Critical environment variables must be set:**
- `DISCORD_TOKEN` - Bot authentication (keep secret)
- `DATABASE_URL` - PostgreSQL connection string

**Database initialization:**
Run `scripts/init_db.py` before starting the bot for the first time. This creates all tables, indexes, and seeds GM data. The bot does NOT automatically create schema on startup (unless `RUN_SCHEMA_ON_STARTUP=1` is set).

**Graceful shutdown:**
The bot handles SIGINT/SIGTERM signals and closes database connections cleanly.
