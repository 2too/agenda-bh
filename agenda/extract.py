"""Turns RawItems into raw event dicts.

1. JSON-LD / API data is converted directly (free, no AI call).
2. Otherwise the text (and, for Instagram, the flyer image) goes to Gemini,
   which answers with JSON in a fixed shape.

Raw event dict shape (strings may be empty):
  title, start, end, venue, address, neighborhood, price_text,
  category, description, url
`start`/`end` are ISO strings ("2026-11-28T21:00" or "2026-11-28") or
Portuguese text that normalize.py can parse.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.request
from datetime import datetime

from .config import Settings, TZ
from .models import CATEGORIES, RawItem

log = logging.getLogger(__name__)

# ---------- JSON-LD ----------

CATEGORY_KEYWORDS = [
    ("Música", r"show|música|musica|concert|samba|choro|jazz|rock|orquestra|sarau|dj|forró\s+ao\s+vivo|MusicEvent"),
    ("Dança", r"dança|danca|ballet|balé|forró|DanceEvent"),
    ("Teatro", r"teatro|espetáculo|peça|stand.?up|comédia|TheaterEvent|ComedyEvent"),
    ("Cinema", r"cinema|filme|mostra de cinema|sessão|ScreeningEvent"),
    ("Exposição", r"exposição|exposicao|mostra|galeria|museu|ExhibitionEvent|VisualArtsEvent"),
    ("Oficina", r"oficina|workshop|curso|aula|vivência|EducationEvent"),
    ("Palestra", r"palestra|debate|roda de conversa|seminário|encontro|lançamento de livro|LiteraryEvent"),
    ("Feira", r"feira|bazar|mercado|SaleEvent|FoodEvent"),
    ("Infantil", r"infantil|crianças|criancas|kids|ChildrensEvent"),
    ("Festa", r"festa|baile|carnaval|bloco|SocialEvent"),
]


def guess_category(text: str) -> str:
    for cat, pattern in CATEGORY_KEYWORDS:
        if re.search(pattern, text or "", re.IGNORECASE):
            return cat
    return "Outro"


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def jsonld_to_raw(ev: dict, fallback_url: str) -> dict:
    loc = _first(ev.get("location")) or {}
    if isinstance(loc, str):
        loc = {"name": loc}
    addr = loc.get("address") or {}
    if isinstance(addr, dict):
        neighborhood = addr.get("addressLocality") if addr.get("addressLocality", "").lower() not in ("belo horizonte", "") else ""
        addr_txt = ", ".join(x for x in [addr.get("streetAddress"), addr.get("addressLocality")] if x)
    else:
        neighborhood, addr_txt = "", str(addr)
    offers = _first(ev.get("offers")) or {}
    price = ""
    if isinstance(offers, dict):
        p = offers.get("price") or offers.get("lowPrice")
        hp = offers.get("highPrice")
        price = f"R$ {p}" + (f"–{hp}" if hp and hp != p else "") if p not in (None, "") else ""
        if str(p) in ("0", "0.0", "0.00"):
            price = "Grátis"
    if ev.get("isAccessibleForFree") is True:
        price = "Grátis"
    types = ev.get("@type")
    kind = " ".join(types) if isinstance(types, list) else str(types or "")
    name = re.sub(r"\s+", " ", str(ev.get("name") or "")).strip()
    return {
        "title": name,
        "start": str(ev.get("startDate") or ""),
        "end": str(ev.get("endDate") or ""),
        "venue": loc.get("name", "") if isinstance(loc, dict) else "",
        "address": addr_txt,
        "neighborhood": neighborhood or "",
        "price_text": price,
        "category": guess_category(f"{kind} {ev.get('keywords', '')} {name}"),
        "description": re.sub(r"\s+", " ", str(ev.get("description") or ""))[:400],
        "url": str(ev.get("url") or fallback_url),
    }


# ---------- Gemini ----------

PROMPT = """Você extrai eventos culturais de textos e imagens para uma agenda de Belo Horizonte (MG).

Data de referência: {ref_date} ({ref_weekday}). {pub_line}
Fonte: {source} — {url}
{venue_line}
{extra_line}
Regras:
- Inclua só eventos em Belo Horizonte ou região metropolitana, que acontecem a partir da data de referência (ou exposições ainda em cartaz).
- Inclua atividades de cultura e de aprendizado de hobbies: shows, teatro, dança, cinema, exposições, oficinas, aulas abertas, palestras, feiras, festas.
- Ignore propaganda, sorteios, notícias sem data e eventos já encerrados.
- "start" e "end" em ISO 8601 sem fuso: "AAAA-MM-DDTHH:MM", ou "AAAA-MM-DD" se não houver horário. Resolva datas relativas ("sábado", "amanhã", "dia 28") usando a data de referência{pub_hint}. Se o ano não estiver escrito, use o próximo dia futuro que corresponda.
- Mesmo evento em vários dias separados: uma entrada por dia. Temporada ou exposição contínua: uma entrada com start e end.
- "price_text": como está escrito ("Grátis", "R$ 40", "R$ 30 a R$ 60"). Vazio se não informado.
- "category": uma destas: {categories}.
- "description": no máximo 2 frases, em português, sem emojis.
- Não invente: campo desconhecido = "".
- Se não houver eventos, devolva {{"events": []}}.

