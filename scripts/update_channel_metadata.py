#!/usr/bin/env python3
"""
Update all channel metadata in the database from Discord.

This script fetches all channels/threads from the guild and updates
the database with correct type, parent_id, and other metadata.

Run this once to fix the 9,386 channels with NULL type.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add parent directory to path so we can import from bot
sys.path.insert(0, str(Path(__file__).parent.parent / 'bot'))

from archiver.config import SOURCE_GUILD_ID, DATABASE_URL
from archiver.db import open_db
import discord


async def update_channel_metadata(client: discord.Client):
    """Update all channel metadata in the database."""

    print("Connecting to database...")
    pool = await open_db()

    print(f"Fetching guild {SOURCE_GUILD_ID}...")
    guild = client.get_guild(SOURCE_GUILD_ID)
    if not guild:
        print(f"ERROR: Could not find guild {SOURCE_GUILD_ID}")
        return

    print(f"Found guild: {guild.name}")

    # Get all channels (includes threads)
    all_channels = []

    # Get regular channels
    for channel in guild.channels:
        all_channels.append(channel)

    # Get all threads (active and archived)
    print("Fetching threads...")
    for channel in guild.text_channels:
        try:
            # Get active threads
            active_threads = channel.threads
            all_channels.extend(active_threads)

            # Get archived threads (this can be slow)
            async for thread in channel.archived_threads(limit=None):
                all_channels.append(thread)
        except Exception as e:
            print(f"Warning: Could not fetch threads from {channel.name}: {e}")

    print(f"\nFound {len(all_channels)} total channels/threads")
    print("Updating database...")

    updated = 0
    skipped = 0

    async with pool.acquire() as conn:
        for channel in all_channels:
            try:
                # Extract metadata
                chan_id = str(channel.id)
                name = channel.name

                # Get type - extract enum name
                channel_type = getattr(channel, "type", None)
                channel_type_str = channel_type.name if channel_type and hasattr(channel_type, 'name') else None

                # Get parent_id
                parent_id = str(channel.parent_id) if getattr(channel, "parent_id", None) else None

                # Get topic
                topic = getattr(channel, "topic", None)

                # Get guild_id
                guild_id = str(guild.id)

                # Update database
                await conn.execute(
                    """
                    INSERT INTO channels (chan_id, guild_id, parent_id, name, type, topic, accessible)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (chan_id) DO UPDATE SET
                        guild_id = EXCLUDED.guild_id,
                        parent_id = EXCLUDED.parent_id,
                        name = EXCLUDED.name,
                        type = EXCLUDED.type,
                        topic = EXCLUDED.topic,
                        accessible = EXCLUDED.accessible
                    """,
                    chan_id,
                    guild_id,
                    parent_id,
                    name,
                    channel_type_str,
                    topic,
                    True
                )

                updated += 1

                if updated % 100 == 0:
                    print(f"  Updated {updated} channels...")

            except Exception as e:
                print(f"Warning: Could not update channel {channel.name} ({channel.id}): {e}")
                skipped += 1

    await pool.close()

    print(f"\n✅ Complete!")
    print(f"  Updated: {updated}")
    print(f"  Skipped: {skipped}")
    print(f"  Total:   {len(all_channels)}")


async def main():
    """Main entry point."""

    # Get Discord token from environment
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        print("ERROR: DISCORD_TOKEN environment variable not set")
        print("Load it from .env file:")
        print("  source /opt/discord-archiver/.env")
        print("  python scripts/update_channel_metadata.py")
        return

    print("Starting Discord client...")

    # Create self-bot client (no intents needed for self-bots)
    client = discord.Client()

    @client.event
    async def on_ready():
        print(f"Logged in as {client.user}")
        try:
            await update_channel_metadata(client)
        finally:
            await client.close()

    # Connect to Discord
    await client.start(token)


if __name__ == '__main__':
    print("=" * 60)
    print("Channel Metadata Update Script")
    print("=" * 60)
    print()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n\nERROR: {e}")
        import traceback
        traceback.print_exc()
