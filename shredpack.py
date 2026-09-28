"""
ShredPack - a WinRAR-style archive extractor for Windows, powered by 7-Zip.

* Right-click any archive -> "Extract with ShredPack..." opens the choice dialog.
* Run the program directly (no file) to install / update / remove that menu entry.
* All extraction is done by 7z.exe (7-Zip must be installed, or 7z.exe + 7z.dll
  placed next to this program).

Build:  pyinstaller --noconsole --onefile --name ShredPack shredpack.py
Optional: pip install send2trash  (deleted originals then go to the Recycle Bin)
"""

import os
import re
import sys
import json
import queue
import shutil
import tempfile
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

if sys.platform == "win32":
    import ctypes
    import winreg

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
APP_NAME = "ShredPack"
MENU_KEY = "ShredPack"
MENU_TEXT = "Extract with ShredPack..."

EXTENSIONS = (
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".txz",
    ".iso", ".cab", ".wim", ".dmg", ".vhd", ".vhdx", ".xar",
)
TAR_COMPRESSED = (
    ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tbz", ".tar.xz", ".txz", ".tar.z", ".taz",
)
CREATE_NO_WINDOW = 0x08000000
PCT_RE = re.compile(r"(?<![\w.])(\d{1,3})%")

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"
BG = "#1f1f1f"
CARD = "#2b2b2b"
BORDER = "#3c3c3c"
TEXT = "#f3f3f3"
MUTED = "#a0a0a0"
ACCENT = "#60cdff"
TROUGH = "#3d3d3d"
GREEN = "#6ccb5f"


# --------------------------------------------------------------------------
# Small helpers
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


def unique_path(parent, name):
    candidate = os.path.join(parent, name)
    i = 2
    while os.path.exists(candidate):
        candidate = os.path.join(parent, "%s (%d)" % (name, i))
        i += 1
    return candidate


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
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
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
    except Exception:
        pass


def hidden_root():
    r = tk.Tk()
    r.withdraw()
    return r


# --------------------------------------------------------------------------
# Config + 7-Zip discovery
# --------------------------------------------------------------------------
def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "config.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(config_path()), exist_ok=True)
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def find_7z():
    """Locate 7z.exe: saved path, next to the app, registry, Program Files, PATH."""
    names = ("7z.exe",) if sys.platform == "win32" else ("7zz", "7z", "7za")
    cands = []

    saved = load_config().get("seven_zip")
    if saved:
        cands.append(saved)

    bases = []
    if getattr(sys, "frozen", False):
        bases.append(os.path.dirname(sys.executable))
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            bases.append(mei)
    else:
        bases.append(os.path.dirname(os.path.abspath(__file__)))
    for b in bases:
        for n in names:
            cands.append(os.path.join(b, n))

    if sys.platform == "win32":
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\7-Zip", 0, winreg.KEY_READ | view) as k:
                        for val in ("Path64", "Path"):
                            try:
                                cands.append(os.path.join(winreg.QueryValueEx(k, val)[0], "7z.exe"))
                            except OSError:
                                pass
                except OSError:
                    pass
        for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "7-Zip", "7z.exe"))

    for n in names:
        found = shutil.which(n)
        if found:
            cands.append(found)

    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def ensure_7z():
    path = find_7z()
    if path:
        return path
    root = hidden_root()
    try:
        if messagebox.askyesno(
            APP_NAME,
            "ShredPack needs 7-Zip to extract archives, but 7z.exe wasn't found.\n\n"
            "Do you want to locate 7z.exe manually?\n"
            "(7-Zip is free: https://www.7-zip.org)",
        ):
            chosen = filedialog.askopenfilename(
                title="Locate 7z.exe",
                filetypes=[("7-Zip executable", "7z.exe"), ("Executables", "*.exe"), ("All files", "*.*")],
            )
            if chosen and os.path.isfile(chosen):
                cfg = load_config()
                cfg["seven_zip"] = chosen
                save_config(cfg)
                return chosen
        return None
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# Windows right-click integration (per-user registry, no admin needed)
# --------------------------------------------------------------------------
def launcher_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw
    return '"%s" "%s"' % (exe, os.path.abspath(__file__))


def ext_key(ext):
    return r"Software\Classes\SystemFileAssociations\%s\shell\%s" % (ext, MENU_KEY)


def notify_shell():
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:
        pass


