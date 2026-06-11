import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ingest import Clip
from scorer import ScoredWindow


def _vf_cinematic_grade(g: dict, lut_path: Optional[str]) -> list[str]:
    """
    Build ordered list of FFmpeg filter strings for cinematic 4K grade.

    Pipeline:
      1. Scale to 4K (if source is smaller)
      2. Optional LUT (user-supplied .cube)
      3. eq — base exposure / contrast / saturation
      4. colorbalance — warm shadows, cool highlights (film emulation)
      5. curves — cinematic S-curve for deeper blacks and lifted whites
      6. unsharp — luma + chroma sharpening for crisp 4K detail
    """
    filters = []

    # 1. LUT first (applied before procedural grade so it acts as a base film stock)
    if lut_path and Path(lut_path).exists():
        filters.append(f"lut3d='{lut_path}'")

    # 2. Base exposure + saturation + contrast
    sat  = g.get("saturation",  1.35)
    con  = g.get("contrast",    1.12)
    bri  = g.get("brightness",  0.03)
    gam  = g.get("gamma",       0.92)
    filters.append(f"eq=saturation={sat}:contrast={con}:brightness={bri}:gamma={gam}")

    # 3. Colour balance — warm shadows (amber/red lift), slightly cool highlights
    sr = g.get("shadows_r",    0.02)
    sb = g.get("shadows_b",   -0.03)
    hr = g.get("highlights_r",-0.01)
    hb = g.get("highlights_b", 0.02)
    filters.append(
        f"colorbalance=rs={sr}:gs=0:bs={sb}:rm=0:gm=0:bm=0:rh={hr}:gh=0:bh={hb}"
    )

    # 4. S-curve — deeper blacks, lifted mids, capped whites (cinematic contrast)
    filters.append(
        "curves=r='0/0 0.08/0.04 0.5/0.53 0.92/0.96 1/1':"
        "g='0/0 0.08/0.05 0.5/0.52 0.92/0.95 1/1':"
        "b='0/0 0.08/0.06 0.5/0.51 0.92/0.94 1/1'"
    )

    # 5. Sharpening — unsharp mask tuned for 4K detail (luma strong, chroma gentle)
    sl = g.get("sharpen_luma",   0.9)
    sc = g.get("sharpen_chroma", 0.3)
    filters.append(f"unsharp=lx=5:ly=5:la={sl}:cx=3:cy=3:ca={sc}")

    return filters


def _encode_args(vf: str, enc: dict) -> list:
    """Return FFmpeg output flags for 4K HEVC with correct colourspace metadata."""
    return [
        "-vf",    vf,
        "-c:v",   enc.get("video_codec",   "hevc_videotoolbox"),
        "-b:v",   enc.get("video_bitrate", "40000k"),
        "-c:a",   enc.get("audio_codec",   "aac"),
        "-b:a",   enc.get("audio_bitrate", "320k"),
        "-pix_fmt", enc.get("pixel_format", "yuv420p"),
        "-color_primaries", enc.get("color_primaries", "bt2020"),
        "-color_trc",       enc.get("color_trc",       "arib-std-b67"),
        "-colorspace",      enc.get("colorspace",      "bt2020nc"),
        "-movflags", "+faststart",
    ]


def _vf_logo(logo_path: str, position: str, scale: float, opacity: float) -> str:
    pos_map = {
        "top-right":    "W-w-20:20",
        "top-left":     "20:20",
        "bottom-right": "W-w-20:H-h-20",
        "bottom-left":  "20:H-h-20",
    }
    xy = pos_map.get(position, "W-w-20:20")
    return (
        f"[1:v]scale=iw*{scale}:-1,format=rgba,colorchannelmixer=aa={opacity}[logo];"
        f"[0:v][logo]overlay={xy}:format=auto"
    )


def _vf_lower_third(brand: str, product: Optional[str]) -> str:
    # Semi-transparent dark bar at bottom — drawtext requires libfreetype which
    # is not available in this FFmpeg build, so we use drawbox only.
    return "drawbox=x=0:y=ih*0.85:w=iw:h=ih*0.10:color=black@0.50:t=fill"


def _base_vf(logo_path: Optional[str], logo_position: str, logo_scale: float,
             logo_opacity: float, brand: Optional[str], product: Optional[str],
             lut_path: Optional[str], grade: Optional[dict] = None,
             resolution: Optional[str] = None) -> tuple[str, list]:
    """Build video filter chain. Returns (vf_string, extra_inputs)."""
    filters = []
    extra_inputs = []

    # Scale to target resolution first (e.g. 2160x3840 for 4K vertical)
    if resolution:
        w, h = resolution.split("x")
        filters.append(f"scale={w}:{h}:flags=lanczos")

    # Cinematic grade (replaces bare lut3d call)
    if grade and grade.get("enabled", True):
        filters.extend(_vf_cinematic_grade(grade, lut_path))

    if brand:
        filters.append(_vf_lower_third(brand, product))

    if logo_path and Path(logo_path).exists():
        extra_inputs = ["-i", logo_path]
        logo_vf = _vf_logo(logo_path, logo_position, logo_scale, logo_opacity)
        if filters:
            # Chain: apply colour + lower-third first, then logo overlay
            pre = ",".join(filters)
            vf = f"{pre},{logo_vf}"
        else:
            vf = logo_vf
        return vf, extra_inputs
    else:
        vf = ",".join(filters) if filters else "null"
        return vf, []


