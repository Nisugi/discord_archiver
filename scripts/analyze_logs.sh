#!/bin/bash
# Log Analysis Script for Discord Archiver
# Analyzes performance, errors, and health of the archiver services

# Color codes for output
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default time range
TIME_RANGE="${1:-24 hours ago}"

echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       Discord Archiver Log Analysis                           ║${NC}"
echo -e "${BLUE}║       Time Range: ${TIME_RANGE}${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if services are running
echo -e "${BLUE}━━━ Service Status ━━━${NC}"
BOT_STATUS=$(sudo systemctl is-active discord-bot.service 2>/dev/null || echo "not-found")
VIEWER_STATUS=$(sudo systemctl is-active discord-viewer.service 2>/dev/null || echo "not-found")

if [ "$BOT_STATUS" == "active" ]; then
    echo -e "Bot Service:    ${GREEN}✓ Running${NC}"
else
    echo -e "Bot Service:    ${RED}✗ $BOT_STATUS${NC}"
fi

if [ "$VIEWER_STATUS" == "active" ]; then
    echo -e "Viewer Service: ${GREEN}✓ Running${NC}"
else
    echo -e "Viewer Service: ${RED}✗ $VIEWER_STATUS${NC}"
fi
echo ""

# Service restarts
echo -e "${BLUE}━━━ Service Restarts ━━━${NC}"
BOT_RESTARTS=$(sudo journalctl -u discord-bot.service --since "$TIME_RANGE" | grep -c "Started Discord" 2>/dev/null || echo "0")
VIEWER_RESTARTS=$(sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" | grep -c "Started Discord" 2>/dev/null || echo "0")

if [ "$BOT_RESTARTS" -eq 0 ]; then
    echo -e "Bot restarts:    ${GREEN}0${NC}"
elif [ "$BOT_RESTARTS" -le 2 ]; then
    echo -e "Bot restarts:    ${YELLOW}$BOT_RESTARTS${NC}"
else
    echo -e "Bot restarts:    ${RED}$BOT_RESTARTS${NC}"
fi

if [ "$VIEWER_RESTARTS" -eq 0 ]; then
    echo -e "Viewer restarts: ${GREEN}0${NC}"
elif [ "$VIEWER_RESTARTS" -le 2 ]; then
    echo -e "Viewer restarts: ${YELLOW}$VIEWER_RESTARTS${NC}"
else
    echo -e "Viewer restarts: ${RED}$VIEWER_RESTARTS${NC}"
fi
echo ""

# Performance issues
echo -e "${BLUE}━━━ Performance Issues ━━━${NC}"
SLOW_REQUESTS=$(sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" | grep -c "SLOW" 2>/dev/null || echo "0")
TOTAL_REQUESTS=$(sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" | grep -c "\[Performance\]" 2>/dev/null || echo "0")

if [ "$SLOW_REQUESTS" -eq 0 ]; then
    echo -e "Slow requests (>2s): ${GREEN}0${NC} / $TOTAL_REQUESTS total"
elif [ "$SLOW_REQUESTS" -le 5 ]; then
    echo -e "Slow requests (>2s): ${YELLOW}$SLOW_REQUESTS${NC} / $TOTAL_REQUESTS total"
else
    echo -e "Slow requests (>2s): ${RED}$SLOW_REQUESTS${NC} / $TOTAL_REQUESTS total"
fi

if [ "$SLOW_REQUESTS" -gt 0 ]; then
    echo ""
    echo "Recent slow requests:"
    sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" | grep "SLOW" | tail -5 | while read line; do
        echo -e "  ${YELLOW}→${NC} $(echo "$line" | grep -oP '\[Performance\].*')"
    done
fi
echo ""

# Materialized view refresh
echo -e "${BLUE}━━━ Materialized View Refresh ━━━${NC}"
REFRESH_COUNT=$(sudo journalctl -u discord-bot.service --since "$TIME_RANGE" | grep -c "90-day view refreshed" 2>/dev/null || echo "0")

if [ "$REFRESH_COUNT" -gt 0 ]; then
    echo -e "Refresh count: ${GREEN}$REFRESH_COUNT${NC}"
    echo "Recent refresh times:"
    sudo journalctl -u discord-bot.service --since "$TIME_RANGE" | grep "90-day view refreshed" | tail -5 | while read line; do
        TIME=$(echo "$line" | grep -oP '\d+\.\d+s')
        if [ ! -z "$TIME" ]; then
            echo "  → $TIME"
        fi
    done
else
    echo -e "Refresh count: ${YELLOW}$REFRESH_COUNT${NC} (should refresh every 10 minutes)"
fi
echo ""

# Errors
echo -e "${BLUE}━━━ Errors and Exceptions ━━━${NC}"
BOT_ERRORS=$(sudo journalctl -u discord-bot.service --since "$TIME_RANGE" -p err | wc -l 2>/dev/null || echo "0")
VIEWER_ERRORS=$(sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" -p err | wc -l 2>/dev/null || echo "0")
TRACEBACKS=$(sudo journalctl -u discord-bot.service -u discord-viewer.service --since "$TIME_RANGE" | grep -c "Traceback" 2>/dev/null || echo "0")

if [ "$BOT_ERRORS" -eq 0 ]; then
    echo -e "Bot error logs:    ${GREEN}0${NC}"
else
    echo -e "Bot error logs:    ${YELLOW}$BOT_ERRORS${NC}"
fi

if [ "$VIEWER_ERRORS" -eq 0 ]; then
    echo -e "Viewer error logs: ${GREEN}0${NC}"
else
    echo -e "Viewer error logs: ${YELLOW}$VIEWER_ERRORS${NC}"
fi

if [ "$TRACEBACKS" -eq 0 ]; then
    echo -e "Python exceptions: ${GREEN}0${NC}"
else
    echo -e "Python exceptions: ${RED}$TRACEBACKS${NC}"

    echo ""
    echo "Recent exceptions (last 3):"
    sudo journalctl -u discord-bot.service -u discord-viewer.service --since "$TIME_RANGE" | grep -A 3 "Traceback" | tail -15
fi
echo ""

# Database issues
echo -e "${BLUE}━━━ Database Issues ━━━${NC}"
DB_TIMEOUTS=$(sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" | grep -ic "timeout\|timed out" 2>/dev/null || echo "0")
DB_CONN_ERRORS=$(sudo journalctl -u discord-bot.service -u discord-viewer.service --since "$TIME_RANGE" | grep -ic "connection.*refused\|could not connect" 2>/dev/null || echo "0")

if [ "$DB_TIMEOUTS" -eq 0 ]; then
    echo -e "Query timeouts:       ${GREEN}0${NC}"
else
    echo -e "Query timeouts:       ${RED}$DB_TIMEOUTS${NC}"
fi

if [ "$DB_CONN_ERRORS" -eq 0 ]; then
    echo -e "Connection failures:  ${GREEN}0${NC}"
else
    echo -e "Connection failures:  ${RED}$DB_CONN_ERRORS${NC}"
fi
echo ""

# GM message processing
echo -e "${BLUE}━━━ GM Message Processing ━━━${NC}"
GM_MESSAGES=$(sudo journalctl -u discord-bot.service --since "$TIME_RANGE" | grep -c "GM message detected" 2>/dev/null || echo "0")
echo "GM messages archived: $GM_MESSAGES"

if [ "$GM_MESSAGES" -gt 0 ]; then
    echo "Recent GM posts:"
    sudo journalctl -u discord-bot.service --since "$TIME_RANGE" | grep "GM message detected" | tail -5 | while read line; do
        MSG=$(echo "$line" | grep -oP "from \K.*")
        echo "  → $MSG"
    done
fi
echo ""

# Endpoint usage
echo -e "${BLUE}━━━ API Endpoint Usage ━━━${NC}"
SEARCH_REQUESTS=$(sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" | grep -c "Performance.*search" 2>/dev/null || echo "0")
STATS_REQUESTS=$(sudo journalctl -u discord-viewer.service --since "$TIME_RANGE" | grep -c "Performance.*stats" 2>/dev/null || echo "0")

echo "/api/search requests: $SEARCH_REQUESTS"
echo "/api/stats requests:  $STATS_REQUESTS"
echo ""

# System resources (if available)
echo -e "${BLUE}━━━ System Resources ━━━${NC}"

# Check for OOM kills
OOM_KILLS=$(sudo journalctl --since "$TIME_RANGE" | grep -ic "out of memory\|oom.*kill" 2>/dev/null || echo "0")
if [ "$OOM_KILLS" -eq 0 ]; then
    echo -e "OOM kills: ${GREEN}0${NC}"
else
    echo -e "OOM kills: ${RED}$OOM_KILLS${NC}"
fi

# Disk usage
DISK_USAGE=$(df -h /opt/discord-archiver 2>/dev/null | awk 'NR==2 {print $5}' | sed 's/%//')
if [ ! -z "$DISK_USAGE" ]; then
    if [ "$DISK_USAGE" -lt 80 ]; then
        echo -e "Disk usage: ${GREEN}${DISK_USAGE}%${NC}"
    elif [ "$DISK_USAGE" -lt 90 ]; then
        echo -e "Disk usage: ${YELLOW}${DISK_USAGE}%${NC}"
    else
        echo -e "Disk usage: ${RED}${DISK_USAGE}%${NC}"
    fi
fi

# Log disk usage
LOG_SIZE=$(sudo journalctl --disk-usage 2>/dev/null | grep -oP '\d+\.\d+[GM]' | head -1)
if [ ! -z "$LOG_SIZE" ]; then
    echo "Log disk usage: $LOG_SIZE"
fi
echo ""

# Summary
echo -e "${BLUE}━━━ Summary & Recommendations ━━━${NC}"

ISSUES=0

if [ "$BOT_STATUS" != "active" ] || [ "$VIEWER_STATUS" != "active" ]; then
    echo -e "${RED}⚠ One or more services are not running${NC}"
    ISSUES=$((ISSUES + 1))
fi

if [ "$BOT_RESTARTS" -gt 2 ] || [ "$VIEWER_RESTARTS" -gt 2 ]; then
    echo -e "${RED}⚠ Excessive service restarts detected${NC}"
    echo "  → Check for crashes: sudo journalctl -u discord-viewer.service | grep -B 5 'Started Discord'"
    ISSUES=$((ISSUES + 1))
fi

if [ "$SLOW_REQUESTS" -gt 10 ]; then
    echo -e "${YELLOW}⚠ High number of slow requests${NC}"
    echo "  → Check if materialized view is refreshing properly"
    echo "  → Consider database optimization"
    ISSUES=$((ISSUES + 1))
fi

if [ "$TRACEBACKS" -gt 0 ]; then
    echo -e "${RED}⚠ Python exceptions occurred${NC}"
    echo "  → Review tracebacks above for root cause"
    ISSUES=$((ISSUES + 1))
fi

if [ "$DB_TIMEOUTS" -gt 0 ]; then
    echo -e "${RED}⚠ Database query timeouts detected${NC}"
    echo "  → Slow queries need optimization"
    echo "  → Check database load"
    ISSUES=$((ISSUES + 1))
fi

if [ "$REFRESH_COUNT" -eq 0 ]; then
    echo -e "${YELLOW}⚠ Materialized view not refreshing${NC}"
    echo "  → Check if bot background task is running"
    ISSUES=$((ISSUES + 1))
fi

if [ "$OOM_KILLS" -gt 0 ]; then
    echo -e "${RED}⚠ Out of memory kills detected${NC}"
    echo "  → Increase server memory or optimize queries"
    ISSUES=$((ISSUES + 1))
fi

if [ ! -z "$DISK_USAGE" ] && [ "$DISK_USAGE" -gt 85 ]; then
    echo -e "${YELLOW}⚠ Disk space running low${NC}"
    echo "  → Clean up old logs or backups"
    ISSUES=$((ISSUES + 1))
fi

if [ "$ISSUES" -eq 0 ]; then
    echo -e "${GREEN}✓ No major issues detected!${NC}"
    echo ""
    echo "System appears healthy. Regular monitoring recommended."
else
    echo ""
    echo -e "Found ${RED}$ISSUES issue(s)${NC} requiring attention."
fi

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "For more details:"
echo "  View live logs:      sudo journalctl -u discord-viewer.service -f"
echo "  Export logs:         sudo journalctl -u discord-viewer.service --since '$TIME_RANGE' > viewer_logs.txt"
echo "  Run health monitor:  /opt/discord-archiver/scripts/monitor_health.py"
echo "  See guide:           cat /opt/discord-archiver/LOG_ANALYSIS_GUIDE.md"
echo ""
