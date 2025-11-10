#!/usr/bin/env python3
"""
Cleanup script to remove unused materialized view and add time-based index.

This script:
1. Adds idx_posts_ts index for efficient time-ordered queries
2. Drops the unused gm_posts_90day materialized view
3. Removes associated indexes

Run this after deploying the updated code.
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

    def cleanup():
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                print("=" * 70)
                print("Materialized View Cleanup & Index Optimization")
                print("=" * 70)
                print()

                # Step 1: Add the simple time-based index
                print("1. Adding simple time-based index...")
                try:
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_posts_ts
                        ON posts (created_ts DESC)
                    """)
                    print("   ✅ Created idx_posts_ts (time-ordered queries)")
                except Exception as e:
                    print(f"   ⚠️  Index creation failed (may already exist): {e}")

                # Step 2: Drop the unused materialized view
                print()
                print("2. Dropping unused materialized view...")
                try:
                    cur.execute("DROP MATERIALIZED VIEW IF EXISTS gm_posts_90day CASCADE")
                    print("   ✅ Dropped gm_posts_90day materialized view")
                except Exception as e:
                    print(f"   ⚠️  Drop failed: {e}")

                # Step 3: Verify the cleanup
                print()
                print("3. Verifying cleanup...")

                # Check that regular view still exists
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_views
                        WHERE schemaname = 'public' AND viewname = 'gm_posts_view'
                    )
                """)
                if cur.fetchone()[0]:
                    print("   ✅ gm_posts_view (regular view) is intact")
                else:
                    print("   ❌ ERROR: gm_posts_view is missing!")

                # Check that materialized view is gone
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_matviews
                        WHERE schemaname = 'public' AND matviewname = 'gm_posts_90day'
                    )
                """)
                if not cur.fetchone()[0]:
                    print("   ✅ gm_posts_90day materialized view removed")
                else:
                    print("   ⚠️  gm_posts_90day still exists")

                # Check that new index exists
                cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE schemaname = 'public'
                        AND tablename = 'posts'
                        AND indexname = 'idx_posts_ts'
                    )
                """)
                if cur.fetchone()[0]:
                    print("   ✅ idx_posts_ts index created successfully")
                else:
                    print("   ❌ ERROR: idx_posts_ts index missing!")

                conn.commit()

                print()
                print("=" * 70)
                print("✅ Cleanup Complete!")
                print("=" * 70)
                print()
                print("What changed:")
                print("  • Removed gm_posts_90day materialized view (unused)")
                print("  • Added idx_posts_ts for fast time-ordered queries")
                print("  • Regular gm_posts_view still works (always up-to-date)")
                print()
                print("Next steps:")
                print("  1. Restart the bot to stop the refresh task")
                print("  2. New posts will appear instantly (no 10-minute delay)")
                print()

    cleanup()

else:
    print(f"Unsupported database type: {DATABASE_URL}")
    sys.exit(1)
