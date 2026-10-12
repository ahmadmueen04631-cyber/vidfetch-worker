import os
import re
import tempfile
import shutil
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Response, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import requests

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = "ffmpeg"

app = FastAPI(
    title="VidFetch Free Extractor Worker",
    description="High-performance media extraction microservice powered by yt-dlp",
    version="1.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_ydl_base_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "ffmpeg_location": FFMPEG_BIN,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
    }
    cookie_file = os.getenv("COOKIES_FILE", "cookies.txt")
    if os.path.exists(cookie_file):
        opts["cookiefile"] = cookie_file
    elif os.getenv("COOKIES_TEXT"):
        tmp_cookie = "cookies.tmp.txt"
        with open(tmp_cookie, "w") as f:
            f.write(os.getenv("COOKIES_TEXT", ""))
        opts["cookiefile"] = tmp_cookie
    return opts

@app.get("/")
def health_check():
    return {
        "status": "healthy",
        "service": "vidfetch-worker",
        "version": "1.2.0",
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg_available": bool(FFMPEG_BIN and os.path.exists(FFMPEG_BIN))
    }

@app.get("/info")
def get_video_info(url: str = Query(..., description="Target video URL")):
    """
    Extracts complete metadata and genuine format download options
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

            # 1. Genuine Audio-only Track (MP3)
            # Find best audio track
            audio_formats = [
                f for f in raw_formats
                if (f.get("vcodec") == "none" or not f.get("vcodec")) and f.get("acodec") != "none"
            ]
            best_audio = audio_formats[-1] if audio_formats else None
            audio_size = best_audio.get("filesize") or best_audio.get("filesize_approx") if best_audio else None

            download_options.append({
                "id": "worker-audio-mp3",
                "label": "Audio Track (MP3 320kbps)",
                "format": "mp3",
                "quality": "audio",
                "isAudioOnly": True,
                "fileSize": audio_size,
                "downloadUrl": best_audio.get("url") if best_audio and best_audio.get("url") else url,
            })

            # 2. Extract distinct video resolutions
            # Check what video heights actually exist
            available_heights = set()
            for f in raw_formats:
                h = f.get("height")
                if h and f.get("vcodec") != "none":
                    available_heights.add(h)

            # Standard resolution tiers
            tiers = [
                {"height": 1080, "label": "Full HD (1080p MP4)", "quality": "1080p"},
                {"height": 720, "label": "HD (720p MP4)", "quality": "720p"},
                {"height": 480, "label": "SD (480p MP4)", "quality": "480p"},
                {"height": 360, "label": "Compact (360p MP4)", "quality": "360p"},
            ]

            max_source_h = max(available_heights) if available_heights else 360

            for tier in tiers:
                target_h = tier["height"]
                # Only offer this tier if the source video has this resolution or if it's the fallback
                if target_h > max_source_h and target_h != 360:
                    continue

                # Find direct stream format matching this resolution
                # Prioritize progressive (has audio + video)
                prog_matching = [
                    f for f in raw_formats
                    if f.get("height") == target_h and f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("url")
                ]

                # Or video-only format at this height
                vid_matching = [
                    f for f in raw_formats
                    if f.get("height") == target_h and f.get("vcodec") != "none" and f.get("url")
                ]

                best_match = prog_matching[-1] if prog_matching else (vid_matching[-1] if vid_matching else None)
                stream_url = best_match.get("url") if best_match else None
                size = best_match.get("filesize") or best_match.get("filesize_approx") if best_match else None

                # If no direct format URL at this height, fall back to watch URL (which download route resolves)
                download_options.append({
                    "id": f"worker-{tier['quality']}",
                    "label": tier["label"],
                    "format": "mp4",
                    "quality": tier["quality"],
                    "fileSize": size,
                    "downloadUrl": stream_url or url,
                })

            # If no video tiers matched, add fallback 360p
            if len([d for d in download_options if not d.get("isAudioOnly")]) == 0:
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
                "downloads": download_options
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/extract")
def extract_video(
    url: str = Query(..., description="Target video URL"),
    format: str = Query("mp4", description="Desired format (mp4 or mp3)"),
    height: int = Query(1080, description="Target height")
):
    """
    Resolves direct binary CDN stream URL for a given format
    """
    try:
        opts = get_ydl_base_opts()
        opts["skip_download"] = True

        if format == "mp3":
            opts["format"] = "bestaudio/best"
        else:
            # Look for progressive first, then best video up to height
            opts["format"] = f"best[height<={height}][ext=mp4]/bestvideo[height<={height}]+bestaudio/best"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise HTTPException(status_code=404, detail="No video stream found")

            direct_url = info.get("url")

            # If extracting audio specifically
            if format == "mp3":
                raw_formats = info.get("formats", [])
                audio_formats = [
                    f for f in raw_formats
                    if (f.get("vcodec") == "none" or not f.get("vcodec")) and f.get("acodec") != "none" and f.get("url")
                ]
                if audio_formats:
                    direct_url = audio_formats[-1].get("url")

            if not direct_url and "formats" in info:
                formats = info["formats"]
                matching = [f for f in formats if f.get("url")]
                if matching:
                    direct_url = matching[-1].get("url")

            if not direct_url:
                raise HTTPException(status_code=502, detail="Unable to resolve stream URL")

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
