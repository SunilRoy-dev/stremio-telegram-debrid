"""
SQLite-backed persistent cache + watch history for the Telegram Stremio addon.

- Survives restarts (data/config.json + data/addon_cache.db)
- Dramatically reduces Telegram API calls (anti-ban protection)
- Stores search results, message metadata, ZIP listings and external metadata
- Tracks watch sessions (start / seek / stop) to build a "Continue Watching" feed
"""

import os
import json
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("cache")

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "addon_cache.db")

conn = None
_db_lock = threading.RLock()
DB_PATH = _DEFAULT_DB


class MediaInfo:
    """Lightweight stand-in for pyrogram media objects (duck-typed)."""

    def __init__(self, file_name, file_size, mime_type):
        self.file_name = file_name
        self.file_size = file_size
        self.mime_type = mime_type


class ChatInfo:
    def __init__(self, chat_id):
        self.id = chat_id


class CachedMessage:
    """Lightweight stand-in for pyrogram Message objects restored from cache."""

    def __init__(self, msg_id, chat_id, kind, file_name, file_size, mime_type, caption, date):
        self.id = msg_id
        self.chat = ChatInfo(chat_id)
        media = MediaInfo(file_name, file_size, mime_type) if kind else None
        self.video = media if kind == "video" else None
        self.document = media if kind == "document" else None
        self.audio = media if kind == "audio" else None
        self.caption = caption
        self.date = date

    @property
    def media(self):
        return self.video or self.document or self.audio


def msg_to_dict(msg) -> dict:
    """Serialize a pyrogram Message (or CachedMessage) into a cache-friendly dict."""
    media = getattr(msg, "video", None) or getattr(msg, "document", None) or getattr(msg, "audio", None)
    kind = "video" if getattr(msg, "video", None) else ("document" if getattr(msg, "document", None) else ("audio" if getattr(msg, "audio", None) else None))
    date = getattr(msg, "date", None)
    if isinstance(date, datetime):
        date_epoch = date.timestamp()
    elif isinstance(date, (int, float)):
        date_epoch = float(date)
    else:
        date_epoch = 0.0
    return {
        "chat_id": msg.chat.id,
        "msg_id": msg.id,
        "kind": kind,
        "file_name": getattr(media, "file_name", None) if media else None,
        "file_size": getattr(media, "file_size", 0) if media else 0,
        "mime_type": getattr(media, "mime_type", None) if media else None,
        "caption": getattr(msg, "caption", None) or "",
        "date": date_epoch,
    }


