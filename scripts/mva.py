#!/usr/bin/env python3
"""
MVA - interactive workflow front-end.

This is a control layer around the existing MVA scripts. It deliberately
does not replace their processing logic.

Normal workflow:
    discover -> build/Whisper/LRC -> human lyrics review -> render
    -> thumbnail -> YouTube

A track is considered complete when upload_youtube.py has recorded its
YouTube video in data/youtube_state.json. Existing completed tracks are
never automatically reprocessed.

Lyrics approval is tracked by SHA-256 of the reviewed LRC. If the LRC is
edited later, the approval becomes invalid and the track must be reviewed
again before rendering.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "input"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
OUTPUT_DIR = PROJECT_ROOT / "output"
DATA_DIR = PROJECT_ROOT / "data"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
MVA_STATE_FILE = DATA_DIR / "mva_state.json"
YOUTUBE_STATE_FILE = DATA_DIR / "youtube_state.json"


def clear_screen() -> None:
    if sys.stdout.isatty():
        os.system("clear")


def pause(message: str = "Press Enter to continue...") -> None:
    input(f"\n{message}")


def python_executable() -> str:
    """Use the project's virtualenv automatically when it exists."""
    if VENV_PYTHON.is_file() and os.access(VENV_PYTHON, os.X_OK):
        return str(VENV_PYTHON)
    return sys.executable


