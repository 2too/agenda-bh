"""Mapa Cultural BH runs on Mapas Culturais, which has a public JSON API.

The API answer is turned into schema.org-style Event dicts so the same
converter used for JSON-LD handles it. If the API call fails or returns
nothing, main.py falls back to reading the page with the web fetcher.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlencode

from ..config import Settings, TZ
from ..models import RawItem

log = logging.getLogger(__name__)


def _get_json(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": "AgendaBH/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def occurrence_to_schema(o: dict, base: str) -> dict | None:
    """Map one findOccurrences row to a schema.org Event dict (defensive: field names vary by version)."""
    name = o.get("name")
    day = o.get("starts_on") or (o.get("rule") or {}).get("startsOn")
    if not name or not day:
        return None
    t = (o.get("starts_at") or (o.get("rule") or {}).get("startsAt") or "")
    t = t[11:16] if len(t) > 8 else t[:5]           # "2026-11-28 19:00:00" or "19:00"
    start = f"{day}T{t}" if t else day
    end_day = o.get("ends_on") or (o.get("rule") or {}).get("until")
    space = o.get("space") if isinstance(o.get("space"), dict) else {}
    price = (o.get("rule") or {}).get("price") or o.get("price") or ""
    terms = o.get("terms") or {}
    ev_id = o.get("id")
    return {
        "@type": "Event",
        "name": name,
        "startDate": start,
        "endDate": end_day if end_day and end_day != day else None,
        "description": o.get("shortDescription") or "",
        "location": {"name": space.get("name", ""), "address": space.get("endereco", "")},
        "offers": {"price": price},
        "keywords": ", ".join(terms.get("linguagem", []) if isinstance(terms, dict) else []),
        "url": o.get("singleUrl") or (f"{base}/evento/{ev_id}/" if ev_id else base),
    }


def fetch_mapas_source(src: dict, settings: Settings, now: datetime) -> list[RawItem]:
    base = src["url"].rstrip("/")
    start = now.astimezone(TZ).date()
    end = start + timedelta(days=settings.window_days)
    params = {
        "@from": start.isoformat(),
        "@to": end.isoformat(),
        "@select": "id,name,shortDescription,terms,singleUrl",
        "@order": "starts_on ASC",
    }
    url = f"{base}/api/event/findOccurrences?{urlencode(params)}"
    data = _get_json(url)
    if not isinstance(data, list):
        raise ValueError("unexpected API answer")
    events = [e for e in (occurrence_to_schema(o, base) for o in data) if e]
    log.info("%s: API returned %d occurrences", src["name"], len(events))
    return [RawItem(source=src["name"], url=base + "/eventos/", jsonld_events=events)]
