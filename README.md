# Telegram Stremio Addon

![Telegram Stremio Addon Banner](stremio_telegram_banner.png)

[![License](https://img.shields.io/badge/License-MIT--NC-blue?style=for-the-badge)](LICENSE)
[![GitHub Stars](https://img.shields.io/github/stars/SunilRoy-dev/stremio-telegram-debrid?style=for-the-badge&logo=github)](https://github.com/SunilRoy-dev/stremio-telegram-debrid/stargazers)
[![GitHub Forks](https://img.shields.io/github/forks/SunilRoy-dev/stremio-telegram-debrid?style=for-the-badge)](https://github.com/SunilRoy-dev/stremio-telegram-debrid/network/members)


Stream video, audio, and subtitle files directly from your private Telegram storage channels inside Stremio. This addon serves as a high-speed on-the-fly streaming HTTP proxy (fully supporting Range Requests for instant seek/scrubbing) that integrates your private Telegram channel into your personal Stremio library.

### Why I built this
I store my personal media files on a private Telegram channel. I wanted a way to play them directly on my TV through Stremio without paying for Debrid links or downloading the files first. Other tools I found required setting up complex external databases like MongoDB, so I wrote this lightweight, database-free Python script to serve as a fast streaming proxy with subtitle loading and instant skipping.

Contributions and bug reports are welcome! If you encounter issues, feel free to open a GitHub Issue, or submit a Pull Request with your improvements. All pull requests will be reviewed and merged accordingly.

> [!NOTE]
> **Show Your Support!** ⭐
> If you find this project useful, please **leave a star on the repository** before you fork, clone, or deploy it. Your stars help keep this project active and maintained!

---

## One-Click Deploy & Setup Options

Deploy your own instance of the Telegram Stremio Addon instantly using any of the services below:

| Platform | Deployment Type / Limitations | Deploy Button |
| :--- | :--- | :--- |
| **Hugging Face Spaces** | Free CPU Tier (Highly Recommended — Generous Bandwidth / Sleeps after 48h) | [Manual Setup Guide](#hugging-face-spaces-setup-guide) |
| **Render** | Free Hobby Tier (5GB Bandwidth Limit & Auto-Sleeps) | [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/SunilRoy-dev/stremio-telegram-debrid) |
| **Koyeb** | Free Edge Tier (Continuous — Requires Card Verification) | [![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&repository=github.com/SunilRoy-dev/stremio-telegram-debrid&branch=main&name=stremio-telegram-debrid) |
| **Railway** | Trial Tier (Limited Credits, approx. 500 hours/month) | [![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template?template=https://github.com/SunilRoy-dev/stremio-telegram-debrid) |
| **Zeabur** | Trial Tier (Limited Credits) | [![Deploy on Zeabur](https://zeabur.com/button.svg)](https://zeabur.com/templates/deploy?template=https://github.com/SunilRoy-dev/stremio-telegram-debrid) |

*Please read the **[Deployment Platform Specs & Limitations](#deployment-platform-specs--limitations)** section below before selecting a hosting provider.*

---

## Key Features

- **Direct Catalog Browsing & Search**: Search files directly inside Stremio or browse the latest 50 files sent to your Telegram channel.
- **Metadata & Catalog Syncing**: Open entries in Stremio; the addon checks your Telegram channel for matching file names and links them as stream sources.
- **Stitched Split Streaming**: Automatically groups, merges, and streams multi-part file archives (such as `.001`, `.part1` patterns) as one continuous virtual stream.
- **Smart Segment Filtering**: Intelligently parses naming patterns and number sequences (e.g. Part 1, Part 2, V1, V2) from filenames to retrieve and stream only the exact segmented file requested.
- **Subtitle Auto-Mapping**: Automatically scans your channel for matching subtitle files (SRT, VTT, ASS), injects them, and auto-detects English, Spanish, and French tracks.
- **High-Speed Range Proxy**: Supports HTTP `206 Partial Content` streaming, enabling instant scrub/seek (fast-forwarding/rewinding) on players like ExoPlayer, VLC, and MPV.
- **Zero-Storage Footprint**: Streams files chunk-by-chunk in memory directly from Telegram DCs. No temporary server storage is consumed.
- **Secure Access Control**: Protects your endpoints using an optional API key query (`?api_key=...`) to prevent unauthorized access.
- **Custom Logging**: Log streaming activity directly back to a separate private Telegram channel.

---

## Stitched Split Streaming

If you have large media files (e.g., 4K HDR video backups) that exceed Telegram's file upload limits (2GB for bots, 4GB for user accounts), you can split them into smaller segments before uploading. The addon automatically detects, groups, and stitches them back together into a single virtual stream.

### Supported Split Formats
The addon parses standard split archive conventions including:
* **Numeric extensions**: `Video.mkv.001`, `Video.mkv.002`, `Video.mkv.003`...
* **Part indicators**: `Video.part1.rar`, `Video.part2.rar`, `Video.part3.rar`... (or `.part01.mkv`, `.part02.mkv`...)
* **Suffix delimiters**: `Video_part_1.mp4`, `Video_part_2.mp4`...

### How It Works Under the Hood
1. **Aggregation**: The catalog handler parses filename patterns and clusters split files together, presenting them as a single item with their total combined file size (e.g., `Stitch stream | 6.2 GB`).
2. **Dynamic Range Mapping**: When you press play or seek in Stremio, the addon maps the player's byte-range requests to the respective split files on the fly.
3. **In-Memory Sequential Access**: It downloads only the necessary segments from Telegram DCs and transitions between split messages seamlessly in memory, resulting in uninterrupted playback.

---

## System Architecture

The diagram below shows how the addon behaves as a range-supported streaming proxy between Stremio and Telegram:

```mermaid
graph TD
    User([Stremio Player]) -->|1. Stream Request with Range Header| Addon[FastAPI Addon Server]
    Addon -->|2. Check Cache / Fetch Message| TGClient[Pyrogram Client]
    TGClient -->|3. Get Media Stream Block| TGDC[Telegram Data Center]
    TGDC -->|4. Return Media Bytes| TGClient
    TGClient -->|5. Forward Bytes Chunk-by-Chunk| Addon
    Addon -->|6. Return HTTP 206 Partial Content| User
    
    subgraph Hosting Environment
        Addon
        TGClient
    end
    
    subgraph Telegram Network
        TGDC
    end
```

---

## Configuration Environment Variables

Configure these settings in your deployment dashboard or local `.env` file:

| Variable | Required | Description |
| :--- | :---: | :--- |
| `API_ID` | **Yes** | Your Telegram API ID from [my.telegram.org](https://my.telegram.org). |
| `API_HASH` | **Yes** | Your Telegram API Hash from [my.telegram.org](https://my.telegram.org). |
| `TELEGRAM_CHANNEL_ID` | **Yes** | Comma-separated list of private/public channel IDs or usernames (e.g. -1001234567890, @my_channel). |
| `BOT_TOKEN` | **Conditional** | Bot Token from `@BotFather` (required if `USER_SESSION_STRING` is not configured). |
| `USER_SESSION_STRING` | **Conditional** | Pyrogram Session String (highly recommended to bypass bot limits, see details below). |
| `API_KEY` | No | Add a secret key (e.g. `mykey123`) to secure your addon endpoint with `?api_key=mykey123`. |
| `ADDON_URL` | **Yes** | The public HTTP URL where your server is deployed (e.g. `https://myaddon.onrender.com`). |
| `LOG_CHANNEL_ID` | No | Telegram channel ID where play/stream logs are recorded. |
| `TIMEZONE` | No | Timezone for logs (e.g., `Asia/Kolkata`, `UTC+05:30`). Defaults to `UTC`. |
| `CACHE_TTL` | No | Cache duration in seconds for searches (default: `1800` [30 mins]). |

---

## Telegram Credentials: Bot vs. User Sessions

You can run this addon using either a standard Telegram Bot Token or a Pyrogram User Session String. Review the differences below:

### 1. Telegram Bot (Bot Token)
- **Drawback/Limit**: Telegram imposes a strict **2GB size limit** on all files uploaded/downloaded by bots. Any file in your channel larger than 2GB **will fail to stream**.
- **Setup**: Must make the bot an **Administrator** in your private channel so it has permissions to search channel history.

### 2. User Client (User Session String)
- **Benefit**: Bypasses the bot limit, allowing you to stream files up to **4GB** (the maximum file size for all standard Telegram accounts).
- **Setup**: Needs only standard member access to private channels.

> [!CAUTION]
> **Security Warning regarding `USER_SESSION_STRING`**
> A Pyrogram User Session String grants **complete access** to your Telegram account. Anyone who acquires this string can read, write, or delete messages in your personal chats and channels.
> - **Never** hardcode this string in files or push it to public repositories.
> - **Only** enter it as a secure secret environment variable on trusted hosting platforms (Render, Koyeb, Railway, etc.).
> - **Always** generate the session string on your trusted local computer.

### How to Generate a `USER_SESSION_STRING` Locally

Run the following command in your terminal to safely generate and export your session string:

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

---

## Configuring Channels (Private & Public)

You can configure the addon to index media from multiple channels (both private and public).

## Channel Formats in 'TELEGRAM_CHANNEL_ID'
* **Private Channels**: Use their numeric IDs (e.g. -1001234567890).
* **Public Channels**: Use their public usernames with or without the '@' symbol (e.g. '@public_channel' or 'public_channel').
* **Multi-Channel Configuration**: Separate them with commas (e.g. 'TELEGRAM_CHANNEL_ID=-1001234567890, @my_public_channel, other_public_channel').

### Access & Membership Requirements
* **Standard Telegram Bot ('BOT_TOKEN')**: The bot **must** be added to the channel as a member or administrator so it has permission to query and read chat history.
* **User Session Client ('USER_SESSION_STRING')**: The user account must be joined or subscribed to the channels so Pyrogram can search and resolve the files.

### Recommended Limits
While the config accepts any number of channels, it is highly recommended to limit your list to **5 to 10 channels max**. 
* **Performance**: The addon queries each channel sequentially. Too many channels will cause Stremio to timeout (expecting responses in 3-5 seconds).
* **Telegram Rate Limits**: Searching across too many channels simultaneously may trigger Telegram's 'FloodWait' warnings.

---

## Deployment Platform Specs & Limitations

Read these limitations carefully to choose the hosting platform that best fits your requirements:

### 1. Hugging Face Spaces

Hugging Face Spaces is the recommended hosting platform as it provides fast networking, stable CPU environments, and does not require credit card verification.

* **Drawbacks & Security Warnings**:
  - **Generous Bandwidth**: Unlike Render's strict 5GB limit, Hugging Face does not enforce a rigid monthly bandwidth quota on free Spaces. This makes it the highly preferred platform for streaming video backups without hitting quota limits.
  - **Public Repos Only**: Free Spaces must be configured as **Public** to run. Private spaces require a paid subscription. Because your Space is public, **never upload your `.env` file to the files section**. Instead, add your configuration keys in your Space **Settings > Variables and Secrets** as secrets.
  - **⚠️ Illegal Activity Termination Policy**: Hugging Face strictly enforces its Acceptable Use Policy. Hosting copyrighted or unauthorized media files for public streaming will lead to **immediate Space deletion, permanent account termination, and potential legal notices/liability** from content owners. Only stream video files you legally own or have permission to access.
  - **Auto-Sleep**: Auto-sleeps after **48 hours** of inactivity. However, it wakes up within **10-15 seconds** of a new request, which is significantly faster than Render.

#### Hugging Face Spaces Setup Guide

1. **Create a Hugging Face Account**: Visit [Hugging Face](https://huggingface.co/) and click **Sign Up** to create a free account.
2. **Create a New Space**: Click your profile picture in the top-right corner and select **New Space** (or go directly to [huggingface.co/new-space](https://huggingface.co/new-space)).
   - **Space Name**: Choose a name (e.g. `my-stremio-addon`).
   - **License**: Choose `mit` or leave blank.
   - **Select the Space SDK**: Click **Docker**.
   - **Docker Template**: Select **Blank** (do not select template options like Gradio or Streamlit).
   - **Space Visibility**: Set to **Public** (required for the free tier; private spaces require a paid subscription).
   - Click **Create Space**.
3. **Configure Environment Secrets**: Click the **Settings** tab in the top menu of your newly created Space. Scroll down to the **Variables and secrets** section and click **New secret** to add each of the following variables:
   - `API_ID`
   - `API_HASH`
   - `BOT_TOKEN` (or `USER_SESSION_STRING`)
   - `TELEGRAM_CHANNEL_ID`
   - `API_KEY` (highly recommended to secure your public Space endpoint!)
   - `ADDON_URL`: Set this to `https://<your-username>-<your-space-name>.hf.space` (you can find this URL by clicking "Embed this Space" in the top-right menu of your Space page).
4. **Push the Files**: Clone your Hugging Face Space repository locally (git commands are shown on the Space's homepage). Copy all files from this project (except `.env` and `.git` folders) into your cloned Space folder. Commit and push the changes:
   ```bash
   git add .
   git commit -m "feat: deploy addon"
   git push
   ```
   Hugging Face will automatically detect the `Dockerfile`, build the container, and deploy your addon. Once the status turns to **Running**, your addon is live!

### 2. Render
- **Cost**: Hobby/Free Tier. No credit card required at signup.
- **Drawbacks**: 
  - **⚠️ Bandwidth Limit (Strict 5GB/Month Outbound Limit)**: Render imposes a strict **5 GB limit** of free outbound bandwidth per month for web service apps (unlike static sites which get 100GB). Since video streaming is data-intensive, **you will hit this 5GB limit almost immediately**. If you exceed it without a credit card/billing configured, **Render will temporarily deactivate your service addon** (it will not ban your personal Render billing account, but the streaming proxy will stop working until the next billing cycle starts or you upgrade).
  - **Auto-Sleep**: The container spins down/goes to sleep after **15 minutes of inactivity**. If you haven't used Stremio for a while, opening a video will trigger a wakeup request. The container will take **1 to 2 minutes** to build/spin up, causing Stremio to show a connection error initially. Simply wait 60 seconds and try playing again.

### 3. Koyeb
- **Cost**: Free Tier. **Requires card verification at signup** (even though you won't be charged).
- **Drawbacks**:
  - The container stays continuously active (no auto-sleep), but you must verify your identity with a valid credit card during registration.
  - Limited to 1 free service per organization.

### 4. Railway
- **Cost**: Trial Tier. Provides $5 free credits (approx. 500 hours of continuous runtime per month).
- **Drawbacks**:
  - The service will run out of hours and stop working before the end of the month unless you upgrade to a developer account (which requires a card and charges on usage).

### 5. Zeabur
- **Cost**: Trial Tier. Limited credits.
- **Drawbacks**:
  - Similar to Railway, has a limited free trial tier or resource caps.

---

## Local Installation & Setup

### Prerequisites
- Python 3.10 or higher.
- System compiler tools (for Pyrogram C extensions - `tgcrypto`):
  - **Windows**: Build Tools for Visual Studio.
  - **Linux**: `build-essential libssl-dev python3-dev`
  - **macOS**: Xcode Command Line Tools.

### Option A: Python Setup
1. Clone the repository:
   ```bash
   git clone https://github.com/SunilRoy-dev/stremio-telegram-debrid.git
   cd stremio-telegram-debrid
   ```
2. Create a virtual environment and activate it:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt tgcrypto
   ```
4. Create a `.env` file in the root folder using your credentials (refer to the [Configuration Variables](#configuration-environment-variables) section).
5. Run the server:
   ```bash
   python addon.py
   ```
   The landing configuration page will be accessible at `http://localhost:7860`.

### Option B: Docker Compose
Build and start the container using Docker Compose:
```bash
docker-compose up --build
```

---

## How to Install in Stremio

1. Deploy the addon publicly (or run it locally with tunnel software like Ngrok).
2. Copy your addon manifest URL (e.g., `https://your-addon-domain.com/manifest.json?api_key=mykey`).
3. Open **Stremio** (Desktop, Mobile, or Web).
4. Go to **Add-ons**, paste the URL into the search bar, and click **Install**.
5. Search for your video backups in Stremio. If matching files exist in your Telegram channel, you will see the stream option labeled `▶ TG Play` or `▶ TG Channel` at the top of the streams panel!

---

## Contributing

Contributions, bug reports, and suggestions are highly welcome!
- **Report Issues**: If you find bugs or want to request features, please open a GitHub Issue.
- **Submit Pull Requests**: Feel free to fork the repository, make improvements, and submit a Pull Request. All pull requests will be reviewed and merged to improve the project.

---

## Built With & Credits

This project is made possible thanks to the following open-source frameworks, libraries, and APIs:

- **[FastAPI](https://fastapi.tiangolo.com/)**: High-performance, easy-to-use Python web framework for building the addon routes.
- **[Pyrogram](https://github.com/pyrogram/pyrogram)**: Elegant, modern, and asynchronous Telegram MTProto API framework, powering our connection to Telegram channels.
- **[tgcrypto](https://github.com/pyrogram/tgcrypto)**: High-speed C-extension for Pyrogram cryptography requirements to ensure smooth streaming.
- **[Uvicorn](https://www.uvicorn.org/)**: Lightning-fast ASGI web server implementation.
- **[Cinemeta API](https://github.com/Stremio/stremio-cinemeta)**: Stremio's default metadata provider, enabling the addon to query and match filenames.

---

## License, Attribution & Stars

### MIT Non-Commercial License (MIT-NC)
This project is licensed under a custom **MIT Non-Commercial License (MIT-NC)** - see the [LICENSE](LICENSE) file for details. Copyright (c) 2026 SunilRoy.

Sublicensing, commercial sale, renting, or financial/monetary exploitation of this software (including its source code and derivatives) is **strictly prohibited**.

### What happens if someone violates the license or removes attribution?
By hosting public code, you are protected by copyright laws. If someone forks or copies this repository and removes your attribution/links, sells/monetizes the software, or uses it in violation of the non-commercial terms, **you have the legal right to file a DMCA Takedown Notice**. 

GitHub, Render, Koyeb, and other major platforms take copyright violations very seriously. Filing a formal DMCA notice through their portals will result in their repository, fork, or hosted service being **disabled or taken down** within 24 hours.

### Attribution Requirement
If you fork, copy, modify, or redistribute this project:
1. You **must** keep the original credits back to [SunilRoy-dev](https://github.com/SunilRoy-dev).
2. Do **not** remove the developed-by credits or links from the web landing page footer, manifest metadata, or startup console banner.
3. Please **star the repository** as a sign of appreciation.

---

## Educational Disclaimer

> [!WARNING]
> This software is created solely for **educational, personal backup, and research purposes**. The author (`SunilRoy`) does not condone, promote, or encourage copyright infringement or the unauthorized streaming/sharing of copyrighted media. 
> - Users are solely responsible for the media files they host in their private Telegram channels.
> - By deploying or running this software, you agree that you are using it in compliance with all local copyright laws and terms of service.
