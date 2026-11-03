"""One-time Supabase setup and check (the app also does the first three steps on start).

    python tools/supabase_setup.py

1. Applies supabase/migrations/*.sql (tables, indexes, RLS, private bucket) through SUPABASE_DB_URL.
2. Creates LangGraph's checkpoint tables (PostgresSaver.setup()).
3. Confirms the Storage bucket exists and is private.
4. Uploads, downloads and deletes a small test object.
Reads credentials from the environment or .env. Prints no secrets.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from checker.env import load_dotenv
    from service.backend import build_checkpointer, resolve_settings
    from service.files import SupabaseFileStore, storage_path_for

    load_dotenv()
    settings = resolve_settings()
    if settings.kind != "supabase":
        print("SUPABASE_URL, SUPABASE_KEY and SUPABASE_DB_URL are not set. Nothing to do.")
        return 1

    import psycopg

    print("1. Applying SQL migrations…")
    with psycopg.connect(settings.db_url, autocommit=True, prepare_threshold=None) as conn:
        for sql in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
            conn.execute(sql.read_text(encoding="utf-8"))
            print(f"   ok  {sql.name}")

    print("2. LangGraph checkpoint tables…")
    _saver, close = build_checkpointer(settings, ROOT / "runtime")
    close()
    print("   ok")

    print(f"3. Storage bucket '{settings.bucket}'…")
    files = SupabaseFileStore(settings.url, settings.key, settings.bucket)
    files.ensure_bucket()
    ok, msg = files.check()
    print(f"   {'ok' if ok else 'FAILED'}  {msg}")
    if not ok:
        return 1

    print("4. Upload / download / delete test…")
    data = b"%PDF-1.4\n% supplier-quote-checker storage self-test\n"
    import hashlib

    path = storage_path_for(hashlib.sha256(data).hexdigest())
    files.put(path, data, "self-test.pdf")
    assert files.get(path) == data, "downloaded bytes differ"
    assert not files.delete([path]), "could not delete the test object"
    print("   ok")
    print("Supabase is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
