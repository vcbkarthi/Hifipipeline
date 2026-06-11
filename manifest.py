import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from ingest import Clip
from scorer import ScoredWindow


def _short_description(brand: Optional[str], product: Optional[str], show_name: str, signal: str) -> str:
    subject = f"{brand} {product}" if product else (brand or "this speaker system")
    signal_copy = {
        "motion": f"Watch the drivers come alive on {subject}.",
        "warmth": f"Glowing tubes and gorgeous sound from {subject}.",
        "sharpness": f"The craftsmanship detail on {subject} is stunning.",
    }.get(signal, f"Incredible sound from {subject}.")
    return (
        f"{signal_copy} Heard live at {show_name}. "
        f"#hifi #audiophile #hifishow #shorts"
    )


def _longform_description(brands: list[str], show_name: str) -> str:
    brand_list = ", ".join(b for b in brands if b)
    return (
        f"A cinematic walkthrough of {show_name} — featuring "
        f"{brand_list}. Recorded live. No edits to the music. "
        f"This is how high-end audio sounds and looks in person.\n\n"
        f"#hifi #audiophile #hifishow #highendaudio #speakers #amplifier"
    )


def _tags(base_tags: list[str], signal_tags: dict, signal: Optional[str], brand: Optional[str]) -> list[str]:
    tags = list(base_tags)
    if signal and signal in signal_tags:
        tags += signal_tags[signal]
    if brand:
        tags.append(brand.lower().replace(" ", ""))
    return list(dict.fromkeys(tags))  # deduplicate, preserve order


def build_short_manifest(
    clip: Clip,
    window: ScoredWindow,
    out_path: Path,
    show_name: str,
    short_index: int,
    cfg: dict,
) -> dict:
    brand = clip.brand
    product = None
    title_parts = [brand or "HiFi"]
    if show_name:
        title_parts.append(show_name)
    signal_label = {
        "motion": "Woofer Porn",
        "warmth": "Tube Glow",
        "sharpness": "Detail Shot",
    }.get(window.dominant_signal, "Short")
    title_parts.append(signal_label)
    title = " | ".join(title_parts)

    return {
        "type": "short",
        "file": str(out_path),
        "title": title,
        "description": _short_description(brand, product, show_name, window.dominant_signal),
        "tags": _tags(cfg["youtube_tags_base"], cfg["signal_tags"], window.dominant_signal, brand),
        "thumbnail_ts": window.hook_frame_ts,
        "duration_s": round(window.end - window.start, 1),
        "score": round(window.score, 3),
        "dominant_signal": window.dominant_signal,
        "source_clip": str(clip.path),
        "brand": brand,
        "approved": False,
        "generated_at": datetime.now().isoformat(),
    }


def build_longform_manifest(
    clips: list[Clip],
    out_path: Path,
    show_name: str,
    cfg: dict,
) -> dict:
    brands = [c.brand for c in clips if c.brand]

    return {
        "type": "longform",
        "file": str(out_path),
        "title": f"{show_name} — Full HiFi Show Walkthrough",
        "description": _longform_description(brands, show_name),
        "tags": _tags(cfg["youtube_tags_base"], cfg["signal_tags"], None, None) + [b.lower().replace(" ", "") for b in brands],
        "brands_featured": brands,
        "source_clips": [str(c.path) for c in clips],
        "approved": False,
        "generated_at": datetime.now().isoformat(),
    }


def save_manifest(manifest_dir: Path, name: str, entries: list[dict]) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    out = manifest_dir / f"{name}.json"
    with open(out, "w") as f:
        json.dump(entries, f, indent=2)
    return out
