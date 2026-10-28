"""Drives the real ShredPack app through a faithful stand-in for Tk's event loop.

Why this exists: tests that hand-deliver worker messages (tests/ui_smoke.py) can't notice when
the app's own scheduling is broken. Here, root.after() callbacks run ONLY when the loop runs, the
way Tk's mainloop runs them - so if the app stops polling its worker (which once left every
background operation stuck with a Cancel button that did nothing), these tests time out and fail.

It includes the exact entry points Explorer uses (main() with --mode test / compress-quick).
Widgets are stand-ins, so this checks behaviour and timing, not how things look.

Standard library only. Run:  python tests/test_ui_event_loop.py
"""
import heapq
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import zipfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class Var(object):
    def __init__(self, master=None, value=None, name=None):
        self._v, self._tr = value, []

    def get(self):
        return self._v

    def set(self, v):
        self._v = v
        for cb in list(self._tr):
            cb("x", "", "w")

    def trace_add(self, mode, cb):
        self._tr.append(cb)


class SV(Var):
    def __init__(self, master=None, value="", name=None):
        Var.__init__(self, master, value)


class BV(Var):
    def __init__(self, master=None, value=False, name=None):
        Var.__init__(self, master, value)


class DV(Var):
    def __init__(self, master=None, value=0.0, name=None):
        Var.__init__(self, master, value)


class TclError(Exception):
    pass


def module(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


W = lambda *a, **k: mock.MagicMock()  # noqa: E731
messagebox = module("tkinter.messagebox", showinfo=mock.MagicMock(), showwarning=mock.MagicMock(),
                    showerror=mock.MagicMock(), askyesno=mock.MagicMock(return_value=True))
simpledialog = module("tkinter.simpledialog", askstring=mock.MagicMock(return_value=None))
filedialog = module("tkinter.filedialog", askopenfilenames=mock.MagicMock(), askopenfilename=mock.MagicMock(),
                    askdirectory=mock.MagicMock())
ttk = module("tkinter.ttk", Style=W, Button=W, Checkbutton=W, Combobox=W, Progressbar=W, Treeview=W, Scrollbar=W)
tk = module("tkinter", StringVar=SV, BooleanVar=BV, DoubleVar=DV, TclError=TclError, Tk=W, Frame=W, Label=W,
            Canvas=W, Entry=W, Toplevel=W, ttk=ttk, messagebox=messagebox, simpledialog=simpledialog,
            filedialog=filedialog)
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": messagebox,
                    "tkinter.simpledialog": simpledialog, "tkinter.filedialog": filedialog})
import shredpack as sp  # noqa: E402

sp.VERSION_URL = ""
sp.log_write = lambda *a, **k: None
SETTINGS_DIR = tempfile.mkdtemp(prefix="shredpack_settings_")   # never touch the real settings
sp.settings_path = lambda: os.path.join(SETTINGS_DIR, "settings.json")

ok, skipped = [], []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    ok.append(bool(cond))


class Loop(object):
    """Tk's event loop in miniature: after() queues; only run() executes."""

    def __init__(self):
        self.q, self.n, self.cancelled = [], itertools.count(), set()
        self.destroyed_at = None
        self.max_callback = 0.0
        self.t0 = time.monotonic()

    def after(self, ms, fn=None, *args):
        i = next(self.n)
        heapq.heappush(self.q, (time.monotonic() + ms / 1000.0, i, fn, args))
        return "after#%d" % i

    def after_cancel(self, token):
        self.cancelled.add(token)

    def destroy(self):
        if self.destroyed_at is None:
            self.destroyed_at = time.monotonic()

    @property
    def destroyed(self):
        return self.destroyed_at is not None

    def run(self, until=None, limit=30.0):
        """Run callbacks until `until()` is true, the window is destroyed, or `limit` seconds pass."""
        end = time.monotonic() + limit
        while not self.destroyed and time.monotonic() < end:
            if until is not None and until():
                return True
            if self.q and self.q[0][0] <= time.monotonic():
                _, i, fn, args = heapq.heappop(self.q)
                if fn is None or "after#%d" % i in self.cancelled:
                    continue
                start = time.monotonic()
                fn(*args)
                self.max_callback = max(self.max_callback, time.monotonic() - start)
            else:
                time.sleep(0.002)
        return until() if until is not None else self.destroyed


