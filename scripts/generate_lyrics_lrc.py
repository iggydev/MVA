#!/usr/bin/env python3
"""
Align canonical lyrics onto Whisper word timings and write synced LRC.

Primary matcher is the original line-level greedy search — it already
scores perfectly on most tracks. Around it:

  - skip instrumental / intro hallucination clusters
  - never stretch one lyric line across a silence
  - interpolate leftover lines instead of dropping them
  - ♪ cues on intro / solo / interlude so lyrics do not sit on screen
  - whole-song global alignment as a fallback when greedy leaves holes
"""

from __future__ import annotations

import json
import re
import sys
import traceback
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path


def find_project_root():
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "temp" / "jobs.json").exists():
            return candidate
    return here.parent


PROJECT_ROOT = find_project_root()
JOBS_FILE = PROJECT_ROOT / "temp" / "jobs.json"
WHISPER_DIR = PROJECT_ROOT / "temp" / "whisper"
OUTPUT_DIR = PROJECT_ROOT / "output"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# ♪ at [00:00.00] when the first vocal starts later than this (seconds).
# Many players show line 1 from t=0 until its timestamp; this clears the intro.
INTRO_MUSIC_GAP = 1.0

# ♪ between two vocal lines when the silence after a line ends is at least
# this long (seconds). Short breaths stay as lyrics; solos / interludes get ♪.
SOLO_MUSIC_GAP = 5.0

# Whisper silence (seconds) treated as an instrumental while aligning.
# Independent of the LRC ♪ gaps above.
INSTRUMENTAL_GAP = 1.75

#MUSIC_SYMBOL = "♪"
MUSIC_SYMBOL = "♫"

SEARCH_WINDOW = 35
FALLBACK_RATIO = 0.85


def normalize_word(text):
    """Normalize a word for fuzzy lyric matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = text.replace("’", "'")
    text = text.replace("`", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = re.sub(r"[^a-z0-9']+", "", text)
    return text


def safe_name(text):
    text = str(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def parse_timestamp(value):
    """Convert Whisper timestamp like 00:01:23,450 to seconds."""
    h, m, s = value.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def in_bounds(seq, index):
    return seq is not None and 0 <= index < len(seq)


@lru_cache(maxsize=20000)
def similarity(a, b):
    """Simple word similarity. Exact matches are strongest."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if a.replace("'", "") == b.replace("'", ""):
        return 0.95
    if len(a) >= 4 and len(b) >= 4:
        if a in b or b in a:
            return 0.80
    if abs(len(a) - len(b)) > max(3, min(len(a), len(b))):
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def is_artifact_text(text):
    raw = (text or "").strip()
    if not raw:
        return True
    t = raw.lower().replace("♪", "").replace("♫", "").strip()
    if t in {"♪", "♫", "...", "---"}:
        return True
    if re.fullmatch(r"[\[\(\{].*[\]\)\}]", t):
        inner = re.sub(r"[^a-z]+", "", t)
        if inner in {
            "music", "instrumental", "applause", "intro", "outro",
            "solo", "verse", "chorus", "bridge", "inaudible", "interlude",
        } or inner == "":
            return True
    return False


def extract_words(whisper_json):
    """Reconstruct Whisper words from fine-grained transcription pieces."""
    with whisper_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    pieces = []
    tokens = []

    for item in data.get("transcription", []):
        if not isinstance(item, dict):
            continue
        text = item.get("text", "")
        offsets = item.get("offsets") or {}
        if text and "from" in offsets and "to" in offsets:
            pieces.append({
                "text": text,
                "start": offsets["from"] / 1000.0,
                "end": offsets["to"] / 1000.0,
            })
        for tok in item.get("tokens") or []:
            if not isinstance(tok, dict):
                continue
            tok_text = tok.get("text", "")
            if not tok_text or tok_text.startswith("["):
                continue
            tok_off = tok.get("offsets") or {}
            if "from" not in tok_off or "to" not in tok_off:
                continue
            tokens.append({
                "start": tok_off["from"] / 1000.0,
                "end": tok_off["to"] / 1000.0,
                "p": float(tok.get("p", 0.7)),
            })

    words = []
    current = None

    for piece in pieces:
        text = piece["text"]
        if not text.strip():
            continue
        if re.fullmatch(r"[^\w]+", text, flags=re.UNICODE):
            if current is not None:
                current["text"] += text
                current["end"] = piece["end"]
            continue

        starts_new_word = text.startswith(" ")
        clean = text.strip()

        if current is None:
            current = {"text": clean, "start": piece["start"], "end": piece["end"]}
        elif starts_new_word:
            words.append(current)
            current = {"text": clean, "start": piece["start"], "end": piece["end"]}
        else:
            current["text"] += clean
            current["end"] = piece["end"]

    if current is not None:
        words.append(current)

    cleaned = []
    for word in words:
        if is_artifact_text(word["text"]):
            continue
        normalized = normalize_word(word["text"])
        if not normalized:
            continue
        if normalized in {"music", "music]", "[music"}:
            continue
        word["norm"] = normalized
        word["p"] = 0.7
        cleaned.append(word)

    if tokens and cleaned:
        for word in cleaned:
            overlapping = [
                t["p"] for t in tokens
                if t["end"] > word["start"] and t["start"] < word["end"]
            ]
            if overlapping:
                word["p"] = sum(overlapping) / len(overlapping)

    mark_clusters(cleaned)
    return cleaned


