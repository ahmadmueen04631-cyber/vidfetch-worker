import os
import re
import shutil
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import requests

try:
    from pytubefix import YouTube as PytubeFix
    import pytubefix.botGuard.bot_guard as bg
    node_bin = shutil.which("node") or "/usr/bin/node"
    if node_bin and os.path.exists(node_bin):
        bg.NODE_PATH = node_bin
    PYTUBE_AVAILABLE = True
except Exception:
    PYTUBE_AVAILABLE = False

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = "ffmpeg"

app = FastAPI(
    title="VidFetch Free Extractor Worker",
    description="High-performance media extraction powered by yt-dlp & pytubefix",
    version="1.5.1"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Cookie helpers ─────────────────────────────────────────────

def _write_cookies_to_tmp() -> Optional[str]:
    """If COOKIES_TEXT env var is set, write it to a temp file and return its path."""
    cookie_text = os.getenv("COOKIES_TEXT")
    if not cookie_text:
        return None

    if "\\n" in cookie_text:
        cookie_text = cookie_text.replace("\\r\\n", "\n").replace("\\n", "\n")
    cookie_text = cookie_text.replace("\r\n", "\n").strip()

    if not cookie_text.startswith("# Netscape"):
        cookie_text = "# Netscape HTTP Cookie File\n" + cookie_text

    tmp = "/tmp/yt_cookies.txt"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(cookie_text + "\n")
        return tmp
    except Exception:
        return None

def _cookie_file_path() -> Optional[str]:
    """Return the best available cookie file path, or None."""
    env_file = os.getenv("COOKIES_FILE")
    if env_file and os.path.exists(env_file):
        return env_file
    tmp = _write_cookies_to_tmp()
    if tmp and os.path.exists(tmp):
        return tmp
    if os.path.exists("cookies.txt"):
        return "cookies.txt"
    return None

def _cookie_debug_info() -> Dict[str, Any]:
    c_path = _cookie_file_path()
    if not c_path or not os.path.exists(c_path):
        return {"configured": False}
    try:
        with open(c_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        domains = set()
        for line in lines:
            parts = line.split("\t")
            if parts:
                domains.add(parts[0])
        return {
            "configured": True,
            "line_count": len(lines),
            "has_youtube": any("youtube" in d for d in domains),
            "domains": sorted(list(domains))[:8],
        }
    except Exception as e:
        return {"configured": True, "error": str(e)}

# ─── yt-dlp options factory ─────────────────────────────────────

def get_ydl_base_opts() -> Dict[str, Any]:
    cookie_file = _cookie_file_path()
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "ffmpeg_location": FFMPEG_BIN,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        # Always prioritize android_vr and android to bypass datacenter bot walls
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "android", "web", "mweb"]
            }
        }
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts

def is_youtube_url(url: str) -> bool:
    low = url.lower()
    return any(domain in low for domain in ["youtube.com", "youtu.be", "youtube-nocookie.com"])