def make_root(loop):
    root = mock.MagicMock()
    root.winfo_fpixels.return_value = 96.0
    root.winfo_screenwidth.return_value = 1920
    root.winfo_screenheight.return_value = 1080
    root.after, root.after_cancel, root.destroy = loop.after, loop.after_cancel, loop.destroy
    root.mainloop = lambda: None            # main() returns; the test then runs the loop
    return root


def reset_popups():
    for m in (messagebox.showinfo, messagebox.showerror, messagebox.showwarning, messagebox.askyesno,
              simpledialog.askstring):
        m.reset_mock()
    messagebox.askyesno.return_value = True
    simpledialog.askstring.return_value = None


def shell_launch(*argv):
    """What Explorer does: start the program with arguments; the window then lives in the event loop."""
    loop = Loop()
    root = make_root(loop)
    holder = {}
    original = sp.ShredPackApp

    class Spy(original):
        def __init__(self, *a, **k):
            original.__init__(self, *a, **k)
            holder["app"] = self

    sp.make_root = lambda with_dnd: root
    sp.ShredPackApp = Spy
    reset_popups()
    try:
        sys.argv = ["shredpack"] + list(argv)
        sp.main()
    finally:
        sp.ShredPackApp = original
    return loop, holder.get("app")


def in_app():
    loop = Loop()
    reset_popups()
    app = sp.ShredPackApp(make_root(loop))
    return app, loop


WORK = tempfile.mkdtemp(prefix="shredpack_loop_tests_")
SCRATCH = os.path.join(WORK, "tmp")
os.makedirs(SCRATCH)
tempfile.tempdir = SCRATCH


def fresh(name):
    p = os.path.join(WORK, name)
    os.makedirs(p)
    return p


