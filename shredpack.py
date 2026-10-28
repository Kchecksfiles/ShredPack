"""
ShredPack - a WinRAR-style archive extractor with its own built-in engine.

* No WinRAR, no 7-Zip: ShredPack extracts archives itself (RAR extraction uses the
  optional unrar-cffi package, which bundles the freely redistributable UnRAR source).
* Right-click any archive -> Extract files... / Extract Here / Extract to a folder named after the archive,
  plus an "Open with ShredPack" submenu (Extract & Free Space, Test, Add to ZIP, Add to archive).
* The program only runs when you open it or pick a right-click option. It does
  its job and exits; nothing stays running in the background.

Python packages bundled into the .exe at build time:
    pip install py7zr pycdlib pyzipper tkinterdnd2 send2trash unrar-cffi pyinstaller
(py7zr adds .7z, pycdlib adds .iso, pyzipper adds AES-encrypted ZIPs, unrar-cffi adds .rar,
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
import zipfile
import zlib
import math
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

if sys.platform == "win32":
    import ctypes
    import winreg

class _Lazy(object):
    """Stands in for an optional library; the real import happens the first time it is used.

    `if not lib:` still means "not installed", so every existing check keeps working - but a
    right-click on a .zip no longer pays for importing py7zr / pyzipper / pycdlib at start-up.
    """

    def __init__(self, name):
        self._name, self._mod, self._tried = name, None, False

    def _load(self):
        if not self._tried:
            self._tried = True
            try:
                import importlib
                self._mod = importlib.import_module(self._name)
            except Exception:  # noqa: BLE001
                self._mod = None
        return self._mod

    def __bool__(self):
        return self._load() is not None

    def __getattr__(self, attr):
        mod = self._load()
        if mod is None:
            raise AttributeError("%s is not installed" % self._name)
        return getattr(mod, attr)


py7zr = _Lazy("py7zr")
pycdlib = _Lazy("pycdlib")
pyzipper = _Lazy("pyzipper")

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
APP_VERSION = "1.2.0"
MENU_KEY = "ShredPack"
# Name of the separate compression cascade that v1.1.0 installed; only used now to
# find and remove it (the commands live in the single "ShredPack" menu).
LEGACY_COMPRESS_MENU_KEY = "ShredPackCompress"
# Registry classes that get the compression-only menu: any file, and any folder.
# Archives get the combined menu from their own per-extension key instead.
COMPRESS_CLASSES = ("*", "Directory")
# On "*" (every file) the compression-only menu would appear a second time on
# archives, which have their own full menu. Hide it there with an AppliesTo
# query. If a future Windows build ever mishandles that query (symptom: no
# ShredPack menu on ordinary files), set this to False - archives then rely on
# Explorer merging the same-named "ShredPack" verbs instead.
GENERIC_MENU_EXCLUDES_ARCHIVES = True

# Point this at a raw text file you publish containing just the latest version
# string (e.g. "1.1.0"), such as a raw GitHub URL to a VERSION.txt in your repo.
# Leave blank to disable the update check entirely.
VERSION_URL = "https://raw.githubusercontent.com/Kchecksfiles/ShredPack/main/VERSION.txt"
RELEASES_URL = "https://github.com/Kchecksfiles/ShredPack/releases"  # opened from the update link

EXTENSIONS = (
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".tbz", ".txz",
    ".iso", ".cab", ".xar", ".z01", ".zip.001",
)
TAR_COMPRESSED = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz")
FORMATS = ("ZIP", "7Z", "RAR", "TAR", "GZ", "BZ2", "XZ", "ISO", "CAB", "XAR", "split ZIP")
FORMATS_LABEL = "  \u2022  ".join(FORMATS)

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

# ---- Right-click menu layout -------------------------------------------------------
# Right-clicking an ARCHIVE shows, in this order:
#     Extract files...                      \
#     Extract Here                           }  direct items (DIRECT_MENU_ITEMS)
#     Extract to <archive name>\            /
#     Open with ShredPack  >  Extract & Free Space... / Test archive / Add to ZIP /
#                             Add to archive...                     (ARCHIVE_SUBMENU_ITEMS)
# Right-clicking any other file or folder shows only "Open with ShredPack" with the two
# compression commands (COMPRESS_MENU_ITEMS).
#
# Explorer lists classic shell verbs by registry key name, hence the numbered key names.
# Windows can only show a fixed label for a registry-based verb, so the third item reads
# "Extract to <archive name>\" literally (Explorer cannot fill in the real name without a
# compiled shell extension, which ShredPack deliberately does not install).
SUBMENU_LABEL = "Open with ShredPack"
SUBMENU_KEY = "ShredPack4Open"        # the submenu on archive extensions (sorts after the direct items)
OWNER_VALUE = "ShredPackItem"         # marker value on every top-level key ShredPack creates
DIRECT_MENU_ITEMS = (
    ("ShredPack1Extract", "Extract files...", "dialog"),
    ("ShredPack2Here", "Extract Here", "here"),
    ("ShredPack3Folder", "Extract to <archive name>\\", "folder"),
)
# "&" starts an accelerator in a menu label, so a literal ampersand is written "&&".
ARCHIVE_SUBMENU_ITEMS = (
    ("1free", "Extract && Free Space...", "free"),
    ("2test", "Test archive", "test"),
    ("3addzip", "Add to ZIP", "compress-quick"),
    ("4addarchive", "Add to archive...", "compress-dialog"),
)
# Shown on ordinary files and folders.
COMPRESS_MENU_ITEMS = (
    ("1addzip", "Add to ZIP", "compress-quick"),
    ("2addarchive", "Add to archive...", "compress-dialog"),
)
# Every command an archive's menu launches, for tests and documentation.
ARCHIVE_MENU_ITEMS = DIRECT_MENU_ITEMS + ARCHIVE_SUBMENU_ITEMS

CHUNK = 1024 * 1024
INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)

# ---- Look: bold, flat, compact; ShredPack's blue stays the one accent colour ----
# Fonts are resolved once a window exists (pick_fonts): Bahnschrift - Windows' built-in
# DIN-style face - gives the headings their industrial look, Consolas the small caps labels.
FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"
FONT_DISPLAY = "Segoe UI Semibold"   # headings; replaced by "Bahnschrift" when installed
FONT_MONO = "Consolas"               # tiny uppercase labels, numbers
BG = "#161616"
CARD = "#212121"
BORDER = "#3a3a3a"
TEXT = "#f5f5f5"
MUTED = "#9d9d9d"
DIM = "#707070"
ACCENT = "#60cdff"          # ShredPack blue (unchanged)
ACCENT_DEEP = "#1f6fe8"     # the deeper blue of the app icon
ON_ACCENT = "#06202d"       # text/icons drawn on top of ACCENT
ICON_BG = "#343434"
TROUGH = "#383838"
GREEN = "#6ccb5f"
DANGER = "#ff8c8c"
FIELD = "#2c2c2c"
KEY = "#2a2a2a"             # resting colour of a "key" (big button)
KEY_HOVER = "#343434"


def pick_fonts(root):
    """Use Bahnschrift / Consolas when this PC has them; otherwise keep the Segoe UI defaults."""
    global FONT_DISPLAY, FONT_MONO
    try:
        from tkinter import font as tkfont
        families = set(tkfont.families(root))
    except Exception:  # noqa: BLE001 - styling must never stop the app from starting
        return
    if "Bahnschrift" in families:
        FONT_DISPLAY = "Bahnschrift"
    if "Consolas" not in families:
        for name in ("Cascadia Mono", "Courier New"):
            if name in families:
                FONT_MONO = name
                break


# The original ShredPack app icon (the blue tile with the white stack), as small PNGs for the
# window header. Generated from shredpack.ico; the .ico itself is still the exe/window icon.
LOGO_PNG_B64 = {
    44: (
        "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"
    ),
    66: (
        "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"
    ),
    88: (
        "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"
    ),
}

# Window height (96-dpi pixels) per screen: each one is only as tall as its content.
VIEW_HEIGHTS = {"home": 470, "choice": 590, "compress": 650, "browse": 600, "progress": 270,
                "done": 300, "settings": 610}
TOUR_EXTRA_HEIGHT = 140     # extra room on the home screen while the first-run tour is showing

# The first-run tour: (title, text). Three steps, so it can be finished in three clicks.
TOUR_STEPS = (
    ("Open an archive",
     "Drop one on the big box, or press Extract. ShredPack reads ZIP, 7Z, RAR, TAR, GZ, BZ2, XZ, "
     "ISO, CAB and XAR."),
    ("Right-click works too",
     "On an archive: Extract files, Extract Here and Extract to <archive name>. "
     "\"Open with ShredPack\" adds Extract & Free Space, Test archive and Add to ZIP."),
    ("Make it yours",
     "SETTINGS (top right) controls what happens to files that already exist and the finish "
     "sound. Extract & Free Space always asks before it deletes anything."),
)


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


def open_url(url):
    import webbrowser  # imported on demand
    webbrowser.open(url)


def check_for_update(callback):
    """Background-safe: fetches VERSION_URL and calls callback(latest_str or None)."""
    if not VERSION_URL:
        return
    def worker():
        try:
            import urllib.request  # imported here: it pulls in ssl, which a quick right-click never needs
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
        if fmt == "7z" and bool(py7zr):
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
        self.multivolume = False   # set by the RAR reader when the archive is one part of a set
        self.total = 0
        self.done = 0
        self.unit = "bytes"   # "items" when an archive has entries but no data bytes at all
        self._last = 0.0

    def _check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _emit(self, force=False):
        now = time.monotonic()
        if force or now - self._last >= 0.05:
            self._last = now
            pct = min(100.0, self.done * 100.0 / self.total) if self.total else 0.0
            if self.unit == "items":
                self.on_progress(pct, 0, 0)       # no byte counts to show
            else:
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


VERIFY_CHUNK = 256 * 1024   # small, so progress and Cancel stay responsive
_ZIP_METHODS = {0: "Stored", 8: "Deflate", 9: "Deflate64", 12: "BZIP2", 14: "LZMA",
                93: "Zstandard", 95: "XZ", 98: "PPMd"}


def _zip_label(info):
    return shorten(info.filename.replace("\\", "/"), 70)


def _verify_zip_entry(zf, info, pw, ctx):
    """Decompress one ZIP entry in small chunks, checking for cancellation and
    reporting progress after every chunk, then validate its size and CRC-32.
    Nothing is written anywhere."""
    ctx._check()
    label = _zip_label(info)
    aes = _is_aes_info(info)
    crc = size = 0
    try:
        with zf.open(info, pwd=pw if info.flag_bits & 0x1 else None) as src:
            while True:
                chunk = src.read(VERIFY_CHUNK)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
                size += len(chunk)
                if ctx.unit == "bytes":
                    ctx.add(len(chunk))     # raises Cancelled; emits progress (throttled)
                else:
                    ctx._check()
    except NotImplementedError:             # (a RuntimeError subclass - must come first)
        method = info.compress_type
        name = _ZIP_METHODS.get(method)
        raise ArchiveError("'%s' uses compression method %d%s, which ShredPack can't read." % (
            label, method, " (%s)" % name if name else ""))
    except RuntimeError as exc:             # zipfile: "Bad password for file ..."
        if "password" in str(exc).lower():
            raise NeedPassword(pw is not None)
        raise ArchiveError("'%s' couldn't be read: %s" % (label, exc))
    except zipfile.BadZipFile as exc:
        if "CRC" in str(exc):
            if info.flag_bits & 0x1 and pw is not None:
                raise NeedPassword(True)    # a wrong ZipCrypto password that slipped past the check byte
            raise ArchiveError("'%s' is corrupted: its checksum (CRC-32) doesn't match the stored value." % label)
        raise ArchiveError("'%s' is corrupted: %s" % (label, exc))
    except (zlib.error, lzma.LZMAError):
        if info.flag_bits & 0x1 and pw is not None and not aes:
            raise NeedPassword(True)        # a wrong ZipCrypto password (1 in 256 pass the check byte) decrypts to garbage
        raise ArchiveError("'%s' is corrupted: its compressed data is damaged." % label)
    except EOFError:
        raise ArchiveError("'%s' is truncated: the archive ends before its data does." % label)
    except OSError as exc:
        if exc.errno is None:               # bz2 reports bad data as a bare OSError
            if info.flag_bits & 0x1 and pw is not None and not aes:
                raise NeedPassword(True)
            raise ArchiveError("'%s' is corrupted: its compressed data is damaged." % label)
        raise
    if size != info.file_size:
        raise ArchiveError("'%s' is corrupted: expected %d bytes but found %d." % (
            label, info.file_size, size))
    if not aes and (crc & 0xFFFFFFFF) != info.CRC:    # AES entries are authenticated instead
        raise ArchiveError("'%s' is corrupted: its checksum (CRC-32) doesn't match the stored value." % label)


def _verify_zip(zf, infos, ctx, pw):
    """Test Archive for ZIP: every file entry is decompressed and checked."""
    files = [i for i in infos if not is_dir_entry(i)]
    data_bytes = sum(i.file_size for i in files)
    if data_bytes > 0:
        ctx.total, ctx.unit = data_bytes, "bytes"
    else:                                   # only empty files: count entries instead
        ctx.total, ctx.unit = max(1, len(files)), "items"
    ctx.done = 0
    ctx._emit(True)                         # show 0% straight away
    for info in files:
        ctx._check()                        # between entries - even zero-byte ones
        _verify_zip_entry(zf, info, pw, ctx)
        ctx.files += 1
        if ctx.unit == "items":
            ctx.add(1)
    if ctx.files == 0:
        ctx.notes.append("The archive contains no files.")


def extract_zip(path, stage, ctx):
    with zipfile.ZipFile(path) as zf:
        all_infos = zf.infolist()
        ctx._check()
        # Testing verifies every entry; extracting skips macOS metadata junk.
        infos = all_infos if ctx.dry_run else [i for i in all_infos if not is_junk(i.filename)]
        if not infos:
            if ctx.dry_run:
                ctx.notes.append("The archive is valid but empty - it contains no files.")
                return
            raise ArchiveError("The archive is empty.")
        encrypted = any(i.flag_bits & 0x1 for i in infos)
        uses_aes = any(_is_aes_info(i) for i in infos)
        pw = ctx.password.encode("utf-8") if ctx.password else None
        if encrypted and pw is None:
            raise NeedPassword(False)

        if uses_aes:
            if not pyzipper:
                raise Unsupported(
                    "This ZIP is AES-encrypted. AES support isn't included in this build "
                    "(the pyzipper package is missing)."
                )
            _extract_zip_with(pyzipper.AESZipFile(path), infos, stage, ctx, pw, encrypted)
            return
        _extract_zip_with(zf, infos, stage, ctx, pw, encrypted, already_open=True)


def _extract_zip_with(zf, infos, stage, ctx, pw, encrypted, already_open=False):
    try:
        if ctx.dry_run:
            _verify_zip(zf, infos, ctx, pw)
            return
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
            except NotImplementedError:             # (a RuntimeError subclass - must come first)
                raise ArchiveError(
                    "This ZIP uses a compression method ShredPack can't read."
                )
            except RuntimeError as exc:
                if "password" in str(exc).lower() or "Bad password" in str(exc):
                    raise NeedPassword(pw is not None)
                raise
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


def join_split_parts(parts, workdir, ctx=None):
    """Concatenate split-archive parts into one temp file and return its path.
    With a Ctx the copy reports progress and can be cancelled (raising Cancelled;
    the caller removes `workdir`, partial file included)."""
    joined = os.path.join(workdir, "joined.zip")
    if ctx is not None:
        ctx.total = max(1, sum(os.path.getsize(lp(p)) for p in parts))
        ctx.done = 0
        ctx._emit(True)
    with open(lp(joined), "wb") as out:
        for part in parts:
            with open(lp(part), "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    if ctx is not None:
                        ctx.add(len(chunk))
    if ctx is not None:
        ctx._emit(True)
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
    if not py7zr:
        raise Unsupported("7Z support isn't included in this build (the py7zr package is missing).")
    from py7zr.callbacks import ExtractCallback

    class Progress(ExtractCallback):
        def report_start_preparation(self):
            ctx._check()

        def report_start(self, processing_file_path, processing_bytes):
            ctx._check()

        def report_update(self, decompressed_bytes):
            pass        # required by py7zr 1.x; progress is reported per file in report_end

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
                ctx.files = len(names)
            else:
                z.extractall(path=stage, callback=Progress())
                ctx.files = len(file_infos)
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
    if not pycdlib:
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


# ---- RAR (optional unrar-cffi) ---------------------------------------------------
# ShredPack only ever READS RAR archives. Reading uses the optional "unrar-cffi" package,
# a thin binding to RARLAB's UnRAR source, whose licence allows free redistribution for
# extraction (it may not be used to create RAR archives, and ShredPack never does).
# If the package isn't in a build, RAR files report a clear "not included" message.
# Known limits, checked against sample archives: password-protected files are supported,
# archives whose *file names* are encrypted are not, and ShredPack does not delete
# multi-part RAR sets with Extract & Free Space.
_RAR_END, _RAR_BAD_DATA, _RAR_BAD_ARCHIVE, _RAR_UNKNOWN_FORMAT, _RAR_EOPEN = 10, 12, 13, 14, 15
_RAR_EREAD, _RAR_MISSING_PASSWORD, _RAR_BAD_PASSWORD = 18, 22, 24
_RAR_SPLIT_BEFORE, _RAR_SPLIT_AFTER, _RAR_ENCRYPTED, _RAR_DIRECTORY = 0x1, 0x2, 0x4, 0x20
_RAR_OP_SKIP, _RAR_OP_TEST = 0, 1          # we "test" every entry and stream the data ourselves
_RAR_OM_EXTRACT, _RAR_OM_LIST_INCSPLIT = 1, 2
_RAR_PART = re.compile(r"\.part0*(\d+)\.rar$", re.IGNORECASE)


def rar_available():
    try:
        _rar_api()
        return True
    except Unsupported:
        return False


def _rar_api():
    try:
        from unrar.cffi import unrarlib  # noqa: F401 - importing it registers the C callback stub
        from unrar.cffi._unrarlib import ffi, lib
    except Exception:  # noqa: BLE001
        raise Unsupported("RAR support isn't included in this build of ShredPack "
                          "(the unrar-cffi package is missing).")
    return ffi, lib


class _RarReader(object):
    """One pass over a RAR archive: read a header, then skip or stream that entry."""

    def __init__(self, path, mode, password=None):
        self.ffi, self.lib = _rar_api()
        ffi, lib = self.ffi, self.lib
        self.password = password or None
        self.on_data = None          # called with each chunk of the entry being processed
        self.error = None            # an exception raised inside on_data (e.g. Cancelled)
        self.password_missing = False
        self._name = ffi.new("wchar_t[]", path)
        data = ffi.new("struct RAROpenArchiveDataEx *", {"ArcNameW": self._name, "OpenMode": mode})
        self.handle = lib.RAROpenArchiveEx(data)
        if not self.handle or data.OpenResult != 0:
            code = data.OpenResult
            if self.handle:
                lib.RARCloseArchive(self.handle)
                self.handle = None
            raise self._failure(code, opening=True)
        self._cb = ffi.new_handle(self._callback)     # must outlive every call into the library
        lib.RARSetCallbackPtr(self.handle, lib.PyUNRARCALLBACKStub, self._cb)

    def close(self):
        if self.handle:
            self.lib.RARCloseArchive(self.handle)
            self.handle = None

    def _callback(self, msg, p1, p2):
        ffi, lib = self.ffi, self.lib
        if msg == lib.UCM_PROCESSDATA:
            if self.on_data is None:
                return 1
            try:
                self.on_data(bytes(ffi.buffer(ffi.cast("char *", p1), p2)))
                return 1
            except BaseException as exc:  # noqa: BLE001 - nothing may escape into C code
                self.error = exc
                return -1
        if msg in (lib.UCM_NEEDPASSWORDW, lib.UCM_NEEDPASSWORD):
            if not self.password:
                self.password_missing = True
                return -1
            if msg == lib.UCM_NEEDPASSWORDW:
                buf, text = ffi.cast("wchar_t *", p1), self.password
            else:
                buf, text = ffi.cast("char *", p1), self.password.encode("utf-8", "replace")
            n = min(len(text), max(0, p2 - 1))
            for i in range(n):
                buf[i] = text[i:i + 1] if isinstance(text, bytes) else text[i]
            buf[n] = b"\0" if isinstance(text, bytes) else "\0"
            return 1
        if msg in (lib.UCM_CHANGEVOLUME, lib.UCM_CHANGEVOLUMEW):
            return 1 if p2 == 1 else -1       # next part found (notify) / not found (ask)
        return 1

    def _failure(self, code, opening=False, encrypted=False):
        """Turn a UnRAR result code into the exception ShredPack's callers expect."""
        if self.error is not None:
            return self.error
        if self.password_missing or code == _RAR_MISSING_PASSWORD:
            if opening:
                return Unsupported("This RAR archive has encrypted file names, which ShredPack "
                                   "can't open yet. Use another tool for this archive.")
            return NeedPassword(False)
        if code == _RAR_BAD_PASSWORD:
            return NeedPassword(True)
        if code == _RAR_BAD_DATA:
            if encrypted and self.password:
                return NeedPassword(True)     # a wrong RAR3 password only shows up as a bad checksum
            return ArchiveError("The RAR archive looks corrupted: a file failed its checksum.")
        if code == _RAR_BAD_ARCHIVE:
            return ArchiveError("This doesn't look like a valid RAR archive, or it is damaged.")
        if code == _RAR_UNKNOWN_FORMAT:
            return Unsupported("This RAR archive uses a format ShredPack can't read.")
        if code == _RAR_EOPEN:
            return ArchiveError("Couldn't open the RAR archive. If it is part of a multi-part set, "
                                "make sure every part is in the same folder and open the first part.")
        if code == _RAR_EREAD:
            return ArchiveError("The RAR archive is incomplete or couldn't be read.")
        return ArchiveError("The RAR archive couldn't be read (error code %d)." % code)

    def next_header(self):
        """The next entry's header, or None at the end of the archive."""
        hd = self.ffi.new("struct RARHeaderDataEx *")
        res = self.lib.RARReadHeaderEx(self.handle, hd)
        if res == _RAR_END:
            return None
        if res != 0:
            raise self._failure(res)
        return hd

    def skip(self):
        self.lib.RARProcessFileW(self.handle, _RAR_OP_SKIP, self.ffi.NULL, self.ffi.NULL)

    def stream(self, hd, sink):
        """Decode the current entry, passing its data to `sink`. Raises on any failure."""
        self.on_data, self.error, self.password_missing = sink, None, False
        res = self.lib.RARProcessFileW(self.handle, _RAR_OP_TEST, self.ffi.NULL, self.ffi.NULL)
        self.on_data = None
        if res != 0 or self.error is not None:
            raise self._failure(res, encrypted=bool(hd.Flags & _RAR_ENCRYPTED))


