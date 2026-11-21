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
| `TELEGRAM_API_URL` | empty | Local Bot API server URL (files up to 2000 MB) |

## Things worth knowing

- **Instagram and Facebook are the usual troublemakers.** Many of their posts now require a logged-in session, and they often block datacenter IPs. If a link works on your laptop but not on the server, export cookies from a browser where you're logged in (a "Get cookies.txt" extension, Netscape format), save them as `data/cookies.txt`, and set `COOKIES_FILE=/data/cookies.txt`. Use a throwaway account, not your main one.
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
    main.py         Telegram handlers, sending, file_id cache
  tests/            URL parsing tests (pytest)
  Dockerfile, entrypoint.sh, requirements.txt
```

Only download content you have the right to save, and respect each platform's terms.
