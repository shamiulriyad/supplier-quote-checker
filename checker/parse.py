"""Step 1: PDF -> plain text, plus small text helpers shared by the other steps."""
import re
from pathlib import Path

import pdfplumber


class DocumentError(Exception):
    """The document could not be read."""


def read_pdf_pages(path: str | Path) -> list[str]:
    """Text of every page, in order (page 1 first)."""
    try:
        with pdfplumber.open(path) as pdf:
            pages = [(page.extract_text() or "") for page in pdf.pages]
    except Exception as exc:  # pdfplumber raises many different types on bad files
        raise DocumentError(f"Could not open PDF: {exc}") from exc
    if not "".join(pages).strip():
        raise DocumentError("PDF has no readable text (it may be a scanned image).")
    return pages


def read_pdf_text(path: str | Path) -> str:
    return "\n".join(read_pdf_pages(path))


def locate_page(quote: str | None, pages: list[str] | None) -> int | None:
    """1-based page whose text contains the quote; None without page texts or if it is on no single page."""
    if not quote or not pages:
        return None
    q = normalize(quote)
    return next((n for n, text in enumerate(pages, 1) if q in normalize(text)), None)


def normalize(text: str) -> str:
    """Lower-case, unify quotes/dashes and collapse whitespace, for quote matching."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_in_text(quote: str | None, text: str) -> bool:
    return bool(quote) and normalize(quote) in normalize(text)


# Clause titles that mean the same thing -> one key used by the extractor.
CLAUSE_NAMES = {
    "payment": ("payment", "payment terms", "terms of payment", "payment term"),
    "delivery": ("delivery", "delivery terms", "delivery period", "delivery time", "lead time", "shipment"),
    "delay": ("delay", "late delivery", "penalty", "late delivery penalty", "liquidated damages", "late penalty"),
    "validity": ("validity", "offer validity", "quotation validity", "validity of offer"),
}
_HEADINGS = "|".join(sorted({n for names in CLAUSE_NAMES.values() for n in names}, key=len, reverse=True))


def canonical_clause(title: str) -> str:
    t = re.sub(r"\s+", " ", title.strip().lower())
    return next((key for key, names in CLAUSE_NAMES.items() if t in names), t)


def split_clauses(text: str) -> dict[str, str]:
    """Contract terms -> {'payment': '10% advance ...', 'delivery': ..., 'delay': ..., 'validity': ..., ...}.

    Reads numbered clauses after 'TERMS AND CONDITIONS' ("1. Payment. ...") and labelled terms anywhere
    in the document ("Payment terms: ...", "Lead time - 3 weeks ..."). Numbered clauses win when both exist.
    Clause bodies are joined onto one line so a quote that spans a line break still matches the document
    after normalize().
    """
    clauses: dict[str, str] = {}
    start = text.upper().find("TERMS AND CONDITIONS")
    if start != -1:
        body = text[start:]
        heads = list(re.finditer(r"^\s*(\d+)\.\s+([A-Za-z][A-Za-z ]{1,30}?)\.\s", body, re.M))
        for i, head in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            clause_text = re.split(r"\n\s*For\s+\S", body[head.end():end])[0]  # stop at the signature block
            clauses.setdefault(canonical_clause(head.group(2)), re.sub(r"\s+", " ", clause_text).strip())

    labelled = list(re.finditer(rf"^[ \t•*\-]*(?:\d+[.)]\s*)?({_HEADINGS})\s*[:\-–]\s*(\S.*)$", text, re.I | re.M))
    for i, m in enumerate(labelled):
        key = canonical_clause(m.group(1))
        if key in clauses:
            continue
        end = labelled[i + 1].start() if i + 1 < len(labelled) else len(text)
        # the value line plus at most two continuation lines, never past a blank line or the signature
        follow = text[m.end():end].split("\n")[1:3]
        parts = [m.group(0).strip(" \t•*-")]  # keep the heading: "Validity: 30 days" is the evidence
        for line in follow:
            if not line.strip() or re.match(r"\s*(For\s+\S|Authori[sz]ed|Signature|[A-Z][A-Za-z ]{2,30}:)", line):
                break
            parts.append(line)
        clauses[key] = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return clauses


def sentence_at(text: str, start: int, end: int | None = None) -> str:
    """The sentence(s) of `text` covering the span start..end."""
    end = start if end is None else end
    left = max(text.rfind(". ", 0, start), -1)
    right = text.find(". ", end)
    return text[left + 2 if left >= 0 else 0: right + 1 if right >= 0 else len(text)].strip()
