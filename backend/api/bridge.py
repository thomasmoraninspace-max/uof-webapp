import os
import sys
from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector


sys.path.append(os.path.join(os.path.dirname(__file__), "..", "config"))
from db_config import DB_CONFIG

# Cap on rows returned by any /query/* route -- the only guard against an
# unbounded SELECT * when a request carries no filters at all.
MAX_ROWS = 5000

# ─────────────────────────────────────────────────────────────────────
# UoF (uof_main_data) query config
# ─────────────────────────────────────────────────────────────────────

# Whitelist of real uof_main_data columns. Filter keys come straight from the
# request body and get spliced into the SQL as identifiers (not parameterizable
# like values are), so anything not in this set must be rejected before it
# touches the query string.
ALLOWED_COLUMNS = {
    "Form_ID", "County", "Agency_Name", "Officer_Name", "User_ID", "Incident_ID",
    "Report_Number", "Incident_Case", "Incident_Date", "Other_Officer_Involved",
    "Officer_In_Uniform", "Incident_Municipality", "Indoor_Or_Outdoor", "Incident_Weather",
    "Video_Footage", "Video_Type", "Incident_Lighting", "Location_Type", "Incident_Type",
    "Contact_Origin", "Planned_Contact", "Officer_Age", "Officer_Race/Ethnicity",
    "Officer_Rank", "Officer_Gender", "Officer_Injury_Type", "Officer_Injuries_Injured",
    "Officer_Medical_Treatment", "Officer_Hospital_Treatment", "Total_Sub_Injured",
    "Subject_Injured", "Subject_Injured_Prior", "Perceived_Condition", "Subject_Actions",
    "Subject_Resistance", "Subject_Medical Treatment", "Subject_Injury Type",
    "Subject_Arrested", "Reason_Not_Arrested", "Subject_Type", "Subject_Age",
    "Subject_Race/Ethnicity", "Subject_Gender", "Force_Type", "Incident_Year",
    "Under_18",
}

# uof_main_data has multiple rows (one per officer/subject) per Incident_ID, so
# any explicit column selection must carry enough to distinguish/group by
# individual incidents. Enforced in build_uof_sql; the frontend also keeps
# these permanently selected so this only bites direct API callers.
REQUIRED_COLUMNS = {"Incident_ID", "Incident_Date"}

# Columns valid for filtering (hence still in ALLOWED_COLUMNS) but excluded
# from SELECT/GROUP BY -- Under_18 exists only as a Subject_Age-derived flag
# for narrowing results, not a column meant to be displayed or grouped on.
FILTER_ONLY_COLUMNS = {"Under_18"}

# Columns compared as numbers (range filters, or "tags-num" IN filters) rather
# than text — mirrors NUMERIC in uof_program_v2.html. Subject_Age is deliberately
# excluded: it's a multi-value column (see MULTI_VALUE_POSITION) so its range
# filter is handled via the tokenized uof_dashboard_values_data table instead.
NUMERIC_COLUMNS = {"Total_Sub_Injured", "Officer_Age", "Incident_Year", "Form_ID", "User_ID"}
# all of these columns use min/max specs in the request body, except only form_id, user_id use IN.

# Columns whose uof_main_data value is a raw comma-joined string (e.g.
# "Kick, Resisted arrest/police officer control"), tokenized per-value into
# uof_dashboard_values_data at ETL time. Filtering these means matching
# individual tokens there and constraining Form_ID, not matching the raw
# string on uof_main_data directly. Maps column name -> Position_Id, taken
# from clean_and_populate.py's pos_map (minus its single_cols, which are
# standardized single values, not tokenized).
MULTI_VALUE_POSITION = {
    "Indoor_Or_Outdoor": 13,
    "Incident_Weather": 14,
    "Video_Type": 16,
    "Incident_Lighting": 17,
    "Location_Type": 18,
    "Incident_Type": 19,
    "Contact_Origin": 20,
    "Planned_Contact": 21,
    "Officer_Injury_Type": 26,
    "Officer_Medical_Treatment": 28,
    "Subject_Injured": 31,
    "Subject_Injured_Prior": 32,
    "Perceived_Condition": 33,
    "Subject_Actions": 34,
    "Subject_Resistance": 35,
    "Subject_Medical Treatment": 36,
    "Subject_Injury Type": 37,
    "Subject_Arrested": 38,
    "Reason_Not_Arrested": 39,
    "Subject_Type": 40,
    "Subject_Age": 41,
    "Subject_Race/Ethnicity": 42,
    "Subject_Gender": 43,
    "Force_Type": 44,
}


