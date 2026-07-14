#!/usr/bin/env python3
"""
Document Field Extraction
==========================
Reads parsed markdown from parse_results/<pipeline>/ and runs LandingAI ADE Extract
on each file using the document-type-specific schemas defined in schema.py.

Results are written to extract_results/<pipeline>/<doc_type>/<stem>.json

Usage:
    python3 extract_docs.py           # extract all pending (parts pipeline)
    python3 extract_docs.py --force   # re-extract everything
    python3 extract_docs.py --limit 5 # extract at most N files

To add a new pipeline (e.g. "jobs"):
    1. Add new DOC_TYPES, FIELD_LABELS, EXTRACTION_SCHEMAS to schema.py
    2. Place PDFs in jobs_inbox/
    3. Run: python3 web_app.py --parse --all   (with PIPELINE = "jobs" in web_app.py)
    4. Change PIPELINE below to "jobs" and re-run this script

Environment:
    VISION_AGENT_API_KEY  — LandingAI ADE key
"""

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import schema

load_dotenv(override=True)

import core

BASE_DIR = core.BASE_DIR

# ── Pipeline ───────────────────────────────────────────────────────────────────
# Defined once in core.py and shared with web_app.py. It no longer has to be kept
# in sync by hand across two files — set PIPELINE there (or via the environment).
PIPELINE = core.PIPELINE

PARSE_RESULTS   = core.PARSE_RESULTS / PIPELINE
EXTRACT_RESULTS = core.EXTRACT_RESULTS / PIPELINE

SEP = core.SEP

core.validate_config()


# ── Parse result file readers ──────────────────────────────────────────────────

def _extract_markdown(txt: str) -> str:
    """Pull the MARKDOWN section out of a parse result file."""
    marker = "MARKDOWN"
    idx = txt.find(marker)
    if idx == -1:
        return txt
    after_marker = txt[idx + len(marker):]
    lines = after_marker.splitlines()
    content_lines = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started:
            if stripped and not all(c in "─━─" for c in stripped if c != " "):
                started = True
                content_lines.append(line)
        else:
            if all(c in "─━─" for c in stripped if c != " ") and stripped:
                break
            content_lines.append(line)
    return "\n".join(content_lines).strip()


def _extract_section_json(txt: str, section: str):
    """Pull a named JSON section from a parse result file.

    Single implementation lives in core.py; web_app.py reads the same one, so the
    two can no longer drift apart.
    """
    return core.get_section_json(txt, section)




def _extract_chunks_confidence(txt: str) -> list:
    """Pull chunk-level confidence scores from a parse result file."""
    chunks = _extract_section_json(txt, "CHUNKS")
    scores = []
    for chunk in chunks:
        conf = chunk.get("confidence")
        if conf is not None:
            scores.append({
                "chunk_id":             chunk.get("id"),
                "chunk_type":           chunk.get("type"),
                "confidence":           conf,
                "low_confidence_spans": chunk.get("low_confidence_spans", []),
            })
    return scores


def _doc_type(stem: str) -> str | None:
    """Return the doc_type for a stem, or None if unrecognised."""
    for key in schema.DOC_TYPES:
        if stem.endswith(f"_{key}"):
            return key
    return None


# ── Extraction runner ──────────────────────────────────────────────────────────