def mark_clusters(whisper_words):
    n = len(whisper_words)
    if n == 0:
        return
    bounds = []
    start = 0
    for i in range(1, n):
        gap = whisper_words[i]["start"] - whisper_words[i - 1]["end"]
        if gap >= INSTRUMENTAL_GAP:
            bounds.append((start, i - 1))
            start = i
    bounds.append((start, n - 1))

    for a, b in bounds:
        cluster = whisper_words[a:b + 1]
        if not cluster:
            continue
        count = b - a + 1
        duration = max(0.08, cluster[-1]["end"] - cluster[0]["start"])
        rate = count / duration
        avg_p = sum(w.get("p", 0.7) for w in cluster) / count
        vocal = (
            count >= 12
            or (count >= 8 and avg_p >= 0.38)
            or (count >= 5 and rate >= 0.85 and avg_p >= 0.32)
            or (count >= 4 and rate >= 1.2 and avg_p >= 0.5)
        )
        for i, w in enumerate(cluster):
            w["cluster_start"] = a
            w["cluster_end"] = b
            w["cluster_vocal"] = vocal
            w["cheap_skip"] = not vocal
            w["is_cluster_start"] = i == 0


def skip_cheap(whisper_words, index):
    """Advance past an instrumental / hallucination cluster."""
    n = len(whisper_words)
    if not in_bounds(whisper_words, index):
        return n
    if not whisper_words[index].get("cheap_skip"):
        return index
    end = whisper_words[index].get("cluster_end", index)
    return min(n, end + 1)


def next_vocal_index(whisper_words, index):
    i = skip_cheap(whisper_words, index)
    while i < len(whisper_words) and whisper_words[i].get("cheap_skip"):
        i = skip_cheap(whisper_words, i)
    return i


def parse_lyrics(lyrics):
    """Split canonical lyrics into display lines. Blank lines and tags are section breaks."""
    lines = []
    pending_break = False
    section_id = 0

    for raw_line in lyrics.splitlines():
        text = raw_line.strip()
        if not text:
            pending_break = True
            continue

        if re.fullmatch(r"\[.*\]", text):
            pending_break = True
            lines.append({
                "text": text, "words": [], "is_break": True, "section_id": section_id,
            })
            continue

        words = [normalize_word(w) for w in re.findall(r"\S+", text)]
        words = [w for w in words if w]
        if not words:
            pending_break = True
            continue

        if pending_break:
            if lines and not lines[-1]["is_break"]:
                lines.append({
                    "text": "", "words": [], "is_break": True, "section_id": section_id,
                })
            if lines:
                section_id += 1
            pending_break = False

        lines.append({
            "text": text, "words": words, "is_break": False, "section_id": section_id,
        })

    return lines


def sung_lines(lines):
    return [line for line in lines if not line["is_break"]]


def align_line(lyric_words, whisper_words, start_index, search_window=SEARCH_WINDOW, quiet=False):
    """Find the best contiguous Whisper span for one lyric line. Original scoring."""
    if not lyric_words or not whisper_words:
        return None
    if start_index >= len(whisper_words):
        return None

    target = [normalize_word(w) for w in lyric_words]
    target = [w for w in target if w]
    if not target:
        return None

    best = None
    max_extra = 5
    max_span = len(target) + max_extra
    search_end = min(len(whisper_words), start_index + search_window)

    for i in range(start_index, search_end):
        first = whisper_words[i].get("norm") or normalize_word(whisper_words[i]["text"])
        if not first or first in {"music", "music]", "[music]"}:
            continue
        if whisper_words[i].get("cheap_skip") and similarity(target[0], first) < 0.9:
            continue

        for span_len in range(
            max(1, len(target) - 2),
            min(max_span, len(whisper_words) - i) + 1,
        ):
            candidate_words = whisper_words[i:i + span_len]
            if not candidate_words:
                continue

            internal_gap = 0.0
            for a, b in zip(candidate_words, candidate_words[1:]):
                internal_gap = max(internal_gap, b["start"] - a["end"])
            if internal_gap >= INSTRUMENTAL_GAP:
                continue

            candidate = [
                (w.get("norm") or normalize_word(w["text"]))
                for w in candidate_words
            ]
            candidate = [w for w in candidate if w]
            if not candidate:
                continue

            n = len(target)
            m = len(candidate)
            dp = [[0.0] * (m + 1) for _ in range(n + 1)]
            for a in range(n + 1):
                dp[a][0] = a
            for b in range(m + 1):
                dp[0][b] = b
            for a in range(1, n + 1):
                for b in range(1, m + 1):
                    if target[a - 1] == candidate[b - 1]:
                        cost = 0.0
                    else:
                        cost = 1.0 - similarity(target[a - 1], candidate[b - 1])
                    dp[a][b] = min(
                        dp[a - 1][b] + 1,
                        dp[a][b - 1] + 1,
                        dp[a - 1][b - 1] + cost,
                    )

            distance = dp[n][m]
            score = 1.0 - (distance / max(n, m))
            if candidate[0] == target[0]:
                score += 0.05
            if candidate[-1] == target[-1]:
                score += 0.05
            score -= max(0, span_len - len(target)) * 0.015

            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "start": i,
                    "end": i + span_len - 1,
                    "distance": distance,
                    "candidate": candidate,
                }

    if best is None:
        return None

    threshold = 0.48 if len(target) <= 4 else 0.52
    best["score"] = min(1.0, best["score"])
    if best["score"] < threshold:
        if not quiet:
            print(
                f"    rejected: "
                f"{best['score']:.0%} < {threshold:.0%} | "
                f"candidate: {' '.join(best['candidate'])}"
            )
        return None

    if not in_bounds(whisper_words, best["start"]) or not in_bounds(whisper_words, best["end"]):
        return None

    start_word = whisper_words[best["start"]]
    end_word = whisper_words[best["end"]]
    return {
        "start": start_word["start"],
        "end": end_word["end"],
        "score": best["score"],
        "whisper_start_index": best["start"],
        "whisper_end_index": best["end"],
        "source": "aligned",
    }


