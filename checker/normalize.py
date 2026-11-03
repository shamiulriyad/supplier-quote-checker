"""Turn the many ways a document writes a value into one form. Never guesses: unclear input -> None + reason."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

CURRENCY = r"(?:BDT|Tk\.?|TK|Taka|৳)"
# 1,234,567.89 | 12,34,567.89 (lakh grouping, common in Bangladesh) | 1234567.89 | 472
NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?|\d+(?:\.\d+)?"
MONEY = rf"(?:{CURRENCY}\s*)?(?P<num>{NUM})(?:\s*{CURRENCY}|/-)?"

UNITS = {
    "metre": "metre", "metres": "metre", "meter": "metre", "meters": "metre", "mtr": "metre", "mtrs": "metre",
    "m": "metre", "yard": "yard", "yards": "yard",
    "piece": "piece", "pieces": "piece", "pcs": "piece", "pc": "piece", "nos": "piece",
    "cone": "cone", "cones": "cone", "box": "box", "boxes": "box", "carton": "carton", "cartons": "carton",
    "kg": "kg", "kgs": "kg", "roll": "roll", "rolls": "roll", "set": "set", "sets": "set", "pair": "pair",
    "pairs": "pair", "dozen": "dozen", "unit": "unit", "units": "unit",
}

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = (r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"
        r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}")

# a token that looks like a number but has letters in it, e.g. "47O.00" (letter O) or "4,7l0"
MALFORMED_NUMBER = re.compile(r"^(?:BDT|Tk\.?)?\d[\d,.]*[OoIl][\d,.OoIl]*$")


def to_decimal(text: str) -> Decimal | None:
    m = re.fullmatch(rf"\s*{MONEY}\s*", text or "")
    if not m:
        return None
    try:
        return Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None


def unit_name(word: str | None) -> str | None:
    return UNITS.get((word or "").strip().strip(".").lower())


def parse_date(text: str) -> tuple[str | None, str | None]:
    """(ISO date, problem). Day/month order is only assumed when it is unambiguous."""
    t = (text or "").strip().rstrip(".,")
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat(), None
        if m := re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", t):
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            y = y + 2000 if y < 100 else y
            if a > 12 >= b:
                return date(y, b, a).isoformat(), None  # day first
            if b > 12 >= a:
                return date(y, a, b).isoformat(), None  # month first
            if a == b:
                return date(y, a, b).isoformat(), None
            return None, f"'{t}' could be {a:02d}/{b:02d} or {b:02d}/{a:02d} (day and month order unclear)"
        if m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", t):
            return date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1])).isoformat(), None
        if m := re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t):
            return date(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2])).isoformat(), None
    except (KeyError, ValueError):
        return None, f"'{t}' is not a valid date"
    return None, f"'{t}' is not a recognised date"
