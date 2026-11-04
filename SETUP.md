# Setup & Run Guide

Everything needed to install, configure, run, test and deploy the **Supplier Quote Checker**.
For what the project does and how it works, see the [README](README.md).

> **Just want to try it?** Open the live app: **https://supplier-quote-checker-6dxzcg8emkywq5yadryrph.streamlit.app/**

## Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [Run the web app](#4-run-the-web-app)
5. [Command-line tools](#5-command-line-tools)
6. [Run the tests](#6-run-the-tests)
7. [Persistent storage with Supabase](#7-persistent-storage-with-supabase)
8. [Deploy on Streamlit Community Cloud](#8-deploy-on-streamlit-community-cloud)
9. [Security](#9-security)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Requirements

### What you need

| Requirement | Needed? | Notes |
|---|---|---|
| **Python 3.10 or newer** | **Required** | 3.12 recommended (used on Streamlit Cloud). |
| **pip** and **git** | **Required** | To install packages and clone the repository. |
| Windows, macOS or Linux | **Required** | Any of them works. |
| **OpenAI API key** | Optional | Enables live AI extraction. Without it the app runs fully offline (regex extractor only). |
| **Supabase project** | Optional | Permanent history and PDF storage. Without it everything is stored locally in `runtime/`. |

### Python packages

Installed from [requirements.txt](requirements.txt):

| Package | Used for |
|---|---|
| `langgraph`, `langgraph-checkpoint-sqlite`, `langgraph-checkpoint-postgres` | The 9-step pipeline and paused human reviews (local SQLite or PostgreSQL) |
| `openai` | Structured field extraction (live mode) |
| `pdfplumber` | Reading PDF text and rendering previews |
| `pydantic` | Typed data models and the LLM output schema |
| `PyYAML` | `config.yaml` (rule thresholds, model, exchange rates) |
| `streamlit`, `pandas`, `plotly` | Web app, tables and charts |
| `supabase`, `psycopg[binary]`, `psycopg-pool` | Supabase Storage and PostgreSQL |
| `pytest` | Test suite |

---

## 2. Installation

```bash
git clone https://github.com/shamiulriyad/supplier-quote-checker.git
cd supplier-quote-checker
```

Create a virtual environment and install the packages:

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Check the installation (offline, no key needed; should print `10/10`):

```bash
python evaluate.py --mode regex
```

---

## 3. Configuration

### Environment variables (`.env`)

Copy the template and fill in what you need. **Everything is optional.**

```bash
cp .env.example .env
```

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | Enables live AI extraction | not set → offline (regex only) |
| `OPENAI_MODEL` | OpenAI model to use | `gpt-4o-mini` |
| `SUPABASE_URL` | Supabase project URL (Project Settings → API) | not set → local storage |
| `SUPABASE_KEY` | Supabase **service role** key (server side only) | not set |
| `SUPABASE_DB_URL` | PostgreSQL **Session pooler** connection string | not set |
| `SUPABASE_STORAGE_BUCKET` | Private bucket for the PDFs | `supplier-documents` |
| `SQC_RUNTIME_DIR` | Absolute path of the local runtime folder | `runtime/` |

`SUPABASE_URL`, `SUPABASE_KEY` and `SUPABASE_DB_URL` must be set **all together or not at all**. If only some
are set, the app stops with a message naming what is missing; it never silently falls back to local storage.

`.env` is git-ignored. Never commit real keys.

### Business rules (`config.yaml`)

All thresholds of the company rules live in [config.yaml](config.yaml). Changing a rule is a one-line edit:

```yaml
rules:
  reject_price_over_pct: 3     # R1 / N1
  max_advance_pct: 20          # R2
  max_delivery_days: 45        # R5
  ...
```

The same file sets the processing mode (`auto` by default), the OpenAI model and the company exchange rates.
**Replace the placeholder exchange rates** (`currency.rates`, `currency.rates_as_of`) with the official
company rates.

---

## 4. Run the web app

```bash
streamlit run app.py
```

Open **http://localhost:8501**. Then:

1. **Document Processing** → **Add all 10 sample documents** (or upload your own PDFs, max 10 MB each).
2. Press **Start processing** and watch every document move through the pipeline.
3. Open any result in **All Documents** to see the decision, the evidence quotes and every rule.
4. Documents that need a person appear in **Human Review**.

The sidebar shows the current mode, the storage in use and the OpenAI connection status.

---

## 5. Command-line tools

```bash
python run.py                                   # all sample packs; writes outputs/results.json and outputs/report.md
python run.py data/supplier-pack-05.pdf --details   # one PDF, with every reason and quote
python run.py --mode regex                      # offline

python evaluate.py --mode regex                 # score against tests/expected.csv (offline)
python evaluate.py --mode live                  # with OpenAI + regex cross-check
```

| Option | Values |
|---|---|
| `--mode` | `auto` (default), `live`, `regex` (offline), `replay` (testing only) |
| `--details` | print reasons and quotes for every pack (`run.py`) |
| `--dir`, `--expected` | evaluate another folder of PDFs with its own answer key (`evaluate.py`) |

---

## 6. Run the tests

```bash
python -m pytest -q
```

200+ automated tests. They need **no credentials** and never call OpenAI or a real Supabase project
(the test setup blanks those variables). They cover the rules, the extractors, the pipeline, unseen PDF
layouts, storage, human review and the Streamlit pages.

Live tests against a real Supabase project are kept out of the repository (local use only). They run only
when `SUPABASE_TEST_URL`, `SUPABASE_TEST_KEY` and `SUPABASE_TEST_DB_URL` point at a **separate test
project**, never the production one.

---

## 7. Persistent storage with Supabase

Without Supabase the app stores everything locally (`runtime/`), and Streamlit Community Cloud deletes that
folder on every reboot. With Supabase, history, paused reviews and PDFs are permanent.

| What | Supabase | Local fallback |
|---|---|---|
| Batches, documents (all fields, rules, quotes), events, step checkpoints, LLM cache | PostgreSQL tables `batches`, `documents`, `document_events`, `pipeline_checkpoints`, `llm_cache` | `runtime/app.db` (SQLite, same SQL) |
| Paused review pipelines (LangGraph state) | PostgreSQL (LangGraph `PostgresSaver` tables) | `runtime/graph-checkpoints.db` |
| Original PDFs | Private Storage bucket, path `pdfs/<sha256[:2]>/<sha256>.pdf` | `runtime/files/` |

### Step 1: Create the project
On [supabase.com](https://supabase.com) → **New project**. Note the database password.

### Step 2: PostgreSQL connection string
**Project Settings → Database → Connection string → Session pooler** (URI), with your password filled in.
If the password contains special characters (`@ # ! * : /` …), percent-encode them (for example `@` → `%40`).
Use the session pooler: the direct connection is IPv6-only and Streamlit Cloud needs IPv4. The app sends no
prepared statements, so the transaction pooler (port 6543) also works.

### Step 3: Storage bucket
Nothing to do: the app creates the private bucket `supplier-documents` (PDF only, 10 MB limit) if it is
missing, and refuses to run if the bucket is public. To create it by hand, run
[supabase/migrations/20261103000100_storage_bucket.sql](supabase/migrations/20261103000100_storage_bucket.sql)
or create a **private** bucket in the dashboard.

### Step 4: Database schema
The app applies [supabase/migrations/20261103000000_initial_schema.sql](supabase/migrations/20261103000000_initial_schema.sql)
on start (idempotent) and creates LangGraph's checkpoint tables. To do it up front and run a self-test:

```bash
python tools/supabase_setup.py   # migrations + checkpoint tables + bucket check + upload/download/delete test
# or with the Supabase CLI:  supabase link --project-ref <ref>  then  supabase db push
```

### Step 5: Environment variables
Put the values in `.env` (local) or Streamlit Secrets (cloud):

```env
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_KEY=<service_role key>
SUPABASE_DB_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
SUPABASE_STORAGE_BUCKET=supplier-documents
```

Start the app and use **Settings → Test storage connection** to check the database and the bucket.

### Step 6: Import an older local history (optional)
```bash
python tools/migrate_local_history.py --dry-run   # show what would be imported
python tools/migrate_local_history.py             # from runtime/history.db, checkpoints.db and the PDFs
```
Keeps batch ids, SHA-256 hashes, results, text, events and human decisions, uploads the PDFs and copies paused
reviews. It is idempotent, opens the source read-only, never deletes local files, and writes
`migration-report-<time>.json`.

---

## 8. Deploy on Streamlit Community Cloud

The project is deployed at **https://supplier-quote-checker-6dxzcg8emkywq5yadryrph.streamlit.app/**.
To deploy your own copy:

1. Push the repository to GitHub.
2. On [share.streamlit.io](https://share.streamlit.io): **Create app** → choose the repository, branch `main`,
   main file `app.py`. Under **Advanced settings** choose **Python 3.12**.
3. In **Settings → Secrets** add:
   ```toml
   OPENAI_API_KEY = "sk-..."
   SUPABASE_URL = "https://<ref>.supabase.co"
   SUPABASE_KEY = "<service_role key>"
   SUPABASE_DB_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres"
   SUPABASE_STORAGE_BUCKET = "supplier-documents"
   ```
   Without the Supabase values the app still works, but history is lost on every reboot.
4. Deploy (or **Reboot** after changing secrets). Keys stay in Streamlit's secrets, never in the code.

---

## 9. Security

- The Storage bucket is **private**, and the app never creates public or signed URLs: previews are downloaded
  and rendered on the server.
- `SUPABASE_KEY` is the service role key. It lives only in `.env` / Streamlit Secrets on the server and is
  never shown in the app.
- Row Level Security is **enabled with no policies** on every table, so Supabase's public API roles can read
  nothing. The app connects server-side.
- Uploads are validated (PDF header, at most 10 MB, must open as a PDF). File names are reduced to a plain name
  and never used as storage paths.
- `.env`, `.streamlit/secrets.toml`, `runtime/` and `cache/llm/` are git-ignored.
- **There is no login yet.** Everyone who can open a deployment sees the same history. Add Supabase Auth and
  per-user RLS policies before storing real supplier documents.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError` | Activate the virtual environment, then `pip install -r requirements.txt`. |
| Every document says "AI unavailable — Regex-only analysis" | No `OPENAI_API_KEY` is set (expected offline), or the key is wrong: use **Settings → Test connection**. |
| "Supabase is only partly configured" | Set all of `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_DB_URL`, or remove all three. |
| "Cannot connect to Supabase PostgreSQL", or a timeout on Streamlit Cloud | Use the **Session pooler** connection string (IPv4), not the direct one; check the password encoding. |
| "Bucket … is PUBLIC" | Dashboard → Storage → the bucket → make it private. |
| Error when starting a batch | Nothing was saved and the queue is kept. Check that the key is the service role key, then press Start again. |
| "N results could not be saved yet" | The database dropped mid-batch; results are kept in memory. Press **Retry saving** once it is back. |
| Slow processing with Supabase (seconds per document even offline) | Each document needs a few database round trips. Put the Supabase project in a region close to the app (Streamlit Community Cloud runs in the US). |
| Clear History reports files not deleted | Those documents stay marked for deletion; run Clear History again. |
| A scanned PDF goes to a person | Expected: OCR is not supported, so a PDF without a text layer is never decided automatically. |
