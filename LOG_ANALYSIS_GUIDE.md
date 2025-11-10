# Log Analysis Guide for Discord Archiver

This guide shows you how to use systemd logs to find and diagnose problems with the Discord Archiver services.

---

## Quick Reference Commands

### View Live Logs
```bash
# Watch bot logs in real-time
sudo journalctl -u discord-bot.service -f

# Watch viewer logs in real-time
sudo journalctl -u discord-viewer.service -f

# Watch both services at once
sudo journalctl -u discord-bot.service -u discord-viewer.service -f
```

### Check Service Status
```bash
# Check if services are running
sudo systemctl status discord-bot.service
sudo systemctl status discord-viewer.service

# See if services have crashed recently
sudo systemctl is-failed discord-bot.service
sudo systemctl is-failed discord-viewer.service
```

---

## Finding Performance Issues

### Slow API Requests

The performance middleware logs all requests and marks slow ones (>2 seconds) with "⚠️ SLOW".

**Find all slow requests from the last 24 hours:**
```bash
sudo journalctl -u discord-viewer.service --since "24 hours ago" | grep "SLOW"
```

**Count slow requests in the last week:**
```bash
sudo journalctl -u discord-viewer.service --since "1 week ago" | grep -c "SLOW"
```

**See the slowest requests with timing:**
```bash
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep "Performance" | grep -E "[3-9]\.[0-9]{2}s|[1-9][0-9]\.[0-9]{2}s"
```

**Example output:**
```
Nov 09 14:23:45 server [Performance] [GET] /api/search - 200 - 0.45s
Nov 09 14:24:12 server ⚠️ SLOW [Performance] [GET] /api/search - 200 - 3.21s
```

### Search-Specific Issues

**Find all /api/search requests:**
```bash
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep "Performance.*search"
```

**Find timeouts (queries that took >25s and failed):**
```bash
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep -E "timed out|timeout|statement timeout"
```

**Find "Failed to fetch" errors:**
```bash
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep -i "failed to fetch"
```

### Database Query Performance

**See how long the materialized view refresh takes:**
```bash
sudo journalctl -u discord-bot.service --since "1 day ago" | grep "90-day view refreshed"
```

**Example output:**
```
Nov 09 14:30:12 server [DB] 90-day view refreshed in 2.34s
Nov 09 14:40:12 server [DB] 90-day view refreshed in 2.41s
Nov 09 14:50:12 server [DB] 90-day view refreshed in 2.38s
```

If refresh times are increasing, it might indicate:
- Database growing larger (normal)
- Database needs vacuuming
- Disk I/O issues

**See database connection issues:**
```bash
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep -i "database\|connection\|psycopg"
```

---

## Finding Crashes and Errors

### Service Crashes

**See when services stopped/restarted:**
```bash
# View all start/stop events
sudo journalctl -u discord-viewer.service | grep -E "Started|Stopped|Stopping"

# Count restarts in the last week
sudo journalctl -u discord-viewer.service --since "1 week ago" | grep -c "Started Discord"
```

**See exit codes (non-zero means crash):**
```bash
sudo journalctl -u discord-viewer.service | grep "code=exited"
```

Common exit codes:
- `code=exited, status=0` - Normal shutdown
- `code=exited, status=1` - Python exception/error
- `code=exited, status=203/EXEC` - Executable not found
- `code=killed, signal=KILL` - Out of memory

### Python Exceptions

**Find all Python tracebacks:**
```bash
sudo journalctl -u discord-bot.service --since "1 day ago" | grep -A 10 "Traceback"
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep -A 10 "Traceback"
```

**Find specific error types:**
```bash
# Connection errors
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep -i "connectionerror\|timeout\|refused"

# Database errors
sudo journalctl -u discord-bot.service --since "1 day ago" | grep -i "operationalerror\|programming"

# Discord API errors
sudo journalctl -u discord-bot.service --since "1 day ago" | grep -i "forbidden\|429\|ratelimit"
```

### Memory Issues

**Check for out-of-memory kills:**
```bash
# OOM kills show up in system logs
sudo journalctl --since "1 week ago" | grep -i "out of memory\|oom"

# Or check dmesg
sudo dmesg | grep -i "killed process"
```

---

## Investigating Specific Incidents

### "The site was down at 3pm yesterday"

```bash
# See what happened around that time (adjust timezone as needed)
sudo journalctl -u discord-viewer.service --since "2025-11-09 14:30" --until "2025-11-09 15:30"

# Look for crashes
sudo journalctl -u discord-viewer.service --since "2025-11-09 14:30" --until "2025-11-09 15:30" | grep -E "Stopped|Failed|Traceback"

# Check for database issues
sudo journalctl -u discord-viewer.service --since "2025-11-09 14:30" --until "2025-11-09 15:30" | grep -i "database\|connection"
```

### "Search was really slow this morning"

```bash
# See all slow searches from this morning
sudo journalctl -u discord-viewer.service --since "today 06:00" --until "today 12:00" | grep "SLOW.*search"

# See average query times
sudo journalctl -u discord-viewer.service --since "today 06:00" --until "today 12:00" | grep "PERF.*query took"
```

### "Users reported 'Failed to fetch' errors"

