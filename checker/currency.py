"""Convert quoted amounts to the purchase request's currency (BDT) with the company's fixed rates.

Deterministic: the rate comes from config.yaml, never from the supplier's document or the internet.
The extracted fields keep the original amounts (evidence); the converted copy is what the price rules use.
"""
from decimal import Decimal

from .models import MONEY_FIELDS, ExtractedPack, Field


def _rate(v) -> Decimal:
    d = Decimal(str(v))
    return d if d.as_tuple().exponent <= -2 else d.quantize(Decimal("0.01"))  # 122 -> 122.00


def round_amount(v: Decimal, places: str) -> Decimal:
    """Two decimals when that is exact, otherwise `places` decimals: 575.84, not 575.8400."""
    exact = v.quantize(Decimal("0.01"))
    return exact if exact == v else v.quantize(Decimal(places))


def currency_settings(cfg: dict) -> dict:
    c = cfg.get("currency") or {}
    return {"base": c.get("base", "BDT"), "as_of": str(c.get("rates_as_of", "")),
            "rates": {k.upper(): _rate(v) for k, v in (c.get("rates") or {}).items()}}


def convert_to_base(ex: ExtractedPack, cfg: dict) -> tuple[ExtractedPack, dict]:
    """(pack with amounts in the base currency, conversion details for the report)."""
    s = currency_settings(cfg)
    base, cur = s["base"], ex.get("currency")
    info = {"base": base, "rates_as_of": s["as_of"]}

    if cur.status == "NOT_FOUND":  # no currency written anywhere: the purchase request's currency is assumed
        return ex, {**info, "currency": base, "rate": "1", "assumed": True,
                    "note": f"no currency is written; amounts are taken as {base}"}
    if not cur.ok:
        return _unusable(ex, f"the currency is unclear ({cur.note})"), {**info, "currency": None,
                                                                          "note": f"currency unclear: {cur.note}"}
    code = str(cur.value).upper()
    if code == base:
        return ex, {**info, "currency": base, "rate": "1"}
    rate = s["rates"].get(code)
    if rate is None:
        return (_unusable(ex, f"amounts are in {code} and the company has no exchange rate for {code}"),
                {**info, "currency": code, "rate": None, "note": f"no company rate for {code}"})

    fields = dict(ex.fields)
    for name in MONEY_FIELDS:
        f = ex.get(name)
        if f.value is None:
            continue
        original = Decimal(f.value)
        places = "0.0001" if name == "unit_price" else "0.01"
        note = (f"converted from {code} {original} at {rate} {base} per {code} (company rate"
                + (f" as of {s['as_of']})" if s["as_of"] else ")"))
        fields[name] = f.model_copy(update={"value": round_amount(original * rate, places),
                                            "note": "; ".join(filter(None, [f.note, note]))})
    return ExtractedPack(fields=fields), {**info, "currency": code, "rate": str(rate)}


def _unusable(ex: ExtractedPack, why: str) -> ExtractedPack:
    fields = dict(ex.fields)
    for name in MONEY_FIELDS:
        f = ex.get(name)
        if f.status != "NOT_FOUND":
            fields[name] = Field(**{**f.model_dump(), "status": "UNCLEAR", "note": why})
    return ExtractedPack(fields=fields)
