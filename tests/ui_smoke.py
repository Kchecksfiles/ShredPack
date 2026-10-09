"""Smoke test for the ShredPack UI logic using stand-in widgets (no display needed).

It can't judge how things look, but it builds the real ShredPackApp class and drives
each flow through its real worker threads, so wiring mistakes (wrong names, missing
attributes, bad signatures) show up here instead of in front of a user.
"""
import os, sys, types, queue, time, threading, tempfile, zipfile, tarfile, io
from unittest import mock


class Var(object):
    def __init__(self, master=None, value=None, name=None):
        self._v, self._tr = value, []
    def get(self): return self._v
    def set(self, v):
        self._v = v
        for cb in list(self._tr): cb("x", "", "w")
    def trace_add(self, mode, cb): self._tr.append(cb)
class StringVar(Var):
    def __init__(self, master=None, value="", name=None): Var.__init__(self, master, value)
class BooleanVar(Var):
    def __init__(self, master=None, value=False, name=None): Var.__init__(self, master, value)
class DoubleVar(Var):
    def __init__(self, master=None, value=0.0, name=None): Var.__init__(self, master, value)
class TclError(Exception): pass

def module(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); return m

widget = lambda *a, **k: mock.MagicMock()
messagebox = module("tkinter.messagebox", showinfo=mock.MagicMock(), showwarning=mock.MagicMock(),
                    showerror=mock.MagicMock(), askyesno=mock.MagicMock(return_value=True))
simpledialog = module("tkinter.simpledialog", askstring=mock.MagicMock(return_value=None))
filedialog = module("tkinter.filedialog", askopenfilenames=mock.MagicMock(), askopenfilename=mock.MagicMock(),
                    askdirectory=mock.MagicMock())
ttk = module("tkinter.ttk", Style=widget, Button=widget, Checkbutton=widget, Combobox=widget,
             Progressbar=widget, Treeview=widget, Scrollbar=widget)
tk = module("tkinter", StringVar=StringVar, BooleanVar=BooleanVar, DoubleVar=DoubleVar, TclError=TclError,
            Tk=widget, Frame=widget, Label=widget, Canvas=widget, Entry=widget, Toplevel=widget,
            ttk=ttk, messagebox=messagebox, simpledialog=simpledialog, filedialog=filedialog)
sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": messagebox,
                    "tkinter.simpledialog": simpledialog, "tkinter.filedialog": filedialog})
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import shredpack as sp
sp.VERSION_URL = ""      # no network in tests
sp.log_write = lambda *a, **k: None

ok = []
def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label); ok.append(bool(cond))

def new_app(from_shell=False):
    root = mock.MagicMock()
    root.winfo_fpixels.return_value = 96.0
    root.winfo_screenwidth.return_value = 1920
    root.winfo_screenheight.return_value = 1080
    root.tk.splitlist.side_effect = lambda s: s.split("|")
    for m in (messagebox.showinfo, messagebox.showwarning, messagebox.showerror, messagebox.askyesno):
        m.reset_mock()
    messagebox.askyesno.return_value = True
    return sp.ShredPackApp(root, from_shell=from_shell), root

