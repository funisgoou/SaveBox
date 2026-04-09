"""
Media Downloader - FastAPI Backend
Supports X/Twitter, YouTube, and Bilibili video download with resolution selection,
subtitle burn-in, and Twitter article/thread Markdown export.
"""

import os
import re
import json
import uuid
import tempfile
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional, List

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="X Media Downloader", version="1.0.0")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"

# Bearer tokens used by Twitter/X web client (same as yt-dlp)
_AUTH = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
_LEGACY_AUTH = "AAAAAAAAAAAAAAAAAAAAAIK1zgAAAAAA2tUWuhGZ2JceoId5GwYWU5GspY4%3DUq7gzFoCZs1QfwGoVdvSac3IniczZEYXIcDyumCauIXpcAPorE"
_API_BASE = "https://api.x.com/1.1/"
_GRAPHQL_API_BASE = "https://x.com/i/api/graphql/"
_GRAPHQL_ENDPOINT = "2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId"

# ── Download task tracking ─────────────────────────────────────────────────────
_download_tasks: dict = {}
_download_lock = threading.Lock()


def _create_task() -> str:
    task_id = uuid.uuid4().hex[:8]
    with _download_lock:
        _download_tasks[task_id] = {
            'progress': 0,
            'status': 'downloading',
            'file_path': None,
            'filename': None,
            'dldir': None,
            'error': None,
        }
    return task_id


def _update_task(task_id: str, **kwargs):
    with _download_lock:
        if task_id in _download_tasks:
            _download_tasks[task_id].update(kwargs)


def _get_task(task_id: str) -> Optional[dict]:
    with _download_lock:
        t = _download_tasks.get(task_id)
        return dict(t) if t else None


def _yt_progress_hook(task_id: str):
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            pct = int(downloaded / total * 100) if total > 0 else 0
            _update_task(task_id, progress=pct, status='downloading')
        elif d['status'] == 'finished':
            _update_task(task_id, progress=100, status='merging')
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


def parse_cookies(content: Optional[str]):
    """Return (cookies_dict, temp_netscape_file_path | None)."""
    if not content or not content.strip():
        return {}, None

    cookies: dict = {}
    text = content.strip()
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

    task_id = _create_task()
    proxy = req.proxy
    cookie_content = req.cookie_content
    format_id = req.format_id

    def run():
        _, cookie_file = parse_cookies(cookie_content)
        dldir = DOWNLOADS_DIR / tid
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

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
            except Exception:
                opts['format'] = format_id
                opts.pop('merge_output_format', None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)

            files = list(dldir.glob('*'))
            if not files:
                _update_task(task_id, status='error', error='下载文件未生成')
                return

            fp = files[0]
            filename = f"{tid}_{format_id}{fp.suffix}"
            _update_task(task_id, status='done', file_path=str(fp),
                        filename=filename, dldir=str(dldir))
        except Exception as exc:
            shutil.rmtree(dldir, True)
            _update_task(task_id, status='error', error=str(exc))
        finally:
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
                          platform: str) -> str:
    """Create a download task and run it in background. Returns task_id."""
    task_id = _create_task()

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

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
            except Exception:
                opts['format'] = format_id
                opts.pop('merge_output_format', None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)

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

            filename = video_file.stem + video_file.suffix
            _update_task(task_id, status='done', file_path=str(final_file),
                        filename=filename, dldir=str(dldir))
        except Exception as exc:
            shutil.rmtree(dldir, True)
            _update_task(task_id, status='error', error=str(exc))
        finally:
            cleanup(cookie_file)

    threading.Thread(target=run, daemon=True).start()
    return task_id


@app.post("/api/yt/download")
async def yt_download(req: VideoDownloadRequest):
    url = normalize_url(req.url)
    if not parse_youtube_url(url):
        raise HTTPException(400, "无效的 YouTube 链接")
    task_id = _start_video_download(url, req.format_id, req.subtitle_lang,
                                    req.proxy, req.cookie_content, 'youtube')
    return {'task_id': task_id}


@app.post("/api/bili/download")
async def bili_download(req: VideoDownloadRequest):
    url = normalize_url(req.url)
    if not parse_bilibili_url(url):
        raise HTTPException(400, "无效的 B站链接")
    task_id = _start_video_download(url, req.format_id, req.subtitle_lang,
                                    req.proxy, req.cookie_content, 'bilibili')
    return {'task_id': task_id}


# ── Progress & File endpoints ─────────────────────────────────────────────────

@app.get("/api/progress/{task_id}")
async def get_progress(task_id: str):
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/file/{task_id}")
async def get_file(task_id: str, bg: BackgroundTasks):
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task['status'] != 'done':
        raise HTTPException(400, "下载未完成")

    file_path = Path(task['file_path'])
    dldir = task.get('dldir')

    def cleanup_task():
        if dldir:
            shutil.rmtree(dldir, True)
        with _download_lock:
            _download_tasks.pop(task_id, None)

    bg.add_task(cleanup_task)
    return FileResponse(file_path, filename=task.get('filename', 'video.mp4'),
                        media_type='application/octet-stream')


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 50)
    print("  X Media Downloader")
    print("  http://localhost:8000")
    print("=" * 50)
    uvicorn.run(app, host='0.0.0.0', port=8000)
