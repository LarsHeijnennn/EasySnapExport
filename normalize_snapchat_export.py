#!/usr/bin/env python3
"""
Normalize unzipped Snapchat Memories exports into a clean, sortable media folder.

The tool is intentionally generic: pass it a directory containing one or more
unzipped Snapchat "mydata" folders and it will discover media, dedupe export
copies, join JSON metadata where possible, copy files into a normalized output
tree, and embed capture dates through ExifTool.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ is expected.
    ZoneInfo = None  # type: ignore


MEDIA_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<uuid>[A-Fa-f0-9-]+)-"
    r"(?P<role>main|overlay)\."
    r"(?P<ext>jpg|jpeg|png|webp|mp4)$",
    re.IGNORECASE,
)
NORMALIZED_MEDIA_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_"
    r"(?P<time>\d{2}-\d{2}-\d{2})_"
    r"(?P<uuid>[A-Fa-f0-9-]+)_"
    r"(?P<role>main|overlay|composited)\."
    r"(?P<ext>jpg|jpeg|png|webp|mp4)$",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"Latitude,\s*Longitude:\s*(?P<lat>[-+]?\d+(?:\.\d+)?),\s*(?P<lon>[-+]?\d+(?:\.\d+)?)"
)
SNAPCHAT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
DISPLAY_DATE_FORMAT = "%Y-%m-%d_%H-%M-%S"
HASH_CHUNK_SIZE = 1024 * 1024


@dataclass
class MediaRecord:
    source: Path
    relative_source: str
    original_name: str
    filename_date: str
    uuid: str
    role: str
    ext: str
    size: int
    mtime_utc: datetime
    birth_utc: datetime
    capture_utc: datetime
    media_type: str
    content_hash: str = ""
    duplicate_of: str = ""
    conflict_group: str = ""
    selected: bool = False
    target_path: str = ""
    local_timestamp: str = ""
    utc_timestamp: str = ""
    latitude: str = ""
    longitude: str = ""
    json_matched: bool = False
    json_location: str = ""
    status: str = "pending"


@dataclass
class JsonRow:
    date_text: str
    media_type: str
    location_text: str = ""
    latitude: str = ""
    longitude: str = ""
    raw: dict = field(default_factory=dict)
    source_json: str = ""
    matched: bool = False


@dataclass
class ScanResult:
    media: List[MediaRecord]
    json_rows: List[JsonRow]
    ignored_counts: Counter
    json_files: List[str]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize unzipped Snapchat Memories exports with embedded metadata."
    )
    parser.add_argument("input", type=Path, help="Folder containing unzipped Snapchat export folders.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run preflight checks for paths, Python modules, ExifTool, ffmpeg, and export shape, then exit.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output folder. Defaults to '<input>/normalized-output'.",
    )
    parser.add_argument(
        "--timezone",
        default="UTC",
        help="IANA timezone for filenames/folders, for example Europe/Amsterdam. Default: UTC.",
    )
    parser.add_argument("--apply", action="store_true", help="Copy files and write metadata.")
    parser.add_argument("--resume", action="store_true", help="Skip already copied files with matching size.")
    parser.add_argument(
        "--compose-overlays",
        action="store_true",
        help="After --apply, also create baked main+overlay image copies under composited/YYYY/MM/.",
    )
    parser.add_argument(
        "--merge-composited-into-media",
        action="store_true",
        help="After composing image overlays, replace matching media/*_main.jpg files and remove successful overlay files.",
    )
    parser.add_argument(
        "--merge-video-overlays",
        action="store_true",
        help="Bake exact-stem media/*_main.mp4 + media/*_overlay.* pairs into the MP4 and remove successful overlays.",
    )
    parser.add_argument(
        "--video-dry-run",
        action="store_true",
        help="Report exact video overlay pairs without baking or deleting files.",
    )
    parser.add_argument(
        "--compose-only",
        action="store_true",
        help="Use an existing normalized output index and only create baked overlay images.",
    )
    parser.add_argument(
        "--low-impact",
        action="store_true",
        help="Reduce CPU pressure: lower process priority, use at most 2 workers, and smaller ExifTool batches.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 4),
        help="Worker threads for stat/copy/hash work. Default: min(8, CPU count).",
    )
    parser.add_argument(
        "--exiftool-batch-size",
        type=int,
        default=750,
        help="Files per ExifTool argfile batch. Default: 750.",
    )
    parser.add_argument(
        "--verify-hashes",
        choices=("suspected", "all", "none"),
        default="suspected",
        help="Hash suspected duplicate/conflict groups, all files, or no files. Default: suspected.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=500,
        help="Print progress after this many copied/metadata files. Default: 500.",
    )
    return parser.parse_args(argv)


def find_exiftool() -> Optional[str]:
    found = which_any("exiftool", "exiftool.exe")
    if found:
        return found
    for candidate in platform_tool_candidates("exiftool"):
        if usable_tool_candidate(candidate):
            return str(candidate)
    return None


def find_ffmpeg() -> Optional[str]:
    found = which_any("ffmpeg", "ffmpeg.exe")
    if found:
        return found
    for candidate in platform_tool_candidates("ffmpeg"):
        if usable_tool_candidate(candidate):
            return str(candidate)
    return None


def find_ffprobe() -> Optional[str]:
    found = which_any("ffprobe", "ffprobe.exe")
    if found:
        return found
    for candidate in platform_tool_candidates("ffprobe"):
        if usable_tool_candidate(candidate):
            return str(candidate)
    return None


def which_any(*names: str) -> Optional[str]:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def usable_tool_candidate(path: Path) -> bool:
    if platform.system() == "Windows":
        return path.is_file()
    return path.exists() and os.access(path, os.X_OK)


def platform_tool_candidates(tool: str) -> List[Path]:
    if platform.system() == "Darwin":
        return [Path("/opt/homebrew/bin") / tool, Path("/usr/local/bin") / tool]
    if platform.system() != "Windows":
        return []

    exe = f"{tool}.exe"
    candidates = [
        Path.home() / "scoop" / "shims" / exe,
        Path(os.environ.get("ProgramData", "C:/ProgramData")) / "chocolatey" / "bin" / exe,
    ]
    if tool in {"ffmpeg", "ffprobe"}:
        local_appdata = os.environ.get("LOCALAPPDATA")
        winget_root = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages" if local_appdata else None
        if winget_root and winget_root.exists():
            candidates.extend(winget_root.glob(f"Gyan.FFmpeg_*/*/bin/{exe}"))
            candidates.extend(winget_root.glob(f"Gyan.FFmpeg_*/ffmpeg-*/bin/{exe}"))
    return candidates


def python_install_hint() -> str:
    if platform.system() == "Windows":
        return "Install Python 3.9+ with: winget install -e --id Python.Python.3.12"
    if platform.system() == "Darwin":
        return "Install a newer Python with: brew install python"
    return "Install Python 3.9+ with your system package manager."


def exiftool_install_hint() -> str:
    if platform.system() == "Windows":
        return "Install ExifTool with: winget install -e --id OliverBetz.ExifTool, then open a new PowerShell window."
    if platform.system() == "Darwin":
        return "Install ExifTool with: brew install exiftool"
    return "Install ExifTool and make sure the exiftool command is on PATH."


def pillow_install_hint() -> str:
    if platform.system() == "Windows":
        return "Install Pillow with: py -m pip install Pillow"
    return "Install Pillow with: python3 -m pip install Pillow"


def ffmpeg_install_hint() -> str:
    if platform.system() == "Windows":
        return "Install ffmpeg with: winget install -e --id Gyan.FFmpeg, then open a new PowerShell window."
    if platform.system() == "Darwin":
        return "Install ffmpeg with: brew install ffmpeg"
    return "Install ffmpeg/ffprobe and make sure both commands are on PATH."


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def first_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def needs_exiftool(args: argparse.Namespace) -> bool:
    return args.apply or args.compose_only or args.compose_overlays or args.merge_video_overlays


def needs_pillow(args: argparse.Namespace) -> bool:
    return args.compose_only or args.compose_overlays or args.merge_composited_into_media


def needs_ffmpeg(args: argparse.Namespace) -> bool:
    return args.merge_video_overlays


def preflight(args: argparse.Namespace, input_dir: Path, output: Path, tz) -> None:
    ok: List[str] = []
    problems: List[str] = []
    warnings: List[str] = []

    if sys.version_info < (3, 9):
        problems.append(
            f"Python 3.9+ is required; found {platform.python_version()}. "
            + python_install_hint()
        )
    else:
        ok.append(f"Python {platform.python_version()}")

    ok.append(f"timezone '{args.timezone}'")

    if not input_dir.exists() or not input_dir.is_dir():
        problems.append(f"Input folder does not exist or is not a directory: {input_dir}")
    else:
        ok.append(f"input folder exists: {input_dir}")

    output_parent = first_existing_parent(output.parent if output.suffix else output)
    if not output_parent.exists():
        problems.append(f"No existing parent folder for output path: {output}")
    elif needs_exiftool(args) or args.check:
        if os.access(output_parent, os.W_OK):
            ok.append(f"output parent is writable: {output_parent}")
        else:
            problems.append(f"Output parent is not writable: {output_parent}")

    exiftool = find_exiftool()
    if needs_exiftool(args):
        if exiftool:
            ok.append(f"ExifTool found: {exiftool}")
        else:
            problems.append(
                f"ExifTool is required for metadata-writing modes. {exiftool_install_hint()}"
            )
    elif exiftool:
        ok.append(f"ExifTool found: {exiftool}")
    else:
        warnings.append(f"ExifTool not found. Dry-run works, but --apply/overlay merge modes need it. {exiftool_install_hint()}")

    if needs_pillow(args):
        if module_available("PIL"):
            ok.append("Python Pillow module found")
        else:
            problems.append(
                f"Python Pillow is required for image overlay composition. {pillow_install_hint()}"
            )
    elif module_available("PIL"):
        ok.append("Python Pillow module found")
    else:
        warnings.append("Python Pillow not found. Normal dry-run works, but image overlay composition needs it.")

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if needs_ffmpeg(args):
        if ffmpeg and ffprobe:
            ok.append(f"ffmpeg found: {ffmpeg}")
            ok.append(f"ffprobe found: {ffprobe}")
        else:
            problems.append(f"ffmpeg and ffprobe are required for video overlay merging. {ffmpeg_install_hint()}")
    elif ffmpeg and ffprobe:
        ok.append(f"ffmpeg found: {ffmpeg}")
        ok.append(f"ffprobe found: {ffprobe}")
    else:
        warnings.append(f"ffmpeg/ffprobe not found. Video overlay dry-run works, but merging videos needs them. {ffmpeg_install_hint()}")

    if input_dir.exists() and input_dir.is_dir():
        media_paths, json_paths, ignored = discover_paths(input_dir, output)
        if media_paths:
            ok.append(f"Snapchat media-looking files found: {len(media_paths)}")
        elif not (args.compose_only or args.merge_video_overlays or args.video_dry_run):
            problems.append(
                "No Snapchat media files found. Expected files named like "
                "YYYY-MM-DD_UUID-main.jpg/mp4 or YYYY-MM-DD_UUID-overlay.png inside unzipped export folders."
            )
        if json_paths:
            ok.append(f"memories_history.json files found: {len(json_paths)}")
        elif not (args.compose_only or args.merge_video_overlays or args.video_dry_run):
            warnings.append("No memories_history.json found. Files can still be normalized from timestamps, but location matching is unavailable.")
        if ignored:
            ok.append(f"ignored junk/irrelevant items: {dict(ignored)}")

    if args.compose_only or args.merge_video_overlays or args.video_dry_run:
        media_root = output / "media"
        if media_root.exists() and media_root.is_dir():
            ok.append(f"normalized media folder exists: {media_root}")
        else:
            problems.append(f"Expected normalized media folder for overlay/video modes: {media_root}")

    print("Preflight checks")
    print("================")
    for item in ok:
        print(f"OK: {item}")
    for item in warnings:
        print(f"WARNING: {item}")
    if problems:
        print("\nProblems to fix")
        print("===============")
        for item in problems:
            print(f"- {item}")
        raise SystemExit(2)


def load_timezone(name: str):
    if ZoneInfo is None:
        raise SystemExit("Python 3.9+ with zoneinfo is required for timezone support.")
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise SystemExit(f"Unknown timezone '{name}': {exc}")


def should_prune_dir(path: Path, output: Path) -> bool:
    name = path.name
    if name in {"__MACOSX", ".git", ".work"}:
        return True
    if name.startswith("."):
        return True
    try:
        path.resolve().relative_to(output.resolve())
        return True
    except ValueError:
        return False


def discover_paths(input_dir: Path, output: Path) -> Tuple[List[Path], List[Path], Counter]:
    media_paths: List[Path] = []
    json_paths: List[Path] = []
    ignored = Counter()
    for root, dirs, files in os.walk(input_dir):
        root_path = Path(root)
        keep_dirs = []
        for dirname in dirs:
            dir_path = root_path / dirname
            if should_prune_dir(dir_path, output):
                ignored["pruned_dirs"] += 1
            else:
                keep_dirs.append(dirname)
        dirs[:] = keep_dirs

        for filename in files:
            lower = filename.lower()
            path = root_path / filename
            if filename == ".DS_Store" or filename.startswith("._"):
                ignored["apple_junk_files"] += 1
                continue
            if lower == "memories_history.json":
                json_paths.append(path)
                continue
            if MEDIA_RE.match(filename):
                media_paths.append(path)
                continue
            if lower.endswith((".html", ".json")):
                ignored["html_or_other_json"] += 1
            else:
                ignored["irrelevant_files"] += 1
    return media_paths, json_paths, ignored


def stat_media(path: Path, input_dir: Path) -> MediaRecord:
    match = MEDIA_RE.match(path.name)
    if not match:
        raise ValueError(f"Not a Snapchat media filename: {path}")
    stat = path.stat()
    birth_ts = getattr(stat, "st_birthtime", stat.st_mtime)
    birth_utc = datetime.fromtimestamp(birth_ts, tz=timezone.utc).replace(microsecond=0)
    mtime_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).replace(microsecond=0)
    ext = match.group("ext").lower()
    actual_ext = normalize_extension_from_signature(path, ext)
    role = match.group("role").lower()
    media_type = "Video" if actual_ext == "mp4" else "Image"
    return MediaRecord(
        source=path,
        relative_source=str(path.relative_to(input_dir)),
        original_name=path.name,
        filename_date=match.group("date"),
        uuid=match.group("uuid").upper(),
        role=role,
        ext=actual_ext,
        size=stat.st_size,
        mtime_utc=mtime_utc,
        birth_utc=birth_utc,
        capture_utc=birth_utc,
        media_type=media_type,
        utc_timestamp=birth_utc.strftime(SNAPCHAT_DATE_FORMAT),
    )


def normalize_extension_from_signature(path: Path, ext: str) -> str:
    ext = "jpg" if ext == "jpeg" else ext
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except Exception:
        return ext
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return ext


def load_media(paths: List[Path], input_dir: Path, workers: int) -> List[MediaRecord]:
    records: List[MediaRecord] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(stat_media, path, input_dir) for path in paths]
        for future in as_completed(futures):
            records.append(future.result())
    records.sort(key=lambda item: (item.capture_utc, item.uuid, item.role, item.ext, item.relative_source))
    return records


def parse_location(text: str) -> Tuple[str, str]:
    if not text:
        return "", ""
    match = LOCATION_RE.search(text)
    if not match:
        return "", ""
    return match.group("lat"), match.group("lon")


def parse_json_date(text: str) -> Optional[datetime]:
    try:
        return datetime.strptime(text, SNAPCHAT_DATE_FORMAT).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def read_saved_media(path: Path) -> Optional[List[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"warning: could not parse JSON {path}: {exc}", file=sys.stderr)
        return None
    saved = data.get("Saved Media") if isinstance(data, dict) else None
    if not isinstance(saved, list):
        return None
    return [row for row in saved if isinstance(row, dict)]


def load_json_rows(json_paths: List[Path], input_dir: Path) -> List[JsonRow]:
    best_path: Optional[Path] = None
    best_saved: List[dict] = []
    for path in sorted(json_paths):
        saved = read_saved_media(path)
        if saved is None:
            continue
        if len(saved) > len(best_saved):
            best_path = path
            best_saved = saved

    rows: List[JsonRow] = []
    if best_path is None:
        return rows

    for raw in best_saved:
        date_text = str(raw.get("Date", "")).strip()
        media_type = str(raw.get("Media Type", "")).strip()
        if not date_text or not media_type:
            continue
        location = str(raw.get("Location", "")).strip()
        lat, lon = parse_location(location)
        rows.append(
            JsonRow(
                date_text=date_text,
                media_type=media_type,
                location_text=location,
                latitude=lat,
                longitude=lon,
                raw=raw,
                source_json=str(best_path.relative_to(input_dir)),
            )
        )
    return sorted(rows, key=lambda row: (row.date_text, row.media_type, row.location_text), reverse=True)


def file_hash(path: Path) -> str:
    digest = hashlib.blake2b(digest_size=32)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_records(records: Iterable[MediaRecord], workers: int) -> None:
    todo = [record for record in records if not record.content_hash]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_map = {pool.submit(file_hash, record.source): record for record in todo}
        for future in as_completed(future_map):
            future_map[future].content_hash = future.result()


def select_unique_records(
    records: List[MediaRecord], verify_hashes: str, workers: int
) -> Tuple[List[MediaRecord], List[dict], List[dict]]:
    duplicates: List[dict] = []
    conflicts: List[dict] = []
    selected: List[MediaRecord] = []

    if verify_hashes == "all":
        hash_records(records, workers)

    by_name: Dict[str, List[MediaRecord]] = defaultdict(list)
    for record in records:
        by_name[record.original_name].append(record)

    for original_name, group in sorted(by_name.items()):
        if len(group) == 1 and verify_hashes != "all":
            chosen = group[0]
            chosen.selected = True
            chosen.status = "selected"
            selected.append(chosen)
            continue

        if verify_hashes == "none":
            by_size: Dict[int, List[MediaRecord]] = defaultdict(list)
            for record in group:
                by_size[record.size].append(record)
            for size, size_group in sorted(by_size.items()):
                chosen = sorted(size_group, key=lambda item: item.relative_source)[0]
                chosen.selected = True
                chosen.status = "selected"
                selected.append(chosen)
                for dup in sorted(size_group, key=lambda item: item.relative_source)[1:]:
                    dup.duplicate_of = chosen.relative_source
                    dup.status = "duplicate"
                    duplicates.append(duplicate_row(dup, chosen, "same filename and size; hashes disabled"))
                if len(by_size) > 1:
                    conflicts.append(conflict_row(original_name, size_group, f"size-{size}"))
            continue

        hash_records(group, workers)
        by_hash: Dict[str, List[MediaRecord]] = defaultdict(list)
        for record in group:
            by_hash[record.content_hash].append(record)

        conflict_index = 0
        for content_hash, hash_group in sorted(by_hash.items(), key=lambda item: item[0]):
            chosen = sorted(hash_group, key=lambda item: item.relative_source)[0]
            chosen.selected = True
            chosen.status = "selected"
            if len(by_hash) > 1:
                conflict_index += 1
                chosen.conflict_group = f"{original_name}#{conflict_index:02d}"
                conflicts.append(conflict_row(original_name, hash_group, chosen.conflict_group))
            selected.append(chosen)
            for dup in sorted(hash_group, key=lambda item: item.relative_source)[1:]:
                dup.duplicate_of = chosen.relative_source
                dup.status = "duplicate"
                duplicates.append(duplicate_row(dup, chosen, "same filename and content hash"))

    if verify_hashes == "all":
        selected, cross_name_duplicates = dedupe_all_hashes(selected)
        duplicates.extend(cross_name_duplicates)

    selected.sort(key=lambda item: (item.capture_utc, item.uuid, item.role, item.ext, item.relative_source))
    return selected, duplicates, conflicts


def dedupe_all_hashes(records: List[MediaRecord]) -> Tuple[List[MediaRecord], List[dict]]:
    duplicates: List[dict] = []
    selected: List[MediaRecord] = []
    by_hash: Dict[Tuple[str, int], List[MediaRecord]] = defaultdict(list)
    for record in records:
        by_hash[(record.content_hash, record.size)].append(record)
    for _, group in by_hash.items():
        chosen = sorted(group, key=lambda item: item.relative_source)[0]
        selected.append(chosen)
        for dup in sorted(group, key=lambda item: item.relative_source)[1:]:
            if dup is chosen:
                continue
            dup.selected = False
            dup.duplicate_of = chosen.relative_source
            dup.status = "duplicate"
            duplicates.append(duplicate_row(dup, chosen, "same content hash across filenames"))
    return selected, duplicates


def duplicate_row(duplicate: MediaRecord, chosen: MediaRecord, reason: str) -> dict:
    return {
        "duplicate_source": duplicate.relative_source,
        "kept_source": chosen.relative_source,
        "original_name": duplicate.original_name,
        "size": duplicate.size,
        "hash": duplicate.content_hash,
        "reason": reason,
    }


def conflict_row(original_name: str, group: List[MediaRecord], conflict_group: str) -> dict:
    return {
        "conflict_group": conflict_group,
        "original_name": original_name,
        "sources": " | ".join(sorted(item.relative_source for item in group)),
        "sizes": " | ".join(str(item.size) for item in sorted(group, key=lambda item: item.relative_source)),
        "hashes": " | ".join(item.content_hash for item in sorted(group, key=lambda item: item.relative_source)),
    }


def attach_json_metadata(records: List[MediaRecord], json_rows: List[JsonRow]) -> Tuple[List[dict], List[dict]]:
    by_key: Dict[Tuple[str, str], Deque[JsonRow]] = defaultdict(deque)
    for row in json_rows:
        by_key[(row.date_text, row.media_type)].append(row)

    main_by_uuid: Dict[Tuple[str, str], MediaRecord] = {}
    unmatched_media: List[dict] = []
    for record in records:
        if record.role != "main":
            continue
        key = (record.utc_timestamp, record.media_type)
        if by_key[key]:
            row = by_key[key].popleft()
            row.matched = True
            record.json_matched = True
            record.json_location = row.location_text
            record.latitude = row.latitude
            record.longitude = row.longitude
        else:
            unmatched_media.append(media_report_row(record, "no exact JSON row for timestamp and media type"))
        main_by_uuid[(record.filename_date, record.uuid)] = record

    for record in records:
        if record.role != "overlay":
            continue
        main = main_by_uuid.get((record.filename_date, record.uuid))
        if main:
            record.capture_utc = main.capture_utc
            record.utc_timestamp = main.utc_timestamp
            record.json_matched = main.json_matched
            record.json_location = main.json_location
            record.latitude = main.latitude
            record.longitude = main.longitude
        else:
            unmatched_media.append(media_report_row(record, "overlay has no matching main media"))

    unmatched_json = []
    for row in json_rows:
        if not row.matched:
            unmatched_json.append(
                {
                    "date_utc": row.date_text,
                    "media_type": row.media_type,
                    "location": row.location_text,
                    "latitude": row.latitude,
                    "longitude": row.longitude,
                    "source_json": row.source_json,
                }
            )
    return unmatched_json, unmatched_media


def media_report_row(record: MediaRecord, reason: str) -> dict:
    return {
        "source": record.relative_source,
        "original_name": record.original_name,
        "role": record.role,
        "media_type": record.media_type,
        "date_utc": record.utc_timestamp,
        "reason": reason,
    }


def assign_targets(records: List[MediaRecord], output: Path, tz) -> None:
    used: Counter = Counter()
    for record in records:
        local_dt = record.capture_utc.astimezone(tz)
        record.local_timestamp = local_dt.isoformat()
        base_name = f"{local_dt.strftime(DISPLAY_DATE_FORMAT)}_{record.uuid}_{record.role}.{record.ext}"
        rel_dir = Path("media") / local_dt.strftime("%Y") / local_dt.strftime("%m")
        rel_path = rel_dir / base_name
        key = str(rel_path)
        used[key] += 1
        if used[key] > 1 or record.conflict_group:
            stem = rel_path.stem
            suffix = max(1, used[key] - 1)
            rel_path = rel_path.with_name(f"{stem}_conflict-{suffix:02d}{rel_path.suffix}")
            while str(rel_path) in used:
                suffix += 1
                rel_path = rel_path.with_name(f"{stem}_conflict-{suffix:02d}{rel_path.suffix}")
            used[str(rel_path)] += 1
        record.target_path = str(rel_path)


def ensure_clean_work_dir(output: Path, resume: bool) -> Path:
    work = output / ".work"
    if work.exists() and not resume:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    return work


def copy_one(record: MediaRecord, output: Path, work: Path, resume: bool) -> Tuple[str, str]:
    final_path = output / record.target_path
    if resume and final_path.exists() and final_path.stat().st_size == record.size:
        return ("skipped", record.target_path)
    temp_path = work / record.target_path
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(record.source, temp_path)
    os.replace(temp_path, final_path)
    return ("copied", record.target_path)


def copy_records(records: List[MediaRecord], output: Path, workers: int, resume: bool, progress_every: int) -> Counter:
    work = ensure_clean_work_dir(output, resume)
    counts = Counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_map = {pool.submit(copy_one, record, output, work, resume): record for record in records}
        for index, future in enumerate(as_completed(future_map), start=1):
            status, _ = future.result()
            counts[status] += 1
            if progress_every and index % progress_every == 0:
                print(f"copy progress: {index}/{len(records)}")
    return counts


def exif_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(EXIF_DATE_FORMAT)


def local_datetime_for_record(record: MediaRecord) -> datetime:
    if record.local_timestamp:
        try:
            parsed = datetime.fromisoformat(record.local_timestamp)
            if parsed.tzinfo is not None:
                return parsed
        except Exception:
            pass
    return record.capture_utc.astimezone()


def offset_text(dt: datetime) -> str:
    raw = dt.strftime("%z")
    if len(raw) == 5:
        return f"{raw[:3]}:{raw[3:]}"
    return raw


def signed_ref(value: str, positive: str, negative: str) -> str:
    try:
        return positive if float(value) >= 0 else negative
    except Exception:
        return positive


def append_exiftool_args(args: List[str], record: MediaRecord, file_path: Path) -> None:
    utc_text = exif_date(record.capture_utc)
    local_dt = local_datetime_for_record(record)
    local_text = local_dt.strftime(EXIF_DATE_FORMAT)
    local_offset = offset_text(local_dt)
    xmp_local_text = f"{local_text}{local_offset}" if local_offset else local_text
    args.extend(
        [
            "-overwrite_original",
            "-P",
            "-m",
            f"-XMP:CreateDate={xmp_local_text}",
            f"-XMP:ModifyDate={xmp_local_text}",
            f"-XMP:MetadataDate={xmp_local_text}",
            f"-XMP:DateCreated={xmp_local_text}",
            f"-XMP:OriginalDocumentID={record.uuid}",
            f"-XMP:DocumentID={record.uuid}",
            f"-XMP:Source={record.relative_source}",
        ]
    )
    if record.latitude and record.longitude:
        args.extend(
            [
                f"-GPSLatitude={abs(float(record.latitude))}",
                f"-GPSLatitudeRef={signed_ref(record.latitude, 'N', 'S')}",
                f"-GPSLongitude={abs(float(record.longitude))}",
                f"-GPSLongitudeRef={signed_ref(record.longitude, 'E', 'W')}",
                f"-GPSCoordinates={record.latitude} {record.longitude}",
            ]
        )
    if record.ext == "jpg":
        args.extend(
            [
                f"-EXIF:DateTimeOriginal={local_text}",
                f"-EXIF:CreateDate={local_text}",
                f"-EXIF:ModifyDate={local_text}",
                f"-EXIF:OffsetTime={local_offset}",
                f"-EXIF:OffsetTimeOriginal={local_offset}",
                f"-EXIF:OffsetTimeDigitized={local_offset}",
            ]
        )
    elif record.ext == "mp4":
        args.extend(
            [
                f"-QuickTime:CreateDate={utc_text}",
                f"-QuickTime:ModifyDate={utc_text}",
                f"-TrackCreateDate={utc_text}",
                f"-TrackModifyDate={utc_text}",
                f"-MediaCreateDate={utc_text}",
                f"-MediaModifyDate={utc_text}",
            ]
        )
    # Snapchat sometimes stores WebP/RIFF overlay content with a .png filename.
    # Keep overlays on generic XMP/date tags instead of PNG-only chunks so those
    # mislabeled files do not fail the whole batch.
    args.extend([str(file_path), "-execute"])


def run_exiftool_batches(records: List[MediaRecord], output: Path, batch_size: int, progress_every: int) -> None:
    exiftool = find_exiftool()
    if not exiftool:
        raise SystemExit(
            "ExifTool is required for --apply because embedded metadata was requested.\n"
            f"{exiftool_install_hint()}"
        )
    total = len(records)
    for start in range(0, total, batch_size):
        chunk = records[start : start + batch_size]
        per_file_args: List[str] = []
        for record in chunk:
            append_exiftool_args(per_file_args, record, output / record.target_path)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            argfile = Path(handle.name)
            for value in per_file_args:
                handle.write(value)
                handle.write("\n")
        try:
            result = subprocess.run(
                [exiftool, "-@", str(argfile)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"ExifTool failed for batch {start // batch_size + 1}: {result.stderr or result.stdout}"
                )
        finally:
            argfile.unlink(missing_ok=True)
        done = min(start + batch_size, total)
        if progress_every and (done % progress_every == 0 or done == total):
            print(f"metadata progress: {done}/{total}")


class AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class TimeSpec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def set_birthtime_macos(path: Path, dt: datetime) -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        attr_list = AttrList()
        attr_list.bitmapcount = 5
        attr_list.commonattr = 0x00000200  # ATTR_CMN_CRTIME
        ts = TimeSpec(int(dt.timestamp()), 0)
        ret = libc.setattrlist(
            os.fsencode(path),
            ctypes.byref(attr_list),
            ctypes.byref(ts),
            ctypes.sizeof(ts),
            0,
        )
        return ret == 0
    except Exception:
        return False


def set_birthtime_setfile(path: Path, dt: datetime) -> bool:
    setfile = shutil.which("SetFile")
    if not setfile:
        return False
    local = dt.astimezone()
    date_text = local.strftime("%m/%d/%Y %H:%M:%S")
    result = subprocess.run([setfile, "-d", date_text, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def set_filesystem_time(record: MediaRecord, output: Path) -> Tuple[bool, str]:
    path = output / record.target_path
    timestamp = record.capture_utc.timestamp()
    os.utime(path, (timestamp, timestamp))
    birth_ok = set_birthtime_macos(path, record.capture_utc)
    if not birth_ok:
        birth_ok = set_birthtime_setfile(path, record.capture_utc)
    return birth_ok, record.target_path


def set_filesystem_times(records: List[MediaRecord], output: Path, workers: int, progress_every: int) -> Counter:
    counts = Counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_map = {pool.submit(set_filesystem_time, record, output): record for record in records}
        for index, future in enumerate(as_completed(future_map), start=1):
            birth_ok, _ = future.result()
            counts["birthtime_set" if birth_ok else "birthtime_not_set"] += 1
            if progress_every and index % progress_every == 0:
                print(f"filesystem time progress: {index}/{len(records)}")
    return counts


def write_csv(path: Path, rows: List[dict], fieldnames: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["message"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def index_row(record: MediaRecord) -> dict:
    return {
        "target_path": record.target_path,
        "source_path": record.relative_source,
        "original_name": record.original_name,
        "uuid": record.uuid,
        "role": record.role,
        "extension": record.ext,
        "media_type": record.media_type,
        "size": record.size,
        "date_utc": record.utc_timestamp,
        "date_local": record.local_timestamp,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "json_matched": record.json_matched,
        "content_hash": record.content_hash,
        "conflict_group": record.conflict_group,
    }


def write_reports(
    output: Path,
    records: List[MediaRecord],
    duplicates: List[dict],
    conflicts: List[dict],
    unmatched_json: List[dict],
    unmatched_media: List[dict],
    summary: dict,
) -> None:
    metadata = output / "metadata"
    logs = output / "logs"
    rows = [index_row(record) for record in records]
    write_csv(metadata / "snapchat_media_index.csv", rows)
    write_json(metadata / "snapchat_media_index.json", rows)
    write_csv(metadata / "duplicate_files.csv", duplicates)
    write_csv(metadata / "conflicts.csv", conflicts)
    write_csv(metadata / "unmatched_json_rows.csv", unmatched_json)
    write_csv(metadata / "unmatched_media_files.csv", unmatched_media)
    write_json(logs / "run_summary.json", summary)
    lines = [f"{key}: {value}" for key, value in summary.items()]
    (logs / "run_summary.txt").parent.mkdir(parents=True, exist_ok=True)
    (logs / "run_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_readme(tool_dir: Path, output: Path) -> None:
    source = tool_dir / "README.md"
    if source.exists():
        shutil.copy2(source, output / "README.md")


def parse_index_datetime(row: dict) -> datetime:
    date_utc = str(row.get("date_utc", "")).strip()
    parsed = parse_json_date(date_utc)
    if parsed:
        return parsed
    local_text = str(row.get("date_local", "")).strip()
    if local_text:
        return datetime.fromisoformat(local_text).astimezone(timezone.utc).replace(microsecond=0)
    raise ValueError(f"Index row has no parseable timestamp: {row.get('target_path')}")


def composited_target_for_main(main_target: str) -> str:
    rel = Path(main_target)
    parts = list(rel.parts)
    if parts and parts[0] == "media":
        parts[0] = "composited"
    rel = Path(*parts)
    stem = rel.stem
    if stem.endswith("_main"):
        stem = stem[: -len("_main")] + "_composited"
    else:
        stem = stem + "_composited"
    return str(rel.with_name(stem + ".jpg"))


def compose_one_image_pair(
    read_root: Path, write_root: Path, main_row: dict, overlay_row: dict
) -> Tuple[dict, Optional[MediaRecord]]:
    from PIL import Image

    main_rel = str(main_row["target_path"])
    overlay_rel = str(overlay_row["target_path"])
    target_rel = composited_target_for_main(main_rel)
    main_path = read_root / main_rel
    overlay_path = read_root / overlay_rel
    target_path = write_root / target_rel
    target_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with Image.open(main_path) as main_image, Image.open(overlay_path) as overlay_image:
            base = main_image.convert("RGBA")
            overlay = overlay_image.convert("RGBA")
            if overlay.size != base.size:
                overlay = overlay.resize(base.size, Image.Resampling.LANCZOS)
            composed = Image.alpha_composite(base, overlay).convert("RGB")
            save_kwargs = {"quality": 95, "subsampling": 0, "optimize": True}
            if "exif" in main_image.info:
                save_kwargs["exif"] = main_image.info["exif"]
            if "icc_profile" in main_image.info:
                save_kwargs["icc_profile"] = main_image.info["icc_profile"]
            composed.save(target_path, "JPEG", **save_kwargs)
    except Exception as exc:
        target_path.unlink(missing_ok=True)
        return (
            {
                "composited_path": "",
                "main_path": main_rel,
                "overlay_path": overlay_rel,
                "uuid": str(main_row.get("uuid", "")),
                "date_utc": str(main_row.get("date_utc", "")),
                "date_local": str(main_row.get("date_local", "")),
                "status": "skipped",
                "reason": f"overlay could not be opened/composited: {exc}",
            },
            None,
        )

    capture_utc = parse_index_datetime(main_row)
    timestamp = capture_utc.timestamp()
    os.utime(target_path, (timestamp, timestamp))
    set_birthtime_macos(target_path, capture_utc) or set_birthtime_setfile(target_path, capture_utc)

    record = MediaRecord(
        source=main_path,
        relative_source=f"{main_rel} + {overlay_rel}",
        original_name=target_path.name,
        filename_date=str(main_row.get("date_utc", ""))[:10],
        uuid=str(main_row.get("uuid", "")),
        role="composited",
        ext="jpg",
        size=target_path.stat().st_size,
        mtime_utc=capture_utc,
        birth_utc=capture_utc,
        capture_utc=capture_utc,
        media_type="Image",
        target_path=target_rel,
        utc_timestamp=capture_utc.strftime(SNAPCHAT_DATE_FORMAT),
        local_timestamp=str(main_row.get("date_local", "")),
        latitude=str(main_row.get("latitude", "")),
        longitude=str(main_row.get("longitude", "")),
        json_matched=str(main_row.get("json_matched", "")).lower() == "true" or bool(main_row.get("json_matched")),
    )
    return (
        {
            "composited_path": target_rel,
            "main_path": main_rel,
            "overlay_path": overlay_rel,
            "uuid": record.uuid,
            "date_utc": record.utc_timestamp,
            "date_local": record.local_timestamp,
            "status": "created",
        },
        record,
    )


def load_normalized_rows(output: Path, tz) -> List[dict]:
    index_path = output / "metadata" / "snapchat_media_index.json"
    if index_path.exists():
        rows = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise SystemExit(f"Index is not a list: {index_path}")
        return rows

    print(f"No index found at {index_path}; rebuilding pairing info from media filenames.")
    media_root = output / "media"
    rows: List[dict] = []
    if not media_root.exists():
        raise SystemExit(f"Missing media folder: {media_root}")
    for path in media_root.rglob("*"):
        if not path.is_file():
            continue
        match = NORMALIZED_MEDIA_RE.match(path.name)
        if not match:
            continue
        role = match.group("role").lower()
        if role == "composited":
            continue
        ext = match.group("ext").lower()
        if ext == "jpeg":
            ext = "jpg"
        stat = path.stat()
        capture_utc = datetime.fromtimestamp(getattr(stat, "st_birthtime", stat.st_mtime), tz=timezone.utc).replace(
            microsecond=0
        )
        rows.append(
            {
                "target_path": str(path.relative_to(output)),
                "source_path": "",
                "original_name": path.name,
                "uuid": match.group("uuid").upper(),
                "role": role,
                "extension": ext,
                "media_type": "Video" if ext == "mp4" else "Image",
                "size": stat.st_size,
                "date_utc": capture_utc.strftime(SNAPCHAT_DATE_FORMAT),
                "date_local": capture_utc.astimezone(tz).isoformat(),
                "latitude": "",
                "longitude": "",
                "json_matched": "",
                "content_hash": "",
                "conflict_group": "",
            }
        )
    if not rows:
        raise SystemExit(f"No normalized Snapchat media files found under {media_root}")
    return rows


def compose_overlays_from_output(output: Path, tz, workers: int, batch_size: int, progress_every: int) -> dict:
    rows = load_normalized_rows(output, tz)
    compose_work = output / ".compose-work"
    final_composited = output / "composited"
    if compose_work.exists():
        shutil.rmtree(compose_work)
    compose_work.mkdir(parents=True, exist_ok=True)

    main_by_uuid: Dict[Tuple[str, str], dict] = {}
    overlay_by_uuid: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    skipped: List[dict] = []

    for row in rows:
        role = str(row.get("role", ""))
        uuid = str(row.get("uuid", ""))
        date_utc = str(row.get("date_utc", ""))[:10]
        key = (date_utc, uuid)
        ext = str(row.get("extension", "")).lower()
        if role == "main" and ext in {"jpg", "jpeg"}:
            main_by_uuid[key] = row
        elif role == "overlay":
            overlay_by_uuid[key].append(row)

    pairs: List[Tuple[dict, dict]] = []
    for key, overlays in sorted(overlay_by_uuid.items()):
        main = main_by_uuid.get(key)
        if not main:
            for overlay in overlays:
                skipped.append(
                    {
                        "overlay_path": overlay.get("target_path", ""),
                        "reason": "no JPG main image; video overlays are preserved but not baked without ffmpeg",
                    }
                )
            continue
        for overlay in overlays:
            pairs.append((main, overlay))

    report_rows: List[dict] = []
    metadata_records: List[MediaRecord] = []
    if pairs:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            future_map = {
                pool.submit(compose_one_image_pair, output, compose_work, main, overlay): (main, overlay)
                for main, overlay in pairs
            }
            for index, future in enumerate(as_completed(future_map), start=1):
                row, record = future.result()
                if record:
                    report_rows.append(row)
                    metadata_records.append(record)
                else:
                    skipped.append(row)
                if progress_every and index % progress_every == 0:
                    print(f"compose progress: {index}/{len(pairs)}")

        if metadata_records:
            print(f"Embedding metadata into {len(metadata_records)} composited image files")
            run_exiftool_batches(metadata_records, compose_work, batch_size, progress_every)
            set_filesystem_times(metadata_records, compose_work, workers, progress_every)

    if final_composited.exists():
        shutil.rmtree(final_composited)
    if (compose_work / "composited").exists():
        shutil.move(str(compose_work / "composited"), str(final_composited))
    shutil.rmtree(compose_work, ignore_errors=True)

    metadata = output / "metadata"
    write_csv(metadata / "composited_overlays.csv", sorted(report_rows, key=lambda row: row["composited_path"]))
    write_csv(metadata / "uncomposited_overlays.csv", skipped)
    summary = {
        "mode": "compose-only",
        "output": str(output),
        "image_overlay_pairs_composited": len(report_rows),
        "overlays_not_composited": len(skipped),
        "workers": workers,
        "exiftool_batch_size": batch_size,
    }
    write_json(output / "logs" / "compose_summary.json", summary)
    (output / "logs" / "compose_summary.txt").write_text(
        "\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n",
        encoding="utf-8",
    )
    return summary


def merge_composited_into_media(output: Path) -> dict:
    from PIL import Image

    media = output / "media"
    composited = output / "composited"
    metadata = output / "metadata"
    if not composited.exists():
        raise SystemExit(f"Missing composited folder to merge: {composited}")

    rows: List[dict] = []
    errors: List[dict] = []
    comps = sorted(composited.rglob("*_composited.jpg"))
    for comp in comps:
        rel = comp.relative_to(composited)
        main_rel = rel.with_name(rel.name.replace("_composited.jpg", "_main.jpg"))
        main = media / main_rel
        overlays = [
            media / rel.with_name(rel.name.replace("_composited.jpg", "_overlay.png")),
            media / rel.with_name(rel.name.replace("_composited.jpg", "_overlay.webp")),
        ]
        existing_overlays = [path for path in overlays if path.exists()]
        if not main.exists() or not existing_overlays:
            errors.append(
                {
                    "composited_path": str(comp.relative_to(output)),
                    "main_path": str(main.relative_to(output)) if main.exists() else str(main),
                    "error": "missing matching media main or overlay",
                }
            )
            continue
        try:
            with Image.open(comp) as image:
                image.verify()
        except Exception as exc:
            errors.append(
                {
                    "composited_path": str(comp.relative_to(output)),
                    "main_path": str(main.relative_to(output)),
                    "error": f"composited image failed validation: {exc}",
                }
            )

    metadata.mkdir(parents=True, exist_ok=True)
    if errors:
        write_csv(metadata / "merge_errors.csv", errors)
        raise SystemExit(f"Aborting merge: {len(errors)} validation errors written to {metadata / 'merge_errors.csv'}")

    for comp in comps:
        rel = comp.relative_to(composited)
        main_rel = rel.with_name(rel.name.replace("_composited.jpg", "_main.jpg"))
        main = media / main_rel
        overlays = [
            media / rel.with_name(rel.name.replace("_composited.jpg", "_overlay.png")),
            media / rel.with_name(rel.name.replace("_composited.jpg", "_overlay.webp")),
        ]
        existing_overlays = [path for path in overlays if path.exists()]
        old_main_size = main.stat().st_size
        new_main_size = comp.stat().st_size
        os.replace(comp, main)
        removed = []
        for overlay in existing_overlays:
            removed.append(str(overlay.relative_to(output)))
            overlay.unlink()
        rows.append(
            {
                "final_main_path": str(main.relative_to(output)),
                "removed_overlay_paths": " | ".join(removed),
                "old_main_size": old_main_size,
                "new_main_size": new_main_size,
                "status": "merged",
            }
        )

    write_csv(metadata / "merged_composited_overlays.csv", rows)
    shutil.rmtree(composited, ignore_errors=True)
    for root in sorted(media.rglob("*"), reverse=True):
        if root.is_dir():
            try:
                root.rmdir()
            except OSError:
                pass
    return {
        "merged_composited_files": len(rows),
        "removed_successful_overlay_files": len(rows),
        "report": str(metadata / "merged_composited_overlays.csv"),
    }


def ffprobe_video_info(ffprobe: str, path: Path) -> Tuple[int, int, float]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"No video stream found in {path}")
    duration_text = streams[0].get("duration") or data.get("format", {}).get("duration") or "0"
    duration = float(duration_text)
    if duration <= 0:
        raise RuntimeError(f"No usable video duration found in {path}")
    return int(streams[0]["width"]), int(streams[0]["height"]), duration


def validate_video(ffprobe: str, path: Path) -> None:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError(f"Output video has no duration: {path}")


def parse_normalized_local_datetime(path: Path, tz) -> datetime:
    match = NORMALIZED_MEDIA_RE.match(path.name)
    if not match:
        raise ValueError(f"Not a normalized Snapchat filename: {path.name}")
    date_s = match.group("date")
    time_s = match.group("time")
    return datetime.strptime(date_s + " " + time_s, "%Y-%m-%d %H-%M-%S").replace(tzinfo=tz)


def video_overlay_pairs(output: Path) -> Tuple[List[Tuple[Path, Path]], List[dict]]:
    media = output / "media"
    pairs: List[Tuple[Path, Path]] = []
    skipped: List[dict] = []
    for main in sorted(media.rglob("*_main.mp4")):
        stem = main.name[: -len("_main.mp4")]
        candidates = [main.with_name(stem + "_overlay.webp"), main.with_name(stem + "_overlay.png")]
        existing = [candidate for candidate in candidates if candidate.exists()]
        if len(existing) == 1:
            pairs.append((main, existing[0]))
        elif len(existing) > 1:
            skipped.append(
                {
                    "main_path": str(main.relative_to(output)),
                    "overlay_path": " | ".join(str(path.relative_to(output)) for path in existing),
                    "reason": "multiple exact-stem overlays found",
                }
            )
    return pairs, skipped


def merge_video_overlays_into_media(output: Path, tz, dry_run: bool, progress_every: int) -> dict:
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if not dry_run and (not ffmpeg or not ffprobe):
        raise SystemExit(f"ffmpeg and ffprobe are required for video overlay merging. {ffmpeg_install_hint()}")

    metadata = output / "metadata"
    logs = output / "logs"
    metadata.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    work = output / ".video-compose-work"
    pairs, skipped = video_overlay_pairs(output)
    pair_rows = [
        {
            "main_path": str(main.relative_to(output)),
            "overlay_path": str(overlay.relative_to(output)),
            "status": "planned",
            "reason": "exact same directory, timestamp, UUID, and stem",
        }
        for main, overlay in pairs
    ]
    write_csv(metadata / "video_overlay_pairs.csv", pair_rows + skipped)
    if dry_run:
        summary = {
            "mode": "video-dry-run",
            "exact_video_overlay_pairs": len(pairs),
            "skipped_video_overlay_candidates": len(skipped),
            "pair_report": str(metadata / "video_overlay_pairs.csv"),
        }
        write_json(logs / "video_overlay_summary.json", summary)
        (logs / "video_overlay_summary.txt").write_text(
            "\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n",
            encoding="utf-8",
        )
        return summary

    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    merged: List[dict] = []
    errors: List[dict] = []
    metadata_records: List[MediaRecord] = []

    try:
        for index, (main, overlay) in enumerate(pairs, start=1):
            try:
                width, height, duration = ffprobe_video_info(ffprobe, main)
                temp = work / main.relative_to(output)
                temp.parent.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-threads",
                        "2",
                        "-i",
                        str(main),
                        "-loop",
                        "1",
                        "-i",
                        str(overlay),
                        "-filter_complex",
                        f"[1:v]format=rgba,scale={width}:{height}[ov];[0:v][ov]overlay=0:0:format=auto,format=yuv420p[v]",
                        "-map",
                        "[v]",
                        "-map",
                        "0:a?",
                        "-t",
                        f"{duration:.6f}",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "20",
                        "-threads",
                        "2",
                        "-c:a",
                        "copy",
                        "-map_metadata",
                        "0",
                        "-movflags",
                        "+faststart",
                        "-shortest",
                        str(temp),
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr or result.stdout)
                validate_video(ffprobe, temp)
                old_size = main.stat().st_size
                new_size = temp.stat().st_size
                os.replace(temp, main)
                overlay.unlink()
                local_dt = parse_normalized_local_datetime(main, tz)
                capture_utc = local_dt.astimezone(timezone.utc).replace(microsecond=0)
                stat = main.stat()
                metadata_records.append(
                    MediaRecord(
                        source=main,
                        relative_source=str(main.relative_to(output)),
                        original_name=main.name,
                        filename_date=local_dt.strftime("%Y-%m-%d"),
                        uuid=NORMALIZED_MEDIA_RE.match(main.name).group("uuid").upper(),  # type: ignore[union-attr]
                        role="main",
                        ext="mp4",
                        size=stat.st_size,
                        mtime_utc=capture_utc,
                        birth_utc=capture_utc,
                        capture_utc=capture_utc,
                        media_type="Video",
                        target_path=str(main.relative_to(output)),
                        utc_timestamp=capture_utc.strftime(SNAPCHAT_DATE_FORMAT),
                        local_timestamp=local_dt.isoformat(),
                    )
                )
                merged.append(
                    {
                        "main_path": str(main.relative_to(output)),
                        "removed_overlay_path": str(overlay.relative_to(output)),
                        "old_main_size": old_size,
                        "new_main_size": new_size,
                        "status": "merged",
                    }
                )
            except Exception as exc:
                errors.append(
                    {
                        "main_path": str(main.relative_to(output)),
                        "overlay_path": str(overlay.relative_to(output)),
                        "status": "skipped",
                        "reason": str(exc).replace("\n", " ")[:1000],
                    }
                )
            if progress_every and index % progress_every == 0:
                print(f"video compose progress: {index}/{len(pairs)}", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if metadata_records:
        print(f"Embedding metadata into {len(metadata_records)} merged video files")
        run_exiftool_batches(metadata_records, output, batch_size=150, progress_every=progress_every)
        set_filesystem_times(metadata_records, output, workers=2, progress_every=progress_every)

    write_csv(metadata / "merged_video_overlays.csv", merged)
    write_csv(metadata / "unmerged_video_overlays.csv", skipped + errors)
    summary = {
        "mode": "merge-video-overlays",
        "exact_video_overlay_pairs": len(pairs),
        "merged_video_overlays": len(merged),
        "unmerged_video_overlays": len(skipped) + len(errors),
        "merged_report": str(metadata / "merged_video_overlays.csv"),
        "unmerged_report": str(metadata / "unmerged_video_overlays.csv"),
    }
    write_json(logs / "video_overlay_summary.json", summary)
    (logs / "video_overlay_summary.txt").write_text(
        "\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n",
        encoding="utf-8",
    )
    return summary


def print_summary(summary: dict) -> None:
    print("\nSnapchat normalization summary")
    print("=" * 31)
    for key, value in summary.items():
        print(f"{key}: {value}")


def apply_low_impact_defaults(args: argparse.Namespace) -> None:
    if not args.low_impact:
        return
    args.workers = min(args.workers, 2)
    args.exiftool_batch_size = min(args.exiftool_batch_size, 150)
    try:
        os.nice(10)
    except Exception:
        pass


def build_summary(
    args: argparse.Namespace,
    scan: ScanResult,
    records: List[MediaRecord],
    selected: List[MediaRecord],
    duplicates: List[dict],
    conflicts: List[dict],
    unmatched_json: List[dict],
    unmatched_media: List[dict],
    copy_counts: Optional[Counter] = None,
    time_counts: Optional[Counter] = None,
) -> dict:
    role_counts = Counter(record.role for record in selected)
    media_type_counts = Counter(record.media_type for record in selected if record.role == "main")
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "input": str(args.input.resolve()),
        "output": str((args.output or args.input / "normalized-output").resolve()),
        "timezone": args.timezone,
        "scanned_media_files": len(scan.media),
        "ignored_items": dict(scan.ignored_counts),
        "json_files": len(scan.json_files),
        "json_rows": len(scan.json_rows),
        "unique_output_media_files": len(selected),
        "main_media_files": role_counts.get("main", 0),
        "overlay_files": role_counts.get("overlay", 0),
        "image_main_files": media_type_counts.get("Image", 0),
        "video_main_files": media_type_counts.get("Video", 0),
        "duplicate_files": len(duplicates),
        "conflict_groups": len(conflicts),
        "unmatched_json_rows": len(unmatched_json),
        "unmatched_media_files": len(unmatched_media),
        "verify_hashes": args.verify_hashes,
        "workers": args.workers,
    }
    if copy_counts:
        summary["copy_counts"] = dict(copy_counts)
    if time_counts:
        summary["filesystem_time_counts"] = dict(time_counts)
    return summary


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    apply_low_impact_defaults(args)
    input_dir = args.input.expanduser().resolve()
    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"Input folder does not exist or is not a directory: {input_dir}")
    output = (args.output or input_dir / "normalized-output").expanduser().resolve()
    tz = load_timezone(args.timezone)

    preflight(args, input_dir, output, tz)
    if args.check:
        print("\nPreflight passed.")
        return 0

    if args.video_dry_run or args.merge_video_overlays:
        print(f"Scanning exact video overlay pairs in normalized output: {output}")
        summary = merge_video_overlays_into_media(output, tz, dry_run=args.video_dry_run, progress_every=args.progress_every)
        print_summary(summary)
        return 0

    if args.compose_only:
        print(f"Compositing overlays from existing normalized output: {output}")
        summary = compose_overlays_from_output(output, tz, args.workers, args.exiftool_batch_size, args.progress_every)
        if args.merge_composited_into_media:
            summary["merge_summary"] = merge_composited_into_media(output)
        print_summary(summary)
        return 0

    print(f"Scanning {input_dir}")
    media_paths, json_paths, ignored = discover_paths(input_dir, output)
    media = load_media(media_paths, input_dir, args.workers)
    json_rows = load_json_rows(json_paths, input_dir)
    scan = ScanResult(media=media, json_rows=json_rows, ignored_counts=ignored, json_files=[str(p) for p in json_paths])

    selected, duplicates, conflicts = select_unique_records(media, args.verify_hashes, args.workers)
    unmatched_json, unmatched_media = attach_json_metadata(selected, json_rows)
    assign_targets(selected, output, tz)

    copy_counts: Optional[Counter] = None
    time_counts: Optional[Counter] = None

    if args.apply:
        output.mkdir(parents=True, exist_ok=True)
        print(f"Copying {len(selected)} unique media files to {output}")
        copy_counts = copy_records(selected, output, args.workers, args.resume, args.progress_every)
        print("Embedding metadata with ExifTool")
        run_exiftool_batches(selected, output, args.exiftool_batch_size, args.progress_every)
        print("Setting filesystem timestamps")
        time_counts = set_filesystem_times(selected, output, args.workers, args.progress_every)
        copy_readme(Path(__file__).resolve().parent, output)
    else:
        print("Dry-run only: no files copied and no metadata written. Add --apply to normalize.")

    summary = build_summary(
        args, scan, media, selected, duplicates, conflicts, unmatched_json, unmatched_media, copy_counts, time_counts
    )
    if args.apply:
        write_reports(output, selected, duplicates, conflicts, unmatched_json, unmatched_media, summary)
        if args.compose_overlays:
            compose_summary = compose_overlays_from_output(output, tz, args.workers, args.exiftool_batch_size, args.progress_every)
            if args.merge_composited_into_media:
                compose_summary["merge_summary"] = merge_composited_into_media(output)
            summary["compose_summary"] = compose_summary
            write_json(output / "logs" / "run_summary.json", summary)
        work = output / ".work"
        if work.exists():
            shutil.rmtree(work)
    else:
        # Dry-runs still get a useful lightweight report in stdout only, avoiding output mutations.
        pass
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