def make_zip(path, files, comp=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", comp) as zf:
        for n, d in files.items():
            zf.writestr(n, d)


# ---- the scheduling helper itself ----
app, loop = in_app()
before = len(loop.q)
app._ensure_poll()
app._ensure_poll()
check("starting the poll schedules exactly one callback (not zero, not two)", len(loop.q) == before + 1)

# ---- right-click "Test archive" (the reported problem) ----
d = fresh("rc")
small = os.path.join(d, "small.zip")
make_zip(small, {"a.txt": "hello" * 1000, "b.txt": "world"})
loop, app = shell_launch("--mode", "test", small)
check("right-click Test (small zip): the window does its job and closes itself", loop.run(limit=10))
check("...and shows the result", messagebox.showinfo.called and "No errors" in messagebox.showinfo.call_args[0][1])
check("...in well under a second (%.2fs)" % (loop.destroyed_at - loop.t0 if loop.destroyed else 99),
      loop.destroyed and loop.destroyed_at - loop.t0 < 1.5)

bad = os.path.join(d, "bad.zip")
make_zip(bad, {"one.bin": os.urandom(3000).hex()})
raw = bytearray(open(bad, "rb").read())
raw[60] ^= 0xFF
open(bad, "wb").write(raw)
loop, app = shell_launch("--mode", "test", bad)
check("right-click Test (corrupted zip): reports the problem and closes",
      loop.run(limit=10) and messagebox.showerror.called and "one.bin" in messagebox.showerror.call_args[0][1])

zp = os.path.join(d, "empty.zip")
zipfile.ZipFile(zp, "w").close()
loop, app = shell_launch("--mode", "test", zp)
check("right-click Test (empty zip): says so and closes",
      loop.run(limit=10) and messagebox.showinfo.called and "empty" in messagebox.showinfo.call_args[0][1])

if shutil.which("zip"):
    cwd = os.getcwd()
    os.chdir(d)
    open("s.txt", "w").write("secret " * 50)
    subprocess.run(["zip", "-q", "-P", "pw", "enc.zip", "s.txt"], check=True)
    os.chdir(cwd)
    loop, app = shell_launch("--mode", "test", os.path.join(d, "enc.zip"))
    check("right-click Test (encrypted zip): asks for a password; declining closes the window",
          loop.run(limit=10) and simpledialog.askstring.called and not messagebox.showerror.called)
    simpledialog.askstring.return_value = "pw"
    loop, app = shell_launch("--mode", "test", os.path.join(d, "enc.zip"))
    simpledialog.askstring.return_value = "pw"
    check("right-click Test (encrypted zip): with the right password it verifies",
          loop.run(limit=10) and messagebox.showinfo.called and "No errors" in messagebox.showinfo.call_args[0][1])
else:
    skipped.append("encrypted right-click test (no 'zip' program)")
    print("SKIP encrypted right-click tests")

# ---- Cancel on a large archive (the other reported problem) ----
big = os.path.join(fresh("big"), "big.zip")
with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as zf:
    with zf.open("huge.bin", "w", force_zip64=True) as w:
        block = b"\0" * (1 << 20)
        for _ in range(900):
            w.write(block)
loop, app = shell_launch("--mode", "test", big)
click = {}


def press_cancel():
    click["t"] = time.monotonic()
    app.on_cancel()                          # the Cancel button


loop.after(300, press_cancel)
closed = loop.run(limit=15)
latency = (loop.destroyed_at - click["t"]) if (closed and "t" in click) else 99
check("right-click Test on a large zip: Cancel closes the window", closed)
check("...promptly (%.0f ms after the click)" % (latency * 1000), latency < 1.0)
check("...with no result popup", not messagebox.showinfo.called and not messagebox.showerror.called)
check("...and no temp files left", os.listdir(SCRATCH) == [])
check("...and the event loop never stalled (longest callback %.0f ms)" % (loop.max_callback * 1000),
      loop.max_callback < 0.5)

# Cancel in the normal (in-app) window returns to the options screen instead of closing
app, loop = in_app()
app.open_archives([big], "dialog")
app.test_selected()
t_click = {}
loop.after(300, lambda: (t_click.__setitem__("t", time.monotonic()), app.on_cancel()))
returned = loop.run(until=lambda: "t" in t_click and app.state == "choice", limit=15)
check("in-app Test: Cancel returns to the options screen", returned)
check("...within a second", returned and time.monotonic() - t_click["t"] < 1.5)
check("...with no popup", not messagebox.showinfo.called and not messagebox.showerror.called)
# ...and the next operation works (the cancel flag does not stick)
app.open_archives([small], "dialog")
app.test_selected()
loop.run(until=lambda: messagebox.showinfo.called, limit=10)
check("...and the next Test works", messagebox.showinfo.called and "No errors" in messagebox.showinfo.call_args[0][1])

# ---- the other background operations, through the same loop ----
d = fresh("ops")
z = os.path.join(d, "x.zip")
make_zip(z, {"a.txt": "A", "sub/b.txt": "B"})
app, loop = in_app()
app.open_archives([z], "here")              # instant by default: no confirmation screen, no begin()
done = loop.run(until=lambda: app.state == "home", limit=15)
check("extract: completes and returns home", done)
check("extract: files extracted into a folder named after the archive",
      sorted(os.listdir(os.path.join(d, "x"))) == ["a.txt", "sub"])

folder = os.path.join(d, "proj")
os.makedirs(folder)
open(os.path.join(folder, "f.txt"), "w").write("data" * 1000)
loop, app = shell_launch("--mode", "compress-quick", folder)
check("right-click 'Add to ZIP': completes and the window closes", loop.run(limit=15))
check("...and the zip exists", os.path.isfile(os.path.join(d, "proj.zip")))

app, loop = in_app()
app.open_archives([z], "dialog")
app.open_browser()
check("browse: the listing arrives", loop.run(until=lambda: app.state == "browse", limit=15))

hf = os.path.join(d, "abc.txt")
open(hf, "wb").write(b"abc")
app, loop = in_app()
app._start_hash(hf)
check("checksum: completes", loop.run(until=lambda: app.state == "home", limit=15))

app, loop = in_app()
app.open_compress([folder], "dialog")
app.begin_compress()
check("compress dialog: completes", loop.run(until=lambda: app.state == "home", limit=15))

# ---- instant Extract Here, the way Explorer launches it --------------------------------------
d = fresh("quickhere")
z = os.path.join(d, "fast.zip")
make_zip(z, {"a.txt": "A", "sub/b.txt": "B"})
loop, app = shell_launch("--mode", "here", z)
check("Extract Here (shell): compact window", app is not None and app.compact)
check("...created hidden", app.root.withdraw.called)
closed = loop.run(limit=15)
check("...extracts and closes by itself", closed)
check("...into a folder named after the archive", sorted(os.listdir(os.path.join(d, "fast"))) == ["a.txt", "sub"])
check("...without ever showing a window for a fast job", not app.root.deiconify.called)
check("...in well under a second (%.0f ms)" % ((loop.destroyed_at - loop.t0) * 1000),
      loop.destroyed and loop.destroyed_at - loop.t0 < 1.0)
check("...and with no popups", not messagebox.showinfo.called and not messagebox.showerror.called)

# a job that takes a while reveals the small progress window, then still closes by itself
def slow_extract(source, dest, ctx, motw_source=None):
    for pct in (10, 40, 70, 100):
        time.sleep(0.25)
        ctx.progress(pct, pct, 100)
    os.makedirs(dest, exist_ok=True)


with mock.patch.object(sp, "extract_archive", slow_extract):
    loop, app = shell_launch("--mode", "here", z)
    closed = loop.run(limit=15)
check("slow Extract Here: the progress window appears (after ~0.45 s)", app.root.deiconify.called)
check("...and the window still closes by itself", closed)

# failure: the window is shown so the error can be read, then it closes
bad = os.path.join(d, "broken.zip")
open(bad, "wb").write(b"this is not a zip file" * 50)
loop, app = shell_launch("--mode", "here", bad)
closed = loop.run(limit=15)
check("broken archive: the window is revealed", app.root.deiconify.called)
check("...an error is shown", messagebox.showerror.called)
check("...and the small window closes afterwards (no confirmation screen)", closed)

# with the setting off, Extract Here brings back the normal confirmation window
open(sp.settings_path(), "w").write('{"quick_here": false}')
loop, app = shell_launch("--mode", "here", z)
check("quick off: normal-size, visible window", app is not None and not app.compact and not app.root.withdraw.called)
check("...showing the confirmation screen", app.state == "choice")
os.remove(sp.settings_path())

# Add to ZIP from Explorer uses the compact window too
folder2 = os.path.join(d, "proj2")
os.makedirs(folder2)
open(os.path.join(folder2, "f.txt"), "w").write("data" * 1000)
loop, app = shell_launch("--mode", "compress-quick", folder2)
check("Add to ZIP (shell): compact window", app.compact and app.root.withdraw.called)
check("...completes, closes, and creates the zip", loop.run(limit=15) and os.path.isfile(os.path.join(d, "proj2.zip")))
check("...never showing a window for a fast job", not app.root.deiconify.called)

tempfile.tempdir = None
shutil.rmtree(WORK, ignore_errors=True)
print("\nEVENT-LOOP RESULT: %d/%d checks passed%s" % (sum(ok), len(ok),
                                                       (", %d skipped" % len(skipped)) if skipped else ""))
sys.exit(0 if all(ok) else 1)