def run_script(script_name: str, *args: str) -> None:
    command = [python_executable(), str(SCRIPTS_DIR / script_name), *args]
    print()
    print("=" * 72)
    print("RUNNING:", " ".join(shlex.quote(str(x)) for x in command))
    print("=" * 72)
    subprocess.run(command, check=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_mva_state() -> dict[str, Any]:
    state = load_json(MVA_STATE_FILE, {"lyrics_approved": {}})
    state.setdefault("lyrics_approved", {})
    return state


def save_mva_state(state: dict[str, Any]) -> None:
    save_json(MVA_STATE_FILE, state)


def load_youtube_state() -> dict[str, Any]:
    state = load_json(YOUTUBE_STATE_FILE, {"videos": {}, "playlists": {}})
    state.setdefault("videos", {})
    state.setdefault("playlists", {})
    return state


def parse_track_number(value: Any) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 999999


def read_album(json_path: Path) -> tuple[str, list[dict[str, Any]]]:
    data = load_json(json_path, {})
    tracks = data.get("data", [])
    if not isinstance(tracks, list) or not tracks:
        raise RuntimeError(f"No tracks found in {json_path.name}")

    tracks = [track for track in tracks if isinstance(track, dict)]
    album_names = {
        str(track.get("Album", "")).strip()
        for track in tracks
        if str(track.get("Album", "")).strip()
    }
    if not album_names:
        raise RuntimeError(f"No Album field found in {json_path.name}")
    if len(album_names) != 1:
        raise RuntimeError(
            f"Multiple album names found in {json_path.name}: "
            + ", ".join(sorted(album_names))
        )

    tracks.sort(key=lambda track: parse_track_number(track.get("Track Number")))
    return next(iter(album_names)), tracks


def album_background(album_name: str) -> Path:
    return PROJECT_ROOT / "assets" / f"{album_name.replace(' ', '_')}.png"


def track_key(album_name: str, track_number: int) -> str:
    return f"{album_name}:{track_number:04d}"


def track_stem(track: dict[str, Any]) -> str:
    number = parse_track_number(track.get("Track Number"))
    title = str(track.get("Title", "")).strip()
    return f"{number:02d} - {title}"


def output_dir(album_name: str) -> Path:
    return OUTPUT_DIR / album_name


def lrc_path(album_name: str, track: dict[str, Any]) -> Path:
    return output_dir(album_name) / f"{track_stem(track)}.lrc"


def video_path(album_name: str, track: dict[str, Any]) -> Path:
    return output_dir(album_name) / f"{track_stem(track)}.mp4"


def thumbnail_path(album_name: str, track: dict[str, Any]) -> Path:
    return output_dir(album_name) / f"{track_stem(track)}.jpg"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def youtube_uploaded(
    youtube_state: dict[str, Any],
    album_name: str,
    track: dict[str, Any],
) -> bool:
    """Return True only when the saved YouTube record matches this track."""
    number = parse_track_number(track.get("Track Number"))
    key = track_key(album_name, number)
    record = youtube_state.get("videos", {}).get(key)
    if not isinstance(record, dict):
        return False

    # Track number is part of the key, but the title is an important guard
    # against a reused track number after an album JSON is edited.
    saved_title = str(record.get("title", "")).strip()
    current_title = str(track.get("Title", "")).strip()
    if saved_title and current_title and saved_title != current_title:
        return False

    return True


def lyrics_status(
    state: dict[str, Any],
    album_name: str,
    track: dict[str, Any],
) -> str:
    path = lrc_path(album_name, track)
    if not path.is_file():
        return "NOT GENERATED"

    key = track_key(album_name, parse_track_number(track.get("Track Number")))
    approved_hash = state.get("lyrics_approved", {}).get(key)
    if not approved_hash:
        return "REVIEW NEEDED"

    try:
        current_hash = file_sha256(path)
    except OSError:
        return "REVIEW NEEDED"

    if current_hash != approved_hash:
        return "CHANGED - REVIEW"

    return "APPROVED"


def track_status(
    mva_state: dict[str, Any],
    youtube_state: dict[str, Any],
    album_name: str,
    track: dict[str, Any],
) -> str:
    if youtube_uploaded(youtube_state, album_name, track):
        if video_path(album_name, track).is_file():
            return "COMPLETE"
        return "UPLOADED - LOCAL FILES MISSING"

    if video_path(album_name, track).is_file():
        return "VIDEO READY"

    return lyrics_status(mva_state, album_name, track)


def discover() -> list[tuple[Path, str, list[dict[str, Any]]]]:
    result = []
    for path in sorted(INPUT_DIR.glob("*.json")):
        try:
            album, tracks = read_album(path)
            result.append((path, album, tracks))
        except Exception as exc:
            print(f"WARNING: {path.name}: {exc}")
    return result


def print_album_summary(
    mva_state: dict[str, Any],
    youtube_state: dict[str, Any],
    album: str,
    tracks: list[dict[str, Any]],
) -> None:
    print(f"  {album}")
    for track in tracks:
        number = parse_track_number(track.get("Track Number"))
        title = str(track.get("Title", "")).strip()
        status = track_status(mva_state, youtube_state, album, track)
        marker = {
            "COMPLETE": "✓",
            "UPLOADED - LOCAL FILES MISSING": "✓",
            "APPROVED": "→",
            "VIDEO READY": "→",
            "NOT GENERATED": "★",
            "REVIEW NEEDED": "!",
            "CHANGED - REVIEW": "!",
        }.get(status, "·")
        print(f"    {marker} {number:02d}  {title}  [{status}]")


def choose_album(
    albums: list[tuple[Path, str, list[dict[str, Any]]]],
) -> tuple[Path, str, list[dict[str, Any]]] | None:
    if not albums:
        print(f"No album JSON files found in {INPUT_DIR}")
        return None

    while True:
        clear_screen()
        print("=" * 72)
        print("MVA - ALBUMS")
        print("=" * 72)
        print()
        for index, (_, album, tracks) in enumerate(albums, 1):
            print(f"  {index}. {album} ({len(tracks)} tracks)")
        print()
        print("  Q. Quit")

        choice = input("\nSelect album: ").strip().lower()
        if choice == "q":
            return None
        try:
            index = int(choice) - 1
            if 0 <= index < len(albums):
                return albums[index]
        except ValueError:
            pass
        print("Please choose one of the listed albums.")


def process_lyrics(
    json_path: Path,
) -> None:
    # These are intentionally the existing, proven scripts.
    run_script("build_jobs.py", json_path.name)
    run_script("whisper_one.py")
    run_script("generate_lyrics_lrc.py")


def open_lrc(path: Path) -> None:
    if not path.exists():
        print(f"LRC does not exist: {path}")
        return

    editor = os.environ.get("MVA_EDITOR") or os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        command = shlex.split(editor) + [str(path)]
        subprocess.run(command, check=False)
        return

    # Graphical Linux fallback. We deliberately do not wait for xdg-open.
    if shutil_which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path)])
        return

    print(f"Open this file manually: {path}")


