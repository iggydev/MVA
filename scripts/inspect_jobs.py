#!/usr/bin/env python3

import json
import sys
from pathlib import Path

from mutagen.flac import FLAC


PROJECT_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_DIR / "input"


def find_json_files():
    return sorted(INPUT_DIR.glob("*.json"))


def find_flac(path_string):
    path = Path(path_string).expanduser()

    if path.exists():
        return path

    return None


def inspect_flac(flac_path):
    audio = FLAC(flac_path)

    pictures = audio.pictures

    return {
        "duration": audio.info.length,
        "sample_rate": audio.info.sample_rate,
        "channels": audio.info.channels,
        "pictures": len(pictures),
    }


def main():
    json_files = find_json_files()

    if not json_files:
        print(f"No JSON files found in {INPUT_DIR}")
        sys.exit(1)

    total = 0

    for json_path in json_files:
        print()
        print("=" * 70)
        print(f"Album JSON: {json_path.name}")
        print("=" * 70)

        with json_path.open("r", encoding="utf-8") as f:
            document = json.load(f)

        entries = document.get("data", [])

        print(f"Tracks in JSON: {len(entries)}")
        print()

        for index, entry in enumerate(entries, 1):
            total += 1

            title = entry.get("Title", "")
            artist = entry.get("Artist", "")
            album = entry.get("Album", "")
            track = entry.get("Track Number", "")
            lyrics = entry.get("Lyrics", "")
            file_path = entry.get("File Path", "")

            print(f"[{index:02d}] {track} - {title}")
            print(f"     Artist : {artist}")
            print(f"     Album  : {album}")

            if not file_path:
                print("     FLAC   : MISSING File Path")
                print()
                continue

            flac_path = find_flac(file_path)

            if flac_path is None:
                print(f"     FLAC   : NOT FOUND")
                print(f"              {file_path}")
                print()
                continue

            print(f"     FLAC   : {flac_path}")

            try:
                info = inspect_flac(flac_path)

                print(f"     Length : {info['duration']:.2f} sec")
                print(
                    f"     Audio  : "
                    f"{info['sample_rate']} Hz / "
                    f"{info['channels']} ch"
                )
                print(f"     Cover  : {info['pictures']} embedded image(s)")
                print(f"     Lyrics : {len(lyrics)} characters")

            except Exception as exc:
                print(f"     ERROR  : {exc}")

            print()

    print("=" * 70)
    print(f"Total tracks discovered: {total}")
    print("=" * 70)


if __name__ == "__main__":
    main()
