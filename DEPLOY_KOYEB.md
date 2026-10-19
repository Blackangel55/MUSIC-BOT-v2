# Deploying on Koyeb

## 1. MongoDB first

The bot needs a reachable MongoDB. Easiest is a free
[MongoDB Atlas](https://www.mongodb.com/atlas) cluster:

1. Create a free cluster and a database user.
2. Network Access → allow `0.0.0.0/0` (Koyeb's outbound IPs change, so you
   can't whitelist a single IP).
3. Copy the connection string:
   `mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority`

## 2. Push this repo to GitHub

Push the project to your `music-bot` repository (the `Dockerfile` at the
repo root is what Koyeb builds).

## 3. Create the Koyeb service

1. Koyeb dashboard → **Create Service** → **GitHub**, select the repo.
2. **Builder:** Dockerfile.
3. **Service type:** Worker if your plan offers it. If only **Web** is
   available, that's fine too — the bot automatically opens an HTTP
   health-check endpoint (`/`, `/health`, `/healthz` → 200) on Koyeb's
   `$PORT`, so the TCP/HTTP health check passes either way.
4. **Region:** Singapore is closest to India.
5. **Instance:** Nano is enough for a bot; ffmpeg conversion of a normal
   track is light.

## 4. Environment variables

Set these in the service's environment (mark the secrets as secret):

| Variable | Value |
|---|---|
| `API_ID` | from https://my.telegram.org |
| `API_HASH` | from https://my.telegram.org |
| `BOT_TOKEN` | from @BotFather (secret) |
| `MONGO_URI` | Atlas connection string (secret) |
| `MONGO_DB` | `spotify_dl_bot` (optional) |
| `MAX_CONCURRENT_DOWNLOADS` | `2` (optional) |
| `BITRATE` | `320k` (optional) |

Do **not** set `PORT` yourself — Koyeb injects it for Web services.

## 5. Deploy and verify

Deploy, then watch the logs. You should see, in order:

```
health check server listening on 0.0.0.0:8000   (Web service only)
connected to MongoDB database spotify_dl_bot
spotdl ready (free Spotify client)
bot started — send it a Spotify track link
```

Then send your bot a Spotify track link on Telegram. The first download
takes a while (spotdl matching + ffmpeg); repeats are instant from MongoDB.

## Notes

- Downloads go to the container's temp dir and are deleted after upload —
  nothing persists on disk, the library lives in MongoDB.
- If the service restarts (redeploy, maintenance), in-flight downloads are
  lost but nothing else: already-cached tracks stay in MongoDB.
