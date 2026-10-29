"""Source registry and local mirroring.

The spec's rule: never depend on a single GitHub repo. Each source records
its license, its role, and a snapshot manifest (revision, per-file SHA-256,
collection time) so every row we load can be traced to the exact bytes it
came from.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Source:
    name: str
    repo_url: str
    license: str
    role: str
    files: tuple[str, ...] = ()  # glob patterns we actually read
    status_note: str = ""


SOURCES: dict[str, Source] = {
    "tml": Source(
        name="tml",
        repo_url="https://github.com/Tennismylife/TML-Database",
        license=(
            "TennisMyLife data usage terms: personal, educational and non-commercial research use only; "
            "no redistribution of the raw database; commercial use (including monetized model output) "
            "needs written permission. Derived from Jeff Sackmann's CC BY-NC-SA tennis_atp."
        ),
        role="Primary historical backbone: ATP tour-level results 1968-, serve/return stats, ATP player IDs, indoor flag.",
        files=("[12][0-9][0-9][0-9].csv", "ongoing_tourneys.csv", "ATP_Database.csv"),
        status_note="GitHub copy is 'kept for historical reference'; live updates moved to stats.tennismylife.org.",
    ),
    "mcp": Source(
        name="mcp",
        repo_url="https://github.com/JeffSackmann/tennis_MatchChartingProject",
        license="CC BY-NC-SA 4.0 (attribution required, non-commercial, share-alike).",
        role="Second, independent source: charted-match metadata (exact dates, start times, courts) and point-by-point sample.",
        files=("charting-m-matches.csv",),
    ),
    "sackmann_atp": Source(
        name="sackmann_atp",
        repo_url="https://github.com/JeffSackmann/tennis_atp",
        license="CC BY-NC-SA 4.0",
        role="Original Sackmann-format backbone named in the spec.",
        status_note="Returned 404 on 2026-10-29 (raw files and git). Kept here so the pipeline notices if it comes back.",
    ),
}


@dataclass
class Snapshot:
    source: str
    path: str
    revision: str
    revision_date: str
    collected_at: str
    license: str
    files: dict[str, str] = field(default_factory=dict)  # relative path -> sha256

    @property
    def snapshot_id(self) -> str:
        return f"{self.source}@{self.revision[:12]}"

    def to_json(self) -> str:
        d = asdict(self)
        d["snapshot_id"] = self.snapshot_id
        return json.dumps(d, indent=2, sort_keys=True)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def mirror_path(name: str) -> Path:
    return config.RAW_DIR / name


def fetch(name: str, local_path: Path | None = None) -> Snapshot:
    """Clone or update a source mirror and write its snapshot manifest.

    ``local_path`` lets you point at an existing checkout instead of cloning.
    """
    src = SOURCES[name]
    config.ensure_dirs()
    path = Path(local_path) if local_path else mirror_path(name)
    if local_path is None:
        if (path / ".git").exists():
            subprocess.run(["git", "-C", str(path), "pull", "--ff-only", "--depth", "1"], check=True)
        else:
            # MCP uses Git LFS for nothing we read; skipping smudge keeps clones working through proxies.
            subprocess.run(
                ["git", "clone", "--depth", "1", src.repo_url, str(path)],
                check=True,
                env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
            )
    return write_snapshot(name, path)


def write_snapshot(name: str, path: Path) -> Snapshot:
    src = SOURCES[name]
    revision = _git(path, "rev-parse", "HEAD")
    revision_date = _git(path, "log", "-1", "--format=%cI")
    files: dict[str, str] = {}
    for pattern in src.files:
        for f in sorted(path.glob(pattern)):
            files[f.name] = _sha256(f)
    snap = Snapshot(
        source=name,
        path=str(path),
        revision=revision,
        revision_date=revision_date,
        collected_at=config.utcnow(),
        license=src.license,
        files=files,
    )
    manifest = config.RAW_DIR / f"{name}.snapshot.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(snap.to_json())
    return snap


def load_snapshot(name: str) -> Snapshot:
    manifest = config.RAW_DIR / f"{name}.snapshot.json"
    if not manifest.exists():
        raise FileNotFoundError(f"No snapshot for {name!r}; run `tennisscout fetch {name}` first.")
    d = json.loads(manifest.read_text())
    d.pop("snapshot_id", None)
    return Snapshot(**d)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