def _rar_name(hd, ffi):
    return ffi.string(hd.FileNameW)


def _rar_size(hd):
    return hd.UnpSize + (hd.UnpSizeHigh << 32)


def _rar_mtime(hd):
    ft = (hd.MtimeHigh << 32) | hd.MtimeLow          # Windows FILETIME, 100 ns since 1601
    if ft:
        return ft / 1e7 - 11644473600.0
    dos = hd.FileTime                                  # 2-second DOS timestamp fallback
    try:
        date, tm = dos >> 16, dos & 0xFFFF
        import calendar
        return float(calendar.timegm((1980 + (date >> 9), (date >> 5) & 0xF or 1, date & 0x1F or 1,
                                      tm >> 11, (tm >> 5) & 0x3F, 2 * (tm & 0x1F))))
    except (ValueError, OverflowError):
        return None


def _rar_is_link(hd):
    if hd.RedirType:
        return True
    return hd.HostOS == 3 and (hd.FileAttr & 0o170000) == 0o120000    # a Unix symlink


def _rar_check_part_name(path):
    m = _RAR_PART.search(path)
    if m and int(m.group(1)) != 1:
        raise ArchiveError("This is a later part of a multi-part RAR set. "
                           "Open the first part (the one ending in .part1.rar) instead.")