def cut_short(
    clip: Clip,
    window: ScoredWindow,
    out_path: Path,
    cfg: dict,
    short_index: int = 0,
) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    logo = cfg.get("logo_path", "")
    enc  = cfg.get("encoding", {})
    vf, extra_inputs = _base_vf(
        logo_path=logo if logo else None,
        logo_position=cfg.get("logo_position", "top-right"),
        logo_scale=cfg.get("logo_scale", 0.12),
        logo_opacity=cfg.get("logo_opacity", 0.85),
        brand=clip.brand,
        product=None,
        lut_path=cfg.get("lut_path", ""),
        grade=cfg.get("grade"),
        resolution=enc.get("short_resolution"),
    )

    duration = window.end - window.start

    # If hook reorder: put best 1s frame at the very start using concat
    if cfg["shorts"].get("hook_reorder") and window.hook_frame_ts > window.start + 1.0:
        hook_start = max(window.start, window.hook_frame_ts - 0.5)
        hook_end = min(window.end, hook_start + 1.0)
        rest_start = window.start
        rest_end = window.end

        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            concat_list = f.name
            f.write(f"file '{clip.path}'\n")
            f.write(f"inpoint {hook_start}\noutpoint {hook_end}\n")
            f.write(f"file '{clip.path}'\n")
            f.write(f"inpoint {rest_start}\noutpoint {rest_end}\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list,
        ] + extra_inputs + _encode_args(vf, enc) + [str(out_path)]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(window.start), "-to", str(window.end),
            "-i", str(clip.path),
        ] + extra_inputs + _encode_args(vf, enc) + [str(out_path)]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"    [editor] FFmpeg error:\n{result.stderr.decode(errors='replace')[-2000:]}")
    return result.returncode == 0


def stitch_longform(
    clips: list[Clip],
    out_path: Path,
    show_name: str,
    cfg: dict,
    order: Optional[list[str]] = None,
) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Sort clips — by brand name alphabetically, or by caller-provided order list
    if order:
        order_lower = [o.lower() for o in order]
        def sort_key(c):
            b = (c.brand or "").lower()
            try:
                return order_lower.index(b)
            except ValueError:
                return len(order_lower)
        sorted_clips = sorted(clips, key=sort_key)
    else:
        sorted_clips = sorted(clips, key=lambda c: (c.brand or "").lower())

    crossfade = cfg["longform"]["crossfade_seconds"]
    target_s = cfg["longform"]["target_seconds"]
    per_clip_s = target_s / max(len(sorted_clips), 1)

    # Build individual trimmed segments with lower-thirds, then concat + crossfade
    segment_paths = []
    tmp_dir = out_path.parent / "_segments"
    tmp_dir.mkdir(exist_ok=True)

    logo = cfg.get("logo_path", "")

    for i, clip in enumerate(sorted_clips):
        seg_path = tmp_dir / f"seg_{i:03d}.mp4"
        # Pick best representative window of per_clip_s seconds
        # Use middle of clip as default if no scored windows
        mid = clip.duration / 2
        start = max(0, mid - per_clip_s / 2)
        end = min(clip.duration, start + per_clip_s)

        enc = cfg.get("encoding", {})
        vf, extra_inputs = _base_vf(
            logo_path=logo if logo else None,
            logo_position=cfg.get("logo_position", "top-right"),
            logo_scale=cfg.get("logo_scale", 0.12),
            logo_opacity=cfg.get("logo_opacity", 0.85),
            brand=clip.brand if cfg["longform"].get("lower_third") else None,
            product=None,
            lut_path=cfg.get("lut_path", ""),
            grade=cfg.get("grade"),
            resolution=enc.get("longform_resolution"),
        )

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start), "-to", str(end),
            "-i", str(clip.path),
        ] + extra_inputs + _encode_args(vf, enc) + [str(seg_path)]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            segment_paths.append(seg_path)

    if not segment_paths:
        return False

    # Concat with xfade transitions
    if len(segment_paths) == 1:
        segment_paths[0].rename(out_path)
        return True

    # Build xfade filter chain
    # Each segment is ~per_clip_s seconds; crossfade at each join
    inputs = []
    for sp in segment_paths:
        inputs += ["-i", str(sp)]

    # Build complex filtergraph for xfade
    n = len(segment_paths)
    filter_parts = []
    offset = per_clip_s - crossfade

    prev_label = "[0:v]"
    prev_audio = "[0:a]"

    for i in range(1, n):
        v_out = f"[v{i}]" if i < n - 1 else "[vout]"
        a_out = f"[a{i}]" if i < n - 1 else "[aout]"
        filter_parts.append(
            f"{prev_label}[{i}:v]xfade=transition=fade:duration={crossfade}"
            f":offset={offset}{v_out}"
        )
        filter_parts.append(
            f"{prev_audio}[{i}:a]acrossfade=d={crossfade}{a_out}"
        )
        prev_label = v_out
        prev_audio = a_out
        offset += per_clip_s - crossfade

    filter_complex = ";".join(filter_parts)

    enc = cfg.get("encoding", {})
    cmd = (
        ["ffmpeg", "-y"] +
        inputs + [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v",   enc.get("video_codec",   "hevc_videotoolbox"),
            "-b:v",   enc.get("video_bitrate", "40000k"),
            "-c:a",   enc.get("audio_codec",   "aac"),
            "-b:a",   enc.get("audio_bitrate", "320k"),
            "-pix_fmt", enc.get("pixel_format", "yuv420p"),
            "-color_primaries", enc.get("color_primaries", "bt2020"),
            "-color_trc",       enc.get("color_trc",       "arib-std-b67"),
            "-colorspace",      enc.get("colorspace",      "bt2020nc"),
            "-movflags", "+faststart",
            str(out_path)
        ]
    )

    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0
