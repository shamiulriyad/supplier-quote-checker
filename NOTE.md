# Supplier Quote Checker: one-page note

**Live app:** https://supplier-quote-checker-6dxzcg8emkywq5yadryrph.streamlit.app/ ·
**Code:** https://github.com/shamiulriyad/supplier-quote-checker

## What I built
A system that checks a supplier's quotation PDF against the purchase request and the company rules, and
answers **Approve**, **Reject** or **Needs a person to check**, quoting the exact line from the PDF for every
reason. It is a fixed LangGraph pipeline of nine steps (read, extract, verify, match, maths, rules, decide,
human review, report), not a free-running agent.

The main idea: **AI reads, code decides, and a person decides when anything is unclear.**

- OpenAI (`gpt-4o-mini`) and a separate rule-based reader both read the document.
- A value is trusted only if its quote is really in the PDF and both readers agree. Otherwise it is marked
  unclear, and unclear facts can never lead to Approve.
- Fixed Python code applies the 14 company rules. The AI never approves or rejects.
- Prices in another currency are converted to BDT with a fixed company rate before the price rules run.
- A Streamlit app lets a person upload PDFs, see the reasons and evidence, and decide the unclear cases.

## How I tested it
- The 10 sample packs, scored against a written answer key (`tests/expected.csv`). A pack only counts as
  right if the decision **and** the reason match.
- 20 extra test PDFs I generated: exact limits, different wording, broken numbers, a scanned PDF.
- 9 PDF layouts the reader was not built for (re-ordered tables, two pages, duplicate totals, and so on).
- Tricky cases: the AI giving a wrong answer, text in the PDF telling the AI to approve, two different
  totals, USD prices.

## Results
| Test | Result |
|---|---|
| 10 sample packs, live (OpenAI + cross-check), 2 runs | **10/10** decision and reason, ~7 s per pack |
| 10 sample packs, offline (no AI) | **10/10**, ~0.1 s per pack |
| 20 extra test packs, offline | **20/20** |
| Automated tests | 220 passed (200 in a fresh clone; 20 use demo files not in git) |

## What does not work well
- **The last live run is older than the last change.** The live 10/10 was measured before I improved the
  reader and the AI prompt. Since then live mode was only tested with a simulated AI. The saved AI answers
  were made with the old prompt, so "replay" mode now scores only 3/10 until they are re-recorded.
- **Only tested on our sample files and my own test PDFs.** Real supplier quotes may use layouts it cannot read.
  Those go to a person: safe, but more manual work.
- **No OCR.** Scanned PDFs always go to a person.
- **One item per quote.** Quotes with several items go to a person.
- **Strict on purpose.** Payment or delivery terms that do not say what they count from (delivery? order?)
  go to a person. This gives more manual reviews.
- **Exchange rates are placeholders** until finance sets the real ones.
- **The database (Supabase) integration test** passed 5/5 earlier but only 3/5 in the last run, and I could
  not re-run it safely. The same code passes all tests on a local database.
- **No login.** Everyone using one deployment shares the same history.

## What I would do next
1. Re-run live mode with the real AI and re-record the replay answers.
2. Test on real quotes from many suppliers and measure accuracy per field.
3. Add OCR for scanned PDFs and support for several items per quote.
4. Learn from the people's decisions in the review queue to see which rules or readers need fixing.
5. Add login so each team only sees its own documents.

More detail: [README.md](README.md) · [docs/design-notes.md](docs/design-notes.md) · [SETUP.md](SETUP.md)
