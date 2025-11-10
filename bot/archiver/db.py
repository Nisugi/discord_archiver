import asyncio
import json
import os
import time
from typing import Any, Iterable, Optional, Sequence

import asyncpg

from .config import DATABASE_URL, GM_NAME_OVERRIDES, SEED_BLUE_IDS

_pool: Optional[asyncpg.Pool] = None

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE IF NOT EXISTS members(
    member_id     TEXT PRIMARY KEY,
    username      TEXT,
    display_name  TEXT,
    avatar_url    TEXT,
    is_bot        BOOLEAN,
    is_gm         BOOLEAN DEFAULT FALSE,
    joined_at     BIGINT
);

CREATE TABLE IF NOT EXISTS roles(
    role_id       TEXT PRIMARY KEY,
    name          TEXT,
    color         INTEGER,
    position      INTEGER,
    permissions   TEXT
);

CREATE TABLE IF NOT EXISTS member_roles(
    member_id     TEXT NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
    role_id       TEXT NOT NULL REFERENCES roles(role_id)     ON DELETE CASCADE,
    captured_at   BIGINT      NOT NULL,
    PRIMARY KEY(member_id, role_id, captured_at)
);

CREATE TABLE IF NOT EXISTS channels(
    chan_id         TEXT PRIMARY KEY,
    guild_id        TEXT,
    parent_id       TEXT,
    name            TEXT,
    type            TEXT,
    topic           TEXT,
    accessible      BOOLEAN,
    last_message_id TEXT,
    created_ts      BIGINT
);

CREATE TABLE IF NOT EXISTS posts(
    post_id      TEXT PRIMARY KEY,
    chan_id      TEXT NOT NULL REFERENCES channels(chan_id),
    author_id    TEXT NOT NULL REFERENCES members(member_id),
    content      TEXT,
    created_ts   BIGINT,
    edited_ts    BIGINT,
    pinned       BOOLEAN,
    deleted      BOOLEAN DEFAULT FALSE,
    reply_to_id  TEXT,
    content_tsv  tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(content, ''))
    ) STORED
);

CREATE TABLE IF NOT EXISTS post_revisions(
    rev_id       BIGSERIAL PRIMARY KEY,
    post_id      TEXT,
    chan_id      TEXT,
    author_id    TEXT,
    content      TEXT,
    captured_ts  BIGINT,
    is_edit      BOOLEAN
);

CREATE TABLE IF NOT EXISTS attachments(
    attach_id    TEXT PRIMARY KEY,
    post_id      TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    filename     TEXT,
    url          TEXT,
    content_type TEXT,
    size         INTEGER,
    width        INTEGER,
    height       INTEGER
);

CREATE TABLE IF NOT EXISTS embeds(
    embed_id   BIGSERIAL PRIMARY KEY,
    post_id    TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    type       TEXT,
    data_json  TEXT
);

CREATE TABLE IF NOT EXISTS gm_names(
    author_id  TEXT PRIMARY KEY,
    gm_name    TEXT NOT NULL,
    notes      TEXT,
    updated_at BIGINT DEFAULT ((extract(epoch FROM now()) * 1000)::bigint)
);

CREATE TABLE IF NOT EXISTS bot_metadata(
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at BIGINT DEFAULT ((extract(epoch FROM now()) * 1000)::bigint)
);

CREATE TABLE IF NOT EXISTS crawl_progress(
    chan_id      TEXT PRIMARY KEY,
    last_seen_id TEXT NOT NULL,
    updated_at   BIGINT NOT NULL
);

CREATE OR REPLACE VIEW gm_posts_view AS
SELECT p.*
FROM posts p
JOIN members m ON m.member_id = p.author_id
WHERE COALESCE((m.is_gm)::text, '0') IN ('1','t','true')
  AND NOT (COALESCE((p.deleted)::text, '0') IN ('1','t','true'));

-- Materialized view for the default 90-day view (optimizes "Reset" button clicks)
-- This caches the most common query to avoid expensive COUNT(*) operations
-- Refresh this view every 10 minutes via bot or periodic task
-- Note: PostgreSQL doesn't support IF NOT EXISTS for materialized views, so this will error on reruns
-- Run manually if needed: DROP MATERIALIZED VIEW IF EXISTS gm_posts_90day;
CREATE MATERIALIZED VIEW gm_posts_90day AS
SELECT p.*
FROM posts p
JOIN members m ON m.member_id = p.author_id
WHERE COALESCE((m.is_gm)::text, '0') IN ('1','t','true')
  AND NOT (COALESCE((p.deleted)::text, '0') IN ('1','t','true'))
  AND p.created_ts >= (EXTRACT(EPOCH FROM NOW() - INTERVAL '90 days') * 1000)::BIGINT
ORDER BY p.created_ts DESC;

