#!/usr/bin/env python3

import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
JOBS_FILE = PROJECT_DIR / "temp" / "jobs.json"
WHISPER_MODEL = Path.home() / ".local/share/whisper-models/ggml-medium.bin"
WHISPER_DIR = PROJECT_DIR / "temp" / "whisper"
LRC_DIR = PROJECT_DIR / "temp" / "lrc"


def normalize_word(text):
    text = text.lower()
    text = re.sub(r"[^\w']+", "", text)
    return text


def load_jobs():
    with JOBS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def run_whisper(job):
    WHISPER_DIR.mkdir(parents=True, exist_ok=True)

    stem = (
        f"{int(job['track_number']):02d}-"
        f"{re.sub(r'[^A-Za-z0-9]+', '-', job['title']).strip('-').lower()}"
    )

    output_base = WHISPER_DIR / stem
    output_json = output_base.with_suffix(".json")

    if output_json.exists():
        print(f"Whisper JSON already exists: {output_json}")
        return output_json, "skipped"

    language = job.get("language", "").strip().lower()

    if not language:
        raise RuntimeError(
            f"No language specified for: {job['title']}"
        )

    language = job["language"].strip().lower()

    print(f"Language: {language}")

    command = [
        str(Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli"),
        "-m",
        str(WHISPER_MODEL),
        "-l",
        language,
        "-ml",
        "1",
        "-oj",
        "-of",
        str(output_base),
        job["audio_path"],
    ]

    print("Running:")
    print(" ".join(
        f'"{x}"' if " " in str(x) else str(x)
        for x in command
    ))
    print()

    subprocess.run(command, check=True)

    if not output_json.exists():
        raise RuntimeError(
            f"Whisper did not create {output_json}"
        )

    return output_json, "generated"

def extract_words(whisper_json):
    with whisper_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    pieces = []

    for item in data.get("transcription", []):
        text = item.get("text", "")

        if not text:
            continue

        start = item["offsets"]["from"] / 1000.0
        end = item["offsets"]["to"] / 1000.0

        pieces.append(
            {
                "text": text,
                "start": start,
                "end": end,
            }
        )

    words = []
    current = None

    for piece in pieces:
        text = piece["text"]

        # Ignore completely empty pieces.
        if not text.strip():
            continue

        # Punctuation belongs to the preceding word.
        if re.fullmatch(r"[^\w]+", text, flags=re.UNICODE):
            if current is not None:
                current["text"] += text
                current["end"] = piece["end"]
            continue

        # Whisper normally puts a leading space before a new word.
        starts_new_word = text.startswith(" ")

        clean = text.strip()

        if current is None:
            current = {
                "text": clean,
                "start": piece["start"],
                "end": piece["end"],
            }

        elif starts_new_word:
            words.append(current)

            current = {
                "text": clean,
                "start": piece["start"],
                "end": piece["end"],
            }

        else:
            # Continuation of the current word.
            current["text"] += clean
            current["end"] = piece["end"]

    if current is not None:
        words.append(current)

    for word in words:
        word["norm"] = normalize_word(word["text"])

    return words


def main():
    jobs = load_jobs()

    if not jobs:
        raise RuntimeError("No jobs found")

    print("=" * 70)
    print("GENERATING WHISPER TRANSCRIPTIONS")
    print("=" * 70)
    print(f"Jobs found: {len(jobs)}")
    print()

    generated = 0
    skipped = 0
    errors = 0

    for job in jobs:
        track = int(job["track_number"])
        title = job["title"]

        print("-" * 70)
        print(f"Track : {track:02d} - {title}")
        print(f"Audio : {job['audio_path']}")
        print("-" * 70)

        try:
            whisper_json, status = run_whisper(job)

            print(f"Whisper JSON: {whisper_json}")

            if status == "generated":
                generated += 1
            else:
                skipped += 1

        except Exception as exc:
            print()
            print(f"ERROR: {exc}")
            errors += 1

        print()

    print("=" * 70)
    print("WHISPER GENERATION SUMMARY")
    print("=" * 70)
    print(f"Processed : {len(jobs)}")
    print(f"Generated : {generated}")
    print(f"Skipped   : {skipped}")
    print(f"Problems  : {errors}")
    print("=" * 70)

if __name__ == "__main__":
    main()
