#!/usr/bin/env python3
"""
Fix GM name: Satsona -> Sotsona in gm_names table
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

# Determine database type
if DATABASE_URL.startswith("postgresql"):
    import psycopg

    def fix_name_postgresql():
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Check current name
                cur.execute("SELECT gm_name FROM gm_names WHERE author_id = %s", ("426755949701890050",))
                row = cur.fetchone()

                if row:
                    print(f"Current name in gm_names table: {row[0]}")

                    if row[0] == "Quillic":
                        print("Updating to Quilic...")
                        cur.execute("UPDATE gm_names SET gm_name = %s WHERE author_id = %s",
                                  ("Quilic", "426755949701890050"))
                        conn.commit()
                        print("✅ Successfully updated name to Quilic")
                    elif row[0] == "Quilic":
                        print("✅ Name is already correct (Quilic)")
                    else:
                        print(f"⚠️ Unexpected name: {row[0]}")
                else:
                    print("⚠️ No entry found for this GM in gm_names table")

    fix_name_postgresql()

else:
    print(f"Unsupported database type: {DATABASE_URL}")
    sys.exit(1)
