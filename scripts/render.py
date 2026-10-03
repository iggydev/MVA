#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOBS_FILE = PROJECT_ROOT / "temp" / "jobs.json"
OUTPUT_DIR = PROJECT_ROOT / "output"
TEMP_DIR = PROJECT_ROOT / "temp"
LOGO_OUTRO = PROJECT_ROOT / "assets" / "logo.mov"
# ---------------------------------------------------------------------------
# Local font
# ---------------------------------------------------------------------------
FONT_DIR = PROJECT_ROOT / "fonts"
FONT_REGULAR = FONT_DIR / "agaramondpro_regular.otf"
FONT_ITALIC = FONT_DIR / "agaramondpro_italic.otf"
FONT_SEMIBOLD = FONT_DIR / "agaramondpro_semibold.otf"
FONT_SEMIBOLD_ITALIC = FONT_DIR / "agaramondpro_semibolditalic.otf"
FONT_SANS = FONT_DIR / "DejaVuSans.ttf"
# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 30
# ---------------------------------------------------------------------------
# Visual layout
# ---------------------------------------------------------------------------
COVER_SIZE = 825
COVER_X = 55
COVER_Y = 95
LYRIC_X = 1400
LYRIC_Y = 400
LYRIC_NEXT_OFFSET = 80
LYRIC_NEXT_NEXT_OFFSET = 145
# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------
LYRIC_FONT_SIZE = 72
LYRIC_SECONDARY_SIZE = 44
ARTIST_FONT_SIZE = 20
TITLE_FONT_SIZE = 39
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_name(text):
    text = str(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
def seconds_to_ass_time(seconds):
    """
    ASS timestamp:
        H:MM:SS.cc
    """
    total_cs = int(round(seconds * 100))
    hours = total_cs // 360000
    total_cs %= 360000
    minutes = total_cs // 6000
    total_cs %= 6000
    secs = total_cs // 100
    centis = total_cs % 100
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"
def ffmpeg_escape_path(path):
    """
    Escape a filesystem path for use inside an FFmpeg filter argument.
    """
    value = str(path).replace("\\", "/")
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value
def ffmpeg_escape_textfile_path(path):
    """
    Path escaping specifically for drawtext=textfile=.
    """
    value = str(path).replace("\\", "/")
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value
def write_text_file(path, text):
    """
    Write a UTF-8 text file used by drawtext.
    """
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    with path.open(
        "w",
        encoding="utf-8",
    ) as f:
        f.write(str(text))
    return path
# ---------------------------------------------------------------------------
# ASS text escaping
# ---------------------------------------------------------------------------
def ass_escape(text):
    """
    Escape lyric text for ASS.
    Lyric text itself does not contain ASS override blocks.
    """
    text = str(text)
    text = text.replace("\\", r"\\")
    text = text.replace("{", r"\{")
    text = text.replace("}", r"\}")
    return text
# ---------------------------------------------------------------------------
# Font
# ---------------------------------------------------------------------------
def validate_font():
    """
    Use the project-local Adobe Garamond Pro font family.
    Regular and Semibold faces are available to libass through
    the project fonts directory.
    """
    required_fonts = [
        FONT_REGULAR,
        FONT_ITALIC,
        FONT_SEMIBOLD,
        FONT_SEMIBOLD_ITALIC,
        FONT_SANS,
    ]
    for font_file in required_fonts:
        if not font_file.exists():
            raise FileNotFoundError(
                f"Font not found: {font_file}"
            )
    print("Font family : Adobe Garamond Pro")
    print(f"Font folder : {FONT_DIR}")
    print(f"Regular     : {FONT_REGULAR}")
    print(f"Italic      : {FONT_ITALIC}")
    print(f"Semibold    : {FONT_SEMIBOLD}")
    print(f"Semibold Italic: {FONT_SEMIBOLD_ITALIC}")
    print(f"Sans: {FONT_SANS}")
    return {
        "family": "Adobe Garamond Pro",
        "file": FONT_REGULAR,          # <-- REQUIRED by drawtext
        "regular": FONT_REGULAR,
        "semibold": FONT_SEMIBOLD,
        "italic": FONT_ITALIC,
        "semibold_italic": FONT_SEMIBOLD_ITALIC,
        "sans": FONT_SANS,
        "directory": FONT_DIR,
    }
# ---------------------------------------------------------------------------
# LRC
# ---------------------------------------------------------------------------
def parse_lrc(lrc_file):
    """
    Read an LRC file.
    Returns:
        [
            {
                "start": 13.68,
                "text": "A quiet morning, I open my eyes"
            },
            ...
        ]
    """
    timestamp_re = re.compile(
        r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)"
    )
    lyrics = []
    with lrc_file.open(
        "r",
        encoding="utf-8",
    ) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            match = timestamp_re.match(line)
            if not match:
                continue
            minutes = int(match.group(1))
            seconds = float(match.group(2))
            text = match.group(3).strip()
            if not text:
                continue
            start = minutes * 60 + seconds
            lyrics.append({
                "start": start,
                "text": text,
            })
    lyrics.sort(
        key=lambda item: item["start"]
    )
    return lyrics
