"""Permanent internal IDs.

Architecture change #1 in the spec: our own player, tournament and match IDs
are the keys; ATP / Sackmann / provider / market identifiers are *mappings*
onto them. The registry is append-only and committed to git, so rebuilding the
database from scratch reproduces the same IDs and anything that references
them (archived market quotes, news items) stays valid.
"""

from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path

from . import config


class Registry:
    """Append-only map from ``(source, external_key)`` to an internal ID."""

    def __init__(self, path: Path, prefix: str, width: int):
        self.path = Path(path)
        self.prefix = prefix
        self.width = width
        self._by_ext: dict[tuple[str, str], str] = {}
        self._rows: list[tuple[str, str, str, str]] = []  # (id, source, key, created_at)
        self._next = 1
        self._dirty = False
        if self.path.exists():
            self._load()

    # -- persistence -------------------------------------------------------
    def _open(self, mode: str):
        if self.path.suffix == ".gz":
            return io.TextIOWrapper(gzip.open(self.path, mode + "b"), encoding="utf-8", newline="")
        return open(self.path, mode, encoding="utf-8", newline="")

    def _load(self) -> None:
        with self._open("r") as fh:
            for row in csv.DictReader(fh):
                key = (row["source"], row["external_key"])
                self._by_ext[key] = row["internal_id"]
                self._rows.append((row["internal_id"], row["source"], row["external_key"], row["created_at"]))
                n = int(row["internal_id"][len(self.prefix):])
                self._next = max(self._next, n + 1)

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.suffix == ".gz":
            # mtime=0 keeps the gzip bytes identical when the content is.
            buf = io.StringIO()
            self._write(buf)
            with open(self.path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(buf.getvalue().encode("utf-8"))
        else:
            with open(self.path, "w", encoding="utf-8", newline="") as fh:
                self._write(fh)
        self._dirty = False

    def _write(self, fh) -> None:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["internal_id", "source", "external_key", "created_at"])
        w.writerows(self._rows)

    # -- API ---------------------------------------------------------------
    def lookup(self, source: str, key: str) -> str | None:
        return self._by_ext.get((source, key))

    def get_or_mint(self, source: str, key: str, created_at: str | None = None) -> str:
        existing = self._by_ext.get((source, key))
        if existing:
            return existing
        new_id = f"{self.prefix}{self._next:0{self.width}d}"
        self._next += 1
        self._add(new_id, source, key, created_at)
        return new_id

    def alias(self, internal_id: str, source: str, key: str, created_at: str | None = None) -> None:
        """Map another external identifier onto an existing internal ID."""
        existing = self._by_ext.get((source, key))
        if existing and existing != internal_id:
            raise ValueError(f"{source}:{key} already maps to {existing}, refusing to remap to {internal_id}")
        if not existing:
            self._add(internal_id, source, key, created_at)

    def _add(self, internal_id: str, source: str, key: str, created_at: str | None) -> None:
        self._by_ext[(source, key)] = internal_id
        self._rows.append((internal_id, source, key, created_at or config.utcnow()))
        self._dirty = True

    def items(self):
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


def players(registry_dir: Path | None = None) -> Registry:
    return Registry((registry_dir or config.REGISTRY_DIR) / "players.csv", "P", 6)


def tournaments(registry_dir: Path | None = None) -> Registry:
    return Registry((registry_dir or config.REGISTRY_DIR) / "tournaments.csv", "T", 5)


def editions(registry_dir: Path | None = None) -> Registry:
    return Registry((registry_dir or config.REGISTRY_DIR) / "editions.csv.gz", "E", 6)


def matches(registry_dir: Path | None = None) -> Registry:
    return Registry((registry_dir or config.REGISTRY_DIR) / "matches.csv.gz", "M", 7)
