"""Step 1: PDF -> plain text, plus small text helpers shared by the other steps."""
import re
from pathlib import Path

import pdfplumber


class DocumentError(Exception):
    """The document could not be read."""


def read_pdf_text(path: str | Path) -> str:
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:  # pdfplumber raises many different types on bad files
        raise DocumentError(f"Could not open PDF: {exc}") from exc
    if not text.strip():
        raise DocumentError("PDF has no readable text (it may be a scanned image).")
    return text


def normalize(text: str) -> str:
    """Lower-case, unify quotes/dashes and collapse whitespace, for quote matching."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in_text(quote: str | None, text: str) -> bool:
    return bool(quote) and normalize(quote) in normalize(text)


def split_clauses(text: str) -> dict[str, str]:
    """Numbered terms after 'TERMS AND CONDITIONS' -> {'payment': '10% advance ...', ...}.

    Clause bodies are joined onto one line so a quote that spans a line break still
    matches the document after normalize().
    """
    start = text.upper().find("TERMS AND CONDITIONS")
    if start == -1:
        return {}
    body = text[start:]
    heads = list(re.finditer(r"^\s*(\d+)\.\s+([A-Za-z][A-Za-z ]{1,30}?)\.\s", body, re.M))
    clauses: dict[str, str] = {}
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        clause_text = body[head.end():end]
        # stop at the signature block
        clause_text = re.split(r"\n\s*For\s+\S", clause_text)[0]
        clauses[head.group(2).strip().lower()] = re.sub(r"\s+", " ", clause_text).strip()
    return clauses


def sentence_at(text: str, start: int, end: int | None = None) -> str:
    """The sentence(s) of `text` covering the span start..end."""
    end = start if end is None else end
    left = max(text.rfind(". ", 0, start), -1)
    right = text.find(". ", end)
    return text[left + 2 if left >= 0 else 0: right + 1 if right >= 0 else len(text)].strip()
