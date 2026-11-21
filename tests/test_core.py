from datetime import datetime, timedelta

import pytest

from agenda.config import TZ, fill_url, run_window
from agenda.dedupe import dedupe, same_event
from agenda.extract import jsonld_to_raw, parse_llm_json, guess_category
from agenda.fetchers.mapas import occurrence_to_schema
from agenda.fetchers.web import extract_jsonld_events, matching_links, visible_text
from agenda.models import Event, RawItem
from agenda.normalize import in_window, parse_price, parse_when, price_label, to_event
from agenda.storage import Store

REF = datetime(2026, 11, 21, 6, 0, tzinfo=TZ)  # a Saturday


# ---------- dates ----------
@pytest.mark.parametrize("text, expected, has_time", [
    ("2026-11-28T21:00", datetime(2026, 11, 28, 21, 0), True),
    ("2026-11-28", datetime(2026, 11, 28), False),
    ("2026-11-28T21:00:00-03:00", datetime(2026, 11, 28, 21, 0), True),
    ("28/11 às 21h", datetime(2026, 11, 28, 21, 0), True),
    ("sáb, 28/11 - 21h30", datetime(2026, 11, 28, 21, 30), True),
    ("28 de novembro", datetime(2026, 11, 28), False),
    ("5 dez 2026, 19:00", datetime(2026, 12, 5, 19, 0), True),
    ("10/01", datetime(2027, 1, 10), False),          # January after November = next year
    ("Até 15 de dezembro", datetime(2026, 12, 15), False),
])
def test_parse_when(text, expected, has_time):
    dt, ht = parse_when(text, REF)
    assert dt.replace(tzinfo=None) == expected
    assert ht is has_time


def test_parse_when_garbage():
    assert parse_when("em breve", REF) == (None, False)
    assert parse_when("", REF) == (None, False)


# ---------- prices ----------
@pytest.mark.parametrize("text, mn, mx, free", [
    ("Grátis", 0, 0, True),
    ("Entrada franca", 0, 0, True),
    ("R$ 40", 40, 40, False),
    ("R$ 30 a R$ 60", 30, 60, False),
    ("Ingressos a partir de R$ 25,50", 25.5, 25.5, False),
    ("", None, None, False),
])
def test_parse_price(text, mn, mx, free):
    assert parse_price(text) == (mn, mx, free)


def test_price_label():
    e = Event("x", REF, "s", "u", price_min=30.0, price_max=60.0)
    assert price_label(e) == "R$ 30–60"
    e.is_free = True
    assert price_label(e) == "Grátis"


# ---------- to_event / window ----------
def test_to_event_cleans_and_guesses():
    raw = {"title": "🎶 SAMBA DE RODA NO TERRAÇO", "start": "2026-11-28T21:00", "venue": "Mercado Novo",
           "price_text": "R$ 20", "category": "não sei"}
    e = to_event(raw, "@x", "https://ig/p/1", REF, is_instagram=True)
    assert e.title == "Samba de roda no terraço"
    assert e.neighborhood == "Centro"
    assert e.category == "Música"
    assert e.price_min == 20 and e.is_instagram


def test_in_window_includes_running_exhibition():
    a, b = run_window(REF, 14)
    expo = Event("Expo", REF - timedelta(days=30), "s", "u", end=REF + timedelta(days=20))
    old = Event("Old", REF - timedelta(days=30), "s", "u")
    later = Event("Later", REF + timedelta(days=20), "s", "u")
    assert in_window(expo, a, b) and not in_window(old, a, b) and not in_window(later, a, b)


def test_fill_url():
    u = fill_url("https://x/agenda?date_from={date_from}&date_to={date_to}", REF, 14)
    assert "2026-11-21T00%3A00%3A00-03%3A00" in u and "2026-12-05T23%3A59%3A59" in u


# ---------- dedupe ----------
def _ev(title, h=21, venue="Mercado Novo", src="site", **kw):
    return Event(title, datetime(2026, 11, 28, h, 0, tzinfo=TZ), src, "https://x/" + src, venue=venue, **kw)


def test_dedupe_merges_same_event_from_site_and_instagram():
    a = _ev("Samba de Roda no Mercado Novo", src="Agenda BH", description="Roda no terraço")
    b = _ev("SAMBA DE RODA", src="@ig", is_instagram=True, price_min=20.0, price_max=20.0, price_text="R$ 20")
    out = dedupe([a, b])
    assert len(out) == 1
    assert out[0].price_min == 20 and out[0].description == "Roda no terraço"
    assert out[0].source == "Agenda BH"   # website version preferred


def test_dedupe_keeps_different_sessions_and_venues():
    assert not same_event(_ev("Show X", h=15), _ev("Show X", h=21))
    assert not same_event(_ev("Show X", venue="Palácio das Artes"), _ev("Show X", venue="Sesc Palladium"))


