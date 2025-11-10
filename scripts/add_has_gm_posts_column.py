#!/usr/bin/env python3
"""
Add has_gm_posts column to channels table for performance optimization.

This migration:
1. Adds has_gm_posts BOOLEAN column to channels table
2. Creates a partial index for fast lookups
3. Backfills data by checking existing GM posts
4. Reports statistics

Run this once after deploying the updated code.
"""
import os
import sys

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable not set")
    print("Set it with: export DATABASE_URL='postgresql://...'")
    sys.exit(1)

if DATABASE_URL.startswith("postgresql"):
    import psycopg

    def migrate():
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                print("=" * 70)
                print("Adding has_gm_posts Column Migration")
                print("=" * 70)
                print()

                # Step 1: Check if column already exists
                print("1. Checking if column already exists...")
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'channels'
                          AND column_name = 'has_gm_posts'
                    )
                """)
                if cur.fetchone()[0]:
                    print("   ⚠️  Column has_gm_posts already exists!")
                    print("   Skipping column creation, will update backfill data...")
                else:
                    # Step 2: Add the column
                    print("   ✅ Column doesn't exist, adding it...")
                    cur.execute("""
                        ALTER TABLE channels
                        ADD COLUMN has_gm_posts BOOLEAN DEFAULT FALSE
                    """)
                    print("   ✅ Added has_gm_posts column (default FALSE)")

                # Step 3: Create index
                print()
                print("2. Creating partial index...")
                try:
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_channels_has_gm_posts
                        ON channels (has_gm_posts)
                        WHERE has_gm_posts = TRUE
                    """)
                    print("   ✅ Created idx_channels_has_gm_posts (partial index)")
                except Exception as e:
                    print(f"   ⚠️  Index creation warning: {e}")

                # Step 4: Count current state
                print()
                print("3. Analyzing current data...")
                cur.execute("SELECT COUNT(*) FROM channels")
                total_channels = cur.fetchone()[0]
                print(f"   Total channels: {total_channels:,}")

                cur.execute("SELECT COUNT(*) FROM channels WHERE has_gm_posts = TRUE")
                already_marked = cur.fetchone()[0]
                print(f"   Already marked with GM posts: {already_marked:,}")

                # Step 5: Backfill data
                print()
                print("4. Backfilling has_gm_posts from existing GM posts...")
                print("   (This may take a minute...)")

                cur.execute("""
                    UPDATE channels c
                    SET has_gm_posts = TRUE
                    WHERE has_gm_posts = FALSE
                      AND EXISTS (
                        SELECT 1
                        FROM posts po
                        INNER JOIN members m ON m.member_id = po.author_id
                        WHERE po.chan_id = c.chan_id
                          AND COALESCE((m.is_gm)::text, '0') IN ('1','t','true')
                          AND NOT (COALESCE((po.deleted)::text, '0') IN ('1','t','true'))
                        LIMIT 1
                      )
                """)
                updated_count = cur.rowcount
                print(f"   ✅ Updated {updated_count:,} channels to has_gm_posts = TRUE")

                # Step 6: Verify results
                print()
                print("5. Verification...")
                cur.execute("SELECT COUNT(*) FROM channels WHERE has_gm_posts = TRUE")
                total_with_gm = cur.fetchone()[0]
                print(f"   ✅ Total channels with GM posts: {total_with_gm:,}")

                cur.execute("SELECT COUNT(*) FROM channels WHERE has_gm_posts = FALSE")
                total_without_gm = cur.fetchone()[0]
                print(f"   Channels without GM posts: {total_without_gm:,}")

                # Step 7: Test the index
                print()
                print("6. Testing query performance...")
                import time
                start = time.time()
                cur.execute("""
                    SELECT COUNT(*)
                    FROM channels
                    WHERE accessible IS TRUE AND has_gm_posts IS TRUE
                """)
                accessible_with_gm = cur.fetchone()[0]
                elapsed = time.time() - start
                print(f"   ✅ Query took {elapsed*1000:.0f}ms")
                print(f"   Accessible channels with GM posts: {accessible_with_gm:,}")

                conn.commit()

                print()
                print("=" * 70)
                print("✅ Migration Complete!")
                print("=" * 70)
                print()
                print("Summary:")
                print(f"  • Total channels: {total_channels:,}")
                print(f"  • Channels with GM posts: {total_with_gm:,}")
                print(f"  • Channels updated: {updated_count:,}")
                print(f"  • Accessible channels with GM posts: {accessible_with_gm:,}")
                print()
                print("Next steps:")
                print("  1. Restart bot to enable automatic updates")
                print("  2. Restart viewer to use new optimized queries")
                print("  3. Expected /api/channels speed: 2s → under 10ms")
                print()

    migrate()

else:
    print(f"Unsupported database type: {DATABASE_URL}")
    sys.exit(1)
