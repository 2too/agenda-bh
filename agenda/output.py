"""Builds the static dashboard (site/) and the weekly newsletter."""
from __future__ import annotations

import csv
import json
import logging
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Settings, TZ
from .models import CATEGORIES, Event, SourceResult
from .normalize import price_label

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).parent / "templates"
WD = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MON = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
MONTH_FULL = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
              "setembro", "outubro", "novembro", "dezembro"]

_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))


def _d(dt: datetime) -> str:
    return f"{dt.day:02d}/{dt.month:02d}"


def week_label(a: datetime, b: datetime) -> str:
    b = b - timedelta(days=1)
    if a.month == b.month:
        return f"{a.day} a {b.day} de {MONTH_FULL[a.month - 1]}"
    return f"{a.day} de {MONTH_FULL[a.month - 1]} a {b.day} de {MONTH_FULL[b.month - 1]}"


def is_run(e: Event, win_start: datetime) -> bool:
    """Long-running things (exhibitions, seasons) that began before this window."""
    return bool(e.end and (e.end.date() - e.start.date()).days >= 3 and e.start < win_start + timedelta(days=1))


def view(e: Event, win_start: datetime) -> dict:
    run = is_run(e, win_start)
    note = f"até {_d(e.end)}" if e.end and e.end.date() != e.start.date() else ""
    return {
        "title": e.title, "day": e.start.date().isoformat(),
        "dayShort": f"{WD[e.start.weekday()][:3]} {e.start.day}",
        "time": e.start.strftime("%H:%M") if e.has_time else "",
        "note": note, "run": run, "venue": e.venue, "hood": e.neighborhood, "cat": e.category,
        "price": price_label(e), "free": e.is_free, "isNew": e.is_new, "ig": e.is_instagram,
        "source": e.source, "url": e.url, "desc": e.description,
        "id": e.key or f"{e.start:%Y%m%d}-{abs(hash(e.title)) % 10**8}",
        "cost": 0 if e.is_free else e.price_min,
        "address": e.address,
    }


def report_rows(report: list[SourceResult]) -> list[dict]:
    rows = []
    for r in report:
        label = (f"{r.events} evento" + ("" if r.events == 1 else "s")) if r.ok else "falhou"
        rows.append({"name": r.name, "ok": r.ok, "label": label})
    return rows


def build_site(events: list[Event], report: list[SourceResult], settings: Settings,
               now: datetime, win_start: datetime, win_end: datetime) -> Path:
    out = settings.site_dir
    out.mkdir(parents=True, exist_ok=True)
    days = []
    d = win_start
    while d < win_end:
        days.append({"key": d.date().isoformat(), "label": WD[d.weekday()],
                     "date": f"{d.day} {MON[d.month - 1]}"})
        d += timedelta(days=1)
    views = [view(e, win_start) for e in events]
    data = {"events": views, "days": days, "categories": CATEGORIES}
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    local = now.astimezone(TZ)
    html = _env.get_template("dashboard.html.j2").render(
        week_label=week_label(win_start, win_end), window_days=settings.window_days,
        updated_label=f"{WD[local.weekday()].lower()[:3]}, {local.day} {MON[local.month - 1]} · {local:%H:%M}",
        total=len(events), sources_ok=sum(r.ok for r in report), report=report_rows(report),
        data_json=data_json,
    )
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "events.json").write_text(json.dumps([e.to_dict() for e in events], ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "events.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["data", "hora", "até", "evento", "local", "bairro", "categoria", "preço", "fonte", "link", "novo"])
        for e in events:
            w.writerow([e.start.date().isoformat(), e.start.strftime("%H:%M") if e.has_time else "",
                        e.end.date().isoformat() if e.end else "", e.title, e.venue, e.neighborhood,
                        e.category, price_label(e), e.source, e.url, "sim" if e.is_new else ""])
    (out / ".nojekyll").write_text("")
    return out / "index.html"


# ---------- newsletter ----------

def newsletter_html(events: list[Event], settings: Settings, now: datetime, win_start: datetime,
                    win_end: datetime) -> tuple[str, str]:
    local = now.astimezone(TZ)
    upcoming = [e for e in events if not is_run(e, win_start)]
    week_end = win_start + timedelta(days=7)
    this_week = [e for e in upcoming if e.start < week_end]

    def line(e: Event) -> dict:
        bits = [e.start.strftime("%H:%M") if e.has_time else "", e.venue, e.neighborhood]
        return {"title": e.title, "url": e.url, "wd": WD[e.start.weekday()][:3].upper(), "dd": e.start.day,
                "line": " · ".join(b for b in bits if b), "price": price_label(e), "free": e.is_free,
                "short": f"{WD[e.start.weekday()][:3]}" + (f" · {e.start:%H:%M}" if e.has_time else f" {e.start.day}")}

    new = [line(e) for e in this_week if e.is_new][:5]
    weekend = [e for e in this_week if e.start.weekday() >= 4]
    free = [line(e) for e in weekend if e.is_free][:6]
    shown = {x["url"] + x["title"] for x in new + free}
    more = [line(e) for e in this_week if e.url + e.title not in shown][:8]
    n_new = sum(e.is_new for e in this_week)
    intro = (f"{len(this_week)} eventos nos próximos 7 dias"
             + (f", {n_new} novos." if n_new else ".") + " Aqui vão os destaques.")
    subject = f"Agenda BH · {week_label(win_start, win_start + timedelta(days=7))}"
    html = _env.get_template("newsletter.html.j2").render(
        week_label=week_label(win_start, week_end), preheader=intro,
        date_label=f"{WD[local.weekday()]}, {local.day} de {MONTH_FULL[local.month - 1]}",
        intro=intro, new=new, free=free, more=more, site_url=settings.site_url,
    )
    return subject, html


def send_newsletter(subject: str, html: str, settings: Settings) -> None:
    if not (settings.gmail_user and settings.gmail_app_password and settings.newsletter_to):
        raise RuntimeError("Gmail not set up (GMAIL_USER, GMAIL_APP_PASSWORD, NEWSLETTER_TO)")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Agenda BH <{settings.gmail_user}>"
    msg["To"] = settings.gmail_user
    plain = "Sua agenda semanal está no HTML deste e-mail." + (f"\n\n{settings.site_url}" if settings.site_url else "")
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(settings.gmail_user, settings.gmail_app_password)
        # recipients go in BCC so they don't see each other's addresses
        s.sendmail(settings.gmail_user, settings.newsletter_to, msg.as_string())
    log.info("newsletter sent to %d recipient(s)", len(settings.newsletter_to))
