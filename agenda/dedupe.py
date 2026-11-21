"""Merges the same event found in more than one place."""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from .models import Event
from .normalize import fold

STOP = {"de", "da", "do", "das", "dos", "e", "em", "no", "na", "com", "a", "o", "as", "os",
        "show", "apresenta", "bh", "belo", "horizonte", "evento", "ao", "vivo"}


def title_key(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", fold(title))
    return " ".join(w for w in words if w not in STOP) or fold(title)


def event_key(e: Event) -> str:
    """Stable id: same title words + same start day."""
    base = f"{title_key(e.title)}|{e.start.date().isoformat()}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def _similar(a: str, b: str, threshold: float) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) > 8 and len(b) > 8 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def same_event(a: Event, b: Event) -> bool:
    if a.start.date() != b.start.date():
        return False
    if not _similar(title_key(a.title), title_key(b.title), 0.82):
        return False
    va, vb = fold(a.venue), fold(b.venue)
    if va and vb and not _similar(va, vb, 0.5):
        return False
    if a.has_time and b.has_time and abs((a.start - b.start).total_seconds()) > 3 * 3600:
        return False   # same show, different session
    return True


def _filled(e: Event) -> int:
    return sum(bool(x) for x in (e.venue, e.address, e.neighborhood, e.description, e.price_text,
                                 e.has_time, e.end, e.image_url)) + (0 if e.is_instagram else 1)


def merge(a: Event, b: Event) -> Event:
    best, other = (a, b) if _filled(a) >= _filled(b) else (b, a)
    for f in ("venue", "address", "neighborhood", "description", "price_text", "image_url"):
        if not getattr(best, f) and getattr(other, f):
            setattr(best, f, getattr(other, f))
    if not best.end and other.end:
        best.end = other.end
    if not best.has_time and other.has_time:
        best.start, best.has_time = other.start, True
    if best.price_min is None and other.price_min is not None:
        best.price_min, best.price_max, best.is_free = other.price_min, other.price_max, other.is_free
    if best.category == "Outro":
        best.category = other.category
    return best


def dedupe(events: list[Event]) -> list[Event]:
    out: list[Event] = []
    for e in sorted(events, key=lambda x: x.start):
        for i, kept in enumerate(out):
            if same_event(kept, e):
                out[i] = merge(kept, e)
                break
        else:
            out.append(e)
    for e in out:
        e.key = event_key(e)
    return out
