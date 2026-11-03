# Supplier Quote Checker: note

## What it is
A fixed, step-by-step pipeline (LangGraph) that reads a supplier quotation PDF and answers **Approve**,
**Reject** or **Needs a person to check**, quoting the document for every reason. The AI only reads; fixed
Python code decides. Anything that cannot be verified goes to a person, who decides in the Streamlit app.

## Extraction architecture
1. **pdfplumber** extracts the text of every page separately (page numbers are kept).
2. **Regex extractor** reads each field in several independent ways and compares them:
   - the item line by a fixed pattern, by the **column order of the table header** (also `|` tables and
     re-ordered columns), and from **"Label: value"** lines;
   - alternative labels (`Qty/Quantity`, `Rate/Unit price`, `Sub-total`, `Grand Total`, `VAT @ 15%`, `e-TIN`, ...);
   - dates in ISO, `21/10/2026`, `21 October 2026`, `Oct 21, 2026`; amounts with `BDT/Tk/৳` and lakh grouping;
   - terms as numbered clauses, labelled lines (`Payment terms:`, `Lead time -`) or sentences anywhere;
   - "valid until <date>" (days computed from the quotation date), "N weeks", "per day" penalties.
3. **OpenAI** (live mode only) returns the same 24 fields as structured JSON, each with the exact quote.
4. **Verification** (`checker/verify.py`) gives every field a status and keeps every reading:
   `matched` (LLM and regex agree) · `regex only` (offline) · `llm only` (descriptive fields only) ·
   `conflict` (LLM and regex disagree) · `conflicting values in document` (e.g. two different totals) ·
   `unverified` (quote not in the PDF, or value not in its quote) · `ambiguous in document` (range, vague,
   "05/11/2026") · `suspicious value` (negative quantity, VAT over 100 %, ...) · `not found`.
   Conflicting values are **never silently replaced**: the field becomes UNCLEAR and all candidates (source,
   value, quote) are stored. Nothing is invented: missing stays missing.

## Evidence for every field
Stored per field in the result (and in Supabase): value, extraction method (Regex / LLM / LLM + Regex),
the exact quote, the **PDF page** (found by searching the quote in each page's text; empty if it is not on
one page or there is no PDF), the verification status, a conflict flag and all candidate readings.
There are **no confidence scores**: the app has no calibrated way to produce one, so it shows the verification
status instead.

## Decision architecture
- The 14 business rules are unchanged (logic and thresholds). Each now also records the **expected condition**
  and the **actual value** from the document.
- Two **verification checks** were added, separate from the 14 rules:
  **V1** mandatory facts (supplier, quotation number, date, item, quantity, unit price, total) must be verified;
  **V2** no field may have conflicting or implausible values. A failed check means *Needs review*.
- Decision: Reject if a reject rule fails · otherwise Needs review if a review rule or check fails, anything is
  unknown, or a step errored · otherwise Approve. So missing, conflicting or unverified information can never
  approve, and every Reject / Needs review lists its reasons.
- The decision uses only this document's verified facts and the purchase request. It never reads stored
  history, `tests/expected.csv` or recorded LLM answers, and the LLM never approves or rejects.

## Explainability in the app
Document details → **Decision** tab: document information · final decision with every reason · financial
analysis (quantity, unit price, line amount vs quantity × price, subtotal, VAT, total, purchase-request maximum,
price and quantity deviation %) · missing or conflicting fields · all 14 rules with expected, actual and
PASS / FAIL / NEEDS REVIEW · the two verification checks. **Extraction evidence** tab: every field with method,
page, verification, conflict and quote; selecting a field shows all candidate readings.

## Test results (actual runs)
| Suite | Result |
|---|---|
| All local tests (`python -m pytest tests --ignore=tests/integration`) | **186 passed** |
| …of which in a fresh clone (demo tests are git-ignored) | 166 |
| Original 10 sample packs, offline (`evaluate.py`) | **10/10** decision + reason |
| 20 generated demo packs, offline | **20/20** |
| Unseen layouts (`tests/test_unseen_layouts.py`, 9 PDFs + text variants) | 28 passed |
| Independence (`tests/test_independence.py`) | 4 passed |

Before/after on the 30 original documents: decisions and fields are identical except demo-17 ("different
wording"), where "Net 45 days from invoice" and "lead time 3 weeks" are now read (still Needs review, now with
the exact reason), and demo-16 (two items), which now also lists V1.

**Tested:** original layouts (`test_pipeline.py`) and unseen layouts (key-value labels, re-ordered `|` table,
two pages with page numbers, duplicate totals, conflicting prices, missing number and date, invalid number
"2O5.00", ambiguous date, scanned PDF, lakh numbers, wording variants) · live mode with a stubbed OpenAI
(agreement, disagreement, an LLM that lies about delivery days cannot change the outcome) · replay mode only
when chosen · a tampered stored decision is never reused · processing reads nothing from the history store ·
uploads in one batch are decided independently.

**Partially tested:**
- Live mode with the real OpenAI API: 10/10 twice (~7 s per pack) **before** this extraction change; after it,
  only with a stubbed API.
- Supabase: the same store code is tested on SQLite and with a mocked Storage client. The real-project
  integration tests passed 5/5 earlier; in the last run 3/5 passed and the failures could not be inspected
  (re-running deletes the project's data, so it was stopped). Not re-run after this change.

## Known limitations
- The regex extractor now handles several layouts, but not every one: free-form letters, multi-column tables
  spread over two lines, or terms only in a footnote may still be missed. They then go to a person.
- One item per quotation; multi-item quotes are marked unclear.
- No OCR: scanned PDFs go to a person.
- Payment and delivery periods must say what they count from (delivery / purchase order); otherwise a person
  checks. This is deliberate but adds manual reviews.
- Ambiguous rules (penalty below 0.5 %, VAT-inclusive prices) use the assumptions listed in the README.
- No login: everyone on one deployment shares the history.

## Deployment
Unchanged and Streamlit Community Cloud compatible: same entry point (`app.py`), same environment variables
(`OPENAI_API_KEY`, optional `SUPABASE_URL` / `SUPABASE_KEY` / `SUPABASE_DB_URL` / `SUPABASE_STORAGE_BUCKET`),
no new runtime dependencies and no local model. Stored results gained fields (page, verification, candidates,
expected/actual, financial); older stored results still open. See the README for setup.

## Next
1. Measure extraction per field on real supplier quotes from many suppliers.
2. A second, independent LLM pass (different prompt or model) as the cross-check for layouts regex cannot read.
3. OCR for scanned PDFs; multi-item quotes.
4. Use human overrides from the review queue to find rules or extractors that need adjusting.
