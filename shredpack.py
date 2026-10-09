"""
ShredPack - a WinRAR-style archive extractor with its own built-in engine.

* No WinRAR, no 7-Zip: ShredPack extracts archives itself.
* Right-click any archive -> ShredPack -> Extract files... / Extract Here /
  Extract to folder named after the archive.
* The program only runs when you open it or pick a right-click option. It does
  its job and exits; nothing stays running in the background.

Python packages bundled into the .exe at build time:
    pip install py7zr pycdlib pyzipper tkinterdnd2 send2trash pyinstaller
(py7zr adds .7z, pycdlib adds .iso, pyzipper adds AES-encrypted ZIPs,
 tkinterdnd2 adds drag-and-drop, send2trash sends deleted originals to the
 Recycle Bin. All are optional at run time.)
"""

import os
import re
import sys
import bz2
import fnmatch
import gzip
import hashlib
import json
import lzma
import posixpath
import queue
import shutil
import struct
import tarfile
import tempfile
import threading
import time
import traceback
import unicodedata
import urllib.request
import zipfile
import zlib
import webbrowser
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

if sys.platform == "win32":
    import ctypes
    import winreg

try:
    import py7zr
except Exception:  # noqa: BLE001
    py7zr = None

try:
    import pycdlib
except Exception:  # noqa: BLE001
    pycdlib = None

try:
    import pyzipper
except Exception:  # noqa: BLE001
    pyzipper = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # noqa: BLE001
    DND_FILES, TkinterDnD = None, None

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
APP_NAME = "ShredPack"
APP_VERSION = "1.1.0"
MENU_KEY = "ShredPack"
COMPRESS_MENU_KEY = "ShredPackCompress"
# Registry classes the "Add to..." submenu is installed under: any file, and
# any folder. (Not Directory\Background - see the note in install_menu.)
COMPRESS_CLASSES = ("*", "Directory")

# Point this at a raw text file you publish containing just the latest version
# string (e.g. "1.1.0"), such as a raw GitHub URL to a VERSION.txt in your repo.
# Leave blank to disable the update check entirely.
VERSION_URL = "https://raw.githubusercontent.com/Kchecksfiles/ShredPack/main/VERSION.txt"
RELEASES_URL = "https://github.com/Kchecksfiles/ShredPack/releases"  # opened from the update link

EXTENSIONS = (
    ".zip", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".tbz", ".txz",
    ".iso", ".cab", ".xar", ".z01", ".zip.001",
)
TAR_COMPRESSED = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz")
FORMATS_LABEL = "ZIP  \u2022  7Z  \u2022  TAR  \u2022  GZ  \u2022  BZ2  \u2022  XZ  \u2022  ISO  \u2022  CAB  \u2022  XAR  \u2022  split ZIP"

# A decompression-bomb / low-disk-space guard. If an archive looks like it will
# expand past this ratio, or won't fit the destination drive, the user is asked
# to confirm before anything is written.
MAX_SAFE_RATIO = 300
MIN_FREE_MARGIN = 1.05  # require 5% headroom beyond the estimated output size

LOG_MAX_BYTES = 512 * 1024

# Ask before compressing anything bigger than this (the scan stops counting once
# either limit is reached, so even a huge selection is checked instantly).
CONFIRM_FILES = 100000
CONFIRM_BYTES = 5 * 1024 ** 3
BROWSE_LIMIT = 20000  # rows shown in the archive browser at once (search narrows the rest)

# (registry sub-key name, menu text, mode). Explorer sorts sub-items by key name.
# "Extract Here" always creates a new folder named after the archive right next
# to it (see compute_dest) - there's no separate flat-extract option, to avoid
# ever dumping an archive's loose files straight into an existing folder.
MENU_ITEMS = (
    ("1extract", "Extract files...", "dialog"),
    ("2here", "Extract Here", "here"),
    ("3test", "Test archive", "test"),
)

# The "Add to..." submenu shown on ordinary files and folders (not archives).
COMPRESS_MENU_ITEMS = (
    ("1addzip", "Add to ZIP", "compress-quick"),
    ("2addarchive", "Add to archive...", "compress-dialog"),
)

CHUNK = 1024 * 1024
INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"
BG = "#1f1f1f"
CARD = "#2b2b2b"
BORDER = "#3c3c3c"
TEXT = "#f3f3f3"
MUTED = "#a0a0a0"
ACCENT = "#60cdff"
ICON_BG = "#343434"
TROUGH = "#3d3d3d"
GREEN = "#6ccb5f"
FIELD = "#333333"


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------
class ArchiveError(Exception):
    """An error whose message can be shown to the user as-is."""


class Unsupported(ArchiveError):
    pass


class NeedPassword(Exception):
    def __init__(self, retry=False):
        Exception.__init__(self, "password required")
        self.retry = retry


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------
def fmt_mtime(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
    except (OverflowError, ValueError, OSError):
        return ""


def shorten(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def fmt_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "%d B" % size if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % n


def lp(path):
    """Extended-length path on Windows for very long paths (idempotent)."""
    if sys.platform != "win32":
        return path
    p = os.path.abspath(path)
    if len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


BIDI_OVERRIDE_CHARS = {
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",  # LRE RLE PDF LRO RLO
    "\u2066", "\u2067", "\u2068", "\u2069",  # LRI RLI FSI PDI
}


def strip_bidi_overrides(name):
    """Remove Unicode bidi-override characters used to disguise file extensions
    (e.g. making 'evil<RLO>gpj.exe' display as 'eviloexe.jpg')."""
    cleaned = "".join(c for c in name if c not in BIDI_OVERRIDE_CHARS)
    return unicodedata.normalize("NFC", cleaned)


def clean_parts(name):
    """Split an archive member name into safe path components."""
    parts = []
    for p in name.replace("\\", "/").split("/"):
        if p in ("", ".", ".."):
            continue
        p = strip_bidi_overrides(p)
        p = INVALID_CHARS.sub("_", p).rstrip(" .")
        if not p:
            continue
        if p.upper().split(".")[0] in RESERVED:
            p = "_" + p
        parts.append(p)
    return parts


def sanitize_name(name):
    parts = clean_parts(name)
    return parts[-1] if parts else ""


def archive_stem(path):
    base = os.path.basename(path)
    low = base.lower()
    for ext in TAR_COMPRESSED + (".tar",):
        if low.endswith(ext):
            base = base[: -len(ext)]
            break
    else:
        base = os.path.splitext(base)[0]
    return sanitize_name(base) or "Extracted"


def unique_path(parent, name):
    """Folder-style unique name: 'Name', 'Name (2)', ..."""
    candidate = os.path.join(parent, name)
    i = 2
    while os.path.exists(lp(candidate)):
        candidate = os.path.join(parent, "%s (%d)" % (name, i))
        i += 1
    return candidate


def unique_file_path(parent, name):
    """File-style unique name: 'a.txt', 'a (2).txt', ..."""
    candidate = os.path.join(parent, name)
    stem, ext = os.path.splitext(name)
    i = 2
    while os.path.exists(lp(candidate)):
        candidate = os.path.join(parent, "%s (%d)%s" % (stem, i, ext))
        i += 1
    return candidate


def is_junk(filename):
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    return (not parts) or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store"


def remove_original(path):
    p = os.path.abspath(path)
    if send2trash is not None:
        try:
            send2trash(p)
            return True
        except Exception:  # noqa: BLE001
            pass
    try:
        os.remove(p)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Diagnostics: crash/error log
# --------------------------------------------------------------------------
def app_data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def log_path():
    return os.path.join(app_data_dir(), "log.txt")


def log_write(message):
    try:
        os.makedirs(app_data_dir(), exist_ok=True)
        path = log_path()
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(b"--- (earlier log trimmed) ---\n")
                f.write(tail)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (stamp, message))
    except OSError:
        pass


def log_exception(context):
    log_write("%s\n%s" % (context, traceback.format_exc()))


def install_crash_handler():
    def handle(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log_write("UNHANDLED EXCEPTION\n" + text)
        try:
            messagebox.showerror(
                APP_NAME,
                "ShredPack hit an unexpected error and needs to close.\n\n"
                "Details were saved to:\n%s" % log_path(),
            )
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = handle


# --------------------------------------------------------------------------
# Update check (optional - reads a plain-text version file you publish)
# --------------------------------------------------------------------------
def parse_version(text):
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3]) or (0,)


def check_for_update(callback):
    """Background-safe: fetches VERSION_URL and calls callback(latest_str or None)."""
    if not VERSION_URL:
        return
    def worker():
        try:
            with urllib.request.urlopen(VERSION_URL, timeout=4) as resp:
                latest = resp.read(200).decode("utf-8", "replace").strip()
            if parse_version(latest) > parse_version(APP_VERSION):
                callback(latest)
            else:
                callback(None)
        except Exception:  # noqa: BLE001
            callback(None)
    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------
# Mark of the Web (Zone.Identifier) propagation
# --------------------------------------------------------------------------
def read_zone_identifier(path):
    """Return the raw bytes of a file's Zone.Identifier NTFS stream, or None."""
    if sys.platform != "win32":
        return None
    try:
        with open(path + ":Zone.Identifier", "rb") as f:
            return f.read()
    except OSError:
        return None


def write_zone_identifier(path, data):
    if sys.platform != "win32" or not data:
        return
    try:
        with open(path + ":Zone.Identifier", "wb") as f:
            f.write(data)
    except OSError:
        pass


def propagate_mark_of_the_web(archive_path, dest_root, new_files):
    """Copy the archive's own download mark onto everything just extracted,
    so Windows SmartScreen still warns before running an extracted .exe,
    the same way Explorer's own 'Extract All' behaves."""
    zone = read_zone_identifier(archive_path)
    if not zone:
        return
    for rel in new_files:
        write_zone_identifier(os.path.join(dest_root, rel), zone)


# --------------------------------------------------------------------------
# Decompression-bomb / low-disk-space guard
# --------------------------------------------------------------------------
def estimate_uncompressed_size(path, fmt):
    """Best-effort uncompressed size for a pre-flight check. Returns None if unknown."""
    try:
        if fmt == "zip":
            with zipfile.ZipFile(path) as zf:
                return sum(i.file_size for i in zf.infolist())
        if fmt == "7z" and py7zr is not None:
            with py7zr.SevenZipFile(path, mode="r") as z:
                return sum(f.uncompressed for f in z.list() if not f.is_directory)
        if fmt == "cab":
            return None  # header-only estimate isn't worth the parse cost twice
    except Exception:  # noqa: BLE001
        return None
    return None


def disk_free_bytes(path):
    try:
        probe = path
        while probe and not os.path.exists(lp(probe)):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        return shutil.disk_usage(probe or ".").free
    except OSError:
        return None


def preflight_risk(path, dest, fmt):
    """Returns a warning string if extraction looks risky, else None."""
    try:
        compressed = os.path.getsize(path)
    except OSError:
        compressed = 0
    uncompressed = estimate_uncompressed_size(path, fmt)
    warnings = []

    if uncompressed and compressed and compressed > 0:
        ratio = uncompressed / float(compressed)
        if ratio > MAX_SAFE_RATIO and uncompressed > 200 * 1024 * 1024:
            warnings.append(
                "This archive would expand to about %s from a %s file (%.0fx). "
                "That's unusually large and could be a decompression bomb."
                % (fmt_size(uncompressed), fmt_size(compressed), ratio)
            )

    if uncompressed:
        free = disk_free_bytes(dest)
        if free is not None and uncompressed * MIN_FREE_MARGIN > free:
            warnings.append(
                "This needs about %s of free space, but only %s is available there."
                % (fmt_size(int(uncompressed * MIN_FREE_MARGIN)), fmt_size(free))
            )

    return "\n\n".join(warnings) if warnings else None


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def enable_dark_titlebar(root):
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)
            ) == 0:
                break
        caption = ctypes.c_int(0x001F1F1F)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption)
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Extraction engine (pure Python - no external programs)
# --------------------------------------------------------------------------
class Ctx(object):
    """Progress, cancellation, password and per-run options for one operation.

    selected   - None, or a set of lowercase archive paths ("dir/file.txt"); a
                 folder path selects everything beneath it
    include /
    exclude    - lists of lowercase wildcard patterns (e.g. "*.jpg")
    dry_run    - read and verify everything but write nothing (Test Archive)
    overwrite  - what to do when a file already exists at the destination:
                 "rename" | "overwrite" | "skip" | "newer"
    notes      - human-readable remarks collected along the way
    """

    def __init__(self, cancel, on_progress, password=None, selected=None,
                 include=None, exclude=None, dry_run=False, overwrite="rename"):
        self.cancel = cancel
        self.on_progress = on_progress
        self.password = password
        self.selected = selected
        self.include = [p.lower() for p in (include or [])]
        self.exclude = [p.lower() for p in (exclude or [])]
        self.dry_run = dry_run
        self.overwrite = overwrite
        self.notes = []
        self.files = 0
        self.total = 0
        self.done = 0
        self._last = 0.0

    def _check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _emit(self, force=False):
        now = time.monotonic()
        if force or now - self._last >= 0.05:
            self._last = now
            pct = min(100.0, self.done * 100.0 / self.total) if self.total else 0.0
            self.on_progress(pct, min(self.done, self.total) if self.total else 0, self.total)

    def add(self, n):
        self._check()
        self.done += n
        self._emit()

    def set_position(self, pos, size):
        self._check()
        self.done, self.total = pos, max(1, size)
        self._emit()

    def finish(self):
        if self.total:
            self.done = self.total
        self._emit(True)

    def wants(self, parts, is_dir=False):
        """Should the archive member with these path components be processed?"""
        low = "/".join(parts).lower()
        if self.selected is not None:
            if not any(low == s or low.startswith(s + "/") for s in self.selected):
                return False
        if is_dir:
            # With include patterns active, folders appear only as needed to hold files.
            return not self.include
        name = parts[-1].lower()
        if self.include and not any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(low, p)
                                    for p in self.include):
            return False
        if self.exclude and any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(low, p)
                                for p in self.exclude):
            return False
        return True


def parse_patterns(text):
    """'*.jpg, *.png;*.pdf' -> ['*.jpg', '*.png', '*.pdf']"""
    return [p.strip() for p in re.split(r"[,;\n]+", text or "") if p.strip()]


def make_dir(stage, parts):
    os.makedirs(lp(os.path.join(stage, *parts)), exist_ok=True)


def write_member(stage, parts, src, ctx, raw=None, raw_size=1):
    """Stream `src` into stage/parts. Progress by bytes, or by raw file position.
    Returns the written path, or None if the member was filtered out or this is
    a dry run (in which case the data is still fully read, so CRC and
    decompression errors surface)."""
    if not ctx.wants(parts):
        return None
    if ctx.dry_run:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            if raw is not None:
                ctx.set_position(raw.tell(), raw_size)
            else:
                ctx.add(len(chunk))
        ctx.files += 1
        return None
    target = os.path.join(stage, *parts)
    os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
    with open(lp(target), "wb") as dst:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            dst.write(chunk)
            if raw is not None:
                ctx.set_position(raw.tell(), raw_size)
            else:
                ctx.add(len(chunk))
    ctx.files += 1
    return target