-- Index on the materialized view for fast lookups
CREATE UNIQUE INDEX idx_gm_posts_90day_pk ON gm_posts_90day (post_id);
CREATE INDEX idx_gm_posts_90day_ts ON gm_posts_90day (created_ts DESC);
CREATE INDEX idx_gm_posts_90day_chan ON gm_posts_90day (chan_id, created_ts DESC);

CREATE INDEX IF NOT EXISTS idx_posts_chan_ts   ON posts (chan_id, created_ts DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author_ts ON posts (author_id, created_ts DESC);
CREATE INDEX IF NOT EXISTS idx_posts_reply     ON posts (reply_to_id) WHERE reply_to_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_posts_tsv       ON posts USING gin (content_tsv);
CREATE INDEX IF NOT EXISTS idx_members_is_gm   ON members (is_gm);
CREATE INDEX IF NOT EXISTS idx_channels_parent ON channels (parent_id);
"""


def _split_statements(sql: str) -> Iterable[str]:
    buff: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            if i + 1 < len(sql) and sql[i + 1] == "'":
                buff.append("''")
                i += 2
                continue
            in_single = not in_single
            buff.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            buff.append(ch)
        elif ch == ";" and not in_single and not in_double:
            statement = "".join(buff).strip()
            if statement:
                yield statement
            buff = []
        else:
            buff.append(ch)
        i += 1
    trailing = "".join(buff).strip()
    if trailing:
        yield trailing


RUN_SCHEMA_ON_STARTUP = os.getenv("RUN_SCHEMA_ON_STARTUP", "0") == "1"


async def open_db() -> asyncpg.Pool:
    """Create (or return) the asyncpg pool and ensure schema exists."""
    global _pool
    if _pool:
        return _pool

    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=60,
    )
    if RUN_SCHEMA_ON_STARTUP:
        async with _pool.acquire() as conn:
            for stmt in _split_statements(SCHEMA_SQL):
                await conn.execute(stmt)
    return _pool


async def close_db():
    """Close the asyncpg pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def _prepare_query(sql: str) -> str:
    """Convert SQLite-style ? placeholders into $-style for asyncpg."""
    if "?" not in sql:
        return sql
    result: list[str] = []
    arg_index = 1
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
            result.append(f"${arg_index}")
            arg_index += 1
        else:
            result.append(ch)
        i += 1
    return "".join(result)


async def _acquire_connection() -> asyncpg.Connection:
    pool = await open_db()
    return await pool.acquire()


async def _release_connection(conn: asyncpg.Connection):
    pool = await open_db()
    await pool.release(conn)


async def _run_sql(
    sql: str,
    params: Sequence[Any] = (),
    *,
    fetch: bool = False,
    fetchrow: bool = False,
    fetchval: bool = False,
) -> Any:
    sql = _prepare_query(sql)
    conn = await _acquire_connection()
    try:
        if fetch:
            return await conn.fetch(sql, *params)
        if fetchrow:
            return await conn.fetchrow(sql, *params)
        if fetchval:
            return await conn.fetchval(sql, *params)
        return await conn.execute(sql, *params)
    finally:
        await _release_connection(conn)


async def fetchone(db, query: str, params: Sequence[Any] = ()):
    del db  # compatibility no-op
    return await _run_sql(query, params, fetchrow=True)


async def fetchall(db, query: str, params: Sequence[Any] = ()):
    del db
    return await _run_sql(query, params, fetch=True)


async def execute_with_retry(db, query: str, params: Sequence[Any] = (), max_retries: int = 3):
    del db
    for attempt in range(max_retries):
        try:
            return await _run_sql(query, params)
        except asyncpg.PostgresError as exc:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(0.1 * (attempt + 1))
            print(f"[DB] Retry due to Postgres error: {exc}")


async def check_database_health(db):
    """Basic health check: ensure we can count posts/members quickly."""
    del db
    try:
        start = time.time()
        posts = await _run_sql("SELECT COUNT(*) FROM posts", fetchval=True)
        members = await _run_sql("SELECT COUNT(*) FROM members", fetchval=True)
        duration = time.time() - start
        if duration > 2.0:
            print(f"[Health] Warning: slow count query took {duration:.2f}s")
        else:
            print(f"[Health] OK: {posts} posts / {members} members in {duration:.2f}s")
        return True
    except Exception as exc:
        print(f"[Health] Database health check failed: {exc}")
        return False


async def upsert_member(db, member, *, conn: Optional[asyncpg.Connection] = None):
    del db
    if member is None:
        return
    payload = (
        str(member.id),
        member.name,
        getattr(member, "display_name", None),
        str(member.display_avatar.url) if getattr(member, "display_avatar", None) else None,
        bool(getattr(member, "bot", False)),
        int(member.joined_at.timestamp() * 1000) if getattr(member, "joined_at", None) else None,
    )
    sql = """
        INSERT INTO members (member_id, username, display_name, avatar_url, is_bot, joined_at)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (member_id) DO UPDATE
        SET username = EXCLUDED.username,
            display_name = EXCLUDED.display_name,
            avatar_url = EXCLUDED.avatar_url,
            is_bot = EXCLUDED.is_bot,
            joined_at = COALESCE(EXCLUDED.joined_at, members.joined_at)
    """
    await _run_with_optional_conn(sql, payload, conn=conn)


async def _run_with_optional_conn(sql: str, params: Sequence[Any], *, conn: Optional[asyncpg.Connection]):
    sql = _prepare_query(sql)
    if conn is not None:
        await conn.execute(sql, *params)
    else:
        await _run_sql(sql, params)


async def save_message(db, msg):
    """Persist a Discord message."""
    del db
    pool = await open_db()
    async with pool.acquire() as conn:
        async with conn.transaction():
            parent_id = str(msg.channel.parent_id) if getattr(msg.channel, "parent_id", None) else None
            channel_type = getattr(msg.channel, "type", None)
            channel_topic = getattr(msg.channel, "topic", None)
            created_ts = int(msg.created_at.timestamp() * 1000)
            guild = getattr(msg.channel, "guild", None)
            guild_id = str(guild.id) if guild else None
            await conn.execute(
                """
                INSERT INTO channels (chan_id, guild_id, parent_id, name, type, topic, accessible, last_message_id, created_ts)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (chan_id) DO UPDATE SET
                    parent_id = EXCLUDED.parent_id,
                    name = EXCLUDED.name,
                    type = EXCLUDED.type,
                    topic = EXCLUDED.topic,
                    accessible = EXCLUDED.accessible,
                    last_message_id = EXCLUDED.last_message_id,
                    created_ts = COALESCE(channels.created_ts, EXCLUDED.created_ts)
                """,
                str(msg.channel.id),
                guild_id,
                parent_id,
                msg.channel.name,
                str(channel_type),
                channel_topic,
                True,
                str(msg.id),
                created_ts,
            )

            await upsert_member(None, msg.author, conn=conn)

            reply_to = None
            if msg.reference and msg.reference.message_id:
                reply_to = str(msg.reference.message_id)

            await conn.execute(
                """
                INSERT INTO posts (post_id, chan_id, author_id, content, created_ts, edited_ts, pinned, deleted, reply_to_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (post_id) DO UPDATE SET
                    content = EXCLUDED.content,
                    edited_ts = EXCLUDED.edited_ts,
                    pinned = EXCLUDED.pinned,
                    deleted = EXCLUDED.deleted,
                    reply_to_id = EXCLUDED.reply_to_id
                """,
                str(msg.id),
                str(msg.channel.id),
                str(msg.author.id),
                msg.content,
                created_ts,
                None,
                bool(getattr(msg, "pinned", False)),
                False,
                reply_to,
            )

            for attachment in getattr(msg, "attachments", []):
                await conn.execute(
                    """
                    INSERT INTO attachments(attach_id, post_id, filename, url, content_type, size, width, height)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    ON CONFLICT (attach_id) DO UPDATE SET
                        filename = EXCLUDED.filename,
                        url = EXCLUDED.url,
                        content_type = EXCLUDED.content_type,
                        size = EXCLUDED.size,
                        width = EXCLUDED.width,
                        height = EXCLUDED.height
                    """,
                    str(attachment.id),
                    str(msg.id),
                    attachment.filename,
                    attachment.url,
                    attachment.content_type,
                    attachment.size,
                    attachment.width,
                    attachment.height,
                )

            for embed in getattr(msg, "embeds", []):
                await conn.execute(
                    """
                    INSERT INTO embeds(post_id, type, data_json)
                    VALUES ($1,$2,$3)
                    """,
                    str(msg.id),
                    embed.type,
                    json.dumps(embed.to_dict(), separators=(",", ":")),
                )

            await conn.execute(
                "UPDATE channels SET last_message_id = $1 WHERE chan_id = $2",
                str(msg.id),
                str(msg.channel.id),
            )


async def update_gm_fts(db, msg):
    """No-op placeholder; Postgres uses generated tsvector columns."""
    del db, msg
    return


async def add_to_repost_queue(db, msg):
    del db
    ts = int(msg.created_at.timestamp() * 1000)
    await execute_with_retry(
        None,
        """
        INSERT INTO bot_metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """,
        (f"repost_queue_{msg.id}", str(msg.channel.id), ts),
    )


async def get_messages_ready_to_repost(db):
    del db
    from .config import REPOST_DELAY_SECONDS

    cutoff = int((time.time() - REPOST_DELAY_SECONDS) * 1000)
    rows = await fetchall(
        None,
        """
        SELECT key, value, updated_at
        FROM bot_metadata
        WHERE key LIKE 'repost_queue_%'
          AND updated_at <= ?
        ORDER BY updated_at
        LIMIT 10
        """,
        (cutoff,),
    )

    messages = []
    for row in rows:
        key, chan_id, _ = row
        msg_id = key.replace("repost_queue_", "")
        msg_row = await fetchone(
            None,
            """
            SELECT p.post_id, p.chan_id, p.author_id, p.created_ts, p.content
            FROM posts p
            JOIN members m ON p.author_id = m.member_id
            WHERE p.post_id = ?
              AND NOT """ + _is_true_expr("p.deleted") + """
              AND """ + _is_true_expr("m.is_gm") + """
            """,
            (msg_id,),
        )
        if msg_row:
            messages.append(
                (
                    msg_id,
                    chan_id,
                    msg_row["author_id"],
                    msg_row["created_ts"],
                    msg_row["content"],
                )
            )
    return messages


async def mark_message_as_reposted(db, msg_id):
    del db
    await execute_with_retry(
        None,
        "DELETE FROM bot_metadata WHERE key = ?",
        (f"repost_queue_{msg_id}",),
    )


async def mark_message_as_deleted(db, msg_id):
    del db
    await execute_with_retry(None, "UPDATE posts SET deleted = '1' WHERE post_id = ?", (str(msg_id),))
    await mark_message_as_reposted(None, msg_id)


async def get_gm_display_name(db, author_id, fallback_name):
    del db
    row = await fetchone(None, "SELECT gm_name FROM gm_names WHERE author_id = ?", (str(author_id),))
    return row["gm_name"] if row else fallback_name


def _is_true_expr(column: str) -> str:
    return f"COALESCE(({column})::text, '0') IN ('1','t','true')"


async def seed_gm_data(db):
    del db
    for author_id in SEED_BLUE_IDS:
        await execute_with_retry(
            None,
            "INSERT INTO members (member_id, is_gm) VALUES (?, '1') "
            "ON CONFLICT (member_id) DO UPDATE SET is_gm = '1'",
            (str(author_id),),
        )
    for author_id, gm_name in GM_NAME_OVERRIDES.items():
        await execute_with_retry(
            None,
            """
            INSERT INTO gm_names (author_id, gm_name)
            VALUES (?, ?)
            ON CONFLICT (author_id) DO UPDATE SET gm_name = EXCLUDED.gm_name, updated_at = (extract(epoch FROM now()) * 1000)::bigint
            """,
            (str(author_id), gm_name),
        )


async def verify_gm_seeding(db):
    del db
    rows = await fetchall(
        None,
        "SELECT member_id FROM members "
        "WHERE member_id = ANY($1::text[]) "
        f"AND {_is_true_expr('is_gm')}",
        (list(str(i) for i in SEED_BLUE_IDS),),
    )
    found_ids = {row["member_id"] for row in rows}
    missing = set(str(i) for i in SEED_BLUE_IDS) - found_ids
    if missing:
        print(f"[DB] Warning: Missing GM flags for {len(missing)} IDs")
    return not missing


async def reseed_gm_data_if_needed(db):
    del db
    cursor = await fetchone(None, f"SELECT COUNT(*) AS count FROM members WHERE {_is_true_expr('is_gm')}")
    gm_count = cursor["count"] if cursor else 0
    if gm_count < len(SEED_BLUE_IDS):
        print("[DB] GM count mismatch; reseeding")
        await seed_gm_data(None)


async def check_gm_data_integrity(db):
    del db
    print("[DB] Checking GM data integrity...")
    rows = await fetchall(
        None,
        f"""
        SELECT member_id FROM members
        WHERE member_id = ANY($1::text[])
          AND NOT {_is_true_expr('is_gm')}
        """,
        (list(str(i) for i in SEED_BLUE_IDS),),
    )
    if rows:
        print(f"[DB] Warning: {len(rows)} GM IDs missing flags")
        return False
    print("[DB] GM data integrity OK")
    return True

async def refresh_90day_view(db):
    """
    Refresh the materialized view for the 90-day default view.
    This should be called every 10 minutes to keep the default view fast.
    """
    del db
    try:
        print("[DB] Refreshing 90-day materialized view...")
        start_time = __import__('time').time()

        await execute_with_retry(
            None,
            "REFRESH MATERIALIZED VIEW CONCURRENTLY gm_posts_90day",
            ()
        )

        elapsed = __import__('time').time() - start_time
        print(f"[DB] 90-day view refreshed in {elapsed:.2f}s")
        return True
    except Exception as e:
        print(f"[DB] Failed to refresh 90-day view: {e}")
        return False
