"""
MyMusicBox v2 - Backend
FastAPI server: Telegram bot integration (autonomous), music library, playlists, streaming.

KEY RULES (from wiki):
- Never mount StaticFiles at "/" - use explicit GET / route
- Bot interaction is invisible to frontend: backend handles all pagination automatically
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from telethon import TelegramClient
    from telethon.tl.types import MessageMediaDocument, DocumentAttributeAudio, DocumentAttributeFilename
    from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
    TELETHON_OK = True
except ImportError:
    TELETHON_OK = False

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
SESSION_PATH = str(BASE_DIR / "tg_session")

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
            tg_msg_id     INTEGER,
            tg_channel    TEXT,
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

        CREATE TABLE IF NOT EXISTS downloads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_msg_id   INTEGER,
            tg_channel  TEXT,
            filename    TEXT,
            title       TEXT,
            artist      TEXT,
            status      TEXT    DEFAULT 'queued',
            progress    INTEGER DEFAULT 0,
            error       TEXT,
            track_id    INTEGER,
            source      TEXT    DEFAULT 'soundcloud',
            started_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
# Telegram client
# ---------------------------------------------------------------------------

_tg_client: Optional[object] = None


# Auth state machine — tracks where we are in the login flow
_auth_state = {
    "step": "idle",      # idle | waiting_code | waiting_2fa | connected | error
    "phone_hash": None,  # returned by send_code
    "error": None,
}


async def get_tg_client() -> "TelegramClient":
    global _tg_client
    if not TELETHON_OK:
        raise HTTPException(503, "telethon not installed")
    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash"):
        raise HTTPException(400, "Telegram credentials not configured")
    if _tg_client is None or not _tg_client.is_connected():
        _tg_client = TelegramClient(SESSION_PATH, int(cfg["api_id"]), cfg["api_hash"])
        await _tg_client.connect()
    if not await _tg_client.is_user_authorized():
        raise HTTPException(401, "not_authorized")
    return _tg_client


async def _get_raw_client() -> "TelegramClient":
    """Get connected client without auth check — used during login flow."""
    global _tg_client
    cfg = load_config()
    if _tg_client is None or not _tg_client.is_connected():
        _tg_client = TelegramClient(SESSION_PATH, int(cfg["api_id"]), cfg["api_hash"])
        await _tg_client.connect()
    return _tg_client


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


def is_audio_doc(doc, fname_attr) -> bool:
    if doc.mime_type and "audio" in doc.mime_type:
        return True
    if fname_attr and Path(fname_attr.file_name).suffix.lower() in AUDIO_EXTS:
        return True
    return False


def extract_audio_info(msg, channel: str) -> Optional[dict]:
    if not msg or not msg.media or not isinstance(msg.media, MessageMediaDocument):
        return None
    doc = msg.media.document
    attrs = {type(a).__name__: a for a in doc.attributes}
    fname_attr = attrs.get("DocumentAttributeFilename")
    audio_attr = attrs.get("DocumentAttributeAudio")
    if not is_audio_doc(doc, fname_attr):
        return None
    filename = fname_attr.file_name if fname_attr else f"track_{msg.id}.mp3"
    return {
        "msg_id": msg.id,
        "channel": channel,
        "filename": filename,
        "title": (audio_attr.title if audio_attr and audio_attr.title else Path(filename).stem),
        "artist": (audio_attr.performer if audio_attr and audio_attr.performer else ""),
        "duration": (audio_attr.duration if audio_attr else 0),
        "size": doc.size,
        "date": msg.date.isoformat() if msg.date else None,
    }


# ---------------------------------------------------------------------------
# Bot search — fully autonomous, invisible to frontend
# ---------------------------------------------------------------------------

async def _wait_messages(client, channel, after_id: int, timeout: float = 20) -> list:
    """Poll for new messages appearing after `after_id`. Returns when silent for 2.5s or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    last_id = after_id
    seen: set = set()
    collected = []
    first_reply_at = None

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1.2)
        msgs = await client.get_messages(channel, min_id=last_id, limit=30)
        for m in reversed(msgs or []):
            if m.id in seen:
                continue
            seen.add(m.id)
            last_id = max(last_id, m.id)
            collected.append(m)
            if first_reply_at is None:
                first_reply_at = asyncio.get_event_loop().time()
        if first_reply_at and asyncio.get_event_loop().time() - first_reply_at >= 2.5:
            break
    return collected