def timed_result(line, hit):
    return {
        "text": line["text"],
        "start": hit["start"],
        "end": hit["end"],
        "score": hit.get("score", 0.0),
        "source": hit.get("source", "aligned"),
        "whisper_start_index": hit.get("whisper_start_index"),
        "whisper_end_index": hit.get("whisper_end_index"),
    }


def align_sequential(lines, whisper_words, quiet=False):
    """Original left-to-right line matching, with instrumental skips."""
    results = [None] * len(lines)
    if not lines or not whisper_words:
        return results

    whisper_index = next_vocal_index(whisper_words, 0)

    for i, line in enumerate(lines):
        if whisper_index >= len(whisper_words):
            break

        idx = next_vocal_index(whisper_words, whisper_index)
        hit = align_line(line["words"], whisper_words, idx, quiet=quiet)

        if hit is None and idx != whisper_index:
            hit = align_line(line["words"], whisper_words, whisper_index, quiet=True)

        if hit is None:
            jumped = next_vocal_index(whisper_words, idx if idx != whisper_index else whisper_index)
            if in_bounds(whisper_words, jumped):
                cluster_end = whisper_words[jumped].get("cluster_end", jumped)
                nxt = next_vocal_index(whisper_words, cluster_end + 1)
                if nxt < len(whisper_words) and nxt > jumped:
                    hit = align_line(line["words"], whisper_words, nxt, quiet=True)

        if hit is None:
            continue

        results[i] = timed_result(line, hit)
        whisper_index = hit["whisper_end_index"] + 1

    return results


def skip_whisper_cost(ww, prev=None, is_prefix=False):
    cost = -0.10
    if is_prefix or ww.get("cheap_skip"):
        cost = -0.02
    if ww.get("p", 0.7) < 0.35:
        cost = min(cost, -0.02)
    if prev is not None and ww["start"] - prev["end"] >= INSTRUMENTAL_GAP:
        cost = min(cost, -0.02)
    return cost


def skip_lyric_cost(lw):
    if len(lw["norm"]) <= 2:
        return -0.28
    return -0.52


def match_score(lw, ww, whisper_words, j):
    sim = similarity(lw["norm"], ww["norm"])
    if sim >= 0.99:
        score = 1.15
    elif sim >= 0.8:
        score = sim
    elif sim >= 0.55:
        score = sim - 0.25
    else:
        score = sim - 1.05
    if ww.get("cheap_skip"):
        score -= 0.55
    if j > 0 and in_bounds(whisper_words, j - 1):
        gap = ww["start"] - whisper_words[j - 1]["end"]
        if gap >= INSTRUMENTAL_GAP:
            score += 0.08 if lw.get("is_line_start") else -0.9
    return score


