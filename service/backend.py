"""Choose and build the storage backend: Supabase (PostgreSQL + Storage) or local (SQLite + folder).

Supabase is used when SUPABASE_URL, SUPABASE_KEY and SUPABASE_DB_URL are all set. If only some are set,
the app refuses to start instead of quietly falling back to local storage.
"""
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from checker import extract_llm
from checker.graph import checkpoint_serde

from .db import Database, PostgresDatabase, SqliteDatabase
from .files import FileStore, LocalFileStore, SupabaseFileStore

REQUIRED = ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_DB_URL")
DEFAULT_BUCKET = "supplier-documents"


class BackendConfigError(Exception):
    """Supabase is partly configured, or cannot be reached."""


@dataclass
class BackendSettings:
    kind: str  # "supabase" | "local"
    url: str | None = None
    key: str | None = None
    db_url: str | None = None
    bucket: str = DEFAULT_BUCKET

    @property
    def label(self) -> str:
        return "Supabase (PostgreSQL + Storage)" if self.kind == "supabase" else "Local (SQLite + folder)"


def resolve_settings(env: dict | None = None) -> BackendSettings:
    env = os.environ if env is None else env
    present = [k for k in REQUIRED if env.get(k)]
    if not present:
        return BackendSettings("local")
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise BackendConfigError("Supabase is only partly configured. Missing: " + ", ".join(missing)
                                 + ". Set all of SUPABASE_URL, SUPABASE_KEY and SUPABASE_DB_URL, or none of them.")
    return BackendSettings("supabase", env["SUPABASE_URL"], env["SUPABASE_KEY"], env["SUPABASE_DB_URL"],
                           env.get("SUPABASE_STORAGE_BUCKET") or DEFAULT_BUCKET)


def build_database(settings: BackendSettings, runtime: Path) -> Database:
    if settings.kind == "supabase":
        return PostgresDatabase(settings.db_url)
    return SqliteDatabase(runtime / "app.db")


def build_files(settings: BackendSettings, runtime: Path) -> FileStore:
    if settings.kind == "supabase":
        store = SupabaseFileStore(settings.url, settings.key, settings.bucket)
        store.ensure_bucket()
        return store
    return LocalFileStore(runtime / "files")


def build_checkpointer(settings: BackendSettings, runtime: Path):
    """LangGraph checkpointer: PostgresSaver on Supabase, SqliteSaver locally. Returns (saver, closer)."""
    if settings.kind == "supabase":
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # Health-checked on checkout: LangGraph has no retry of its own, and a dead connection here would
        # fail the document. max_idle closes idle connections before the server drops them.
        pool = ConnectionPool(settings.db_url, min_size=1, max_size=4, open=True, max_idle=60,
                              kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
                              check=ConnectionPool.check_connection)
        saver = PostgresSaver(pool, serde=checkpoint_serde())
        saver.setup()  # creates LangGraph's checkpoint tables if needed (idempotent)
        return saver, pool.close
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(runtime / "graph-checkpoints.db", check_same_thread=False)
    return SqliteSaver(conn, serde=checkpoint_serde()), conn.close


class DbLLMCache:
    """extract_llm cache backend on the llm_cache table (survives Streamlit Cloud restarts)."""

    def __init__(self, store):
        self.store = store

    def get(self, key):
        return self.store.cache_get(key)

    def put(self, key, response, model, document):
        self.store.cache_put(key, response, model, document, extract_llm.PROMPT_VERSION)

    def delete(self, keys):
        return self.store.cache_delete(list(keys))