# Single-value columns standardized via standard_values_table (see
# clean_and_populate.py's single_cols). column_name there is stored with
# spaces, not underscores (e.g. 'Officer Rank'), matching the ETL script's
# own col.replace("_", " ").lower() convention.
SINGLE_VALUE_COLUMNS = {
    "Video_Footage": "Video Footage",
    "Officer_Race/Ethnicity": "Officer Race/Ethnicity",
    "Officer_Rank": "Officer Rank",
    "Officer_Gender": "Officer Gender",
    "Officer_Hospital_Treatment": "Officer Hospital Treatment",
}

# Boolean-flag columns stored in uof_main_data as tinyint 1/0/NULL (see
# clean_and_populate.py's bool_cols) but presented to users under
# column-specific labels rather than the raw 1/0/NULL. Filter values coming
# in from the request need mapping to the DB encoding before they hit the SQL
# query, and values coming back out need mapping to the label before they're
# returned to the frontend. Keyed per-column (not one global label set) since
# Under_18's labels don't read as generic True/False.
BOOLEAN_COLUMN_LABELS = {
    "Other_Officer_Involved": {"True": 1, "False": 0, "Not Provided": None},
    "Officer_In_Uniform": {"True": 1, "False": 0, "Not Provided": None},
    "Officer_Injuries_Injured": {"True": 1, "False": 0, "Not Provided": None},
    # Derived per-row from Subject_Age tokens (see clean_and_populate.py's
    # compute_under_18): 1 = at least one subject on this row is "Under 18",
    # 0 = no subject is, None = can't tell because at least one Subject_Age
    # token on the row is itself unknown/non-numeric.
    "Under_18": {"Under 18": 1, "Not Under 18": 0, "Unknown": None},
}
BOOLEAN_COLUMNS = set(BOOLEAN_COLUMN_LABELS)
BOOLEAN_COLUMN_DB_TO_LABEL = {
    col: {v: k for k, v in labels.items()} for col, labels in BOOLEAN_COLUMN_LABELS.items()
}

# Free-text uof_main_data columns with no standard/column-values catalog but
# low enough cardinality to be genuinely categorical (a few hundred distinct
# values at most), unlike Officer_Name/Report_Number/Incident_Case which are
# closer to unique identifiers (20k+ distinct values each) and wouldn't
# benefit from being bundled into a suggestion list.
DISTINCT_VALUE_COLUMNS = ["County", "Agency_Name", "Incident_Municipality"]

# ─────────────────────────────────────────────────────────────────────
# ARRIVE (arrive_main_data) query config
# ─────────────────────────────────────────────────────────────────────

# Whitelist of real arrive_main_data columns, same rationale as ALLOWED_COLUMNS
# above (filter keys are spliced into the SQL as identifiers).
ARRIVE_ALLOWED_COLUMNS = {
    "Random_ID", "Incident_Year", "Arrive_Model", "Outreach_Attempts",
    "Behaviors_Indicated_Prior_to_Arrival", "Other_Individuals_on_Scene",
    "Law_Enforcement_Observed_Behavior", "Law_Enforcement_Outcomes",
    "Mental_Health_Outcome", "Day_30_Outcomes",
}

ARRIVE_NUMERIC_COLUMNS = {"Random_ID", "Incident_Year", "Outreach_Attempts"}

