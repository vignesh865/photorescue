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
# Phase: Volume Shadow Copies
# --------------------------------------------------------------------------

def list_shadow_copies():
    if not IS_WIN:
        return []
    try:
        cp = subprocess.run(["vssadmin", "list", "shadows"], capture_output=True, text=True, timeout=120)
        out = cp.stdout
    except Exception:
        return []
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
    try:
        cp = subprocess.run(["powershell", "-NoProfile", "-Command", PS_LIST],
                            capture_output=True, text=True, timeout=180)
        print(cp.stdout.strip() or cp.stderr.strip())
    except Exception as e:
        print("could not list disks: %s" % e)
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
        try:
            cp = subprocess.run(["powershell", "-NoProfile", "-Command",
                                 "(Get-Partition -DiskNumber %s).DriveLetter" % m.group(1)],
                                capture_output=True, text=True, timeout=60)
            letters = {c.strip().upper() for c in cp.stdout.split() if c.strip()}
            return out_letter in letters
        except Exception:
            return False
    m = re.search(r"(?i)^\\\\\.\\([A-Z]):$", source_path)
    if m:
        return out_letter == m.group(1).upper()
    return False


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="PhotoRescue - deep photo recovery")
    ap.add_argument("--list", action="store_true", help="list disks/partitions and exit")
    ap.add_argument("--source", help="D:  |  \\\\.\\PhysicalDrive1  |  1  |  image.img")
    ap.add_argument("--out", help="output folder (MUST be on a different disk)")
    ap.add_argument("--phases", default="carve",
                    help="comma list of: sweep,bin,vss,carve  (default carve)")
    ap.add_argument("--types", default="all",
                    help="jpg,png,gif,bmp,psd,raw,heic,video or all (default all)")
    ap.add_argument("--min-size", type=int, default=16 * KB, help="ignore carved files below this (bytes)")
    ap.add_argument("--block", type=int, default=16, help="read block size in MB (default 16)")
    ap.add_argument("--start", type=int, default=0, help="start byte offset for carving")
    ap.add_argument("--end", type=int, default=0, help="end byte offset for carving (0 = end of disk)")
    ap.add_argument("--skip-video", action="store_true", help="do not recover mp4/mov/avi")
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
