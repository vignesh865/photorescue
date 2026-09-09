#!/usr/bin/env python3
"""
PhotoRescue - deep photo/video recovery for Windows 10/11.

Phases (all read-only against the source):
  1. sweep  - copy image files still present on a volume (incl. hidden/system,
              Recycle Bin $R files, temp/cache dirs)
  2. bin    - parse $Recycle.Bin $I metadata and restore $R payloads with real names
  3. vss    - repeat the sweep inside every Volume Shadow Copy snapshot
  4. carve  - raw signature carving of an entire physical disk / volume / image file
              (this is what recovers deleted + formatted + corrupted-filesystem photos)

Usage examples (run in an *elevated* PowerShell / cmd):
  python photorescue.py --list
  python photorescue.py --source \\\\.\\PhysicalDrive1 --out E:\\Recovered --phases carve
  python photorescue.py --source D: --out E:\\Recovered --phases sweep,bin,vss,carve
  python photorescue.py --source disk.img --out ./recovered --phases carve   (works on mac/linux too)
"""

import argparse
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

IS_WIN = os.name == "nt"
KB = 1024
MB = 1024 * 1024
GB = 1024 * MB

# --------------------------------------------------------------------------
# Raw device access
# --------------------------------------------------------------------------

if IS_WIN:
    import msvcrt

    class DISK_GEOMETRY(ctypes.Structure):
        _fields_ = [
            ("Cylinders", ctypes.c_longlong),
            ("MediaType", ctypes.c_uint),
            ("TracksPerCylinder", ctypes.c_uint),
            ("SectorsPerTrack", ctypes.c_uint),
            ("BytesPerSector", ctypes.c_uint),
        ]

    IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

    def _ioctl(handle, code, out_struct):
        returned = ctypes.c_uint(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            ctypes.c_void_p(handle), code, None, 0,
            ctypes.byref(out_struct), ctypes.sizeof(out_struct),
            ctypes.byref(returned), None)
        return bool(ok)

    def device_geometry(fileobj):
        """Return (size_bytes, sector_size) for a raw Windows device."""
        handle = msvcrt.get_osfhandle(fileobj.fileno())
        sector = 512
        geo = DISK_GEOMETRY()
        if _ioctl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY, geo) and geo.BytesPerSector:
            sector = int(geo.BytesPerSector)
        length = ctypes.c_longlong(0)
        size = 0
        if _ioctl(handle, IOCTL_DISK_GET_LENGTH_INFO, length):
            size = int(length.value)
        if not size:
            # fall back to CHS math
            size = int(geo.Cylinders) * geo.TracksPerCylinder * geo.SectorsPerTrack * sector
        return size, sector
else:
    def device_geometry(fileobj):
        try:
            size = os.fstat(fileobj.fileno()).st_size
        except OSError:
            size = 0
        if not size:
            cur = fileobj.tell()
            size = fileobj.seek(0, 2)
            fileobj.seek(cur)
        return size, 512


def normalize_source(src):
    """Accept 'D:', 'PhysicalDrive1', '1', a \\\\.\\ path, or a file path."""
    if not IS_WIN:
        return src
    s = src.strip()
    if s.startswith("\\\\.\\"):
        return s
    if re.fullmatch(r"[A-Za-z]:?\\?", s):
        return "\\\\.\\%s:" % s[0].upper()
    if re.fullmatch(r"\d+", s):
        return "\\\\.\\PhysicalDrive%s" % s
    if re.fullmatch(r"(?i)physicaldrive\d+", s):
        return "\\\\.\\" + s
    return s


