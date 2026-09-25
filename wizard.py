"""
Web configuration wizard (/configure) for the Telegram Stremio addon.

- Lets the instance owner set up the addon from a browser instead of env vars.
- Locked behind a password (CONFIG_PASSWORD env or set on first run).
- First-run claim: if no password exists anywhere and no config was saved yet,
  the first visitor can claim the wizard (standard self-hosted app behaviour).
- On save, the new config is validated, persisted to data/config.json and the
  Telegram client is restarted with the new credentials.
"""

import os
import secrets
import asyncio
import logging
import urllib.parse

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

import cache
import config_store
from config import Config

logger = logging.getLogger("wizard")

router = APIRouter()

WIZARD_COOKIE = "tg_wizard_session"
_sessions: set = set()

LANGUAGES = [
    ("en-US", "English (US)"), ("en-GB", "English (UK)"), ("hi-IN", "Hindi"), ("es-ES", "Spanish"),
    ("fr-FR", "French"), ("de-DE", "German"), ("it-IT", "Italian"), ("pt-BR", "Portuguese (Brazil)"),
    ("ru-RU", "Russian"), ("ar-SA", "Arabic"), ("tr-TR", "Turkish"), ("id-ID", "Indonesian"),
    ("ja-JP", "Japanese"), ("ko-KR", "Korean"), ("zh-CN", "Chinese (Simplified)"),
    ("ta-IN", "Tamil"), ("te-IN", "Telugu"), ("ml-IN", "Malayalam"), ("kn-IN", "Kannada"),
    ("bn-IN", "Bengali"), ("mr-IN", "Marathi"), ("pa-IN", "Punjabi"), ("gu-IN", "Gujarati"),
]


def _authorized(request: Request) -> bool:
    # Access is granted ONLY via a session cookie obtained by unlocking with the
    # password (or claiming it on first run). This prevents strangers from
    # using or changing the configuration on public deployments.
    token = request.cookies.get(WIZARD_COOKIE, "")
    return token in _sessions and token != ""


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host or "?"
    return "?"


async def _current_values() -> dict:
    saved = config_store.load_file_config()
    values = {}
    for key in config_store.WIZARD_KEYS:
        env_val = getattr(Config, key, "") if hasattr(Config, key) else os.getenv(key, "")
        if key in saved and saved[key] not in (None, ""):
            val = saved[key]
            if key == "CONFIG_PASSWORD":
                val = ""  # never show the hash; empty means "unchanged"
        else:
            val = env_val or ""
        if key == "CONFIG_PASSWORD":
            val = ""
        if isinstance(val, bool):
            val = "true" if val else "false"
        values[key] = "" if val is None else str(val)
    return values


def _field(key: str, label: str, values: dict, placeholder: str = "", secret: bool = False,
          hint: str = "", input_id: str = None, input_type: str = "text") -> str:
    v = values.get(key, "")
    shown = config_store.mask_value(key, v) if secret else v
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
    return f"""
    <div class="field" data-key="{key}">
        <label for="f_{input_id or key}">{label}</label>
        <input id="f_{input_id or key}" data-key="{key}" type="{input_type}" value="{shown}" placeholder="{placeholder}" autocomplete="off" spellcheck="false">
        {hint_html}
    </div>"""


def _render_password_page(error: str = "", first_run: bool = False) -> str:
    err = f'<div class="alert error">{error}</div>' if error else ""
    if first_run:
        notice = """
        <div class="alert info">
            <strong>First-run setup:</strong> no owner password is configured yet.
            You can claim this wizard now by setting an owner password below &mdash;
            after that, only the password will unlock this page.
        </div>
        <form id="claimForm" class="login-box">
            <h2>Claim this addon</h2>
            <div class="field"><label for="newPass">Set owner password</label>
            <input id="newPass" type="password" placeholder="Choose a strong password" autocomplete="new-password"></div>
            <div class="field"><label for="newPass2">Repeat password</label>
            <input id="newPass2" type="password" placeholder="Repeat the password" autocomplete="new-password"></div>
            <button type="submit" class="btn primary">Claim &amp; open wizard</button>
        </form>"""
        return _wrap_page("Setup", notice, login_mode=True)

    if not config_store.can_unlock_wizard():
        locked_msg = """
        <div class="login-box">
            <h2>Wizard Locked</h2>
            <div class="alert info" style="margin-top: 14px; text-align: left;">
                This instance has credentials configured via environment variables. To access the web wizard,
                please add <code>CONFIG_PASSWORD</code> or <code>API_KEY</code> to your environment variables / secrets.
            </div>
        </div>"""
        return _wrap_page("Locked", locked_msg, login_mode=True)

    has_api_key = bool(os.getenv("API_KEY", "").strip())
    sub_text = "This configuration wizard is protected. Enter your owner password or API key to unlock it." if has_api_key else "This configuration wizard is protected. Only the owner who deployed this addon (with the password) can unlock it."
    placeholder = "Enter owner password or API key" if has_api_key else "Enter the owner password"

    return _wrap_page("Locked", f"""
    <form id="loginForm" class="login-box">
        <h2>Owner access</h2>
        <p class="sub">{sub_text}</p>
        {err}
        <div class="field"><label for="pass">Password</label>
        <input id="pass" type="password" placeholder="{placeholder}" autocomplete="current-password"></div>
        <button type="submit" class="btn primary">Unlock</button>
    </form>""", login_mode=True)


