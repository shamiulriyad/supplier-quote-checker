"""Small database interface used by the history store: PostgreSQL (Supabase) or SQLite (local / tests).

SQL is written once with `?` placeholders; the PostgreSQL dialect converts them to `%s`. Strings are
sent untyped, so PostgreSQL casts them to uuid / jsonb / timestamptz from the column. Rows come back as
plain dicts with timestamps as naive local ISO strings, so callers never see dialect differences.
"""
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"


class DatabaseUnavailable(Exception):
    """The database could not be reached after retrying."""


def now_iso() -> str:
    """Current time with UTC offset: correct for timestamptz, and sortable as text in SQLite."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def as_json(value: Any) -> Any:
    """jsonb comes back parsed from PostgreSQL, as text from SQLite."""
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def to_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, ensure_ascii=False)


_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _local_naive(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.isoformat(timespec="seconds")


def _normalize(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            v = _local_naive(v)
        elif isinstance(v, date):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, uuid.UUID):
            v = str(v)
        elif isinstance(v, str) and (k.endswith("_at") or k == "ts") and _TS.match(v) and len(v) > 19:
            v = _local_naive(datetime.fromisoformat(v))  # SQLite text with an offset
        out[k] = v
    return out


class Tx:
    """A transaction: every statement commits together or not at all."""

    def __init__(self, cursor, dialect: "Database"):
        self._cur = cursor
        self._db = dialect

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        self._cur.execute(self._db.sql(sql), tuple(params))
        return self._cur.rowcount

    def executemany(self, sql: str, rows: list) -> None:
        if rows:
            self._cur.executemany(self._db.sql(sql), [tuple(r) for r in rows])

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        self._cur.execute(self._db.sql(sql), tuple(params))
        return [_normalize(dict(r)) for r in self._cur.fetchall()]

    def insert_many(self, prefix: str, rows: list, suffix: str = "", chunk: int = 200) -> None:
        """INSERT ... VALUES (...), (...), ... in one statement: one round trip instead of one per row."""
        rows = [tuple(r) for r in rows]
        if not rows:
            return
        one = "(" + ", ".join(["?"] * len(rows[0])) + ")"
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            sql = f"{prefix} VALUES {', '.join([one] * len(part))} {suffix}".strip()
            self._cur.execute(self._db.sql(sql), [v for r in part for v in r])


class Database:
    name = "base"
    retries = 3

    def sql(self, statement: str) -> str:
        return statement

    @contextmanager
    def _cursor(self) -> Iterator:
        raise NotImplementedError

    def _is_transient(self, exc: Exception) -> bool:
        return False

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self._cursor() as cur:
            yield Tx(cur, self)

    def run(self, fn, attempts: int | None = None):
        """Run fn(tx) in a transaction, retrying the whole transaction on transient connection errors.

        Statements that may run twice must be idempotent (deterministic ids + ON CONFLICT).
        """
        attempts = attempts or self.retries
        for n in range(1, attempts + 1):
            try:
                with self.transaction() as tx:
                    return fn(tx)
            except Exception as exc:
                if not self._is_transient(exc) or n == attempts:
                    if self._is_transient(exc):
                        raise DatabaseUnavailable(f"{self.name} database unavailable: {exc}") from exc
                    raise
                time.sleep(0.4 * 2 ** (n - 1))

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        return self.run(lambda tx: tx.query(sql, params))

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        return self.run(lambda tx: tx.execute(sql, params))

    def check(self) -> tuple[bool, str]:
        try:
            self.query("SELECT 1 AS ok")
        except Exception as exc:
            return False, f"Cannot reach the {self.name} database: {type(exc).__name__}"
        return True, f"{self.name} database reachable"

    def close(self) -> None:
        pass


class SqliteDatabase(Database):
    name = "SQLite"

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._apply_schema()

    @contextmanager
    def _cursor(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _is_transient(self, exc: Exception) -> bool:
        return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)

    def _apply_schema(self) -> None:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.executescript(SQLITE_SCHEMA)
            conn.commit()
        finally:
            conn.close()


class PostgresDatabase(Database):
    name = "Supabase PostgreSQL"

    def __init__(self, url: str, min_size: int = 1, max_size: int = 5):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self._psycopg = psycopg
        # prepare_threshold=None: no server-side prepared statements, so the Supabase pooler works.
        # No per-checkout health check (it costs a round trip per transaction); a dropped connection raises
        # OperationalError, which run() retries on a fresh connection.
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=False, max_idle=60,
                                   kwargs={"autocommit": False, "prepare_threshold": None, "row_factory": dict_row})
        try:
            self.pool.open(wait=True, timeout=20)
        except Exception as exc:
            self.pool.close()
            raise DatabaseUnavailable(f"Cannot connect to Supabase PostgreSQL: {exc}") from exc
        self.apply_migrations()

    def sql(self, statement: str) -> str:
        return statement.replace("?", "%s")

    @contextmanager
    def _cursor(self):
        with self.pool.connection() as conn:  # commits on success, rolls back on error
            with conn.cursor() as cur:
                yield cur

    def _is_transient(self, exc: Exception) -> bool:
        from psycopg_pool import PoolTimeout

        return isinstance(exc, (self._psycopg.OperationalError, PoolTimeout))

    def apply_migrations(self) -> None:
        """Apply the application schema (idempotent). The storage-bucket SQL is not needed here."""
        path = MIGRATIONS / "20261103000000_initial_schema.sql"
        with self.pool.connection() as conn:
            conn.execute(path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.pool.close()


# The same tables as supabase/migrations/20261103000000_initial_schema.sql, in SQLite types.
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    total INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    position INTEGER NOT NULL, file_hash TEXT, original_filename TEXT NOT NULL, storage_path TEXT,
    size_bytes INTEGER, supplier TEXT, item TEXT, decision TEXT, extraction_source TEXT,
    extracted_text TEXT, result_json TEXT, processing_status TEXT NOT NULL DEFAULT 'queued',
    failed_stage TEXT, error TEXT, seconds REAL, thread_id TEXT, human_decision TEXT, human_comment TEXT,
    reviewed_at TEXT, processed_at TEXT, legacy_id TEXT UNIQUE, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, UNIQUE (batch_id, file_hash), UNIQUE (batch_id, position)
);
CREATE INDEX IF NOT EXISTS documents_batch_idx ON documents (batch_id);
CREATE INDEX IF NOT EXISTS documents_status_idx ON documents (processing_status);
CREATE INDEX IF NOT EXISTS documents_hash_idx ON documents (file_hash);
CREATE TABLE IF NOT EXISTS document_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    document_id TEXT REFERENCES documents(id) ON DELETE CASCADE, event_type TEXT NOT NULL,
    event_data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS document_events_doc_idx ON document_events (document_id, id);
CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL, status TEXT NOT NULL, checkpoint_data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (document_id, step_name)
);
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY, model TEXT, prompt_version TEXT, document TEXT, response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
