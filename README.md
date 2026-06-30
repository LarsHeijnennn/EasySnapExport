# Snapchat Export Metadata Normalizer

Snapchat Memories exports often arrive as many ZIP files with repeated folders,
HTML preview pages, JSON metadata, AppleDouble junk, and thousands of media files
spread across multiple `mydata.../memories/` directories. This tool turns those
unzipped folders into one clean, sortable media library.

The raw export is never modified. The tool copies unique media files into a new
output folder, embeds capture date/time metadata, sets filesystem timestamps for
Finder sorting, and writes CSV/JSON indexes so the result is auditable.

## Requirements

- Python 3.9 or newer.
- ExifTool for metadata-writing modes.
- Pillow for still-image overlay composition.
- ffmpeg/ffprobe for video overlay merging.

On macOS:

```bash
brew install exiftool
brew install ffmpeg
python3 -m pip install Pillow
```

Plain dry-runs do not require ExifTool, Pillow, or ffmpeg. Applying metadata
requires ExifTool. Image overlay composition requires Pillow. Video overlay
merging requires ffmpeg and ffprobe.

## Basic Usage

Run preflight first on a new Mac:

```bash
python normalize_snapchat_export.py "/path/to/unzipped Snapchat export" \
  --output "/path/to/normalized-output" \
  --timezone Europe/Amsterdam \
  --check
```

This checks Python version, input/output paths, ExifTool, Pillow, ffmpeg/ffprobe,
timezone support, and whether the input folder looks like an unzipped Snapchat
export. If something is missing, it prints the install command.

Dry-run first:

```bash
python normalize_snapchat_export.py "/path/to/unzipped Snapchat export" \
  --output "/path/to/normalized-output" \
  --timezone Europe/Amsterdam
```

Actually create the normalized copy:

```bash
python normalize_snapchat_export.py "/path/to/unzipped Snapchat export" \
  --output "/path/to/normalized-output" \
  --timezone Europe/Amsterdam \
  --apply
```

Create baked image copies after a completed run:

```bash
python normalize_snapchat_export.py "/path/to/unzipped Snapchat export" \
  --output "/path/to/normalized-output" \
  --timezone Europe/Amsterdam \
  --compose-only \
  --merge-composited-into-media \
  --low-impact
```

This reads the existing normalized output index when available. If the index is
missing, it rebuilds the needed pairing information from `media/` filenames and
Finder/filesystem timestamps. With `--merge-composited-into-media`, successful
still-image overlays replace their matching `media/..._main.jpg`, and the
separate successful `media/..._overlay.*` file is removed. A merge report is
written to:

```text
normalized-output/metadata/merged_composited_overlays.csv
```

Unsupported overlays remain in `media/` and are reported in
`metadata/uncomposited_overlays.csv`.

Merge video overlays after a completed run:

```bash
python normalize_snapchat_export.py "/path/to/unzipped Snapchat export" \
  --output "/path/to/normalized-output" \
  --timezone Europe/Amsterdam \
  --merge-video-overlays \
  --low-impact
```

Video merging is exact-stem only: the tool only combines files in the same
folder where `*_main.mp4` has a matching `*_overlay.png` or `*_overlay.webp`
with the exact same timestamp and UUID prefix. Successful videos replace the
matching `media/..._main.mp4`; the matching overlay file is removed only after
the new MP4 validates. Reports are written to:

```text
normalized-output/metadata/video_overlay_pairs.csv
normalized-output/metadata/merged_video_overlays.csv
normalized-output/metadata/unmerged_video_overlays.csv
```

Resume a partially completed run:

```bash
python normalize_snapchat_export.py "/path/to/unzipped Snapchat export" \
  --output "/path/to/normalized-output" \
  --timezone Europe/Amsterdam \
  --apply \
  --resume
```

If `--output` is omitted, the default is:

```text
<input>/normalized-output/
```

## Output