def install_menu():
    if sys.platform != "win32":
        return
    command = launcher_command() + ' "%1"'
    icon = '"%s",0' % sys.executable if getattr(sys, "frozen", False) else None
    for ext in EXTENSIONS:
        key = ext_key(ext)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, MENU_TEXT)
            if icon:
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key + r"\command", 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
    notify_shell()


def uninstall_menu():
    if sys.platform != "win32":
        return
    for ext in EXTENSIONS:
        key = ext_key(ext)
        for sub in (key + r"\command", key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass
    notify_shell()


def install_state():
    """Return 'none', 'stale' (points elsewhere) or 'current'."""
    if sys.platform != "win32":
        return "none"
    expected = launcher_command() + ' "%1"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_key(EXTENSIONS[0]) + r"\command") as k:
            value = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return "none"
    return "current" if str(value).lower() == expected.lower() else "stale"


def run_setup():
    root = hidden_root()
    try:
        if sys.platform != "win32":
            messagebox.showinfo(APP_NAME, "Right-click integration is only available on Windows.")
            return
        state = install_state()
        if state == "current":
            if messagebox.askyesno(
                APP_NAME,
                'The right-click option "%s" is already installed.\n\nWould you like to remove it?' % MENU_TEXT,
            ):
                uninstall_menu()
                messagebox.showinfo(APP_NAME, "The right-click option was removed.")
            return
        if state == "stale":
            question = (
                "The right-click option points to a different copy of ShredPack.\n\n"
                "Update it to use this copy?"
            )
        else:
            question = (
                'Add "%s" to the Windows right-click menu for archive files '
                "(ZIP, RAR, 7Z, TAR, ISO and more)?\n\n"
                "This only changes settings for your user account and needs no administrator rights."
                % MENU_TEXT
            )
        if not messagebox.askyesno(APP_NAME, question):
            return
        try:
            install_menu()
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Couldn't update the registry:\n\n%s" % exc)
            return
        note = (
            'Done! Right-click any archive and choose "%s".\n\n'
            'On Windows 11 it appears under "Show more options".' % MENU_TEXT
        )
        if not find_7z():
            note += (
                "\n\nHeads-up: 7-Zip wasn't found. Install it from https://www.7-zip.org "
                "(ShredPack uses its 7z.exe to extract)."
            )
        messagebox.showinfo(APP_NAME, note)
    finally:
        root.destroy()


# --------------------------------------------------------------------------
# 7-Zip backend
# --------------------------------------------------------------------------
def run_7z(seven, args, on_pct, on_proc):
    """Run 7z.exe, stream progress percentages, return (exit_code, output_tail)."""
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [seven] + args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kw
    )
    on_proc(proc)
    fd = proc.stdout.fileno()
    carry, tail, last = "", "", -1
    while True:
        data = os.read(fd, 4096)
        if not data:
            break
        text = data.decode("utf-8", "replace")
        window = carry + text
        carry = window[-8:]
        for m in PCT_RE.finditer(window):
            v = int(m.group(1))
            if v <= 100 and v > last:
                last = v
                on_pct(v)
        tail = (tail + text)[-8000:]
    return proc.wait(), tail


def extract_one(seven, archive, dest, password, on_pct, on_proc):
    """Extract one archive into dest with 7z.exe. Returns (exit_code, output)."""
    os.makedirs(dest, exist_ok=True)
    base = ["-y", "-aou", "-bso0", "-bsp1", "-sccUTF-8"]
    if password:
        base.append("-p" + password)

    if archive.lower().endswith(TAR_COMPRESSED):
        # tar.gz / tgz / tar.bz2 / tar.xz ...: unwrap the compression, then the tar.
        tmp = tempfile.mkdtemp(prefix=".shredpack_", dir=dest)
        try:
            code, out = run_7z(
                seven, ["x", archive, "-o" + tmp] + base, lambda p: on_pct(p * 0.5), on_proc
            )
            if code != 0:
                return code, out
            items = os.listdir(tmp)
            if len(items) == 1 and items[0].lower().endswith(".tar"):
                return run_7z(
                    seven,
                    ["x", os.path.join(tmp, items[0]), "-o" + dest] + base,
                    lambda p: on_pct(50 + p * 0.5),
                    on_proc,
                )
            for item in items:
                shutil.move(os.path.join(tmp, item), unique_path(dest, item))
            on_pct(100)
            return 0, out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return run_7z(seven, ["x", archive, "-o" + dest] + base, on_pct, on_proc)


def summarize(output):
    lines = []
    for line in re.split(r"[\r\n]+", output):
        line = line.strip()
        if line and not re.match(r"^\d{1,3}%", line):
            lines.append(line)
    text = "\n".join(lines[-8:])
    return shorten(text, 900) if text else "7-Zip did not report any details."


