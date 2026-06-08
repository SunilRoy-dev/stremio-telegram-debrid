import re
import logging
import httpx

logger = logging.getLogger("utils")

EPISODE_PATTERNS = [
    re.compile(r'\bs(?P<season>\d+)\s*[._\- ]?\s*e(?P<episode>\d+)\b', re.IGNORECASE),
    re.compile(r'\b(?P<season>\d+)\s*[._\- ]?\s*x\s*[._\- ]?\s*(?P<episode>\d+)\b', re.IGNORECASE),
    re.compile(r'\bseason\s*[._\- ]?\s*(?P<season>\d+)\s*[._\- ]?\s*episode\s*[._\- ]?\s*(?P<episode>\d+)\b', re.IGNORECASE),
    re.compile(r'\b(?:ep|episode)\s*[._\- ]?\s*(?P<episode>\d+)\b', re.IGNORECASE),
    re.compile(r'\be(?P<episode>\d+)\b', re.IGNORECASE),
    re.compile(r'\b(?P<season>\d{1,2})(?P<episode>\d{2})\b')
]

def format_size(bytes_size: int) -> str:
    if not bytes_size:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while bytes_size >= 1024 and i < len(units) - 1:
        bytes_size /= 1024.0
        i += 1
    return f"{bytes_size:.2f} {units[i]}"

def parse_season_episode(filename: str) -> tuple:
    if not filename:
        return None, None
        
    clean_name = filename.lower()
    clean_name = re.sub(
        r'\b(2160p|1080p|720p|480p|360p|4k|8k|10bit|h264|x264|h265|x265|hevc|dd5\.1|aac2\.0|xvid|divx|dts)\b',
        ' ',
        clean_name
    )
    
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(clean_name)
        if match:
            try:
                gd = match.groupdict()
                episode = int(gd["episode"])
                season = int(gd.get("season", 1))
                
                # Exclude release years (e.g. 1999) from 3-4 digit matches
                if pattern == EPISODE_PATTERNS[-1]:
                    full_val = int(match.group(0))
                    if 1900 <= full_val <= 2030:
                        continue
                        
                return season, episode
            except (ValueError, KeyError):
                pass
    return None, None

def matches_episode(filename: str, season: int, episode: int) -> bool:
    if season is None or episode is None:
        return True
        
    f_season, f_episode = parse_season_episode(filename)
    if f_season == season and f_episode == episode:
        return True
        
    clean_fn = filename.lower()
    s_pad = f"s{season:02d}"
    e_pad = f"e{episode:02d}"
    
    patterns = [
        f"{s_pad}{e_pad}",
        f"s{season}e{episode}",
        f"{season}x{episode:02d}",
        f"{season}x{episode}",
        f"s{s_pad} e{e_pad}",
        f"s{season} e{episode}",
        f"season {season} episode {episode}"
    ]
    
    for pattern in patterns:
        if pattern in clean_fn:
            return True
            
    s_regex = re.compile(rf"\bs(eason)?\s*0*{season}\b", re.IGNORECASE)
    e_regex = re.compile(rf"\be(pisode)?\s*0*{episode}\b", re.IGNORECASE)
    if s_regex.search(clean_fn) and e_regex.search(clean_fn):
        return True
        
    return False

def matches_subtitle(video_filename: str, sub_filename: str) -> bool:
    if not video_filename or not sub_filename:
        return False
        
    v_fn = video_filename.lower()
    s_fn = sub_filename.lower()
    
    v_base = v_fn.rsplit('.', 1)[0]
    s_base = s_fn.rsplit('.', 1)[0]
    
    # Strip language tags
    s_base_clean = re.sub(r'\.(eng|en|english|sub|subtitle|srt|vtt)$', '', s_base)
    
    if s_base_clean in v_base or v_base in s_base_clean:
        return True
        
    return False

def get_search_query_from_filename(filename: str) -> str:
    if not filename:
        return ""
    name = filename.lower()
    name = name.rsplit('.', 1)[0]
    name = re.sub(r'[._\-]', ' ', name)
    
    terms = r'\b(2160p|1080p|720p|480p|360p|4k|8k|10bit|h264|x264|h265|x265|hevc|web[- ]?rip|bluray|brrip|hdrip)\b'
    match = re.search(terms, name)
    if match:
        name = name[:match.start()]
    
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def parse_split_info(filename: str) -> tuple:
    if not filename:
        return None, None
        
    # Match suffix .001, .002 etc.
    m1 = re.search(r'\.(\d{3,4})$', filename)
    if m1:
        part = int(m1.group(1))
        base = filename[:m1.start()]
        return base, part
        
    # Match part1, part01, part_1 etc.
    m2 = re.search(r'[._\- ]part_?(\d+)(?:\.([^.]+))?$', filename, re.IGNORECASE)
    if m2:
        part = int(m2.group(1))
        ext = m2.group(2) or ""
        base = filename[:m2.start()]
        if ext:
            base += f".{ext}"
        return base, part
        
    return None, None

_metadata_cache = {}

async def get_metadata_from_cinemeta(meta_type: str, imdb_id: str) -> dict:
    cache_key = f"{meta_type}:{imdb_id}"
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]

    url = f"https://v3-cinemeta.strem.io/meta/{meta_type}/{imdb_id}.json"
    logger.info(f"Fetching metadata from Cinemeta: {url}")
    
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                meta = data.get("meta", {})
                if meta:
                    result = {
                        "name": meta.get("name"),
                        "year": meta.get("year"),
                        "genres": meta.get("genres", []),
                        "poster": meta.get("poster")
                    }
                    _metadata_cache[cache_key] = result
                    return result
    except Exception as e:
        logger.error(f"Cinemeta metadata lookup failed: {e}")
        
    return {}

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m4v')

def is_video_file(filename: str) -> bool:
    return filename.lower().endswith(VIDEO_EXTENSIONS)

