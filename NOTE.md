# Supplier Quote Checker: one-page note

**What I built.** A fixed, step-by-step pipeline (LangGraph) that reads a supplier quotation PDF and answers Approve, Reject or Needs a person to check, quoting the document for every reason. OpenAI only *extracts* facts, each with the exact text it came from. A separate regex extractor reads the same document. A verify step throws away any value whose quote is not in the PDF, or whose value is not in its own quote, or where the two extractors disagree. All arithmetic and all company rules are plain Python, with thresholds in `config.yaml`. Anything unclear, and any failed step, goes to a person: LangGraph pauses the run until someone approves or rejects it in the Streamlit app.

**How I tested it.**
- `tests/expected.csv`: answer key for the 10 packs, worked out by hand from the rules, with the decision *and* the rule(s) that must be cited.
- `evaluate.py`: a pack only counts as right if the decision matches and the expected rules are among the reasons.
- 68 pytest tests: every rule at its boundary (e.g. price exactly +3% vs +3.01%, delivery 30/31/45/46 days, advance 20/21%, quantity ±5%), a fake LLM that invents a penalty clause for pack 06 (caught: goes to a person, never Approve), LLM timeout, a broken PDF, a non-quotation text, the same input three times giving the same output, and the human-review pause/resume.

**Results.**
| Mode | Decisions right | Decision + reason right | Avg time / pack |
|---|---|---|---|
| Live (OpenAI `gpt-4o-mini` + regex check), 2 runs | 10/10 | 10/10 | ~7 s |
| Offline (regex only) | 10/10 | 10/10 | ~0.1 s |

The first prompt scored 8/10 live. The LLM said "yes, VAT included" for every pack (because the grand total includes VAT) and invented quotes like `"Qty 800"`. The checks caught all of it, so the two errors were clean packs sent to a person, never a wrong Approve. I fixed the prompt with general instructions (quotes must be one continuous piece of text; the VAT question is about the unit price), not pack-specific ones. Even now the LLM claims pack 08's penalty is "% of order value" (the clause does not say so); the verify step throws that away.

**What does not work well (honest).**
- The regex extractor was written against this one document layout. On a new supplier's layout it will often fail to find fields. The system then sends the pack to a person (safe, but more manual work), so in real use the "Needs a person" rate would go up.
- Text PDFs only. A scanned/photographed quote has no text and goes straight to a person (no OCR).
- One item per quotation. Multi-item quotes are marked unclear.
- The 10/10 is on 10 documents whose answers I worked out myself. It shows the logic matches my reading of the rules, not that it generalises.
- Some rules are ambiguous (penalty below 0.5%, VAT-inclusive prices, "4 weeks"). I made assumptions, listed in the README, rather than asking the business.
- Human-review decisions live in memory: restart the app and they are gone. There is no audit log or login.
- Live mode depends on the OpenAI API. If it is down, every pack goes to a person (by design).

**What I would do next.**
1. Collect real quotes from more suppliers and measure the extraction separately per field.
2. Replace the layout-specific regex check with a second, independent LLM pass (different prompt or model) for cross-checking.
3. Add OCR for scanned PDFs and support for multi-item quotes.
4. Store decisions and human overrides in a database as an audit trail, and use the overrides to find rules that need adjusting.
5. Confirm the ambiguous rules with the procurement team.
