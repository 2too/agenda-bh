"""Fixed programmes typed by hand (eventos_fixos/*.yaml): no scraping, no AI."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..config import ROOT
from ..models import RawItem


def fetch_manual_source(src: dict) -> list[RawItem]:
    path = ROOT / src["file"]
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raws = []
    for ev in data.get("eventos") or []:
        if not ev.get("titulo") or not ev.get("data"):
            continue
        day = str(ev["data"])
        hora = str(ev.get("hora") or "").strip()
        raws.append({
            "title": str(ev["titulo"]),
            "start": f"{day}T{hora}" if hora else day,
            "end": str(ev.get("ate") or ""),
            "venue": data.get("local", ""),
            "address": data.get("endereco", ""),
            "neighborhood": data.get("bairro", ""),
            "price_text": str(ev.get("preco") or data.get("preco") or ""),
            "category": ev.get("categoria") or data.get("categoria", "Outro"),
            "description": str(ev.get("descricao") or ""),
            "url": ev.get("link") or data.get("link", ""),
        })
    return [RawItem(source=src["name"], url=data.get("link", ""), raw_events=raws)]
