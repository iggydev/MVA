#!/usr/bin/env python3
"""Generate YouTube thumbnails from album assets and track metadata.

Project layout expected:

    project/
    ├── assets/
    │   └── <Album>_album.png        # complete 1920x1080 background
    ├── fonts/
    │   ├── agaramondpro_semibold.otf
    │   └── DejaVuSans.ttf
    ├── input/
    │   └── <album>.json
    ├── output/
    │   └── <Album>/
    │       ├── <track video>.mp4
    │       ├── <track>.lrc
    │       └── <track>.jpg          # generated here
    └── scripts/
        └── generate_thumbnail.py

The album background is the static visual composition. It should already
contain the album artwork and Iggy4King logo. The script adds only the
per-track song cover, song title, and album metadata.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = PROJECT_ROOT / "input"
OUTPUT_DIR = PROJECT_ROOT / "output"
ASSETS_DIR = PROJECT_ROOT / "assets"
FONTS_DIR = PROJECT_ROOT / "fonts"

CANVAS_SIZE = (1920, 1080)
SONG_COVER_SIZE = (940, 940)
SONG_COVER_POSITION = (70, 70)

# Typography/layout based on the supplied 1920x1080 mockup.
TITLE_FONT_FILE = FONTS_DIR / "agaramondpro_semibold.otf"
TITLE_FONT_SIZE = 140
TITLE_MAX_LINES = 4
TITLE_BOX = (1090, 250, 1810, 610)  # x1, y1, x2, y2
TITLE_LINE_SPACING = 5
TITLE_SHADOW_OFFSET = 3

META_REGULAR_FONT_FILE = FONTS_DIR / "DejaVuSans.ttf"
META_BOLD_FONT_FILE = FONTS_DIR / "agaramondpro_semibold.otf"
META_LABEL_SIZE = 28
META_TITLE_SIZE = 44
META_YEAR_SIZE = 26
META_POSITION = (1090, 940)
META_SECOND_LINE_GAP = 3
META_LABEL = "Album: "
META_COLOR = (255, 255, 255, 255)  # White
#META_COLOR = (20, 20, 20, 255)    # Black

SUPPORTED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


class ThumbnailError(RuntimeError):
    """Raised when thumbnail generation cannot safely continue."""


def print_warning(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def load_album(album_file: str) -> tuple[list[dict[str, Any]], Path]:
    """Load album JSON and return tracks plus resolved JSON path."""
    path = Path(album_file)

    if not path.is_absolute():
        path = INPUT_DIR / path

    if not path.exists():
        raise ThumbnailError(f"Album JSON not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ThumbnailError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ThumbnailError(f"Album JSON must contain an object: {path}")

    tracks = data.get("data")
    if not isinstance(tracks, list):
        raise ThumbnailError(f"Missing or invalid 'data' array in {path}")

    normalized_tracks = [track for track in tracks if isinstance(track, dict)]
    if not normalized_tracks:
        raise ThumbnailError(f"No track records found in {path}")

    return normalized_tracks, path


def get_album_name(tracks: list[dict[str, Any]]) -> str:
    """Read album name from the first track and validate consistency."""
    album_names = {
        str(track.get("Album", "")).strip()
        for track in tracks
        if str(track.get("Album", "")).strip()
    }

    if not album_names:
        raise ThumbnailError("No 'Album' value found in album JSON.")

    if len(album_names) > 1:
        raise ThumbnailError(
            "Multiple album names found in the JSON: "
            + ", ".join(sorted(album_names))
        )

    return next(iter(album_names))


def parse_track_number(value: Any) -> int:
    """Convert zero-padded track number to an integer."""
    match = re.search(r"\d+", str(value))
    if not match:
        raise ThumbnailError(f"Invalid track number: {value!r}")
    return int(match.group(0))


def format_track_stem(track: dict[str, Any]) -> str:
    """Return the project's standard video/LRC filename stem."""
    number = parse_track_number(track.get("Track Number", ""))
    title = str(track.get("Title", "")).strip()
    if not title:
        raise ThumbnailError(f"Track {number:02d} has no Title.")
    return f"{number:02d} - {title}"


