# PhotoRescue — deep photo recovery for Windows 10/11

Read-only recovery of photos and videos from a disk, SD card, USB stick or disk image.
Pure Python 3.8+ (stdlib only) plus a PowerShell launcher. No installs, no cloud, nothing
is written to the damaged disk.

## Before you do anything else

1. **Stop using the disk.** Every write can overwrite a deleted photo permanently.
2. Do **not** run `chkdsk /f`, do not let Windows "repair" or format it, do not defragment.
3. Recover **to a different physical disk** — the tool refuses otherwise (`--force` overrides).
4. If the drive makes clicking noises or keeps disconnecting, stop and image it first
   (see *Imaging a dying drive* below), then recover from the image file.

## Run it

Copy the `photorescue` folder to the Windows machine, then in **PowerShell (Run as administrator)**:

```powershell
cd C:\path\to\photorescue
powershell -ExecutionPolicy Bypass -File .\Run-PhotoRescue.ps1
```

It elevates itself, checks Python, lists your disks, asks for source + destination and runs
all four phases. Non-interactive form:

```powershell
.\Run-PhotoRescue.ps1 -Source 1 -Out E:\Recovered -Phases sweep,bin,vss,carve
```

Or call the engine directly:

```powershell
py -3 photorescue.py --list
py -3 photorescue.py --source \\.\PhysicalDrive1 --out E:\Recovered --phases carve
py -3 photorescue.py --source D: --out E:\Recovered --phases sweep,bin,vss,carve
py -3 photorescue.py --source E:\disk.img --out E:\Recovered --phases carve --resume
```

If Python is missing: `winget install -e --id Python.Python.3.12` (tick *Add to PATH*).

### "cannot be loaded ... not digitally signed" (UnauthorizedAccess)

That is PowerShell's execution policy refusing an unsigned script — it is unrelated to being
Administrator. Any one of these fixes it:

```powershell
# a) double-click / run the shim, which bypasses the policy for that process only
.\Run-PhotoRescue.cmd -Source 1 -Out E:\Recovered -Phases sweep,bin,vss,carve

# b) put the flag BEFORE -File (it does nothing if you just type .\Run-PhotoRescue.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-PhotoRescue.ps1 -Source 1 -Out E:\Recovered

# c) clear the Mark of the Web (set when the folder came from a download or USB), session-only policy
Unblock-File .\Run-PhotoRescue.ps1
Set-ExecutionPolicy -Scope Process Bypass -Force

# d) ignore the wrapper completely - it is only a convenience launcher
py -3 photorescue.py --source 1 --out E:\Recovered --phases sweep,bin,vss,carve
```

Use `-Scope Process`, not a machine-wide policy change.

## What "complete sweep" runs

| Phase | What it recovers | Needs |
|---|---|---|
| `sweep` | Photos still on the filesystem, including hidden/system files, caches, temp dirs, and `$R…` Recycle Bin payloads | mounted volume (`--source D:`) |
| `bin`   | Recycle Bin items with their **original filename and deletion date**, from the `$I` metadata records | drive letter |
| `vss`   | The same sweep inside every **Volume Shadow Copy** — often finds photos deleted weeks ago | admin, `vssadmin` snapshots exist |
| `carve` | **Deep scan.** Reads every sector of the raw device and rebuilds files from their signatures — works on deleted, formatted, RAW/unmountable, and repartitioned disks | admin, raw device |

`carve` is the one that matters after a delete or a format; it ignores the filesystem entirely.
For a whole-disk deep scan point it at `\\.\PhysicalDrive<N>` (covers every partition plus
unallocated space), not at a drive letter.

## Formats carved

- **JPEG** — full marker + entropy-stream walk, so the end of file is exact (no truncation, no
  embedded-thumbnail duplicates)
- **PNG** — chunk walk to `IEND`
- **GIF** 87a/89a — block walk to the trailer
- **HEIC / HEIF / AVIF / CR3 / MP4 / MOV / 3GP** — ISO-BMFF box walk
- **TIFF and raw photos** — DNG, CR2, NEF, ARW, ORF, RW2, PEF (IFD/SubIFD walk, takes the
  furthest referenced strip/tile), plus Fujifilm **RAF**
- **WebP / AVI** (RIFF), **PSD**, **BMP** (strict header validation)

Every file is length-accurate, not "fixed size blob" — carved output opens cleanly.

## Output

```
E:\Recovered\
  JPG\2019-07\20190712_181233_off0003a9c00000.jpg   <- named from EXIF DateTimeOriginal
  JPG\off0004d1200000.jpg                           <- no EXIF: named by disk offset
  HEIC\ PNG\ TIF\ CR2\ MP4\ ...
  _photorescue_manifest.csv    every file: source, byte offset, size, sha1, EXIF date
  _photorescue_hashes.txt      dedup ledger (drives --resume)
  _photorescue_state.json      scan position for --resume
```

- Duplicates are dropped by SHA-1, so re-running or overlapping phases never doubles up.
- File timestamps are set from EXIF when present, so Windows Photos sorts them correctly.
- `--no-organize` to skip the `YYYY-MM` subfolders.

## Options

```
--source      D:  |  \\.\PhysicalDrive1  |  1  |  C:\image.img
--out         output folder (must be on another disk)
--phases      sweep,bin,vss,carve            (default: carve)
--types       jpg,png,gif,bmp,psd,raw,heic,video   or  all   (default: all)
--min-size    ignore carved files smaller than N bytes (default 16384 — raise to 65536
              to skip icons/thumbnails, lower to 4096 to catch small pictures)
--skip-video  don't recover mp4/mov/avi (much faster, much less output)
--start/--end byte range, for scanning one partition of a disk
--block       read block size in MB (default 16; 64 is faster on healthy SSDs)
--resume      continue an interrupted scan into the same --out
--force       bypass the same-disk safety check
--verbose     print each recovered file
```

## Time and space

Carving reads the whole device: ~85 MB/s CPU-side, so it is limited by your disk
(≈1 h/TB on SATA SSD, 3–6 h/TB on an external HDD, longer on a failing one).
Progress, throughput and ETA print live; Ctrl-C saves state and `--resume` picks up where it
stopped (rewinding one block so nothing is missed at the seam).

Reserve output space up to the size of the source — a deep scan finds old overwritten copies
too, so it usually produces more data than you expect.

## Imaging a dying drive first

If the disk is failing, image it once and carve the image (an image read is one pass; carving
directly makes the drive work much harder):

```powershell
# with ddrescue under WSL — best for bad sectors, retries and logs
wsl sudo ddrescue -d -r3 /dev/sdb /mnt/e/disk.img /mnt/e/disk.log
py -3 photorescue.py --source E:\disk.img --out E:\Recovered --phases carve
```

## Notes and limits

- Carving recovers **contents, not filenames or folders** — that metadata lives in the
  filesystem, which is why `sweep`/`bin`/`vss` run first when the volume still mounts.
- Heavily fragmented files may come back partially (a photo split across the disk is
  recovered up to its first fragment boundary). JPEGs like this usually still open with the
  top part of the image intact.
- Bad sectors are zero-filled rather than aborting the scan.
- BitLocker-encrypted volumes must be unlocked first; a locked volume carves to nothing.
- Runs on macOS/Linux too against image files — that is how the carvers were tested
  (7/7 planted JPEG/PNG/GIF/HEIC/TIFF files recovered byte-identical by SHA-1).
