# YouTube & Bilibili Video Download Design

## Overview

Add YouTube and Bilibili video download support to the existing X/Twitter Media Downloader.

## Requirements

- Download YouTube and Bilibili videos with resolution selection
- Subtitle burn-in (hard subtitles via ffmpeg)
- Separate UI tabs per platform (X/Twitter, YouTube, Bilibili)
- Independent proxy/cookie settings per platform

## Architecture

### Frontend

- Tab bar at page top: X/Twitter | YouTube | Bilibili
- Each tab has its own URL input, advanced settings (proxy, cookie), and result area
- Result area shows: info card (title, author, thumbnail, description) + format selector + subtitle selector + download button

### Backend

New API endpoints:

- `POST /api/yt/analyze` — extract YouTube video info + available formats + subtitles
- `POST /api/yt/download` — download YouTube video with optional subtitle burn-in
- `POST /api/bili/analyze` — extract Bilibili video info + available formats + subtitles
- `POST /api/bili/download` — download Bilibili video with optional subtitle burn-in

### Data Flow

1. User inputs URL → clicks analyze
2. Backend uses yt-dlp `extract_info(download=False)` to get metadata
3. Returns: title, author, thumbnail, description, formats list, subtitles list
4. User selects format + optional subtitle language → clicks download
5. Backend downloads video via yt-dlp, burns in subtitle via ffmpeg if selected
6. Returns file stream to browser

### Subtitle Burn-in

- Requires ffmpeg installed on server
- Uses yt-dlp `--write-subs --sub-langs <lang>` to fetch subtitle
- Uses ffmpeg to burn subtitle into video (hard sub)
- If subtitle requested but unavailable, downloads without subtitle and shows warning

### URL Validation

- YouTube: `youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/`
- Bilibili: `bilibili.com/video/BV`, `b23.tv/`

## Files Changed

- `app.py` — add 4 API routes, URL parsers, subtitle burn-in logic
- `static/index.html` — add tab UI, YouTube/Bili input areas, JS logic
- `requirements.txt` — no new dependencies (yt-dlp already present)

## Dependencies

- ffmpeg must be installed on the system for subtitle burn-in
