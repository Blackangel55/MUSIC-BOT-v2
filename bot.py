#!/usr/bin/env python3
"""
Spotify track downloader Telegram bot (Kurigram + MongoDB).

Send the bot a Spotify track link and it replies with the audio file.
Every downloaded track is stored in MongoDB along with its Telegram
file_id — if the same track is requested again, the bot re-sends the
stored file_id instantly instead of downloading it again.

Requires: API_ID, API_HASH, BOT_TOKEN in the environment (.env supported).
Optional: MONGO_URI, MONGO_DB, MAX_CONCURRENT_DOWNLOADS, BITRATE,
           SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (for spotdl's
           official Spotify API instead of its free client).
"""

import asyncio
import io
import logging
import os
import re
import shutil
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
# Kurigram is a drop-in Pyrogram fork, so the import name stays `pyrogram`.
from pyrogram import Client, filters, idle
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

# ---------------------------------------------------------------- config

for _var in ("API_ID", "API_HASH", "BOT_TOKEN"):
    if not os.environ.get(_var):
        raise SystemExit(f"missing {_var} in environment (see example.env)")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.environ.get("MONGO_DB", "spotify_dl_bot")
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
MAX_TRACKS_PER_MESSAGE = int(os.environ.get("MAX_TRACKS_PER_MESSAGE", "5"))
BITRATE = os.environ.get("BITRATE", "320k")
AUDIO_FORMAT = "mp3"
# Telegram Bot API limit for sending audio
MAX_TG_AUDIO_BYTES = 50 * 1024 * 1024

# Matches https://open.spotify.com/track/<id>, with optional /intl-xx/ prefix,
# and spotify:track:<id> URIs. Spotify IDs are always 22 chars.
TRACK_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?track/|spotify:track:)([A-Za-z0-9]{22})"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("spotdl-bot")

# ---------------------------------------------------------------- globals

mongo: AsyncIOMotorClient
db = None
users = None
tracks = None

spotdl_client = None  # spotdl.Spotdl, for metadata search (init once)
download_sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Per-track locks so two simultaneous requests for the same uncached track
# only trigger one download; the loser re-checks the DB afterwards.
_inflight: dict[str, asyncio.Lock] = {}
_inflight_guard = asyncio.Lock()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def redact_uri(uri: str) -> str:
    return re.sub(r"://[^@]*@", "://***@", uri)


def make_caption(title: str, artists: list[str], album: str | None) -> str:
    lines = [f"\U0001f3b5 {title}", f"\U0001f464 {', '.join(artists)}"]
    if album:
        lines.append(f"\U0001f4bd {album}")
    return "\n".join(lines)


# ---------------------------------------------------------------- database

async def init_db() -> None:
    global mongo, db, users, tracks
    mongo = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=10_000)
    try:
        await mongo.admin.command("ping")
    except Exception as exc:
        log.error("cannot reach MongoDB at %s: %s", redact_uri(MONGO_URI), exc)
        raise SystemExit(1)
    db = mongo[MONGO_DB]
    users = db["users"]
    tracks = db["tracks"]
    await tracks.create_index("spotify_id", unique=True)
    await users.create_index("telegram_id", unique=True)
    log.info("connected to MongoDB database %s", MONGO_DB)


async def upsert_user(tg_user) -> None:
    """Insert or refresh a user record (identity fields only)."""
    await users.update_one(
        {"telegram_id": tg_user.id},
        {
            "$set": {
                "username": tg_user.username,
                "first_name": tg_user.first_name,
                "last_seen": utcnow(),
            },
            "$setOnInsert": {"joined_at": utcnow(), "request_count": 0},
        },
        upsert=True,
    )


async def bump_user_requests(tg_user_id: int, n: int = 1) -> None:
    await users.update_one(
        {"telegram_id": tg_user_id}, {"$inc": {"request_count": n}}
    )


# ---------------------------------------------------------------- spotdl

def init_spotdl() -> None:
    """Create the single shared Spotdl instance (initializes Spotify client)."""
    global spotdl_client
    from spotdl import Spotdl

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    spotdl_client = Spotdl(
        client_id=client_id,
        client_secret=client_secret,
        # Without credentials spotdl falls back to its free (unofficial)
        # Spotify client; with credentials it uses the official API.
        use_official_api=bool(client_id and client_secret),
        downloader_settings={"format": AUDIO_FORMAT, "bitrate": BITRATE},
    )
    log.info(
        "spotdl ready (%s Spotify client)",
        "official" if (client_id and client_secret) else "free",
    )