def _render_wizard(values: dict, stats: dict, rate_enabled: bool, watch_logs: bool) -> str:
    lang_options = "".join(
        f'<option value="{code}" {"selected" if values.get("TMDB_LANGUAGE", "en-US") == code else ""}>{name}</option>'
        for code, name in LANGUAGES
    )

    html = f"""
    <div class="wizard-header">
        <h1>Configuration Wizard</h1>
        <p>Set up your Telegram Stremio addon without touching env files. Changes are validated and applied instantly.</p>
        <div class="stats-row">
            <div class="stat"><span class="stat-num" id="statCache">{stats.get('cached_entries', 0)}</span><span class="stat-label">cached entries</span></div>
            <div class="stat"><span class="stat-num" id="statSessions">{stats.get('watch_sessions', 0)}</span><span class="stat-label">watch sessions</span></div>
            <div class="stat"><span class="stat-num" id="statShows">{stats.get('tracked_shows', 0)}</span><span class="stat-label">tracked shows</span></div>
        </div>
    </div>

    <div id="alerts"></div>

    <details class="sec" open>
        <summary>1. Telegram credentials <span class="tag">required</span></summary>
        <div class="grid">
            {_field("API_ID", "API ID", values, "1234567", input_type="number")}
            {_field("API_HASH", "API Hash", values, "0123456789abcdef0123456789abcdef", secret=True,
                    hint="From my.telegram.org → API development tools")}
        </div>
        <div class="grid">
            {_field("USER_SESSION_STRING", "User Session String (recommended)", values, "", secret=True,
                    hint="Needed for files > 2 GB and public channels. Generate with the session notebook in /deployment/colab")}
            {_field("BOT_TOKEN", "Bot Token (fallback)", values, "123456:ABC-DEF...", secret=True,
                    hint="Bot must be admin in the target channels")}
        </div>
        <div class="grid">
            {_field("TELEGRAM_CHANNEL_ID", "Media Channel IDs", values, "-1001234567890, -1009876543210",
                    hint="Comma-separated. Right-click a channel in Telegram Desktop → Copy ID (needs developer mode)")}
            {_field("LOG_CHANNEL_ID", "Log Channel ID (optional)", values, "-1001122334455",
                    hint="Play/seek/stop logs with your region time, date, year and play counts are posted here")}
        </div>
        <button type="button" class="btn secondary" id="btnTest">Test Telegram connection</button>
        <span id="testResult" class="test-result"></span>
    </details>

    <details class="sec">
        <summary>2. Addon URL &amp; access</summary>
        <div class="grid">
            {_field("ADDON_URL", "Public Addon URL", values, "https://your-app.onrender.com",
                    hint="Where this addon is reachable (used to build stream URLs)")}
            {_field("API_KEY", "Manifest API Key (optional)", values, "", secret=True,
                    hint="If set, the manifest is only served at /&lt;key&gt;/manifest.json")}
        </div>
        <div class="grid">
            {_field("TIMEZONE", "Timezone for logs", values, "Asia/Kolkata",
                    hint="IANA name or UTC+05:30 offset")}
            {_field("CONFIG_PASSWORD", "Wizard password", values, "", secret=True, input_type="password",
                    hint="Leave empty to keep the current password")}
        </div>
    </details>

    <details class="sec">
        <summary>3. Posters, genres &amp; language (TMDB)</summary>
        <div class="grid">
            {_field("TMDB_API_KEY", "TMDB API Key (optional)", values, "", secret=True,
                    hint="Free key from themoviedb.org. Enables TMDB posters + localized titles. Without it, Cinemeta (English) is used")}
            <div class="field">
                <label for="f_TMDB_LANGUAGE">Poster / title language</label>
                <select id="f_TMDB_LANGUAGE" data-key="TMDB_LANGUAGE">{lang_options}</select>
                <div class="hint">Applied as the TMDB language= parameter</div>
            </div>
        </div>
    </details>

    <details class="sec">
        <summary>4. Cache &amp; protection</summary>
        <div class="grid">
            {_field("CACHE_TTL", "Search cache TTL (seconds)", values, "1800", input_type="number",
                    hint="How long catalog/search results are reused before re-querying Telegram")}
            {_field("RATE_LIMIT_REQUESTS", "API requests / window / IP", values, "120", input_type="number")}
        </div>
        <div class="grid">
            {_field("RATE_LIMIT_WINDOW", "Rate limit window (seconds)", values, "60", input_type="number")}
            {_field("RATE_LIMIT_STREAM_REQUESTS", "Stream range requests / window / IP", values, "600", input_type="number",
                    hint="Keep this high - players fire many small range requests")}
        </div>
        <label class="toggle">
            <input type="checkbox" id="f_RATE_LIMIT_ENABLED" data-key="RATE_LIMIT_ENABLED" {"checked" if rate_enabled else ""}>
            <span>Enable per-IP rate limiting (turn on when sharing publicly)</span>
        </label>
    </details>

    <details class="sec">
        <summary>5. Watch history &amp; logs</summary>
        <label class="toggle">
            <input type="checkbox" id="f_WATCH_LOG_EVENTS" data-key="WATCH_LOG_EVENTS" {"checked" if watch_logs else ""}>
            <span>Log start / seek / stop events (builds the Continue Watching feed and Telegram play logs)</span>
        </label>
    </details>

    <div class="actions">
        <button type="button" class="btn primary" id="btnSave">Save &amp; apply</button>
        <button type="button" class="btn danger" id="btnClearCache">Clear persistent cache</button>
        <button type="button" class="btn secondary" id="btnLogout">Lock wizard</button>
    </div>
    <p class="foot-note">Saved config lives in <code>data/config.json</code> and survives restarts. Env vars still work as fallback for anything not set here.</p>
    """
    return _wrap_page("Wizard", html, login_mode=False)


