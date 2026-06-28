"""
Media Downloader - FastAPI Backend
Supports X/Twitter, YouTube, and Bilibili video download with resolution selection,
subtitle burn-in, and Twitter article/thread Markdown export.
"""

import os
import re
import json
import uuid
import time
import sqlite3
import tempfile
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional, List, Iterator

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="X Media Downloader", version="1.0.0")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"

# Default save directory (user-overridable per request via save_dir param).
# Files land here directly and are NOT deleted after delivery.
DEFAULT_SAVE_DIR = Path("downloads_done")

# SQLite-backed task store (survives restarts; powers history + recovery).
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "tasks.db"

# Bearer tokens used by Twitter/X web client (same as yt-dlp)
_AUTH = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
_LEGACY_AUTH = "AAAAAAAAAAAAAAAAAAAAAIK1zgAAAAAA2tUWuhGZ2JceoId5GwYWU5GspY4%3DUq7gzFoCZs1QfwGoVdvSac3IniczZEYXIcDyumCauIXpcAPorE"
_API_BASE = "https://api.x.com/1.1/"
_GRAPHQL_API_BASE = "https://x.com/i/api/graphql/"
_GRAPHQL_ENDPOINT = "2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId"

# ── Download task tracking (SQLite-backed, restart-safe) ──────────────────────
_download_lock = threading.Lock()
_db_lock = threading.Lock()

# SSE subscribers: task_id -> list of queue.Queue. Progress updates push to all.
_sse_subs: dict = {}
_sse_lock = threading.Lock()


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _db_lock, _db_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id     TEXT PRIMARY KEY,
                url         TEXT,
                platform    TEXT,
                title       TEXT,
                uploader    TEXT,
                status      TEXT,
                progress    INTEGER DEFAULT 0,
                speed           REAL DEFAULT 0,
                total_bytes     INTEGER DEFAULT 0,
                downloaded_bytes INTEGER DEFAULT 0,
                eta             INTEGER DEFAULT 0,
                save_path   TEXT,
                filename    TEXT,
                error       TEXT,
                created_at  REAL,
                updated_at  REAL
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)"
        )
        # ── migrations: live download telemetry columns (idempotent) ──
        # Added so progress hooks can stream real speed/bytes/ETA to the UI.
        # ALTER is a no-op (caught) once the column already exists.
        for _col, _decl in (
            ('speed', 'REAL DEFAULT 0'),
            ('total_bytes', 'INTEGER DEFAULT 0'),
            ('downloaded_bytes', 'INTEGER DEFAULT 0'),
            ('eta', 'INTEGER DEFAULT 0'),
        ):
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {_col} {_decl}")
            except sqlite3.OperationalError:
                pass  # column already exists on upgraded DBs


_init_db()


def _row_to_dict(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    # alias for legacy code that read 'file_path' / 'dldir'
    d.setdefault('file_path', d.get('save_path'))
    return d


def _create_task(url: str = '', platform: str = '', title: str = '',
                 uploader: str = '') -> str:
    task_id = uuid.uuid4().hex[:8]
    now = time.time()
    with _db_lock, _db_conn() as c:
        c.execute(
            "INSERT INTO tasks (task_id, url, platform, title, uploader, "
            "status, progress, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'downloading', 0, ?, ?)",
            (task_id, url, platform, title, uploader, now, now),
        )
    return task_id


def _update_task(task_id: str, **kwargs):
    now = time.time()
    fields, vals = [], []
    for k, v in kwargs.items():
        # map legacy alias file_path -> save_path at write time
        key = 'save_path' if k == 'file_path' else k
        fields.append(f"{key}=?")
        vals.append(v)
    fields.append("updated_at=?")
    vals.append(now)
    vals.append(task_id)

    with _db_lock, _db_conn() as c:
        c.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE task_id=?", vals
        )

    # Push snapshot to SSE subscribers (non-blocking).
    _sse_broadcast(task_id)


def _get_task(task_id: str) -> Optional[dict]:
    with _db_lock, _db_conn() as c:
        row = c.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    return _row_to_dict(row)


def _list_tasks(limit: int = 50) -> List[dict]:
    with _db_lock, _db_conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── SSE fan-out ───────────────────────────────────────────────────────────────
import queue


def _sse_subscribe(task_id: str):
    q: "queue.Queue" = queue.Queue()
    with _sse_lock:
        _sse_subs.setdefault(task_id, []).append(q)
    return q


def _sse_unsubscribe(task_id: str, q):
    with _sse_lock:
        subs = _sse_subs.get(task_id)
        if subs and q in subs:
            subs.remove(q)
        if subs is not None and not subs:
            _sse_subs.pop(task_id, None)


def _sse_broadcast(task_id: str):
    """Push current task snapshot to all SSE subscribers for this task."""
    task = _get_task(task_id)
    if not task:
        return
    with _sse_lock:
        subs = list(_sse_subs.get(task_id, []))
    for q in subs:
        try:
            q.put_nowait(task)
        except Exception:
            pass  # drop if subscriber queue full; it'll re-sync on next tick


def _yt_progress_hook(task_id: str):
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            pct = int(downloaded / total * 100) if total > 0 else 0
            # cap below 100% while still downloading: yt-dlp sometimes reports
            # 100 here before the 'finished' hook fires, which would make the
            # UI look complete prematurely. The real 100% is set on 'done'.
            pct = min(pct, 99)
            _update_task(
                task_id, progress=pct, status='downloading',
                speed=d.get('speed') or 0,
                total_bytes=total,
                downloaded_bytes=downloaded,
                eta=d.get('eta') or 0,
            )
        elif d['status'] == 'finished':
            # download bytes fully received → entering merge/transcode phase.
            # Progress is intentionally NOT forced to 100% here: leave it at
            # the last real value so the bar doesn't look done while merging.
            final_total = (d.get('total_bytes')
                           or d.get('total_bytes_estimate')
                           or d.get('downloaded_bytes', 0) or 0)
            _update_task(
                task_id, status='merging', speed=0, eta=0,
                downloaded_bytes=final_total,
                total_bytes=final_total or 0,
            )
    return hook