def is_dir_entry(info):
    return info.is_dir() or info.filename.replace("\\", "/").endswith("/")


# ---- ZIP ------------------------------------------------------------------
def _is_aes_info(info):
    """A ZIP entry using WinZip AES encryption carries extra field 0x9901."""
    extra = info.extra
    i = 0
    while i + 4 <= len(extra):
        hid, size = struct.unpack("<HH", extra[i:i + 4])
        if hid == 0x9901:
            return True
        i += 4 + size
    return False


def extract_zip(path, stage, ctx):
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not is_junk(i.filename)]
        if not infos:
            raise ArchiveError("The archive is empty.")
        encrypted = any(i.flag_bits & 0x1 for i in infos)
        uses_aes = any(_is_aes_info(i) for i in infos)
        pw = ctx.password.encode("utf-8") if ctx.password else None
        if encrypted and pw is None:
            raise NeedPassword(False)

        if uses_aes:
            if pyzipper is None:
                raise Unsupported(
                    "This ZIP is AES-encrypted. AES support isn't included in this build "
                    "(the pyzipper package is missing)."
                )
            _extract_zip_with(pyzipper.AESZipFile(path), infos, stage, ctx, pw, encrypted)
            return
        _extract_zip_with(zf, infos, stage, ctx, pw, encrypted, already_open=True)


def _extract_zip_with(zf, infos, stage, ctx, pw, encrypted, already_open=False):
    try:
        ctx.total = max(1, sum(i.file_size for i in infos))
        for info in infos:
            parts = clean_parts(info.filename)
            if not parts:
                continue
            if is_dir_entry(info):
                if not ctx.dry_run and ctx.wants(parts, True):
                    make_dir(stage, parts)
                continue
            if not ctx.wants(parts):
                continue
            try:
                with zf.open(info, pwd=pw if info.flag_bits & 0x1 else None) as src:
                    target = write_member(stage, parts, src, ctx)
            except RuntimeError as exc:
                if "password" in str(exc).lower() or "Bad password" in str(exc):
                    raise NeedPassword(pw is not None)
                raise
            except NotImplementedError:
                raise ArchiveError(
                    "This ZIP uses a compression method ShredPack can't read."
                )
            except zipfile.BadZipFile:
                if encrypted and pw is not None:
                    raise NeedPassword(True)
                raise
            if target:
                try:
                    ts = time.mktime(tuple(info.date_time) + (0, 0, -1))
                    os.utime(lp(target), (ts, ts))
                except (OverflowError, ValueError, OSError):
                    pass
    finally:
        if not already_open:
            zf.close()


# ---- multi-part / split archives -------------------------------------------
def detect_split_parts(path):
    """If `path` is one piece of a split archive, return the ordered list of
    all part paths; otherwise None. Supports PKZIP-style name.z01..name.zip
    and the generic name.zip.001/.002... convention produced by many archivers."""
    folder = os.path.dirname(path)
    base = os.path.basename(path)
    low = base.lower()

    def exists(name):
        return os.path.isfile(os.path.join(folder, name))

    # name.zip.001, name.zip.002, ... (case-preserving: strip the numeric suffix
    # from the actual filename rather than the lowercased copy)
    m = re.match(r"^(.+\.zip)\.(\d{2,4})$", low)
    width = len(m.group(2)) if m else (3 if low.endswith(".zip") and exists(base + ".001") else 0)
    if width:
        stem_name = base[: -(width + 1)] if m else base
        parts, n = [], 1
        while exists("%s.%s" % (stem_name, str(n).zfill(width))):
            parts.append(os.path.join(folder, "%s.%s" % (stem_name, str(n).zfill(width))))
            n += 1
        if len(parts) > 1:
            return parts

    # name.z01, name.z02, ..., name.zip (the .zip piece holds the central directory)
    stem = None
    if re.match(r"^.+\.z\d{2,3}$", low):
        stem = base[: base.rfind(".")]
    elif low.endswith(".zip"):
        stem = base[:-4]
    if stem is not None:
        final = os.path.join(folder, stem + ".zip")
        if os.path.isfile(final):
            parts, n = [], 1
            while True:
                hit = None
                for width in (2, 3):
                    cand = os.path.join(folder, "%s.z%s" % (stem, str(n).zfill(width)))
                    if os.path.isfile(cand):
                        hit = cand
                        break
                if hit is None:
                    break
                parts.append(hit)
                n += 1
            if parts:
                return parts + [final]
    return None


def join_split_parts(parts, workdir):
    """Concatenate split-archive parts into one temp file and return its path."""
    joined = os.path.join(workdir, "joined.zip")
    with open(lp(joined), "wb") as out:
        for part in parts:
            with open(lp(part), "rb") as f:
                shutil.copyfileobj(f, out, CHUNK)
    return joined


# ---- TAR (plain, .gz, .bz2, .xz) --------------------------------------------
def _set_mtime(path, mtime):
    try:
        os.utime(lp(path), (mtime, mtime))
    except (OverflowError, ValueError, OSError):
        pass


def _resolve_link(parts, member):
    """Archive-relative key of the file a TAR link points at, or None if it
    points outside the archive (absolute or ../ escapes are never followed)."""
    target = member.linkname.replace("\\", "/")
    if member.islnk():
        resolved = posixpath.normpath(target)
    else:
        if target.startswith("/"):
            return None
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname("/".join(parts)), target))
    if resolved.startswith("..") or resolved.startswith("/"):
        return None
    cleaned = clean_parts(resolved)
    return "/".join(cleaned).lower() if cleaned else None


def extract_tar(path, stage, ctx):
    """Extract a TAR (plain or compressed). Regular files and folders are
    extracted with their timestamps. Symbolic/hard links are never created as
    real links (that can be abused to write outside the destination); when a
    link points at a regular file inside the same archive, that file's
    contents are copied to the link's location instead. Anything else
    (devices, links to folders, dangling links) is skipped and reported."""
    size = max(1, os.path.getsize(path))
    written = {}      # "a/b.txt" (lowercase) -> staged file path
    pending = []      # (parts, member) links to resolve once all files exist
    special = 0
    with open(path, "rb") as raw:
        # Not stream mode ("r|*"): that skips the gzip trailer CRC, so a corrupted
        # .tar.gz could extract without any complaint. Draining to EOF below makes
        # the decompressor verify its checksum.
        with tarfile.open(fileobj=raw, mode="r:*") as tf:
            for member in tf:
                parts = clean_parts(member.name)
                if not parts:
                    continue
                if member.isdir():
                    if not ctx.dry_run and ctx.wants(parts, True):
                        make_dir(stage, parts)
                elif member.isreg():
                    src = tf.extractfile(member)
                    if src is not None:
                        target = write_member(stage, parts, src, ctx, raw, size)
                        if target:
                            _set_mtime(target, member.mtime)
                            written["/".join(parts).lower()] = target
                elif member.issym() or member.islnk():
                    if ctx.wants(parts):
                        pending.append((parts, member))
                else:
                    special += 1
                ctx.set_position(raw.tell(), size)
            stream = tf.fileobj
            while stream is not None and stream.read(CHUNK):
                ctx._check()

    skipped = special
    if not ctx.dry_run:
        progress = True
        while pending and progress:
            progress = False
            for item in list(pending):
                parts, member = item
                key = _resolve_link(parts, member)
                if key is None:
                    continue
                source = written.get(key)
                if not source:
                    continue
                dest = os.path.join(stage, *parts)
                os.makedirs(lp(os.path.dirname(dest)), exist_ok=True)
                shutil.copyfile(lp(source), lp(dest))
                _set_mtime(dest, member.mtime)
                written["/".join(parts).lower()] = dest
                pending.remove(item)
                progress = True
        skipped += len(pending)
    if skipped:
        ctx.notes.append(
            "%d link(s) or special file(s) couldn't be restored and were skipped." % skipped)


# ---- single-file .gz / .bz2 / .xz ---------------------------------------------
def extract_single(path, stage, ctx):
    size = max(1, os.path.getsize(path))
    with open(path, "rb") as raw:
        head = raw.read(6)
        raw.seek(0)
        if head[:2] == b"\x1f\x8b":
            src = gzip.GzipFile(fileobj=raw)
        elif head[:3] == b"BZh":
            src = bz2.BZ2File(raw)
        else:
            src = lzma.LZMAFile(raw)
        write_member(stage, [archive_stem(path)], src, ctx, raw, size)


def sanitize_stage(stage, ctx=None):
    """Clean a folder written by a library that doesn't sanitise names itself
    (py7zr): strip bidi-override and invalid characters, defuse reserved
    Windows names, and delete any symbolic links. Returns links removed."""
    removed = 0
    for root, dirs, files in os.walk(lp(stage), topdown=False):
        for name in files + dirs:
            full = os.path.join(root, name)
            if os.path.islink(full):
                try:
                    os.remove(full)
                except OSError:
                    try:
                        os.rmdir(full)
                    except OSError:
                        pass
                removed += 1
                continue
            cleaned = "_".join(clean_parts(name)) or "_"
            if cleaned != name:
                try:
                    os.rename(full, os.path.join(root, os.path.basename(
                        unique_file_path(root, cleaned))))
                except OSError:
                    pass
    if removed and ctx is not None:
        ctx.notes.append("%d symbolic link(s) in the archive were not restored." % removed)
    return removed


# ---- 7Z (py7zr) ---------------------------------------------------------------
def extract_7z(path, stage, ctx):
    if py7zr is None:
        raise Unsupported("7Z support isn't included in this build (the py7zr package is missing).")
    from py7zr.callbacks import ExtractCallback

    class Progress(ExtractCallback):
        def report_start_preparation(self):
            ctx._check()

        def report_start(self, processing_file_path, processing_bytes):
            ctx._check()

        def report_end(self, processing_file_path, wrote_bytes):
            ctx.add(int(wrote_bytes))

        def report_warning(self, message):
            pass

        def report_postprocess(self):
            pass

    pw = ctx.password or None
    try:
        with py7zr.SevenZipFile(path, mode="r", password=pw) as z:
            if z.needs_password() and not pw:
                raise NeedPassword(False)
            try:
                file_infos = [f for f in z.list() if not f.is_directory]
                ctx.total = max(1, sum(f.uncompressed for f in file_infos))
            except Exception:  # noqa: BLE001
                file_infos, ctx.total = [], 0
            if ctx.dry_run:
                if z.test() is False:
                    raise ArchiveError("The archive failed its integrity check - its data is corrupted.")
                ctx.files = len(file_infos)
                return
            if ctx.selected is not None or ctx.include or ctx.exclude:
                names = [n for n in z.getnames() if ctx.wants(clean_parts(n) or ["_"])]
                if not names:
                    raise ArchiveError("Nothing in the archive matches the selection or filters.")
                try:
                    z.extract(path=stage, targets=names, callback=Progress())
                except TypeError:  # older py7zr without callback support on extract()
                    z.extract(path=stage, targets=names)
            else:
                z.extractall(path=stage, callback=Progress())
        sanitize_stage(stage, ctx)
    except (Cancelled, NeedPassword, ArchiveError):
        raise
    except Exception as exc:  # noqa: BLE001
        if pw and "password" in (str(exc) + exc.__class__.__name__).lower():
            raise NeedPassword(True)
        if pw and exc.__class__.__name__ in ("CrcError", "Bad7zFile", "UnsupportedCompressionMethodError"):
            raise NeedPassword(True)
        if exc.__class__.__name__ == "PasswordRequired":
            raise NeedPassword(False)
        raise


# ---- ISO (pycdlib) ------------------------------------------------------------
def _iso_facade(iso):
    if iso.has_udf():
        return iso.get_udf_facade()
    if iso.has_joliet():
        return iso.get_joliet_facade()
    if iso.has_rock_ridge():
        return iso.get_rock_ridge_facade()
    return iso.get_iso9660_facade()


def _iso_collect(facade):
    """-> (folder paths, [(file path, length)]) as stored in the image."""
    dirs, files = [], []
    for dirname, dirlist, filelist in facade.walk("/"):
        base = dirname if dirname.endswith("/") else dirname + "/"
        for d in dirlist:
            dirs.append(base + d)
        for fn in filelist:
            full = base + fn
            try:
                length = int(facade.get_record(full).get_data_length())
            except Exception:  # noqa: BLE001
                length = 0
            files.append((full, length))
    return dirs, files


def _iso_parts(stored_path):
    return clean_parts(re.sub(r";\d+$", "", stored_path))


def extract_iso(path, stage, ctx):
    if pycdlib is None:
        raise Unsupported("ISO support isn't included in this build (the pycdlib package is missing).")
    iso = pycdlib.PyCdlib()
    iso.open(path)
    try:
        facade = _iso_facade(iso)
        dirs, files = _iso_collect(facade)
        files = [(f, n) for f, n in files if (_iso_parts(f) and ctx.wants(_iso_parts(f)))]
        ctx.total = max(1, sum(n for _f, n in files))

        if not ctx.dry_run:
            for d in dirs:
                parts = _iso_parts(d)
                if parts and ctx.wants(parts, True):
                    make_dir(stage, parts)
        for full, length in files:
            ctx._check()
            parts = _iso_parts(full)
            if ctx.dry_run:
                with open(os.devnull, "wb") as out:
                    facade.get_file_from_iso_fp(out, full)
            else:
                target = os.path.join(stage, *parts)
                os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
                with open(lp(target), "wb") as out:
                    facade.get_file_from_iso_fp(out, full)
            ctx.files += 1
            ctx.add(length)
    finally:
        try:
            iso.close()
        except Exception:  # noqa: BLE001
            pass


# ---- CAB (stored + MSZIP) -----------------------------------------------------
def _cab_csum(buf, seed=0):
    """The cabinet-format block checksum: XOR of little-endian 32-bit words,
    with a trailing 1-3 bytes folded in as a partial word."""
    cs = seed
    full = len(buf) & ~3
    for (word,) in struct.iter_unpack("<I", buf[:full]):
        cs ^= word
    tail = buf[full:]
    ul = 0
    if len(tail) == 3:
        ul = (tail[0] << 16) | (tail[1] << 8) | tail[2]
    elif len(tail) == 2:
        ul = (tail[0] << 8) | tail[1]
    elif len(tail) == 1:
        ul = tail[0]
    return cs ^ ul


