"""
Persistent configuration store for the Telegram Stremio addon.

- Environment variables remain the base layer (12-factor friendly)
- data/config.json (saved via the /configure wizard) overrides env values
- The wizard is locked behind CONFIG_PASSWORD; first-run claim is allowed only
  when no password exists and no config has been saved yet.
"""

import os
import json
import base64
import hashlib
import hmac
import logging
import tempfile

logger = logging.getLogger("config_store")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# Keys that may be stored/edited via the wizard
WIZARD_KEYS = [
    "API_ID", "API_HASH", "BOT_TOKEN", "USER_SESSION_STRING",
    "TELEGRAM_CHANNEL_ID", "LOG_CHANNEL_ID",
    "API_KEY", "ADDON_URL", "TIMEZONE",
    "TMDB_API_KEY", "TMDB_LANGUAGE",
    "CACHE_TTL",
    "RATE_LIMIT_ENABLED", "RATE_LIMIT_REQUESTS", "RATE_LIMIT_WINDOW", "RATE_LIMIT_STREAM_REQUESTS",
    "WATCH_LOG_EVENTS", "CONFIG_PASSWORD",
]

_INT_KEYS = {"API_ID", "CACHE_TTL", "RATE_LIMIT_REQUESTS", "RATE_LIMIT_WINDOW", "RATE_LIMIT_STREAM_REQUESTS"}
_BOOL_KEYS = {"RATE_LIMIT_ENABLED", "WATCH_LOG_EVENTS"}
_SECRET_KEYS = {"API_HASH", "BOT_TOKEN", "USER_SESSION_STRING", "API_KEY", "TMDB_API_KEY", "CONFIG_PASSWORD"}


def _hash_password(password: str, salt: bytes = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, _ = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        candidate = _hash_password(password, salt)
        return hmac.compare_digest(candidate, stored)
    except Exception:
        return False


def load_file_config() -> dict:
    """Load the saved wizard config (empty dict if none)."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error(f"Failed to load config file: {e}")
    return {}


def sanitize_file_config(data: dict) -> dict:
    """Keep only known keys, coerce types. Password is stored hashed."""
    clean = {}
    for k in WIZARD_KEYS:
        if k not in data:
            continue
        v = data[k]
        if v is None:
            continue
        if k in _INT_KEYS:
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        elif k in _BOOL_KEYS:
            v = bool(v) if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        else:
            v = str(v).strip()
            if v == "":
                continue
        if k == "CONFIG_PASSWORD":
            if "$" not in str(v):  # store hashed, never plaintext
                v = _hash_password(str(v))
        clean[k] = v
    return clean


def save_file_config(data: dict) -> bool:
    """Persist sanitized config atomically. Returns True on success."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        clean = sanitize_file_config(data)
        fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_FILE)
        return True
    except Exception as e:
        logger.error(f"Failed to save config file: {e}")
        return False


def verify_wizard_password(password: str) -> bool:
    """Check a password against env CONFIG_PASSWORD or the stored hash."""
    if not password:
        return False
    file_cfg = load_file_config()
    stored = file_cfg.get("CONFIG_PASSWORD")
    if stored:
        return _verify_password(password, stored)
    env_pass = os.getenv("CONFIG_PASSWORD", "").strip()
    if env_pass:
        return hmac.compare_digest(password, env_pass)
    return False


def is_first_run() -> bool:
    """Wizard is openly claimable only when no password exists anywhere and nothing saved yet."""
    file_cfg = load_file_config()
    if file_cfg:
        return False
    return not os.getenv("CONFIG_PASSWORD", "").strip()


def wizard_locked() -> bool:
    return not is_first_run()


def mask_value(key: str, value) -> str:
    """Mask secrets for display in the wizard UI."""
    if value is None or value == "":
        return ""
    s = str(value)
    if key in _SECRET_KEYS:
        if len(s) <= 6:
            return "••••••"
        return s[:3] + "••••••••••" + s[-3:]
    return s


def validate_config_dict(data: dict) -> list:
    """Validate a candidate config dict (pre-save). Returns list of error strings."""
    errors = []
    merged = dict(data)
    api_id = merged.get("API_ID")
    try:
        api_id_int = int(api_id)
        if api_id_int <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("API_ID must be a positive integer.")

    if not merged.get("API_HASH"):
        errors.append("API_HASH is required (get it from my.telegram.org).")

    if not merged.get("BOT_TOKEN") and not merged.get("USER_SESSION_STRING"):
        errors.append("Either BOT_TOKEN or USER_SESSION_STRING is required.")

    if not merged.get("TELEGRAM_CHANNEL_ID"):
        errors.append("TELEGRAM_CHANNEL_ID is required (comma separated channel ids).")

    if merged.get("ADDON_URL"):
        url = str(merged["ADDON_URL"]).strip()
        if not url.startswith(("http://", "https://")):
            errors.append("ADDON_URL must start with http:// or https://")

    if merged.get("BOT_TOKEN") and not str(merged["BOT_TOKEN"]).count(":") == 1:
        errors.append("BOT_TOKEN format looks invalid (expected '123456:ABC-DEF...').")

    return errors
