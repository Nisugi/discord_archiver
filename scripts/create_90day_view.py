#!/usr/bin/env python3
"""
Create the 90-day materialized view for optimized default searches.
This should be run once after deploying the new code.
"""
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set in .env file")
    sys.exit(1)

if DATABASE_URL.startswith("postgresql"):
    import psycopg

    def create_view():
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                print("Dropping existing materialized view (if exists)...")
                cur.execute("DROP MATERIALIZED VIEW IF EXISTS gm_posts_90day CASCADE")

                print("Creating materialized view...")
                cur.execute("""
                    CREATE MATERIALIZED VIEW gm_posts_90day AS
                    SELECT p.*
                    FROM posts p
                    JOIN members m ON m.member_id = p.author_id
                    WHERE COALESCE((m.is_gm)::text, '0') IN ('1','t','true')
                      AND NOT (COALESCE((p.deleted)::text, '0') IN ('1','t','true'))
                      AND p.created_ts >= (EXTRACT(EPOCH FROM NOW() - INTERVAL '90 days') * 1000)::BIGINT
                    ORDER BY p.created_ts DESC
                """)

                print("Creating indexes...")
                cur.execute("CREATE UNIQUE INDEX idx_gm_posts_90day_pk ON gm_posts_90day (post_id)")
                cur.execute("CREATE INDEX idx_gm_posts_90day_ts ON gm_posts_90day (created_ts DESC)")
                cur.execute("CREATE INDEX idx_gm_posts_90day_chan ON gm_posts_90day (chan_id, created_ts DESC)")

                print("Analyzing view...")
                cur.execute("ANALYZE gm_posts_90day")

                # Get row count
                cur.execute("SELECT COUNT(*) FROM gm_posts_90day")
                count = cur.fetchone()[0]

                conn.commit()

                print(f"\n✅ Successfully created materialized view with {count:,} GM posts from last 90 days")
                print("The bot will refresh this view every 10 minutes to keep it up-to-date.")

    create_view()

else:
    print(f"Unsupported database type: {DATABASE_URL}")
    sys.exit(1)
