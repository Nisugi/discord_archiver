import discord, asyncio, time, signal, sys
from collections import deque
from datetime import datetime, timezone

from .config import (
    SOURCE_GUILD_ID, AGGREGATOR_GUILD_ID, CENTRAL_CHAN_ID,
    API_PAUSE, REPOST_DELAY_SECONDS, PRIVATE_CHANNELS
)

# TOKEN may be absent in archive-only mode
try:
    from .config import TOKEN
except Exception:
    TOKEN = None
    print("[Archiver] Warning: Could not import TOKEN from config")

from .db import (
    open_db, close_db, fetchone, fetchall, save_message, check_database_health,
    update_gm_fts, add_to_repost_queue, get_messages_ready_to_repost,
    mark_message_as_reposted, mark_message_as_deleted, execute_with_retry
)
from .repost import repost_live, build_snippet, delayed_repost_task
from .crawler import slow_crawl

client = discord.Client()
db = None
db_ready = asyncio.Event()

_pending_msgs: deque[discord.Message] = deque()
_pending_edits: deque[tuple[discord.Message, discord.Message]] = deque()
_pending_deletes: deque[discord.Message] = deque()

# ── Helper functions ────────────────────────────────────────────────
async def initialize_stats_cache(db):
    try:
        print("[Archiver] Updating stats cache.")
        row = await fetchone(db, "SELECT COUNT(*) FROM gm_posts_view")
        total_posts = row[0] if row else 0
        row = await fetchone(db, "SELECT COUNT(*) FROM members WHERE is_gm = 1")
        total_gms = row[0] if row else 0
        now_ms = int(time.time() * 1000)
        upsert_sql = """
            INSERT INTO bot_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """
        await execute_with_retry(db, upsert_sql, ('stats_total_posts', str(total_posts), now_ms))
        await execute_with_retry(db, upsert_sql, ('stats_total_gms', str(total_gms), now_ms))
        await execute_with_retry(db, upsert_sql, ('stats_last_updated', str(now_ms), now_ms))
        print(f"[Archiver] Stats cache updated: {total_posts:,} posts, {total_gms} GMs")
    except Exception as e:
        print(f"[Archiver] Failed to update stats cache: {e}")

async def initialize_gm_names(db):
    from .config import GM_NAME_OVERRIDES
    upsert_sql = """
        INSERT INTO gm_names (author_id, gm_name, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (author_id) DO UPDATE SET gm_name = EXCLUDED.gm_name, updated_at = EXCLUDED.updated_at
    """
    for author_id, gm_name in GM_NAME_OVERRIDES.items():
        await execute_with_retry(db, upsert_sql, (author_id, gm_name, int(time.time() * 1000)))
    print(f"[DB] Initialized {len(GM_NAME_OVERRIDES)} GM name overrides")

async def notify_flask_server(msg: discord.Message, author_name: str):
    import aiohttp
    try:
        connector = aiohttp.TCPConnector(limit=10, limit_per_host=2)
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.post('http://localhost:8080/api/notify_gm_post',
                                    json={
                                        'id': str(msg.id),
                                        'channel_id': str(msg.channel.id),
                                        'channel_name': msg.channel.name,
                                        'author_name': author_name,
                                        'content': msg.content[:100] + ('.' if len(msg.content) > 100 else ''),
                                        'timestamp': int(msg.created_at.timestamp() * 1000)
                                    }) as resp:
                await resp.text()
    except Exception:
        # Notification failures should not break archiver
        pass

# Centralized handlers so pending-queue flushing can reuse them
async def _handle_new_message(m: discord.Message):
    await save_message(db, m)

    if m.channel.id in PRIVATE_CHANNELS:
        return

    if m.author and not m.author.bot:
        row = await fetchone(db, "SELECT is_gm FROM members WHERE member_id = ?", (str(m.author.id),))
        if row and row[0] == 1:
            print(f"[Archiver] GM message detected from {m.author.display_name} in #{m.channel.name}")
            await update_gm_fts(db, m)

            gm_name_row = await fetchone(db, "SELECT gm_name FROM gm_names WHERE author_id = ?", (str(m.author.id),))
            author_name = gm_name_row[0] if gm_name_row else m.author.display_name

            await notify_flask_server(m, author_name)
            await add_to_repost_queue(db, m)

async def _handle_edit(before: discord.Message, after: discord.Message):
    ts = int(time.time() * 1000)
    await execute_with_retry(
        db,
        "UPDATE posts SET content = ?, edited_ts = ? WHERE post_id = ?",
        (after.content, ts, str(after.id)),
    )
    await execute_with_retry(
        db,
        """INSERT INTO post_revisions
           (post_id, chan_id, author_id, content, captured_ts, is_edit)
           VALUES (?,?,?,?,?,1)""",
        (str(after.id), str(after.channel.id), str(after.author.id), after.content, ts),
    )

    if after.channel.id not in PRIVATE_CHANNELS and after.author:
        row = await fetchone(db, "SELECT is_gm FROM members WHERE member_id = ?", (str(after.author.id),))
        if row and row[0] == 1:
            await update_gm_fts(db, after)
            print(f"[Archiver] GM message edited: {after.author.display_name} in #{after.channel.name}")

async def _handle_delete(msg: discord.Message):
    ts = int(time.time() * 1000)
    await execute_with_retry(db, "UPDATE posts SET deleted = 1 WHERE post_id = ?", (str(msg.id),))
    await execute_with_retry(
        db,
        """INSERT INTO post_revisions
           (post_id, chan_id, author_id, content, captured_ts, is_edit)
           VALUES (?,?,?,?,?,1)""",
        (
            str(msg.id),
            str(msg.channel.id),
            str(msg.author.id) if msg.author else None,
            "[[DELETED]]",
            ts,
        ),
    )

    if msg.author:
        row = await fetchone(db, "SELECT is_gm FROM members WHERE member_id = ?", (str(msg.author.id),))
        if row and row[0] == 1:
            await mark_message_as_deleted(db, msg.id)
            print(f"[Archiver] GM message deleted: {msg.author.display_name} in #{msg.channel.name}")

