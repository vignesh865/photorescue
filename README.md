# PhotoRescue — deep photo recovery for Windows 10/11

Read-only recovery of photos and videos from a disk, SD card, USB stick, disk image,
or an **Android phone plugged in over USB**.
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
| `cache` | Photos cached by **browsers, QuickLook, Explorer thumbnails and chat apps**. These are blob files, so it carves *inside* each one. Often the only survivor of a photo deleted from an SSD | opt-in, see below |
| `mft`   | Deleted files **with their original filename and folder path**, straight from the NTFS `$MFT`. Filter to one folder with `--folder` | admin, NTFS volume |
| `vss`   | The same sweep inside every **Volume Shadow Copy** — often finds photos deleted weeks ago | admin, `vssadmin` snapshots exist |
| `carve` | **Deep scan.** Reads every sector of the raw device and rebuilds files from their signatures — works on deleted, formatted, RAW/unmountable, and repartitioned disks | admin, raw device |
| `phone` | An **Android phone over USB**: the MediaStore trash, thumbnail stores, and app caches — see *Recovering from a phone* | adb, USB debugging |

## App caches and thumbnails

When the raw disk is a dead end — an SSD with TRIM, or an encrypted Apple Silicon Mac — a
cached copy or a thumbnail is often the only surviving image. Chrome stores them inside opaque
blobs (`data_1`, `f_00001a`), Windows in `thumbcache_*.db`, macOS in the QuickLook cache, so
this phase carves inside each file rather than copying it.

It will not scan your whole machine unless you say so:

```bash
# one folder at a time (preferred)
python3 photorescue.py --source ~/Library/Caches/Google/Chrome --out ~/Recovered --phases cache

# see what it would find, write nothing
python3 photorescue.py --use-default-caches --out ~/Recovered --phases cache --dry-run

# every known browser/app cache on this machine
python3 photorescue.py --use-default-caches --out ~/Recovered --phases cache
```

Without `--source` or `--use-default-caches` it prints the folders it *would* scan and stops.
That is deliberate: a machine-wide cache sweep pulls images from everywhere — other people's
avatars, web pages, app artwork — and that should always be a decision, never a default.

Cached images are usually smaller than the original (browsers keep display-sized copies, and
thumbnails are tiny), so lower the floor when hunting for them: `--min-size 4096`.

## Recovering from a phone

Plug the phone into the laptop and run:

```powershell
py -3 photorescue.py --source adb --out E:\Recovered --phases phone
```

`py -3 photorescue.py --list` now lists connected phones alongside disks, and
`Run-PhotoRescue.ps1` offers `adb` as a source. Use `--source adb:<serial>` if more
than one phone is attached. Nothing is ever written to the phone.

### What you need first

1. **adb** on the PC: `winget install -e --id Google.PlatformTools`
2. On the phone: *Settings → About phone →* tap **Build number** seven times, then
   *Settings → Developer options → USB debugging* **on**.
3. Plug in, unlock the screen, and accept the **"Allow USB debugging?"** prompt. Tick
   *Always allow from this computer*.
4. Pull down the USB notification and set the mode to **File transfer**, not charge-only.
   A charge-only cable will show the phone as `offline` or not at all.

### What can actually be recovered — and what cannot

A phone is **not a block device**. Windows talks MTP to it, which is a logical file
listing with no sectors behind it, so there is nothing for the deep carver to read.
There is no `\\.\PhysicalDrive` for a phone, and no tool can conjure one.

On top of that, **Android 10+ encrypts user data per-file** (`ro.crypto.type=file`).
Even with root and a `dd` of `/dev/block/userdata`, a carve returns encrypted noise —
the keys live in hardware and are only applied on the file-level view. The `phone`
phase prints the device's `ro.crypto.type` so you can see which case you are in, and
`--phases carve` refuses on an FBE device rather than wasting hours (`--force` overrides).

So what survives a delete, in order of how often it saves the day:

| Source | Why it survives |
|---|---|
| **MediaStore trash** — `.trashed-<expiry>-<name>` | Android 11+ keeps deleted photos ~30 days. Recovered **with their original filename** |
| **Thumbnail stores** — `.thumbnails/.thumbdata*` | One big blob of every thumbnail ever made; entries linger long after the photo is gone. Carved *inside*, so each picture comes out separately |
| **App caches** — Google Photos/Glide, WhatsApp, Telegram, Signal, Instagram | Display-sized copies, often the only survivor |
| **`LOST.DIR`** | Files an fsck orphaned, with no name or extension — identified by content |
| `/storage/XXXX-XXXX` | A physical SD card, scanned as another root |

Thumbnails and cached copies are **smaller than the original** — often only a few KB.
The default `--min-size 16384` throws most of them away, so lower it:

```powershell
py -3 photorescue.py --source adb --out E:\Recovered --phases phone --min-size 4096
```

### Preview, narrow, and limit

```powershell
# see what it would pull, write nothing
py -3 photorescue.py --source adb --out E:\Recovered --phases phone --dry-run --verbose

# only one album, and stop after 20 GB
py -3 photorescue.py --source adb --out E:\Recovered --phases phone --folder Camera --max-pull 20
```

Recovered files keep their real name where one exists (`.trashed-…` prefixes are
stripped), get their date from EXIF or the phone's timestamp, and land in the same
`YYYY-MM` folders, manifest and SHA-1 dedup ledger as a disk scan — so a phone scan and
a disk scan can safely share one `--out`.

### The two cases where a real deep carve still works

