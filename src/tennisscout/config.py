"""Paths and runtime settings. Everything can be overridden with env vars."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("TENNISSCOUT_DATA", REPO_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("TENNISSCOUT_DB", DATA_DIR / "tennisscout.db"))
OUTPUT_DIR = DATA_DIR / "out"

# Committed, small, our own data: permanent ID registries and curated metadata.
REGISTRY_DIR = Path(os.environ.get("TENNISSCOUT_REGISTRY", REPO_ROOT / "registry"))
METADATA_DIR = REPO_ROOT / "metadata"
DOCS_DIR = REPO_ROOT / "docs"


def utcnow() -> str:
    """Collection timestamps are always UTC ISO-8601 with a Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, OUTPUT_DIR, REGISTRY_DIR):
        d.mkdir(parents=True, exist_ok=True)
