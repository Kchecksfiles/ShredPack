"""
ShredPack - a minimalist, drag-and-drop ZIP extractor for Windows.

Dependencies:  pip install tkinterdnd2 send2trash
"""

import os
import re
import sys
import time
import queue
import shutil
import tempfile
import threading
import subprocess
import zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        "ShredPack",
        "Missing dependency: tkinterdnd2\n\nInstall it with:\n    pip install tkinterdnd2 send2trash",
    )
    sys.exit(1)

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------
APP_NAME = "ShredPack"
FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"

BG = "#1f1f1f"
CARD = "#2b2b2b"
CARD_HOVER = "#2e3338"
BORDER = "#3c3c3c"
TEXT = "#f3f3f3"
MUTED = "#a0a0a0"
SUBTLE = "#7c7c7c"
ACCENT = "#60cdff"
ICON_BG = "#343434"
TROUGH = "#3d3d3d"
GREEN = "#6ccb5f"
GREEN_BG = "#22392a"
RED = "#ff7b7b"
RED_BG = "#3d2626"

INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {"COM%d" % i for i in range(1, 10)}
    | {"LPT%d" % i for i in range(1, 10)}
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class ZenError(Exception):
    """A user-presentable extraction error."""


def lerp_color(a, b, t):
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#%02x%02x%02x" % (
        round(ar + (br - ar) * t),
        round(ag + (bg - ag) * t),
        round(ab + (bb - ab) * t),
    )


def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


def fmt_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%d %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % n