class CabReader(object):
    """Sequential reader over the uncompressed stream of one CAB folder."""

    def __init__(self, path, coff, n_data, method, data_res):
        self.f = open(path, "rb")
        self.f.seek(coff)
        self.blocks = n_data
        self.method = method
        self.data_res = data_res
        self.buf = bytearray()
        self.prev = b""

    def close(self):
        self.f.close()

    def _fill(self):
        hdr = self.f.read(8)
        if len(hdr) < 8:
            raise ArchiveError("The CAB file is truncated or corrupted.")
        csum, cb_data, _cb_uncomp = struct.unpack("<IHH", hdr)
        if self.data_res:
            self.f.read(self.data_res)
        data = self.f.read(cb_data)
        if len(data) < cb_data:
            raise ArchiveError("The CAB file is truncated or corrupted.")
        if csum and _cab_csum(data, _cab_csum(hdr[4:8])) != csum:
            raise ArchiveError("The CAB file is corrupted (a data block failed its checksum).")
        if self.method == 0:
            out = data
        else:
            if data[:2] != b"CK":
                raise ArchiveError("The CAB file is corrupted (bad MSZIP block).")
            d = zlib.decompressobj(-15, zdict=self.prev) if self.prev else zlib.decompressobj(-15)
            out = d.decompress(data[2:]) + d.flush()
            self.prev = (self.prev + out)[-32768:]
        self.buf.extend(out)
        self.blocks -= 1

    def read(self, n):
        while len(self.buf) < n and self.blocks > 0:
            self._fill()
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk

    def skip(self, n):
        while n > 0:
            c = self.read(min(n, CHUNK))
            if not c:
                raise ArchiveError("The CAB file is truncated or corrupted.")
            n -= len(c)


def _read_asciiz(f):
    out = bytearray()
    while True:
        b = f.read(1)
        if not b or b == b"\x00":
            return bytes(out)
        out.extend(b)


def _cab_parse(path):
    """-> (folders, files, cb_data_res). folders: [(data offset, n blocks, method)];
    files: [(name, size, offset in folder, folder index, dos date, dos time)]."""
    with open(path, "rb") as f:
        hdr = f.read(36)
        if len(hdr) < 36 or hdr[:4] != b"MSCF":
            raise ArchiveError("This isn't a valid CAB file.")
        (_sig, _r1, _cb, _r2, coff_files, _r3, _vmin, _vmaj,
         n_folders, n_files, flags, _set_id, _i_cab) = struct.unpack("<4sIIIIIBBHHHHH", hdr)
        cb_folder_res = cb_data_res = 0
        if flags & 0x4:
            cb_hdr_res, cb_folder_res, cb_data_res = struct.unpack("<HBB", f.read(4))
            f.read(cb_hdr_res)
        if flags & 0x1:
            _read_asciiz(f)
            _read_asciiz(f)
        if flags & 0x2:
            _read_asciiz(f)
            _read_asciiz(f)

        folders = []
        for _ in range(n_folders):
            coff, n_data, ctype = struct.unpack("<IHH", f.read(8))
            if cb_folder_res:
                f.read(cb_folder_res)
            folders.append((coff, n_data, ctype & 0x0F))

        f.seek(coff_files)
        files = []
        for _ in range(n_files):
            cb_file, uoff, ifolder, fdate, ftime, attribs = struct.unpack("<IIHHHH", f.read(16))
            raw_name = _read_asciiz(f)
            name = raw_name.decode("utf-8" if attribs & 0x80 else "cp437", "replace")
            files.append((name, cb_file, uoff, ifolder, fdate, ftime))
    return folders, files, cb_data_res


def _dos_datetime(fdate, ftime):
    try:
        return time.mktime((1980 + (fdate >> 9), (fdate >> 5) & 15, fdate & 31,
                            ftime >> 11, (ftime >> 5) & 63, (ftime & 31) * 2, 0, 0, -1))
    except (OverflowError, ValueError):
        return None


def extract_cab(path, stage, ctx):
    folders, files, cb_data_res = _cab_parse(path)

    if any(x[3] >= 0xFFFD for x in files):
        raise Unsupported("Multi-part cabinets (split across several .cab files) aren't supported.")
    for _coff, _n, method in folders:
        if method not in (0, 1):
            raise Unsupported(
                "This CAB uses LZX or Quantum compression, which ShredPack can't read. "
                "Only stored and MSZIP cabinets are supported."
            )

    ctx.total = max(1, sum(x[1] for x in files))
    for idx, (coff, n_data, method) in enumerate(folders):
        group = sorted((x for x in files if x[3] == idx), key=lambda x: x[2])
        if not group:
            continue
        reader = CabReader(path, coff, n_data, method, cb_data_res)
        try:
            pos = 0
            for name, cb_file, uoff, _ifolder, fdate, ftime in group:
                if uoff < pos:
                    raise ArchiveError("The CAB file has overlapping entries and can't be read.")
                reader.skip(uoff - pos)
                pos = uoff
                parts = clean_parts(name)
                remaining = cb_file
                target = None
                dst = None
                if parts and ctx.wants(parts):
                    ctx.files += 1
                    if not ctx.dry_run:
                        target = os.path.join(stage, *parts)
                        os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
                        dst = open(lp(target), "wb")
                try:
                    while remaining > 0:
                        chunk = reader.read(min(CHUNK, remaining))
                        if not chunk:
                            raise ArchiveError("The CAB file is truncated or corrupted.")
                        remaining -= len(chunk)
                        if dst is not None:
                            dst.write(chunk)
                        ctx.add(len(chunk))
                finally:
                    if dst is not None:
                        dst.close()
                if target:
                    ts = _dos_datetime(fdate, ftime)
                    if ts:
                        _set_mtime(target, ts)
                pos += cb_file
        finally:
            reader.close()


# ---- XAR ------------------------------------------------------------------------
class XarStream(object):
    """File-like reader for one XAR heap entry (handles zlib/bzip2/lzma encodings)."""

    def __init__(self, f, start, length, style):
        self.f = f
        self.f.seek(start)
        self.left = length
        self.pending = b""
        style = (style or "").lower()
        if "gzip" in style or "zlib" in style or "deflate" in style:
            self.dec = zlib.decompressobj(zlib.MAX_WBITS | 32)
        elif "bzip2" in style:
            self.dec = bz2.BZ2Decompressor()
        elif "lzma" in style or "xz" in style:
            self.dec = lzma.LZMADecompressor()
        elif style in ("", "application/octet-stream"):
            self.dec = None
        else:
            raise Unsupported("This XAR uses an unsupported encoding (%s)." % style)

    def read(self, n):
        while True:
            if self.pending:
                out, self.pending = self.pending[:n], self.pending[n:]
                return out
            if self.left <= 0:
                return b""
            raw = self.f.read(min(CHUNK, self.left))
            if not raw:
                return b""
            self.left -= len(raw)
            self.pending = self.dec.decompress(raw) if self.dec else raw


def _xar_parse(f):
    """-> (entries, heap offset). entries: [(parts, 'dir'|'file', offset, length, style, size)]"""
    hdr = f.read(28)
    if len(hdr) < 28 or hdr[:4] != b"xar!":
        raise ArchiveError("This isn't a valid XAR file.")
    _magic, header_size, _ver, toc_c, _toc_u, _alg = struct.unpack(">4sHHQQI", hdr)
    f.seek(header_size)
    toc = zlib.decompress(f.read(toc_c))
    heap = header_size + toc_c
    toc_el = ET.fromstring(toc).find("toc")
    if toc_el is None:
        raise ArchiveError("The XAR table of contents is missing or corrupted.")

    entries = []

    def walk(element, prefix):
        for fe in element.findall("file"):
            parts = prefix + clean_parts(fe.findtext("name") or "")
            kind = fe.findtext("type") or "file"
            data = fe.find("data")
            if kind == "directory":
                entries.append((parts, "dir", 0, 0, "", 0))
            elif kind == "file":
                if data is not None:
                    enc = data.find("encoding")
                    entries.append((
                        parts, "file",
                        int(data.findtext("offset") or 0),
                        int(data.findtext("length") or 0),
                        enc.get("style", "") if enc is not None else "",
                        int(data.findtext("size") or 0),
                    ))
                else:
                    entries.append((parts, "file", 0, 0, "", 0))
            walk(fe, parts)

    walk(toc_el, [])
    return entries, heap


def extract_xar(path, stage, ctx):
    with open(path, "rb") as f:
        entries, heap = _xar_parse(f)
        ctx.total = max(1, sum(e[5] for e in entries if e[1] == "file"))
        for parts, kind, off, length, style, _size in entries:
            if not parts:
                continue
            if kind == "dir":
                if not ctx.dry_run and ctx.wants(parts, True):
                    make_dir(stage, parts)
            elif ctx.wants(parts):
                write_member(stage, parts, XarStream(f, heap + off, length, style), ctx)


# ---- format detection -----------------------------------------------------------
HANDLERS = {
    "zip": extract_zip,
    "tar": extract_tar,
    "single": extract_single,
    "7z": extract_7z,
    "iso": extract_iso,
    "cab": extract_cab,
    "xar": extract_xar,
}


def detect_format(path):
    with open(path, "rb") as f:
        head = f.read(16)
        f.seek(0x8001)
        iso_sig = f.read(5)
    low = path.lower()

    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:4] == b"xar!":
        return "xar"
    if head[:4] == b"MSCF":
        return "cab"
    if head[:4] == b"Rar!":
        raise Unsupported("RAR archives aren't supported by ShredPack's built-in engine.")
    if head[:5] in (b"MSWIM", b"WLPWM"):
        raise Unsupported("WIM images aren't supported by ShredPack's built-in engine.")
    if iso_sig in (b"CD001", b"BEA01"):
        return "iso"
    if head[:2] == b"\x1f\x8b" or head[:3] == b"BZh" or head[:6] == b"\xfd7zXZ\x00":
        return "tar" if tarfile.is_tarfile(path) else "single"
    if tarfile.is_tarfile(path):
        return "tar"
    if zipfile.is_zipfile(path):
        return "zip"
    for ext, label in ((".rar", "RAR"), (".wim", "WIM"), (".dmg", "DMG"), (".vhd", "VHD"), (".vhdx", "VHDX")):
        if low.endswith(ext):
            raise Unsupported("%s images/archives aren't supported by ShredPack's built-in engine." % label)
    if low.endswith(".iso"):
        return "iso"
    raise Unsupported("ShredPack doesn't recognise this file as a supported archive.")


OVERWRITE_POLICIES = (
    ("rename", "Keep both (rename the new file)"),
    ("overwrite", "Overwrite the existing file"),
    ("skip", "Skip - keep the existing file"),
    ("newer", "Overwrite only if the new file is newer"),
)


def merge_into(src, dst, rel="", policy="rename", stats=None):
    """Move everything from the staging folder into dst. `policy` decides what
    happens when a file already exists there (see OVERWRITE_POLICIES). Returns
    the list of final file paths (relative to the top-level dst) that were
    newly placed, for Mark-of-the-Web propagation. `stats` (a dict) collects
    'skipped' and 'replaced' counts."""
    if stats is None:
        stats = {}
    placed = []
    for entry in os.scandir(lp(src)):
        staged = os.path.join(src, entry.name)
        target = os.path.join(dst, entry.name)
        rel_name = os.path.join(rel, entry.name) if rel else entry.name
        if entry.is_dir(follow_symlinks=False):
            if os.path.isdir(lp(target)):
                placed.extend(merge_into(staged, target, rel_name, policy, stats))
            elif os.path.exists(lp(target)):
                renamed = unique_path(dst, entry.name)
                os.rename(lp(staged), lp(renamed))
                placed.extend(
                    os.path.join(rel, os.path.basename(renamed), r) if rel
                    else os.path.join(os.path.basename(renamed), r)
                    for r in _list_all_files(renamed)
                )
            else:
                os.rename(lp(staged), lp(target))
                placed.extend(os.path.join(rel_name, r) for r in _list_all_files(target))
            continue

        if os.path.exists(lp(target)) and not os.path.isdir(lp(target)):
            replace = policy == "overwrite"
            if policy == "newer":
                try:
                    replace = os.path.getmtime(lp(staged)) > os.path.getmtime(lp(target)) + 2
                except OSError:
                    replace = False
            if policy in ("skip", "newer") and not replace:
                os.remove(lp(staged))
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            if replace:
                os.replace(lp(staged), lp(target))
                stats["replaced"] = stats.get("replaced", 0) + 1
                placed.append(rel_name)
                continue
        if os.path.exists(lp(target)):
            target = unique_file_path(dst, entry.name)
            rel_name = os.path.join(rel, os.path.basename(target)) if rel else os.path.basename(target)
        os.rename(lp(staged), lp(target))
        placed.append(rel_name)
    try:
        os.rmdir(lp(src))
    except OSError:
        pass
    return placed


def _list_all_files(folder):
    out = []
    for r, _d, fs in os.walk(lp(folder)):
        for f in fs:
            out.append(os.path.relpath(os.path.join(r, f), lp(folder)))
    return out


def compute_dest(arc, mode, dest_dir, subfolder):
    base = os.path.dirname(arc)
    stem = archive_stem(arc)
    if mode in ("here", "folder"):
        # "Extract Here" always lands in a new folder named after the archive,
        # next to the archive itself - never dumps loose files into that folder.
        return unique_path(base, stem)
    return unique_path(dest_dir, stem) if subfolder else dest_dir


def extract_archive(path, dest, ctx, motw_source=None):
    """Extract `path` into `dest` (created if needed). Raises on any failure.
    If `motw_source` is given, its download "Mark of the Web" (if any) is
    copied onto every file that lands in `dest` from this extraction."""
    existed = os.path.isdir(lp(dest))
    os.makedirs(lp(dest), exist_ok=True)
    stage = None
    try:
        handler = HANDLERS[detect_format(path)]
        stage = tempfile.mkdtemp(prefix=".shredpack_", dir=dest)
        handler(path, stage, ctx)
        ctx.finish()
        stats = {}
        placed = merge_into(stage, dest, policy=ctx.overwrite, stats=stats)
        filtered = ctx.selected is not None or ctx.include or ctx.exclude
        if filtered and not placed and not stats:
            raise ArchiveError("Nothing in the archive matches the selection or filters.")
        if stats.get("skipped"):
            ctx.notes.append("%d existing file(s) were kept (skipped)." % stats["skipped"])
        if stats.get("replaced"):
            ctx.notes.append("%d existing file(s) were overwritten." % stats["replaced"])
        if motw_source:
            propagate_mark_of_the_web(motw_source, dest, placed)
    except BaseException:
        if stage:
            shutil.rmtree(lp(stage), ignore_errors=True)
        if not existed:
            try:
                os.rmdir(lp(dest))
            except OSError:
                pass
        raise
    else:
        shutil.rmtree(lp(stage), ignore_errors=True)


def test_archive(path, ctx):
    """Read every entry of the archive - decompressing and verifying CRCs - and
    write nothing to disk. Raises on the first problem; on success ctx.files
    holds the number of files verified."""
    ctx.dry_run = True
    handler = HANDLERS[detect_format(path)]
    scratch = tempfile.mkdtemp(prefix=".shredpack_test_")
    try:
        handler(path, scratch, ctx)
        ctx.finish()
    finally:
        shutil.rmtree(lp(scratch), ignore_errors=True)


FORMAT_LABELS = {
    "zip": "ZIP", "tar": "TAR", "single": "Compressed file", "7z": "7-Zip",
    "iso": "ISO image", "cab": "CAB (Windows cabinet)", "xar": "XAR",
}


def _to_timestamp(value):
    """Best-effort conversion of the assorted timestamp types libraries hand out."""
    try:
        if value is None:
            return None
        if hasattr(value, "totimestamp"):
            return float(value.totimestamp())
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _entry(parts, size, packed, mtime, is_dir, encrypted=False):
    return {"path": "/".join(parts), "size": size, "packed": packed,
            "mtime": mtime, "is_dir": is_dir, "encrypted": encrypted}


