
import uuid
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(
    title="GTA SHADOW X",
    description="Video clipping API",
    version="2.0"
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
TEMP_DIR = BASE_DIR / "temp"

OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

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


def run_command(command):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=1800
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:])

    return result.stdout.strip()


def get_video_duration(path):
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ])

    return float(result)


@app.get("/")
def home():
    return FileResponse(
        str(BASE_DIR / "index.html"),
        media_type="text/html"
    
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/api/clip")
async def create_clips(
    video: UploadFile = File(...),
    clips: int = Form(5),
    duration: int = Form(30),
    ratio: str = Form("9:16"),
    method: str = Form("balanced")
):
    if clips not in [5, 10, 20, 30]:
        raise HTTPException(
            status_code=400,
            detail="Choose 5, 10, 20 or 30 clips"
        )

    if duration not in [15, 30, 45, 60]:
        raise HTTPException(
            status_code=400,
            detail="Choose 15, 30, 45 or 60 seconds"
        )

    if ratio not in ["9:16", "1:1", "16:9"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid video ratio"
        )

    extension = Path(video.filename or "").suffix.lower()

    if extension not in [".mp4", ".mov", ".m4v", ".webm", ".mkv"]:
        raise HTTPException(
            status_code=400,
            detail="Upload MP4, MOV, M4V, WEBM or MKV"
        )

    job_id = uuid.uuid4().hex
    input_path = TEMP_DIR / f"{job_id}{extension}"
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        size = 0
        max_size = 1024 * 1024 * 1024

        with input_path.open("wb") as buffer:
            while True:
                chunk = await video.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > max_size:
                    raise HTTPException(
                        status_code=413,
                        detail="Maximum upload size is 1 GB"
                    )

                buffer.write(chunk)

        if size == 0:
            raise HTTPException(
                status_code=400,
                detail="Uploaded video is empty"
            )

        total_duration = get_video_duration(input_path)

        if total_duration < 2:
            raise HTTPException(
                status_code=400,
                detail="Video is too short"
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

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Video processing failed: {str(error)}"
        )

    finally:
        await video.close()

        if input_path.exists():
            input_path.unlink()
        
