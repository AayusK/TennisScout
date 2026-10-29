"""Small, pure helpers shared by ingestion, identity checks and the model."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from difflib import SequenceMatcher

# Order of rounds inside one tournament edition. Anything that happens later in
# the draw must sort later, because the backtest replays matches in this order.
ROUND_ORDER: dict[str, int] = {
    "Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "PQ": 0,
    "R256": 10, "R128": 11, "R64": 12, "R32": 13, "R16": 14,
    "RR": 15,  # round robin (Tour Finals, team events) precedes knockout SF
    "ER": 15,
    "QF": 16, "SF": 17,
    "BR": 18, "3rd/4th": 18,  # bronze / third-place play-off
    "F": 19, "Fs": 19,
}

SURFACES = ("Hard", "Clay", "Grass", "Carpet")


def round_order(rnd: str) -> int:
    return ROUND_ORDER.get(rnd.strip(), 15)


def surface_key(surface: str, indoor: str, split_indoor_hard: bool = True) -> str:
    """Rating bucket for a match. The spec asks for hard, clay, grass and indoor hard."""
    s = (surface or "").strip().capitalize()
    if s not in SURFACES:
        return "Unknown"
    if s == "Hard" and split_indoor_hard and (indoor or "").strip().upper() == "I":
        return "IndoorHard"
    return s


def outcome(score: str) -> str:
    """Classify a result string. Walkovers carry no information about play."""
    s = (score or "").strip().upper()
    if not s or s in {"NA", "N/A"}:
        return "unknown"
    if "W/O" in s or "WALKOVER" in s:
        return "walkover"
    if "DEF" in s:
        return "default"
    if "RET" in s or "ABD" in s or "ABN" in s:
        return "retired"
    return "completed"


_NON_ALPHA = re.compile(r"[^a-z ]+")
_SPACES = re.compile(r"\s+")


def norm_name(name: str) -> str:
    """Accent-, case- and punctuation-insensitive form of a display name.

    This is for *candidate lookup* only. A normalized name is never a join key.
    """
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("-", " ").replace("'", "").replace(".", " ")
    s = _NON_ALPHA.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def names_compatible(a: str, b: str) -> bool:
    """Do two display names plausibly describe the same person?

    Used to catch rows where a source attached the wrong external ID to a
    player (e.g. one TML row lists Marc-Andrea Huesler under John Millman's ATP
    ID). Spelling variants, extra middle names and transliterations pass;
    different people fail.
    """
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if ta <= tb or tb <= ta:
        return True
    if SequenceMatcher(None, na, nb).ratio() >= 0.85:
        return True
    # Same surname, same first initial ("Alex de Minaur" / "Alexander De Minaur").
    sa, sb = na.split(), nb.split()
    return sa[-1] == sb[-1] and sa[0][0] == sb[0][0]


def parse_yyyymmdd(s: str) -> date | None:
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError:
        return None


def edition_span_days(level: str, draw_size: int | None) -> int:
    """Upper bound on how many days after its start date an edition can run."""
    if level == "G":
        return 15
    if level == "D":
        return 3
    if draw_size and draw_size >= 96:
        return 13
    return 8


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)


def to_int(s: str | None) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def to_float(s: str | None) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