def remove_original(path):
    p = os.path.abspath(path)
    if send2trash is not None:
        try:
            send2trash(p)
            return True
        except Exception:
            pass
    try:
        os.remove(p)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# The dialog
# --------------------------------------------------------------------------
class ShredPackApp:
    def __init__(self, root, archives, seven):
        self.root = root
        self.archives = list(archives)
        self.seven = seven
        self.k = max(1.0, root.winfo_fpixels("1i") / 96.0)

        self.state = "choice"  # choice | processing | finished
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.proc = None
        self.pw_event = threading.Event()
        self.pw_value = None
        self.cur_idx, self.cur_n = 1, 1

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        w, h = self.px(580), self.px(470)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 3))

        self._build_styles()
        self._build_ui()
        self.refresh_choice()
        self.show(self.view_choice)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Return>", lambda e: self.begin(None) if self.state == "choice" else None)
        root.bind("<Escape>", lambda e: self.on_escape())

        enable_dark_titlebar(root)
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))
        root.lift()
        root.focus_force()

    def px(self, v):
        return int(round(v * self.k))

    # ---- construction -----------------------------------------------------
    def _build_styles(self):
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(
            "Primary.TButton", background=ACCENT, foreground="#08222e", borderwidth=0,
            focusthickness=0, focuscolor=ACCENT, padding=(self.px(20), self.px(10)),
            font=(FONT_SEMI, 10),
        )
        s.map("Primary.TButton", background=[("pressed", "#4bb8e8"), ("active", "#7fd7ff")])
        s.configure(
            "Secondary.TButton", background="#3a3a3a", foreground=TEXT, borderwidth=0,
            focusthickness=0, focuscolor="#3a3a3a", padding=(self.px(20), self.px(10)),
            font=(FONT, 10),
        )
        s.map("Secondary.TButton", background=[("pressed", "#505050"), ("active", "#454545")])
        s.configure(
            "Zen.TCheckbutton", background=CARD, foreground=TEXT, font=(FONT, 10),
            focuscolor=CARD, indicatorbackground="#3a3a3a", indicatorforeground="#08222e",
            upperbordercolor=BORDER, lowerbordercolor=BORDER, padding=(0, self.px(4)),
        )
        s.map(
            "Zen.TCheckbutton",
            background=[("active", CARD)],
            foreground=[("active", TEXT)],
            indicatorbackground=[("selected", ACCENT), ("active", "#454545")],
        )
        s.configure(
            "Zen.Horizontal.TProgressbar", troughcolor=TROUGH, background=ACCENT,
            bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, borderwidth=0,
            thickness=self.px(6),
        )

    def _card(self, parent):
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=self.px(26), pady=self.px(22))
        return card, inner

    def _build_ui(self):
        px = self.px
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=px(28), pady=(px(22), px(12)))
        logo = tk.Canvas(header, width=px(30), height=px(30), bg=BG, highlightthickness=0)
        logo.pack(side="left")
        rounded_rect(logo, px(2), px(2), px(28), px(28), px(7), fill=ACCENT, outline="")
        logo.create_text(px(15), px(15), text="S", font=(FONT, 12, "bold"), fill="#0b2530")
        tk.Label(header, text=APP_NAME, font=(FONT_SEMI, 15), fg=TEXT, bg=BG).pack(
            side="left", padx=(px(10), 0)
        )

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=px(28), pady=(0, px(24)))

        # --- choice view
        self.view_choice, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Cancel", style="Secondary.TButton", command=self.on_close).pack(side="right")

        self.lbl_name = tk.Label(inner, text="", font=(FONT_SEMI, 14), fg=TEXT, bg=CARD,
                                 anchor="w", justify="left", wraplength=px(480))
        self.lbl_name.pack(fill="x")
        self.lbl_meta = tk.Label(inner, text="", font=(FONT, 10), fg=MUTED, bg=CARD,
                                 anchor="w", justify="left", wraplength=px(480))
        self.lbl_meta.pack(fill="x", pady=(px(2), 0))
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=px(16))

        tk.Label(inner, text="Extract to", font=(FONT, 10), fg=MUTED, bg=CARD, anchor="w").pack(fill="x")
        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=(px(8), 0))
        ttk.Button(row, text="Extract Here", style="Primary.TButton",
                   command=lambda: self.begin(None)).pack(side="left")
        ttk.Button(row, text="Extract to Folder...", style="Secondary.TButton",
                   command=self.pick_folder).pack(side="left", padx=(px(10), 0))
        self.lbl_hint = tk.Label(inner, text="", font=(FONT, 9), fg=MUTED, bg=CARD,
                                 anchor="w", justify="left", wraplength=px(480))
        self.lbl_hint.pack(fill="x", pady=(px(8), 0))

        self.delete_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            inner, text="Delete original archive after successful extraction",
            variable=self.delete_var, style="Zen.TCheckbutton",
        ).pack(anchor="w", pady=(px(16), 0))

        # --- progress view
        self.view_progress, inner = self._card(body)
        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Cancel", style="Secondary.TButton", command=self.on_cancel).pack(side="right")
        self.lbl_p_title = tk.Label(inner, text="Preparing\u2026", font=(FONT_SEMI, 14), fg=TEXT,
                                    bg=CARD, anchor="w", justify="left", wraplength=px(480))
        self.lbl_p_title.pack(fill="x", pady=(px(24), 0))
        self.progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(inner, style="Zen.Horizontal.TProgressbar", orient="horizontal",
                        mode="determinate", maximum=100, variable=self.progress_var
                        ).pack(fill="x", pady=(px(22), 0))
        self.lbl_p_sub = tk.Label(inner, text="", font=(FONT, 10), fg=MUTED, bg=CARD, anchor="w")
        self.lbl_p_sub.pack(fill="x", pady=(px(10), 0))

        # --- done view
        self.view_done = tk.Frame(body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        box = tk.Frame(self.view_done, bg=CARD)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(box, text="\u2713", font=(FONT_SEMI, 46), fg=GREEN, bg=CARD).pack()
        tk.Label(box, text="Extraction complete", font=(FONT_SEMI, 16), fg=TEXT, bg=CARD).pack(pady=(px(4), 0))
        self.lbl_d_sub = tk.Label(box, text="", font=(FONT, 10), fg=MUTED, bg=CARD,
                                  justify="center", wraplength=px(440))
        self.lbl_d_sub.pack(pady=(px(6), 0))

    def show(self, view):
        for v in (self.view_choice, self.view_progress, self.view_done):
            v.pack_forget()
        view.pack(fill="both", expand=True)

    def refresh_choice(self):
        first = self.archives[0]
        if len(self.archives) == 1:
            try:
                size = fmt_size(os.path.getsize(first))
            except OSError:
                size = "?"
            self.lbl_name.config(text=shorten(os.path.basename(first), 52))
            self.lbl_meta.config(text="%s  \u2022  %s" % (size, shorten(os.path.dirname(first), 52)))
        else:
            names = ", ".join(shorten(os.path.basename(a), 22) for a in self.archives[:2])
            self.lbl_name.config(text="%d archives selected" % len(self.archives))
            self.lbl_meta.config(text=names + ("\u2026" if len(self.archives) > 2 else ""))
        self.lbl_hint.config(
            text="Extract Here  \u2192  " + shorten(os.path.dirname(first), 56)
            + ("  (each archive's own folder)" if len(self.archives) > 1 else "")
        )

    # ---- actions ------------------------------------------------------------
    def pick_folder(self):
        if self.state != "choice":
            return
        chosen = filedialog.askdirectory(
            parent=self.root,
            title="Choose destination folder",
            initialdir=os.path.dirname(self.archives[0]),
            mustexist=False,
        )
        if chosen:
            self.begin(os.path.normpath(chosen))

    def begin(self, dest_override):
        if self.state != "choice":
            return
        self.state = "processing"
        self.cancel.clear()
        self.progress_var.set(0)
        self.lbl_p_title.config(text="Preparing\u2026")
        self.lbl_p_sub.config(text="")
        self.show(self.view_progress)
        delete = self.delete_var.get()
        threading.Thread(
            target=self._worker, args=(list(self.archives), dest_override, delete), daemon=True
        ).start()
        self.root.after(40, self._poll)

    def on_cancel(self):
        if self.state != "processing":
            return
        self.cancel.set()
        self.lbl_p_sub.config(text="Cancelling\u2026")
        self._terminate()

    def _terminate(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def on_escape(self):
        if self.state == "choice":
            self.root.destroy()
        elif self.state == "processing":
            self.on_cancel()

    def on_close(self):
        if self.state == "processing":
            self.cancel.set()
            self._terminate()
        self.root.destroy()

    # ---- worker thread ------------------------------------------------------
    def _set_proc(self, proc):
        self.proc = proc

    def _ask_password(self, name, retry):
        self.pw_event.clear()
        self.q.put(("password", name, retry))
        self.pw_event.wait()
        return self.pw_value

    def _worker(self, archives, dest_override, delete):
        results = []
        total = len(archives)
        for idx, arc in enumerate(archives, 1):
            if self.cancel.is_set():
                break
            name = os.path.basename(arc)
            self.q.put(("file", idx, total, name))
            dest = dest_override or os.path.dirname(os.path.abspath(arc))
            res = {"path": arc, "name": name, "status": "error", "detail": "",
                   "deleted": False, "delete_failed": False}
            password = None
            code, out = -1, ""
            try:
                while True:
                    code, out = extract_one(
                        self.seven, arc, dest, password,
                        lambda p: self.q.put(("pct", p)), self._set_proc,
                    )
                    if (code not in (0, 1) and not self.cancel.is_set()
                            and "password" in out.lower()):
                        password = self._ask_password(name, password is not None)
                        if password is None:
                            self.cancel.set()
                            break
                        continue
                    break
            except Exception as exc:  # noqa: BLE001
                res["detail"] = "Could not run 7-Zip: %s" % exc
                results.append(res)
                continue

            if self.cancel.is_set():
                break
            if code == 0:
                res["status"] = "ok"
                if delete:
                    ok = remove_original(arc)
                    res["deleted"], res["delete_failed"] = ok, not ok
            elif code == 1:
                res["status"] = "warn"
                res["detail"] = "%s\n%s" % (name, summarize(out))
            else:
                res["detail"] = "7-Zip exit code %d\n%s" % (code, summarize(out))
            results.append(res)
        self.q.put(("finished", results))

    # ---- main-thread event handling ----------------------------------------
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
            self._set_progress(0)
        elif kind == "pct":
            self._set_progress(msg[1])
        elif kind == "password":
            _, name, retry = msg
            prompt = ("Wrong password. Try again.\n\n" if retry else "") + (
                '"%s" is password-protected.\nEnter password:' % shorten(name, 40)
            )
            pw = simpledialog.askstring(APP_NAME, prompt, show="*", parent=self.root)
            self.pw_value = pw if pw else None
            self.pw_event.set()
        elif kind == "finished":
            self._finish(msg[1])

    def _set_progress(self, pct):
        overall = ((self.cur_idx - 1) + pct / 100.0) / self.cur_n * 100.0
        self.progress_var.set(overall)
        if not self.cancel.is_set():
            prefix = "%d of %d  \u2022  " % (self.cur_idx, self.cur_n) if self.cur_n > 1 else ""
            self.lbl_p_sub.config(text="%s%d%%" % (prefix, int(pct)))

    def _finish(self, results):
        self.state = "finished"
        oks = [r for r in results if r["status"] == "ok"]
        warns = [r for r in results if r["status"] == "warn"]
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

        if warns:
            messagebox.showwarning(
                "%s - Finished with warnings" % APP_NAME,
                "7-Zip finished with warnings (exit code 1). "
                "The original archive was kept.\n\n" + "\n\n".join(r["detail"] for r in warns),
                parent=self.root,
            )

        notes = []
        if any(r["deleted"] for r in oks):
            notes.append("Original archive deleted.")
        if any(r["delete_failed"] for r in oks):
            notes.append("The original archive couldn't be deleted.")
        self.lbl_d_sub.config(text=" ".join(notes))
        self.show(self.view_done)
        self.root.after(1300, self.root.destroy)

    def _back_to_choice(self, results):
        finished = {r["path"] for r in results if r["status"] in ("ok", "warn")}
        self.archives = [a for a in self.archives if a not in finished]
        if not self.archives:
            self.root.destroy()
            return
        self.state = "choice"
        self.refresh_choice()
        self.show(self.view_choice)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    enable_dpi_awareness()
    args = sys.argv[1:]

    if "--install" in args:
        install_menu()
        return
    if "--uninstall" in args:
        uninstall_menu()
        return

    files = [os.path.abspath(a) for a in args if not a.startswith("--")]
    if not files:
        run_setup()
        return

    existing = [f for f in files if os.path.isfile(f)]
    if not existing:
        root = hidden_root()
        messagebox.showerror(APP_NAME, "The selected file couldn't be found:\n\n%s" % files[0])
        root.destroy()
        return

    seven = ensure_7z()
    if not seven:
        return

    root = tk.Tk()
    ShredPackApp(root, existing, seven)
    root.mainloop()


if __name__ == "__main__":
    main()
