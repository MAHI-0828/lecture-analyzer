#!/usr/bin/env python3
"""
Daily automation, matching the real 3-sheet flow: writes the target date into
"Daily Report"!B1 (same cell you type into by hand), waits for the sheet's own
FILTER formulas to refresh "YAMM Report" to that date's lectures, then joins
those rows back to "Raw Data" (via instructor email + lecture title) to get
each lecture's recording link. Runs the same download/score/PDF pipeline as
auto_lecture_analyzer.py, uploads each PDF to Drive, and writes the links into
a PDF_Links tab so an "Attachment" formula added to YAMM Report can pick them
up for YAMM's personalized-attachment send.

Meant to run unattended via .github/workflows/daily-report.yml.

Env vars required: GOOGLE_API_KEY (Gemini/Google AI Studio key for scoring),
SHEET_ID, DRIVE_FOLDER_ID, GOOGLE_SERVICE_ACCOUNT_JSON (the full
service-account JSON key as a string, used for Sheets/Drive access — a
separate credential from GOOGLE_API_KEY).

Usage: python daily_pdf_pipeline.py [--date YYYY-MM-DD] [--frames 8] [--edge-margin 0.03]
Passing --date (or the default "yesterday") also sets Daily Report!B1 to that
date, so the sheet always ends up showing whatever date was just processed —
no manual date entry needed.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from auto_lecture_analyzer import (
    process_session, write_session_reports,
    DEFAULT_FRAME_COUNT, DEFAULT_EDGE_MARGIN,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

RAW_DATA_TAB     = "Raw Data'"  # yes, the sheet's real tab name has a trailing apostrophe
DAILY_REPORT_TAB = "Daily Report"
YAMM_REPORT_TAB  = "YAMM Report"
PDF_LINKS_TAB    = "PDF_Links"


def sheet_range(tab, a1_range=""):
    """Quote the tab name for A1 notation — required for spaces, and any literal
    apostrophe in the name must itself be doubled inside the quotes. With no
    a1_range, returns the whole-sheet form (all populated rows/columns) —
    avoids hardcoding a column cap that real sheets can grow past."""
    escaped = tab.replace("'", "''")
    quoted = f"'{escaped}'"
    return f"{quoted}!{a1_range}" if a1_range else quoted


DATE_CELL = sheet_range(DAILY_REPORT_TAB, "B1")
RECALC_WAIT_SECONDS = 3  # let Daily Report -> YAMM Report's FILTER formulas refresh

PDF_LINKS_HEADER = ["date", "session_id", "instructor_email", "lecture_title", "drive_link"]

COLUMN_MAP = {
    "date":             "date",
    "recording_url":    "recording_link",
    "batch":            "batch_name",
    "module":           "lecture_title",
    "session_id":       "lecture_id",
    "instructor_email": "instructor_email",
}

YAMM_COLUMN_MAP = {
    "instructor_email": "Instructor Email",
    "lecture_title":    "Lecture Title",
}

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y")


def get_credentials():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def parse_sheet_date(value):
    value = (value or "").strip()
    if not value:
        return None
    date_part = value.split("T")[0].split(" ")[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_part, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def read_raw_data(sheets_service, sheet_id, target_date):
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=sheet_range(RAW_DATA_TAB)
    ).execute()
    values = resp.get("values", [])
    if not values:
        return []

    header = values[0]
    missing = [col for col in COLUMN_MAP.values() if col not in header]
    if missing:
        raise RuntimeError(
            f"{RAW_DATA_TAB} is missing expected column(s): {missing}. "
            f"Available columns: {header}"
        )

    rows = [dict(zip(header, raw)) for raw in values[1:]]
    return [r for r in rows if parse_sheet_date(r.get(COLUMN_MAP["date"])) == target_date]


def build_raw_data_lookup(raw_rows):
    """Key raw_data rows by (instructor_email, lecture_title) so YAMM Report rows can find their recording link."""
    lookup = {}
    for r in raw_rows:
        key = (
            r.get(COLUMN_MAP["instructor_email"], "").strip().lower(),
            r.get(COLUMN_MAP["module"], "").strip().lower(),
        )
        lookup[key] = r
    return lookup


def set_report_date(sheets_service, sheet_id, target_date_str):
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=DATE_CELL,
        valueInputOption="USER_ENTERED", body={"values": [[target_date_str]]},
    ).execute()


def read_yamm_report(sheets_service, sheet_id):
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=sheet_range(YAMM_REPORT_TAB)
    ).execute()
    values = resp.get("values", [])
    if not values:
        return []

    header = values[0]
    missing = [col for col in YAMM_COLUMN_MAP.values() if col not in header]
    if missing:
        raise RuntimeError(
            f"{YAMM_REPORT_TAB} is missing expected column(s): {missing}. "
            f"Available columns: {header}"
        )

    return [dict(zip(header, raw)) for raw in values[1:] if any(raw)]


def to_session_row(raw_row):
    return {
        "recording_url": raw_row.get(COLUMN_MAP["recording_url"], "").strip(),
        "batch":         raw_row.get(COLUMN_MAP["batch"], "").strip(),
        "module":        raw_row.get(COLUMN_MAP["module"], "").strip(),
        "session_id":    raw_row.get(COLUMN_MAP["session_id"], "").strip(),
    }


def upload_pdf_to_drive(drive_service, pdf_path, folder_id):
    media = MediaFileUpload(str(pdf_path), mimetype="application/pdf")
    file = drive_service.files().create(
        body={"name": pdf_path.name, "parents": [folder_id]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    return file["webViewLink"]


def ensure_tab_exists(sheets_service, sheet_id, tab, header):
    meta = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab in titles:
        return
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=sheet_range(tab, "A1"),
        valueInputOption="RAW", body={"values": [header]},
    ).execute()


def rewrite_pdf_links(sheets_service, sheet_id, target_date_str, new_rows):
    ensure_tab_exists(sheets_service, sheet_id, PDF_LINKS_TAB, PDF_LINKS_HEADER)

    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=sheet_range(PDF_LINKS_TAB, "A:E")
    ).execute()
    values = resp.get("values", [])
    header = values[0] if values else PDF_LINKS_HEADER
    existing = values[1:] if len(values) > 1 else []

    kept = [row for row in existing if row and row[0] != target_date_str]
    updated = [header] + kept + new_rows

    sheets_service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=sheet_range(PDF_LINKS_TAB, "A:Z")
    ).execute()
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=sheet_range(PDF_LINKS_TAB, "A1"),
        valueInputOption="RAW", body={"values": updated},
    ).execute()


def main():
    parser = argparse.ArgumentParser(
        description=f"Generate lecture PDFs for a date's {YAMM_REPORT_TAB} rows and publish them to Drive/PDF_Links."
    )
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAME_COUNT)
    parser.add_argument("--edge-margin", type=float, default=DEFAULT_EDGE_MARGIN)
    parser.add_argument("--list-tabs", action="store_true",
                         help="Print the exact tab names in the sheet (repr'd, to reveal stray whitespace) and exit.")
    args = parser.parse_args()

    sheet_id = os.environ.get("SHEET_ID", "")
    if not (sheet_id and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")):
        print("Error: SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be set.")
        sys.exit(1)

    creds = get_credentials()
    sheets_service = build("sheets", "v4", credentials=creds)

    if args.list_tabs:
        meta = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
        for s in meta.get("sheets", []):
            print(repr(s["properties"]["title"]))
        return

    google_api_key  = os.environ.get("GOOGLE_API_KEY", "")
    drive_folder_id = os.environ.get("DRIVE_FOLDER_ID", "")
    if not (google_api_key and drive_folder_id):
        print("Error: GOOGLE_API_KEY and DRIVE_FOLDER_ID must be set.")
        sys.exit(1)

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
        else (datetime.now() - timedelta(days=1)).date()
    )
    target_date_str = target_date.isoformat()
    date_compact = target_date.strftime("%Y%m%d")

    drive_service = build("drive", "v3", credentials=creds)

    print(f"Reading {RAW_DATA_TAB} for {target_date_str}...")
    raw_rows = read_raw_data(sheets_service, sheet_id, target_date)
    raw_lookup = build_raw_data_lookup(raw_rows)

    print(f"Setting {DATE_CELL} to {target_date_str}...")
    set_report_date(sheets_service, sheet_id, target_date_str)
    time.sleep(RECALC_WAIT_SECONDS)

    print(f"Reading {YAMM_REPORT_TAB}...")
    yamm_rows = read_yamm_report(sheets_service, sheet_id)
    if not yamm_rows:
        print(f"No lectures found in {YAMM_REPORT_TAB} for {target_date_str}.")
        return

    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    pdf_link_rows = []
    for yamm_row in yamm_rows:
        instructor_email = yamm_row.get(YAMM_COLUMN_MAP["instructor_email"], "").strip()
        lecture_title     = yamm_row.get(YAMM_COLUMN_MAP["lecture_title"], "").strip()

        raw_row = raw_lookup.get((instructor_email.lower(), lecture_title.lower()))
        if raw_row is None:
            print(f"  No {RAW_DATA_TAB} match for {instructor_email!r} / {lecture_title!r}, skipping.")
            continue

        session_row = to_session_row(raw_row)
        if not session_row["recording_url"]:
            print(f"  Skipping row with no recording URL: {session_row}")
            continue

        try:
            session = process_session(session_row, google_api_key, args.frames, args.edge_margin)
        except Exception as e:
            print(f"  FAILED session {session_row.get('session_id', '?')}: {e}")
            continue
        if session is None:
            continue

        write_session_reports(session, reports_dir, date_compact)

        slug = f"{session['batch']}_{session['module']}_{session['session_id']}".replace(" ", "-").replace("/", "-")
        pdf_path = reports_dir / f"{slug}_{date_compact}.pdf"
        if not pdf_path.exists():
            print(f"  PDF missing for session {session['session_id']}, skipping upload.")
            continue

        print(f"  Uploading {pdf_path.name} to Drive...")
        drive_link = upload_pdf_to_drive(drive_service, pdf_path, drive_folder_id)
        pdf_link_rows.append([target_date_str, session["session_id"], instructor_email, session["module"], drive_link])

    if pdf_link_rows:
        print(f"Writing {len(pdf_link_rows)} row(s) to {PDF_LINKS_TAB}...")
        rewrite_pdf_links(sheets_service, sheet_id, target_date_str, pdf_link_rows)
    else:
        print("No PDFs generated, nothing to write to PDF_Links.")

    print("Done.")


if __name__ == "__main__":
    main()
