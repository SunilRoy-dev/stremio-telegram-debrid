"""
Metadata enrichment for the Telegram Stremio addon.

- If TMDB_API_KEY is set: uses TMDB with `language=` localization for posters,
  titles, descriptions and genres.
- Otherwise: falls back to Stremio's Cinemeta (no key required, English only).
- All lookups are cached in the persistent SQLite cache (7 days TTL) so Telegram
  and metadata APIs are not hammered on repeated catalog loads.
"""

import re
import asyncio
import logging
from urllib.parse import quote

import httpx

import cache
from config import Config
from utils import _clean_title_prefix

logger = logging.getLogger("metadata")

META_TTL = 7 * 24 * 3600          # 7 days
GENRE_LIST_TTL = 7 * 24 * 3600
_ENRICH_SEMAPHORE = asyncio.Semaphore(8)
_http_client: httpx.AsyncClient = None


# Standard TMDB genre ids -> English names (fallback when the localized list fails)
TMDB_GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics",
}

# Genre options exposed in the manifest (drives Stremio home genre rows)
GENRE_OPTIONS = [
    "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "History", "Horror", "Music", "Mystery",
    "Romance", "Science Fiction", "Thriller", "War", "Western",
]

_YEAR_RE = re.compile(r"\b(19\d{2}|20[0-3]\d)\b")


async def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=8.0, follow_redirects=True)
    return _http_client


async def close_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def derive_title_year(filename: str) -> tuple:
    """Extract a clean search title + optional year from a media filename."""
    if not filename:
        return "", None
    year = None
    m = _YEAR_RE.search(filename)
    if m:
        try:
            year = int(m.group(1))
        except ValueError:
            year = None
    title = _clean_title_prefix(filename)
    title = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title)          # drop (1080p) [x264] groups
    title = re.sub(r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|10bit|8bit|x264|x265|h\.?264|h\.?265|hevc|aac|aac5\.1|dts|ddp?5\.1|dd\+|atmos|web[- ]?dl|web[- ]?rip|brrip|bluray|hdrip|bdrip|dvdrip|hdts|camrip|remux|10bit|dual[- ]?audio|esub?s?|m-sub?s?|hd)\b",
                   " ", title, flags=re.IGNORECASE)
    title = title.replace(".", " ").replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip(" -–—.,([{)]}")
    return title, year


def cache_key_for(title: str, mtype: str, year, language: str) -> str:
    y = year or ""
    return f"{mtype}:{language}:{y}:{(title or '').strip().lower()}"


async def _tmdb_genres(language: str) -> dict:
    """Localized genre id -> name map (cached 7 days)."""
    ns_key = f"tmdbgenres:{language}"
    cached = cache.cache_get("meta", ns_key)
    if cached:
        return cached
    try:
        client = await _get_client()
        url = f"https://api.themoviedb.org/3/genre/movie/list?api_key={Config.TMDB_API_KEY}&language={quote(language)}"
        resp = await client.get(url)
        if resp.status_code == 200:
            mapping = {g["id"]: g["name"] for g in resp.json().get("genres", [])}
            tv_url = f"https://api.themoviedb.org/3/genre/tv/list?api_key={Config.TMDB_API_KEY}&language={quote(language)}"
            tv_resp = await client.get(tv_url)
            if tv_resp.status_code == 200:
                mapping.update({g["id"]: g["name"] for g in tv_resp.json().get("genres", [])})
            if mapping:
                cache.cache_set("meta", ns_key, mapping, GENRE_LIST_TTL)
                return mapping
    except Exception as e:
        logger.debug(f"TMDB genre list failed: {e}")
    return dict(TMDB_GENRE_MAP)