def shorten(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def clean_parts(name):
    """Split an archive member name into safe path components."""
    parts = []
    for p in name.replace("\\", "/").split("/"):
        if p in ("", ".", ".."):
            continue
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


def is_junk(filename):
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    return (not parts) or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store"


def is_dir_entry(info):
    return info.is_dir() or info.filename.replace("\\", "/").endswith("/")


def detect_single_root(infos):
    """Return the name of the single top-level folder, or None if loose."""
    tops = {}
    for info in infos:
        parts = clean_parts(info.filename)
        if not parts:
            continue
        folder_like = len(parts) > 1 or is_dir_entry(info)
        tops[parts[0]] = tops.get(parts[0], False) or folder_like
    if len(tops) == 1:
        name, is_folder = next(iter(tops.items()))
        return name if is_folder else None
    return None


def unique_path(parent, name):
    candidate = os.path.join(parent, name)
    i = 2
    while os.path.exists(candidate):
        candidate = os.path.join(parent, "%s (%d)" % (name, i))
        i += 1
    return candidate


def lp(path):
    """Use extended-length paths on Windows for very long paths."""
    if sys.platform != "win32":
        return path
    p = os.path.abspath(path)
    if len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def stamp_time(path, info):
    try:
        ts = time.mktime(tuple(info.date_time) + (0, 0, -1))
        os.utime(lp(path), (ts, ts))
    except (OverflowError, ValueError, OSError):
        pass


def reveal(path):
    try:
        if sys.platform == "win32":
            os.startfile(os.path.normpath(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def recycle(path):
    """Send a file to the Recycle Bin; fall back to a clean delete."""
    target = os.path.abspath(path)
    if send2trash is not None:
        try:
            send2trash(target)
            return True
        except Exception:
            pass
    try:
        os.remove(target)
        return True
    except OSError:
        return False


def enable_dark_titlebar(root):
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old id)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)
            ) == 0:
                break
        caption = ctypes.c_int(0x001F1F1F)  # DWMWA_CAPTION_COLOR (Windows 11)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption)
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# Extraction engine
# --------------------------------------------------------------------------
def extract_archive(zip_path, progress_cb):
    """
    Smart-extract a zip next to itself and return the final folder path.

    - Single top-level folder in the archive -> that folder lands beside the zip.
    - Loose files -> a new folder named after the zip is created for them.
    Extraction happens in a temporary sibling folder first, so failures never
    leave partial output and existing folders are never overwritten.
    """
    zip_path = os.path.abspath(zip_path)
    parent = os.path.dirname(zip_path)
    stem = sanitize_name(os.path.splitext(os.path.basename(zip_path))[0]) or "Extracted"
    last_emit = [0.0]

    def report(done, total, force=False):
        now = time.monotonic()
        if force or now - last_emit[0] >= 0.05:
            last_emit[0] = now
            progress_cb(min(100.0, done * 100.0 / total), min(done, total), total)

    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not is_junk(i.filename)]
        if not infos:
            raise ZenError("The archive is empty.")
        if any(i.flag_bits & 0x1 for i in infos):
            raise ZenError("This archive is password-protected.")

        root = detect_single_root(infos)
        total = max(1, sum(i.file_size for i in infos))
        tmp = tempfile.mkdtemp(prefix=".shredpack_", dir=parent)

        try:
            done = 0
            report(0, total, True)
            for info in infos:
                parts = clean_parts(info.filename)
                if not parts:
                    continue
                target = os.path.join(tmp, *parts)
                if is_dir_entry(info):
                    os.makedirs(lp(target), exist_ok=True)
                    continue
                os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
                with zf.open(info) as src, open(lp(target), "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        done += len(chunk)
                        report(done, total)
                stamp_time(target, info)
            report(total, total, True)

            single = (
                root is not None
                and os.listdir(lp(tmp)) == [root]
                and os.path.isdir(lp(os.path.join(tmp, root)))
            )
            if single:
                dest = unique_path(parent, root)
                os.rename(lp(os.path.join(tmp, root)), lp(dest))
                try:
                    os.rmdir(lp(tmp))
                except OSError:
                    shutil.rmtree(lp(tmp), ignore_errors=True)
            else:
                dest = unique_path(parent, stem)
                os.rename(lp(tmp), lp(dest))
        except BaseException:
            shutil.rmtree(lp(tmp), ignore_errors=True)
            raise
    return dest


def describe_error(exc):
    if isinstance(exc, ZenError):
        return str(exc)
    if isinstance(exc, zipfile.BadZipFile):
        return "The archive looks corrupted or incomplete."
    if isinstance(exc, NotImplementedError):
        return "This archive uses a compression method ShredPack can't read."
    if isinstance(exc, RuntimeError) and "password" in str(exc).lower():
        return "This archive is password-protected."
    if isinstance(exc, PermissionError):
        return "Permission denied. Move the archive to a folder you can write to."
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return str(exc) or exc.__class__.__name__


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class ShredPackApp:
    def __init__(self, root):
        self.root = root
        self.k = max(1.0, root.winfo_fpixels("1i") / 96.0)

        self.state = "idle"  # idle | processing | success | error
        self.hover = False
        self.pct = 0.0
        self.cur_name = ""
        self.cur_idx = 1
        self.cur_count = 1
        self.cur_done = 0
        self.cur_total = 0
        self.result_title = ""
        self.result_sub = ""
        self.fade_entries = []
        self.spin_angle = 90
        self._reset_job = None
        self._fade_job = None
        self._spin_job = None
        self._poll_job = None
        self.q = queue.Queue()

        root.title(APP_NAME)
        root.configure(bg=BG)
        w, h = self.px(780), self.px(520)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 2))
        root.minsize(self.px(560), self.px(400))

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Zen.Horizontal.TProgressbar",
            troughcolor=TROUGH,
            background=ACCENT,
            bordercolor=CARD,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            borderwidth=0,
            thickness=self.px(6),
        )

        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.pct_var = tk.DoubleVar(value=0.0)
        self.bar = ttk.Progressbar(
            self.canvas,
            style="Zen.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=self.pct_var,
        )

        for widget in (root, self.canvas):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)

        self.canvas.bind("<Configure>", lambda e: self.render())
        self.canvas.bind("<Button-1>", self._on_click)
        root.bind("<Control-o>", lambda e: self._browse())

        enable_dark_titlebar(root)
        self.render()

    # ---- geometry helper --------------------------------------------------
    def px(self, v):
        return int(round(v * self.k))

    # ---- drag & drop ------------------------------------------------------
    def _on_drop_enter(self, event):
        if self.state != "processing":
            self.hover = True
            self.render()
        return getattr(event, "action", "copy")

    def _on_drop_leave(self, event):
        self.hover = False
        self.render()
        return getattr(event, "action", "copy")

    def _on_drop(self, event):
        self.hover = False
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except tk.TclError:
            paths = []
        self.start(paths)
        return getattr(event, "action", "copy")

    def _on_click(self, event):
        if self.state == "idle":
            self._browse()

    def _browse(self):
        if self.state == "processing":
            return
        files = filedialog.askopenfilenames(
            title="Choose ZIP archive(s)",
            filetypes=[("ZIP archives", "*.zip"), ("All files", "*.*")],
        )
        if files:
            self.start(list(files))

    # ---- job control ------------------------------------------------------
    def _cancel_timers(self):
        for attr in ("_reset_job", "_fade_job", "_spin_job", "_poll_job"):
            job = getattr(self, attr)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def start(self, raw_paths):
        if self.state == "processing":
            return
        paths = []
        for p in raw_paths:
            p = os.path.abspath(p)
            if os.path.isfile(p) and p.lower().endswith(".zip") and zipfile.is_zipfile(p):
                paths.append(p)
        paths = list(dict.fromkeys(paths))
        if not paths:
            self._cancel_timers()
            self.show_result(
                "error",
                "Not a ZIP archive",
                "ShredPack can unpack .zip files. Drop one here to get started.",
            )
            return

        self._cancel_timers()
        self.state = "processing"
        self.pct = 0.0
        self.cur_name = os.path.basename(paths[0])
        self.cur_idx, self.cur_count = 1, len(paths)
        self.cur_done = self.cur_total = 0
        self.render()

        threading.Thread(target=self._worker, args=(paths,), daemon=True).start()
        self._poll_job = self.root.after(30, self._poll)
        self._spin_job = self.root.after(25, self._spin)

    def _worker(self, paths):
        results = []
        for idx, path in enumerate(paths, 1):
            name = os.path.basename(path)
            self.q.put(("file", idx, len(paths), name))
            try:
                dest = extract_archive(
                    path, lambda pct, d, t: self.q.put(("progress", pct, d, t))
                )
                reveal(dest)
                recycled = recycle(path)
                results.append({"name": name, "dest": dest, "error": None, "recycled": recycled})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "dest": None, "error": describe_error(exc), "recycled": False})
        self.q.put(("finished", results))

    def _poll(self):
        self._poll_job = None
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.state == "processing":
            self._poll_job = self.root.after(30, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "file":
            _, idx, count, name = msg
            self.cur_idx, self.cur_count, self.cur_name = idx, count, name
            self.pct, self.cur_done, self.cur_total = 0.0, 0, 0
            self._refresh_progress()
        elif kind == "progress":
            _, pct, done, total = msg
            self.pct, self.cur_done, self.cur_total = pct, done, total
            self._refresh_progress()
        elif kind == "finished":
            self._finish(msg[1])

    def _finish(self, results):
        ok = [r for r in results if not r["error"]]
        bad = [r for r in results if r["error"]]
        if not ok:
            first = bad[0]
            if len(results) == 1:
                text = first["error"]
            else:
                text = "%s: %s" % (first["name"], first["error"])
            if len(bad) > 1:
                text += "\n(+%d more failed)" % (len(bad) - 1)
            self.show_result("error", "Couldn't unzip", text)
            return

        title = "Extracted" if len(ok) == 1 else "%d archives extracted" % len(ok)
        lines = []
        if len(ok) == 1:
            lines.append(os.path.basename(ok[0]["dest"]))
        if bad:
            lines.append("%d failed: %s" % (len(bad), bad[0]["error"]))
        if any(not r["recycled"] for r in ok):
            lines.append("The original ZIP couldn't be removed.")
        self.show_result("success", title, "\n".join(lines))

    # ---- state transitions --------------------------------------------------
    def show_result(self, kind, title, sub):
        self.state = kind
        self.result_title = title
        self.result_sub = sub
        self.render()
        hold = 1700 if kind == "success" else 3600
        self._reset_job = self.root.after(hold, self._begin_fade)

    def _begin_fade(self):
        self._reset_job = None
        self._fade_step(0)

    def _fade_step(self, i, steps=14):
        if self.state not in ("success", "error"):
            return
        t = (i + 1) / float(steps)
        for tag, option, start, end in self.fade_entries:
            self.canvas.itemconfigure(tag, **{option: lerp_color(start, end, t)})
        if i + 1 < steps:
            self._fade_job = self.root.after(28, lambda: self._fade_step(i + 1, steps))
        else:
            self._fade_job = None
            self._reset_idle()

    def _reset_idle(self):
        self._cancel_timers()
        self.state = "idle"
        self.render()

    def _spin(self):
        self._spin_job = None
        if self.state != "processing":
            return
        self.spin_angle = (self.spin_angle - 10) % 360
        self.canvas.itemconfigure("spinner", start=self.spin_angle)
        self._spin_job = self.root.after(25, self._spin)

    def _refresh_progress(self):
        if self.state != "processing":
            return
        name = shorten(self.cur_name, 42)
        self.canvas.itemconfigure("p_title", text=("Unpacking " + name) if name else "Preparing\u2026")
        prefix = "%d of %d  \u2022  " % (self.cur_idx, self.cur_count) if self.cur_count > 1 else ""
        if self.cur_total:
            detail = "%d%%  \u2022  %s of %s" % (
                int(self.pct), fmt_size(self.cur_done), fmt_size(self.cur_total)
            )
        else:
            detail = "Preparing\u2026"
        self.canvas.itemconfigure("p_sub", text=prefix + detail)
        self.pct_var.set(self.pct)

    # ---- rendering ----------------------------------------------------------
    def render(self):
        c = self.canvas
        W, H = c.winfo_width(), c.winfo_height()
        if W < 80 or H < 80:
            return
        px = self.px
        c.delete("all")
        c.configure(cursor="hand2" if self.state == "idle" else "arrow")
        self.fade_entries = []

        # Header
        rounded_rect(c, px(32), px(22), px(58), px(48), px(7), fill=ACCENT, outline="")
        c.create_text(px(45), px(35), text="S", font=(FONT, 12, "bold"), fill="#0b2530")
        c.create_text(px(70), px(35), text=APP_NAME, anchor="w", font=(FONT_SEMI, 14), fill=TEXT)

        # Card
        hover = self.hover and self.state == "idle"
        x1, y1, x2, y2 = px(32), px(70), W - px(32), H - px(60)
        rounded_rect(
            c, x1, y1, x2, y2, px(22),
            fill=CARD_HOVER if hover else CARD,
            outline=ACCENT if hover else BORDER,
            width=2 if hover else 1,
        )
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        wrap = max(px(220), (x2 - x1) - px(80))

        if self.state == "idle":
            self._draw_idle(cx, cy, wrap)
        elif self.state == "processing":
            self._draw_processing(cx, cy, wrap)
        else:
            self._draw_result(cx, cy, wrap)

        # Footer
        c.create_text(
            W // 2, H - px(30),
            text="Smart folders   \u2022   Auto-reveal   \u2022   Recycles the original",
            font=(FONT, 9), fill=SUBTLE,
        )

    def _draw_idle(self, cx, cy, wrap):
        c, px = self.canvas, self.px
        icy, r, lw = cy - px(52), px(36), px(3)
        c.create_oval(cx - r, icy - r, cx + r, icy + r, fill=ICON_BG, outline=ACCENT, width=px(2))
        c.create_line(cx, icy - px(16), cx, icy + px(8), fill=ACCENT, width=lw, capstyle="round")
        c.create_line(
            cx - px(10), icy - px(2), cx, icy + px(8), cx + px(10), icy - px(2),
            fill=ACCENT, width=lw, capstyle="round", joinstyle="round",
        )
        c.create_line(cx - px(14), icy + px(18), cx + px(14), icy + px(18), fill=ACCENT, width=lw, capstyle="round")
        title = c.create_text(
            cx, cy + px(12), text="Drop Archive Here for a Clean Unzip",
            font=(FONT_SEMI, 19), fill=TEXT, width=wrap, justify="center", anchor="n",
        )
        bottom = c.bbox(title)[3]
        c.create_text(
            cx, bottom + px(12), text="or click anywhere on this card to browse for a .zip file",
            font=(FONT, 11), fill=MUTED, width=wrap, justify="center", anchor="n",
        )

    def _draw_processing(self, cx, cy, wrap):
        c, px = self.canvas, self.px
        icy, r = cy - px(56), px(30)
        c.create_oval(cx - r, icy - r, cx + r, icy + r, outline=TROUGH, width=px(4))
        c.create_arc(
            cx - r, icy - r, cx + r, icy + r,
            start=self.spin_angle, extent=110, style="arc",
            outline=ACCENT, width=px(4), tags="spinner",
        )
        c.create_text(
            cx, cy - px(8), text="", tags="p_title", anchor="n",
            font=(FONT_SEMI, 16), fill=TEXT, width=wrap, justify="center",
        )
        c.create_window(cx, cy + px(46), window=self.bar, width=min(px(440), wrap))
        c.create_text(
            cx, cy + px(70), text="", tags="p_sub", anchor="n",
            font=(FONT, 10), fill=MUTED, width=wrap, justify="center",
        )
        self._refresh_progress()

    def _draw_result(self, cx, cy, wrap):
        c, px = self.canvas, self.px
        icy, r = cy - px(52), px(36)
        if self.state == "success":
            ring, ring_bg = GREEN, GREEN_BG
        else:
            ring, ring_bg = RED, RED_BG
        c.create_oval(
            cx - r, icy - r, cx + r, icy + r,
            fill=ring_bg, outline=ring, width=px(3), tags="f_ring",
        )
        if self.state == "success":
            c.create_line(
                cx - px(14), icy + px(1), cx - px(4), icy + px(11), cx + px(15), icy - px(11),
                fill=ring, width=px(5), capstyle="round", joinstyle="round", tags="f_mark",
            )
        else:
            d = px(12)
            c.create_line(cx - d, icy - d, cx + d, icy + d, fill=ring, width=px(5), capstyle="round", tags="f_mark")
            c.create_line(cx - d, icy + d, cx + d, icy - d, fill=ring, width=px(5), capstyle="round", tags="f_mark")

        title = c.create_text(
            cx, cy + px(12), text=self.result_title, tags="f_title",
            font=(FONT_SEMI, 19), fill=TEXT, width=wrap, justify="center", anchor="n",
        )
        bottom = c.bbox(title)[3]
        c.create_text(
            cx, bottom + px(10), text=self.result_sub, tags="f_sub",
            font=(FONT, 11), fill=MUTED, width=wrap, justify="center", anchor="n",
        )
        self.fade_entries = [
            ("f_ring", "fill", ring_bg, CARD),
            ("f_ring", "outline", ring, CARD),
            ("f_mark", "fill", ring, CARD),
            ("f_title", "fill", TEXT, CARD),
            ("f_sub", "fill", MUTED, CARD),
        ]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    if sys.platform == "win32":
        try:
            import ctypes

            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = TkinterDnD.Tk()
    app = ShredPackApp(root)
    if len(sys.argv) > 1:
        root.after(400, lambda: app.start(sys.argv[1:]))
    root.mainloop()


if __name__ == "__main__":
    main()