def rar_scan(path, password=None):
    """Read a RAR archive's headers (no data). Returns a list of dicts."""
    _rar_check_part_name(path)
    reader = _RarReader(path, _RAR_OM_LIST_INCSPLIT, password)
    ffi, entries = reader.ffi, []
    try:
        while True:
            hd = reader.next_header()
            if hd is None:
                break
            entries.append({
                "name": _rar_name(hd, ffi), "size": _rar_size(hd),
                "packed": hd.PackSize + (hd.PackSizeHigh << 32), "mtime": _rar_mtime(hd),
                "is_dir": bool(hd.Flags & _RAR_DIRECTORY), "encrypted": bool(hd.Flags & _RAR_ENCRYPTED),
                "split": bool(hd.Flags & (_RAR_SPLIT_BEFORE | _RAR_SPLIT_AFTER)),
                "link": _rar_is_link(hd),
            })
            reader.skip()
    except NeedPassword:
        # Headers are only unreadable like this when the archive hides its file names.
        raise Unsupported("This RAR archive has encrypted file names, which ShredPack "
                          "can't open yet. Use another tool for this archive.")
    finally:
        reader.close()
    return entries


def extract_rar(path, stage, ctx):
    entries = rar_scan(path)
    ctx.multivolume = bool(_RAR_PART.search(path)) or any(e["split"] for e in entries)
    want_total = 0
    for e in entries:
        parts = clean_parts(e["name"])
        if parts and not e["is_dir"] and not e["link"] and ctx.wants(parts):
            want_total += e["size"]
    ctx.total = max(1, want_total)
    reader = _RarReader(path, _RAR_OM_EXTRACT, ctx.password)
    ffi, links = reader.ffi, 0
    try:
        while True:
            ctx._check()
            hd = reader.next_header()
            if hd is None:
                break
            parts = clean_parts(_rar_name(hd, ffi))
            is_dir = bool(hd.Flags & _RAR_DIRECTORY)
            if _rar_is_link(hd):
                links += 1
                reader.skip()
                continue
            if not parts or not ctx.wants(parts, is_dir):
                reader.skip()
                continue
            if is_dir:
                if not ctx.dry_run:
                    make_dir(stage, parts)
                reader.skip()
                continue
            if ctx.dry_run:
                reader.stream(hd, lambda chunk: ctx.add(len(chunk)))
                ctx.files += 1
                continue
            target = os.path.join(stage, *parts)
            os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
            with open(lp(target), "wb") as dst:
                def sink(chunk, dst=dst):
                    dst.write(chunk)
                    ctx.add(len(chunk))
                reader.stream(hd, sink)
            mtime = _rar_mtime(hd)
            if mtime:
                _set_mtime(target, mtime)
            ctx.files += 1
    finally:
        reader.close()
    if links:
        ctx.notes.append("%d link(s) in the archive were not restored." % links)


# ---- format detection -----------------------------------------------------------
HANDLERS = {
    "zip": extract_zip,
    "tar": extract_tar,
    "single": extract_single,
    "7z": extract_7z,
    "iso": extract_iso,
    "cab": extract_cab,
    "xar": extract_xar,
    "rar": extract_rar,
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
        return "rar"
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
    for ext, label in ((".wim", "WIM"), (".dmg", "DMG"), (".vhd", "VHD"), (".vhdx", "VHDX")):
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


# --------------------------------------------------------------------------
# User settings (a small JSON file next to the log; every key is optional)
# --------------------------------------------------------------------------
SETTINGS_DEFAULTS = {
    "quick_here": True,      # "Extract Here" starts at once - no confirmation screen
    "delete_after": False,   # delete the original archive after a successful extraction
    "overwrite": "rename",   # one of the OVERWRITE_POLICIES keys
    "sound": True,           # short chime when a task finishes
    "tutorial_done": False,  # the first-run tour was finished or skipped
    "menu_declined": False,  # the user said no to adding the right-click menu at start-up
}


def settings_path():
    return os.path.join(app_data_dir(), "settings.json")


def load_settings():
    data = dict(SETTINGS_DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return data
    if isinstance(raw, dict):
        policy_keys = {k for k, _label in OVERWRITE_POLICIES}
        for name in SETTINGS_DEFAULTS:
            value = raw.get(name, SETTINGS_DEFAULTS[name])
            if name == "overwrite":
                if value in policy_keys:
                    data[name] = value
            elif isinstance(value, bool):
                data[name] = value
    return data


def save_settings(settings):
    try:
        os.makedirs(app_data_dir(), exist_ok=True)
        tmp = settings_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: settings.get(k, v) for k, v in SETTINGS_DEFAULTS.items()}, f, indent=2)
        os.replace(tmp, settings_path())
        return True
    except OSError:
        log_exception("Couldn't save settings")
        return False


# --------------------------------------------------------------------------
# Completion sounds - synthesised in memory, no audio files, Windows only
# --------------------------------------------------------------------------
SOUND_RATE = 44100
DONE_SOUND_STYLE = "glass"   # "glass" | "pop" | "chord"

# note = (start ms, Hz, length ms, peak, decay ms, FM brightness, 2nd-harmonic amount)
_SOUND_RECIPES = {
    # three quick rising bell tones (G - D - G) with a soft echo: bright attack, clean tail
    "glass": dict(total=720, echo=((0.115, 0.30), (0.19, 0.16)), level=0.30, notes=(
        (0, 784.0, 650, 0.55, 105, 2.4, 0.0), (70, 1174.7, 650, 0.50, 115, 2.1, 0.0),
        (140, 1568.0, 700, 0.45, 150, 1.7, 0.0))),
    # a short two-note "bu-bing"
    "pop": dict(total=420, echo=((0.09, 0.22),), level=0.30, notes=(
        (0, 659.3, 260, 0.6, 55, 1.6, 0.0), (55, 987.8, 330, 0.55, 90, 1.9, 0.0))),
    # a soft major chord, strummed over 16 ms
    "chord": dict(total=760, echo=((0.13, 0.25),), level=0.28, notes=(
        (0, 1046.5, 700, 0.5, 220, 0.9, 0.0), (8, 1318.5, 700, 0.45, 240, 0.9, 0.0),
        (16, 1568.0, 700, 0.42, 260, 0.9, 0.0))),
    # one short low blip (A4 -> F4); the sound used for failures
    "error": dict(total=380, echo=(), level=None, notes=(
        (0, 440.0, 220, 0.22, 70, 0.0, 0.18), (110, 349.2, 260, 0.20, 80, 0.0, 0.18))),
}
_sound_cache = {}
_sound_lock = threading.Lock()


def synth_sound(kind):
    """Return (WAV file bytes, length in ms) for 'done' / 'error' (or a style name)."""
    import io
    import wave
    if kind == "done":
        kind = DONE_SOUND_STYLE
    rec = _SOUND_RECIPES.get(kind) or _SOUND_RECIPES["glass"]
    rate = SOUND_RATE
    n = int(rate * rec["total"] / 1000.0)
    buf = [0.0] * n
    two_pi = 2.0 * math.pi
    for start_ms, freq, length_ms, peak, decay_ms, index, harm in rec["notes"]:
        s0, decay = int(rate * start_ms / 1000.0), decay_ms / 1000.0
        for i in range(min(int(rate * length_ms / 1000.0), n - s0)):
            t = i / rate
            env = min(1.0, t / 0.004) * math.exp(-t / decay)
            phase = two_pi * freq * t
            if index:
                phase += index * math.exp(-t / 0.06) * math.sin(3.5 * phase)  # bright attack, pure tail
            v = math.sin(phase) + (harm * math.sin(2.0 * phase) if harm else 0.0)
            buf[s0 + i] += peak * env * v / (1.0 + harm)
    for delay_s, gain in rec["echo"]:
        d = int(rate * delay_s)
        for i in range(d, n):
            buf[i] += gain * buf[i - d]
    if rec["level"]:
        top = max(abs(v) for v in buf) or 1.0
        buf = [v * rec["level"] / top for v in buf]
    fade = int(rate * 0.03)
    for i in range(min(fade, n)):
        buf[n - 1 - i] *= i / float(fade)
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, int(v * 32767)))) for v in buf))
    return bio.getvalue(), rec["total"]


def _sound(kind):
    with _sound_lock:
        if kind not in _sound_cache:
            _sound_cache[kind] = synth_sound(kind)
        return _sound_cache[kind]


def prime_sounds():
    """Build the sounds on a background thread so the chime is instant when a job ends."""
    if sys.platform == "win32":
        threading.Thread(target=lambda: (_sound("done"), _sound("error")), daemon=True).start()


def play_sound(kind):
    """Play 'done' or 'error' without blocking. Returns its length in ms (0 = nothing played).

    winsound can't play from memory asynchronously (SND_MEMORY | SND_ASYNC raises
    RuntimeError), so the in-memory WAV plays on a short-lived daemon thread instead.
    """
    if sys.platform != "win32":
        return 0
    try:
        import winsound
        data, ms = _sound(kind)
        threading.Thread(target=winsound.PlaySound, args=(data, winsound.SND_MEMORY),
                         daemon=True).start()
        return ms
    except Exception:  # noqa: BLE001 - a sound must never break an extraction
        return 0


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
    if mode in ("here", "folder", "free"):
        # "Extract Here" always lands in a new folder named after the archive,
        # next to the archive itself - never dumps loose files into that folder.
        return unique_path(base, stem)
    return unique_path(dest_dir, stem) if subfolder else dest_dir


def extract_archive(path, dest, ctx, motw_source=None):
    """Extract `path` into `dest` (created if needed). Raises on any failure.
    If `motw_source` is given, its download "Mark of the Web" (if any) is
    copied onto every file that lands in `dest` from this extraction.
    Returns what was done: {'dest', 'placed' (relative paths of files newly written),
    'skipped', 'replaced', 'files' (files the reader processed)} - Extract & Free Space
    uses it to decide whether deleting the archive is safe."""
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
        result = {"dest": dest, "placed": placed, "skipped": stats.get("skipped", 0),
                  "replaced": stats.get("replaced", 0), "files": ctx.files}
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
        return result