async def _flush_pending():
    while _pending_msgs:
        m = _pending_msgs.popleft()
        await _handle_new_message(m)
    while _pending_edits:
        before, after = _pending_edits.popleft()
        await _handle_edit(before, after)
    while _pending_deletes:
        msg = _pending_deletes.popleft()
        await _handle_delete(msg)

# ── Graceful shutdown handling ─────────────────────────────────────
async def cleanup_on_exit():
    print("\n[Archiver] Shutting down gracefully.")
    try:
        await close_db()
        if not client.is_closed():
            await client.close()
    except Exception:
        pass

def signal_handler(sig, frame):
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(cleanup_on_exit())
        else:
            asyncio.run(cleanup_on_exit())
    except Exception:
        pass

# ── Discord event handlers ─────────────────────────────────────────
@client.event
async def on_ready():
    global db
    try:
        db = await open_db()
        if not db:
            print("[Archiver] FATAL: Could not open database")
            sys.exit(1)

        db_ready.set()
        await _flush_pending()

        print(f"[Archiver] Logged in as {client.user} ({client.user.id})")

        # Wait for guild
        src_guild = client.get_guild(SOURCE_GUILD_ID)
        while not src_guild:
            await asyncio.sleep(1)
            src_guild = client.get_guild(SOURCE_GUILD_ID)
        print(f"[Archiver] Connected to source guild: {src_guild.name}")

        # GM seeding verification
        from .db import seed_gm_data, verify_gm_seeding, reseed_gm_data_if_needed
        print("[Archiver] Checking GM data seeding.")
        await reseed_gm_data_if_needed(db)
        if await verify_gm_seeding(db):
            print("[Archiver] ✅ GM seeding verification passed")
        else:
            print("[Archiver] ⚠️ GM seeding verification failed - check logs")

        await initialize_stats_cache(db)

        row = await fetchone(db, "SELECT value FROM bot_metadata WHERE key = 'stats_total_posts'")
        total_posts = int(row[0]) if row else 0
        print(f"[DB] posts table currently holds {total_posts:,} rows.")

        row = await fetchone(db, "SELECT value FROM bot_metadata WHERE key = 'stats_total_gms'")
        total_gms = int(row[0]) if row else 0
        print(f"[DB] Identified {total_gms} GMs in database")

        # Start viewer
        print("[Archiver] Starting web viewer.")
        try:
            from .viewer_launcher import start_viewer_thread
            start_viewer_thread()
            await asyncio.sleep(2)
            print("[Archiver] Web viewer started successfully")
        except ImportError as e:
            print(f"[Archiver] Warning: Could not start web viewer: {e}")
        except Exception as e:
            print(f"[Archiver] Warning: Web viewer failed to start: {e}")
            import traceback
            traceback.print_exc()

        # Background tasks
        print("[Archiver] Starting background tasks.")
        print(f"[Repost] task started id={id(asyncio.current_task())}")
        if not getattr(client, "_repost_task", None) or client._repost_task.done():
            client._repost_task = asyncio.create_task(delayed_repost_task(db, client))
        print(f"[Crawler] task started id={id(asyncio.current_task())}")
        if not getattr(client, "_crawl_task", None) or client._crawl_task.done():
            client._crawl_task = asyncio.create_task(slow_crawl(src_guild, db, build_snippet, client))

        print("[Archiver] Ready! All systems operational.")

    except Exception as e:
        print(f"[Archiver] Error in on_ready: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

@client.event
async def on_message(m: discord.Message):
    if not m.guild or m.guild.id != SOURCE_GUILD_ID:
        return
    if not db_ready.is_set():
        _pending_msgs.append(m)
        return
    try:
        await _handle_new_message(m)
    except Exception as e:
        print(f"[Archiver] Error processing message {m.id}: {e}")
        import traceback
        traceback.print_exc()

@client.event
async def on_message_edit(before, after):
    if not after.guild or after.guild.id != SOURCE_GUILD_ID:
        return
    if not db_ready.is_set():
        _pending_edits.append((before, after))
        return
    try:
        await _handle_edit(before, after)
    except Exception as e:
        print(f"[Archiver] Error in on_message_edit: {e}")

@client.event
async def on_message_delete(msg):
    if not msg.guild or msg.guild.id != SOURCE_GUILD_ID:
        return
    if not db_ready.is_set():
        _pending_deletes.append(msg)
        return
    try:
        await _handle_delete(msg)
    except Exception as e:
        print(f"[Archiver] Error in on_message_delete: {e}")

@client.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"[Archiver] Error in event {event}:")
    traceback.print_exc()

# ── Main entry point ────────────────────────────────────────────────
async def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        print("[Archiver] Starting Discord bot.")
        await client.start(TOKEN)
    except KeyboardInterrupt:
        print("\n[Archiver] Received keyboard interrupt")
    except Exception as e:
        print(f"[Archiver] Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await cleanup_on_exit()

if __name__ == "__main__":
    print("[Archiver] Starting BlueTracker.")
    if not TOKEN:
        print("[Archiver] ERROR: No Discord token available!")
        print("[Archiver] Running in web-only mode...")
        from .viewer import app
        print("[Archiver] Starting Flask directly on 0.0.0.0:8080")
        app.run(host='0.0.0.0', port=8080, debug=False)
    else:
        asyncio.run(main())
