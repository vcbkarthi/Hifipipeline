"""
YouTube Data API v3 — OAuth + resumable upload.

First run:  opens browser for Google consent, saves token to credentials/token.json.
Subsequent: token auto-refreshed, no browser needed.

Setup (one-time):
  1. Go to https://console.cloud.google.com
  2. Create project → Enable "YouTube Data API v3"
  3. Credentials → Create OAuth 2.0 Client ID → Desktop app
  4. Download JSON → save as credentials/client_secret.json
"""

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import keychain as kc

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CREDENTIALS_DIR  = Path("credentials")
CLIENT_SECRET    = CREDENTIALS_DIR / "client_secret.json"
TOKEN_PATH       = CREDENTIALS_DIR / "token.json"

# YouTube category IDs
CATEGORY_MUSIC       = "10"
CATEGORY_SCIENCE_TECH = "28"
HIFI_CATEGORY        = CATEGORY_MUSIC  # music fits HiFi content best

PRIVACY_PUBLIC   = "public"
PRIVACY_UNLISTED = "unlisted"
PRIVACY_PRIVATE  = "private"


class UploadProgress:
    def __init__(self, entry_id: str):
        self.entry_id = entry_id
        self.percent: int = 0
        self.status: str = "idle"   # idle | uploading | done | error
        self.youtube_id: str = ""
        self.youtube_url: str = ""
        self.error: str = ""
        self.bytes_sent: int = 0
        self.total_bytes: int = 0


def get_credentials() -> Credentials:
    CREDENTIALS_DIR.mkdir(exist_ok=True)

    # Build client_secret.json from Keychain values at runtime
    kc.write_client_secret(CLIENT_SECRET)

    if not CLIENT_SECRET.exists():
        raise FileNotFoundError(
            "YouTube credentials missing. Add Client ID and Secret in the Settings tab."
        )

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            # Opens browser once for consent
            creds = flow.run_local_server(port=0, open_browser=True)
        TOKEN_PATH.write_text(creds.to_json())

    return creds


def upload_video(
    entry: dict,
    progress: UploadProgress,
    privacy: str = PRIVACY_PRIVATE,
    on_progress: Optional[Callable[[UploadProgress], None]] = None,
) -> str:
    """
    Upload video to YouTube. Returns YouTube video ID on success.
    Updates `progress` in-place throughout — caller can read it from another thread.
    Privacy defaults to private so you can review on YouTube before making public.
    """
    video_path = Path(entry.get("file", ""))
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    progress.status = "authenticating"
    progress.total_bytes = video_path.stat().st_size
    if on_progress:
        on_progress(progress)

    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    title = entry.get("title", video_path.stem)[:100]
    description = entry.get("description", "")[:5000]
    tags = entry.get("tags", [])[:500]

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": HIFI_CATEGORY,
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    # Shorts need #Shorts in title/description for algorithm detection
    if entry.get("type") == "short":
        if "#shorts" not in description.lower():
            body["snippet"]["description"] = description + "\n\n#Shorts"

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=5 * 1024 * 1024,  # 5 MB chunks — resumable if interrupted
    )

    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    progress.status = "uploading"
    if on_progress:
        on_progress(progress)

    response = None
    retry = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                progress.bytes_sent = int(status.resumable_progress)
                progress.percent = int(progress.bytes_sent / progress.total_bytes * 100)
                if on_progress:
                    on_progress(progress)
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retry < 5:
                # Transient server error — exponential backoff
                time.sleep(2 ** retry)
                retry += 1
            else:
                progress.status = "error"
                progress.error = str(e)
                if on_progress:
                    on_progress(progress)
                raise

    video_id = response["id"]
    progress.status = "done"
    progress.percent = 100
    progress.youtube_id = video_id
    progress.youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    if on_progress:
        on_progress(progress)

    return video_id