def _wrap_page(inner_title: str, body: str, login_mode: bool) -> str:
    if login_mode:
        script = """
        document.getElementById('loginForm')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const res = await fetch('/configure/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: document.getElementById('pass').value})});
            if (res.ok) { location.reload(); } else { const j = await res.json().catch(()=>({})); location.reload(); }
        });
        document.getElementById('claimForm')?.addEventListener('submit', async (e) => {
            e.preventDefault();
            const p1 = document.getElementById('newPass').value, p2 = document.getElementById('newPass2').value;
            if (!p1 || p1 !== p2) { alert('Passwords must match and not be empty'); return; }
            const res = await fetch('/configure/claim', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: p1})});
            if (res.ok) { location.reload(); } else { alert('Failed to claim: ' + (await res.text())); }
        });
        """
    else:
        script = """
        function alertBox(msg, ok) {
            const box = document.getElementById('alerts');
            box.innerHTML = `<div class="alert ${ok ? 'success' : 'error'}">${msg}</div>`;
            setTimeout(() => { box.innerHTML = ''; }, 6000);
        }
        function collect() {
            const data = {};
            document.querySelectorAll('[data-key]').forEach(el => {
                const k = el.dataset.key;
                if (el.type === 'checkbox') { data[k] = el.checked; return; }
                let v = el.value.trim();
                if (v === '') return;                    // empty = keep existing
                if (v.includes('••••')) return;          // masked placeholder = unchanged
                data[k] = v;
            });
            return data;
        }
        document.getElementById('btnSave')?.addEventListener('click', async () => {
            const btn = document.getElementById('btnSave');
            btn.disabled = true; btn.textContent = 'Validating & applying...';
            try {
                const res = await fetch('/configure/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(collect())});
                const j = await res.json();
                if (res.ok && j.ok) { alertBox('Saved & applied! ' + (j.note || ''), true); setTimeout(()=>location.reload(), 2500); }
                else { alertBox('Error: ' + (j.errors ? j.errors.join(' | ') : (j.detail || 'unknown')), false); }
            } catch (err) { alertBox('Request failed: ' + err, false); }
            btn.disabled = false; btn.textContent = 'Save & apply';
        });
        document.getElementById('btnTest')?.addEventListener('click', async () => {
            const el = document.getElementById('testResult');
            el.textContent = ' Testing... (may take ~15s)';
            try {
                const res = await fetch('/configure/test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(collect())});
                const j = await res.json();
                el.textContent = j.ok ? ' ✓ Connected as ' + j.user : ' ✗ ' + (j.detail || 'Connection failed');
            } catch (err) { el.textContent = ' ✗ ' + err; }
        });
        document.getElementById('btnClearCache')?.addEventListener('click', async () => {
            if (!confirm('Delete all cached search results and watch history?')) return;
            const res = await fetch('/configure/clear-cache', {method:'POST'});
            alertBox(res.ok ? 'Cache cleared.' : 'Failed to clear cache.', res.ok);
            setTimeout(()=>location.reload(), 1200);
        });
        document.getElementById('btnLogout')?.addEventListener('click', async () => {
            await fetch('/configure/logout', {method:'POST'});
            location.reload();
        });
        """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{inner_title} · Telegram Stremio Addon</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;600;700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#09090b; --card:#18181b; --border:#27272a; --text:#f4f4f5; --text2:#a1a1aa;
  --primary:#2563eb; --primary-h:#1d4ed8; --accent:#60a5fa; --green:#22c55e; --red:#ef4444; --amber:#f59e0b;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:'Inter',-apple-system,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; display:flex; justify-content:center; padding:40px 16px; }}