# Columns whose arrive_main_data value is a plain joined string (comma-joined,
# except Arrive_Model which is " & "-joined -- see import_arrive_data.py),
# tokenized per-value into arrive_values_data at ETL time, but only for cells
# that were genuinely multi-valued (see tokenize_arrive_data.py). Unlike
# MULTI_VALUE_POSITION above, arrive_values_data keys tokens by column_name
# text directly rather than an integer Position_Id against a separate
# dictionary table, so no position map is needed here.
ARRIVE_MULTI_VALUE_COLS = {
    "Arrive_Model",
    "Behaviors_Indicated_Prior_to_Arrival",
    "Other_Individuals_on_Scene",
    "Law_Enforcement_Observed_Behavior",
    "Law_Enforcement_Outcomes",
    "Mental_Health_Outcome",
    "Day_30_Outcomes",
}


def quote_col(col):
    return f"`{col}`"


# Shared by build_uof_sql/build_arrive_sql: turns the request body's "columns"
# (output columns, SELECT) and "count_mode" (GROUP BY those same columns) into
# a SELECT list and an optional GROUP BY clause. Unlike filters, which differ
# enough per dataset to need their own builder each, this part is identical
# once you're just given the right column whitelist.
def build_select_and_group(body, allowed_columns):
    requested_cols = body.get("columns") or []
    # requested_cols come straight from the request body and get spliced into
    # the SQL as identifiers, not parameterizable like values are -- same
    # rationale as ALLOWED_COLUMNS above, so anything not whitelisted must be
    # dropped before it touches the query string. dict.fromkeys dedupes while
    # preserving order. FILTER_ONLY_COLUMNS are valid for filtering but never
    # for SELECT/GROUP BY -- see its definition above.
    valid_cols = [
        c for c in dict.fromkeys(requested_cols)
        if c in allowed_columns and c not in FILTER_ONLY_COLUMNS
    ]
    count_mode = bool(body.get("count_mode"))

    if count_mode:
        if valid_cols:
            select_list = ", ".join(quote_col(c) for c in valid_cols) + ", COUNT(*) AS count"
            group_by = " GROUP BY " + ", ".join(quote_col(c) for c in valid_cols)
        else:
            # No valid output columns to group by -- just the grand total,
            # rather than a GROUP BY with nothing to group on.
            select_list = "COUNT(*) AS count"
            group_by = ""
    else:
        select_list = ", ".join(quote_col(c) for c in valid_cols) if valid_cols else "*"
        group_by = ""

    return select_list, group_by


app = Flask(__name__)
app.config.from_mapping(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "dev")
)

CORS(app)

# test route
@app.route("/health", methods=("GET",))
def health():
  return {"status": "ok", "datasets": ["uof", "arrive"]}

# Cached response for /filter-values/<dataset> (below): this data only changes
# when the ETL pipeline lands new data, not on every page load, so recomputing
# it per-request just re-pays a several-second DISTINCT-query cost for no
# reason. Render's free-tier deployment (see render.yaml) runs a single
# gunicorn worker, so a plain process-global is a genuinely shared cache across
# all requests, not just the current one -- and since the free tier already
# spins the process down after ~15min idle (recomputing on the next cold start
# regardless), "cache forever until restart" gets the same practical freshness
# as a TTL here, with less code. Keyed by dataset so uof/arrive cache
# independently.
# NOTE: this Render+Aiven deployment is temporary/testing-only. Once the app
# moves to an always-on server (no idle spin-down to naturally invalidate
# this), switch this to a TTL so it self-refreshes without needing a manual
# restart/redeploy to pick up new ETL data.
_filter_values_cache = {"uof": None, "arrive": None}


