"""SQLite storage: events, an AI-result cache, and run reports."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .dedupe import merge, same_event
from .models import Event
from .normalize import in_window

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  key TEXT PRIMARY KEY,
  start TEXT NOT NULL,
  data TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_start ON events(start);
CREATE TABLE IF NOT EXISTS extracted (
  hash TEXT PRIMARY KEY,
  source TEXT, url TEXT, at TEXT,
  raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  at TEXT PRIMARY KEY,
  report TEXT NOT NULL
);
"""


def _event_from_row(data: str, first_seen: str, last_seen: str) -> Event:
    d = json.loads(data)
    for k in ("start", "end"):
        d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
    d["first_seen"] = datetime.fromisoformat(first_seen)
    d["last_seen"] = datetime.fromisoformat(last_seen)
    d.pop("is_new", None)
    return Event(**d)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # ----- AI cache: unchanged page/post -> reuse last answer, no new AI call -----
    def cached_extraction(self, h: str) -> list[dict] | None:
        row = self.db.execute("SELECT raw FROM extracted WHERE hash=?", (h,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_extraction(self, h: str, source: str, url: str, raw: list[dict], now: datetime):
        self.db.execute("INSERT OR REPLACE INTO extracted VALUES (?,?,?,?,?)",
                        (h, source, url, now.isoformat(), json.dumps(raw, ensure_ascii=False)))
        self.db.commit()

    # ----- events -----
    def _upcoming(self, since: datetime) -> list[Event]:
        """Events that start after `since` or are runs still on at `since` (a few thousand rows at most)."""
        rows = self.db.execute("SELECT data, first_seen, last_seen FROM events").fetchall()
        cutoff = since - timedelta(days=1)
        out = []
        for r in rows:
            e = _event_from_row(*r)
            if e.start >= cutoff or (e.end and e.end >= cutoff):
                out.append(e)
        return out

    def upsert(self, events: list[Event], now: datetime) -> None:
        existing = {e.key: e for e in self._upcoming(now)}
        for e in events:
            match = existing.get(e.key) or next((x for x in existing.values() if same_event(x, e)), None)
            if match:
                merged = merge(match, e)
                merged.key, first = match.key, match.first_seen
            else:
                merged, first = e, now
            merged.first_seen, merged.last_seen = first, now
            existing[merged.key] = merged
            d = merged.to_dict()
            for k in ("first_seen", "last_seen", "is_new"):
                d.pop(k, None)
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "start=excluded.start, data=excluded.data, last_seen=excluded.last_seen",
                (merged.key, merged.start.isoformat(), json.dumps(d, ensure_ascii=False),
                 first.isoformat(), now.isoformat()))
        self.db.commit()

    def window(self, win_start: datetime, win_end: datetime, now: datetime,
               stale_days: int = 10, new_days: int = 6) -> list[Event]:
        """Events in the window, seen recently. Keeps last week's finds if a source failed today."""
        out = []
        for e in self._upcoming(win_start):
            if not in_window(e, win_start, win_end):
                continue
            if e.last_seen < now - timedelta(days=stale_days):
                continue
            e.is_new = e.first_seen >= now - timedelta(days=new_days)
            out.append(e)
        return sorted(out, key=lambda x: (x.start, x.title))

    # ----- run reports -----
    def save_run(self, now: datetime, report: list[dict]):
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?)",
                        (now.isoformat(), json.dumps(report, ensure_ascii=False)))
        self.db.commit()

    def last_run(self) -> list[dict]:
        row = self.db.execute("SELECT report FROM runs ORDER BY at DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else []

    def close(self):
        self.db.close()