Responda SOMENTE com JSON neste formato:
{{"events": [{{"title": "", "start": "", "end": "", "venue": "", "address": "", "neighborhood": "", "price_text": "", "category": "", "description": "", "url": ""}}]}}

Conteúdo:
---
{text}
---"""

WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def build_prompt(item: RawItem, now: datetime) -> str:
    ref = now.astimezone(TZ)
    pub = item.published_at.astimezone(TZ) if item.published_at else None
    return PROMPT.format(
        ref_date=ref.date().isoformat(), ref_weekday=WEEKDAYS[ref.weekday()],
        pub_line=f"O post foi publicado em {pub.date().isoformat()} ({WEEKDAYS[pub.weekday()]})." if pub else "",
        pub_hint=" — para posts, datas relativas contam a partir da data de publicação" if pub else "",
        source=item.source, url=item.url,
        venue_line=f"Local provável (se o texto não disser outro): {item.venue_hint}" if item.venue_hint else "",
        extra_line=f"Instrução específica desta fonte (tem prioridade): {item.extract_hint}\n" if item.extract_hint else "",
        categories=", ".join(CATEGORIES), text=item.text[:30_000] or "(sem texto — leia a imagem)",
    )


def parse_llm_json(text: str) -> list[dict]:
    """Tolerates ```json fences and stray text around the JSON object."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    events = data.get("events", []) if isinstance(data, dict) else []
    return [e for e in events if isinstance(e, dict) and e.get("title")]


def content_hash(item: RawItem) -> str:
    h = hashlib.sha256()
    h.update(item.url.encode())
    h.update(item.text.encode())
    for u in item.image_urls[:1]:
        h.update(u.split("?")[0].encode())   # CDN query strings change; the path doesn't
    return h.hexdigest()[:24]


def _download(url: str, limit: int = 8_000_000) -> tuple[bytes, str] | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(limit + 1)
            mime = r.headers.get_content_type() or "image/jpeg"
        if len(data) > limit or not mime.startswith("image/"):
            return None
        return data, mime
    except Exception as e:
        log.info("image download failed %s (%s)", url, e)
        return None


SKIP_MODEL_WORDS = ("image", "tts", "live", "audio", "embedding", "exp", "learnlm", "robotics", "computer")


def pick_model(models: list[tuple[str, list[str]]]) -> str | None:
    """Choose the best general 'flash' model from (name, supported_actions) pairs.

    Stable before preview (previews have tighter free limits), then newest, then full before lite.
    """
    best, best_rank = None, None
    for name, actions in models:
        name = name.split("/")[-1]
        if actions and "generateContent" not in actions:
            continue
        if "flash" not in name or any(w in name for w in SKIP_MODEL_WORDS):
            continue
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        version = float(m.group(1)) if m else 0.0
        rank = ("preview" not in name, version, "lite" not in name, "latest" not in name, -len(name))
        if best_rank is None or rank > best_rank:
            best, best_rank = name, rank
    return best


class GeminiExtractor:
    """Thin wrapper with pacing and retry, so the free tier isn't exceeded."""

    def __init__(self, settings: Settings, min_interval: float = 6.0):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (see README, step 3)")
        from google import genai
        from google.genai import types
        self._client = genai.Client(api_key=settings.gemini_api_key,
                                    http_options=types.HttpOptions(api_version="v1beta"))
        self._model = settings.gemini_model
        self._picked = False
        self._min_interval = min_interval
        self._last = 0.0
        self.calls = 0

    def _pick_available_model(self) -> str:
        """Ask Google which models this key can use and choose one."""
        pairs = []
        for m in self._client.models.list():
            actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
            pairs.append((m.name or "", list(actions)))
        name = pick_model(pairs)
        if not name:
            raise RuntimeError("no Gemini 'flash' model available for this key: "
                               + ", ".join(n.split("/")[-1] for n, _ in pairs[:15]))
        log.info("Gemini model %r not available; using %r instead", self._model, name)
        return name

    def _wait(self):
        gap = time.monotonic() - self._last
        if gap < self._min_interval:
            time.sleep(self._min_interval - gap)
        self._last = time.monotonic()

    def extract(self, item: RawItem, now: datetime, with_image: bool) -> list[dict]:
        from google.genai import types
        parts: list = [build_prompt(item, now)]
        if with_image and item.image_urls:
            img = _download(item.image_urls[0])
            if img:
                parts.append(types.Part.from_bytes(data=img[0], mime_type=img[1]))
        config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
        for attempt in range(5):
            self._wait()
            try:
                resp = self._client.models.generate_content(model=self._model, contents=parts, config=config)
                self.calls += 1
                return parse_llm_json(resp.text or "")
            except Exception as e:
                msg = str(e)
                if ("404" in msg or "NOT_FOUND" in msg) and not self._picked:
                    self._picked = True          # only once per run
                    self._model = self._pick_available_model()
                    continue
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg:
                    wait = 30 * (attempt + 1)
                    log.warning("Gemini busy/limited, waiting %ss (%s)", wait, msg[:120])
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("Gemini kept refusing (rate limit). Try again later.")
