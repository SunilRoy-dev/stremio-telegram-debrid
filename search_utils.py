import re
import unicodedata
from typing import List, Optional, Tuple

# Quality constants
RES_4K = "4K"
RES_1080P = "1080p"
RES_720P = "720p"
RES_480P = "480p"
RES_360P = "360p"
RES_CAM = "CAM"
RES_SCR = "SCR"
RES_UNKNOWN = "Unknown"

# Regexes for matching
RE_YEAR = re.compile(r'\b(19\d{2}|20\d{2})\b')
RE_EPISODE_EXPLICIT = re.compile(
    r'\b[sS]\s*(\d{1,2})\s*[eE]\s*(\d{1,4})\b'
    r'|\b[tT]emp(?:orada)?\s*(\d{1,2})\s*[cC]ap(?:itulo)?\s*(\d{1,4})\b'
    r'|(?<!\d)(\d{1,2})\s*[xX]\s*(\d{1,4})(?!\d)',
    re.IGNORECASE
)
RE_EPISODE_ONLY = re.compile(
    r'\b[eE]p(?:isode)?\s*(\d{1,4})\b'
    r'|\b[cC]ap(?:itulo)?\s*(\d{1,4})\b',
    re.IGNORECASE
)
RE_CLEANUP = re.compile(r'[._\-\[\]()\'",!?:]')
RE_SPACES = re.compile(r'\s+')
RE_EXTENSION = re.compile(r'\.(mkv|mp4|avi|mov|wmv|m4v|ts|m2ts)$', re.IGNORECASE)

_RES_SCORES = {
    RES_4K: 60,
    RES_1080P: 50,
    RES_720P: 40,
    RES_480P: 30,
    RES_360P: 20,
    RES_CAM: -10,
    RES_SCR: -10,
}

def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = RE_EXTENSION.sub("", text)
    text = RE_CLEANUP.sub(" ", text)
    decomposed = unicodedata.normalize('NFKD', text)
    cleaned = "".join(c for c in decomposed if unicodedata.category(c) != 'Mn')
    return RE_SPACES.sub(" ", cleaned).strip().lower()

def extract_season_episode_pair(text: str) -> Optional[Tuple[int, int]]:
    match = RE_EPISODE_EXPLICIT.search(text) or RE_EPISODE_EXPLICIT.search(normalize_text(text))
    
    if match:
        g = match.groups()
        s, e = g[0] or g[2] or g[4], g[1] or g[3] or g[5]
        if s is not None and e is not None:
            try:
                return int(s), int(e)
            except ValueError:
                pass
    return None

def extract_standalone_episode(text: str) -> Optional[int]:
    match = RE_EPISODE_ONLY.search(text) or RE_EPISODE_ONLY.search(normalize_text(text))
        
    if match:
        g = match.groups()
        e = g[0] or g[1]
        if e is not None:
            try:
                return int(e)
            except ValueError:
                pass
    return None

def parse_video_resolution(raw_name: str) -> str:
    tag = raw_name.lower().replace(' ', '.')
    
    if any(q in tag for q in ["2160", "216o", "4k", "uhd", "ultrahd"]):
        return RES_4K
    if any(q in tag for q in ["1080", "1o8o", "108o", "1o80", "fhd"]):
        return RES_1080P
    if any(q in tag for q in ["720", "72o", "hd"]):
        return RES_720P
    if any(q in tag for q in ["480", "48o", "sd"]):
        return RES_480P
    if any(q in tag for q in ["360", "36o"]):
        return RES_360P
    if any(q in tag for q in ["camrip", "hdcam", "hdts", "telesync", "cam"]):
        return RES_CAM
    if any(q in tag for q in ["dvdscr", "screener", "scr"]):
        return RES_SCR
        
    return RES_UNKNOWN

def get_resolution_score(res: str) -> int:
    return _RES_SCORES.get(res, 0)

class VideoMatcher:
    def __init__(self, score_threshold: int = 55):
        self.score_threshold = score_threshold

    def calculate_match_score(
        self,
        filename: str,
        caption: str,
        title: str,
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None
    ) -> int:
        combined = f"{filename or ''} {caption or ''}"
        norm_combined = normalize_text(combined)
        norm_title = normalize_text(title)
        
        has_title = norm_title in norm_combined
        if not has_title:
            keywords = norm_title.split()
            words_in_combined = norm_combined.split()
            if keywords and all(kw in words_in_combined for kw in keywords):
                has_title = True
                
        if not has_title:
            return 0
            
        score = 60
        
        if year is not None:
            found_years = [int(y) for y in RE_YEAR.findall(combined)]
            if year in found_years:
                score += 20
            elif any(abs(y - year) == 1 for y in found_years):
                score += 5
            elif not found_years:
                score += 5
            else:
                score -= 10
                
        if season is not None and episode is not None:
            se_file = extract_season_episode_pair(filename)
            se_cap = extract_season_episode_pair(caption)
            
            matched_se = (se_file and se_file[0] == season and se_file[1] == episode) or \
                         (se_cap and se_cap[0] == season and se_cap[1] == episode)
                         
            if matched_se:
                score += 20
            elif se_file or se_cap:
                return 0
            elif season == 1:
                ep_file = extract_standalone_episode(filename)
                ep_cap = extract_standalone_episode(caption)
                if ep_file == episode or ep_cap == episode:
                    score += 20
                elif ep_file is not None or ep_cap is not None:
                    return 0
                else:
                    score -= 10
            else:
                score -= 10
        elif season is None:
            if RE_EPISODE_EXPLICIT.search(combined) or RE_EPISODE_EXPLICIT.search(norm_combined):
                score -= 20
                
        return max(0, min(100, score))

    def make_movie_search_queries(self, title: str, year: Optional[int] = None) -> List[str]:
        clean_name = title.replace(":", "").replace("-", " ").replace("  ", " ").strip()
        queries = []
        if year is not None:
            queries.append(f"{clean_name} {year}")
        queries.append(clean_name)
        
        deduped = []
        for q in queries:
            lowered = q.lower()
            if lowered not in deduped:
                deduped.append(lowered)
        return deduped

    def make_series_search_queries(self, title: str, season: int, episode: int) -> List[str]:
        clean_name = title.replace(":", "").replace("-", " ").replace("  ", " ").strip()
        
        s = str(season)
        e = str(episode)
        s_padded = s.zfill(2)
        e_padded = e.zfill(2)
        
        variations = [
            clean_name,
            f"{clean_name} s{s_padded}e{e_padded}",
            f"{clean_name} {s}x{e_padded}",
            f"{clean_name} {s_padded}x{e_padded}",
            f"{clean_name} s{s}e{e}",
            f"{clean_name} {s}x{e}",
            f"{clean_name} s{s} e{e}",
            f"{clean_name} s{s_padded} e{e_padded}"
        ]
        
        deduped = []
        for v in variations:
            lowered = v.lower()
            if lowered not in deduped:
                deduped.append(lowered)
        return deduped
