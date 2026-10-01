#!/usr/bin/env python3

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#
# First test:
#
# ./scripts/upload_youtube.py "Vođeni Duhom.json" 1
#
# Several tracks:
#
# ./scripts/upload_youtube.py "Vođeni Duhom.json" 1 2 6
#
# Whole album:
#
# ./scripts/upload_youtube.py "Vođeni Duhom.json"
#
#
# IMPORTANT:
#
# All videos are uploaded as PRIVATE.
# This script NEVER publishes videos.
#
# The album playlist is also PRIVATE.
#
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

INPUT_DIR = PROJECT_ROOT / "input"
OUTPUT_DIR = PROJECT_ROOT / "output"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
DATA_DIR = PROJECT_ROOT / "data"

CLIENT_SECRETS_FILE = PROJECT_ROOT / "client_secret.json"
TOKEN_FILE = DATA_DIR / "youtube_token.json"
STATE_FILE = DATA_DIR / "youtube_state.json"

# Custom YouTube thumbnails must be 50 MB or smaller.
THUMBNAIL_MAX_SIZE_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# YouTube API
# ---------------------------------------------------------------------------

YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

# Broader scope is required because this script manages:
#
# - videos
# - playlists
# - playlist items
#
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube"
]


# ---------------------------------------------------------------------------
# Album metadata
# ---------------------------------------------------------------------------

def load_album(album_file):
    """
    Load album metadata from ./input/<album_file>.
    """

    path = INPUT_DIR / album_file

    if not path.exists():
        raise FileNotFoundError(
            f"Album metadata not found: {path}"
        )

    if not path.is_file():
        raise FileNotFoundError(
            f"Album metadata path is not a file: {path}"
        )

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    tracks = data.get("data")

    if not isinstance(tracks, list) or not tracks:
        raise ValueError(
            f"No track data found in: {path}"
        )

    return tracks


def find_track(tracks, track_number):
    """
    Find a track by Track Number.
    """

    wanted = int(track_number)

    for track in tracks:

        try:
            number = int(track["Track Number"])
        except (KeyError, ValueError):
            continue

        if number == wanted:
            return track

    raise ValueError(
        f"Track {wanted:02d} not found in album metadata."
    )


def sort_tracks_by_number(tracks):
    """
    Return tracks sorted by Track Number.
    """

    return sorted(
        tracks,
        key=lambda track: int(track["Track Number"])
    )


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------

def build_video_path(track):
    """
    Build the expected rendered MP4 path.
    """

    album = track["Album"]
    track_number = int(track["Track Number"])
    title = track["Title"]

    filename = f"{track_number:02d} - {title}.mp4"

    return OUTPUT_DIR / album / filename


def build_thumbnail_path(track):
    """
    Build the expected generated thumbnail path.

    Thumbnails are generated next to the rendered video and LRC file:
        output/<Album>/<NN> - <Title>.jpg
    """

    album = track["Album"]
    track_number = int(track["Track Number"])
    title = track["Title"]

    filename = f"{track_number:02d} - {title}.jpg"

    return OUTPUT_DIR / album / filename


def file_sha256(path):
    """
    Calculate a SHA-256 hash for a local file.

    The hash is stored in youtube_state.json after a successful thumbnail
    upload. This makes thumbnail uploads idempotent and avoids re-uploading
    the same image on every normal run.
    """

    digest = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def prepare_thumbnail(track):
    """
    Validate and prepare the generated thumbnail for upload.

    Returns:
        thumbnail_path, sha256
    """

    thumbnail_path = build_thumbnail_path(track)

    if not thumbnail_path.exists():
        raise FileNotFoundError(
            f"Generated thumbnail not found: {thumbnail_path}"
        )

    if not thumbnail_path.is_file():
        raise FileNotFoundError(
            f"Generated thumbnail path is not a file: {thumbnail_path}"
        )

    size = thumbnail_path.stat().st_size

    if size > THUMBNAIL_MAX_SIZE_BYTES:
        size_mb = size / (1024 * 1024)
        raise ValueError(
            f"Thumbnail is too large: {thumbnail_path} "
            f"({size_mb:.2f} MB). Maximum allowed is 50 MB."
        )

    thumbnail_hash = file_sha256(thumbnail_path)

    return thumbnail_path, thumbnail_hash


