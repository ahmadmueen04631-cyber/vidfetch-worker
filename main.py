import os
import re
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = "ffmpeg"

app = FastAPI(
    title="VidFetch Free Extractor Worker",
    description="High-performance media extraction powered by yt-dlp",
    version="1.3.0"
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

    # Handle cases where newlines were escaped as literal '\n'
    if "\\n" in cookie_text:
        cookie_text = cookie_text.replace("\\r\\n", "\n").replace("\\n", "\n")
    cookie_text = cookie_text.replace("\r\n", "\n").strip()

    # Ensure valid Netscape header
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
        }
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
        # When browser cookies are provided, use web and mweb clients which accept session cookies
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["web", "mweb"]
            }
        }
    else:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "android", "web", "mweb"]
            }
        }
    return opts

# ─── Endpoints ───────────────────────────────────────────────────

@app.get("/")
def health_check():
    debug = _cookie_debug_info()
    return {
        "status": "healthy",
        "service": "vidfetch-worker",
        "version": "1.3.1",
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg_available": bool(FFMPEG_BIN and os.path.exists(FFMPEG_BIN)),
        "cookies_configured": debug.get("configured", False),
        "cookie_debug": debug,
    }

@app.get("/info")
def get_video_info(url: str = Query(..., description="Target video URL")):
    """
    Extract video metadata + download options.
    The download options contain direct googlevideo.com CDN stream URLs.
    """
    try:
        opts = get_ydl_base_opts()
        opts["skip_download"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="No video found at URL")

            raw_formats = info.get("formats", [])
            download_options: List[Dict[str, Any]] = []

            # ── Audio-only track ──────────────────────────────
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
                "downloadUrl": best_audio.get("url") if best_audio else url,
            })

            # ── Video resolution tiers ────────────────────────
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
                    continue  # source video can't support this tier

                # Prefer progressive (has audio+video), otherwise video-only at this height
                matching = [
                    f for f in video_formats if f["height"] == target_h
                ]
                prog = [f for f in matching if f.get("acodec") not in (None, "none")]
                best = (prog[-1] if prog else matching[-1]) if matching else None

                stream_url = best["url"] if best else url
                size = (best.get("filesize") or best.get("filesize_approx")) if best else None

                download_options.append({
                    "id": f"worker-{tier['quality']}",
                    "label": tier["label"],
                    "format": "mp4",
                    "quality": tier["quality"],
                    "fileSize": size,
                    "downloadUrl": stream_url,
                })
                added = True

            if not added:
                # Absolute fallback — use the watch URL and let /extract handle it
                download_options.append({
                    "id": "worker-360p",
                    "label": "Standard Video (MP4)",
                    "format": "mp4",
                    "quality": "360p",
                    "downloadUrl": url,
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

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/extract")
def extract_video(
    url: str = Query(..., description="Target video URL"),
    format: str = Query("mp4", description="Desired format: mp4 or mp3"),
    height: int = Query(1080, description="Target resolution height (e.g. 1080)")
):
    """
    Resolves the best direct binary CDN stream URL for the given format/height.
    Used by the download proxy when the downloadUrl from /info needs re-resolving.
    """
    try:
        opts = get_ydl_base_opts()
        opts["skip_download"] = True

        if format == "mp3":
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        else:
            opts["format"] = (
                f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={height}][ext=mp4]/"
                f"best[height<={height}]/best"
            )

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="No stream found")

            raw = info.get("formats", [])

            if format == "mp3":
                audio_only = [
                    f for f in raw
                    if f.get("url") and
                       f.get("vcodec") in (None, "none") and
                       f.get("acodec") not in (None, "none")
                ]
                direct_url = audio_only[-1]["url"] if audio_only else info.get("url")
            else:
                direct_url = info.get("url")
                if not direct_url:
                    vid_only = [
                        f for f in raw
                        if f.get("url") and f.get("height") and
                           (f.get("height") <= height)
                    ]
                    if vid_only:
                        direct_url = vid_only[-1]["url"]

            if not direct_url:
                raise HTTPException(status_code=502, detail="Unable to resolve binary stream URL")

            return {
                "url": direct_url,
                "title": info.get("title", "Video"),
                "format": format,
                "quality": f"{info.get('height', height)}p" if format != "mp3" else "audio",
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
