"""Loads sources.yaml and environment variables (.env supported)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

TZ = ZoneInfo("America/Sao_Paulo")
ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader: KEY=value lines, existing env wins."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    window_days: int = 14
    instagram_max_posts: int = 12
    instagram_max_age_days: int = 21
    request_delay_seconds: float = 3
    sources: list[dict] = field(default_factory=list)

    db_path: Path = ROOT / "data" / "events.db"
    site_dir: Path = ROOT / "site"
    log_dir: Path = ROOT / "logs"
    site_url: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"

    gmail_user: str = ""
    gmail_app_password: str = ""
    newsletter_to: list[str] = field(default_factory=list)

    ig_username: str = ""
    ig_session_file: str = ""
    ig_graph_token: str = ""
    ig_graph_user_id: str = ""
    ig_graph_version: str = "v21.0"


def load_settings(sources_file: Path = ROOT / "sources.yaml") -> Settings:
    load_dotenv()
    data = yaml.safe_load(sources_file.read_text(encoding="utf-8")) or {}
    s = Settings(**(data.get("settings") or {}))
    s.sources = data.get("sources") or []
    env = os.environ.get
    s.site_url = env("SITE_URL", "")
    s.gemini_api_key = env("GEMINI_API_KEY", "")
    s.gemini_model = env("GEMINI_MODEL", s.gemini_model)
    s.gmail_user = env("GMAIL_USER", "")
    s.gmail_app_password = env("GMAIL_APP_PASSWORD", "")
    s.newsletter_to = [x.strip() for x in env("NEWSLETTER_TO", "").split(",") if x.strip()]
    s.ig_username = env("IG_USERNAME", "")
    s.ig_session_file = env("IG_SESSION_FILE", "")
    s.ig_graph_token = env("IG_GRAPH_TOKEN", "")
    s.ig_graph_user_id = env("IG_GRAPH_USER_ID", "")
    s.ig_graph_version = env("IG_GRAPH_VERSION", s.ig_graph_version)
    return s


def run_window(now: datetime, days: int) -> tuple[datetime, datetime]:
    start = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=days)


def fill_url(url: str, now: datetime, days: int) -> str:
    """Replace {date_from}/{date_to} with URL-encoded ISO timestamps."""
    if "{date_" not in url:
        return url
    from urllib.parse import quote
    a, b = run_window(now, days)
    b = b.replace(hour=23, minute=59, second=59)
    return url.format(date_from=quote(a.isoformat()), date_to=quote(b.isoformat()))
