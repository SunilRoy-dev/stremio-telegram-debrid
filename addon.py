import logging
import asyncio
import os
import re

# Fix Pyrogram event loop crash on Python 3.12/3.14
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import urllib.parse
import markupsafe
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

from config import Config
import config_store
import cache
import metadata as meta_enricher
from ratelimit import check_rate_limit
from tg_client import tg_client_manager
from utils import (
    format_size,
    matches_episode,
    get_metadata_from_cinemeta,
    matches_subtitle,
    get_search_query_from_filename,
    parse_split_info,
    is_video_file,
    matches_title,
    parse_season_episode,
    normalize_title,
    _clean_title_prefix,
)
from zip_helper import (
    list_zip_files,
    TelegramSeekableReader,
    get_zip_entry_data_offset,
    zip_compressed_generator
)
from search_utils import VideoMatcher, parse_video_resolution, get_resolution_score
from wizard import router as wizard_router


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] (%(name)s) - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("stremio_addon")

SUBTITLE_EXTENSIONS = ('.srt', '.vtt', '.ass', '.ssa', '.sub')
PAGE_SIZE = 100


def is_subtitle_file(filename: str) -> bool:
    if not filename:
        return False
    return filename.lower().endswith(SUBTITLE_EXTENSIONS)


_background_tasks = set()
_finalizer_task = None


