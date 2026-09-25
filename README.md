---
title: Telegram Stremio Addon
emoji: 🎬
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

# Telegram Stremio Addon

![Telegram Stremio Addon Banner](stremio_telegram_banner.png)

[![License](https://img.shields.io/badge/License-MIT--NC-blue?style=for-the-badge)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/SunilRoy-dev/stremio-telegram-debrid?style=for-the-badge&logo=github)](https://github.com/SunilRoy-dev/stremio-telegram-debrid/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/SunilRoy-dev/stremio-telegram-debrid?style=for-the-badge)](https://github.com/SunilRoy-dev/stremio-telegram-debrid/network/members)

Turn your private Telegram channels into a personal streaming library for Stremio. You put your own video files on a channel, connect this addon once, and everything shows up inside Stremio with real posters, clean part lists and instant playback. Nothing gets downloaded first — when you press play, the addon pulls the file straight from Telegram and streams it to your player as you watch.

### Why I built this

I keep my files on a private Telegram channel and wanted to watch them on my TV through Stremio without downloading every file first or running a media server at home. Everything I found either needed a paid subscription, a home server running around the clock, or an afternoon of configuration. So I wrote this. It started as a tiny weekend script and slowly grew into what you see here — a setup wizard, home screen catalogs, watch progress, the works.

Found a bug, or something in this guide confusing? Please open an Issue. Pull requests are welcome too.
*Dont ask for piracy related questions*

> [!NOTE]
> If you like this Project, keeping a ⭐ on the repo helps more than you'd think. It keeps me motivated to maintain it.

---

## Key Features

- **Web setup wizard** — deploy it, open `/configure` in your browser, set an owner password, fill in a form. Done. Environment variables are now optional, not required.
- **Home screen catalogs with genre rows** — your channel shows up as organized video catalogs in Stremio, and the home page gets genre rows (Action, Drama, Sci-Fi, Documentary and so on) for easy browsing.
- **Videos grouped the right way** — multi-part and episodic videos are grouped cleanly under one title card with an organized part list, instead of appearing as a pile of loose files.
- **Posters and titles in your language** — add a free TMDB key, set `TMDB_LANGUAGE` (say `hi-IN` or `es-ES`), and posters, titles and descriptions come back in that language.
- **Survives restarts** — searches, file details, posters and watch progress now live in a small SQLite database file, so your hosting platform restarting the app no longer wipes anything or repeats the same lookups against Telegram.
- **Continue Watching** — pause a video halfway, come back tomorrow, and resume seamlessly right from the Continue Watching row on the home screen.
- **Play logs in a Telegram channel (optional)** — every play, seek and stop can be posted to a log channel of your choice, with your local date, time, year and total play count.
- **Rate limiting per visitor (optional)** — off by default. Sharing your addon link with friends or a group? Flip one switch so nobody can hog all the bandwidth.
- **Split files play as one** — big videos that had to be uploaded in parts (Telegram caps uploads at 2GB for regular accounts, 4GB for Premium) get stitched back into a single continuous stream automatically.
- **Clean catalog presentation** — subtitle and audio files won't clutter video rows, stream links recover automatically if interrupted, and persistent caching prevents hammering Telegram.

---

## Getting started in 5 steps

No coding needed. You'll touch a terminal exactly once (to generate a session string), and that's it.

| Step | What you do | Where |
| :--- | :--- | :--- |
| **1. Fork this repo** | Click **Fork** at the top of this page. That copies the project to your own GitHub account so you can deploy it. | This page |
| **2. Get Telegram API keys** | Create free API keys at [my.telegram.org](https://my.telegram.org). Takes about 2 minutes — [walkthrough below](#one-time-setup-telegram-api-keys). | Telegram website |
| **3. Get a session string** | One small script run on a computer or phone gives you a `USER_SESSION_STRING`. [Computer guide](#how-to-generate-user_session_string-on-your-computer) · [phone guide](#how-to-generate-user_session_string-on-your-phone) | Your device |
| **4. Deploy it** | Pick a platform from the [deploy options](#where-can-i-deploy-this) and click its deploy button. | Hosting platform |
| **5. Configure and install** | Open `https://your-deployed-url/configure`, set your owner password, paste your keys into the form, hit save. Then install into Stremio with one click or via manifest link. ([Stremio guide](#installing-it-in-stremio)) | Browser + Stremio |

That's genuinely the whole process. If you'd rather configure things the old way with environment variables, that's still fully supported — see the [settings reference](#all-settings-reference) further down. The two play nicely together: anything saved in the wizard wins.

### One-time setup: Telegram API keys

1. Go to [my.telegram.org](https://my.telegram.org) and log in with your phone number (international format, e.g. `+1234567890`). Telegram sends a code to your app — enter it.
2. Click **API development tools**.
3. Fill in **App title** and **Short name** — anything works, like `tgaddon`. Leave the rest as-is.
4. Hit submit and copy your `api_id` and `api_hash`.

Getting an error when saving? Turn off your VPN or adblocker, or try a private/incognito window. That fixes it most of the time.

### One-time setup: find your channel ID

The addon needs to know which channel(s) to read from.

1. Create a channel in Telegram and keep it **Private** (recommended).
2. Open [web.telegram.org](https://web.telegram.org), click your channel, and look at the browser URL. It'll look like `https://web.telegram.org/a/#-1001234567890` — that number starting with `-100` is your channel ID.
3. Prefer a bot? Forward any message from the channel to `@username_to_id_bot` and it replies with the ID.

---

## Where can I deploy this?

Every service below works with the one-click buttons. None of them charge you at signup unless noted.

| Platform | What to expect | Deploy |
| :--- | :--- | :--- |
| **Render** | Free tier, no card needed. Sleeps after 15 min of inactivity (about a minute to wake). Has a 5GB/month outbound data cap — fine for testing, tight for daily use. | [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/SunilRoy-dev/stremio-telegram-debrid) |
| **Koyeb** | Free and always on (no sleeping). Needs a card check at signup, but doesn't charge it. | [![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&repository=github.com/SunilRoy-dev/stremio-telegram-debrid&branch=main&name=stremio-telegram-debrid) |
| **Hugging Face Spaces** | Docker Spaces now require a **paid PRO plan** — this used to be free, but not anymore. | [Setup guide](#hugging-face-spaces) |
| **Railway** | Trial credits (roughly 500 hours of runtime), then it stops until you upgrade. | [![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/SunilRoy-dev/stremio-telegram-debrid) |
| **Zeabur** | Trial credits, similar story to Railway. | [![Deploy on Zeabur](https://zeabur.com/button.svg)](https://zeabur.com/templates/deploy?template=https://github.com/SunilRoy-dev/stremio-telegram-debrid) |
| **Heroku** | Paid from $5/month, but rock solid, fast, and always on. | [![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://www.heroku.com/deploy/?template=https://github.com/SunilRoy-dev/stremio-telegram-debrid) |
| **Google Colab** | Free but temporary. Great for trying it out; shuts down after a few hours. | [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/SunilRoy-dev/stremio-telegram-debrid/blob/beta/deployment/colab/deploy_colab.ipynb) |

**Honest advice:** streaming is a bandwidth-hungry activity — one 1080p video uses roughly 1–2GB of outbound data. Free tiers with small monthly caps will run out quickly with regular watching. For a personal addon used by one or two people: start with Render (easiest, just know the 5GB cap), use Koyeb if you don't mind the card check (always on, no cap surprise), and go Heroku if you want zero drama and don't mind $5. More detail on each platform [further down](#deployment-platform-details).

## What it can do

Grouped by the stuff you'd actually care about, not the order I wrote the code in.

### Your library inside Stremio

**Catalog rows on the home screen.** The addon registers dedicated catalogs for your video collections alongside Continue Watching rows. Only real video files are listed — subtitle and audio files that happen to sit in the channel are filtered out automatically, so you never get a stray `.srt` file cluttering your video catalogs.

**Genre rows.** The addon advertises the standard genres (Action, Adventure, Animation, Comedy, Crime, Documentary, Drama, Family, Fantasy, History, Horror, Music, Mystery, Romance, Science Fiction, Thriller, War, Western). Once your files are matched to their titles, Stremio's home page fills up with genre-filtered rows for your library.

**Multi-part and episodic videos grouped properly.** Files like `Video S01E01.mkv`, `Video S01E02.mkv` or `Video Part 1.mp4` become one entry with an organized episode and part list. If the same part exists in multiple releases or qualities, the highest quality file wins (usually the better one). Split parts are stitched together automatically.

**Real posters and details.** Every item gets a poster, a description, a year and genres. With a free [TMDB API key](https://www.themoviedb.org/settings/api) you get the best results, including titles and posters in your own language via `TMDB_LANGUAGE` — for example `hi-IN` for Hindi or `es-ES` for Spanish. Without a key it falls back to Stremio's built-in Cinemeta, which works fine but stays in English. Either way, results are cached for 7 days so catalogs load fast and the metadata service isn't hammered.

### Playback

**Split files become one continuous video.** Telegram caps uploads at 2GB (regular accounts) or 4GB (Premium), so long videos often get uploaded in chunks — `Video.mkv.001`, `Video.mkv.002`, `Video.part1.rar`, `Video_part_1.mp4` and friends. The addon recognizes these patterns and stitches the parts back into a single continuous stream. Seeking works across part boundaries. It behaves well with standard formats in Stremio's player, and split detection is fully automatic. More detail [below](#split-file-playback).

**Instant seeking.** Full range request support end to end (HTTP 206). Scrub around in Stremio, VLC or MPV without waiting for anything to load.

**Stream links that don't die mid-video.** Telegram links expire after a few hours. If your stream stalls because the link went stale, the addon quietly fetches a fresh one and playback continues.

**Subtitles picked up automatically.** Drop `.srt`, `.vtt` or `.ass` files next to your videos in the channel and they appear in Stremio's subtitle menu. They're offered as subtitles only — never as playable items in your catalogs.

**ZIP archives — possible, but I don't recommend it.** You can play videos stored inside a `.zip` (including split ones like `.zip.001` / `.zip.002`). The catch: seeking doesn't work, startup is slow, and it's heavy on the server. If you care about the details, read the [ZIP section](#zip-support-read-this-before-uploading) — otherwise, just upload files directly.

### Safety and control

**The `/configure` wizard.** A password-protected web page where you set up everything — Telegram keys, channels, TMDB, timezone, rate limits — from a form, with a live connection test before saving. It's described in full [in its own section](#the-configuration-wizard), because it's the part most people will use.

**A cache that keeps Telegram happy.** This matters more than people think: hammering Telegram's API is how accounts end up restricted. The addon serves repeat lookups from its cache first, runs at most 2 searches at a time with a small delay between them, and backs off automatically when Telegram says "slow down" (FloodWait). The cache lives in a SQLite file, so it survives restarts instead of starting cold every time.

**Rate limiting you control.** Off by default — your personal instance, your call. If you ever share the addon link with a group, turn it on (one checkbox in the wizard or `RATE_LIMIT_ENABLED=true`) and each visitor gets their own budget: 120 API requests per minute and 600 media range requests per minute (deliberately generous so normal playback never trips it). Exceeding it returns a clean "slow down" response instead of choking the server.

**Watch history.** Every stream gets start, seek and stop events recorded. A session that sits idle for 10 minutes is treated as stopped. Anything between roughly 1% and 95% watched shows up in the Continue Watching rows, with progress and play counts. For episodic videos, the entry links back to the collection's part list with the last-watched item noted.

**Play logs in Telegram (optional).** Set `LOG_CHANNEL_ID` (or fill it in the wizard) and a channel of your choice receives a message for every playback event — with the file name, your region's date and time (from `TIMEZONE`), the year, and how many times that file has been played in total. Example:

```text
🟢 Playback Started
📁 File: `Video.2023.1080p.mkv`
📅 Date & Time: `2026-09-25 21:45:10` (Asia/Kolkata)
📆 Year: `2026`
🔁 Play Count: `1`
💬 Source Channel: `-1001234567890`
🆔 Message ID: `42`
```

Seek, stop and finish events get logged the same way, including progress. Not interested? `WATCH_LOG_EVENTS=false` turns the whole thing off.

---

## The configuration wizard

This is the part designed for people who don't want to touch config files, and it replaces the old "edit a dozen environment variables" ritual.

**How it works:**

1. Deploy the addon with no configuration at all. Yes, really — nothing is required up front.
2. Open `https://your-deployed-url/configure` in a browser.
3. **On first visit** the page asks you to *claim* it by setting an owner password. From that moment on, only that password opens the page. Anyone else who stumbles on the URL sees a lock screen, nothing more.
4. Fill in the form: API ID and Hash, your session string (or bot token), your channel IDs, and whatever optional extras you want — TMDB key and language, log channel, timezone, the manifest password (`API_KEY`), rate limit toggle, search cache duration.
5. Hit **Test Telegram connection**. It checks your credentials against Telegram for real and tells you who you're logged in as. Takes about 15 seconds.
6. Hit **Save & apply**. The settings are validated, written to `data/config.json`, and the Telegram client restarts with the new values — no redeploy, no rebuild, no waiting.

The page also shows cache stats and has a **Clear persistent cache** button for when you want a clean slate, plus a logout button for shared computers.

> [!TIP]
> **Uploaded new videos to your channel?** Telegram search results are cached for 30 minutes (`CACHE_TTL=1800`) to keep requests fast and prevent Telegram rate limits. If you uploaded new files and want them to appear in Stremio right away, simply click **Clear persistent cache** in the wizard.

> [!IMPORTANT]
> **Claim the wizard yourself, right after deploying.** If you deploy without setting `CONFIG_PASSWORD`, the *first person* who opens `/configure` gets to claim it. On a private instance that's you anyway — just don't leave a public URL unclaimed for hours. If you'd rather be safe from the start, set `CONFIG_PASSWORD` as an env var and the claim step is skipped entirely.

A few things worth knowing:

- Passwords are stored hashed, never in plain text.
- Anything saved in the wizard overrides the same setting from env vars. Env vars still work as fallback for anything the wizard hasn't set.
- Wizard login sessions expire when the addon restarts. Annoying after a platform redeploys, but safer.

---

## Split file playback

If Telegram's 2GB/4GB upload cap forced you to upload a long video in parts, the addon puts it back together at play time. You don't configure anything — it recognizes the patterns on its own:

- **Numbered extensions:** `Video.mkv.001`, `Video.mkv.002`, `Video.mkv.003`...
- **Part indicators:** `Video.part1.rar`, `Video.part2.rar`... (also `.part01.mkv`, `.part02.mkv`...)
- **Suffix separators:** `Video_part_1.mp4`, `Video_part_2.mp4`...

What happens under the hood: the parts are grouped into one item with their combined size (say, `6.2 GB`), and when you press play, the addon maps each byte range of your player's request onto the right part. It only downloads the segments needed at each moment and hands over from one part to the next in memory, so playback doesn't stop at the 2GB mark.

> [!NOTE]
> This one's an early-stage feature. It works well for typical `.mp4`/`.mkv` files, but if a player does something unusual with ranges, results can vary. If a split file misbehaves, tell me which player and which format — it genuinely helps.

---

## ZIP support (read this before uploading)

> [!CAUTION]
> **ZIP playback can't seek.** To skip to minute 40 of a video inside a ZIP, the server has to download and unpack everything from the start to that point. For big files that takes so long your player gives up first. Startup is slow too. If seeking matters — and it does for most people — upload your videos **directly** as `.mp4`, `.mkv` etc., or as split video parts. ZIPs are a last resort.

That said, if you already have ZIPs in the channel: the addon opens them, lists the video files inside as playable items, and streams them. Split ZIPs (`.zip.001`, `.zip.002`) work as well. Just know the limitations above before you wonder why the scrub bar isn't cooperating.

---

## Naming and matching guide

The addon finds your files by reading file names and message captions. The cleaner the name, the better the matching.

```text
[Title] [Part/Episode info] [Extra tags].extension
```

**Rules of thumb:**

1. **Title and part details can go in the file name, the caption, or both.** The addon reads all of them.
2. **Numbering and part formats are flexible.** `S01E01`, `s1e1`, `1x01`, `Season 1 Episode 1`, `Part 1`, `Temporada 1 Capitulo 1` (Spanish and other languages work), `Ep 12`, `capitulo 12`, `[12]`, `- 12 -` — all work. Standalone part numbers are grouped together automatically.
3. **Videos sent directly as Telegram video messages work too**, even without an extension in the name.
4. **Put extras at the end:** resolution, audio tags and the like — e.g. `Video_Title_S01E02_[1080p]_[Dual-Audio].mkv`.

## Telegram credentials: bot token vs. user session

The addon can log into Telegram two ways, and the difference matters a lot for streaming. my advice is to use both bot token and user session for best experience.  

> [!IMPORTANT]
> **Use a user session string if you can.** Bots are limited to 2GB files, get throttled hard by Telegram, and are generally flaky for streaming large files. A user session handles files up to 4GB, downloads faster, and is far more stable. It's what I use personally.

**Bot token** — what it is: a token from [@BotFather](https://t.me/BotFather). The catch: Telegram enforces a hard 2GB limit on bot downloads, and connection rates are throttled. Files over 2GB simply won't stream. Setup: the bot must be an **administrator** of your channel so it can read messages.

**User session** — what it is: a string that represents your own Telegram account login. The upside: no 2GB wall (up to 4GB), faster speeds, no throttling drama. Setup: your account just needs to be a member of the channel. No admin rights needed.

> [!CAUTION]
> **Treat your session string like a password to your whole Telegram account.** Anyone holding it can read and write your chats.
> - Never paste it into files, screenshots, or public repos.
> - Only enter it as a secret on your hosting platform, or into the wizard over HTTPS.
> - Generate it yourself, on your own device, using the scripts below.

### How to generate USER_SESSION_STRING on your computer

Run this in a terminal (Python installed, `pip install pyrogram tgcrypto` done once):

```bash
python -c "
import asyncio
from pyrogram import Client
api_id = int(input('API ID: '))
api_hash = input('API HASH: ')
async def main():
    async with Client('temp_session', api_id, api_hash) as app:
        print('\nYour USER_SESSION_STRING is:\n')
        print(await app.export_session_string())
        print('\nCopy the string completely.')
async def run():
    try:
        await main()
    except Exception as e:
        import traceback
        traceback.print_exc()
asyncio.run(run())
"
```

Enter your API ID, API Hash, phone number and the login code Telegram sends you. Then copy the printed string.

### How to generate USER_SESSION_STRING on your phone

**Option A — Android with Pydroid 3 (works offline):**

1. Install **Pydroid 3 - IDE for Python 3** from the Play Store.
2. In the app menu, open **Pip**, search for `pyrogram tgcrypto`, install.
3. Paste this into the editor:

```python
import asyncio
from pyrogram import Client
api_id = int(input('API ID: '))
api_hash = input('API HASH: ')
async def main():
    async with Client('temp_session', api_id, api_hash) as app:
        print('\nYour USER_SESSION_STRING is:\n')
        print(await app.export_session_string())
asyncio.run(main())
```

4. Tap the play button and answer the prompts (API ID, API Hash, phone number, login code).
5. Copy the string from the output.

**Option B — any phone or PC, straight in the browser (Google Colab):**

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/SunilRoy-dev/stremio-telegram-debrid/blob/beta/deployment/colab/generate_session.ipynb)

Open the notebook, fill in your `API_ID` and `API_HASH`, run both steps, log in with the code Telegram sends, and copy the string. Nothing gets installed on your device.

---

## Configuring your channels

Multiple channels work fine — separate them with commas.

- **Private channels:** use their numeric IDs, e.g. `-1001234567890` (see [how to find it](#one-time-setup-find-your-channel-id)).
- **Public channels:** username with or without `@`, e.g. `@my_channel`.
- **Multiple channels:** `-1001234567890, @my_public_channel, another_one`.

Access rules: with a **bot token**, the bot must be an admin in each private channel. With a **user session**, your account just needs to be joined to the channel.

**Performance tip:** keep it to 5–10 channels max. Searches run channel by channel, and Stremio gives the addon only a few seconds to respond before it gives up. More channels = slower responses and a grumpier Telegram.

---

## All settings reference

You can set everything through the [wizard](#the-configuration-wizard) and skip this table entirely. But if you prefer env vars (or want both), here's the full list:

| Variable | Required | What it does |
| :--- | :---: | :--- |
| `API_ID` | Yes* | Telegram API ID from [my.telegram.org](https://my.telegram.org). |
| `API_HASH` | Yes* | Telegram API Hash from the same place. |
| `TELEGRAM_CHANNEL_ID` | Yes* | Channel IDs / usernames, comma-separated. |
| `USER_SESSION_STRING` | — | User session string. Recommended over a bot token. |
| `BOT_TOKEN` | — | Bot token from @BotFather. Use this *or* a session string (session wins). |
| `ADDON_URL` | Yes* | Public URL of your deployed app, e.g. `https://myaddon.onrender.com`. |
| `API_KEY` | No | Extra password for the manifest URL (`https://your-domain/YOUR_KEY/manifest.json`) so strangers can't use your addon. |
| `CONFIG_PASSWORD` | No | Password for the `/configure` wizard. If unset, the first visitor claims it. |
| `LOG_CHANNEL_ID` | No | Channel that receives play/seek/stop log messages. |
| `TIMEZONE` | No | Timezone for log timestamps, e.g. `Asia/Kolkata`. Defaults to `UTC`. |
| `WATCH_LOG_EVENTS` | No | Record watch events (default `true`). `false` disables logging and Continue Watching. |
| `TMDB_API_KEY` | No | Free key from [themoviedb.org](https://www.themoviedb.org/settings/api) for posters, genres and localized titles. Falls back to Cinemeta. |
| `TMDB_LANGUAGE` | No | Language code for TMDB results (`language=` param), e.g. `hi-IN`, `es-ES`. Defaults to `en-US`. |
| `RATE_LIMIT_ENABLED` | No | `true`/`false`. Turn on when sharing the instance publicly. Defaults to `false`. |
| `RATE_LIMIT_REQUESTS` | No | API requests per window per IP (default `120`). |
| `RATE_LIMIT_WINDOW` | No | Rate limit window in seconds (default `60`). |
| `RATE_LIMIT_STREAM_REQUESTS` | No | Media range requests per window per IP (default `600`). |
| `CACHE_TTL` | No | Search cache duration in seconds (default `1800` = 30 minutes). |
| `DATA_DIR` | No | Where the SQLite cache and wizard config live (default `./data`). Mount it as a volume to survive restarts. |
| `PORT` | No | Server port (default `7860`). |
| `AUTO_UPDATE` | No | `true`/`false` (Docker/Spaces). Automatically pulls the latest code on container restart. |
| `GITHUB_REPO_URL` | No | Custom fork URL to pull from when `AUTO_UPDATE=true`. |

*\* — "required" only if you skip the wizard. Configure via `/configure` and none of these are needed.*

---

## Deployment platform details

The details behind the table above, so you can pick with open eyes.

### Render

Free hobby tier, no card at signup. Two things to know: **sleep** and **bandwidth**. The container sleeps after 15 minutes idle and takes about a minute to wake — Stremio may show a connection error on that first play, just wait and retry. More importantly, free web services get a **5GB/month outbound cap**. When it's exhausted, the service pauses until the next month. For occasional personal use it works; for daily watching you'll hit the wall.

### Koyeb

Free tier, always on, no sleep — but card verification at signup (nothing gets charged). One free service per account. If you want a free instance that behaves like a real server, this is the one.

### Hugging Face Spaces

> [!WARNING]
> Docker Spaces now require a **paid PRO subscription**. The free tier only supports Gradio/Streamlit/static templates, so this is no longer the free home it used to be. Also note Hugging Face enforces its content rules aggressively on public Spaces, and video streaming tends to get flagged quickly.

If you do have PRO, setup is simple: fork this repo, create a Space with the **Docker** SDK, upload the files (or set up auto-update), add `ADDON_URL` as a secret and configure the rest in the wizard. Public or private Space, keep secrets in **Settings → Variables and secrets**, never in files. `AUTO_UPDATE=true` + `GITHUB_REPO_URL=<your fork>` makes the Space pull your latest code on every restart, which is handy for updates.

### Railway

Trial credits worth about 500 hours of runtime per month. When the credits run out, the service stops until you upgrade to a developer plan (card required, usage-based billing).

### Zeabur

Same idea as Railway — trial credits, then it stops. Fine for a short test.

### Heroku

Paid (Eco dynos from $5/month), but the most "deploy and forget" option: always on, fast, no sleep, no caps to think about. Use the deploy button above, or the [Colab deployer notebook](https://colab.research.google.com/github/SunilRoy-dev/stremio-telegram-debrid/blob/beta/deployment/colab/deploy_heroku.ipynb) if you want to do it from a browser form.

### Google Colab (temporary runs)

Free and surprisingly useful for testing. Install dependencies and run the server in the notebook, expose it via an ngrok tunnel (free [ngrok](https://ngrok.com) account for a token), and paste the URL into Stremio. The container dies after a few hours or when you close the tab, so it's a demo environment, not a home.

---

## Run it on your own machine

**With Python (3.10+):**

```bash
git clone https://github.com/SunilRoy-dev/stremio-telegram-debrid.git
cd stremio-telegram-debrid
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python addon.py
```

Then open `http://localhost:7860` (or better: `http://localhost:7860/configure`) and set things up. For Stremio to reach a local server from other devices, you'll need a tunnel (ngrok, cloudflared) or to keep everything on one device.

**With Docker Compose:**

```bash
docker-compose up --build
```

The `data/` folder is mounted as a volume, so your cache and wizard config survive container rebuilds.

**On a VPS with Traefik (Viren070's template):** if you run [Viren070/docker-compose-template](https://github.com/Viren070/docker-compose-template), drop in the pre-made compose file:

```bash
mkdir -p apps/stremio-telegram-debrid
curl -s https://raw.githubusercontent.com/SunilRoy-dev/stremio-telegram-debrid/main/deployment/vps/compose.yaml -o apps/stremio-telegram-debrid/compose.yaml
```

Then add `- apps/stremio-telegram-debrid/compose.yaml` under the `include:` section of the template's root compose file, make sure `addon` is in your `COMPOSE_PROFILES`, and run `docker compose up -d`. If you already run a global Watchtower container, delete the `stremio-telegram-debrid-updater` block from the compose file to avoid running two of them.

---

## Installing it in Stremio

### Method A — 1-Click Install via Landing Page (Easiest)
1. Open your deployed URL `https://your-deployed-url` in any web browser.
2. If you set an `API_KEY`, type it into the input box on the landing page.
3. Click **Install on Stremio** to open the Stremio desktop or mobile app directly, or click **Open in Web Stremio** if you watch from your browser.

### Method B — Manual Install
1. Open **Stremio** (desktop, mobile or web).
2. Go to the **Add-ons** section (puzzle piece icon).
3. In the search bar at the top, paste your manifest URL:
   - Without API key: `https://your-deployed-url/manifest.json`
   - With API key: `https://your-deployed-url/YOUR_API_KEY/manifest.json` (or `https://your-deployed-url/manifest.json?api_key=YOUR_API_KEY`)
4. Click **Install** when prompted.
5. In Stremio, open your Discover or Home screen. Your Telegram video catalogs and Continue Watching rows will appear automatically.
6. When playing a video matching your channel files, you'll see stream options labeled `▶ TG Play` or `▶ TG Channel` at the top of the streams panel.

### Troubleshooting: Nothing shows up?
- **Check file names:** The addon matches files by reading captions and clean names. Check the [naming guide](#naming-and-matching-guide) — e.g. `Video_Title_2023.1080p.mkv` or `Video_Title_S01E01.mkv`.
- **Check channel ID:** Private channels must start with `-100` (e.g. `-1001234567890`). See [how to find your channel ID](#one-time-setup-find-your-channel-id).
- **Check bot permissions:** If using a bot token instead of a user session, the bot *must* be added as an administrator in the private channel.
- **Clear cache:** If you uploaded new files recently, click **Clear persistent cache** in `/configure` or wait for `CACHE_TTL` (default 30 mins) to expire.
- **Check connection:** Open `/configure` and click **Test Telegram connection** to verify your credentials are alive and responding.

---

## Contributing

Bug reports, feature ideas and pull requests are all welcome. If something breaks, an Issue with your platform, what you played and what happened usually gets it fixed fastest.

## Credits

Built on top of some great open source:

- **[FastAPI](https://fastapi.tiangolo.com/)** and **[Uvicorn](https://www.uvicorn.org/)** — the web server doing the proxying.
- **[Pyrogram](https://github.com/pyrogram/pyrogram)** + **[tgcrypto](https://github.com/pyrogram/tgcrypto)** — the Telegram connection and its speed.
- **[Cinemeta](https://github.com/Stremio/stremio-cinemeta)** — Stremio's own metadata service, used as fallback.
- **[TMDB](https://www.themoviedb.org/)** — posters, titles and genre data. This product uses the TMDB API but is not endorsed or certified by TMDB.

## License

This project is licensed under a non-commercial license — see the [LICENSE](LICENSE) file. In short: use it, modify it, self-host it as much as you like, but don't sell it, rent it out, or strip the credits. If you fork it, keeping the attribution links (footer, manifest, console banner) intact is the only real ask. If you come across someone reselling it or passing it off as their own, reporting it to the hosting platform usually gets it sorted.

## Before you deploy

This addon streams files from the Telegram channels *you* configure. Please use it with content you own or have permission to stream — what gets hosted in your channels, and who you share your addon link with, is on you. Also give the terms of your hosting platform a skim so there are no surprises.