def _parse_numbered_list(text: str) -> list[dict]:
    """Parse '1. Artist - Title' lines from bot reply text."""
    items = []
    for line in (text or "").split("\n"):
        m = re.match(r"^(\d+)[.)]\s+(.+)$", line.strip())
        if m:
            num = int(m.group(1))
            label = m.group(2).strip()
            parts = label.split(" - ", 1)
            items.append({
                "num": num,
                "label": label,
                "artist": parts[0].strip() if len(parts) > 1 else "",
                "title": parts[1].strip() if len(parts) > 1 else label,
            })
    return items


def _get_buttons(msg) -> list[dict]:
    """Extract inline keyboard buttons from a message."""
    buttons = []
    if not hasattr(msg, "reply_markup") or not msg.reply_markup:
        return buttons
    if not hasattr(msg.reply_markup, "rows"):
        return buttons
    for row_idx, row in enumerate(msg.reply_markup.rows):
        for btn in (row.buttons or []):
            data = ""
            btn_type = "callback"
            if hasattr(btn, "data") and btn.data:
                try:
                    data = btn.data.decode() if isinstance(btn.data, bytes) else str(btn.data)
                except Exception:
                    data = str(btn.data)
            elif hasattr(btn, "url") and btn.url:
                data = btn.url
                btn_type = "url"
            buttons.append({"text": getattr(btn, "text", ""), "data": data, "type": btn_type, "row": row_idx})
    return buttons


async def _press_button(client, channel, msg_id: int, btn_data: str, fast: bool = False) -> list:
    """
    Press a callback button and return new messages that follow.
    fast=True: short wait, used when pressing many track buttons in sequence.
    fast=False: full settle window, used for pagination.
    """
    entity = await client.get_entity(channel)
    try:
        data_bytes = btn_data.encode() if isinstance(btn_data, str) else btn_data
        await client(GetBotCallbackAnswerRequest(peer=entity, msg_id=msg_id, data=data_bytes))
    except Exception as e:
        log.warning(f"Callback answer failed (non-fatal): {e}")
    if fast:
        return await _wait_one_message(client, channel, msg_id)
    return await _wait_messages(client, channel, msg_id)


