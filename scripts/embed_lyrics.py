#!/usr/bin/env python3

# ============================================================================
# EMBED SYNCHRONIZED LYRICS INTO FLAC FILES
# ============================================================================
#
# Purpose:
#   Embeds synchronized lyrics (from .lrc files) into the corresponding
#   FLAC files as an ID3v2 SYLT frame (Kid3 "Tag 3").
#
# Requirements:
#   pip install mutagen   (or: sudo pacman -S python-mutagen)
#
# Usage:
#   python3 scripts/embed_lyrics.py "Album Name"
#   python3 scripts/embed_lyrics.py "Album Name" --force
# ============================================================================

import re
import sys
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, SYLT, Encoding, ID3NoHeaderError


# ============================================================================
# Configuration
# ============================================================================

# ISO 639-2 language code used in the SYLT frame
# Common examples: 'hrv' (Croatian), 'eng' (English), 'deu' (German),
#                  'fra' (French), 'spa' (Spanish), 'und' (undefined)
LANGUAGE = "hrv"


# ============================================================================
# Paths
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR   = PROJECT_ROOT / "output"
MUSIC_DIR    = Path("/home/iggy/Glazba/Iggy4King")


# ============================================================================
# LRC parsing
# ============================================================================

_LRC_LINE = re.compile(
    r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]\s*(.*)"
)

def parse_lrc(lrc_path: Path) -> list[tuple[str, int]]:
    """
    Convert an LRC file into the list of (text, milliseconds) tuples
    required by mutagen.id3.SYLT.
    """
    entries = []
    with open(lrc_path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            m = _LRC_LINE.match(raw)
            if not m:
                continue
            minutes, seconds, frac, text = m.groups()
            ms = int(minutes) * 60_000 + int(seconds) * 1_000
            if frac:
                # normalise to exactly 3 digits
                frac = (frac + "000")[:3]
                ms += int(frac)
            text = text.strip()
            if text:                       # skip empty lyric lines
                entries.append((text, ms))
    # SYLT expects the list sorted by timestamp
    entries.sort(key=lambda x: x[1])
    return entries


# ============================================================================
# Helpers
# ============================================================================

def find_audio_file(album_dir: Path, lrc_file: Path) -> Path | None:
    """
    Map   01 - Title.lrc   →   01 Title.flac
    """
    stem = lrc_file.stem
    parts = stem.split(" - ", 1)
    if len(parts) == 2:
        track_number, title = parts
        audio_name = f"{track_number} {title}.flac"
    else:
        audio_name = f"{stem}.flac"
    candidate = album_dir / audio_name
    return candidate if candidate.exists() else None


def has_synced_lyrics(audio_file: Path) -> bool:
    """Return True if the FLAC already contains a SYLT frame."""
    try:
        tags = ID3(audio_file)
        return bool(tags.getall("SYLT"))
    except ID3NoHeaderError:
        return False
    except Exception:
        return False


def embed_lyrics(lrc_file: Path, audio_file: Path) -> None:
    """
    Parse the LRC and write a proper ID3v2 SYLT frame into the FLAC
    (creating Tag 3 if it does not exist yet).
    """
    sylt_text = parse_lrc(lrc_file)
    if not sylt_text:
        raise ValueError(f"No timed lyrics found in {lrc_file.name}")

    # Load the FLAC (keeps existing Vorbis comments intact)
    audio = FLAC(str(audio_file))

    # Obtain / create the ID3v2 tag (Kid3 "Tag 3")
    try:
        id3 = ID3(str(audio_file))
    except ID3NoHeaderError:
        id3 = ID3()

    # Replace any previous SYLT frames
    id3.delall("SYLT")

    frame = SYLT(
        encoding=Encoding.UTF8,
        lang=LANGUAGE,       # ← uses the configurable variable
        format=2,            # milliseconds
        type=1,              # lyrics
        desc="Lyrics",
        text=sylt_text,
    )
    id3.add(frame)

    # Save the ID3 tag back into the FLAC file
    id3.save(str(audio_file), v2_version=4)

    print()
    print(f"LRC   : {lrc_file.name}")
    print(f"FLAC  : {audio_file.name}")
    print(f"Embed : SYLT / Lyrics  ({len(sylt_text)} lines)  [{LANGUAGE}]")


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    force = False
    arguments = sys.argv[1:]

    if "--force" in arguments:
        force = True
        arguments.remove("--force")

    if len(arguments) != 1:
        print()
        print("Usage:")
        print('  python3 scripts/embed_lyrics.py "Album Name"')
        print('  python3 scripts/embed_lyrics.py "Album Name" --force')
        print()
        sys.exit(1)

    album = arguments[0]

    lrc_dir   = OUTPUT_DIR / album
    album_dir = MUSIC_DIR / album

    if not lrc_dir.exists():
        print(f"LRC directory not found: {lrc_dir}")
        sys.exit(1)
    if not album_dir.exists():
        print(f"Music directory not found: {album_dir}")
        sys.exit(1)

    lrc_files = sorted(lrc_dir.glob("*.lrc"))
    if not lrc_files:
        print(f"No LRC files found in: {lrc_dir}")
        sys.exit(1)

    print()
    print("=" * 70)
    print("EMBED SYNCHRONIZED LYRICS")
    print("=" * 70)
    print()
    print(f"Album : {album}")
    print(f"LRC   : {lrc_dir}")
    print(f"FLAC  : {album_dir}")
    print(f"Lang  : {LANGUAGE}")
    print(f"Mode  : {'FORCE' if force else 'SAFE'}")
    print()
    print(f"Found {len(lrc_files)} LRC files.")
    print()

    completed = skipped = failed = 0

    for lrc_file in lrc_files:
        audio_file = find_audio_file(album_dir, lrc_file)

        if audio_file is None:
            print()
            print(f"SKIP: No matching FLAC for {lrc_file.name}")
            skipped += 1
            continue

        try:
            if not force and has_synced_lyrics(audio_file):
                print()
                print("SKIP: SYLT already exists")
                print(f"FLAC : {audio_file.name}")
                skipped += 1
                continue

            if force and has_synced_lyrics(audio_file):
                print()
                print("FORCE: Replacing existing SYLT")
                print(f"FLAC : {audio_file.name}")

            embed_lyrics(lrc_file, audio_file)
            completed += 1

        except Exception as exc:
            print()
            print(f"FAILED: {lrc_file.name}")
            print(f"        {exc}")
            failed += 1

    print()
    print("=" * 70)
    print("EMBEDDING COMPLETE")
    print("=" * 70)
    print()
    print(f"Completed : {completed}")
    print(f"Skipped   : {skipped}")
    print(f"Failed    : {failed}")
    print()

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
