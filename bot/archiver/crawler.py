import asyncio, time, discord
from datetime import datetime, timedelta, timezone
from discord import TextChannel, ForumChannel
from .db import fetchone, execute_with_retry, save_message
from .repost import cleanup_caches
from .config import REQ_PAUSE, PAGE_SIZE, CUTOFF_DAYS, CRAWL_VERBOSITY, PRIVATE_CHANNELS, SKIP_CRAWL_FORUMS, COMMIT_BATCH_SIZE

save_counter = 0
inaccessible_channels = set()  # Cache of channel IDs we can't access
finished_channels = set()  # Track channels that have been fully crawled
crawler_active = True  # Global flag to track if crawler should continue

async def get_last_seen_id(db, chan_id):
    """Get the last message ID we've seen in this channel"""
    # First check progress table
    row = await fetchone(db, "SELECT last_seen_id FROM crawl_progress WHERE chan_id = ?", (str(chan_id),))
    if row and row[0]:
        return int(row[0])
    
    # Fall back to posts table for backward compatibility
    row = await fetchone(db, "SELECT MAX(post_id) FROM posts WHERE chan_id = ?", (str(chan_id),))
    return int(row[0]) if row and row[0] else None

async def update_last_seen_id(db, chan_id, message_id):
    """Update the last message ID we've seen in this channel"""
    timestamp = int(time.time() * 1000)
    try:
        last_seen_int = int(message_id)
    except (TypeError, ValueError):
        # Fallback to the raw value if it can't be coerced; keep crawler running
        last_seen_int = message_id

    await execute_with_retry(
        db,
        """
        INSERT INTO crawl_progress (chan_id, last_seen_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (chan_id) DO UPDATE
        SET last_seen_id = EXCLUDED.last_seen_id,
            updated_at = EXCLUDED.updated_at
        """,
        (str(chan_id), last_seen_int, timestamp)
    )

async def save_channel(db, chan_id, name, accessible=True, parent_id=None):
    """
    Insert or update a row in the channels table.
    """
    await execute_with_retry(
        db,
        """
        INSERT INTO channels (chan_id, name, accessible)
        VALUES (?, ?, ?)
        ON CONFLICT (chan_id) DO UPDATE
        SET name = EXCLUDED.name,
            accessible = EXCLUDED.accessible
        """,
        (str(chan_id), name, 1 if accessible else 0)
    )
    
    # Update parent_id if provided
    if parent_id:
        await execute_with_retry(
            db,
            "UPDATE channels SET parent_id = ? WHERE chan_id = ?",
            (str(parent_id), str(chan_id))
        )

