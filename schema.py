"""
schema.py — Configure your document types, extraction schemas, and field labels.

This is the primary configuration file for the HITL workflow starter kit.
Edit this file before running the extraction pipeline.

Quick start:
  1. Define your document types in DOC_TYPES (key = stem suffix, value = display label)
  2. Define the fields to review per doc type in FIELD_LABELS
  3. Write ADE extraction schemas in EXTRACTION_SCHEMAS
  4. Set ENTITY_NAME_FIELDS to the field key(s) that identify the group entity
  5. Set GROUP_PREFIX_PARTS to the number of underscore-separated stem segments
     that form a logical group (e.g. "case_001_form_a" → 2 groups "case_001")

File naming convention:
  Your PDF stems should follow the pattern:
      <group_key>_<doc_type>.pdf
  e.g. "case_001_form_a.pdf"  →  group "case_001",  doc_type "form_a"
"""

import json
from pathlib import Path

_SCHEMAS_DIR = Path(__file__).parent / "schemas"

# ── Document types ─────────────────────────────────────────────────────────────
# key   = suffix that appears at the end of each PDF stem
# value = display label shown in the UI

DOC_TYPES: dict[str, str] = {
    "shipping_label": "Shipping Label",
}

# ── Field labels per document type ────────────────────────────────────────────
# Defines which fields appear in the review panel and their display names.
# Keys must match the property names in EXTRACTION_SCHEMAS.
# Order here is the order shown in the UI.

FIELD_LABELS: dict[str, dict[str, str]] = {
    "shipping_label": {
        "tracking_number":  "Tracking Number",
        "carrier":          "Carrier",
        "service_level":    "Service Level",
        "ship_to_name":     "Ship To",
        "ship_to_address":  "Ship To Address",
        "ship_from_name":   "Ship From",
        "ship_from_address": "Ship From Address",
        "weight":           "Weight",
        "ship_date":        "Ship Date",
        "reference_number": "Reference Number",
    },
}

# ── Entity name fields ─────────────────────────────────────────────────────────
# Field key(s) used to derive the entity display name shown in the task board.
# Tried in order; first non-empty value wins. Falls back to the group prefix.

ENTITY_NAME_FIELDS: list[str] = [
    "ship_to_name",
    "tracking_number",
]

# ── Group prefix parts ─────────────────────────────────────────────────────────
# Number of underscore-separated parts of a file stem that form the group key.
# e.g. GROUP_PREFIX_PARTS=1 on "case_001_form_a" → group key "case"

GROUP_PREFIX_PARTS: int = 1

# ── ADE extraction schemas ─────────────────────────────────────────────────────
# JSON Schema objects passed to client.extract(schema=...) for each doc type.
# See https://docs.landing.ai/ade for schema format and description tips.

EXTRACTION_SCHEMAS: dict[str, dict] = {
    "shipping_label": json.loads((_SCHEMAS_DIR / "shipping-label-schema.json").read_text()),
}