```text
normalized-output/
  media/YYYY/MM/
  metadata/snapchat_media_index.csv
  metadata/snapchat_media_index.json
  metadata/duplicate_files.csv
  metadata/conflicts.csv
  metadata/unmatched_json_rows.csv
  metadata/unmatched_media_files.csv
  logs/run_summary.json
  logs/run_summary.txt
  README.md
```

Normalized filenames use local time in the timezone you choose:

```text
YYYY-MM-DD_HH-MM-SS_UUID_role.ext
```

Example:

```text
2020-08-30_19-04-25_B818AA44-45F0-44ED-AC2A-B874D85324CA_main.mp4
```

UTC timestamps are still preserved in the index files.

## Deduplication

The default dedupe mode is optimized for large macOS exports:

1. Group likely duplicates by original filename.
2. Hash only suspected duplicate/conflict groups.
3. Keep one identical copy.
4. Record skipped duplicates in `metadata/duplicate_files.csv`.

If the same original filename exists with different content, the tool treats it
as a conflict and preserves each version with a `_conflict-XX` suffix. It never
overwrites a different file.

For stricter but slower checking:

```bash
python normalize_snapchat_export.py "/path/to/export" \
  --output "/path/to/output" \
  --verify-hashes all
```

Available modes:

- `--verify-hashes suspected`: fast default; hash duplicate-looking groups only.
- `--verify-hashes all`: hash all media and dedupe identical content globally.
- `--verify-hashes none`: fastest; dedupe by filename and size only.

## Metadata Written

The tool writes embedded metadata through ExifTool in batches:

- JPG: EXIF/XMP date fields and GPS when Snapchat JSON contains location.
- MP4: QuickTime date fields and location where ExifTool supports it.
- PNG overlays: XMP/text timestamp metadata where supported.

It also sets filesystem access/modified time with Python and, on macOS, sets the
Finder creation date through the native filesystem API with `SetFile` fallback.

## Performance Options

```text
--workers N
--exiftool-batch-size N
--verify-hashes suspected|all|none
--progress-every N
--low-impact
--merge-composited-into-media
--merge-video-overlays
--video-dry-run
```

Good defaults are already chosen for large local exports:

- workers: `min(8, CPU count)`
- ExifTool batch size: `750`
- hash mode: `suspected`
- `--low-impact`: reduces workers to at most 2, uses smaller ExifTool batches,
  and lowers process priority on macOS.

## Verification

Check the summary:

```bash
cat "/path/to/normalized-output/logs/run_summary.txt"
```

Inspect Finder-visible dates on macOS:

```bash
mdls -name kMDItemContentCreationDate \
     -name kMDItemFSCreationDate \
     "/path/to/normalized-output/media/YYYY/MM/file.mp4"
```

Inspect embedded metadata:

```bash
exiftool -time:all -gps:all "/path/to/normalized-output/media/YYYY/MM/file.mp4"
```

## Known Snapchat Weirdness

- Snapchat may include duplicate ZIP contents.
- `memories_history.json` can contain rows for media files that are missing from
  the downloaded export.
- Overlay PNGs often pair with a main JPG/MP4 and inherit the main media's
  timestamp/location.
- Baked overlay copies are optional. Use `--compose-only` after a normal run, or
  `--compose-overlays` together with `--apply`, to create separate JPGs where
  transparent overlays are visually applied to JPG main images.
- Video overlays can be baked with `--merge-video-overlays` when ffmpeg is
  installed. The pairing is exact-stem only to avoid combining the wrong files.
- JPG metadata is written with local EXIF time plus timezone offset so macOS
  Finder/Spotlight does not shift the displayed content date.
- Some Snapchat overlay files are named `.png` but contain WebP data. The tool
  detects that by file signature and writes them as `.webp` in the normalized
  output so metadata tools and photo apps handle them correctly.
- HTML preview files are not authoritative; the JSON plus media filesystem
  times are better sources for exact timestamps.