def shutil_which(command: str) -> str | None:
    # Keep the interface dependency-free.
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def approve_lyrics(
    state: dict[str, Any],
    album: str,
    track: dict[str, Any],
) -> bool:
    path = lrc_path(album, track)
    if not path.is_file():
        print(f"LRC not found: {path}")
        return False

    key = track_key(album, parse_track_number(track.get("Track Number")))
    state.setdefault("lyrics_approved", {})[key] = file_sha256(path)
    save_mva_state(state)
    print("Lyrics approved. The current LRC hash has been recorded.")
    return True


def render_track(album: str, track: dict[str, Any]) -> None:
    number = parse_track_number(track.get("Track Number"))
    background = album_background(album)
    if not background.is_file():
        raise RuntimeError(f"Render background not found: {background}")

    # Rebuild jobs so jobs.json is guaranteed to represent this album.
    # render.py then receives only the selected track number.
    json_path = find_album_json(album)
    run_script("build_jobs.py", json_path.name)
    run_script("render.py", "--background", str(background), str(number))


def generate_thumbnail(album: str, track: dict[str, Any]) -> None:
    number = parse_track_number(track.get("Track Number"))
    json_path = find_album_json(album)
    run_script("generate_thumbnail.py", json_path.name, str(number))


def upload_track(album: str, track: dict[str, Any]) -> None:
    number = parse_track_number(track.get("Track Number"))
    json_path = find_album_json(album)
    run_script("upload_youtube.py", json_path.name, str(number))


def confirm_guided_upload(album: str, track: dict[str, Any]) -> bool:
    number = parse_track_number(track.get("Track Number"))
    title = str(track.get("Title", "")).strip()
    video = video_path(album, track)
    thumbnail = thumbnail_path(album, track)

    print()
    print("=" * 72)
    print("READY FOR YOUTUBE UPLOAD")
    print("=" * 72)
    print(f"Track     : {number:02d} - {title}")
    print(f"Video     : {video}")
    print(f"Thumbnail : {thumbnail}")
    print()
    print("Review the rendered video and thumbnail before uploading.")
    print()
    try:
        answer = input("Press Enter to upload, or type N to cancel: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nUpload cancelled.")
        return False

    if answer in ("n", "no", "q", "quit", "c", "cancel"):
        print("Upload cancelled.")
        return False

    return True


def find_album_json(album: str) -> Path:
    for path in INPUT_DIR.glob("*.json"):
        try:
            current_album, _ = read_album(path)
            if current_album == album:
                return path
        except Exception:
            continue
    raise RuntimeError(f"Could not find JSON for album '{album}'")


def require_lyrics_approved(
    state: dict[str, Any],
    album: str,
    track: dict[str, Any],
) -> bool:
    status = lyrics_status(state, album, track)
    if status != "APPROVED":
        print(f"Lyrics are not approved: {status}")
        print("Review/fix the LRC first.")
        return False
    return True


def complete_track(
    json_path: Path,
    album: str,
    track: dict[str, Any],
    mva_state: dict[str, Any],
    youtube_state: dict[str, Any],
) -> None:
    if youtube_uploaded(youtube_state, album, track):
        print("This track is already uploaded. Nothing to do.")
        return

    if lyrics_status(mva_state, album, track) == "NOT GENERATED":
        print("Lyrics are not generated yet.")
        process_lyrics(json_path)
        print()
        print("Lyrics generated. Review them before continuing.")
        return

    if not require_lyrics_approved(mva_state, album, track):
        return

    if not video_path(album, track).is_file():
        render_track(album, track)

    if not thumbnail_path(album, track).is_file():
        generate_thumbnail(album, track)

    if not youtube_uploaded(youtube_state, album, track):
        if confirm_guided_upload(album, track):
            upload_track(album, track)
        else:
            print("Guided completion stopped before YouTube upload.")


