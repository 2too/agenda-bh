"""Weekly run: fetch every source, extract, clean, merge, save, publish.

  python -m agenda.main                 full run (no e-mail)
  python -m agenda.main --email         full run + send the newsletter
  python -m agenda.main --source Sympla test one source (nothing saved)
  python -m agenda.main --dry-run       run everything but save nothing, send nothing
  python -m agenda.main --rebuild       rebuild the dashboard from saved data only
  python -m agenda.main --sample        build the dashboard from sample data (no internet)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings, TZ, load_settings, run_window
from .dedupe import dedupe
from .extract import GeminiExtractor, content_hash, jsonld_to_raw
from .models import Event, RawItem, SourceResult
from .normalize import in_window, to_event
from .output import build_site, newsletter_html, send_newsletter
from .storage import Store

log = logging.getLogger("agenda")


def setup_logging(settings: Settings, now: datetime) -> Path:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    path = settings.log_dir / f"run-{now:%Y%m%d-%H%M}.log"
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt,
                        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()])
    for noisy in ("httpx", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


class Runner:
    def __init__(self, settings: Settings, now: datetime, store: Store | None):
        self.s = settings
        self.now = now
        self.store = store
        self.win_start, self.win_end = run_window(now, settings.window_days)
        self._browser = None
        self._ai = None
        self._ai_error = ""

    # lazy helpers -------------------------------------------------
    def browser(self):
        if self._browser is None:
            from .fetchers.web import Browser
            self._browser = Browser().__enter__()
        return self._browser

    def ai(self):
        if self._ai is None and not self._ai_error:
            try:
                self._ai = GeminiExtractor(self.s)
            except Exception as e:
                self._ai_error = str(e)
        if self._ai is None:
            raise RuntimeError(self._ai_error)
        return self._ai

    def close(self):
        if self._browser is not None:
            self._browser.__exit__(None, None, None)

    # steps --------------------------------------------------------
    def fetch(self, src: dict) -> list[RawItem]:
        kind = src.get("type", "web")
        if kind == "instagram":
            from .fetchers.instagram import fetch_instagram_source
            return fetch_instagram_source(src, self.s)
        if kind == "mapas":
            from .fetchers.mapas import fetch_mapas_source
            try:
                items = fetch_mapas_source(src, self.s, self.now)
                if any(i.jsonld_events for i in items):
                    return items
                log.warning("%s: API returned no events, reading the page instead", src["name"])
            except Exception as e:
                log.warning("%s: API failed (%s), reading the page instead", src["name"], e)
            src = {**src, "type": "web", "url": src.get("fallback_url", src["url"]), "scroll": 3}
        from .fetchers.web import fetch_web_source
        return fetch_web_source(src, self.s, self.browser(), self.now)

    def extract(self, item: RawItem, is_ig: bool) -> list[dict]:
        if item.jsonld_events:
            return [jsonld_to_raw(ev, item.url) for ev in item.jsonld_events]
        if not item.text.strip() and not item.image_urls:
            return []
        h = content_hash(item)
        if self.store:
            cached = self.store.cached_extraction(h)
            if cached is not None:
                return cached
        # Instagram: read the flyer when the caption is short or lacks a date
        with_image = is_ig and (len(item.text) < 400 or not any(ch.isdigit() for ch in item.text))
        raws = self.ai().extract(item, self.now, with_image=with_image)
        if self.store:
            self.store.save_extraction(h, item.source, item.url, raws, self.now)
        return raws

    def run_source(self, src: dict) -> tuple[SourceResult, list[Event]]:
        name = src["name"]
        is_ig = src.get("type") == "instagram"
        events: list[Event] = []
        try:
            items = self.fetch(src)
            item_errors = 0
            for item in items:
                try:
                    raws = self.extract(item, is_ig)
                except Exception as e:
                    item_errors += 1
                    log.warning("%s: extraction failed for %s (%s)", name, item.url, e)
                    if item_errors >= 3 and not events:
                        raise RuntimeError(f"extraction keeps failing: {e}")
                    continue
                img = item.image_urls[0] if (item.image_urls and not is_ig) else ""
                for raw in raws:
                    ev = to_event(raw, name, item.url, self.now, is_instagram=is_ig, image_url=img)
                    if ev and in_window(ev, self.win_start, self.win_end):
                        events.append(ev)
            log.info("%s: %d item(s) read, %d event(s) in window", name, len(items), len(events))
            note = "nada encontrado" if not events else ""
            return SourceResult(name, True, len(events), note), events
        except Exception as e:
            log.error("%s: FAILED — %s", name, e)
            return SourceResult(name, False, 0, str(e)[:200]), []


def sample_events(now: datetime) -> list[Event]:
    base = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    def at(days, h=None, m=0):
        return base + timedelta(days=days, hours=h or 0, minutes=m), h is not None
    rows = [
        (-10, None, 25, "Exposição de fotografia (exemplo)", "CCBB BH", "Funcionários", "Exposição", True, None, False, False, "Culturadoria"),
        (3, 19, None, "Roda de conversa sobre literatura (exemplo)", "Biblioteca Pública Estadual", "Funcionários", "Palestra", True, None, False, False, "Portal Belo Horizonte"),
        (5, 19, None, "Aula aberta de forró (exemplo)", "Espaço de dança exemplo", "Savassi", "Dança", False, 30, True, True, "@rolesdocarmona"),
        (5, 20, None, "Choro na praça (exemplo)", "Praça da Liberdade", "Funcionários", "Música", True, None, True, True, "@strvbh"),
        (6, 19, None, "Espetáculo de dança contemporânea (exemplo)", "Palácio das Artes", "Centro", "Dança", False, 40, False, False, "Sympla"),
        (7, 10, None, "Oficina de cerâmica para iniciantes (exemplo)", "Ateliê exemplo", "Santa Tereza", "Oficina", False, 120, True, True, "@strvbh"),
        (7, 21, None, "Samba de roda (exemplo)", "Mercado Novo", "Centro", "Música", False, 20, True, True, "@rolesdocarmona"),
        (8, 9, None, "Feira de arte e artesanato (exemplo)", "Av. Afonso Pena", "Centro", "Feira", True, None, False, False, "Sou BH"),
        (10, 20, None, "Peça de teatro (exemplo)", "Teatro Francisco Nunes", "Centro", "Teatro", False, 30, False, False, "Circuito Liberdade"),
    ]
    out = []
    for d, h, run_days, title, venue, hood, cat, free, price, new, ig, src in rows:
        start, has_time = at(d, h)
        e = Event(title=title, start=start, has_time=has_time, source=src, url="https://example.com/",
                  end=start + timedelta(days=run_days) if run_days else None, venue=venue, neighborhood=hood,
                  category=cat, is_free=free, price_min=0.0 if free else price, price_max=0.0 if free else price,
                  description="Dados de exemplo para visualizar o layout.", is_instagram=ig, is_new=new)
        out.append(e)
    return dedupe(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Agenda BH weekly run")
    p.add_argument("--source", help="run only this source (by name); nothing is saved")
    p.add_argument("--dry-run", action="store_true", help="save nothing, send nothing")
    p.add_argument("--email", action="store_true", help="send the newsletter at the end")
    p.add_argument("--rebuild", action="store_true", help="rebuild the site from saved data only")
    p.add_argument("--sample", action="store_true", help="build the site from sample data")
    args = p.parse_args(argv)

    settings = load_settings()
    now = datetime.now(TZ)
    log_path = setup_logging(settings, now)
    win_start, win_end = run_window(now, settings.window_days)

    if args.sample:
        evs = sample_events(now)
        report = [SourceResult(s["name"], True, 1) for s in settings.sources]
        print("Sample dashboard:", build_site(evs, report, settings, now, win_start, win_end))
        subject, html = newsletter_html(evs, settings, now, win_start, win_end)
        (settings.site_dir / "newsletter-preview.html").write_text(html, encoding="utf-8")
        return 0

    persist = not (args.dry_run or args.source)
    store = Store(settings.db_path)

    if args.rebuild:
        events = store.window(win_start, win_end, now)
        report = [SourceResult(**r) for r in store.last_run()]
        build_site(events, report, settings, now, win_start, win_end)
        store.close()
        return 0

    sources = [s for s in settings.sources if s.get("enabled", True)]
    if args.source:
        sources = [s for s in sources if s["name"].lower() == args.source.lower()]
        if not sources:
            print(f"No enabled source called {args.source!r}. Names: " +
                  ", ".join(s["name"] for s in settings.sources))
            return 2

    runner = Runner(settings, now, store)
    report: list[SourceResult] = []
    found: list[Event] = []
    try:
        for src in sources:
            res, evs = runner.run_source(src)
            report.append(res)
            found.extend(evs)
    finally:
        runner.close()

    merged = dedupe(found)
    log.info("total: %d events found, %d after merging duplicates; AI calls: %d",
             len(found), len(merged), runner._ai.calls if runner._ai else 0)

    if args.source:
        for e in merged:
            print(f"- {e.start:%a %d/%m %H:%M} | {e.title} | {e.venue} | {e.price_text} | {e.url}")
        print(f"\n{len(merged)} event(s). Log: {log_path}")
        store.close()
        return 0 if report and report[0].ok else 1

    if persist:
        store.upsert(merged, now)
        store.save_run(now, [r.__dict__ for r in report])
        events = store.window(win_start, win_end, now)
    else:
        for e in merged:
            e.is_new = True
        events = merged

    build_site(events, report, settings, now, win_start, win_end)
    subject, html = newsletter_html(events, settings, now, win_start, win_end)
    (settings.site_dir / "newsletter-preview.html").write_text(html, encoding="utf-8")

    if args.email and not args.dry_run:
        try:
            send_newsletter(subject, html, settings)
        except Exception as e:
            log.error("newsletter not sent: %s", e)

    failed = [r.name for r in report if not r.ok]
    print("\nRun report")
    for r in report:
        print(f"  {'OK  ' if r.ok else 'FAIL'} {r.name}: {r.events} events {('— ' + r.note) if r.note else ''}")
    print(f"\nDashboard: {settings.site_dir / 'index.html'}\nLog: {log_path}")
    store.close()
    return 1 if report and len(failed) == len(report) else 0


if __name__ == "__main__":
    sys.exit(main())