# --------------------------------------------------------------------------
# Extract & Free Space: extract, then permanently delete the original archive
# --------------------------------------------------------------------------
# The goal is to give disk space back. It is a separate action from ordinary extraction,
# which never deletes anything. The archive is deleted only when EVERY check below passes;
# otherwise it is kept and the reason is shown. It is deleted permanently (not sent to the
# Recycle Bin), because a Recycle Bin copy would not free any space.
def file_signature(path):
    """(size, mtime in ns) of a file, or None if it can't be read."""
    try:
        st = os.stat(lp(path))
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def free_space_blocker(originals, before, ctx, outcome, filtered):
    """None if it is safe to delete `originals` after this extraction, otherwise a short
    reason (shown to the user) why they must be kept.

    originals - the archive file(s) to delete (all parts of a split archive)
    before    - their file_signature() values taken before extraction started
    ctx       - the finished extraction's Ctx
    outcome   - what extract_archive() returned
    filtered  - True if only part of the archive was selected for extraction"""
    if filtered:
        return "only part of the archive was extracted"
    if getattr(ctx, "multivolume", False):
        return "it is part of a multi-part RAR set, and ShredPack can't safely remove every part"
    if not outcome or not outcome.get("dest"):
        return "the extraction result couldn't be confirmed"
    if outcome["skipped"]:
        return "some files already existed and were skipped, so not everything was extracted"
    placed = outcome["placed"]
    if ctx.files == 0 or not placed:
        return "the archive contained no files to verify"
    if len(placed) != ctx.files:
        return "the number of files written doesn't match the number in the archive"
    dest = outcome["dest"]
    wrote = set()
    for rel in placed:
        full = os.path.join(dest, rel)
        if not os.path.isfile(lp(full)):
            return "an extracted file is missing from the destination"
        wrote.add(os.path.normcase(os.path.abspath(full)))
    for orig, sig in zip(originals, before):
        if os.path.normcase(os.path.abspath(orig)) in wrote:
            return "the extraction replaced the archive itself"
        if _is_link_or_reparse(orig):
            return "the archive is a shortcut or link, not a regular file"
        now = file_signature(orig)
        if now is None:
            return "the archive is no longer where it was"
        if sig is None or now != sig:
            return "the archive changed while it was being extracted"
    return None


def delete_permanently(path):
    """Delete one file for good. Returns None on success or a short reason on failure."""
    try:
        os.remove(lp(path))
        return None
    except PermissionError:
        try:                                    # a read-only file: clear the flag and retry once
            os.chmod(lp(path), 0o666)
            os.remove(lp(path))
            return None
        except OSError:
            return "it is read-only or in use by another program"
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc.strerror or "the file couldn't be removed"


def free_space_after_extraction(originals, before, ctx, outcome, filtered):
    """Apply the Extract & Free Space rules. Returns a dict with exactly one of:
    {'freed': bytes}      - the archive(s) were deleted
    {'kept': reason}      - checks didn't pass; nothing was deleted
    {'failed': reason}    - checks passed but deletion failed (extracted files are kept)"""
    reason = free_space_blocker(originals, before, ctx, outcome, filtered)
    if reason:
        return {"kept": reason}
    freed = sum(sig[0] for sig in before if sig)
    for path in originals:
        problem = delete_permanently(path)
        if problem:
            return {"failed": problem}
    return {"freed": freed}


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
    "iso": "ISO image", "cab": "CAB (Windows cabinet)", "xar": "XAR", "rar": "RAR",
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
        if not py7zr:
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
        if not pycdlib:
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

    elif fmt == "rar":
        for e in rar_scan(path, password):
            parts = clean_parts(e["name"])
            if parts and not e["link"]:
                encrypted = encrypted or e["encrypted"]
                entries.append(_entry(parts, None if e["is_dir"] else e["size"],
                                      None if e["is_dir"] else e["packed"], e["mtime"],
                                      e["is_dir"], e["encrypted"]))

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
        return bool(py7zr)
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
        if not pyzipper:
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
    if not py7zr:
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
    """The 'Open with ShredPack' submenu on an archive extension."""
    return r"Software\Classes\SystemFileAssociations\%s\shell\%s" % (ext, SUBMENU_KEY)


def ext_verb_key(ext, name):
    """One of the direct items (Extract files... / Extract Here / Extract to <name>\\)."""
    return r"Software\Classes\SystemFileAssociations\%s\shell\%s" % (ext, name)


def legacy_ext_key(ext):
    """Where v1.0.x - v1.1.x put the single 'ShredPack' cascade on an archive extension."""
    return r"Software\Classes\SystemFileAssociations\%s\shell\%s" % (ext, MENU_KEY)


def compress_class_key(cls):
    """Where the compression-only menu lives for '*' (files) and 'Directory'."""
    return r"Software\Classes\%s\shell\%s" % (cls, MENU_KEY)


def legacy_compress_class_key(cls):
    return r"Software\Classes\%s\shell\%s" % (cls, LEGACY_COMPRESS_MENU_KEY)


def registry_extensions():
    """Extensions that can actually be registered. A name with two dots such as
    '.zip.001' never matches in Explorer (its extension is just '.001'), so it is
    left out when installing - but EXTENSIONS still lists it so any key an older
    version created for it gets cleaned up."""
    return [e for e in EXTENSIONS if e.count(".") == 1]


def archive_applies_to():
    """AppliesTo query that matches every file EXCEPT archives (see
    GENERIC_MENU_EXCLUDES_ARCHIVES)."""
    return "NOT (" + " OR ".join('System.FileExtension:="%s"' % e for e in registry_extensions()) + ")"