def align_words_global(lyric_seq, whisper_words):
    n = len(lyric_seq)
    m = len(whisper_words)
    if n == 0 or m == 0:
        return [None] * n

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    bt = [[0] * (m + 1) for _ in range(n + 1)]

    for j in range(1, m + 1):
        prev = whisper_words[j - 2] if j >= 2 else None
        dp[0][j] = dp[0][j - 1] + skip_whisper_cost(
            whisper_words[j - 1], prev=prev, is_prefix=True,
        )
        bt[0][j] = 1
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + skip_lyric_cost(lyric_seq[i - 1])
        bt[i][0] = 2

    for i in range(1, n + 1):
        lw = lyric_seq[i - 1]
        row, prev_row, bt_row = dp[i], dp[i - 1], bt[i]
        for j in range(1, m + 1):
            ww = whisper_words[j - 1]
            prev = whisper_words[j - 2] if j >= 2 else None
            diag = prev_row[j - 1] + match_score(lw, ww, whisper_words, j - 1)
            skip_w = row[j - 1] + skip_whisper_cost(ww, prev=prev)
            skip_l = prev_row[j] + skip_lyric_cost(lw)
            if diag >= skip_w and diag >= skip_l:
                row[j] = diag
                bt_row[j] = 0
            elif skip_w >= skip_l:
                row[j] = skip_w
                bt_row[j] = 1
            else:
                row[j] = skip_l
                bt_row[j] = 2

    mapping = [None] * n
    i, j = n, m
    while i > 0 or j > 0:
        if i == 0:
            j -= 1
            continue
        if j == 0:
            i -= 1
            continue
        move = bt[i][j]
        if move == 0:
            mapping[i - 1] = j - 1
            i -= 1
            j -= 1
        elif move == 1:
            j -= 1
        else:
            i -= 1
    return mapping


def densest_cluster(indices, whisper_words):
    indices = [i for i in sorted(set(indices)) if in_bounds(whisper_words, i)]
    if not indices:
        return []
    groups = [[indices[0]]]
    for idx in indices[1:]:
        prev = groups[-1][-1]
        gap = whisper_words[idx]["start"] - whisper_words[prev]["end"]
        if gap >= INSTRUMENTAL_GAP or idx - prev > 12:
            groups.append([idx])
        else:
            groups[-1].append(idx)
    return max(groups, key=len)


def line_timing_from_mapping(line, mapping_slice, whisper_words):
    matched = [j for j in mapping_slice if j is not None and in_bounds(whisper_words, j)]
    if not matched:
        return None
    cluster = densest_cluster(matched, whisper_words)
    if not cluster:
        return None
    start_w = whisper_words[cluster[0]]
    end_w = whisper_words[cluster[-1]]
    hits = 0
    for j, lw in zip(mapping_slice, line["words"]):
        if j is None or not in_bounds(whisper_words, j):
            continue
        if similarity(lw, whisper_words[j]["norm"]) >= 0.7:
            hits += 1
    score = hits / max(1, len(line["words"]))
    duration = end_w["end"] - start_w["start"]
    typical = max(1.0, 0.38 * len(line["words"]))
    if duration > typical * 4.5:
        return None
    return {
        "text": line["text"],
        "start": start_w["start"],
        "end": end_w["end"],
        "score": min(1.0, score),
        "source": "aligned",
        "whisper_start_index": cluster[0],
        "whisper_end_index": cluster[-1],
    }


def align_global(lines, whisper_words):
    """Whole-song word alignment. Used only if greedy leaves many holes."""
    results = [None] * len(lines)
    if not lines or not whisper_words:
        return results
    flat = []
    for line_index, line in enumerate(lines):
        for word_index, norm in enumerate(line["words"]):
            flat.append({
                "norm": norm,
                "line_index": line_index,
                "is_line_start": word_index == 0,
            })
    mapping = align_words_global(flat, whisper_words)
    offset = 0
    for i, line in enumerate(lines):
        n = len(line["words"])
        results[i] = line_timing_from_mapping(
            line, mapping[offset:offset + n], whisper_words,
        )
        offset += n
    return results


def real_count(results):
    return sum(
        1 for r in results
        if r is not None and r.get("source") in {"aligned", "local"}
    )


def expected_line_duration(line):
    return min(6.0, max(1.15, 0.42 * len(line["words"])))


def fill_with_local(results, lines, whisper_words):
    n = len(results)
    if not whisper_words:
        return results
    for i, r in enumerate(results):
        if r is not None:
            continue
        prev_idx = next((k for k in range(i - 1, -1, -1) if results[k] is not None), None)
        next_idx = next((k for k in range(i + 1, n) if results[k] is not None), None)

        if prev_idx is None:
            start_index = next_vocal_index(whisper_words, 0)
            t_min = 0.0
        else:
            end_i = results[prev_idx].get("whisper_end_index")
            if end_i is None:
                start_index = 0
                t_prev = results[prev_idx]["end"]
                for wi, w in enumerate(whisper_words):
                    if w["start"] >= t_prev - 0.05:
                        start_index = wi
                        break
            else:
                start_index = end_i + 1
            t_min = results[prev_idx]["end"]

        t_limit = (
            whisper_words[-1]["end"] if next_idx is None
            else results[next_idx]["start"] - 0.05
        )
        start_index = next_vocal_index(whisper_words, start_index)
        if start_index >= len(whisper_words) or t_limit <= t_min:
            continue
        hit = align_line(lines[i]["words"], whisper_words, start_index, quiet=True)
        if hit is None:
            continue
        if hit["start"] < t_min - 0.15:
            continue
        if next_idx is not None and hit["start"] > results[next_idx]["start"] - 0.05:
            continue
        hit["source"] = "local"
        results[i] = timed_result(lines[i], hit)
    return results