# ---------------------------------------------------------------------------
# Description template
# ---------------------------------------------------------------------------

def get_description_template(language):
    """
    Select the YouTube description template based on track language.
    """

    language = (language or "").strip().lower()

    if language == "hr":
        return TEMPLATE_DIR / "youtube_description_hr.txt"

    if language == "en":
        return TEMPLATE_DIR / "youtube_description.txt"

    raise ValueError(
        f"Unsupported YouTube description language: {language!r}"
    )


def load_description_template(language):

    template_path = get_description_template(language)

    if not template_path.exists():
        raise FileNotFoundError(
            f"YouTube description template not found: "
            f"{template_path}"
        )

    if not template_path.is_file():
        raise FileNotFoundError(
            f"YouTube description template is not a file: "
            f"{template_path}"
        )

    template = template_path.read_text(
        encoding="utf-8"
    )

    return template_path, template


def build_description(track, template):
    """
    Substitute track metadata into the YouTube description template.
    """

    values = {
        "title": track.get("Title", ""),
        "album": track.get("Album", ""),
        "artist": track.get("Artist", ""),
        "album_artist": track.get("Album Artist", ""),
        "track_number": f"{int(track.get('Track Number', 0)):02d}",
        "genre": track.get("Genre", ""),
        "language": track.get("Language", ""),
        "description": track.get("Description", ""),
        "lyrics": track.get("Lyrics", ""),
        "website": track.get("Website", ""),
    }

    try:

        return template.format(**values)

    except KeyError as e:

        raise ValueError(
            f"Unknown placeholder in YouTube description template: "
            f"{{{e.args[0]}}}"
        )


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def get_youtube_service():
    """
    Authenticate with YouTube using OAuth 2.0.

    The first run opens a browser for Google authorization.

    Later runs reuse the stored refresh token.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    credentials = None

    # ---------------------------------------------------------------
    # Existing token
    # ---------------------------------------------------------------

    if TOKEN_FILE.exists():

        credentials = Credentials.from_authorized_user_file(
            str(TOKEN_FILE),
            YOUTUBE_SCOPES,
        )

    # ---------------------------------------------------------------
    # Refresh expired token
    # ---------------------------------------------------------------

    if credentials and credentials.expired and credentials.refresh_token:

        print()
        print("Refreshing YouTube OAuth token...")

        credentials.refresh(Request())

    # ---------------------------------------------------------------
    # First authorization
    # ---------------------------------------------------------------

    if not credentials or not credentials.valid:

        if not CLIENT_SECRETS_FILE.exists():

            raise FileNotFoundError(
                f"YouTube OAuth client secrets not found:\n"
                f"{CLIENT_SECRETS_FILE}\n\n"
                f"Create an OAuth Desktop App credential in Google Cloud "
                f"and save the downloaded JSON as client_secret.json."
            )

        print()
        print("=" * 70)
        print("YOUTUBE OAUTH AUTHORIZATION")
        print("=" * 70)
        print()
        print("A browser window will open.")
        print("Sign in with the Google account that owns the YouTube channel.")
        print()

        flow = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_SECRETS_FILE),
            YOUTUBE_SCOPES,
        )

        credentials = flow.run_local_server(
            port=0
        )

    # ---------------------------------------------------------------
    # Save token
    # ---------------------------------------------------------------

    TOKEN_FILE.write_text(
        credentials.to_json(),
        encoding="utf-8",
    )

    print()
    print(f"OAuth token saved to: {TOKEN_FILE}")

    return build(
        YOUTUBE_API_SERVICE_NAME,
        YOUTUBE_API_VERSION,
        credentials=credentials,
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    """
    Load local YouTube upload state.

    This is deliberately separate from jobs.json.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not STATE_FILE.exists():

        return {
            "videos": {},
            "playlists": {},
        }

    with STATE_FILE.open("r", encoding="utf-8") as f:

        state = json.load(f)

    if not isinstance(state, dict):

        raise ValueError(
            f"Invalid YouTube state file: {STATE_FILE}"
        )

    if "videos" not in state:
        state["videos"] = {}

    if "playlists" not in state:
        state["playlists"] = {}

    return state