def list_archive(path, password=None):
    """Read an archive's table of contents without extracting anything.
    Returns {'format': key, 'entries': [...], 'encrypted': bool}; each entry has
    path, size, packed (None if unknown), mtime (None if unknown), is_dir,
    encrypted. Raises NeedPassword for archives whose listing is encrypted."""
    fmt = detect_format(path)
    entries, encrypted = [], False

    if fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                parts = clean_parts(info.filename)
                if not parts or is_junk(info.filename):
                    continue
                enc = bool(info.flag_bits & 0x1)
                encrypted = encrypted or enc
                try:
                    mtime = time.mktime(tuple(info.date_time) + (0, 0, -1))
                except (OverflowError, ValueError):
                    mtime = None
                entries.append(_entry(parts, info.file_size, info.compress_size, mtime,
                                      is_dir_entry(info), enc))

    elif fmt == "tar":
        with open(path, "rb") as raw:
            with tarfile.open(fileobj=raw, mode="r:*") as tf:
                for m in tf:
                    parts = clean_parts(m.name)
                    if not parts or not (m.isdir() or m.isreg() or m.issym() or m.islnk()):
                        continue
                    entries.append(_entry(parts, m.size if m.isreg() else 0, None,
                                          m.mtime, m.isdir()))

    elif fmt == "single":
        entries.append(_entry([archive_stem(path)], None, os.path.getsize(path),
                              os.path.getmtime(path), False))

    elif fmt == "7z":
        if py7zr is None:
            raise Unsupported("7Z support isn't included in this build (the py7zr package is missing).")
        try:
            with py7zr.SevenZipFile(path, mode="r", password=password or None) as z:
                encrypted = bool(z.needs_password())
                for f in z.list():
                    parts = clean_parts(f.filename)
                    if not parts:
                        continue
                    entries.append(_entry(
                        parts, None if f.is_directory else f.uncompressed,
                        None if f.is_directory else getattr(f, "compressed", None),
                        _to_timestamp(getattr(f, "lastwritetime", None)
                                      or getattr(f, "creationtime", None)),
                        bool(f.is_directory), encrypted))
        except (Cancelled, NeedPassword, ArchiveError):
            raise
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "PasswordRequired":
                raise NeedPassword(False)
            if password and "password" in (str(exc) + exc.__class__.__name__).lower():
                raise NeedPassword(True)
            raise

    elif fmt == "iso":
        if pycdlib is None:
            raise Unsupported("ISO support isn't included in this build (the pycdlib package is missing).")
        iso = pycdlib.PyCdlib()
        iso.open(path)
        try:
            dirs, files = _iso_collect(_iso_facade(iso))
        finally:
            iso.close()
        for d in dirs:
            parts = _iso_parts(d)
            if parts:
                entries.append(_entry(parts, 0, None, None, True))
        for full, length in files:
            parts = _iso_parts(full)
            if parts:
                entries.append(_entry(parts, length, None, None, False))

    elif fmt == "cab":
        _folders, files, _res = _cab_parse(path)
        for name, size, _off, _fi, fdate, ftime in files:
            parts = clean_parts(name)
            if parts:
                entries.append(_entry(parts, size, None, _dos_datetime(fdate, ftime), False))

    elif fmt == "xar":
        with open(path, "rb") as f:
            xar_entries, _heap = _xar_parse(f)
        for parts, kind, _off, length, _style, size in xar_entries:
            if parts:
                entries.append(_entry(parts, size if kind == "file" else 0,
                                      length if kind == "file" else None, None, kind == "dir"))

    entries.sort(key=lambda e: e["path"].lower())
    return {"format": fmt, "entries": entries, "encrypted": encrypted}


def summarize_listing(listing, path):
    files = [e for e in listing["entries"] if not e["is_dir"]]
    folders = [e for e in listing["entries"] if e["is_dir"]]
    sizes = [e["size"] for e in files if e["size"] is not None]
    total = sum(sizes) if sizes and len(sizes) == len(files) else None
    packed = os.path.getsize(path)
    return {
        "format": FORMAT_LABELS.get(listing["format"], listing["format"]),
        "files": len(files), "folders": len(folders),
        "total": total, "packed": packed,
        "ratio": (packed * 100.0 / total) if total else None,
        "encrypted": listing["encrypted"],
    }


def describe_summary(s):
    bits = [s["format"], "%d file%s" % (s["files"], "" if s["files"] == 1 else "s")]
    if s["folders"]:
        bits.append("%d folder%s" % (s["folders"], "" if s["folders"] == 1 else "s"))
    if s["total"] is not None:
        bits.append("%s unpacked" % fmt_size(s["total"]))
    bits.append("%s on disk" % fmt_size(s["packed"]))
    if s["ratio"] is not None:
        bits.append("%.0f%% of original" % s["ratio"])
    if s["encrypted"]:
        bits.append("encrypted")
    return "  \u2022  ".join(bits)


HASH_ALGOS = (("sha256", "SHA-256"), ("sha1", "SHA-1"), ("md5", "MD5"), ("crc32", "CRC32"))