# ---------- extraction helpers ----------
HTML = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"MusicEvent","name":"Orquestra no Parque",
"startDate":"2026-11-29T11:00:00-03:00","location":{"@type":"Place","name":"Parque Municipal",
"address":{"streetAddress":"Av. Afonso Pena, 1377","addressLocality":"Belo Horizonte"}},
"offers":{"price":"0","priceCurrency":"BRL"},"url":"https://x/orquestra"}]}
</script></head><body><nav>menu</nav><main><h1>Agenda</h1><a href="/2026/11/show-grande-no-centro/">a</a>
<a href="/category/show/">c</a><script>var x=1</script></main></body></html>"""


def test_jsonld_extraction_and_conversion():
    evs = extract_jsonld_events(HTML)
    assert len(evs) == 1
    raw = jsonld_to_raw(evs[0], "https://x")
    assert raw["price_text"] == "Grátis" and raw["category"] == "Música"
    e = to_event(raw, "Teste", "https://x", REF)
    assert e.is_free and e.neighborhood == "Centro" and e.start.hour == 11


def test_visible_text_and_links():
    assert "menu" not in visible_text(HTML) and "var x" not in visible_text(HTML)
    links = matching_links(HTML, "https://x.com/", [r"/\d{4}/\d{2}/"], ["/category/"], 10)
    assert links == ["https://x.com/2026/11/show-grande-no-centro/"]


def test_parse_llm_json_tolerates_fences():
    txt = '```json\n{"events":[{"title":"A","start":"2026-11-28"},{"title":""}]}\n```'
    assert parse_llm_json(txt) == [{"title": "A", "start": "2026-11-28"}]
    assert parse_llm_json("não é json") == []


def test_mapas_occurrence():
    o = {"id": 7, "name": "Sarau", "starts_on": "2026-11-27", "starts_at": "2026-11-27 19:00:00",
         "space": {"name": "Centro Cultural Lagoa do Nado", "endereco": "Rua X"}, "rule": {"price": "Gratuito"},
         "terms": {"linguagem": ["Literatura"]}}
    raw = jsonld_to_raw(occurrence_to_schema(o, "https://m"), "https://m")
    e = to_event(raw, "Mapa", "https://m", REF)
    assert e.start.hour == 19 and e.is_free and e.url == "https://m/evento/7/"


def test_guess_category():
    assert guess_category("Oficina de cerâmica") == "Oficina"
    assert guess_category("Qualquer coisa") == "Outro"


# ---------- storage ----------
def test_store_new_flag_and_cache(tmp_path):
    s = Store(tmp_path / "t.db")
    a, b = run_window(REF, 14)
    e = dedupe([_ev("Show Y")])
    s.upsert(e, REF)
    assert s.window(a, b, REF)[0].is_new
    later = REF + timedelta(days=7)
    s.upsert(dedupe([_ev("Show Y!")]), later)            # same event seen again a week later
    rows = s.window(*run_window(REF, 14), later)
    assert len(rows) == 1 and not rows[0].is_new
    s.save_extraction("h", "src", "u", [{"title": "A"}], REF)
    assert s.cached_extraction("h") == [{"title": "A"}]
    s.save_run(REF, [{"name": "x", "ok": True, "events": 1, "note": ""}])
    assert s.last_run()[0]["name"] == "x"


# ---------- runner with fakes (no internet, no AI) ----------
def test_runner_with_fake_ai(monkeypatch, tmp_path):
    from agenda import main as M
    from agenda.config import Settings

    class FakeAI:
        calls = 0
        def extract(self, item, now, with_image):
            FakeAI.calls += 1
            assert with_image   # short caption -> flyer is read
            return [{"title": "Oficina de bordado", "start": "sábado 28/11 14h", "price_text": "R$ 80",
                     "venue": "Ateliê", "category": "Oficina"}]

    settings = Settings(db_path=tmp_path / "e.db", site_dir=tmp_path / "site")
    store = Store(settings.db_path)
    r = M.Runner(settings, REF, store)
    r._ai = FakeAI()
    item = RawItem(source="@x", url="https://ig/p/1", text="Vem bordar com a gente!", image_urls=["https://img"])
    monkeypatch.setattr(r, "fetch", lambda src: [item])
    res, evs = r.run_source({"name": "@x", "type": "instagram"})
    assert res.ok and len(evs) == 1 and evs[0].start.hour == 14
    r.run_source({"name": "@x", "type": "instagram"})   # unchanged post: cache, no 2nd AI call
    assert FakeAI.calls == 1


def test_runner_failure_is_reported_not_raised(monkeypatch, tmp_path):
    from agenda import main as M
    from agenda.config import Settings
    r = M.Runner(Settings(db_path=tmp_path / "e.db"), REF, None)
    def boom(src):
        raise TimeoutError("site down")
    monkeypatch.setattr(r, "fetch", boom)
    res, evs = r.run_source({"name": "Site"})
    assert not res.ok and "site down" in res.note and evs == []


def test_source_hint_reaches_prompt_and_config_loads():
    from agenda.config import load_settings
    from agenda.extract import build_prompt
    s = load_settings()
    cine = next(x for x in s.sources if x["name"] == "Cine Belas Artes")
    item = RawItem(source=cine["name"], url=cine["url"], text="19 A 25 DE NOVEMBRO",
                   venue_hint=cine["default_venue_hint"], extract_hint=cine["extract_hint"])
    p = build_prompt(item, REF)
    assert "UMA" in p and "Lourdes" in p and "tem prioridade" in p
    e = to_event({"title": "Peixe Abissal", "start": "2026-11-19", "end": "2026-11-25",
                  "venue": "Cine Belas Artes", "category": "Cinema"}, "Cine Belas Artes", cine["url"], REF)
    assert e.neighborhood == "Lourdes" and in_window(e, *run_window(REF, 14))