def _uof_filter_values():
  conn = mysql.connector.connect(**DB_CONFIG)
  cursor = conn.cursor(dictionary=True)
  try:
    values = {col: [] for col in SINGLE_VALUE_COLUMNS}
    values.update({col: [] for col in MULTI_VALUE_POSITION if col != "Subject_Age"})
    values.update({col: [] for col in DISTINCT_VALUE_COLUMNS})
    # Fixed set, not queried from the DB -- these columns are tinyint 1/0/NULL,
    # not a text catalog, so the label list is just each column's 3 constant options.
    values.update({col: list(labels.keys()) for col, labels in BOOLEAN_COLUMN_LABELS.items()})

    # One query for all 5 single-value columns instead of 5 separate
    # round-trips -- each query here is a network hop to Aiven, and with 31
    # cataloged/categorical columns total, doing them one-by-one made this
    # endpoint take ~5.7s to answer (measured), which is a real page-load
    # delay even though it's fetched only once, not per keystroke.
    single_col_names = list(SINGLE_VALUE_COLUMNS.values())
    reverse_single = {v: k for k, v in SINGLE_VALUE_COLUMNS.items()}
    placeholders = ", ".join(["%s"] * len(single_col_names))
    cursor.execute(
        f"SELECT DISTINCT column_name, standard_value FROM standard_values_table "
        f"WHERE column_name IN ({placeholders}) ORDER BY column_name, standard_value",
        tuple(single_col_names),
    )
    for row in cursor.fetchall():
      r = dict(row)
      col = reverse_single[r["column_name"]]
      values[col].append(r["standard_value"])

    # One query for all 23 multi-value columns instead of 23 round-trips.
    multi_positions = {col: pid for col, pid in MULTI_VALUE_POSITION.items() if col != "Subject_Age"}
    reverse_multi = {v: k for k, v in multi_positions.items()}
    placeholders = ", ".join(["%s"] * len(multi_positions))
    cursor.execute(
        f"SELECT DISTINCT Position_Id, Column_Value FROM uof_column_values_data "
        f"WHERE Position_Id IN ({placeholders}) ORDER BY Position_Id, Column_Value",
        tuple(multi_positions.values()),
    )
    for row in cursor.fetchall():
      r = dict(row)
      col = reverse_multi[r["Position_Id"]]
      values[col].append(r["Column_Value"])

    # One UNION query for the 3 uncataloged categorical columns instead of 3.
    union_sql = " UNION ALL ".join(
        f"SELECT %s AS field, {quote_col(col)} AS val FROM uof_main_data WHERE {quote_col(col)} IS NOT NULL"
        for col in DISTINCT_VALUE_COLUMNS
    )
    cursor.execute(f"SELECT DISTINCT field, val FROM ({union_sql}) AS distinct_cols ORDER BY field, val", tuple(DISTINCT_VALUE_COLUMNS))
    for row in cursor.fetchall():
      r = dict(row)
      values[r["field"]].append(r["val"])

    return values
  finally:
    cursor.close()
    conn.close()


def _arrive_filter_values():
  # Unlike UoF, every categorical ARRIVE column is already tokenized into
  # arrive_values_data (see ARRIVE_MULTI_VALUE_COLS) -- there's no ARRIVE
  # equivalent of SINGLE_VALUE_COLUMNS or an uncataloged DISTINCT_VALUE_COLUMNS
  # set, so one grouped query covers all of them.
  conn = mysql.connector.connect(**DB_CONFIG)
  cursor = conn.cursor(dictionary=True)
  try:
    values = {col: [] for col in ARRIVE_MULTI_VALUE_COLS}
    cols = list(ARRIVE_MULTI_VALUE_COLS)
    placeholders = ", ".join(["%s"] * len(cols))
    cursor.execute(
        f"SELECT DISTINCT column_name, column_value FROM arrive_values_data "
        f"WHERE column_name IN ({placeholders}) ORDER BY column_name, column_value",
        tuple(cols),
    )
    for row in cursor.fetchall():
      r = dict(row)
      values[r["column_name"]].append(r["column_value"])

    return values
  finally:
    cursor.close()
    conn.close()


_FILTER_VALUE_BUILDERS = {"uof": _uof_filter_values, "arrive": _arrive_filter_values}

# Bulk endpoint for the frontend's autocomplete: returns every known value for
# each cataloged/categorical free-text column of the given dataset in one
# shot, fetched once per page load rather than queried per keystroke. Columns
# not present in the response (Officer_Name, Report_Number, Incident_Case,
# IDs, numeric ranges) get no suggestions client-side -- same as today.
@app.route("/filter-values/<dataset>", methods=("GET",))
def filter_values(dataset):
  if dataset not in _FILTER_VALUE_BUILDERS:
    return jsonify({"error": f"Unknown dataset: {dataset}"}), 400

  if _filter_values_cache[dataset] is not None:
    return _filter_values_cache[dataset]

  try:
    values = _FILTER_VALUE_BUILDERS[dataset]()
    response = {"values": values}
    _filter_values_cache[dataset] = response  # only cache on success; an error should retry next request
  except Exception as e:
    response = {"error": str(e)}

  return response