```bash
# These usually mean the service crashed or became unresponsive
# Check for crashes around that time
sudo journalctl -u discord-viewer.service --since "2 hours ago" | grep -E "Stopped|Failed|killed"

# Check for long-running requests that might have blocked the service
sudo journalctl -u discord-viewer.service --since "2 hours ago" | grep "SLOW"

# Check for database timeouts
sudo journalctl -u discord-viewer.service --since "2 hours ago" | grep -i "timeout\|timed out"
```

---

## Log Retention and Disk Space

### Check Log Disk Usage
```bash
# See how much disk space logs are using
sudo journalctl --disk-usage

# See log settings
sudo cat /etc/systemd/journald.conf | grep -E "SystemMaxUse|MaxRetentionSec"
```

### Limit Log Size (if needed)
```bash
# Edit journald config
sudo nano /etc/systemd/journald.conf

# Add/modify these lines:
SystemMaxUse=2G
MaxRetentionSec=4week

# Restart journald
sudo systemctl restart systemd-journald
```

### Clean Old Logs
```bash
# Keep only logs from the last 7 days
sudo journalctl --vacuum-time=7d

# Keep only 1GB of logs
sudo journalctl --vacuum-size=1G
```

---

## Exporting Logs for Analysis

### Save Logs to a File
```bash
# Save last 24 hours of viewer logs
sudo journalctl -u discord-viewer.service --since "24 hours ago" > viewer_logs.txt

# Save logs from a specific time period
sudo journalctl -u discord-bot.service --since "2025-11-09 00:00" --until "2025-11-09 23:59" > bot_logs_nov9.txt

# Save all performance data for the week
sudo journalctl -u discord-viewer.service --since "1 week ago" | grep "Performance" > performance_week.log
```

### Export JSON Format (for parsing)
```bash
# Export as JSON for scripting
sudo journalctl -u discord-viewer.service --since "1 day ago" -o json > logs.json
```

---

## Common Problem Patterns

### Pattern: Service keeps restarting
```bash
# Check what's causing the crashes
sudo journalctl -u discord-viewer.service | grep -B 5 "Started Discord"
```

**Common causes:**
- Python exceptions on startup
- Missing dependencies
- Database connection failures
- Port already in use

### Pattern: Intermittent slowness
```bash
# Check if slowness correlates with materialized view refresh
sudo journalctl -u discord-bot.service -u discord-viewer.service --since "1 day ago" | grep -E "90-day view|SLOW" | sort
```

**Common causes:**
- Materialized view refresh happening during query (shouldn't happen with CONCURRENTLY)
- Database needs VACUUM
- High memory usage causing swap

### Pattern: "Failed to fetch" errors
```bash
# Check if service was down
sudo journalctl -u discord-viewer.service --since "1 hour ago" | grep -E "Stopped|Started"

# Check for long-running queries blocking the service
sudo journalctl -u discord-viewer.service --since "1 hour ago" | grep "SLOW"
```

**Common causes:**
- Service crashed
- Queries taking >25s hitting statement timeout
- Database connection pool exhausted

---

## Monitoring Commands Cheat Sheet

```bash
# Quick health check
sudo systemctl is-active discord-bot.service discord-viewer.service

# See last 20 lines from each service
sudo journalctl -u discord-bot.service -n 20 --no-pager
sudo journalctl -u discord-viewer.service -n 20 --no-pager

# Count errors in last hour
sudo journalctl -u discord-viewer.service --since "1 hour ago" -p err | wc -l

# See performance summary
sudo journalctl -u discord-viewer.service --since "1 day ago" | grep "Performance" | tail -20

# Check if materialized view is refreshing
sudo journalctl -u discord-bot.service --since "30 minutes ago" | grep "90-day"

# See recent GM posts detected
sudo journalctl -u discord-bot.service --since "1 hour ago" | grep "GM message detected"
```

---

## Using the Log Analysis Script

A dedicated script is available at `scripts/analyze_logs.sh`:

```bash
# Run the analysis script
/opt/discord-archiver/scripts/analyze_logs.sh

# Or with a specific time range
/opt/discord-archiver/scripts/analyze_logs.sh "24 hours ago"
/opt/discord-archiver/scripts/analyze_logs.sh "1 week ago"
```

---

## Tips and Best Practices

1. **Use timestamps**: Always use `--since` and `--until` to narrow down searches
2. **Pipe to less**: For long output, pipe to `less` for scrolling: `journalctl ... | less`
3. **Use grep carefully**: Combine multiple greps with `grep -E "pattern1|pattern2"`
4. **Save important logs**: Export logs before they get rotated out
5. **Check both services**: Some issues affect both bot and viewer
6. **Look for patterns**: Is slowness at a specific time of day? After how long running?

---

## Getting Help

If you're seeing persistent issues:

1. **Run the health monitor:**
   ```bash
   /opt/discord-archiver/scripts/monitor_health.py
   ```

2. **Export recent logs:**
   ```bash
   sudo journalctl -u discord-viewer.service --since "1 day ago" > viewer_issue.log
   sudo journalctl -u discord-bot.service --since "1 day ago" > bot_issue.log
   ```

3. **Check resource usage:**
   ```bash
   htop  # or top
   df -h  # disk space
   free -h  # memory
   ```

4. **Review the performance logs for patterns**

5. **Check database health:**
   ```bash
   psql $DATABASE_URL -c "SELECT COUNT(*) FROM posts;"
   psql $DATABASE_URL -c "SELECT COUNT(*) FROM gm_posts_90day;"
   ```
