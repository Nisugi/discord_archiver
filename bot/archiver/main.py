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
    open_db, close_db, fetchone, fetchall, save_message,
    update_gm_fts, add_to_repost_queue, get_messages_ready_to_repost,
    mark_message_as_reposted, mark_message_as_deleted, execute_with_retry,
    refresh_90day_view
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
    await execute_with_retry(db, "UPDATE posts SET deleted = '1' WHERE post_id = ?", (str(msg.id),))
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

async def refresh_90day_view_task(db):
    """
    Background task to refresh the 90-day materialized view every 10 minutes.
    This keeps the default "Reset" view fast for users.
    """
    await asyncio.sleep(60)  # Wait 1 minute before first refresh

    while True:
        try:
            await refresh_90day_view(db)
        except Exception as e:
            print(f"[Refresh] Error refreshing 90-day view: {e}")

        # Wait 10 minutes before next refresh
        await asyncio.sleep(600)

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

        # Background tasks
        print("[Archiver] Starting background tasks.")
        print(f"[Repost] task started id={id(asyncio.current_task())}")
        if not getattr(client, "_repost_task", None) or client._repost_task.done():
            client._repost_task = asyncio.create_task(delayed_repost_task(db, client))
        print(f"[Crawler] task started id={id(asyncio.current_task())}")
        if not getattr(client, "_crawl_task", None) or client._crawl_task.done():
            client._crawl_task = asyncio.create_task(slow_crawl(src_guild, db, build_snippet, client))
        print(f"[Refresh] task started id={id(asyncio.current_task())}")
        if not getattr(client, "_refresh_task", None) or client._refresh_task.done():
            client._refresh_task = asyncio.create_task(refresh_90day_view_task(db))

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
        raise SystemExit(1)
    asyncio.run(main())