# ---------------------------------------------------------------------------
# Lyrics sizing
# ---------------------------------------------------------------------------
def lyric_font_sizes(lines):
    if not lines:
        return 72, 44
    longest = max(
        len(line["text"])
        for line in lines
    )
    if longest <= 34:
        return 72, 44
    if longest <= 42:
        return 65, 40
    if longest <= 50:
        return 61, 38
    if longest <= 58:
        return 57, 35
    if longest <= 68:
        return 53, 32
    return 49, 29
# ---------------------------------------------------------------------------
# ASS generation
# ---------------------------------------------------------------------------
def create_ass_file(
    lrc_file,
    ass_file,
    font,
):
    """
    Convert LRC into a cinematic three-line lyric presentation.
    Three separate ASS styles are used:
        LyricCurrent
        LyricNext
        LyricNextNext
    At lyric N:
        current    = lyric N
        next       = lyric N+1
        next-next  = lyric N+2
    IMPORTANT:
    All three lines use the SAME timestamp window.
    Example:
        13.68 -> 17.20
        lyric N       = CURRENT
        lyric N+1     = NEXT
        lyric N+2     = NEXT-NEXT
    At 17.20 the entire stack advances:
        lyric N+1     = CURRENT
        lyric N+2     = NEXT
        lyric N+3     = NEXT-NEXT
    """
    lyrics = parse_lrc(lrc_file)
    if not lyrics:
        raise RuntimeError(
            f"No timestamped lyrics found in {lrc_file}"
        )
    lines = []
    # -----------------------------------------------------------------------
    # ASS header
    # -----------------------------------------------------------------------
    lines.extend([
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        # -------------------------------------------------------------------
        # Current lyric — Semibold
        # -------------------------------------------------------------------
        (
            f"Style: LyricCurrent,{font['family']},60,"
            "&H00FFFFFF,&H00FFFFFF,&H70000000,&H1A000000,"
            "1,0,0,0,100,100,0,0,1,1,2,5,0,0,0,1"
        ),
        # -------------------------------------------------------------------
        # Next lyric — Regular
        # -------------------------------------------------------------------
        (
            f"Style: LyricNext,{font['family']},36,"
            "&H40FFFFFF,&H40FFFFFF,&H80000000,&H80000000,"
            "0,0,0,0,100,100,0,0,1,1,1,5,0,0,0,1"
        ),
        # -------------------------------------------------------------------
        # Next-next lyric — Regular
        # -------------------------------------------------------------------
        (
            f"Style: LyricNextNext,{font['family']},28,"
            "&H70FFFFFF,&H70FFFFFF,&HC0000000,&HC0000000,"
            "0,0,0,0,100,100,0,0,1,1,1,5,0,0,0,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, "
        "MarginR, MarginV, Effect, Text",
    ])
    # -----------------------------------------------------------------------
    # THREE-LINE PREVIEW STACK
    #
    # Every stack gets ONE common timestamp window:
    #
    #     start = lyric[index].start
    #     end   = lyric[index + 1].start
    #
    # Within that window:
    #
    #     lyric[index]     = CURRENT
    #     lyric[index + 1] = NEXT
    #     lyric[index + 2] = NEXT-NEXT
    #
    # When the next timestamp arrives, the whole stack advances.
    # -----------------------------------------------------------------------
    for index, lyric in enumerate(lyrics):
        visible = lyrics[index:index + 3]
        current_size, secondary_size = lyric_font_sizes(
            visible
        )
        # ================================================================
        # COMMON TIMING FOR THE ENTIRE THREE-LINE STACK
        # ================================================================
        stack_start = lyrics[index]["start"]
        if index + 1 < len(lyrics):
            stack_end = lyrics[index + 1]["start"]
        else:
            stack_end = stack_start + 4.0
        if stack_end <= stack_start:
            stack_end = stack_start + 0.10
        # ================================================================
        # CURRENT
        #
        # lyric[index]
        #
        # Same timing as NEXT and NEXT-NEXT.
        # ================================================================
        current_prefix = (
            rf"{{\an5\pos({LYRIC_X},{LYRIC_Y})"
            rf"\fs{current_size}"
            r"\fad(220,240)}"
        )
        lines.append(
            "Dialogue: 0,"
            f"{seconds_to_ass_time(stack_start)},"
            f"{seconds_to_ass_time(stack_end)},"
            "LyricCurrent,,0,0,0,,"
            f"{current_prefix}"
            f"{ass_escape(lyrics[index]['text'])}"
        )
        # ================================================================
        # NEXT
        #
        # lyric[index + 1]
        #
        # IMPORTANT:
        # Uses the SAME start/end as CURRENT.
        # ================================================================
        if index + 1 < len(lyrics):
            next_prefix = (
                rf"{{\an5\pos({LYRIC_X},{LYRIC_Y + LYRIC_NEXT_OFFSET})"
                rf"\fs{secondary_size}"
                r"\fad(220,240)}"
            )
            lines.append(
                "Dialogue: 0,"
                f"{seconds_to_ass_time(stack_start)},"
                f"{seconds_to_ass_time(stack_end)},"
                "LyricNext,,0,0,0,,"
                f"{next_prefix}"
                f"{ass_escape(lyrics[index + 1]['text'])}"
            )
        # ================================================================
        # NEXT-NEXT
        #
        # lyric[index + 2]
        #
        # IMPORTANT:
        # Uses the SAME start/end as CURRENT and NEXT.
        # ================================================================
        if index + 2 < len(lyrics):
            next_next_prefix = (
                rf"{{\an5\pos({LYRIC_X},{LYRIC_Y + LYRIC_NEXT_NEXT_OFFSET})"
                rf"\fs{secondary_size}"
                r"\fad(220,240)}"
            )
            lines.append(
                "Dialogue: 0,"
                f"{seconds_to_ass_time(stack_start)},"
                f"{seconds_to_ass_time(stack_end)},"
                "LyricNextNext,,0,0,0,,"
                f"{next_next_prefix}"
                f"{ass_escape(lyrics[index + 2]['text'])}"
            )
    # -----------------------------------------------------------------------
    # Write ASS
    # -----------------------------------------------------------------------
    ass_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    with ass_file.open(
        "w",
        encoding="utf-8",
    ) as f:
        f.write(
            "\n".join(lines)
        )
        f.write("\n")
    print(
        f"ASS lyrics   : {ass_file}"
    )
    print(
        f"Lyrics events: {len(lyrics)} × 3-line layout"
    )
    return ass_file
# ---------------------------------------------------------------------------
# Cover / LRC discovery
# ---------------------------------------------------------------------------
def find_cover(job):
    track_number = int(
        job["track_number"]
    )
    title = job["title"]
    album = job["album"]
    cover_dir = (
        TEMP_DIR /
        album /
        "covers"
    )
    expected = (
        cover_dir /
        f"{track_number:02d} - {title}.jpg"
    )
    if expected.exists():
        return expected
    pattern = (
        f"{track_number:02d}*"
        f"{safe_name(title)}*.jpg"
    )
    candidates = list(
        cover_dir.glob(pattern)
    )
    if candidates:
        return candidates[0]
    candidates = list(
        cover_dir.glob(
            f"*{safe_name(title)}*.jpg"
        )
    )
    if candidates:
        return candidates[0]
    raise FileNotFoundError(
        f"Cover not found for {title} "
        f"in {cover_dir}"
    )
def find_lrc(job):
    album = job["album"]
    track_number = int(
        job["track_number"]
    )
    title = job["title"]
    lrc_file = (
        OUTPUT_DIR /
        album /
        f"{track_number:02d} - {title}.lrc"
    )
    if lrc_file.exists():
        return lrc_file
    raise FileNotFoundError(
        f"LRC not found: {lrc_file}"
    )
# ---------------------------------------------------------------------------
# Audio duration
# ---------------------------------------------------------------------------
def get_audio_duration(audio):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
    )
    return float(
        result.stdout.strip()
    )
