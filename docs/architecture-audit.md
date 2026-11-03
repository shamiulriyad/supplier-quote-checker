# Architecture audit (before the UI redesign)

## Backend (`checker/`) — unchanged business logic

| Module | Role |
|---|---|
| `parse.py` | PDF → text (pdfplumber). Raises `DocumentError` for unreadable/empty PDFs. |
| `extract_regex.py` | Rule-based extractor, 24 fields, each `Field(value, quote, status, source, note)`. |
| `extract_llm.py` | OpenAI structured output; answers cached in `cache/llm/` (replay). |
| `verify.py` | Merges LLM + regex: quote must be in the PDF, value in the quote, critical fields must agree. |
| `match.py`, `math_check.py`, `rules.py` | PR match (X1), maths (M1–M4), company rules (R1–R5, N1–N7, P1). |
| `decide.py` | Reject > Needs review (incl. UNKNOWN / errors) > Approve. |
| `graph.py` | LangGraph `StateGraph`; `step()` wrapper records errors and timing, never crashes. |

### Actual LangGraph nodes (the UI stages map 1:1 to these)

`parse → extract → verify → match_pr → check_math → check_rules → decide → [human_review] → report`

- `extract` runs **both** extractors (regex, then OpenAI in live/replay). An OpenAI failure is caught inside
  the node: the regex result is kept and an error is added, so the pack ends as *Needs a person*.
- `human_review` only runs when the decision is `NEEDS_REVIEW` and `ask_human=True`. It calls `interrupt()`;
  the graph is resumed with `Command(resume={"decision", "comment"})`.
- A fatal error (e.g. corrupt PDF) makes the remaining steps return "skipped"; `decide` and `report` always run.

### Modes
`auto` (OpenAI if key → saved answer → regex), `live`, `replay`, `regex` — resolved per document in `extract`.

## Old frontend (`app.py`, single file)
- One `graph.invoke()` per PDF inside the button handler: the page is blocked until all PDFs finish; no
  per-step visibility (only a progress bar per document).
- Graph + results in `st.session_state` with `InMemorySaver`: everything is lost on browser refresh or restart;
  pending reviews cannot be resumed after a restart.
- Any widget click during a run would stop the script mid-batch.

## Existing progress-reporting capability
- `graph.stream(..., stream_mode="tasks")` emits, for every node, a **start** event (name, input) and a
  **result** event (node output, error, interrupts). Verified on langgraph 1.2.19. This is the source of the
  real-time monitor — no simulated progress is needed.
- Node outputs already carry the facts to report (text length, extraction source, field statuses, rule
  outcomes, decision, errors).

## Changes required
| Area | Change |
|---|---|
| `checker/graph.py` | `build_graph(..., checkpointer=None)` + `checkpoint_serde()` so a SQLite checkpointer can be used. No logic change. |
| `checker/extract_llm.py` | `check_connection()` for the Settings page (one cheap API call, on request only). |
| New `service/` | `progress.py` (stage map + events from `stream_mode="tasks"`), `batch.py` (queue, sequential background runner, review submit with duplicate protection), `store.py` (SQLite history). Pure Python, no Streamlit → unit-testable. |
| New `ui/` | Theme, sidebar, components, one module per page. |
| `app.py` | Thin entry point: page config, theme, navigation. |

## Design decisions
- **Background thread, not blocking reruns.** One worker thread processes the queue sequentially and writes
  events into a lock-protected snapshot; the monitor polls it with `st.fragment(run_every=1s)`. The user can
  browse completed results while the next PDF runs, and clicks never interrupt processing.
- **SQLite for history and checkpoints** (`runtime/`). Results, events and pending reviews survive restarts on the
  same machine. No Redis/Celery/WebSockets.
- **Cancel** stops *between* documents only (never mid-document), so no half-processed state.
