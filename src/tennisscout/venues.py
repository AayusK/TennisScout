"""Venue metadata versioned by year.

``metadata/venues.csv`` is a hand-written seed: city-level coordinates and
approximate elevation for tour events since 2019, keyed by the source's
tournament name and a year range (events move: the Canada Masters alternates
cities, the Paris Masters left Bercy in 2025, the ATP licence code 9410 has
been Astana and Almaty). It is *unverified*; every value it supplies is tagged
``venue_source = 'manual_seed_unverified'`` so the audit and the model can tell
it apart from verified metadata.

Coordinates are city level, not court level. Roof status can change inside
an event (spec, "Conditions"), so the edition's indoor flag is a default, not
a per-match fact.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from . import config

VENUE_SOURCE = "manual_seed_unverified"


@dataclass(frozen=True)
class Venue:
    tourney_name: str
    year_from: int
    year_to: int
    city: str
    country: str
    latitude: float | None
    longitude: float | None
    elevation_m: float | None
    notes: str


def _f(s: str) -> float | None:
    s = s.strip()
    return float(s) if s else None


def load(path: Path | None = None) -> list[Venue]:
    path = path or config.METADATA_DIR / "venues.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        return [
            Venue(
                tourney_name=r["tourney_name"],
                year_from=int(r["year_from"]),
                year_to=int(r["year_to"]),
                city=r["city"],
                country=r["country"],
                latitude=_f(r["latitude"]),
                longitude=_f(r["longitude"]),
                elevation_m=_f(r["elevation_m"]),
                notes=r["notes"],
            )
            for r in csv.DictReader(fh)
        ]


class VenueIndex:
    def __init__(self, venues: list[Venue]):
        self._by_name: dict[str, list[Venue]] = {}
        for v in venues:
            self._by_name.setdefault(v.tourney_name, []).append(v)

    def lookup(self, tourney_name: str, year: int) -> Venue | None:
        for v in self._by_name.get(tourney_name, ()):
            if v.year_from <= year <= v.year_to:
                return v
        return None
