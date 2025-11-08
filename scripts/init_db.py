#!/usr/bin/env python3
"""
Database initialization script for discord-archiver.

Run this script once before starting the bot for the first time.
Requires: PostgreSQL database created and DATABASE_URL in .env file

Usage:
    cd bot
    python ../scripts/init_db.py
"""

import asyncio
import sys
from pathlib import Path

# Add bot directory to path so we can import archiver modules
bot_dir = Path(__file__).parent.parent / "bot"
sys.path.insert(0, str(bot_dir))

from archiver.db import open_db, close_db, seed_gm_data, verify_gm_seeding, _split_statements, SCHEMA_SQL


async def initialize_database():
    """Initialize the database schema and seed GM data."""
    print("[init_db] Connecting to database...")

    try:
        db = await open_db()

        # Create schema
        print("[init_db] Creating database schema...")
        async with db.acquire() as conn:
            for i, stmt in enumerate(_split_statements(SCHEMA_SQL), 1):
                try:
                    await conn.execute(stmt)
                    print(f"[init_db]   Executed statement {i}")
                except Exception as e:
                    print(f"[init_db]   Warning on statement {i}: {e}")

        print("[init_db] ✅ Schema created successfully")

        # Seed GM data
        print("[init_db] Seeding GM data...")
        await seed_gm_data(db)
        print("[init_db] ✅ GM data seeded")

        # Verify
        print("[init_db] Verifying GM seeding...")
        if await verify_gm_seeding(db):
            print("[init_db] ✅ GM seeding verification passed")
        else:
            print("[init_db] ⚠️  GM seeding verification failed - check configuration")
            return False

        print("\n[init_db] 🎉 Database initialization complete!")
        print("[init_db] You can now start the bot with: python -m archiver.main")
        return True

    except Exception as e:
        print(f"[init_db] ❌ Error during initialization: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        await close_db()


def main():
    """Main entry point."""
    print("=" * 60)
    print("Discord Archiver - Database Initialization")
    print("=" * 60)
    print()

    # Check if we're in the right directory
    if not (bot_dir / "archiver").exists():
        print("❌ Error: Could not find bot/archiver directory")
        print("   Please run this script from the project root or bot directory")
        sys.exit(1)

    success = asyncio.run(initialize_database())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
