
import uuid
import subprocess
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
TMP = ROOT / "tmp"
OUT = ROOT / "outputs"

TMP.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

MAX_SIZE = 500 * 1024 * 1024

app = FastAPI(title="GTA SHADOW X API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

app.mount("/outputs", StaticFiles(directory=OUT), name="outputs")


def cmd(args):
    p = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if p.returncode:
        raise RuntimeError(p.stderr[-1500:])
    return p.stdout


@app.get("/")
def home():
    return {
        "status": "online",
        "app": "GTA SHADOW X",
        "features": ["upload", "direct-video-url", "video-clipping"]
    }


def download_video(url, destination):
    parsed = urlparse(url)

    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(400, "Valid HTTPS video URL required")

    try:
        with requests.get(
            url,
            stream=True,
            timeout=(15, 60),
            allow_redirects=True
        ) as response:

            response.raise_for_status()

            if urlparse(response.url).scheme != "https":
                raise HTTPException(400, "HTTPS redirects only")

            size = 0

            with destination.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue

                    size += len(chunk)

                    if size > MAX_SIZE:
                        raise HTTPException(413, "Maximum video size is 500 MB")

                    output.write(chunk)

            if size == 0:
                raise HTTPException(400, "Empty video source")

    except HTTPException:
        raise
    except requests.RequestException as error:
        raise HTTPException(400, f"Unable to access URL: {error}")


def get_video_duration(source):
    metadata = json.loads(cmd([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=codec_type",
        "-of", "json",
        str(source)
    ]))

    if not any(
        stream.get("codec_type") == "video"
        for stream in metadata.get("streams", [])
    ):
        raise HTTPException(400, "Source is not a video")

    total = float(metadata["format"]["duration"])

    if total <= 0:
        raise HTTPException(400, "Invalid video duration")

    return total


@app.post("/api/clip")
async def clip(
    file: UploadFile | None = File(None),
    media_url: str | None = Form(None),
    count: int = Form(10),
    duration: int = Form(30),
    aspect: str = Form("9:16"),
    method: str = Form("balanced")
):
    if not 1 <= count <= 30:
        raise HTTPException(400, "Choose 1 to 30 clips")

    if duration not in [15, 30, 45, 60]:
        raise HTTPException(400, "Invalid clip duration")

    if aspect not in ["9:16", "1:1", "16:9"]:
        raise HTTPException(400, "Invalid aspect ratio")

    if method not in ["balanced", "even"]:
        raise HTTPException(400, "Invalid clipping method")

    if bool(file) == bool(media_url):
        raise HTTPException(
            400,
            "Provide one video file or one direct video URL"
        )

    job = uuid.uuid4().hex
    source = TMP / f"{job}.mp4"

    try:
        if file:
            if file.content_type and not file.content_type.startswith("video/"):
                raise HTTPException(400, "Upload a video file")

            size = 0

            with source.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break

                    size += len(chunk)

                    if size > MAX_SIZE:
                        raise HTTPException(413, "Maximum upload is 500 MB")

                    output.write(chunk)
        else:
            download_video(media_url, source)

        total = get_video_duration(source)

        if total < duration:
            raise HTTPException(
                400,
                "Video is shorter than selected clip duration"
            )

        marks = []

        if method == "balanced":
            try:
                scene_output = cmd([
                    "ffmpeg",
                    "-i", str(source),
                    "-vf", "select='gt(scene,0.28)',showinfo",
                    "-an",
                    "-f", "null",
                    "-"
                ])

                marks = [
                    float(value)
                    for value in re.findall(
                        r"pts_time:([0-9.]+)",
                        scene_output
                    )
                ]
            except Exception:
                marks = []

        candidates = [
            max(0, min(total - duration, t - duration / 2))
            for t in marks
            if duration / 2 <= t <= total - duration / 2
        ]

        if len(candidates) < count:
            candidates = [
                i * (total - duration) / max(1, count - 1)
                for i in range(count)
            ]

        starts = []

        for i in range(count):
            target = i * (total - duration) / max(1, count - 1)

            if candidates:
                pick = min(candidates, key=lambda x: abs(x - target))
            else:
                pick = target

            if all(abs(pick - previous) > duration * 0.5 for previous in starts):
                starts.append(pick)
            else:
                starts.append(target)

        filters = {
            "9:16": "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "1:1": "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080",
            "16:9": "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"
        }

        clips = []

        for i, start in enumerate(starts):
            name = f"{job}_{i+1}.mp4"
            destination = OUT / name

            cmd([
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-i", str(source),
                "-t", str(duration),
                "-vf", filters[aspect],
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "24",
                "-c:a", "aac",
                "-movflags", "+faststart",
                str(destination)
            ])

            clips.append({
                "start": round(start, 2),
                "end": round(start + duration, 2),
                "url": "/outputs/" + name
            })

        return {
            "success": True,
            "clips": clips,
            "note": "Clips generated using scene-change or evenly spaced selection. Not semantic AI highlights."
        }

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(500, str(error))
    finally:
        source.unlink(missing_ok=True)
     
