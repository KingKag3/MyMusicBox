"""
MyMusicBox v2 - Backend
FastAPI server: local music library, playlists, streaming, tagging.

KEY RULES (from wiki):
- Never mount StaticFiles at "/" - use explicit GET / route
"""

import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from mutagen.mp3 import MP3
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, APIC
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
log = logging.getLogger("mymusicbox")

BASE_DIR = Path(__file__).parent.parent
MUSIC_DIR = BASE_DIR / "music_library"
COVERS_DIR = BASE_DIR / "covers"
DB_PATH = BASE_DIR / "mymusicbox.db"
CONFIG_PATH = BASE_DIR / "config.json"

MUSIC_DIR.mkdir(exist_ok=True)
COVERS_DIR.mkdir(exist_ok=True)

AUDIO_EXTS = {".mp3", ".ogg", ".flac", ".m4a", ".wav", ".aac", ".opus"}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db_init():
    conn = db_connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT    NOT NULL,
            artist        TEXT    DEFAULT 'Unknown Artist',
            album         TEXT    DEFAULT 'Unknown Album',
            duration      INTEGER DEFAULT 0,
            file_path     TEXT    NOT NULL UNIQUE,
            file_size     INTEGER DEFAULT 0,
            cover_path    TEXT,
            genre         TEXT    DEFAULT '',
            year          TEXT    DEFAULT '',
            play_count    INTEGER DEFAULT 0,
            added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS group_tracks (
            group_id  INTEGER REFERENCES groups(id) ON DELETE CASCADE,
            track_id  INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
            position  INTEGER DEFAULT 0,
            PRIMARY KEY (group_id, track_id)
        );

        CREATE TABLE IF NOT EXISTS playlists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            cover_path  TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
            track_id    INTEGER REFERENCES tracks(id)    ON DELETE CASCADE,
            position    INTEGER DEFAULT 0,
            PRIMARY KEY (playlist_id, track_id)
        );

        CREATE TABLE IF NOT EXISTS watched_folders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT NOT NULL UNIQUE,
            last_scan   TIMESTAMP,
            track_count INTEGER DEFAULT 0,
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


def row_dict(row) -> dict:
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config_file(data: dict):
    existing = load_config()
    existing.update(data)
    with open(CONFIG_PATH, "w") as f:
        json.dump(existing, f, indent=2)


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


def read_tags(path: Path) -> dict:
    """Read ID3 tags from an audio file. Cover art is read directly from the MP3."""
    meta = {"title": path.stem, "artist": "Unknown Artist",
            "album": "Unknown Album", "duration": 0, "genre": "", "year": "",
            "cover_path": None, "has_cover": False}
    if not MUTAGEN_OK:
        return meta
    try:
        audio = MP3(path)
        meta["duration"] = int(audio.info.length)
    except Exception:
        pass
    try:
        tags = EasyID3(path)
        meta["title"] = tags.get("title", [path.stem])[0]
        meta["artist"] = tags.get("artist", ["Unknown Artist"])[0]
        meta["album"] = tags.get("album", ["Unknown Album"])[0]
        meta["genre"] = tags.get("genre", [""])[0]
        meta["year"] = tags.get("date", [""])[0]
    except Exception:
        pass
    # Check if cover art exists in the MP3 (don't extract, just flag it)
    try:
        full = ID3(path)
        for tag in full.values():
            if isinstance(tag, APIC):
                meta["has_cover"] = True
                break
    except Exception:
        pass
    return meta


def extract_cover_from_mp3(file_path: Path) -> Optional[bytes]:
    """Extract raw cover art bytes from an MP3's ID3 tags."""
    if not MUTAGEN_OK:
        return None
    try:
        tags = ID3(file_path)
        for tag in tags.values():
            if isinstance(tag, APIC):
                return tag.data
    except Exception:
        pass
    return None