class _WinRegistry(object):
    """The only code that touches the real Windows registry (HKEY_CURRENT_USER).
    Tests install an in-memory stand-in with the same methods via set_registry()."""

    def set_values(self, path, values):
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as k:
            for name, value in values.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def get_value(self, path, name=""):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except OSError:
            return None

    def key_exists(self, path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
                return True
        except OSError:
            return False

    def subkeys(self, path):
        names = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                while True:
                    try:
                        names.append(winreg.EnumKey(k, len(names)))
                    except OSError:
                        break
        except OSError:
            pass
        return names

    def delete_key(self, path):
        """Delete ONE key (it must have no subkeys left)."""
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    def notify(self):
        _notify_shell()


_registry_override = None


def registry():
    """The registry in use: None when there isn't one (not Windows)."""
    if _registry_override is not None:
        return _registry_override
    return _WinRegistry() if sys.platform == "win32" else None


def set_registry(reg):
    """Tests only: route all menu registration to a stand-in (None restores normal)."""
    global _registry_override
    _registry_override = reg


def _notify_shell():
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:  # noqa: BLE001
        pass


def _owned_by_shredpack(reg, path):
    """True only if the key at `path` was created by ShredPack (any version), so
    a same-named key belonging to some other program is never touched.
      * the current layout marks every top-level key with OWNER_VALUE;
      * the cascade layouts of v1.0.x - v1.1.x carry MUIVerb = "ShredPack";
      * the very first layout had a default value mentioning ShredPack and a
        command line launching it."""
    if not reg.key_exists(path):
        return False
    if reg.get_value(path, OWNER_VALUE) is not None:
        return True
    if reg.get_value(path, "MUIVerb") in (APP_NAME, SUBMENU_LABEL):
        return True
    default = reg.get_value(path, "") or ""
    command = reg.get_value(path + "\\command", "") or ""
    return APP_NAME.lower() in default.lower() and APP_NAME.lower() in command.lower()


def _delete_tree(reg, path):
    for sub in reg.subkeys(path):
        _delete_tree(reg, path + "\\" + sub)
    reg.delete_key(path)


def _all_menu_paths():
    """Every key path a ShredPack menu could occupy, current and from older versions."""
    paths = []
    for ext in EXTENSIONS:                                   # incl. old '.zip.001'
        paths.append(legacy_ext_key(ext))
        paths.append(ext_key(ext))
        paths.extend(ext_verb_key(ext, name) for name, _l, _m in DIRECT_MENU_ITEMS)
    for cls in COMPRESS_CLASSES:
        paths.append(compress_class_key(cls))
        paths.append(legacy_compress_class_key(cls))
    return paths


def uninstall_menu():
    """Remove every ShredPack menu key - current and from earlier versions - and
    nothing else. Keys of other programs are left alone even if they sit in the
    same place (they're only removed if they're verifiably ours)."""
    reg = registry()
    if reg is None:
        return
    for path in _all_menu_paths():
        if _owned_by_shredpack(reg, path):
            _delete_tree(reg, path)
    reg.notify()


def _current_icon():
    return '"%s",0' % sys.executable if getattr(sys, "frozen", False) else None


def _direct_plan(ext, icon):
    """[(key path, values)] for the three direct items of one archive extension."""
    plan = []
    for name, label, mode in DIRECT_MENU_ITEMS:
        base = ext_verb_key(ext, name)
        values = {"MUIVerb": label, OWNER_VALUE: APP_NAME}
        if icon:
            values["Icon"] = icon
        plan.append((base, values))
        plan.append((base + "\\command", {"": menu_command(mode)}))
    return plan


def _cascade_plan(base, items, icon, applies_to=None):
    """[(key path, {value name: value})] for one 'Open with ShredPack' submenu."""
    top = {"MUIVerb": SUBMENU_LABEL, "SubCommands": "", OWNER_VALUE: APP_NAME}
    if icon:
        top["Icon"] = icon
    if applies_to:
        top["AppliesTo"] = applies_to
    plan = [(base, top)]
    for name, label, mode in items:
        sub = base + "\\shell\\" + name
        plan.append((sub, {"": label}))
        plan.append((sub + "\\command", {"": menu_command(mode)}))
    return plan


def _menu_plan(icon):
    """The complete set of registry keys a correct installation consists of:
    per archive extension the three direct items plus the 'Open with ShredPack'
    submenu, and the compression-only submenu on files ('*', hidden on archives)
    and folders ('Directory')."""
    plan = []
    for ext in registry_extensions():
        plan.extend(_direct_plan(ext, icon))
        plan.extend(_cascade_plan(ext_key(ext), ARCHIVE_SUBMENU_ITEMS, icon))
    for cls in COMPRESS_CLASSES:
        applies = archive_applies_to() if (cls == "*" and GENERIC_MENU_EXCLUDES_ARCHIVES) else None
        plan.extend(_cascade_plan(compress_class_key(cls), COMPRESS_MENU_ITEMS, icon, applies))
    return plan


def _foreign_blockers(reg):
    """Spots ShredPack would install into that another program's key occupies."""
    tops = [p for p, v in _menu_plan(None) if OWNER_VALUE in v]
    return [p for p in tops if reg.key_exists(p) and not _owned_by_shredpack(reg, p)]


def install_menu():
    """(Re)install the right-click menus. Safe to run any number of times: it first
    removes everything of ours - including layouts from older versions - so the
    result is always exactly one ShredPack menu per file type. Returns a list of
    warnings (a spot already occupied by another program's key is skipped, never
    overwritten)."""
    reg = registry()
    if reg is None:
        return []
    uninstall_menu()
    blocked = set(_foreign_blockers(reg))     # still there after uninstall -> not ours
    warnings = ["Skipped %s: it belongs to another program." % p for p in sorted(blocked)]
    for path, values in _menu_plan(_current_icon()):
        if any(path == b or path.startswith(b + "\\") for b in blocked):
            continue
        reg.set_values(path, values)
    reg.notify()
    return warnings


def install_state():
    """'none' (nothing of ours), 'stale' (older layout, changed, or another
    location) or 'current'."""
    reg = registry()
    if reg is None:
        return "none"
    ours = [p for p in _all_menu_paths() if _owned_by_shredpack(reg, p)]
    if not ours:
        return "none"
    legacy = [p for p in ours if p.endswith("\\" + LEGACY_COMPRESS_MENU_KEY)]
    if legacy:
        return "stale"
    blocked = _foreign_blockers(reg)          # another program's key: not ours to judge
    plan = [(p, v) for p, v in _menu_plan(_current_icon())
            if not any(p == b or p.startswith(b + "\\") for b in blocked)]
    for path, values in plan:
        for name, value in values.items():
            actual = reg.get_value(path, name)
            if actual is None or actual.lower() != value.lower():
                return "stale"
    # Anything of ours outside the expected layout (e.g. an old '.zip.001' key) is stale too.
    expected = {p.lower() for p, _v in plan}
    if any(p.lower() not in expected for p in ours):
        return "stale"
    return "current"


# --------------------------------------------------------------------------
# The application window
# --------------------------------------------------------------------------
class ShredPackApp(object):
    def __init__(self, root, archives=None, mode="dialog", from_shell=False, compact=False):
        self.root = root
        self.from_shell = from_shell
        self.archives = []
        self.mode = mode
        self.settings = load_settings()
        # Right-click actions that run straight through get a small, flat progress window that
        # stays hidden unless the job lasts more than a blink.
        self.compact = bool(from_shell and (compact or (
            archives and mode == "here" and self.settings.get("quick_here", True))))
        self._hidden = False
        self._settings_prev = "home"
        self.k = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.tour_active, self.tour_step = False, 0
        self._views = ()
        pick_fonts(root)

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
        self._win_w, self._screen_h = self.px(560), sh
        if self.compact:
            w, h = self.px(440), self.px(128)
        else:
            w = self._win_w
            h = min(self.px(VIEW_HEIGHTS["home"]), sh - self.px(90))
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 3)))

        self._build_styles()
        self._build_ui()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Return>", lambda e: self._on_return())
        root.bind("<Escape>", lambda e: self.on_escape())

        if DND_FILES is not None and hasattr(root, "drop_target_register"):
            self._register_dnd(root)

        if self.compact:
            self._hidden = True
            root.withdraw()

        if archives:
            self.open_archives(archives, mode)
        else:
            self.go_home()

        if not self._hidden:
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
        pad = (self.px(16), self.px(8))
        bold = (FONT_DISPLAY, 10, "bold")
        s.configure("Primary.TButton", background=ACCENT, foreground=ON_ACCENT, borderwidth=0,
                    focusthickness=0, focuscolor=ACCENT, padding=pad, font=bold, relief="flat")
        s.map("Primary.TButton", background=[("pressed", "#3fb4ea"), ("active", "#86daff")])
        s.configure("Secondary.TButton", background=KEY, foreground=TEXT, borderwidth=0,
                    focusthickness=0, focuscolor=KEY, padding=pad, font=bold, relief="flat")
        s.map("Secondary.TButton", background=[("pressed", "#454545"), ("active", KEY_HOVER)])
        s.configure("Danger.TButton", background="#3a2424", foreground=DANGER, borderwidth=0,
                    focusthickness=0, focuscolor="#3a2424", padding=pad, font=bold, relief="flat")
        s.map("Danger.TButton", background=[("pressed", "#5a3030"), ("active", "#4a2b2b")])
        s.configure("Zen.TCheckbutton", background=CARD, foreground=TEXT, font=(FONT, 10),
                    focuscolor=CARD, indicatorbackground=KEY, indicatorforeground=ON_ACCENT,
                    upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=(0, self.px(3)))
        s.map("Zen.TCheckbutton", background=[("active", CARD)], foreground=[("active", TEXT)],
              indicatorbackground=[("selected", ACCENT), ("active", KEY_HOVER)])
        s.configure("Zen.Horizontal.TProgressbar", troughcolor=TROUGH, background=ACCENT,
                    bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, borderwidth=0,
                    thickness=self.px(8))
        s.configure("Mini.Horizontal.TProgressbar", troughcolor=TROUGH, background=ACCENT,
                    bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT, borderwidth=0,
                    thickness=self.px(8))
        s.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                    borderwidth=0, rowheight=self.px(22), font=(FONT, 10))
        s.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", ON_ACCENT)])
        s.configure("Treeview.Heading", background=KEY, foreground=TEXT, borderwidth=0,
                    relief="flat", font=(FONT_MONO, 8, "bold"), padding=(self.px(6), self.px(4)))
        s.map("Treeview.Heading", background=[("active", KEY_HOVER)])
        s.configure("Vertical.TScrollbar", background=KEY, troughcolor=CARD,
                    bordercolor=CARD, arrowcolor=TEXT, relief="flat", borderwidth=0)
        s.configure("TCombobox", fieldbackground=FIELD, background=KEY, foreground=TEXT,
                    arrowcolor=TEXT, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)

    # ---- small building blocks ------------------------------------------------
    def _entry(self, parent, var, **kw):
        e = tk.Entry(parent, textvariable=var, font=(FONT, 10), bg=FIELD, fg=TEXT,
                     insertbackground=TEXT, relief="flat", highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT, **kw)
        return e

    def _card(self, parent):
        """A flat panel with a short blue tab along its top edge."""
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        tab = tk.Frame(card, bg=CARD, height=self.px(3))
        tab.pack(fill="x")
        tk.Frame(tab, bg=ACCENT, width=self.px(56), height=self.px(3)).place(x=0, y=0)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=self.px(20), pady=(self.px(14), self.px(16)))
        return card, inner

    def _label(self, parent, text="", size=10, semi=False, color=MUTED, **kw):
        return tk.Label(parent, text=text, font=(FONT_SEMI if semi else FONT, size),
                        fg=color, bg=CARD, **kw)

    def _heading(self, parent, text, size=18, bg=CARD, **kw):
        return tk.Label(parent, text=text, font=(FONT_DISPLAY, size, "bold"), fg=TEXT, bg=bg, **kw)

    def _tag(self, parent, text, bg=CARD, color=DIM, **kw):
        """Tiny uppercase monospace caption, e.g. '01  DESTINATION'."""
        return tk.Label(parent, text=text.upper(), font=(FONT_MONO, 8), fg=color, bg=bg,
                        anchor="w", **kw)

    def _rule(self, parent, bg=CARD, pady=None):
        line = tk.Frame(parent, bg=BORDER, height=1)
        line.pack(fill="x", pady=pady if pady is not None else self.px(12))
        return line

    def _key(self, parent, tag, title, command, primary=False):
        """A big flat button ('key') with a tiny caption: bold, rectangular, easy to hit."""
        px = self.px
        bg = ACCENT if primary else KEY
        hover = "#86daff" if primary else KEY_HOVER
        fg = ON_ACCENT if primary else TEXT
        sub = ON_ACCENT if primary else DIM
        frame = tk.Frame(parent, bg=bg, cursor="hand2", highlightthickness=1,
                         highlightbackground=ACCENT if primary else BORDER)
        cap = tk.Label(frame, text=tag.upper(), font=(FONT_MONO, 8), fg=sub, bg=bg, anchor="w")
        cap.pack(fill="x", padx=px(12), pady=(px(10), 0))
        name = tk.Label(frame, text=title, font=(FONT_DISPLAY, 15, "bold"), fg=fg, bg=bg, anchor="w")
        name.pack(fill="x", padx=px(12), pady=(px(10), px(12)))
        parts = (frame, cap, name)

        def paint(color):
            for w in parts:
                w.config(bg=color)

        for w in parts:
            w.bind("<Enter>", lambda e, c=hover: paint(c))
            w.bind("<Leave>", lambda e, c=bg: paint(c))
            w.bind("<Button-1>", lambda e: command())
        return frame

    def _chip(self, parent, text, command=None):
        chip = tk.Label(parent, text=text, font=(FONT_MONO, 9, "bold"), fg=MUTED, bg=BG,
                        padx=self.px(7), pady=self.px(2), highlightthickness=1,
                        highlightbackground=BORDER)
        if command:
            chip.config(cursor="hand2")
            chip.bind("<Button-1>", lambda e: command())
            chip.bind("<Enter>", lambda e: chip.config(fg=ACCENT, highlightbackground=ACCENT))
            chip.bind("<Leave>", lambda e: chip.config(fg=MUTED, highlightbackground=BORDER))
        return chip

    # ---- the ShredPack mark: the app icon itself, drawn from an embedded PNG ----------
    def _logo(self, parent, size, bg=BG):
        """The original blue ShredPack icon at roughly `size` pixels (96-dpi units)."""
        want = self.px(size)
        sizes = sorted(LOGO_PNG_B64)
        pick = min(sizes, key=lambda n: (n < want, abs(n - want)))
        try:
            img = tk.PhotoImage(data=LOGO_PNG_B64[pick])
            label = tk.Label(parent, image=img, bg=bg, bd=0)
            label.image = img            # keep a reference or Tk drops the picture
            return label
        except Exception:  # noqa: BLE001 - no PNG support: fall back to a plain blue tile
            tile = tk.Canvas(parent, width=want, height=want, bg=bg, highlightthickness=0)
            rounded_rect(tile, 0, 0, want, want, want // 4, fill=ACCENT_DEEP, outline="")
            return tile

    def _build_ui(self):
        px = self.px
        header = tk.Frame(self.root, bg=BG)
        if not self.compact:
            header.pack(fill="x", padx=px(20), pady=(px(16), px(10)))
        self._logo(header, 46).pack(side="left")
        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", padx=(px(12), 0))
        tk.Label(titles, text=APP_NAME, font=(FONT_DISPLAY, 29, "bold"), fg=TEXT, bg=BG,
                 anchor="w").pack(anchor="w")
        self._tag(titles, "Archive tool  ·  v" + APP_VERSION, bg=BG).pack(anchor="w")
        right = tk.Frame(header, bg=BG)
        right.pack(side="right", anchor="n")
        self.settings_link = self._chip(right, "SETTINGS", self.open_settings)
        self.settings_link.pack(side="right")
        self.update_link = tk.Label(right, text="", font=(FONT_MONO, 9, "bold"), fg=ACCENT, bg=BG,
                                    cursor="hand2")
        self.update_link.pack(side="right", padx=(0, px(10)))
        self.update_link.bind("<Button-1>", lambda e: open_url(RELEASES_URL))
        if not self.compact:
            rule = tk.Frame(self.root, bg=BG, height=px(3))
            rule.pack(fill="x", padx=px(20))
            line = tk.Frame(rule, bg=BORDER, height=1)
            line.place(relx=0.0, rely=1.0, anchor="sw", relwidth=1.0)
            tk.Frame(rule, bg=ACCENT, width=px(64), height=px(3)).place(x=0, y=0)

        body = tk.Frame(self.root, bg=BG)
        if self.compact:
            body.pack(fill="both", expand=True, padx=px(20), pady=px(12))
        else:
            body.pack(fill="both", expand=True, padx=px(20), pady=(px(14), px(16)))

        self._build_home(body)
        self._build_choice(body)
        self._build_compress(body)
        self._build_browse(body)
        self._build_progress(body)
        self._build_done(body)
        self._build_settings(body)
        self._views = (("home", self.view_home), ("choice", self.view_choice),
                       ("compress", self.view_compress), ("browse", self.view_browse),
                       ("progress", self.view_progress), ("done", self.view_done),
                       ("settings", self.view_settings))

    def _build_home(self, body):
        px = self.px
        self.view_home = tk.Frame(body, bg=BG)
        inner = self.view_home

        zw, zh = px(520), px(128)
        zone = tk.Canvas(inner, width=zw, height=zh, bg=BG, highlightthickness=0, cursor="hand2")
        zone.pack()
        rounded_rect(zone, px(2), px(2), zw - px(2), zh - px(2), px(10),
                     fill=CARD, outline="#4a4a4a", width=1, dash=(px(5), px(4)))
        tx, ty, ts = px(26), px(24), px(80)           # the arrow tile
        zone.create_rectangle(tx, ty, tx + ts, ty + ts, fill=ACCENT, outline="")
        cx, cy = tx + ts // 2, ty + ts // 2
        zone.create_line(cx, cy - px(20), cx, cy + px(14), fill=ON_ACCENT, width=px(6), capstyle="butt")
        zone.create_line(cx - px(17), cy - px(2), cx, cy + px(15), cx + px(17), cy - px(2),
                         fill=ON_ACCENT, width=px(6), capstyle="butt", joinstyle="miter")
        zone.create_text(px(128), px(48), anchor="w",
                         text="DROP ARCHIVES HERE" if DND_FILES else "CHOOSE AN ARCHIVE",
                         font=(FONT_DISPLAY, 17, "bold"), fill=TEXT)
        zone.create_text(px(128), px(76), anchor="w", text="or click to browse",
                         font=(FONT, 10), fill=MUTED)
        zone.create_text(px(128), px(98), anchor="w", text="EXTRACT  ·  TEST  ·  BROWSE",
                         font=(FONT_MONO, 8), fill=DIM)
        zone.bind("<Button-1>", lambda e: self.choose_archives())

        keys = tk.Frame(inner, bg=BG)
        keys.pack(fill="x", pady=(px(12), 0))
        for col in range(3):
            keys.columnconfigure(col, weight=1, uniform="keys")
        specs = (("01  open archive", "Extract", self.choose_archives, True),
                 ("02  new archive", "Create", self.choose_compress_entry, False),
                 ("03  verify file", "Checksum", self.choose_checksum, False))
        for col, (tag, title, command, primary) in enumerate(specs):
            self._key(keys, tag, title, command, primary).grid(
                row=0, column=col, sticky="ew", padx=(0 if col == 0 else px(8), 0))

        self._tag(inner, "Reads", bg=BG).pack(anchor="w", pady=(px(14), px(4)))
        chips = tk.Frame(inner, bg=BG)
        chips.pack(fill="x")
        for name in FORMATS[:-1]:
            self._chip(chips, name).pack(side="left", padx=(0, px(5)))
        note = "Creates ZIP, 7Z and TAR.  RAR is extract-only."
        tk.Label(inner, text=note, font=(FONT_MONO, 8), fg=DIM, bg=BG, anchor="w").pack(
            anchor="w", pady=(px(6), 0))

        # First-run tour: a slim strip under the keys, never a blocking window.
        self.tour = tk.Frame(inner, bg=CARD, highlightbackground=ACCENT, highlightthickness=1)
        tour_in = tk.Frame(self.tour, bg=CARD)
        tour_in.pack(fill="x", padx=px(14), pady=px(10))
        top = tk.Frame(tour_in, bg=CARD)
        top.pack(fill="x")
        self.lbl_tour_step = self._tag(top, "Quick tour  1/%d" % len(TOUR_STEPS), color=ACCENT)
        self.lbl_tour_step.pack(side="left")
        skip = tk.Label(top, text="SKIP TOUR", font=(FONT_MONO, 8, "bold"), fg=MUTED, bg=CARD,
                        cursor="hand2")
        skip.pack(side="right")
        skip.bind("<Button-1>", lambda e: self.tour_finish())
        self.lbl_tour_title = tk.Label(tour_in, text="", font=(FONT_DISPLAY, 13, "bold"), fg=TEXT,
                                       bg=CARD, anchor="w")
        self.lbl_tour_title.pack(fill="x", pady=(px(6), 0))
        self.lbl_tour_body = tk.Label(tour_in, text="", font=(FONT, 9), fg=MUTED, bg=CARD,
                                      anchor="w", justify="left", wraplength=px(470))
        self.lbl_tour_body.pack(fill="x", pady=(px(3), px(8)))
        self.btn_tour_next = ttk.Button(tour_in, text="Next", style="Primary.TButton",
                                        command=self.tour_next)
        self.btn_tour_next.pack(anchor="e")

    def _build_choice(self, body):
        px = self.px
        self.view_choice, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Cancel", style="Secondary.TButton",
                   command=self.cancel_choice).pack(side="right")
        ttk.Button(bottom, text="Extract", style="Primary.TButton",
                   command=self.begin).pack(side="right", padx=(0, px(8)))
        self.btn_free = ttk.Button(bottom, text="Extract & Free Space", style="Danger.TButton",
                                   command=self.begin_free)
        self.btn_free.pack(side="right", padx=(0, px(8)))
        self.btn_test = ttk.Button(bottom, text="Test", style="Secondary.TButton",
                                   command=self.test_selected)
        self.btn_test.pack(side="left")
        self.btn_browse = ttk.Button(bottom, text="Browse", style="Secondary.TButton",
                                     command=self.open_browser)
        self.btn_browse.pack(side="left", padx=(px(8), 0))

        self._tag(inner, "Archive").pack(fill="x")
        self.lbl_name = self._heading(inner, "", 17, anchor="w", justify="left", wraplength=px(470))
        self.lbl_name.pack(fill="x", pady=(px(2), 0))
        self.lbl_meta = self._label(inner, "", 10, anchor="w", justify="left", wraplength=px(470))
        self.lbl_meta.pack(fill="x", pady=(px(2), 0))
        self._rule(inner)

        self.lbl_section = self._tag(inner, "")
        self.lbl_section.pack(fill="x")

        # Mode-dependent area (rebuilt in refresh_choice, always sits here).
        self.dest_area = tk.Frame(inner, bg=CARD)
        self.dest_area.pack(fill="x", pady=(px(6), 0))

        self.dest_var = tk.StringVar()
        self.dest_row = tk.Frame(self.dest_area, bg=CARD)
        self.dest_entry = tk.Entry(
            self.dest_row, textvariable=self.dest_var, font=(FONT, 10), bg=FIELD, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.dest_entry.pack(side="left", fill="x", expand=True, ipady=px(6))
        ttk.Button(self.dest_row, text="Browse", style="Secondary.TButton",
                   command=self.pick_folder).pack(side="left", padx=(px(8), 0))

        self.sub_var = tk.BooleanVar(value=False)
        self.sub_chk = ttk.Checkbutton(self.dest_area, text="Create a folder named after the archive",
                                       variable=self.sub_var, style="Zen.TCheckbutton")
        self.lbl_hint = self._label(self.dest_area, "", 10, anchor="w", justify="left",
                                    wraplength=px(470))

        self.lbl_sel = self._label(inner, "", 10, color=ACCENT, anchor="w", justify="left",
                                   wraplength=px(470))

        # "Delete original" and "If a file already exists" live in Settings now; the variables
        # stay (filled from the saved settings) because begin() reads them.
        self.delete_var = tk.BooleanVar(value=False)
        self.overwrite_var = tk.StringVar(value=OVERWRITE_POLICIES[0][1])
        self._rule(inner, pady=(px(12), px(8)))
        opt_row = tk.Frame(inner, bg=CARD)
        opt_row.pack(fill="x")
        self.lbl_opts = self._label(opt_row, "", 9, anchor="w", justify="left", wraplength=px(360))
        self.lbl_opts.pack(side="left")
        opts_link = tk.Label(opt_row, text="CHANGE IN SETTINGS", font=(FONT_MONO, 8, "bold"),
                             fg=ACCENT, bg=CARD, cursor="hand2")
        opts_link.pack(side="right")
        opts_link.bind("<Button-1>", lambda e: self.open_settings())
        flt = tk.Frame(inner, bg=CARD)
        flt.pack(fill="x", pady=(px(10), 0))
        self._tag(flt, "Only extract").grid(row=0, column=0, sticky="w")
        self._tag(flt, "Skip").grid(row=0, column=1, sticky="w", padx=(px(10), 0))
        self.include_var = tk.StringVar()
        self.exclude_var = tk.StringVar()
        self._entry(flt, self.include_var).grid(row=1, column=0, sticky="ew", ipady=px(5))
        self._entry(flt, self.exclude_var).grid(row=1, column=1, sticky="ew", ipady=px(5),
                                                padx=(px(10), 0))
        flt.columnconfigure(0, weight=1)
        flt.columnconfigure(1, weight=1)
        self._label(inner, "Wildcards, e.g. *.jpg, *.pdf  (leave empty to extract everything)", 8,
                    color=DIM, anchor="w").pack(fill="x", pady=(px(3), 0))

    def _build_compress(self, body):
        px = self.px
        self.view_compress, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Cancel", style="Secondary.TButton",
                   command=self.cancel_compress).pack(side="right")
        ttk.Button(bottom, text="Compress", style="Primary.TButton",
                   command=self.begin_compress).pack(side="right", padx=(0, px(8)))

        self._tag(inner, "New archive").pack(fill="x")
        self.lbl_cx_name = self._heading(inner, "", 17, anchor="w", justify="left", wraplength=px(470))
        self.lbl_cx_name.pack(fill="x", pady=(px(2), 0))
        self.lbl_cx_meta = self._label(inner, "", 10, anchor="w", justify="left", wraplength=px(470))
        self.lbl_cx_meta.pack(fill="x", pady=(px(2), 0))
        add_row = tk.Frame(inner, bg=CARD)
        add_row.pack(fill="x", pady=(px(8), 0))
        ttk.Button(add_row, text="Add Files", style="Secondary.TButton",
                   command=self.add_compress_files).pack(side="left")
        ttk.Button(add_row, text="Add Folder", style="Secondary.TButton",
                   command=self.add_compress_folder).pack(side="left", padx=(px(8), 0))

        self._rule(inner)

        fmt_row = tk.Frame(inner, bg=CARD)
        fmt_row.pack(fill="x")
        self._tag(fmt_row, "Format").pack(side="left")
        self.compress_fmt_var = tk.StringVar(value="ZIP")
        self.fmt_combo = ttk.Combobox(
            fmt_row, textvariable=self.compress_fmt_var, state="readonly",
            values=[f[1] for f in COMPRESS_FORMATS if compress_format_available(f[0])], width=16)
        self.fmt_combo.pack(side="right")
        self.fmt_combo.bind("<<ComboboxSelected>>", lambda e: self._on_format_change())

        lvl_row = tk.Frame(inner, bg=CARD)
        lvl_row.pack(fill="x", pady=(px(8), 0))
        self._tag(lvl_row, "Compression level").pack(side="left")
        self.compress_level_var = tk.StringVar(value=COMPRESS_LEVELS[2][1])
        self.level_combo = ttk.Combobox(
            lvl_row, textvariable=self.compress_level_var, state="readonly",
            values=[label for _k, label in COMPRESS_LEVELS], width=22)
        self.level_combo.pack(side="right")

        self._tag(inner, "Archive name").pack(fill="x", pady=(px(12), 0))
        name_row = tk.Frame(inner, bg=CARD)
        name_row.pack(fill="x", pady=(px(4), 0))
        self.compress_name_var = tk.StringVar()
        self.compress_name_var.trace_add("write", lambda *a: self._on_name_edit())
        self.name_entry = tk.Entry(
            name_row, textvariable=self.compress_name_var, font=(FONT, 10), bg=FIELD, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.name_entry.pack(side="left", fill="x", expand=True, ipady=px(6))

        self._tag(inner, "Destination folder").pack(fill="x", pady=(px(12), 0))
        dest_row = tk.Frame(inner, bg=CARD)
        dest_row.pack(fill="x", pady=(px(4), 0))
        self.compress_dest_var = tk.StringVar()
        self.cdest_entry = tk.Entry(
            dest_row, textvariable=self.compress_dest_var, font=(FONT, 10), bg=FIELD, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT)
        self.cdest_entry.pack(side="left", fill="x", expand=True, ipady=px(6))
        ttk.Button(dest_row, text="Browse", style="Secondary.TButton",
                   command=self.pick_compress_folder).pack(side="left", padx=(px(8), 0))

        self.encrypt_var = tk.BooleanVar(value=False)
        self.encrypt_chk = ttk.Checkbutton(
            inner, text="Encrypt with a password (ZIP/7Z only)", variable=self.encrypt_var,
            style="Zen.TCheckbutton", command=self._toggle_password_field)
        self.encrypt_chk.pack(anchor="w", pady=(px(12), 0))

        self.compress_password_var = tk.StringVar()
        self.pw_entry = tk.Entry(
            inner, textvariable=self.compress_password_var, show="*", font=(FONT, 10),
            bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled")
        self.pw_entry.pack(fill="x", pady=(px(6), 0), ipady=px(6))

    def _build_browse(self, body):
        px = self.px
        self.view_browse, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x", pady=(px(10), 0))
        ttk.Button(bottom, text="Back", style="Secondary.TButton",
                   command=self.browse_back).pack(side="left")
        ttk.Button(bottom, text="Extract All", style="Secondary.TButton",
                   command=lambda: self.browse_extract(False)).pack(side="right")
        ttk.Button(bottom, text="Extract Selected", style="Primary.TButton",
                   command=lambda: self.browse_extract(True)).pack(side="right", padx=(0, px(8)))

        self._tag(inner, "Contents").pack(fill="x")
        self.lbl_b_name = self._heading(inner, "", 16, anchor="w", justify="left", wraplength=px(480))
        self.lbl_b_name.pack(fill="x", pady=(px(2), 0))
        self.lbl_b_info = self._label(inner, "", 9, anchor="w", justify="left", wraplength=px(480))
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
        self.tree.heading("#0", text="NAME", anchor="w")
        self.tree.heading("size", text="SIZE", anchor="e")
        self.tree.heading("packed", text="PACKED", anchor="e")
        self.tree.heading("modified", text="MODIFIED", anchor="w")
        self.tree.column("#0", width=px(230), stretch=True)
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
        self.view_progress = tk.Frame(body, bg=BG)
        inner = tk.Frame(self.view_progress, bg=BG)
        inner.pack(fill="both", expand=True, padx=px(4), pady=px(6))
        top = tk.Frame(inner, bg=BG)
        top.pack(fill="x", pady=(px(4), 0))
        self.lbl_p_pct = tk.Label(top, text="", font=(FONT_DISPLAY, 18 if self.compact else 26, "bold"),
                                  fg=ACCENT, bg=BG)
        self.lbl_p_pct.pack(side="right", padx=(px(10), 0))
        self.lbl_p_title = tk.Label(top, text="Preparing\u2026", font=(FONT_DISPLAY, 13, "bold"),
                                    fg=TEXT, bg=BG, anchor="w", justify="left", wraplength=px(340))
        self.lbl_p_title.pack(side="left", fill="x", expand=True)
        self.progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(inner, style="Mini.Horizontal.TProgressbar", orient="horizontal",
                        mode="determinate", maximum=100, variable=self.progress_var
                        ).pack(fill="x", pady=(px(12), 0))
        foot = tk.Frame(inner, bg=BG)
        foot.pack(fill="x", pady=(px(10), 0))
        self.lbl_p_sub = tk.Label(foot, text="", font=(FONT_MONO, 9), fg=MUTED, bg=BG, anchor="w",
                                  justify="left", wraplength=px(340))
        self.lbl_p_sub.pack(side="left")
        cancel = tk.Label(foot, text="CANCEL", font=(FONT_MONO, 9, "bold"), fg=MUTED, bg=BG,
                          cursor="hand2")
        cancel.pack(side="right")
        cancel.bind("<Button-1>", lambda e: self.on_cancel())
        cancel.bind("<Enter>", lambda e: cancel.config(fg=DANGER))
        cancel.bind("<Leave>", lambda e: cancel.config(fg=MUTED))

    def _build_done(self, body):
        px = self.px
        self.view_done = tk.Frame(body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        box = tk.Frame(self.view_done, bg=CARD)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tile = tk.Label(box, text="\u2713", font=(FONT_DISPLAY, 30, "bold"), fg=BG, bg=GREEN,
                        width=3)
        tile.pack()
        self.lbl_d_title = self._heading(box, "Extraction complete", 18)
        self.lbl_d_title.pack(pady=(px(10), 0))
        self.lbl_d_sub = self._label(box, "", 10, justify="center", wraplength=px(440))
        self.lbl_d_sub.pack(pady=(px(6), 0))

    def _build_settings(self, body):
        px = self.px
        self.view_settings, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Done", style="Primary.TButton",
                   command=self.close_settings).pack(side="right")
        replay = tk.Label(bottom, text="SHOW THE QUICK TOUR AGAIN", font=(FONT_MONO, 8, "bold"),
                          fg=ACCENT, bg=CARD, cursor="hand2")
        replay.pack(side="left", pady=(px(8), 0))
        replay.bind("<Button-1>", lambda e: self.tour_restart())
        self._tag(inner, "Preferences").pack(fill="x")
        self._heading(inner, "Settings", 20, anchor="w").pack(fill="x", pady=(px(2), 0))
        self._label(inner, "Saved automatically. They apply to every right-click action.", 9,
                    anchor="w").pack(fill="x", pady=(px(2), 0))
        self._rule(inner)

        self.set_quick_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text="Extract Here starts immediately (no confirmation screen)",
                        variable=self.set_quick_var, style="Zen.TCheckbutton",
                        command=self._settings_changed).pack(anchor="w")
        self.set_delete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(inner, text="Delete the original archive after a successful extraction",
                        variable=self.set_delete_var, style="Zen.TCheckbutton",
                        command=self._settings_changed).pack(anchor="w", pady=(px(4), 0))
        self.set_sound_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text="Play a short sound when tasks finish",
                        variable=self.set_sound_var, style="Zen.TCheckbutton",
                        command=self._settings_changed).pack(anchor="w", pady=(px(4), 0))

        ow_row = tk.Frame(inner, bg=CARD)
        ow_row.pack(fill="x", pady=(px(14), 0))
        self._tag(ow_row, "If a file already exists").pack(side="left")
        self.set_overwrite_var = tk.StringVar(value=OVERWRITE_POLICIES[0][1])
        combo = ttk.Combobox(ow_row, textvariable=self.set_overwrite_var, state="readonly",
                             values=[label for _k, label in OVERWRITE_POLICIES], width=34)
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", lambda e: self._settings_changed())

        self._rule(inner, pady=(px(16), px(10)))
        self._tag(inner, "Extract & Free Space", color=DANGER).pack(fill="x")
        self._label(
            inner,
            "A separate action (choice screen, or right-click > Open with ShredPack). It extracts "
            "the whole archive, then deletes the original permanently - never to the Recycle Bin "
            "- and only after the extraction checks out. It always asks first, and keeps the "
            "archive if anything looks wrong.", 9, anchor="w", justify="left",
            wraplength=px(470)).pack(fill="x", pady=(px(4), 0))

    def _register_dnd(self, widget):
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:  # noqa: BLE001
            pass
        for child in widget.winfo_children():
            self._register_dnd(child)

    def show(self, view):
        for _name, v in self._views:
            v.pack_forget()
        view.pack(fill="both", expand=True)
        self._fit(view)

    def _fit(self, view):
        """Make the window exactly as tall as the screen being shown (the small right-click
        progress window keeps its own fixed size)."""
        if self.compact:
            return
        name = next((n for n, v in self._views if v is view), "home")
        height = VIEW_HEIGHTS.get(name, 560) + (TOUR_EXTRA_HEIGHT if name == "home" and self.tour_active else 0)
        height = min(self.px(height), self._screen_h - self.px(90))
        try:
            self.root.geometry("%dx%d" % (self._win_w, height))
        except tk.TclError:
            pass

    # ---- settings -----------------------------------------------------------
    def _apply_settings_to_vars(self):
        self.delete_var.set(bool(self.settings.get("delete_after", False)))
        self.overwrite_var.set(dict(OVERWRITE_POLICIES).get(
            self.settings.get("overwrite", "rename"), OVERWRITE_POLICIES[0][1]))

    def _opts_summary(self):
        label = self.overwrite_var.get().split(" (")[0].split(" - ")[0]
        return "If a file exists: %s  \u2022  Delete original: %s" % (
            label, "yes" if self.delete_var.get() else "no")

    def open_settings(self):
        if self.state not in ("home", "choice"):
            return
        self._settings_prev = self.state
        self.state = "settings"
        self.set_quick_var.set(bool(self.settings.get("quick_here", True)))
        self.set_delete_var.set(bool(self.settings.get("delete_after", False)))
        self.set_sound_var.set(bool(self.settings.get("sound", True)))
        self.set_overwrite_var.set(dict(OVERWRITE_POLICIES).get(
            self.settings.get("overwrite", "rename"), OVERWRITE_POLICIES[0][1]))
        self.show(self.view_settings)

    def _settings_changed(self):
        label = self.set_overwrite_var.get()
        self.settings.update({
            "quick_here": bool(self.set_quick_var.get()),
            "delete_after": bool(self.set_delete_var.get()),
            "sound": bool(self.set_sound_var.get()),
            "overwrite": next((k for k, lab in OVERWRITE_POLICIES if lab == label), "rename"),
        })
        save_settings(self.settings)

    def close_settings(self):
        if self.state != "settings":
            return
        self._settings_changed()
        if self._settings_prev == "choice" and self.archives:
            self.state = "choice"
            self._apply_settings_to_vars()
            self.refresh_choice()
            self.show(self.view_choice)
        else:
            self.go_home()

    # ---- quiet, quick endings ------------------------------------------------
    def _chime(self, kind="done"):
        if not self.settings.get("sound", True):
            return 0
        return play_sound(kind)

    def _reveal(self):
        """Show the window if a compact run kept it hidden (long job, error, or a question)."""
        if not self._hidden:
            return
        self._hidden = False
        try:
            self.root.deiconify()
            enable_dark_titlebar(self.root)
            self.root.attributes("-topmost", True)
            self.root.after(400, lambda: self.root.attributes("-topmost", False))
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _arm_reveal(self):
        if self._hidden:
            self.root.after(450, self._reveal_if_busy)

    def _reveal_if_busy(self):
        if self.state == "processing":
            self._reveal()

    def _close_soon(self, ms=0):
        """Vanish at once; the process only lingers long enough for the chime to finish."""
        try:
            self.root.withdraw()
        except tk.TclError:
            pass
        self.root.after(max(int(ms), 1), self.root.destroy)

    def _finish_ok(self, title, lines, notes):
        """Shared success ending. Nothing to read -> the window closes immediately; otherwise
        the details stay up briefly."""
        sound_ms = self._chime("done")
        if self.from_shell and not notes:
            self._close_soon(sound_ms)
            return
        if self.compact:                    # small window: reuse the progress view
            self._reveal()
            self.lbl_p_pct.config(text="")
            self.lbl_p_title.config(text="\u2713  " + title)
            self.lbl_p_sub.config(text="  \u2022  ".join(lines))
            self.progress_var.set(100)
            self.show(self.view_progress)
        else:
            self.lbl_d_title.config(text=title)
            self.lbl_d_sub.config(text="\n".join(lines))
            self.show(self.view_done)
        delay = 900 if not notes else 3000
        if self.from_shell:
            self.root.after(max(delay, sound_ms), self.root.destroy)
        else:
            self.root.after(delay, self.go_home)

    def _quick_aborted(self):
        """An instant Extract Here was declined (or couldn't start)."""
        if self.from_shell:
            self.root.destroy()
        else:
            self.refresh_choice()
            self.show(self.view_choice)

    # ---- home ---------------------------------------------------------------
    def go_home(self):
        self.state = "home"
        self.action = "extract"
        self.archives = []
        self.selection = None
        self.initial_password = None
        self.lbl_p_pct.config(text="")
        self._tour_sync()
        self.show(self.view_home)

    # ---- first-run tour -------------------------------------------------------
    def _tour_sync(self):
        """Show the slim tour strip on the home screen until it has been finished or skipped."""
        want = (not self.from_shell) and (not self.settings.get("tutorial_done", False))
        if want and not self.tour_active:
            self.tour_active, self.tour_step = True, 0
        elif not want:
            self.tour_active = False
        if self.tour_active:
            self._tour_render()
            self.tour.pack(fill="x", pady=(self.px(14), 0))
        else:
            self.tour.pack_forget()

    def _tour_render(self):
        title, text = TOUR_STEPS[self.tour_step]
        self.lbl_tour_step.config(text=("Quick tour  %d/%d" % (self.tour_step + 1, len(TOUR_STEPS))).upper())
        self.lbl_tour_title.config(text=title)
        self.lbl_tour_body.config(text=text)
        self.btn_tour_next.config(text="Done" if self.tour_step + 1 >= len(TOUR_STEPS) else "Next")

    def tour_next(self):
        if self.tour_step + 1 >= len(TOUR_STEPS):
            self.tour_finish()
            return
        self.tour_step += 1
        self._tour_render()

    def tour_finish(self):
        """Finished or skipped: remember it so the tour never comes back on its own."""
        self.tour_active = False
        self.settings["tutorial_done"] = True
        save_settings(self.settings)
        self.tour.pack_forget()
        if self.state == "home":
            self._fit(self.view_home)

    def tour_restart(self):
        """Settings > 'show the quick tour again'."""
        self.settings["tutorial_done"] = False
        self.tour_active = False
        self.close_settings()

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
        self._apply_settings_to_vars()
        self.include_var.set("")
        self.exclude_var.set("")
        self.state = "choice"
        self.refresh_choice()
        if mode == "free":
            # Right-click > Open with ShredPack > Extract & Free Space: always confirm first.
            self.show(self.view_choice)
            if self._confirm_free_space(self.archives):
                self.begin(free_space=True)
            if self.state != "processing":
                self._quick_aborted()
            return
        if mode == "here" and self.settings.get("quick_here", True):
            # Extract Here does what it says: no confirmation screen, the saved settings apply.
            self.begin()
            if self.state != "processing":
                self._quick_aborted()
            return
        self.show(self.view_choice)

    def refresh_choice(self):
        px = self.px
        self.lbl_opts.config(text=self._opts_summary())
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
            self.lbl_section.config(text="DESTINATION FOLDER")
            self.dest_row.pack(fill="x")
            self.sub_chk.pack(anchor="w", pady=(px(8), 0))
        else:
            self.lbl_section.config(
                text="EXTRACT & FREE SPACE" if self.mode == "free" else "EXTRACT HERE")
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
        self._reveal()
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
        if self.settings.get("sound", True):
            prime_sounds()
        self._arm_reveal()
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
            if self.compact:
                self.root.destroy()
                return
            self.state = "compress_choice"
            self.show(self.view_compress)
            return
        if result["status"] == "error":
            self._reveal()
            self._chime("error")
            messagebox.showerror("%s - Compression failed" % APP_NAME, result["detail"],
                                 parent=self.root)
            if self.compact:
                self.root.destroy()
                return
            self.state = "compress_choice"
            self.show(self.view_compress)
            return
        self.state = "finished"
        self._finish_ok("Compression complete",
                        [os.path.basename(result["dest"])] + result["notes"], result["notes"])

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
                    source = join_split_parts(
                        parts, join_dir,
                        Ctx(self.cancel, lambda p, d, t: self.q.put(("progress", p, d, t))))
                password = initial_password
                while True:
                    ctx = Ctx(self.cancel, lambda p, d, t: self.q.put(("progress", p, d, t)),
                              password)
                    try:
                        test_archive(source, ctx)
                        res["files"] = ctx.files
                        res["notes"] = list(ctx.notes)
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
                self._chime("done")
                text = "No errors found.\n\n%d file%s verified in %d archive%s." % (
                    n, "" if n == 1 else "s", len(good), "" if len(good) == 1 else "s")
                notes = [note for r in good for note in r.get("notes", [])]
                if notes:
                    text += "\n\n" + "\n".join(notes)
                messagebox.showinfo(APP_NAME, text, parent=self.root)
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
    def begin_free(self):
        """The 'Extract & Free Space' button: confirm, then extract and delete the archive."""
        if self.state != "choice":
            return
        if self.selection or parse_patterns(self.include_var.get()) or parse_patterns(self.exclude_var.get()):
            messagebox.showinfo(
                APP_NAME,
                "Extract & Free Space needs the whole archive, because the original is deleted "
                "afterwards.\n\nClear the selection and the 'Only extract' / 'Skip' filters, "
                "or use the normal Extract button.", parent=self.root)
            return
        if self._confirm_free_space(self.archives):
            self.begin(free_space=True)

    def _confirm_free_space(self, archives):
        """The explicit warning that must be accepted before anything is deleted."""
        self._reveal()
        names = "\n".join("   " + shorten(os.path.basename(a), 50) for a in archives[:4])
        if len(archives) > 4:
            names += "\n   ...and %d more" % (len(archives) - 4)
        multi = len(archives) > 1
        parts = any(detect_split_parts(a) for a in archives)
        text = (
            "Extract & Free Space\n\n"
            "ShredPack will extract:\n%s\n\n"
            "and then PERMANENTLY DELETE the original archive%s%s. It is not sent to the "
            "Recycle Bin, so it can't be restored.\n\n"
            "An archive is deleted only after all of its files were extracted and checked. If "
            "anything fails, or you cancel, it is kept.\n\nContinue?"
            % (names, "s" if multi else "", " (every part of a split archive)" if parts else ""))
        return messagebox.askyesno(APP_NAME, text, parent=self.root, icon="warning", default="no")

    def begin(self, free_space=False):
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
        delete = self.delete_var.get() and not free_space

        policy_label = self.overwrite_var.get()
        opts = {
            "selected": set(self.selection) if self.selection else None,
            "include": parse_patterns(self.include_var.get()),
            "exclude": parse_patterns(self.exclude_var.get()),
            "overwrite": next((k for k, label in OVERWRITE_POLICIES if label == policy_label),
                              "rename"),
            "password": self.initial_password,
            "free_space": bool(free_space),
        }
        filtered = bool(opts["selected"] or opts["include"] or opts["exclude"])
        if free_space and filtered:
            return        # begin_free() already explained this; never delete after a partial extraction
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
        if self.settings.get("sound", True):
            prime_sounds()
        self._arm_reveal()
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
                self._reveal()
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
        elif self.state == "settings":
            self.close_settings()
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
                originals = parts if parts else [arc]
                before = [file_signature(p) for p in originals]   # to notice a change mid-extraction
                source = arc
                if parts:
                    join_dir = tempfile.mkdtemp(prefix=".shredpack_join_")
                    source = join_split_parts(
                        parts, join_dir,
                        Ctx(self.cancel, lambda p, d, t: self.q.put(("progress", p, d, t))))

                password = opts.get("password")
                while True:
                    ctx = Ctx(self.cancel,
                              lambda p, d, t: self.q.put(("progress", p, d, t)), password,
                              selected=opts.get("selected"), include=opts.get("include"),
                              exclude=opts.get("exclude"),
                              overwrite=opts.get("overwrite", "rename"))
                    try:
                        outcome = extract_archive(source, dest, ctx, motw_source=arc)
                        res["notes"] = list(ctx.notes)
                        break
                    except NeedPassword as need:
                        password = self._ask_password(name, need.retry)
                        if password is None:
                            self.cancel.set()
                            raise Cancelled()

                res["status"] = "ok"
                if opts.get("free_space"):
                    # Extract & Free Space: delete only if every safety check passes.
                    if self.cancel.is_set():
                        raise Cancelled()
                    filtered = bool(opts.get("selected") or opts.get("include") or opts.get("exclude"))
                    res.update(free_space_after_extraction(originals, before, ctx, outcome, filtered))
                elif delete:
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
            self.root.after(40, self._poll)

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
        self._reveal()
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
            self._reveal()
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
        overall = ((self.cur_idx - 1) + pct / 100.0) / self.cur_n * 100.0
        self.progress_var.set(overall)
        self.lbl_p_pct.config(text="%d%%" % int(overall))
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
            self._reveal()
            self._chime("error")
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
        freed = [r for r in oks if "freed" in r]
        if freed:
            total = sum(r["freed"] for r in freed)
            notes.append("%s deleted permanently - %s freed." % (
                "Original archive" if len(freed) == 1 else "%d original archives" % len(freed),
                fmt_size(total)))
        problems = []
        for r in oks:
            if "kept" in r:
                problems.append("%s\nThe archive was kept because %s. Nothing was deleted."
                                % (r["name"], r["kept"]))
            elif "failed" in r:
                problems.append("%s\nThe files were extracted, but the original archive couldn't "
                                "be deleted: %s. The extracted files were kept." % (r["name"], r["failed"]))
        if problems:
            self._reveal()
            messagebox.showwarning("%s - Extract & Free Space" % APP_NAME, "\n\n".join(problems),
                                   parent=self.root)
        for r in oks:
            notes.extend(r.get("notes", []))
        self._finish_ok("Extraction complete", notes, notes)

    def _back_to_choice(self, results):
        finished = {r["path"] for r in results if r["status"] == "ok"}
        self.archives = [a for a in self.archives if a not in finished]
        if not self.archives or self.compact:
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
            valid = ("dialog", "here", "folder", "free", "test", "compress-quick", "compress-dialog")
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
        app = ShredPackApp(root, from_shell=True,
                           compact=(mode == "compress-quick" and len(existing) == 1))
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

    # Opened directly by the user: offer once to add the right-click menu, then show the app.
    # (The installer registers it itself; this covers the portable exe and menus that point at
    # another copy of ShredPack. Saying no is remembered, so it never nags.)
    root = make_root(True)
    if sys.platform == "win32":
        state = install_state()
        settings = load_settings()
        if state == "stale" or (state == "none" and not settings.get("menu_declined")):
            root.withdraw()
            question = (
                "The right-click options are out of date or point to a different copy of ShredPack.\n\n"
                "Update them to use this copy?"
                if state == "stale" else
                'Add ShredPack to the Windows right-click menu?\n\n'
                "On archives: Extract files..., Extract Here and Extract to <archive name>, plus an "
                '"%s" submenu (Extract & Free Space, Test archive, Add to ZIP, Add to archive...).\n'
                'On other files and folders: "%s" > Add to ZIP / Add to archive...\n\n'
                "This only changes settings for your user account and needs no administrator rights."
                % (SUBMENU_LABEL, SUBMENU_LABEL)
            )
            if messagebox.askyesno(APP_NAME, question, parent=root):
                try:
                    install_menu()
                    messagebox.showinfo(
                        APP_NAME,
                        "Done! Right-click an archive to extract it, or right-click any file or "
                        'folder and choose "%s" to compress it.\n\n'
                        'On Windows 11 the options appear under "Show more options".' % SUBMENU_LABEL,
                        parent=root)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, "Couldn't update the registry:\n\n%s" % exc, parent=root)
            elif state == "none":
                settings["menu_declined"] = True
                save_settings(settings)
            root.deiconify()
    ShredPackApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
