"""Generate the unseen-layout test PDFs in tests/fixtures/layouts/ (needs: pip install reportlab).

    python tests/fixtures/make_layouts.py

These are inputs only. The expected outcome of each one is written in tests/test_unseen_layouts.py,
derived from the company rules; nothing here is read by the application.
All suppliers and numbers are fictional.
"""
import io
from pathlib import Path

OUT = Path(__file__).resolve().parent / "layouts"


def pdf(pages: list[list[str]]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = A4[1] - 60
        for line in lines:
            size, text = (13, line[2:]) if line.startswith("# ") else (9.5, line)
            c.setFont("Helvetica-Bold" if size > 10 else "Helvetica", size)
            c.drawString(50, y, text)
            y -= 18 if size > 10 else 14
        c.showPage()
    c.save()
    return buf.getvalue()


def scanned(source: bytes) -> bytes:
    """The same page as a picture only (no text layer), like a phone scan."""
    import pdfplumber
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    with pdfplumber.open(io.BytesIO(source)) as doc:
        img = doc.pages[0].to_image(resolution=100).original
    png = io.BytesIO()
    img.save(png, format="PNG")
    png.seek(0)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(ImageReader(png), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    return buf.getvalue()


KEY_VALUE = [
    "# PRICE QUOTATION",
    "Supplier: Nilphamari Textile Co.",
    "Quote No: NT/2026/0457",
    "Date: 21/10/2026",
    "Customer: Aamar Garments Ltd (fictional)    Your ref: PR-2026-114",
    "Item code: A",
    "Description: Cotton twill fabric, 280 gsm",
    "Quantity: 12,000 metres",
    "Unit price: BDT 470.00 per metre",
    "Amount: BDT 5,640,000.00",
    "Sub-total: BDT 5,640,000.00",
    "VAT @ 15%: BDT 846,000.00",
    "Grand Total: BDT 6,486,000.00",
    "e-TIN No: 123456789012    BIN No: 001234567-0101",
    "Payment terms: 10% advance with the PO; balance within 45 days after delivery.",
    "Lead time: 3 weeks from the date of purchase order.",
    "Penalty: 1% of order value per week of delay.",
    "Validity: 30 days",
    "For Nilphamari Textile Co.",
    "Authorised signatory",
]

PIPE_TABLE = [
    "# Rangpur Trims House",
    "Quotation # RTH-0091    Date: Oct 22, 2026    Ref: PR-2026-114",
    "Description | Item | Qty | Unit | Rate (Tk) | Amount (Tk)",
    "Metal zipper, 18 cm | B | 20,000 | pcs | 36.50 | 730,000.00",
    "Sub Total: Tk 730,000.00",
    "VAT (15%): Tk 109,500.00",
    "Total payable: Tk 839,500.00",
    "TIN: 334455667788  BIN: 009988776-0101",
    "Terms",
    "- Payment: Net 30 days after delivery, no advance.",
    "- Delivery: within 50 days from PO.",
    "- Late delivery: 0.5% of order value per week.",
    "- Offer validity: 45 days",
    "For Rangpur Trims House",
]

DUPLICATE_TOTALS = [
    "# Madhumati Fabrics Ltd",
    "QUOTATION",
    "Quotation no. MF-2026-12 Date 2026-10-24",
    "To Aamar Garments Ltd (fictional) Reference PR-2026-114",
    "Item Description Qty Unit Unit price (BDT) Amount (BDT)",
    "A Cotton twill fabric, 280 gsm 12,000 metre 472.00 5,664,000.00",
    "Subtotal 5,664,000.00",
    "VAT 15% 849,600.00",
    "Total 6,513,600.00",
    "Grand Total: BDT 6,600,000.00",
    "Supplier TIN: 101458013399 BIN: 000170141-0109",
    "TERMS AND CONDITIONS",
    "1. Payment. 10% advance payable with the purchase order. Balance within 45 days of delivery.",
    "2. Delivery. Delivery within 25 days of PO.",
    "3. Delay. Late delivery penalty of 1% of order value per week.",
    "4. Validity. This quotation is valid for 45 days from the date above.",
    "For Madhumati Fabrics Ltd",
]

CONFLICTING_PRICE = [
    "# Shitalakshya Weaving",
    "QUOTATION",
    "Quotation no. SW-2026-77 Date 2026-10-24",
    "To Aamar Garments Ltd (fictional) Reference PR-2026-114",
    "Item Description Qty Unit Unit price (BDT) Amount (BDT)",
    "A Cotton twill fabric, 280 gsm 12,000 metre 472.00 5,664,000.00",
    "Unit price: BDT 497.00 per metre",
    "Subtotal 5,664,000.00",
    "VAT 15% 849,600.00",
    "Total 6,513,600.00",
    "Supplier TIN: 101458013311 BIN: 000170141-0111",
    "TERMS AND CONDITIONS",
    "1. Payment. 10% advance payable with the purchase order. Balance within 45 days of delivery.",
    "2. Delivery. Delivery within 25 days of PO.",
    "3. Delay. Late delivery penalty of 1% of order value per week.",
    "4. Validity. This quotation is valid for 45 days from the date above.",
    "For Shitalakshya Weaving",
]

MISSING_IDENTITY = [  # a clean offer, but no quotation number and no date
    "# Karnaphuli Cartons",
    "To Aamar Garments Ltd (fictional) Reference PR-2026-114",
    "Item Description Qty Unit Unit price (BDT) Amount (BDT)",
    "E Carton box, 5-ply 3,000 piece 90.00 270,000.00",
    "Subtotal 270,000.00",
    "VAT 15% 40,500.00",
    "Total 310,500.00",
    "Supplier TIN: 105458053300 BIN: 000570541-0100",
    "TERMS AND CONDITIONS",
    "1. Payment. 10% advance payable with the purchase order. Balance within 30 days of delivery.",
    "2. Delivery. Delivery within 20 days of PO.",
    "3. Delay. Late delivery penalty of 1% of order value per week.",
    "4. Validity. This quotation is valid for 30 days from the date above.",
    "For Karnaphuli Cartons",
]

INVALID_NUMBER = [
    "# Buriganga Thread Mills",
    "Quotation # BTM-551    Date: 2026-10-23    Ref: PR-2026-114",
    "Item | Description | Qty | Unit | Rate | Amount",
    "C | Polyester sewing thread, cone | 800 | cone | 2O5.00 | 164,000.00",
    "Sub Total: 164,000.00",
    "VAT 15%: 24,600.00",
    "Grand Total: 188,600.00",
    "TIN: 223344556677  BIN: 001122334-0101",
    "Payment terms: 10% advance with the PO, balance within 30 days after delivery.",
    "Delivery: within 18 days of PO.",
    "Penalty: 0.5% of order value per week.",
    "Validity: 30 days",
    "For Buriganga Thread Mills",
]

AMBIGUOUS_DATE = [
    "# Teesta Poly Industries",
    "Quote No: TPI-2026-08    Date: 05/11/2026    Ref: PR-2026-114",
    "Item | Description | Qty | Unit | Rate | Amount",
    "D | Poly bag, 30 x 40 cm | 50,000 | pcs | 6.30 | 315,000.00",
    "Sub Total: 315,000.00",
    "VAT 15%: 47,250.00",
    "Grand Total: 362,250.00",
    "TIN: 998877665544  BIN: 004455667-0101",
    "Payment terms: no advance, payment within 45 days after delivery.",
    "Delivery: within 28 days of PO.",
    "Penalty: 0.1% of order value per day of delay.",
    "Validity: 30 days",
    "For Teesta Poly Industries",
]

TWO_PAGES = [
    [
        "# Jamuna Packaging Co",
        "Quotation no. JPC-2026-301",
        "Date: 21 October 2026",
        "Reference: PR-2026-114",
        "Item | Description | Qty | Unit | Unit price (BDT) | Amount (BDT)",
        "E | Carton box, 5-ply | 3,000 | piece | 93.00 | 279,000.00",
        "Subtotal: BDT 279,000.00",
        "VAT 15%: BDT 41,850.00",
        "Total: BDT 320,850.00",
        "TIN: 556677889900  BIN: 007766554-0101",
        "Terms and conditions: see page 2.",
    ],
    [
        "Payment terms: 15% advance with the purchase order; balance within 40 days of delivery.",
        "Delivery: within 21 days from the date of purchase order.",
        "Late delivery penalty: 0.75% of order value for each full week of delay.",
        "This quotation is valid until 30 November 2026.",
        "For Jamuna Packaging Co",
        "Authorised signatory",
    ],
]

LAYOUTS = {
    "key-value-labels.pdf": [KEY_VALUE],
    "pipe-table-reordered.pdf": [PIPE_TABLE],
    "duplicate-totals.pdf": [DUPLICATE_TOTALS],
    "conflicting-price.pdf": [CONFLICTING_PRICE],
    "missing-number-and-date.pdf": [MISSING_IDENTITY],
    "invalid-number.pdf": [INVALID_NUMBER],
    "ambiguous-date.pdf": [AMBIGUOUS_DATE],
    "two-pages.pdf": TWO_PAGES,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, pages in LAYOUTS.items():
        (OUT / name).write_bytes(pdf(pages))
    (OUT / "scanned-key-value.pdf").write_bytes(scanned(pdf([KEY_VALUE])))
    print(f"wrote {len(LAYOUTS) + 1} PDFs to {OUT}")


if __name__ == "__main__":
    main()