.wrap {{ width:100%; max-width:860px; }}
h1 {{ font-family:'Plus Jakarta Sans',sans-serif; font-size:1.7rem; letter-spacing:-0.02em; }}
.login-box, .sec {{ background:var(--card); border:1px solid var(--border); border-radius:12px; }}
.login-box {{ padding:32px; max-width:460px; margin:10vh auto 0; }}
.login-box h2 {{ font-family:'Plus Jakarta Sans',sans-serif; margin-bottom:6px; }}
.login-box .sub {{ color:var(--text2); font-size:.86rem; line-height:1.5; margin-bottom:16px; }}
.wizard-header {{ margin-bottom:24px; }}
.wizard-header p {{ color:var(--text2); font-size:.92rem; margin-top:6px; }}
.stats-row {{ display:flex; gap:12px; margin-top:16px; flex-wrap:wrap; }}
.stat {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:10px 16px; display:flex; align-items:baseline; gap:8px; }}
.stat-num {{ font-family:'Plus Jakarta Sans',sans-serif; font-weight:700; font-size:1.15rem; color:var(--accent); }}
.stat-label {{ font-size:.76rem; color:var(--text2); }}
.sec {{ padding:20px 24px; margin-bottom:16px; }}
.sec summary {{ font-family:'Plus Jakarta Sans',sans-serif; font-weight:600; cursor:pointer; user-select:none; outline:none; }}
.sec summary .tag {{ font-size:.66rem; background:rgba(239,68,68,.15); color:var(--red); border:1px solid rgba(239,68,68,.4); padding:2px 8px; border-radius:99px; margin-left:8px; vertical-align:middle; }}
.grid {{ display:grid; grid-template-columns:1fr; gap:14px; margin:18px 0 6px; }}
@media(min-width:640px) {{ .grid {{ grid-template-columns:1fr 1fr; }} }}
.field label, .login-box label {{ display:block; font-size:.78rem; font-weight:600; color:var(--text2); margin-bottom:6px; text-transform:uppercase; letter-spacing:.04em; }}
.field input, .login-box input, .field select {{
  width:100%; background:#09090b; border:1px solid var(--border); color:var(--text);
  padding:11px 14px; border-radius:8px; font-size:.88rem; font-family:'Inter',sans-serif; outline:none; transition:border-color .2s;
}}
.field input:focus, .field select:focus, .login-box input:focus {{ border-color:var(--primary); }}
.hint {{ font-size:.72rem; color:var(--text2); margin-top:5px; line-height:1.45; }}
.hidden {{ display:none; }}
.toggle {{ display:flex; align-items:center; gap:10px; margin-top:14px; font-size:.86rem; color:var(--text); cursor:pointer; }}
.toggle input {{ width:16px; height:16px; accent-color:var(--primary); }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin:8px 0 18px; }}
.btn {{ padding:12px 22px; border-radius:8px; border:1px solid transparent; font-size:.9rem; font-weight:600; cursor:pointer; font-family:'Inter',sans-serif; transition:all .2s; }}
.btn.primary {{ background:var(--primary); color:#fff; }} .btn.primary:hover {{ background:var(--primary-h); }}
.btn.secondary {{ background:#27272a; color:var(--text); border-color:#3f3f46; }} .btn.secondary:hover {{ background:#3f3f46; }}
.btn.danger {{ background:transparent; color:var(--red); border-color:rgba(239,68,68,.5); }} .btn.danger:hover {{ background:rgba(239,68,68,.1); }}
.alert {{ padding:12px 16px; border-radius:8px; margin-bottom:14px; font-size:.86rem; line-height:1.5; }}
.alert.error {{ background:rgba(239,68,68,.12); border:1px solid rgba(239,68,68,.4); color:#fca5a5; }}
.alert.success {{ background:rgba(34,197,94,.12); border:1px solid rgba(34,197,94,.4); color:#86efac; }}
.alert.info {{ background:rgba(96,165,250,.1); border:1px solid rgba(96,165,250,.4); color:#93c5fd; }}
.test-result {{ font-size:.82rem; margin-left:10px; }}
.foot-note {{ color:var(--text2); font-size:.78rem; line-height:1.6; }}
.foot-note code {{ background:#27272a; padding:2px 6px; border-radius:4px; }}
.btn:disabled {{ opacity:.6; cursor:wait; }}
</style>
</head>
<body>
<div class="wrap">
{body}
</div>
<script>{script}</script>
</body>
</html>"""


@router.get("/configure", response_class=HTMLResponse)
async def configure_page(request: Request):
    if config_store.is_first_run():
        # Nothing configured and no password anywhere: show the claim page so
        # the actual owner sets the password before anyone can touch the config.
        if _authorized(request):
            values = await _current_values()
            stats = cache.get_stats()
            return HTMLResponse(_render_wizard(
                values, stats,
                rate_enabled=bool(Config.RATE_LIMIT_ENABLED),
                watch_logs=bool(Config.WATCH_LOG_EVENTS),
            ))
        return HTMLResponse(_render_password_page(first_run=True))
    if not _authorized(request):
        return HTMLResponse(_render_password_page())
    values = await _current_values()
    stats = cache.get_stats()
    return HTMLResponse(_render_wizard(
        values, stats,
        rate_enabled=bool(Config.RATE_LIMIT_ENABLED),
        watch_logs=bool(Config.WATCH_LOG_EVENTS),
    ))


@router.post("/configure/login")
async def configure_login(request: Request):
    try:
        body = await request.json()
        password = str(body.get("password", ""))
    except Exception:
        return JSONResponse({"detail": "Invalid request"}, status_code=400)
    if not config_store.verify_wizard_password(password):
        logger.warning(f"Failed wizard login attempt from {_client_ip(request)}")
        return JSONResponse({"detail": "Wrong password"}, status_code=403)
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(WIZARD_COOKIE, token, httponly=True, samesite="lax", max_age=12 * 3600)
    return resp


@router.post("/configure/claim")
async def configure_claim(request: Request):
    """First-run only: set the owner password, then open the wizard."""
    if config_store.wizard_locked():
        return JSONResponse({"detail": "Already claimed"}, status_code=403)
    try:
        body = await request.json()
        password = str(body.get("password", ""))
    except Exception:
        return JSONResponse({"detail": "Invalid request"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"detail": "Password must be at least 6 characters"}, status_code=400)
    cfg = config_store.load_file_config()
    cfg["CONFIG_PASSWORD"] = password  # will be hashed by sanitize
    if not config_store.save_file_config(cfg):
        return JSONResponse({"detail": "Failed to persist config"}, status_code=500)
    Config.apply_file_overrides()
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(WIZARD_COOKIE, token, httponly=True, samesite="lax", max_age=12 * 3600)
    return resp


@router.post("/configure/logout")
async def configure_logout(request: Request):
    token = request.cookies.get(WIZARD_COOKIE, "")
    _sessions.discard(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(WIZARD_COOKIE)
    return resp


@router.post("/configure/save")
async def configure_save(request: Request):
    if not _authorized(request):
        return JSONResponse({"detail": "Unauthorized - unlock the wizard first"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "Invalid payload"}, status_code=400)

    existing = config_store.load_file_config()
    candidate = dict(existing)
    for k, v in body.items():
        if k in config_store.WIZARD_KEYS:
            if isinstance(v, str) and "••••" in v:
                continue
            candidate[k] = v

    # Fall back to active Config (e.g., from .env) for keys not yet in config.json
    for k in config_store.WIZARD_KEYS:
        if k == "CONFIG_PASSWORD":
            continue
        if candidate.get(k) in (None, ""):
            env_val = getattr(Config, k, None) or os.getenv(k, None)
            if env_val is not None and env_val != "":
                candidate[k] = env_val

    # Never overwrite the password with an empty or masked value
    pw = str(body.get("CONFIG_PASSWORD", "") or "").strip()
    if not pw or "••••" in pw:
        candidate["CONFIG_PASSWORD"] = existing.get("CONFIG_PASSWORD", "") or getattr(Config, "CONFIG_PASSWORD", "")

    errors = config_store.validate_config_dict(candidate)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)

    # Apply in-memory first to validate Telegram-specific formatting
    saved_ok = config_store.save_file_config(candidate)
    if not saved_ok:
        return JSONResponse({"ok": False, "errors": ["Failed to write config file (check data/ permissions)"]}, status_code=500)

    Config.apply_file_overrides()

    note = "Config saved."
    # Restart the Telegram client so new credentials take effect
    try:
        from tg_client import tg_client_manager
        await tg_client_manager.reconfigure()
        try:
            from addon import start_watch_finalizer
            start_watch_finalizer()
        except Exception:
            pass
        note += " Telegram client restarted with the new credentials."
    except Exception as e:
        note += f" Warning: client restart failed ({e}) - check your credentials."
        logger.error(f"Client reconfigure failed after wizard save: {e}")

    return JSONResponse({"ok": True, "note": note})


@router.post("/configure/test")
async def configure_test(request: Request):
    if not _authorized(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}

    from pyrogram import Client

    api_id = body.get("API_ID") or Config.API_ID
    api_hash = body.get("API_HASH") or Config.API_HASH
    session = body.get("USER_SESSION_STRING") or Config.USER_SESSION_STRING
    bot_token = body.get("BOT_TOKEN") or Config.BOT_TOKEN

    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "detail": "API_ID must be an integer"})

    if not api_id or not api_hash or (not session and not bot_token):
        return JSONResponse({"ok": False, "detail": "API_ID, API_HASH and (session string or bot token) are required"})

    def _masked_token(tok: str) -> str:
        return "".join(ch if ch.isdigit() or ch.isalpha() else ch for ch in tok[:6]) + "..."

    temp = None
    try:
        if session:
            temp = Client(name=f"wizard_test_{secrets.token_hex(4)}", api_id=api_id, api_hash=api_hash,
                          session_string=session, in_memory=True, no_updates=True)
        else:
            temp = Client(name=f"wizard_test_{secrets.token_hex(4)}", api_id=api_id, api_hash=api_hash,
                          bot_token=bot_token, in_memory=True, no_updates=True)
        await asyncio.wait_for(temp.start(), timeout=20)
        me = temp.me
        user = getattr(me, "username", None) or getattr(me, "first_name", None) or "connected"

        # Also verify the first configured channel is reachable
        channels = str(body.get("TELEGRAM_CHANNEL_ID") or Config.TELEGRAM_CHANNEL_ID or "")
        first = channels.split(",")[0].strip() if channels else ""
        if first:
            try:
                chat = await asyncio.wait_for(temp.get_chat(first), timeout=10)
                user += f" · channel OK ({getattr(chat, 'title', first)})"
            except Exception as ce:
                return JSONResponse({"ok": False, "detail": f"Login OK ({user}) but channel not reachable: {ce}"})
        return JSONResponse({"ok": True, "user": user})
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "detail": "Timed out connecting to Telegram (check credentials/internet)"})
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)})
    finally:
        if temp is not None:
            try:
                await asyncio.wait_for(temp.stop(), timeout=5)
            except Exception:
                pass


@router.post("/configure/clear-cache")
async def configure_clear_cache(request: Request):
    if not _authorized(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=403)
    try:
        import sqlite3
        if cache.conn is not None:
            with cache._db_lock:
                cache.conn.execute("DELETE FROM kv_cache")
                cache.conn.execute("DELETE FROM watch_sessions")
                cache.conn.execute("DELETE FROM show_map")
                cache.conn.commit()
        from tg_client import tg_client_manager
        tg_client_manager._search_cache.clear()
        tg_client_manager._message_cache.clear()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=500)
