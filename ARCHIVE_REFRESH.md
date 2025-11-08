## Discord Archive ➜ Postgres Refresh

This is the full, repeatable procedure we used to move a fresh Discord archive
from SQLite into the Postgres schema that the asyncpg bot/viewer expect.

> **Paths / Services**
>
> - SQLite dump: `/opt/discord-archiver/discord_archive.db`
> - Postgres: `postgresql://archiver:8z9jJK9LqMK2p@127.0.0.1/discord_archiver`
> - Bot code: `/opt/discord-archiver/bot`
> - Viewer code: `/opt/discord-archiver/source`

---

### 0. Prerequisites

1. Copy the latest `discord_archive.db` into `/opt/discord-archiver/`.
2. Ensure `pgloader` is installed (`sudo apt-get install pgloader`).
3. Be ready to stop the bot + viewer during the import.

---

### 1. SQLite Hygiene

All commands run on `vmi2897338`.

#### 1.1 UTF‑8 cleanup

```bash
cat <<'PY' > /tmp/utf_fix.py
import sqlite3

DB_PATH = '/opt/discord-archiver/discord_archive.db'
TARGETS = {
    'posts': ['content'],
    'post_revisions': ['content'],
    'attachments': ['filename', 'url', 'content_type'],
    'embeds': ['data_json'],
    'channels': ['name', 'topic'],
    'members': ['username', 'display_name', 'avatar_url'],
    'gm_names': ['gm_name', 'notes'],
    'bot_metadata': ['key', 'value'],
}

conn = sqlite3.connect(DB_PATH)
conn.text_factory = bytes

for table, columns in TARGETS.items():
    for col in columns:
        updated = 0
        cur = conn.execute(f"SELECT rowid, {col} FROM {table}")
        for rowid, value in cur.fetchall():
            if value is None:
                continue
            try:
                value.decode('utf-8')
            except UnicodeDecodeError:
                fixed = value.decode('utf-8', errors='replace')
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", (fixed, rowid))
                updated += 1
        if updated:
            print(f"[{table}.{col}] fixed {updated} rows")

conn.commit()
conn.close()
print('UTF-8 cleanup complete')
PY

python3 /tmp/utf_fix.py
rm /tmp/utf_fix.py
```

#### 1.2 Placeholder channels + attachment pruning

```bash
sqlite3 /opt/discord-archiver/discord_archive.db <<'SQL'
INSERT INTO channels (chan_id, name)
SELECT DISTINCT pr.chan_id, '[missing]'
FROM post_revisions pr
LEFT JOIN channels c ON c.chan_id = pr.chan_id
WHERE c.chan_id IS NULL;

DELETE FROM attachments
WHERE post_id NOT IN (SELECT post_id FROM posts);
SQL
```

Optional checks:

```sql
SELECT chan_id FROM post_revisions
WHERE chan_id NOT IN (SELECT chan_id FROM channels);
```

---

### 2. pgloader Import

1. Stop bot/viewer (avoid writes during the load).
2. Loader file `~/load_sqlite_to_pg.load`:

```lisp
LOAD DATABASE
    FROM sqlite:///opt/discord-archiver/discord_archive.db
    INTO postgresql://archiver:8z9jJK9LqMK2p@127.0.0.1/discord_archiver

 EXCLUDING TABLE NAMES LIKE 'gm_posts_fts%'
 EXCLUDING TABLE NAMES LIKE 'posts_fts%'

    WITH include drop, create tables, create indexes, reset sequences,
         workers = 8, concurrency = 1,
         batch rows = 5000, prefetch rows = 5000

    SET maintenance_work_mem to '1GB',
        work_mem = '64MB',
        search_path = 'public';
```

3. Run:

```bash
pgloader ~/load_sqlite_to_pg.load
```

Typical runtime: ~20 minutes.

---

### 3. Postgres Normalization (`psql -U archiver -d discord_archiver`)

#### 3.1 Boolean conversions

```sql
ALTER TABLE members
  ALTER COLUMN is_gm DROP DEFAULT,
  ALTER COLUMN is_gm TYPE boolean
    USING CASE WHEN is_gm::text IN ('1','t','true') THEN TRUE ELSE FALSE END,
  ALTER COLUMN is_gm SET DEFAULT FALSE;

ALTER TABLE posts
  ALTER COLUMN deleted DROP DEFAULT,
  ALTER COLUMN deleted TYPE boolean
    USING CASE WHEN deleted::text IN ('1','t','true') THEN TRUE ELSE FALSE END,
  ALTER COLUMN deleted SET DEFAULT FALSE;

ALTER TABLE channels
  ALTER COLUMN accessible DROP DEFAULT,
  ALTER COLUMN accessible TYPE boolean
    USING CASE WHEN accessible::text IN ('1','t','true') THEN TRUE ELSE FALSE END,
  ALTER COLUMN accessible SET DEFAULT TRUE;

ALTER TABLE post_revisions
  ALTER COLUMN is_edit DROP DEFAULT,
  ALTER COLUMN is_edit TYPE boolean
    USING CASE WHEN is_edit::text IN ('1','t','true') THEN TRUE ELSE FALSE END,
  ALTER COLUMN is_edit SET DEFAULT FALSE;
```

Repeat the pattern for any other flag columns that imported as text/int.

#### 3.2 Timestamp columns (example)

```sql
ALTER TABLE crawl_progress
  ALTER COLUMN updated_at TYPE bigint USING updated_at::bigint;
```

Verify other timestamp fields (`channels.created_ts`, etc.) are `bigint` and convert if not.

#### 3.3 Rebuild gm_posts + TSV index

```sql
ALTER TABLE posts
  ADD COLUMN IF NOT EXISTS content_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_posts_content_tsv
  ON posts USING gin (content_tsv);

DROP MATERIALIZED VIEW IF EXISTS gm_posts;
CREATE MATERIALIZED VIEW gm_posts AS
SELECT p.*
FROM posts p
JOIN members m ON m.member_id = p.author_id
WHERE m.is_gm = TRUE
  AND COALESCE(p.deleted, FALSE) = FALSE;

CREATE UNIQUE INDEX idx_gm_posts_id ON gm_posts (post_id);
CREATE INDEX idx_gm_posts_created_ts ON gm_posts (created_ts DESC);
CREATE INDEX idx_gm_posts_tsv ON gm_posts USING gin (content_tsv);

REFRESH MATERIALIZED VIEW gm_posts;
```

#### 3.4 Vacuum

```sql
VACUUM ANALYZE;
\q
```

---

### 4. Restart Services

```bash
PYTHONPATH=/opt/discord-archiver/bot /opt/discord-archiver/bot/.venv/bin/python -m archiver.main
sudo systemctl restart discord-viewer.service
```

Watch the bot logs for the GM reseed message and confirm the viewer API responds.

---

### 5. Verification (optional)

```bash
psql -U archiver -d discord_archiver -c "SELECT COUNT(*) FROM posts;"
psql -U archiver -d discord_archiver -c "SELECT COUNT(*) FROM gm_posts;"
psql -U archiver -d discord_archiver -c "\d+ members"
```

Check that `members.is_gm` is `boolean`, `channels.guild_id` exists/populated, etc.

---

### 6. Optional Safety Net

```bash
pg_dump -Fc discord_archiver > ~/discord_archiver_postload.dump
```

Keep or remove `~/discord-archiver` and `~/discord_archiver.tgz` as desired; they’re no longer used by the running services.

