
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask


app = FastAPI(
    title="GTA SHADOW X",
    description="Video clipping API",
    version="2.3",
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
TEMP_DIR = BASE_DIR / "temp"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_VIDEO_BYTES = 1024 * 1024 * 1024

ALLOWED_CLIP_COUNTS = {5, 10, 20, 30}
ALLOWED_DURATIONS = {15, 30, 45, 60}
ALLOWED_RATIOS = {"9:16", "1:1", "16:9"}

ALLOWED_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv"
}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/outputs",
    StaticFiles(directory=str(OUTPUT_DIR)),
    name="outputs",
)


def run_command(command, timeout=1800):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    except FileNotFoundError as error:
        raise RuntimeError(
            f"Required program not found: {command[0]}"
        ) from error

    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            "Video processing timed out."
        ) from error

    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or "Command failed"
        ).strip()

        raise RuntimeError(detail[-2500:])

    return result.stdout.strip()


def validate_options(clips, duration, ratio):

    if clips not in ALLOWED_CLIP_COUNTS:
        raise HTTPException(
            status_code=400,
            detail="Choose 5, 10, 20 or 30 clips."
        )

    if duration not in ALLOWED_DURATIONS:
        raise HTTPException(
            status_code=400,
            detail="Choose 15, 30, 45 or 60 seconds."
        )

    if ratio not in ALLOWED_RATIOS:
        raise HTTPException(
            status_code=400,
            detail="Invalid video format."
        )


def validate_youtube_url(url):

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        allowed_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }

        if parsed.scheme not in ("http", "https"):
            raise ValueError()

        if host not in allowed_hosts:
            raise ValueError()

        if host.endswith("youtu.be"):
            if not parsed.path.strip("/"):
                raise ValueError()

        if host.endswith("youtube.com"):
            if parsed.path == "/watch":
                if not parse_qs(parsed.query).get("v"):
                    raise ValueError()

            elif not (
                parsed.path.startswith("/shorts/")
                or parsed.path.startswith("/live/")
                or parsed.path.startswith("/embed/")
            ):
                raise ValueError()

        return url

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid YouTube video URL."
        )


def download_authorized_youtube_video(url, job_dir):

    output_template = str(job_dir / "source.%(ext)s")

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-progress",
        "--no-warnings",
        "--socket-timeout", "30",
        "--retries", "3",
        "--fragment-retries", "3",
        "--max-filesize", "1G",
        "--js-runtimes", "deno",
        "--remote-components", "ejs:github",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "-o", output_template,
        url
    ]

    try:
        run_command(command, timeout=1800)

    except RuntimeError as error:
        raise HTTPException(
            status_code=400,
            detail=f"YouTube download failed: {str(error)[-1200:]}"
        ) from error

    candidates = [
        p for p in job_dir.glob("source.*")
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl"))
    ]

    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="YouTube video file not found."
        )

    source = max(
        candidates,
        key=lambda p: p.stat().st_size
    )

    if source.stat().st_size > MAX_VIDEO_BYTES:
        source.unlink(missing_ok=True)

        raise HTTPException(
            status_code=413,
            detail="Maximum video size is 1 GB."
        )

    if source.stat().st_size == 0:
        raise HTTPException(
            status_code=400,
            detail="Downloaded video is empty."
        )

    return source


def get_video_duration(path):

    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ], timeout=60)

    try:
        duration = float(result)

    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "Could not read video duration."
        ) from error

    if duration <= 0:
        raise RuntimeError("Invalid video duration.")

    return duration


def build_video_filter(ratio):

    if ratio == "9:16":
        return (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920"
        )

    if ratio == "1:1":
        return (
            "scale=1080:1080:force_original_aspect_ratio=increase,"
            "crop=1080:1080"
        )

    return (
        "scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080"
    )


