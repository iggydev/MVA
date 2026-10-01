#!/usr/bin/env python3

import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

from mutagen.flac import FLAC


PROJECT_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_DIR / "input"
TEMP_DIR = PROJECT_DIR / "temp"
OUTPUT_DIR = PROJECT_DIR / "output"


@dataclass
class SongJob:
    title: str
    artist: str
    album: str
    album_artist: str
    track_number: int
    year: str
    genre: str
    language: str
    website: str
    audio_path: str
    cover_path: str
    lyrics: str
    duration: float
    sample_rate: int
    channels: int
    output_path: str


def track_number(value):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 999999


def safe_filename(value):
    value = str(value or "untitled")
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .")


def extract_cover(flac_path, output_path):
    audio = FLAC(flac_path)

    if not audio.pictures:
        raise RuntimeError("No embedded artwork found")

    # Prefer front-cover artwork.
    picture = next(
        (p for p in audio.pictures if p.type == 3),
        audio.pictures[0],
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(picture.data)

    return output_path


def build_job(entry):
    title = entry.get("Title", "").strip()
    artist = entry.get("Artist", "").strip()
    album = entry.get("Album", "").strip()
    album_artist = entry.get("Album Artist", "").strip()

    audio_path = Path(entry["File Path"]).expanduser()

    if not audio_path.exists():
        raise FileNotFoundError(audio_path)

    if audio_path.suffix.lower() != ".flac":
        raise RuntimeError(f"Expected FLAC, got {audio_path.suffix}")

    audio = FLAC(audio_path)

    cover_dir = TEMP_DIR / safe_filename(album) / "covers"

    cover_filename = (
        f"{track_number(entry.get('Track Number')):02d} - "
        f"{safe_filename(title)}.jpg"
    )

    cover_path = cover_dir / cover_filename
    extract_cover(audio_path, cover_path)

    track = track_number(entry.get("Track Number"))

    output_dir = OUTPUT_DIR / safe_filename(album)

    output_filename = (
        f"{track:02d} - {safe_filename(title)}.mp4"
    )

    output_path = output_dir / output_filename

    return SongJob(
        title=title,
        artist=artist,
        album=album,
        album_artist=album_artist,
        track_number=track,
        year=entry.get("Date", "").strip(),
        genre=entry.get("Genre", "").strip(),
        language=entry.get("Language", "").strip(),
        website=entry.get("Website", "").strip(),
        audio_path=str(audio_path),
        cover_path=str(cover_path),
        lyrics=entry.get("Lyrics", ""),
        duration=float(audio.info.length),
        sample_rate=audio.info.sample_rate,
        channels=audio.info.channels,
        output_path=str(output_path),
    )


def main():

    if len(sys.argv) > 1:

        json_path = INPUT_DIR / sys.argv[1]

        if not json_path.exists():

            print(
                f"JSON file not found: {json_path}"
            )

            sys.exit(1)

        json_files = [json_path]

    else:

        json_files = sorted(
            INPUT_DIR.glob("*.json")
        )

        if not json_files:

            print(
                f"No JSON files found in {INPUT_DIR}"
            )

            sys.exit(1)

    jobs = []

    for json_path in json_files:

        print(
            f"\nReading: {json_path.name}"
        )

        with json_path.open(
            "r",
            encoding="utf-8",
        ) as f:

            document = json.load(f)

        entries = document.get(
            "data",
            []
        )

        for entry in entries:

            title = entry.get(
                "Title",
                "Unknown",
            )

            try:

                job = build_job(
                    entry
                )

                jobs.append(
                    job
                )

                print(
                    f"  OK  "
                    f"{job.track_number:02d} - "
                    f"{job.title} "
                    f"({job.duration:.1f}s)"
                )

            except Exception as exc:

                print(
                    f"  ERROR "
                    f"{title}: {exc}"
                )

    # Track-number order, regardless of JSON order.
    jobs.sort(
        key=lambda job: (
            job.album,
            job.track_number,
        )
    )

    jobs_path = TEMP_DIR / "jobs.json"

    jobs_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with jobs_path.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            [asdict(job) for job in jobs],
            f,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print("=" * 70)
    print(
        f"Jobs created : {len(jobs)}"
    )
    print(
        f"Jobs file    : {jobs_path}"
    )
    print("=" * 70)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
