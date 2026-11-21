
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(
    title="GTA SHADOW X",
    description="Video clipping API",
    version="2.2"
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
TEMP_DIR = BASE_DIR / "temp"

OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

MAX_VIDEO_BYTES = 1024 * 1024 * 1024

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
    name="outputs"
)


def run_command(command, timeout=1800):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout
    )

    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or "Command failed"
        )[-2500:]
        raise RuntimeError(detail)

    return result.stdout.strip()


def validate_options(clips, duration, ratio):
    if clips not in [5, 10, 20, 30]:
        raise HTTPException(
            status_code=400,
            detail="Choose 5, 10, 20 or 30 clips."
        )

    if duration not in [15, 30, 45, 60]:
        raise HTTPException(
            status_code=400,
            detail="Choose 15, 30, 45 or 60 seconds."
        )

    if ratio not in ["9:16", "1:1", "16:9"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid video format."
        )


def validate_youtube_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        allowed = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be"
        }

        if parsed.scheme not in ("http", "https"):
            raise ValueError()

        if host not in allowed:
            raise ValueError()

        if host.endswith("youtu.be") and not parsed.path.strip("/"):
            raise ValueError()

        if "youtube.com" in host and parsed.path == "/watch":
            if not parsed.query:
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

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="YouTube import timed out. Try a shorter video."
        )

    except RuntimeError as error:
        message = str(error)[-1200:]

        raise HTTPException(
            status_code=400,
            detail="YouTube download failed: " + message
        )

    candidates = [
        p for p in job_dir.glob("source.*")
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl"))
    ]

    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="YouTube did not provide a downloadable video file."
        )

    source = candidates[0]

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
    ])

    return float(result)


def generate_clips(input_path, job_id, clips, duration, ratio):
    total_duration = get_video_duration(input_path)

    if total_duration < 2:
        raise HTTPException(
            status_code=400,
            detail="Video is too short."
        )

    clip_length = min(duration, total_duration)

    if total_duration <= clip_length:
        starts = [0]
    else:
        starts = [
            i * (total_duration - clip_length) / (clips - 1)
            for i in range(clips)
        ]

    if ratio == "9:16":
        video_filter = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920"
        )

    elif ratio == "1:1":
        video_filter = (
            "scale=1080:1080:force_original_aspect_ratio=increase,"
            "crop=1080:1080"
        )

    else:
        video_filter = (
            "scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080"
        )

    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    results = []

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
        ])

        results.append({
            "name": filename,
            "url": f"/outputs/{job_id}/{filename}",
            "download_url": f"/outputs/{job_id}/{filename}",
            "start": round(start, 2),
            "duration": round(clip_length, 2)
        })

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
    return FileResponse(
        str(BASE_DIR / "index.html"),
        media_type="text/html"
    )


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "app": "GTA SHADOW X",
        "version": "2.2"
    }


@app.post("/api/clip-url")
async def create_clips_from_youtube(request: Request):
    payload = await request.json()

    url = validate_youtube_url(
        str(payload.get("url", "")).strip()
    )

    clips = int(payload.get("clips", 10))
    duration = int(payload.get("duration", 30))
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
        )

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

    if extension not in [
        ".mp4", ".mov", ".m4v", ".webm", ".mkv"
    ]:
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
        )

    finally:
        await video.close()
        input_path.unlink(missing_ok=True)
    