# ─────────────────────────────────────────────────────────────────────
# Shared query execution: connect, run, log, clean up. Both /query/uof and
# /query/arrive build their own (sql, params) and hand them here -- the
# filter-building logic differs enough between the two datasets (see
# ARRIVE_MULTI_VALUE_COLS vs MULTI_VALUE_POSITION above) that sharing it
# would mean branching on dataset shape inside a "shared" function, but
# running/formatting a finished query is identical either way.
# ─────────────────────────────────────────────────────────────────────
def execute_query(sql, params):
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(sql, tuple(params))
        executed_query = cursor.statement if hasattr(cursor, "statement") else "no statement available"
        print(f"Executed SQL: {executed_query}")

        results = cursor.fetchall()
        rows = []
        for row in results:
            r = dict(row)
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
            rows.append(r)
        return rows
    finally:
        cursor.close()
        conn.close()


def build_uof_sql(body):
    # Sample Body:
    # {
    #   "text_match": "exact", // or "partial" for LIKE
    #   "filters": {
    #     "Incident_Date": { "from": "2025-01-01", "to": "2025-06-30" },
    #     "County":        { "in": ["Essex", "Union"] },
    #     "Subject_Age":   { "min": "18", "max": "40" }
    #     ...
    #   }
    # }
    print(f"Building UoF SQL for request body: {body}")
    filters = body.get("filters", {})
    text_match = body.get("text_match", "exact")

    clauses = []
    params = []

    # Handle date range filter for Incident_Date
    date_filter = filters.get("Incident_Date", {})
    if date_filter:
        date_from = date_filter.get("from")
        date_to = date_filter.get("to")
        if date_from and date_to:
            clauses.append("Incident_Date BETWEEN %s AND %s")
            params.extend([date_from, date_to])
        elif date_from:
            clauses.append("Incident_Date >= %s")
            params.append(date_from)
        elif date_to:
            clauses.append("Incident_Date <= %s")
            params.append(date_to)
            
    # Main filter loop
    for col, spec in filters.items():
        # Skip Incident_Date since it's handled separately, and skip any columns not in the allowed list or non-dict specs
        if col == "Incident_Date" or col not in ALLOWED_COLUMNS or not isinstance(spec, dict):
            continue

        if col in BOOLEAN_COLUMNS:
            # Exact-match only (the like_match toggle doesn't apply to a
            # True/False/Not Provided flag) -- map labels to their tinyint
            # 1/0/NULL encoding, then IN (...) for True/False and IS NULL
            # for Not Provided, since SQL's IN doesn't match NULL.
            raw_values = [v for v in (spec.get("in") or []) if v not in (None, "")]
            if not raw_values:
                # Filter wasn't actually set -- skip it, same as elsewhere.
                continue
            label_to_db = BOOLEAN_COLUMN_LABELS[col]
            mapped = [label_to_db[v] for v in raw_values if v in label_to_db]
            non_null = [v for v in mapped if v is not None]
            sub_clauses = []
            if non_null:
                placeholders = ", ".join(["%s"] * len(non_null))
                sub_clauses.append(f"{quote_col(col)} IN ({placeholders})")
                params.extend(non_null)
            if None in mapped:
                sub_clauses.append(f"{quote_col(col)} IS NULL")
            if sub_clauses:
                clauses.append("(" + " OR ".join(sub_clauses) + ")")
            else:
                # This is a free-text tag field, so the user can type
                # anything -- if every entry they typed is something other
                # than True/False/Not Provided, it can't match any real row.
                # Force zero results instead of silently dropping the filter
                # and returning everything unfiltered.
                clauses.append("1=0")
            continue

        position_id = MULTI_VALUE_POSITION.get(col)

        # Handle columns with an "in" filter
        if "in" in spec:
            #get all "in" values
            values = [v for v in (spec.get("in") or []) if v not in (None, "")]
            if not values:
                continue

            # multi-value column: match individual tokens in uof_dashboard_values_data
            # and constrain Form_ID, since uof_main_data holds the raw joined string
            # for genuinely multi-valued forms. Single-valued forms have no rows in
            # uof_dashboard_values_data at all (see clean_and_populate.py), so also
            # match those directly against uof_main_data, excluding any Form_ID that
            # does have tokenized rows for this position (those are the multi-valued
            # ones, already covered by the first branch, and their raw joined string
            # shouldn't be compared directly).
            if position_id is not None:
                if text_match == "partial":
                    token_clauses = " OR ".join(["Column_Value LIKE %s"] * len(values))
                    token_params = [f"%{v}%" for v in values]
                    main_clauses = " OR ".join([f"{quote_col(col)} LIKE %s"] * len(values))
                    main_params = [f"%{v}%" for v in values]
                else:
                    token_clauses = " OR ".join(["Column_Value = %s"] * len(values))
                    token_params = list(values)
                    main_clauses = " OR ".join([f"{quote_col(col)} = %s"] * len(values))
                    main_params = list(values)
                clauses.append(
                    "(Form_ID IN (SELECT Form_Id FROM uof_dashboard_values_data "
                    f"WHERE Position_Id = %s AND ({token_clauses})) "
                    f"OR (({main_clauses}) AND Form_ID NOT IN "
                    "(SELECT Form_Id FROM uof_dashboard_values_data WHERE Position_Id = %s)))"
                )
                params.append(position_id)
                params.extend(token_params)
                params.extend(main_params)
                params.append(position_id)
            # append params for non-numeric columns with partial match; use LIKE clauses
            elif col not in NUMERIC_COLUMNS and text_match == "partial":
                like_clauses = " OR ".join([f"{quote_col(col)} LIKE %s"] * len(values))
                clauses.append(f"({like_clauses})")
                params.extend(f"%{v}%" for v in values)
            # append params for numeric columns or exact match; use IN clause
            else:
                placeholders = ", ".join(["%s"] * len(values))
                clauses.append(f"{quote_col(col)} IN ({placeholders})")
                params.extend(values)

        # handle min/max range filters for numeric columns
        elif "min" in spec or "max" in spec:
            min_v, max_v = spec.get("min"), spec.get("max")

            # multi-value numeric column (currently just Subject_Age): tokens can
            # also be non-numeric ("Unknown", "Under 18"), so only compare tokens
            # that are actually digits — a numeric range can't meaningfully match
            # the rest. Single-valued forms have no tokenized rows in
            # uof_dashboard_values_data at all (see clean_and_populate.py), so also
            # match those directly against uof_main_data (same digit-only guard),
            # excluding any Form_ID that does have tokenized rows for this position.
            if position_id is not None:
                numeric_guard = "Column_Value REGEXP '^[0-9]+$'"
                main_numeric_guard = f"{quote_col(col)} REGEXP '^[0-9]+$'"
                not_tokenized = "Form_ID NOT IN (SELECT Form_Id FROM uof_dashboard_values_data WHERE Position_Id = %s)"
                if min_v not in (None, "") and max_v not in (None, ""):
                    clauses.append(
                        "(Form_ID IN (SELECT Form_Id FROM uof_dashboard_values_data "
                        f"WHERE Position_Id = %s AND {numeric_guard} "
                        "AND CAST(Column_Value AS UNSIGNED) BETWEEN %s AND %s) "
                        f"OR ({main_numeric_guard} AND CAST({quote_col(col)} AS UNSIGNED) BETWEEN %s AND %s "
                        f"AND {not_tokenized}))"
                    )
                    params.extend([position_id, min_v, max_v, min_v, max_v, position_id])
                elif min_v not in (None, ""):
                    clauses.append(
                        "(Form_ID IN (SELECT Form_Id FROM uof_dashboard_values_data "
                        f"WHERE Position_Id = %s AND {numeric_guard} "
                        "AND CAST(Column_Value AS UNSIGNED) >= %s) "
                        f"OR ({main_numeric_guard} AND CAST({quote_col(col)} AS UNSIGNED) >= %s "
                        f"AND {not_tokenized}))"
                    )
                    params.extend([position_id, min_v, min_v, position_id])
                elif max_v not in (None, ""):
                    clauses.append(
                        "(Form_ID IN (SELECT Form_Id FROM uof_dashboard_values_data "
                        f"WHERE Position_Id = %s AND {numeric_guard} "
                        "AND CAST(Column_Value AS UNSIGNED) <= %s) "
                        f"OR ({main_numeric_guard} AND CAST({quote_col(col)} AS UNSIGNED) <= %s "
                        f"AND {not_tokenized}))"
                    )
                    params.extend([position_id, max_v, max_v, position_id])
            elif min_v not in (None, "") and max_v not in (None, ""):
                clauses.append(f"{quote_col(col)} BETWEEN %s AND %s")
                params.extend([min_v, max_v])
            elif min_v not in (None, ""):
                clauses.append(f"{quote_col(col)} >= %s")
                params.append(min_v)
            elif max_v not in (None, ""):
                clauses.append(f"{quote_col(col)} <= %s")
                params.append(max_v)

    requested_cols = body.get("columns") or []
    if requested_cols and not REQUIRED_COLUMNS.issubset(requested_cols):
        missing = REQUIRED_COLUMNS - set(requested_cols)
        raise ValueError(
            f"'columns' must include {sorted(REQUIRED_COLUMNS)} (missing: {sorted(missing)})"
        )

    select_list, group_by = build_select_and_group(body, ALLOWED_COLUMNS)
    sql = f"SELECT {select_list} FROM uof_main_data"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += group_by
    # Sort by Incident_ID then Incident_Date so results always arrive grouped
    # by incident, in chronological order. These are required columns (see
    # REQUIRED_COLUMNS above) so they're always available to sort on.
    # Skip ORDER BY when count_mode is active — the GROUP BY result set has no
    # meaningful row-level ordering (and MySQL would require the sort column
    # to appear in the SELECT list or GROUP BY, which it won't if the user
    # didn't select those columns).
    if not body.get("count_mode"):
        sql += " ORDER BY Incident_ID ASC, Incident_Date ASC"
    sql += f" LIMIT {MAX_ROWS};"
    return sql, params


