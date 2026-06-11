import json
import hashlib
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


VIDEO_EXTENSIONS = {".mp4", ".mov", ".MP4", ".MOV", ".m4v", ".M4V"}


@dataclass
class Clip:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    file_hash: str
    brand: Optional[str] = None
    brand_confidence: float = 0.0
    shorts: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)


def _ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(result.stdout)


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        # Hash first 4MB — fast fingerprint, avoids reading whole 4K clip
        h.update(f.read(4 * 1024 * 1024))
    return h.hexdigest()[:16]


def scan_folder(input_dir: str) -> list[Clip]:
    folder = Path(input_dir)
    if not folder.exists():
        raise FileNotFoundError(f"Input folder not found: {input_dir}")

    video_files = [
        p for p in folder.rglob("*")
        if p.suffix in VIDEO_EXTENSIONS and p.is_file()
    ]

    if not video_files:
        raise ValueError(f"No video files found in {input_dir}")

    clips = []
    for path in sorted(video_files):
        try:
            probe = _ffprobe(path)
            video_stream = next(
                s for s in probe["streams"] if s["codec_type"] == "video"
            )
            duration = float(probe["format"]["duration"])
            width = int(video_stream["width"])
            height = int(video_stream["height"])
            fps_parts = video_stream.get("r_frame_rate", "30/1").split("/")
            fps = float(fps_parts[0]) / float(fps_parts[1])

            clips.append(Clip(
                path=path,
                duration=duration,
                width=width,
                height=height,
                fps=fps,
                file_hash=_file_hash(path),
            ))
        except Exception as e:
            print(f"  [skip] {path.name}: {e}")

    return clips