def pump(app, until, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try: msg = app.q.get(timeout=0.5)
        except queue.Empty: continue
        app._handle(msg)
        if msg[0] in until: return msg
    raise TimeoutError("waiting for %s (state=%s)" % (until, app.state))

def make_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for n, d in files.items(): zf.writestr(n, d)

T = tempfile.mkdtemp()
def fresh(n):
    p = os.path.join(T, n); os.makedirs(p); return p

# ---- construction ----
app, root = new_app()
check("app builds and starts at home", app.state == "home")

# ---- normal extraction, options defaults ----
d = fresh("ex"); z = os.path.join(d, "a.zip"); make_zip(z, {"x.txt": "X", "sub/y.txt": "Y"})
app.open_archives([z], "here")
check("choice screen reached", app.state == "choice")
app.begin(); pump(app, {"finished"})
check("extract here -> named folder", sorted(os.listdir(os.path.join(d, "a"))) == ["sub", "x.txt"])
check("done screen shown", app.state == "finished")

# ---- selection from browser + delete guard ----
d = fresh("sel"); z = os.path.join(d, "s.zip"); make_zip(z, {"a.txt": "A", "dir/b.txt": "B", "dir/c.txt": "C"})
app.go_home(); app.open_archives([z], "dialog"); out = os.path.join(d, "out"); app.dest_var.set(out)
app.open_browser(); pump(app, {"listing_done", "listing_failed"})
check("browser reached", app.state == "browse")
paths = [e["path"] for e in app.listing["entries"]]
check("browser lists entries", paths == ["a.txt", "dir", "dir/b.txt", "dir/c.txt"] or paths == ["a.txt", "dir/b.txt", "dir/c.txt"])
check("browser populated rows", app.tree.insert.call_count == len(paths))
idx = [str(i) for i, e in enumerate(app.listing["entries"]) if e["path"].startswith("dir")]
app.tree.selection.return_value = tuple(idx)
app.browse_extract(True)
check("selection recorded + back on choice screen", app.state == "choice" and app.selection == {"dir", "dir/b.txt", "dir/c.txt"} or app.selection == {"dir/b.txt", "dir/c.txt"})
app.delete_var.set(True); messagebox.askyesno.return_value = False   # user refuses to delete a partial archive
app.begin(); pump(app, {"finished"})
check("selected only extracted", sorted(os.listdir(out)) == ["dir"] and sorted(os.listdir(os.path.join(out, "dir"))) == ["b.txt", "c.txt"])
check("partial extract: original kept when delete declined", os.path.exists(z))
check("partial extract asked before deleting", messagebox.askyesno.called)

# ---- browse with nothing selected / back / search ----
app.go_home(); app.open_archives([z], "dialog"); app.open_browser(); pump(app, {"listing_done"})
app.tree.selection.return_value = ()
app.browse_extract(True)
check("no selection -> prompt, stay in browser", app.state == "browse" and messagebox.showinfo.called)
app.search_var.set("c.txt"); app._browse_filter()
check("search filters rows", "1 of" in str(app.lbl_b_count.config.call_args))
app.browse_back()
check("back returns to choice and clears selection", app.state == "choice" and app.selection is None)

# ---- filters + overwrite policy via the real dialog fields ----
d = fresh("flt"); z = os.path.join(d, "f.zip"); make_zip(z, {"a.jpg": "1", "b.txt": "2", "c.jpg": "3"})
app.go_home(); app.open_archives([z], "dialog"); out = os.path.join(d, "o"); app.dest_var.set(out)
app.include_var.set("*.jpg"); app.exclude_var.set("c.*")
app.begin(); pump(app, {"finished"})
check("include/exclude patterns applied", sorted(os.listdir(out)) == ["a.jpg"])
os.makedirs(out, exist_ok=True)
app.go_home(); app.open_archives([z], "dialog"); app.dest_var.set(out)
app.overwrite_var.set(dict(sp.OVERWRITE_POLICIES)["skip"])
app.include_var.set("*.jpg")
app.begin(); pump(app, {"finished"})
check("skip policy reported on done screen", "kept" in str(app.lbl_d_sub.config.call_args))
check("nothing is renamed under skip", sorted(os.listdir(out)) == ["a.jpg", "c.jpg"])

# ---- nothing matches -> error, no stuck state ----
app.go_home(); app.open_archives([z], "dialog"); app.dest_var.set(os.path.join(d, "o2")); app.include_var.set("*.zzz")
app.begin(); pump(app, {"finished"})
check("no-match shows error and returns to choice", messagebox.showerror.called and app.state == "choice")

# ---- Test archive ----
d = fresh("tst"); good = os.path.join(d, "g.zip"); make_zip(good, {"one.bin": os.urandom(5000).hex(), "two.txt": "2"})
app.go_home(); app.open_archives([good], "dialog"); app.test_selected(); pump(app, {"test_finished"})
check("test good archive -> info", messagebox.showinfo.called and "No errors" in messagebox.showinfo.call_args[0][1] and app.state == "choice")
bad = os.path.join(d, "b.zip"); make_zip(bad, {"one.bin": os.urandom(3000).hex()})
raw = bytearray(open(bad, "rb").read()); raw[60] ^= 0xFF; open(bad, "wb").write(raw)
messagebox.showerror.reset_mock()
app.go_home(); app.open_archives([bad], "dialog"); app.test_selected(); pump(app, {"test_finished"})
check("test corrupted archive -> error popup", messagebox.showerror.called)

# ---- right-click "Test archive" closes the window afterwards ----
app2, root2 = new_app(from_shell=True)
app2.run_test_now([good]); pump(app2, {"test_finished"})
check("shell test closes window", root2.destroy.called)

# ---- checksum ----
d = fresh("hsh"); f = os.path.join(d, "abc.txt"); open(f, "wb").write(b"abc")
app.go_home(); app._start_hash(f); pump(app, {"hash_done", "hash_failed"})
check("checksum dialog shown, back home", app.state == "home" and not messagebox.showerror.call_args_list[1:])

# ---- compress: dialog, level, notes, quick ----
d = fresh("cmp"); folder = os.path.join(d, "proj"); os.makedirs(folder)
open(os.path.join(folder, "r.txt"), "w").write("ABCD" * 40000)
try:
    os.symlink("/etc", os.path.join(folder, "lnk")); linked = True
except (OSError, NotImplementedError):
    linked = False
app.go_home(); app.open_compress([folder], "dialog")
check("compress dialog default name", app.compress_name_var.get() == "proj.zip" and app.state == "compress_choice")
app.compress_level_var.set(dict(sp.COMPRESS_LEVELS)["max"])
app.begin_compress(); pump(app, {"compress_finished"})
check("compress writes zip", os.path.isfile(os.path.join(d, "proj.zip")))
if linked:
    check("link skipped and reported", "link" in str(app.lbl_d_sub.config.call_args))
app.go_home(); app.open_compress([folder], "compress-quick"); pump(app, {"compress_finished"})
check("quick compress creates proj (2).zip", os.path.isfile(os.path.join(d, "proj (2).zip")))
# large-job guard declines
app.go_home(); sp.CONFIRM_FILES = 1; messagebox.askyesno.return_value = False
app.open_compress([folder], "dialog"); before = set(os.listdir(d)); app.begin_compress()
check("big-job confirm can cancel", app.state == "compress_choice" and set(os.listdir(d)) == before)
sp.CONFIRM_FILES = 100000; messagebox.askyesno.return_value = True

# ---- drag-drop into the compress screen ----
extra = os.path.join(d, "extra.txt"); open(extra, "w").write("e")
app.go_home(); app.open_compress([folder], "dialog")
app._on_drop(types.SimpleNamespace(data=extra, action="copy"))
check("drop adds to compress list", extra in app.compress_sources)
# drop archives on home -> extract flow; others -> compress flow
app.go_home(); app._on_drop(types.SimpleNamespace(data=good, action="copy"))
check("dropped archive -> extract flow", app.state == "choice")
app.go_home(); app._on_drop(types.SimpleNamespace(data=extra, action="copy"))
check("dropped plain file -> compress flow", app.state == "compress_choice")

# ---- error safety net ----
app.go_home(); messagebox.showerror.reset_mock()
app.q.put(("listing_done",)); app.state = "processing"; app._poll()
check("bad UI message doesn't wedge the app", messagebox.showerror.called and app.state == "home")

print("\nUI SMOKE RESULT: %d/%d" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
