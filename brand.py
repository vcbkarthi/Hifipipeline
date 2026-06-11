import base64
import subprocess
import json
import re
from pathlib import Path
from typing import Optional

import ollama

from ingest import Clip


def _extract_frame(video_path: Path, timestamp: float, out_path: Path) -> bool:
    cmd = [
        "ffmpeg", "-y", "-ss", str(timestamp),
        "-i", str(video_path),
        "-vframes", "1", "-q:v", "2",
        str(out_path)
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and out_path.exists()


def _image_to_b64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _ask_llava(image_b64: str, brand_dictionary: list[str]) -> dict:
    brands_hint = ", ".join(brand_dictionary[:20])
    prompt = (
        "You are analyzing a frame from a HiFi audio show. "
        "Look carefully at any banners, logos, signage, product labels, or text in this image. "
        f"Known HiFi brands include: {brands_hint}, and others. "
        "Return ONLY a JSON object with these keys: "
        "\"brand\" (string or null), \"product\" (string or null), \"confidence\" (0.0-1.0). "
        "If multiple brands are visible, return the most prominent one. "
        "Example: {\"brand\": \"Focal\", \"product\": \"Utopia\", \"confidence\": 0.9}"
    )

    response = ollama.chat(
        model="llava",
        messages=[{
            "role": "user",
            "content": prompt,
            "images": [image_b64]
        }]
    )

    text = response["message"]["content"].strip()
    # Extract JSON from response
    match = re.search(r'\{.*?\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"brand": None, "product": None, "confidence": 0.0}


def _fuzzy_match(detected: str, dictionary: list[str]) -> Optional[str]:
    if not detected:
        return None
    detected_lower = detected.lower()
    for brand in dictionary:
        if brand.lower() in detected_lower or detected_lower in brand.lower():
            return brand
    return detected  # Return as-is if not in dictionary — novel brand


def detect_brand_ollama_available() -> bool:
    try:
        import ollama as _ol
        _ol.list()
        return True
    except Exception:
        return False


def detect_brand(clip: Clip, frames_dir: Path, brand_dictionary: list[str]) -> tuple[Optional[str], Optional[str], float]:
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Sample 4 frames: 10%, 30%, 50%, 70% through the clip
    sample_points = [0.10, 0.30, 0.50, 0.70]
    results = []

    if not detect_brand_ollama_available():
        print(f"    [brand] Ollama not available — brand set to Unknown (edit in UI after run)")
        return None, None, 0.0

    for i, frac in enumerate(sample_points):
        ts = clip.duration * frac
        frame_path = frames_dir / f"{clip.file_hash}_brand_{i}.jpg"

        if not _extract_frame(clip.path, ts, frame_path):
            continue

        try:
            b64 = _image_to_b64(frame_path)
            result = _ask_llava(b64, brand_dictionary)
            if result.get("brand") and result.get("confidence", 0) > 0.3:
                results.append(result)
        except Exception as e:
            print(f"    [brand] frame {i} failed: {e}")

    if not results:
        return None, None, 0.0

    # Pick highest confidence result
    best = max(results, key=lambda r: r.get("confidence", 0))
    brand = _fuzzy_match(best.get("brand"), brand_dictionary)
    product = best.get("product")
    confidence = best.get("confidence", 0.0)

    return brand, product, confidence


def safe_filename(s: str) -> str:
    return re.sub(r'[^\w\-]', '_', s).strip("_")


def build_output_name(clip: Clip, show_name: str, suffix: str = "") -> str:
    brand = clip.brand or "Unknown"
    parts = [safe_filename(brand)]
    if show_name:
        parts.append(safe_filename(show_name))
    if suffix:
        parts.append(suffix)
    return "_".join(parts)
