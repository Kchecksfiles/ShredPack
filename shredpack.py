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
import gzip
import json
import lzma
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
APP_VERSION = "1.0.0"
MENU_KEY = "ShredPack"

# Point this at a raw text file you publish containing just the latest version
# string (e.g. "1.1.0"), such as a raw GitHub URL to a VERSION.txt in your repo.
# Leave blank to disable the update check entirely.
VERSION_URL = ""
RELEASES_URL = "https://github.com/"  # shown to the user when an update exists

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

# (registry sub-key name, menu text, mode). Explorer sorts sub-items by key name.
MENU_ITEMS = (
    ("1extract", "Extract files...", "dialog"),
    ("2here", "Extract Here", "here"),
    ("3folder", "Extract to folder named after the archive", "folder"),
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
    """Progress reporting, cancellation and password holder for one extraction."""

    def __init__(self, cancel, on_progress, password=None):
        self.cancel = cancel
        self.on_progress = on_progress
        self.password = password
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


def make_dir(stage, parts):
    os.makedirs(lp(os.path.join(stage, *parts)), exist_ok=True)


def write_member(stage, parts, src, ctx, raw=None, raw_size=1):
    """Stream `src` into stage/parts. Progress by bytes, or by raw file position."""
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
                make_dir(stage, parts)
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
def extract_tar(path, stage, ctx):
    size = max(1, os.path.getsize(path))
    with open(path, "rb") as raw:
        with tarfile.open(fileobj=raw, mode="r|*") as tf:
            for member in tf:
                parts = clean_parts(member.name)
                if not parts:
                    continue
                if member.isdir():
                    make_dir(stage, parts)
                elif member.isreg():
                    src = tf.extractfile(member)
                    if src is not None:
                        write_member(stage, parts, src, ctx, raw, size)
                ctx.set_position(raw.tell(), size)


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
                ctx.total = max(1, sum(f.uncompressed for f in z.list() if not f.is_directory))
            except Exception:  # noqa: BLE001
                ctx.total = 0
            z.extractall(path=stage, callback=Progress())
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
def extract_iso(path, stage, ctx):
    if pycdlib is None:
        raise Unsupported("ISO support isn't included in this build (the pycdlib package is missing).")
    iso = pycdlib.PyCdlib()
    iso.open(path)
    try:
        if iso.has_udf():
            facade = iso.get_udf_facade()
        elif iso.has_joliet():
            facade = iso.get_joliet_facade()
        elif iso.has_rock_ridge():
            facade = iso.get_rock_ridge_facade()
        else:
            facade = iso.get_iso9660_facade()

        dirs, files = [], []
        total = 0
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
                total += length
                files.append((full, length))
        ctx.total = max(1, total)

        for d in dirs:
            parts = clean_parts(re.sub(r";\d+$", "", d))
            if parts:
                make_dir(stage, parts)
        for full, length in files:
            ctx._check()
            parts = clean_parts(re.sub(r";\d+$", "", full))
            if not parts:
                continue
            target = os.path.join(stage, *parts)
            os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
            with open(lp(target), "wb") as out:
                facade.get_file_from_iso_fp(out, full)
            ctx.add(length)
    finally:
        try:
            iso.close()
        except Exception:  # noqa: BLE001
            pass


# ---- CAB (stored + MSZIP) -----------------------------------------------------
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
        _csum, cb_data, _cb_uncomp = struct.unpack("<IHH", hdr)
        if self.data_res:
            self.f.read(self.data_res)
        data = self.f.read(cb_data)
        if len(data) < cb_data:
            raise ArchiveError("The CAB file is truncated or corrupted.")
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


def extract_cab(path, stage, ctx):
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
            cb_file, uoff, ifolder, _date, _time, attribs = struct.unpack("<IIHHHH", f.read(16))
            raw_name = _read_asciiz(f)
            name = raw_name.decode("utf-8" if attribs & 0x80 else "cp437", "replace")
            files.append((name, cb_file, uoff, ifolder))

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
            for name, cb_file, uoff, _ in group:
                if uoff < pos:
                    raise ArchiveError("The CAB file has overlapping entries and can't be read.")
                reader.skip(uoff - pos)
                pos = uoff
                parts = clean_parts(name)
                remaining = cb_file
                if parts:
                    target = os.path.join(stage, *parts)
                    os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
                    dst = open(lp(target), "wb")
                else:
                    dst = None
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


def extract_xar(path, stage, ctx):
    with open(path, "rb") as f:
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
        ctx.total = max(1, sum(e[5] for e in entries if e[1] == "file"))
        for parts, kind, off, length, style, _size in entries:
            if not parts:
                continue
            if kind == "dir":
                make_dir(stage, parts)
            else:
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


def merge_into(src, dst, rel=""):
    """Move everything from the staging folder into dst; never overwrite existing
    files. Returns the list of final file paths (relative to the top-level dst)
    that were newly placed, for Mark-of-the-Web propagation."""
    placed = []
    for entry in os.scandir(lp(src)):
        target = os.path.join(dst, entry.name)
        rel_name = os.path.join(rel, entry.name) if rel else entry.name
        if entry.is_dir(follow_symlinks=False):
            if os.path.isdir(lp(target)):
                placed.extend(merge_into(os.path.join(src, entry.name), target, rel_name))
            elif os.path.exists(lp(target)):
                renamed = unique_path(dst, entry.name)
                os.rename(lp(os.path.join(src, entry.name)), lp(renamed))
                placed.extend(
                    os.path.join(rel, os.path.basename(renamed), r) if rel
                    else os.path.join(os.path.basename(renamed), r)
                    for r in _list_all_files(renamed)
                )
            else:
                os.rename(lp(os.path.join(src, entry.name)), lp(target))
                placed.extend(
                    os.path.join(rel_name, r) for r in _list_all_files(target)
                )
        else:
            if os.path.exists(lp(target)):
                target = unique_file_path(dst, entry.name)
                rel_name = os.path.join(rel, os.path.basename(target)) if rel else os.path.basename(target)
            os.rename(lp(os.path.join(src, entry.name)), lp(target))
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
    if mode == "here":
        return base
    if mode == "folder":
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
        placed = merge_into(stage, dest)
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
# Windows right-click integration (per-user registry, no admin rights needed)
# --------------------------------------------------------------------------
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
    _notify_shell()


def install_menu():
    """Create a cascading 'ShredPack' submenu for every supported archive type."""
    if sys.platform != "win32":
        return
    uninstall_menu()  # also removes the old single-entry layout
    icon = '"%s",0' % sys.executable if getattr(sys, "frozen", False) else None
    for ext in EXTENSIONS:
        base = ext_key(ext)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
            if icon:
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
        for name, label, mode in MENU_ITEMS:
            sub = base + "\\shell\\" + name
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub, 0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, label)
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, sub + r"\command", 0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, menu_command(mode))
    _notify_shell()