def dict_to_msg(d: dict) -> CachedMessage:
    date = d.get("date") or 0
    try:
        date_dt = datetime.fromtimestamp(float(date), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        date_dt = datetime.fromtimestamp(0, tz=timezone.utc)
    return CachedMessage(
        msg_id=d["msg_id"],
        chat_id=d["chat_id"],
        kind=d.get("kind"),
        file_name=d.get("file_name"),
        file_size=d.get("file_size") or 0,
        mime_type=d.get("mime_type"),
        caption=d.get("caption") or "",
        date=date_dt,
    )


def init(db_path: str = None):
    """Initialize the SQLite database (WAL mode). Safe to call multiple times."""
    global conn, DB_PATH
    if db_path:
        DB_PATH = db_path
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with _db_lock:
        if conn is not None:
            return
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kv_cache (
                ns TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                expires REAL NOT NULL,
                PRIMARY KEY (ns, key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watch_sessions (
                file_key TEXT PRIMARY KEY,
                meta_id TEXT,
                display_name TEXT,
                poster TEXT,
                kind TEXT,
                show_meta_id TEXT,
                show_name TEXT,
                show_poster TEXT,
                total_bytes INTEGER,
                max_pos INTEGER,
                last_pos INTEGER,
                play_count INTEGER,
                started_at REAL,
                last_seen REAL,
                completed INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS show_map (
                chat_id TEXT,
                msg_id INTEGER,
                show_meta_id TEXT,
                show_name TEXT,
                PRIMARY KEY (chat_id, msg_id)
            )
        """)
        # Keep the cache table from growing forever
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kv_expires ON kv_cache (expires)")
        conn.commit()
        logger.info(f"Persistent cache initialized at {DB_PATH}")


def close():
    global conn
    with _db_lock:
        if conn is not None:
            try:
                conn.commit()
                conn.close()
            except Exception:
                pass
            conn = None


# ---------------------------------------------------------------------------
# Generic KV cache
# ---------------------------------------------------------------------------

def cache_get(ns: str, key: str):
    """Return cached value (any JSON-serializable object) or None if missing/expired."""
    if conn is None:
        return None
    try:
        with _db_lock:
            row = conn.execute(
                "SELECT value, expires FROM kv_cache WHERE ns=? AND key=?", (ns, key)
            ).fetchone()
        if not row:
            return None
        value, expires = row
        if expires and time.time() > expires:
            return None
        return json.loads(value)
    except Exception as e:
        logger.debug(f"cache_get failed for {ns}:{key}: {e}")
        return None


def cache_set(ns: str, key: str, value, ttl: int = 1800):
    if conn is None:
        return
    try:
        payload = json.dumps(value, ensure_ascii=False)
        with _db_lock:
            conn.execute(
                "INSERT OR REPLACE INTO kv_cache (ns, key, value, expires) VALUES (?, ?, ?, ?)",
                (ns, key, payload, time.time() + max(60, ttl)),
            )
            # Occasional purge of expired rows (cheap)
            if int(time.time()) % 97 == 0:
                conn.execute("DELETE FROM kv_cache WHERE expires < ?", (time.time(),))
            conn.commit()
    except Exception as e:
        logger.debug(f"cache_set failed for {ns}:{key}: {e}")


def search_cache_get(key: str):
    return cache_get("search", key)


def search_cache_set(key: str, msg_dicts: list, ttl: int):
    cache_set("search", key, msg_dicts, ttl)


def message_cache_get(chat_id, message_id):
    d = cache_get("msg", f"{chat_id}:{message_id}")
    return dict_to_msg(d) if d else None


def message_cache_set(msg) -> None:
    try:
        d = msg_to_dict(msg)
        cache_set("msg", f"{d['chat_id']}:{d['msg_id']}", d, ttl=86400)
    except Exception as e:
        logger.debug(f"message_cache_set failed: {e}")


# ---------------------------------------------------------------------------
# Show map (episode message -> show) for series grouping / continue watching
# ---------------------------------------------------------------------------

def show_map_put(chat_id, msg_ids: list, show_meta_id: str, show_name: str):
    if conn is None or not msg_ids:
        return
    try:
        with _db_lock:
            conn.executemany(
                "INSERT OR REPLACE INTO show_map (chat_id, msg_id, show_meta_id, show_name) VALUES (?, ?, ?, ?)",
                [(str(chat_id), int(m), show_meta_id, show_name) for m in msg_ids],
            )
            conn.commit()
    except Exception as e:
        logger.debug(f"show_map_put failed: {e}")


def show_map_get(chat_id, msg_id):
    if conn is None:
        return None, None
    try:
        with _db_lock:
            row = conn.execute(
                "SELECT show_meta_id, show_name FROM show_map WHERE chat_id=? AND msg_id=?",
                (str(chat_id), int(msg_id)),
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception as e:
        logger.debug(f"show_map_get failed: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Watch history (start / seek / stop events + Continue Watching feed)
# ---------------------------------------------------------------------------

_MIN_START_GAP = 900        # 15 min idle => new play session (increments play count)
_SEEK_THRESHOLD = 4 * 1024 * 1024   # jump > 4 MB counts as a seek event
_STOP_IDLE = 600            # 10 min without activity => session stopped


def record_watch(
    file_key: str,
    meta_id: str,
    display_name: str,
    total_bytes: int,
    position: int,
    kind: str = "movie",
    chat_id=None,
    msg_id=None,
    poster: str = None,
) -> dict:
    """
    Record a streaming position for a file. Returns an event dict when a notable
    event happened ("start", "seek") that should be logged, else {"type": "progress"}.
    """
    if conn is None or not file_key:
        return {"type": None}
    now = time.time()
    position = max(0, int(position or 0))
    event = {"type": "progress"}

    try:
        with _db_lock:
            row = conn.execute(
                "SELECT total_bytes, max_pos, last_pos, play_count, started_at, last_seen, completed, display_name FROM watch_sessions WHERE file_key=?",
                (file_key,),
            ).fetchone()

            if row is None:
                # First ever play (or db was cleared)
                conn.execute(
                    "INSERT INTO watch_sessions (file_key, meta_id, display_name, poster, kind, show_meta_id, show_name, show_poster, total_bytes, max_pos, last_pos, play_count, started_at, last_seen, completed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (file_key, meta_id, display_name, poster, kind, None, None, None,
                     total_bytes, position, position, 1, now, now, 0),
                )
                event = {"type": "start", "play_count": 1, "position": position}
            else:
                (db_total, max_pos, last_pos, play_count, started_at, last_seen, completed, db_name) = row
                new_session = (now - (last_seen or 0)) > _MIN_START_GAP
                if new_session:
                    play_count = (play_count or 0) + 1
                    started_at = now
                    max_pos = position
                    completed = 0
                    event = {"type": "start", "play_count": play_count, "position": position}
                elif position > (last_pos or 0) + _SEEK_THRESHOLD or (last_pos or 0) - position > _SEEK_THRESHOLD:
                    event = {"type": "seek", "play_count": play_count or 1,
                             "position": position, "from_position": last_pos or 0}

                max_pos = max(max_pos or 0, position)
                name = display_name or db_name
                conn.execute(
                    "UPDATE watch_sessions SET meta_id=?, display_name=?, poster=?, kind=?, total_bytes=?, max_pos=?, last_pos=?, play_count=?, started_at=?, last_seen=?, completed=0 WHERE file_key=?",
                    (meta_id, name, poster, kind, total_bytes or db_total, max_pos, position,
                     play_count or 1, started_at or now, now, file_key),
                )
            conn.commit()

        # Attach show info for series (best-effort, outside lock)
        if kind == "series" and chat_id is not None and msg_id is not None:
            show_meta_id, show_name = show_map_get(chat_id, msg_id)
            if show_meta_id:
                with _db_lock:
                    conn.execute(
                        "UPDATE watch_sessions SET show_meta_id=?, show_name=?, kind='series' WHERE file_key=?",
                        (show_meta_id, show_name, file_key),
                    )
                    conn.commit()
    except Exception as e:
        logger.debug(f"record_watch failed: {e}")

    return event


def finalize_stale_sessions(idle_seconds: int = _STOP_IDLE) -> list:
    """
    Mark sessions idle for > idle_seconds as stopped.
    Returns list of dicts for sessions that transitioned to 'stopped' (for TG logs).
    """
    if conn is None:
        return []
    now = time.time()
    finalized = []
    try:
        with _db_lock:
            rows = conn.execute(
                "SELECT file_key, display_name, show_name, total_bytes, max_pos, play_count, last_seen, completed FROM watch_sessions WHERE completed=0 AND last_seen < ?",
                (now - idle_seconds,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE watch_sessions SET completed=1 WHERE completed=0 AND last_seen < ?",
                    (now - idle_seconds,),
                )
                conn.commit()
        for r in rows:
            total = r[3] or 0
            max_pos = r[4] or 0
            progress = (max_pos / total * 100) if total else 0
            finalized.append({
                "file_key": r[0],
                "display_name": r[1],
                "show_name": r[2],
                "progress": round(progress, 1),
                "play_count": r[5] or 1,
                "last_seen": r[6],
                "finished": progress >= 92,
            })
    except Exception as e:
        logger.debug(f"finalize_stale_sessions failed: {e}")
    return finalized


def get_continue_watching(kind: str = "movie", limit: int = 20) -> list:
    """Sessions still in progress (between ~1% and ~95%), most recent first."""
    if conn is None:
        return []
    try:
        with _db_lock:
            rows = conn.execute(
                "SELECT meta_id, display_name, poster, show_meta_id, show_name, show_poster, total_bytes, max_pos, play_count, last_seen, kind FROM watch_sessions WHERE completed=0 AND kind=? ORDER BY last_seen DESC LIMIT ?",
                (kind, limit * 2),
            ).fetchall()
        items = []
        for r in rows:
            total = r[6] or 0
            max_pos = r[7] or 0
            pct = (max_pos / total * 100) if total else 0
            if pct < 0.5 or pct > 95:
                continue
            items.append({
                "meta_id": r[0],
                "display_name": r[1],
                "poster": r[2],
                "show_meta_id": r[3],
                "show_name": r[4],
                "show_poster": r[5],
                "progress_pct": round(pct, 1),
                "play_count": r[8] or 1,
                "last_seen": r[9],
                "kind": r[10],
            })
            if len(items) >= limit:
                break
        return items
    except Exception as e:
        logger.debug(f"get_continue_watching failed: {e}")
        return []


def get_stats() -> dict:
    """Aggregate stats for the wizard dashboard."""
    if conn is None:
        return {}
    try:
        with _db_lock:
            n_cache = conn.execute("SELECT COUNT(*) FROM kv_cache").fetchone()[0]
            n_sessions = conn.execute("SELECT COUNT(*) FROM watch_sessions").fetchone()[0]
            n_shows = conn.execute("SELECT COUNT(DISTINCT show_meta_id) FROM show_map").fetchone()[0]
        return {"cached_entries": n_cache, "watch_sessions": n_sessions, "tracked_shows": n_shows, "db_path": DB_PATH}
    except Exception as e:
        logger.debug(f"get_stats failed: {e}")
        return {}


def format_player_position(position: int) -> str:
    if not position or position <= 0:
        return "0m"
    # Rough estimate: assume ~5 Mbps average bitrate for display purposes
    seconds = position / (5_000_000 / 8)
    m = int(seconds // 60) % 60
    h = int(seconds // 3600)
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def friendly_ts(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "unknown"