def get_video_duration(video):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
    )
    return float(
        result.stdout.strip()
    )
# ---------------------------------------------------------------------------
# FFmpeg filter graph
# ---------------------------------------------------------------------------
def build_filter_complex(
    ass_file,
    artist_file,
    title_file,
    duration,
    font,
    outro_duration,
):
    """
    Complete polished lyric-video pipeline.
    Cover shadows are removed completely.
    The progress bar uses overlay sources with eval=frame.
    This avoids the unsupported drawbox eval option in FFmpeg 9.
    """
    ass_path = ffmpeg_escape_path(
        ass_file
    )
    artist_path = ffmpeg_escape_textfile_path(
        artist_file
    )
    title_path = ffmpeg_escape_textfile_path(
        title_file
    )
    fonts_dir = ffmpeg_escape_path(
        font["directory"]
    )
    font_path = ffmpeg_escape_path(
        font["file"]
    )
    font_sans = ffmpeg_escape_path(
        font["sans"]
    )
    logo_fade_start = max(
        0,
        duration - outro_duration
    )
    progress_width = VIDEO_WIDTH
    # -----------------------------------------------------------------------
    # Progress expressions
    # -----------------------------------------------------------------------
    progress_position = (
        f"-{progress_width}+"
        f"{progress_width}*"
        f"min(1,max(0,t/{duration}))"
    )
    dot_position = (
        f"{progress_width}*"
        f"min(1,max(0,t/{duration}))"
        "-3"
    )
    # -----------------------------------------------------------------------
    # Main filtergraph
    # -----------------------------------------------------------------------
    filters = (
        # ===================================================================
        # Background.
        # ===================================================================
        # Album background.
        "[0:v]"
        "format=gbrp,"
        "scale=1920:1080,"
        "setsar=1,"
        "format=gbrp"
        "[bg];"
        # ===================================================================
        # Foreground cover.
        #
        # 640x640 square.
        # 28px rounded corners.
        #
        # NO SHADOW LAYERS.
        # ===================================================================
        "[1:v]"
        "format=gbrp,"
        f"scale={COVER_SIZE}:{COVER_SIZE}:"
        "force_original_aspect_ratio=increase,"
        f"crop={COVER_SIZE}:{COVER_SIZE},"
        "format=gbrp"
        "[cover];"
        # ===================================================================
        # Cover directly over background.
        # ===================================================================
        "[bg]"
        "[cover]"
        "overlay="
        f"x={COVER_X}:"
        f"y={COVER_Y}:"
        "eof_action=repeat:"
        "format=gbrp"
        "[base];"
        # ===================================================================
        # Overall readability.
        # ===================================================================
        "[base]"
        "drawbox="
        "x=0:y=0:w=1920:h=1080:"
        "color=black@0.10:"
        "t=fill,"
        "format=gbrp"
        "[dark];"
        # ===================================================================
        # Progress track.
        # ===================================================================
        "[dark]"
        "drawbox="
        "x=0:y=42:"
        f"w={progress_width}:"
        "h=2:"
        "color=white@0.22:"
        "t=fill"
        "[progress_bg];"
        # ===================================================================
        # Progress bar source.
        # ===================================================================
        f"color=c=white@0.82:s={progress_width}x2:"
        f"r={FPS}:d={duration},"
        "format=rgba"
        "[progress_bar];"
        "[progress_bg]"
        "[progress_bar]"
        "overlay="
        f"x='{progress_position}':"
        "y=42:"
        "eval=frame:"
        "eof_action=pass:"
        "format=gbrp"
        "[progress];"
        # ===================================================================
        # Progress dot source.
        # ===================================================================
        f"color=c=white@0.95:s=6x12:"
        f"r={FPS}:d={duration},"
        "format=rgba"
        "[progress_dot_src];"
        "[progress]"
        "[progress_dot_src]"
        "overlay="
        f"x='{dot_position}':"
        "y=37:"
        "eval=frame:"
        "eof_action=pass:"
        "format=gbrp"
        "[progress_dot];"
        # ===================================================================
        # Artist.
        # ===================================================================
        "[progress_dot]"
        "drawtext="
        f"textfile='{artist_path}':"
        f"fontfile='{font_sans}':"
        "fontcolor=white@0.82:"
        f"fontsize={ARTIST_FONT_SIZE}:"
        "x=w-text_w-335:"
        "y=899:"
        "shadowcolor=black@0.85:"
        "shadowx=2:"
        "shadowy=2"
        "[artist];"
        # ===================================================================
        # Title.
        # ===================================================================
        "[artist]"
        "drawtext="
        f"textfile='{title_path}':"
        f"fontfile='{font_path}':"
        "fontcolor=white@0.96:"
        f"fontsize={TITLE_FONT_SIZE}:"
        "x=w-text_w-335:"
        "y=847:"
        "shadowcolor=black@0.90:"
        "shadowx=2:"
        "shadowy=2"
        "[title_text];"
        # ===================================================================
        # Audio waveform.
        # ===================================================================
        "[2:a]"
        "showwaves="
        "s=1920x60:"
        "mode=p2p:"
        "rate=15:"
        "colors=white@0.77:"
        "scale=sqrt:"
        "draw=full,"
        "format=rgba"
        "[wave];"
        # ===================================================================
        # Waveform.
        # ===================================================================
        "[title_text]"
        "[wave]"
        "overlay="
        "x=0:"
        "y=H-105:"
        "eof_action=pass:"
        "format=gbrp"
        "[visual];"
        # ===================================================================
        # ASS lyrics.
        #
        # DO NOT MODIFY THIS SECTION.
        # ===================================================================
        "[visual]"
        f"ass=filename='{ass_path}':"
        f"fontsdir='{fonts_dir}':"
        "original_size=1920x1080:"
        "shaping=1,"
        "format=gbrp"
        "[song_video];"
        # ===================================================================
        # Fade the COMPLETE song video to pure black.
        #
        # The fade lasts exactly as long as the logo.
        #
        # Example for the 30-second test:
        #
        #   Song duration : 30.000s
        #   Logo duration :  3.413s
        #   Black fade    : 26.587s -> 30.000s
        #   Logo          : 30.000s -> 33.413s
        #
        # Because this fade is applied AFTER the background and foreground
        # have been composited, the entire visual scene becomes black.
        # ===================================================================
        "[song_video]"
        "fade="
        "t=out:"
        f"st={logo_fade_start}:"
        f"d={outro_duration}"
        "[black_video];"
        # ===================================================================
        # Logo outro.
        #
        # The logo starts exactly when the song ends.
        # ===================================================================
        "[3:v]"
        "format=gbrap,"
        "scale=1920:1080,"
        "setsar=1,"
        f"trim=duration={outro_duration},"
        "setpts=PTS-STARTPTS+"
        f"{duration}/TB"
        "[logo];"
        # ===================================================================
        # Overlay transparent logo over the pure-black video.
        # ===================================================================
        "[black_video]"
        "[logo]"
        "overlay="
        "x=0:"
        "y=0:"
        "eof_action=pass:"
        "format=gbrp"
        "[logo_composite];"
        "[logo_composite]"
        "format=yuv444p,"
        "colorspace="
        "all=bt709:"
        "iall=bt601-6-625:"
        "irange=tv:"
        "range=tv:"
        "format=yuv420p,"
        "setsar=1,"
        "setparams="
        "range=tv:"
        "color_primaries=bt709:"
        "color_trc=bt709:"
        "colorspace=bt709"
        "[v];"
        # ===================================================================
        # Song audio.
        # ===================================================================
        "[2:a]"
        f"atrim=duration={duration},"
        "asetpts=PTS-STARTPTS"
        "[song_audio];"
        # ===================================================================
        # Logo audio.
        # ===================================================================
        "[3:a]"
        "asetpts=PTS-STARTPTS"
        "[logo_audio];"
        # ===================================================================
        # Song audio followed by logo audio.
        # ===================================================================
        "[song_audio]"
        "[logo_audio]"
        "concat="
        "n=2:"
        "v=0:"
        "a=1"
        "[a]"
    )
    return filters
# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render(
    job,
    background,
    duration=None,
):
    """
    Render the polished lyric video.
    """
    title = job["title"]
    artist = job.get(
        "artist",
        "Iggy4King Music",
    )
    album = job["album"]
    track_number = int(job["track_number"])
    title = job["title"]
    audio = Path(
        job["audio_path"]
    )
    cover = find_cover(job)
    lrc_file = find_lrc(job)
    # -----------------------------------------------------------------------
    # Background png
    # -----------------------------------------------------------------------
    background = Path(background)
    if not background.exists():
        raise FileNotFoundError(
            f"Background image not found: {background}"
        )
    if not background.is_file():
        raise FileNotFoundError(
            f"Background path is not a file: {background}"
        )
    # -----------------------------------------------------------------------
    # Duration
    # -----------------------------------------------------------------------
    if duration is None:
        duration = get_audio_duration(
            audio
        )
    if not LOGO_OUTRO.exists():
        raise FileNotFoundError(
            f"Logo outro not found: {LOGO_OUTRO}"
        )
    outro_duration = get_video_duration(
        LOGO_OUTRO
    )
    # -----------------------------------------------------------------------
    # Font
    # -----------------------------------------------------------------------
    font = validate_font()
    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------
    preview_dir = (
        TEMP_DIR /
        "preview"
    )
    preview_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    album_output_dir = OUTPUT_DIR / album
    album_output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )
    output_file = (
        album_output_dir /
        f"{track_number:02d} - {title}.mp4"
    )
    # -----------------------------------------------------------------------
    # ASS
    # -----------------------------------------------------------------------
    ass_dir = (
        TEMP_DIR /
        "ass"
    )
    ass_file = (
        ass_dir /
        f"{int(job['track_number']):02d}-"
        f"{safe_name(title)}.ass"
    )
    create_ass_file(
        lrc_file,
        ass_file,
        font,
    )
    # -----------------------------------------------------------------------
    # drawtext textfiles
    # -----------------------------------------------------------------------
    text_dir = (
        TEMP_DIR /
        "text"
    )
    artist_file = (
        text_dir /
        f"{int(job['track_number']):02d}-artist.txt"
    )
    title_file = (
        text_dir /
        f"{int(job['track_number']):02d}-title.txt"
    )
    write_text_file(
        artist_file,
        f"{album}",
    )
    write_text_file(
        title_file,
        title,
    )
    # -----------------------------------------------------------------------
    # Filter graph
    # -----------------------------------------------------------------------
    filter_complex = build_filter_complex(
        ass_file=ass_file,
        artist_file=artist_file,
        title_file=title_file,
        duration=duration,
        font=font,
        outro_duration=outro_duration,
    )
    # -----------------------------------------------------------------------
    # FFmpeg command
    # -----------------------------------------------------------------------
    command = [
        "ffmpeg",
        "-y",
        # Background.
        "-loop", "1",
        "-framerate", str(FPS),
        "-i", str(background),
        "-loop", "1",
        "-framerate", str(FPS),
        "-i", str(cover),
        # Audio.
        "-i", str(audio),
        # Transparent logo outro.
        "-i", str(LOGO_OUTRO),
        # Filtergraph.
        "-filter_complex",
        filter_complex,
        # Streams.
        "-map",
        "[v]",
        "-map",
        "[a]",
        # Preview duration + logo outro.
        "-t",
        str(duration + outro_duration),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-x264-params",
        "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        # Audio.
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        # MP4.
        "-movflags",
        "+faststart+write_colr",
        str(output_file),
    ]
    # -----------------------------------------------------------------------
    # Display
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print("RENDERING POLISHED LYRIC VIDEO")
    print("=" * 70)
    print(
        f"Title    : {title}"
    )
    print(
        f"Artist   : {artist}"
    )
    print(
        f"Album    : {album}"
    )
    print(
        f"Duration : {duration:.2f}s"
    )
    print(
        f"Logo outro: {outro_duration:.3f}s"
    )
    print(
        f"Total duration: "
        f"{duration + outro_duration:.3f}s"
    )
    print(
        f"Font     : {font['family']}"
    )
    print(
        f"Font file: {font['file']}"
    )
    print(
        f"Audio    : {audio}"
    )
    print(
        f"Cover    : {cover}"
    )
    print(
        f"Background: {background}"
    )
    print(
        f"LRC      : {lrc_file}"
    )
    print(
        f"Logo     : {LOGO_OUTRO}"
    )
    print(
        f"ASS      : {ass_file}"
    )
    print(
        f"Output   : {output_file}"
    )
    print("=" * 70)
    print()
    print("Running FFmpeg:")
    print()
    print(
        " ".join(
            f'"{x}"'
            if " " in str(x)
            else str(x)
            for x in command
        )
    )
    print()
    # -----------------------------------------------------------------------
    # Execute
    # -----------------------------------------------------------------------
    try:
        subprocess.run(
            command,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print()
        print(
            f"FFmpeg failed with exit code "
            f"{exc.returncode}"
        )
        sys.exit(
            exc.returncode
        )
    # -----------------------------------------------------------------------
    # Complete
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print("RENDER COMPLETE")
    print("=" * 70)
    print(
        f"Video: {output_file}"
    )
    print("=" * 70)
    return output_file
# ---------------------------------------------------------------------------
# Bacground png
#
# python render.py --background ./assets/Led_by_the_Spirit.png
# python render.py --background ./assets/Led_by_the_Spirit.png 1 2 3
# ---------------------------------------------------------------------------
def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Render lyric videos."
    )
    parser.add_argument(
        "--background",
        required=True,
        type=Path,
        help="Background image to use for the render.",
    )
    parser.add_argument(
        "tracks",
        nargs="*",
        type=int,
        help="Optional track numbers to render.",
    )
    return parser.parse_args()
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_arguments()
    background = args.background
    # -----------------------------------------------------------------------
    # Validate background before doing anything else.
    # -----------------------------------------------------------------------
    if not background.exists():
        print(
            f"ERROR: Background image not found: "
            f"{background}"
        )
        sys.exit(1)
    if not background.is_file():
        print(
            f"ERROR: Background path is not a file: "
            f"{background}"
        )
        sys.exit(1)
    # -----------------------------------------------------------------------
    # Jobs file
    # -----------------------------------------------------------------------
    if not JOBS_FILE.exists():
        print(
            f"ERROR: {JOBS_FILE} "
            f"does not exist"
        )
        sys.exit(1)
    with JOBS_FILE.open(
        "r",
        encoding="utf-8",
    ) as f:
        jobs = json.load(f)
    if not jobs:
        print(
            "ERROR: No jobs found"
        )
        sys.exit(1)
    # -----------------------------------------------------------------------
    # Command-line track selection
    #
    # IMPORTANT:
    # jobs.json contains only actual jobs. It may intentionally have gaps
    # in track numbers (for example 1, 2, 3, 4, 11) while the unfinished
    # tracks are not present yet. Track selection must therefore use the
    # job's real track_number instead of treating the JSON list position as
    # the album track number.
    # -----------------------------------------------------------------------
    jobs_by_track = {}
    for job in jobs:
        try:
            number = int(job["track_number"])
        except (KeyError, TypeError, ValueError):
            print(
                f"ERROR: Invalid job without a valid track_number: {job!r}"
            )
            sys.exit(1)

        if number in jobs_by_track:
            print(
                f"ERROR: Duplicate track number in jobs.json: {number}"
            )
            sys.exit(1)

        jobs_by_track[number] = job

    available_tracks = sorted(jobs_by_track)

    if args.tracks:
        selected_tracks = []
        for track_number in args.tracks:
            if track_number not in jobs_by_track:
                print(
                    f"ERROR: Track {track_number} is not present in jobs.json."
                )
                print(
                    "Available track numbers: "
                    f"{', '.join(map(str, available_tracks))}"
                )
                sys.exit(1)
            selected_tracks.append(track_number)
    else:
        # No track numbers: render every actual job, in track-number order.
        selected_tracks = available_tracks

    # -----------------------------------------------------------------------
    # Display batch plan
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print("LYRIC VIDEO BATCH RENDER")
    print("=" * 70)
    print(
        f"Jobs available        : {len(jobs)}"
    )
    print(
        f"Track numbers available: "
        f"{', '.join(map(str, available_tracks))}"
    )
    print(
        f"Tracks to render      : "
        f"{', '.join(map(str, selected_tracks))}"
    )
    print(
        f"Background            : {background}"
    )
    print("=" * 70)
    print()
    # -----------------------------------------------------------------------
    # Render selected tracks
    # -----------------------------------------------------------------------
    completed = []
    failed = []
    for track_number in selected_tracks:
        job = jobs_by_track[track_number]
        title = job["title"]
        album = job["album"]
        print()
        print("#" * 70)
        print(
            f"TRACK {track_number}"
        )
        print(
            f"{album} - {title}"
        )
        print("#" * 70)
        print()
        try:
            output_file = render(
                job,
                background,
            )
            completed.append(
                output_file
            )
        except Exception as exc:
            print()
            print(
                "=" * 70
            )
            print(
                f"RENDER FAILED: "
                f"{track_number:02d} - {title}"
            )
            print(
                f"Reason: {exc}"
            )
            print(
                "=" * 70
            )
            failed.append(
                (
                    track_number,
                    title,
                    exc,
                )
            )
    # -----------------------------------------------------------------------
    # Final batch summary
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print("BATCH RENDER COMPLETE")
    print("=" * 70)
    print(
        f"Completed : {len(completed)}"
    )
    print(
        f"Failed    : {len(failed)}"
    )
    if failed:
        print()
        print("FAILED TRACKS:")
        for track_number, title, exc in failed:
            print(
                f"  {track_number:02d} - "
                f"{title}: {exc}"
            )
    print("=" * 70)
    if failed:
        sys.exit(1)
if __name__ == "__main__":
    main()