def generate_clips(input_path, job_id, clips, duration, ratio):

    total_duration = get_video_duration(input_path)

    if total_duration < 2:
        raise HTTPException(
            status_code=400,
            detail="Video is too short."
        )

    clip_length = min(float(duration), total_duration)

    if total_duration <= clip_length:
        starts = [0.0]

    else:
        last_start = total_duration - clip_length

        starts = [
            i * last_start / (clips - 1)
            for i in range(clips)
        ]

    video_filter = build_video_filter(ratio)

    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    results = []

    try:
        for index, start in enumerate(starts, start=1):

            filename = f"clip_{index:02d}.mp4"
            output_path = job_dir / filename

            run_command([
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-i", str(input_path),
                "-t", str(clip_length),
                "-vf", video_filter,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                str(output_path)
            ], timeout=1800)

            results.append({
                "name": filename,
                "url": f"/outputs/{job_id}/{filename}",
                "download_url": f"/outputs/{job_id}/{filename}",
                "start": round(start, 2),
                "duration": round(clip_length, 2)
            })

    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    return {
        "success": True,
        "app": "GTA SHADOW X",
        "job_id": job_id,
        "total_clips": len(results),
        "method": "evenly_spaced",
        "clips": results
    }


@app.get("/")
def home():

    index_file = BASE_DIR / "index.html"

    if not index_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="index.html was not found."
        )

    return FileResponse(
        str(index_file),
        media_type="text/html"
    )


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():

    sitemap_file = BASE_DIR / "sitemap.xml"

    if not sitemap_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="sitemap.xml was not found."
        )

    return FileResponse(
        str(sitemap_file),
        media_type="application/xml"
    )


@app.get("/robots.txt", include_in_schema=False)
def robots():

    robots_file = BASE_DIR / "robots.txt"

    if not robots_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="robots.txt was not found."
        )

    return FileResponse(
        str(robots_file),
        media_type="text/plain"
    )


@app.get("/health")
def health():

    return {
        "status": "healthy",
        "app": "GTA SHADOW X",
        "version": "2.3"
    }


@app.post("/api/download-youtube")
async def download_youtube_video(request: Request):

    try:
        payload = await request.json()

    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail="Request body must be valid JSON."
        ) from error

    url = validate_youtube_url(
        str(payload.get("url", "")).strip()
    )

    job_id = uuid.uuid4().hex
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        source = download_authorized_youtube_video(
            url,
            job_dir
        )

        return FileResponse(
            path=str(source),
            media_type="application/octet-stream",
            filename=f"GTA-SHADOW-X-video{source.suffix}",
            background=BackgroundTask(
                shutil.rmtree,
                str(job_dir),
                ignore_errors=True
            )
        )

    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


@app.post("/api/clip-url")
async def create_clips_from_youtube(request: Request):

    try:
        payload = await request.json()

    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail="Request body must be valid JSON."
        ) from error

    url = validate_youtube_url(
        str(payload.get("url", "")).strip()
    )

    try:
        clips = int(payload.get("clips", 10))
        duration = int(payload.get("duration", 30))

    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=400,
            detail="clips and duration must be whole numbers."
        ) from error

    ratio = str(payload.get("ratio", "9:16"))

    validate_options(clips, duration, ratio)

    job_id = uuid.uuid4().hex
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    source_path = None

    try:
        source_path = download_authorized_youtube_video(
            url,
            job_dir
        )

        return generate_clips(
            source_path,
            job_id,
            clips,
            duration,
            ratio
        )

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Video processing failed: {str(error)[-1200:]}"
        ) from error

    finally:
        if source_path and source_path.exists():
            source_path.unlink(missing_ok=True)

        shutil.rmtree(job_dir, ignore_errors=True)


@app.post("/api/clip")
async def create_clips_from_file(
    video: UploadFile = File(...),
    clips: int = Form(5),
    duration: int = Form(30),
    ratio: str = Form("9:16"),
    method: str = Form("balanced")
):

    validate_options(clips, duration, ratio)

    extension = Path(video.filename or "").suffix.lower()

    if extension not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format."
        )

    job_id = uuid.uuid4().hex
    input_path = TEMP_DIR / f"{job_id}{extension}"

    size = 0

    try:
        with input_path.open("wb") as buffer:

            while True:
                chunk = await video.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_VIDEO_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Maximum video size is 1 GB."
                    )

                buffer.write(chunk)

        if size == 0:
            raise HTTPException(
                status_code=400,
                detail="Uploaded video is empty."
            )

        return generate_clips(
            input_path,
            job_id,
            clips,
            duration,
            ratio
        )

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Video processing failed: {str(error)[-1200:]}"
        ) from error

    finally:
        await video.close()
        input_path.unlink(missing_ok=True)
            