def album_asset_path(album_name: str) -> Path:
    """Map album name to assets/<Album>_album.png using underscores for spaces."""
    stem = re.sub(r"\s+", "_", album_name.strip())
    return ASSETS_DIR / f"{stem}_album.png"


def resolve_picture_path(track: dict[str, Any], album_name: str) -> Path:
    """Resolve the song-cover image from the project's temp cover directory.

    album.py stores generated song covers at:
        temp/<Album>/covers/<NN> - <Title>.jpg

    That location is the primary source. The JSON Picture field is retained
    as an optional fallback for compatibility with older metadata.
    """
    stem = format_track_stem(track)
    temp_covers_dir = PROJECT_ROOT / "temp" / album_name / "covers"

    candidates: list[Path] = [
        temp_covers_dir / f"{stem}.jpg",
        temp_covers_dir / f"{stem}.jpeg",
        temp_covers_dir / f"{stem}.png",
        temp_covers_dir / f"{stem}.webp",
    ]

    picture_value = str(track.get("Picture", "")).strip()
    if picture_value:
        picture_path = Path(picture_value).expanduser()
        if picture_path.is_absolute():
            candidates.append(picture_path)
        else:
            candidates.extend(
                [
                    PROJECT_ROOT / picture_path,
                    INPUT_DIR / picture_path,
                    OUTPUT_DIR / picture_path,
                    ASSETS_DIR / picture_path,
                ]
            )

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate

    searched = "\n    ".join(str(path) for path in candidates)
    raise ThumbnailError(
        f"Song cover art not found for '{stem}'.\n"
        f"Expected album.py cover location:\n  {temp_covers_dir / (stem + '.jpg')}\n\n"
        f"Checked:\n    {searched}"
    )


def extract_year(track: dict[str, Any]) -> str:
    """Get a 4-digit year from Release Date, falling back to Date."""
    for field in ("Release Date", "Date"):
        value = str(track.get(field, "")).strip()
        match = re.search(r"\b(\d{4})\b", value)
        if match:
            return match.group(1)
    return ""


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    """Load a project-local font with a clear error."""
    if not path.is_file():
        raise ThumbnailError(f"Required font not found: {path}")
    try:
        return ImageFont.truetype(str(path), size=size)
    except OSError as exc:
        raise ThumbnailError(f"Could not load font {path}: {exc}") from exc


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def wrap_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Wrap title by words, preserving whole words whenever possible."""
    words = title.split()
    if not words:
        return []

    lines: list[str] = []
    current = ""

    for word in words:
        proposed = word if not current else f"{current} {word}"
        if text_width(draw, proposed, font) <= max_width:
            current = proposed
            continue

        if current:
            lines.append(current)
            current = word
        else:
            # A single word can exceed the box. Keep it intact so the fitting
            # loop can reduce the font size rather than splitting the word.
            lines.append(word)
            current = ""

    if current:
        lines.append(current)

    return lines


def fit_title_font(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_width: int,
    max_height: int,
    initial_size: int,
    max_lines: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Find the largest local Garamond font that fits the reserved title area."""
    size = initial_size

    while size >= 40:
        font = load_font(TITLE_FONT_FILE, size)
        lines = wrap_title(draw, title, font, max_width, max_lines)

        if len(lines) <= max_lines:
            line_bboxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
            if line_bboxes:
                line_height = max(bbox[3] - bbox[1] for bbox in line_bboxes)
            else:
                line_height = 0
            total_height = len(lines) * line_height + max(0, len(lines) - 1) * TITLE_LINE_SPACING
            if total_height <= max_height:
                return font, lines

        size -= 2

    raise ThumbnailError(
        f"Song title cannot fit into the reserved {max_lines}-line title area: {title!r}"
    )


def paste_cover(background: Image.Image, cover_path: Path) -> None:
    """Fit song cover to 885x885 and place it at the fixed mockup position."""
    try:
        cover = Image.open(cover_path).convert("RGBA")
    except OSError as exc:
        raise ThumbnailError(f"Could not open song cover {cover_path}: {exc}") from exc

    cover = cover.resize(SONG_COVER_SIZE, Image.Resampling.LANCZOS)
    background.alpha_composite(cover, dest=SONG_COVER_POSITION)


