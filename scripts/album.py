#!/usr/bin/env python3

import subprocess
import sys
from pathlib import Path
from datetime import datetime


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "input"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
LOGS_DIR = PROJECT_ROOT / "logs"
ASSETS_DIR = PROJECT_ROOT / "assets"

# Set to False to completely disable the render step by default.
RENDER_ENABLED = True


def album_to_background(album_name):
    """
    Convert an album name into its background image filename.

    Example:
        "Led by the Spirit"
        -> assets/Led_by_the_Spirit.png
    """

    filename = album_name.replace(" ", "_")

    return ASSETS_DIR / f"{filename}.png"


def run_step(script_name, *arguments):

    command = [
        sys.executable,
        str(SCRIPTS_DIR / script_name),
        *arguments,
    ]

    print()
    print("=" * 70)
    print(f"RUNNING: {script_name}")
    print("=" * 70)
    print()

    subprocess.run(
        command,
        check=True,
    )


def main():

    # --------------------------------------------------------------
    # Command line arguments.
    # --------------------------------------------------------------

    render_from_command_line = "--render" in sys.argv

    json_files = sorted(
        INPUT_DIR.glob("*.json")
    )

    if not json_files:

        print(
            f"No JSON files found in {INPUT_DIR}"
        )

        sys.exit(1)

    print()
    print("=" * 70)
    print("MUSIC VIDEO AUTOMATION")
    print("=" * 70)
    print()
    print("Available albums:")
    print()

    for index, json_path in enumerate(
        json_files,
        start=1,
    ):

        print(
            f"  {index}. {json_path.stem}"
        )

    print()

    while True:

        choice = input(
            f"Select album [1-{len(json_files)}]: "
        ).strip()

        try:
            choice_number = int(choice)

        except ValueError:

            print(
                "Please enter a number."
            )

            continue

        if 1 <= choice_number <= len(json_files):
            break

        print(
            f"Please enter a number between "
            f"1 and {len(json_files)}."
        )

    selected_json = json_files[
        choice_number - 1
    ]

    # --------------------------------------------------------------
    # Determine background from album name.
    # --------------------------------------------------------------

    album_name = selected_json.stem

    background_path = album_to_background(
        album_name
    )

    # --------------------------------------------------------------
    # Logging starts after album selection.
    # --------------------------------------------------------------

    LOGS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_file = LOGS_DIR / f"{selected_json.stem}.log"

    log_file_handle = log_file.open(
        "a",
        encoding="utf-8",
    )

    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, data):
            for stream in self.streams:
                stream.write(data)
                stream.flush()

        def flush(self):
            for stream in self.streams:
                stream.flush()

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    sys.stdout = Tee(
        original_stdout,
        log_file_handle,
    )

    sys.stderr = Tee(
        original_stderr,
        log_file_handle,
    )

    print()
    print("=" * 70)
    print("ALBUM RUN START")
    print("=" * 70)
    print(
        f"Album      : {album_name}"
    )
    print(
        f"Background : {background_path}"
    )
    print(
        f"Time       : {datetime.now().isoformat(timespec='seconds')}"
    )
    print(
        f"Log        : {log_file}"
    )
    print("=" * 70)
    print()

    print(
        f"Selected: {selected_json.name}"
    )

    # --------------------------------------------------------------
    # Verify that the album background exists.
    # --------------------------------------------------------------

    if not background_path.exists():

        print()
        print(
            "ERROR: Background image not found:"
        )
        print(
            f"       {background_path}"
        )
        print()

        sys.exit(1)

    try:

        run_step(
            "build_jobs.py",
            selected_json.name,
        )

        run_step(
            "whisper_one.py",
        )

        run_step(
            "generate_lyrics_lrc.py",
        )

        # ----------------------------------------------------------
        # Optional render step.
        # ----------------------------------------------------------

        if RENDER_ENABLED:

            if render_from_command_line:

                # --render was supplied, so render without asking.
                run_step(
                    "render.py",
                    "--background",
                    str(background_path),
                )

            else:

                while True:

                    render_choice = input(
                        "Render album? [y/n]: "
                    ).strip().lower()

                    if render_choice in ("y", "n"):
                        break

                    print(
                        "Please enter y or n."
                    )

                if render_choice == "y":

                    # Pass the album-specific background to render.py.
                    run_step(
                        "render.py",
                        "--background",
                        str(background_path),
                    )

                else:

                    print()
                    print(
                        "Render skipped."
                    )
                    print()

    except subprocess.CalledProcessError as exc:

        print()
        print("=" * 70)
        print("PIPELINE FAILED")
        print("=" * 70)
        print()
        print(
            f"Step exited with code {exc.returncode}"
        )
        print()
        print(
            "The pipeline has stopped."
        )

        sys.exit(
            exc.returncode
        )

    print()
    print("=" * 70)
    print("ALBUM PROCESSING COMPLETE")
    print("=" * 70)
    print()
    print(
        f"Input : {selected_json.name}"
    )
    print(
        f"Background : {background_path}"
    )
    print()
    print(
        "The album has been processed successfully."
    )
    print()

    print("=" * 70)
    print("ALBUM RUN END")
    print(
        f"Time: {datetime.now().isoformat(timespec='seconds')}"
    )
    print()

    log_file_handle.close()


if __name__ == "__main__":
    main()