async def crawl_one(ch, cutoff, me, db, build_snippet):
    """Crawl one channel or thread for messages"""
    global save_counter, inaccessible_channels, crawler_active

    if not crawler_active:
        return False  # Signal to stop crawling

    # Save basic channel info
    parent_id = ch.parent_id if isinstance(ch, discord.Thread) else None
    await save_channel(db, ch.id, ch.name, accessible=True, parent_id=parent_id)
    
    # Skip forums during crawl if configured
    if isinstance(ch, discord.ForumChannel) and ch.id in SKIP_CRAWL_FORUMS:
        print(f"[crawler] 📋 Skipping forum #{ch.name} (configured to skip due to volume)")
        return True
    
    # Skip if we already know we can't access this channel
    if ch.id in inaccessible_channels:
        return True  # Skipped but not finished

    if ch.id in finished_channels:
        return True  # Already finished
    
    # Forum channels don't have messages directly - skip them
    if isinstance(ch, discord.ForumChannel):
        print(f"[crawler] 📋 Skipping forum channel #{ch.name} (forums only contain threads)")
        return True
        
    if not ch.permissions_for(me).read_message_history: 
        inaccessible_channels.add(ch.id)
        await save_channel(db, ch.id, ch.name, accessible=False)
        print(f"[crawler] 🚫 No access to #{ch.name} (ID: {ch.id})")
        return True

    # In catch-up mode, always start from the present
    # if catch_up_mode:
    before_obj = None  # Start from most recent message
    # else:
    #     # Normal mode: continue from where we left off
    #     earliest_seen = await get_last_seen_id(db, ch.id)
    #     before_obj = discord.Object(id=earliest_seen) if earliest_seen else None

    
    pulled = 0
    saved_this_run = 0
    new_messages_found = 0
    reached_cutoff = False
    earliest_seen = None

    try:
        # Use timeout to prevent hanging on slow channels
        async def _get_messages():
            return [m async for m in ch.history(
                limit=PAGE_SIZE,
                before=before_obj,
                oldest_first=False)]
        messages = await asyncio.wait_for(_get_messages(), timeout=15.0)

        if not messages:
            finished_channels.add(ch.id)
            await update_last_seen_id(db, ch.id, earliest_seen or "0")

            return True  # Channel is fully crawled

        messages.reverse()  # now oldest→newest
        new_earliest = messages[0].id
        
        for m in messages:
            if m.created_at < cutoff:
                reached_cutoff = True
                finished_channels.add(ch.id)
                break
            pulled += 1

            existing = await fetchone(db, "SELECT post_id FROM posts WHERE post_id = ?", (str(m.id),))
            if existing:
                continue
                
            new_messages_found += 1            
            
            await save_message(db, m)
            save_counter += 1
            saved_this_run += 1
            # Yield occasionally to keep event loop responsive
            if saved_this_run % COMMIT_BATCH_SIZE == 0:
                await asyncio.sleep(0)

        # update progress tracker
        if new_earliest and new_earliest != earliest_seen:
            await update_last_seen_id(db, ch.id, new_earliest)

        
        # Show progress
        ch_type = "thread" if isinstance(ch, discord.Thread) else "channel"
        if save_counter % CRAWL_VERBOSITY == 0 or new_messages_found > 0:
            if new_messages_found > 0:
                print(f"[crawler] #{ch.name:<30} ({ch_type:<7}) pulled={pulled:<3} new={new_messages_found:<3} saved={saved_this_run:<2} total={save_counter:<5}")
            elif pulled > 0:
                print(f"[crawler] #{ch.name:<30} ({ch_type:<7}) pulled={pulled:<3} (all duplicates)")
        
        # If we hit the cutoff, this channel is done
        if reached_cutoff:
            return True
            
    except asyncio.TimeoutError:
        print(f"[crawler] ⚠️  TIMEOUT in #{ch.name} - skipping this pass")
    except discord.Forbidden:
        inaccessible_channels.add(ch.id)
        await save_channel(db, ch.id, ch.name, accessible=False)
        print(f"[crawler] 🚫 Forbidden access to #{ch.name} (ID: {ch.id})")
    except discord.HTTPException as e:
        if e.status == 403:
            inaccessible_channels.add(ch.id)
            await save_channel(db, ch.id, ch.name, accessible=False)
            print(f"[crawler] 🚫 HTTP 403 for #{ch.name} (ID: {ch.id})")
        elif 500 <= e.status < 600 or e.status == 429:
            print(f"[crawler] ⚠️  Skipping #{ch.name}: {e.status} {e.text or ''}".strip())
        else:
            print(f"[crawler] ❌ Error in #{ch.name}: {e}")
    except Exception as e:
        print(f"[crawler] ❌ Unexpected error in #{ch.name}: {e}")
        import traceback
        traceback.print_exc()
    
    return False  # Not finished if we got here

async def iter_all_threads(parent: discord.abc.GuildChannel):
    """Yield active threads first, then archived public threads."""
    # Active threads first
    for th in parent.threads:
        yield th

    # Then archived public threads
    try:
        archived = []
        try:
            iterator = parent.archived_threads(limit=None, private=False)
        except TypeError:
            iterator = parent.archived_threads(limit=None)
        async for th in iterator:
            archived.append(th)
        
        # Reverse to get oldest-first order
        for th in reversed(archived):
            yield th
    except discord.Forbidden:
        print(f"[crawler] No access to archived threads in #{parent.name}")
        return
    except discord.HTTPException as e:
        if 500 <= e.status < 600 or e.status == 429:
            print(f"[crawler] Skipping archived threads in #{parent.name}: "
                  f"{e.status} {e.text or ''}".strip())
            return
        raise

