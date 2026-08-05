# UOF Webapp — Project Documentation

View live at: https://1reubent.github.io/uof-webapp/

## Table of Contents

- [Part 1: Understanding the Project](#part-1-understanding-the-project)
  - [Section 1: What Is This Project](#section-1-what-is-this-project)
  - [Section 2: Database Schema Design Decisions](#section-2-database-schema-design-decisions)
  - [Section 3: Code Repository Structure](#section-3-code-repository-structure)
    - [Important Files](#important-files)
    - [Files That Aren't Important to the Project](#files-that-arent-important-to-the-project)
  - [Section 4: Key Code Files, Broken Down](#section-4-key-code-files-broken-down-bridge-clean_and_populate-etl_delta)
    - [`backend/api/bridge.py` — The API Layer](#backendapibridgepy--the-api-layer)
    - [`backend/etl/uof_etl/clean_and_populate.py` — The Cleaning Core](#backendetluof_etlclean_and_populatepy--the-cleaning-core)
    - [`backend/etl/etl_delta.py` — The Incremental Loader](#backendetletl_deltapy--the-incremental-loader)
- [Part 2: Setup and Operations](#part-2-setup-and-operations)
  - [Section 5: Building the Database from Scratch](#section-5-building-the-database-from-scratch)
    - [1. Install the Python Dependencies](#1-install-the-python-dependencies)
    - [2. Configure the Database Connection](#2-configure-the-database-connection)
    - [3. Create the Database Tables and Reference Data](#3-create-the-database-tables-and-reference-data)
    - [4. Add the Source Files](#4-add-the-source-files)
    - [5. Preview the Initial Load](#5-preview-the-initial-load)
    - [6. Import and Process the Data](#6-import-and-process-the-data)
    - [7. Verify the Database Load](#7-verify-the-database-load)
  - [Section 6: Configuring and Running the Website](#section-6-configuring-and-running-the-website)
    - [Running It Locally](#running-it-locally)
    - [Hosting It](#hosting-it)
  - [Section 7: Running the Delta Loader](#section-7-running-the-delta-loader)
    - [Preview an Update](#preview-an-update)
    - [Import New Records](#import-new-records)
    - [Update Only the UoF Dataset](#update-only-the-uof-dataset)
    - [Update Only the ARRIVE Together Dataset](#update-only-the-arrive-together-dataset)
    - [Optional Batch Size](#optional-batch-size)

<!-- ## Documentation Assignments

- **Database schema design decisions:** Omar
- **Code repository structure:** Reuben
- **Building the database from scratch:** Omar
- **Configuring and running the website:** Reuben
- **Running the delta loader:** Omar -->

---

## **PART 1:** Understanding the Project

### **SECTION 1:** What Is This Project

A tool that turns two New Jersey public-safety datasets — **Use of Force (UoF)** incident reports and **ARRIVE Together** program data — from raw Excel exports into clean, queryable MySQL databases, with a single browser-based UI for filtering and paging through both.

The project is built out of four layers:

1. **Database schema** (MySQL) — 8 tables total: a raw staging table, a cleaned/standardized table, a tokenized-multi-value table, and two reference tables for UoF, plus a smaller matching pair of tables for ARRIVE. See [Section 2: Database Schema Design Decisions](#section-2-database-schema-design-decisions).
2. **ETL pipeline** (Python) — For the UoF dataset, we read an Excel export, stage it as-is, then clean and standardize it into the schema, tokenizing multi-value columns. For the ARRIVE dataset, we import the data and only tokenize multi-value columns. See [`clean_and_populate.py` — the cleaning core](#backendetluof_etlclean_and_populatepy--the-cleaning-core) and [Section 5: Building the Database from Scratch](#section-5-building-the-database-from-scratch).
3. **API layer** (`backend/api/bridge.py`, Flask) — the only piece that talks to MySQL; takes a JSON filter specification from the frontend and returns matching rows from the database. See [`bridge.py` — the API layer](#backendapibridgepy--the-api-layer).
4. **Frontend** (`frontend/index.html`, static HTML/JS) — a single page with a dataset switcher (UoF ⟷ ARRIVE) that builds a query from user-selected filters and calls the API directly for live results, with CSV/spreadsheet export.

Together, these four layers work as a unified system. Filtering in the browser produces a real query, run against a real database, with real results back in the browser.

---

### **SECTION 2:** Database Schema Design Decisions

**Section owner: Omar**

The database was redesigned to make the Use of Force and ARRIVE Together datasets easier to clean, maintain, and query while preserving the original public records.

- Separates staged, cleaned, and multi-value data to improve organization and filtering.
- Keeps the UoF and ARRIVE Together datasets in separate structures because they contain different fields and reporting formats.
- Uses standardization and exception tracking to address inconsistent values without silently removing source data.
- Supports future dataset updates and the web-based query interface.

Table Breakdown:

| Table | Purpose |
|-------|---------|
| `uof_main_processing_table` | Raw staging table, 1:1 with Excel columns, plus a `processed` flag. |
| `uof_main_data` | Cleaned/standardized "final" table — same shape as staging minus `processed` with an added `Under 18` column. |
| `uof_dashboard_values_data` | Tokenized multi-value fields: one row per `(Form_Id, Position_Id, Value_Id, Column_Value)` — the normalized form of columns like `Subject_Actions` that can hold multiple selections per incident. It's important to note that only multi-value fields that *actually* have multiple values are tokenized into this table. Single-value entries are maintained in the main table.|
| `uof_column_values_data` | Static catalog of every valid value per column position — reference/lookup data, not incident data. Loaded using `column_values_seed.sql` |
| `standard_values_table` | Raw-value → standard-value synonym map used during cleaning. Loaded using `standard_values_seed.sql`|
| `exceptions_table` | Audit log of values that failed cleaning/standardization, keyed to the original form/column, with a human-readable `reason`. Nothing is silently dropped — everything that doesn't clean cleanly is preserved here for review. |
| `arrive_main_data`| Table for both staged and cleaned/tokenized arrive data |
| `arrive_values_data` | The `uof_dashboard_values_data` equivalent for the arrive data. Tokenizes genuinely multi-value fields |


---

### **SECTION 3:** Code Repository Structure

**Section owner: Reuben**

```
uof-webapp/
├── backend/
│   ├── api/
│   │   └── bridge.py                    ← Flask API: runs queries from the frontend against MySQL, returns JSON
│   │
│   ├── config/
│   │   ├── db_config.py                 ← builds DB_CONFIG from environment variables (committed, no secrets)
│   │   ├── .env.example                 ← checked-in template — copy to .env and fill in (local dev only)
│   │   ├── .env                         ← real local credentials (gitignored)
│   │   └── ca.pem                       ← CA cert (gitignored, only needed if SSL verification is necessary) 
│   │
│   ├── database/
│   │   ├── schema.sql                   ← builds all 8 tables from scratch (run once)
│   │   └── seeds/
│   │       ├── column_values_seed.sql   ← seeds uof_column_values_data (valid-value catalog for multi-value fields)
│   │       └── standard_values_seed.sql ← seeds standard_values_table (synonym/spelling map)
│   │
│   └── etl/
│       ├── etl_delta.py                 ← append-only loader for new UoF and/or ARRIVE Excel exports (can also run cleaner)
│       ├── uof_etl/
│       │   ├── import_script.py         ← UoF Excel → uof_main_processing_table (raw staging)
│       │   └── clean_and_populate.py    ← staging → uof_main_data (cleaning/standardization)
│       └── arrive_etl/
│           ├── import_arrive_data.py    ← ARRIVE Excel → arrive_main_data
│           └── tokenize_arrive_data.py  ← arrive_main_data → arrive_values_data (tokenizing multi-value fields)
│
├── frontend/
│   └── index.html                       ← the live single-page app (query builder + results viewer)
│
├── data/                                ← sample/source Excel exports (see below)
│
├── docs/                                ← planning docs + earlier prototypes (not part of the running app)
│
├── render.yaml                          ← Render deployment blueprint (not necessary; used during development)
├── requirements.txt                     ← runtime dependencies (Flask API)
└── requirements-etl.txt                 ← ETL-only dependencies (pandas/numpy/openpyxl)
```

#### Important Files

A one-line orientation to everything in the tree above. Three of these — `bridge.py`, `clean_and_populate.py`, and `etl_delta.py` — are covered in more depth in [Section 4: Key Code Files, Broken Down](#section-4-key-code-files-broken-down-bridge-clean_and_populate-etl_delta) below, as they might be important to understand if you plan on hosting this project's API and database.

- **`backend/api/bridge.py`** — The Flask API; the only piece that talks to MySQL on the frontend's behalf. *(Deep dive below.)*

- **`backend/config/db_config.py`** — Builds the DB connection config from environment variables. Contains no real credentials, so it's safe to commit; every script that touches the database imports from here rather than connecting directly.

- **`backend/config/.env.example`** — Checked-in template for local development. Copy to `.env` (gitignored) and fill it in with the real host/port/user/password for the database connection. Used for setting environment variables during local development

- **`backend/database/schema.sql`** — A `mysqldump`-style structure dump that builds all 8 tables (plus indexes) from scratch. Meant to be run once. Inline comments explain why some columns deviate from their "natural" type. (explained above in [Section 2: Database Schema Design Decisions](#section-2-database-schema-design-decisions))

- **`backend/database/seeds/column_values_seed.sql`** / **`standard_values_seed.sql`** — Static reference data loaded once, independent of any Excel import: `column_values_seed.sql` loads `uof_column_values_data`, the valid-value catalog for multi-value columns. `standard_values_seed.sql` loads `standard_values_table`, the raw-spelling → canonical-spelling synonym map for single-value columns.

- **`backend/etl/uof_etl/import_script.py`** — Reads the UoF Excel file, renames headers to schema column names, and batch-inserts every row as-is into the raw staging table (`uof_main_processing_table`).

- **`backend/etl/uof_etl/clean_and_populate.py`** — moves UoF data from staging → final cleaned tables. *(Deep dive below.)*

- **`backend/etl/arrive_etl/import_arrive_data.py`** — Same shape as `import_script.py`, but for the separate ARRIVE Together dataset: reads the Excel export, converts its multi-value cells from a list to a string, and inserts the new rows into `arrive_main_data`.

- **`backend/etl/arrive_etl/tokenize_arrive_data.py`** — Reads `arrive_main_data` and builds `arrive_values_data`, tokenizing genuinely multi-value cells. Fully builds from scratch each run, so always safe to re-run.

- **`backend/etl/etl_delta.py`** — Append-only loader for new/incremental UoF and ARRIVE Together Excel exports. *(Deep dive below.)*

- **`frontend/index.html`** — The actual running application: a single self-contained HTML/CSS/JS file with a tab switcher between the UoF and ARRIVE datasets, calling `bridge.py` directly for live results.

- **`data/`** — Sample/source Excel exports consumed by the ETL scripts (not read by the app at runtime). Currently contains the full UoF dataset, a ~1k-row UoF subset for quick local testing, and the ARRIVE Together export.

Again, the three most important files to understand if you plan on hosting this project's API and database on your own infrastructure are `bridge.py` (the API file), `clean_and_populate.py` (the UoF cleaning procedure), and `etl_delta.py` (the delta loader). These are explained in depth in the [Section 4: Key Code Files, Broken Down](#section-4-key-code-files-broken-down-bridge-clean_and_populate-etl_delta) section.

#### Files That Aren't Important to the Project

- **`render.yaml`** — The Render deployment blueprint, since the API was hosted on Render for development.
- **`docs/`** — A mix of early planning artifacts (`Project_Charter_Document.pdf`, the two `.docx` form-design docs) and an earlier two-file frontend prototype (`query-builder.html`, `results-viewer.html`) that `frontend/index.html` has since replaced with a live-querying single page. Worth a skim for original project scope/intent, but none of it is part of the running application.

---

### **SECTION 4:** Key Code Files, Broken Down (`bridge`, `clean_and_populate`, `etl_delta`)

**Section owner: Reuben**

If you're hosting this project's API and database on your own infrastructure, these are the three files that might be worth understanding.

#### `backend/api/bridge.py` — The API Layer

This is where the MySQL connection is opened on the frontend's behalf in order to answer the user's queries live. Everything else in `backend/` is offline data-prep; `bridge.py` is the live, always-running piece.

- **Endpoints**: `GET /health` is a static endpoint for testing; `GET /filter-values/<dataset>` (`uof` or `arrive`) returns every known value for each categorical column (used for the frontend's autocomplete); `POST /query/uof` and `POST /query/arrive` each take a JSON filter specification (containing the user's inputs) and return matching rows as JSON.
- **Query building**: `build_uof_sql()` / `build_arrive_sql()` turn a request body into a parameterized SQL string — walked through in detail below. `execute_query()` is the single shared place that actually opens a connection, runs it, and closes it again — used by both.
- **Security model**: column *names* from the request (which columns to filter or select) are checked against a hardcoded whitelist (`ALLOWED_COLUMNS` / `ARRIVE_ALLOWED_COLUMNS`) before being spliced into the SQL string as identifiers. If you add a new filterable column to the schema, it has to be added to one of these whitelists here, or `bridge.py` will silently ignore it.
- **Boolean columns** (`Other_Officer_Involved`, `Officer_In_Uniform`, `Officer_Injuries_Injured`, `Under_18`) are stored as `tinyint` 1/0/`NULL` but shown to users as labels like `"True"`/`"False"`/`"Not Provided"`. `BOOLEAN_COLUMN_LABELS` maps one to the other in both directions — check it if a boolean filter or displayed value looks wrong.
- **Caching**: `/filter-values/<dataset>` is the endpoint that returns autocomplete values. They are requested once on the first page load and cached forever in a plain process-global dict (`_filter_values_cache`).
   - The autocomplete values are loaded as follows: The values for the 5 standard values columns (`SINGLE_VALUE_COLUMNS`) are loaded by querying `standard_values_table`. The values for 23 multi-value columns (`MULTI_VALUE_POSITION`) are loaded by querying `uof_column_values_data`. The values for the last 3 uncataloged categorical columns (`DISTINCT_VALUE_COLUMNS`) are loaded using a DISTINCT query on the main table. Boolean autocomplete values are stored in the code, and ID fields are not given any autocomplete.
- **Hosting elsewhere**: This is reiterated in the [Hosting it](#hosting-it) section, but if you plan on hosting the API there are two things to address:
      1. `frontend/index.html`'s `BASE` constant (currently `https://uof-webapp-api.onrender.com` in production) needs to point at your new host
      2. `CORS(app)` in `bridge.py` currently allows any origin, which you may want to restrict once you're not just testing.
      Also, the API runs on port 5001 when run locally. (see [Running it locally](#running-it-locally))

The main SQL query-building logic is in the functions `build_uof_sql()` and `build_arrive_sql()` in `bridge.py`. The primary thing to understand here is how multi-value columns are queried using our `uof_dashboard_values_data` and `arrive_values_data` tables. Since only multi-value columns that actually have multiple values get tokenized, the query needs to check both tables: for a given row and multi-value column, it either has a match in `uof_main_data` or a match in the dashboard values table, but not both. Code: [[L457-477](backend/api/bridge.py#L457-L477)]

#### `backend/etl/uof_etl/clean_and_populate.py` — The Cleaning Core

This is the code that runs our cleaning/ETL pipeline after we've imported the data.

- **What it does, in order**: pulls only unprocessed rows (`WHERE processed = 0`) from `uof_main_processing_table`; normalizes whitespace/casing and coerces null-ish strings (`"None"`, `"NaN"`, `""`) to real `NaN`; derives the `Under_18` flag from `Subject_Age` tokens; maps loose boolean text (`yes/true/1/y`, etc.) to 1/0 for the genuinely-boolean columns; parses `Incident_Date`; coerces the genuinely-integer columns; then calls `populate_subtables_and_standardize()`, which does the real work:
  - **Single-value columns** (`Video_Footage`, `Officer_Rank`, etc.) are looked up in `standard_values_table` and rewritten to their canonical spelling.
  - **Multi-value columns** (most of the rest) are comma-split into tokens — with a hardcoded `embedded_commas` list protecting values that contain a comma from being split — and each token is validated against `uof_column_values_data`.
  - As mentioned before, only genuinely multi-valued rows (more than one token) get tokenized into `uof_dashboard_values_data`; single-valued rows are left as plain text directly in `uof_main_data`. This split is what `bridge.py`'s query-building has to account for (see above).
  - Anything that fails to standardize or validate is logged to `exceptions_table` with a reason, not silently dropped.
  - **Trimming over-repeated values**: some multi-value columns (`TRIM_TO_SUBJECT_COUNT_COLS`, e.g. `Force_Type`) are meant to hold one value per subject involved in the incident, but source data occasionally repeats the same token more times than there are subjects to justify it. These columns are trimmed so each unique value appears at most once per subject; a repeat that's still within the subject count (e.g. two subjects both hit with "Used arms/hands") is left alone, since that's indistinguishable from two subjects legitimately sharing the same value.
- **Idempotency**: source rows are marked `processed = 1` at the end, so re-running this script only ever touches rows added since the last run.
- **Read the inline comments before changing `bool_cols`/`int_cols`**: certain boolean columns (Subject_Arrested) and certain integer columns (Subject_Age) are not added to `bool_cols`/`int_cols` because they are actually stored as strings, because they're multi-value. The inline comments explain further about this.
- **Depends on**: the staging table (`uof_main_processing_table`) and the reference/seed tables (`standard_values_table`, `uof_column_values_data`) being already populated

#### `backend/etl/etl_delta.py` — The Incremental Loader

This tool handles new monthly/quarterly exports for **both** datasets — an alternative to re-running `import_script.py`/`import_arrive_data.py` against a full re-export every time. `--uof-file` and `--arrive-file` are independent: pass either one, or both, and each dataset's delta runs on its own.

- **UoF path** (`--uof-file`): reads and validates the incoming Excel file (drops fully blank rows, renames headers, fills any schema columns missing from this particular export as `NULL`, rejects the file outright if `Form_ID` is missing, non-integer, or has in-file duplicates); checks which of the incoming `Form_ID`s already exist in *either* `uof_main_data` or `uof_main_processing_table`; inserts only the rows that are genuinely new into `uof_main_processing_table`; and, if `--run-cleaners` is passed, chains straight into `clean_and_populate.py`'s `clean_uof_data()` so the newly staged rows get cleaned in the same run.
- **ARRIVE path** (`--arrive-file`): the same shape, keyed by `Random_ID` instead of `Form_ID` and checked only against `arrive_main_data` (there's no separate staging table for ARRIVE). New rows are inserted directly into `arrive_main_data`; if `--run-cleaners` is passed, it then runs `tokenize_arrive_data.py`, which fully rebuilds `arrive_values_data` from scratch every time rather than appending, so the tokenized table always ends up complete and consistent even though only the newly inserted `arrive_main_data` rows were actually new.
- **CLI**: `python etl_delta.py --uof-file "UoF_July_2026.xlsx" --arrive-file "ARRIVE_July_2026.xlsx"` stages/inserts new rows for whichever file(s) you pass. Add `--dry-run` to report the delta without inserting anything (use this first on an unfamiliar export), or `--run-cleaners` (alias: `--run-cleaner`) to also run the relevant cleaning/tokenization step(s) immediately afterward. `--batch-size` controls how many rows are committed per round-trip (default 500). The older single-dataset `--file` flag still works as an alias for `--uof-file`, for backwards compatibility.

---

## **PART 2:** Setup and Operations

### **SECTION 5:** Building the Database from Scratch

**Section owner: Omar**

Follow these steps when setting up a new or empty database. Run all commands from the project's root folder.

#### 1. Install the Python Dependencies

Install the packages required by the web application and ETL scripts.

##### Windows PowerShell

```powershell
py -m pip install -r requirements.txt
py -m pip install -r requirements-etl.txt
```

##### macOS or Linux Terminal

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-etl.txt
```

#### 2. Configure the Database Connection

Create a local environment file from the provided template.

##### Windows PowerShell

```powershell
Copy-Item "backend\config\.env.example" "backend\config\.env"
```

##### macOS or Linux Terminal

```bash
cp backend/config/.env.example backend/config/.env
```

Open `backend/config/.env` and replace the placeholder values with the correct database host, port, username, password, database name, and SSL settings required by the database provider.

The `.env` file contains private credentials and must not be committed to GitHub.

If your database requires SSL (common for managed/cloud MySQL), also set `DB_SSL_CA_PATH` to a CA certificate downloaded from your provider and save it as:

```text
backend/config/ca.pem
```

#### 3. Create the Database Tables and Reference Data

Use MySQL Workbench, the Aiven Query Editor, or another MySQL client to run these files in order:

1. `backend/database/schema.sql`
2. `backend/database/seeds/column_values_seed.sql`
3. `backend/database/seeds/standard_values_seed.sql`

`schema.sql` creates the UoF and ARRIVE Together database tables. The seed files load the reference values used during data cleaning and standardization.

#### 4. Add the Source Files

Place the Use of Force and ARRIVE Together Excel files in the project's `data` folder.

Example files included with the project:

- `data/UoF_database_1k_subset_100120_to_053126.xlsx`
- `data/ARRIVE_Reports_Download_File_7_1_2026.xlsx`

Replace these filenames with the latest source files when updated datasets are released.

#### 5. Preview the Initial Load

Run the delta loader in dry-run mode before inserting data.

##### Windows PowerShell

```powershell
py backend\etl\etl_delta.py `
  --uof-file "data\UoF_database_1k_subset_100120_to_053126.xlsx" `
  --arrive-file "data\ARRIVE_Reports_Download_File_7_1_2026.xlsx" `
  --dry-run
```

##### macOS or Linux Terminal

```bash
python3 backend/etl/etl_delta.py \
  --uof-file "data/UoF_database_1k_subset_100120_to_053126.xlsx" \
  --arrive-file "data/ARRIVE_Reports_Download_File_7_1_2026.xlsx" \
  --dry-run
```

Dry-run mode validates both files and reports how many records are new. It does not insert data or run the cleaning scripts.

#### 6. Import and Process the Data

After reviewing the dry-run results, run the loader with the cleaning workflows enabled.

##### Windows PowerShell

```powershell
py backend\etl\etl_delta.py `
  --uof-file "data\UoF_database_1k_subset_100120_to_053126.xlsx" `
  --arrive-file "data\ARRIVE_Reports_Download_File_7_1_2026.xlsx" `
  --run-cleaners
```

##### macOS or Linux Terminal

```bash
python3 backend/etl/etl_delta.py \
  --uof-file "data/UoF_database_1k_subset_100120_to_053126.xlsx" \
  --arrive-file "data/ARRIVE_Reports_Download_File_7_1_2026.xlsx" \
  --run-cleaners
```

This command:

- Imports only new UoF records and runs `backend/etl/uof_etl/clean_and_populate.py`.
- Imports only new ARRIVE Together records and runs `backend/etl/arrive_etl/tokenize_arrive_data.py`.
- Skips records already stored in the database, making the workflow safe to rerun.

#### 7. Verify the Database Load

Confirm that records were added to both main tables:

```sql
SELECT COUNT(*) FROM uof_main_data;
SELECT COUNT(*) FROM arrive_main_data;
```

Confirm that no UoF records remain waiting to be processed:

```sql
SELECT COUNT(*)
FROM uof_main_processing_table
WHERE processed = 0;
```

A result of `0` means that all staged UoF records completed the cleaning workflow.

---

### **SECTION 6:** Configuring and Running the Website

**Section owner: Reuben**

"The website" is two independent pieces that get configured and run differently: the API (`backend/api/bridge.py`, which talks to MySQL) and the frontend (`frontend/index.html`, a static file that talks to the API). Both sections below assume a MySQL database already exists with the schema built and data loaded — see [Section 5: Building the Database from Scratch](#section-5-building-the-database-from-scratch) above.

#### Running It Locally

For developing or testing against a database on your own machine.

1. **Prerequisites** — Python 3.9+, and a MySQL database you can already connect to (local or remote) with the schema built and data loaded.
2. **Set up the Python environment** — Only `requirements.txt` is needed to run the API/frontend — `requirements-etl.txt` is separate and only needed if you're also running the ETL scripts.

   **Windows PowerShell:**
   ```powershell
   py -m venv .venv
   .venv\Scripts\Activate.ps1
   py -m pip install -r requirements.txt
   ```

   **macOS or Linux Terminal:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python3 -m pip install -r requirements.txt
   ```
3. **Configure the DB connection** — Create a local environment file from the provided template.

   **Windows PowerShell:**
   ```powershell
   Copy-Item "backend\config\.env.example" "backend\config\.env"
   ```

   **macOS or Linux Terminal:**
   ```bash
   cp backend/config/.env.example backend/config/.env
   ```

   Open `backend/config/.env` and fill in `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` for your database. `backend/config/db_config.py` loads all of this automatically — nothing else needs to be touched.

   The `.env` file contains private credentials and must not be committed to GitHub.

   If your database requires SSL (common for managed/cloud MySQL), also set `DB_SSL_CA_PATH` to a CA certificate downloaded from your provider and save it as:

   ```text
   backend/config/ca.pem
   ```
4. **Start the API** — It starts on `http://localhost:5001` — leave it running in its own terminal.

   ```bash
   python backend/api/bridge.py
   ```
5. **Open the frontend** — Double-click `frontend/index.html`, or open it via File → Open in your browser. No build step, no dev server — when opened this way it automatically detects it's running locally and points itself at `http://localhost:5001`, so no further configuration is needed.

#### Hosting It

If you are hosting the site on a server, then the configuration will be different than if you were doing so locally. Environment variables get set through your hosting provider rather than a `.env` file, and the frontend needs to be told the API's real, public URL instead of `localhost`.

The exact steps depend on which provider(s) you use, but every provider needs the same things from this project:

1. **A place to run `bridge.py` continuously** — A VM, a container host, or a "deploy from git" platform (Render, Railway, Fly.io, etc.): anything that can keep a Python process alive. Run it with a real WSGI server rather than Flask's local-dev server: `gunicorn backend.api.bridge:app` (`gunicorn` is already in `requirements.txt`). Check your host's docs for how it expects the app to bind to a port — many platforms inject a `$PORT` environment variable rather than letting you hardcode 5001.
2. **A MySQL database reachable from wherever `bridge.py` ends up running** — For a managed/cloud database, this usually means allow-listing the API host's outbound IP or otherwise confirming the provider accepts connections from outside your own machine. If it requires SSL, follow the same `DB_SSL_CA_PATH` steps as local dev, but as an absolute path — many hosts offer a "secret file" mechanism for exactly this.
3. **Environment variables set through your host, not a `.env` file** — `.env` files are a local-dev convenience — `db_config.py`'s `load_dotenv()` call silently does nothing if the file isn't there, which is exactly the case in production. Instead, set `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` (and `FLASK_SECRET_KEY`, if you want session signing to be more than a placeholder) directly through whatever mechanism your host provides — a dashboard, a systemd unit's `Environment=` lines, a container's env config, etc.
4. **Point the frontend at the API's public URL** — Once `bridge.py` is reachable at a real URL, update the `BASE` constant in `frontend/index.html` (currently line 449) — it falls back to a placeholder URL whenever it doesn't detect a local environment. This is the one line of application code you need to change to re-host this project somewhere new.
5. **Serve `frontend/index.html` from somewhere reachable** — It's a single self-contained static file with no build step and no server-side logic, so any static file host works — GitHub Pages, Netlify, S3, or even the same host running the API.
6. **CORS** — `bridge.py` already enables CORS for all origins, so the frontend will be able to reach the API regardless of where each ends up hosted. Worth restricting to your actual frontend's origin once you're past initial testing.

---

### **SECTION 7:** Running the Delta Loader

**Section owner: Omar**

Use `backend/etl/etl_delta.py` whenever updated UoF or ARRIVE Together Excel files are received. The loader compares the source files with the database and imports only records that have not already been loaded.

The database connection must be configured before running the loader.

#### Preview an Update

Always begin with a dry run.

##### Windows PowerShell

```powershell
py backend\etl\etl_delta.py `
  --uof-file "data\UoF_database_1k_subset_100120_to_053126.xlsx" `
  --arrive-file "data\ARRIVE_Reports_Download_File_7_1_2026.xlsx" `
  --dry-run
```

##### macOS or Linux Terminal

```bash
python3 backend/etl/etl_delta.py \
  --uof-file "data/UoF_database_1k_subset_100120_to_053126.xlsx" \
  --arrive-file "data/ARRIVE_Reports_Download_File_7_1_2026.xlsx" \
  --dry-run
```

Review the reported record counts before continuing. Dry-run mode does not insert data or run the cleaning scripts.

#### Import New Records

After reviewing the dry-run results, run the update.

##### Windows PowerShell

```powershell
py backend\etl\etl_delta.py `
  --uof-file "data\UoF_database_1k_subset_100120_to_053126.xlsx" `
  --arrive-file "data\ARRIVE_Reports_Download_File_7_1_2026.xlsx" `
  --run-cleaners
```

##### macOS or Linux Terminal

```bash
python3 backend/etl/etl_delta.py \
  --uof-file "data/UoF_database_1k_subset_100120_to_053126.xlsx" \
  --arrive-file "data/ARRIVE_Reports_Download_File_7_1_2026.xlsx" \
  --run-cleaners
```

The loader will:

- Import only new records from each dataset.
- Skip records already stored in the database.
- Run the UoF cleaning and standardization workflow.
- Run the ARRIVE Together tokenization workflow.

#### Update Only the UoF Dataset

##### Windows PowerShell

Preview the update:

```powershell
py backend\etl\etl_delta.py `
  --uof-file "data\UoF_database_1k_subset_100120_to_053126.xlsx" `
  --dry-run
```

Import and process the new records:

```powershell
py backend\etl\etl_delta.py `
  --uof-file "data\UoF_database_1k_subset_100120_to_053126.xlsx" `
  --run-cleaners
```

##### macOS or Linux Terminal

Preview the update:

```bash
python3 backend/etl/etl_delta.py \
  --uof-file "data/UoF_database_1k_subset_100120_to_053126.xlsx" \
  --dry-run
```

Import and process the new records:

```bash
python3 backend/etl/etl_delta.py \
  --uof-file "data/UoF_database_1k_subset_100120_to_053126.xlsx" \
  --run-cleaners
```

#### Update Only the ARRIVE Together Dataset

##### Windows PowerShell

Preview the update:

```powershell
py backend\etl\etl_delta.py `
  --arrive-file "data\ARRIVE_Reports_Download_File_7_1_2026.xlsx" `
  --dry-run
```

Import and process the new records:

```powershell
py backend\etl\etl_delta.py `
  --arrive-file "data\ARRIVE_Reports_Download_File_7_1_2026.xlsx" `
  --run-cleaners
```

##### macOS or Linux Terminal

Preview the update:

```bash
python3 backend/etl/etl_delta.py \
  --arrive-file "data/ARRIVE_Reports_Download_File_7_1_2026.xlsx" \
  --dry-run
```

Import and process the new records:

```bash
python3 backend/etl/etl_delta.py \
  --arrive-file "data/ARRIVE_Reports_Download_File_7_1_2026.xlsx" \
  --run-cleaners
```

#### Optional Batch Size

The default insert batch size is 500 rows. A different batch size can be added to any command.

##### Windows PowerShell

```powershell
--batch-size 1000
```

##### macOS or Linux Terminal

```bash
--batch-size 1000
```

Replace the example filenames with the paths to the most recent source files.
<!-- 
## What it is

A tool for turning a New Jersey **Use of Force (UoF)** incident dataset — currently distributed as an Excel file (`data/UoF_database_1k_subset_100120_to_053126.xlsx`, ~1k rows) — into a clean, queryable MySQL database, with a browser-based UI for building filtered queries and paging through results. The repo name (`use_of_force_database_redesign_project`) signals this is a **redesign** of an existing/prior UoF database, not a greenfield build — the messiness handled in the ETL layer (misspellings, inconsistent formats, multi-value fields) is inherited from that source data.

The project has three layers, built in this order:

1.  **Database schema**  (MySQL) — done
2.  **ETL pipeline**  (Python) — Excel → raw staging table → cleaned/standardized table + lookup tables — done
3.  **Frontend**  (static HTML/JS) — query builder + results viewer — done, but  **not yet wired to a live backend**
4.  **API layer**  (`backend/api/`) — the piece that would connect frontend ⟷ MySQL —  **not built yet**  (only a  `.gitkeep`  placeholder exists)

So today the frontend and backend are functionally disconnected demos: the query builder produces SQL/JSON you copy out manually, and the results viewer accepts pasted JSON/CSV rather than a live API response.

----------

## Running the app locally

The database is a managed MySQL instance hosted on [Aiven](https://aiven.io) — there's no local MySQL server to install and no schema to build. `backend/database/schema_aiven.sql` and the seed scripts only need to be (re-)run against Aiven directly if you're standing up a *new* database; if one already exists, skip straight to configuring the connection.

Assumes only Python (3.9+) is already installed. Steps are the same on macOS and Windows except where noted.

### 1. Set up Python

Create a virtual environment and install dependencies:

- **macOS**:
  ```
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  ```
- **Windows** (PowerShell or Command Prompt):
  ```
  python -m venv .venv
  .venv\Scripts\activate
  pip install -r requirements.txt
  ```

Re-activate this environment (`source .venv/bin/activate` / `.venv\Scripts\activate`) in any new terminal you use for the remaining steps.

### 2. Configure the Aiven connection

`backend/config/db_config.py` builds its `DB_CONFIG` from environment variables (loaded from `backend/config/.env` via `python-dotenv` if present), so it's safe to commit — no real credentials live in it. Copy the example env file:

```
cp backend/config/.env.example backend/config/.env      # macOS
copy backend\config\.env.example backend\config\.env    # Windows
```

Then, from the Aiven Console → your MySQL service → **Overview → Quick connect**:

1. Note the host, port, user, and password shown there and fill in `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` in `.env` (leave `DB_NAME` as-is — it's already set up for this project's schema).
2. Download the service's **CA certificate** and save it as `backend/config/ca.pem` (leave `DB_SSL_CA_PATH=ca.pem` as-is).

Both `.env` and `ca.pem` are gitignored on purpose — never commit real credentials or the cert. (This is also how the app is configured on Render — see [Deploying to Render](#deploying-to-render) — except there the values are set directly as environment variables/secret files, no `.env` file involved.)

### 3. Load the incident data

Only needed if the target database doesn't already have data loaded, or you're importing a new Excel export. Safe to re-run either way — each script only processes rows it hasn't seen yet:

```
python backend/etl/import_script.py
python backend/etl/clean_and_populate.py
```

### 4. Start the API

```
python backend/api/bridge.py
```

Leave this running in its own terminal — it serves on `http://localhost:5001`. (macOS-specific note: this project deliberately avoids port 5000 because macOS's AirPlay Receiver listens there by default and silently intercepts requests meant for a local Flask server.)

### 5. Open the frontend

Open `frontend/uof_program_v2.html` directly in a browser (double-click it, or File → Open in your browser). No build step or dev server needed — it's a static file that talks to the API over HTTP from `file://`.

----------

## Data flow / pipeline

```
Excel (.xlsx)
   │  import_script.py
   ▼
uof_main_processing_table   (raw staging table, 1:1 with Excel columns, `processed` flag)
   │  clean_and_populate.py
   ▼
uof_main_data                (cleaned, standardized, one row per incident form)
   +  uof_dashboard_values_data   (tokenized multi-value fields, one row per token)
   +  exceptions_table            (rows/values that failed cleaning/standardization — logged, not dropped)

```

Two static reference tables support the cleaning step and are loaded once via seed scripts, independent of any Excel import:

-   `standard_values_table`  — maps raw value spellings/abbreviations → canonical form (e.g.  `"Sgt."`  →  `"Sergeant"`), used for single-value columns.
-   `uof_column_values_data`  — the master catalog of every valid value for every multi-value column, keyed by  `(Position_Id, Value_Id)`. Also used to validate tokens produced by the multi-value tokenizer.

### `import_script.py`  (Excel → staging)

-   Reads the Excel file with pandas, drops a  `KEEP/DROP`  scratch column from the source spreadsheet.
-   Renames ~40 Excel headers (human-readable, spaced) to the schema's  `Snake_Case`/underscore column names via an explicit  `col_map`  dict.
-   Converts  `Officer_In_Uniform`  from Python bool to MySQL tinyint (1/0).
-   Batch-inserts (100 rows/batch) into  `uof_main_processing_table`, converting NaN →  `None`  so MySQL gets real  `NULL`s.

### `clean_and_populate.py`  (staging → clean)

This is the core of the project and where most of the design decisions live. It:

1.  Pulls only unprocessed rows (`WHERE processed = 0`) from the staging table.
2.  Normalizes whitespace/casing on all text columns; coerces string forms of null (`"None"`,  `"NaN"`,  `"null"`,  `""`) to real NaN.
3.  Maps loose boolean-ish text (`yes/true/1/y`,  `no/false/0/n`) to 1/0 for a small set of genuinely-boolean columns (`Other_Officer_Involved`,  `Officer_In_Uniform`,  `Officer_Injuries_Injured`).
4.  Parses  `Incident_Date`  to a real date.
5.  Coerces a small set of genuinely-integer columns.
6.  Runs  `populate_subtables_and_standardize()`, which is the interesting part:
    -   **Single-value columns**  (`Video_Footage`,  `Officer_Race/Ethnicity`,  `Officer_Rank`,  `Officer_Gender`,  `Officer_Hospital_Treatment`) are looked up in  `standard_values_table`  and rewritten to their canonical spelling. Unmatched values are logged to  `exceptions_table`  rather than silently dropped.
    -   **Multi-value columns**  (most of the rest —  `Subject_Actions`,  `Location_Type`,  `Force_Type`, etc.) are comma-split into individual tokens, each becoming a row in  `uof_dashboard_values_data`. A small hardcoded list (`embedded_commas`) protects specific known values that legitimately contain a comma (e.g.  `"Disturbance (drinking, fighting, disorderly)"`) from being split by temporarily swapping the comma for  `||`  before splitting, then restoring it.
    -   Each token is validated against  `uof_column_values_data`; a miss is logged as an exception (with special-cased messaging for  `Subject_Age`, which is nominally numeric but stored as text because it can contain "Unknown"/"Under 18"/etc.).
7.  Writes the cleaned DataFrame into  `uof_main_data`  (all identifiers backtick-quoted, since several column names contain  `/`  or spaces — e.g.  `Officer_Race/Ethnicity`).
8.  Marks source rows  `processed = 1`  so re-running the script is idempotent / incremental.

**Notable bugs found and fixed during development** (documented inline as comments — worth knowing if you extend this):

-   A misspelling mismatch: both the raw source data and the seed table consistently spell "tightening" as  **"tighening"**  (missing a "t") for one  `Subject_Resistance`  value. The embedded-comma protection pattern originally used the correct spelling and silently never matched, so that value's comma was never protected. Fixed by matching the actual misspelling in use.
-   `Subject_Arrested`  was originally in the boolean-coercion list, which corrupted it to numpy floats (`1.0`/`0.0`) before it reached the tokenizer/lookup stage (which expects the literal strings  `'True'`/`'False'`/`'Not Provided'`) — this silently broke matching for ~986 rows. Fixed by removing it from  `bool_cols`  and letting it flow through as plain text into the standard lookup pipeline like other  `pos_map`  columns.
-   `Subject_Age`  was originally in the integer-coercion list, which wiped every non-single-integer entry (ranges, "Unknown", "Under 18") to NaN before the per-token validator ever got to see it — even though that validator already has bespoke logic to check each token. Fixed by removing it from  `int_cols`.
-   Backtick-quoting was required for  `INSERT`  column lists because some column names contain  `/`  and spaces — unquoted, MySQL threw a syntax error.
-   Raw pandas/numpy scalar types (e.g.  `numpy.int64`) weren't accepted directly by  `mysql-connector-python`  in this environment; added a  `to_native()`  coercion step before insert.

This trail of comments is itself a good signal for an AI picking up the project: it explains _why_ the code looks the way it does, not just what it does — preserve that context if refactoring.

----------

## Database schema (`backend/database/schema.sql`)

MySQL 8, `utf8mb4`/`utf8mb4_0900_ai_ci`. Six tables:


| Table | Purpose |
|-------|---------|
| `uof_main_processing_table` | Raw staging table, 1:1 with Excel columns, plus a `processed` flag. Indexed on `Form_ID` and `processed`. |
| `uof_main_data` | Cleaned/standardized "final" table — same shape as staging minus `processed`. This is the table the frontend's query builder currently targets in its generated SQL comments (though the UI itself says `uof_main_processing_table` — see note below). |
| `uof_dashboard_values_data` | Tokenized multi-value fields: one row per `(Form_Id, Position_Id, Value_Id, Column_Value)` — the normalized/exploded form of columns like `Subject_Actions` that can hold multiple selections per incident. |
| `uof_column_values_data` | Static catalog of every valid value per column position — reference/lookup data, not incident data. |
| `standard_values_table` | Raw-value → standard-value synonym map used during cleaning. |
| `exceptions_table` | Audit log of values that failed cleaning/standardization, keyed to the original form/column, with a human-readable `reason`. Nothing is silently dropped — everything that doesn't clean cleanly is preserved here for review. |
Design choices worth calling out:

-   **Text-typed fields deliberately overridden from more "natural" numeric/boolean types.**  E.g.  `Subject_Arrested`  and  `Subject_Age`  are  `text`  in  `uof_main_data`/`uof_main_processing_table`, annotated inline with  `-- changed from tinyint(1)/int to text by Reuben`  — because the real-world data isn't strictly boolean/numeric (arrest status has a  `"Not Provided"`  state; age can be a range or "Unknown").
-   **Wide, denormalized  `uof_main_data`/`uof_main_processing_table`**  (45 columns) rather than a fully normalized incident/officer/subject schema — this seems to mirror the source Excel form 1:1 for traceability, with normalization happening only for the genuinely multi-value fields (via  `uof_dashboard_values_data`).
-   Schema file is a literal  `mysqldump`  structure dump (`CREATE DATABASE IF NOT EXISTS`  +  `DROP TABLE IF EXISTS`  +  `CREATE TABLE`) — meant to be run once to build the DB from scratch, not a migration-managed schema.

----------

## Frontend (`frontend/`)

Two **standalone, dependency-free HTML files** (single-file, inline `<style>`/`<script>`, no build step, no framework) — designed to be opened directly in a browser or served as static files.

### `query-builder.html`

A form-based SQL query constructor over the 45-column schema.

-   **"Launch gate" pattern**: the Build button stays disabled until the user provides  _either_  an incident date range  _or_  at least one Incident ID — a deliberate guardrail against generating an unbounded  `SELECT *`  over the whole table. This mirrors a real operational concern (don't let someone accidentally dump the entire incident table).
-   Fields are grouped into 7 collapsible sections (Geography & agency, Incident & force, Subject, Officer, Environment, Video, Records & IDs) mirroring the schema groupings used later in the results viewer, using  `<details>`/`<summary>`  for progressive disclosure with live "N set" badges per group.
-   Field types:  `tags`  (chip-based multi-value → SQL  `IN (...)`),  `tags-num`  (numeric IDs),  `range`  (min/max →  `BETWEEN`/`>=`/`<=`).
-   A "Partial text match (LIKE)" toggle switches text-tag filters from exact  `IN`  matching to  `OR`-chained  `LIKE '%value%'`  clauses.
-   Live-renders both a human-readable criteria list and syntax-highlighted SQL as filters are added; "Copy SQL" and "Copy JSON" buttons let the user hand the query off elsewhere (there's no live execution — this is intentionally just a query  _constructor_).
-   Values are inserted via naive string interpolation with manual quote-escaping (`sqlStr`  doubles single quotes) — acceptable for a client-side query  _previewer_  that a human copies out, but  **not safe to point directly at a live DB connection without parameterization**  if/when the API layer executes it.

### `results-viewer.html`

A paged record/table viewer for result sets.

-   No live query execution — instead has a "Load results" panel where you paste a JSON array or CSV/TSV, which gets column-mapped onto the canonical 45-column schema (case/punctuation-insensitive header matching via a normalized lookup, so  `officer race ethnicity`,  `Officer_Race/Ethnicity`, etc. all resolve to the same canonical column).
-   Two view modes:  **Record**  (one incident per screen, grouped into the same 7 categories as the query builder, with keyboard paging — arrow keys/Home/End) and  **Table**  (dense sortable-looking grid with a sticky header and frozen row-number column).
-   Ships with 4 clearly-synthetic sample rows (`SAMPLE-0001`  etc., "Sample Township PD") shown by default so the page isn't blank before any data is loaded — explicitly labeled as sample data in the UI copy.
-   Export to CSV (hand-rolled CSV writer, BOM-prefixed for Excel compatibility) and to XLSX (lazy-loads SheetJS from a CDN on first use, falls back to CSV export if that fails).

Both pages share a consistent visual language (teal/ink palette, monospace for schema/code references, card-based layout) despite being two separate files with no shared CSS/JS — they were clearly designed as a matched pair, not just independently.

----------

## Config & environment

-   `backend/config/db_config.py`  holds  `DB_CONFIG`  (host/port/user/password/database, plus Aiven's SSL settings), built entirely from environment variables (`DB_HOST`,  `DB_PORT`,  `DB_USER`,  `DB_PASSWORD`,  `DB_NAME`,  `DB_SSL_CA_PATH`) — no real credentials in the file itself, so it's committed.  `.env.example`  is the checked-in template for local dev, copied to  `.env`  (gitignored) and loaded via  `python-dotenv`. `import_script.py`, `clean_and_populate.py`, and `bridge.py` all  `sys.path.append`  their way to  `../config`  to import it.
-   `backend/config/ca.pem`  is the Aiven-issued CA certificate, gitignored, downloaded once from the Aiven Console and referenced by `db_config.py`'s `ssl_ca` (a relative  `DB_SSL_CA_PATH`  is resolved relative to the config directory, not the process's working directory, so an absolute path — e.g. Render's mounted Secret File — also works unchanged).
-   `db_config.py`  also sets  `use_pure: True`  — the default C-extension connector unconditionally calls  `SSL_CTX_set_default_verify_paths()`, which fails on macOS (no Linux-style default cert store paths); the pure-Python implementation avoids that call and uses `ssl_ca` directly.
-   Python deps pinned in  `requirements.txt`:  `pandas`,  `numpy`,  `mysql-connector-python`,  `openpyxl`  (Excel reading),  `flask`/`flask-cors`  (API),  `python-dotenv`  (loads  `.env`  locally),  `gunicorn`  (production WSGI server, used on Render),  `et_xmlfile`/`python-dateutil`/`six`  (transitive).
-   No  `package.json`/Node tooling anywhere — frontend is deliberately zero-build.

----------

## Deploying to Render

The repo includes a `render.yaml` [Blueprint](https://render.com/docs/blueprint-spec) that defines a single web service (`uof-webapp-api`) running `bridge.py` behind `gunicorn`.

1. In the Render dashboard: **New → Blueprint**, point it at this repo/branch. Render reads `render.yaml` and creates the service.
2. The blueprint declares `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` with `sync: false` — Render will prompt you to fill these in (same values as your local `.env`). `FLASK_SECRET_KEY` is auto-generated.
3. **CA cert**: `render.yaml` can't upload file contents, so add it by hand once — in the service's **Environment** tab, add a **Secret File** named `ca.pem` with the path `/etc/secrets/ca.pem` and paste in the contents of your local `backend/config/ca.pem`. The blueprint already sets `DB_SSL_CA_PATH=/etc/secrets/ca.pem` to match.
4. Deploy. Render runs `pip install -r requirements.txt` then `gunicorn backend.api.bridge:app`, binding to the `$PORT` it provides automatically.
5. Once live, update `BRIDGE_URL` in `frontend/uof_program_v2.html` (currently hardcoded to `http://localhost:5001/query`) to point at the deployed service's `/query` URL.

If you'd rather configure it by hand instead of via the blueprint: **New → Web Service**, build command `pip install -r requirements.txt`, start command `gunicorn backend.api.bridge:app`, and set the same env vars/secret file as above.

----------

## What's explicitly unfinished

Per the README's own annotations:

-   `backend/api/`  — the layer that would take a query from the frontend, execute it against MySQL, and return JSON to the results viewer. Currently just a  `.gitkeep`.
-   `docs/`  contains a Project Charter PDF and two Word docs (`Query_and_Response_-_Input_Form_design.docx`,  `Query_to_Claude_-_Return_Data_Form.docx`) describing the intended query/response form design — these weren't machine-readable in this pass (no  `poppler`/docx parser available) but are likely the authoritative spec for what the API layer should look like, if you want an AI to consult them.

If you're handing this to another AI to continue the work, the natural next step is clearly **building the API layer** to connect `query-builder.html`'s generated query to a live MySQL execution, returning JSON that `results-viewer.html` can consume directly instead of via copy-paste — at which point the query builder's string-interpolated SQL construction should be replaced with parameterized queries server-side.

---
 -->