def get_base_service_url(request: Request) -> str:
    env_override = os.getenv("SERVICE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if env_override:
        return env_override.rstrip("/")
    base = str(request.base_url).rstrip("/")
    if "onrender.com" in base and base.startswith("http://"):
        base = "https://" + base[7:]
    return base

# ─── YouTube Extraction with pytubefix ────────────────────────────

def extract_youtube_pytube(url: str, base_url: str) -> Dict[str, Any]:
    if not PYTUBE_AVAILABLE:
        raise RuntimeError("pytubefix is not installed or available")

    # Ensure system node is pointed to for PO token generation
    try:
        node_bin = shutil.which("node") or "/usr/bin/node"
        if node_bin and os.path.exists(node_bin):
            bg.NODE_PATH = node_bin
    except Exception:
        pass

    clients = ["WEB", "ANDROID", "MWEB"]
    yt = None
    last_err = None
    for client in clients:
        try:
            candidate = PytubeFix(url, client=client)
            _ = candidate.title
            yt = candidate
            break
        except Exception as e:
            last_err = e
            continue

    if not yt:
        raise RuntimeError(f"All pytubefix clients failed for YouTube URL: {last_err}")

    downloads: List[Dict[str, Any]] = []

    # 1. Audio stream (MP3/M4A)
    audio_s = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
    if audio_s:
        downloads.append({
            "id": "pytube-audio",
            "label": "Audio Track (MP3)",
            "format": "mp3",
            "quality": "audio",
            "isAudioOnly": True,
            "fileSize": getattr(audio_s, "filesize_approx", None),
            "downloadUrl": f"{base_url}/stream?url={quote_plus(url)}&itag={audio_s.itag}&format=mp3",
        })

    # 2. Progressive streams (Audio + Video combined)
    prog_streams = yt.streams.filter(progressive=True)
    added_heights = set()
    for s in prog_streams:
        h = int(s.resolution.replace("p", "")) if s.resolution else 360
        added_heights.add(h)
        downloads.append({
            "id": f"pytube-{s.resolution}",
            "label": f"HD ({s.resolution} MP4)" if h >= 720 else f"Standard ({s.resolution} MP4)",
            "format": "mp4",
            "quality": s.resolution,
            "isAudioOnly": False,
            "fileSize": getattr(s, "filesize_approx", None),
            "downloadUrl": f"{base_url}/stream?url={quote_plus(url)}&itag={s.itag}&format=mp4",
        })

    # 3. Resolution tiers if not already covered
    target_tiers = [
        {"height": 1080, "res": "1080p", "label": "Full HD (1080p MP4)"},
        {"height": 720,  "res": "720p",  "label": "HD (720p MP4)"},
        {"height": 480,  "res": "480p",  "label": "SD (480p MP4)"},
    ]

    for tier in target_tiers:
        if tier["height"] not in added_heights:
            s = yt.streams.filter(res=tier["res"], file_extension="mp4").first()
            if s:
                downloads.append({
                    "id": f"pytube-{tier['res']}",
                    "label": tier["label"],
                    "format": "mp4",
                    "quality": tier["res"],
                    "isAudioOnly": False,
                    "fileSize": getattr(s, "filesize_approx", None),
                    "downloadUrl": f"{base_url}/stream?url={quote_plus(url)}&itag={s.itag}&format=mp4",
                })
                added_heights.add(tier["height"])

    if not downloads:
        downloads.append({
            "id": "pytube-default",
            "label": "Standard Video (MP4)",
            "format": "mp4",
            "quality": "360p",
            "isAudioOnly": False,
            "downloadUrl": f"{base_url}/stream?url={quote_plus(url)}&format=mp4",
        })

    def sort_key(d):
        if d.get("isAudioOnly"):
            return -1
        res = d.get("quality", "0p").replace("p", "")
        return int(res) if res.isdigit() else 0

    downloads.sort(key=sort_key, reverse=True)

    return {
        "metadata": {
            "platform": "youtube",
            "title": yt.title or "YouTube Video",
            "thumbnail": yt.thumbnail_url,
            "duration": yt.length,
            "author": yt.author or "YouTube Creator",
            "authorHandle": None,
            "viewCount": yt.views,
            "likeCount": None,
            "originalUrl": url,
        },
        "downloads": downloads,
    }

# ─── Universal Extractor with yt-dlp ────────────────────────────

def extract_ytdlp(url: str, base_url: str) -> Dict[str, Any]:
    opts = get_ydl_base_opts()
    opts["skip_download"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise HTTPException(status_code=404, detail="No video found at URL")

        raw_formats = info.get("formats", [])
        download_options: List[Dict[str, Any]] = []

        # Audio-only
        audio_formats = [
            f for f in raw_formats
            if f.get("url") and
               (f.get("vcodec") in (None, "none") or not f.get("vcodec")) and
               f.get("acodec") not in (None, "none")
        ]
        best_audio = audio_formats[-1] if audio_formats else None
        download_options.append({
            "id": "worker-audio-mp3",
            "label": "Audio Track (MP3)",
            "format": "mp3",
            "quality": "audio",
            "isAudioOnly": True,
            "fileSize": (best_audio.get("filesize") or best_audio.get("filesize_approx")) if best_audio else None,
            "downloadUrl": f"{base_url}/stream?url={quote_plus(url)}&format=mp3",
        })

        # Video formats
        video_formats = [
            f for f in raw_formats
            if f.get("url") and f.get("height") and
               f.get("vcodec") not in (None, "none")
        ]
        available_heights = {f["height"] for f in video_formats}
        max_h = max(available_heights) if available_heights else 360

        tiers = [
            {"height": 1080, "label": "Full HD (1080p MP4)", "quality": "1080p"},
            {"height": 720,  "label": "HD (720p MP4)",       "quality": "720p"},
            {"height": 480,  "label": "SD (480p MP4)",        "quality": "480p"},
            {"height": 360,  "label": "Compact (360p MP4)",   "quality": "360p"},
        ]

        added = False
        for tier in tiers:
            target_h = tier["height"]
            if target_h > max_h:
                continue

            matching = [f for f in video_formats if f["height"] == target_h]
            prog = [f for f in matching if f.get("acodec") not in (None, "none")]
            best = (prog[-1] if prog else matching[-1]) if matching else None

            size = (best.get("filesize") or best.get("filesize_approx")) if best else None

            download_options.append({
                "id": f"worker-{tier['quality']}",
                "label": tier["label"],
                "format": "mp4",
                "quality": tier["quality"],
                "isAudioOnly": False,
                "fileSize": size,
                "downloadUrl": f"{base_url}/stream?url={quote_plus(url)}&format=mp4&height={target_h}",
            })
            added = True

        if not added:
            download_options.append({
                "id": "worker-360p",
                "label": "Standard Video (MP4)",
                "format": "mp4",
                "quality": "360p",
                "isAudioOnly": False,
                "downloadUrl": f"{base_url}/stream?url={quote_plus(url)}&format=mp4",
            })

        return {
            "metadata": {
                "platform": info.get("extractor_key", "generic").lower(),
                "title": info.get("title", "Video"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "author": info.get("uploader") or info.get("channel") or "Creator",
                "authorHandle": info.get("uploader_id"),
                "viewCount": info.get("view_count"),
                "likeCount": info.get("like_count"),
                "originalUrl": url,
            },
            "downloads": download_options,
        }

# ─── Endpoints ───────────────────────────────────────────────────

@app.get("/")
def health_check():
    debug = _cookie_debug_info()
    node_path = getattr(bg, "NODE_PATH", None) if PYTUBE_AVAILABLE else None
    return {
        "status": "healthy",
        "service": "vidfetch-worker",
        "version": "1.5.1",
        "pytube_available": PYTUBE_AVAILABLE,
        "node_path": node_path,
        "node_exists": bool(node_path and os.path.exists(node_path)),
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg_available": bool(FFMPEG_BIN and os.path.exists(FFMPEG_BIN)),
        "cookies_configured": debug.get("configured", False),
        "cookie_debug": debug,
    }

@app.get("/info")
def get_video_info(request: Request, url: str = Query(..., description="Target video URL")):
    """
    Extract video metadata and download options for any platform.
    Automatically leverages yt-dlp (with android_vr) and pytubefix (with PO tokens).
    """
    base_url = get_base_service_url(request)

    if is_youtube_url(url):
        # 1. Try yt-dlp with android_vr first (reliable on datacenter IPs without bot block)
        try:
            return extract_ytdlp(url, base_url)
        except Exception as ytdlp_err:
            # 2. Fall back to pytubefix if yt-dlp fails
            if PYTUBE_AVAILABLE:
                try:
                    return extract_youtube_pytube(url, base_url)
                except Exception as pytube_err:
                    raise HTTPException(
                        status_code=500,
                        detail=f"yt-dlp error: {ytdlp_err}; pytubefix error: {pytube_err}"
                    )
            raise HTTPException(status_code=500, detail=str(ytdlp_err))

    # All non-YouTube platforms (TikTok, Instagram, Twitter, Facebook, etc.)
    try:
        return extract_ytdlp(url, base_url)
    except Exception as ytdlp_err:
        raise HTTPException(status_code=500, detail=str(ytdlp_err))

@app.get("/stream")
def stream_video(
    url: str = Query(..., description="Original video watch URL or direct stream URL"),
    itag: Optional[int] = Query(None, description="Stream itag for pytubefix"),
    format: str = Query("mp4", description="Format: mp4 or mp3"),
    height: Optional[int] = Query(None, description="Resolution height")
):
    """
    Direct high-performance binary streaming proxy.
    Downloads from the resolved source CDN on Render and streams chunks to the client.
    """
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }

    if is_youtube_url(url):
        # 1. Try yt-dlp to resolve the exact stream and headers
        try:
            opts = get_ydl_base_opts()
            opts["skip_download"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get("formats", [])

                chosen_format = None
                if format == "mp3":
                    audio_formats = [
                        f for f in formats
                        if f.get("url") and f.get("acodec") not in (None, "none")
                    ]
                    chosen_format = audio_formats[-1] if audio_formats else None
                else:
                    target_h = height or 720
                    vid_formats = [
                        f for f in formats
                        if f.get("url") and f.get("height") and f.get("height") <= target_h
                    ]
                    chosen_format = vid_formats[-1] if vid_formats else None

                if chosen_format and chosen_format.get("url"):
                    stream_headers = chosen_format.get("http_headers", default_headers)
                    def chunk_gen():
                        with requests.get(chosen_format["url"], headers=stream_headers, stream=True, timeout=20) as r:
                            r.raise_for_status()
                            for chunk in r.iter_content(chunk_size=128 * 1024):
                                if chunk:
                                    yield chunk

                    media_type = "audio/mpeg" if format == "mp3" else "video/mp4"
                    filename = f"vidfetch-{format}.{format}"
                    resp_headers = {
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Accept-Ranges": "bytes",
                    }
                    size = chosen_format.get("filesize") or chosen_format.get("filesize_approx")
                    if size:
                        resp_headers["Content-Length"] = str(size)

                    return StreamingResponse(chunk_gen(), media_type=media_type, headers=resp_headers)

        except Exception as e:
            pass

        # 2. Fall back to pytubefix streaming if yt-dlp did not resolve
        if PYTUBE_AVAILABLE:
            try:
                yt = PytubeFix(url, client="WEB")
                s = None
                if itag:
                    s = yt.streams.get_by_itag(itag)
                if not s and format == "mp3":
                    s = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
                if not s and height:
                    s = yt.streams.filter(res=f"{height}p", file_extension="mp4").first()
                if not s:
                    s = yt.streams.filter(progressive=True).order_by("resolution").desc().first()
                if not s:
                    s = yt.streams.filter(file_extension="mp4").order_by("resolution").desc().first()

                if s and s.url:
                    def chunk_generator():
                        with requests.get(s.url, headers=default_headers, stream=True, timeout=20) as r:
                            r.raise_for_status()
                            for chunk in r.iter_content(chunk_size=128 * 1024):
                                if chunk:
                                    yield chunk

                    media_type = "audio/mpeg" if format == "mp3" else "video/mp4"
                    filename = f"vidfetch-{format}.{format}"
                    resp_headers = {
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Accept-Ranges": "bytes",
                    }
                    size = getattr(s, "filesize_approx", None)
                    if size:
                        resp_headers["Content-Length"] = str(size)

                    return StreamingResponse(chunk_generator(), media_type=media_type, headers=resp_headers)

            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Streaming error: {e}")

    # General direct stream URL proxy
    target_url = url
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Invalid stream URL")

    def general_chunk_generator():
        with requests.get(target_url, headers=default_headers, stream=True, timeout=20) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if chunk:
                    yield chunk

    media_type = "audio/mpeg" if format == "mp3" else "video/mp4"
    return StreamingResponse(general_chunk_generator(), media_type=media_type)

@app.get("/extract")
def extract_video(
    request: Request,
    url: str = Query(..., description="Target video URL"),
    format: str = Query("mp4", description="Desired format: mp4 or mp3"),
    height: int = Query(1080, description="Target resolution height (e.g. 1080)")
):
    base_url = get_base_service_url(request)
    stream_url = f"{base_url}/stream?url={quote_plus(url)}&format={format}&height={height}"

    try:
        opts = get_ydl_base_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "url": stream_url,
                "title": info.get("title", "Video"),
                "format": format,
                "quality": f"{height}p" if format != "mp3" else "audio",
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
            }
    except Exception:
        return {
            "url": stream_url,
            "title": "Video",
            "format": format,
            "quality": f"{height}p" if format != "mp3" else "audio",
        }
