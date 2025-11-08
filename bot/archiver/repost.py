import discord, asyncio
from datetime import datetime, timezone
from .config import CENTRAL_CHAN_ID, CREATE_COOLDOWN, PRIVATE_CHANNELS, REPOST_DELAY_SECONDS, API_PAUSE
from .db import get_messages_ready_to_repost, mark_message_as_reposted, mark_message_as_deleted

# Caches
mirror_cache = {}  # (src_guild_id, src_chan_id) -> mirror channel/thread
wh_cache = {}      # mirror_chan_id -> webhook

def should_repost(msg):
    """Check if message should be reposted"""
    if msg.channel.id in PRIVATE_CHANNELS: 
        return False
    
    return True

async def make_read_only(channel: discord.TextChannel):
    """Make a channel read-only for @everyone"""
    try:
        default = channel.guild.default_role
        ov = channel.overwrites_for(default) or discord.PermissionOverwrite()
        if ov.send_messages is False: 
            return  # Already read-only
        ov.send_messages = False
        await channel.set_permissions(default, overwrite=ov)
    except discord.Forbidden:
        print(f"[repost] Cannot make #{channel.name} read-only - missing permissions")

async def ensure_mirror(dst_guild, src_channel):
    """Create/get mirror channel or thread in destination guild"""
    key = (src_channel.guild.id, src_channel.id)
    if key in mirror_cache: 
        return mirror_cache[key]

    parent_src = src_channel.parent if isinstance(src_channel, discord.Thread) else src_channel
    cat_name = parent_src.category.name if parent_src.category else "No-Category"

    # Find or create category
    category = discord.utils.get(dst_guild.categories, name=cat_name)
    if not category:
        try:
            category = await dst_guild.create_category(cat_name)
            await asyncio.sleep(CREATE_COOLDOWN)
        except discord.HTTPException as e:
            print(f"[repost] Failed to create category {cat_name}: {e}")
            raise

    # Find or create parent channel
    mirror_parent = discord.utils.get(category.text_channels, name=parent_src.name)
    if not mirror_parent:
        try:
            mirror_parent = await category.create_text_channel(parent_src.name)
            await make_read_only(mirror_parent)
            await asyncio.sleep(CREATE_COOLDOWN)
        except discord.HTTPException as e:
            print(f"[repost] Failed to create channel {parent_src.name}: {e}")
            raise

    # If source is not a thread, return the parent channel
    if not isinstance(src_channel, discord.Thread):
        mirror_cache[key] = mirror_parent
        return mirror_parent

    # Find or create thread
    mirror_thread = discord.utils.get(mirror_parent.threads, name=src_channel.name)
    if not mirror_thread:
        try:
            mirror_thread = await mirror_parent.create_thread(
                name=src_channel.name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=src_channel.auto_archive_duration
            )
            await asyncio.sleep(CREATE_COOLDOWN)
        except discord.HTTPException as e:
            print(f"[repost] Failed to create thread {src_channel.name}: {e}")
            raise

    mirror_cache[key] = mirror_thread
    return mirror_thread

async def get_webhook(channel):
    """Get or create webhook for channel"""
    if channel.id in wh_cache: 
        return wh_cache[channel.id]
    
    try:
        hooks = await channel.webhooks()
        for h in hooks:
            if h.name == "BlueTracker":
                wh_cache[channel.id] = h
                return h
        
        wh = await channel.create_webhook(name="BlueTracker")
        wh_cache[channel.id] = wh
        return wh
    except discord.HTTPException as e:
        print(f"[repost] Failed to create webhook in #{channel.name}: {e}")
        raise

async def safe_webhook_send(webhook, max_retries=3, **kwargs):
    """Send webhook message with retry logic for rate limits"""
    for attempt in range(max_retries):
        try:
            await webhook.send(**kwargs)
            return
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = getattr(e, 'retry_after', 5)
                print(f"[webhook] Rate limited, waiting {retry_after}s...")
                await asyncio.sleep(retry_after)
            elif attempt == max_retries - 1:
                print(f"[webhook] Failed after {max_retries} attempts: {e}")
                raise
            else:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff

async def build_snippet(msg: discord.Message) -> str:
    """Build message snippet with reply context if available"""
    snippet = msg.content or "(embed/attachment only)"

    # Handle replies
    if not msg.reference:
        return snippet

    parent = None
    if msg.reference and isinstance(msg.reference.resolved, discord.Message):
        parent = msg.reference.resolved
    else:
        # Try to fetch the referenced message
        try:
            parent = await msg.channel.fetch_message(msg.reference.message_id)
            await asyncio.sleep(0.2)  # Gentle rate limiting
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            parent = None

    if parent:
        p_txt = parent.content or "(embed/attachment only)"
        if len(p_txt) > 100:
            p_txt = p_txt[:97] + "…"
        snippet = f"> **↪️   {parent.author.display_name}:** {p_txt}\n\n" + snippet

    return snippet

