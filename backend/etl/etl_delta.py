# =====================================================================
# etl_delta.py — Append-only delta loader for UoF and ARRIVE Together
#
# Purpose
#   1. Read one or both incoming Excel exports.
#   2. Identify UoF rows by Form_ID and ARRIVE rows by Random_ID.
#   3. Insert only unseen UoF rows into uof_main_processing_table.
#   4. Insert only unseen ARRIVE rows into arrive_main_data.
#   5. Optionally run the existing UoF cleaner and ARRIVE tokenizer.
#
# Important
#   UoF Incident_ID is NOT unique. One incident can contain several Form_ID
#   rows, so UoF delta detection must use Form_ID.
#
# Examples
#   python etl_delta.py --uof-file "UoF_July_2026.xlsx" --dry-run
#   python etl_delta.py --arrive-file "ARRIVE_July_2026.xlsx" --dry-run
#   python etl_delta.py --uof-file "UoF_July_2026.xlsx" \
#       --arrive-file "ARRIVE_July_2026.xlsx" --run-cleaners
# =====================================================================

from __future__ import annotations

import argparse
import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import mysql.connector as mysql_connector
except ModuleNotFoundError:
    mysql_connector = None
import numpy as np
import pandas as pd

CONFIG_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "config",
)
sys.path.append(CONFIG_DIR)


def get_db_config() -> dict[str, Any]:
    """Load database settings only when a database operation is requested."""
    from db_config import DB_CONFIG

    return DB_CONFIG


COL_MAP = {
    "Form ID": "Form_ID",
    "Agency Name": "Agency_Name",
    "Officer Name": "Officer_Name",
    "User ID": "User_ID",
    "Incident ID": "Incident_ID",
    "Report Number": "Report_Number",
    "Incident Case Number": "Incident_Case",
    "Incident Date": "Incident_Date",
    "Other Officer Involved": "Other_Officer_Involved",
    "Officer In Uniform": "Officer_In_Uniform",
    "Incident Municipality": "Incident_Municipality",
    "Indoor Or Outdoor": "Indoor_Or_Outdoor",
    "Incident Weather": "Incident_Weather",
    "Video Footage": "Video_Footage",
    "Video Type": "Video_Type",
    "Incident Lighting": "Incident_Lighting",
    "Location Type": "Location_Type",
    "Incident Type": "Incident_Type",
    "Contact Origin": "Contact_Origin",
    "Planned Contact": "Planned_Contact",
    "Officer Age": "Officer_Age",
    "Officer Race/Ethnicity": "Officer_Race/Ethnicity",
    "Officer Rank": "Officer_Rank",
    "Officer Gender": "Officer_Gender",
    "Officer Injury Type": "Officer_Injury_Type",
    "Officer Injuries Injured": "Officer_Injuries_Injured",
    "Officer Medical Treatment": "Officer_Medical_Treatment",
    "Officer Hospital Treatment": "Officer_Hospital_Treatment",
    "Total Sub Injured In Incident": "Total_Sub_Injured",
    "Subject Injured In Incident": "Subject_Injured",
    "Subject Injured Prior To Incident": "Subject_Injured_Prior",
    "Perceived Condition Of Subject": "Perceived_Condition",
    "Subject Actions": "Subject_Actions",
    "Subject Resistance": "Subject_Resistance",
    "Subject Medical Treatment": "Subject_Medical Treatment",
    "Subject Injury Type": "Subject_Injury Type",
    "Subject Arrested": "Subject_Arrested",
    "Reason Subject Not Arrested": "Reason_Not_Arrested",
    "Subject Type": "Subject_Type",
    "Subject Age": "Subject_Age",
    "Subject Race/Ethnicity": "Subject_Race/Ethnicity",
    "Subject Gender": "Subject_Gender",
    "Force Type": "Force_Type",
    "Incident Year": "Incident_Year",
}