def run_extract(force: bool = False, limit: int | None = None) -> None:
    from landingai_ade import LandingAIADE

    if not schema.DOC_TYPES:
        print("No document types configured. Edit schema.py and define DOC_TYPES.")
        sys.exit(1)

    if not PARSE_RESULTS.is_dir():
        print(f"parse_results/{PIPELINE}/ not found. Run: python3 web_app.py --parse --all")
        sys.exit(1)

    txt_files = sorted(PARSE_RESULTS.glob("*.txt"))
    if not txt_files:
        print("No parse result files found. Run the parser first.")
        sys.exit(1)

    if force:
        import shutil
        if EXTRACT_RESULTS.exists():
            shutil.rmtree(EXTRACT_RESULTS)
            print(f"  [force] Cleared {EXTRACT_RESULTS}")

    pending = []
    for txt_path in txt_files:
        doc_type = _doc_type(txt_path.stem)
        if doc_type is None:
            continue
        out_file = EXTRACT_RESULTS / doc_type / f"{txt_path.stem}.json"
        if not out_file.exists():
            pending.append((txt_path, doc_type, out_file))

    if limit is not None:
        pending = pending[:limit]

    total = len(pending)
    print("=" * 60)
    print(f"Document Extractor — pipeline={PIPELINE} — {total} file(s) to process")
    print("=" * 60)

    if total == 0:
        print("Nothing to do — all files already extracted. Use --force to re-run.")
        return

    if not schema.EXTRACTION_SCHEMAS:
        print("No extraction schemas configured. Edit schema.py and define EXTRACTION_SCHEMAS.")
        sys.exit(1)

    client = LandingAIADE()
    success = fail = 0

    for i, (txt_path, doc_type, out_file) in enumerate(pending, 1):
        print(f"\n[{i}/{total}] {txt_path.stem}  ({doc_type})")

        txt = txt_path.read_text(encoding="utf-8")
        markdown = _extract_markdown(txt)
        if not markdown:
            print(f"  [warn] No markdown content found — skipping")
            fail += 1
            continue

        extraction_schema = schema.EXTRACTION_SCHEMAS.get(doc_type)
        if not extraction_schema:
            print(f"  [warn] No extraction schema defined for doc_type '{doc_type}' — skipping")
            fail += 1
            continue

        try:
            extracted = client.extract(
                schema=json.dumps(extraction_schema),
                markdown=markdown,
                model="extract-latest",
            )
        except Exception as e:
            print(f"  [error] Extract failed: {e}")
            fail += 1
            continue

        fields              = extracted.extraction or {}
        extraction_metadata = extracted.extraction_metadata or {}

        chunk_confidences = _extract_chunks_confidence(txt)
        avg_confidence = (
            sum(c["confidence"] for c in chunk_confidences) / len(chunk_confidences)
            if chunk_confidences else None
        )
        low_confidence_chunks = [
            c for c in chunk_confidences if c["confidence"] < 0.90
        ]

        chunks_raw = _extract_section_json(txt, "CHUNKS")
        grounding  = _extract_section_json(txt, "GROUNDING")

        result = {
            "source_file":        txt_path.name,
            "document_type":      doc_type,
            "pipeline":           PIPELINE,
            "extracted_fields":   fields,
            "extraction_metadata": extraction_metadata,
            "confidence_summary": {
                "avg_parse_confidence":       round(avg_confidence, 4) if avg_confidence else None,
                "total_chunks_scored":        len(chunk_confidences),
                "low_confidence_chunk_count": len(low_confidence_chunks),
                "low_confidence_chunks":      low_confidence_chunks,
            },
            "chunks":    chunks_raw,
            "grounding": grounding,
        }

        # Atomic write: a plain write_text leaves a half-written file visible to a
        # concurrently-running web server, which would read it as corrupt JSON.
        core.atomic_write_json(out_file, json.loads(json.dumps(result, default=str)))

        fields_found = len([v for v in fields.values() if v is not None]) if isinstance(fields, dict) else "?"
        n_fields     = len(schema.FIELD_LABELS.get(doc_type, {}))
        conf_str     = f"  |  avg confidence: {avg_confidence:.3f}" if avg_confidence else ""
        print(f"  ✓ {fields_found}/{n_fields} fields extracted{conf_str}")
        success += 1

        if i < total:
            time.sleep(0.5)

    print("\n" + "=" * 60)
    print(f"DONE  pipeline={PIPELINE}  extracted={success}  failed={fail}")
    print(f"Results: {EXTRACT_RESULTS}/")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract key fields from parsed documents using ADE.")
    parser.add_argument("--force", action="store_true",
                        help=f"Delete existing extract_results/{PIPELINE}/ and re-extract everything.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Process at most N pending files.")
    args = parser.parse_args()
    run_extract(force=args.force, limit=args.limit)