async def _wait_one_message(client, channel, after_id: int, timeout: float = 8) -> list:
    """
    Wait for a single new message after after_id.
    Returns as soon as one arrives (or after timeout).
    Used when pressing track buttons — bot posts one audio message per press.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last_id = after_id
    seen: set = set()
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.8)
        msgs = await client.get_messages(channel, min_id=last_id, limit=5)
        for m in reversed(msgs or []):
            if m.id in seen:
                continue
            seen.add(m.id)
            if m.id > after_id:
                return [m]  # return immediately on first new message
    return []


async def _send_and_wait(client, channel, text: str, timeout: float = 20) -> list:
    sent = await client.send_message(channel, text)
    msgs = await _wait_messages(client, channel, sent.id, timeout)
    return sent.id, msgs


def _parse_page(msg) -> tuple:
    """Parse a bot result page. Returns (track_buttons, next_btn)."""
    buttons = _get_buttons(msg)
    track_btns = []
    next_btn = None

    for btn in buttons:
        text = btn["text"].strip()
        # Numbered track: "1. Artist - Title"
        m = re.match(r"^(\d+)[.)]\s+(.+)$", text)
        if m and btn["type"] == "callback":
            num = int(m.group(1))
            label = m.group(2).strip()
            parts = label.split(" - ", 1)
            track_btns.append({
                "num": num,
                "label": label,
                "title": parts[1].strip() if len(parts) > 1 else label,
                "artist": parts[0].strip() if len(parts) > 1 else "",
                "btn_data": btn["data"],
                "msg_id": msg.id,
            })
            continue
        # Next page button: ▶ or ► (but not source/close buttons)
        if btn["type"] == "callback" and re.search(r"[▶►]", text):
            tl = text.lower()
            if not any(x in tl for x in ["track","album","artist","deezer","sound","vk","close"]):
                next_btn = btn

    return track_btns, next_btn


async def bot_full_search(channel: str, query: str, timeout: float = 20) -> list[dict]:
    """
    Complete search flow:
    1. Send query, paginate ALL pages collecting track button callback data
    2. Press EVERY track button — bot posts audio files into the channel
    3. Collect all posted audio messages
    4. Return clean list with msg_id, title, artist, duration, size, cover

    The frontend just shows the list and downloads on demand — no further
    bot interaction needed after this.
    """
    client = await get_tg_client()
    all_track_btns = []  # collected across all pages
    seen_nums: set[int] = set()

    # ── Phase 1: collect all track buttons across all pages ──────────────────
    sent_id, replies = await _send_and_wait(client, channel, query, timeout)
    if not replies:
        log.warning("Bot search: no replies")
        return []

    log.info(f"Got {len(replies)} reply messages")

    async def collect_all_pages(msg):
        track_btns, next_btn = _parse_page(msg)
        log.info(f"Page msg {msg.id}: {len(track_btns)} tracks, has_next={bool(next_btn)}")

        for t in track_btns:
            if t["num"] not in seen_nums:
                seen_nums.add(t["num"])
                all_track_btns.append({**t, "page_msg_id": msg.id})

        if next_btn:
            log.info(f"Following ▶ to next page")
            next_msgs = await _press_button(client, channel, msg.id, next_btn["data"])
            for nm in next_msgs:
                t2, _ = _parse_page(nm)
                if t2:
                    await collect_all_pages(nm)
                    break

    for msg in replies:
        t, _ = _parse_page(msg)
        if t:
            await collect_all_pages(msg)
            break

    log.info(f"Total tracks collected across all pages: {len(all_track_btns)}")
    if not all_track_btns:
        return []

    # ── Phase 2: press each track button, collect audio messages ────────────
    results = []
    # Track the highest message ID seen so far to use as after_id for each press
    # We use the page_msg_id of each track as the anchor
    last_seen_id = max(t["page_msg_id"] for t in all_track_btns)

    for i, track in enumerate(all_track_btns):
        log.info(f"Pressing track {track['num']}: {track['label']}")
        try:
            audio_msgs = await _press_button(
                client, channel, track["page_msg_id"], track["btn_data"], fast=True
            )
            got_audio = False
            for am in audio_msgs:
                if am.id > last_seen_id:
                    last_seen_id = am.id
                info = extract_audio_info(am, channel)
                if info:
                    info["title"] = info.get("title") or track["title"]
                    info["artist"] = info.get("artist") or track["artist"]
                    info["num"] = track["num"]
                    results.append(info)
                    log.info(f"  ✓ {info['title']} ({info.get('size',0)//1024}KB)")
                    got_audio = True
                    break
            if not got_audio:
                log.warning(f"  No audio for track {track['num']}")
                results.append({
                    "num": track["num"],
                    "title": track["title"],
                    "artist": track["artist"],
                    "msg_id": None,
                    "channel": channel,
                    "filename": "",
                    "duration": 0,
                    "size": 0,
                })
        except Exception as e:
            log.warning(f"Failed to press track {track['num']}: {e}")
            results.append({
                "num": track["num"],
                "title": track["title"],
                "artist": track["artist"],
                "msg_id": None,
                "channel": channel,
                "filename": "",
                "duration": 0,
                "size": 0,
                "error": str(e),
            })

    results.sort(key=lambda x: x.get("num", 999))
    log.info(f"Search complete: {len([r for r in results if r.get('msg_id')])} / {len(results)} tracks with audio")
    return results



# ---------------------------------------------------------------------------
# Download engine
# ---------------------------------------------------------------------------

_dl_progress: dict[int, int] = {}


async def _do_download(dl_id: int, channel: str, msg_id: int, filename: str, title: str, artist: str):
    conn = db_connect()
    try:
        client = await get_tg_client()
        safe = sanitize(filename)
        dest = MUSIC_DIR / safe
        counter = 1
        while dest.exists():
            stem = Path(safe).stem
            ext = Path(safe).suffix
            dest = MUSIC_DIR / f"{stem}_{counter}{ext}"
            counter += 1

        conn.execute("UPDATE downloads SET status='downloading' WHERE id=?", (dl_id,))
        conn.commit()

        def on_progress(current, total):
            pct = int(current / total * 100) if total else 0
            _dl_progress[dl_id] = pct
            conn.execute("UPDATE downloads SET progress=? WHERE id=?", (pct, dl_id))
            conn.commit()

        msg = await client.get_messages(channel, ids=msg_id)
        await client.download_media(msg, file=str(dest), progress_callback=on_progress)

        meta = read_tags(dest)
        # Prefer the title/artist we got from the bot search
        if title and title != "Unknown":
            meta["title"] = title
        if artist and artist not in ("Unknown", "Unknown Artist", ""):
            meta["artist"] = artist

        cur = conn.execute("""
            INSERT OR IGNORE INTO tracks
              (title, artist, album, duration, file_path, file_size, cover_path, genre, year, tg_msg_id, tg_channel)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (meta["title"], meta["artist"], meta["album"], meta["duration"],
              str(dest), dest.stat().st_size, meta.get("cover_path"),
              meta["genre"], meta["year"], msg_id, channel))
        conn.commit()
        track_id = cur.lastrowid

        conn.execute("UPDATE downloads SET status='done', progress=100, track_id=? WHERE id=?",
                     (track_id, dl_id))
        conn.commit()
        _dl_progress.pop(dl_id, None)

    except Exception as e:
        log.exception(f"Download {dl_id} failed")
        conn.execute("UPDATE downloads SET status='error', error=? WHERE id=?", (str(e), dl_id))
        conn.commit()
    finally:
        conn.close()


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
# Config endpoints
# ---------------------------------------------------------------------------

