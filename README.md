# MyMusicBox

A self-hosted music library and player you run on your own machine. It pulls
tracks in from a Telegram music bot, your local folders, or drag-and-drop
uploads, tags and organizes everything into one library, and streams it back
through a browser UI — playlists, artists, albums, custom groups, and cover
art included.

There's no cloud service, no account system, and no external database. Your
library lives in a single SQLite file and your audio files stay on your disk
exactly where you put them.

## How it works

- **FastAPI backend** (`backend/main.py`) manages the SQLite library, talks to
  Telegram via [Telethon](https://docs.telethon.dev/), reads/writes ID3 tags
  with [Mutagen](https://mutagen.readthedocs.io/), and streams audio to the
  frontend.
- **Single-page frontend** (`frontend/index.html`) is a self-contained
  HTML/CSS/JS app — no build step, no framework, just one file served
  directly by the backend.
- **`start.py`** installs backend dependencies and launches everything on
  `http://localhost:8765`.

## Features

- **Telegram search & download** — connect your Telegram account to a music
  bot/channel, search for tracks, and download matches straight into your
  library (multi-select supported).
- **Local library management** — watch folders on disk and auto-scan them for
  new audio, or drag-and-drop files directly into the browser.
- **Library, Artists, and Albums views** — auto-grouped from track metadata,
  with search/filter across your whole collection.
- **Playlists & custom groups** — build and reorder playlists; group tracks
  however you like outside the playlist model.
- **Tag editing** — edit title/artist/album/genre/year per track, pull
  metadata from iTunes, and fetch/apply cover art per track or per album.
- **Streaming playback** — tracks stream on demand via range-request-friendly
  endpoints, no local copies needed in the browser.
- **Download history** — track what's been pulled in from Telegram and when.

## Getting started

**Requirements:** Python 3.10+.

```bash
python start.py
```

This installs the backend dependencies from `backend/requirements.txt` and
starts the server. Open **http://localhost:8765** in your browser.

### Connecting Telegram (optional)

Telegram search/download is opt-in — the rest of the app (local folders,
uploads, playlists, playback) works without it. To enable it:

1. Get an `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org).
2. Open the Telegram setup panel in the app (sidebar pill, top-left) and enter
   your `api_id`, `api_hash`, phone number, and the bot/channel to search.
3. Verify with the code Telegram sends you (plus 2FA password, if enabled).

This writes a local `config.json` and a Telethon session file
(`tg_session*`) — both hold live credentials/session tokens and are
gitignored, so they never get committed.

## Project structure

```
backend/         FastAPI app, SQLite schema, Telegram + tagging logic
frontend/        Single-file browser UI
music_library/   Your audio files live here (gitignored)
covers/          Extracted/fetched cover art (gitignored)
mymusicbox.db    SQLite library database (gitignored, generated on first run)
start.py         Installs deps and launches the server
```

## Notes

- Supported audio formats: MP3, FLAC, OGG, M4A, WAV, AAC, Opus.
- Nothing here is published anywhere — it's a local app that happens to run
  in a browser tab.
