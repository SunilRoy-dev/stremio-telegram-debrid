import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import config_store


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    PORT = int(os.getenv("PORT", 7860))
    ADDON_URL = os.getenv("ADDON_URL", f"http://localhost:{PORT}").rstrip("/")
    API_KEY = os.getenv("API_KEY", "")
    CACHE_TTL = int(os.getenv("CACHE_TTL", 1800))
    TIMEZONE = os.getenv("TIMEZONE", "UTC")

    API_ID = os.getenv("API_ID")
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "")

    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
    LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")

    # --- New: metadata / localization ---
    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
    TMDB_LANGUAGE = os.getenv("TMDB_LANGUAGE", "en-US")

    # --- New: rate limiting (disabled by default; owner can enable) ---
    RATE_LIMIT_ENABLED = _env_bool("RATE_LIMIT_ENABLED", "false")
    RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", 120))      # JSON API reqs / window / IP
    RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", 60))           # seconds
    RATE_LIMIT_STREAM_REQUESTS = int(os.getenv("RATE_LIMIT_STREAM_REQUESTS", 600))  # media range reqs / window / IP

    # --- New: watch history / play logs ---
    WATCH_LOG_EVENTS = _env_bool("WATCH_LOG_EVENTS", "true")              # log start/seek/stop events

    # --- New: wizard password (plaintext via env; hashed in config file) ---
    CONFIG_PASSWORD = os.getenv("CONFIG_PASSWORD", "")

    @classmethod
    def apply_file_overrides(cls):
        """Overlay values saved through the /configure wizard (file > env)."""
        saved = config_store.load_file_config()
        for key, value in saved.items():
            if key == "CONFIG_PASSWORD":
                # env password still works alongside the stored hash
                if value:
                    cls.CONFIG_PASSWORD = value
                continue
            if hasattr(cls, key):
                setattr(cls, key, value)
        cls.normalize()

    @classmethod
    def normalize(cls):
        """Normalize channel ids / ints after any config change."""
        try:
            cls.API_ID = int(cls.API_ID)
        except (ValueError, TypeError):
            pass

        for attr in ("TELEGRAM_CHANNEL_ID", "LOG_CHANNEL_ID"):
            val = getattr(cls, attr, None)
            if val and isinstance(val, str):
                stripped = val.strip()
                if stripped.startswith("-") or stripped.isdigit():
                    try:
                        setattr(cls, attr, int(stripped))
                    except ValueError:
                        pass

        cls.RATE_LIMIT_ENABLED = bool(cls.RATE_LIMIT_ENABLED)
        cls.WATCH_LOG_EVENTS = bool(cls.WATCH_LOG_EVENTS)

    @classmethod
    def validate(cls):
        missing = []
        if not cls.API_ID:
            missing.append("API_ID")
        if not cls.API_HASH:
            missing.append("API_HASH")
        if not cls.BOT_TOKEN and not cls.USER_SESSION_STRING:
            missing.append("BOT_TOKEN or USER_SESSION_STRING")
        if not cls.TELEGRAM_CHANNEL_ID:
            missing.append("TELEGRAM_CHANNEL_ID")

        if missing:
            raise ValueError(
                f"Missing critical configuration variables: {', '.join(missing)}. "
                "Run the configuration wizard at /configure or set them in your environment."
            )

        try:
            cls.API_ID = int(cls.API_ID)
        except (ValueError, TypeError):
            raise ValueError("API_ID must be a valid integer.")

        cls.normalize()
