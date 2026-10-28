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
SETTINGS_DIR = tempfile.mkdtemp(prefix="shredpack_settings_")   # never touch the real settings
sp.settings_path = lambda: os.path.join(SETTINGS_DIR, "settings.json")

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
app.settings["quick_here"] = False          # the confirmation screen is optional now
app.open_archives([z], "here")
check("choice screen reached", app.state == "choice")
app.begin(); pump(app, {"finished"})
check("extract here -> named folder", sorted(os.listdir(os.path.join(d, "a"))) == ["sub", "x.txt"])
check("done screen shown", app.state == "finished")
app.settings["quick_here"] = True

# ---- instant Extract Here (the new default) ----
check("instant Extract Here is on by default", sp.SETTINGS_DEFAULTS["quick_here"] is True)
d = fresh("quick"); z = os.path.join(d, "q.zip"); make_zip(z, {"a.txt": "A"})
app.go_home(); app.open_archives([z], "here")
check("Extract Here starts at once (no confirmation screen)", app.state == "processing")
pump(app, {"finished"})
check("...into the folder named after the archive", os.listdir(os.path.join(d, "q")) == ["a.txt"])
app.go_home(); app.open_archives([z], "dialog")
check("Extract files... still shows its dialog", app.state == "choice")
check("...which summarises the saved options", "Delete original: no" in str(app.lbl_opts.config.call_args))

# saved settings are honoured by the instant path
app.settings.update(delete_after=True, overwrite="skip")
d = fresh("quickdel"); z = os.path.join(d, "k.zip"); make_zip(z, {"a.txt": "A"})
app.go_home(); app.open_archives([z], "here"); pump(app, {"finished"})
check("delete-after setting applied", not os.path.exists(z) and os.path.isdir(os.path.join(d, "k")))
check("overwrite setting reaches the dialog variables", app.overwrite_var.get() == dict(sp.OVERWRITE_POLICIES)["skip"])
app.settings.update(delete_after=False, overwrite="rename")

# declining a safety warning during an instant extract returns to the confirmation screen
d = fresh("quickwarn"); z = os.path.join(d, "w.zip"); make_zip(z, {"a.txt": "A"})
messagebox.askyesno.reset_mock(); messagebox.askyesno.return_value = False
with mock.patch.object(sp, "preflight_risk", return_value="Test warning: looks risky"):
    app.go_home(); app.open_archives([z], "here")
check("declined warning: nothing extracted, back on the confirmation screen",
      app.state == "choice" and not os.path.exists(os.path.join(d, "w")))
check("...and the warning was shown", messagebox.askyesno.called)
messagebox.askyesno.return_value = True

# ---- settings screen + storage ----
check("settings default to the documented values", sp.load_settings() == sp.SETTINGS_DEFAULTS)
app.go_home(); app.open_settings()
check("Settings opens from home", app.state == "settings")
app.set_quick_var.set(False); app.set_delete_var.set(True); app.set_sound_var.set(False)
app.set_overwrite_var.set(dict(sp.OVERWRITE_POLICIES)["newer"]); app._settings_changed()
saved = sp.load_settings()
check("changes are saved to disk at once", saved == {"quick_here": False, "delete_after": True,
                                                       "overwrite": "newer", "sound": False})
app.close_settings()
check("Done returns home", app.state == "home")
app.settings = sp.load_settings()
d = fresh("setchoice"); z = os.path.join(d, "c.zip"); make_zip(z, {"a.txt": "A"})
app.open_archives([z], "here")
check("quick off -> Extract Here shows the confirmation screen again", app.state == "choice")
check("...pre-filled from Settings", app.delete_var.get() is True and
      app.overwrite_var.get() == dict(sp.OVERWRITE_POLICIES)["newer"])
app.open_settings(); app.set_delete_var.set(False); app.close_settings()
check("Settings opened from the choice screen returns to it", app.state == "choice" and app.delete_var.get() is False)
check("Escape closes Settings", (app.open_settings(), app.on_escape(), app.state)[2] == "choice")
open(sp.settings_path(), "w").write("{ not json")
check("a damaged settings file falls back to defaults", sp.load_settings() == sp.SETTINGS_DEFAULTS)
open(sp.settings_path(), "w").write('{"quick_here": "yes", "overwrite": "explode", "sound": false}')
check("invalid values are ignored, valid ones kept", sp.load_settings() == dict(sp.SETTINGS_DEFAULTS, sound=False))
os.remove(sp.settings_path()); app.settings = dict(sp.SETTINGS_DEFAULTS)

# ---- completion sounds ----
import wave
for style in ("glass", "pop", "chord", "error"):
    data, ms = sp.synth_sound(style)
    w = wave.open(io.BytesIO(data))
    check("sound '%s' is a valid WAV, %d ms" % (style, ms),
          w.getnchannels() == 1 and w.getframerate() == 44100 and abs(w.getnframes() / 44100.0 * 1000 - ms) < 2)
    raw = w.readframes(w.getnframes())
    import struct as _st
    samples = _st.unpack("<%dh" % w.getnframes(), raw)
    check("sound '%s': quiet (peak %.0f%%), starts and ends at silence" % (style, 100.0 * max(map(abs, samples)) / 32767),
          max(map(abs, samples)) / 32767.0 < 0.40 and samples[0] == 0 and samples[-1] == 0)
check("play_sound is a silent no-op where winsound doesn't exist", sp.play_sound("done") == 0)