# Columns accepted by uof_main_processing_table, excluding its generated id and
# its processed flag (processed defaults to 0 in the database).
PROCESSING_COLUMNS = [
    "Form_ID",
    "County",
    "Agency_Name",
    "Officer_Name",
    "User_ID",
    "Incident_ID",
    "Report_Number",
    "Incident_Case",
    "Incident_Date",
    "Other_Officer_Involved",
    "Officer_In_Uniform",
    "Incident_Municipality",
    "Indoor_Or_Outdoor",
    "Incident_Weather",
    "Video_Footage",
    "Video_Type",
    "Incident_Lighting",
    "Location_Type",
    "Incident_Type",
    "Contact_Origin",
    "Planned_Contact",
    "Officer_Age",
    "Officer_Race/Ethnicity",
    "Officer_Rank",
    "Officer_Gender",
    "Officer_Injury_Type",
    "Officer_Injuries_Injured",
    "Officer_Medical_Treatment",
    "Officer_Hospital_Treatment",
    "Total_Sub_Injured",
    "Subject_Injured",
    "Subject_Injured_Prior",
    "Perceived_Condition",
    "Subject_Actions",
    "Subject_Resistance",
    "Subject_Medical Treatment",
    "Subject_Injury Type",
    "Subject_Arrested",
    "Reason_Not_Arrested",
    "Subject_Type",
    "Subject_Age",
    "Under_18",
    "Subject_Race/Ethnicity",
    "Subject_Gender",
    "Force_Type",
    "Incident_Year",
]

INTEGER_COLUMNS = ["Form_ID", "User_ID", "Total_Sub_Injured", "Incident_Year"]

# These columns are stored as text in the processing table.  Explicitly
# converting them prevents pandas from inferring a one-row or all-True/all-False
# Excel column as bool and mysql-connector then storing it as 1/0.
NON_TEXT_COLUMNS = {
    "Form_ID",  # int
    "User_ID",  # int
    "Incident_Date",  # date
    "Other_Officer_Involved",  # bool (0,1,Null)
    "Officer_In_Uniform",  # bool (1,0,Null)
    "Officer_Injuries_Injured",  # bool (0,1,Null)
    "Total_Sub_Injured",  # int or Null
    "Under_18",  # bool (0,1,Null)
    "Incident_Year",  # int
}
TEXT_COLUMNS = [col for col in PROCESSING_COLUMNS if col not in NON_TEXT_COLUMNS]


def normalize_text_value(value: Any) -> str | None:
    """Preserve literal text values, including Excel booleans as True/False."""
    if pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return "True" if bool(value) else "False"
    return str(value).strip()


def normalize_officer_in_uniform(value: Any) -> int | None:
    if pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return int(value)
    text = str(value).strip().lower()
    if text in {"1", "1.0", "true", "yes", "y"}:
        return 1
    if text in {"0", "0.0", "false", "no", "n"}:
        return 0
    if text in {"", "not provided", "none", "null", "nan"}:
        return None
    raise ValueError(f"Unexpected Officer_In_Uniform value: {value!r}")


def load_source(excel_file: str | Path) -> pd.DataFrame:
    """Read and validate the raw source while preserving downstream cleaning."""
    excel_path = Path(excel_file).expanduser().resolve()
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    df = pd.read_excel(excel_path)
    original_rows = len(df)

    # Some subset workbooks retain a very large used range after rows are cleared.
    # Remove completely empty rows before validation/delta comparison.
    df = df.dropna(how="all").copy()
    blank_rows = original_rows - len(df)
    if blank_rows:
        print(f"  Removed {blank_rows:,} completely blank worksheet rows")

    df = df.drop(columns=["KEEP/DROP"], errors="ignore")
    df = df.rename(columns=COL_MAP)

    if "Form_ID" not in df.columns:
        raise ValueError("Source file is missing the required 'Form ID' column.")

    unexpected = [col for col in df.columns if col not in PROCESSING_COLUMNS]
    if unexpected:
        print(f"  WARNING: dropping columns not present in processing schema: {unexpected}")
        df = df.drop(columns=unexpected)

    missing_columns = [col for col in PROCESSING_COLUMNS if col not in df.columns]
    for col in missing_columns:
        df[col] = None
    if missing_columns:
        print(f"  Added {len(missing_columns)} missing schema column(s) as NULL: {missing_columns}")

    # Under_18 is derived from Subject_Age by clean_and_populate.py.
    df["Under_18"] = None

    # Remove rows without a usable Form_ID and fail loudly on malformed IDs.
    form_numeric = pd.to_numeric(df["Form_ID"], errors="coerce")
    missing_form_mask = form_numeric.isna()
    if missing_form_mask.any():
        print(f"  WARNING: dropping {int(missing_form_mask.sum()):,} row(s) with no Form_ID")
        df = df.loc[~missing_form_mask].copy()
        form_numeric = form_numeric.loc[~missing_form_mask]

    non_integer_mask = (form_numeric % 1) != 0
    if non_integer_mask.any():
        samples = df.loc[non_integer_mask, "Form_ID"].head(5).tolist()
        raise ValueError(f"Form_ID must be an integer. Bad sample values: {samples}")
    df["Form_ID"] = form_numeric.astype("Int64")

    duplicate_mask = df.duplicated(subset=["Form_ID"], keep=False)
    if duplicate_mask.any():
        duplicate_ids = (
            df.loc[duplicate_mask, "Form_ID"].astype(str).drop_duplicates().head(10).tolist()
        )
        raise ValueError(
            "The incoming file contains duplicate Form_ID values. Resolve them "
            f"before loading. Sample IDs: {duplicate_ids}"
        )

    # Keep database TEXT fields as literal strings. This is especially important
    # for small delta files where Excel values such as True/False may otherwise
    # be inferred as booleans and inserted into TEXT columns as 1/0.
    for col in TEXT_COLUMNS:
        if col in df.columns:
            df[col] = df[col].map(normalize_text_value)

    if "Officer_In_Uniform" in df.columns:
        df["Officer_In_Uniform"] = df["Officer_In_Uniform"].map(
            normalize_officer_in_uniform
        )

    if "Incident_Date" in df.columns:
        df["Incident_Date"] = pd.to_datetime(
            df["Incident_Date"], errors="coerce"
        ).dt.date

    for col in INTEGER_COLUMNS:
        if col == "Form_ID" or col not in df.columns:
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    # Exact schema order makes inserts deterministic and avoids source-column drift.
    return df[PROCESSING_COLUMNS]


def chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def fetch_existing_form_ids(
    cursor: Any,
    incoming_ids: list[int],
    query_chunk_size: int = 10_000,
) -> tuple[set[int], set[int]]:
    """Find incoming Form_IDs already present in final or staging tables."""
    existing_main: set[int] = set()
    existing_processing: set[int] = set()

    for batch in chunks(incoming_ids, query_chunk_size):
        placeholders = ", ".join(["%s"] * len(batch))

        cursor.execute(
            f"SELECT Form_ID FROM uof_main_data "
            f"WHERE Form_ID IN ({placeholders})",
            tuple(batch),
        )
        existing_main.update(int(row[0]) for row in cursor.fetchall() if row[0] is not None)

        cursor.execute(
            f"SELECT Form_ID FROM uof_main_processing_table "
            f"WHERE Form_ID IN ({placeholders})",
            tuple(batch),
        )
        existing_processing.update(
            int(row[0]) for row in cursor.fetchall() if row[0] is not None
        )

    return existing_main, existing_processing


def to_native(value: Any) -> Any:
    """Convert pandas/numpy values to types accepted by mysql-connector."""
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if isinstance(value, np.generic):
        return value.item()
    return value


def insert_processing_rows(
    cursor: Any,
    conn: Any,
    df: pd.DataFrame,
    batch_size: int,
) -> int:
    columns = list(df.columns)
    quoted_columns = ", ".join(f"`{col}`" for col in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO uof_main_processing_table ({quoted_columns}) "
        f"VALUES ({placeholders})"
    )

    rows = [
        tuple(to_native(value) for value in row)
        for row in df.itertuples(index=False, name=None)
    ]

    inserted = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        try:
            cursor.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
            print(f"  Staged {inserted:,} of {len(rows):,} new row(s)")
        except mysql_connector.Error:
            conn.rollback()
            raise
    return inserted



# ARRIVE source-to-schema column names.
ARRIVE_COL_MAP = {
    "Year of Date_of_Incident": "Incident_Year",
    "ARRIVE Model": "Arrive_Model",
    "outreach_attempts": "Outreach_Attempts",
    "30_Day_Outcomes": "Day_30_Outcomes",
}

ARRIVE_COLUMNS = [
    "Random_ID",
    "Incident_Year",
    "Arrive_Model",
    "Behaviors_Indicated_Prior_to_Arrival",
    "Other_Individuals_on_Scene",
    "Law_Enforcement_Observed_Behavior",
    "Law_Enforcement_Outcomes",
    "Outreach_Attempts",
    "Mental_Health_Outcome",
    "Day_30_Outcomes",
]

ARRIVE_INTEGER_COLUMNS = [
    "Random_ID",
    "Incident_Year",
    "Outreach_Attempts",
]

