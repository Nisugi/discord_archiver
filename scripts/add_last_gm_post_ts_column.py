#!/usr/bin/env python3
"""
Add last_gm_post_ts column to channels table for performance optimization.

This migration:
1. Adds last_gm_post_ts BIGINT column to channels table
2. Creates an index for fast sorting
3. Backfills data with the most recent GM post timestamp per channel
4. Reports statistics

Run this once after deploying the updated code.
"""
import os
import sys
from dotenv import load_dotenv

# Load .env file (same as bot/viewer)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable not set")
    print("Make sure .env file exists with DATABASE_URL='postgresql://...'")
    sys.exit(1)

if DATABASE_URL.startswith("postgresql"):
    import psycopg

    def migrate():
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                print("=" * 70)
                print("Adding last_gm_post_ts Column Migration")
                print("=" * 70)
                print()

                # Step 1: Check if column already exists
                print("1. Checking if column already exists...")
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'channels'
                          AND column_name = 'last_gm_post_ts'
                    )
                """)
                if cur.fetchone()[0]:
                    print("   ⚠️  Column last_gm_post_ts already exists!")
                    print("   Skipping column creation, will update backfill data...")
                else:
                    # Step 2: Add the column
                    print("   ✅ Column doesn't exist, adding it...")
                    cur.execute("""
                        ALTER TABLE channels
                        ADD COLUMN last_gm_post_ts BIGINT
                    """)
                    print("   ✅ Added last_gm_post_ts column")

                # Step 3: Create index
                print()
                print("2. Creating index for sorting...")
                try:
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_channels_last_gm_post_ts
                        ON channels (last_gm_post_ts DESC NULLS LAST)
                    """)
                    print("   ✅ Created idx_channels_last_gm_post_ts")
                except Exception as e:
                    print(f"   ⚠️  Index creation warning: {e}")

                # Step 4: Count current state
                print()
                print("3. Analyzing current data...")
                cur.execute("SELECT COUNT(*) FROM channels WHERE has_gm_posts = TRUE")
                channels_with_gm = cur.fetchone()[0]
                print(f"   Channels with GM posts: {channels_with_gm:,}")

                cur.execute("SELECT COUNT(*) FROM channels WHERE last_gm_post_ts IS NOT NULL")
                already_populated = cur.fetchone()[0]
                print(f"   Already have last_gm_post_ts: {already_populated:,}")

                # Step 5: Backfill data
                print()
                print("4. Backfilling last_gm_post_ts from existing GM posts...")
                print("   (This may take a minute...)")

                cur.execute("""
                    UPDATE channels c
                    SET last_gm_post_ts = (
                        SELECT MAX(p.created_ts)
                        FROM posts p
                        INNER JOIN members m ON m.member_id = p.author_id
                        WHERE p.chan_id = c.chan_id
                          AND COALESCE((m.is_gm)::text, '0') IN ('1','t','true')
                          AND NOT (COALESCE((p.deleted)::text, '0') IN ('1','t','true'))
                    )
                    WHERE c.has_gm_posts = TRUE
                      AND c.last_gm_post_ts IS NULL
                """)
                updated_count = cur.rowcount
                print(f"   ✅ Updated {updated_count:,} channels with last GM post timestamp")

                # Step 6: Verify results
                print()
                print("5. Verification...")
                cur.execute("SELECT COUNT(*) FROM channels WHERE last_gm_post_ts IS NOT NULL")
                total_with_ts = cur.fetchone()[0]
                print(f"   ✅ Total channels with last_gm_post_ts: {total_with_ts:,}")

                # Get a sample of recent channels
                cur.execute("""
                    SELECT c.name,
                           to_timestamp(c.last_gm_post_ts)::text as last_post_time
                    FROM channels c
                    WHERE c.last_gm_post_ts IS NOT NULL
                    ORDER BY c.last_gm_post_ts DESC
                    LIMIT 5
                """)
                print("\n   Most recently active GM channels:")
                for row in cur.fetchall():
                    print(f"     • {row[0]} (last GM post: {row[1]})")

                # Step 7: Test the query performance
                print()
                print("6. Testing query performance...")
                import time
                start = time.time()
                cur.execute("""
                    SELECT c.chan_id, c.name
                    FROM channels c
                    WHERE c.accessible = TRUE
                      AND c.has_gm_posts = TRUE
                    ORDER BY c.last_gm_post_ts DESC NULLS LAST
                    LIMIT 100
                """)
                result_count = len(cur.fetchall())
                elapsed = time.time() - start
                print(f"   ✅ Query took {elapsed*1000:.1f}ms")
                print(f"   Retrieved {result_count} channels sorted by recency")

                conn.commit()

                print()
                print("=" * 70)
                print("✅ Migration Complete!")
                print("=" * 70)
                print()
                print("Summary:")
                print(f"  • Channels with GM posts: {channels_with_gm:,}")
                print(f"  • Channels updated: {updated_count:,}")
                print(f"  • Total with timestamps: {total_with_ts:,}")
                print()
                print("Next steps:")
                print("  1. Restart bot to enable automatic updates")
                print("  2. Restart viewer to use new optimized queries")
                print("  3. Expected /api/channels speed: ~6s → under 10ms")
                print()

    migrate()

else:
    print(f"Unsupported database type: {DATABASE_URL}")
    sys.exit(1)