def interpolate_unmatched(results, lines, whisper_words):
    n = len(results)
    if n == 0:
        return results

    vocal_onset = 0.0
    for w in whisper_words:
        if not w.get("cheap_skip"):
            vocal_onset = w["start"]
            break
    else:
        if whisper_words:
            vocal_onset = whisper_words[0]["start"]

    def fill_range(lo, hi, t_left, t_right):
        block = list(range(max(0, lo), min(n, hi)))
        if not block:
            return
        weights = [max(1, len(lines[i]["words"])) for i in block]
        total_w = sum(weights) or 1
        typical = sum(expected_line_duration(lines[i]) for i in block)
        if t_right < t_left:
            t_left, t_right = t_right, t_left
        span = max(0.4, t_right - t_left)
        if span > typical * 2.2:
            span = typical
        t = t_left
        for i, w in zip(block, weights):
            dur = span * (w / total_w)
            results[i] = {
                "text": lines[i]["text"],
                "start": t,
                "end": t + max(0.5, dur * 0.85),
                "score": 0.0,
                "source": "interpolated",
                "whisper_start_index": None,
                "whisper_end_index": None,
            }
            t += dur

    first = next((i for i, r in enumerate(results) if r), None)
    if first is None:
        return results
    if first > 0:
        t_right = results[first]["start"]
        typical = sum(expected_line_duration(lines[i]) for i in range(first))
        t_left = max(vocal_onset, t_right - typical)
        if t_left < t_right - 0.2:
            fill_range(0, first, t_left, t_right)

    i = 0
    while i < n:
        if results[i] is not None:
            i += 1
            continue
        j = i
        while j < n and results[j] is None:
            j += 1
        if j >= n:
            break
        prev_i = i - 1
        t_left = results[prev_i]["end"] if prev_i >= 0 and results[prev_i] else vocal_onset
        t_right = results[j]["start"]
        gap = t_right - t_left
        typical = sum(expected_line_duration(lines[k]) for k in range(i, j))
        prev_sec = lines[prev_i].get("section_id", 0) if prev_i >= 0 else lines[i].get("section_id", 0)
        next_sec = lines[j].get("section_id", prev_sec)
        split = i
        while split < j and lines[split].get("section_id", prev_sec) == prev_sec:
            split += 1
        if split > i and split < j and prev_sec != next_sec:
            typical_left = sum(expected_line_duration(lines[k]) for k in range(i, split))
            typical_right = sum(expected_line_duration(lines[k]) for k in range(split, j))
            fill_range(i, split, t_left, t_left + typical_left)
            fill_range(split, j, max(t_left, t_right - typical_right), t_right)
        elif gap > max(8.0, typical * 2.5):
            if lines[i].get("section_id", prev_sec) == prev_sec:
                fill_range(i, j, t_left, t_left + typical)
            else:
                fill_range(i, j, max(t_left, t_right - typical), t_right)
            last = results[j - 1] if j - 1 >= 0 else None
            if last is not None and last["end"] > t_right - 0.05:
                fill_range(i, j, max(t_left, t_right - typical), t_right)
        else:
            fill_range(i, j, t_left, t_right)
        i = j

    last = max((i for i, r in enumerate(results) if r), default=None)
    if last is not None and last < n - 1:
        t_left = results[last]["end"]
        typical = sum(expected_line_duration(lines[i]) for i in range(last + 1, n))
        song_end = whisper_words[-1]["end"] if whisper_words else t_left + typical
        t_right = min(song_end, t_left + typical)
        if t_right <= t_left:
            t_right = t_left + typical
        fill_range(last + 1, n, t_left, t_right)

    return results


def enforce_monotonic(results):
    prev_start = -0.05
    for r in results:
        if r is None:
            continue
        if r["start"] < prev_start + 0.05:
            r["start"] = prev_start + 0.05
        if r["end"] <= r["start"]:
            r["end"] = r["start"] + 0.4
        prev_start = r["start"]
    return results


def finish(results, lines, whisper_words):
    results = fill_with_local(results, lines, whisper_words)
    results = interpolate_unmatched(results, lines, whisper_words)
    results = enforce_monotonic(results)
    return [r for r in results if r is not None]


def align_lyrics(lyrics, whisper_words, quiet=False):
    parsed = parse_lyrics(lyrics)
    lines = sung_lines(parsed)
    if not lines or not whisper_words:
        return []

    sequential = align_sequential(lines, whisper_words, quiet=quiet)
    seq_real = real_count(sequential)

    chosen = sequential
    if seq_real < max(1, int(round(len(lines) * FALLBACK_RATIO))):
        global_results = align_global(lines, whisper_words)
        if real_count(global_results) > seq_real:
            chosen = global_results
            if not quiet:
                print(
                    f"  fallback: global alignment "
                    f"{real_count(global_results)}/{len(lines)} "
                    f"(greedy was {seq_real}/{len(lines)})"
                )

    return finish(chosen, lines, whisper_words)


def seconds_to_lrc(seconds):
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    remaining = seconds - minutes * 60
    whole = int(remaining)
    centiseconds = int(round((remaining - whole) * 100))
    if centiseconds >= 100:
        whole += 1
        centiseconds = 0
    if whole >= 60:
        minutes += 1
        whole = 0
    return f"[{minutes:02d}:{whole:02d}.{centiseconds:02d}]"


