"""Cleans raw event dicts into Event objects: dates, prices, categories, places."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, time as dtime
from typing import Optional

from .config import TZ
from .extract import guess_category
from .models import CATEGORIES, Event

MONTHS = {
    "jan": 1, "janeiro": 1, "fev": 2, "fevereiro": 2, "mar": 3, "marco": 3, "março": 3,
    "abr": 4, "abril": 4, "mai": 5, "maio": 5, "jun": 6, "junho": 6, "jul": 7, "julho": 7,
    "ago": 8, "agosto": 8, "set": 9, "setembro": 9, "out": 10, "outubro": 10,
    "nov": 11, "novembro": 11, "dez": 12, "dezembro": 12,
}

# Well-known venues -> neighbourhood (only used when the source gives none)
VENUE_HOODS = {
    "palacio das artes": "Centro", "sesc palladium": "Centro", "mercado novo": "Centro",
    "teatro francisco nunes": "Centro", "parque municipal": "Centro", "mercado central": "Centro",
    "cine theatro brasil": "Centro", "ccbb": "Funcionários", "centro cultural banco do brasil": "Funcionários",
    "praca da liberdade": "Funcionários", "biblioteca publica estadual": "Funcionários",
    "memorial minas gerais": "Funcionários", "mm gerdau": "Funcionários", "museu das minas e do metal": "Funcionários",
    "mineirao": "Pampulha", "mineirinho": "Pampulha", "lagoa da pampulha": "Pampulha",
    "museu de arte da pampulha": "Pampulha", "belas artes": "Lourdes", "praca da estacao": "Centro", "viaduto santa tereza": "Centro",
}


def fold(s: str) -> str:
    """lower-case, no accents, single spaces."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


def _infer_year(month: int, day: int, ref: datetime) -> int:
    year = ref.year
    try:
        candidate = datetime(year, month, day, tzinfo=TZ)
    except ValueError:
        return year
    # a date more than ~2 months in the past most likely means next year
    if candidate < ref - timedelta(days=60):
        return year + 1
    return year


def _find_time(s: str) -> Optional[dtime]:
    m = re.search(r"\b(\d{1,2})\s*(?:h|:)\s*(\d{2})?\b", s)
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2) or 0)
    if 0 <= h <= 23 and 0 <= mnt <= 59:
        return dtime(h, mnt)
    return None


def parse_when(value: str, ref: datetime) -> tuple[Optional[datetime], bool]:
    """Parse ISO or Portuguese date text. Returns (aware datetime, has_time)."""
    s = (value or "").strip()
    if not s:
        return None, False
    # ISO first
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        has_time = "T" in s or " " in s.strip()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        dt = dt.astimezone(TZ)
        if dt.hour == 0 and dt.minute == 0:
            has_time = False   # midnight is almost always "time unknown"
        return dt, has_time
    except ValueError:
        pass

    f = fold(s)
    day = month = year = None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", f)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            year += 2000 if year < 100 else 0
    else:
        m = re.search(r"\b(\d{1,2})\s*(?:de\s+)?([a-z]{3,9})\.?(?:\s+(?:de\s+)?(\d{4}))?", f)
        if m and m.group(2) in MONTHS:
            day, month = int(m.group(1)), MONTHS[m.group(2)]
            year = int(m.group(3)) if m.group(3) else None
    if not (day and month and 1 <= month <= 12):
        return None, False
    year = year or _infer_year(month, day, ref)
    t = _find_time(re.sub(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", " ", f))
    try:
        dt = datetime(year, month, day, t.hour if t else 0, t.minute if t else 0, tzinfo=TZ)
    except ValueError:
        return None, False
    return dt, t is not None


FREE_WORDS = ("gratis", "gratuito", "gratuita", "entrada franca", "free", "livre", "0,00")


def parse_price(text: str) -> tuple[Optional[float], Optional[float], bool]:
    f = fold(text)
    if not f:
        return None, None, False
    if any(w in f for w in FREE_WORDS) and not re.search(r"r\$\s*[1-9]", f):
        return 0.0, 0.0, True
    nums = [float(n.replace(".", "").replace(",", ".")) for n in re.findall(r"\d{1,4}(?:\.\d{3})*(?:,\d{1,2})?", f)]
    nums = [n for n in nums if 0 < n < 10_000]
    if not nums:
        return None, None, False
    return min(nums), max(nums), False


def price_label(e: Event) -> str:
    if e.is_free:
        return "Grátis"
    if e.price_min is None:
        return e.price_text or "Ver preço"
    fmt = lambda n: f"{n:.0f}" if n == int(n) else f"{n:.2f}".replace(".", ",")
    if e.price_max and e.price_max != e.price_min:
        return f"R$ {fmt(e.price_min)}–{fmt(e.price_max)}"
    return f"R$ {fmt(e.price_min)}"


def clean_title(t: str) -> str:
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", t or "")
    t = re.sub(r"\s+", " ", t).strip(" -–|·")
    if t.isupper() and len(t) > 6:
        t = t.capitalize()
    return t[:140]


def guess_neighborhood(venue: str, address: str) -> str:
    hay = fold(f"{venue} {address}")
    for key, hood in VENUE_HOODS.items():
        if key in hay:
            return hood
    m = re.search(r"-\s*([A-Za-zÀ-ú ]{3,30}),\s*belo horizonte", f"{address}", re.IGNORECASE)
    return m.group(1).strip().title() if m else ""


def to_event(raw: dict, source: str, item_url: str, ref: datetime, is_instagram: bool = False,
             image_url: str = "") -> Optional[Event]:
    title = clean_title(str(raw.get("title", "")))
    if len(title) < 3:
        return None
    start, has_time = parse_when(str(raw.get("start", "")), ref)
    if not start:
        return None
    end, _ = parse_when(str(raw.get("end", "")), ref)
    if end and end < start:
        end = None
    pmin, pmax, free = parse_price(str(raw.get("price_text", "")))
    cat = str(raw.get("category", "")).strip()
    if cat not in CATEGORIES:
        cat = guess_category(f"{cat} {title}")
    venue = re.sub(r"\s+", " ", str(raw.get("venue", ""))).strip()[:120]
    address = re.sub(r"\s+", " ", str(raw.get("address", ""))).strip()[:200]
    hood = str(raw.get("neighborhood", "")).strip()[:60] or guess_neighborhood(venue, address)
    url = str(raw.get("url") or "").strip()
    if not url.startswith("http"):
        url = item_url
    ev = Event(
        title=title, start=start, end=end, has_time=has_time, source=source, url=url,
        venue=venue, address=address, neighborhood=hood,
        price_min=pmin, price_max=pmax, is_free=free, price_text=str(raw.get("price_text", ""))[:60],
        category=cat, description=re.sub(r"\s+", " ", str(raw.get("description", ""))).strip()[:300],
        image_url=image_url, is_instagram=is_instagram,
    )
    return ev


def in_window(e: Event, win_start: datetime, win_end: datetime) -> bool:
    """Starts inside the window, or is a run (exhibition) still on during it."""
    if win_start <= e.start < win_end:
        return True
    return bool(e.end and e.start < win_start <= e.end)
