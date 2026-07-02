# EasySnapExport

EasySnapExport turns a messy Snapchat Memories export into one clean, sortable
folder.

Snapchat often gives you many ZIP files. After you unzip them, you get repeated
folders, HTML previews, JSON files, duplicate media, separate overlay files, and
files with dates that are hard to trust. This tool cleans that up.

It creates a new output folder with:

- one deduplicated copy of each memory;
- correct capture date/time in the filename;
- embedded photo/video metadata;
- Finder-friendly creation dates on macOS;
- embedded metadata and sortable modified dates on Windows;
- Snapchat text/image/video overlays merged into the right files when safe;
- CSV reports showing what was merged, skipped, duplicated, or missing.

Your original Snapchat export folder is not edited.

## What You Need

This tool works on macOS and Windows.

The script needs Python 3.9 or newer, ExifTool, ffmpeg, and Pillow.

On macOS, install the requirements once:

```bash
brew install exiftool
brew install ffmpeg
python3 -m pip install Pillow
```

If you do not have Homebrew, install it from:

```text
https://brew.sh
```

On Windows, open PowerShell and install the requirements once:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id OliverBetz.ExifTool
winget install -e --id Gyan.FFmpeg
py -m pip install Pillow
```

After installing ExifTool or ffmpeg on Windows, close PowerShell and open it
again. This lets Windows refresh the command path.

## Step 1: Unzip Snapchat's ZIP Files

Put all unzipped Snapchat export folders inside one parent folder.

Example:

```text
/Users/you/Downloads/Snapchat export/
  mydata~123/
  mydata~123-2/
  mydata~123-3/
  ...
```

Do not point the tool at one inner `memories/` folder. Point it at the parent
folder that contains all the unzipped export folders.

## Step 2: Run a Check First

Open Terminal on macOS or PowerShell on Windows.

macOS example:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --check
```

Real macOS path example:

```bash
python3 normalize_snapchat_export.py "/Users/you/Downloads/Snapchat export" \
  --output "/Users/you/Downloads/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --check
```

Windows path example:

```powershell
py normalize_snapchat_export.py "C:\Users\you\Downloads\Snapchat export" `
  --output "C:\Users\you\Downloads\Snapchat export normalized" `
  --timezone Europe/Amsterdam `
  --check
```

If something is missing, the script tells you what to install.

The examples below use `python3`, which is the usual macOS command. On Windows,
use `py` instead and keep your paths in quotes.

## Step 3: Do a Dry Run

A dry run scans everything but does not copy or edit files.

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam
```

Look for a summary like:

```text
unique_output_media_files: ...
duplicate_files: ...
conflict_groups: ...
unmatched_media_files: ...
```

If `conflict_groups` or `unmatched_media_files` is high, check the reports and
make sure you pointed the script at the right folder.

## Step 4: Create the Clean Folder

This command does the main cleanup and merges still-image overlays into the
right JPG files:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --apply \
  --compose-overlays \
  --merge-composited-into-media \
  --low-impact
```

Use `--low-impact` if you want the computer to stay more usable while the script
runs. It is slower, but gentler.

## Step 5: Merge Video Overlays

Some Snapchat videos have a matching overlay file. Merge those after Step 4:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --merge-video-overlays \
  --low-impact
```

The script is strict here. It only combines a video and overlay when they are in
the same folder and have the exact same timestamp and UUID in the filename.

## What the Output Looks Like

After a successful run:

```text
Snapchat export normalized/
  media/
    2017/
    2018/
    2019/
    ...
  metadata/
  logs/
  README.md
```

Your actual memories are in:

```text
Snapchat export normalized/media/
```

Files are named like this:

```text
YYYY-MM-DD_HH-MM-SS_UUID_main.jpg
YYYY-MM-DD_HH-MM-SS_UUID_main.mp4
```

Example:

```text
2020-08-30_19-04-25_B818AA44-45F0-44ED-AC2A-B874D85324CA_main.mp4
```

The date/time in the filename uses the timezone you pass with `--timezone`.
UTC timestamps are still kept in the metadata reports.

On macOS, EasySnapExport also tries to set the Finder creation date. On Windows,
it sets the embedded media metadata plus the file modified/access time; Windows
Explorer's `Created` column may still show when the cleaned copy was created.

## What Happens to Overlays?

Snapchat often stores the visual overlay separately from the photo or video.
EasySnapExport handles that in three ways:

- If a JPG overlay can be safely applied, the `*_main.jpg` file is replaced by
  the baked image and the separate overlay file is removed.
- If a video overlay can be safely applied, the `*_main.mp4` file is replaced
  by the baked video and the separate overlay file is removed.
- If an overlay is broken, unreadable, or unsafe to match, it is left in
  `media/` and reported.

The tool never guesses. It only merges exact filename matches.

## Reports

Reports live in:

```text
Snapchat export normalized/metadata/
```

Useful files:

```text
snapchat_media_index.csv
duplicate_files.csv
merged_composited_overlays.csv
merged_video_overlays.csv
uncomposited_overlays.csv
unmerged_video_overlays.csv
unmatched_json_rows.csv
unmatched_media_files.csv
```

Run summaries live in:

```text
Snapchat export normalized/logs/
```

## If Something Goes Wrong