async def repost_live(msg: discord.Message, dst_guild, client, db):
    """Send GM/CM message to both central channel and mirrored hierarchy"""
    jump = f"https://discord.com/channels/{msg.guild.id}/{msg.channel.id}/{msg.id}"
    snippet = await build_snippet(msg)

    from .db import get_gm_display_name
    display_name = await get_gm_display_name(db, msg.author.id, msg.author.display_name)
    body = (f"{display_name} ({msg.guild.name} • #{msg.channel.name}):\n"
            f"{snippet}\n{jump}")

    # CRITICAL: Truncate if too long for Discord (2000 char limit)
    if len(body) > 2000:
        # Calculate how much space we need for the jump URL and formatting
        overhead = len(f"{display_name} ({msg.guild.name} • #{msg.channel.name}):\n\n{jump}")
        available_space = 2000 - overhead - 20  # Leave some buffer
        
        if available_space > 0:
            truncated_snippet = snippet[:available_space] + "... [truncated]"
            body = (f"{display_name} ({msg.guild.name} • #{msg.channel.name}):\n"
                    f"{truncated_snippet}\n{jump}")
        else:
            # If even the header is too long, just use a minimal version
            body = f"{display_name}: [Message too long to display]\n{jump}"

    # Send to central feed
    central = client.get_channel(CENTRAL_CHAN_ID)
    if central:
        try:
            wh = await get_webhook(central)
            await safe_webhook_send(
                wh,
                content=body,
                username=display_name,
                avatar_url=msg.author.display_avatar.url,
                allowed_mentions=discord.AllowedMentions.none()
            )
            print(f"[repost] Posted to central channel: {display_name}")
        except Exception as e:
            print(f"[repost] Failed to send to central channel: {e}")

    # Send to mirrored hierarchy
    try:
        mirror = await ensure_mirror(dst_guild, msg.channel)
        is_thread = isinstance(mirror, discord.Thread)
        parent = mirror.parent if is_thread else mirror
        wh2 = await get_webhook(parent)
        kwargs = dict(
            content=body,
            username=display_name,
            avatar_url=msg.author.display_avatar.url,
            allowed_mentions=discord.AllowedMentions.none()
        )
        if is_thread:
            kwargs["thread"] = mirror
        await safe_webhook_send(wh2, **kwargs)
        print(f"[repost] Posted to mirror: #{mirror.name}")
    except Exception as e:
        print(f"[repost] Failed to send to mirror: {e}")

async def delayed_repost_task(db, client):
    """Background task to repost messages after delay"""
    from .config import AGGREGATOR_GUILD_ID
    
    print(f"[DelayedRepost] Starting background task with {REPOST_DELAY_SECONDS}s delay")
    
    # Wait a bit for client to be fully ready
    await asyncio.sleep(5)
    
    while True:
        try:
            # Use UTC time consistently
            current_utc_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            
            # Get messages ready to repost
            ready_messages = await get_messages_ready_to_repost(db)
            
            if ready_messages:
                dst_guild = client.get_guild(AGGREGATOR_GUILD_ID)
                if not dst_guild:
                    print(f"[DelayedRepost] Warning: Aggregator guild {AGGREGATOR_GUILD_ID} not found")
                    await asyncio.sleep(30)
                    continue
                    
                print(f"[DelayedRepost] Processing {len(ready_messages)} messages ready for repost")
                
                for msg_id, chan_id, author_id, ts, content in ready_messages:
                    try:
                        ts_val = int(ts)
                    except (TypeError, ValueError):
                        try:
                            ts_val = int(float(ts))
                        except (TypeError, ValueError):
                            print(f"[DelayedRepost] Skipping message {msg_id}: invalid timestamp {ts!r}")
                            await mark_message_as_reposted(db, msg_id)
                            continue
                    
                    # Calculate message age
                    message_age_seconds = (current_utc_ms - ts_val) / 1000
                    
                    try:
                        # Get the current message to check if it still exists
                        channel = client.get_channel(int(chan_id))
                        if not channel:
                            channel = await client.fetch_channel(int(chan_id))
                        
                        current_msg = await channel.fetch_message(int(msg_id))
                        
                        # Use current message content (in case it was edited)
                        await repost_live(current_msg, dst_guild, client, db)
                        await mark_message_as_reposted(db, msg_id)
                        
                        print(f"[DelayedRepost] ✅ Reposted message from {current_msg.author.display_name} (age: {message_age_seconds:.1f}s)")
                        
                    except (discord.NotFound, discord.Forbidden):
                        # Message was deleted or we lost access
                        await mark_message_as_deleted(db, msg_id)
                        print(f"[DelayedRepost] Message {msg_id} deleted or inaccessible")
                        
                    except discord.HTTPException as e:
                        print(f"[DelayedRepost] Error fetching message {msg_id}: {e}")
                        # Don't retry forever
                        if message_age_seconds > 3600:  # 1 hour old
                            await mark_message_as_reposted(db, msg_id)
                            print(f"[DelayedRepost] Message too old, removing from queue")
                            
                    except Exception as e:
                        print(f"[DelayedRepost] Unexpected error processing message {msg_id}: {e}")
                        # Mark as reposted to avoid infinite retry
                        await mark_message_as_reposted(db, msg_id)
                        
                    # Rate limiting between reposts
                    await asyncio.sleep(API_PAUSE)
            
        except Exception as e:
            print(f"[DelayedRepost] Error in background task: {e}")
            import traceback
            traceback.print_exc()
        
        # Wait 30 seconds before checking again
        await asyncio.sleep(30)

def cleanup_caches():
    """Clean up caches to prevent memory leaks"""
    global mirror_cache, wh_cache
    
    # Keep only recent entries
    if len(mirror_cache) > 1000:
        keys_to_remove = list(mirror_cache.keys())[:len(mirror_cache)//2]
        for key in keys_to_remove:
            del mirror_cache[key]
        print(f"[repost] Cleaned mirror cache: {len(keys_to_remove)} entries removed")
    
    if len(wh_cache) > 100:
        keys_to_remove = list(wh_cache.keys())[:len(wh_cache)//2]  
        for key in keys_to_remove:
            del wh_cache[key]
        print(f"[repost] Cleaned webhook cache: {len(keys_to_remove)} entries removed")