def build_arrive_sql(body):
    # Sample Body:
    # {
    #   "text_match": "exact", // or "partial" for LIKE
    #   "filters": {
    #     "Incident_Year": { "min": "2023", "max": "2025" },
    #     "Arrive_Model":  { "in": ["Co-Response", "Telehealth"] },
    #     "Behaviors_Indicated_Prior_to_Arrival": { "in": ["Violence"] }
    #     ...
    #   }
    # }
    print(f"Building ARRIVE SQL for request body: {body}")
    filters = body.get("filters", {})
    text_match = body.get("text_match", "exact")

    clauses = []
    params = []

    # No per-day date field on arrive_main_data (only Incident_Year, handled
    # as a numeric range below), so no date-range branch is needed here.

    for col, spec in filters.items():
        if col not in ARRIVE_ALLOWED_COLUMNS or not isinstance(spec, dict):
            continue

        is_multi_value = col in ARRIVE_MULTI_VALUE_COLS

        if "in" in spec:
            values = [v for v in (spec.get("in") or []) if v not in (None, "")]
            if not values:
                continue

            # multi-value column: match individual tokens in arrive_values_data
            # and constrain Random_ID, since arrive_main_data holds the raw
            # joined string for genuinely multi-valued rows. Single-valued rows
            # have no rows in arrive_values_data at all (see
            # tokenize_arrive_data.py), so also match those directly against
            # arrive_main_data, excluding any Random_ID that does have tokenized
            # rows for this column (those are the multi-valued ones, already
            # covered by the first branch, and their raw joined string
            # shouldn't be compared directly). Same pattern as build_uof_sql's
            # handling of uof_dashboard_values_data, keyed by column_name text
            # instead of an integer Position_Id.
            if is_multi_value:
                if text_match == "partial":
                    token_clauses = " OR ".join(["column_value LIKE %s"] * len(values))
                    token_params = [f"%{v}%" for v in values]
                    main_clauses = " OR ".join([f"{quote_col(col)} LIKE %s"] * len(values))
                    main_params = [f"%{v}%" for v in values]
                else:
                    token_clauses = " OR ".join(["column_value = %s"] * len(values))
                    token_params = list(values)
                    main_clauses = " OR ".join([f"{quote_col(col)} = %s"] * len(values))
                    main_params = list(values)
                clauses.append(
                    "(Random_ID IN (SELECT Random_ID FROM arrive_values_data "
                    f"WHERE column_name = %s AND ({token_clauses})) "
                    f"OR (({main_clauses}) AND Random_ID NOT IN "
                    "(SELECT Random_ID FROM arrive_values_data WHERE column_name = %s)))"
                )
                params.append(col)
                params.extend(token_params)
                params.extend(main_params)
                params.append(col)
            elif col not in ARRIVE_NUMERIC_COLUMNS and text_match == "partial":
                like_clauses = " OR ".join([f"{quote_col(col)} LIKE %s"] * len(values))
                clauses.append(f"({like_clauses})")
                params.extend(f"%{v}%" for v in values)
            else:
                placeholders = ", ".join(["%s"] * len(values))
                clauses.append(f"{quote_col(col)} IN ({placeholders})")
                params.extend(values)

        # handle min/max range filters for numeric columns. None of ARRIVE's
        # multi-value columns are numeric (Incident_Year/Outreach_Attempts are
        # both single-valued), so unlike build_uof_sql there's no tokenized
        # numeric-range case to handle here.
        elif "min" in spec or "max" in spec:
            min_v, max_v = spec.get("min"), spec.get("max")
            if min_v not in (None, "") and max_v not in (None, ""):
                clauses.append(f"{quote_col(col)} BETWEEN %s AND %s")
                params.extend([min_v, max_v])
            elif min_v not in (None, ""):
                clauses.append(f"{quote_col(col)} >= %s")
                params.append(min_v)
            elif max_v not in (None, ""):
                clauses.append(f"{quote_col(col)} <= %s")
                params.append(max_v)

    select_list, group_by = build_select_and_group(body, ARRIVE_ALLOWED_COLUMNS)
    sql = f"SELECT {select_list} FROM arrive_main_data"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += group_by
    # Sort by year so results always arrive in chronological order.
    # Skip ORDER BY in count_mode for the same reason as build_uof_sql above.
    if not body.get("count_mode"):
        sql += " ORDER BY Incident_Year ASC"
    sql += f" LIMIT {MAX_ROWS};"
    return sql, params


@app.route("/query/uof", methods=["POST"])
def query_uof():
    try:
        body = request.get_json(force=True)
        try:
            sql, params = build_uof_sql(body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        rows = execute_query(sql, params)
        # Map the tinyint 1/0/NULL encoding back to each column's own labels --
        # applies both to row-level values and to count_mode's grouped column
        # values, since both come back under the column's own key. Checked
        # once against the first row (every row has the same keys) rather
        # than per row, and skipped entirely if no boolean column was
        # selected/grouped on.
        if rows:
            present_bool_cols = BOOLEAN_COLUMNS & rows[0].keys()
            if present_bool_cols:
                for row in rows:
                    for col in present_bool_cols:
                        row[col] = BOOLEAN_COLUMN_DB_TO_LABEL[col].get(row[col], row[col])
        return jsonify({"rows": rows, "count": len(rows)}), 200
    except Exception as e:
        print(f"  ERROR: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/query/arrive", methods=["POST"])
def query_arrive():
    try:
        body = request.get_json(force=True)
        sql, params = build_arrive_sql(body)
        rows = execute_query(sql, params)
        return jsonify({"rows": rows, "count": len(rows)}), 200
    except Exception as e:
        print(f"  ERROR: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
  app.run(debug=True, port=5001)
