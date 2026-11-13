"""
schema_loader.py — Load workflow JSON schemas and build header alias maps.

The spec says:
  "To normalise a header, lower-case it, trim it, and replace every run of
   _ or whitespace with a single space. Match the result against the field
   name or any of its aliases (normalised the same way). Ignore columns
   that match nothing."
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from solution.config import SCHEMA_DIR


def normalise_key(s: str) -> str:
    """
    Lower-case, trim, and collapse every run of underscores or whitespace
    to a single space.

    Examples:
      "Mobile No"    → "mobile no"
      "INSURED_NAME" → "insured name"
      "policy_expiry"→ "policy expiry"
    """
    return re.sub(r"[_\s]+", " ", s.lower()).strip()


def load_schema(workflow_id: str) -> dict[str, Any]:
    """
    Load the JSON schema for a workflow from the schemas directory.
    Returns the parsed dict unchanged.
    """
    path = SCHEMA_DIR / f"{workflow_id}.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def build_alias_map(schema: dict[str, Any]) -> dict[str, str]:
    """
    Build a mapping from every normalised header variant to the canonical
    field name.

    For each field in the schema, we add:
      - normalise(field['name'])  → field['name']
      - normalise(alias)          → field['name']  for each alias

    Example output for motor_renewal:
      {
        "customer name":  "customer_name",
        "name":           "customer_name",
        "insured name":   "customer_name",
        "phone":          "phone",
        "mobile":         "phone",
        ...
      }
    """
    alias_map: dict[str, str] = {}
    for field in schema["fields"]:
        field_name = field["name"]
        # canonical name itself
        alias_map[normalise_key(field_name)] = field_name
        # each listed alias
        for alias in field.get("aliases", []):
            alias_map[normalise_key(alias)] = field_name
    return alias_map