def draw_title(
    image: Image.Image,
    title: str,
) -> None:
    """Draw the song title in the reserved four-line area."""
    draw = ImageDraw.Draw(image)
    x1, y1, x2, y2 = TITLE_BOX
    max_width = x2 - x1
    max_height = y2 - y1

    font, lines = fit_title_font(
        draw,
        title,
        max_width=max_width,
        max_height=max_height,
        initial_size=TITLE_FONT_SIZE,
        max_lines=TITLE_MAX_LINES,
    )

    line_bboxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_height = max((bbox[3] - bbox[1] for bbox in line_bboxes), default=0)
    total_height = len(lines) * line_height + max(0, len(lines) - 1) * TITLE_LINE_SPACING
    y = y1 + max(0, (max_height - total_height) // 2)

    for line in lines:
        # Subtle dark shadow keeps white lettering readable over busy artwork.
        draw.text(
            (x1 + TITLE_SHADOW_OFFSET, y + TITLE_SHADOW_OFFSET),
            line,
            font=font,
            fill=(0, 0, 0, 150),
        )
        draw.text(
            (x1, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
        )
        y += line_height + TITLE_LINE_SPACING


def draw_album_metadata(
    image: Image.Image,
    album_name: str,
    year: str,
) -> None:
    """Draw dynamic album title and year into the reserved lower-right area."""
    draw = ImageDraw.Draw(image)
    x, y = META_POSITION

    regular = load_font(META_REGULAR_FONT_FILE, META_LABEL_SIZE)
    bold = load_font(META_BOLD_FONT_FILE, META_TITLE_SIZE)
    year_font = load_font(META_REGULAR_FONT_FILE, META_YEAR_SIZE)

    label = META_LABEL
    label_width = int(text_width(draw, label, regular))

    draw.text(
        (x, y),
        label,
        font=regular,
        fill=META_COLOR,
    )
    draw.text(
        (x + label_width, y - 5),
        album_name,
        font=bold,
        fill=META_COLOR,
    )

    if year:
        label_bbox = draw.textbbox((x, y), label, font=regular)
        title_bbox = draw.textbbox((x + label_width, y), album_name, font=bold)
        first_line_bottom = max(label_bbox[3], title_bbox[3])
        second_y = first_line_bottom + META_SECOND_LINE_GAP
        draw.text(
            (x, second_y),
            year,
            font=year_font,
            fill=META_COLOR,
        )


def validate_background(background_path: Path) -> None:
    """Ensure the required album background exists and is a usable image."""
    if not background_path.is_file():
        raise ThumbnailError(
            "Album thumbnail background not found.\n"
            f"Expected:\n  {background_path}\n\n"
            "Thumbnail generation aborted."
        )

    try:
        with Image.open(background_path) as image:
            if image.size != CANVAS_SIZE:
                raise ThumbnailError(
                    f"Album thumbnail background must be {CANVAS_SIZE[0]}x{CANVAS_SIZE[1]}, "
                    f"but is {image.size}: {background_path}"
                )
    except OSError as exc:
        raise ThumbnailError(
            f"Could not open album thumbnail background {background_path}: {exc}"
        ) from exc


def generate_thumbnail(
    track: dict[str, Any],
    album_name: str,
    background_path: Path,
    force: bool,
) -> Path:
    """Generate one thumbnail and return its output path."""
    stem = format_track_stem(track)
    output_album_dir = OUTPUT_DIR / album_name
    output_album_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_album_dir / f"{stem}.jpg"

    if output_path.exists() and not force:
        print(f"SKIPPED  : {output_path.name} (already exists)")
        return output_path

    cover_path = resolve_picture_path(track, album_name)
    title = str(track.get("Title", "")).strip()
    year = extract_year(track)

    with Image.open(background_path) as bg_source:
        image = bg_source.convert("RGBA")

    paste_cover(image, cover_path)
    draw_title(image, title)
    draw_album_metadata(image, album_name, year)

    # JPEG does not support alpha; flatten onto white only if the background
    # itself happens to contain transparency.
    flattened = Image.new("RGB", CANVAS_SIZE, (255, 255, 255))
    flattened.paste(image, mask=image.getchannel("A"))
    flattened.save(
        output_path,
        format="JPEG",
        quality=95,
        subsampling=0,
        optimize=True,
        progressive=True,
    )

    print(f"GENERATED: {output_path}")
    return output_path


def select_tracks(
    tracks: list[dict[str, Any]],
    requested_numbers: list[int],
) -> list[dict[str, Any]]:
    """Select tracks in album order."""
    sorted_tracks = sorted(
        tracks,
        key=lambda track: parse_track_number(track.get("Track Number", "")),
    )

    if not requested_numbers:
        return sorted_tracks

    requested = set(requested_numbers)
    selected = [
        track
        for track in sorted_tracks
        if parse_track_number(track.get("Track Number", "")) in requested
    ]

    found = {
        parse_track_number(track.get("Track Number", ""))
        for track in selected
    }
    missing = sorted(requested - found)
    if missing:
        raise ThumbnailError(
            "Requested track number(s) not found: "
            + ", ".join(f"{number:02d}" for number in missing)
        )

    return selected


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate YouTube thumbnails from album artwork and JSON metadata."
    )
    parser.add_argument(
        "album",
        help="Album JSON filename, e.g. 'Vođeni Duhom.json'",
    )
    parser.add_argument(
        "tracks",
        nargs="*",
        type=int,
        help="Optional track numbers. Omit to generate all tracks.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate thumbnails that already exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    try:
        tracks, album_json_path = load_album(args.album)
        album_name = get_album_name(tracks)
        selected_tracks = select_tracks(tracks, args.tracks)
        background_path = album_asset_path(album_name)

        # Validate all fixed resources before generating anything. In
        # particular, a missing album background aborts the complete run.
        validate_background(background_path)
        load_font(TITLE_FONT_FILE, TITLE_FONT_SIZE)
        load_font(META_REGULAR_FONT_FILE, META_LABEL_SIZE)
        load_font(META_BOLD_FONT_FILE, META_TITLE_SIZE)
        load_font(META_REGULAR_FONT_FILE, META_YEAR_SIZE)

        print("=" * 70)
        print("THUMBNAIL GENERATION")
        print("=" * 70)
        print(f"Album                 : {album_name}")
        print(f"Source JSON           : {album_json_path}")
        print(f"Tracks available      : {len(tracks)}")
        print(f"Tracks selected       : {len(selected_tracks)}")
        print(f"Canvas                : {CANVAS_SIZE[0]}x{CANVAS_SIZE[1]}")
        print(f"Album background      : {background_path}")
        print(f"Song cover size       : {SONG_COVER_SIZE[0]}x{SONG_COVER_SIZE[1]}")
        print(f"Force regeneration    : {'YES' if args.force else 'NO'}")
        print("=" * 70)
        print()

        generated = 0
        skipped = 0

        for track in selected_tracks:
            stem = format_track_stem(track)
            print("=" * 70)
            print(stem)
            print("=" * 70)

            output_path = OUTPUT_DIR / album_name / f"{stem}.jpg"
            was_existing = output_path.exists() and not args.force
            generate_thumbnail(
                track=track,
                album_name=album_name,
                background_path=background_path,
                force=args.force,
            )

            if was_existing:
                skipped += 1
            else:
                generated += 1
            print()

        print("=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"Generated             : {generated}")
        print(f"Skipped               : {skipped}")
        print(f"Output directory      : {OUTPUT_DIR / album_name}")
        print("=" * 70)
        return 0

    except ThumbnailError as exc:
        print_warning(str(exc))
        return 1
    except KeyboardInterrupt:
        print_warning("Interrupted by user.")
        return 130
    except Exception as exc:  # Defensive top-level error reporting.
        print_warning(f"Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
