#!/usr/bin/env python3
"""
Daily batch runner for Lecture Quality Analyzer.

Reads recordings_today.csv (recording_url,batch,module,session_id), downloads
each recording, extracts evenly-spaced frames, scores them with the same
core.analyze_image() used by app.py / analyze.py, and writes a per-session
PDF/CSV/JSON plus a combined report_<date>.csv / .json rollup into reports/.

Usage: python auto_lecture_analyzer.py --csv recordings_today.csv --api-key gsk_...
       (or set GROQ_API_KEY env var)
"""

import argparse
import csv
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import requests

from core import load_image_file, analyze_image, build_csv_rows, aggregate_scores
from pdf_report import generate_pdf

DEFAULT_FRAME_COUNT = 8
DEFAULT_EDGE_MARGIN = 0.03  # skip first/last 3% of the video (intro/outro slides)


def resolve_video_url(recording_url: str) -> str:
    """Unwrap a portal link's ?url= query param, else use the link as-is."""
    parsed = urlparse(recording_url)
    qs = parse_qs(parsed.query)
    if "url" in qs and qs["url"]:
        return qs["url"][0]
    return recording_url


def download_video(url: str, dest_path: Path) -> None:
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def extract_frames(video_path: Path, output_dir: Path,
                    count: int = DEFAULT_FRAME_COUNT,
                    edge_margin: float = DEFAULT_EDGE_MARGIN) -> list:
    """Extract `count` evenly-spaced frames, skipping the first/last edge_margin of the video."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        raise RuntimeError(f"Video reports no frames: {video_path}")

    start = int(total_frames * edge_margin)
    end = int(total_frames * (1 - edge_margin))
    span = end - start
    if span <= 0:
        start, end, span = 0, total_frames, total_frames

    positions = [start + int(span * (i + 1) / (count + 1)) for i in range(count)]

    frame_paths = []
    for i, frame_no in enumerate(positions, 1):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        ok, frame = cap.read()
        if not ok:
            continue
        frame_path = output_dir / f"frame_{i:02d}.jpg"
        cv2.imwrite(str(frame_path), frame)
        frame_paths.append(frame_path)

    cap.release()
    return frame_paths


def process_session(row: dict, api_key: str, frame_count: int, edge_margin: float) -> dict:
    recording_url = row["recording_url"]
    batch         = row["batch"]
    module        = row["module"]
    session_id    = row["session_id"]

    print(f"\n=== Session {session_id} ({batch} / {module}) ===")

    direct_url = resolve_video_url(recording_url)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)
        video_path = tmp_dir / "recording.mp4"
        frames_dir = tmp_dir / "frames"
        frames_dir.mkdir()

        print(f"  Downloading recording...")
        download_video(direct_url, video_path)

        print(f"  Extracting {frame_count} frames...")
        frame_paths = extract_frames(video_path, frames_dir, frame_count, edge_margin)
        if not frame_paths:
            print(f"  No frames extracted, skipping session.")
            return None

        results = []
        for i, frame_path in enumerate(frame_paths, 1):
            print(f"  [{i}/{len(frame_paths)}] scoring {frame_path.name}...", end=" ", flush=True)
            try:
                image_data, media_type = load_image_file(frame_path)
                analysis = analyze_image(api_key, image_data, media_type)
                analysis["screenshot"]  = frame_path.name
                analysis["analyzed_at"] = datetime.now().isoformat()
                results.append(analysis)
                print(f"done (overall {analysis.get('overall_score', '?')}/5)")
            except Exception as e:
                print(f"FAILED ({e})")
        # tmp_dir (video + frames) is deleted automatically on exit

    if not results:
        print(f"  No results for session {session_id}, skipping.")
        return None

    return {
        "session_id":    session_id,
        "batch":         batch,
        "module":        module,
        "recording_url": recording_url,
        "results":       results,
    }


def write_session_reports(session: dict, reports_dir: Path, date_str: str) -> list:
    """Write per-session PDF/CSV/JSON. Returns the CSV rows (with session_id) for the daily rollup."""
    batch, module, session_id, results = (
        session["batch"], session["module"], session["session_id"], session["results"]
    )
    slug = f"{batch}_{module}_{session_id}".replace(" ", "-").replace("/", "-")
    base_name = f"{slug}_{date_str}"

    pdf_bytes = generate_pdf(batch, module, results)
    (reports_dir / f"{base_name}.pdf").write_bytes(pdf_bytes)

    rows = build_csv_rows(batch, module, results)
    for row in rows:
        row["session_id"]    = session_id
        row["recording_url"] = session["recording_url"]

    csv_path = reports_dir / f"{base_name}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    json_path = reports_dir / f"{base_name}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "session_id":    session_id,
            "batch_name":    batch,
            "lecture_module": module,
            "recording_url": session["recording_url"],
            "analyzed_at":   date_str,
            "results":       results,
        }, f, indent=2, ensure_ascii=False)

    averages = aggregate_scores(results)
    overall_scores = [r["overall_score"] for r in results if isinstance(r.get("overall_score"), (int, float))]
    overall = sum(overall_scores) / len(overall_scores) if overall_scores else None
    print(f"  Reports written: {base_name}.{{pdf,csv,json}}  (overall {overall:.1f}/5)" if overall is not None
          else f"  Reports written: {base_name}.{{pdf,csv,json}}")

    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Daily batch runner: score today's lecture recordings from a CSV manifest."
    )
    parser.add_argument("--csv", "-c", default="recordings_today.csv",
                         help="CSV with columns recording_url,batch,module,session_id")
    parser.add_argument("--reports-dir", "-o", default="reports", help="Output directory (default: reports)")
    parser.add_argument("--frames", type=int, default=DEFAULT_FRAME_COUNT, help="Frames to extract per recording")
    parser.add_argument("--edge-margin", type=float, default=DEFAULT_EDGE_MARGIN,
                         help="Fraction of video to skip at start/end (default: 0.03)")
    parser.add_argument("--api-key", "-k", help="Groq API key (or set GROQ_API_KEY env var)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        print("Error: provide --api-key or set GROQ_API_KEY environment variable.")
        sys.exit(1)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV not found: {csv_path}")
        sys.exit(1)

    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"No rows found in {csv_path}")
        sys.exit(1)

    date_str = datetime.now().strftime("%Y%m%d")
    sessions = []
    combined_rows = []

    for row in rows:
        try:
            session = process_session(row, api_key, args.frames, args.edge_margin)
        except Exception as e:
            print(f"  FAILED session {row.get('session_id', '?')}: {e}")
            continue
        if session is None:
            continue
        sessions.append(session)
        combined_rows.extend(write_session_reports(session, reports_dir, date_str))

    if not sessions:
        print("\nNo sessions produced results.")
        sys.exit(1)

    combined_csv_path = reports_dir / f"report_{date_str}.csv"
    with open(combined_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(combined_rows[0].keys()))
        writer.writeheader()
        writer.writerows(combined_rows)

    combined_json_path = reports_dir / f"report_{date_str}.json"
    with open(combined_json_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "sessions": sessions}, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*65}")
    print(f"  {len(sessions)} session(s) analyzed")
    print(f"  Combined CSV : {combined_csv_path}")
    print(f"  Combined JSON: {combined_json_path}")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
