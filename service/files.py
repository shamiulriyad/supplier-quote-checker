"""Permanent PDF storage: a private Supabase Storage bucket, or a local folder when Supabase is not configured.

Objects are addressed by content: `pdfs/<sha256[:2]>/<sha256>.pdf`, so the same PDF is stored once.
Nothing here ever creates a public URL; files are read server-side with the service key.
"""
import re
import time
from pathlib import Path

STORAGE_PREFIX = "pdfs/"
_PATH = re.compile(r"^pdfs/[0-9a-f]{2}/[0-9a-f]{64}\.pdf$")


class StorageUnavailable(Exception):
    """Storage could not be reached, or an operation kept failing after retries."""


def storage_path_for(file_hash: str) -> str:
    return f"{STORAGE_PREFIX}{file_hash[:2]}/{file_hash}.pdf"


def is_managed_path(path: str | None) -> bool:
    """Only paths this app created may ever be deleted."""
    return bool(path) and bool(_PATH.match(path))


def retry(fn, attempts: int = 3, base_delay: float = 0.5, transient=(Exception,), what: str = "storage"):
    for n in range(1, attempts + 1):
        try:
            return fn()
        except transient as exc:
            if n == attempts:
                raise StorageUnavailable(f"{what} failed after {attempts} attempts: {exc}") from exc
            time.sleep(base_delay * 2 ** (n - 1))


class FileStore:
    name = "base"
    persistent = False

    def put(self, path: str, data: bytes, original_filename: str | None = None) -> bool:
        """Store the file. Returns True if it was newly created, False if it already existed."""
        raise NotImplementedError

    def get(self, path: str) -> bytes:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        raise NotImplementedError

    def delete(self, paths: list[str]) -> list[str]:
        """Delete managed paths. Returns the paths that could NOT be deleted."""
        raise NotImplementedError

    def check(self) -> tuple[bool, str]:
        raise NotImplementedError


class LocalFileStore(FileStore):
    name = "Local folder"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, path: str) -> Path:
        if not is_managed_path(path):
            raise ValueError(f"Not a managed storage path: {path!r}")
        return self.root / path

    def put(self, path, data, original_filename=None):
        target = self._p(path)
        if target.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)  # atomic: a half-written file is never visible
        return True

    def get(self, path):
        return self._p(path).read_bytes()

    def exists(self, path):
        return self._p(path).exists()

    def delete(self, paths):
        failed = []
        for path in paths:
            try:
                self._p(path).unlink(missing_ok=True)
            except Exception:
                failed.append(path)
        return failed

    def check(self):
        return True, f"Local folder {self.root} (not persistent on Streamlit Cloud)"


class SupabaseFileStore(FileStore):
    name = "Supabase Storage"
    persistent = True

    def __init__(self, url: str, key: str, bucket: str, client=None):
        if client is None:
            from supabase import create_client

            client = create_client(url, key)
        self.client = client
        self.bucket = bucket

    @property
    def _b(self):
        return self.client.storage.from_(self.bucket)

    def ensure_bucket(self) -> None:
        """Create the private bucket if it does not exist; refuse to use a public one."""
        def get():
            return self.client.storage.get_bucket(self.bucket)
        try:
            bucket = get()
        except Exception:
            retry(lambda: self.client.storage.create_bucket(
                self.bucket, options={"public": False, "file_size_limit": 10 * 1024 * 1024,
                                      "allowed_mime_types": ["application/pdf"]}), what="create bucket")
            bucket = retry(get, what="read bucket")
        if getattr(bucket, "public", False):
            raise StorageUnavailable(f"Bucket '{self.bucket}' is public. Make it private before using it.")

    def put(self, path, data, original_filename=None):
        if not is_managed_path(path):
            raise ValueError(f"Not a managed storage path: {path!r}")
        if retry(lambda: self._b.exists(path), what="storage lookup"):
            return False
        options = {"content-type": "application/pdf", "upsert": "false"}
        if original_filename:
            options["metadata"] = {"original_filename": original_filename}

        def upload():
            try:
                self._b.upload(path, data, file_options=options)
            except Exception as exc:  # someone else uploaded the same file meanwhile: that is fine
                if "exists" in str(exc).lower() or "duplicate" in str(exc).lower() or "409" in str(exc):
                    return False
                raise
            return True
        return retry(upload, what=f"upload of {original_filename or path}")

    def get(self, path):
        return retry(lambda: self._b.download(path), what="download")

    def exists(self, path):
        return retry(lambda: self._b.exists(path), what="storage lookup")

    def delete(self, paths):
        managed = [p for p in paths if is_managed_path(p)]
        failed = [p for p in paths if not is_managed_path(p)]
        for i in range(0, len(managed), 100):  # the API takes batches
            chunk = managed[i:i + 100]
            try:
                retry(lambda: self._b.remove(chunk), what="delete")
            except StorageUnavailable:
                for path in chunk:  # isolate the file(s) that really fail
                    try:
                        retry(lambda: self._b.remove([path]), attempts=2, what="delete")
                    except StorageUnavailable:
                        failed.append(path)
        return failed

    def check(self):
        try:
            bucket = retry(lambda: self.client.storage.get_bucket(self.bucket), attempts=2, what="bucket check")
        except Exception as exc:
            return False, f"Cannot reach bucket '{self.bucket}': {type(exc).__name__}"
        if getattr(bucket, "public", False):
            return False, f"Bucket '{self.bucket}' is PUBLIC: make it private"
        return True, f"Private bucket '{self.bucket}' reachable"
