"""Shared data shapes."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

CATEGORIES = [
    "Música", "Dança", "Teatro", "Exposição", "Cinema",
    "Oficina", "Palestra", "Feira", "Infantil", "Festa", "Outro",
]


@dataclass
class RawItem:
    """Something a fetcher found, before the AI turns it into events."""
    source: str
    url: str
    text: str = ""
    image_urls: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None   # Instagram post date, article date
    jsonld_events: list[dict] = field(default_factory=list)
    venue_hint: str = ""
    extract_hint: str = ""     # extra instructions for the AI, per source
    raw_events: list[dict] = field(default_factory=list)   # ready-made events (manual lists)


@dataclass
class Event:
    title: str
    start: datetime                     # timezone-aware, America/Sao_Paulo
    source: str
    url: str
    end: Optional[datetime] = None
    has_time: bool = True               # False when only the date is known
    venue: str = ""
    address: str = ""
    neighborhood: str = ""
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    is_free: bool = False
    price_text: str = ""
    category: str = "Outro"
    description: str = ""
    image_url: str = ""
    is_instagram: bool = False
    # filled in by storage
    key: str = ""
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    is_new: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("start", "end", "first_seen", "last_seen"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d


@dataclass
class SourceResult:
    name: str
    ok: bool
    events: int = 0
    note: str = ""