Run the check command again:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --check
```

Common macOS fixes:

```bash
brew install exiftool
brew install ffmpeg
python3 -m pip install Pillow
```

If Terminal says `python3: command not found`, install Python:

```bash
brew install python
```

Common Windows fixes:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id OliverBetz.ExifTool
winget install -e --id Gyan.FFmpeg
py -m pip install Pillow
```

After installing Windows tools, open a new PowerShell window and run the check
again. If `ffmpeg` is still not found, Windows did not add it to `PATH`; reinstall
with winget or add the ffmpeg `bin` folder to your Windows `PATH`.

If the script says it found no Snapchat media files, you probably selected the
wrong folder. Choose the parent folder that contains all unzipped `mydata...`
folders.

If video merging feels slow, that is normal. Videos must be re-encoded. Keep
`--low-impact` on if you want the computer to stay responsive.

## Resume a Run

If the main normalization step was interrupted, run:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --apply \
  --resume \
  --low-impact
```

Then rerun the overlay steps if needed.

## Settings You Can Use

Most people only need the commands above. This section explains every setting in
plain language.

### Folder Settings

`input`

The first path after the script name. This is the folder that contains all your
unzipped Snapchat export folders.

```bash
python3 normalize_snapchat_export.py "/Users/you/Downloads/Snapchat export"
```

`--output "/path/to/output"`

Where the clean folder should be created. If you skip this, the script creates:

```text
<input>/normalized-output/
```

Recommended: set it yourself so it is obvious where the result goes.

```bash
--output "/Users/you/Downloads/Snapchat export normalized"
```

`--timezone Europe/Amsterdam`

The timezone used in the readable filenames. For example, this controls the
`19-04-25` part in:

```text
2020-08-30_19-04-25_UUID_main.mp4
```

Use your own timezone if needed, for example:

```text
America/New_York
Europe/London
Asia/Tokyo
```

UTC timestamps are still stored in the reports.

### Safety Settings

`--check`

Checks whether your computer is ready. It does not copy, edit, or delete media.

Use this first on a new computer.

`--resume`

Use this if the main `--apply` step was interrupted. It skips files that already
exist in the output folder with the expected size.

`--low-impact`

Makes the script gentler on your computer. It uses fewer workers, smaller
metadata batches, and lower process priority where the operating system supports
it. This is recommended for big exports.

### Main Action Settings

`--apply`

Actually creates the clean output folder. Without `--apply`, the script only
does a dry run.

`--compose-overlays`

Creates baked JPG copies where Snapchat image overlays are applied to matching
JPG memories.

Usually use it together with:

```text
--merge-composited-into-media
```

`--compose-only`

Runs only the still-image overlay step on an output folder that already exists.
Use this if you already ran the main cleanup, but later decide you want to merge
JPG overlays.

Usually use it like this:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --compose-only \
  --merge-composited-into-media \
  --low-impact
```

`--merge-composited-into-media`

Takes successful baked image overlays and puts them back into the normal
`media/` folder. The final file stays named like:

```text
YYYY-MM-DD_HH-MM-SS_UUID_main.jpg
```

When this succeeds, the separate matching `*_overlay.*` file is removed.

`--merge-video-overlays`

Bakes exact video overlays into matching MP4 files. This is separate from the
main `--apply` run because video re-encoding can be slow.

The tool only merges exact matches:

```text
same folder
same timestamp
same UUID
same filename stem
```

It does not guess.

`--video-dry-run`

Shows which video overlays would be merged, without changing files. It writes a
report to:

```text
metadata/video_overlay_pairs.csv
```

Use this if you want to inspect video pairings before merging.

### Speed and Deduplication Settings

`--workers N`

Controls how much parallel work the script does for scanning, copying, hashing,
and timestamp updates.

Default:

```text
min(8, CPU count)
```

For a calmer computer:

```bash
--workers 2
```

If you use `--low-impact`, the script automatically uses at most 2 workers.

`--exiftool-batch-size N`

Controls how many files are sent to ExifTool at once when writing metadata.

Default:

```text
750
```

You normally do not need to change this. If your computer feels slow or memory-heavy,
try:

```bash
--exiftool-batch-size 150
```

If you use `--low-impact`, the script automatically uses a smaller batch size.

`--progress-every N`

Controls how often progress is printed.

Example:

```bash
--progress-every 100
```

Lower numbers print more often. Higher numbers print less often.

`--verify-hashes suspected|all|none`

Controls how carefully duplicates are checked.

Recommended default:

```bash
--verify-hashes suspected
```

Modes:

- `suspected`: fast and safe for most exports. Only likely duplicate groups are
  hashed.
- `all`: slowest, strictest. Hashes all media and can detect identical files
  even if names differ.
- `none`: fastest, least strict. Uses filename and file size only.

For stricter but slower duplicate checking:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --verify-hashes all
```

### Recommended Settings

For most people, use this for the main cleanup:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --apply \
  --compose-overlays \
  --merge-composited-into-media \
  --low-impact
```

Then use this for video overlays:

```bash
python3 normalize_snapchat_export.py "/path/to/Snapchat export" \
  --output "/path/to/Snapchat export normalized" \
  --timezone Europe/Amsterdam \
  --merge-video-overlays \
  --low-impact
```

## Safety Notes

- Raw Snapchat export folders are not edited.
- The normalized output folder is safe to delete and recreate.
- Large media files should not be committed to Git.
- The included `.gitignore` blocks common media/export outputs.

## License

No license has been added yet.