# ── Request Models ────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    url: str
    proxy: Optional[str] = None
    cookie_content: Optional[str] = None


class DownloadRequest(BaseModel):
    url: str
    format_id: str
    proxy: Optional[str] = None
    cookie_content: Optional[str] = None
    save_dir: Optional[str] = None


class VideoAnalyzeRequest(BaseModel):
    url: str
    proxy: Optional[str] = None
    cookie_content: Optional[str] = None


class VideoDownloadRequest(BaseModel):
    url: str
    format_id: str
    subtitle_lang: Optional[str] = None
    proxy: Optional[str] = None
    cookie_content: Optional[str] = None
    save_dir: Optional[str] = None


class BatchRequest(BaseModel):
    urls: List[str]
    platform: str  # 'twitter' | 'youtube' | 'bilibili'
    format_id: Optional[str] = None  # optional default; first available if None
    subtitle_lang: Optional[str] = None
    proxy: Optional[str] = None
    cookie_content: Optional[str] = None
    save_dir: Optional[str] = None
    concurrency: int = 3


class OpenFolderRequest(BaseModel):
    path: str


# ── URL Parsers ────────────────────────────────────────────────────────────────

def parse_youtube_url(url: str) -> Optional[str]:
    """Return video ID from various YouTube URL formats."""
    patterns = [
        r'(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)([\w-]{11})',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def parse_bilibili_url(url: str) -> Optional[str]:
    """Return BV or AV ID from Bilibili URL."""
    m = re.search(r'(?:bilibili\.com/video/|b23\.tv/)(BV[\w]+|av\d+)', url)
    return m.group(1) if m else None


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_tweet_url(url: str) -> Optional[str]:
    m = re.search(
        r'(?:twitter\.com|x\.com|mobile\.twitter\.com)/\w+/status(?:es)?/(\d+)', url
    )
    return m.group(1) if m else None


def extract_url(text: str) -> str:
    """Extract the first URL from text that may contain other content."""
    m = re.search(r'https?://[^\s<>\"\']+', text)
    if m:
        url = m.group(0)
        # strip trailing Chinese/mixed punctuation
        url = re.sub(r'[，。！？、）】》"\']+$', '', url)
        return url
    return text.strip()


def normalize_url(url: str) -> str:
    url = extract_url(url)
    return re.sub(r'\?.*$', '', url).rstrip('/')


def _looks_like_raw_cookie_header(text: str) -> bool:
    """Heuristic: a raw `key=value; key=value` cookie header pasted from a browser.

    Recognized when every non-empty chunk separated by ';' or newline is a
    single `key=value` pair (value may contain '='). Distinguishes from JSON
    (starts with '[') and Netscape (tab-separated, >=7 columns) formats.
    """
    if not text:
        return False
    chunks = re.split(r'[;\n]', text)
    pairs = 0
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if '=' not in chunk:
            return False
        pairs += 1
    return pairs > 0


def _normalize_raw_cookie_header(text: str) -> str:
    """Convert a raw `key=value; key=value` cookie header into Netscape format.

    Each pair is written for both .x.com and .twitter.com so it works whether
    yt-dlp / the API hits either host.
    """
    lines = ["# Netscape HTTP Cookie File"]
    far_future = 2145916800  # 2037-12-31, generous non-expiring sentinel
    for chunk in re.split(r'[;\n]', text):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        name, _, value = chunk.partition('=')
        name = name.strip()
        value = value.strip().strip('"')
        if not name:
            continue
        for domain in ('.x.com', '.twitter.com'):
            lines.append(
                f"{domain}\tTRUE\t/\tTRUE\t{far_future}\t{name}\t{value}"
            )
    return '\n'.join(lines) + '\n'


def parse_cookies(content: Optional[str]):
    """Return (cookies_dict, temp_netscape_file_path | None).

    Accepts three input formats and auto-detects between them:
      1. Raw browser cookie header: `key=value; key=value; ...`
         (the string you copy from DevTools Application > Cookies or the
         Cookie request header). Auto-converted to Netscape.
      2. JSON array (browser-extension export): `[{"name","value",...}]`.
      3. Netscape cookies.txt format: tab-separated, >=7 columns per line.
    """
    if not content or not content.strip():
        return {}, None

    cookies: dict = {}
    text = content.strip()

    # Raw browser cookie header -> convert to Netscape first
    if _looks_like_raw_cookie_header(text) and not text.startswith('['):
        text = _normalize_raw_cookie_header(text)

    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', delete=False, encoding='utf-8'
    )

    try:
        # JSON array format
        if text.startswith('['):
            try:
                items = json.loads(text)
                tmp.write("# Netscape HTTP Cookie File\n")
                for c in items:
                    name = c.get('name', '')
                    value = c.get('value', '')
                    cookies[name] = value
                    tmp.write(
                        f"{c.get('domain', '.twitter.com')}\tTRUE\t"
                        f"{c.get('path', '/')}\t"
                        f"{'TRUE' if c.get('secure', True) else 'FALSE'}\t"
                        f"{int(c.get('expirationDate', 2145916800))}\t"
                        f"{name}\t{value}\n"
                    )
                tmp.flush(); tmp.close()
                return cookies, tmp.name
            except (json.JSONDecodeError, KeyError):
                tmp.close(); os.unlink(tmp.name)
                tmp = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.txt', delete=False, encoding='utf-8'
                )

        # Netscape format
        tmp.write("# Netscape HTTP Cookie File\n")
        for line in text.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
                tmp.write(line + '\n')
        tmp.flush(); tmp.close()
        return cookies, tmp.name
    except Exception:
        try:
            tmp.close(); os.unlink(tmp.name)
        except OSError:
            pass
        return {}, None