def compute_hashes(path, ctx):
    """One pass over the file -> {'sha256': hex, 'sha1': hex, 'md5': hex, 'crc32': hex}."""
    sha256, sha1 = hashlib.sha256(), hashlib.sha1()
    try:
        md5 = hashlib.md5()
    except ValueError:  # FIPS-restricted Python builds
        md5 = None
    crc = 0
    ctx.total = max(1, os.path.getsize(path))
    with open(lp(path), "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            sha256.update(chunk)
            sha1.update(chunk)
            if md5 is not None:
                md5.update(chunk)
            crc = zlib.crc32(chunk, crc)
            ctx.add(len(chunk))
    ctx.finish()
    return {"sha256": sha256.hexdigest(), "sha1": sha1.hexdigest(),
            "md5": md5.hexdigest() if md5 is not None else "unavailable",
            "crc32": "%08x" % (crc & 0xFFFFFFFF)}


def describe_error(exc):
    if isinstance(exc, ArchiveError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "Permission denied. Choose a folder you can write to."
    if isinstance(exc, (zipfile.BadZipFile, tarfile.TarError, EOFError, zlib.error,
                        lzma.LZMAError, struct.error, ET.ParseError)):
        return "The archive looks corrupted, incomplete or isn't a valid archive."
    if isinstance(exc, OSError):
        if exc.__class__.__name__ == "BadGzipFile":
            return "The archive looks corrupted or incomplete."
        return exc.strerror or str(exc)
    return str(exc) or exc.__class__.__name__


# --------------------------------------------------------------------------
# Compression engine (pure Python - creates archives, not just reads them)
# --------------------------------------------------------------------------
COMPRESS_FORMATS = (
    # (key, display label, file extension, tar mode or None, supports password)
    ("zip", "ZIP", ".zip", None, True),
    ("7z", "7Z", ".7z", None, True),
    ("tar", "TAR (uncompressed)", ".tar", "w", False),
    ("targz", "TAR.GZ", ".tar.gz", "w:gz", False),
    ("tarbz2", "TAR.BZ2", ".tar.bz2", "w:bz2", False),
    ("tarxz", "TAR.XZ", ".tar.xz", "w:xz", False),
)


def compress_format_available(key):
    if key == "7z":
        return py7zr is not None
    return True


def _is_link_or_reparse(path):
    """True for symlinks and Windows junctions/reparse points, which are never
    followed or archived (they could pull in files from outside the folder
    the user picked)."""
    try:
        if os.path.islink(path):
            return True
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return True


def compress_members(sources, skipped=None):
    """Yield (arcname, abs_path, is_dir) for the given top-level sources,
    recursing into folders. A folder source becomes the archive's own root
    folder (name/...); a lone file keeps just its name; several sources are
    each added at the top level under their own name. Links and junctions are
    skipped (their paths are appended to `skipped` if given)."""
    def skip(p):
        if skipped is not None:
            skipped.append(p)

    for src in sources:
        if _is_link_or_reparse(src):
            skip(src)
            continue
        name = sanitize_name(os.path.basename(os.path.normpath(src))) or "item"
        if os.path.isdir(src):
            yield (name, src, True)
            for root, dirs, files in os.walk(src):
                keep = []
                for d in dirs:
                    if _is_link_or_reparse(os.path.join(root, d)):
                        skip(os.path.join(root, d))
                    else:
                        keep.append(d)
                dirs[:] = keep
                rel_root = os.path.relpath(root, src)
                arc_root = name if rel_root == "." else "/".join(
                    [name] + clean_parts(rel_root))
                for d in keep:
                    yield (arc_root + "/" + sanitize_name(d), os.path.join(root, d), True)
                for f in files:
                    full = os.path.join(root, f)
                    if _is_link_or_reparse(full):
                        skip(full)
                        continue
                    yield (arc_root + "/" + sanitize_name(f), full, False)
        else:
            yield (name, src, False)


def scan_sources(sources, max_files=None, max_bytes=None):
    """Count what a compression would include, stopping early once either cap
    is reached (so a stray 'compress my whole drive' can't freeze the window).
    -> (files, bytes, skipped_links, capped)"""
    files = total = 0
    skipped = []
    for _arc, path, is_dir in compress_members(sources, skipped):
        if is_dir:
            continue
        files += 1
        try:
            total += os.path.getsize(lp(path))
        except OSError:
            pass
        if (max_files and files >= max_files) or (max_bytes and total >= max_bytes):
            return files, total, skipped, True
    return files, total, skipped, False


def default_archive_name(sources, ext):
    if len(sources) == 1:
        base = os.path.basename(os.path.normpath(sources[0]))
        stem = os.path.splitext(base)[0] if os.path.isfile(sources[0]) else base
    else:
        parent = os.path.dirname(os.path.normpath(sources[0]))
        stem = os.path.basename(parent) or "Archive"
    return (sanitize_name(stem) or "Archive") + ext


COMPRESS_LEVELS = (
    ("store", "Store (no compression)"),
    ("fast", "Fast"),
    ("normal", "Normal"),
    ("max", "Maximum"),
)
_DEFLATE_LEVEL = {"fast": 1, "normal": 6, "max": 9}


def _gather_members(sources, ctx):
    skipped = []
    members = list(compress_members(sources, skipped))
    if skipped:
        ctx.notes.append("%d link(s)/junction(s) were skipped, not followed." % len(skipped))
    return members


def compress_zip(sources, dest, ctx, password=None, level="normal"):
    members = _gather_members(sources, ctx)
    ctx.total = max(1, sum(os.path.getsize(p) for _, p, d in members if not d))

    if password:
        if pyzipper is None:
            raise Unsupported(
                "Password-protected ZIPs need the pyzipper package, which isn't included in this build."
            )
        zf = pyzipper.AESZipFile(
            dest, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        )
        zf.setpassword(password.encode("utf-8"))
    else:
        zf = zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED)

    try:
        added_dirs = set()
        for arcname, path, is_dir in members:
            ctx._check()
            if is_dir:
                if arcname not in added_dirs:
                    zf.writestr(zipfile.ZipInfo(arcname + "/"), b"")
                    added_dirs.add(arcname)
                continue
            try:
                dt = time.localtime(os.path.getmtime(path))[:6]
            except OSError:
                dt = time.localtime()[:6]
            info = zipfile.ZipInfo(arcname, date_time=dt)
            if level == "store":
                info.compress_type = zipfile.ZIP_STORED
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
                info._compresslevel = _DEFLATE_LEVEL.get(level, 6)
            with open(lp(path), "rb") as src, zf.open(info, "w") as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
                    ctx.add(len(chunk))
    finally:
        zf.close()
    ctx.finish()


class _ProgressReader(object):
    """Wraps a file object so tarfile.addfile()'s internal reads count as progress."""

    def __init__(self, fh, ctx):
        self.fh, self.ctx = fh, ctx

    def read(self, n=-1):
        data = self.fh.read(n)
        self.ctx.add(len(data))
        return data


def compress_tar(sources, dest, mode, ctx, level="normal"):
    members = _gather_members(sources, ctx)
    ctx.total = max(1, sum(os.path.getsize(p) for _, p, d in members if not d))
    kwargs = {}
    if mode in ("w:gz", "w:bz2"):
        kwargs["compresslevel"] = {"store": 1, "fast": 1, "normal": 6 if mode == "w:gz" else 9,
                                   "max": 9}.get(level, 6)
    elif mode == "w:xz":
        kwargs["preset"] = {"store": 0, "fast": 1, "normal": 6, "max": 9}.get(level, 6)
    with tarfile.open(dest, mode, **kwargs) as tf:
        added_dirs = set()
        for arcname, path, is_dir in members:
            ctx._check()
            if is_dir:
                if arcname not in added_dirs:
                    info = tarfile.TarInfo(arcname)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = time.time()
                    tf.addfile(info)
                    added_dirs.add(arcname)
                continue
            info = tf.gettarinfo(lp(path), arcname=arcname)
            with open(lp(path), "rb") as f:
                tf.addfile(info, _ProgressReader(f, ctx))
    ctx.finish()


def compress_7z(sources, dest, ctx, password=None, level="normal"):
    if py7zr is None:
        raise Unsupported(
            "7Z creation needs the py7zr package, which isn't included in this build."
        )
    members = _gather_members(sources, ctx)
    ctx.total = max(1, sum(os.path.getsize(p) for _, p, d in members if not d))
    kwargs = {}
    if level == "store":
        kwargs["filters"] = [{"id": py7zr.FILTER_COPY}]
    elif level in ("fast", "max"):
        kwargs["filters"] = [{"id": py7zr.FILTER_LZMA2, "preset": 1 if level == "fast" else 9}]
    with py7zr.SevenZipFile(dest, "w", password=password, **kwargs) as z:
        for arcname, path, is_dir in members:
            ctx._check()
            z.write(path, arcname)
            if not is_dir:
                ctx.add(os.path.getsize(path))
    ctx.finish()


def compress_archive(sources, dest, fmt_key, ctx, password=None, level="normal"):
    """Create `dest` from `sources` (files/folders). Writes to a temp sibling
    file first so a failed or cancelled run never leaves a broken archive
    where the real one should be."""
    fmt = next(f for f in COMPRESS_FORMATS if f[0] == fmt_key)
    tmp = dest + ".part"
    try:
        if fmt_key == "zip":
            compress_zip(sources, tmp, ctx, password, level)
        elif fmt_key == "7z":
            compress_7z(sources, tmp, ctx, password, level)
        else:
            compress_tar(sources, tmp, fmt[3], ctx, level)
        os.replace(lp(tmp), lp(dest))
    except BaseException:
        try:
            os.remove(lp(tmp))
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Windows right-click integration (per-user registry, no admin rights needed)
# --------------------------------------------------------------------------
def app_icon_path():
    """Path to the bundled shredpack.ico, whether running frozen or as a script."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "shredpack.ico")
    return path if os.path.isfile(path) else None


def launcher_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def menu_command(mode):
    return '%s --mode %s "%%1"' % (launcher_command(), mode)


def ext_key(ext):
    return r"Software\Classes\SystemFileAssociations\%s\shell\%s" % (ext, MENU_KEY)


def compress_class_key(cls):
    return r"Software\Classes\%s\shell\%s" % (cls, COMPRESS_MENU_KEY)


def _delete_tree(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(path + "\\" + sub)
    except OSError:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def _notify_shell():
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:  # noqa: BLE001
        pass


def uninstall_menu():
    if sys.platform != "win32":
        return
    for ext in EXTENSIONS:
        _delete_tree(ext_key(ext))
    for cls in COMPRESS_CLASSES:
        _delete_tree(compress_class_key(cls))
    _notify_shell()


def _install_cascade(base, items, icon):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
        if icon:
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
    for name, label, mode in items:
        sub = base + "\\shell\\" + name
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, label)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub + r"\command", 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, menu_command(mode))


def install_menu():
    """Create cascading 'ShredPack' submenus: extraction on each archive type,
    and 'Add to...' compression on any ordinary file or folder."""
    if sys.platform != "win32":
        return
    uninstall_menu()  # also removes any old layout
    icon = '"%s",0' % sys.executable if getattr(sys, "frozen", False) else None
    for ext in EXTENSIONS:
        _install_cascade(ext_key(ext), MENU_ITEMS, icon)
    for cls in COMPRESS_CLASSES:
        _install_cascade(compress_class_key(cls), COMPRESS_MENU_ITEMS, icon)
    _notify_shell()


def install_state():
    """'none', 'stale' (old layout or another location) or 'current'."""
    if sys.platform != "win32":
        return "none"
    key = ext_key(EXTENSIONS[0]) + "\\shell\\" + MENU_ITEMS[-1][0] + r"\command"
    compress_key = compress_class_key("*") + "\\shell\\" + COMPRESS_MENU_ITEMS[-1][0] + r"\command"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value = winreg.QueryValueEx(k, "")[0]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, compress_key) as k2:
            value2 = winreg.QueryValueEx(k2, "")[0]
        current = (
            str(value).lower() == menu_command(MENU_ITEMS[-1][2]).lower()
            and str(value2).lower() == menu_command(COMPRESS_MENU_ITEMS[-1][2]).lower()
        )
        return "current" if current else "stale"
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_key(EXTENSIONS[0])):
            return "stale"
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, compress_class_key("*")):
            return "stale"
    except OSError:
        return "none"


# --------------------------------------------------------------------------
# The application window
# --------------------------------------------------------------------------
class ShredPackApp(object):
    def __init__(self, root, archives=None, mode="dialog", from_shell=False):
        self.root = root
        self.from_shell = from_shell
        self.archives = []
        self.mode = mode
        self.k = max(1.0, root.winfo_fpixels("1i") / 96.0)

        self.state = "home"  # home | choice | compress_choice | processing | finished
        self.action = "extract"  # "extract" or "compress" - which flow "processing" belongs to
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.pw_event = threading.Event()
        self.pw_value = None
        self.cur_idx, self.cur_n = 1, 1

        self.compress_sources = []
        self._name_dirty = False
        self._suppress_name_trace = False
        self.selection = None          # set of lowercase archive paths chosen in the browser
        self.initial_password = None   # password already entered while browsing
        self.listing = None
        self.auto_test = False         # right-click "Test archive": run the test straight away
        self.browse_password = None
        self._poll_active = False
        self._filter_job = None
        self._browse_rows = []

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        icon_path = app_icon_path()
        if icon_path and sys.platform == "win32":
            try:
                root.iconbitmap(default=icon_path)
            except tk.TclError:
                pass
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = self.px(600)
        h = min(self.px(680), sh - self.px(90))
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 3)))

        self._build_styles()
        self._build_ui()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Return>", lambda e: self._on_return())
        root.bind("<Escape>", lambda e: self.on_escape())

        if DND_FILES is not None and hasattr(root, "drop_target_register"):
            self._register_dnd(root)

        if archives:
            self.open_archives(archives, mode)
        else:
            self.go_home()

        enable_dark_titlebar(root)
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
        root.lift()
        root.focus_force()

        if not from_shell:
            check_for_update(self._on_update_result)

    def _on_update_result(self, latest):
        if latest:
            self.root.after(0, lambda: self.update_link.config(text="Update available: v" + latest))

    def px(self, v):
        return int(round(v * self.k))

    # ---- construction -------------------------------------------------------
    def _build_styles(self):
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        pad = (self.px(20), self.px(10))
        s.configure("Primary.TButton", background=ACCENT, foreground="#08222e", borderwidth=0,
                    focusthickness=0, focuscolor=ACCENT, padding=pad, font=(FONT_SEMI, 10))
        s.map("Primary.TButton", background=[("pressed", "#4bb8e8"), ("active", "#7fd7ff")])
        s.configure("Secondary.TButton", background="#3a3a3a", foreground=TEXT, borderwidth=0,
                    focusthickness=0, focuscolor="#3a3a3a", padding=pad, font=(FONT, 10))
        s.map("Secondary.TButton", background=[("pressed", "#505050"), ("active", "#454545")])
        s.configure("Zen.TCheckbutton", background=CARD, foreground=TEXT, font=(FONT, 10),
                    focuscolor=CARD, indicatorbackground="#3a3a3a", indicatorforeground="#08222e",
                    upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=(0, self.px(4)))
        s.map("Zen.TCheckbutton", background=[("active", CARD)], foreground=[("active", TEXT)],
              indicatorbackground=[("selected", ACCENT), ("active", "#454545")])
        s.configure("Zen.Horizontal.TProgressbar", troughcolor=TROUGH, background=ACCENT,
                    bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, borderwidth=0,
                    thickness=self.px(6))
        s.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                    borderwidth=0, rowheight=self.px(24), font=(FONT, 10))
        s.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#08222e")])
        s.configure("Treeview.Heading", background="#3a3a3a", foreground=TEXT, borderwidth=0,
                    relief="flat", font=(FONT_SEMI, 9), padding=(self.px(6), self.px(5)))
        s.map("Treeview.Heading", background=[("active", "#454545")])
        s.configure("Vertical.TScrollbar", background="#3a3a3a", troughcolor=CARD,
                    bordercolor=CARD, arrowcolor=TEXT, relief="flat", borderwidth=0)
        s.configure("TCombobox", fieldbackground=FIELD, background="#3a3a3a", foreground=TEXT,
                    arrowcolor=TEXT, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#08222e")

    def _entry(self, parent, var, **kw):
        e = tk.Entry(parent, textvariable=var, font=(FONT, 10), bg=FIELD, fg=TEXT,
                     insertbackground=TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT, **kw)
        return e

    def _card(self, parent):
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=self.px(26), pady=self.px(22))
        return card, inner

    def _label(self, parent, text="", size=10, semi=False, color=MUTED, **kw):
        return tk.Label(parent, text=text, font=(FONT_SEMI if semi else FONT, size),
                        fg=color, bg=CARD, **kw)

    def _build_ui(self):
        px = self.px
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=px(28), pady=(px(22), px(12)))
        logo = tk.Canvas(header, width=px(30), height=px(30), bg=BG, highlightthickness=0)
        logo.pack(side="left")
        rounded_rect(logo, px(2), px(2), px(28), px(28), px(7), fill=ACCENT, outline="")
        logo.create_text(px(15), px(15), text="S", font=(FONT, 12, "bold"), fill="#0b2530")
        tk.Label(header, text=APP_NAME, font=(FONT_SEMI, 15), fg=TEXT, bg=BG).pack(
            side="left", padx=(px(10), 0))
        self.update_link = tk.Label(
            header, text="", font=(FONT, 9, "underline"), fg=ACCENT, bg=BG, cursor="hand2")
        self.update_link.pack(side="right")
        self.update_link.bind("<Button-1>", lambda e: webbrowser.open(RELEASES_URL))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=px(28), pady=(0, px(24)))

        self._build_home(body)
        self._build_choice(body)
        self._build_compress(body)
        self._build_browse(body)
        self._build_progress(body)
        self._build_done(body)

    def _build_home(self, body):
        px = self.px
        self.view_home, inner = self._card(body)

        icon = tk.Canvas(inner, width=px(84), height=px(84), bg=CARD, highlightthickness=0)
        icon.pack(pady=(px(6), 0))
        c, r, lw = px(42), px(36), px(3)
        icon.create_oval(c - r, c - r, c + r, c + r, fill=ICON_BG, outline=ACCENT, width=px(2))
        icon.create_line(c, c - px(16), c, c + px(8), fill=ACCENT, width=lw, capstyle="round")
        icon.create_line(c - px(10), c - px(2), c, c + px(8), c + px(10), c - px(2),
                         fill=ACCENT, width=lw, capstyle="round", joinstyle="round")
        icon.create_line(c - px(14), c + px(18), c + px(14), c + px(18),
                         fill=ACCENT, width=lw, capstyle="round")

        self._label(inner, "Extract or create an archive", 16, True, TEXT).pack(pady=(px(12), 0))
        hint = "Choose a file, or drag and drop it onto this window" if DND_FILES else "Choose an archive, or build a new one"
        self._label(inner, hint, 10).pack(pady=(px(4), 0))
        btn_row = tk.Frame(inner, bg=CARD)
        btn_row.pack(pady=(px(16), 0))
        ttk.Button(btn_row, text="Choose Archive...", style="Primary.TButton",
                   command=self.choose_archives).pack(side="left")
        ttk.Button(btn_row, text="Create Archive...", style="Secondary.TButton",
                   command=self.choose_compress_entry).pack(side="left", padx=(px(10), 0))
        ttk.Button(btn_row, text="Checksum...", style="Secondary.TButton",
                   command=self.choose_checksum).pack(side="left", padx=(px(10), 0))
        self._label(inner, FORMATS_LABEL, 8, color="#7c7c7c").pack(pady=(px(12), 0))

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(px(16), px(12)))
        self.menu_row = tk.Frame(inner, bg=CARD)
        self.menu_row.pack(fill="x")
        self.lbl_menu = self._label(self.menu_row, "", 10, color=TEXT, anchor="w")
        self.lbl_menu.pack(side="left")
        self.btn_menu = ttk.Button(self.menu_row, text="", style="Secondary.TButton",
                                   command=self.toggle_menu)
        self.btn_menu.pack(side="right")

    def _build_choice(self, body):
        px = self.px
        self.view_choice, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Cancel", style="Secondary.TButton",
                   command=self.cancel_choice).pack(side="right")
        ttk.Button(bottom, text="Extract", style="Primary.TButton",
                   command=self.begin).pack(side="right", padx=(0, px(10)))
        self.btn_test = ttk.Button(bottom, text="Test", style="Secondary.TButton",
                                   command=self.test_selected)
        self.btn_test.pack(side="left")
        self.btn_browse = ttk.Button(bottom, text="Browse...", style="Secondary.TButton",
                                     command=self.open_browser)
        self.btn_browse.pack(side="left", padx=(px(8), 0))

        self.lbl_name = self._label(inner, "", 14, True, TEXT, anchor="w", justify="left",
                                    wraplength=px(480))
        self.lbl_name.pack(fill="x")
        self.lbl_meta = self._label(inner, "", 10, anchor="w", justify="left", wraplength=px(480))
        self.lbl_meta.pack(fill="x", pady=(px(2), 0))
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=px(14))

        self.lbl_section = self._label(inner, "", 10, anchor="w")
        self.lbl_section.pack(fill="x")

        # Mode-dependent area (rebuilt in refresh_choice, always sits here).
        self.dest_area = tk.Frame(inner, bg=CARD)
        self.dest_area.pack(fill="x", pady=(px(8), 0))

        self.dest_var = tk.StringVar()
        self.dest_row = tk.Frame(self.dest_area, bg=CARD)
        self.dest_entry = tk.Entry(
            self.dest_row, textvariable=self.dest_var, font=(FONT, 10), bg=FIELD, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.dest_entry.pack(side="left", fill="x", expand=True, ipady=px(7))
        ttk.Button(self.dest_row, text="Browse...", style="Secondary.TButton",
                   command=self.pick_folder).pack(side="left", padx=(px(10), 0))

        self.sub_var = tk.BooleanVar(value=False)
        self.sub_chk = ttk.Checkbutton(self.dest_area, text="Create a folder named after the archive",
                                       variable=self.sub_var, style="Zen.TCheckbutton")
        self.lbl_hint = self._label(self.dest_area, "", 10, anchor="w", justify="left",
                                    wraplength=px(480))

        self.lbl_sel = self._label(inner, "", 10, color=ACCENT, anchor="w", justify="left",
                                   wraplength=px(480))

        self.delete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(inner, text="Delete original archive after successful extraction",
                        variable=self.delete_var, style="Zen.TCheckbutton"
                        ).pack(anchor="w", pady=(px(14), 0))

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(px(14), px(8)))
        ow_row = tk.Frame(inner, bg=CARD)
        ow_row.pack(fill="x")
        self._label(ow_row, "If a file already exists", 10, anchor="w").pack(side="left")
        self.overwrite_var = tk.StringVar(value=OVERWRITE_POLICIES[0][1])
        ttk.Combobox(ow_row, textvariable=self.overwrite_var, state="readonly",
                     values=[label for _k, label in OVERWRITE_POLICIES], width=34
                     ).pack(side="right")
        flt = tk.Frame(inner, bg=CARD)
        flt.pack(fill="x", pady=(px(8), 0))
        self._label(flt, "Only extract", 10, anchor="w").grid(row=0, column=0, sticky="w")
        self._label(flt, "Skip", 10, anchor="w").grid(row=0, column=1, sticky="w", padx=(px(10), 0))
        self.include_var = tk.StringVar()
        self.exclude_var = tk.StringVar()
        self._entry(flt, self.include_var).grid(row=1, column=0, sticky="ew", ipady=px(5))
        self._entry(flt, self.exclude_var).grid(row=1, column=1, sticky="ew", ipady=px(5),
                                                padx=(px(10), 0))
        flt.columnconfigure(0, weight=1)
        flt.columnconfigure(1, weight=1)
        self._label(inner, "Wildcards, e.g. *.jpg, *.pdf  (leave empty to extract everything)", 8,
                    color="#7c7c7c", anchor="w").pack(fill="x", pady=(px(3), 0))

    def _build_compress(self, body):
        px = self.px
        self.view_compress, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Cancel", style="Secondary.TButton",
                   command=self.cancel_compress).pack(side="right")
        ttk.Button(bottom, text="Compress", style="Primary.TButton",
                   command=self.begin_compress).pack(side="right", padx=(0, px(10)))

        self.lbl_cx_name = self._label(inner, "", 14, True, TEXT, anchor="w", justify="left",
                                       wraplength=px(480))
        self.lbl_cx_name.pack(fill="x")
        self.lbl_cx_meta = self._label(inner, "", 10, anchor="w", justify="left", wraplength=px(480))
        self.lbl_cx_meta.pack(fill="x", pady=(px(2), 0))
        add_row = tk.Frame(inner, bg=CARD)
        add_row.pack(fill="x", pady=(px(8), 0))
        ttk.Button(add_row, text="Add Files...", style="Secondary.TButton",
                   command=self.add_compress_files).pack(side="left")
        ttk.Button(add_row, text="Add Folder...", style="Secondary.TButton",
                   command=self.add_compress_folder).pack(side="left", padx=(px(8), 0))

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=px(14))

        fmt_row = tk.Frame(inner, bg=CARD)
        fmt_row.pack(fill="x")
        self._label(fmt_row, "Format", 10, anchor="w").pack(side="left")
        self.compress_fmt_var = tk.StringVar(value="ZIP")
        self.fmt_combo = ttk.Combobox(
            fmt_row, textvariable=self.compress_fmt_var, state="readonly",
            values=[f[1] for f in COMPRESS_FORMATS if compress_format_available(f[0])], width=16)
        self.fmt_combo.pack(side="right")
        self.fmt_combo.bind("<<ComboboxSelected>>", lambda e: self._on_format_change())

        lvl_row = tk.Frame(inner, bg=CARD)
        lvl_row.pack(fill="x", pady=(px(8), 0))
        self._label(lvl_row, "Compression level", 10, anchor="w").pack(side="left")
        self.compress_level_var = tk.StringVar(value=COMPRESS_LEVELS[2][1])
        self.level_combo = ttk.Combobox(
            lvl_row, textvariable=self.compress_level_var, state="readonly",
            values=[label for _k, label in COMPRESS_LEVELS], width=22)
        self.level_combo.pack(side="right")

        self._label(inner, "Archive name", 10, anchor="w").pack(fill="x", pady=(px(12), 0))
        name_row = tk.Frame(inner, bg=CARD)
        name_row.pack(fill="x", pady=(px(4), 0))
        self.compress_name_var = tk.StringVar()
        self.compress_name_var.trace_add("write", lambda *a: self._on_name_edit())
        self.name_entry = tk.Entry(
            name_row, textvariable=self.compress_name_var, font=(FONT, 10), bg=FIELD, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.name_entry.pack(side="left", fill="x", expand=True, ipady=px(7))

        self._label(inner, "Destination folder", 10, anchor="w").pack(fill="x", pady=(px(12), 0))
        dest_row = tk.Frame(inner, bg=CARD)
        dest_row.pack(fill="x", pady=(px(4), 0))
        self.compress_dest_var = tk.StringVar()
        self.cdest_entry = tk.Entry(
            dest_row, textvariable=self.compress_dest_var, font=(FONT, 10), bg=FIELD, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.cdest_entry.pack(side="left", fill="x", expand=True, ipady=px(7))
        ttk.Button(dest_row, text="Browse...", style="Secondary.TButton",
                   command=self.pick_compress_folder).pack(side="left", padx=(px(10), 0))

        self.encrypt_var = tk.BooleanVar(value=False)
        self.encrypt_chk = ttk.Checkbutton(
            inner, text="Encrypt with a password (ZIP/7Z only)", variable=self.encrypt_var,
            style="Zen.TCheckbutton", command=self._toggle_password_field)
        self.encrypt_chk.pack(anchor="w", pady=(px(14), 0))

        self.compress_password_var = tk.StringVar()
        self.pw_entry = tk.Entry(
            inner, textvariable=self.compress_password_var, show="*", font=(FONT, 10),
            bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled")
        self.pw_entry.pack(fill="x", pady=(px(6), 0), ipady=px(7))

    def _build_browse(self, body):
        px = self.px
        self.view_browse, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x", pady=(px(10), 0))
        ttk.Button(bottom, text="Back", style="Secondary.TButton",
                   command=self.browse_back).pack(side="left")
        ttk.Button(bottom, text="Extract All...", style="Secondary.TButton",
                   command=lambda: self.browse_extract(False)).pack(side="right")
        ttk.Button(bottom, text="Extract Selected...", style="Primary.TButton",
                   command=lambda: self.browse_extract(True)).pack(side="right", padx=(0, px(8)))

        self.lbl_b_name = self._label(inner, "", 13, True, TEXT, anchor="w", justify="left",
                                      wraplength=px(500))
        self.lbl_b_name.pack(fill="x")
        self.lbl_b_info = self._label(inner, "", 9, anchor="w", justify="left", wraplength=px(500))
        self.lbl_b_info.pack(fill="x", pady=(px(2), px(8)))

        search_row = tk.Frame(inner, bg=CARD)
        search_row.pack(fill="x")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._schedule_browse_filter())
        self._entry(search_row, self.search_var).pack(side="left", fill="x", expand=True,
                                                      ipady=px(5))
        self.lbl_b_count = self._label(search_row, "", 9, anchor="e")
        self.lbl_b_count.pack(side="right", padx=(px(8), 0))

        tree_frame = tk.Frame(inner, bg=CARD)
        tree_frame.pack(fill="both", expand=True, pady=(px(8), 0))
        self.tree = ttk.Treeview(tree_frame, columns=("size", "packed", "modified"),
                                 selectmode="extended")
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("packed", text="Packed", anchor="e")
        self.tree.heading("modified", text="Modified", anchor="w")
        self.tree.column("#0", width=px(240), stretch=True)
        self.tree.column("size", width=px(75), anchor="e", stretch=False)
        self.tree.column("packed", width=px(75), anchor="e", stretch=False)
        self.tree.column("modified", width=px(120), stretch=False)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview,
                               style="Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

    def _build_progress(self, body):
        px = self.px
        self.view_progress, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Cancel", style="Secondary.TButton",
                   command=self.on_cancel).pack(side="right")
        self.lbl_p_title = self._label(inner, "Preparing\u2026", 14, True, TEXT, anchor="w",
                                       justify="left", wraplength=px(480))
        self.lbl_p_title.pack(fill="x", pady=(px(30), 0))
        self.progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(inner, style="Zen.Horizontal.TProgressbar", orient="horizontal",
                        mode="determinate", maximum=100, variable=self.progress_var
                        ).pack(fill="x", pady=(px(22), 0))
        self.lbl_p_sub = self._label(inner, "", 10, anchor="w")
        self.lbl_p_sub.pack(fill="x", pady=(px(10), 0))

    def _build_done(self, body):
        px = self.px
        self.view_done = tk.Frame(body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        box = tk.Frame(self.view_done, bg=CARD)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(box, text="\u2713", font=(FONT_SEMI, 46), fg=GREEN, bg=CARD).pack()
        self.lbl_d_title = self._label(box, "Extraction complete", 16, True, TEXT)
        self.lbl_d_title.pack(pady=(px(4), 0))
        self.lbl_d_sub = self._label(box, "", 10, justify="center", wraplength=px(440))
        self.lbl_d_sub.pack(pady=(px(6), 0))

    def _register_dnd(self, widget):
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:  # noqa: BLE001
            pass
        for child in widget.winfo_children():
            self._register_dnd(child)

    def show(self, view):
        for v in (self.view_home, self.view_choice, self.view_compress, self.view_browse,
                  self.view_progress, self.view_done):
            v.pack_forget()
        view.pack(fill="both", expand=True)

    # ---- home ---------------------------------------------------------------
    def go_home(self):
        self.state = "home"
        self.action = "extract"
        self.archives = []
        self.selection = None
        self.initial_password = None
        if sys.platform == "win32":
            self.refresh_menu_status()
        else:
            self.menu_row.pack_forget()
        self.show(self.view_home)

    def refresh_menu_status(self):
        st = install_state()
        if st == "current":
            self.lbl_menu.config(text="Right-click menu: installed \u2713")
            self.btn_menu.config(text="Remove")
        elif st == "stale":
            self.lbl_menu.config(text="Right-click menu: needs updating")
            self.btn_menu.config(text="Update")
        else:
            self.lbl_menu.config(text="Right-click menu: not installed")
            self.btn_menu.config(text="Add to menu")

    def toggle_menu(self):
        st = install_state()
        try:
            if st == "current":
                uninstall_menu()
            else:
                install_menu()
                messagebox.showinfo(
                    APP_NAME,
                    'Done! Right-click an archive and choose "%s" to extract it, '
                    'or right-click any file or folder and choose "%s" to compress it.\n\n'
                    'On Windows 11 both appear under "Show more options".' % (APP_NAME, APP_NAME),
                    parent=self.root)
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Couldn't update the registry:\n\n%s" % exc,
                                 parent=self.root)
        self.refresh_menu_status()

    def choose_archives(self):
        pats = " ".join("*" + e for e in EXTENSIONS)
        files = filedialog.askopenfilenames(
            parent=self.root, title="Choose archive(s) to extract",
            filetypes=[("Archives", pats), ("All files", "*.*")])
        if files:
            self.open_archives(list(files), "dialog")

    def choose_compress_entry(self):
        self.open_compress([], "dialog")

    def _looks_like_archive(self, path):
        try:
            detect_format(path)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _on_drop(self, event):
        if self.state not in ("home", "compress_choice"):
            return getattr(event, "action", "copy")
        try:
            paths = [p for p in self.root.tk.splitlist(event.data) if os.path.exists(p)]
        except tk.TclError:
            paths = []
        if not paths:
            return getattr(event, "action", "copy")
        if self.state == "compress_choice":
            for p in paths:
                p = os.path.abspath(p)
                if p not in self.compress_sources:
                    self.compress_sources.append(p)
            self.refresh_compress()
            return getattr(event, "action", "copy")
        files_only = [p for p in paths if os.path.isfile(p)]
        all_archives = (
            files_only and len(files_only) == len(paths)
            and all(self._looks_like_archive(p) for p in files_only)
        )
        if all_archives:
            self.open_archives(files_only, "dialog")
        else:
            self.open_compress(paths, "dialog")
        return getattr(event, "action", "copy")

    # ---- choice view --------------------------------------------------------
    def open_archives(self, files, mode):
        self.action = "extract"
        abs_files = [os.path.abspath(f) for f in files]
        # Collapse a multi-part set down to one representative entry so a user
        # who selects (or drags) several pieces of the same split archive only
        # gets one extraction, not one per part.
        seen, resolved = set(), []
        for f in abs_files:
            group = detect_split_parts(f)
            key = tuple(sorted(p.lower() for p in group)) if group else (f.lower(),)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(f)
        self.archives = resolved
        self.mode = mode
        self.selection = None
        self.initial_password = None
        self.dest_var.set(os.path.dirname(self.archives[0]))
        self.sub_var.set(False)
        self.delete_var.set(False)
        self.overwrite_var.set(OVERWRITE_POLICIES[0][1])
        self.include_var.set("")
        self.exclude_var.set("")
        self.state = "choice"
        self.refresh_choice()
        self.show(self.view_choice)

    def refresh_choice(self):
        px = self.px
        first = self.archives[0]
        folder = os.path.dirname(first)
        if len(self.archives) == 1:
            try:
                size = fmt_size(os.path.getsize(first))
            except OSError:
                size = "?"
            self.lbl_name.config(text=shorten(os.path.basename(first), 52))
            self.lbl_meta.config(text="%s  \u2022  %s" % (size, shorten(folder, 52)))
        else:
            names = ", ".join(shorten(os.path.basename(a), 22) for a in self.archives[:2])
            self.lbl_name.config(text="%d archives selected" % len(self.archives))
            self.lbl_meta.config(text=names + ("\u2026" if len(self.archives) > 2 else ""))

        for w in (self.dest_row, self.sub_chk, self.lbl_hint):
            w.pack_forget()
        if self.mode == "dialog":
            self.lbl_section.config(text="Destination folder")
            self.dest_row.pack(fill="x")
            self.sub_chk.pack(anchor="w", pady=(px(8), 0))
        else:
            self.lbl_section.config(text="Extract Here")
            stem = archive_stem(first) if len(self.archives) == 1 else "<archive name>"
            hint = shorten(os.path.join(folder, stem), 56) + "\\"
            if len(self.archives) > 1:
                hint += "  (each archive gets its own folder)"
            self.lbl_hint.config(text="\u2192  " + hint)
            self.lbl_hint.pack(fill="x")

        self.lbl_sel.pack_forget()
        if self.selection:
            n = len(self.selection)
            self.lbl_sel.config(text="Extracting only the %d item%s you selected in the browser."
                                % (n, "" if n == 1 else "s"))
            self.lbl_sel.pack(fill="x", pady=(px(8), 0))
        single = len(self.archives) == 1 and not detect_split_parts(first)
        self.btn_browse.state(["!disabled"] if single else ["disabled"])

    def pick_folder(self):
        if self.state != "choice":
            return
        start = self.dest_var.get().strip() or os.path.dirname(self.archives[0])
        chosen = filedialog.askdirectory(parent=self.root, title="Choose destination folder",
                                         initialdir=start, mustexist=False)
        if chosen:
            self.dest_var.set(os.path.normpath(chosen))

    def cancel_choice(self):
        if self.from_shell:
            self.root.destroy()
        else:
            self.go_home()

    # ---- compress view --------------------------------------------------------
    def open_compress(self, sources, trigger_mode="dialog"):
        self.action = "compress"
        sources = [os.path.abspath(s) for s in sources if os.path.exists(s)]
        if trigger_mode == "compress-quick" and len(sources) == 1:
            self._run_quick_compress(sources[0])
            return
        self.compress_sources = sources
        self._name_dirty = False
        self.compress_dest_var.set("")
        self.compress_password_var.set("")
        self.encrypt_var.set(False)
        self.pw_entry.config(state="disabled")
        self.compress_fmt_var.set("ZIP")
        self.compress_level_var.set(COMPRESS_LEVELS[2][1])
        self.state = "compress_choice"
        self.refresh_compress()
        self.show(self.view_compress)

    def _run_quick_compress(self, source):
        """The right-click 'Add to ZIP' quick action: no dialog, matches a
        single click the way WinRAR's own 'Add to <name>.zip' entry does -
        safe to skip confirmation because it only ever creates a new file
        (never overwrites or deletes anything)."""
        if not self._confirm_large([source]):
            if self.from_shell:
                self.root.destroy()
            else:
                self.go_home()
            return
        dest_dir = os.path.dirname(source)
        dest_path = unique_file_path(dest_dir, default_archive_name([source], ".zip"))
        self.compress_sources = [source]
        self._start_compress_job([source], dest_path, "zip", None)

    def _confirm_large(self, sources):
        files, size, _skipped, capped = scan_sources(sources, CONFIRM_FILES, CONFIRM_BYTES)
        if not capped:
            return True
        return messagebox.askyesno(
            APP_NAME,
            "This is a large job: at least {:,} files / {} so far.\n\n"
            "Compressing it could take a long time and a lot of disk space. Continue?".format(
                files, fmt_size(size)),
            parent=self.root, icon="warning")

    def refresh_compress(self):
        n = len(self.compress_sources)
        if n == 0:
            self.lbl_cx_name.config(text="No items added yet")
            self.lbl_cx_meta.config(text="Add files or a folder to build your archive")
        elif n == 1:
            s = self.compress_sources[0]
            kind = "Folder" if os.path.isdir(s) else "File"
            self.lbl_cx_name.config(text=shorten(os.path.basename(os.path.normpath(s)), 52))
            self.lbl_cx_meta.config(text=kind)
        else:
            names = ", ".join(shorten(os.path.basename(os.path.normpath(s)), 20)
                              for s in self.compress_sources[:3])
            self.lbl_cx_name.config(text="%d items selected" % n)
            self.lbl_cx_meta.config(text=names + ("\u2026" if n > 3 else ""))

        fmt = next((f for f in COMPRESS_FORMATS if f[1] == self.compress_fmt_var.get()),
                   COMPRESS_FORMATS[0])
        if not self._name_dirty and self.compress_sources:
            self._set_compress_name(default_archive_name(self.compress_sources, fmt[2]))
        if self.compress_sources and not self.compress_dest_var.get().strip():
            self.compress_dest_var.set(os.path.dirname(os.path.normpath(self.compress_sources[0])))

    def _set_compress_name(self, name):
        self._suppress_name_trace = True
        self.compress_name_var.set(name)
        self._suppress_name_trace = False

    def _on_name_edit(self):
        if not self._suppress_name_trace:
            self._name_dirty = True

    def _on_format_change(self):
        fmt = next((f for f in COMPRESS_FORMATS if f[1] == self.compress_fmt_var.get()),
                   COMPRESS_FORMATS[0])
        if not compress_format_available(fmt[0]):
            messagebox.showwarning(
                APP_NAME, "%s support isn't included in this build." % fmt[1], parent=self.root)
        if not fmt[4]:
            self.encrypt_var.set(False)
            self.encrypt_chk.state(["disabled"])
            self.pw_entry.config(state="disabled")
        else:
            self.encrypt_chk.state(["!disabled"])
        self.refresh_compress()

    def _toggle_password_field(self):
        self.pw_entry.config(state="normal" if self.encrypt_var.get() else "disabled")

    def add_compress_files(self):
        files = filedialog.askopenfilenames(parent=self.root, title="Add files to the archive")
        if files:
            self.compress_sources.extend(os.path.abspath(f) for f in files)
            self.refresh_compress()

    def add_compress_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root, title="Add a folder to the archive", mustexist=True)
        if folder:
            self.compress_sources.append(os.path.abspath(folder))
            self.refresh_compress()

    def pick_compress_folder(self):
        start = self.compress_dest_var.get().strip() or (
            os.path.dirname(self.compress_sources[0]) if self.compress_sources
            else os.path.expanduser("~"))
        chosen = filedialog.askdirectory(parent=self.root, title="Choose destination folder",
                                         initialdir=start, mustexist=False)
        if chosen:
            self.compress_dest_var.set(os.path.normpath(chosen))

    def cancel_compress(self):
        if self.from_shell:
            self.root.destroy()
        else:
            self.go_home()

    def begin_compress(self):
        if self.state != "compress_choice":
            return
        if not self.compress_sources:
            messagebox.showwarning(APP_NAME, "Add at least one file or folder first.", parent=self.root)
            return

        dest_dir = os.path.expandvars(self.compress_dest_var.get().strip().strip('"'))
        if not dest_dir:
            messagebox.showwarning(APP_NAME, "Please choose a destination folder.", parent=self.root)
            return
        dest_dir = os.path.abspath(dest_dir)

        fmt = next((f for f in COMPRESS_FORMATS if f[1] == self.compress_fmt_var.get()),
                   COMPRESS_FORMATS[0])
        if not compress_format_available(fmt[0]):
            messagebox.showerror(APP_NAME, "%s support isn't included in this build." % fmt[1],
                                 parent=self.root)
            return

        name = self.compress_name_var.get().strip()
        if not name:
            name = default_archive_name(self.compress_sources, fmt[2])
        for _k, _l, ext, _m, _p in COMPRESS_FORMATS:
            if name.lower().endswith(ext.lower()):
                name = name[: -len(ext)]
                break
        name = (sanitize_name(name) or "Archive") + fmt[2]

        password = None
        if self.encrypt_var.get():
            if not fmt[4]:
                messagebox.showwarning(
                    APP_NAME, "%s archives don't support passwords. Choose ZIP or 7Z." % fmt[1],
                    parent=self.root)
                return
            password = self.compress_password_var.get()
            if not password:
                messagebox.showwarning(APP_NAME, "Enter a password, or turn off encryption.",
                                       parent=self.root)
                return

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Couldn't use that destination:\n\n%s" % exc,
                                 parent=self.root)
            return

        if not self._confirm_large(self.compress_sources):
            return
        level_label = self.compress_level_var.get()
        level = next((k for k, label in COMPRESS_LEVELS if label == level_label), "normal")
        dest_path = unique_file_path(dest_dir, name)
        self._start_compress_job(list(self.compress_sources), dest_path, fmt[0], password, level)

    def _start_compress_job(self, sources, dest_path, fmt_key, password, level="normal"):
        self.action = "compress"
        self.state = "processing"
        self.cancel.clear()
        self.progress_var.set(0)
        self.lbl_p_title.config(text="Preparing\u2026")
        self.lbl_p_sub.config(text="")
        self.cur_idx, self.cur_n = 1, 1
        self.show(self.view_progress)
        threading.Thread(
            target=self._compress_worker, args=(sources, dest_path, fmt_key, password, level),
            daemon=True,
        ).start()
        self._ensure_poll()

    def _compress_worker(self, sources, dest_path, fmt_key, password, level="normal"):
        self.q.put(("file", 1, 1, os.path.basename(dest_path)))
        result = {"status": "ok", "detail": "", "dest": dest_path, "notes": []}
        try:
            ctx = Ctx(self.cancel, lambda p, d, t: self.q.put(("progress", p, d, t)))
            compress_archive(sources, dest_path, fmt_key, ctx, password, level)
            result["notes"] = list(ctx.notes)
        except Cancelled:
            result["status"] = "cancelled"
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["detail"] = describe_error(exc)
            log_exception("Compression failed for %s" % dest_path)
        self.q.put(("compress_finished", result))

    def _finish_compress(self, result):
        if result["status"] == "cancelled":
            self.state = "compress_choice"
            self.show(self.view_compress)
            return
        if result["status"] == "error":
            messagebox.showerror("%s - Compression failed" % APP_NAME, result["detail"],
                                 parent=self.root)
            self.state = "compress_choice"
            self.show(self.view_compress)
            return
        self.state = "finished"
        self.lbl_d_title.config(text="Compression complete")
        self.lbl_d_sub.config(text="\n".join([os.path.basename(result["dest"])] + result["notes"]))
        self.show(self.view_done)
        delay = 1300 if not result["notes"] else 4000
        if self.from_shell:
            self.root.after(delay, self.root.destroy)
        else:
            self.root.after(delay, self.go_home)

    # ---- test archive ---------------------------------------------------------
    def test_selected(self):
        if self.state == "choice":
            self._start_test(list(self.archives))

    def run_test_now(self, files):
        """Right-click 'Test archive': skip the options screen and just test."""
        self.auto_test = True
        self.open_archives(files, "dialog")
        self._start_test(list(self.archives))

    def _start_test(self, archives):
        self.action = "test"
        self.state = "processing"
        self.cancel.clear()
        self.progress_var.set(0)
        self.lbl_p_title.config(text="Preparing\u2026")
        self.lbl_p_sub.config(text="")
        self.cur_idx, self.cur_n = 1, len(archives)
        self.show(self.view_progress)
        threading.Thread(target=self._test_worker, args=(archives, self.initial_password),
                         daemon=True).start()
        self._ensure_poll()

    def _test_worker(self, archives, initial_password):
        results = []
        total = len(archives)
        for idx, arc in enumerate(archives, 1):
            if self.cancel.is_set():
                break
            name = os.path.basename(arc)
            self.q.put(("file", idx, total, name))
            res = {"name": name, "status": "ok", "files": 0, "detail": ""}
            join_dir = None
            try:
                parts = detect_split_parts(arc)
                source = arc
                if parts:
                    join_dir = tempfile.mkdtemp(prefix=".shredpack_join_")
                    source = join_split_parts(parts, join_dir)
                password = initial_password
                while True:
                    ctx = Ctx(self.cancel, lambda p, d, t: self.q.put(("progress", p, d, t)),
                              password)
                    try:
                        test_archive(source, ctx)
                        res["files"] = ctx.files
                        break
                    except NeedPassword as need:
                        password = self._ask_password(name, need.retry)
                        if password is None:
                            self.cancel.set()
                            raise Cancelled()
            except Cancelled:
                break
            except Exception as exc:  # noqa: BLE001
                res["status"] = "error"
                res["detail"] = describe_error(exc)
                log_exception("Test failed for %s" % arc)
            finally:
                if join_dir:
                    shutil.rmtree(join_dir, ignore_errors=True)
            results.append(res)
        self.q.put(("test_finished", results))

    def _finish_test(self, results):
        cancelled = self.cancel.is_set()
        self.state = "choice"
        bad = [r for r in results if r["status"] == "error"]
        good = [r for r in results if r["status"] == "ok"]
        if not cancelled:
            if bad:
                text = "\n\n".join("%s\n%s" % (r["name"], r["detail"]) for r in bad)
                if good:
                    text += "\n\n%d archive(s) passed." % len(good)
                messagebox.showerror("%s - Problems found" % APP_NAME, text, parent=self.root)
            else:
                n = sum(r["files"] for r in good)
                messagebox.showinfo(
                    APP_NAME,
                    "No errors found.\n\n%d file%s verified in %d archive%s." % (
                        n, "" if n == 1 else "s", len(good), "" if len(good) == 1 else "s"),
                    parent=self.root)
        if self.auto_test:
            self.root.destroy()
            return
        self._back_to_choice_view()

    def _back_to_choice_view(self):
        self.state = "choice"
        self.action = "extract"
        self.refresh_choice()
        self.show(self.view_choice)

    # ---- archive browser --------------------------------------------------------
    def open_browser(self):
        if self.state != "choice" or len(self.archives) != 1:
            return
        arc = self.archives[0]
        if detect_split_parts(arc):
            messagebox.showinfo(
                APP_NAME, "Browsing isn't available for split archives yet - use Extract instead.",
                parent=self.root)
            return
        self._start_listing(arc, self.initial_password)

    def _start_listing(self, arc, password):
        self.action = "list"
        self.state = "processing"
        self.cancel.clear()
        self.progress_var.set(0)
        self.lbl_p_title.config(text="Reading " + shorten(os.path.basename(arc), 44))
        self.lbl_p_sub.config(text="Reading the archive's contents\u2026")
        self.cur_idx, self.cur_n = 1, 1
        self.show(self.view_progress)
        threading.Thread(target=self._list_worker, args=(arc, password), daemon=True).start()
        self._ensure_poll()

    def _list_worker(self, arc, password):
        try:
            listing = list_archive(arc, password)
            self.q.put(("listing_done", arc, password, listing, summarize_listing(listing, arc)))
        except NeedPassword as need:
            self.q.put(("listing_password", arc, need.retry))
        except Exception as exc:  # noqa: BLE001
            log_exception("Listing failed for %s" % arc)
            self.q.put(("listing_failed", describe_error(exc)))

    def _on_listing(self, arc, password, listing, summary):
        if self.cancel.is_set():
            self._back_to_choice_view()
            return
        self.listing = listing
        self.browse_password = password
        self.lbl_b_name.config(text=shorten(os.path.basename(arc), 56))
        self.lbl_b_info.config(text=describe_summary(summary))
        self.search_var.set("")
        self.state = "browse"
        self._browse_filter()
        self.show(self.view_browse)

    def _schedule_browse_filter(self):
        if self._filter_job is not None:
            try:
                self.root.after_cancel(self._filter_job)
            except tk.TclError:
                pass
        self._filter_job = self.root.after(150, self._browse_filter)

    def _browse_filter(self):
        self._filter_job = None
        if not self.listing:
            return
        query = self.search_var.get().strip().lower()
        entries = self.listing["entries"]
        rows = [i for i, e in enumerate(entries) if not query or query in e["path"].lower()]
        self._browse_rows = rows
        self.tree.delete(*self.tree.get_children())
        shown = rows[:BROWSE_LIMIT]
        for i in shown:
            e = entries[i]
            self.tree.insert("", "end", iid=str(i),
                             text=e["path"] + ("/" if e["is_dir"] else ""),
                             values=("" if e["is_dir"] or e["size"] is None else fmt_size(e["size"]),
                                     "" if e["is_dir"] or e["packed"] is None else fmt_size(e["packed"]),
                                     fmt_mtime(e["mtime"])))
        if len(rows) > len(shown):
            self.lbl_b_count.config(text="First %d of %d" % (len(shown), len(rows)))
        elif query:
            self.lbl_b_count.config(text="%d of %d" % (len(rows), len(entries)))
        else:
            self.lbl_b_count.config(text="%d items" % len(entries))

    def browse_back(self):
        self.selection = None
        self._back_to_choice_view()

    def browse_extract(self, selected_only):
        if self.state != "browse":
            return
        selection = None
        if selected_only:
            ids = self.tree.selection()
            if not ids:
                messagebox.showinfo(
                    APP_NAME,
                    "Select one or more files or folders first (Ctrl- or Shift-click for several).",
                    parent=self.root)
                return
            entries = self.listing["entries"]
            selection = {entries[int(i)]["path"].lower() for i in ids}
        self.selection = selection
        self.initial_password = self.browse_password
        self._back_to_choice_view()

    # ---- checksums -----------------------------------------------------------------
    def choose_checksum(self):
        path = filedialog.askopenfilename(parent=self.root, title="Choose a file to checksum")
        if path:
            self._start_hash(os.path.abspath(path))

    def _start_hash(self, path):
        self.action = "hash"
        self.state = "processing"
        self.cancel.clear()
        self.progress_var.set(0)
        self.lbl_p_title.config(text="Reading " + shorten(os.path.basename(path), 44))
        self.lbl_p_sub.config(text="")
        self.cur_idx, self.cur_n = 1, 1
        self.show(self.view_progress)
        threading.Thread(target=self._hash_worker, args=(path,), daemon=True).start()
        self._ensure_poll()

    def _hash_worker(self, path):
        self.q.put(("file", 1, 1, os.path.basename(path)))
        try:
            ctx = Ctx(self.cancel, lambda p, d, t: self.q.put(("progress", p, d, t)))
            self.q.put(("hash_done", path, compute_hashes(path, ctx)))
        except Cancelled:
            self.q.put(("hash_done", path, None))
        except Exception as exc:  # noqa: BLE001
            log_exception("Checksum failed for %s" % path)
            self.q.put(("hash_failed", describe_error(exc)))

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _show_hashes(self, path, result):
        self.go_home()
        px = self.px
        dlg = tk.Toplevel(self.root)
        dlg.title("Checksums")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        card = tk.Frame(dlg, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=px(16), pady=px(16))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(padx=px(20), pady=px(18))
        self._label(inner, shorten(os.path.basename(path), 50), 13, True, TEXT,
                    anchor="w").grid(row=0, column=0, columnspan=3, sticky="w")
        try:
            size_text = fmt_size(os.path.getsize(path))
        except OSError:
            size_text = ""
        self._label(inner, size_text, 9, anchor="w").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, px(10)))
        row = 2
        for key, label in HASH_ALGOS:
            self._label(inner, label, 10, anchor="w").grid(row=row, column=0, sticky="w", pady=px(3))
            var = tk.StringVar(value=result[key])
            tk.Entry(inner, textvariable=var, font=("Consolas", 9), width=66, bg=FIELD, fg=TEXT,
                     readonlybackground=FIELD, relief="flat", state="readonly",
                     highlightthickness=1, highlightbackground=BORDER
                     ).grid(row=row, column=1, padx=px(8), ipady=px(4))
            ttk.Button(inner, text="Copy", style="Secondary.TButton",
                       command=lambda v=var: self._copy_text(v.get())
                       ).grid(row=row, column=2)
            row += 1
        self._label(inner, "Compare with a published checksum", 10, anchor="w").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(px(12), 0))
        cmp_var = tk.StringVar()
        self._entry(inner, cmp_var, width=66).grid(
            row=row + 1, column=0, columnspan=3, sticky="ew", ipady=px(5), pady=(px(4), 0))
        verdict = self._label(inner, "", 10, anchor="w", justify="left", wraplength=px(520))
        verdict.grid(row=row + 2, column=0, columnspan=3, sticky="w", pady=(px(6), 0))

        def on_compare(*_args):
            text = cmp_var.get().strip().lower().replace(" ", "")
            if not text:
                verdict.config(text="", fg=MUTED)
                return
            hit = next((lbl for key, lbl in HASH_ALGOS if result[key].lower() == text), None)
            if hit:
                verdict.config(text="\u2713 Matches the %s checksum." % hit, fg=GREEN)
            else:
                verdict.config(
                    text="No match. Check that you copied the whole checksum - if it's complete, "
                         "this file differs from the original.", fg="#ff7b7b")

        cmp_var.trace_add("write", on_compare)
        ttk.Button(inner, text="Close", style="Primary.TButton", command=dlg.destroy).grid(
            row=row + 3, column=2, sticky="e", pady=(px(12), 0))
        enable_dark_titlebar(dlg)

    # ---- running ------------------------------------------------------------
    def begin(self):
        if self.state != "choice":
            return
        dest_dir, subfolder = None, False
        if self.mode == "dialog":
            text = os.path.expandvars(self.dest_var.get().strip().strip('"'))
            if not text:
                messagebox.showwarning(APP_NAME, "Please choose a destination folder.", parent=self.root)
                return
            dest_dir = os.path.abspath(text)
            subfolder = self.sub_var.get()
        delete = self.delete_var.get()

        policy_label = self.overwrite_var.get()
        opts = {
            "selected": set(self.selection) if self.selection else None,
            "include": parse_patterns(self.include_var.get()),
            "exclude": parse_patterns(self.exclude_var.get()),
            "overwrite": next((k for k, label in OVERWRITE_POLICIES if label == policy_label),
                              "rename"),
            "password": self.initial_password,
        }
        filtered = bool(opts["selected"] or opts["include"] or opts["exclude"])
        if filtered and delete:
            if not messagebox.askyesno(
                    APP_NAME,
                    "You're extracting only part of the archive.\n\n"
                    "Delete the original archive anyway? Everything you aren't extracting "
                    "would be lost.",
                    parent=self.root, icon="warning", default="no"):
                delete = False
        if not filtered and not self._preflight_ok(dest_dir, subfolder):
            return

        self.state = "processing"
        self.action = "extract"
        self.cancel.clear()
        self.progress_var.set(0)
        self.lbl_p_title.config(text="Preparing\u2026")
        self.lbl_p_sub.config(text="")
        self.show(self.view_progress)
        threading.Thread(
            target=self._worker,
            args=(list(self.archives), self.mode, dest_dir, subfolder, delete, opts),
            daemon=True,
        ).start()
        self._ensure_poll()

    def _preflight_ok(self, dest_dir, subfolder):
        """Warn (and let the user bail out) before extracting anything that
        looks like a decompression bomb or won't fit the destination drive.
        Skipped for split/multi-part archives, which can't be sized cheaply."""
        for arc in self.archives:
            if detect_split_parts(arc):
                continue
            try:
                fmt = detect_format(arc)
            except ArchiveError:
                continue
            dest = compute_dest(arc, self.mode, dest_dir, subfolder)
            risk = preflight_risk(arc, dest, fmt)
            if risk:
                proceed = messagebox.askyesno(
                    APP_NAME,
                    "%s\n\n%s\n\nContinue anyway?" % (shorten(os.path.basename(arc), 50), risk),
                    parent=self.root, icon="warning",
                )
                if not proceed:
                    return False
        return True

    def on_cancel(self):
        if self.state == "processing":
            self.cancel.set()
            self.lbl_p_sub.config(text="Cancelling\u2026")

    def _on_return(self):
        if self.state == "choice":
            self.begin()
        elif self.state == "compress_choice":
            self.begin_compress()

    def on_escape(self):
        if self.state == "choice":
            self.cancel_choice()
        elif self.state == "compress_choice":
            self.cancel_compress()
        elif self.state == "browse":
            self.browse_back()
        elif self.state == "processing":
            self.on_cancel()

    def on_close(self):
        if self.state == "processing":
            self.cancel.set()
        self.root.destroy()

    def _ask_password(self, name, retry):
        self.pw_event.clear()
        self.q.put(("password", name, retry))
        self.pw_event.wait()
        return self.pw_value

    def _worker(self, archives, mode, dest_dir, subfolder, delete, opts=None):
        opts = opts or {}
        results = []
        total = len(archives)
        for idx, arc in enumerate(archives, 1):
            if self.cancel.is_set():
                break
            name = os.path.basename(arc)
            self.q.put(("file", idx, total, name))
            res = {"path": arc, "name": name, "status": "error", "detail": "",
                   "deleted": False, "delete_failed": False, "notes": []}
            join_dir = None
            try:
                dest = compute_dest(arc, mode, dest_dir, subfolder)

                parts = detect_split_parts(arc)
                source = arc
                if parts:
                    join_dir = tempfile.mkdtemp(prefix=".shredpack_join_")
                    source = join_split_parts(parts, join_dir)

                password = opts.get("password")
                while True:
                    ctx = Ctx(self.cancel,
                              lambda p, d, t: self.q.put(("progress", p, d, t)), password,
                              selected=opts.get("selected"), include=opts.get("include"),
                              exclude=opts.get("exclude"),
                              overwrite=opts.get("overwrite", "rename"))
                    try:
                        extract_archive(source, dest, ctx, motw_source=arc)
                        res["notes"] = list(ctx.notes)
                        break
                    except NeedPassword as need:
                        password = self._ask_password(name, need.retry)
                        if password is None:
                            self.cancel.set()
                            raise Cancelled()

                res["status"] = "ok"
                if delete:
                    originals = parts if parts else [arc]
                    all_ok = True
                    for p in originals:
                        all_ok = remove_original(p) and all_ok
                    res["deleted"], res["delete_failed"] = all_ok, not all_ok
            except Cancelled:
                break
            except Exception as exc:  # noqa: BLE001
                res["detail"] = describe_error(exc)
                log_exception("Extraction failed for %s" % arc)
            finally:
                if join_dir:
                    shutil.rmtree(join_dir, ignore_errors=True)
            results.append(res)
        self.q.put(("finished", results))

    def _ensure_poll(self):
        if not self._poll_active:
            self._poll_active = True
            self._ensure_poll()

    def _poll(self):
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(msg)
            except Exception:  # noqa: BLE001
                log_exception("UI handler failed for message %r" % (msg[0],))
                self._recover_from_error()
                break
        if self.state == "processing":
            self.root.after(40, self._poll)
        else:
            self._poll_active = False

    def _recover_from_error(self):
        self.state = "home"
        try:
            messagebox.showerror(
                APP_NAME,
                "Something went wrong.\n\nDetails were saved to:\n%s" % log_path(),
                parent=self.root)
        except Exception:  # noqa: BLE001
            pass
        if self.from_shell:
            self.root.destroy()
        else:
            self.go_home()

    def _handle(self, msg):
        kind = msg[0]
        if kind == "file":
            _, self.cur_idx, self.cur_n, name = msg
            verb = {"compress": "Compressing ", "test": "Testing ", "hash": "Reading ",
                    "list": "Reading "}.get(self.action, "Extracting ")
            self.lbl_p_title.config(text=verb + shorten(name, 44))
            self._set_progress(0, 0, 0)
        elif kind == "progress":
            self._set_progress(msg[1], msg[2], msg[3])
        elif kind == "password":
            _, name, retry = msg
            prompt = ("Wrong password. Try again.\n\n" if retry else "") + (
                '"%s" is password-protected.\nEnter password:' % shorten(name, 40))
            pw = simpledialog.askstring(APP_NAME, prompt, show="*", parent=self.root)
            self.pw_value = pw if pw else None
            self.pw_event.set()
        elif kind == "finished":
            self._finish(msg[1])
        elif kind == "compress_finished":
            self._finish_compress(msg[1])
        elif kind == "test_finished":
            self._finish_test(msg[1])
        elif kind == "listing_done":
            self._on_listing(*msg[1:])
        elif kind == "listing_password":
            _, arc, retry = msg
            prompt = ("Wrong password. Try again.\n\n" if retry else "") + (
                '"%s" has an encrypted file list.\nEnter password:' % shorten(os.path.basename(arc), 40))
            pw = simpledialog.askstring(APP_NAME, prompt, show="*", parent=self.root)
            if pw:
                self._start_listing(arc, pw)
            else:
                self._back_to_choice_view()
        elif kind == "listing_failed":
            messagebox.showerror("%s - Couldn't read the archive" % APP_NAME, msg[1], parent=self.root)
            self._back_to_choice_view()
        elif kind == "hash_done":
            if msg[2] is None:
                self.go_home()
            else:
                self._show_hashes(msg[1], msg[2])
        elif kind == "hash_failed":
            messagebox.showerror("%s - Checksum failed" % APP_NAME, msg[1], parent=self.root)
            self.go_home()

    def _set_progress(self, pct, done, total):
        self.progress_var.set(((self.cur_idx - 1) + pct / 100.0) / self.cur_n * 100.0)
        if self.cancel.is_set():
            return
        prefix = "%d of %d  \u2022  " % (self.cur_idx, self.cur_n) if self.cur_n > 1 else ""
        detail = "%d%%" % int(pct)
        if total:
            detail += "  \u2022  %s of %s" % (fmt_size(done), fmt_size(total))
        self.lbl_p_sub.config(text=prefix + detail)

    def _finish(self, results):
        self.state = "finished"
        oks = [r for r in results if r["status"] == "ok"]
        fails = [r for r in results if r["status"] == "error"]

        if self.cancel.is_set():
            self._back_to_choice(results)
            return

        if fails:
            text = "\n\n".join("%s\n%s" % (r["name"], r["detail"]) for r in fails)
            if oks:
                text += "\n\n%d archive(s) extracted successfully." % len(oks)
            messagebox.showerror("%s - Extraction failed" % APP_NAME, text, parent=self.root)
            self._back_to_choice(results)
            return

        notes = []
        if any(r["deleted"] for r in oks):
            notes.append("Original archive deleted.")
        if any(r["delete_failed"] for r in oks):
            notes.append("The original archive couldn't be deleted.")
        for r in oks:
            notes.extend(r.get("notes", []))
        self.lbl_d_title.config(text="Extraction complete")
        self.lbl_d_sub.config(text="\n".join(notes))
        self.show(self.view_done)
        delay = 1300 if not notes else 4000
        if self.from_shell:
            self.root.after(delay, self.root.destroy)
        else:
            self.root.after(delay, self.go_home)

    def _back_to_choice(self, results):
        finished = {r["path"] for r in results if r["status"] == "ok"}
        self.archives = [a for a in self.archives if a not in finished]
        if not self.archives:
            if self.from_shell:
                self.root.destroy()
            else:
                self.go_home()
            return
        self.state = "choice"
        self.refresh_choice()
        self.show(self.view_choice)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def make_root(with_dnd):
    if with_dnd and TkinterDnD is not None:
        try:
            return TkinterDnD.Tk()
        except Exception:  # noqa: BLE001
            pass
    return tk.Tk()


def main():
    install_crash_handler()
    log_write("%s %s starting (args: %s)" % (APP_NAME, APP_VERSION, sys.argv[1:]))
    enable_dpi_awareness()
    args = sys.argv[1:]
    mode, files, i = "dialog", [], 0
    while i < len(args):
        a = args[i]
        if a == "--install":
            install_menu()
            return
        if a == "--uninstall":
            uninstall_menu()
            return
        if a == "--mode" and i + 1 < len(args):
            valid = ("dialog", "here", "folder", "test", "compress-quick", "compress-dialog")
            mode = args[i + 1] if args[i + 1] in valid else "dialog"
            i += 2
            continue
        if not a.startswith("--"):
            files.append(os.path.abspath(a))
        i += 1

    if mode == "test":
        existing = [f for f in files if os.path.isfile(f)]
        root = make_root(False)
        if not existing:
            root.withdraw()
            messagebox.showerror(APP_NAME, "The selected file couldn't be found:\n\n%s"
                                 % (files[0] if files else "?"))
            root.destroy()
            return
        app = ShredPackApp(root, from_shell=True)
        app.run_test_now(existing)
        root.mainloop()
        return

    if mode in ("compress-quick", "compress-dialog"):
        existing = [f for f in files if os.path.exists(f)]
        root = make_root(False)
        if not existing:
            root.withdraw()
            messagebox.showerror(APP_NAME, "The selected item couldn't be found:\n\n%s"
                                 % (files[0] if files else "?"))
            root.destroy()
            return
        app = ShredPackApp(root, from_shell=True)
        app.open_compress(existing, mode)
        root.mainloop()
        return

    if files:
        existing = [f for f in files if os.path.isfile(f)]
        root = make_root(False)
        if not existing:
            root.withdraw()
            messagebox.showerror(APP_NAME, "The selected file couldn't be found:\n\n%s" % files[0])
            root.destroy()
            return
        ShredPackApp(root, existing, mode, from_shell=True)
        root.mainloop()
        return

    # Opened directly by the user: offer to add the right-click menu, then show the app.
    root = make_root(True)
    if sys.platform == "win32":
        state = install_state()
        if state != "current":
            root.withdraw()
            question = (
                "The right-click option is out of date or points to a different copy of ShredPack.\n\n"
                "Update it to use this copy?"
                if state == "stale" else
                'Add "%s" to the Windows right-click menu?\n\n'
                "On archive files: Extract files...  /  Extract Here.\n"
                "On any other file or folder: Add to ZIP  /  Add to archive...\n\n"
                "This only changes settings for your user account and needs no administrator rights."
                % APP_NAME
            )
            if messagebox.askyesno(APP_NAME, question, parent=root):
                try:
                    install_menu()
                    messagebox.showinfo(
                        APP_NAME,
                        'Done! Right-click an archive and choose "%s" to extract it, '
                        'or right-click any file or folder and choose "%s" to compress it.\n\n'
                        'On Windows 11 both appear under "Show more options".' % (APP_NAME, APP_NAME),
                        parent=root)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, "Couldn't update the registry:\n\n%s" % exc, parent=root)
            root.deiconify()
    ShredPackApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