def write_tags(file_path: Path, metadata: dict) -> bool:
    """Write ID3 metadata to an MP3 file. Returns True on success."""
    if not MUTAGEN_OK:
        return False
    try:
        try:
            tags = EasyID3(file_path)
        except Exception:
            tags = EasyID3()
            tags.save(file_path)
            tags = EasyID3(file_path)

        if "title" in metadata and metadata["title"]:
            tags["title"] = metadata["title"]
        if "artist" in metadata and metadata["artist"]:
            tags["artist"] = metadata["artist"]
        if "album" in metadata and metadata["album"]:
            tags["album"] = metadata["album"]
        if "genre" in metadata and metadata["genre"]:
            tags["genre"] = metadata["genre"]
        if "year" in metadata and metadata["year"]:
            tags["date"] = metadata["year"]
        tags.save()
        return True
    except Exception as e:
        log.error(f"write_tags failed for {file_path}: {e}")
        return False


def write_cover_to_mp3(file_path: Path, cover_data: bytes, mime: str = "image/jpeg") -> bool:
    """Embed cover art into an MP3's ID3 tags."""
    if not MUTAGEN_OK:
        return False
    try:
        try:
            tags = ID3(file_path)
        except Exception:
            tags = ID3()
        # Remove existing covers
        tags.delall("APIC")
        tags.add(APIC(
            encoding=3,
            mime=mime,
            type=3,  # Cover (front)
            desc="Cover",
            data=cover_data,
        ))
        tags.save(file_path)
        return True
    except Exception as e:
        log.error(f"write_cover failed for {file_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()
    yield


app = FastAPI(title="MyMusicBox", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Library — tracks, artists, albums
# ---------------------------------------------------------------------------

@app.get("/api/tracks")
def list_tracks(search: str = "", artist: str = "", album: str = "",
                limit: int = 200, offset: int = 0):
    conn = db_connect()
    conds, params = [], []
    if search:
        conds.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ?)")
        params += [f"%{search}%"] * 3
    if artist:
        conds.append("t.artist = ?"); params.append(artist)
    if album:
        conds.append("t.album = ?"); params.append(album)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute(f"SELECT COUNT(*) FROM tracks t {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM tracks t {where} ORDER BY t.artist, t.album, t.title LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return {"tracks": [row_dict(r) for r in rows], "total": total}


@app.get("/api/tracks/{track_id}")
def get_track(track_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    return row_dict(row)


@app.patch("/api/tracks/{track_id}")
def update_track(track_id: int, body: dict):
    allowed = {"title", "artist", "album", "genre", "year"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields")
    conn = db_connect()
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE tracks SET {sets} WHERE id=?", list(updates.values()) + [track_id])
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: int, delete_file: bool = False):
    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        conn.close(); raise HTTPException(404)
    if delete_file:
        p = Path(row["file_path"])
        if p.exists():
            p.unlink()
    conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/artists")
def list_artists():
    conn = db_connect()
    rows = conn.execute("""
        SELECT artist,
               COUNT(*) as track_count,
               COUNT(DISTINCT album) as album_count,
               MAX(cover_path) as cover_path
        FROM tracks GROUP BY artist ORDER BY artist
    """).fetchall()
    conn.close()
    return {"artists": [row_dict(r) for r in rows]}


@app.get("/api/albums")
def list_albums():
    conn = db_connect()
    rows = conn.execute("""
        SELECT album, artist, COUNT(*) as track_count,
               MAX(cover_path) as cover_path
        FROM tracks GROUP BY album, artist ORDER BY artist, album
    """).fetchall()
    conn.close()
    return {"albums": [row_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Groups (custom groupings)
# ---------------------------------------------------------------------------

class GroupCreate(BaseModel):
    name: str
    description: str = ""


@app.get("/api/groups")
def list_groups():
    conn = db_connect()
    rows = conn.execute("""
        SELECT g.*, COUNT(gt.track_id) as track_count
        FROM groups g LEFT JOIN group_tracks gt ON g.id = gt.group_id
        GROUP BY g.id ORDER BY g.name
    """).fetchall()
    conn.close()
    return {"groups": [row_dict(r) for r in rows]}


@app.post("/api/groups")
def create_group(body: GroupCreate):
    conn = db_connect()
    cur = conn.execute("INSERT INTO groups (name, description) VALUES (?,?)", (body.name, body.description))
    conn.commit()
    gid = cur.lastrowid
    conn.close()
    return {"id": gid, "name": body.name}


@app.get("/api/groups/{gid}")
def get_group(gid: int):
    conn = db_connect()
    g = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    if not g:
        conn.close(); raise HTTPException(404)
    tracks = conn.execute("""
        SELECT t.* FROM tracks t JOIN group_tracks gt ON t.id = gt.track_id
        WHERE gt.group_id = ? ORDER BY gt.position, t.title
    """, (gid,)).fetchall()
    conn.close()
    return {**row_dict(g), "tracks": [row_dict(r) for r in tracks]}


@app.post("/api/groups/{gid}/tracks")
def add_to_group(gid: int, body: dict):
    track_id = body.get("track_id")
    conn = db_connect()
    max_pos = conn.execute("SELECT MAX(position) FROM group_tracks WHERE group_id=?", (gid,)).fetchone()[0] or 0
    conn.execute("INSERT OR IGNORE INTO group_tracks (group_id, track_id, position) VALUES (?,?,?)",
                 (gid, track_id, max_pos + 1))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/groups/{gid}/tracks/{tid}")
def remove_from_group(gid: int, tid: int):
    conn = db_connect()
    conn.execute("DELETE FROM group_tracks WHERE group_id=? AND track_id=?", (gid, tid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/groups/{gid}")
def delete_group(gid: int):
    conn = db_connect()
    conn.execute("DELETE FROM groups WHERE id=?", (gid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------

class PlaylistCreate(BaseModel):
    name: str
    description: str = ""


@app.get("/api/playlists")
def list_playlists():
    conn = db_connect()
    rows = conn.execute("""
        SELECT p.*, COUNT(pt.track_id) as track_count
        FROM playlists p LEFT JOIN playlist_tracks pt ON p.id = pt.playlist_id
        GROUP BY p.id ORDER BY p.created_at DESC
    """).fetchall()
    conn.close()
    return {"playlists": [row_dict(r) for r in rows]}


@app.post("/api/playlists")
def create_playlist(body: PlaylistCreate):
    conn = db_connect()
    cur = conn.execute("INSERT INTO playlists (name, description) VALUES (?,?)", (body.name, body.description))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return {"id": pid, "name": body.name}


@app.get("/api/playlists/{pid}")
def get_playlist(pid: int):
    conn = db_connect()
    pl = conn.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    if not pl:
        conn.close(); raise HTTPException(404)
    tracks = conn.execute("""
        SELECT t.*, pt.position FROM tracks t
        JOIN playlist_tracks pt ON t.id = pt.track_id
        WHERE pt.playlist_id = ? ORDER BY pt.position, t.title
    """, (pid,)).fetchall()
    conn.close()
    return {**row_dict(pl), "tracks": [row_dict(r) for r in tracks]}


@app.post("/api/playlists/{pid}/tracks")
def playlist_add_track(pid: int, body: dict):
    track_id = body.get("track_id")
    conn = db_connect()
    max_pos = conn.execute("SELECT MAX(position) FROM playlist_tracks WHERE playlist_id=?", (pid,)).fetchone()[0] or 0
    conn.execute("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?,?,?)",
                 (pid, track_id, max_pos + 1))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/playlists/{pid}/tracks/multi")
def playlist_add_multi(pid: int, body: dict):
    track_ids = body.get("track_ids", [])
    conn = db_connect()
    max_pos = conn.execute("SELECT MAX(position) FROM playlist_tracks WHERE playlist_id=?", (pid,)).fetchone()[0] or 0
    for i, tid in enumerate(track_ids):
        conn.execute("INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?,?,?)",
                     (pid, tid, max_pos + 1 + i))
    conn.commit()
    conn.close()
    return {"ok": True, "added": len(track_ids)}


@app.delete("/api/playlists/{pid}/tracks/{tid}")
def playlist_remove_track(pid: int, tid: int):
    conn = db_connect()
    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (pid, tid))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/playlists/{pid}")
def delete_playlist(pid: int):
    conn = db_connect()
    conn.execute("DELETE FROM playlists WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Streaming + cover art
# ---------------------------------------------------------------------------

@app.get("/api/stream/{track_id}")
async def stream_track(track_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(404, "File not on disk")
    return FileResponse(str(path), media_type="audio/mpeg", headers={"Accept-Ranges": "bytes"})


@app.get("/api/cover/{track_id}")
async def get_cover(track_id: int):
    """
    Serve cover art directly from the MP3's ID3 tags.
    If this track has no cover, look for an album-mate that does.
    """
    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)

    file_path = Path(row["file_path"])

    # Try this track first
    if file_path.exists():
        cover = extract_cover_from_mp3(file_path)
        if cover:
            from fastapi.responses import Response
            mime = "image/jpeg" if cover[:2] == b"\xff\xd8" else "image/png"
            return Response(content=cover, media_type=mime,
                          headers={"Cache-Control": "public, max-age=86400"})

    # Fall back to another track from the same album
    if row["album"] and row["album"] != "Unknown Album":
        conn = db_connect()
        mates = conn.execute(
            "SELECT file_path FROM tracks WHERE album=? AND artist=? AND id!=? LIMIT 10",
            (row["album"], row["artist"], track_id)
        ).fetchall()
        conn.close()
        for mate in mates:
            mp = Path(mate["file_path"])
            if mp.exists():
                cover = extract_cover_from_mp3(mp)
                if cover:
                    from fastapi.responses import Response
                    mime = "image/jpeg" if cover[:2] == b"\xff\xd8" else "image/png"
                    return Response(content=cover, media_type=mime,
                                  headers={"Cache-Control": "public, max-age=86400"})

    raise HTTPException(404)


@app.get("/api/albums-cover/{artist}/{album}")
async def get_album_cover(artist: str, album: str):
    """Get cover for an album by looking at any track in that album."""
    conn = db_connect()
    rows = conn.execute(
        "SELECT file_path FROM tracks WHERE artist=? AND album=? LIMIT 5",
        (artist, album)
    ).fetchall()
    conn.close()
    for row in rows:
        p = Path(row["file_path"])
        if p.exists():
            cover = extract_cover_from_mp3(p)
            if cover:
                from fastapi.responses import Response
                mime = "image/jpeg" if cover[:2] == b"\xff\xd8" else "image/png"
                return Response(content=cover, media_type=mime,
                              headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(404)


@app.get("/api/artist-image/{artist}")
async def get_artist_image(artist: str):
    """Return cover from the first track found for this artist."""
    conn = db_connect()
    rows = conn.execute(
        "SELECT file_path FROM tracks WHERE artist=? LIMIT 10", (artist,)
    ).fetchall()
    conn.close()
    for row in rows:
        p = Path(row["file_path"])
        if p.exists():
            cover = extract_cover_from_mp3(p)
            if cover:
                from fastapi.responses import Response
                mime = "image/jpeg" if cover[:2] == b"\xff\xd8" else "image/png"
                return Response(content=cover, media_type=mime,
                              headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(404)


# ---------------------------------------------------------------------------
# Metadata editing + cover art
# ---------------------------------------------------------------------------

class MetadataUpdate(BaseModel):
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    genre: Optional[str] = None
    year: Optional[str] = None


@app.patch("/api/tracks/{track_id}/metadata")
async def update_metadata(track_id: int, body: MetadataUpdate):
    """Update ID3 tags on the MP3 file and sync the DB record."""
    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)

    file_path = Path(row["file_path"])
    if not file_path.exists():
        conn.close()
        raise HTTPException(404, "File not on disk")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        conn.close()
        return {"ok": True, "message": "Nothing to update"}

    # Write to MP3
    ok = write_tags(file_path, updates)
    if not ok:
        conn.close()
        raise HTTPException(500, "Failed to write tags to file")

    # Sync DB
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE tracks SET {sets} WHERE id=?",
                 list(updates.values()) + [track_id])
    conn.commit()
    conn.close()
    log.info(f"Metadata updated for track {track_id}: {updates}")
    return {"ok": True, "updated": updates}


@app.post("/api/tracks/{track_id}/cover")
async def update_cover(track_id: int, file: UploadFile = File(...)):
    """
    Upload a new cover image, embed it into the MP3's ID3 tags.
    Also applies to all other tracks on the same album if requested.
    """
    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)

    file_path = Path(row["file_path"])
    if not file_path.exists():
        conn.close()
        raise HTTPException(404, "File not on disk")

    if not file.content_type.startswith("image/"):
        conn.close()
        raise HTTPException(400, "Must be an image file")

    cover_data = await file.read()
    mime = file.content_type

    # Write cover to this track
    ok = write_cover_to_mp3(file_path, cover_data, mime)
    if not ok:
        conn.close()
        raise HTTPException(500, "Failed to embed cover in MP3")

    conn.close()
    log.info(f"Cover updated for track {track_id}")
    return {"ok": True, "track_id": track_id}


@app.post("/api/tracks/{track_id}/cover/apply-to-album")
async def apply_cover_to_album(track_id: int):
    """Copy this track's embedded cover to all other tracks on the same album."""
    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)

    cover = extract_cover_from_mp3(Path(row["file_path"]))
    if not cover:
        conn.close()
        raise HTTPException(404, "This track has no embedded cover")

    mime = "image/jpeg" if cover[:2] == b"\xff\xd8" else "image/png"
    mates = conn.execute(
        "SELECT id, file_path FROM tracks WHERE album=? AND artist=? AND id!=?",
        (row["album"], row["artist"], track_id)
    ).fetchall()
    conn.close()

    applied = 0
    for mate in mates:
        p = Path(mate["file_path"])
        if p.exists() and write_cover_to_mp3(p, cover, mime):
            applied += 1

    log.info(f"Applied cover to {applied} album mates of track {track_id}")
    return {"ok": True, "applied_to": applied}


# ---------------------------------------------------------------------------
# iTunes Metadata Search
# ---------------------------------------------------------------------------

@app.get("/api/itunes/search")
async def itunes_search(artist: str = "", title: str = "", album: str = "", limit: int = 5):
    """
    Search iTunes for track metadata.
    Returns multiple results so user can pick the best match.
    Free API — no key needed.
    """
    import urllib.request, urllib.parse, json as _json

    # Build query — try artist + title first, fall back to broader
    query = f"{artist} {title}".strip() or album
    if not query:
        raise HTTPException(400, "Provide artist, title, or album")

    encoded = urllib.parse.quote(query)
    url = f"https://itunes.apple.com/search?term={encoded}&media=music&entity=song&limit={limit * 2}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MyMusicBox/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"iTunes API error: {e}")

    results = []
    for item in data.get("results", [])[:limit]:
        # Get hi-res artwork (replace 100x100 with 600x600)
        artwork = item.get("artworkUrl100", "").replace("100x100", "600x600")
        results.append({
            "track_id":    item.get("trackId"),
            "title":       item.get("trackName", ""),
            "artist":      item.get("artistName", ""),
            "album":       item.get("collectionName", ""),
            "genre":       item.get("primaryGenreName", ""),
            "year":        item.get("releaseDate", "")[:4] if item.get("releaseDate") else "",
            "track_num":   item.get("trackNumber"),
            "track_count": item.get("trackCount"),
            "duration_ms": item.get("trackTimeMillis", 0),
            "artwork_url": artwork,
            "artwork_100": item.get("artworkUrl100", ""),
            "preview_url": item.get("previewUrl", ""),
            "itunes_url":  item.get("trackViewUrl", ""),
            "explicit":    item.get("trackExplicitness") == "explicit",
        })

    return {"results": results, "count": len(results), "query": query}


@app.post("/api/itunes/apply-artwork")
async def itunes_apply_artwork(body: dict):
    """Download artwork from iTunes URL and embed it into the MP3."""
    import urllib.request
    track_id = body.get("track_id")
    artwork_url = body.get("artwork_url", "")
    if not track_id or not artwork_url:
        raise HTTPException(400, "track_id and artwork_url required")

    conn = db_connect()
    row = conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)

    try:
        req = urllib.request.Request(artwork_url, headers={"User-Agent": "MyMusicBox/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            cover_data = r.read()
            mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    except Exception as e:
        raise HTTPException(502, f"Failed to download artwork: {e}")

    ok = write_cover_to_mp3(Path(row["file_path"]), cover_data, mime)
    if not ok:
        raise HTTPException(500, "Failed to embed artwork in MP3")

    return {"ok": True, "bytes": len(cover_data)}


# ---------------------------------------------------------------------------
# YouTube Music Video Search
# ---------------------------------------------------------------------------

@app.get("/api/youtube/search")
async def youtube_search(artist: str, title: str):
    """
    Search YouTube for a music video via the YouTube Data API v3.
    Requires a user-supplied API key (set via /api/config/youtube-key) —
    there is no key-less fallback since scraping requires a backend that
    can hit youtube.com directly.
    """
    import urllib.request, urllib.parse, json as _json

    cfg = load_config()
    yt_key = cfg.get("youtube_api_key")
    if not yt_key:
        raise HTTPException(400, "No YouTube API key configured")

    query = f"{artist} {title} official music video"
    encoded = urllib.parse.quote(query)

    try:
        url = (f"https://www.googleapis.com/youtube/v3/search"
               f"?part=snippet&q={encoded}&type=video"
               f"&videoCategoryId=10&maxResults=5&key={yt_key}")
        with urllib.request.urlopen(url, timeout=8) as r:
            data = _json.loads(r.read())
        items = data.get("items", [])
        if items:
            item = items[0]
            vid_id = item["id"]["videoId"]
            return {
                "video_id": vid_id,
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "thumbnail": item["snippet"]["thumbnails"]["medium"]["url"],
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "source": "api"
            }
    except Exception as e:
        log.warning(f"YouTube API failed: {e}")

    raise HTTPException(404, "No YouTube video found")


@app.post("/api/config/youtube-key")
def save_youtube_key(body: dict):
    """Save YouTube Data API v3 key (required for the YouTube video lookup feature)."""
    key = body.get("key", "").strip()
    save_config_file({"youtube_api_key": key})
    return {"ok": True}


@app.post("/api/shutdown")
async def shutdown():
    """Stop the server. Works on Windows by killing the entire process tree."""
    import threading
    def _stop():
        import time, os, sys
        time.sleep(0.3)
        # Kill the whole process group including uvicorn reloader
        if sys.platform == "win32":
            import subprocess
            subprocess.Popen(f"taskkill /F /T /PID {os.getpid()}", shell=True)
        else:
            os._exit(0)
    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True, "message": "Server shutting down..."}


# ---------------------------------------------------------------------------
# Local Music — folder scan + file upload
# ---------------------------------------------------------------------------

@app.get("/api/local/folders")
def list_watched_folders():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM watched_folders ORDER BY added_at DESC").fetchall()
    conn.close()
    return {"folders": [row_dict(r) for r in rows]}


@app.post("/api/local/folders")
def add_watched_folder(body: dict):
    folder_path = body.get("path", "").strip()
    if not folder_path:
        raise HTTPException(400, "Path required")
    p = Path(folder_path)
    if not p.exists():
        raise HTTPException(400, f"Folder does not exist: {folder_path}")
    if not p.is_dir():
        raise HTTPException(400, f"Not a folder: {folder_path}")
    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO watched_folders (path) VALUES (?)", (str(p),))
    conn.commit()
    conn.close()
    return {"ok": True, "path": str(p)}


@app.delete("/api/local/folders/{folder_id}")
def remove_watched_folder(folder_id: int):
    conn = db_connect()
    conn.execute("DELETE FROM watched_folders WHERE id=?", (folder_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/local/scan/{folder_id}")
async def scan_folder(folder_id: int, bg: BackgroundTasks):
    conn = db_connect()
    row = conn.execute("SELECT * FROM watched_folders WHERE id=?", (folder_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    bg.add_task(_do_scan_folder, folder_id, row["path"])
    return {"ok": True, "scanning": row["path"]}


@app.post("/api/local/scan-all")
async def scan_all_folders(bg: BackgroundTasks):
    conn = db_connect()
    rows = conn.execute("SELECT * FROM watched_folders").fetchall()
    conn.close()
    for row in rows:
        bg.add_task(_do_scan_folder, row["id"], row["path"])
    return {"ok": True, "scanning": len(rows)}


async def _do_scan_folder(folder_id: int, folder_path: str):
    """Walk folder recursively, read ID3 tags, insert new tracks into library."""
    conn = db_connect()
    added = 0
    skipped = 0
    p = Path(folder_path)
    if not p.exists():
        log.warning(f"Scan: folder not found: {folder_path}")
        conn.close()
        return

    for file_path in sorted(p.rglob("*")):
        if file_path.suffix.lower() not in AUDIO_EXTS:
            continue
        # Check if already in library
        existing = conn.execute("SELECT id FROM tracks WHERE file_path=?", (str(file_path),)).fetchone()
        if existing:
            skipped += 1
            continue
        try:
            meta = read_tags(file_path)
            conn.execute("""
                INSERT OR IGNORE INTO tracks
                  (title, artist, album, duration, file_path, file_size,
                   cover_path, genre, year)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (meta["title"], meta["artist"], meta["album"], meta["duration"],
                  str(file_path), file_path.stat().st_size,
                  meta.get("cover_path"), meta["genre"], meta["year"]))
            conn.commit()
            added += 1
        except Exception as e:
            log.warning(f"Scan: failed to add {file_path}: {e}")

    conn.execute("""
        UPDATE watched_folders
        SET last_scan=CURRENT_TIMESTAMP, track_count=track_count+?
        WHERE id=?
    """, (added, folder_id))
    conn.commit()
    conn.close()
    log.info(f"Scan complete: {folder_path} — {added} added, {skipped} skipped")


@app.post("/api/local/upload-file")
async def upload_file(file: UploadFile = File(...)):
    if not any(file.filename.lower().endswith(ext) for ext in AUDIO_EXTS):
        raise HTTPException(400, f"Not a supported audio file: {file.filename}")
    safe = sanitize(file.filename)
    dest = MUSIC_DIR / safe
    counter = 1
    while dest.exists():
        stem = Path(safe).stem
        suffix = Path(safe).suffix
        dest = MUSIC_DIR / f"{stem}_{counter}{suffix}"
        counter += 1
    file_bytes = await file.read()
    dest.write_bytes(file_bytes)
    meta = read_tags(dest)
    conn = db_connect()
    cur = conn.execute("""
        INSERT OR IGNORE INTO tracks
          (title, artist, album, duration, file_path, file_size,
           cover_path, genre, year)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (meta["title"], meta["artist"], meta["album"], meta["duration"],
          str(dest), dest.stat().st_size,
          meta.get("cover_path"), meta["genre"], meta["year"]))
    conn.commit()
    track_id = cur.lastrowid
    conn.close()
    log.info(f"Uploaded: {file.filename} -> {dest}")
    return {"ok": True, "track_id": track_id, "title": meta["title"], "artist": meta["artist"]}


# ---------------------------------------------------------------------------
# Frontend — CRITICAL: explicit route, NOT StaticFiles at "/"
# See wiki: 05-Bugs-and-Fixes/Bug Log#Static Files Mount
# ---------------------------------------------------------------------------

FRONTEND_DIR = BASE_DIR / "frontend"


@app.get("/")
def serve_root():
    idx = FRONTEND_DIR / "index.html"
    if not idx.exists():
        raise HTTPException(404, f"Frontend not found at {idx}")
    return FileResponse(str(idx))


assets_dir = FRONTEND_DIR / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8765, reload=True)
