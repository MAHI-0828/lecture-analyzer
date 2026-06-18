"""
Shared analysis logic — used by both the CLI (analyze.py) and the Streamlit app (app.py).
"""

import base64
import json
import re
from pathlib import Path

import anthropic

# ─── Parameter registry ─────────────────────────────────────────────────────────

PARAMETERS = [
    "camera_angle",
    "face_lighting",
    "background_quality",
    "appearance",
    "instructor_energy",
    "screen_share_active",
    "content_type",
    "slide_text_density",
    "visual_first_design",
    "slide_template_consistency",
    "code_readability",
    "annotation_activity",
    "chat_panel_visible",
    "desktop_notifications",
    "dual_screen_evidence",
    "agenda_slide_present",
]

PARAMETER_LABELS = {
    "camera_angle":               "Camera Angle",
    "face_lighting":              "Face Lighting",
    "background_quality":         "Background Quality",
    "appearance":                 "Appearance & Clothing",
    "instructor_energy":          "Instructor Energy & Posture",
    "screen_share_active":        "Screen Share Active",
    "content_type":               "Content Type on Screen",
    "slide_text_density":         "Slide Text Density",
    "visual_first_design":        "Visual-First Slide Design",
    "slide_template_consistency": "Slide Template Consistency",
    "code_readability":           "Code Readability",
    "annotation_activity":        "Annotation / Whiteboard Activity",
    "chat_panel_visible":         "Chat Panel Visible",
    "desktop_notifications":      "Desktop Notifications",
    "dual_screen_evidence":       "Dual Screen Evidence",
    "agenda_slide_present":       "Agenda Slide Present",
}

# ─── System prompt ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert instructional quality analyst reviewing screenshots from live online lectures.

For each screenshot, evaluate the following 16 parameters. For each parameter return:
- score: integer 1–5, or null if the parameter is not applicable to this screenshot
- observation: exactly 2 sentences describing what you can literally see in the screenshot (be specific)
- improvement: exactly 2 sentences of actionable advice for the instructor

If a parameter is not applicable (e.g., code_readability when no code is on screen, agenda_slide_present when it is clearly mid-session), set score to null and write "Not applicable in this screenshot." for both observation and improvement.

Return ONLY valid JSON with no markdown, no code fences, no extra text:
{
  "scores": {
    "camera_angle":               { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "face_lighting":              { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "background_quality":         { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "appearance":                 { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "instructor_energy":          { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "screen_share_active":        { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "content_type":               { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "slide_text_density":         { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "visual_first_design":        { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "slide_template_consistency": { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "code_readability":           { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "annotation_activity":        { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "chat_panel_visible":         { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "desktop_notifications":      { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "dual_screen_evidence":       { "score": <1-5|null>, "observation": "...", "improvement": "..." },
    "agenda_slide_present":       { "score": <1-5|null>, "observation": "...", "improvement": "..." }
  },
  "overall_score": <average of all non-null scores, rounded to 1 decimal>,
  "priority_fix": "<single most impactful improvement the instructor should make, 1-2 sentences>"
}

Scoring rubrics:
- camera_angle:               5=perfect eye level, centered; 3=slightly off; 1=extreme angle (nostrils/ceiling/far off-axis)
- face_lighting:              5=evenly lit from front, no shadows; 3=mild one-side shadowing; 1=face mostly dark or washed out
- background_quality:         5=plain neutral wall, zero distractions; 3=minor clutter; 1=very cluttered or bright backlit window
- appearance:                 5=professional solid-color attire; 3=casual but acceptable; 1=unprofessional or heavy moiré pattern
- instructor_energy:          5=upright, smiling, visibly engaged; 3=neutral posture/expression; 1=slouched, blank, clearly disengaged
- screen_share_active:        5=relevant content fully shared; 3=screen shared but generic desktop; 1=black/blank or no share
- content_type:               5=live coding/whiteboard/active annotation; 3=static slides; 1=black screen or irrelevant content
- slide_text_density:         5=under 30 words, concise bullets; 3=45-70 words, somewhat crowded; 1=wall of text over 100 words
- visual_first_design:        5=primarily diagrams/infographics; 3=mixed text and visuals; 1=entirely text-based
- slide_template_consistency: 5=consistent branded template; 3=mostly consistent, minor deviations; 1=no coherent template
- code_readability:           5=monospace font, syntax highlighting, large text (18pt+); 3=code visible but small/unformatted; 1=unreadable
- annotation_activity:        5=active drawings/underlines/circles clearly visible; 3=annotation surface open but sparse; 1=no annotation
- chat_panel_visible:         5=chat clearly in layout or on separate device; 3=ambiguous; 1=no evidence of chat monitoring
- desktop_notifications:      5=no notifications visible; 3=minor badge only; 1=multiple notification banners on shared screen
- dual_screen_evidence:       5=second monitor visible in webcam frame; 3=natural eye-shift suggests side screen; 1=single screen, overlapping windows
- agenda_slide_present:       5=clear agenda with all topics and time estimates; 3=partial agenda; 1=no agenda at session start"""


# ─── Image helpers ───────────────────────────────────────────────────────────────

def encode_image_file(image_path: Path) -> tuple:
    """Return (base64_string, media_type) for an image file."""
    suffix = image_path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }
    media_type = media_types.get(suffix, "image/png")
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return data, media_type


def encode_image_bytes(file_bytes: bytes, filename: str) -> tuple:
    """Return (base64_string, media_type) for raw bytes (used by Streamlit uploader)."""
    suffix = Path(filename).suffix.lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    }
    media_type = media_types.get(suffix, "image/png")
    data = base64.standard_b64encode(file_bytes).decode("utf-8")
    return data, media_type


# ─── Analysis ────────────────────────────────────────────────────────────────────

def analyze_image(client: anthropic.Anthropic, image_data: str, media_type: str) -> dict:
    """Send an image to Claude and return the structured JSON result."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_data},
                },
                {
                    "type": "text",
                    "text": "Analyze this lecture screenshot and return the JSON evaluation.",
                },
            ],
        }],
    )
    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


# ─── CSV helpers ─────────────────────────────────────────────────────────────────

def build_csv_rows(batch: str, module: str, results: list) -> list:
    """Flatten results into one dict per screenshot for CSV output."""
    rows = []
    for r in results:
        row = {
            "batch_name":     batch,
            "lecture_module": module,
            "screenshot":     r["screenshot"],
            "analyzed_at":    r["analyzed_at"],
        }
        for param in PARAMETERS:
            data = r.get("scores", {}).get(param, {})
            row[f"{param}_score"]       = data.get("score", "")
            row[f"{param}_observation"] = data.get("observation", "")
            row[f"{param}_improvement"] = data.get("improvement", "")
        row["overall_score"] = r.get("overall_score", "")
        row["priority_fix"]  = r.get("priority_fix", "")
        rows.append(row)
    return rows


def aggregate_scores(results: list) -> dict:
    """Return {param: avg_score} across all results, skipping nulls."""
    param_scores = {p: [] for p in PARAMETERS}
    for r in results:
        for param in PARAMETERS:
            score = r.get("scores", {}).get(param, {}).get("score")
            if score is not None:
                param_scores[param].append(score)
    return {p: (sum(v) / len(v) if v else None) for p, v in param_scores.items()}
