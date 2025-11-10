import os
import re
import time
import argparse
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, g, Response, send_from_directory, abort
from werkzeug.serving import run_simple
from .config import PRIVATE_CHANNELS, SOURCE_GUILD_ID, DATABASE_URL


def _convert_placeholders(sql: str) -> str:
    """Convert sqlite-style ? placeholders to %s for psycopg."""
    if "?" not in sql:
        return sql
    result = []
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            result.append(ch)
            if in_single and i + 1 < len(sql) and sql[i + 1] == "'":
                result.append("'")
                i += 1
            else:
                in_single = not in_single
        elif ch == '"' and not in_single:
            result.append(ch)
            in_double = not in_double
        elif ch == "?" and not in_single and not in_double:
            result.append("%s")
        else:
            result.append(ch)
        i += 1
    return "".join(result)


class RowWrapper:
    def __init__(self, row):
        if row is None:
            self._data = {}
        elif isinstance(row, dict):
            self._data = dict(row)
        elif hasattr(row, "_mapping"):
            self._data = dict(row._mapping)
        else:
            self._data = dict(row)
        self._ordered = list(self._data.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._ordered[key]
        return self._data[key]

    def __getattr__(self, item):
        return self._data[item]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def items(self):
        return self._data.items()

    def values(self):
        return self._ordered

    def keys(self):
        return self._data.keys()


class PostgresCursorAdapter:
    def __init__(self, cursor: psycopg.Cursor):
        self._cursor = cursor

    def fetchone(self):
        row = self._cursor.fetchone()
        return RowWrapper(row) if row is not None else None

    def fetchall(self):
        return [RowWrapper(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()


class PostgresConnectionAdapter:
    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    def execute(self, sql: str, params=()):
        cursor = self._conn.cursor()
        prepared_params = tuple(params) if params else ()
        cursor.execute(_convert_placeholders(sql), prepared_params)
        return PostgresCursorAdapter(cursor)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()
import json
import queue
import threading

message_queue = queue.Queue()
app = Flask(__name__)
app.config['DATABASE_URL'] = DATABASE_URL

# Initialize performance monitoring
from .middleware import PerformanceMonitor
PerformanceMonitor(app, slow_threshold=2.0)

TRUTHY_SQL = "COALESCE({}::text, '0') IN ('1','t','true')"


def _truthy(column: str) -> str:
    return TRUTHY_SQL.format(column)


def _falsy(column: str) -> str:
    return f"NOT ({_truthy(column)})"

def _create_connection():
    conn = psycopg.connect(app.config['DATABASE_URL'], row_factory=dict_row)
    conn.autocommit = True
    # Set statement timeout to 25 seconds to prevent runaway queries
    # This is less than the frontend timeout (30s) to ensure proper error handling
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '25s'")
    return PostgresConnectionAdapter(conn)

endpoint_cache = {
    'gms': None,
    'channels': None, 
    'stats': None,
    'cache_time': {},
    'lock': threading.Lock()
}

CACHE_DURATION = 300

def get_cached_data(key):
    """Get cached data if still valid"""
    with endpoint_cache['lock']:
        if (key in endpoint_cache['cache_time'] and 
            time.time() - endpoint_cache['cache_time'][key] < CACHE_DURATION and
            endpoint_cache[key] is not None):
            return endpoint_cache[key]
    return None

def set_cached_data(key, data):
    """Cache data with timestamp"""
    with endpoint_cache['lock']:
        endpoint_cache[key] = data
        endpoint_cache['cache_time'][key] = time.time()

def get_db():
    """Get database connection for current request with retry logic"""
    db = getattr(g, '_database', None)
    if db is None:
        max_retries = 5
        for attempt in range(max_retries):
            try:
                db = g._database = _create_connection()
                break
            except psycopg.OperationalError as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
                else:
                    raise
    return db

@app.teardown_appcontext
def close_connection(exception):
    """Close database connection at end of request"""
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def parse_search_query(query):
    """Parse advanced search syntax"""
    # Extract regex patterns: /pattern/flags
    regex_patterns = []
    remaining_query = query
    
    regex_matches = re.finditer(r'/([^/]+)/([gimsx]*)', query)
    for match in regex_matches:
        pattern = match.group(1)
        flags = match.group(2)
        regex_flags = 0
        if 'i' in flags:
            regex_flags |= re.IGNORECASE
        if 'm' in flags:
            regex_flags |= re.MULTILINE
        if 's' in flags:
            regex_flags |= re.DOTALL
        regex_patterns.append((pattern, regex_flags))
        remaining_query = remaining_query.replace(match.group(0), '')
    
    # Extract quoted phrases: "exact phrase"
    quoted_phrases = []
    quote_matches = re.finditer(r'"([^"]+)"', remaining_query)
    for match in quote_matches:
        quoted_phrases.append(match.group(1))
        remaining_query = remaining_query.replace(match.group(0), '')
    
    # Extract AND terms: word + word
    and_groups = []
    and_matches = re.finditer(r'(\w+)\s*\+\s*(\w+)', remaining_query)
    for match in and_matches:
        and_groups.append([match.group(1), match.group(2)])
        remaining_query = remaining_query.replace(match.group(0), '')
    
    # Remaining words are OR terms
    or_terms = remaining_query.strip().split()
    
    return {
        'regex': regex_patterns,
        'phrases': quoted_phrases,
        'and_groups': and_groups,
        'or_terms': [t for t in or_terms if t]
    }

def matches_search(content, search_params):
    """Check if content matches search parameters"""
    if not content:
        return False
    
    content_lower = content.lower()
    
    # Check regex patterns
    for pattern, flags in search_params['regex']:
        try:
            if re.search(pattern, content, flags):
                return True
        except re.error:
            pass  # Invalid regex, skip
    
    # Check quoted phrases
    for phrase in search_params['phrases']:
        if phrase.lower() in content_lower:
            return True
    
    # Check AND groups (all terms must be present)
    for and_group in search_params['and_groups']:
        if all(term.lower() in content_lower for term in and_group):
            return True
    
    # Check OR terms (any term can match)
    for term in search_params['or_terms']:
        if term.lower() in content_lower:
            return True
    
    # If no search criteria, don't match
    if not (search_params['regex'] or
            search_params['phrases'] or
            search_params['and_groups'] or
            search_params['or_terms']):
        return True   # no criteria at all → match everything
    return False      # criteria exist but none matched

@app.route('/')
def index():
    """Main search interface"""
    from flask import Response
    # Import config values
    try:
        from archiver.config import SOURCE_GUILD_ID
    except ImportError:
        SOURCE_GUILD_ID = '226045346399256576'
    
    # Replace placeholder in template
    template = search_template.replace('SOURCE_GUILD_ID = \'226045346399256576\'', 
                                     f'SOURCE_GUILD_ID = \'{SOURCE_GUILD_ID}\'')
    return Response(template, mimetype='text/html')

@app.route('/api/gms')
def get_gms():
    """Get list of GM users for dropdown - OPTIMIZED"""
    try:
        # Check cache first
        cached = get_cached_data('gms')
        if cached is not None:
            return jsonify(cached)
            
        start_time = time.time()
        db = get_db()
        
        # Much faster query - no complex JOINs
        cursor = db.execute("""
            SELECT 
                m.member_id,
                COALESCE(g.gm_name, m.display_name, m.username, 'Unknown') as display_name
            FROM members m
            LEFT JOIN gm_names g ON m.member_id = g.author_id
            WHERE """ + _truthy("m.is_gm") + """
            ORDER BY display_name
            LIMIT 200
        """)
        
        gms = [{'id': row['member_id'], 'name': row['display_name']} for row in cursor]
        
        # Cache the result
        set_cached_data('gms', gms)
        
        elapsed = time.time() - start_time
        print(f"[Viewer] Loaded {len(gms)} GMs in {elapsed:.2f}s")
        
        return jsonify(gms)
    except Exception as e:
        print(f"[Viewer] Error in get_gms: {e}")
        return jsonify([]), 500

@app.route("/api/members")
def api_members():
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT member_id AS id,
               display_name AS name
        FROM members
        WHERE display_name IS NOT NULL
          AND TRIM(display_name) <> ''
        ORDER BY name
    """).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"]} for r in rows])

@app.route('/api/channels')
def get_channels():
    """Get channels with aggressive optimization - OPTIMIZED"""
    try:
        from archiver.config import PRIVATE_CHANNELS
    except ImportError:
        PRIVATE_CHANNELS = set()
    
    try:
        # Check cache first
        cached = get_cached_data('channels')
        if cached is not None:
            return jsonify(cached)
            
        start_time = time.time()
        db = get_db()

        # MUCH faster approach - use the gm_posts_view which is already optimized
        ignored_clause = ""
        ignored_params = []
        if PRIVATE_CHANNELS:
            placeholders = ",".join("?" * len(PRIVATE_CHANNELS))
            ignored_clause = f"AND c.chan_id NOT IN ({placeholders})"
            ignored_params = [str(ch_id) for ch_id in PRIVATE_CHANNELS]
        
        # Optimized query - get channels that have GM posts directly from the view
        rows = db.execute(f"""
            SELECT DISTINCT 
                c.chan_id, 
                c.name, 
                COALESCE(p.name, '') AS parent_name
            FROM channels c
            LEFT JOIN channels p ON c.parent_id = p.chan_id
            WHERE EXISTS (
                SELECT 1 FROM gm_posts_view gv 
                WHERE gv.chan_id = c.chan_id 
                LIMIT 1
            )
            AND c.accessible IS TRUE
            {ignored_clause}
            ORDER BY parent_name, c.name
            LIMIT 500
        """, ignored_params)
        
        grouped = {}
        for r in rows:
            parent = r['parent_name'] or r['name']
            if parent not in grouped:
                grouped[parent] = []
            grouped[parent].append({'id': r['chan_id'], 'name': r['name']})

        # Cache the result
        set_cached_data('channels', grouped)
        
        elapsed = time.time() - start_time
        print(f"[Viewer] Loaded {len(grouped)} channel groups in {elapsed:.2f}s")

        return jsonify(grouped)
    except Exception as e:
        print(f"[Viewer] Error in get_channels: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({}), 500

@app.route('/api/all_channels')
def get_all_channels():
    """Get channels with aggressive optimization - OPTIMIZED"""

    try:
        # Check cache first
        cached = get_cached_data('all_channels')
        if cached is not None:
            return jsonify(cached)
            
        start_time = time.time()
        db = get_db()

        # MUCH faster approach - use the gm_posts_view which is already optimized
        ignored_clause = ""
        ignored_params = []
        
        # Optimized query - get channels that have GM posts directly from the view
        rows = db.execute(f"""
            SELECT DISTINCT 
                c.chan_id, 
                c.name, 
                COALESCE(p.name, '') AS parent_name
            FROM channels c
            LEFT JOIN channels p ON c.parent_id = p.chan_id
            WHERE EXISTS (
                SELECT 1 FROM gm_posts_view gv 
                WHERE gv.chan_id = c.chan_id 
                LIMIT 1
            )
            AND c.accessible IS TRUE
            {ignored_clause}
            ORDER BY parent_name, c.name
            LIMIT 500
        """, ignored_params)
        
        grouped = {}
        for r in rows:
            parent = r['parent_name'] or r['name']
            if parent not in grouped:
                grouped[parent] = []
            grouped[parent].append({'id': r['chan_id'], 'name': r['name']})

        # Cache the result
        set_cached_data('all_channels', grouped)
        
        elapsed = time.time() - start_time
        print(f"[Viewer] Loaded {len(grouped)} channel groups in {elapsed:.2f}s")

        return jsonify(grouped)
    except Exception as e:
        print(f"[Viewer] Error in get_all_channels: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({}), 500

@app.route('/api/parent_channels')
def get_parent_channels():
    try:
        db = get_db()
        rows = db.execute("""
            SELECT chan_id, name
            FROM channels
            WHERE parent_id IS NULL
              AND accessible = 1
              AND last_message_id IS NOT NULL
            ORDER BY name
        """)
        return jsonify([{'id': row['chan_id'], 'name': row['name']} for row in rows])
    except Exception as e:
        print(f"[Viewer] Error in get_parent_channels: {e}")
        return jsonify([]), 500

@app.route('/api/channel_children')
def get_channel_children():
    parent_id = request.args.get('parent_id')
    if not parent_id:
        return jsonify([])

    try:
        db = get_db()
        rows = db.execute("""
            SELECT chan_id, name
            FROM channels
            WHERE parent_id = ?
              AND accessible = 1
              AND last_message_id IS NOT NULL
            ORDER BY name
        """, (parent_id,))
        return jsonify([{'id': row['chan_id'], 'name': row['name']} for row in rows])
    except Exception as e:
        print(f"[Viewer] Error in get_channel_children: {e}")
        return jsonify([]), 500

@app.route('/stream')
def stream():
    def generate():
        import time
        last_keepalive = time.time()
        
        while True:
            try:
                # Try to get a message with a 25-second timeout
                message = message_queue.get(timeout=25)
                yield f"data: {json.dumps(message)}\n\n"
                last_keepalive = time.time()
                
            except queue.Empty:
                # Send keepalive every 25 seconds to prevent proxy timeouts
                current_time = time.time()
                if current_time - last_keepalive >= 25:
                    keepalive_msg = {
                        'type': 'keepalive',
                        'timestamp': int(current_time * 1000)
                    }
                    yield f"data: {json.dumps(keepalive_msg)}\n\n"
                    last_keepalive = current_time
    
    response = Response(generate(), mimetype="text/event-stream")
    
    # Critical headers for Fly.io proxy compatibility
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    response.headers['Connection'] = 'keep-alive'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Cache-Control'
    
    # Tell Fly.io proxy not to buffer this response
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['X-Proxy-Buffering'] = 'no'
    
    return response

@app.route('/api/notify_gm_post', methods=['POST'])
def notify_gm_post():
    """Called by Discord archiver when GM posts"""
    post_data = request.json
    # Add to message queue for all SSE connections
    message_queue.put(post_data)
    return jsonify({'status': 'ok'})

@app.route('/api/search')
def search():
    """Search posts with automatic limits for broad searches - OPTIMIZED"""
    try:
        query       = request.args.get('q', '').strip()
        all_time    = request.args.get('all_time', '0') in ('1', 'true', 'True')
        gm_param    = request.args.get('gm_ids') or request.args.get('gm_id') or request.args.get('gm', '')
        gm_ids      = [value.strip() for value in gm_param.split(',') if value and value.strip()]
        channel_ids = [c for c in request.args.get('channels', '').split(',') if c]
        date_from   = request.args.get('date_from', '')
        date_to     = request.args.get('date_to', '')
        page        = int(request.args.get('page', 1))
        per_page    = min(int(request.args.get('per_page', 50)), 100)
        sort_param  = request.args.get('sort', 'desc').lower()
        sort_dir    = 'ASC' if sort_param == 'asc' else 'DESC'

        print(f"[DEBUG] Search params: gm_ids={gm_ids}, query='{query}', channels={channel_ids}, sort={sort_dir}")

        db = get_db()
        start_time = time.time()

        # Check if this is a broad "all GMs" search
        is_broad_search = not gm_ids and not channel_ids and not date_from and not date_to and not query
        
        # For broad searches, automatically limit to recent posts
        if is_broad_search and not all_time:
            three_months_ago_ts = time.time() - (90*24*60*60)
            auto_date_limit = int(three_months_ago_ts * 1000)
        else:
            auto_date_limit = None

        if is_broad_search and not all_time:
            print(f"[info] Showing posts from last 3 months only…")


        # Build WHERE conditions
        where_conditions = []
        params = []

        # Private channels filter
        if PRIVATE_CHANNELS:
            placeholders = ",".join("?" * len(PRIVATE_CHANNELS))
            where_conditions.append(f"p.chan_id NOT IN ({placeholders})")
            params.extend([str(ch_id) for ch_id in PRIVATE_CHANNELS])

        # Specific GM filter
        if gm_ids:
            placeholders = ",".join("?" * len(gm_ids))
            where_conditions.append(f"p.author_id IN ({placeholders})")
            params.extend(gm_ids)

        # Channel filter
        if channel_ids:
            placeholders = ",".join("?" * len(channel_ids))
            where_conditions.append(f"p.chan_id IN ({placeholders})")
            params.extend(channel_ids)

        # Date filters
        if date_from:
            try:
                from datetime import datetime as dt
                parsed_dt = dt.fromisoformat(date_from)
                where_conditions.append("p.created_ts >= ?")
                params.append(int(parsed_dt.timestamp() * 1000))
            except:
                pass
        elif auto_date_limit:
            where_conditions.append("p.created_ts >= ?")
            params.append(auto_date_limit)

        if date_to:
            try:
                from datetime import datetime as dt
                parsed_dt = dt.fromisoformat(date_to)
                where_conditions.append("p.created_ts <= ?")
                params.append(int(parsed_dt.timestamp() * 1000))
            except:
                pass

        # Build WHERE clause
        where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"

        # OPTIMIZED: Postgres full-text search path
        offset = (page - 1) * per_page
        order_clause = f"ORDER BY p.created_ts {sort_dir}"

        if query:
            final_query = f"""
                WITH search_query AS (
                    SELECT websearch_to_tsquery('english', ?) AS query
                )
                SELECT
                    p.post_id as id,
                    p.chan_id,
                    p.author_id,
                    p.created_ts as ts,
                    p.content,
                    p.reply_to_id,
                    c.name AS channel_name,
                    COALESCE(g.gm_name, m.display_name, m.username, 'Unknown') as author_name
                FROM gm_posts_view p
                CROSS JOIN search_query sq
                LEFT JOIN channels c ON p.chan_id = c.chan_id
                LEFT JOIN members m ON p.author_id = m.member_id
                LEFT JOIN gm_names g ON p.author_id = g.author_id
                WHERE {where_clause}
                  AND p.content_tsv @@ sq.query
                {order_clause}
                LIMIT ? OFFSET ?
            """
            final_params = [query] + params + [per_page, offset]
        else:
            # Non-FTS search - direct query
            final_query = f"""
                SELECT
                    p.post_id as id,
                    p.chan_id,
                    p.author_id,
                    p.created_ts as ts,
                    p.content,
                    p.reply_to_id,
                    c.name AS channel_name,
                    COALESCE(g.gm_name, m.display_name, m.username, 'Unknown') as author_name
                FROM gm_posts_view p
                LEFT JOIN channels c ON p.chan_id = c.chan_id
                LEFT JOIN members m ON p.author_id = m.member_id
                LEFT JOIN gm_names g ON p.author_id = g.author_id
                WHERE {where_clause}
                {order_clause}
                LIMIT ? OFFSET ?
            """
            final_params = params + [per_page, offset]

        # Execute main query
        cursor = db.execute(final_query, final_params)
        rows = cursor.fetchall()
        
        main_query_time = time.time() - start_time
        print(f"[PERF] Main query took {main_query_time:.2f}s, returned {len(rows)} rows")

        if not rows:
            return jsonify({
                'results': [],
                'total': 0,
                'page': page,
                'per_page': per_page,
                'total_pages': 0,
                'query_time': round(main_query_time, 2),
                'info': 'No results found' + (' (limited to last 3 months)' if is_broad_search else ''),
                'sort': sort_dir.lower(),
                'gm_ids': gm_ids
            })

        # OPTIMIZED: Batch fetch reply data with a single query
        reply_lookup_start = time.time()
        reply_data = {}
        reply_ids = [row['reply_to_id'] for row in rows if row['reply_to_id']]
        
        if reply_ids:
            # De-duplicate reply IDs
            unique_reply_ids = list(set(reply_ids))
            reply_placeholders = ",".join("?" * len(unique_reply_ids))
            
            # Get all reply data in one query
            replies_cursor = db.execute(f"""
                SELECT 
                    p.post_id,
                    p.chan_id,
                    SUBSTR(p.content, 1, 200) as content,
                    COALESCE(g.gm_name, m.display_name, m.username, 'Unknown') as author_name
                FROM posts p
                LEFT JOIN members m ON p.author_id = m.member_id
                LEFT JOIN gm_names g ON p.author_id = g.author_id
                WHERE p.post_id IN ({reply_placeholders})
            """, unique_reply_ids)
            
            reply_data = {row['post_id']: row for row in replies_cursor}
            
        reply_lookup_time = time.time() - reply_lookup_start
        print(f"[PERF] Reply lookup took {reply_lookup_time:.2f}s for {len(unique_reply_ids) if reply_ids else 0} unique replies")

        # Build results
        results = []
        for row in rows:
            result = {
                'id': row['id'],
                'channel_id': row['chan_id'],
                'channel': row['channel_name'] or f"Unknown ({row['chan_id']})",
                'author_id': row['author_id'],
                'author_name': row['author_name'],
                'timestamp': row['ts'],
                'datetime': datetime.fromtimestamp(row['ts']/1000, timezone.utc).isoformat(),
                'content': row['content'],
                'jump_url': f"https://discord.com/channels/{SOURCE_GUILD_ID}/{row['chan_id']}/{row['id']}",
                'replied_to': None
            }
            
            if row['reply_to_id'] and row['reply_to_id'] in reply_data:
                reply = reply_data[row['reply_to_id']]
                result['replied_to'] = {
                    'id': row['reply_to_id'],
                    'author_name': reply['author_name'],
                    'channel_id': reply['chan_id'],
                    'content': reply['content']
                }
            
            results.append(result)

        # OPTIMIZED count query with timeout protection
        count_start = time.time()

        # For broad searches (without all_time), use estimated count to avoid slow full table scans
        if is_broad_search and not all_time and not query:
            # Estimate: Use stats or just say "many results"
            # This avoids the expensive COUNT(*) on large date ranges
            total_count = 10000  # Estimated, will show "Page 1 of 200" etc
            total_pages = 200  # Reasonable estimate for pagination
            print(f"[PERF] Using estimated count for broad search (skipped COUNT query)")
        else:
            if query:
                count_sql = f"""
                    WITH search_query AS (
                        SELECT websearch_to_tsquery('english', ?) AS query
                    )
                    SELECT COUNT(*)
                    FROM gm_posts_view p
                    CROSS JOIN search_query sq
                    WHERE {where_clause}
                      AND p.content_tsv @@ sq.query
                """
                count_params = [query] + params

            else:
                # Regular count
                count_sql = f"""
                    SELECT COUNT(*)
                    FROM gm_posts_view p
                    WHERE {where_clause}
                """
                count_params = params

            total_count = db.execute(count_sql, count_params).fetchone()[0]
            total_pages = (total_count + per_page - 1) // per_page

            count_time = time.time() - count_start
            print(f"[PERF] Count query took {count_time:.2f}s")

        total_time = time.time() - start_time
        print(f"[PERF] Total request time: {total_time:.2f}s")

        # Build response
        info_msg = 'Limited to last 3 months' if is_broad_search and not all_time else ''

        response = {
            'results': results,
            'total': total_count,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'sort': sort_dir.lower(),
            'query_time': round(total_time, 2),
            'info': info_msg
        }
        
        return jsonify(response)
        
    except Exception as e:
        print(f"[Viewer] Error in search: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'results': [],
            'total': 0,
            'page': 1,
            'per_page': per_page,
            'total_pages': 0,
            'error': str(e)
        }), 500

@app.route('/health')
def health_check():
    """Simple health check endpoint"""
    return jsonify({'status': 'ok', 'service': 'BlueTracker Viewer'}), 200

@app.route('/api/stats')
def get_stats():
    """Get basic stats quickly - HIGHLY OPTIMIZED"""
    try:
        # Check cache first
        cached = get_cached_data('stats')
        if cached is not None:
            return jsonify(cached)
            
        start_time = time.time()
        db = get_db()
        
        stats = {}

        # CRITICAL: Use pre-calculated counts from bot_metadata table
        # First, check if we have cached counts
        cursor = db.execute("""
            SELECT key, value FROM bot_metadata 
            WHERE key IN ('stats_total_posts', 'stats_total_gms', 'stats_last_updated')
        """)
        
        cached_stats = {row['key']: row['value'] for row in cursor}
        
        # Check if cached stats are recent (within 5 minutes)
        last_updated = int(cached_stats.get('stats_last_updated', '0'))
        current_time = int(time.time() * 1000)

        if last_updated > 0 and (current_time - last_updated) < 300000:  # 5 minutes
            # Use cached values
            stats['total_posts'] = int(cached_stats.get('stats_total_posts', '0'))
            stats['total_gms'] = int(cached_stats.get('stats_total_gms', '0'))
        else:
            # Need to recalculate - but do it in a background thread
            stats['total_posts'] = int(cached_stats.get('stats_total_posts', '0'))  # Use old value for now
            stats['total_gms'] = int(cached_stats.get('stats_total_gms', '0'))
            
            # Trigger background update
            def update_stats_async():
                try:
                    temp_db = _create_connection()

                    # Count GM posts
                    cursor = temp_db.execute("SELECT COUNT(*) as count FROM gm_posts_view")
                    total_posts = cursor.fetchone()['count']

                    # Count GMs
                    cursor = temp_db.execute(f"SELECT COUNT(*) as count FROM members WHERE {_truthy('is_gm')}")
                    total_gms = cursor.fetchone()['count']

                    upsert_sql = """
                        INSERT INTO bot_metadata (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value,
                            updated_at = EXCLUDED.updated_at
                    """
                    temp_db.execute(upsert_sql, ('stats_total_posts', str(total_posts), current_time))
                    temp_db.execute(upsert_sql, ('stats_total_gms', str(total_gms), current_time))
                    temp_db.execute(upsert_sql, ('stats_last_updated', str(current_time), current_time))
                    temp_db.commit()
                    temp_db.close()

                    # Clear the endpoint cache so next request gets fresh data
                    with endpoint_cache['lock']:
                        endpoint_cache['stats'] = None

                    print(f"[Viewer] Stats updated in background: {total_posts:,} posts, {total_gms} GMs")
                except Exception as e:
                    print(f"[Viewer] Background stats update failed: {e}")
                finally:
                    # CRITICAL: Reset the flag so future updates can run
                    app._stats_updating = False
            
            # Only update if not already updating
            if not hasattr(app, '_stats_updating') or not app._stats_updating:
                app._stats_updating = True
                threading.Thread(target=update_stats_async, daemon=True).start()

        # For now, skip expensive queries
        stats['top_gms'] = []
        stats['recent_activity'] = []
        
        # Cache the result
        set_cached_data('stats', stats)
        
        elapsed = time.time() - start_time
        print(f"[Viewer] Loaded basic stats in {elapsed:.2f}s")

        return jsonify(stats)
    except Exception as e:
        print(f"[Viewer] Error in get_stats: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'total_posts': 0, 'total_gms': 0, 'top_gms': [], 'recent_activity': []}), 500

@app.route('/api/v1/posts')
def get_posts_v1():
    """Ruby-compatible posts endpoint"""
    limit = int(request.args.get('limit', 50))
    order = request.args.get('order', 'asc')  # 'asc' or 'desc'
    
    order_clause = "ORDER BY p.created_ts DESC" if order == 'desc' else "ORDER BY p.created_ts ASC"
    
    db = get_db()
    cursor = db.execute(f"""
        SELECT 
            p.post_id as id,
            p.chan_id,
            p.author_id,
            p.created_ts as ts,
            p.content,
            p.reply_to_id,
            COALESCE(c.name, p.chan_id) AS channel_name,
            COALESCE(g.gm_name, m.display_name, m.username, 'Unknown') as author_name
        FROM posts p
        JOIN members m ON p.author_id = m.member_id
        LEFT JOIN channels c ON p.chan_id = c.chan_id
        LEFT JOIN gm_names g ON p.author_id = g.author_id
        WHERE {_truthy('m.is_gm')}
        {order_clause}
        LIMIT ?
    """, (limit,))
    
    posts = []
    for row in cursor:
        posts.append({
            'id': row['id'],
            'chan_id': row['chan_id'],
            'author_id': row['author_id'],
            'ts': row['ts'],
            'content': row['content'],
            'reply_to_id': row['reply_to_id'],
            'channel_name': row['channel_name'],
            'author_name': row['author_name']
        })
    
    return jsonify(posts)

@app.route('/api/v1/posts/<post_id>')
def get_post_v1(post_id):
    """Ruby-compatible single post endpoint"""
    db = get_db()
    cursor = db.execute(f"""
        SELECT 
            p.post_id as id,
            p.chan_id,
            p.author_id,
            p.created_ts as ts,
            p.content,
            p.reply_to_id,
            COALESCE(c.name, p.chan_id) AS channel_name,
            COALESCE(g.gm_name, m.display_name, m.username, 'Unknown') as author_name
        FROM posts p
        JOIN members m ON p.author_id = m.member_id
        LEFT JOIN channels c ON p.chan_id = c.chan_id
        LEFT JOIN gm_names g ON p.author_id = g.author_id
        WHERE p.post_id = ? AND {_truthy('m.is_gm')}
    """, (post_id,))
    
    row = cursor.fetchone()
    if not row:
        return jsonify(None), 404
    
    post = {
        'id': row['id'],
        'chan_id': row['chan_id'],
        'author_id': row['author_id'],
        'ts': row['ts'],
        'content': row['content'],
        'reply_to_id': row['reply_to_id'],
        'channel_name': row['channel_name'],
        'author_name': row['author_name']
    }
    
    return jsonify(post)

@app.route('/api/posts/<post_id>')
def get_post_any(post_id):
    """Get ANY post by ID, not just GM posts"""
    db = get_db()
    cursor = db.execute("""
        SELECT 
            p.post_id as id,
            p.chan_id,
            p.author_id,
            p.created_ts as ts,
            p.content,
            p.reply_to_id,
            p.deleted,
            COALESCE(c.name, p.chan_id) AS channel_name,
            COALESCE(g.gm_name, m.display_name, m.username, 'Unknown') as author_name
        FROM posts p
        LEFT JOIN members m ON p.author_id = m.member_id
        LEFT JOIN channels c ON p.chan_id = c.chan_id
        LEFT JOIN gm_names g ON p.author_id = g.author_id
        WHERE p.post_id = ?
          AND {_falsy('p.deleted')}
    """, (post_id,))
    
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': 'Post not found or has been deleted'}), 404

    if int(row['chan_id']) in PRIVATE_CHANNELS:
        return jsonify({'error': 'Post is from a restricted channel'}), 403
    
    post = {
        'id': row['id'],
        'chan_id': row['chan_id'],
        'author_id': row['author_id'],
        'ts': row['ts'],
        'content': row['content'],
        'reply_to_id': row['reply_to_id'],
        'channel_name': row['channel_name'],
        'author_name': row['author_name']
    }
    
    return jsonify(post)

@app.route('/surprise')
def surprise_page():
    from flask import Response
    return Response(surprise_template, mimetype='text/html')

@app.route('/api/surprise_search')
def surprise_search():
    try:
        q           = request.args.get('q', '').strip()
        deleted_only= request.args.get('deleted', '0') in ('1', 'true', 'True')
        date_from   = request.args.get('date_from', '').strip()
        date_to     = request.args.get('date_to', '').strip()
        channels    = [c for c in request.args.get('channels', '').split(',') if c]
        members     = [m for m in request.args.get('members', '').split(',')  if m]
        all_time    = request.args.get('all_time', '0') in ('1', 'true', 'True')
        page        = int(request.args.get('page', 1))
        per_page    = min(int(request.args.get('per_page', 50)), 100)
        offset      = (page - 1) * per_page

        db    = get_db()
        start = time.time()

        where_clauses = []
        params        = []

        # deleted filter
        if deleted_only:
            where_clauses.append(_truthy("p.deleted"))
        else:
            where_clauses.append(_falsy("p.deleted"))

        # FTS clause
        if q:
            where_clauses.append("posts_fts MATCH ?")
            params.append(q)

        # Channel filter
        if channels:
            placeholders = ",".join("?" * len(channels))
            where_clauses.append(f"p.chan_id IN ({placeholders})")
            params.extend(channels)

        # Member filter
        if members:
            placeholders = ",".join("?" * len(members))
            where_clauses.append(f"p.author_id IN ({placeholders})")
            params.extend(members)

        # Date filters
        if date_from:
            dt = datetime.fromisoformat(date_from)
            where_clauses.append("p.created_ts >= ?")
            params.append(int(dt.timestamp() * 1000))

        if date_to:
            dt = datetime.fromisoformat(date_to)
            where_clauses.append("p.created_ts <= ?")
            params.append(int(dt.timestamp() * 1000))

        # Auto 90‑day cap for totally blank searches
        if not all_time and not (q or date_from or date_to or channels):
            cutoff = int((time.time() - 90*24*60*60) * 1000)
            where_clauses.append("p.created_ts >= ?")
            params.append(cutoff)

        where_clause = " AND ".join(f"({c})" for c in where_clauses) or "1=1"

        # Main query (FTS join only when needed)
        join_fts = "JOIN posts_fts ON posts_fts.rowid = p.post_id" if q else ""

        sql = f"""
            SELECT  p.post_id  AS id,
                    p.chan_id,
                    p.author_id,
                    p.created_ts AS ts,
                    p.content,
                    c.name       AS channel_name,
                    COALESCE(m.display_name, m.username, 'Unknown') AS author_name
            FROM posts p
            JOIN members  m ON p.author_id = m.member_id
            LEFT JOIN channels c ON p.chan_id = c.chan_id
            {join_fts}
            WHERE {where_clause}
            ORDER BY p.created_ts DESC
            LIMIT ? OFFSET ?
        """

        rows = db.execute(sql, (*params, per_page, offset)).fetchall()

        results = [{
            'id': r['id'],
            'channel': r['channel_name'],
            'author': r['author_name'],
            'content': r['content'],
            'timestamp': r['ts'],
        } for r in rows]

        # Count query for pagination
        count_sql = f"""
            SELECT COUNT(*)
            FROM posts p
            {join_fts}
            WHERE {where_clause}
        """
        total = db.execute(count_sql, params).fetchone()[0]
        total_pages = (total + per_page - 1) // per_page

        duration = round(time.time() - start, 2)
        return jsonify({
            'results': results,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'query_time': duration,
            'deleted_only': deleted_only,
            'all_time': all_time
        })

    except Exception as e:
        print(f"[Viewer] Error in /api/surprise_search: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'results': [], 'error': str(e)}), 500


# Cache warming endpoint (call this on startup)
@app.route('/api/warm_cache')
def warm_cache():
    """Warm up caches in background - SEQUENTIAL"""
    def warm_worker():
        try:
            print("[Viewer] Warming caches...")
            with app.test_client() as client:
                # Load GMs first (fast)
                client.get('/api/gms')
                time.sleep(0.1)
                
                # Then channels (medium speed)
                client.get('/api/channels') 
                time.sleep(0.1)
                
                # Skip stats during warming - let it load on first real request
                # client.get('/api/stats')
                
            print("[Viewer] Cache warming complete")
        except Exception as e:
            print(f"[Viewer] Cache warming failed: {e}")
    
    # Run in background thread
    threading.Thread(target=warm_worker, daemon=True).start()
    return jsonify({'status': 'warming'})

# HTML template
search_template = '''
<!DOCTYPE html>
<html>
<head>
    <title>BlueTracker Database Viewer</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=0">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/choices.js/public/assets/styles/choices.min.css">
    <script defer src="https://cdn.jsdelivr.net/npm/choices.js/public/assets/scripts/choices.min.js"></script>
    <style>
        * {
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 0;
            background: #f0f2f5;
            -webkit-text-size-adjust: 100%;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 10px;
        }
        
        .header {
            background: white;
            padding: 12px 15px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 15px;
        }

        .header-top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 10px;
        }
        
        h1 {
            font-size: 22px;
            margin: 0;
        }
        
        .stats-inline {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }

        .stat-chip {
            background: #f5f6fa;
            border-radius: 6px;
            padding: 6px 10px;
            text-align: right;
            min-width: 120px;
        }

        .stat-chip .label {
            font-size: 11px;
            color: #6b7280;
        }

        .stat-chip .value {
            font-size: 18px;
            font-weight: 600;
            color: #1a73e8;
        }

        .search-top {
            display: grid;
            grid-template-columns: minmax(320px, 1fr) repeat(2, minmax(140px, 180px)) repeat(2, max-content);
            gap: 8px;
            align-items: center;
            margin-bottom: 4px;
        }
        
        .search-bottom {
            display: grid;
            grid-template-columns: repeat(2, minmax(200px, 1fr)) repeat(3, max-content);
            gap: 8px;
            align-items: center;
            margin-bottom: 8px;
        }
        
        .search-top input,
        .search-bottom select,
        .search-bottom input[type="checkbox"],
        .search-bottom label {
            font-size: 15px;
        }
        
        .search-top input,
        .search-bottom select {
            padding: 8px 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            min-height: 44px;
        }
        
        .search-top input[type="text"] {
            width: 100%;
        }
        
        .search-top button {
            padding: 10px 18px;
            font-size: 15px;
            min-height: 40px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            color: white;
        }
        
        .btn-primary {
            background: #1a73e8;
        }
        
        .btn-primary:active {
            background: #1557b0;
        }
        
        .btn-secondary {
            background: #4b5563;
        }
        
        .status-text {
            font-size: 12px;
            color: #6b7280;
            margin-top: 4px;
        }
        .status-text:empty {
            display: none;
            margin-top: 0;
        }
        
        .inline-toggle {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 14px;
            border: 1px solid #ddd;
            border-radius: 6px;
            background: #fff;
            min-height: 44px;
            font-size: 14px;
            color: #4b5563;
            box-sizing: border-box;
        }
        
        .inline-toggle input {
            width: 16px;
            height: 16px;
        }
        
        .option-select {
            min-width: 120px;
        }
        
        .field-stack {
            display: flex;
            flex-direction: column;
        }
        
        .search-help {
            font-size: 12px;
            color: #666;
            margin-top: 5px;
            line-height: 1.4;
        }
        
        .filters {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
            margin-bottom: 6px;
        }
        
        .filters input[type="date"] {
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 16px;
            min-height: 44px;
        }
        
        .filters button {
            padding: 8px 16px;
            background: #fff;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            cursor: pointer;
            min-height: 40px;
        }
        
        .filters select {
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            min-height: 40px;
        }
        
        .channel-row {
            margin-top: 10px;
            width: 100%;
        }
        
        /* Choices.js overrides */
        .choices {
            background: #1f2330;
            border: 1px solid #343948;
            border-radius: 6px;
            color: #e5e7eb;
        }

        .choices__inner {
            padding: 8px 40px 8px 12px;
            min-height: 44px;
            font-size: 16px;
            border: none;
            background: transparent;
            color: inherit;
        }
        
        .choices__input {
            background: transparent;
            color: inherit;
        }
        
        .choices__list--multiple .choices__item {
            margin: 2px 4px 2px 0;
            padding: 4px 8px;
            font-size: 14px;
            background: #2a3040;
            border: none;
            color: #e5e7eb;
        }
        
        .choices__button {
            width: 20px;
            height: 20px;
        }

        .choices__list--dropdown,
        .choices__list {
            background: #1b1f2a;
            border: 1px solid #343948;
            color: #e5e7eb;
        }

        .choices__list--dropdown .choices__item,
        .choices__list--dropdown .choices__item--selectable,
        .choices__list--dropdown .choices__item--choice {
            background: transparent !important;
            color: inherit;
        }

        .choices__list--dropdown .choices__item--choice.is-selected,
        .choices__list--dropdown .choices__item--choice.is-highlighted,
        .choices__list--dropdown .choices__item--selectable.is-highlighted,
        .choices__item--selectable.is-highlighted {
            background: #2f3648 !important;
            color: #fff !important;
        }
        
        .choices__placeholder {
            opacity: 0.7;
            color: #9ca3af;
            background: transparent;
        }

        .search-bottom .choices {
            min-height: 44px;
            display: flex;
            flex-direction: column;
        }

        .search-bottom .choices__inner {
            width: 100%;
            min-height: 44px;
        }
        
        .search-bottom .inline-toggle,
        .search-bottom .option-select {
            align-self: stretch;
            height: 44px;
        }
        
        .choices__list--dropdown .choices__item--selectable::after {
            color: #4b5563;
        }
        
        .search-bottom select.option-select {
            padding-right: 32px;
        }
        
        @media (max-width: 1024px) {
            .search-top,
            .search-bottom {
                grid-template-columns: repeat(2, 1fr);
            }
        }
        
        @media (max-width: 640px) {
            .search-top,
            .search-bottom {
                grid-template-columns: 1fr;
            }
            
            .inline-toggle {
                width: 100%;
                justify-content: flex-start;
            }
        }

        .choices__item--selectable.is-highlighted {
            background: #dae5ff;
            color: #1f2937;
        }
        
        .result-summary {
            margin: 0 0 10px 0;
            font-size: 14px;
            color: #555;
        }
        .choices__list--dropdown,
        .choices__list {
            background: #fff;
            border: 1px solid #ddd;
            color: #111;
        }
        
        .result-summary {
            margin: 0 0 10px 0;
            font-size: 14px;
            color: #555;
        }

        .results {
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .post {
            padding: 15px;
            border-bottom: 1px solid #eee;
        }
        
        .post:last-child {
            border-bottom: none;
        }
        
        .post-header {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 10px;
            font-size: 14px;
            align-items: baseline;
        }
        
        .post-author {
            font-weight: 600;
            color: #1a73e8;
            cursor: pointer;
        }
        
        .post-channel {
            color: #666;
            cursor: pointer;
        }
        
        .post-time {
            color: #666;
            font-size: 12px;
            margin-left: auto;
        }
        
        .post-content {
            white-space: pre-wrap;
            word-wrap: break-word;
            overflow-wrap: break-word;
            line-height: 1.5;
        }
        
        .post-link {
            margin-top: 10px;
            font-size: 12px;
        }
        
        .post-link a {
            color: #666;
            text-decoration: none;
            margin-right: 10px;
        }
        
        .post-link a:active {
            color: #1a73e8;
        }

        .post-link button {
            background: none;
            border: none;
            color: #666;
            cursor: pointer;
            font-size: 12px;
            padding: 0;
            text-decoration: none;
            margin-left: 5px;
        }

        .post-link button:hover {
            color: #1a73e8;
            text-decoration: underline;
        }

        .post-link button:active {
            color: #1a73e8;
        }

        .post-link button.copied {
            color: #0f9d58;
        }
        
        .pagination {
            display: flex;
            justify-content: center;
            gap: 4px;
            padding: 15px;
            flex-wrap: wrap;
        }
        
        .pagination button {
            padding: 8px 12px;
            border: 1px solid #ddd;
            background: white;
            cursor: pointer;
            border-radius: 4px;
            font-size: 14px;
            min-width: 40px;
            min-height: 40px;
        }
        
        .pagination button:active {
            background: #f0f2f5;
        }
        
        .pagination button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .pagination .current {
            background: #1a73e8;
            color: white;
            border-color: #1a73e8;
        }
        
        .pagination span {
            padding: 8px 4px;
            font-size: 14px;
        }
        
        .stats {
            background: white;
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 15px;
        }
        
        .stats h2 {
            font-size: 20px;
            margin: 0 0 15px 0;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
        }
        
        .stat-box {
            text-align: center;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 4px;
        }
        
        .stat-number {
            font-size: 24px;
            font-weight: 600;
            color: #1a73e8;
        }
        
        .stat-label {
            color: #666;
            font-size: 12px;
            margin-top: 4px;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
            color: #666;
        }
        
        .error {
            background: #fee;
            color: #c00;
            padding: 10px;
            border-radius: 4px;
            margin: 10px;
        }
        
        .highlight {
            background: #ff0;
            padding: 2px;
        }
        
        .reply-to {
            background: #f9f9f9;
            border-left: 3px solid #ddd;
            padding: 10px;
            margin-bottom: 10px;
            font-size: 13px;
            color: #555;
            border-radius: 0 4px 4px 0;
        }
        
        .replied-content {
            margin-top: 4px;
            font-style: italic;
            overflow: hidden;
            text-overflow: ellipsis;
            display: -webkit-box;
            -webkit-line-clamp: 3;
            -webkit-box-orient: vertical;
        }
        
        #gmStatus, #channelStatus {
            margin-left: 8px;
            font-size: 12px;
            min-width: 120px;
        }

        @media (max-width: 768px) {
            #gmStatus, #channelStatus {
                margin-left: 0;
                margin-top: 4px;
            }
        }
        
        /* Mobile-specific styles */
        @media (max-width: 768px) {
            .container {
                padding: 8px;
            }
            
            .header {
                padding: 12px;
            }
            
            h1 {
                font-size: 20px;
                margin-bottom: 12px;
            }
            
            .search-box {
                flex-direction: column;
            }
            
            .search-box input[type="text"] {
                width: 100%;
            }
            
            .search-box select {
                width: 100%;
            }
            
            .search-box button {
                width: 100%;
            }
            
            .filters {
                flex-direction: column;
                align-items: stretch;
            }
            
            .filters input[type="date"],
            .filters button,
            .filters select {
                width: 100%;
            }
            
            .post {
                padding: 12px;
            }
            
            .post-header {
                flex-direction: column;
                gap: 4px;
            }
            
            .post-time {
                margin-left: 0;
            }
            
            .pagination button {
                padding: 6px 10px;
                font-size: 13px;
                min-width: 36px;
                min-height: 36px;
            }
            
            .search-help {
                display: none;
            }
            
            /* Show simplified help on mobile */
            .search-help-mobile {
                display: block;
                font-size: 11px;
                color: #666;
                margin-top: 8px;
            }
        }
        
        /* Hide mobile help on desktop */
        .search-help-mobile {
            display: none;
        }
        
        /* Improve touch targets */
        @media (pointer: coarse) {
            .post-author,
            .post-channel,
            .post-link a {
                padding: 4px;
                margin: -4px;
            }
        }
        
        /* Dark mode support */
        @media (prefers-color-scheme: dark) {
            body {
                background: #0f1116;
                color: #e5e7eb;
            }
            .header,
            .results {
                background: #181b22;
                box-shadow: none;
                color: #e5e7eb;
            }
            .stat-chip {
                background: #232738;
                color: #e5e7eb;
            }
            .stat-chip .label {
                color: #9ca3af;
            }
            .stat-chip .value {
                color: #60a5fa;
            }
            .search-top input,
            .search-bottom select,
            .filters input[type="date"],
            .filters select,
            .filters button,
            .stat-box,
            .post,
            .inline-toggle {
                background: #1f2330;
                border-color: #343948;
                color: #e5e7eb;
            }
            .search-box button {
                background: #2563eb;
            }
            .search-box button:active {
                background: #1d4fd8;
            }
            .post {
                border-bottom-color: #2a3040;
            }
            .post-channel,
            .post-time,
            .post-link a,
            .post-link button,
            .search-help {
                color: #9ca3af;
            }
            .post-link button:hover {
                color: #60a5fa;
            }
            .reply-to {
                background: #222738;
                border-color: #3b4257;
                color: #cbd5f5;
            }
            .result-summary {
                color: #cbd5f5;
            }
            .choices__inner,
            .choices__input {
                background: #1f2330;
                border-color: #343948;
                color: #e5e7eb;
            }
            .choices__list--dropdown,
            .choices__list {
                background: #1f2330;
                border-color: #343948;
                color: #e5e7eb;
            }
            .choices__list--dropdown .choices__item--selectable.is-highlighted,
            .choices__item--selectable.is-highlighted {
                background: #2f3648;
                color: #fff;
            }
            .choices__button {
                color: #e5e7eb;
            }
            .choices__list--dropdown .choices__item--selectable::after {
                color: #a5b4fc;
            }
            .loading {
                color: #aaa;
            }
            .choices__placeholder {
                color: #9ca3af;
                opacity: 0.8;
            }
            .search-bottom .choices {
                background: #1f2330;
                border-color: #343948;
            }
            .inline-toggle {
                background: #1f2330;
                border-color: #3a4150;
                color: #d1d5db;
            }
            .inline-toggle input {
                accent-color: #60a5fa;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-top">
                <h1>BlueTracker Database Viewer</h1>
                <div class="stats-inline" id="statsInline">
                    <div class="stat-chip">
                        <div class="label">Posts</div>
                        <div class="value" id="statPosts">--</div>
                    </div>
                    <div class="stat-chip">
                        <div class="label">GMs</div>
                        <div class="value" id="statGms">--</div>
                    </div>
                </div>
            </div>
            
            <div class="search-top">
                <input type="text" id="searchQuery" placeholder="Search posts..." value="">
                <input type="date" id="dateFrom">
                <input type="date" id="dateTo">
                <button class="btn-primary" onclick="search()">Search</button>
                <button class="btn-secondary" onclick="clearFilters()">Reset</button>
            </div>
            
            <div class="search-help">
                <strong>Search syntax:</strong> 
                word (any word) | 
                "exact phrase" | 
                word + word (both required) | 
                /regex/i (regex with flags) |
                Combine multiple patterns
            </div>
            <div class="search-help-mobile">
                Use quotes for exact phrases, + for AND
            </div>
            
            <div class="search-bottom">
                <div class="field-stack">
                    <select id="channelSelect" multiple></select>
                    <div id="channelStatus" class="status-text"></div>
                </div>
                <div class="field-stack">
                    <select id="gmFilter" multiple></select>
                    <div id="gmStatus" class="status-text"></div>
                </div>
                <label class="inline-toggle">
                    <input type="checkbox" id="allTime" />
                    All time
                </label>
                <select id="pageSize" class="option-select">
                    <option value="20">20/page</option>
                    <option value="50" selected>50/page</option>
                    <option value="100">100/page</option>
                </select>
                <select id="sortOrder" class="option-select">
                    <option value="desc" selected>Newest first</option>
                    <option value="asc">Oldest first</option>
                </select>
            </div>

            </div>
        
        <div class="result-summary" id="resultSummary">Use the filters above to search the archive.</div>
        
        <div class="results" id="results">
            <div class="loading">Loading...</div>
        </div>
        
        <div class="pagination" id="pagination"></div>
    </div>
    
    <script>
        // Source guild ID from config
        const SOURCE_GUILD_ID = '226045346399256576';
        
        let currentPage = 1;
        let totalPages = 1;
        let channelChoices;
        let gmChoices;
        
        // Status indicators
        function showLoadingStatus(elementId, message) {
            const element = document.getElementById(elementId);
            if (element) {
                element.innerHTML = `<div style="color: #666; font-size: 12px;">⏳ ${message}</div>`;
            }
        }
        
        function showReadyStatus(elementId, message) {
            const element = document.getElementById(elementId);
            if (element) {
                element.innerHTML = `<div style="color: #28a745; font-size: 12px;">✓ ${message}</div>`;
                setTimeout(() => {
                    if (element.parentElement) {
                        element.style.display = 'none';
                    }
                }, 2000);
            }
        }
        
        function showErrorStatus(elementId, message) {
            const element = document.getElementById(elementId);
            if (element) {
                element.innerHTML = `<div style="color: #dc3545; font-size: 12px;">⚠ ${message}</div>`;
            }
        }

        // Load GMs for dropdown
        async function loadGMs() {
            try {
                showLoadingStatus('gmStatus', 'Loading GMs...');
                
                const response = await fetch('/api/gms');
                if (!response.ok) throw new Error('Failed to load');
                
                const gms = await response.json();
                
                const select = document.getElementById('gmFilter');
                gms.forEach(gm => {
                    const option = document.createElement('option');
                    option.value = gm.id;
                    option.textContent = gm.name;
                    select.appendChild(option);
                });

                gmChoices = new Choices(select, {
                    removeItemButton: true,
                    placeholderValue: 'Select GM(s)…',
                    searchPlaceholderValue: 'Type to search GMs',
                    shouldSort: true
                });
                
                showReadyStatus('gmStatus', `${gms.length} GMs loaded`);
            } catch (error) {
                console.error('Failed to load GMs:', error);
                showErrorStatus('gmStatus', 'Failed to load GMs');
            }
        }

        async function loadChannels() {
            try {
                showLoadingStatus('channelStatus', 'Loading channels...');
                
                const response = await fetch('/api/channels');
                if (!response.ok) throw new Error('Failed to load');
                
                const data = await response.json();
                const select = document.getElementById('channelSelect');
                let totalChannels = 0;
                
                for (const [group, items] of Object.entries(data)) {
                    const optgroup = document.createElement('optgroup');
                    optgroup.label = group;
                    items.forEach(ch => {
                        const opt = document.createElement('option');
                        opt.value = ch.id;
                        opt.text = ch.name;
                        optgroup.appendChild(opt);
                        totalChannels++;
                    });
                    select.appendChild(optgroup);
                }

                channelChoices = new Choices(select, {
                    removeItemButton: true,
                    placeholderValue: 'Select channels…',
                    searchPlaceholderValue: 'Type to search',
                    shouldSort: false
                });
                
                showReadyStatus('channelStatus', `${totalChannels} channels loaded`);
            } catch (error) {
                console.error('Failed to load channels:', error);
                showErrorStatus('channelStatus', 'Failed to load channels');
            }
        }

        function selectedChannelIds() {
            return channelChoices ? channelChoices.getValue(true).join(',') : '';
        }

        function selectedGmIds() {
            return gmChoices ? gmChoices.getValue(true).join(',') : '';
        }

        function readFiltersFromUI() {
            const gmValues = selectedGmIds();
            return {
                q:        document.getElementById('searchQuery').value,
                gm:       gmValues,
                gm_ids:   gmValues,
                channels: selectedChannelIds(),        // already returns "id1,id2"
                from:     document.getElementById('dateFrom').value,
                to:       document.getElementById('dateTo').value,
                page:     currentPage,
                per_page: parseInt(document.getElementById('pageSize').value, 10),
                all_time:  document.getElementById('allTime').checked ? '1' : '0',
                sort:      document.getElementById('sortOrder').value
           };
        }

        function applyFiltersToUI(f) {
            document.getElementById('searchQuery').value = f.q   || '';
            const gmList = (f.gm_ids || f.gm || f.gm_id || '').split(',').filter(Boolean);
            if (gmChoices) {
                gmChoices.removeActiveItems();
                gmList.forEach(id => gmChoices.setChoiceByValue(id));
            } else {
                document.getElementById('gmFilter').value = gmList[0] || '';
            }
            document.getElementById('dateFrom').value     = f.from || '';
            document.getElementById('dateTo').value       = f.to   || '';
            document.getElementById('sortOrder').value    = f.sort || 'desc';
            document.getElementById('allTime').checked    = f.all_time === '1';
            document.getElementById('pageSize').value     = f.per_page || '50';
        
            /* channels: set via Choices.js */
            if (channelChoices) {
                channelChoices.removeActiveItems();
                (f.channels || '').split(',').filter(Boolean).forEach(id => {
                    channelChoices.setChoiceByValue(id);
                });
            }
            currentPage = parseInt(f.page || 1, 10);
        }

        // Load statistics
        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const stats = await response.json();
                document.getElementById('statPosts').textContent = stats.total_posts.toLocaleString();
                document.getElementById('statGms').textContent = stats.total_gms.toLocaleString();
            } catch (error) {
                console.error('Failed to load stats:', error);
                document.getElementById('statPosts').textContent = '--';
                document.getElementById('statGms').textContent = '--';
            }
        }
        
        // Search function
        async function search(page = 1) {
            currentPage = page;

        const f = readFiltersFromUI();
        f.page = page;
        const url = new URL(window.location);
        const shareable = {...f};
        delete shareable.gm;
        Object.entries(shareable).forEach(([k, v]) =>
            v ? url.searchParams.set(k, v) : url.searchParams.delete(k)
        );
            history.replaceState(null, '', url);
            localStorage.setItem('btFilters', JSON.stringify(f));
            
        const params = new URLSearchParams({
            q: f.q, gm_ids: f.gm_ids, channels: f.channels,
            date_from: f.from, date_to: f.to,
            page: f.page, per_page: f.per_page,
            all_time: f.all_time, sort: f.sort
        });
            
        const resultsDiv = document.getElementById('results');
        const summaryEl = document.getElementById('resultSummary');
        summaryEl.textContent = 'Searching...';
        resultsDiv.innerHTML = '<div class="loading">Searching...</div>';

            try {
                // Add 30 second timeout to prevent hanging
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 30000);

                const response = await fetch('/api/search?' + params, {
                    signal: controller.signal
                });
                clearTimeout(timeoutId);

                const data = await response.json();
                
                totalPages = data.total_pages || 1;
                
                if (data.results.length === 0) {
                    resultsDiv.innerHTML = '<div class="loading">No results found</div>';
                    summaryEl.textContent = data.info || 'No results found.';
                    return;
                }

                const total = data.total || 0;
                const startIdx = ((data.page - 1) * data.per_page) + 1;
                const endIdx = startIdx + data.results.length - 1;
                const infoTail = data.info ? ` • ${data.info}` : '';
                summaryEl.textContent = `${total.toLocaleString()} results • Showing ${startIdx}-${endIdx} • Page ${data.page} of ${Math.max(1, data.total_pages || 1)} • ${data.sort === 'asc' ? 'Oldest first' : 'Newest first'}${infoTail}`;
                
                resultsDiv.innerHTML = data.results.map(post => `
                    <div class="post">
                        <div class="post-header">
                            <span class="post-author"
                                data-author-id="${post.author_id}">
                                ${escapeHtml(post.author_name)}
                            </span>
                            <span> · </span>
                            <span class="post-channel"
                                data-chan-id="${post.channel_id}">
                                #${escapeHtml(post.channel)}
                            </span>
                            <span class="post-time">${formatDate(post.datetime)}</span>
                        </div>
                        ${post.replied_to ? `
                            <div class="reply-to">
                                ↳ Replying to <strong>${escapeHtml(post.replied_to.author_name)}</strong>:
                                <div class="replied-content">${highlightSearch(escapeHtml(post.replied_to.content || '(no content)'))}</div>
                            </div>
                        ` : ''}
                        <div class="post-content">${highlightSearch(escapeHtml(post.content || '(no content)'))}</div>
                        <div class="post-link">
                            <a href="${post.jump_url}" target="_blank">Jump to message ↗</a>
                            <button onclick="copyToClipboard('${post.jump_url}', this)" title="Copy link to clipboard">Copy link</button>
                            • ID: ${post.id}
                        </div>
                    </div>
                `).join('');
                
                updatePagination();
                
            } catch (error) {
                console.error('Search failed', error);
                let errorMsg = error.message;
                if (error.name === 'AbortError') {
                    errorMsg = 'Search timed out after 30 seconds. The database may be busy. Try again in a moment.';
                } else if (error.message === 'Failed to fetch') {
                    errorMsg = 'Could not reach the server. The service may be temporarily down.';
                }
                resultsDiv.innerHTML = '<div class="error">Search failed: ' + errorMsg + '</div>';
                document.getElementById('resultSummary').textContent = 'Search failed.';
            }
        }
        
        // Update pagination buttons
        function updatePagination() {
            const paginationDiv = document.getElementById('pagination');
            
            if (totalPages <= 1) {
                paginationDiv.innerHTML = '';
                return;
            }
            
            let buttons = [];
            
            // Previous button
            buttons.push(`<button onclick="search(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''}>Previous</button>`);
            
            // Page numbers
            for (let i = 1; i <= Math.min(totalPages, 10); i++) {
                if (i === currentPage) {
                    buttons.push(`<button class="current">${i}</button>`);
                } else {
                    buttons.push(`<button onclick="search(${i})">${i}</button>`);
                }
            }
            
            if (totalPages > 10) {
                buttons.push('<span>...</span>');
                buttons.push(`<button onclick="search(${totalPages})">${totalPages}</button>`);
            }
            
            // Next button
            buttons.push(`<button onclick="search(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''}>Next</button>`);
            
            paginationDiv.innerHTML = buttons.join('');
        }
        
        // Clear all filters
        function clearFilters() { 
            document.getElementById('searchQuery').value = '';
            if (gmChoices) gmChoices.removeActiveItems();
            if (channelChoices) channelChoices.removeActiveItems();
            document.getElementById('dateFrom').value = '';
            document.getElementById('dateTo').value = '';
            document.getElementById('allTime').checked = false;
            document.getElementById('sortOrder').value = 'desc';
            document.getElementById('pageSize').value = '50';
            history.replaceState(null,'',location.pathname);
            localStorage.removeItem('btFilters');
            search(1); 
        }
        
        // Utility functions
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function copyToClipboard(text, button) {
            navigator.clipboard.writeText(text).then(() => {
                // Visual feedback
                const originalText = button.textContent;
                button.textContent = 'Copied ✓';
                button.classList.add('copied');

                // Reset after 2 seconds
                setTimeout(() => {
                    button.textContent = originalText;
                    button.classList.remove('copied');
                }, 2000);
            }).catch(err => {
                console.error('Failed to copy:', err);
                button.textContent = 'Failed to copy';
                setTimeout(() => {
                    button.textContent = 'Copy link';
                }, 2000);
            });
        }
        
        function formatDate(iso) {
            return new Date(iso).toLocaleString();
        }
        
        function highlightSearch(text) {
            const query = document.getElementById('searchQuery').value;
            if (!query) return text;
            
            // Simple highlight for quoted phrases
            const phrases = query.match(/"([^"]+)"/g);
            if (phrases) {
                phrases.forEach(phrase => {
                    const clean = phrase.replace(/"/g, '');
                    try {
                        const regex = new RegExp(escapeRegex(clean), 'gi');
                        text = text.replace(regex, match => `<span class="highlight">${match}</span>`);
                    } catch (_) { /* ignore malformed pattern */ }
                });
            }
            return text;
        }
        
        function escapeRegex(string) {
            return string.replace(/[.*+?^${}()|\\[\\]\\\\]/g, '\\$&')
        }
               
        // Initialize
        window.onload = async function() {
            await loadGMs();
            await loadChannels();
            await loadStats();
            let f;
            if (location.search.length > 1) {
                f = Object.fromEntries(new URLSearchParams(location.search));
            } else {
                f = JSON.parse(localStorage.getItem('btFilters') || '{}');
            }
            applyFiltersToUI(f);

            /* 2. run the first search */
            const page = parseInt(f.page || 1, 10);
            await search(page)

            
            // Enter key in search box
            document.getElementById('searchQuery').addEventListener('keypress', function(e) {
                if (e.key === 'Enter') search();
            });
            document.getElementById('pageSize').addEventListener('change', () => {
                search(1); // restart from page 1 with new page size
            });
            document.getElementById('sortOrder').addEventListener('change', () => search(1));
            document.getElementById('allTime').addEventListener('change', () => search(1));
            
            // Handle clicks with better mobile support
            document.getElementById('results').addEventListener('click', e => {
                // GM name clicked?
                const authorSpan = e.target.closest('[data-author-id]');
                if (authorSpan) {
                    e.preventDefault();
                    document.getElementById('gmFilter').value = authorSpan.dataset.authorId;
                    search(1);     // reset to page 1
                    return;
                }
                // Channel name clicked?
                const chanSpan = e.target.closest('[data-chan-id]');
                if (chanSpan && channelChoices) {
                    e.preventDefault();
                    const id = chanSpan.dataset.chanId;
                    /* add only if not already selected */
                    if (!channelChoices.getValue(true).includes(id)) {
                        channelChoices.setChoiceByValue(id);
                    }
                    search(1);
                }
            });
            
            // Mobile-specific improvements
            if ('ontouchstart' in window) {
                // Prevent double-tap zoom on buttons
                let lastTouchEnd = 0;
                document.addEventListener('touchend', function(e) {
                    const now = Date.now();
                    if (now - lastTouchEnd <= 300) {
                        e.preventDefault();
                    }
                    lastTouchEnd = now;
                }, false);
                
                // Add active states for better touch feedback
                document.querySelectorAll('button, a').forEach(el => {
                    el.addEventListener('touchstart', function() {
                        this.classList.add('touch-active');
                    });
                    el.addEventListener('touchend', function() {
                        setTimeout(() => this.classList.remove('touch-active'), 100);
                    });
                });
            }
        };
    </script>
</body>
</html>
'''

surprise_template = '''
<!DOCTYPE html>
<html>
<head>
    <title>BlueTracker Deep Search</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=0">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/choices.js/public/assets/styles/choices.min.css">
    <script defer src="https://cdn.jsdelivr.net/npm/choices.js/public/assets/scripts/choices.min.js"></script>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 0;
            background: #f0f2f5;
        }
        .stats-inline {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }
        .stat-chip {
            background: #f5f6fa;
            border-radius: 6px;
            padding: 6px 10px;
            min-width: 120px;
            text-align: right;
        }
        .stat-chip .label {
            font-size: 11px;
            color: #6b7280;
        }
        .stat-chip .value {
            font-size: 18px;
            font-weight: 600;
            color: #1a73e8;
        }
        .container { max-width: 1100px; margin: 0 auto; padding: 10px; }
        .header {
            background: #fff;
            padding: 14px 16px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.08);
            margin-bottom: 14px;
        }
        .header-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
            margin-bottom: 8px;
        }
        h1 { margin: 0; font-size: 22px; }
        .subtitle { color: #6b7280; font-size: 14px; }
        .search-top {
            display: grid;
            grid-template-columns: minmax(320px, 1fr) repeat(2, minmax(140px, 1fr)) repeat(2, max-content);
            gap: 8px;
            align-items: center;
            margin-bottom: 4px;
        }
        .search-bottom {
            display: grid;
            grid-template-columns: repeat(2, minmax(200px, 1fr)) auto auto auto;
            gap: 8px;
            align-items: center;
            margin-bottom: 6px;
        }
        .search-top input,
        .search-bottom select {
            padding: 8px 10px;
            border: 1px solid #ddd;
            border-radius: 6px;
            min-height: 44px;
        }
        .search-top button {
            padding: 10px 18px;
            border: none;
            border-radius: 6px;
            color: #fff;
            font-size: 15px;
            min-height: 40px;
            cursor: pointer;
        }
        .btn-primary { background: #1a73e8; }
        .btn-secondary { background: #4b5563; }
        .search-help {
            font-size: 12px;
            color: #6b7280;
            line-height: 1.4;
            margin-bottom: 6px;
        }
        .field-stack {
            display: flex;
            flex-direction: column;
        }
        .status-text { font-size: 12px; color: #6b7280; margin-top: 4px; }
        .status-text:empty {
            display: none;
            margin-top: 0;
        }
        .inline-toggle {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 14px;
            border: 1px solid #ddd;
            border-radius: 6px;
            background: #fff;
            min-height: 44px;
            font-size: 14px;
            color: #4b5563;
            box-sizing: border-box;
        }
        .inline-toggle input { width: 16px; height: 16px; }
        .option-select { min-width: 130px; }
        .results {
            background: #fff;
            border-radius: 8px;
            padding: 0;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .result-summary {
            font-size: 14px;
            color: #4b5563;
            margin-bottom: 10px;
        }
        .post {
            border-bottom: 1px solid #e5e7eb;
            padding: 14px 16px;
        }
        .post:last-child { border-bottom: none; }
        .post-header {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            font-size: 14px;
            color: #4b5563;
            margin-bottom: 6px;
        }
        .post-content { font-size: 15px; line-height: 1.45; white-space: pre-wrap; }
        .post-channel { font-weight: 600; color: #1f2937; }
        .post-author { font-weight: 600; }
        .post-time { color: #6b7280; }
        .post-link { margin-top: 6px; font-size: 12px; color: #6b7280; }
        .loading { padding: 20px; text-align: center; color: #6b7280; }
        .error { padding: 12px; color: #b91c1c; }
        .pagination {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            margin-top: 12px;
        }
        .pagination button {
            padding: 6px 12px;
            border-radius: 6px;
            border: 1px solid #cbd5f5;
            background: #fff;
            cursor: pointer;
        }
        .pagination button.current {
            background: #1a73e8;
            border-color: #1a73e8;
            color: #fff;
        }
        .pagination button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .choices {
            background: #1f2330;
            border: 1px solid #343948;
            border-radius: 6px;
            color: #e5e7eb;
        }
        .choices__inner {
            padding: 8px 40px 8px 12px;
            min-height: 44px;
            border-radius: 6px;
            border: none;
            background: transparent;
            color: inherit;
        }
        .choices__input {
            background: transparent;
            color: inherit;
        }
        .choices__list--dropdown,
        .choices__list {
            background: #1b1f2a;
            border: 1px solid #343948;
            color: #e5e7eb;
        }
        .choices__list--dropdown .choices__item,
        .choices__list--dropdown .choices__item--selectable,
        .choices__list--dropdown .choices__item--choice {
            background: transparent !important;
            color: inherit;
        }
        .choices__list--dropdown .choices__item--choice.is-selected,
        .choices__list--dropdown .choices__item--choice.is-highlighted,
        .choices__list--dropdown .choices__item--selectable.is-highlighted,
        .choices__item--selectable.is-highlighted {
            background: #2f3648 !important;
            color: #fff !important;
        }
        .search-bottom .inline-toggle,
        .search-bottom .option-select {
            align-self: stretch;
            height: 44px;
        }
        @media (max-width: 1024px) {
            .search-top,
            .search-bottom { grid-template-columns: repeat(2, 1fr); }
        }
        @media (max-width: 640px) {
            .search-top,
            .search-bottom { grid-template-columns: 1fr; }
            .inline-toggle { width: 100%; }
        }
        @media (prefers-color-scheme: dark) {
            body { background: #0f1116; color: #e5e7eb; }
            .header,
            .results { background: #181b22; box-shadow: none; }
            .search-top input,
            .search-bottom select,
            .inline-toggle,
            .search-top button {
                background: #1f2330;
                border-color: #343948;
                color: #e5e7eb;
            }
            .btn-primary { background: #2563eb; }
            .btn-secondary { background: #4b5563; }
            .post { border-bottom-color: #2a3040; }
            .post-channel,
            .post-time,
            .post-link,
            .subtitle,
            .result-summary { color: #9ca3af; }
            .choices__inner,
            .choices__list--dropdown,
            .choices__list { background: #1f2330; border-color: #343948; color: #e5e7eb; }
            .choices__list--dropdown .choices__item--selectable.is-highlighted,
            .choices__item--selectable.is-highlighted { background: inherit; color: inherit; }
            .stat-chip {
                background: #232738;
                color: #e5e7eb;
            }
            .stat-chip .label { color: #9ca3af; }
            .stat-chip .value { color: #60a5fa; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-top">
                <h1>Deleted & Surprise Search</h1>
                <div class="stats-inline">
                    <div class="stat-chip">
                        <div class="label">Posts indexed</div>
                        <div class="value" id="surprisePostCount">--</div>
                    </div>
                </div>
            </div>
            <div class="subtitle">Inspect deleted, edited, or legacy posts with full-text search.</div>
            <div class="search-top">
                <input type="text" id="query" placeholder="Search deleted content...">
                <input type="date" id="dateFrom">
                <input type="date" id="dateTo">
                <button class="btn-primary" onclick="doSearch(1)">Search</button>
                <button class="btn-secondary" onclick="resetForm()">Reset</button>
            </div>
            <div class="search-help">
                Search syntax: word (any) | "exact phrase" | word + word (both) | /regex/i | combine multiple patterns.
            </div>
            <div class="search-bottom">
                <div class="field-stack">
                    <select id="channels" multiple></select>
                    <div id="surpriseChannelStatus" class="status-text"></div>
                </div>
                <div class="field-stack">
                    <select id="members" multiple></select>
                    <div id="surpriseMemberStatus" class="status-text"></div>
                </div>
                <label class="inline-toggle">
                    <input type="checkbox" id="deletedOnly" checked>
                    Deleted only
                </label>
                <label class="inline-toggle">
                    <input type="checkbox" id="surpriseAllTime">
                    All time
                </label>
                <select id="pageSize" class="option-select">
                    <option value="25">25/page</option>
                    <option value="50" selected>50/page</option>
                    <option value="100">100/page</option>
                </select>
            </div>
        </div>
        <div class="result-summary" id="surpriseSummary">Use the filters above to search deleted or hidden posts.</div>
        <div class="results" id="results">
            <div class="loading">Waiting for your first query…</div>
        </div>
        <div class="pagination" id="pagination"></div>
    </div>
<script>
let channelChoices;
let memberChoices;
let currentPage = 1;
let totalPages = 1;

const resultsDiv = document.getElementById("results");
const summaryEl = document.getElementById("surpriseSummary");
const paginationEl = document.getElementById("pagination");
const postCountEl = document.getElementById("surprisePostCount");

function escapeHtml(text) {
    const map = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
    };
    return text ? text.replace(/[&<>"']/g, m => map[m]) : "";
}

function formatDate(ts) {
    if (!ts) return "Unknown time";
    return new Date(ts).toLocaleString();
}

function selectedValues(choiceInstance, fallbackSelect) {
    if (choiceInstance) {
        return choiceInstance.getValue(true);
    }
    return Array.from(fallbackSelect.selectedOptions).map(o => o.value);
}

function buildParams(page) {
    const params = new URLSearchParams({
        q: document.getElementById("query").value.trim(),
        date_from: document.getElementById("dateFrom").value,
        date_to: document.getElementById("dateTo").value,
        deleted: document.getElementById("deletedOnly").checked ? 1 : 0,
        all_time: document.getElementById("surpriseAllTime").checked ? 1 : 0,
        channels: selectedValues(channelChoices, document.getElementById("channels")).join(","),
        members: selectedValues(memberChoices, document.getElementById("members")).join(","),
        per_page: document.getElementById("pageSize").value,
        page
    });
    return params;
}

function resetForm() {
    document.getElementById("query").value = "";
    document.getElementById("dateFrom").value = "";
    document.getElementById("dateTo").value = "";
    document.getElementById("deletedOnly").checked = true;
    document.getElementById("surpriseAllTime").checked = false;
    document.getElementById("pageSize").value = "50";
    if (channelChoices) channelChoices.removeActiveItems();
    if (memberChoices) memberChoices.removeActiveItems();
    doSearch(1);
}

function setStatus(id, message, success=false) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = message;
    el.style.color = success ? "#16a34a" : "#6b7280";
    if (success) {
        setTimeout(() => { el.textContent = ""; }, 2000);
    }
}

async function loadChannels() {
    setStatus("surpriseChannelStatus", "Loading channels…");
    try {
        const res = await fetch("/api/all_channels");
        const data = await res.json();
        const select = document.getElementById("channels");
        select.innerHTML = "";
        const grouped = Object.entries(data).map(([group, chans]) => ({
            label: group,
            id: group,
            disabled: false,
            choices: chans.map(ch => ({ value: ch.id, label: ch.name }))
        }));
        channelChoices = new Choices(select, {
            removeItemButton: true,
            shouldSort: false,
            placeholder: true,
            placeholderValue: "Select channels…",
            searchResultLimit: 20
        });
        channelChoices.setChoices(grouped, "value", "label", true);
        setStatus("surpriseChannelStatus", "Channels ready", true);
    } catch (err) {
        console.error("Channel load failed", err);
        setStatus("surpriseChannelStatus", "Failed to load channels");
    }
}

async function loadMembers() {
    setStatus("surpriseMemberStatus", "Loading members…");
    try {
        const res = await fetch("/api/members");
        const data = await res.json();
        const select = document.getElementById("members");
        select.innerHTML = "";
        memberChoices = new Choices(select, {
            removeItemButton: true,
            shouldSort: false,
            placeholder: true,
            placeholderValue: "Select GM(s)…",
            searchResultLimit: 30
        });
        memberChoices.setChoices(
            data.map(m => ({ value: m.id, label: m.name })),
            "value",
            "label",
            true
        );
        setStatus("surpriseMemberStatus", "Members ready", true);
    } catch (err) {
        console.error("Member load failed", err);
        setStatus("surpriseMemberStatus", "Failed to load members");
    }
}

function renderPagination() {
    if (totalPages <= 1) {
        paginationEl.innerHTML = "";
        return;
    }
    const buttons = [];
    buttons.push(`<button onclick="doSearch(${currentPage - 1})" ${currentPage === 1 ? "disabled" : ""}>Previous</button>`);
    const windowSize = Math.min(totalPages, 10);
    let start = Math.max(1, currentPage - Math.floor(windowSize / 2));
    let end = Math.min(totalPages, start + windowSize - 1);
    if (end - start < windowSize - 1) {
        start = Math.max(1, end - windowSize + 1);
    }
    for (let i = start; i <= end; i++) {
        if (i === currentPage) {
            buttons.push(`<button class="current">${i}</button>`);
        } else {
            buttons.push(`<button onclick="doSearch(${i})">${i}</button>`);
        }
    }
    buttons.push(`<button onclick="doSearch(${currentPage + 1})" ${currentPage === totalPages ? "disabled" : ""}>Next</button>`);
    paginationEl.innerHTML = buttons.join("");
}

async function doSearch(page = 1) {
    currentPage = page;
    const params = buildParams(page);
    summaryEl.textContent = "Searching…";
    resultsDiv.innerHTML = '<div class="loading">Searching…</div>';
    paginationEl.innerHTML = "";
    try {
        const response = await fetch("/api/surprise_search?" + params.toString());
        const data = await response.json();
        totalPages = data.total_pages || 1;
        if (!data.results || data.results.length === 0) {
            resultsDiv.innerHTML = '<div class="loading">No results found</div>';
            summaryEl.textContent = "No results found.";
            return;
        }
        const total = data.total || 0;
        const start = ((data.page - 1) * data.per_page) + 1;
        const end = start + data.results.length - 1;
        summaryEl.textContent = `${total.toLocaleString()} results • Showing ${start}-${end} • Page ${data.page} of ${Math.max(1, data.total_pages || 1)} • ${document.getElementById("deletedOnly").checked ? "Deleted only" : "All posts"}`;
        resultsDiv.innerHTML = data.results.map(post => `
            <div class="post">
                <div class="post-header">
                    <span class="post-author">${escapeHtml(post.author || "Unknown")}</span>
                    <span>•</span>
                    <span class="post-channel">#${escapeHtml(post.channel || "unknown")}</span>
                    <span class="post-time">${formatDate(post.timestamp)}</span>
                </div>
                <div class="post-content">${escapeHtml(post.content || "(no content)")}</div>
                <div class="post-link">ID: ${post.id}</div>
            </div>
        `).join("");
        renderPagination();
    } catch (err) {
        console.error("Surprise search failed", err);
        resultsDiv.innerHTML = `<div class="error">Search failed: ${escapeHtml(err.message)}</div>`;
        summaryEl.textContent = "Search failed.";
    }
}

document.getElementById("pageSize").addEventListener("change", () => doSearch(1));
document.getElementById("deletedOnly").addEventListener("change", () => doSearch(1));
document.getElementById("surpriseAllTime").addEventListener("change", () => doSearch(1));
document.getElementById("query").addEventListener("keypress", e => {
    if (e.key === "Enter") {
        e.preventDefault();
        doSearch(1);
    }
});

async function loadStats() {
    if (!postCountEl) return;
    try {
        const res = await fetch("/api/stats");
        const data = await res.json();
        if (typeof data.total_posts !== "undefined") {
            postCountEl.textContent = Number(data.total_posts).toLocaleString();
        } else if (typeof data.posts !== "undefined") {
            postCountEl.textContent = Number(data.posts).toLocaleString();
        } else {
            postCountEl.textContent = "--";
        }
    } catch (err) {
        console.error("Failed to load stats", err);
        postCountEl.textContent = "--";
    }
}

loadChannels();
loadMembers();
loadStats();
doSearch(1);
</script>
</body>
</html>
'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BlueTracker Viewer")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", default=5000, type=int, help="Port to bind")
    parser.add_argument("--db", help="Override DATABASE_URL")
    parser.add_argument("--reload", action="store_true", help="Enable Flask reloader (dev only)")
    parser.add_argument("--debug", action="store_true", help="Enable Werkzeug debugger")
    args = parser.parse_args()

    if args.db:
        app.config['DATABASE_URL'] = args.db

    print(f"Starting viewer on http://{args.host}:{args.port}")
    print(f"Database: {app.config['DATABASE_URL']}")

    run_simple(
        args.host,
        args.port,
        app,
        use_reloader=args.reload,
        use_debugger=args.debug,
    )