def fetch_cover(song, tmpdir: Path) -> Path | None:
    """
    Download the track's cover art and shrink it to Telegram's audio
    thumbnail size (320x320 JPEG). Returns the path, or None on failure.
    """
    url = getattr(song, "cover_url", None)
    if not url:
        return None
    try:
        from PIL import Image

        with urllib.request.urlopen(url, timeout=20) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((320, 320))
        cover_path = tmpdir / "cover.jpg"
        img.save(cover_path, "JPEG", quality=85)
        return cover_path
    except Exception:
        log.warning("could not fetch cover art for %s", song.song_id, exc_info=True)
        return None


def fetch_and_download(url: str, tmpdir: Path):
    """
    Blocking: resolve the Spotify URL to a Song and download it into tmpdir.
    Runs in a worker thread. Returns (song, path_to_audio, path_to_cover).
    """
    from spotdl.download.downloader import Downloader

    songs = spotdl_client.search([url])
    if not songs:
        raise RuntimeError("track not found on Spotify")
    song = songs[0]

    # A dedicated Downloader per download so concurrent downloads don't
    # clobber each other's output path. {track-id} is the Spotify track ID,
    # so the file name is predictable: <spotify_id>.mp3
    downloader = Downloader(
        settings={
            "output": str(tmpdir / "{track-id}.{output-ext}"),
            "format": AUDIO_FORMAT,
            "bitrate": BITRATE,
            "overwrite": "force",
        }
    )
    _, path = downloader.download_song(song)
    if path is None or not Path(path).exists():
        raise RuntimeError("download produced no audio file")
    cover_path = fetch_cover(song, tmpdir)
    return song, Path(path), cover_path


# ---------------------------------------------------------------- handlers
# NOTE: handlers are plain functions, registered in main() via add_handler().
# They must NOT use @app.on_message decorators at module level: pyrogram's
# decorator path touches the event loop at import time, which pins the client
# to a dead loop and crashes app.start() with "attached to a different loop"
# when the program later runs under asyncio.run().


async def cmd_start(client: Client, message: Message):
    if message.from_user:
        await upsert_user(message.from_user)
    await message.reply_text(
        "\U0001f3b5 Send me a Spotify track link and I'll download it for you.\n\n"
        "Tracks are saved to a library — if you (or anyone) asks for the same "
        "track again, I send it instantly without re-downloading.\n\n"
        "Use /stats to see the library size."
    )


async def cmd_stats(client: Client, message: Message):
    total_tracks = await tracks.count_documents({})
    agg = await tracks.aggregate(
        [{"$group": {"_id": None, "n": {"$sum": "$request_count"}}}]
    ).to_list(1)
    total_requests = agg[0]["n"] if agg else 0
    mine = 0
    if message.from_user:
        doc = await users.find_one({"telegram_id": message.from_user.id})
        mine = (doc or {}).get("request_count", 0)
    await message.reply_text(
        f"\U0001f4da tracks in library: {total_tracks}\n"
        f"\u2b07\ufe0f total requests served: {total_requests}\n"
        f"\U0001f464 your requests: {mine}"
    )


async def get_inflight_lock(spotify_id: str) -> asyncio.Lock:
    async with _inflight_guard:
        return _inflight.setdefault(spotify_id, asyncio.Lock())


async def send_cached_track(client: Client, message: Message, doc: dict) -> None:
    """Re-send a track using the file_id stored in MongoDB."""
    await message.reply_audio(
        audio=doc["file_id"],
        caption=make_caption(doc["title"], doc["artists"], doc.get("album")),
        title=doc["title"],
        performer=", ".join(doc["artists"]),
        duration=doc.get("duration_sec") or 0,
    )
    await tracks.update_one(
        {"spotify_id": doc["spotify_id"]},
        {"$inc": {"request_count": 1}, "$set": {"last_requested_at": utcnow()}},
    )