class ConfigIn(BaseModel):
    api_id: str
    api_hash: str
    phone: str
    bot_channel: str = ""


@app.get("/api/config")
def get_config():
    cfg = load_config()
    return {
        "configured": bool(cfg.get("api_id") and cfg.get("api_hash") and cfg.get("phone")),
        "bot_channel": cfg.get("bot_channel", ""),
    }


@app.post("/api/config")
def post_config(body: ConfigIn):
    save_config_file(body.model_dump())
    return {"ok": True}


# ---------------------------------------------------------------------------
# Telegram status + search
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Telegram auth flow (multi-step: send code -> verify code -> optional 2FA)
# ---------------------------------------------------------------------------

@app.post("/api/auth/start")
async def auth_start():
    """Step 1: Send verification code to phone."""
    global _auth_state
    cfg = load_config()
    phone = cfg.get("phone", "").strip()
    if not phone:
        raise HTTPException(400, "Phone number not set")
    # Ensure phone has + prefix
    if not phone.startswith("+"):
        phone = "+" + phone
        save_config_file({"phone": phone})

    try:
        client = await _get_raw_client()
        result = await client.send_code_request(phone)
        _auth_state["step"] = "waiting_code"
        _auth_state["phone_hash"] = result.phone_code_hash
        _auth_state["error"] = None
        return {"ok": True, "step": "waiting_code", "message": f"Code sent to {phone}"}
    except Exception as e:
        _auth_state["step"] = "error"
        _auth_state["error"] = str(e)
        raise HTTPException(400, str(e))


@app.post("/api/auth/verify")
async def auth_verify(body: dict):
    """Step 2: Submit the verification code."""
    global _auth_state
    from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
    code = str(body.get("code", "")).strip()
    cfg = load_config()
    phone = cfg.get("phone", "").strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    try:
        client = await _get_raw_client()
        await client.sign_in(phone=phone, code=code, phone_code_hash=_auth_state["phone_hash"])
        _auth_state["step"] = "connected"
        return {"ok": True, "step": "connected"}
    except SessionPasswordNeededError:
        _auth_state["step"] = "waiting_2fa"
        return {"ok": True, "step": "waiting_2fa", "message": "2FA password required"}
    except PhoneCodeInvalidError:
        raise HTTPException(400, "Invalid code — check and try again")
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/2fa")
async def auth_2fa(body: dict):
    """Step 3 (optional): Submit 2FA password."""
    global _auth_state
    password = body.get("password", "")
    try:
        client = await _get_raw_client()
        await client.sign_in(password=password)
        _auth_state["step"] = "connected"
        return {"ok": True, "step": "connected"}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/auth/status")
async def auth_status_check():
    """Quick check: is the session already authorized?"""
    global _auth_state
    try:
        client = await _get_raw_client()
        if await client.is_user_authorized():
            _auth_state["step"] = "connected"
            me = await client.get_me()
            name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or "?"
            return {"authorized": True, "step": "connected", "name": name}
        return {"authorized": False, "step": _auth_state["step"]}
    except Exception as e:
        return {"authorized": False, "step": "error", "error": str(e)}


@app.get("/api/telegram/status")
async def telegram_status():
    cfg = load_config()
    if not cfg.get("api_id") or not cfg.get("api_hash") or not cfg.get("phone"):
        return {"status": "not_configured", "message": "API credentials not set. Open Setup."}
    try:
        client = await get_tg_client()
        me = await client.get_me()
        if me:
            name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or "?"
            return {"status": "connected", "message": f"Connected as {name}", "username": me.username}
        return {"status": "error", "message": "Could not retrieve account info"}
    except Exception as e:
        msg = str(e)
        if any(k in msg.lower() for k in ("auth", "code", "2fa", "password", "phone")):
            return {"status": "needs_auth", "message": "Check your terminal — Telegram is waiting for a code."}
        return {"status": "error", "message": f"Connection failed: {msg}"}


