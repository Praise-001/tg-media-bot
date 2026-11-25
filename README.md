# Telegram Media Downloader Bot

Send the bot a link from Instagram, TikTok, YouTube, Facebook, X/Twitter, Reddit, Pinterest, Threads and more. It downloads the video or images and sends them back, fast.

**How it stays fast**
- Prefers pre-merged MP4 formats (no ffmpeg merge step) and downloads fragments 8 at a time.
- Remembers what it already sent: a repeated link is re-sent from Telegram's servers instantly, with no download.
- Handles several links and users at once (`MAX_CONCURRENT`).
- Carousels/albums are sent as a single album (up to 10 items per album).

**Stack:** Python 3.12, [aiogram 3](https://docs.aiogram.dev), [yt-dlp](https://github.com/yt-dlp/yt-dlp) (video), [gallery-dl](https://github.com/mikf/gallery-dl) (image-post fallback), ffmpeg.

---

## 1. Reuse your existing bot (no new bot needed)

1. Open **@BotFather** in Telegram → `/mybots` → pick your bot → **API Token** → **Revoke current token**.
2. Copy the new token. The bot keeps its username, its chats, and its users; only the token changes, and the old (broken) deployment stops working, which is what you want.

## 2. Create the new repo

```bash
cd tg-media-bot
git init && git add . && git commit -m "Rebuild media downloader bot"
# create an empty repo on GitHub, then:
git remote add origin git@github.com:<you>/<repo>.git
git push -u origin main
```

`.env` and `data/` are git-ignored, so your token never gets committed.

## 3. Run it

### Docker (recommended, works on any VPS)

```bash
cp .env.example .env      # paste BOT_TOKEN
mkdir -p data
docker compose up -d --build
docker compose logs -f    # you should see "Starting @yourbot"
```

### Vercel Daemons (beta)

`vercel.json` already declares the bot as a daemon (a long-running container built from `worker/Dockerfile`).

1. Push the repo to GitHub and import it as a new project on Vercel (no framework, no build command).
2. In **Project Settings → Environment Variables**, add `BOT_TOKEN` (and any other variables from `.env.example`) for **Production**.
3. Deploy to **production**. Daemons don't run in preview deployments.
4. Open the project's **Logs**; you should see `Starting @yourbot`.

Things to know:
- **Hobby plan quota:** 420 GB-hours of memory per month. The bot is configured for 1 GiB, which runs out after roughly 17 days of continuous running. Vercel then stops the daemon until the 1st of the next month (UTC). For a bot that must stay up all month, use Pro.
- Hobby also has a 5-hour monthly Active CPU quota, which downloads consume.
- No persistent storage: the repeat-link cache resets on every restart or redeploy.
- Region is `fra1` (Frankfurt) in `vercel.json`. Change it there if Vercel rejects it or you want another.
- Vercel restarts the process if it crashes. Check [Vercel's Daemons docs](https://vercel.com/docs/daemons) for current limits, since this feature is in beta.

### Without Docker

```bash
# needs Python 3.10+ and ffmpeg installed
cd worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env      # paste BOT_TOKEN into worker/.env
python -m app.main
```

Railway, Render, Fly.io and similar hosts: deploy from the repo using `worker/Dockerfile` (build context: `worker/`) and set the variables from `.env.example` in their dashboard. Run it as a long-running worker (not a web service).

---

## Settings (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `BOT_TOKEN` | - | **Required.** From @BotFather |
| `MAX_FILE_MB` | 50 | Largest file to send. 50 MB is Telegram's cloud limit |
| `MAX_CONCURRENT` | 4 | Parallel downloads |
| `DOWNLOAD_TIMEOUT` | 120 | Seconds before giving up |
| `ALLOWED_USER_IDS` | empty | Comma-separated user IDs; empty means everyone |
| `COOKIES_FILE` | empty | Cookies for login-gated posts (see below) |
| `AUTO_UPDATE` | 1 | Update yt-dlp/gallery-dl on each container start |
| `ASK_FORMAT` | 1 | 1 = Video/Audio pop-up after each link; 0 = send video straight away |
| `TELEGRAM_API_URL` | empty | Local Bot API server URL (files up to 2000 MB) |

## Video or audio

When someone sends a link, the bot replies with a small pop-up: **🎬 Video** or **🎵 Audio**. Nothing is downloaded until they tap one, then the bot sends that and removes the pop-up.
- **Skip the question:** put `audio` or `mp3` in the message (`audio https://...` or `/audio https://...`) and the bot sends only the sound straight away, without downloading the video.
- **Audio** comes as a playable track (title, uploader and length filled in), M4A or MP3, within the same 50 MB limit as video. Repeats are instant (cached).
- **Several links in one message** get one pop-up each.
- SwissTransfer links skip the pop-up, since files have no audio to extract; they are sent as before.
- **Photo posts:** the pop-up can't know in advance whether a link is a video or photos. Tapping **Video** on a photo post sends the photos; tapping **Audio** on one says the audio couldn't be fetched.
- Pop-ups are kept in memory, so after a restart or redeploy an old pop-up says "That button has expired. Please send the link again." Each pop-up works once.
- In groups, a link also brings up the pop-up, and anyone in the group can tap it. Failed requests the person tapped for always get an error reply.
- **Prefer instant videos?** Set `ASK_FORMAT=0`. The bot then sends the video straight away with an **Audio only** button under it (single videos only).
- `/stats` has an **Audio requests** line. An audio request also counts in "Posts delivered".

## SwissTransfer and Vecteezy

**SwissTransfer** (`swisstransfer.com/d/...` links): the bot has its own downloader for these (`worker/app/swisstransfer.py`), because yt-dlp doesn't support the site.
- Photos and videos are sent as photos/videos, everything else (zip, pdf, ...) as documents. A transfer with several files sends up to 10 of them.
- **Telegram's 50 MB bot limit applies.** Files above `MAX_FILE_MB` are skipped, and if nothing fits the bot says how big the transfer is. SwissTransfer is normally used for big files, so for larger ones run a local Telegram Bot API server and set `TELEGRAM_API_URL` and `MAX_FILE_MB` (up to 2000).
- Password-protected transfers, expired links and used-up download limits give a clear message instead of a download.
- SwissTransfer has no official public API. This uses the same requests as an open-source client, so if SwissTransfer changes its site, this is the one file to update. The tests run against a fake server, so they can't catch that.

**Vecteezy** (`vecteezy.com/video/...` links): the bot lets these through to the general downloader, which looks for a video on the page. This has not been tested against the live site.
- Vecteezy has no download API, and its full-quality files are behind an account and a download button, so you may only get what the public page shows (often a preview).
- Vecteezy's licence is personal: free files need attribution, Pro files need the downloader's own paid seat, and sharing or redistributing files isn't allowed. Check that your use of the bot fits those terms.

## Usage stats (`/stats`)

The bot keeps a small SQLite file of who uses it and how much. Only the Telegram user IDs listed in `ADMIN_IDS` can read the report; anyone else gets no reply.

1. Send `/id` to the bot to see your numeric Telegram user ID.
2. Set `ADMIN_IDS=<that number>` (several admins: separate with commas) and restart or redeploy.
3. Send `/stats`. You get: people who've used it, active today / last 7 days, posts delivered (and today), a count per site, failed requests, and `/start` presses. Days are UTC.

Where the numbers are kept (`STATS_DB_PATH`):
- **Docker / VPS:** `/data/stats.db`, stored in the `./data` folder, so counts survive restarts and rebuilds.
- **Vercel Daemons:** storage there is temporary, so the counts reset whenever the bot restarts or you redeploy. Use a normal host, or add a hosted database, if you need long-term history.
- **Local run:** `worker/data/stats.db`.

A person counts as a user when they send a link or message the bot privately. Chatter in groups is not counted. Only the numeric user ID and the time of use are stored, not names or message text.

## Things worth knowing

- **Instagram and Facebook are the usual troublemakers.** Many of their posts now require a logged-in session, and they often block datacenter IPs. If a link works on your laptop but not on the server, export cookies from a browser where you're logged in (a "Get cookies.txt" extension, Netscape format), save them as `data/cookies.txt`, and set `COOKIES_FILE=/data/cookies.txt`. Use a throwaway account, not your main one.
  - **Instagram photo posts** (and carousels) are fetched by gallery-dl, which almost always needs this login. Videos usually work without it, which is why "videos work, images don't" is the typical symptom.
  - **On Vercel there is no folder to put `cookies.txt` in.** Turn the file into text and put that in an environment variable instead: in PowerShell run `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt")) | Set-Clipboard`, then paste it as `COOKIES_B64` in Vercel (mark it Sensitive) and redeploy. Export only instagram.com's cookies so the value stays small.
  - Cookies expire after a while; when photo posts start failing again, export a fresh file.
  - To see why a link failed, search the logs for `no media for` and `gallery-dl got nothing`.
- **Keep yt-dlp fresh.** Most "it suddenly stopped working" problems are fixed by a newer yt-dlp. With `AUTO_UPDATE=1`, just `docker compose restart`.
- **50 MB limit.** Longer videos are retried at lower quality; if still too big, the bot says so. For bigger files, run a local Bot API server and set `TELEGRAM_API_URL` and `MAX_FILE_MB`.
- **Groups:** add the bot to a group; it replies to links there and stays quiet on failures. If it doesn't see messages, disable privacy mode with @BotFather → `/setprivacy` → Disable.
- Only a built-in list of social domains is accepted by default (this also blocks requests aimed at internal addresses). Set `ALLOW_ANY_DOMAIN=true` to lift that.

## Project layout

```
vercel.json        Vercel Daemons config
docker-compose.yml run on any VPS with Docker
worker/
  app/
    config.py       settings from env
    downloader.py   link detection, yt-dlp + gallery-dl
    main.py         Telegram handlers, sending, file_id cache, audio button
    stats.py        usage stats (SQLite)
    swisstransfer.py SwissTransfer downloader
  tests/            pytest suite (URLs, stats, SwissTransfer, audio, startup)
  Dockerfile, entrypoint.sh, requirements.txt
```

Only download content you have the right to save, and respect each platform's terms.