def write_lrc(path, aligned):
    """
    Write LRC with ♪ on detected intro / solo / interlude gaps.

    Intro: ♪ at 00:00.00 when the first vocal is later than INTRO_MUSIC_GAP.
    Solo / interlude: ♪ after a line when the next vocal is at least
    SOLO_MUSIC_GAP seconds later. Outro always gets a closing ♪.
    """
    def line_out(ts, text):
        return seconds_to_lrc(ts) + " " + text + "\n"

    with path.open("w", encoding="utf-8") as f:
        if aligned and aligned[0]["start"] > INTRO_MUSIC_GAP:
            f.write(line_out(0.0, MUSIC_SYMBOL))

        for i, line in enumerate(aligned):
            f.write(line_out(line["start"], line["text"]))
            end = line["end"]
            if i + 1 < len(aligned):
                nxt = aligned[i + 1]["start"]
                if nxt - end >= SOLO_MUSIC_GAP:
                    clear_at = end + 0.12
                    if clear_at < nxt - 0.15:
                        f.write(line_out(clear_at, MUSIC_SYMBOL))
            else:
                f.write(line_out(end + 0.12, MUSIC_SYMBOL))


def find_whisper_file(track_number, title):
    whisper_file = WHISPER_DIR / f"{safe_name(track_number)}-{safe_name(title)}.json"
    if whisper_file.exists():
        return whisper_file
    candidates = list(WHISPER_DIR.glob(f"*{safe_name(title)}*.json"))
    return candidates[0] if candidates else None


def process_job(job):
    title = job["title"]
    album = job["album"]
    track_number = job["track_number"]
    lyrics = job.get("lyrics", "")

    output_dir = OUTPUT_DIR / album
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{int(track_number):02d} - {title}.lrc"

    if output_file.exists():
        print("-" * 70)
        print(f"SKIP {int(track_number):02d} - {title}")
        print(f"Existing LRC: {output_file}")
        return {
            "track": int(track_number), "title": title,
            "status": "skipped-existing", "aligned": None, "total": None,
        }

    whisper_file = find_whisper_file(track_number, title)
    if whisper_file is None:
        print("-" * 70)
        print(f"ERROR {int(track_number):02d} - {title}")
        print("Whisper JSON not found.")
        return {
            "track": int(track_number), "title": title,
            "status": "missing-whisper", "aligned": 0, "total": 0,
        }

    if not str(lyrics).strip():
        print("-" * 70)
        print(f"SKIP {int(track_number):02d} - {title}")
        print("No lyrics in metadata.")
        return {
            "track": int(track_number), "title": title,
            "status": "no-lyrics", "aligned": 0, "total": 0,
        }

    print("=" * 70)
    print(f"Track : {int(track_number):02d} - {title}")
    print(f"Whisper: {whisper_file}")
    print("=" * 70)

    try:
        whisper_words = extract_words(whisper_file)
        parsed = parse_lyrics(lyrics)
        lines = sung_lines(parsed)
        vocal_clusters = {
            (w.get("cluster_start"), w.get("cluster_end"))
            for w in whisper_words if w.get("cluster_vocal")
        }
        skip_clusters = {
            (w.get("cluster_start"), w.get("cluster_end"))
            for w in whisper_words if w.get("cheap_skip")
        }
        print(f"Whisper words : {len(whisper_words)}")
        print(f"Lyric lines   : {len(lines)}")
        print(f"Vocal clusters: {len(vocal_clusters)}  skip-clusters: {len(skip_clusters)}")
        print()

        aligned = align_lyrics(lyrics, whisper_words)

        warnings = 0
        interpolated = 0
        used = [False] * len(aligned)
        for line_number, line in enumerate(lines, start=1):
            found = found_k = None
            for k, r in enumerate(aligned):
                if used[k]:
                    continue
                if r["text"] == line["text"]:
                    found, found_k = r, k
                    break
            if found is None:
                print(f"WARNING {line_number:03d}: {line['text']}")
                warnings += 1
                continue
            used[found_k] = True
            src = found.get("source", "aligned")
            if src == "interpolated":
                interpolated += 1
                tag = "interp"
            else:
                tag = f"{found.get('score', 0):.0%}"
            print(
                f"{line_number:03d} "
                f"{found['start']:7.2f} -> "
                f"{found['end']:7.2f} "
                f"({tag:>6}) "
                f"{line['text']}"
            )

        print()
        print("-" * 70)
        print(f"Aligned lines: {len(aligned)} / {len(lines)}")
        if interpolated:
            print(f"Interpolated : {interpolated}")
        if warnings:
            print(f"Unmatched    : {warnings}")

        write_lrc(output_file, aligned)
        print(f"LRC written: {output_file}")
        return {
            "track": int(track_number),
            "title": title,
            "status": "generated",
            "aligned": len(aligned) - interpolated,
            "total": len(lines),
            "warnings": warnings,
            "interpolated": interpolated,
        }

    except Exception as exc:
        print()
        print(f"ERROR processing {int(track_number):02d} - {title}:")
        print(f"  {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return {
            "track": int(track_number), "title": title,
            "status": "error", "aligned": 0, "total": 0,
        }