async def slow_crawl(src_guild, db, build_snippet, client):
    """Main crawler loop - crawls back 10 days then stops"""
    global inaccessible_channels, finished_channels, crawler_active
    finished_channels = set()  # Reset for this crawl run

    try:
        # Ensure progress table exists


        me = src_guild.get_member(client.user.id) or await src_guild.fetch_member(client.user.id)
        cutoff = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=CUTOFF_DAYS)

        cleanup_counter = 0

        # Calculate accessible channels
        text_like_channels = []
        for c in src_guild.channels:
            if isinstance(c, (TextChannel, ForumChannel)):
                text_like_channels.append(c)

        all_channels = [c for c in text_like_channels]  # Don't filter anything out during crawl

        print(f"[crawler] Starting {CUTOFF_DAYS}-day backfill crawler")
        print(f"[crawler] Total channels: {len(text_like_channels)}, Non-ignored: {len(all_channels)}")
        print(f"[crawler] Cutoff date: {cutoff.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        channels_processed = 0
        threads_processed = 0
        start_time = time.time()

        # Crawl all channels and threads
        for parent in all_channels:
            if parent.id in inaccessible_channels:
                continue

            channels_processed += 1
            print(f"\n[crawler] 📁 Processing channel #{parent.name} ({channels_processed}/{len(all_channels)})")

            # Crawl the parent channel
            finished = await crawl_one(parent, cutoff, me, db, build_snippet)
            await asyncio.sleep(REQ_PAUSE)

            # Crawl all threads in this channel
            thread_count = 0
            async for th in iter_all_threads(parent):
                if th.id in inaccessible_channels or th.id in finished_channels:
                    continue

                thread_count += 1
                threads_processed += 1
                print(f"[crawler] 🧵 Processing thread #{th.name} (#{thread_count} in #{parent.name})")
                await crawl_one(th, cutoff, me, db, build_snippet)
                await asyncio.sleep(REQ_PAUSE)

            if thread_count > 0:
                print(f"[crawler] ✅ Completed #{parent.name} - processed {thread_count} threads")

        # Calculate and display final statistics
        elapsed_time = time.time() - start_time
        hours = int(elapsed_time // 3600)
        minutes = int((elapsed_time % 3600) // 60)

        print(f"\n[crawler] 🏁 BACKFILL COMPLETE!")
        print(f"[crawler] 📊 Final statistics:")
        print(f"[crawler]    - Channels processed: {channels_processed}")
        print(f"[crawler]    - Threads processed: {threads_processed}")
        print(f"[crawler]    - Messages saved: {save_counter:,}")
        print(f"[crawler]    - Inaccessible channels: {len(inaccessible_channels)}")
        print(f"[crawler]    - Time elapsed: {hours}h {minutes}m")
        print(f"[crawler] ✅ Crawler task shutting down - backfill complete!")

        # Set crawler as inactive
        crawler_active = False

        # Cleanup
        cleanup_caches()

    except Exception as e:
        print(f"[crawler] ❌ FATAL ERROR in slow_crawl: {e}")
        import traceback
        traceback.print_exc()
        crawler_active = False

def get_inaccessible_count():
    """Get count of cached inaccessible channels (for monitoring)"""
    return len(inaccessible_channels)

def clear_inaccessible_cache():
    """Manually clear the inaccessible channels cache"""
    global inaccessible_channels
    count = len(inaccessible_channels)
    inaccessible_channels.clear()
    return count

def reset_finished_channels():
    """Reset the finished channels set for a new crawl sweep"""
    global finished_channels
    finished_channels.clear()

async def cleanup_old_progress(db, days=30):
    """Clean up old progress entries for channels that no longer exist"""
    cutoff_ts = int((time.time() - (days * 24 * 60 * 60)) * 1000)
    rows = await fetchall(
        db,
        "DELETE FROM crawl_progress WHERE updated_at < ? RETURNING 1",
        (cutoff_ts,),
    )
    deleted = len(rows)
    if deleted > 0:

        print(f"[crawler] Cleaned up {deleted} old progress entries")
    return deleted
