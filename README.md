# Spotify Track Downloader Bot

A Telegram bot (built with [Kurigram](https://github.com/Kurigram)) that downloads
Spotify tracks and sends them as audio files. Downloaded tracks and users are stored
in **MongoDB** — when someone requests a track that's already in the database, the
bot re-sends the stored Telegram file instantly instead of downloading it again.

## How it works

1. A user sends a Spotify track link (e.g. `https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQy`)
   or a `spotify:track:…` URI.
2. The bot looks up the 22-character track ID in the `tracks` collection.
   - **Hit:** the stored Telegram `file_id` is sent back immediately — no re-download,
     no re-upload. Request counters are bumped.
   - **Miss:** the bot downloads the track with [spotdl](https://github.com/spotDL/spotify-downloader),
     uploads it to Telegram with the track's cover art as the audio thumbnail,
     and saves the metadata + `file_id` in MongoDB.
3. If two users request the same not-yet-cached track at the same time, an
   in-flight lock makes sure it's only downloaded once (the second request
   waits and then gets the cached file).

Users are upserted into the `users` collection on `/start` and on every request.

## Setup

### 1. Telegram credentials

- API ID / API HASH: https://my.telegram.org → API development tools
- Bot token: message [@BotFather](https://t.me/BotFather) on Telegram and create a **new** bot

### 2. MongoDB

Easiest is a free [MongoDB Atlas](https://www.mongodb.com/atlas) cluster:

1. Create a free cluster, add a database user, and allow your IP (or `0.0.0.0/0`).
2. Copy the connection string — it looks like
   `mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority`
3. Put it in `.env` as `MONGO_URI`.

Or run MongoDB locally with Docker: `docker run -d -p 27017:27017 mongo:7`
and keep the default `MONGO_URI=mongodb://localhost:27017`.

### 3. Run locally

```bash
cd spotify-dl-bot
cp example.env .env        # then fill in API_ID, API_HASH, BOT_TOKEN, MONGO_URI
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

`ffmpeg` must be installed on the machine (spotdl needs it to convert audio).

### 4. Run with Docker

```bash
docker build -t spotify-dl-bot .
docker run --env-file .env spotify-dl-bot
```

The image already includes ffmpeg. The same pattern works on Koyeb/Fly/Render —
it's a plain worker process, no ports needed.

## Commands

- `/start` — registers you and shows how to use the bot
- `/stats` — library size, total requests, your request count

Just paste a Spotify track link any time to download it.

## Collections

**tracks** — one document per Spotify track:

| field | purpose |
|---|---|
| `spotify_id` | 22-char Spotify track ID (unique index) |
| `title`, `artists`, `album`, `duration_sec` | metadata |
| `file_id` | Telegram file_id — this is what makes re-sends instant |
| `added_by`, `added_at` | who first requested it and when |
| `request_count`, `last_requested_at` | usage stats |

**users** — one document per Telegram user (`telegram_id` unique index) with
`username`, `first_name`, `joined_at`, `request_count`, `last_seen`.

## Notes

- spotdl matches each Spotify track to an audio source (typically YouTube Music)
  and downloads it — quality depends on the match. Use this for personal use and
  respect copyright in your region.
- Telegram bots can send audio files up to 50 MB. If a download exceeds that,
  the bot reports the failure instead of crashing.
- Telegram `file_id`s stay valid as long as the file exists on Telegram's
  servers; if one ever goes stale the bot falls back to re-downloading.
