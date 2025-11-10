#!/usr/bin/env python3
"""
Health monitoring script for BlueTracker viewer
Checks database performance and logs slow queries
Can be run periodically via cron or systemd timer
"""
import os
import sys
import time
import requests
from pathlib import Path
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "source"))

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
VIEWER_URL = os.getenv("VIEWER_URL", "http://localhost:8080")
LOG_FILE = os.getenv("MONITOR_LOG", "/var/log/discord-archiver/monitor.log")

def log(message):
    """Log message with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)

    # Also write to log file if path exists
    log_path = Path(LOG_FILE)
    if log_path.parent.exists():
        with open(log_path, "a") as f:
            f.write(log_line + "\n")

def check_database_connection():
    """Test database connectivity"""
    try:
        if DATABASE_URL.startswith("postgresql"):
            import psycopg
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            log("✅ Database connection: OK")
            return True
        else:
            log(f"⚠️  Unknown database type in DATABASE_URL")
            return False
    except Exception as e:
        log(f"❌ Database connection FAILED: {e}")
        return False

def check_viewer_health():
    """Check viewer API health"""
    try:
        response = requests.get(f"{VIEWER_URL}/health", timeout=5)
        if response.status_code == 200:
            log(f"✅ Viewer health check: OK ({response.elapsed.total_seconds():.2f}s)")
            return True
        else:
            log(f"⚠️  Viewer health check returned {response.status_code}")
            return False
    except requests.exceptions.Timeout:
        log(f"❌ Viewer health check TIMEOUT (>5s)")
        return False
    except Exception as e:
        log(f"❌ Viewer health check FAILED: {e}")
        return False

def check_search_performance():
    """Test search endpoint performance"""
    try:
        # Test basic search (no parameters)
        start = time.time()
        response = requests.get(f"{VIEWER_URL}/api/search", timeout=30)
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            result_count = len(data.get('results', []))
            log(f"✅ Search endpoint: OK ({elapsed:.2f}s, {result_count} results)")

            if elapsed > 3.0:
                log(f"⚠️  Search was SLOW: {elapsed:.2f}s")

            return True
        else:
            log(f"❌ Search endpoint returned {response.status_code}")
            log(f"   Response: {response.text[:200]}")
            return False
    except requests.exceptions.Timeout:
        log(f"❌ Search endpoint TIMEOUT (>30s)")
        return False
    except Exception as e:
        log(f"❌ Search endpoint FAILED: {e}")
        return False

def check_stats_endpoint():
    """Test stats endpoint"""
    try:
        start = time.time()
        response = requests.get(f"{VIEWER_URL}/api/stats", timeout=10)
        elapsed = time.time() - start

        if response.status_code == 200:
            data = response.json()
            total_posts = data.get('total_posts', 0)
            total_gms = data.get('total_gms', 0)
            log(f"✅ Stats endpoint: OK ({elapsed:.2f}s, {total_posts:,} posts, {total_gms} GMs)")
            return True
        else:
            log(f"❌ Stats endpoint returned {response.status_code}")
            return False
    except Exception as e:
        log(f"❌ Stats endpoint FAILED: {e}")
        return False

def check_database_size():
    """Check database size and table statistics"""
    try:
        if DATABASE_URL.startswith("postgresql"):
            import psycopg
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    # Get database size
                    cur.execute("""
                        SELECT pg_size_pretty(pg_database_size(current_database())) as size
                    """)
                    db_size = cur.fetchone()[0]

                    # Get table counts
                    cur.execute("SELECT COUNT(*) FROM posts")
                    post_count = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(*) FROM gm_posts_view")
                    gm_post_count = cur.fetchone()[0]

                    log(f"📊 Database size: {db_size}")
                    log(f"📊 Total posts: {post_count:,}")
                    log(f"📊 GM posts: {gm_post_count:,}")

                    return True
    except Exception as e:
        log(f"❌ Database size check FAILED: {e}")
        return False

def main():
    log("=" * 60)
    log("Starting BlueTracker health check...")
    log("=" * 60)

    results = {
        'database': check_database_connection(),
        'viewer': check_viewer_health(),
        'stats': check_stats_endpoint(),
        'search': check_search_performance(),
        'db_size': check_database_size()
    }

    log("=" * 60)
    passed = sum(results.values())
    total = len(results)
    log(f"Health check complete: {passed}/{total} checks passed")

    if passed < total:
        log("⚠️  ALERT: Some health checks failed!")
        sys.exit(1)
    else:
        log("✅ All systems operational")
        sys.exit(0)

if __name__ == "__main__":
    main()