async def _enrich_tmdb(title: str, mtype: str, year, language: str) -> dict:
    client = await _get_client()
    endpoint = "tv" if mtype == "series" else "movie"
    url = (
        f"https://api.themoviedb.org/3/search/{endpoint}"
        f"?api_key={Config.TMDB_API_KEY}&query={quote(title)}&language={quote(language)}&include_adult=false&page=1"
    )
    if year and mtype != "series":
        url += f"&year={year}"
    resp = await client.get(url)
    if resp.status_code != 200:
        return {}
    results = resp.json().get("results") or []
    if not results:
        return {}

    # Pick best match: prefer title/keyword agreement, then popularity
    best = results[0]
    if len(results) > 1:
        tnorm = re.sub(r"[^a-z0-9]", "", title.lower())
        for r in results:
            rname = r.get("title") or r.get("name") or ""
            if re.sub(r"[^a-z0-9]", "", rname.lower()) == tnorm:
                best = r
                break

    genre_map = await _tmdb_genres(language)
    date = best.get("release_date") or best.get("first_air_date") or ""
    out_year = int(date[:4]) if date[:4].isdigit() else year

    result = {
        "name": best.get("title") or best.get("name") or title,
        "poster": f"https://image.tmdb.org/t/p/w500{best['poster_path']}" if best.get("poster_path") else None,
        "background": f"https://image.tmdb.org/t/p/w1280{best['backdrop_path']}" if best.get("backdrop_path") else None,
        "description": best.get("overview") or "",
        "genres": [genre_map.get(gid, TMDB_GENRE_MAP.get(gid, "")) for gid in best.get("genre_ids", [])],
        "year": out_year,
        "imdb_id": None,
        "source": "tmdb",
    }
    result["genres"] = [g for g in result["genres"] if g]

    # Try to attach the IMDb id (best-effort; needs external_ids which returns English ids)
    tmdb_id = best.get("id")
    if tmdb_id:
        try:
            ext_url = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}/external_ids?api_key={Config.TMDB_API_KEY}"
            ext = await client.get(ext_url)
            if ext.status_code == 200:
                result["imdb_id"] = ext.json().get("imdb_id")
        except Exception:
            pass
    return result


async def _enrich_cinemeta_search(title: str, mtype: str, year) -> dict:
    client = await _get_client()
    url = f"https://v3-cinemeta.strem.io/catalog/{mtype}/top/search/{quote(title)}.json"
    resp = await client.get(url)
    if resp.status_code != 200:
        return {}
    metas = resp.json().get("metas") or []
    if not metas:
        return {}

    tnorm = re.sub(r"[^a-z0-9]", "", title.lower())
    best = metas[0]
    for m in metas:
        mname = m.get("name") or ""
        if re.sub(r"[^a-z0-9]", "", mname.lower()) == tnorm:
            best = m
            break

    release = str(best.get("releaseInfo", ""))
    m_year = None
    ym = _YEAR_RE.search(release)
    if ym:
        m_year = int(ym.group(1))
    if year and m_year and abs(m_year - year) > 2:
        # wrong title match; try next candidates
        for m in metas:
            ym2 = _YEAR_RE.search(str(m.get("releaseInfo", "")))
            if ym2 and abs(int(ym2.group(1)) - year) <= 1:
                best = m
                m_year = int(ym2.group(1))
                break

    genres = best.get("genres") or []
    if not genres and best.get("id"):
        try:
            detail_url = f"https://v3-cinemeta.strem.io/meta/{mtype}/{best['id']}.json"
            dresp = await client.get(detail_url)
            if dresp.status_code == 200:
                dmeta = dresp.json().get("meta", {})
                genres = dmeta.get("genres") or []
                if dmeta.get("description") and not best.get("description"):
                    best["description"] = dmeta.get("description")
        except Exception:
            pass

    return {
        "name": best.get("name") or title,
        "poster": best.get("poster"),
        "background": best.get("background") or best.get("poster"),
        "description": best.get("description") or "",
        "genres": genres,
        "year": m_year or year,
        "imdb_id": best.get("id"),
        "source": "cinemeta",
    }


async def enrich(title: str, mtype: str = "movie", year=None, language: str = None) -> dict:
    """
    Look up rich metadata (poster / description / genres / year) for a title.
    Cached in SQLite for 7 days. Returns {} when nothing found.
    """
    title = (title or "").strip()
    if not title or len(title) < 2:
        return {}
    language = language or getattr(Config, "TMDB_LANGUAGE", "en-US") or "en-US"
    key = cache_key_for(title, mtype, year, language)

    cached = cache.cache_get("meta", key)
    if cached is not None:
        return cached if cached else {}

    async with _ENRICH_SEMAPHORE:
        result = {}
        try:
            if Config.TMDB_API_KEY:
                result = await _enrich_tmdb(title, mtype, year, language)
                if not result:
                    result = await _enrich_cinemeta_search(title, mtype, year)
            else:
                result = await _enrich_cinemeta_search(title, mtype, year)
        except Exception as e:
            logger.debug(f"enrich('{title}') failed: {e}")
            result = {}

    # Positive results cached for 7 days; negative results only 1 hour so
    # temporary network failures are retried soon.
    cache.cache_set("meta", key, result, META_TTL if result else 3600)
    return result


async def enrich_batch(titles: list, mtype: str = "movie", language: str = None) -> dict:
    """Enrich multiple titles concurrently. Returns {title_lower: info}."""
    tasks = {t: asyncio.create_task(enrich(t, mtype, None, language)) for t in titles if t}
    out = {}
    for t, task in tasks.items():
        try:
            out[t.lower()] = await task
        except Exception:
            out[t.lower()] = {}
    return out