def main():
    if not JOBS_FILE.exists():
        print(f"ERROR: {JOBS_FILE} does not exist")
        sys.exit(1)

    with JOBS_FILE.open("r", encoding="utf-8") as f:
        jobs = json.load(f)

    if not jobs:
        print("ERROR: No jobs found")
        sys.exit(1)

    print("=" * 70)
    print("GENERATING LYRICS LRC FILES")
    print("=" * 70)
    print(f"Jobs found: {len(jobs)}")
    print()

    summary = []
    for job in jobs:
        summary.append(process_job(job))
        print()

    print()
    print("=" * 70)
    print("LYRICS GENERATION SUMMARY")
    print("=" * 70)

    generated = skipped = errors = 0
    for item in summary:
        track, title, status = item["track"], item["title"], item["status"]
        if status == "generated":
            generated += 1
            aligned, total = item["aligned"], item["total"]
            warnings = item.get("warnings") or 0
            interpolated = item.get("interpolated") or 0
            bits = [f"{aligned}/{total}"]
            if interpolated:
                bits.append(f"{interpolated} interp")
            if warnings:
                bits.append(f"{warnings} unmatched")
            detail = " ".join(bits) if (warnings or interpolated) else f"{aligned}/{total} OK"
            print(f"{track:02d} - {title:<35} {detail}")
        elif status == "skipped-existing":
            skipped += 1
            print(f"{track:02d} - {title:<35} SKIPPED (existing LRC)")
        else:
            errors += 1
            print(f"{track:02d} - {title:<35} {status.upper()}")

    print()
    print("-" * 70)
    print(f"Generated : {generated}")
    print(f"Skipped   : {skipped}")
    print(f"Problems  : {errors}")
    print("=" * 70)


def _w(text, start, dur=0.35, p=0.85):
    return {
        "text": text, "norm": normalize_word(text),
        "start": start, "end": start + dur, "p": p,
    }