class RawReader:
    """Sector-aligned reader that works on raw devices and plain image files."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb", buffering=0)
        self.size, self.sector = device_geometry(self.f)
        if self.sector <= 0:
            self.sector = 512

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def read_at(self, offset, length):
        if offset < 0 or length <= 0:
            return b""
        if self.size and offset >= self.size:
            return b""
        if self.size:
            length = min(length, self.size - offset)
        sec = self.sector
        start = offset - (offset % sec)
        end = offset + length
        end += (-end) % sec
        if self.size and end > self.size:
            end = self.size - (self.size % sec)
            if end <= start:
                end = start + sec
        try:
            self.f.seek(start)
            data = self.f.read(end - start)
        except OSError:
            # bad sectors: walk the range sector by sector, zero-filling failures
            data = bytearray()
            for off in range(start, end, sec):
                try:
                    self.f.seek(off)
                    chunk = self.f.read(sec)
                except OSError:
                    chunk = b""
                data.extend(chunk if len(chunk) == sec else b"\x00" * sec)
            data = bytes(data)
        head = offset - start
        return data[head:head + length]


# --------------------------------------------------------------------------
# Small binary helpers
# --------------------------------------------------------------------------

def run_cmd(args, timeout=120):
    """subprocess.run wrapper that works on Python 3.5+ (capture_output and
    text= are 3.7+, and some systems still ship an older python.exe)."""
    try:
        cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=timeout)
    except Exception as e:
        return "", str(e)
    return cp.stdout or "", cp.stderr or ""


def u16(b, o, le=True):
    return int.from_bytes(b[o:o + 2], "little" if le else "big")


def u32(b, o, le=True):
    return int.from_bytes(b[o:o + 4], "little" if le else "big")


def u64(b, o, le=True):
    return int.from_bytes(b[o:o + 8], "little" if le else "big")


# --------------------------------------------------------------------------
# Format carvers.  Each returns (length, extension) or None.
# They receive a `rd(rel_off, n)` accessor relative to the file start.
# --------------------------------------------------------------------------

MAX_JPEG = 96 * MB
MAX_PNG = 256 * MB
MAX_GIF = 64 * MB
MAX_TIFF = 512 * MB
MAX_ISO = 6 * GB          # mp4/mov/heic container
MAX_PSD = 2 * GB
MAX_BMP = 256 * MB


def carve_jpeg(rd, limit):
    """Walk JPEG markers, then the entropy stream, to find the true EOI."""
    off = 2  # past SOI
    buf = rd(0, min(limit, 4 * MB))
    if len(buf) < 4:
        return None

    def byte_at(i):
        nonlocal buf
        if i >= len(buf):
            need = min(limit, max(i + 4 * MB, len(buf) * 2))
            buf = rd(0, need)
        if i >= len(buf):
            return None
        return buf[i]

    saw_sos = False
    while off < limit:
        b0 = byte_at(off)
        if b0 is None:
            return None
        if b0 != 0xFF:
            return None
        marker = byte_at(off + 1)
        if marker is None:
            return None
        while marker == 0xFF:  # fill bytes
            off += 1
            marker = byte_at(off + 1)
            if marker is None:
                return None
        if marker in (0xD8,):
            off += 2
            continue
        if marker == 0xD9:
            return (off + 2, "jpg")
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            off += 2
            continue
        hi, lo = byte_at(off + 2), byte_at(off + 3)
        if hi is None or lo is None:
            return None
        seglen = (hi << 8) | lo
        if seglen < 2:
            return None
        if marker == 0xDA:  # start of scan -> entropy coded data
            saw_sos = True
            i = off + 2 + seglen
            while i < limit:
                b = byte_at(i)
                if b is None:
                    break
                if b == 0xFF:
                    nb = byte_at(i + 1)
                    if nb is None:
                        break
                    if nb == 0xD9:
                        return (i + 2, "jpg")
                    if nb == 0x00 or 0xD0 <= nb <= 0xD7 or nb == 0xFF:
                        i += 2
                        continue
                    # another marker (multi-scan / progressive): resume header walk
                    off = i
                    break
                i += 1
            else:
                break
            if off != i:
                break
            continue
        off += 2 + seglen
    return None if not saw_sos else None


def carve_png(rd, limit):
    off = 8
    while off < limit:
        hdr = rd(off, 8)
        if len(hdr) < 8:
            return None
        clen = u32(hdr, 0, le=False)
        ctype = hdr[4:8]
        if not all(0x41 <= c <= 0x7A for c in ctype):
            return None
        if clen > limit:
            return None
        off += 12 + clen
        if ctype == b"IEND":
            return (off, "png")
    return None


def _gif_subblocks(rd, off, limit):
    while off < limit:
        n = rd(off, 1)
        if not n:
            return None
        size = n[0]
        off += 1
        if size == 0:
            return off
        off += size
    return None


def carve_gif(rd, limit):
    head = rd(0, 13)
    if len(head) < 13:
        return None
    flags = head[10]
    off = 13
    if flags & 0x80:
        off += 3 * (2 ** ((flags & 0x07) + 1))
    while off < limit:
        b = rd(off, 1)
        if not b:
            return None
        tag = b[0]
        if tag == 0x3B:
            return (off + 1, "gif")
        if tag == 0x21:  # extension
            off += 2
            off = _gif_subblocks(rd, off, limit)
            if off is None:
                return None
        elif tag == 0x2C:  # image descriptor
            desc = rd(off, 10)
            if len(desc) < 10:
                return None
            lflags = desc[9]
            off += 10
            if lflags & 0x80:
                off += 3 * (2 ** ((lflags & 0x07) + 1))
            off += 1  # LZW min code size
            off = _gif_subblocks(rd, off, limit)
            if off is None:
                return None
        else:
            return None
    return None


TIFF_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}


def carve_tiff(rd, limit):
    """Walk IFDs/SubIFDs and take the furthest byte referenced."""
    head = rd(0, 16)
    if len(head) < 8:
        return None
    le = head[0:2] == b"II"
    ext = "tif"
    if head[2:4] in (b"\x52\x4f", b"\x4f\x52"):  # ORF
        ext = "orf"
    if head[8:10] == b"CR":
        ext = "cr2"
    first = u32(head, 4, le)
    if first < 8 or first > limit:
        return None
    max_end = 16
    todo = [first]
    seen = set()
    ifds = 0
    strip_off, strip_len, tile_off, tile_len = [], [], [], []
    while todo and ifds < 64:
        ifd = todo.pop()
        if ifd in seen or ifd + 2 > limit:
            continue
        seen.add(ifd)
        ifds += 1
        cnt_b = rd(ifd, 2)
        if len(cnt_b) < 2:
            continue
        count = u16(cnt_b, 0, le)
        if count == 0 or count > 512:
            continue
        entries = rd(ifd + 2, count * 12 + 4)
        if len(entries) < count * 12:
            continue
        max_end = max(max_end, ifd + 2 + count * 12 + 4)
        for i in range(count):
            e = entries[i * 12:(i + 1) * 12]
            tag = u16(e, 0, le)
            typ = u16(e, 2, le)
            n = u32(e, 4, le)
            tsize = TIFF_TYPE_SIZE.get(typ, 0)
            if not tsize or n > 1 << 24:
                continue
            total = tsize * n
            if total > 4:
                voff = u32(e, 8, le)
                if 0 < voff < limit:
                    max_end = max(max_end, voff + total)
                vals = None
            else:
                voff = None
            if tag in (0x0111, 0x0117, 0x0144, 0x0145, 0x0201, 0x0202, 0x014A, 0x8769, 0xC612):
                if tag == 0xC612:
                    ext = "dng"
                    continue
                vals = _tiff_values(rd, e, le, limit)
                if vals is None:
                    continue
                if tag == 0x0111:
                    strip_off = vals
                elif tag == 0x0117:
                    strip_len = vals
                elif tag == 0x0144:
                    tile_off = vals
                elif tag == 0x0145:
                    tile_len = vals
                elif tag == 0x0201 and vals:
                    thumb_off = vals[0]
                elif tag == 0x0202 and vals:
                    max_end = max(max_end, vals[0])
                elif tag in (0x014A, 0x8769):
                    todo.extend(v for v in vals if 8 < v < limit)
        for o, l in zip(strip_off, strip_len):
            max_end = max(max_end, o + l)
        for o, l in zip(tile_off, tile_len):
            max_end = max(max_end, o + l)
        strip_off, strip_len, tile_off, tile_len = [], [], [], []
        nxt_b = entries[count * 12:count * 12 + 4]
        if len(nxt_b) == 4:
            nxt = u32(nxt_b, 0, le)
            if 8 < nxt < limit:
                todo.append(nxt)
    if max_end <= 64 or max_end > limit:
        return None
    return (max_end, ext)


def _tiff_values(rd, entry, le, limit):
    typ = u16(entry, 2, le)
    n = u32(entry, 4, le)
    tsize = TIFF_TYPE_SIZE.get(typ, 0)
    if not tsize or n == 0 or n > 65536:
        return None
    total = tsize * n
    if total <= 4:
        raw = entry[8:8 + total]
    else:
        off = u32(entry, 8, le)
        if off <= 0 or off + total > limit:
            return None
        raw = rd(off, total)
        if len(raw) < total:
            return None
    out = []
    for i in range(n):
        chunk = raw[i * tsize:(i + 1) * tsize]
        out.append(int.from_bytes(chunk, "little" if le else "big"))
    return out


ISO_BRANDS = {
    b"heic": "heic", b"heix": "heic", b"heim": "heic", b"heis": "heic",
    b"hevc": "heic", b"hevx": "heic", b"mif1": "heic", b"msf1": "heic",
    b"avif": "avif", b"avis": "avif",
    b"crx ": "cr3", b"crx\x00": "cr3",
    b"qt  ": "mov",
    b"3gp4": "3gp", b"3gp5": "3gp",
}


def carve_iso_bmff(rd, limit):
    head = rd(0, 16)
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    brand = head[8:12]
    ext = ISO_BRANDS.get(brand)
    if ext is None:
        ext = "mp4" if brand[:3] in (b"iso", b"mp4", b"M4V", b"M4A", b"dash", b"avc") or brand[:2] in (b"m4",) else None
    if ext is None:
        # unknown brand: accept it as mp4 only if the box chain looks sane
        ext = "mp4"
    off = 0
    seen_types = set()
    boxes = 0
    while off < limit and boxes < 4096:
        hdr = rd(off, 16)
        if len(hdr) < 8:
            break
        size = u32(hdr, 0, le=False)
        btype = hdr[4:8]
        if not all(32 <= c <= 126 for c in btype):
            break
        if size == 1:
            if len(hdr) < 16:
                break
            size = u64(hdr, 8, le=False)
        elif size == 0:
            size = limit - off
        if size < 8 or off + size > limit:
            break
        seen_types.add(btype)
        off += size
        boxes += 1
    if not ({b"mdat", b"moov", b"meta"} & seen_types):
        return None
    if off <= 32:
        return None
    return (off, ext)


def carve_riff(rd, limit):
    head = rd(0, 16)
    if len(head) < 12:
        return None
    form = head[8:12]
    size = u32(head, 4)
    if size < 8 or size + 8 > limit:
        return None
    if form == b"WEBP":
        return (size + 8, "webp")
    if form == b"AVI ":
        return (size + 8, "avi")
    return None


def carve_psd(rd, limit):
    head = rd(0, 26)
    if len(head) < 26 or head[4:6] != b"\x00\x01":
        return None
    off = 26
    for _ in range(3):  # color mode data, image resources, layer info
        b = rd(off, 4)
        if len(b) < 4:
            return None
        n = u32(b, 0, le=False)
        if n > limit:
            return None
        off += 4 + n
    tail = rd(off, 2)
    if len(tail) < 2:
        return None
    # image data section length is not stored; approximate from dimensions
    ch = u16(head, 12, le=False)
    h = u32(head, 14, le=False)
    w = u32(head, 18, le=False)
    depth = u16(head, 22, le=False)
    if not (0 < ch <= 56 and 0 < w < 300000 and 0 < h < 300000 and depth in (1, 8, 16, 32)):
        return None
    off += 2 + ch * w * h * (depth // 8 or 1)
    if off > limit:
        return None
    return (off, "psd")


def carve_bmp(rd, limit):
    head = rd(0, 34)
    if len(head) < 34:
        return None
    size = u32(head, 2)
    reserved = u32(head, 6)
    data_off = u32(head, 10)
    dib = u32(head, 14)
    if reserved != 0 or dib not in (12, 40, 52, 56, 64, 108, 124):
        return None
    if not (54 <= size <= min(limit, MAX_BMP)) or not (26 <= data_off < size):
        return None
    return (size, "bmp")


def carve_raf(rd, limit):
    head = rd(0, 0x80)
    if len(head) < 0x80:
        return None
    off = u32(head, 0x54, le=False)
    ln = u32(head, 0x58, le=False)
    end = off + ln
    if not (0 < end <= limit):
        return None
    return (end, "raf")


# signature -> (offset_of_sig_within_file, carver, max_size, group)
SIGNATURES = [
    (b"\xff\xd8\xff", 0, carve_jpeg, MAX_JPEG, "jpg"),
    (b"\x89PNG\r\n\x1a\n", 0, carve_png, MAX_PNG, "png"),
    (b"GIF87a", 0, carve_gif, MAX_GIF, "gif"),
    (b"GIF89a", 0, carve_gif, MAX_GIF, "gif"),
    (b"II\x2a\x00", 0, carve_tiff, MAX_TIFF, "raw"),
    (b"MM\x00\x2a", 0, carve_tiff, MAX_TIFF, "raw"),
    (b"IIRO", 0, carve_tiff, MAX_TIFF, "raw"),
    (b"MMOR", 0, carve_tiff, MAX_TIFF, "raw"),
    (b"FUJIFILMCCD-RAW", 0, carve_raf, MAX_TIFF, "raw"),
    (b"ftyp", 4, carve_iso_bmff, MAX_ISO, "iso"),
    (b"RIFF", 0, carve_riff, MAX_ISO, "riff"),
    (b"8BPS", 0, carve_psd, MAX_PSD, "psd"),
    (b"BM", 0, carve_bmp, MAX_BMP, "bmp"),
]

GROUP_ALIASES = {
    "jpg": {"jpg"}, "jpeg": {"jpg"},
    "png": {"png"}, "gif": {"gif"}, "bmp": {"bmp"}, "psd": {"psd"},
    "raw": {"raw"}, "heic": {"iso"}, "video": {"iso", "riff"},
}

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".webp", ".psd", ".dng", ".cr2", ".cr3", ".nef",
    ".arw", ".orf", ".raf", ".rw2", ".pef", ".srw", ".sr2", ".raw", ".ico",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mts", ".mkv", ".wmv"}


# --------------------------------------------------------------------------
# EXIF date extraction (used for foldering + naming)
# --------------------------------------------------------------------------

def exif_datetime(head):
    """Best-effort DateTimeOriginal from the first bytes of a JPEG/TIFF/HEIC."""
    idx = head.find(b"Exif\x00\x00")
    base = idx + 6 if idx != -1 else (0 if head[:2] in (b"II", b"MM") else -1)
    if base < 0 or base + 8 > len(head):
        m = re.search(rb"(19|20)\d{2}:[0-1]\d:[0-3]\d [0-2]\d:[0-5]\d:[0-6]\d", head)
        return _parse_exif_str(m.group(0)) if m else None
    tiff = head[base:]
    le = tiff[:2] == b"II"
    try:
        ifd = u32(tiff, 4, le)
        for _ in range(2):
            if ifd + 2 > len(tiff):
                return None
            count = u16(tiff, ifd, le)
            if count > 512:
                return None
            exif_ifd = None
            for i in range(count):
                e = tiff[ifd + 2 + i * 12: ifd + 14 + i * 12]
                if len(e) < 12:
                    return None
                tag = u16(e, 0, le)
                if tag in (0x9003, 0x9004, 0x0132):
                    off = u32(e, 8, le)
                    s = tiff[off:off + 19]
                    d = _parse_exif_str(s)
                    if d:
                        return d
                if tag == 0x8769:
                    exif_ifd = u32(e, 8, le)
            if exif_ifd is None:
                return None
            ifd = exif_ifd
    except Exception:
        return None
    return None


def _parse_exif_str(s):
    try:
        return datetime.strptime(s.decode("ascii", "ignore")[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None


# --------------------------------------------------------------------------
# Output management
# --------------------------------------------------------------------------

class Output:
    def __init__(self, root, resume=False, organize=True):
        self.root = os.path.abspath(root)
        self.organize = organize
        os.makedirs(self.root, exist_ok=True)
        self.hash_path = os.path.join(self.root, "_photorescue_hashes.txt")
        self.manifest_path = os.path.join(self.root, "_photorescue_manifest.csv")
        self.state_path = os.path.join(self.root, "_photorescue_state.json")
        self.hashes = set()
        if resume and os.path.exists(self.hash_path):
            with open(self.hash_path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self.hashes.add(line)
        new_manifest = not os.path.exists(self.manifest_path)
        self.hash_fh = open(self.hash_path, "a", encoding="utf-8")
        self.manifest = open(self.manifest_path, "a", encoding="utf-8", newline="")
        if new_manifest:
            self.manifest.write("saved_path,source,offset,size,ext,sha1,exif_date\n")
        self.counts = {}
        self.dupes = 0
        self.bytes_written = 0

    def _dest(self, ext, dt, stem):
        sub = ext.upper()
        if self.organize and dt:
            sub = os.path.join(sub, dt.strftime("%Y-%m"))
        d = os.path.join(self.root, sub)
        os.makedirs(d, exist_ok=True)
        name = "%s.%s" % (stem, ext)
        path = os.path.join(d, name)
        n = 1
        while os.path.exists(path):
            path = os.path.join(d, "%s_%d.%s" % (stem, n, ext))
            n += 1
        return path

    def save_stream(self, reader, offset, length, ext, source):
        """Copy `length` bytes at `offset` out of `reader`, dedup by sha1."""
        tmp = os.path.join(self.root, "_incoming.tmp")
        h = hashlib.sha1()
        head = b""
        remaining = length
        pos = offset
        with open(tmp, "wb") as out:
            while remaining > 0:
                chunk = reader.read_at(pos, min(4 * MB, remaining))
                if not chunk:
                    break
                if len(head) < 128 * KB:
                    head += chunk[:128 * KB - len(head)]
                out.write(chunk)
                h.update(chunk)
                pos += len(chunk)
                remaining -= len(chunk)
        written = length - remaining
        if written < 512:
            os.remove(tmp)
            return None
        digest = h.hexdigest()
        if digest in self.hashes:
            os.remove(tmp)
            self.dupes += 1
            return None
        dt = exif_datetime(head)
        stem = (dt.strftime("%Y%m%d_%H%M%S") + "_" if dt else "") + ("off%012x" % offset)
        dest = self._dest(ext, dt, stem)
        os.replace(tmp, dest)
        if dt:
            ts = dt.timestamp()
            try:
                os.utime(dest, (ts, ts))
            except OSError:
                pass
        self._record(dest, source, offset, written, ext, digest, dt)
        return dest

    def save_bytes(self, data, ext, source, name_hint=None, mtime=None):
        digest = hashlib.sha1(data).hexdigest()
        if digest in self.hashes:
            self.dupes += 1
            return None
        dt = exif_datetime(data[:128 * KB]) or mtime
        stem = name_hint or ((dt.strftime("%Y%m%d_%H%M%S_") if dt else "") + digest[:12])
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)[:120]
        dest = self._dest(ext, dt, stem)
        with open(dest, "wb") as fh:
            fh.write(data)
        if dt:
            try:
                os.utime(dest, (dt.timestamp(), dt.timestamp()))
            except OSError:
                pass
        self._record(dest, source, 0, len(data), ext, digest, dt)
        return dest

    def _record(self, dest, source, offset, size, ext, digest, dt):
        self.hashes.add(digest)
        self.hash_fh.write(digest + "\n")
        self.counts[ext] = self.counts.get(ext, 0) + 1
        self.bytes_written += size
        self.manifest.write('"%s","%s",%d,%d,%s,%s,%s\n' % (
            dest.replace('"', "'"), str(source).replace('"', "'"), offset, size, ext,
            digest, dt.isoformat() if dt else ""))

    def flush(self):
        self.hash_fh.flush()
        self.manifest.flush()

    def close(self):
        self.flush()
        self.hash_fh.close()
        self.manifest.close()

    def summary(self):
        parts = ", ".join("%s=%d" % (k, v) for k, v in sorted(self.counts.items()))
        total = sum(self.counts.values())
        return "%d files (%s) | %.2f GB | %d duplicates skipped" % (
            total, parts or "none", self.bytes_written / GB, self.dupes)


# --------------------------------------------------------------------------
# Phase: raw carving
# --------------------------------------------------------------------------

def select_signatures(groups):
    sigs = [x for x in SIGNATURES if x[4] in groups]
    if not sigs:
        raise SystemExit("no signature groups selected")
    return sigs


def scan_block(buf, sigs):
    """Multi-pattern literal scan. bytes.find() is memmem in C and roughly
    15x faster than an re alternation over the same data."""
    hits = []
    for idx, entry in enumerate(sigs):
        sig = entry[0]
        pos = buf.find(sig)
        while pos != -1:
            hits.append((pos, idx))
            pos = buf.find(sig, pos + 1)
    hits.sort()
    return hits


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fPB" % n


def phase_carve(source_path, out, args):
    reader = RawReader(source_path)
    extractor = RawReader(source_path)
    groups = set()
    for t in args.types.split(","):
        t = t.strip().lower()
        if t == "all":
            groups |= {"jpg", "png", "gif", "bmp", "psd", "raw", "iso", "riff"}
        elif t in GROUP_ALIASES:
            groups |= GROUP_ALIASES[t]
    if args.skip_video:
        groups.discard("riff")
    sigs = select_signatures(groups)
    max_sig = max(len(x[0]) + x[1] for x in sigs)

    total = reader.size
    block = max(args.block * MB, MB)
    block -= block % reader.sector
    overlap = max_sig + 16

    start = args.start
    start -= start % reader.sector
    end = total if not args.end else min(args.end, total or args.end)

    state = {}
    if args.resume and os.path.exists(out.state_path):
        try:
            with open(out.state_path) as fh:
                state = json.load(fh)
            if state.get("source") == source_path and state.get("offset", 0) > start:
                # rewind one block so a file whose header sat just before the
                # cut is picked up again (sha1 dedup absorbs the overlap)
                resume_at = max(args.start, state["offset"] - block)
                start = resume_at - (resume_at % reader.sector)
                print("[carve] resuming at offset %s (%s)" % (start, human(start)))
        except Exception:
            pass

    print("[carve] device : %s" % source_path)
    print("[carve] size   : %s (%d bytes), sector %d" % (human(total) if total else "unknown", total, reader.sector))
    print("[carve] types  : %s" % ",".join(sorted(groups)))
    print("[carve] block  : %s   min-size: %s" % (human(block), human(args.min_size)))

    pos = start
    carry = b""
    skip_until = 0
    t0 = time.time()
    last_report = 0.0
    read_bytes = 0

    try:
        while (not end) or pos < end:
            want = block if not end else min(block, end - pos)
            if want <= 0:
                break
            data = reader.read_at(pos, want)
            if not data:
                break
            buf = carry + data
            base = pos - len(carry)
            read_bytes += len(data)

            for rel, idx in scan_block(buf, sigs):
                sig, sig_at, carver, maxsize, group = sigs[idx]
                abs_off = base + rel
                if abs_off + len(sig) <= pos:
                    continue  # already handled in the previous block
                file_off = abs_off - sig_at
                if file_off < 0 or file_off < skip_until:
                    continue
                limit = maxsize
                if end:
                    limit = min(limit, end - file_off)
                if limit < 512:
                    continue

                def rd(rel_off, n, _o=file_off):
                    return extractor.read_at(_o + rel_off, n)

                try:
                    res = carver(rd, limit)
                except Exception:
                    res = None
                if not res:
                    continue
                length, ext = res
                if length < args.min_size or length > limit:
                    continue
                if args.skip_video and ext in ("mp4", "mov", "3gp", "avi", "m4v"):
                    skip_until = file_off + length
                    continue
                saved = out.save_stream(extractor, file_off, length, ext, source_path)
                skip_until = file_off + length
                if saved and args.verbose:
                    print("\n  + %s  @0x%x  %s" % (os.path.basename(saved), file_off, human(length)))

            carry = buf[-overlap:]
            pos += len(data)

            now = time.time()
            if now - last_report > 1.0:
                last_report = now
                elapsed = now - t0
                rate = read_bytes / elapsed if elapsed else 0
                pct = (pos / end * 100.0) if end else 0.0
                eta = ((end - pos) / rate) if (end and rate) else 0
                sys.stdout.write("\r[carve] %5.2f%%  %s/%s  %5.1f MB/s  found %d  ETA %s   " % (
                    pct, human(pos), human(end) if end else "?", rate / MB,
                    sum(out.counts.values()), str(timedelta(seconds=int(eta))) if eta else "?"))
                sys.stdout.flush()
                out.flush()
                with open(out.state_path, "w") as fh:
                    json.dump({"source": source_path, "offset": pos,
                               "updated": datetime.now().isoformat()}, fh)
    except KeyboardInterrupt:
        print("\n[carve] interrupted - progress saved, re-run with --resume")
    finally:
        with open(out.state_path, "w") as fh:
            json.dump({"source": source_path, "offset": pos,
                       "updated": datetime.now().isoformat()}, fh)
        reader.close()
        extractor.close()
    print("\n[carve] done: %s" % out.summary())


# --------------------------------------------------------------------------
# Phase: filesystem sweep
# --------------------------------------------------------------------------

def iter_files(root):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            yield e
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue


def phase_sweep(root, out, args, label="sweep"):
    exts = set(IMAGE_EXTS)
    if not args.skip_video:
        exts |= VIDEO_EXTS
    n = 0
    scanned = 0
    t0 = time.time()
    print("[%s] walking %s" % (label, root))
    for entry in iter_files(root):
        scanned += 1
        name = entry.name
        if args.folder and args.folder.lower() not in os.path.dirname(entry.path).lower():
            continue
        ext = os.path.splitext(name)[1].lower()
        base = os.path.basename(entry.path)
        is_recycled = base.startswith("$R")
        if ext not in exts and not is_recycled:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_size < args.min_size or st.st_size > 8 * GB:
            continue
        try:
            with open(entry.path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        real_ext = sniff_ext(data) if is_recycled else ext.lstrip(".")
        if not real_ext:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime)
        stem = os.path.splitext(name)[0]
        if out.save_bytes(data, real_ext, entry.path, name_hint=stem, mtime=mtime):
            n += 1
        if scanned % 500 == 0:
            sys.stdout.write("\r[%s] scanned %d, recovered %d (%.0fs)   " % (label, scanned, n, time.time() - t0))
            sys.stdout.flush()
            out.flush()
    print("\r[%s] %s: scanned %d files, recovered %d" % (label, root, scanned, n))


def sniff_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[4:8] == b"ftyp":
        return ISO_BRANDS.get(data[8:12], "mp4")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "avi"
    if data[:2] in (b"II", b"MM") and data[2:4] in (b"\x2a\x00", b"\x00\x2a", b"RO", b"OR"):
        return "tif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"8BPS":
        return "psd"
    if data[:15] == b"FUJIFILMCCD-RAW":
        return "raf"
    return None


# --------------------------------------------------------------------------
# Phase: Recycle Bin
# --------------------------------------------------------------------------

def phase_recyclebin(volume, out, args):
    root = os.path.join(volume + "\\" if len(volume) == 2 else volume, "$Recycle.Bin")
    if not os.path.isdir(root):
        print("[bin] no $Recycle.Bin on %s" % volume)
        return
    n = 0
    for sid in _listdir(root):
        sid_dir = os.path.join(root, sid)
        if not os.path.isdir(sid_dir):
            continue
        for name in _listdir(sid_dir):
            if not name.startswith("$I"):
                continue
            ipath = os.path.join(sid_dir, name)
            rpath = os.path.join(sid_dir, "$R" + name[2:])
            orig, deleted = parse_dollar_i(ipath)
            if not os.path.isfile(rpath):
                continue
            try:
                with open(rpath, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            ext = sniff_ext(data)
            if not ext or len(data) < args.min_size:
                continue
            stem = os.path.splitext(os.path.basename(orig))[0] if orig else name
            if out.save_bytes(data, ext, ipath, name_hint=stem, mtime=deleted):
                n += 1
    print("[bin] recovered %d files from %s Recycle Bin" % (n, volume))


def _listdir(p):
    try:
        return os.listdir(p)
    except OSError:
        return []


def parse_dollar_i(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read(1024)
    except OSError:
        return None, None
    if len(data) < 24:
        return None, None
    ver = u64(data, 0)
    ft = u64(data, 16)
    deleted = None
    if ft:
        try:
            deleted = datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
        except Exception:
            deleted = None
    name = None
    try:
        if ver == 2:
            nlen = u32(data, 24)
            name = data[28:28 + nlen * 2].decode("utf-16-le", "ignore").rstrip("\x00")
        else:
            name = data[24:24 + 520].decode("utf-16-le", "ignore").rstrip("\x00")
    except Exception:
        pass
    return name, deleted


# --------------------------------------------------------------------------
# Phase: NTFS $MFT scan  (recover deleted files WITH their names and folders)
# --------------------------------------------------------------------------

MFT_ROOT = 5
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80


def apply_fixups(rec, sector_size):
    """NTFS scatters an update-sequence number over the last 2 bytes of every
    sector of a record; put the real bytes back."""
    usa_off = u16(rec, 0x04)
    usa_cnt = u16(rec, 0x06)
    if usa_cnt == 0 or usa_off + usa_cnt * 2 > len(rec):
        return rec
    usn = rec[usa_off:usa_off + 2]
    out = bytearray(rec)
    for i in range(1, usa_cnt):
        end = i * sector_size
        if end > len(out):
            break
        if bytes(out[end - 2:end]) != usn:
            return None  # torn / not a real record
        out[end - 2:end] = rec[usa_off + i * 2: usa_off + i * 2 + 2]
    return bytes(out)


def parse_runlist(data):
    """Decode a data-run list into [(vcn, lcn_or_None_for_sparse, clusters)]."""
    runs = []
    off = 0
    lcn = 0
    vcn = 0
    while off < len(data):
        head = data[off]
        if head == 0:
            break
        lsz = head & 0x0F
        osz = head >> 4
        off += 1
        if lsz == 0 or off + lsz + osz > len(data):
            break
        count = int.from_bytes(data[off:off + lsz], "little")
        off += lsz
        if osz:
            lcn += int.from_bytes(data[off:off + osz], "little", signed=True)
            off += osz
            runs.append((vcn, lcn, count))
        else:
            runs.append((vcn, None, count))  # sparse
        vcn += count
        if len(runs) > 8192:
            break
    return runs


def iter_attributes(rec):
    """Yield (type, name, is_nonresident, flags, payload) for each attribute.
    payload is raw bytes for resident attrs, or (runs, real_size) if not."""
    off = u16(rec, 0x14)
    while off + 8 <= len(rec):
        atype = u32(rec, off)
        if atype == 0xFFFFFFFF:
            return
        alen = u32(rec, off + 4)
        if alen < 16 or off + alen > len(rec):
            return
        non_res = rec[off + 8]
        name_len = rec[off + 9]
        name_off = u16(rec, off + 10)
        flags = u16(rec, off + 12)
        name = ""
        if name_len:
            name = rec[off + name_off: off + name_off + name_len * 2].decode("utf-16-le", "ignore")
        if non_res:
            run_off = u16(rec, off + 0x20)
            real = u64(rec, off + 0x30)
            runs = parse_runlist(rec[off + run_off: off + alen])
            yield (atype, name, True, flags, (runs, real))
        else:
            vlen = u32(rec, off + 0x10)
            voff = u16(rec, off + 0x14)
            yield (atype, name, False, flags, rec[off + voff: off + voff + vlen])
        off += alen


def parse_filename_attr(val):
    """-> (parent_index, name, namespace) from a $FILE_NAME value."""
    if len(val) < 0x42:
        return None
    parent = u64(val, 0) & 0x0000FFFFFFFFFFFF
    nlen = val[0x40]
    ns = val[0x41]
    name = val[0x42:0x42 + nlen * 2].decode("utf-16-le", "ignore")
    return (parent, name, ns)


class NTFSVolume:
    def __init__(self, reader, base):
        self.reader = reader
        self.base = base
        boot = reader.read_at(base, 512)
        if len(boot) < 512 or boot[3:11] != b"NTFS    ":
            raise ValueError("not NTFS")
        self.bps = u16(boot, 0x0B)
        spc = boot[0x0D]
        if spc > 0x80:
            spc = 1 << (256 - spc)
        if self.bps not in (512, 1024, 2048, 4096) or spc == 0:
            raise ValueError("bad NTFS geometry")
        self.cluster = self.bps * spc
        self.total_sectors = u64(boot, 0x28)
        self.mft_lcn = u64(boot, 0x30)
        v = boot[0x40]
        self.rec_size = (1 << (256 - v)) if v > 0x80 else v * self.cluster
        if self.rec_size not in (256, 512, 1024, 2048, 4096):
            raise ValueError("bad MFT record size")
        rec0 = self.raw_record_at(base + self.mft_lcn * self.cluster)
        if rec0 is None:
            raise ValueError("unreadable $MFT")
        self.mft_runs, self.mft_size = None, 0
        for atype, name, non_res, flags, payload in iter_attributes(rec0):
            if atype == ATTR_DATA and not name and non_res:
                self.mft_runs, self.mft_size = payload
                break
        if not self.mft_runs:
            raise ValueError("$MFT has no data runs")
        self.record_count = self.mft_size // self.rec_size

    def raw_record_at(self, abs_off):
        rec = self.reader.read_at(abs_off, self.rec_size)
        if len(rec) < self.rec_size or rec[:4] != b"FILE":
            return None
        return apply_fixups(rec, self.bps)

    def read_runs(self, runs, offset, length, real_size=None):
        """Read from a non-resident stream by virtual offset."""
        if real_size is not None:
            length = min(length, max(0, real_size - offset))
        out = bytearray()
        for vcn, lcn, count in runs:
            run_start = vcn * self.cluster
            run_len = count * self.cluster
            if run_start + run_len <= offset:
                continue
            if run_start >= offset + length:
                break
            take_from = max(offset, run_start)
            take_to = min(offset + length, run_start + run_len)
            n = take_to - take_from
            if n <= 0:
                continue
            if lcn is None:
                out.extend(b"\x00" * n)
            else:
                disk = self.base + lcn * self.cluster + (take_from - run_start)
                chunk = self.reader.read_at(disk, n)
                out.extend(chunk if len(chunk) == n else chunk + b"\x00" * (n - len(chunk)))
        return bytes(out)

    def record(self, index):
        raw = self.read_runs(self.mft_runs, index * self.rec_size, self.rec_size)
        if len(raw) < self.rec_size or raw[:4] != b"FILE":
            return None
        return apply_fixups(raw, self.bps)

    def iter_records(self, chunk_records=1024):
        """Stream every MFT record: yields (index, fixed_up_record)."""
        total = self.record_count
        idx = 0
        while idx < total:
            n = min(chunk_records, total - idx)
            blob = self.read_runs(self.mft_runs, idx * self.rec_size, n * self.rec_size)
            if not blob:
                break
            for i in range(n):
                raw = blob[i * self.rec_size:(i + 1) * self.rec_size]
                if len(raw) < self.rec_size or raw[:4] != b"FILE":
                    continue
                fixed = apply_fixups(raw, self.bps)
                if fixed:
                    yield (idx + i, fixed)
            idx += n


def find_volumes(reader):
    """Return candidate NTFS volume start offsets: the source itself, plus every
    MBR/GPT partition on it."""
    offsets = [0]
    sec = reader.sector or 512
    mbr = reader.read_at(0, 512)
    if len(mbr) >= 512 and mbr[510:512] == b"\x55\xaa":
        gpt = False
        for i in range(4):
            e = mbr[0x1BE + i * 16: 0x1BE + (i + 1) * 16]
            ptype = e[4]
            start = u32(e, 8)
            if ptype == 0xEE:
                gpt = True
            elif ptype and start:
                offsets.append(start * sec)
        if gpt:
            hdr = reader.read_at(sec, 512)
            if hdr[:8] == b"EFI PART":
                ent_lba = u64(hdr, 72)
                n_ent = u32(hdr, 80)
                ent_sz = u32(hdr, 84)
                if 0 < n_ent <= 256 and 128 <= ent_sz <= 1024:
                    table = reader.read_at(ent_lba * sec, n_ent * ent_sz)
                    for i in range(n_ent):
                        e = table[i * ent_sz:(i + 1) * ent_sz]
                        if len(e) < 56 or e[:16] == b"\x00" * 16:
                            continue
                        offsets.append(u64(e, 32) * sec)
    seen = []
    for o in offsets:
        if o not in seen and (not reader.size or o < reader.size):
            seen.append(o)
    return seen


def _build_path(dirs, index, cache):
    if index in cache:
        return cache[index]
    parts = []
    seen = set()
    cur = index
    while cur != MFT_ROOT and cur in dirs and cur not in seen:
        seen.add(cur)
        name, parent = dirs[cur]
        parts.append(name)
        cur = parent
    path = "\\".join(reversed(parts)) if parts else ""
    cache[index] = path
    return path


def phase_mft(source_path, out, args):
    reader = RawReader(source_path)
    want = (args.folder or "").lower()
    exts = set(IMAGE_EXTS)
    if not args.skip_video:
        exts |= VIDEO_EXTS
    try:
        for base in find_volumes(reader):
            try:
                vol = NTFSVolume(reader, base)
            except Exception:
                continue
            print("[mft] NTFS volume at offset %s: cluster %s, %d MFT records" % (
                human(base), human(vol.cluster), vol.record_count))

            # pass 1: directory tree (directories are a tiny fraction of the MFT)
            dirs = {}
            for idx, rec in vol.iter_records():
                if not (u16(rec, 0x16) & 0x0002):
                    continue
                best = None
                for atype, _n, non_res, _f, payload in iter_attributes(rec):
                    if atype == ATTR_FILE_NAME and not non_res:
                        fn = parse_filename_attr(payload)
                        if fn and (best is None or fn[2] != 2):  # prefer non-DOS name
                            best = fn
                if best:
                    dirs[idx] = (best[1], best[0])
            print("[mft] %d directories indexed" % len(dirs))

            targets = None
            if want:
                hit = set(i for i, (name, _p) in dirs.items() if want in name.lower())
                if not hit:
                    print('[mft] no folder matching "%s" on this volume' % args.folder)
                    continue
                # include every sub-folder underneath the matches
                targets = set(hit)
                changed = True
                while changed:
                    changed = False
                    for i, (_n, parent) in dirs.items():
                        if parent in targets and i not in targets:
                            targets.add(i)
                            changed = True
                cache = {}
                for i in sorted(hit):
                    print('[mft] match: \\%s  (mft #%d)' % (_build_path(dirs, i, cache), i))
                print("[mft] %d folders in scope (including sub-folders)" % len(targets))

            # pass 2: files
            cache = {}
            found = saved = overwritten = 0
            for idx, rec in vol.iter_records():
                flags = u16(rec, 0x16)
                if flags & 0x0002:
                    continue
                in_use = bool(flags & 0x0001)
                if args.deleted_only and in_use:
                    continue
                fn = None
                data = None
                for atype, aname, non_res, aflags, payload in iter_attributes(rec):
                    if atype == ATTR_FILE_NAME and not non_res:
                        cand = parse_filename_attr(payload)
                        if cand and (fn is None or cand[2] != 2):
                            fn = cand
                    elif atype == ATTR_DATA and not aname:
                        data = (non_res, aflags, payload)
                if not fn or data is None:
                    continue
                parent, name, _ns = fn
                if targets is not None and parent not in targets:
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in exts:
                    continue
                found += 1
                non_res, aflags, payload = data
                if non_res:
                    runs, real = payload
                    if aflags & 0x00FF:      # compressed / encrypted stream
                        continue
                    if not runs or real < args.min_size or real > 8 * GB:
                        continue
                    blob = vol.read_runs(runs, 0, real, real)
                else:
                    blob = payload
                    if len(blob) < 512:
                        continue
                real_ext = sniff_ext(blob)
                if not real_ext:
                    overwritten += 1   # clusters already reused by another file
                    continue
                path = _build_path(dirs, parent, cache)
                full = "\\" + (path + "\\" if path else "") + name
                stem = os.path.splitext(name)[0]
                if args.dry_run:
                    saved += 1
                    print("  would recover %s  (%s)%s" % (
                        full, human(len(blob)), "" if in_use else "   [deleted]"))
                    continue
                if out.save_bytes(blob, real_ext, full, name_hint=stem):
                    saved += 1
                    if args.verbose:
                        print("  + %s%s" % (full, "" if in_use else "   [deleted]"))
                if found % 200 == 0:
                    out.flush()
            print("[mft] %d candidate files, %d %s, %d had their data overwritten"
                  % (found, saved, "would be recovered (dry run)" if args.dry_run else "recovered",
                     overwritten))
    finally:
        reader.close()


# --------------------------------------------------------------------------
# Phase: Volume Shadow Copies
# --------------------------------------------------------------------------

def list_shadow_copies():
    if not IS_WIN:
        return []
    out, _ = run_cmd(["vssadmin", "list", "shadows"], timeout=120)
    return re.findall(r"(\\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy\d+)", out)


def phase_vss(out, args):
    shadows = list_shadow_copies()
    if not shadows:
        print("[vss] no shadow copies found (run elevated; `vssadmin list shadows`)")
        return
    print("[vss] %d shadow copies found" % len(shadows))
    for s in shadows:
        for sub in ("Users", ""):
            path = s + "\\" + sub if sub else s + "\\"
            if os.path.isdir(path):
                phase_sweep(path, out, args, label="vss")
                break


# --------------------------------------------------------------------------
# Listing / safety
# --------------------------------------------------------------------------

PS_LIST = r"""
Get-Disk | Sort-Object Number | ForEach-Object {
  $d = $_
  "DISK {0}  {1}  {2:N1} GB  {3}  Partitions: {4}" -f $d.Number, $d.FriendlyName, ($d.Size/1GB), $d.PartitionStyle, $d.NumberOfPartitions
  Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | ForEach-Object {
    $p = $_
    $v = Get-Volume -Partition $p -ErrorAction SilentlyContinue
    "    part {0}  letter {1}  {2:N1} GB  {3}  {4}" -f $p.PartitionNumber, $(if($p.DriveLetter){$p.DriveLetter}else{'-'}), ($p.Size/1GB), $v.FileSystemType, $v.FileSystemLabel
  }
}
"""


def list_disks():
    if not IS_WIN:
        print("Not on Windows - point --source at a disk image file.")
        return
    out, err = run_cmd(["powershell", "-NoProfile", "-Command", PS_LIST], timeout=180)
    text = out.strip() or err.strip()
    print(text if text else "could not list disks (is powershell on PATH?)")
    print("\nScan a whole disk with:  --source \\\\.\\PhysicalDrive<N>")
    print("Scan one partition with: --source <DriveLetter>:")


def is_admin():
    if not IS_WIN:
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def output_on_source(source_path, out_root):
    """Refuse to write recovered data back onto the disk being recovered."""
    if not IS_WIN:
        return False
    out_letter = os.path.splitdrive(os.path.abspath(out_root))[0].rstrip(":").upper()
    if not out_letter:
        return False
    m = re.search(r"(?i)PhysicalDrive(\d+)", source_path)
    if m:
        out, _ = run_cmd(["powershell", "-NoProfile", "-Command",
                          "(Get-Partition -DiskNumber %s).DriveLetter" % m.group(1)], timeout=60)
        letters = set(c.strip().upper() for c in out.split() if c.strip())
        return out_letter in letters
    m = re.search(r"(?i)^\\\\\.\\([A-Z]):$", source_path)
    if m:
        return out_letter == m.group(1).upper()
    return False


# --------------------------------------------------------------------------

def main():
    if sys.version_info < (3, 6):
        print("PhotoRescue needs Python 3.6 or newer (you have %s).\n"
              "Install a current one with:  winget install -e --id Python.Python.3.12\n"
              "then run it as:  py -3.12 photorescue.py ..." % sys.version.split()[0])
        return 1
    ap = argparse.ArgumentParser(description="PhotoRescue - deep photo recovery")
    ap.add_argument("--list", action="store_true", help="list disks/partitions and exit")
    ap.add_argument("--source", help="D:  |  \\\\.\\PhysicalDrive1  |  1  |  image.img")
    ap.add_argument("--out", help="output folder (MUST be on a different disk)")
    ap.add_argument("--phases", default="carve",
                    help="comma list of: sweep,bin,mft,vss,carve  (default carve)")
    ap.add_argument("--types", default="all",
                    help="jpg,png,gif,bmp,psd,raw,heic,video or all (default all)")
    ap.add_argument("--min-size", type=int, default=16 * KB, help="ignore carved files below this (bytes)")
    ap.add_argument("--block", type=int, default=16, help="read block size in MB (default 16)")
    ap.add_argument("--start", type=int, default=0, help="start byte offset for carving")
    ap.add_argument("--end", type=int, default=0, help="end byte offset for carving (0 = end of disk)")
    ap.add_argument("--skip-video", action="store_true", help="do not recover mp4/mov/avi")
    ap.add_argument("--folder", help="only recover from folders whose name contains this "
                                     "(applies to the mft/sweep/vss phases)")
    ap.add_argument("--dry-run", action="store_true",
                    help="mft phase: list what would be recovered, write nothing")
    ap.add_argument("--deleted-only", action="store_true",
                    help="mft phase: skip files that still exist, recover only deleted ones")
    ap.add_argument("--no-organize", action="store_true", help="do not create YYYY-MM subfolders")
    ap.add_argument("--resume", action="store_true", help="resume a previous run into the same --out")
    ap.add_argument("--force", action="store_true", help="skip safety checks")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.list:
        list_disks()
        return 0
    if not args.source or not args.out:
        ap.print_help()
        return 2

    source = normalize_source(args.source)
    is_device = source.startswith("\\\\.\\")

    if IS_WIN and is_device and not is_admin():
        print("ERROR: raw disk access needs an elevated prompt. "
              "Right-click PowerShell -> Run as administrator.")
        return 1
    if not is_device and not os.path.exists(source):
        print("ERROR: source not found: %s" % source)
        return 1

    if output_on_source(source, args.out) and not args.force:
        print("ERROR: --out is on the same disk you are recovering. Writing there will "
              "overwrite the very data you are trying to get back.\n"
              "       Use an external drive or another disk (or --force if you really mean it).")
        return 1

    out = Output(args.out, resume=args.resume, organize=not args.no_organize)
    phases = [p.strip().lower() for p in args.phases.split(",") if p.strip()]
    t0 = time.time()
    try:
        if "sweep" in phases:
            root = source if not is_device else None
            if root is None:
                m = re.search(r"(?i)^\\\\\.\\([A-Z]):$", source)
                root = (m.group(1) + ":\\") if m else None
            if root:
                phase_sweep(root if root.endswith("\\") or not IS_WIN else root + "\\", out, args)
            else:
                print("[sweep] skipped: sweep needs a mounted volume (use --source D:)")
        if "bin" in phases:
            if IS_WIN:
                m = re.search(r"(?i)^\\\\\.\\([A-Z]):$", source)
                vol = (m.group(1) + ":") if m else (source.rstrip("\\") if len(source) <= 3 else None)
                if vol:
                    phase_recyclebin(vol, out, args)
                else:
                    print("[bin] skipped: needs a drive letter source")
            else:
                print("[bin] Windows only")
        if "mft" in phases:
            phase_mft(source, out, args)
        if "vss" in phases:
            phase_vss(out, args)
        if "carve" in phases:
            phase_carve(source, out, args)
    finally:
        out.close()
    print("\n================ PhotoRescue finished in %s ================" %
          timedelta(seconds=int(time.time() - t0)))
    print(out.summary())
    print("Output    : %s" % out.root)
    print("Manifest  : %s" % out.manifest_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