- **The microSD card.** Take it out and put it in a card reader: it is an ordinary
  unencrypted FAT/exFAT volume, so the full `carve` phase applies with nothing special:
  `--source \\.\PhysicalDrive2 --phases carve`. This is by far the best outcome.
- **A rooted phone that is not FBE-encrypted** (older Android, or full-disk encryption
  already unlocked). Image it first — one sequential pass is far faster than carving
  over per-read `dd` calls, and leaves an image you can rescan without the phone:

  ```powershell
  py -3 photorescue.py --source adb --out E:\Recovered --phases carve --phone-dump E:\phone.img
  ```

### If nothing comes back

That is a real outcome, not a bug. A photo deleted from an unrooted, encrypted phone
whose trash window has passed and whose thumbnail was evicted is genuinely gone from the
device. Check **Google Photos → Bin** (60 days), the app's own trash (Samsung Gallery
keeps 30 days), and any PC backup — those are the remaining places a copy exists.

## Recovering one folder you remember by name

Carving cannot do this — it is filesystem-blind, it sees only image-shaped bytes, so folder
and file names are gone. NTFS, though, keeps a deleted file's name, its parent folder and its
cluster map in the `$MFT` long after the delete. That is the `mft` phase:

```powershell
# preview first - writes nothing
py -3 photorescue.py --source D: --out E:\Recovered --phases mft --folder "Wedding" --dry-run

# then actually pull them out
py -3 photorescue.py --source D: --out E:\Recovered --phases mft --folder "Wedding"
```

`--folder` matches any folder whose name *contains* the text (case-insensitive) and pulls in
every sub-folder beneath it. Add `--deleted-only` to skip files that still exist. It reads only
the MFT, so it takes minutes, not hours — run it before, or alongside, a full carve; SHA-1
dedup means output can share one `--out` folder safely.

Files whose clusters have already been reused by newer data are reported as
`had their data overwritten` rather than saved as garbage — for those, the deep `carve`
is the remaining hope (an older copy may still be lying in unallocated space).

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
--source      D:  |  \\.\PhysicalDrive1  |  1  |  C:\image.img  |  adb  |  adb:<serial>
--out         output folder (must be on another disk)
--phases      sweep,bin,cache,phone,mft,vss,carve   (default: carve)
--types       jpg,png,gif,bmp,psd,raw,heic,video   or  all   (default: all)
--min-size    ignore carved files smaller than N bytes (default 16384 — raise to 65536
              to skip icons/thumbnails, lower to 4096 to catch small pictures)
--skip-video  don't recover mp4/mov/avi (much faster, much less output)
--folder      mft/sweep/vss: only recover from folders whose name contains this
--deleted-only  mft: skip files that still exist
--dry-run     mft/phone/cache: list what would be recovered, write nothing
--max-pull    phone: stop after pulling this many GB off the phone (0 = no limit)
--phone-dump  phone+carve: image a rooted phone's partition to this file first
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

## Running it from macOS instead

The same script, unchanged — useful when the Windows machine is the problem, or when you
want the external drive on a healthier computer. Everything except the Windows-only phases
works: `carve` and `mft` are the ones that matter.

```bash
diskutil list                                  # find the disk, e.g. /dev/disk4
diskutil unmountDisk /dev/disk4                # unmount volumes, leave the disk attached
sudo python3 photorescue.py --source /dev/rdisk4 --out ~/Recovered \
     --phases mft --folder "Wedding" --dry-run
sudo python3 photorescue.py --source /dev/rdisk4 --out ~/Recovered --phases carve
```

- Use `/dev/rdiskN` (raw), not `/dev/diskN` — it is many times faster.
- `sudo` is required for raw device access, exactly as Administrator is on Windows.
- Recover to a folder on a *different* disk, same rule as Windows.
- `bin` and `vss` are Windows-only; `sweep` works against any mounted volume path
  (`--source /Volumes/MyDrive`).
- macOS mounts NTFS read-only, which is a bonus here: the source cannot be modified.
- Linux works the same way (`/dev/sdb`, `sudo`).

## Notes and limits

- A phone gives files, not sectors, and Android 10+ encrypts them per-file — so on a
  phone the trash/thumbnail/cache phase is the recovery surface, not carving. The tool
  reports the device's encryption state instead of pretending otherwise.
- Carving recovers **contents, not filenames or folders** — that metadata lives in the
  filesystem, which is why `sweep`/`bin`/`vss` run first when the volume still mounts.
- Heavily fragmented files may come back partially (a photo split across the disk is
  recovered up to its first fragment boundary). JPEGs like this usually still open with the
  top part of the image intact.
- Bad sectors are zero-filled rather than aborting the scan.
- BitLocker-encrypted volumes must be unlocked first; a locked volume carves to nothing.
- The `mft` phase is NTFS-only (exFAT/FAT SD cards have no MFT — carve those). It was
  validated against a synthetic NTFS volume built for the purpose: nested deleted folders,
  resident and non-resident data, fixup/update-sequence handling, and a file with reused
  clusters — recovered files came back byte-identical, with the out-of-scope file correctly
  excluded. Use `--dry-run` to confirm the paths look right on your own disk first.
- Verified against a real device node on macOS (`/dev/rdisk`), not just image files: disk
  size and sector size come from the driver via ioctl, and MBR/extended partitions are
  enumerated the same way macOS itself enumerates them.
- Runs on macOS/Linux too against image files — that is how the carvers were tested
  (7/7 planted JPEG/PNG/GIF/HEIC/TIFF files recovered byte-identical by SHA-1).