def _cluster_words(items):
    words = []
    for text, start, *rest in items:
        dur = rest[0] if rest else 0.32
        p = rest[1] if len(rest) > 1 else 0.85
        words.append(_w(text, start, dur=dur, p=p))
    mark_clusters(words)
    return words


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run_self_test():
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
            traceback.print_exc()

    def test_intro_hallucination():
        lyrics = (
            "Hello darkness my old friend\n"
            "I've come to talk with you again\n"
            "Because a vision softly creeping\n"
        )
        intro = [
            ("Hello", 0.4, 0.3, 0.22),
            ("darkness", 1.2, 0.3, 0.20),
            ("friend", 3.8, 0.3, 0.18),
        ]
        t = 42.0
        real = []
        for word in (
            "Hello darkness my old friend I've come to talk with you again "
            "Because a vision softly creeping"
        ).split():
            real.append((word, t, 0.28, 0.9))
            t += 0.38
        aligned = align_lyrics(lyrics, _cluster_words(intro + real), quiet=True)
        _assert(len(aligned) == 3, f"got {len(aligned)}")
        _assert(aligned[0]["start"] >= 35.0, f"pinned early at {aligned[0]['start']:.2f}")

    def test_instrumental_gap():
        lyrics = (
            "Walking down the street tonight\n"
            "Nothing but the city lights\n"
            "\n"
            "Hold me closer to the fire\n"
            "Sing it louder lift me higher\n"
        )
        items = []
        t = 5.0
        for word in "Walking down the street tonight Nothing but the city lights".split():
            items.append((word, t, 0.28, 0.9))
            t += 0.36
        items += [("Hold", 22.0, 0.25, 0.2), ("fire", 24.5, 0.25, 0.18), ("higher", 27.0, 0.25, 0.15)]
        t = 38.0
        for word in "Hold me closer to the fire Sing it louder lift me higher".split():
            items.append((word, t, 0.28, 0.9))
            t += 0.36
        aligned = align_lyrics(lyrics, _cluster_words(items), quiet=True)
        _assert(len(aligned) == 4, f"got {len(aligned)}")
        _assert(aligned[0]["start"] < 10.0, "verse 1 should stay early")
        _assert(aligned[2]["start"] >= 30.0, f"verse 2 too early: {aligned[2]['start']:.2f}")

    def test_immediate_start():
        lyrics = "One two three four\nFive six seven eight\n"
        items = []
        t = 0.15
        for word in "One two three four Five six seven eight".split():
            items.append((word, t, 0.28, 0.92))
            t += 0.34
        aligned = align_lyrics(lyrics, _cluster_words(items), quiet=True)
        _assert(len(aligned) == 2, f"got {len(aligned)}")
        _assert(aligned[0]["start"] < 1.0, f"delayed immediate start: {aligned[0]['start']:.2f}")

    def test_music_cues():
        import tempfile

        def dump(aligned):
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "t.lrc"
                write_lrc(path, aligned)
                return path.read_text(encoding="utf-8")

        long_gap = dump([
            {"text": "First", "start": 12.0, "end": 14.5, "score": 1, "source": "aligned"},
            {"text": "Second", "start": 40.0, "end": 42.0, "score": 1, "source": "aligned"},
        ])
        _assert(long_gap.startswith("[00:00.00] " + MUSIC_SYMBOL), long_gap)
        _assert("[00:12.00] First" in long_gap, long_gap)
        _assert("[00:14.62] " + MUSIC_SYMBOL in long_gap, long_gap)
        _assert("[00:40.00] Second" in long_gap, long_gap)
        _assert(long_gap.strip().endswith(MUSIC_SYMBOL), long_gap)

        short_gap = dump([
            {"text": "First", "start": 12.0, "end": 14.5, "score": 1, "source": "aligned"},
            {"text": "Second", "start": 17.5, "end": 19.0, "score": 1, "source": "aligned"},
        ])
        _assert(short_gap.startswith("[00:00.00] " + MUSIC_SYMBOL), short_gap)
        _assert(("[00:14.62] " + MUSIC_SYMBOL) not in short_gap, short_gap)

        immediate = dump([
            {"text": "First", "start": 0.2, "end": 2.0, "score": 1, "source": "aligned"},
            {"text": "Second", "start": 2.5, "end": 4.0, "score": 1, "source": "aligned"},
        ])
        _assert(not immediate.startswith("[00:00.00] " + MUSIC_SYMBOL), immediate)

    def test_three_cluster_song_like_09():
        lyrics_lines = []
        items = []
        t = 2.0
        for s in range(6):
            if s in (2, 4):
                t += 20.0
                lyrics_lines.append("")
            for i in range(6):
                text = f"Stih {s+1} linija {i+1} tvoja ljubav svjetlo"
                lyrics_lines.append(text)
                for word in text.split():
                    items.append((word, t, 0.28, 0.9))
                    t += 0.32
        lyrics = "\n".join(lyrics_lines)
        sung = [l for l in lyrics_lines if l.strip()]
        aligned = align_lyrics(lyrics, _cluster_words(items), quiet=True)
        interp = sum(1 for r in aligned if r.get("source") == "interpolated")
        real = len(aligned) - interp
        _assert(len(aligned) == len(sung), f"got {len(aligned)}/{len(sung)}")
        _assert(real >= int(len(sung) * 0.9), f"only {real}/{len(sung)} really aligned, {interp} interp")

    def test_more_sections_than_whisper():
        lyrics = "\n\n".join([
            "Prvi stih ide ovako\nDrugi stih isto tako",
            "Treci stih kroz noc\nCetvrti stih u zoru",
            "Peti stih na kraju\nSesti stih za kraj",
            "Sedmi opet zapevaj\nOsmi stih ne stane",
            "Deveti poslednji put\nDeseti gotovo sad",
        ])
        items = []
        t = 5.0
        for word in (
            "Prvi stih ide ovako Drugi stih isto tako "
            "Treci stih kroz noc Cetvrti stih u zoru"
        ).split():
            items.append((word, t, 0.28, 0.9))
            t += 0.35
        aligned = align_lyrics(lyrics, _cluster_words(items), quiet=True)
        _assert(len(aligned) == 10, f"got {len(aligned)}")

    def test_croatian_diacritics():
        lyrics = "Tvoj zaštitnik stoji kraj mene\nČuvaj me noćas\n"
        items = []
        t = 12.0
        for word in "Tvoj zastitnik stoji kraj mene Cuvaj me nocas".split():
            items.append((word, t, 0.28, 0.9))
            t += 0.34
        aligned = align_lyrics(lyrics, _cluster_words(items), quiet=True)
        _assert(len(aligned) == 2, f"got {len(aligned)}")

    def test_no_crash_exhausted_cursor():
        lyrics = "Alpha beta gamma delta\n" * 20
        items = [("Alpha", 1.0, 0.3, 0.9), ("beta", 1.4, 0.3, 0.9)]
        aligned = align_lyrics(lyrics, _cluster_words(items), quiet=True)
        _assert(len(aligned) >= 1, "should not crash")

    print("SELF-TEST")
    check("intro hallucination is not used", test_intro_hallucination)
    check("instrumental gap does not steal verse 2", test_instrumental_gap)
    check("immediate start stays immediate", test_immediate_start)
    check("intro vs solo music-gap tunables", test_music_cues)
    check("three-cluster song matches, not interpolates", test_three_cluster_song_like_09)
    check("more lyric sections than whisper words", test_more_sections_than_whisper)
    check("croatian diacritics still match", test_croatian_diacritics)
    check("exhausted whisper does not crash", test_no_crash_exhausted_cursor)

    if failures:
        print(f"\n{len(failures)} failed")
        sys.exit(1)
    print("\nAll tests passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"--self-test", "--test"}:
        run_self_test()
    else:
        main()