def start_watch_finalizer():
    global _finalizer_task
    if _finalizer_task is None or _finalizer_task.done():
        _finalizer_task = asyncio.create_task(_watch_session_finalizer())
        _background_tasks.add(_finalizer_task)
        _finalizer_task.add_done_callback(_background_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        print("\n" + "=" * 60)
        print("   TELEGRAM ADDON BY SUNILROY-DEV")
        print("   GitHub: https://github.com/SunilRoy-dev/stremio-telegram-debrid")
        print("   Personal private media streaming proxy.")
        print("   Always follow your local laws.")
        print("=" * 60 + "\n")

        # Wizard-saved config (data/config.json) overrides env vars
        Config.apply_file_overrides()
        cache.init(os.path.join(config_store.DATA_DIR, "addon_cache.db"))
        try:
            Config.validate()
            await tg_client_manager.start()
            start_watch_finalizer()
        except ValueError as e:
            # Not configured yet: keep the web server alive so the owner can
            # finish setup through the /configure wizard from the browser.
            logger.warning("=" * 60)
            logger.warning(f"CONFIG INCOMPLETE: {e}")
            logger.warning("Open /configure in your browser to set up the addon without env vars.")
            logger.warning("=" * 60)
        yield
    finally:
        global _finalizer_task
        if _finalizer_task and not _finalizer_task.done():
            _finalizer_task.cancel()
            _finalizer_task = None
        for task in list(_background_tasks):
            if not task.done():
                task.cancel()
        await meta_enricher.close_client()
        await tg_client_manager.stop()
        cache.close()


async def _watch_session_finalizer():
    """
    Background task: when no streaming activity happened for a file for 10+ minutes,
    mark the watch session as stopped/finished and (optionally) post the stop event
    with progress + play count to the configured Telegram log channel.
    """
    while True:
        await asyncio.sleep(60)
        try:
            finalized = cache.finalize_stale_sessions()
        except Exception as e:
            logger.debug(f"Watch finalizer error: {e}")
            finalized = []
        if not finalized:
            continue
        for item in finalized:
            try:
                if not Config.LOG_CHANNEL_ID or not Config.WATCH_LOG_EVENTS:
                    break
                when = cache.friendly_ts(item.get("last_seen") or 0)
                await tg_client_manager.send_watch_log(
                    "finish" if item.get("finished") else "stop",
                    item.get("display_name") or "Unknown file",
                    None,
                    None,
                    item.get("play_count", 1),
                    progress=f"{item.get('progress', 0)}%",
                    extra=f"Session ended around {when}",
                )
            except Exception as e:
                logger.debug(f"Stop log failed: {e}")


app = FastAPI(lifespan=lifespan)
app.include_router(wizard_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def addon_middleware(request: Request, call_next):
    # Optional per-IP rate limiting (owner toggle - enable when sharing publicly)
    if Config.RATE_LIMIT_ENABLED:
        path = request.url.path
        bucket = None
        if ("/stream/file/" in path or "/stream/split/" in path
                or "/stream/zip/" in path or "/stream/subtitle/" in path):
            bucket = "stream"
        elif path.endswith(".json") and ("/catalog/" in path or "/meta/" in path or "/subtitles/" in path):
            bucket = "api"
        if bucket:
            allowed, retry_after, headers = await check_rate_limit(request, bucket)
            if not allowed:
                return JSONResponse(
                    {"detail": "Rate limit exceeded. Please slow down."},
                    status_code=429,
                    headers=headers,
                )

    response = await call_next(request)
    if "/stream/" in request.url.path:
        response.headers["X-Accel-Buffering"] = "no"
    return response


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def group_tg_messages(messages: list) -> list:
    grouped = {}
    standalone = []

    for msg in messages:
        media = msg.video or msg.document or msg.audio
        if not media:
            continue

        fn = getattr(media, "file_name", "") or msg.caption or f"Telegram File {msg.id}"
        if is_subtitle_file(fn):
            continue

        base, part = parse_split_info(fn)

        if base and part is not None:
            key = base.lower()
            if key not in grouped:
                grouped[key] = {
                    "base_name": base,
                    "parts": {}
                }
            grouped[key]["parts"][part] = msg
        else:
            standalone.append(msg)

    results = []
    for key, data in grouped.items():
        parts = data["parts"]
        base_name = data["base_name"]

        if len(parts) == 1:
            results.append(list(parts.values())[0])
        else:
            sorted_parts = [msg for part, msg in sorted(parts.items())]
            results.append((base_name, sorted_parts))

    for msg in standalone:
        results.append(msg)

    return results


def parse_catalog_extra(extra: str) -> dict:
    """Parse Stremio extra params: search=..., genre=..., skip=..."""
    out = {}
    if not extra:
        return out
    decoded = urllib.parse.unquote(extra)
    if "?" in decoded:
        decoded = decoded.split("?", 1)[0]
    try:
        params = urllib.parse.parse_qs(decoded)
    except Exception:
        return out
    if "search" in params:
        out["search"] = params["search"][0]
    if "genre" in params:
        out["genre"] = params["genre"][0]
    if "skip" in params:
        try:
            out["skip"] = max(0, int(params["skip"][0]))
        except ValueError:
            out["skip"] = 0
    return out


_GENRE_ALIASES = {
    "sci-fi": "science fiction",
    "sci-fi & fantasy": "science fiction",
    "science fiction & fantasy": "science fiction",
    "sci-fi and fantasy": "science fiction",
    "romance": "romance",
    "kids": "family",
    "family": "family",
    "superhero": "action",
}


def genre_matches(genres: list, wanted: str) -> bool:
    """Case-insensitive genre comparison with common aliases."""
    if not wanted:
        return True
    w = _GENRE_ALIASES.get(wanted.strip().lower(), wanted.strip().lower())
    for g in genres or []:
        gn = _GENRE_ALIASES.get(str(g).strip().lower(), str(g).strip().lower())
        if gn == w or w in gn or gn in w:
            return True
    return False


def build_show_groups(messages: list) -> list:
    """
    Group episode files into shows for the series catalog.
    Returns a list of {name, chat_id, meta_id, episodes_list} where each episode
    has its own resolvable stream id (split parts stitched automatically).
    """
    shows = {}
    for msg in messages:
        media = msg.video or msg.document or msg.audio
        if not media or msg.audio:
            continue
        fn = getattr(media, "file_name", "") or msg.caption or ""
        if not fn or is_subtitle_file(fn):
            continue
        season, episode = parse_season_episode(fn)
        if season is None or episode is None:
            continue
        raw_prefix = _clean_title_prefix(fn) or fn
        title = raw_prefix.replace(".", " ").replace("_", " ")
        title = re.sub(r"\s+", " ", title).strip(" -–—.,([{)]}")
        norm = normalize_title(title)
        if not norm:
            continue
        key = (str(msg.chat.id), norm)
        show = shows.setdefault(key, {"name": title, "chat_id": msg.chat.id, "episodes": {}})
        base, part = parse_split_info(fn)
        ep_key = (season, episode, (base or fn).lower())
        ep = show["episodes"].setdefault(ep_key, {
            "season": season, "episode": episode, "parts": [], "name": base or fn, "size": 0
        })
        ep["parts"].append(msg)
        try:
            ep["size"] += media.file_size or 0
        except Exception:
            pass

    out = []
    for show in shows.values():
        # Deduplicate identical (season, episode) from different releases: keep largest
        best_by_se = {}
        for ep in show["episodes"].values():
            se = (ep["season"], ep["episode"])
            if se not in best_by_se or ep["size"] > best_by_se[se]["size"]:
                best_by_se[se] = ep

        eps = []
        for se, ep in sorted(best_by_se.items()):
            parts_sorted = sorted(ep["parts"], key=lambda m: m.id)
            ids = ",".join(str(m.id) for m in parts_sorted)
            if len(parts_sorted) > 1:
                vid = f"tgfile_split_{show['chat_id']}_{ids}"
            else:
                vid = f"tgfile_{show['chat_id']}_{ids}"
            eps.append({
                "season": ep["season"], "episode": ep["episode"],
                "id": vid, "filename": ep["name"], "msg_ids": ids,
            })

        if not eps:
            continue
        all_ids = ",".join(i for e in eps for i in e["msg_ids"].split(","))
        show["meta_id"] = f"tgshow_{show['chat_id']}_{all_ids}"
        show["episodes_list"] = eps
        show["msg_ids_ints"] = [int(i) for i in all_ids.split(",")]
        out.append(show)

    out.sort(key=lambda s: s["name"].lower())
    return out


def build_continue_metas(kind: str, logo_url: str) -> list:
    """Build 'Continue Watching' catalog metas from the persistent watch history."""
    metas = []
    try:
        items = cache.get_continue_watching(kind, 20)
    except Exception:
        items = []
    for it in items:
        if kind == "series" and it.get("show_meta_id"):
            meta_id = it["show_meta_id"]
            name = it.get("show_name") or it.get("display_name") or "Unknown show"
            poster = it.get("show_poster") or it.get("poster") or logo_url
            desc = (f"▶️ Last watched: {it.get('display_name', '')} · "
                    f"{it.get('progress_pct', 0)}% watched · played {it.get('play_count', 1)}x")
        else:
            meta_id = it.get("meta_id")
            name = it.get("display_name") or "Unknown file"
            poster = it.get("poster") or logo_url
            desc = f"▶️ {it.get('progress_pct', 0)}% watched · played {it.get('play_count', 1)}x"
        if not meta_id:
            continue
        metas.append({
            "id": meta_id,
            "type": kind,
            "name": name,
            "poster": poster,
            "description": desc,
        })
    return metas


def verify_api_key(request: Request):
    if Config.API_KEY:
        api_key = request.query_params.get("api_key", "") or request.path_params.get("api_key", "")
        if api_key != Config.API_KEY:
            raise HTTPException(status_code=403, detail="Unauthorized: Invalid API Key")


def get_manifest(api_key: str = ""):
    from metadata import GENRE_OPTIONS
    return {
        "id": "community.telegram.stremio.addon",
        "version": "1.0.0",
        "name": "Telegram Addon by SunilRoy-dev",
        "description": "Personal Telegram streaming proxy with catalogs, TMDB posters, genre rows, Continue Watching and split-file stitching. For educational & personal testing only.",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg",
        "background": f"{Config.ADDON_URL}/stremio_telegram_banner.png",
        "resources": ["catalog", "meta", "stream", "subtitles"],
        "types": ["movie", "series"],
        "idPrefixes": ["tgfile_", "tgshow_", "tt"],
        "catalogs": [
            {
                "id": "tg-movies",
                "type": "movie",
                "name": "Telegram Movies",
                "genres": GENRE_OPTIONS,
                "extra": [{"name": "genre", "options": GENRE_OPTIONS}, {"name": "search"}, {"name": "skip"}],
            },
            {
                "id": "tg-series",
                "type": "series",
                "name": "Telegram Shows",
                "genres": GENRE_OPTIONS,
                "extra": [{"name": "genre", "options": GENRE_OPTIONS}, {"name": "search"}, {"name": "skip"}],
            },
            {
                "id": "tg-continue-movie",
                "type": "movie",
                "name": "Continue Watching",
                "extra": [{"name": "skip"}],
            },
            {
                "id": "tg-continue-series",
                "type": "series",
                "name": "Continue Watching",
                "extra": [{"name": "skip"}],
            },
        ],
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": False
        }
    }

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def landing(request: Request):
    api_key = request.query_params.get("api_key", "")
    if api_key:
        manifest_url = f"{Config.ADDON_URL}/{urllib.parse.quote(api_key)}/manifest.json"
    else:
        manifest_url = f"{Config.ADDON_URL}/manifest.json"

    escaped_manifest_url = markupsafe.escape(manifest_url)
    escaped_stremio_url = markupsafe.escape(manifest_url.replace('http://', '').replace('https://', ''))

    web_stremio_url = f"https://web.stremio.com/#/addons?addon={urllib.parse.quote(manifest_url)}"
    escaped_web_stremio_url = markupsafe.escape(web_stremio_url)

    api_key_section = ""
    if Config.API_KEY:
        escaped_api_key = markupsafe.escape(api_key)
        api_key_section = f"""
                <div class="url-section" style="margin-bottom: 16px;">
                    <div class="section-title">Enter API Key</div>
                    <div class="input-group">
                        <input class="url-box" id="apiKeyInput" type="text" placeholder="Enter your API Key..." value="{escaped_api_key}" oninput="updateManifestUrl()">
                    </div>
                </div>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Telegram Addon by SunilRoy-dev</title>
            <meta name="description" content="Stream private Telegram files directly inside Stremio. Secure, lightweight, and ranges-supported proxy.">
            <link rel="preconnect" href="https://fonts.googleapis.com">
            <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
            <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
            <style>
                :root {{
                    --bg-dark: #09090b;
                    --bg-card: #18181b;
                    --border-muted: #27272a;
                    --text-primary: #f4f4f5;
                    --text-secondary: #a1a1aa;
                    --text-muted: #71717a;
                    --color-primary: #2563eb;
                    --color-primary-hover: #1d4ed8;
                    --color-accent: #60a5fa;
                    --font-title: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    --font-body: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                }}
                * {{
                    box-sizing: border-box;
                    margin: 0;
                    padding: 0;
                }}
                body {{
                    font-family: var(--font-body);
                    background-color: var(--bg-dark);
                    color: var(--text-primary);
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    min-height: 100vh;
                    padding: 40px 20px;
                    margin: 0;
                    overflow-x: hidden;
                }}
                .app-card {{
                    background-color: var(--bg-card);
                    border: 1px solid var(--border-muted);
                    border-radius: 12px;
                    padding: 40px;
                    width: 100%;
                    max-width: 680px;
                    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
                    position: relative;
                }}
                .nav-header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 32px;
                    gap: 8px;
                    flex-wrap: wrap;
                }}
                .brand {{
                    display: flex;
                    align-items: center;
                    gap: 10px;
                    font-family: var(--font-title);
                    font-weight: 700;
                    font-size: 1.1rem;
                    letter-spacing: -0.02em;
                    color: var(--text-primary);
                }}
                .brand-logo {{
                    width: 28px;
                    height: 28px;
                }}
                .header-actions {{
                    display: flex;
                    gap: 8px;
                }}
                .star-badge {{
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    background: linear-gradient(135deg, #fbbf24 0%, #d97706 100%);
                    color: #09090b;
                    padding: 8px 14px;
                    border-radius: 6px;
                    font-size: 0.78rem;
                    font-weight: 700;
                    text-decoration: none;
                    box-shadow: 0 0 15px rgba(251, 191, 36, 0.3);
                    transition: all 0.3s ease;
                }}
                .star-badge:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 0 20px rgba(251, 191, 36, 0.6);
                    color: #000000;
                }}
                .config-badge {{
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
                    color: #ffffff;
                    padding: 8px 14px;
                    border-radius: 6px;
                    font-size: 0.78rem;
                    font-weight: 700;
                    text-decoration: none;
                    box-shadow: 0 0 15px rgba(37, 99, 235, 0.3);
                    transition: all 0.3s ease;
                }}
                .config-badge:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 0 20px rgba(37, 99, 235, 0.6);
                    color: #ffffff;
                }}
                .hero {{
                    text-align: center;
                    margin-bottom: 32px;
                }}
                .hero h1 {{
                    font-family: var(--font-title);
                    font-size: 2rem;
                    font-weight: 700;
                    line-height: 1.25;
                    letter-spacing: -0.02em;
                    margin: 8px 0 16px 0;
                    color: #ffffff;
                }}
                .hero p {{
                    font-size: 0.95rem;
                    color: var(--text-secondary);
                    line-height: 1.5;
                    max-width: 520px;
                    margin: 0 auto;
                }}
                .url-section {{
                    background: #09090b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 20px;
                    margin-bottom: 24px;
                }}
                .section-title {{
                    font-family: var(--font-title);
                    font-size: 0.8rem;
                    font-weight: 700;
                    text-transform: uppercase;
                    letter-spacing: 0.05em;
                    color: var(--text-secondary);
                    margin-bottom: 12px;
                }}
                .input-group {{
                    display: flex;
                    gap: 10px;
                }}
                .url-box {{
                    flex: 1;
                    background-color: #18181b;
                    border: 1px solid #27272a;
                    color: var(--text-primary);
                    padding: 12px 16px;
                    border-radius: 6px;
                    font-size: 0.85rem;
                    font-family: monospace;
                    outline: none;
                    transition: border-color 0.2s;
                }}
                .url-box:focus {{
                    border-color: var(--color-primary);
                }}
                .btn-copy {{
                    background: #27272a;
                    border: 1px solid #3f3f46;
                    color: var(--text-primary);
                    padding: 0 16px;
                    border-radius: 6px;
                    font-size: 0.85rem;
                    font-weight: 500;
                    cursor: pointer;
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    transition: all 0.2s;
                }}
                .btn-copy:hover {{
                    background: #3f3f46;
                    border-color: #52525b;
                }}
                .button-group {{
                    display: grid;
                    grid-template-columns: 1fr;
                    gap: 12px;
                    margin-bottom: 32px;
                }}
                @media (min-width: 520px) {{
                    .button-group {{
                        grid-template-columns: 1fr 1fr;
                    }}
                }}
                .btn {{
                    padding: 12px 20px;
                    font-family: var(--font-body);
                    font-size: 0.9rem;
                    font-weight: 500;
                    text-decoration: none;
                    border-radius: 6px;
                    text-align: center;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    gap: 8px;
                    transition: all 0.2s;
                }}
                .btn-primary {{
                    background-color: var(--color-primary);
                    color: #ffffff;
                }}
                .btn-primary:hover {{
                    background-color: var(--color-primary-hover);
                }}
                .btn-secondary {{
                    background: #27272a;
                    border: 1px solid #3f3f46;
                    color: var(--text-primary);
                }}
                .btn-secondary:hover {{
                    background: #3f3f46;
                    border-color: #52525b;
                }}
                .troubleshoot-details {{
                    background: #09090b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 16px;
                    margin-bottom: 24px;
                }}
                .troubleshoot-summary {{
                    font-family: var(--font-title);
                    font-size: 0.9rem;
                    font-weight: 600;
                    color: var(--text-primary);
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    user-select: none;
                    outline: none;
                }}
                .troubleshoot-content {{
                    margin-top: 14px;
                    font-size: 0.85rem;
                    color: var(--text-secondary);
                    line-height: 1.5;
                    border-top: 1px solid #27272a;
                    padding-top: 14px;
                }}
                .troubleshoot-content ol {{
                    margin-left: 20px;
                    margin-top: 8px;
                }}
                .troubleshoot-content li {{
                    margin-bottom: 6px;
                }}
                .features-grid {{
                    display: grid;
                    grid-template-columns: 1fr;
                    gap: 16px;
                    margin-bottom: 32px;
                }}
                @media (min-width: 600px) {{
                    .features-grid {{
                        grid-template-columns: 1fr 1fr;
                    }}
                }}
                .feature-card {{
                    background: #18181b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 20px;
                }}
                .feature-icon {{
                    width: 36px;
                    height: 36px;
                    background: #27272a;
                    border-radius: 6px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    color: var(--color-accent);
                    margin-bottom: 12px;
                }}
                .feature-title {{
                    font-family: var(--font-title);
                    font-size: 0.95rem;
                    font-weight: 600;
                    margin-bottom: 6px;
                    color: var(--text-primary);
                }}
                .feature-desc {{
                    font-size: 0.8rem;
                    color: var(--text-secondary);
                    line-height: 1.45;
                }}
                .license-card {{
                    background: #18181b;
                    border: 1px solid var(--border-muted);
                    border-radius: 8px;
                    padding: 20px;
                    margin-bottom: 32px;
                }}
                .license-title {{
                    font-family: var(--font-title);
                    font-size: 0.9rem;
                    font-weight: 600;
                    color: var(--text-primary);
                    margin-bottom: 6px;
                }}
                .license-text {{
                    font-size: 0.8rem;
                    color: var(--text-secondary);
                    line-height: 1.45;
                }}
                .footer {{
                    text-align: center;
                    font-size: 0.78rem;
                    color: var(--text-muted);
                    border-top: 1px solid var(--border-muted);
                    padding-top: 24px;
                    line-height: 1.6;
                }}
                .footer a {{
                    color: var(--text-secondary);
                    text-decoration: none;
                    font-weight: 500;
                    transition: color 0.2s;
                }}
                .footer a:hover {{
                    color: var(--text-primary);
                    text-decoration: underline;
                }}
                .footer em {{
                    display: block;
                    margin-top: 6px;
                    color: var(--text-muted);
                    font-style: normal;
                }}
            </style>
        </head>
        <body>
            <div class="app-card">
                <div class="nav-header">
                    <div class="brand">
                        <svg class="brand-logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                            <path d="M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22Z" fill="url(#logoGrad)"/>
                            <path fill-rule="evenodd" clip-rule="evenodd" d="M16.974 8.23272C17.1568 7.2796 16.2004 6.5492 15.3533 6.94008L6.46743 11.0398C5.72727 11.3813 5.76103 12.4431 6.51651 12.7336L8.85507 13.6331C9.52554 13.891 10.2831 13.7828 10.8553 13.3486L14.4754 10.6011C14.6195 10.4917 14.7766 10.7042 14.6534 10.8406L11.597 14.2238C11.107 14.7663 11.2335 15.6322 11.854 16.015L15.3854 18.1936C16.1471 18.6635 17.1264 18.0673 17.0792 17.1685L16.974 8.23272Z" fill="white"/>
                            <defs>
                                <linearGradient id="logoGrad" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
                                    <stop stop-color="#3b82f6"/>
                                    <stop offset="1" stop-color="#1d4ed8"/>
                                </linearGradient>
                            </defs>
                        </svg>
                        Stremio Telegram Addon
                    </div>
                    <div class="header-actions">
                        <a href="/configure" class="config-badge">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
                            Configure
                        </a>
                        <a href="https://github.com/SunilRoy-dev/stremio-telegram-debrid" target="_blank" class="star-badge">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none" style="margin-right: 4px;"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>
                            Star on GitHub
                        </a>
                    </div>
                </div>

                <div class="hero">
                    <h1>Stremio Telegram Addon</h1>
                    <p>A self-hosted Stremio addon proxy to stream videos, audios, and segmented archive parts directly from Telegram — now with catalogs, TMDB posters, genre rows and Continue Watching.</p>
                </div>

                {api_key_section}
                <div class="url-section">
                    <div class="section-title">Addon Manifest URL</div>
                    <div class="input-group">
                        <input class="url-box" id="manifestUrl" type="text" readonly value="{escaped_manifest_url}">
                        <button class="btn-copy" id="btnCopy" onclick="copyManifestUrl()">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="feather feather-copy"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                            <span id="btnCopyText">Copy</span>
                        </button>
                    </div>
                </div>

                <div class="button-group">
                    <a class="btn btn-primary" id="installApp" href="stremio://{escaped_stremio_url}">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                        Install on Stremio App
                    </a>
                    <a class="btn btn-secondary" id="installWeb" href="{escaped_web_stremio_url}" target="_blank">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line></svg>
                        Install on Stremio Web
                    </a>
                </div>

                <details class="troubleshoot-details">
                    <summary class="troubleshoot-summary">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="margin-right: 8px; color: #fbbf24;"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
                        Local Deployment Troubleshooting
                    </summary>
                    <div class="troubleshoot-content">
                        This error <strong>only occurs in local HTTP deployments</strong>. If you deploy this project to a secure public HTTPS server (such as Render, Koyeb or a VPS), this installation button will work <strong>flawlessly</strong>.
                        <br><br>
                        For local deployments, Stremio's desktop protocol handler (<strong>stremio://</strong>) strips local ports and forces HTTPS, resulting in connection failure.
                        <br><br>
                        <strong>How to install locally:</strong>
                        <ol>
                            <li>Click the <strong>Copy</strong> button on the manifest URL field above.</li>
                            <li>Open the <strong>Stremio Desktop App</strong>.</li>
                            <li>Navigate to <strong>Add-ons</strong> (puzzle icon in the sidebar).</li>
                            <li>Paste the copied URL directly into the <strong>Add-on Repository URL</strong> input box at the bottom and click <strong>Install</strong>.</li>
                            <li>Alternatively, use the <strong>Install on Stremio Web</strong> button above.</li>
                        </ol>
                    </div>
                </details>

                <div class="features-grid">
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg>
                        </div>
                        <div class="feature-title">Segmented File Stitching</div>
                        <div class="feature-desc">Groups and stitches split file parts (.001, .part1, etc.) into a virtual continuous stream on the fly.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="M3 9h18M9 21V9"></path></svg>
                        </div>
                        <div class="feature-title">Catalogs, Genres &amp; Posters</div>
                        <div class="feature-desc">Home screen rows with genre filters, TMDB/Cinemeta posters, localized titles and grouped series pages.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                        </div>
                        <div class="feature-title">Continue Watching</div>
                        <div class="feature-desc">Tracks start/seek/stop events and builds a Continue Watching feed, with optional play logs in your Telegram channel.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
                        </div>
                        <div class="feature-title">Subtitle Mapping</div>
                        <div class="feature-desc">Scans the channel dynamically for matching subtitle files (.srt, .vtt, .ass) and injects them.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                        </div>
                        <div class="feature-title">Access Control &amp; Rate Limiting</div>
                        <div class="feature-desc">API key protection plus an optional per-IP rate limiter and a password-protected /configure wizard.</div>
                    </div>
                    <div class="feature-card">
                        <div class="feature-icon">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><ellipse cx="12" cy="5" rx="9" ry="3"></ellipse><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path></svg>
                        </div>
                        <div class="feature-title">Persistent SQLite Cache</div>
                        <div class="feature-desc">Search results, metadata and watch history survive restarts — fewer Telegram API calls, less ban risk.</div>
                    </div>
                </div>

                <div class="license-card">
                    <div class="license-title">License: MIT Non-Commercial License (MIT-NC)</div>
                    <div class="license-text">
                        This software is published under a custom <strong>MIT Non-Commercial License (MIT-NC)</strong>. Sublicensing, commercial distribution, renting, or monetization of this code or its derivatives is strictly prohibited. Attribution must be preserved in all copies.
                    </div>
                </div>

                <div class="footer">
                    Developed by <a href="https://github.com/SunilRoy-dev" target="_blank">SunilRoy-dev</a> | Licensed under MIT-NC
                    <em>For educational and personal testing only. Do not use for unauthorized hosting or distribution of copyrighted media.</em>
                </div>
            </div>

            <script>
                const baseManifestUrl = "{Config.ADDON_URL}/manifest.json";
                const baseStremioUrl = baseManifestUrl.replace('http://', '').replace('https://', '');

                function updateManifestUrl() {{
                    const apiKeyInput = document.getElementById("apiKeyInput");
                    const manifestUrlEl = document.getElementById("manifestUrl");
                    const installAppEl = document.getElementById("installApp");
                    const installWebEl = document.getElementById("installWeb");

                    let apiKey = "";
                    if (apiKeyInput) {{
                        apiKey = apiKeyInput.value.trim();
                    }} else {{
                        apiKey = new URLSearchParams(window.location.search).get("api_key") || "";
                    }}

                    let manifestUrl = baseManifestUrl;
                    let stremioUrl = baseStremioUrl;

                    if (apiKey) {{
                        const encodedKey = encodeURIComponent(apiKey);
                        manifestUrl = "{Config.ADDON_URL}/" + encodedKey + "/manifest.json";
                        stremioUrl = baseStremioUrl.replace("manifest.json", encodedKey + "/manifest.json");
                    }}

                    if (manifestUrlEl) {{
                        manifestUrlEl.value = manifestUrl;
                    }}
                    if (installAppEl) {{
                        installAppEl.href = "stremio://" + stremioUrl;
                    }}
                    if (installWebEl) {{
                        installWebEl.href = "https://web.stremio.com/#/addons?addon=" + encodeURIComponent(manifestUrl);
                    }}
                }}

                function copyManifestUrl() {{
                    var copyText = document.getElementById("manifestUrl");
                    copyText.select();
                    copyText.setSelectionRange(0, 99999);
                    navigator.clipboard.writeText(copyText.value);

                    var btnText = document.getElementById("btnCopyText");
                    var originalText = btnText.innerHTML;
                    btnText.innerHTML = "Copied!";

                    var copyBtn = document.getElementById("btnCopy");

                    copyBtn.style.background = "#22c55e";
                    copyBtn.style.borderColor = "#22c55e";
                    copyBtn.style.color = "#ffffff";

                    setTimeout(function() {{
                        btnText.innerHTML = originalText;
                        copyBtn.style.background = "";
                        copyBtn.style.borderColor = "";
                        copyBtn.style.color = "";
                    }}, 2000);
                }}

                window.onload = function() {{
                    updateManifestUrl();
                }};
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.api_route("/manifest.json", methods=["GET", "HEAD"])
@app.api_route("/{api_key}/manifest.json", methods=["GET", "HEAD"])
async def manifest_endpoint(api_key: str = ""):
    if Config.API_KEY and api_key != Config.API_KEY:
        return JSONResponse({"detail": "Unauthorized: Invalid API Key"}, status_code=403)
    return get_manifest(api_key)

@app.get("/catalog/{type}/{catalog_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/catalog/{type}/{catalog_id}/{extra}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/catalog/{type}/{catalog_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/catalog/{type}/{catalog_id}/{extra}.json", dependencies=[Depends(verify_api_key)])
async def catalog_handler(
    type: str,
    catalog_id: str,
    extra: str = None,
    api_key: str = ""
):
    if type not in ["movie", "series"]:
        return {"metas": []}

    extras = parse_catalog_extra(extra)
    query = extras.get("search", "")
    genre = extras.get("genre")
    skip = extras.get("skip", 0)

    logo_url = f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None

    # ---- Continue Watching feeds (from persistent watch history) ----
    if catalog_id.startswith("tg-continue"):
        kind = "series" if catalog_id.endswith("series") else "movie"
        metas = build_continue_metas(kind, logo_url)
        return {"metas": metas[skip:skip + PAGE_SIZE]}

    try:
        messages = await tg_client_manager.search_messages(query=query, limit=150)
    except Exception as e:
        logger.error(f"Catalog search failed: {e}")
        return {"metas": []}

    search_lower = query.strip().lower()

    # ------------------------------------------------------------------
    # SERIES catalog: group episodes by show
    # ------------------------------------------------------------------
    if type == "series":
        shows = build_show_groups(messages)
        if search_lower:
            shows = [s for s in shows if search_lower in s["name"].lower()]

        # Enrichment (posters/descriptions/genres) - page-only unless genre filtering
        if genre:
            titles = [s["name"] for s in shows[:150]]
            enriched = await meta_enricher.enrich_batch(titles, "series") if titles else {}
            shows_to_render = shows
        else:
            page_shows = shows[skip:skip + PAGE_SIZE]
            titles = [s["name"] for s in page_shows]
            enriched = await meta_enricher.enrich_batch(titles, "series") if titles else {}
            shows_to_render = page_shows

        metas = []
        for show in shows_to_render:
            info = enriched.get(show["name"].lower()) or {}
            genres = info.get("genres") or []
            if genre and not genre_matches(genres, genre):
                continue

            # Register episode -> show mapping for the Continue Watching feed
            try:
                cache.show_map_put(show["chat_id"], show["msg_ids_ints"], show["meta_id"], show["name"])
            except Exception:
                pass

            seasons = sorted({e["season"] for e in show["episodes_list"]})
            total_eps = len(show["episodes_list"])
            desc = f"📺 Series · {total_eps} episode(s) · Season: {', '.join(map(str, seasons))}"
            if info.get("year"):
                desc += f" · {info['year']}"
            if genres:
                desc += f"\n\n🎭 {', '.join(genres[:6])}"
            if info.get("description"):
                desc += f"\n\n{info['description'][:400]}"

            metas.append({
                "id": show["meta_id"],
                "type": "series",
                "name": info.get("name") or show["name"],
                "poster": info.get("poster") or logo_url,
                "background": info.get("background") or None,
                "description": desc,
                "genres": genres,
                "releaseInfo": str(info.get("year") or ""),
            })
        return {"metas": metas[skip:skip + PAGE_SIZE] if genre else metas}

    # ------------------------------------------------------------------
    # MOVIE catalog: skip episodes / subtitles / audio, group splits & zips
    # ------------------------------------------------------------------
    grouped_items = group_tg_messages(messages)
    pending = []  # (meta_id, name, description, is_series_like)

    for item in grouped_items:
        if isinstance(item, tuple):
            base_name, parts = item
            total_size = sum((x.video or x.document or x.audio).file_size for x in parts if (x.video or x.document or x.audio))
            first_msg = parts[0]
            chat_id = first_msg.chat.id
            msg_ids = ",".join(str(x.id) for x in parts)

            # Skip episode-like files in the movie catalog
            if parse_season_episode(base_name)[0] is not None:
                continue
            if search_lower and search_lower not in base_name.lower():
                continue

            is_zip = False
            if base_name.lower().endswith(".zip"):
                try:
                    entries = await list_zip_files(tg_client_manager.client, parts)
                    video_entries = [e for e in entries if is_video_file(e.filename)]
                    if video_entries:
                        is_zip = True
                        for entry in video_entries:
                            if search_lower and search_lower not in entry.filename.lower():
                                continue
                            tg_id = f"tgfile_splitzip_{chat_id}_{msg_ids}//{entry.filename}"
                            pending.append((
                                tg_id,
                                entry.filename,
                                f"💾 Telegram ZIP Entry\n📦 Size: {format_size(entry.file_size)}\n📂 ZIP Archive: {base_name}",
                            ))
                except Exception as e:
                    logger.error(f"Error reading split ZIP archive: {e}")

            if not is_zip:
                tg_id = f"tgfile_split_{chat_id}_{msg_ids}"
                pending.append((
                    tg_id,
                    base_name,
                    f"💾 Telegram File (Split Parts: {len(parts)})\n📦 Total Size: {format_size(total_size)}",
                ))
        else:
            msg = item
            media = msg.video or msg.document or msg.audio
            file_name = getattr(media, "file_name", None) or msg.caption or f"Telegram File {msg.id}"
            file_size = media.file_size
            caption = msg.caption or ""

            if is_subtitle_file(file_name) or msg.audio:
                continue
            # Skip episode-like files in the movie catalog
            if parse_season_episode(file_name)[0] is not None:
                continue
            if search_lower and search_lower not in file_name.lower() and search_lower not in caption.lower():
                continue

            is_zip = False
            if file_name.lower().endswith(".zip"):
                try:
                    entries = await list_zip_files(tg_client_manager.client, msg)
                    video_entries = [e for e in entries if is_video_file(e.filename)]
                    if video_entries:
                        is_zip = True
                        for entry in video_entries:
                            if search_lower and search_lower not in entry.filename.lower():
                                continue
                            tg_id = f"tgfile_zip_{msg.chat.id}_{msg.id}//{entry.filename}"
                            pending.append((
                                tg_id,
                                entry.filename,
                                f"💾 Telegram ZIP Entry\n📦 Size: {format_size(entry.file_size)}\n📂 ZIP Archive: {file_name}",
                            ))
                except Exception as e:
                    logger.error(f"Error reading standalone ZIP archive: {e}")

            if not is_zip:
                tg_id = f"tgfile_{msg.chat.id}_{msg.id}"
                desc = f"💾 Telegram File\n📦 Size: {format_size(file_size)}"
                if caption:
                    desc += f"\n💬 {caption}"
                pending.append((tg_id, file_name, desc))

    # Enrich only the requested page (genre filtering needs all titles first)
    try:
        if genre:
            titles = [p[1] for p in pending[:150]]
            enriched = await meta_enricher.enrich_batch(titles, "movie") if titles else {}
        else:
            page_pending = pending[skip:skip + PAGE_SIZE]
            titles = [p[1] for p in page_pending]
            enriched = await meta_enricher.enrich_batch(titles, "movie") if titles else {}
    except Exception as e:
        logger.error(f"Catalog enrichment failed: {e}")
        enriched = {}

    metas = []
    entries_to_render = pending if genre else pending[skip:skip + PAGE_SIZE]
    for tg_id, name, description in entries_to_render:
        info = enriched.get(name.lower()) or {}
        genres = info.get("genres") or []
        if genre and not genre_matches(genres, genre):
            continue
        final_desc = description
        if info.get("year"):
            final_desc += f"\n📆 {info['year']}"
        if genres:
            final_desc += f"\n🎭 {', '.join(genres[:6])}"
        if info.get("description"):
            final_desc += f"\n\n{info['description'][:300]}"

        meta = {
            "id": tg_id,
            "type": "movie",
            "name": info.get("name") or name,
            "description": final_desc,
            "poster": info.get("poster") or logo_url,
        }
        if info.get("background"):
            meta["background"] = info["background"]
        if info.get("year"):
            meta["releaseInfo"] = str(info["year"])
        if genres:
            meta["genres"] = genres
        metas.append(meta)

    return {"metas": metas[skip:skip + PAGE_SIZE] if genre else metas}


@app.get("/stremio_telegram_logo.png")
async def get_logo():
    if os.path.exists("stremio_telegram_logo.png"):
        return FileResponse("stremio_telegram_logo.png")
    return Response(status_code=404)


@app.get("/stremio_telegram_banner.png")
async def get_banner():
    if os.path.exists("stremio_telegram_banner.png"):
        return FileResponse("stremio_telegram_banner.png")
    return Response(status_code=404)


def _parse_media_id(base_meta_id: str):
    """Extract (chat_id, msg_ids_str, is_split) from a tgfile_* style id."""
    is_split = False
    if base_meta_id.startswith(("tgfile_splitzip_", "tgfile_split_")):
        is_split = True
        parts = base_meta_id.split("_")
        chat_id = parts[2]
        msg_ids_str = parts[3]
    elif base_meta_id.startswith("tgfile_zip_"):
        parts = base_meta_id.split("_")
        chat_id = parts[2]
        msg_ids_str = parts[3]
    elif base_meta_id.startswith("tgshow_"):
        parts = base_meta_id.split("_")
        chat_id = parts[1]
        msg_ids_str = parts[2]
    else:  # tgfile_
        parts = base_meta_id.split("_")
        chat_id = parts[1]
        msg_ids_str = parts[2]
    return chat_id, msg_ids_str, is_split


def _to_int_chat(chat_id):
    try:
        return int(chat_id)
    except (ValueError, TypeError):
        return chat_id


async def _fetch_messages(chat_id, msg_ids_str: str) -> list:
    msg_id_list = []
    for x in msg_ids_str.split(","):
        x = x.strip()
        if x.isdigit():
            msg_id_list.append(int(x))
    messages = []
    for msg_id in msg_id_list:
        try:
            msg = await tg_client_manager.get_message(msg_id, chat_id=_to_int_chat(chat_id))
            if msg:
                messages.append(msg)
        except Exception as e:
            logger.warning(f"Meta fetch: message {msg_id} unavailable: {e}")
    return messages


@app.get("/meta/{type}/{meta_id}.json", dependencies=[Depends(verify_api_key)])
@app.get("/{api_key}/meta/{type}/{meta_id}.json", dependencies=[Depends(verify_api_key)])
async def meta_handler(type: str, meta_id: str, api_key: str = ""):
    if not (meta_id.startswith("tgfile_") or meta_id.startswith("tgshow_")):
        return {"meta": {}}

    try:
        is_zip_entry = False
        zip_entry_filename = ""
        base_meta_id = meta_id
        if "//" in meta_id:
            is_zip_entry = True
            base_meta_id, zip_entry_filename = meta_id.split("//", 1)

        logo_url = f"{Config.ADDON_URL}/stremio_telegram_logo.png" if getattr(Config, "ADDON_URL", None) else None
        banner_url = f"{Config.ADDON_URL}/stremio_telegram_banner.png" if getattr(Config, "ADDON_URL", None) else None

        # ------------------------------------------------------------------
        # Show meta: build episode list grouped by season/episode
        # ------------------------------------------------------------------
        if base_meta_id.startswith("tgshow_"):
            chat_id, msg_ids_str, _ = _parse_media_id(base_meta_id)
            messages = await _fetch_messages(chat_id, msg_ids_str)
            if not messages:
                return {"meta": {}}

            shows = build_show_groups(messages)
            show = None
            if shows:
                # Prefer the show whose id matches exactly, else the first
                for s in shows:
                    if s["meta_id"] == base_meta_id:
                        show = s
                        break
                if show is None:
                    show = shows[0]

            if show:
                info = await meta_enricher.enrich(show["name"], "series")
                videos = []
                for e in show["episodes_list"]:
                    videos.append({
                        "id": e["id"],
                        "title": f"S{e['season']:02d}E{e['episode']:02d} · {e['filename']}",
                        "name": e["filename"],
                        "season": e["season"],
                        "episode": e["episode"],
                    })
                seasons = sorted({e["season"] for e in show["episodes_list"]})
                desc = f"📺 Series · {len(videos)} episode(s) · Season: {', '.join(map(str, seasons))}"
                if info.get("description"):
                    desc += f"\n\n{info['description'][:500]}"
                meta = {
                    "id": meta_id,
                    "type": "series",
                    "name": info.get("name") or show["name"],
                    "description": desc,
                    "poster": info.get("poster") or logo_url,
                    "background": info.get("background") or banner_url,
                    "logo": logo_url,
                    "genres": info.get("genres") or [],
                    "releaseInfo": str(info.get("year") or ""),
                    "videos": videos,
                }
                return {"meta": meta}
            return {"meta": {}}

        # ------------------------------------------------------------------
        # Regular file / zip entry meta (with poster enrichment)
        # ------------------------------------------------------------------
        chat_id, msg_ids_str, is_split = _parse_media_id(base_meta_id)
        messages = await _fetch_messages(chat_id, msg_ids_str)
        if not messages:
            return {"meta": {}}

        first_msg = messages[0]
        media = first_msg.video or first_msg.document or first_msg.audio
        first_fn = getattr(media, "file_name", "video.mp4") or "video.mp4"

        if is_zip_entry and zip_entry_filename:
            file_name = zip_entry_filename
            zip_entries = await list_zip_files(tg_client_manager.client, messages)
            file_size = 0
            for entry in zip_entries:
                if entry.filename == zip_entry_filename:
                    file_size = entry.file_size
                    break
            description = f"💾 Telegram ZIP Entry\n📦 Size: {format_size(file_size)}\n📂 ZIP Archive: {first_fn}"
        else:
            file_name = first_fn
            if is_split:
                base_name, _ = parse_split_info(first_fn)
                file_name = base_name or first_fn
                total_size = sum((x.video or x.document or x.audio).file_size for x in messages if (x.video or x.document or x.audio))
                description = f"💾 Telegram File (Split Parts: {len(messages)})\n📦 Total Size: {format_size(total_size)}"
            else:
                total_size = media.file_size
                caption = first_msg.caption or ""
                description = f"💾 Telegram File\n📦 Size: {format_size(total_size)}\n💬 {caption}" if caption else f"💾 Telegram File\n📦 Size: {format_size(total_size)}"

        # Enrich with TMDB/Cinemeta info when a title can be derived
        poster = logo_url
        background = banner_url
        release_info = ""
        try:
            title_guess, year_guess = meta_enricher.derive_title_year(file_name)
            if title_guess and len(title_guess) >= 2:
                info = await meta_enricher.enrich(title_guess, "movie", year_guess)
                if info:
                    if info.get("poster"):
                        poster = info["poster"]
                    if info.get("background"):
                        background = info["background"]
                    if info.get("year"):
                        release_info = str(info["year"])
                    bits = []
                    if info.get("year"):
                        bits.append(f"📆 {info['year']}")
                    if info.get("genres"):
                        bits.append(f"🎭 {', '.join(info['genres'][:6])}")
                    extra = "\n".join(bits)
                    if extra:
                        description = f"{description}\n{extra}"
                    if info.get("description"):
                        description = f"{description}\n\n{info['description'][:400]}"
        except Exception as e:
            logger.debug(f"Meta enrichment failed for {meta_id}: {e}")

        meta = {
            "id": meta_id,
            "type": type,
            "name": file_name,
            "description": description,
            "poster": poster,
            "background": background,
            "logo": logo_url,
        }
        if release_info:
            meta["releaseInfo"] = release_info

        if type == "series" and not is_zip_entry:
            season, episode = parse_season_episode(file_name)
            if season is None:
                season, episode = 1, 1
            meta["videos"] = [
                {
                    "id": meta_id,
                    "title": file_name,
                    "season": season,
                    "episode": episode
                }
            ]

        return {"meta": meta}
    except Exception as e:
        logger.error(f"Failed to generate metadata for {meta_id}: {e}")
        return {"meta": {}}

async def find_subtitles_for_video(video_filename: str, api_key: str = "", cached_messages=None) -> list:
    subtitles = []
    search_results = cached_messages or []
    query_param = f"?api_key={api_key}" if api_key else ""

    if not search_results:
        query = get_search_query_from_filename(video_filename)
        if query:
            try:
                search_results = await tg_client_manager.search_messages(query=query, limit=20)
            except Exception as e:
                logger.error(f"Subtitle search failed for '{query}': {e}")

    seen_msg_ids = set()
    for msg in search_results:
        if msg.id in seen_msg_ids:
            continue

        doc = msg.document or msg.audio or msg.video
        if not doc:
            continue

        sub_fn = getattr(doc, "file_name", "") or ""
        if sub_fn.lower().endswith(('.srt', '.vtt', '.ass')):
            if matches_subtitle(video_filename, sub_fn):
                seen_msg_ids.add(msg.id)

                lang = "eng"
                sub_fn_lower = sub_fn.lower()
                if ".spa" in sub_fn_lower or "spanish" in sub_fn_lower:
                    lang = "spa"
                elif ".fre" in sub_fn_lower or "french" in sub_fn_lower:
                    lang = "fre"

                subtitles.append({
                    "id": f"tgsub_{msg.chat.id}_{msg.id}",
                    "url": f"{Config.ADDON_URL}/stream/subtitle/{msg.chat.id}/{msg.id}/{urllib.parse.quote(sub_fn)}{query_param}",
                    "lang": lang
                })

    return subtitles


def _stream_qs(actual_key: str, type_str: str = "") -> str:
    """Build the query string for stream proxy URLs (api_key + media type)."""
    params = []
    if actual_key:
        params.append(f"api_key={urllib.parse.quote(actual_key)}")
    if type_str in ("movie", "series"):
        params.append(f"type={type_str}")
    return ("?" + "&".join(params)) if params else ""


def _range_start(request: Request, total_size: int) -> int:
    range_header = request.headers.get("Range")
    if range_header:
        try:
            bytes_range = range_header.replace("bytes=", "").split("-")
            if bytes_range[0]:
                return int(bytes_range[0])
        except ValueError:
            pass
    return 0


def _cached_poster_for(filename: str):
    """Look up an already-cached poster for a filename (no network calls)."""
    try:
        title, year = meta_enricher.derive_title_year(filename)
        if title and len(title) >= 2:
            for mtype in ("movie", "series"):
                key = meta_enricher.cache_key_for(title, mtype, year, Config.TMDB_LANGUAGE)
                info = cache.cache_get("meta", key) or {}
                if info.get("poster"):
                    return info["poster"]
    except Exception:
        pass
    return None


def _record_play(request: Request, file_key: str, meta_id: str, display_name: str,
                 total_size: int, position: int, chat_id, msg_id):
    """Record a start/seek/progress watch event (drives Continue Watching + TG logs)."""
    if request.method != "GET":
        return
    kind = request.query_params.get("type", "movie")
    if kind not in ("movie", "series"):
        kind = "movie"
    try:
        event = cache.record_watch(
            file_key=file_key,
            meta_id=meta_id,
            display_name=display_name,
            total_bytes=total_size,
            position=position,
            kind=kind,
            chat_id=chat_id,
            msg_id=msg_id,
            poster=_cached_poster_for(display_name),
        )
    except Exception as e:
        logger.debug(f"record_watch failed: {e}")
        return

    etype = event.get("type")
    if etype in ("start", "seek"):
        pct = (position / total_size * 100) if total_size else 0
        try:
            task = asyncio.create_task(tg_client_manager.send_watch_log(
                etype,
                display_name,
                chat_id,
                msg_id,
                event.get("play_count", 1),
                progress=f"{pct:.0f}%" if total_size else "",
            ))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        except Exception:
            pass


@app.get("/stream/{type}/{stream_id}.json")
@app.get("/{api_key}/stream/{type}/{stream_id}.json")
async def stream_handler(
    type: str,
    stream_id: str,
    request: Request,
    api_key: str = ""
):
    # Fix: accept the api key from BOTH the path and the query string
    actual_key = api_key or request.query_params.get("api_key", "")
    if Config.API_KEY and actual_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    streams = []

    if stream_id.startswith("tgfile_"):
        if "//" in stream_id:
            base_stream_id, zip_entry_filename = stream_id.split("//", 1)
            if base_stream_id.startswith(("tgfile_splitzip_", "tgfile_split_")):
                parts = base_stream_id.split("_")
                chat_id = parts[2]
                msg_ids = parts[3]
            elif base_stream_id.startswith("tgfile_zip_"):
                parts = base_stream_id.split("_")
                chat_id = parts[2]
                msg_ids = parts[3]
            else:
                parts = base_stream_id.split("_")
                chat_id = parts[1]
                msg_ids = parts[2]

            try:
                chat_id_val = _to_int_chat(chat_id)
            except Exception:
                chat_id_val = chat_id

            msg_id_list = [int(x) for x in msg_ids.split(",") if x.strip().isdigit()]

            try:
                messages = []
                for msg_id in msg_id_list:
                    msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
                    if msg:
                        messages.append(msg)

                if messages:
                    zip_entries = await list_zip_files(tg_client_manager.client, messages)
                    file_size = 0
                    for entry in zip_entries:
                        if entry.filename == zip_entry_filename:
                            file_size = entry.file_size
                            break

                    stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg_ids}/{urllib.parse.quote(zip_entry_filename)}{_stream_qs(actual_key, type)}"
                    subtitles = await find_subtitles_for_video(zip_entry_filename, api_key=actual_key)

                    streams.append({
                        "name": "▶ TG ZIP Play",
                        "title": f"{zip_entry_filename}\n💾 Stream ZIP entry | 📦 {format_size(file_size)}",
                        "url": stream_url,
                        "subtitles": subtitles,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
            except Exception as e:
                logger.error(f"Failed resolving zip stream for {stream_id}: {e}")
        elif stream_id.startswith(("tgfile_split_", "tgfile_splitzip_")):
            parts = stream_id.split("_")
            if len(parts) >= 4:
                chat_id = parts[2]
                msg_ids = parts[3]
                try:
                    msg_id_list = [int(x) for x in msg_ids.split(",") if x.isdigit()]
                    chat_id_val = _to_int_chat(chat_id)

                    first_msg = await tg_client_manager.get_message(msg_id_list[0], chat_id=chat_id_val)
                    media = first_msg.video or first_msg.document or first_msg.audio
                    first_fn = getattr(media, "file_name", "video.mp4") or "video.mp4"
                    base_name, _ = parse_split_info(first_fn)
                    if not base_name:
                        base_name = first_fn

                    total_size = 0
                    for m_id in msg_id_list:
                        m = await tg_client_manager.get_message(m_id, chat_id=chat_id_val)
                        if m:
                            med = m.video or m.document or m.audio
                            if med:
                                total_size += med.file_size

                    stream_url = f"{Config.ADDON_URL}/stream/split/{chat_id}/{msg_ids}/{urllib.parse.quote(base_name)}{_stream_qs(actual_key, type)}"

                    streams.append({
                        "name": "▶ TG Play (Split)",
                        "title": f"{base_name}\n💾 Stitch stream | 📦 {format_size(total_size)}",
                        "url": stream_url,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
                except Exception as e:
                    logger.error(f"Failed resolving split stream for {stream_id}: {e}")
        else:
            parts = stream_id.split("_")
            if len(parts) >= 3:
                chat_id = parts[1]
                msg_id = parts[2]
                try:
                    chat_id_val = _to_int_chat(chat_id)
                    msg = await tg_client_manager.get_message(int(msg_id), chat_id=chat_id_val)
                    media = msg.video or msg.document or msg.audio
                    file_name = getattr(media, "file_name", "video.mp4") or "video.mp4"
                    file_size = media.file_size

                    stream_url = f"{Config.ADDON_URL}/stream/file/{chat_id}/{msg_id}/{urllib.parse.quote(file_name)}{_stream_qs(actual_key, type)}"
                    subtitles = await find_subtitles_for_video(file_name, api_key=actual_key)

                    streams.append({
                        "name": "▶ TG Play",
                        "title": f"{file_name}\n💾 Direct stream | 📦 {format_size(file_size)}",
                        "url": stream_url,
                        "subtitles": subtitles,
                        "behaviorHints": {
                            "notWebReady": True,
                        }
                    })
                except Exception as e:
                    logger.error(f"Failed resolving direct stream for {stream_id}: {e}")

    elif stream_id.startswith("tt"):
        imdb_id = stream_id
        season = None
        episode = None

        if ":" in stream_id:
            parts = stream_id.split(":")
            imdb_id = parts[0]
            season = int(parts[1])
            episode = int(parts[2])

        try:
            meta = await get_metadata_from_cinemeta(type, imdb_id)
            movie_name = meta.get("name")
            year_str = meta.get("year")
            year = None
            if year_str:
                try:
                    year = int(str(year_str).split("-")[0])
                except Exception:
                    pass

            if movie_name:
                matcher = VideoMatcher()
                if type == "series" and season is not None and episode is not None:
                    queries = matcher.make_series_search_queries(movie_name, season, episode)
                else:
                    queries = matcher.make_movie_search_queries(movie_name, year)

                logger.info(f"Resolved IMDb {imdb_id} to '{movie_name}'. Searching Telegram with {len(queries)} safe queries...")

                # Search target channels for queries in parallel
                search_tasks = [tg_client_manager.search_messages(query=q, limit=100) for q in queries]
                search_results_lists = await asyncio.gather(*search_tasks, return_exceptions=True)

                # Dedup results
                seen_messages = set()
                tg_results_flat = []
                for res_list in search_results_lists:
                    if isinstance(res_list, list):
                        for msg in res_list:
                            if msg and (msg.chat.id, msg.id) not in seen_messages:
                                seen_messages.add((msg.chat.id, msg.id))
                                tg_results_flat.append(msg)

                grouped_results = group_tg_messages(tg_results_flat)
                valid_streams = []

                for item in grouped_results:
                    if isinstance(item, tuple):
                        base_name, parts = item
                        first_msg = parts[0]
                        media = first_msg.video or first_msg.document or first_msg.audio
                        file_name = getattr(media, "file_name", "") or ""
                        caption = first_msg.caption or ""

                        score = matcher.calculate_match_score(
                            filename=base_name,
                            caption=caption,
                            title=movie_name,
                            year=year,
                            season=season,
                            episode=episode
                        )
                        if score < matcher.score_threshold:
                            continue

                        total_size = sum((x.video or x.document or x.audio).file_size for x in parts if (x.video or x.document or x.audio))
                        msg_ids = ",".join(str(x.id) for x in parts)
                        chat_id = first_msg.chat.id
                        resolution = parse_video_resolution(f"{base_name} {caption}")

                        is_zip = False
                        if base_name.lower().endswith(".zip"):
                            try:
                                entries = await list_zip_files(tg_client_manager.client, parts)
                                video_entries = [e for e in entries if is_video_file(e.filename)]
                                if video_entries:
                                    is_zip = True
                                    for entry in video_entries:
                                        entry_score = matcher.calculate_match_score(
                                            filename=entry.filename,
                                            caption="",
                                            title=movie_name,
                                            year=year,
                                            season=season,
                                            episode=episode
                                        )
                                        if entry_score < matcher.score_threshold:
                                            continue

                                        entry_res = parse_video_resolution(entry.filename)
                                        stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg_ids}/{urllib.parse.quote(entry.filename)}{_stream_qs(actual_key, type)}"
                                        subtitles = await find_subtitles_for_video(entry.filename, api_key=actual_key, cached_messages=tg_results_flat)
                                        valid_streams.append({
                                            "name": f"▶ TG ZIP Play [{entry_res}]",
                                            "title": f"{entry.filename}\n💾 Stream ZIP entry | 📦 {format_size(entry.file_size)}",
                                            "url": stream_url,
                                            "subtitles": subtitles,
                                            "behaviorHints": {"notWebReady": True},
                                            "_res_score": get_resolution_score(entry_res),
                                            "_file_size": entry.file_size
                                        })
                            except Exception as e:
                                logger.error(f"Error checking split ZIP for IMDB: {e}")

                        if not is_zip:
                            if not is_video_file(base_name):
                                continue
                            stream_url = f"{Config.ADDON_URL}/stream/split/{chat_id}/{msg_ids}/{urllib.parse.quote(base_name)}{_stream_qs(actual_key, type)}"
                            valid_streams.append({
                                "name": f"▶ TG Play (Split) [{resolution}]",
                                "title": f"{base_name}\n💾 Stitch stream | 📦 {format_size(total_size)}",
                                "url": stream_url,
                                "behaviorHints": {"notWebReady": True},
                                "_res_score": get_resolution_score(resolution),
                                "_file_size": total_size
                            })
                    else:
                        msg = item
                        media = msg.video or msg.document or msg.audio
                        file_name = getattr(media, "file_name", None) or msg.caption or ""
                        caption = msg.caption or ""

                        if msg.video and not is_video_file(file_name):
                            file_name = f"{file_name}.mp4" if file_name else f"{movie_name}.mp4"

                        score = matcher.calculate_match_score(
                            filename=file_name,
                            caption=caption,
                            title=movie_name,
                            year=year,
                            season=season,
                            episode=episode
                        )
                        if score < matcher.score_threshold:
                            continue

                        file_size = media.file_size
                        chat_id = msg.chat.id
                        resolution = parse_video_resolution(f"{file_name} {caption}")

                        is_zip = False
                        if file_name.lower().endswith(".zip"):
                            try:
                                entries = await list_zip_files(tg_client_manager.client, msg)
                                video_entries = [e for e in entries if is_video_file(e.filename)]
                                if video_entries:
                                    is_zip = True
                                    for entry in video_entries:
                                        entry_score = matcher.calculate_match_score(
                                            filename=entry.filename,
                                            caption="",
                                            title=movie_name,
                                            year=year,
                                            season=season,
                                            episode=episode
                                        )
                                        if entry_score < matcher.score_threshold:
                                            continue

                                        entry_res = parse_video_resolution(entry.filename)
                                        stream_url = f"{Config.ADDON_URL}/stream/zip/{chat_id}/{msg.id}/{urllib.parse.quote(entry.filename)}{_stream_qs(actual_key, type)}"
                                        subtitles = await find_subtitles_for_video(entry.filename, api_key=actual_key, cached_messages=tg_results_flat)
                                        valid_streams.append({
                                            "name": f"▶ TG ZIP Play [{entry_res}]",
                                            "title": f"{entry.filename}\n💾 Stream ZIP entry | 📦 {format_size(entry.file_size)}",
                                            "url": stream_url,
                                            "subtitles": subtitles,
                                            "behaviorHints": {"notWebReady": True},
                                            "_res_score": get_resolution_score(entry_res),
                                            "_file_size": entry.file_size
                                        })
                            except Exception as e:
                                logger.error(f"Error checking standalone ZIP for IMDB: {e}")

                        if not is_zip:
                            if not is_video_file(file_name):
                                continue
                            stream_url = f"{Config.ADDON_URL}/stream/file/{chat_id}/{msg.id}/{urllib.parse.quote(file_name)}{_stream_qs(actual_key, type)}"
                            subtitles = await find_subtitles_for_video(file_name, api_key=actual_key, cached_messages=tg_results_flat)

                            valid_streams.append({
                                "name": f"▶ TG Play [{resolution}]",
                                "title": f"{file_name}\n💾 Telegram File | 📦 {format_size(file_size)}",
                                "url": stream_url,
                                "subtitles": subtitles,
                                "behaviorHints": {"notWebReady": True},
                                "_res_score": get_resolution_score(resolution),
                                "_file_size": file_size
                            })

                # Sort by resolution, then size
                valid_streams.sort(key=lambda s: (s.get("_res_score", 0), s.get("_file_size", 0)), reverse=True)
                for s in valid_streams:
                    s.pop("_res_score", None)
                    s.pop("_file_size", None)
                    streams.append(s)

        except Exception as e:
            logger.error(f"Cinemeta search/resolve failed: {e}")

    return {"streams": streams}

@app.get("/subtitles/{type}/{id}.json")
@app.get("/subtitles/{type}/{id}/{extra}.json")
@app.get("/{api_key}/subtitles/{type}/{id}.json")
@app.get("/{api_key}/subtitles/{type}/{id}/{extra}.json")
async def subtitles_handler(
    type: str,
    id: str,
    request: Request,
    extra: str = None,
    api_key: str = ""
):
    actual_key = api_key or request.query_params.get("api_key", "")
    if Config.API_KEY and actual_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    subtitles = []

    if id.startswith("tgfile_"):
        parts = id.split("_")
        if len(parts) >= 3:
            chat_id = parts[1]
            msg_id = parts[2]
            try:
                chat_id_val = _to_int_chat(chat_id)
                msg = await tg_client_manager.get_message(int(msg_id), chat_id=chat_id_val)
                media = msg.video or msg.document or msg.audio
                video_filename = getattr(media, "file_name", "") or ""
                if video_filename:
                    subtitles = await find_subtitles_for_video(video_filename, api_key=actual_key)
            except Exception as e:
                logger.error(f"Failed to resolve subtitles for direct catalog ID {id}: {e}")

    elif id.startswith("tt"):
        imdb_id = id
        season = None
        episode = None
        if ":" in id:
            parts = id.split(":")
            imdb_id = parts[0]
            season = int(parts[1])
            episode = int(parts[2])

        try:
            video_filename = None
            if extra:
                decoded_extra = urllib.parse.unquote(extra)
                if "?" in decoded_extra:
                    decoded_extra = decoded_extra.split("?", 1)[0]
                params = urllib.parse.parse_qs(decoded_extra)
                if "filename" in params:
                    video_filename = params["filename"][0]

            if video_filename:
                logger.info(f"Resolving subtitles directly for filename: '{video_filename}'")
                subtitles = await find_subtitles_for_video(video_filename, api_key=actual_key)
            else:
                meta = await get_metadata_from_cinemeta(type, imdb_id)
                movie_name = meta.get("name")
                if movie_name:
                    tg_results = await tg_client_manager.search_messages(query=movie_name, limit=50)
                    for msg in tg_results:
                        media = msg.video or msg.document or msg.audio
                        fn = getattr(media, "file_name", "") or msg.caption or ""
                        if type == "series" and not matches_episode(fn, season, episode):
                            continue
                        video_filename = fn
                        break

                    if video_filename:
                        subtitles = await find_subtitles_for_video(video_filename, api_key=actual_key, cached_messages=tg_results)
        except Exception as e:
            logger.error(f"Failed to resolve subtitles for IMDb ID {id}: {e}")

    return {"subtitles": subtitles}

@app.api_route("/stream/subtitle/{chat_id}/{message_id}/{filename}", methods=["GET", "HEAD"])
async def tg_subtitle_proxy(
    chat_id: str,
    message_id: int,
    filename: str,
    request: Request,
    api_key: str = ""
):
    actual_key = api_key or request.query_params.get("api_key", "")
    if Config.API_KEY and actual_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        chat_id_val = _to_int_chat(chat_id)
        msg = await tg_client_manager.get_message(message_id, chat_id=chat_id_val)
    except Exception as e:
        logger.error(f"Proxy failed to fetch subtitle message: {e}")
        raise HTTPException(status_code=404, detail="Subtitle file not found")

    if not msg:
        raise HTTPException(status_code=404, detail="Subtitle message not found")

    media = msg.document or msg.audio or msg.video
    if not media:
        raise HTTPException(status_code=404, detail="No media found in subtitle message")

    content_type = "text/plain"
    filename_lower = filename.lower()
    if filename_lower.endswith(".srt"):
        content_type = "application/x-subrip"
    elif filename_lower.endswith(".vtt"):
        content_type = "text/vtt"
    elif filename_lower.endswith(".ass"):
        content_type = "text/plain"

    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "Access-Control-Allow-Origin": "*",
        "Content-Length": str(media.file_size),
    }

    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type=content_type,
            headers=headers
        )

    try:
        logger.info(f"Downloading subtitle file from Telegram: {filename} (msg ID {message_id})")
        file_buffer = await tg_client_manager.client.download_media(msg, in_memory=True)
        content = file_buffer.getvalue()
    except Exception as e:
        logger.error(f"Failed to download subtitle file: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve subtitle media")

    return Response(
        content=content,
        media_type=content_type,
        headers=headers
    )

@app.api_route("/stream/file/{chat_id}/{message_id}/{filename}", methods=["GET", "HEAD"])
async def tg_stream_proxy(
    chat_id: str,
    message_id: int,
    filename: str,
    request: Request,
    api_key: str = ""
):
    actual_key = api_key or request.query_params.get("api_key", "")
    if Config.API_KEY and actual_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        chat_id_val = _to_int_chat(chat_id)
        # Get fresh message reference for streaming
        try:
            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=message_id)
        except Exception:
            msg = await tg_client_manager.get_message(message_id, chat_id=chat_id_val)
    except Exception as e:
        logger.error(f"Proxy failed to fetch message: {e}")
        raise HTTPException(status_code=404, detail="Media file not found")

    if not msg:
        raise HTTPException(status_code=404, detail="Media message not found")

    media = msg.video or msg.document or msg.audio
    if not media:
        raise HTTPException(status_code=404, detail="No playable media found in message")

    file_size = media.file_size
    mime_type = media.mime_type or "video/mp4"

    start = _range_start(request, file_size)
    _record_play(request, f"file:{chat_id_val}:{message_id}", f"tgfile_{chat_id_val}_{message_id}",
                 filename, file_size, start, chat_id_val, message_id)

    end = file_size - 1
    if request.headers.get("Range"):
        try:
            bytes_range = request.headers.get("Range").replace("bytes=", "").split("-")
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
        except ValueError:
            pass

    content_length = end - start + 1

    chunk_size = 1024 * 1024
    offset = start // chunk_size
    skip_bytes = start % chunk_size

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": f'inline; filename="{filename}"',
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }

    # Content-Range must only be sent on 206
    status_code = 200
    if request.headers.get("Range"):
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    if request.method == "HEAD":
        logger.info(f"HEAD request for media '{filename}' (bytes {start}-{end}/{file_size}) - Status {status_code}")
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )

    async def file_generator():
        nonlocal msg
        bytes_sent = 0
        bytes_to_skip = skip_bytes
        retry_count = 0
        max_retries = 2
        current_offset = offset

        while retry_count <= max_retries:
            try:
                # Stream using message object
                async for chunk in tg_client_manager.client.stream_media(msg, offset=current_offset):
                    if bytes_to_skip > 0:
                        if bytes_to_skip < len(chunk):
                            chunk = chunk[bytes_to_skip:]
                            bytes_to_skip = 0
                        else:
                            bytes_to_skip -= len(chunk)
                            continue

                    if bytes_sent + len(chunk) > content_length:
                        chunk = chunk[:content_length - bytes_sent]

                    yield chunk
                    bytes_sent += len(chunk)

                    if bytes_sent >= content_length:
                        break
                # Stream completed successfully
                break
            except asyncio.CancelledError:
                logger.info(f"Streaming cancelled by client for message {message_id}")
                break
            except Exception as e:
                err_str = str(e).upper()
                is_expired = "FILEREFERENCEEXPIRED" in type(e).__name__.upper() or "FILE_REFERENCE" in err_str
                if is_expired and retry_count < max_retries:
                    retry_count += 1
                    logger.warning(f"File reference expired for message {message_id}, refreshing and retrying ({retry_count}/{max_retries})")
                    try:
                        msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=message_id)
                        total_bytes_streamed = start + bytes_sent
                        current_offset = total_bytes_streamed // chunk_size
                        bytes_to_skip = total_bytes_streamed % chunk_size
                        continue
                    except Exception as refresh_err:
                        logger.error(f"Failed to refresh message for reference recovery: {refresh_err}")
                        break
                else:
                    logger.error(f"Streaming error on message {message_id}: {e}")
                    break

    logger.info(f"Streaming media '{filename}' (bytes {start}-{end}/{file_size}) - Status {status_code}")

    return StreamingResponse(
        file_generator(),
        status_code=status_code,
        media_type=mime_type,
        headers=headers
    )

@app.api_route("/stream/split/{chat_id}/{message_ids}/{filename}", methods=["GET", "HEAD"])
async def tg_split_stream_proxy(
    chat_id: str,
    message_ids: str,
    filename: str,
    request: Request,
    api_key: str = ""
):
    actual_key = api_key or request.query_params.get("api_key", "")
    if Config.API_KEY and actual_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    msg_id_list = [int(x) for x in message_ids.split(",") if x.strip().isdigit()]
    if not msg_id_list:
        raise HTTPException(status_code=400, detail="Invalid message IDs")

    try:
        chat_id_val = _to_int_chat(chat_id)
    except Exception:
        chat_id_val = chat_id

    chunks_info = []
    total_size = 0

    for msg_id in msg_id_list:
        try:
            # Get fresh message reference for streaming
            try:
                msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=msg_id)
            except Exception:
                msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)

            if not msg:
                raise HTTPException(status_code=404, detail=f"Message {msg_id} not found")
            media = msg.video or msg.document or msg.audio
            if not media:
                raise HTTPException(status_code=400, detail=f"No media in message {msg_id}")

            chunks_info.append({
                "msg": msg,
                "media": media,
                "size": media.file_size,
                "start_byte": total_size,
                "end_byte": total_size + media.file_size - 1
            })
            total_size += media.file_size
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching metadata for msg {msg_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed resolving split file metadata")

    start = _range_start(request, total_size)
    _record_play(request, f"split:{chat_id_val}:{message_ids}", f"tgfile_split_{chat_id_val}_{message_ids}",
                 filename, total_size, start, chat_id_val, msg_id_list[0])

    end = total_size - 1
    if request.headers.get("Range"):
        try:
            bytes_range = request.headers.get("Range").replace("bytes=", "").split("-")
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
        except ValueError:
            pass

    content_length = end - start + 1
    mime_type = chunks_info[0]["media"].mime_type or "video/mp4"

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": f'inline; filename="{filename}"',
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }

    status_code = 200
    if request.headers.get("Range"):
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"

    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )

    async def split_file_generator():
        bytes_sent = 0
        block_size = 1024 * 1024  # 1 MB blocks

        for chunk in chunks_info:
            c_start = chunk["start_byte"]
            c_end = chunk["end_byte"]

            if c_end < start or c_start > end:
                continue

            read_start = max(c_start, start)
            read_end = min(c_end, end)
            chunk_read_len = read_end - read_start + 1

            local_offset = read_start - c_start
            offset_blocks = local_offset // block_size
            skip_bytes = local_offset % block_size

            chunk_bytes_sent = 0
            bytes_to_skip = skip_bytes

            try:
                # Stream using message object
                async for block in tg_client_manager.client.stream_media(chunk["msg"], offset=offset_blocks):
                    if bytes_to_skip > 0:
                        if bytes_to_skip < len(block):
                            block = block[bytes_to_skip:]
                            bytes_to_skip = 0
                        else:
                            bytes_to_skip -= len(block)
                            continue

                    if chunk_bytes_sent + len(block) > chunk_read_len:
                        block = block[:chunk_read_len - chunk_bytes_sent]

                    yield block
                    chunk_bytes_sent += len(block)
                    bytes_sent += len(block)

                    if chunk_bytes_sent >= chunk_read_len:
                        break
            except asyncio.CancelledError:
                logger.info("Split streaming cancelled by client")
                break
            except Exception as e:
                logger.error(f"Error streaming split chunk: {e}")
                break

            if bytes_sent >= content_length:
                break

    logger.info(f"Streaming split media '{filename}' (bytes {start}-{end}/{total_size}) - Status {status_code}")

    return StreamingResponse(
        split_file_generator(),
        status_code=status_code,
        media_type=mime_type,
        headers=headers
    )

@app.api_route("/stream/zip/{chat_id}/{message_ids}/{filename}", methods=["GET", "HEAD"])
async def tg_zip_stream_proxy(
    chat_id: str,
    message_ids: str,
    filename: str,
    request: Request,
    api_key: str = ""
):
    actual_key = api_key or request.query_params.get("api_key", "")
    if Config.API_KEY and actual_key != Config.API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    msg_id_list = [int(x) for x in message_ids.split(",") if x.strip().isdigit()]
    if not msg_id_list:
        raise HTTPException(status_code=400, detail="Invalid message IDs")

    try:
        chat_id_val = _to_int_chat(chat_id)
    except Exception:
        chat_id_val = chat_id

    messages = []
    for msg_id in msg_id_list:
        # Get fresh message reference for streaming
        try:
            msg = await tg_client_manager.client.get_messages(chat_id=chat_id_val, message_ids=msg_id)
        except Exception:
            msg = await tg_client_manager.get_message(msg_id, chat_id=chat_id_val)
        if msg:
            messages.append(msg)

    if not messages:
        raise HTTPException(status_code=404, detail="Messages not found")

    zip_entries = await list_zip_files(tg_client_manager.client, messages)
    target_entry = None
    for entry in zip_entries:
        if entry.filename == filename:
            target_entry = entry
            break

    if not target_entry:
        raise HTTPException(status_code=404, detail=f"File '{filename}' not found in ZIP archive")

    file_size = target_entry.file_size
    mime_type = "video/mp4"
    filename_lower = filename.lower()
    if filename_lower.endswith(".mkv"):
        mime_type = "video/x-matroska"
    elif filename_lower.endswith(".mp4"):
        mime_type = "video/mp4"
    elif filename_lower.endswith(".avi"):
        mime_type = "video/x-msvideo"

    start = _range_start(request, file_size)
    zip_prefix = "tgfile_splitzip_" if len(msg_id_list) > 1 else "tgfile_zip_"
    _record_play(request, f"zip:{chat_id_val}:{message_ids}:{filename}", f"{zip_prefix}{chat_id_val}_{message_ids}//{filename}",
                 filename, file_size, start, chat_id_val, msg_id_list[0])

    end = file_size - 1
    if request.headers.get("Range"):
        try:
            bytes_range = request.headers.get("Range").replace("bytes=", "").split("-")
            if len(bytes_range) > 1 and bytes_range[1]:
                end = int(bytes_range[1])
        except ValueError:
            pass

    content_length = end - start + 1

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Disposition": f'inline; filename="{filename}"',
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }

    status_code = 200
    if request.headers.get("Range"):
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )

    import zipfile
    if target_entry.compress_type == zipfile.ZIP_STORED:
        logger.info(f"ZIP entry '{filename}' is STORED (uncompressed). Using direct offset proxy.")
        reader = TelegramSeekableReader(tg_client_manager.client, messages)
        data_start = await get_zip_entry_data_offset(reader, target_entry.header_offset)

        stream_start = data_start + start
        stream_end = data_start + end
        stream_len = stream_end - stream_start + 1

        chunks_info = []
        total_size = 0

        for part in reader.parts:
            chunks_info.append({
                "message": part["message"],
                "media": part["media"],
                "size": part["size"],
                "start_byte": part["start"],
                "end_byte": part["end"] - 1
            })
            total_size += part["size"]

        async def split_file_generator():
            bytes_sent = 0
            block_size = 1024 * 1024

            for chunk in chunks_info:
                c_start = chunk["start_byte"]
                c_end = chunk["end_byte"]

                if c_end < stream_start or c_start > stream_end:
                    continue

                read_start = max(c_start, stream_start)
                read_end = min(c_end, stream_end)
                chunk_read_len = read_end - read_start + 1

                local_offset = read_start - c_start
                offset_blocks = local_offset // block_size
                skip_bytes = local_offset % block_size

                chunk_bytes_sent = 0
                bytes_to_skip = skip_bytes

                try:
                    # Stream using message object
                    async for block in tg_client_manager.client.stream_media(chunk["message"], offset=offset_blocks):
                        if bytes_to_skip > 0:
                            if bytes_to_skip < len(block):
                                block = block[bytes_to_skip:]
                                bytes_to_skip = 0
                            else:
                                bytes_to_skip -= len(block)
                                continue

                        if chunk_bytes_sent + len(block) > chunk_read_len:
                            block = block[:chunk_read_len - chunk_bytes_sent]

                        yield block
                        chunk_bytes_sent += len(block)
                        bytes_sent += len(block)

                        if chunk_bytes_sent >= chunk_read_len:
                            break
                except asyncio.CancelledError:
                    logger.info("ZIP streaming cancelled by client")
                    break
                except Exception as e:
                    logger.error(f"Error streaming split ZIP chunk: {e}")
                    break

                if bytes_sent >= stream_len:
                    break

        logger.info(f"Streaming uncompressed ZIP entry '{filename}' (raw bytes {stream_start}-{stream_end}/{total_size}) - Status {status_code}")
        return StreamingResponse(
            split_file_generator(),
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )
    else:
        logger.info(f"ZIP entry '{filename}' is COMPRESSED (type {target_entry.compress_type}). Streaming on-the-fly decompression.")
        reader = TelegramSeekableReader(tg_client_manager.client, messages)
        return StreamingResponse(
            zip_compressed_generator(reader, filename, start, end),
            status_code=status_code,
            media_type=mime_type,
            headers=headers
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("addon:app", host="0.0.0.0", port=Config.PORT, reload=True, timeout_keep_alive=300)