# These separators match import_arrive_data.py and tokenize_arrive_data.py.
ARRIVE_MULTI_VALUE_COLS = {
    "Arrive_Model": " & ",
    "Behaviors_Indicated_Prior_to_Arrival": ",",
    "Other_Individuals_on_Scene": ",",
    "Law_Enforcement_Observed_Behavior": ",",
    "Law_Enforcement_Outcomes": ",",
    "Mental_Health_Outcome": ",",
    "Day_30_Outcomes": ",",
}


def parse_arrive_list_cell(value: Any, separator: str) -> list[str]:
    """Decode ARRIVE list-like cells and return clean text tokens."""
    if pd.isna(value):
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]

    text = str(value).strip()
    if not text:
        return []

    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        parsed = None

    if isinstance(parsed, (list, tuple)):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if parsed is not None:
        parsed_text = str(parsed).strip()
        return [parsed_text] if parsed_text else []

    return [token.strip() for token in text.split(separator) if token.strip()]


def load_arrive_source(excel_file: str | Path) -> pd.DataFrame:
    """Read and validate an ARRIVE Reports Download File export."""
    excel_path = Path(excel_file).expanduser().resolve()
    if not excel_path.exists():
        raise FileNotFoundError(f"ARRIVE Excel file not found: {excel_path}")

    df = pd.read_excel(excel_path)
    original_rows = len(df)
    df = df.dropna(how="all").copy()
    blank_rows = original_rows - len(df)
    if blank_rows:
        print(f"  Removed {blank_rows:,} completely blank worksheet rows")

    df = df.rename(columns=ARRIVE_COL_MAP)

    if "Random_ID" not in df.columns:
        raise ValueError(
            "ARRIVE source file is missing the required 'Random_ID' column."
        )

    unexpected = [col for col in df.columns if col not in ARRIVE_COLUMNS]
    if unexpected:
        print(
            "  WARNING: dropping ARRIVE columns not present in the database "
            f"schema: {unexpected}"
        )
        df = df.drop(columns=unexpected)

    missing_columns = [col for col in ARRIVE_COLUMNS if col not in df.columns]
    for col in missing_columns:
        df[col] = None
    if missing_columns:
        print(
            f"  Added {len(missing_columns)} missing ARRIVE schema column(s) "
            f"as NULL: {missing_columns}"
        )

    random_numeric = pd.to_numeric(df["Random_ID"], errors="coerce")
    missing_id_mask = random_numeric.isna()
    if missing_id_mask.any():
        print(
            f"  WARNING: dropping {int(missing_id_mask.sum()):,} ARRIVE row(s) "
            "with no Random_ID"
        )
        df = df.loc[~missing_id_mask].copy()
        random_numeric = random_numeric.loc[~missing_id_mask]

    non_integer_mask = (random_numeric % 1) != 0
    if non_integer_mask.any():
        samples = (
            df.loc[non_integer_mask, "Random_ID"].head(5).tolist()
        )
        raise ValueError(
            f"Random_ID must be an integer. Bad sample values: {samples}"
        )
    df["Random_ID"] = random_numeric.astype("Int64")

    duplicate_mask = df.duplicated(subset=["Random_ID"], keep=False)
    if duplicate_mask.any():
        duplicate_ids = (
            df.loc[duplicate_mask, "Random_ID"]
            .astype(str)
            .drop_duplicates()
            .head(10)
            .tolist()
        )
        raise ValueError(
            "The incoming ARRIVE file contains duplicate Random_ID values. "
            f"Resolve them before loading. Sample IDs: {duplicate_ids}"
        )

    for col in ["Incident_Year", "Outreach_Attempts"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    for col, separator in ARRIVE_MULTI_VALUE_COLS.items():
        display_separator = ", " if separator == "," else separator
        df[col] = df[col].map(
            lambda value, sep=separator, display=display_separator: (
                display.join(parse_arrive_list_cell(value, sep)) or None
            )
        )

    # Keep all remaining text fields as literal strings.
    for col in ARRIVE_COLUMNS:
        if col not in ARRIVE_INTEGER_COLUMNS and col not in ARRIVE_MULTI_VALUE_COLS:
            df[col] = df[col].map(normalize_text_value)

    return df[ARRIVE_COLUMNS]


def fetch_existing_random_ids(
    cursor: Any,
    incoming_ids: list[int],
    query_chunk_size: int = 10_000,
) -> set[int]:
    """Find incoming Random_ID values already present in arrive_main_data."""
    existing: set[int] = set()

    for batch in chunks(incoming_ids, query_chunk_size):
        placeholders = ", ".join(["%s"] * len(batch))
        cursor.execute(
            f"SELECT Random_ID FROM arrive_main_data "
            f"WHERE Random_ID IN ({placeholders})",
            tuple(batch),
        )
        existing.update(
            int(row[0]) for row in cursor.fetchall() if row[0] is not None
        )

    return existing


def insert_arrive_rows(
    cursor: Any,
    conn: Any,
    df: pd.DataFrame,
    batch_size: int,
) -> int:
    """Insert ARRIVE rows in batches, isolating any row-level failures."""
    columns = list(df.columns)
    quoted_columns = ", ".join(f"`{col}`" for col in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO arrive_main_data ({quoted_columns}) "
        f"VALUES ({placeholders})"
    )

    rows = [
        tuple(to_native(value) for value in row)
        for row in df.itertuples(index=False, name=None)
    ]

    inserted = 0
    failed_rows: list[tuple[int, str]] = []

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        try:
            cursor.executemany(sql, batch)
            conn.commit()
            inserted += len(batch)
            print(f"  Inserted {inserted:,} of {len(rows):,} ARRIVE row(s)")
        except mysql_connector.Error as batch_error:
            conn.rollback()
            print(
                f"  Batch {start + 1}-{start + len(batch)} failed "
                f"({batch_error}); checking rows individually"
            )
            for offset, row in enumerate(batch):
                try:
                    cursor.execute(sql, row)
                    conn.commit()
                    inserted += 1
                except mysql_connector.Error as row_error:
                    conn.rollback()
                    source_row = start + offset + 1
                    failed_rows.append((source_row, str(row_error)))
                    print(f"    ARRIVE source row {source_row} failed: {row_error}")

    if failed_rows:
        sample = "; ".join(
            f"row {row_number}: {error}"
            for row_number, error in failed_rows[:5]
        )
        raise RuntimeError(
            f"{len(failed_rows)} ARRIVE row(s) failed to insert. "
            f"Sample errors: {sample}"
        )

    return inserted


def run_uof_cleaner() -> None:
    """Invoke the existing UoF cleaner after staging, when requested."""
    uof_etl_dir = Path(__file__).resolve().parent / "uof_etl"
    sys.path.insert(0, str(uof_etl_dir))
    cleaner = importlib.import_module("clean_and_populate")
    cleaner.clean_uof_data()


def run_arrive_tokenizer() -> None:
    """Run the existing ARRIVE tokenizer as a separate Python process."""
    tokenizer_path = (
        Path(__file__).resolve().parent
        / "arrive_etl"
        / "tokenize_arrive_data.py"
    )
    subprocess.run(
        [sys.executable, str(tokenizer_path)],
        check=True,
    )


def run_uof_delta(
    excel_file: str,
    batch_size: int,
    dry_run: bool,
) -> None:
    print("\n" + "=" * 68)
    print(f"UoF append-only delta load: {Path(excel_file).name}")
    print("Identity key: Form_ID")
    print("Target table: uof_main_processing_table")
    print("=" * 68)

    print("\n[UoF 1/3] Reading and validating source file...")
    df = load_source(excel_file)
    print(f"  {len(df):,} valid UoF source row(s)")

    if df.empty:
        print("  Nothing to compare or load.")
        return

    print("\n[UoF 2/3] Checking existing Form_ID values...")
    conn = mysql_connector.connect(**get_db_config())
    cursor = conn.cursor()

    try:
        incoming_ids = [int(value) for value in df["Form_ID"].tolist()]
        existing_main, existing_processing = fetch_existing_form_ids(
            cursor, incoming_ids
        )
        existing_any = existing_main | existing_processing

        df_new = df.loc[~df["Form_ID"].isin(existing_any)].copy()
        skipped = len(df) - len(df_new)

        print(f"  Already in uof_main_data: {len(existing_main):,}")
        print(
            "  Already in uof_main_processing_table: "
            f"{len(existing_processing):,}"
        )
        print(f"  New UoF Form_ID rows: {len(df_new):,}")
        print(f"  Existing UoF rows skipped: {skipped:,}")

        if dry_run:
            print("\n[UoF 3/3] Dry run: no UoF rows inserted.")
        elif df_new.empty:
            print("\n[UoF 3/3] No new UoF rows to stage.")
        else:
            print("\n[UoF 3/3] Staging rows for the UoF cleaning pipeline...")
            insert_processing_rows(cursor, conn, df_new, batch_size)
    finally:
        cursor.close()
        conn.close()


def run_arrive_delta(
    excel_file: str,
    batch_size: int,
    dry_run: bool,
) -> None:
    print("\n" + "=" * 68)
    print(f"ARRIVE append-only delta load: {Path(excel_file).name}")
    print("Identity key: Random_ID")
    print("Target table: arrive_main_data")
    print("=" * 68)

    print("\n[ARRIVE 1/3] Reading and validating source file...")
    df = load_arrive_source(excel_file)
    print(f"  {len(df):,} valid ARRIVE source row(s)")

    if df.empty:
        print("  Nothing to compare or load.")
        return

    print("\n[ARRIVE 2/3] Checking existing Random_ID values...")
    conn = mysql_connector.connect(**get_db_config())
    cursor = conn.cursor()

    try:
        incoming_ids = [int(value) for value in df["Random_ID"].tolist()]
        existing = fetch_existing_random_ids(cursor, incoming_ids)
        df_new = df.loc[~df["Random_ID"].isin(existing)].copy()
        skipped = len(df) - len(df_new)

        print(f"  Already in arrive_main_data: {len(existing):,}")
        print(f"  New ARRIVE Random_ID rows: {len(df_new):,}")
        print(f"  Existing ARRIVE rows skipped: {skipped:,}")

        if dry_run:
            print("\n[ARRIVE 3/3] Dry run: no ARRIVE rows inserted.")
        elif df_new.empty:
            print("\n[ARRIVE 3/3] No new ARRIVE rows to insert.")
        else:
            print("\n[ARRIVE 3/3] Inserting new ARRIVE rows...")
            insert_arrive_rows(cursor, conn, df_new, batch_size)
    finally:
        cursor.close()
        conn.close()


def main(
    uof_file: str | None,
    arrive_file: str | None,
    batch_size: int,
    dry_run: bool,
    run_cleaners: bool,
) -> None:
    if mysql_connector is None:
        raise ModuleNotFoundError(
            "mysql-connector-python is required for database operations. "
            "Install it with: pip install mysql-connector-python"
        )

    print("\nCombined UoF and ARRIVE delta workflow")
    print(f"Mode: {'DRY RUN' if dry_run else 'LOAD'}")

    if uof_file:
        run_uof_delta(uof_file, batch_size, dry_run)

    if arrive_file:
        run_arrive_delta(arrive_file, batch_size, dry_run)

    if run_cleaners and dry_run:
        print("\nCleaners were requested but are not run during dry-run mode.")
    elif run_cleaners:
        if uof_file:
            print("\nRunning UoF clean_and_populate.py...")
            run_uof_cleaner()
        if arrive_file:
            print("\nRunning ARRIVE tokenize_arrive_data.py...")
            run_arrive_tokenizer()
    else:
        print("\nCleaning/tokenization not requested.")
        if uof_file:
            print("  UoF: run with --run-cleaners to process staged rows.")
        if arrive_file:
            print("  ARRIVE: run with --run-cleaners to rebuild token values.")

    print("\nCombined delta workflow complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Load only unseen UoF and/or ARRIVE records, optionally running "
            "their existing cleaning/tokenization workflows afterward."
        )
    )
    parser.add_argument(
        "--uof-file",
        help="Path to an incoming UoF Excel file",
    )
    parser.add_argument(
        "--arrive-file",
        help="Path to an incoming ARRIVE Reports Download File",
    )
    parser.add_argument(
        "--file",
        dest="legacy_uof_file",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows committed per insert batch (default: 500)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report both deltas without inserting or running cleaners",
    )
    parser.add_argument(
        "--run-cleaners",
        "--run-cleaner",
        dest="run_cleaners",
        action="store_true",
        help=(
            "Run clean_and_populate.py for UoF and "
            "tokenize_arrive_data.py for ARRIVE"
        ),
    )
    args = parser.parse_args()

    uof_file = args.uof_file or args.legacy_uof_file
    if args.uof_file and args.legacy_uof_file:
        parser.error("Use --uof-file or --file, not both.")
    if not uof_file and not args.arrive_file:
        parser.error("Provide --uof-file, --arrive-file, or both.")
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than zero")

    main(
        uof_file=uof_file,
        arrive_file=args.arrive_file,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        run_cleaners=args.run_cleaners,
    )