def track_menu(
    json_path: Path,
    album: str,
    track: dict[str, Any],
    mva_state: dict[str, Any],
    youtube_state: dict[str, Any],
) -> None:
    while True:
        number = parse_track_number(track.get("Track Number"))
        title = str(track.get("Title", "")).strip()

        # Reload YouTube state because upload_youtube.py may have changed it.
        youtube_state = load_youtube_state()
        status = track_status(mva_state, youtube_state, album, track)

        clear_screen()
        print("=" * 72)
        print(f"MVA - {album}")
        print(f"TRACK {number:02d} - {title}")
        print("=" * 72)
        print()
        print(f"  Lyrics    : {lyrics_status(mva_state, album, track)}")
        print(f"  Video     : {'READY' if video_path(album, track).is_file() else 'NOT RENDERED'}")
        print(f"  Thumbnail : {'READY' if thumbnail_path(album, track).is_file() else 'NOT GENERATED'}")
        print(f"  YouTube   : {'UPLOADED' if youtube_uploaded(youtube_state, album, track) else 'NOT UPLOADED'}")
        print()
        print(f"Overall: {status}")
        print()
        print("  P  Process lyrics (build + Whisper + LRC)")
        print("  O  Open LRC for review")
        print("  A  Approve current LRC")
        print("  R  Render video")
        print("  T  Generate thumbnail")
        print("  U  Upload to YouTube")
        print("  C  Complete song (guided)")
        print("  B  Back")
        print()

        choice = input("Action: ").strip().lower()

        try:
            if choice == "p":
                process_lyrics(json_path)
                pause("Lyrics generated/updated. Press Enter to continue...")
            elif choice == "o":
                open_lrc(lrc_path(album, track))
                pause("When you are finished editing, press Enter here...")
            elif choice == "a":
                if lyrics_status(mva_state, album, track) in ("NOT GENERATED",):
                    print("Generate the LRC first.")
                else:
                    approve_lyrics(mva_state, album, track)
                pause()
            elif choice == "r":
                if require_lyrics_approved(mva_state, album, track):
                    render_track(album, track)
                pause()
            elif choice == "t":
                if not video_path(album, track).is_file():
                    print("Render the video first.")
                else:
                    generate_thumbnail(album, track)
                pause()
            elif choice == "u":
                if not video_path(album, track).is_file():
                    print("Render the video first.")
                elif not thumbnail_path(album, track).is_file():
                    print("Generate the thumbnail first.")
                else:
                    upload_track(album, track)
                pause()
            elif choice == "c":
                complete_track(json_path, album, track, mva_state, load_youtube_state())
                pause()
            elif choice == "b":
                return
        except subprocess.CalledProcessError as exc:
            print(f"\nStep failed with exit code {exc.returncode}.")
            pause()
        except Exception as exc:
            print(f"\nERROR: {exc}")
            pause()


def album_menu(
    json_path: Path,
    album: str,
    tracks: list[dict[str, Any]],
    mva_state: dict[str, Any],
    youtube_state: dict[str, Any],
) -> None:
    while True:
        mva_state = load_mva_state()
        youtube_state = load_youtube_state()
        clear_screen()

        print("=" * 72)
        print(f"MVA - {album}")
        print("=" * 72)
        print()
        print_album_summary(mva_state, youtube_state, album, tracks)
        print()
        print("  N  Process lyrics for new/pending tracks")
        print("  C  Complete a track")
        print("  R  Review lyrics")
        print("  S  Refresh status")
        print("  B  Back to albums")
        print()

        choice = input("Action: ").strip().lower()

        if choice == "b":
            return
        if choice == "s":
            continue
        if choice == "n":
            try:
                process_lyrics(json_path)
                pause("Processing finished. Review the generated LRC files.")
            except subprocess.CalledProcessError as exc:
                print(f"\nStep failed with exit code {exc.returncode}.")
                pause()
            except Exception as exc:
                print(f"\nERROR: {exc}")
                pause()
        elif choice in ("c", "r"):
            pending = [
                track for track in tracks
                if track_status(mva_state, youtube_state, album, track) != "COMPLETE"
            ]
            if not pending:
                print("No unfinished tracks.")
                pause()
                continue

            print()
            for index, track in enumerate(pending, 1):
                number = parse_track_number(track.get("Track Number"))
                title = str(track.get("Title", "")).strip()
                status = track_status(mva_state, youtube_state, album, track)
                print(f"  {index}. {number:02d} - {title} [{status}]")
            selection = input("\nSelect track (or B): ").strip().lower()
            if selection == "b":
                continue
            try:
                selected = pending[int(selection) - 1]
                track_menu(
                    json_path,
                    album,
                    selected,
                    mva_state,
                    youtube_state,
                )
            except (ValueError, IndexError):
                print("Invalid track.")
                pause()


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        albums = discover()
        if not albums:
            print(f"No usable album JSON files found in {INPUT_DIR}")
            return 1

        selected = choose_album(albums)
        if selected is None:
            return 0

        json_path, album, tracks = selected
        album_menu(
            json_path,
            album,
            tracks,
            load_mva_state(),
            load_youtube_state(),
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