# Windows path, with a fake winsound: it must NOT combine SND_MEMORY with SND_ASYNC (that raises
# RuntimeError on real Windows) - the in-memory WAV plays synchronously on a background thread.
calls = []
fake_ws = types.SimpleNamespace(SND_MEMORY=4, SND_ASYNC=1, PlaySound=lambda data, flags: calls.append((data, flags)))
with mock.patch.dict(sys.modules, {"winsound": fake_ws}), mock.patch.object(sp.sys, "platform", "win32"):
    sp._sound_cache.clear()
    ms = sp.play_sound("done")
    deadline = time.time() + 5
    while not calls and time.time() < deadline: time.sleep(0.01)
check("play_sound (Windows path) returns the chime length and plays from memory",
      ms > 300 and len(calls) == 1 and calls[0][0][:4] == b"RIFF" and calls[0][1] == fake_ws.SND_MEMORY)
sp._sound_cache.clear()

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

# ---- Test archive: Cancel must return the UI to a usable state; messages must be clear ----
import tempfile as _tempfile
SCRATCH = fresh("scratch")
_tempfile.tempdir = SCRATCH          # so leftover temp files from a cancelled run would be visible


def pump_until(app, kinds, on_msg=None, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try: msg = app.q.get(timeout=0.5)
        except queue.Empty: continue
        app._handle(msg)
        if on_msg: on_msg(msg)
        if msg[0] in kinds: return msg
    raise TimeoutError("waiting for %s (state=%s)" % (kinds, app.state))


d = fresh("bigtest"); big = os.path.join(d, "big.zip")
with zipfile.ZipFile(big, "w", zipfile.ZIP_DEFLATED) as zf:
    with zf.open("huge.bin", "w", force_zip64=True) as w:
        block = b"\0" * (1 << 20)
        for _ in range(600): w.write(block)          # 600 MiB once unpacked
app.go_home(); app.open_archives([big], "dialog")
messagebox.showinfo.reset_mock(); messagebox.showerror.reset_mock()
app.test_selected()
check("UI: a Test run is in progress", app.state == "processing" and app.action == "test")
mark = {}
def cancel_at_10(msg):
    if msg[0] == "progress" and msg[1] >= 10 and "t" not in mark:
        mark["t"] = time.monotonic(); app.on_cancel()          # the Cancel button
pump_until(app, {"test_finished"}, cancel_at_10)
latency = time.monotonic() - mark["t"]
check("UI: Cancel during a large Test returns to the options screen", app.state == "choice")
check("UI: ...within a second (%.0f ms)" % (latency * 1000), latency < 1.0)
check("UI: ...with no result popup", not messagebox.showinfo.called and not messagebox.showerror.called)
check("UI: ...after showing 'Cancelling'", "Cancelling" in str(app.lbl_p_sub.config.call_args_list))
check("UI: ...and no temp files left", os.listdir(SCRATCH) == [])
app.go_home(); app.open_archives([good], "dialog"); app.test_selected(); pump(app, {"test_finished"})
check("UI: the next Test works normally (cancel flag was reset)",
      messagebox.showinfo.called and "No errors" in messagebox.showinfo.call_args[0][1])

# cancelling while a split archive is being joined, then checking nothing is left behind
d = fresh("splitui"); base = os.path.join(d, "s.zip")
with zipfile.ZipFile(base, "w", zipfile.ZIP_STORED) as zf:
    zf.writestr("r.bin", os.urandom(60 * (1 << 20)))
whole = open(base, "rb").read(); os.remove(base)
step = len(whole) // 3 + 1
parts = []
for i in range(3):
    p = os.path.join(d, "s.zip.%03d" % (i + 1)); open(p, "wb").write(whole[i * step:(i + 1) * step]); parts.append(p)
old_chunk, sp.CHUNK = sp.CHUNK, 16 * 1024
messagebox.showinfo.reset_mock(); messagebox.showerror.reset_mock()
app.go_home(); app.open_archives([parts[0]], "dialog"); app.test_selected()
fired = {}
def cancel_first(msg):
    if msg[0] == "progress" and "t" not in fired:
        fired["t"] = 1; app.on_cancel()
try:
    pump_until(app, {"test_finished"}, cancel_first)
finally:
    sp.CHUNK = old_chunk
check("UI: cancelling a split-archive test returns to the options screen", app.state == "choice")
check("UI: ...and removes the temporary joined file",
      not [n for n in os.listdir(SCRATCH) if n.startswith(".shredpack_join_")])
check("UI: ...with no popup", not messagebox.showinfo.called and not messagebox.showerror.called)

# clear messages
ez = os.path.join(fresh("emptyui"), "e.zip"); zipfile.ZipFile(ez, "w").close()
messagebox.showinfo.reset_mock()
app.go_home(); app.open_archives([ez], "dialog"); app.test_selected(); pump(app, {"test_finished"})
check("UI: testing an empty ZIP says so instead of failing",
      messagebox.showinfo.called and "empty" in messagebox.showinfo.call_args[0][1])
messagebox.showerror.reset_mock()
app.go_home(); app.open_archives([bad], "dialog"); app.test_selected(); pump(app, {"test_finished"})
txt = messagebox.showerror.call_args[0][1] if messagebox.showerror.called else ""
check("UI: a corrupted entry is named, with a CRC explanation", "one.bin" in txt and "CRC" in txt)
check("UI: ...and the app stays usable", app.state == "choice")
_tempfile.tempdir = None

print("\nUI SMOKE RESULT: %d/%d" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