def cleanup(path: Optional[str]):
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Filename + save-dir helpers ───────────────────────────────────────────────
_ILLEGAL_FN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """Strip illegal FS chars, collapse whitespace, truncate to max_len."""
    if not name:
        return ''
    name = _ILLEGAL_FN_CHARS.sub(' ', name)
    name = re.sub(r'\s+', ' ', name).strip().strip('.')
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def build_save_filename(uploader: str, title: str, ext: str,
                        quality_tag: str = '') -> str:
    """Semantic filename: '@user_title[_quality].ext'.

    Falls back gracefully when uploader/title are empty.
    """
    ext = ext.lstrip('.') or 'mp4'
    parts: List[str] = []
    if uploader:
        parts.append(sanitize_filename(uploader, 30))
    if title:
        parts.append(sanitize_filename(title, 50))
    if not parts:
        parts.append(sanitize_filename(quality_tag, 30) or 'media')
    if quality_tag:
        parts.append(sanitize_filename(quality_tag, 12))
    name = '_'.join(p for p in parts if p) or 'media'
    return f"{name}.{ext}"


def resolve_save_dir(save_dir: Optional[str]) -> Path:
    """Resolve where final files land. Falls back to DEFAULT_SAVE_DIR.

    User-supplied paths are taken as-is (we trust the local user; this is a
    single-machine tool). Empty/invalid -> default dir.
    """
    if save_dir and save_dir.strip():
        p = Path(save_dir.strip())
    else:
        p = DEFAULT_SAVE_DIR
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = DEFAULT_SAVE_DIR
        p.mkdir(parents=True, exist_ok=True)
    return p


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-clobbering path: append ' (n)' before extension if needed."""
    target = directory / filename
    if not target.exists():
        return target
    stem, ext = target.stem, target.suffix
    n = 2
    while True:
        cand = directory / f"{stem} ({n}){ext}"
        if not cand.exists():
            return cand
        n += 1


def extract_video_formats(info: dict) -> list:
    """Extract unique resolution formats from yt-dlp info, sorted by height desc."""
    seen: set = set()
    fmts: list = []
    for f in (info.get('formats') or []):
        h = f.get('height')
        if f.get('vcodec', 'none') == 'none' or not h:
            continue
        if h in seen:
            continue
        seen.add(h)
        fmts.append({
            'format_id': f['format_id'],
            'height': h,
            'width': f.get('width'),
            'ext': f.get('ext', 'mp4'),
            'filesize': f.get('filesize'),
            'tbr': f.get('tbr'),
            'vcodec': f.get('vcodec', ''),
            'acodec': f.get('acodec', 'none'),
        })
    fmts.sort(key=lambda x: x['height'] or 0, reverse=True)
    return fmts


def extract_subtitles(info: dict) -> list:
    """Extract available subtitle languages from yt-dlp info."""
    subs: list = []
    for src in ('subtitles', 'automatic_captions'):
        for lang, tracks in (info.get(src) or {}).items():
            for t in tracks:
                ext = t.get('ext', '')
                if ext in ('srt', 'vtt', 'ass'):
                    subs.append({
                        'lang': lang,
                        'name': t.get('name', lang),
                        'ext': ext,
                        'auto': src == 'automatic_captions',
                    })
                    break
            else:
                if tracks:
                    t = tracks[0]
                    subs.append({
                        'lang': lang,
                        'name': t.get('name', lang),
                        'ext': t.get('ext', 'srt'),
                        'auto': src == 'automatic_captions',
                    })
    seen: dict = {}
    for s in subs:
        if s['lang'] not in seen or not s['auto']:
            seen[s['lang']] = s
    return list(seen.values())


def burn_subtitle(video_path: str, sub_path: str, output_path: str):
    """Burn subtitle into video using ffmpeg."""
    import subprocess
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vf', f"subtitles={sub_path.replace(':', '\\:')}",
        '-c:a', 'copy', output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        logger.warning(f"ffmpeg subtitle burn failed: {result.stderr}")
        raise RuntimeError(f"字幕烧录失败: {result.stderr[:200]}")


def proxies_for(proxy: Optional[str]) -> Optional[dict]:
    return {'http': proxy, 'https': proxy} if proxy else None


# ── Twitter API helpers ───────────────────────────────────────────────────────

def _get_guest_token(proxy: Optional[str] = None) -> Optional[str]:
    try:
        r = requests.post(
            f'{_API_BASE}guest/activate.json',
            headers={
                'Authorization': f'Bearer {_AUTH}',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            },
            proxies=proxies_for(proxy), timeout=10, data=b'',
        )
        return r.json().get('guest_token') if r.ok else None
    except Exception:
        return None


def _graphql_headers(cookies_dict: dict = None, guest_token: str = None) -> dict:
    h = {
        'Authorization': f'Bearer {_AUTH}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Origin': 'https://x.com',
        'Referer': 'https://x.com/',
    }
    if guest_token:
        h['x-guest-token'] = guest_token
    if cookies_dict and 'ct0' in cookies_dict and 'auth_token' in cookies_dict:
        h['x-csrf-token'] = cookies_dict['ct0']
        h['cookie'] = f"auth_token={cookies_dict['auth_token']}; ct0={cookies_dict['ct0']}"
    return h


def _api_headers(cookies_dict: dict = None, guest_token: str = None) -> dict:
    h = {
        'Authorization': f'Bearer {_AUTH}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    if guest_token:
        h['x-guest-token'] = guest_token
    if cookies_dict and 'ct0' in cookies_dict and 'auth_token' in cookies_dict:
        h['x-csrf-token'] = cookies_dict['ct0']
        h['cookie'] = f"auth_token={cookies_dict['auth_token']}; ct0={cookies_dict['ct0']}"
    return h


_GRAPHQL_FEATURES = {
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': False,
    'tweet_awards_web_tipping_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'responsive_web_media_download_video_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_enhance_cards_enabled': False,
}


def _build_graphql_query(tweet_id: str) -> dict:
    return {
        'variables': json.dumps({
            'tweetId': tweet_id,
            'withCommunity': False,
            'includePromotedContent': False,
            'withVoice': False,
        }, separators=(',', ':')),
        'features': json.dumps(_GRAPHQL_FEATURES, separators=(',', ':')),
        'fieldToggles': json.dumps({'withArticleRichContentState': False}, separators=(',', ':')),
    }


def _extract_graphql_status(data: dict) -> Optional[dict]:
    result = data.get('tweetResult', {}).get('result', {})
    if not result:
        return None

    typename = result.get('__typename', '')
    if typename == 'TweetTombstone':
        return None
    if typename == 'TweetUnavailable':
        return None
    if typename == 'TweetWithVisibilityResults':
        result = result.get('tweet', {})

    status = result.get('legacy', {})
    if not status:
        return None

    user = _deep_get(result, ['core', 'user_results', 'result', 'legacy'])
    if user:
        status['user'] = user

    card = _deep_get(result, ['card', 'legacy'])
    if card:
        status['card'] = card

    quoted = _deep_get(result, ['quoted_status_result', 'result', 'legacy'])
    if quoted:
        status['quoted_status'] = quoted

    retweeted = _deep_get(result, ['legacy', 'retweeted_status_result', 'result', 'legacy'])
    if retweeted:
        status['retweeted_status'] = retweeted

    status.setdefault('id_str', result.get('rest_id', ''))
    return status


def _deep_get(d: dict, keys: list):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def get_tweet_from_api(
    tweet_id: str,
    cookies_dict: dict = None,
    proxy: Optional[str] = None,
) -> Optional[dict]:
    if cookies_dict and 'auth_token' in cookies_dict and 'ct0' in cookies_dict:
        try:
            r = requests.get(
                f'{_GRAPHQL_API_BASE}{_GRAPHQL_ENDPOINT}',
                headers=_graphql_headers(cookies_dict),
                params=_build_graphql_query(tweet_id),
                proxies=proxies_for(proxy), timeout=15,
            )
            if r.ok:
                status = _extract_graphql_status(r.json().get('data', {}))
                if status:
                    return status
        except Exception:
            pass

    gt = _get_guest_token(proxy)
    if gt:
        try:
            r = requests.get(
                f'{_GRAPHQL_API_BASE}{_GRAPHQL_ENDPOINT}',
                headers=_graphql_headers(guest_token=gt),
                params=_build_graphql_query(tweet_id),
                proxies=proxies_for(proxy), timeout=15,
            )
            if r.ok:
                status = _extract_graphql_status(r.json().get('data', {}))
                if status:
                    return status
        except Exception:
            pass

    return None


def fetch_thread_tweets(
    tweet_id: str,
    cookies_dict: dict = None,
    proxy: Optional[str] = None,
) -> List[dict]:
    initial = get_tweet_from_api(tweet_id, cookies_dict, proxy)
    if not initial:
        return []

    tweets = [initial]
    visited = {tweet_id}
    author = initial.get('user', {}).get('screen_name', '')

    cur = initial
    for _ in range(50):
        parent_id = cur.get('in_reply_to_status_id_str')
        if not parent_id or cur.get('in_reply_to_screen_name') != author:
            break
        if parent_id in visited:
            break
        parent = get_tweet_from_api(parent_id, cookies_dict, proxy)
        if not parent:
            break
        tweets.append(parent)
        visited.add(parent_id)
        cur = parent

    tweets.sort(key=lambda t: int(t.get('id_str', '0')))
    root_id = tweets[0].get('id_str', tweet_id)

    try:
        headers = _api_headers(cookies_dict)
        if not (cookies_dict and 'ct0' in cookies_dict):
            gt = _get_guest_token(proxy)
            if gt:
                headers = _api_headers(guest_token=gt)
            else:
                headers = None
        if headers:
            r = requests.get(
                'https://api.twitter.com/1.1/search/tweets.json',
                headers=headers,
                params={
                    'q': f'conversation_id:{root_id} from:{author}',
                    'tweet_mode': 'extended',
                    'count': 100,
                },
                proxies=proxies_for(proxy), timeout=15,
            )
            if r.ok:
                for t in r.json().get('statuses', []):
                    tid = t.get('id_str', '')
                    if tid and tid not in visited:
                        tweets.append(t)
                        visited.add(tid)
                tweets.sort(key=lambda t: int(t.get('id_str', '0')))
    except Exception:
        pass

    return tweets


# ── Markdown builders ─────────────────────────────────────────────────────────

def _clean_tweet_text(tweet: dict) -> str:
    text = tweet.get('full_text', '')
    for u in tweet.get('entities', {}).get('urls', []):
        text = text.replace(u.get('url', ''), u.get('expanded_url', ''))
    for m in tweet.get('extended_entities', {}).get('media', []):
        if m.get('type') == 'photo':
            text = text.replace(m.get('url', ''), f"\n![图片]({m.get('media_url_https', '')})\n")
        else:
            text = text.replace(m.get('url', ''), '')
    text = re.sub(r'https?://(?:twitter\.com|x\.com)/\w+/status/\d+', '', text)
    return text.strip()


def build_markdown(tweets: List[dict], base_url: str) -> str:
    if not tweets:
        return ""
    first = tweets[0]
    author = first.get('user', {}).get('screen_name', '')
    name = first.get('user', {}).get('name', '')
    created = first.get('created_at', '')

    lines = [
        f"# {name} (@{author}) 的推文",
        "",
    ]
    if created:
        lines.append(f"**时间**: {created}")
    lines.append(f"**原文链接**: {base_url}")
    if len(tweets) > 1:
        lines.append(f"**推文数量**: {len(tweets)}")
    lines += ["", "---", ""]

    for i, tw in enumerate(tweets, 1):
        if len(tweets) > 1:
            lines += [f"## {i}/{len(tweets)}", ""]
        lines.append(_clean_tweet_text(tw))
        lines.append("")
        if i < len(tweets):
            lines += ["---", ""]

    return '\n'.join(lines)


def build_markdown_ytdlp(info: dict, url: str) -> str:
    ud = info.get('upload_date', '')
    if ud and len(ud) == 8:
        ud = f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}"
    lines = [
        f"# {info.get('title', '')}",
        "",
        f"**作者**: @{info.get('uploader', '')}" if info.get('uploader') else "",
        f"**时间**: {ud}" if ud else "",
        f"**原文链接**: {url}",
        "",
        "---",
        "",
        info.get('description', ''),
    ]
    return '\n'.join(l for l in lines if l is not None)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    path = STATIC_DIR / "index.html"
    if not path.exists():
        raise HTTPException(404, "Frontend not found – check static/index.html")
    return HTMLResponse(path.read_text(encoding='utf-8'))


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = normalize_url(req.url)
    tid = parse_tweet_url(url)
    if not tid:
        raise HTTPException(400, "无效的推文链接，请输入正确的 X / Twitter URL")

    cookies_dict, cookie_file = parse_cookies(req.cookie_content)
    try:
        api_tweet = get_tweet_from_api(tid, cookies_dict, req.proxy)

        ytdlp_info = None
        vid_fmts: list = []
        ytdlp_err: Optional[str] = None

        try:
            opts: dict = {'quiet': True, 'no_warnings': True}
            if req.proxy:
                opts['proxy'] = req.proxy
            if cookie_file:
                opts['cookiefile'] = cookie_file
            with yt_dlp.YoutubeDL(opts) as ydl:
                ytdlp_info = ydl.extract_info(url, download=False)

            if ytdlp_info:
                seen: set = set()
                for f in (ytdlp_info.get('formats') or []):
                    h = f.get('height')
                    if f.get('vcodec', 'none') == 'none' or not h:
                        continue
                    if h in seen:
                        continue
                    seen.add(h)
                    vid_fmts.append({
                        'format_id': f['format_id'],
                        'height': h,
                        'width': f.get('width'),
                        'ext': f.get('ext', 'mp4'),
                        'filesize': f.get('filesize'),
                        'tbr': f.get('tbr'),
                        'vcodec': f.get('vcodec', ''),
                        'acodec': f.get('acodec', 'none'),
                    })
                vid_fmts.sort(key=lambda x: x['height'] or 0, reverse=True)
        except yt_dlp.utils.DownloadError as exc:
            ytdlp_err = str(exc)
            if '401' in ytdlp_err or 'Unauthorized' in ytdlp_err:
                raise HTTPException(401, "认证失败 (401)。请在高级设置中配置有效的 Cookie 后重试。")
            if '403' in ytdlp_err:
                if not api_tweet:
                    raise HTTPException(403, "访问被拒绝 (403)。推文可能为私密或需要登录。")
            if '404' in ytdlp_err:
                if not api_tweet:
                    raise HTTPException(404, "推文不存在或已被删除。")

        has_video = bool(vid_fmts)

        if api_tweet:
            user = api_tweet.get('user', {})
            uploader = user.get('screen_name', '')
            description = api_tweet.get('full_text', '')
            for u in api_tweet.get('entities', {}).get('urls', []):
                description = description.replace(u.get('url', ''), u.get('expanded_url', ''))
            is_thread = api_tweet.get('in_reply_to_screen_name') == uploader
            title = f"@{uploader}: {description[:80]}"
            created_at = api_tweet.get('created_at', '')
            thumbnail = ''
            for m in api_tweet.get('extended_entities', {}).get('media', []):
                if m.get('media_url_https'):
                    thumbnail = m['media_url_https']
                    break
        elif ytdlp_info:
            uploader = ytdlp_info.get('uploader', '')
            description = ytdlp_info.get('description', '')
            title = ytdlp_info.get('title', '')
            created_at = ytdlp_info.get('upload_date', '')
            thumbnail = ytdlp_info.get('thumbnail', '')
            is_thread = bool(re.findall(
                rf'https?://(?:twitter\.com|x\.com)/{re.escape(uploader)}/status/\d+',
                description,
            ))
        else:
            if ytdlp_err:
                raise HTTPException(500, f"获取推文失败：{ytdlp_err}")
            raise HTTPException(404, "无法获取推文信息，请检查网络或配置代理/Cookie。")

        result = {
            'type': 'video' if has_video else 'article',
            'is_thread': is_thread,
            'tweet_id': tid,
            'title': title,
            'description': description,
            'thumbnail': thumbnail,
            'uploader': uploader,
            'upload_date': created_at,
            'view_count': ytdlp_info.get('view_count') if ytdlp_info else None,
            'like_count': ytdlp_info.get('like_count') if ytdlp_info else None,
            'url': url,
        }
        if has_video:
            result['formats'] = vid_fmts

        return result

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("analyze error")
        raise HTTPException(500, f"服务器错误：{exc}")
    finally:
        cleanup(cookie_file)


# ── Task-based download endpoints ─────────────────────────────────────────────

@app.post("/api/download")
async def download_video(req: DownloadRequest):
    url = normalize_url(req.url)
    tid = parse_tweet_url(url)
    if not tid:
        raise HTTPException(400, "无效的推文链接")

    task_id = _create_task(url=url, platform='twitter')
    proxy = req.proxy
    cookie_content = req.cookie_content
    format_id = req.format_id
    save_dir = resolve_save_dir(req.save_dir)

    def run():
        _, cookie_file = parse_cookies(cookie_content)
        dldir = DOWNLOADS_DIR / f"tw_{task_id}"
        dldir.mkdir(exist_ok=True)
        try:
            opts: dict = {
                'quiet': True, 'no_warnings': True,
                'format': f'{format_id}+bestaudio/best/{format_id}',
                'merge_output_format': 'mp4',
                'outtmpl': str(dldir / f'{tid}.%(ext)s'),
                'progress_hooks': [_yt_progress_hook(task_id)],
            }
            if proxy:
                opts['proxy'] = proxy
            if cookie_file:
                opts['cookiefile'] = cookie_file

            info = None
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception:
                opts['format'] = format_id
                opts.pop('merge_output_format', None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)

            files = list(dldir.glob('*'))
            if not files:
                _update_task(task_id, status='error', error='下载文件未生成')
                return

            # prefer the actual video file (skip stray subtitle files)
            video_files = [f for f in files if f.suffix.lower()
                           in ('.mp4', '.webm', '.mkv')]
            src = (video_files or files)[0]

            uploader = (info or {}).get('uploader') or ''
            title = (info or {}).get('title') or tid
            final_name = build_save_filename(
                uploader, title, src.suffix.lstrip('.'),
                quality_tag=f"{format_id}p" if format_id.isdigit() else ''
            )
            dest = unique_path(save_dir, final_name)
            shutil.move(str(src), str(dest))
            _update_task(task_id, status='done', save_path=str(dest),
                        filename=dest.name, uploader=uploader, title=title)
        except Exception as exc:
            _update_task(task_id, status='error', error=str(exc))
        finally:
            shutil.rmtree(dldir, True)
            cleanup(cookie_file)

    threading.Thread(target=run, daemon=True).start()
    return {'task_id': task_id}


@app.post("/api/article")
async def article(req: AnalyzeRequest):
    url = normalize_url(req.url)
    tid = parse_tweet_url(url)
    if not tid:
        raise HTTPException(400, "无效的推文链接")

    cookies_dict, cookie_file = parse_cookies(req.cookie_content)
    try:
        api_tweet = get_tweet_from_api(tid, cookies_dict, req.proxy)
        if api_tweet:
            return {'markdown': build_markdown([api_tweet], url), 'tweet_id': tid}

        opts: dict = {'quiet': True, 'no_warnings': True}
        if req.proxy:
            opts['proxy'] = req.proxy
        if cookie_file:
            opts['cookiefile'] = cookie_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise HTTPException(404, "无法获取推文信息")
        return {'markdown': build_markdown_ytdlp(info, url), 'tweet_id': tid}

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(500, f"获取失败：{exc}")
    except Exception as exc:
        logger.exception("article error")
        raise HTTPException(500, f"服务器错误：{exc}")
    finally:
        cleanup(cookie_file)


@app.post("/api/thread")
async def thread(req: AnalyzeRequest):
    url = normalize_url(req.url)
    tid = parse_tweet_url(url)
    if not tid:
        raise HTTPException(400, "无效的推文链接")

    cookies_dict, cookie_file = parse_cookies(req.cookie_content)
    try:
        tweets = fetch_thread_tweets(tid, cookies_dict, req.proxy)

        if not tweets:
            opts: dict = {'quiet': True, 'no_warnings': True}
            if req.proxy:
                opts['proxy'] = req.proxy
            if cookie_file:
                opts['cookiefile'] = cookie_file
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                raise HTTPException(404, "无法获取推文信息")
            return {
                'markdown': build_markdown_ytdlp(info, url),
                'tweet_count': 1,
            }

        return {
            'markdown': build_markdown(tweets, url),
            'tweet_count': len(tweets),
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("thread error")
        raise HTTPException(500, f"服务器错误：{exc}")
    finally:
        cleanup(cookie_file)


# ── YouTube & Bilibili Analyze ────────────────────────────────────────────────

def _analyze_video(url: str, platform: str, proxy: Optional[str], cookie_content: Optional[str]):
    _, cookie_file = parse_cookies(cookie_content)
    try:
        opts: dict = {'quiet': True, 'no_warnings': True}
        if proxy:
            opts['proxy'] = proxy
        if cookie_file:
            opts['cookiefile'] = cookie_file

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise HTTPException(404, f"无法获取{platform}视频信息")

        formats = extract_video_formats(info)
        subtitles = extract_subtitles(info)

        upload_date = info.get('upload_date', '')
        if upload_date and len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

        result = {
            'type': 'video',
            'platform': platform,
            'title': info.get('title', ''),
            'description': (info.get('description', '') or '')[:500],
            'thumbnail': info.get('thumbnail', ''),
            'uploader': info.get('uploader', '') or info.get('channel', ''),
            'upload_date': upload_date,
            'duration': info.get('duration'),
            'view_count': info.get('view_count'),
            'url': url,
            'formats': formats,
            'subtitles': subtitles,
        }
        return result

    except HTTPException:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(400, f"获取视频失败：{exc}")
    except Exception as exc:
        logger.exception(f"{platform} analyze error")
        raise HTTPException(500, f"服务器错误：{exc}")
    finally:
        cleanup(cookie_file)


@app.post("/api/yt/analyze")
async def yt_analyze(req: VideoAnalyzeRequest):
    url = normalize_url(req.url)
    if not parse_youtube_url(url):
        raise HTTPException(400, "无效的 YouTube 链接")
    return _analyze_video(url, 'youtube', req.proxy, req.cookie_content)


@app.post("/api/bili/analyze")
async def bili_analyze(req: VideoAnalyzeRequest):
    url = normalize_url(req.url)
    if not parse_bilibili_url(url):
        raise HTTPException(400, "无效的 B站链接")
    return _analyze_video(url, 'bilibili', req.proxy, req.cookie_content)


# ── YouTube & Bilibili Download (task-based) ──────────────────────────────────

def _start_video_download(url: str, format_id: str, subtitle_lang: Optional[str],
                          proxy: Optional[str], cookie_content: Optional[str],
                          platform: str, save_dir: Optional[str] = None) -> str:
    """Create a download task and run it in background. Returns task_id.

    Final file is moved to `save_dir` (or DEFAULT_SAVE_DIR) with a semantic
    name and kept there permanently. The temporary work dir is cleaned up.
    """
    task_id = _create_task(url=url, platform=platform)
    final_save_dir = resolve_save_dir(save_dir)

    def run():
        dldir = DOWNLOADS_DIR / f"{platform}_{task_id}"
        dldir.mkdir(exist_ok=True)
        _, cookie_file = parse_cookies(cookie_content)
        try:
            need_sub = bool(subtitle_lang)
            outtmpl = str(dldir / 'video.%(ext)s')

            opts: dict = {
                'quiet': True, 'no_warnings': True,
                'format': f'{format_id}+bestaudio/best/{format_id}',
                'merge_output_format': 'mp4',
                'outtmpl': outtmpl,
                'progress_hooks': [_yt_progress_hook(task_id)],
            }
            if proxy:
                opts['proxy'] = proxy
            if cookie_file:
                opts['cookiefile'] = cookie_file
            if need_sub:
                opts['writeautomaticsub'] = True
                opts['writesubtitles'] = True
                opts['subtitleslangs'] = [subtitle_lang]
                opts['subtitlesformat'] = 'srt'

            info = None
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except Exception:
                opts['format'] = format_id
                opts.pop('merge_output_format', None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)

            files = list(dldir.glob('*'))
            if not files:
                _update_task(task_id, status='error', error='下载文件未生成')
                return

            video_file = None
            sub_file = None
            for f in files:
                if f.suffix in ('.mp4', '.webm', '.mkv') and video_file is None:
                    video_file = f
                elif f.suffix in ('.srt', '.vtt', '.ass') and sub_file is None:
                    sub_file = f

            if not video_file:
                video_file = files[0]

            final_file = video_file

            if need_sub and sub_file and sub_file.exists():
                burned = dldir / f"burned{video_file.suffix}"
                try:
                    burn_subtitle(str(video_file), str(sub_file), str(burned))
                    final_file = burned
                except Exception as exc:
                    logger.warning(f"Subtitle burn failed: {exc}")

            uploader = (info or {}).get('uploader') or (info or {}).get('channel') or ''
            title = (info or {}).get('title') or 'media'
            quality_tag = f"{format_id}p" if format_id and format_id.isdigit() else ''
            final_name = build_save_filename(
                uploader, title, final_file.suffix.lstrip('.'), quality_tag
            )
            dest = unique_path(final_save_dir, final_name)
            shutil.move(str(final_file), str(dest))
            _update_task(task_id, status='done', save_path=str(dest),
                        filename=dest.name, uploader=uploader, title=title)
        except Exception as exc:
            _update_task(task_id, status='error', error=str(exc))
        finally:
            shutil.rmtree(dldir, True)
            cleanup(cookie_file)

    threading.Thread(target=run, daemon=True).start()
    return task_id


@app.post("/api/yt/download")
async def yt_download(req: VideoDownloadRequest):
    url = normalize_url(req.url)
    if not parse_youtube_url(url):
        raise HTTPException(400, "无效的 YouTube 链接")
    task_id = _start_video_download(url, req.format_id, req.subtitle_lang,
                                    req.proxy, req.cookie_content, 'youtube',
                                    req.save_dir)
    return {'task_id': task_id}


@app.post("/api/bili/download")
async def bili_download(req: VideoDownloadRequest):
    url = normalize_url(req.url)
    if not parse_bilibili_url(url):
        raise HTTPException(400, "无效的 B站链接")
    task_id = _start_video_download(url, req.format_id, req.subtitle_lang,
                                    req.proxy, req.cookie_content, 'bilibili',
                                    req.save_dir)
    return {'task_id': task_id}


# ── Progress & File endpoints ─────────────────────────────────────────────────

@app.get("/api/progress/{task_id}")
async def get_progress(task_id: str):
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/file/{task_id}")
async def get_file(task_id: str):
    """Legacy endpoint: stream the saved file to the browser.

    Files are now kept permanently in save_dir, so this no longer deletes
    anything after delivery. Kept for backward-compat; the new UI reads
    save_path directly instead of pulling through this endpoint.
    """
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task['status'] != 'done':
        raise HTTPException(400, "下载未完成")

    file_path = Path(task.get('save_path') or task.get('file_path') or '')
    if not file_path.exists():
        raise HTTPException(404, "文件已被移动或删除")

    return FileResponse(file_path, filename=task.get('filename', 'media.mp4'),
                        media_type='application/octet-stream')


# ── Open folder (local OS integration) ────────────────────────────────────────
@app.post("/api/open-folder")
async def open_folder(req: OpenFolderRequest):
    """Open the file's containing folder in the OS file manager, with the
    file selected. Windows uses `explorer.exe /select,`; others fall back to
    opening the parent dir.

    Path safety: only allow paths that exist; reject path traversal in the
    response (the OS call itself is the trust boundary on a local machine).
    """
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    real = str(p.resolve())
    try:
        if os.name == 'nt':
            # /select needs backslashes; quoted to survive spaces.
            import subprocess
            subprocess.Popen(
                ['explorer.exe', '/select,', real],
                shell=False,
            )
        else:
            import subprocess
            target = str(p.parent) if p.is_file() else real
            subprocess.Popen(['xdg-open', target])
    except Exception as exc:
        raise HTTPException(500, f"无法打开文件夹：{exc}")
    return {'ok': True, 'path': real}


# ── SSE progress stream ───────────────────────────────────────────────────────
@app.get("/api/progress/stream/{task_id}")
async def progress_stream(task_id: str):
    """Server-Sent Events stream of task progress snapshots.

    Emits one `data:` line per status change until the task reaches a terminal
    state (done/error), then closes.
    """
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    q = _sse_subscribe(task_id)

    def event_stream() -> Iterator[bytes]:
        try:
            # send current snapshot immediately
            snap = _get_task(task_id)
            if snap:
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n".encode()
                if snap.get('status') in ('done', 'error'):
                    return
            while True:
                try:
                    snap = q.get(timeout=15)
                except Exception:
                    # heartbeat keeps the connection alive
                    yield b": ping\n\n"
                    continue
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n".encode()
                if snap.get('status') in ('done', 'error'):
                    return
        finally:
            _sse_unsubscribe(task_id, q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Batch download ────────────────────────────────────────────────────────────
@app.post("/api/batch")
async def batch_download(req: BatchRequest):
    """Enqueue multiple URLs for the same platform. Returns per-url task ids.

    Concurrency is bounded by a semaphore; each URL reuses the existing
    single-download path. For Twitter multi-URL, each must be a video tweet.
    """
    if not req.urls:
        raise HTTPException(400, "urls 不能为空")
    platform = req.platform.lower()
    if platform not in ('twitter', 'youtube', 'bilibili'):
        raise HTTPException(400, "platform 必须是 twitter/youtube/bilibili")

    concurrency = max(1, min(req.concurrency or 3, 6))
    sem = threading.Semaphore(concurrency)
    results: List[dict] = []
    results_lock = threading.Lock()

    def enqueue(url: str, index: int):
        u = normalize_url(url)
        tid = None
        # validate per platform; skip invalid
        if platform == 'twitter':
            if parse_tweet_url(u):
                tid = _twitter_batch_task(u, req, sem)
        elif platform == 'youtube':
            if parse_youtube_url(u):
                fmt = req.format_id or _pick_default_format(u, req, 'youtube')
                tid = _guarded_start(sem, u, fmt, req, 'youtube')
        elif platform == 'bilibili':
            if parse_bilibili_url(u):
                fmt = req.format_id or _pick_default_format(u, req, 'bilibili')
                tid = _guarded_start(sem, u, fmt, req, 'bilibili')

        with results_lock:
            results.append({
                'index': index, 'url': url,
                'task_id': tid,
                'skipped': tid is None,
            })

    threads = []
    for i, u in enumerate(req.urls):
        t = threading.Thread(target=enqueue, args=(u, i), daemon=True)
        t.start()
        threads.append(t)
    # we return immediately with task ids as they get created; join briefly so
    # task_ids are populated before response
    for t in threads:
        t.join(timeout=5)

    results.sort(key=lambda r: r['index'])
    return {'tasks': results, 'platform': platform}


def _guarded_start(sem: threading.Semaphore, url: str, fmt: str,
                   req: 'BatchRequest', platform: str) -> str:
    """Acquire semaphore then kick off a normal download task."""
    sem.acquire()
    try:
        return _start_video_download(
            url, fmt, req.subtitle_lang,
            req.proxy, req.cookie_content, platform, req.save_dir,
        )
    finally:
        # release after the task is *created* (not finished); the semaphore
        # here just throttles task creation burst to avoid hammering the API.
        sem.release()


def _twitter_batch_task(url: str, req: 'BatchRequest',
                        sem: threading.Semaphore) -> str:
    """Twitter batch: analyze first to get a real format_id, then download.

    yt-dlp Twitter format_ids are dynamic; we can't pass a user default safely.
    """
    sem.acquire()
    try:
        # pick best video format by analyzing first
        _, cookie_file = parse_cookies(req.cookie_content)
        fmt_id = '0'
        try:
            opts: dict = {'quiet': True, 'no_warnings': True}
            if req.proxy:
                opts['proxy'] = req.proxy
            if cookie_file:
                opts['cookiefile'] = cookie_file
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = extract_video_formats(info)
            if fmts:
                fmt_id = fmts[0]['format_id']  # best
        except Exception:
            pass
        finally:
            cleanup(cookie_file)
        return _start_video_download(
            url, fmt_id, None,
            req.proxy, req.cookie_content, 'twitter', req.save_dir,
        )
    finally:
        sem.release()


def _pick_default_format(url: str, req: 'BatchRequest', platform: str) -> str:
    """Analyze and return the best format_id for batch (best video)."""
    _, cookie_file = parse_cookies(req.cookie_content)
    try:
        opts: dict = {'quiet': True, 'no_warnings': True}
        if req.proxy:
            opts['proxy'] = req.proxy
        if cookie_file:
            opts['cookiefile'] = cookie_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        fmts = extract_video_formats(info)
        if fmts:
            return fmts[0]['format_id']
    except Exception:
        pass
    finally:
        cleanup(cookie_file)
    return 'best'


# ── History ───────────────────────────────────────────────────────────────────
@app.get("/api/history")
async def history(limit: int = 50):
    """Return recent tasks, newest first."""
    limit = max(1, min(limit, 200))
    return {'tasks': _list_tasks(limit)}


def _purge_task_file(task: dict) -> bool:
    """Delete the saved file for a task. Returns True if a file was removed.

    Only the single file at save_path is touched — never the directory or
    siblings. Missing files are silently skipped.
    """
    path_str = task.get('save_path') or task.get('file_path') or ''
    if not path_str:
        return False
    p = Path(path_str)
    try:
        if p.is_file():
            p.unlink()
            logger.info(f"deleted file on history purge: {p}")
            return True
    except OSError as exc:
        logger.warning(f"failed to delete file {p}: {exc}")
    return False


@app.delete("/api/history/{task_id}")
async def delete_history_one(task_id: str, delete_file: bool = False):
    """Delete a single history record. Optionally remove the saved file.

    File deletion is irreversible; only the file at save_path is touched.
    """
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "记录不存在")

    file_removed = _purge_task_file(task) if delete_file else False

    with _db_lock, _db_conn() as c:
        c.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
    return {'ok': True, 'file_removed': file_removed}


@app.delete("/api/history")
async def delete_history_all(delete_file: bool = False):
    """Clear all history records. Optionally remove all saved files.

    Iterates over done tasks with a save_path; missing files are skipped.
    """
    tasks = _list_tasks(limit=200)
    files_removed = 0
    if delete_file:
        for t in tasks:
            if _purge_task_file(t):
                files_removed += 1

    with _db_lock, _db_conn() as c:
        c.execute("DELETE FROM tasks")
    logger.info(
        f"cleared history: {len(tasks)} records, {files_removed} files removed"
    )
    return {'ok': True, 'cleared': len(tasks), 'files_removed': files_removed}


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 50)
    print("  X Media Downloader")
    print("  http://localhost:8000")
    print("=" * 50)
    uvicorn.run(app, host='0.0.0.0', port=8000)