async def download_and_send(
    client: Client, message: Message, spotify_id: str, url: str
) -> None:
    status = await message.reply_text("\u2b07\ufe0f downloading…")
    tmpdir = Path(tempfile.mkdtemp(prefix="spotdl_"))
    try:
        async with download_sem:
            song, path, cover_path = await asyncio.to_thread(
                fetch_and_download, url, tmpdir
            )

        if path.stat().st_size > MAX_TG_AUDIO_BYTES:
            await status.edit_text(
                "\u274c this track is over Telegram's 50 MB bot limit, can't send it."
            )
            return

        await status.edit_text("\u2b06\ufe0f uploading…")
        sent = await message.reply_audio(
            audio=str(path),
            caption=make_caption(song.name, song.artists, song.album_name),
            title=song.name,
            performer=", ".join(song.artists),
            duration=int(song.duration or 0),
            thumb=str(cover_path) if cover_path else None,
        )

        await tracks.update_one(
            {"spotify_id": spotify_id},
            {
                "$set": {
                    "title": song.name,
                    "artists": list(song.artists),
                    "album": song.album_name,
                    "duration_sec": int(song.duration or 0),
                    "file_id": sent.audio.file_id,
                    "file_unique_id": sent.audio.file_unique_id,
                    "last_requested_at": utcnow(),
                },
                "$setOnInsert": {
                    "added_by": message.from_user.id if message.from_user else None,
                    "added_at": utcnow(),
                    "request_count": 0,
                },
                "$inc": {"request_count": 1},
            },
            upsert=True,
        )
        log.info("cached new track %s (%s)", spotify_id, song.name)
        await status.delete()
    except Exception as exc:
        log.exception("download failed for %s", spotify_id)
        await status.edit_text(f"\u274c couldn't download this track ({exc}).")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def handle_track(client: Client, message: Message, spotify_id: str) -> None:
    url = f"https://open.spotify.com/track/{spotify_id}"

    # Fast path: already in the library.
    doc = await tracks.find_one({"spotify_id": spotify_id})
    if doc and doc.get("file_id"):
        try:
            await send_cached_track(client, message, doc)
            return
        except Exception:
            # Stored file_id went stale (or the file is gone) — fall through
            # and re-download, then overwrite it.
            log.warning("cached file_id for %s failed, re-downloading", spotify_id)
            await tracks.update_one(
                {"spotify_id": spotify_id}, {"$unset": {"file_id": ""}}
            )

    # Slow path: serialize concurrent requests for the same track, then
    # re-check the DB — the winner downloads, everyone else gets the cache.
    async with await get_inflight_lock(spotify_id):
        doc = await tracks.find_one({"spotify_id": spotify_id})
        if doc and doc.get("file_id"):
            await send_cached_track(client, message, doc)
            return
        await download_and_send(client, message, spotify_id, url)


async def handle_links(client: Client, message: Message):
    ids = TRACK_RE.findall(message.text or "")
    if not ids:
        return
    if message.from_user:
        await upsert_user(message.from_user)

    # De-dupe while preserving order, cap per message.
    unique_ids = list(dict.fromkeys(ids))[:MAX_TRACKS_PER_MESSAGE]
    if len(ids) > len(unique_ids):
        await message.reply_text(
            f"\u2139\ufe0f taking the first {MAX_TRACKS_PER_MESSAGE} tracks."
        )
    if message.from_user:
        await bump_user_requests(message.from_user.id, len(unique_ids))

    for spotify_id in unique_ids:
        await handle_track(client, message, spotify_id)


# ---------------------------------------------------------------- health check

def start_health_server(port: int):
    """
    Minimal HTTP server so PaaS health checks (e.g. Koyeb web services)
    see a healthy port. Runs in a daemon thread; the bot itself still
    uses polling and needs no incoming connections.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = "HealthBot/1.0"

        def _respond(self, include_body: bool):
            body = b'{"status":"ok","service":"spotify-dl-bot"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def _handle(self, include_body: bool):
            if self.path in ("/", "/health", "/healthz"):
                self._respond(include_body)
            else:
                self.send_error(404)

        def do_GET(self):
            self._handle(include_body=True)

        def do_HEAD(self):
            self._handle(include_body=False)

        def log_message(self, *args):
            log.debug("health: %s", args[0] % args[1:])

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="health-server"
    )
    thread.start()
    log.info("health check server listening on 0.0.0.0:%d", port)


# ---------------------------------------------------------------- main

async def main() -> None:
    # Bind the health-check port first so the platform sees us as alive
    # even while Telegram/MongoDB are still connecting. Koyeb sets PORT
    # automatically for Web services; with a Worker service (or locally)
    # PORT is unset and the server is skipped.
    port = os.environ.get("PORT")
    if port:
        try:
            start_health_server(int(port))
        except ValueError:
            log.warning("PORT=%r is not a number, skipping health server", port)

    await init_db()
    await asyncio.to_thread(init_spotdl)

    # Create the client and register handlers HERE, inside the running loop.
    # (See the note above the handlers: doing it at module level pins the
    # client to a dead import-time event loop.)
    app = Client("spotdl-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    app.add_handler(
        MessageHandler(cmd_start, filters.command("start") & filters.private)
    )
    app.add_handler(
        MessageHandler(cmd_stats, filters.command("stats") & filters.private)
    )
    app.add_handler(
        MessageHandler(
            handle_links, filters.text & ~filters.command(["start", "stats"])
        )
    )

    await app.start()
    log.info("bot started — send it a Spotify track link")
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
