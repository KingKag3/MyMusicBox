# MyMusicBox

A Spotify-style music player for your own local files — no server, no
account, no cloud. Point it at MP3s on your computer and it becomes a full
library: playlists, artists, albums, custom groups, tag editing, a
graphical equalizer, and a Now Playing panel with YouTube video lookup.

It's a single static HTML file. There's nothing to install and nothing
running in the background — your library, playlists, and audio files never
leave your browser. The same file works two ways:

- **Locally**, via `python start.py` — for offline use or editing tags
  in place on disk.
- **Publicly**, via GitHub Pages — open the hosted version from any device
  and point it at your own files. Nobody else ever sees them; nothing is
  uploaded anywhere.

## How it works

Everything lives in `frontend/index.html`:

- **IndexedDB** stores your library, playlists, and groups in the browser.
- **The File System Access API** (Chrome/Edge) lets you pick a folder on
  disk and keep it "watched" — MyMusicBox reads files live and can write
  ID3 tag edits straight back to them. Firefox/Safari and one-off
  drag-and-drop uploads fall back to storing the file bytes directly in
  IndexedDB instead (works everywhere, just without live disk access).
- **A hand-rolled ID3v2 reader/writer** handles MP3 tags and embedded cover
  art — no server-side library needed. FLAC/OGG/M4A/WAV/AAC play fine but
  only get filename-based titles for now (tag reading/writing for those is
  a possible future addition).
- **iTunes's public search API** and, if you supply your own free API key,
  **YouTube Data API v3** are called directly from the browser for metadata
  and music-video lookup.

No backend, no database server, no build step — just open the file.

## Features

- **Local library** — pick a folder (auto-scans for new files) or
  drag-and-drop individual files in.
- **Library, Artists, and Albums views** — auto-grouped from tags, with
  search/filter across your collection.
- **Playlists & custom groups** — build playlists; group tracks however you
  like outside the playlist model.
- **Tag editing** — edit title/artist/album/genre/year (MP3: written back to
  the actual file), pull metadata from iTunes, and set/apply cover art per
  track or per album.
- **Playback** — full player with queue, shuffle, repeat, and a real-time
  graphical equalizer.
- **Now Playing panel** — cover art, "more from this artist," up-next queue,
  and an optional YouTube video lookup (needs your own free API key).

## Getting started

**Local install** — requires Python 3.10+, nothing else:

```bash
python start.py
```

Opens **http://localhost:8765**. (`start.py` is just a static file server —
the File System Access API needs a real `http://` origin, which is why this
exists instead of double-clicking `index.html` directly.)

**Public / hosted** — open the GitHub Pages URL for this repo. Same app,
same file, works identically. Folder access still requires re-granting
permission once per browser session (a browser security requirement, not a
bug) since sites can't silently access your filesystem across visits.

## Project structure

```
frontend/index.html          The entire app — UI, storage, tagging, playback
start.py                     Trivial local static server (no dependencies)
.github/workflows/pages.yml  Deploys frontend/ to GitHub Pages on push
```

## Notes

- Folder watching + write-back tag edits: Chrome/Edge only (File System
  Access API). Other browsers get file-picker/drag-and-drop with
  in-browser storage instead.
- Supported audio formats: MP3, FLAC, OGG, M4A, WAV, AAC, Opus.
- Nothing here is uploaded to any server — your library is exactly as
  private on the hosted version as it is running locally.