def install_state():
    """'none', 'stale' (old layout or another location) or 'current'."""
    if sys.platform != "win32":
        return "none"
    key = ext_key(EXTENSIONS[0]) + "\\shell\\" + MENU_ITEMS[-1][0] + r"\command"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value = winreg.QueryValueEx(k, "")[0]
        return "current" if str(value).lower() == menu_command(MENU_ITEMS[-1][2]).lower() else "stale"
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_key(EXTENSIONS[0])):
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

        self.state = "home"  # home | choice | processing | finished
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.pw_event = threading.Event()
        self.pw_value = None
        self.cur_idx, self.cur_n = 1, 1

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        w, h = self.px(580), self.px(520)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 3))

        self._build_styles()
        self._build_ui()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Return>", lambda e: self.begin() if self.state == "choice" else None)
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

        self._label(inner, "Open an archive to extract it", 16, True, TEXT).pack(pady=(px(12), 0))
        hint = "Choose a file, or drag and drop it onto this window" if DND_FILES else "Choose an archive file"
        self._label(inner, hint, 10).pack(pady=(px(4), 0))
        ttk.Button(inner, text="Choose Archive...", style="Primary.TButton",
                   command=self.choose_archives).pack(pady=(px(16), 0))
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

        self.delete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(inner, text="Delete original archive after successful extraction",
                        variable=self.delete_var, style="Zen.TCheckbutton"
                        ).pack(anchor="w", pady=(px(14), 0))

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
        self._label(box, "Extraction complete", 16, True, TEXT).pack(pady=(px(4), 0))
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
        for v in (self.view_home, self.view_choice, self.view_progress, self.view_done):
            v.pack_forget()
        view.pack(fill="both", expand=True)

    # ---- home ---------------------------------------------------------------
    def go_home(self):
        self.state = "home"
        self.archives = []
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
                    'Done! Right-click any archive and choose "%s".\n\n'
                    'On Windows 11 it appears under "Show more options".' % APP_NAME,
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

    def _on_drop(self, event):
        if self.state == "home":
            try:
                paths = list(self.root.tk.splitlist(event.data))
            except tk.TclError:
                paths = []
            files = [p for p in paths if os.path.isfile(p)]
            if files:
                self.open_archives(files, "dialog")
        return getattr(event, "action", "copy")

    # ---- choice view --------------------------------------------------------
    def open_archives(self, files, mode):
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
        self.dest_var.set(os.path.dirname(self.archives[0]))
        self.sub_var.set(False)
        self.delete_var.set(False)
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
            self.lbl_section.config(text="Extract Here" if self.mode == "here"
                                    else "Extract to folder named after the archive")
            if self.mode == "here":
                hint = shorten(folder, 56) + ("  (each archive's own folder)" if len(self.archives) > 1 else "")
            else:
                stem = archive_stem(first) if len(self.archives) == 1 else "<archive name>"
                hint = shorten(os.path.join(folder, stem), 56) + "\\"
            self.lbl_hint.config(text="\u2192  " + hint)
            self.lbl_hint.pack(fill="x")

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

        if not self._preflight_ok(dest_dir, subfolder):
            return

        self.state = "processing"
        self.cancel.clear()
        self.progress_var.set(0)
        self.lbl_p_title.config(text="Preparing\u2026")
        self.lbl_p_sub.config(text="")
        self.show(self.view_progress)
        threading.Thread(
            target=self._worker,
            args=(list(self.archives), self.mode, dest_dir, subfolder, delete),
            daemon=True,
        ).start()
        self.root.after(40, self._poll)

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

    def on_escape(self):
        if self.state == "choice":
            self.cancel_choice()
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

    def _worker(self, archives, mode, dest_dir, subfolder, delete):
        results = []
        total = len(archives)
        for idx, arc in enumerate(archives, 1):
            if self.cancel.is_set():
                break
            name = os.path.basename(arc)
            self.q.put(("file", idx, total, name))
            res = {"path": arc, "name": name, "status": "error", "detail": "",
                   "deleted": False, "delete_failed": False}
            join_dir = None
            try:
                dest = compute_dest(arc, mode, dest_dir, subfolder)

                parts = detect_split_parts(arc)
                source = arc
                if parts:
                    join_dir = tempfile.mkdtemp(prefix=".shredpack_join_")
                    source = join_split_parts(parts, join_dir)

                password = None
                while True:
                    ctx = Ctx(self.cancel,
                              lambda p, d, t: self.q.put(("progress", p, d, t)), password)
                    try:
                        extract_archive(source, dest, ctx, motw_source=arc)
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

    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.state == "processing":
            self.root.after(40, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "file":
            _, self.cur_idx, self.cur_n, name = msg
            self.lbl_p_title.config(text="Extracting " + shorten(name, 44))
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
        self.lbl_d_sub.config(text=" ".join(notes))
        self.show(self.view_done)
        if self.from_shell:
            self.root.after(1300, self.root.destroy)
        else:
            self.root.after(1300, self.go_home)

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
            mode = args[i + 1] if args[i + 1] in ("dialog", "here", "folder") else "dialog"
            i += 2
            continue
        if not a.startswith("--"):
            files.append(os.path.abspath(a))
        i += 1

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
                'Add "%s" to the Windows right-click menu for archive files?\n\n'
                "You'll get: Extract files...  /  Extract Here  /  Extract to folder.\n"
                "This only changes settings for your user account and needs no administrator rights."
                % APP_NAME
            )
            if messagebox.askyesno(APP_NAME, question, parent=root):
                try:
                    install_menu()
                    messagebox.showinfo(
                        APP_NAME,
                        'Done! Right-click any archive and choose "%s".\n\n'
                        'On Windows 11 it appears under "Show more options".' % APP_NAME,
                        parent=root)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, "Couldn't update the registry:\n\n%s" % exc, parent=root)
            root.deiconify()
    ShredPackApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
