import csv
from pathlib import Path

import pytest

from checker import extract_llm
from checker.config import load_config
from checker.extract_regex import extract_regex
from checker.models import FIELD_KINDS, ExtractedPack, Field
from checker.parse import read_pdf_text

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def expected_rows() -> list[dict]:
    with open(ROOT / "tests" / "expected.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def pack_text(n: str) -> str:
    return read_pdf_text(DATA / f"supplier-pack-{n}.pdf")


def llm_answer_from(ex: ExtractedPack) -> dict:
    """Build an LLM-style answer (strings) from an ExtractedPack: a stand-in for OpenAI in tests."""
    out = {}
    for name, kind in FIELD_KINDS.items():
        f = ex.get(name)
        if f.value is None:
            value = None
        elif kind == "bool":
            value = "yes" if f.value else "no"
        elif kind == "days":
            value = f"{f.value} days"
        else:
            value = str(f.value)
        out[name] = {"value": value, "quote": f.quote, "status": f.status, "note": f.note}
    return out


@pytest.fixture(autouse=True)
def no_real_supabase(monkeypatch):
    """Unit tests never touch a real Supabase project, even if .env configures one.

    Empty values (not deleted) so checker.env.load_dotenv, which only fills missing names, cannot set them.
    Integration tests use the separate SUPABASE_TEST_* variables.
    """
    for name in ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_DB_URL", "SUPABASE_STORAGE_BUCKET"):
        monkeypatch.setenv(name, "")
    # Streamlit copies .streamlit/secrets.toml into os.environ (overwriting the empty values above),
    # so point AppTest-run apps at a secrets file that does not exist.
    from streamlit import config as st_config
    from streamlit.runtime.secrets import secrets_singleton

    original = st_config.get_option("secrets.files")
    st_config.set_option("secrets.files", [str(ROOT / "tests" / "no-secrets-for-tests.toml")])
    secrets_singleton._reset()
    yield
    st_config.set_option("secrets.files", original)
    secrets_singleton._reset()


@pytest.fixture(autouse=True)
def isolated_recordings(tmp_path, monkeypatch):
    """No test reads the developer's local recordings (data/llm-replay/, cache/llm/): those are git-ignored
    and absent from a fresh clone. Tests that need recordings create them in their own temp folder."""
    monkeypatch.setattr(extract_llm, "FIXTURES_DIR", tmp_path / "no-fixtures")
    extract_llm.set_cache_backend(extract_llm.FileCache(tmp_path / "recordings"))
    yield
    extract_llm.set_cache_backend(None)


def record(text: str, name: str = "test.pdf") -> None:
    """Save a recorded OpenAI-style response for this exact text (what a live run would have saved)."""
    extract_llm.save_cached("unused", text, llm_answer_from(extract_regex(text)), "gpt-test", name)


@pytest.fixture
def rules():
    return load_config()["rules"]


@pytest.fixture
def base_pack() -> ExtractedPack:
    """Pack 01 (clean, Approve) as extracted by the regex extractor."""
    return extract_regex(pack_text("01"))


def with_fields(ex: ExtractedPack, **values) -> ExtractedPack:
    fields = dict(ex.fields)
    for name, value in values.items():
        fields[name] = value if isinstance(value, Field) else Field(value=value, quote=f"test {name}", status="FOUND")
    return ExtractedPack(fields=fields)


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the OpenAI call. Pass a function text -> answer dict."""
    def install(answer_fn):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(extract_llm, "call_openai", lambda text, cfg: answer_fn(text))
        monkeypatch.setattr(extract_llm, "save_cached", lambda *a, **k: None)
    return install
