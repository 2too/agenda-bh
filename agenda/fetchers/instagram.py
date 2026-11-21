"""Instagram fetcher.

Two free routes, tried in this order:
1. Official Graph API "Business Discovery" (needs IG_GRAPH_TOKEN and
   IG_GRAPH_USER_ID from YOUR Instagram Business/Creator account). Works only
   when the target profile is also a Business/Creator account.
2. instaloader, logged in with a secondary account through a saved session
   file (IG_USERNAME + IG_SESSION_FILE). Anonymous access is blocked quickly,
   so it is not attempted.

Both return the caption, the post date and the first image (the flyer).
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from ..config import Settings
from ..models import RawItem

log = logging.getLogger(__name__)


def _via_graph(handle: str, settings: Settings) -> list[RawItem]:
    fields = (f"business_discovery.username({handle})"
              f"{{media.limit({settings.instagram_max_posts})"
              "{caption,media_url,thumbnail_url,timestamp,permalink,media_type,"
              "children{media_url}}}}")
    url = (f"https://graph.facebook.com/{settings.ig_graph_version}/{settings.ig_graph_user_id}?"
           + urlencode({"fields": fields, "access_token": settings.ig_graph_token}))
    with urllib.request.urlopen(url, timeout=40) as r:
        data = json.loads(r.read().decode("utf-8"))
    media = (data.get("business_discovery") or {}).get("media", {}).get("data", [])
    items = []
    for m in media:
        img = m.get("media_url") if m.get("media_type") != "VIDEO" else m.get("thumbnail_url")
        children = (m.get("children") or {}).get("data") or []
        if m.get("media_type") == "CAROUSEL_ALBUM" and children:
            img = children[0].get("media_url") or img
        items.append(RawItem(
            source=f"@{handle}", url=m.get("permalink", f"https://www.instagram.com/{handle}/"),
            text=m.get("caption") or "", image_urls=[img] if img else [],
            published_at=datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")),
        ))
    return items


_loader = None


def _get_loader(settings: Settings):
    global _loader
    if _loader is None:
        import instaloader
        L = instaloader.Instaloader(download_pictures=False, download_videos=False,
                                    download_video_thumbnails=False, save_metadata=False,
                                    compress_json=False, quiet=True, max_connection_attempts=1)
        L.load_session_from_file(settings.ig_username, settings.ig_session_file or None)
        _loader = L
    return _loader


def _via_instaloader(handle: str, settings: Settings) -> list[RawItem]:
    import instaloader
    L = _get_loader(settings)
    profile = instaloader.Profile.from_username(L.context, handle)
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.instagram_max_age_days)
    items = []
    for i, post in enumerate(profile.get_posts()):
        if i >= settings.instagram_max_posts:
            break
        posted = post.date_utc.replace(tzinfo=timezone.utc)
        if posted < cutoff:
            if not post.is_pinned:
                break      # posts are newest-first; pinned ones can be old
            continue
        img = post.url
        if post.typename == "GraphSidecar":
            try:
                first = next(iter(post.get_sidecar_nodes()))
                img = first.display_url
            except StopIteration:
                pass
        items.append(RawItem(
            source=f"@{handle}", url=f"https://www.instagram.com/p/{post.shortcode}/",
            text=post.caption or "", image_urls=[img] if img else [], published_at=posted,
        ))
        time.sleep(random.uniform(2, 5))   # look less like a bot
    return items


def fetch_instagram_source(src: dict, settings: Settings) -> list[RawItem]:
    handle = src["handle"].lstrip("@")
    errors = []
    if settings.ig_graph_token and settings.ig_graph_user_id:
        try:
            return _via_graph(handle, settings)
        except Exception as e:
            errors.append(f"Graph API: {e}")
            log.warning("@%s: Graph API failed (%s), trying instaloader", handle, e)
    if settings.ig_username:
        try:
            return _via_instaloader(handle, settings)
        except Exception as e:
            errors.append(f"instaloader: {e}")
    if not errors:
        raise RuntimeError("no Instagram login set up (see README, step 4)")
    raise RuntimeError("; ".join(errors))