class SearchRequest(BaseModel):
    channel: str = ""
    query: str
    timeout: int = 30


@app.post("/api/telegram/search")
async def telegram_search(body: SearchRequest):
    """
    Full search: sends query, paginates all pages, presses every track button,
    collects all audio messages the bot posts. Returns complete track list.
    User just picks what to download — no further bot interaction needed.
    """
    channel = body.channel or load_config().get("bot_channel", "")
    if not channel:
        raise HTTPException(400, "No bot channel configured — open Setup")
    log.info(f"Full search: query={body.query!r} channel={channel!r}")
    try:
        results = await bot_full_search(channel, body.query, body.timeout)
        return {"results": results, "count": len(results), "query": body.query}
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"Search failed: {e}")
        raise HTTPException(500, f"Search error: {str(e)}")



# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

class DownloadRequest(BaseModel):
    channel: str
    msg_id: int
    filename: str
    title: str = ""
    artist: str = ""
    source: str = "soundcloud"


class MultiDownloadRequest(BaseModel):
    items: list[DownloadRequest]


@app.post("/api/downloads")
async def start_download(body: DownloadRequest, bg: BackgroundTasks):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO downloads (tg_msg_id, tg_channel, filename, title, artist, status, source) VALUES (?,?,?,?,?,'queued',?)",
        (body.msg_id, body.channel, body.filename, body.title, body.artist, body.source)
    )
    conn.commit()
    dl_id = cur.lastrowid
    conn.close()
    bg.add_task(_do_download, dl_id, body.channel, body.msg_id, body.filename, body.title, body.artist)
    return {"download_id": dl_id}


@app.post("/api/downloads/multi")
async def multi_download(body: MultiDownloadRequest, bg: BackgroundTasks):
    conn = db_connect()
    ids = []
    for item in body.items:
        cur = conn.execute(
            "INSERT INTO downloads (tg_msg_id, tg_channel, filename, title, artist, status, source) VALUES (?,?,?,?,?,'queued',?)",
            (item.msg_id, item.channel, item.filename, item.title, item.artist, item.source)
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    for i, item in enumerate(body.items):
        bg.add_task(_do_download, ids[i], item.channel, item.msg_id, item.filename, item.title, item.artist)
    return {"download_ids": ids, "count": len(ids)}


@app.get("/api/downloads")
def list_downloads():
    conn = db_connect()
    rows = conn.execute("""
        SELECT d.*, t.title as track_title FROM downloads d
        LEFT JOIN tracks t ON d.track_id = t.id
        ORDER BY d.started_at DESC LIMIT 100
    """).fetchall()
    conn.close()
    result = [row_dict(r) for r in rows]
    for r in result:
        r["live_progress"] = _dl_progress.get(r["id"], r.get("progress", 0))
    return {"downloads": result}


@app.get("/api/downloads/{dl_id}/progress")
def download_progress(dl_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM downloads WHERE id=?", (dl_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    d = row_dict(row)
    d["live_progress"] = _dl_progress.get(dl_id, d.get("progress", 0))
    return d


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
# Shutdown
# ---------------------------------------------------------------------------

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
    Search YouTube for a music video.
    Uses YouTube Data API v3 if key is configured, otherwise scrapes search page.
    Returns video_id, title, thumbnail for embedding.
    """
    import urllib.request, urllib.parse, json as _json, re as _re

    cfg = load_config()
    query = f"{artist} {title} official music video"
    encoded = urllib.parse.quote(query)

    # Try YouTube Data API v3 first (if key configured)
    yt_key = cfg.get("youtube_api_key")
    if yt_key:
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
            log.warning(f"YouTube API failed: {e}, falling back to scrape")

    # Fallback: scrape YouTube search results (no key needed)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        url = f"https://www.youtube.com/results?search_query={encoded}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")

        # Extract video IDs from ytInitialData JSON
        match = _re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
        if match:
            vid_id = match.group(1)
            # Extract title near the video ID
            title_match = _re.search(
                rf'"videoId":"{vid_id}".*?"title".*?"text":"([^"]+)"', html
            )
            vid_title = title_match.group(1) if title_match else f"{artist} - {title}"
            return {
                "video_id": vid_id,
                "title": vid_title,
                "channel": artist,
                "thumbnail": f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg",
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "source": "scrape"
            }
    except Exception as e:
        log.warning(f"YouTube scrape failed: {e}")

    raise HTTPException(404, "No YouTube video found")


@app.post("/api/config/youtube-key")
def save_youtube_key(body: dict):
    """Save YouTube Data API v3 key (optional — scraping works without it)."""
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