def save_state(state):
    """
    Save YouTube state.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------

def prepare_track(track, template):

    video_path = build_video_path(track)

    if not video_path.exists():

        raise FileNotFoundError(
            f"Rendered video not found: {video_path}"
        )

    if not video_path.is_file():

        raise FileNotFoundError(
            f"Rendered video path is not a file: {video_path}"
        )

    description = build_description(
        track,
        template,
    )

    return video_path, description


# ---------------------------------------------------------------------------
# YouTube upload
# ---------------------------------------------------------------------------

def upload_video(
    youtube,
    video_path,
    track,
    description,
):
    """
    Upload one video to YouTube.

    Visibility is intentionally hard-coded to PRIVATE.
    """

    title = track["Title"]

    # YouTube category 10 = Music.
    category_id = "10"

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "private",
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
    )

    print()
    print("Starting YouTube upload...")
    print(f"File       : {video_path}")
    print(f"Title      : {title}")
    print("Visibility : PRIVATE")
    print()

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None

    while response is None:

        status, response = request.next_chunk()

        if status:

            progress = int(
                status.progress() * 100
            )

            print(
                f"Upload progress: {progress}%"
            )

    video_id = response.get("id")

    if not video_id:

        raise RuntimeError(
            "YouTube upload completed but no video ID was returned."
        )

    return video_id


def upload_thumbnail(youtube, video_id, thumbnail_path, retries=3):
    """
    Upload and set a custom thumbnail for an existing YouTube video.

    YouTube's thumbnails.set endpoint supports JPEG/PNG uploads.
    The generated thumbnail pipeline writes JPEG files.

    A few transient server/rate-limit errors are retried with backoff.
    """

    last_error = None

    for attempt in range(1, retries + 1):

        try:

            media = MediaFileUpload(
                str(thumbnail_path),
                mimetype="image/jpeg",
                resumable=False,
            )

            request = youtube.thumbnails().set(
                videoId=video_id,
                media_body=media,
                media_mime_type="image/jpeg",
            )

            request.execute()

            return

        except HttpError as e:

            last_error = e

            status_code = getattr(e.resp, "status", None)

            retryable = status_code in {
                429,
                500,
                502,
                503,
                504,
            }

            if not retryable or attempt >= retries:
                raise

            delay = min(2 ** attempt, 10)

            print()
            print(
                f"Thumbnail upload temporary error "
                f"(attempt {attempt}/{retries})."
            )
            print(
                f"Retrying in {delay} second(s)..."
            )

            time.sleep(delay)

    if last_error:
        raise last_error


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------

def find_playlist_by_title(youtube, title):
    """
    Find an existing playlist owned by the authenticated YouTube account.

    Returns:
        playlist_id or None
    """

    page_token = None

    while True:

        response = youtube.playlists().list(
            part="snippet",
            mine=True,
            maxResults=50,
            pageToken=page_token,
        ).execute()

        for item in response.get("items", []):

            playlist_title = (
                item.get("snippet", {})
                .get("title", "")
            )

            if playlist_title == title:

                return item["id"]

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return None


def create_playlist(youtube, title):
    """
    Create a PRIVATE YouTube playlist.

    Returns:
        playlist_id
    """

    body = {
        "snippet": {
            "title": title,
            "description": f"Album: {title}",
        },
        "status": {
            "privacyStatus": "private",
        },
    }

    response = youtube.playlists().insert(
        part="snippet,status",
        body=body,
    ).execute()

    playlist_id = response.get("id")

    if not playlist_id:

        raise RuntimeError(
            "Playlist creation completed but no playlist ID was returned."
        )

    print(
        f"Playlist created successfully: {playlist_id}"
    )

    return playlist_id


def get_or_create_playlist(
    youtube,
    album,
    state,
):
    """
    Get the album playlist from local state, find it remotely,
    or create it.

    Returns:
        playlist_id, created
    """

    existing_state = state["playlists"].get(
        album
    )

    # ---------------------------------------------------------------
    # Local state says the playlist exists.
    #
    # We trust the recorded ID, but verify it with a cheap API call.
    # ---------------------------------------------------------------

    if existing_state:

        playlist_id = existing_state.get(
            "playlist_id"
        )

        if playlist_id:

            try:

                response = youtube.playlists().list(
                    part="id,snippet,status",
                    id=playlist_id,
                ).execute()

                if response.get("items"):

                    item = response["items"][0]

                    actual_title = (
                        item.get("snippet", {})
                        .get("title", "")
                    )

                    if actual_title == album:

                        return playlist_id, False

            except HttpError:
                pass

    # ---------------------------------------------------------------
    # Search all playlists owned by the account.
    # ---------------------------------------------------------------

    playlist_id = find_playlist_by_title(
        youtube,
        album,
    )

    if playlist_id:

        state["playlists"][album] = {
            "album": album,
            "playlist_id": playlist_id,
            "title": album,
            "visibility": "private",
        }

        save_state(state)

        return playlist_id, False

    # ---------------------------------------------------------------
    # Create new playlist.
    # ---------------------------------------------------------------

    print()
    print("Creating album playlist...")
    print(f"Playlist title : {album}")
    print("Visibility     : PRIVATE")

    playlist_id = create_playlist(
        youtube,
        album,
    )

    state["playlists"][album] = {
        "album": album,
        "playlist_id": playlist_id,
        "title": album,
        "visibility": "private",
    }

    save_state(state)

    return playlist_id, True


def get_playlist_items(youtube, playlist_id, retries=6):
    """
    Retrieve all items currently in a playlist.

    YouTube can take a short time to make a newly-created playlist
    available to playlistItems.list(). Retry transient playlistNotFound
    responses before giving up.

    Returns a list of dictionaries containing:
        playlist_item_id
        video_id
        position
    """

    last_error = None

    for attempt in range(1, retries + 1):

        try:

            items = []
            page_token = None

            while True:

                response = youtube.playlistItems().list(
                    part="id,snippet,contentDetails",
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ).execute()

                for item in response.get("items", []):

                    video_id = (
                        item.get("contentDetails", {})
                        .get("videoId")
                    )

                    position = (
                        item.get("snippet", {})
                        .get("position")
                    )

                    items.append({
                        "playlist_item_id": item.get("id"),
                        "video_id": video_id,
                        "position": position,
                    })

                page_token = response.get(
                    "nextPageToken"
                )

                if not page_token:
                    break

            return items

        except HttpError as e:

            last_error = e

            # YouTube may briefly report a newly-created playlist
            # as "not found".
            if (
                e.resp.status == 404
                and "playlistNotFound" in str(e)
                and attempt < retries
            ):

                delay = min(
                    2 ** (attempt - 1),
                    10,
                )

                print()
                print(
                    f"Playlist not available yet "
                    f"(attempt {attempt}/{retries})."
                )

                print(
                    f"Retrying in {delay} second(s)..."
                )

                time.sleep(delay)

                continue

            raise

    if last_error:
        raise last_error

    return []


def add_video_to_playlist(
    youtube,
    playlist_id,
    video_id,
    position,
):
    """
    Add a video to a playlist at a specific position.
    """

    body = {
        "snippet": {
            "playlistId": playlist_id,
            "position": position,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        }
    }

    response = youtube.playlistItems().insert(
        part="snippet",
        body=body,
    ).execute()

    return response.get("id")


def move_playlist_item(
    youtube,
    playlist_item_id,
    playlist_id,
    video_id,
    position,
):
    """
    Move an existing playlist item to a specific position.
    """

    body = {
        "id": playlist_item_id,
        "snippet": {
            "playlistId": playlist_id,
            "position": position,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id,
            },
        },
    }

    youtube.playlistItems().update(
        part="snippet",
        body=body,
    ).execute()


def sync_playlist(
    youtube,
    playlist_id,
    tracks,
    state,
):
    """
    Synchronize uploaded videos into the album playlist.

    Behavior:

    - Only videos already uploaded are considered.
    - Missing/unuploaded tracks are ignored.
    - Existing playlist entries are not duplicated.
    - Missing entries are added.
    - Existing entries are moved into album track order.
    """

    sorted_tracks = sort_tracks_by_number(
        tracks
    )

    # ---------------------------------------------------------------
    # Build desired playlist order.
    #
    # Only tracks with recorded YouTube IDs are included.
    # ---------------------------------------------------------------

    desired = []

    for track in sorted_tracks:

        track_number = int(
            track["Track Number"]
        )

        state_key = (
            f"{track['Album']}:{track_number:04d}"
        )

        video_state = state["videos"].get(
            state_key
        )

        if not video_state:
            continue

        video_id = video_state.get(
            "video_id"
        )

        if not video_id:
            continue

        desired.append({
            "track_number": track_number,
            "title": track["Title"],
            "video_id": video_id,
        })

    if not desired:

        return {
            "added": 0,
            "moved": 0,
            "already_present": 0,
            "total": 0,
        }

    # ---------------------------------------------------------------
    # Read current playlist.
    # ---------------------------------------------------------------

    current_items = get_playlist_items(
        youtube,
        playlist_id,
    )

    current_by_video = {}

    for item in current_items:

        video_id = item.get(
            "video_id"
        )

        if video_id and video_id not in current_by_video:

            current_by_video[video_id] = item

    added_count = 0
    moved_count = 0
    already_count = 0

    # ---------------------------------------------------------------
    # Add missing videos.
    #
    # We add them in desired order at their target position.
    # ---------------------------------------------------------------

    for desired_position, item in enumerate(desired):

        video_id = item["video_id"]

        if video_id in current_by_video:

            already_count += 1

            continue

        print()
        print(
            f"Adding track {item['track_number']:02d} "
            f"to playlist..."
        )

        print(
            f"Title    : {item['title']}"
        )

        print(
            f"Position : {desired_position}"
        )

        playlist_item_id = add_video_to_playlist(
            youtube,
            playlist_id,
            video_id,
            desired_position,
        )

        current_by_video[video_id] = {
            "playlist_item_id": playlist_item_id,
            "video_id": video_id,
            "position": desired_position,
        }

        added_count += 1

    # ---------------------------------------------------------------
    # Re-read playlist after additions.
    #
    # This gives us the real positions assigned by YouTube.
    # ---------------------------------------------------------------

    current_items = get_playlist_items(
        youtube,
        playlist_id,
    )

    current_by_video = {}

    for item in current_items:

        video_id = item.get(
            "video_id"
        )

        if video_id:

            current_by_video[video_id] = item

    # ---------------------------------------------------------------
    # Move existing items into the desired order.
    #
    # We process from position 0 onward.
    # Moving an item to its target position causes YouTube to shift
    # the other entries automatically.
    # ---------------------------------------------------------------

    for desired_position, item in enumerate(desired):

        video_id = item["video_id"]

        current = current_by_video.get(
            video_id
        )

        if not current:

            continue

        current_position = current.get(
            "position"
        )

        try:
            current_position = int(
                current_position
            )
        except (TypeError, ValueError):
            current_position = None

        if current_position == desired_position:

            continue

        print()
        print(
            f"Ordering track {item['track_number']:02d}..."
        )

        print(
            f"Title    : {item['title']}"
        )

        print(
            f"Position : {current_position} -> "
            f"{desired_position}"
        )

        move_playlist_item(
            youtube,
            current["playlist_item_id"],
            playlist_id,
            video_id,
            desired_position,
        )

        moved_count += 1

        # Re-read after every move.
        #
        # This keeps our position information accurate because moving
        # one item shifts other items.
        current_items = get_playlist_items(
            youtube,
            playlist_id,
        )

        current_by_video = {}

        for current_item in current_items:

            current_video_id = current_item.get(
                "video_id"
            )

            if current_video_id:

                current_by_video[current_video_id] = (
                    current_item
                )

    return {
        "added": added_count,
        "moved": moved_count,
        "already_present": already_count,
        "total": len(desired),
    }


# ---------------------------------------------------------------------------
# Command-line arguments
# ---------------------------------------------------------------------------

def parse_arguments():

    parser = argparse.ArgumentParser(
        description=(
            "Upload rendered music videos to YouTube and set their generated "
            "custom thumbnails."
        )
    )

    parser.add_argument(
        "album",
        help=(
            "Album JSON filename from ./input, "
            "e.g. 'Vođeni Duhom.json'"
        ),
    )

    parser.add_argument(
        "tracks",
        nargs="*",
        type=int,
        help="Optional track numbers to upload.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Upload even if the track already has a recorded "
            "YouTube video ID."
        ),
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    args = parse_arguments()

    tracks = load_album(
        args.album
    )

    album = tracks[0].get(
        "Album",
        "",
    )

    if not album:

        raise ValueError(
            "Album name is missing from metadata."
        )

    # ---------------------------------------------------------------
    # Select tracks
    # ---------------------------------------------------------------

    if args.tracks:

        selected_tracks = []

        for track_number in args.tracks:

            selected_tracks.append(
                find_track(
                    tracks,
                    track_number,
                )
            )

    else:

        selected_tracks = tracks

    # ---------------------------------------------------------------
    # State
    # ---------------------------------------------------------------

    state = load_state()

    # ---------------------------------------------------------------
    # Authenticate
    # ---------------------------------------------------------------

    youtube = get_youtube_service()

    # ---------------------------------------------------------------
    # Header
    # ---------------------------------------------------------------

    print()
    print("=" * 70)
    print("YOUTUBE UPLOAD")
    print("=" * 70)
    print(f"Album                 : {album}")
    print(f"Tracks available      : {len(tracks)}")
    print(f"Tracks selected       : {len(selected_tracks)}")
    print("Upload visibility     : PRIVATE")
    print("Playlist visibility   : PRIVATE")
    print("Automatic publishing  : DISABLED")
    print("=" * 70)

    # ---------------------------------------------------------------
    # Counters
    # ---------------------------------------------------------------

    uploaded_count = 0
    already_count = 0
    missing_count = 0
    failed_count = 0

    thumbnail_uploaded_count = 0
    thumbnail_already_count = 0
    thumbnail_missing_count = 0
    thumbnail_failed_count = 0

    # ---------------------------------------------------------------
    # Process tracks
    # ---------------------------------------------------------------

    for track in selected_tracks:

        track_number = int(
            track["Track Number"]
        )

        title = track["Title"]

        language = track.get(
            "Language",
            "",
        )

        state_key = (
            f"{album}:{track_number:04d}"
        )

        print()
        print("=" * 70)
        print(
            f"TRACK {track_number:02d} - {title}"
        )
        print("=" * 70)

        # -----------------------------------------------------------
        # Already uploaded?
        # -----------------------------------------------------------

        existing = state["videos"].get(
            state_key
        )

        if existing and not args.force:

            video_id = existing.get("video_id", "")

            print(
                f"YouTube video : {video_id}"
            )

            print(
                "STATUS        : ALREADY UPLOADED"
            )

            print(
                "UPLOAD        : SKIPPED"
            )

            already_count += 1

            # -----------------------------------------------------------
            # Ensure the current generated thumbnail is applied.
            #
            # We compare SHA-256 hashes so a normal rerun does not
            # repeatedly spend YouTube thumbnail-upload quota, while a
            # deliberately regenerated local thumbnail is uploaded.
            # -----------------------------------------------------------

            try:

                thumbnail_path, thumbnail_hash = prepare_thumbnail(track)

                recorded_hash = existing.get(
                    "thumbnail_sha256",
                    "",
                )

                print()
                print("Thumbnail")
                print(f"File        : {thumbnail_path}")

                if recorded_hash == thumbnail_hash:

                    print(
                        "THUMBNAIL   : ALREADY UPLOADED"
                    )

                    thumbnail_already_count += 1

                else:

                    print(
                        "Uploading custom thumbnail..."
                    )

                    upload_thumbnail(
                        youtube,
                        video_id,
                        thumbnail_path,
                    )

                    existing["thumbnail_path"] = str(thumbnail_path)
                    existing["thumbnail_sha256"] = thumbnail_hash
                    existing["thumbnail_uploaded"] = True

                    save_state(state)

                    print(
                        "THUMBNAIL   : UPLOADED"
                    )

                    thumbnail_uploaded_count += 1

            except FileNotFoundError as e:

                print()
                print(
                    "THUMBNAIL   : MISSING"
                )
                print(
                    f"DETAIL      : {e}"
                )
                thumbnail_missing_count += 1

            except HttpError as e:

                print()
                print(
                    "THUMBNAIL   : YOUTUBE API ERROR"
                )
                print(
                    f"DETAIL      : {e}"
                )
                thumbnail_failed_count += 1

            except Exception as e:

                print()
                print(
                    "THUMBNAIL   : FAILED"
                )
                print(
                    f"DETAIL      : {e}"
                )
                thumbnail_failed_count += 1

            continue

        try:

            # -------------------------------------------------------
            # Template
            # -------------------------------------------------------

            template_path, template = (
                load_description_template(
                    language
                )
            )

            # -------------------------------------------------------
            # Video
            # -------------------------------------------------------

            video_path, description = (
                prepare_track(
                    track,
                    template,
                )
            )

            print(
                f"Video       : {video_path}"
            )

            print(
                f"Language    : {language}"
            )

            print(
                f"Template    : {template_path}"
            )

            print(
                "Visibility  : PRIVATE"
            )

            # -------------------------------------------------------
            # Upload
            # -------------------------------------------------------

            video_id = upload_video(
                youtube,
                video_path,
                track,
                description,
            )

            # -------------------------------------------------------
            # Save state immediately
            # -------------------------------------------------------

            state["videos"][state_key] = {
                "album": album,
                "track_number": track_number,
                "title": title,
                "video_id": video_id,
                "visibility": "private",
                "video_path": str(video_path),
                "thumbnail_path": "",
                "thumbnail_sha256": "",
                "thumbnail_uploaded": False,
            }

            save_state(
                state
            )

            # -------------------------------------------------------
            # Result
            # -------------------------------------------------------

            print()
            print(
                f"YouTube video ID : {video_id}"
            )

            print(
                "STATUS            : UPLOADED"
            )

            print(
                "VISIBILITY        : PRIVATE"
            )

            print(
                "PUBLISH            : NOT PERFORMED"
            )

            # -------------------------------------------------------
            # Thumbnail
            # -------------------------------------------------------

            try:

                thumbnail_path, thumbnail_hash = prepare_thumbnail(track)

                print()
                print("Thumbnail")
                print(f"File              : {thumbnail_path}")
                print("Setting custom thumbnail...")

                upload_thumbnail(
                    youtube,
                    video_id,
                    thumbnail_path,
                )

                state["videos"][state_key][
                    "thumbnail_path"
                ] = str(thumbnail_path)

                state["videos"][state_key][
                    "thumbnail_sha256"
                ] = thumbnail_hash

                state["videos"][state_key][
                    "thumbnail_uploaded"
                ] = True

                save_state(state)

                print(
                    "THUMBNAIL          : UPLOADED"
                )

                thumbnail_uploaded_count += 1

            except FileNotFoundError as e:

                print()
                print(
                    "THUMBNAIL          : MISSING"
                )
                print(
                    f"DETAIL             : {e}"
                )
                thumbnail_missing_count += 1

            except HttpError as e:

                print()
                print(
                    "THUMBNAIL          : YOUTUBE API ERROR"
                )
                print(
                    f"DETAIL             : {e}"
                )
                thumbnail_failed_count += 1

            except Exception as e:

                print()
                print(
                    "THUMBNAIL          : FAILED"
                )
                print(
                    f"DETAIL             : {e}"
                )
                thumbnail_failed_count += 1

            uploaded_count += 1

        except FileNotFoundError as e:

            print()
            print(
                "STATUS : MISSING VIDEO"
            )

            print(
                f"DETAIL : {e}"
            )

            print(
                "UPLOAD : NOT PERFORMED"
            )

            missing_count += 1

        except HttpError as e:

            print()
            print(
                "STATUS : YOUTUBE API ERROR"
            )

            print(
                f"DETAIL : {e}"
            )

            print(
                "UPLOAD : FAILED"
            )

            failed_count += 1

        except Exception as e:

            print()
            print(
                "STATUS : FAILED"
            )

            print(
                f"DETAIL : {e}"
            )

            print(
                "UPLOAD : FAILED"
            )

            failed_count += 1

    # -----------------------------------------------------------------------
    # Playlist
    #
    # IMPORTANT:
    #
    # Playlist synchronization happens AFTER the selected upload processing.
    #
    # This means:
    #
    # - videos are uploaded first
    # - their IDs are saved
    # - then the playlist is synchronized
    #
    # We use ALL album metadata here, not only selected_tracks.
    # Therefore previously uploaded tracks can also be added to the playlist.
    # -----------------------------------------------------------------------

    playlist_created = False
    playlist_added = 0
    playlist_moved = 0
    playlist_already = 0
    playlist_total = 0

    try:

        print()
        print("=" * 70)
        print("ALBUM PLAYLIST")
        print("=" * 70)

        playlist_id, playlist_created = (
            get_or_create_playlist(
                youtube,
                album,
                state,
            )
        )

        print(
            f"Playlist title : {album}"
        )

        print(
            f"Playlist ID    : {playlist_id}"
        )

        print(
            "Visibility     : PRIVATE"
        )

        if playlist_created:

            print(
                "Status         : CREATED"
            )

        else:

            print(
                "Status         : EXISTING"
            )

        # -----------------------------------------------------------
        # Synchronize all uploaded videos belonging to this album.
        # -----------------------------------------------------------

        playlist_result = sync_playlist(
            youtube,
            playlist_id,
            tracks,
            state,
        )

        playlist_added = playlist_result["added"]
        playlist_moved = playlist_result["moved"]
        playlist_already = playlist_result["already_present"]
        playlist_total = playlist_result["total"]

        # -----------------------------------------------------------
        # Save playlist state.
        # -----------------------------------------------------------

        state["playlists"][album] = {
            "album": album,
            "playlist_id": playlist_id,
            "title": album,
            "visibility": "private",
        }

        save_state(
            state
        )

        print()
        print(
            "PLAYLIST STATUS : READY"
        )

    except HttpError as e:

        print()
        print(
            "PLAYLIST STATUS : YOUTUBE API ERROR"
        )

        print(
            f"DETAIL           : {e}"
        )

    except Exception as e:

        print()
        print(
            "PLAYLIST STATUS : FAILED"
        )

        print(
            f"DETAIL           : {e}"
        )

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print(
        f"Uploaded             : {uploaded_count}"
    )

    print(
        f"Already uploaded     : {already_count}"
    )

    print(
        f"Missing video        : {missing_count}"
    )

    print(
        f"Failed               : {failed_count}"
    )

    print()
    print("THUMBNAILS")
    print(
        f"Uploaded             : {thumbnail_uploaded_count}"
    )
    print(
        f"Already uploaded     : {thumbnail_already_count}"
    )
    print(
        f"Missing              : {thumbnail_missing_count}"
    )
    print(
        f"Failed               : {thumbnail_failed_count}"
    )

    print("-" * 70)

    print(
        f"Playlist created     : "
        f"{'YES' if playlist_created else 'NO'}"
    )

    print(
        f"Playlist added       : {playlist_added}"
    )

    print(
        f"Playlist reordered   : {playlist_moved}"
    )

    print(
        f"Already in playlist  : {playlist_already}"
    )

    print(
        f"Playlist videos      : {playlist_total}"
    )

    print("=" * 70)

    print(
        "All uploaded videos were PRIVATE."
    )

    print(
        "The album playlist is PRIVATE."
    )

    print(
        "No videos were automatically published."
    )

    print(
        f"State file: {STATE_FILE}"
    )

    print("=" * 70)
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nInterrupted."
        )

        sys.exit(130)

    except Exception as e:

        print(
            f"\nERROR: {e}",
            file=sys.stderr,
        )

        sys.exit(1)
