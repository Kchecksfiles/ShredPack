"""Tests for (1) the Explorer right-click menu registration and (2) Test Archive on ZIP files.

Menu registration is exercised against an in-memory stand-in for the registry
(sp.set_registry), so this never touches the real Windows registry - on any OS.
What it cannot check is how Explorer itself renders the menus; see the notes in README.

Standard library only. Run:  python tests/test_menu_and_zip.py
"""
import os, tarfile, io
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import zipfile

try:
    import tkinter  # noqa: F401
except ImportError:  # headless machine: the engine never touches the GUI
    for _n in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox", "tkinter.simpledialog"):
        sys.modules[_n] = types.ModuleType(_n)
    sys.modules["tkinter"].ttk = sys.modules["tkinter.ttk"]
    sys.modules["tkinter"].filedialog = sys.modules["tkinter.filedialog"]
    sys.modules["tkinter"].messagebox = sys.modules["tkinter.messagebox"]
    sys.modules["tkinter"].simpledialog = sys.modules["tkinter.simpledialog"]
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import shredpack as sp  # noqa: E402

sp.log_write = lambda *a, **k: None       # keep the developer's real log untouched

ok, skipped = [], []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    ok.append(bool(cond))


def skip(label):
    print("SKIP " + label)
    skipped.append(label)


# ======================================================================
# Part 1: right-click menu registration, against a fake registry
# ======================================================================
class FakeRegistry(object):
    """In-memory stand-in for sp._WinRegistry (case-insensitive paths like Windows)."""

    def __init__(self):
        self.keys = {}          # lower path -> {"name": path, "values": {lower name: (name, value)}}
        self.notified = 0

    def set_values(self, path, values):
        parts = path.split("\\")
        for i in range(1, len(parts) + 1):
            p = "\\".join(parts[:i])
            self.keys.setdefault(p.lower(), {"name": p, "values": {}})
        self.keys[path.lower()]["values"].update({n.lower(): (n, v) for n, v in values.items()})

    def get_value(self, path, name=""):
        key = self.keys.get(path.lower())
        entry = key["values"].get(name.lower()) if key else None
        return entry[1] if entry else None

    def key_exists(self, path):
        return path.lower() in self.keys

    def subkeys(self, path):
        prefix = path.lower() + "\\"
        return [k["name"][len(path) + 1:] for p, k in self.keys.items()
                if p.startswith(prefix) and "\\" not in p[len(prefix):]]

    def delete_key(self, path):
        if self.subkeys(path):                       # like RegDeleteKey: refuses if subkeys remain
            raise OSError("key has subkeys: " + path)
        self.keys.pop(path.lower(), None)

    def notify(self):
        self.notified += 1

    def snapshot(self):
        return {k["name"]: {n: v for n, v in k["values"].values()} for k in self.keys.values()}


class NeverRealRegistry(object):
    def __init__(self, *a, **k):
        raise AssertionError("the real registry class was instantiated during a test")


REAL_CLASS = sp._WinRegistry
sp._WinRegistry = NeverRealRegistry          # any accidental use of the real registry fails loudly

CLS = "Software\\Classes\\"
SFA = CLS + "SystemFileAssociations\\"


def seed_cascade(reg, base, items, top_extra=None):
    top = {"MUIVerb": "ShredPack", "SubCommands": ""}
    top.update(top_extra or {})
    reg.set_values(base, top)
    for name, label, cmd in items:
        reg.set_values(base + "\\shell\\" + name, {"": label})
        reg.set_values(base + "\\shell\\" + name + "\\command", {"": cmd})


def seed_v110(reg):
    """The layout released in v1.1.0: two separate 'ShredPack' cascades on an archive."""
    cmd = '"C:\\old\\ShredPack.exe" --mode %s "%%1"'
    for ext in sp.EXTENSIONS:                    # note: includes the useless '.zip.001' key
        seed_cascade(reg, SFA + ext + "\\shell\\ShredPack",
                     [("1extract", "Extract files...", cmd % "dialog"),
                      ("2here", "Extract Here", cmd % "here"),
                      ("3test", "Test archive", cmd % "test")])
    for cls in ("*", "Directory"):
        seed_cascade(reg, CLS + cls + "\\shell\\ShredPackCompress",
                     [("1addzip", "Add to ZIP", cmd % "compress-quick"),
                      ("2addarchive", "Add to archive...", cmd % "compress-dialog")])


def seed_v10x(reg):
    cmd = '"C:\\old\\ShredPack.exe" --mode %s "%%1"'
    for ext in (".zip", ".7z", ".tar"):
        seed_cascade(reg, SFA + ext + "\\shell\\ShredPack",
                     [("1extract", "Extract files...", cmd % "dialog"),
                      ("2here", "Extract Here", cmd % "here"),
                      ("3folder", "Extract to folder named after the archive", cmd % "folder")])


def seed_first_layout(reg):
    """The very first version: one plain entry per extension, no cascade."""
    for ext in (".zip", ".rar", ".7z"):
        base = SFA + ext + "\\shell\\ShredPack"
        reg.set_values(base, {"": "Extract with ShredPack..."})
        reg.set_values(base + "\\command", {"": '"C:\\old\\ShredPack.exe" "%1"'})


UNRELATED = [
    (CLS + "*\\shell\\OtherApp", {"MUIVerb": "OtherApp"}),
    (CLS + "*\\shell\\OtherApp\\command", {"": '"C:\\other.exe" "%1"'}),
    (SFA + ".zip\\shell\\7-Zip", {"MUIVerb": "7-Zip"}),
    (SFA + ".zip\\shell\\7-Zip\\command", {"": '"C:\\7z.exe" x "%1"'}),
    (CLS + "Directory\\shell\\git_shell", {"MUIVerb": "Git Bash Here"}),
    (CLS + "Directory\\shell\\git_shell\\command", {"": '"C:\\git.exe"'}),
    (CLS + ".zip", {"": "CompressedFolder"}),
]

# Keys that merely share ShredPack's names but belong to something else.
COLLISIONS = [
    (SFA + ".7z\\shell\\ShredPack4Open", {"MUIVerb": "Other Tool"}),            # a spot we'd install into
    (SFA + ".7z\\shell\\ShredPack4Open\\command", {"": '"C:\\othertool.exe" "%1"'}),
    (SFA + ".7z\\shell\\ShredPack", {"MUIVerb": "Another Tool"}),               # the old v1.1 position
    (SFA + ".7z\\shell\\ShredPack\\command", {"": '"C:\\another.exe" "%1"'}),
    (CLS + "Directory\\shell\\ShredPackCompress", {"MUIVerb": "Not Ours"}),     # the old v1.1.0 name
    (CLS + "Directory\\shell\\ShredPackCompress\\command", {"": '"C:\\notours.exe"'}),
]


def seed_unrelated(reg):
    for path, values in UNRELATED:
        reg.set_values(path, values)


def unrelated_snapshot(reg, include_collisions=False):
    """Values of the other programs' keys - the ones that must never change."""
    wanted = {p.lower() for p, _v in UNRELATED + (COLLISIONS if include_collisions else [])}
    return {k: v for k, v in reg.snapshot().items() if k.lower() in wanted}


def aqs_matches(applies_to, ext):
    """Minimal evaluator for the one AppliesTo shape ShredPack writes: NOT (ext OR ext ...).
    (A stand-in for Explorer - it can't tell us whether Explorer agrees.)"""
    if not applies_to:
        return True
    exts = re.findall(r'System\.FileExtension:="(\.[^"]+)"', applies_to)
    if applies_to.startswith("NOT ("):
        return ext.lower() not in exts
    return ext.lower() in exts


def effective_entries(reg, target):
    """What Explorer would list from ShredPack for an extension ('.zip') or 'FOLDER', in the
    order of the registry key names. Each entry is ('item', label, command) or
    ('submenu', label, [(label, command), ...])."""
    shells = [CLS + "Directory\\shell"] if target == "FOLDER" \
        else [SFA + target + "\\shell", CLS + "*\\shell"]
    entries = []
    for shell in shells:
        for name in sorted(reg.subkeys(shell), key=str.lower):
            path = shell + "\\" + name
            ours = reg.get_value(path, "ShredPackItem") is not None \
                or reg.get_value(path, "MUIVerb") in ("ShredPack", "Open with ShredPack")
            if not ours:
                continue
            if target != "FOLDER" and not aqs_matches(reg.get_value(path, "AppliesTo"), target):
                continue
            label = reg.get_value(path, "MUIVerb") or reg.get_value(path, "")
            if reg.get_value(path, "SubCommands") is not None:
                subs = [(reg.get_value(path + "\\shell\\" + n), reg.get_value(path + "\\shell\\" + n + "\\command"))
                        for n in sorted(reg.subkeys(path + "\\shell"))]
                entries.append(("submenu", label, subs))
            else:
                entries.append(("item", label, reg.get_value(path + "\\command")))
    return entries


def labels_of(entries):
    """Flatten entries to ['Extract files...', ..., ('Open with ShredPack', ['Test archive', ...])]."""
    return [e[1] if e[0] == "item" else (e[1], [l for l, _c in e[2]]) for e in entries]


def commands_of(entries):
    out = {}
    for e in entries:
        if e[0] == "item":
            out[e[1]] = e[2]
        else:
            out.update({l: c for l, c in e[2]})
    return out


ARCHIVE_LABELS = ["Extract files...", "Extract Here", "Extract to <archive name>\\",
                  ("Open with ShredPack", ["Extract && Free Space...", "Test archive", "Add to ZIP",
                                           "Add to archive..."])]
COMPRESS_LABELS = [("Open with ShredPack", ["Add to ZIP", "Add to archive..."])]

reg = FakeRegistry()
sp.set_registry(reg)
check("tests use the fake registry, not the real one", isinstance(sp.registry(), FakeRegistry))

# ---- fresh install ----
seed_unrelated(reg)
before_unrelated = unrelated_snapshot(reg)
warnings = sp.install_menu()
check("install state is current after install", sp.install_state() == "current")
for ext in sp.registry_extensions():
    got = labels_of(effective_entries(reg, ext))
    if got != ARCHIVE_LABELS:
        check("archive %s shows the 3 direct items + the submenu (got %r)" % (ext, got), False)
        break
else:
    check("every archive type shows 3 direct items, then ONE 'Open with ShredPack' submenu", True)
check(".rar archives are registered", ".rar" in sp.registry_extensions())
for ext in (".txt", ".exe", ".docx", ".png"):
    check("ordinary file %s: only the compression submenu" % ext,
          labels_of(effective_entries(reg, ext)) == COMPRESS_LABELS)
check("folder: only the compression submenu",
      labels_of(effective_entries(reg, "FOLDER")) == COMPRESS_LABELS)
check("the generic '*' menu is hidden on archives via AppliesTo",
      all(e in reg.get_value(sp.compress_class_key("*"), "AppliesTo") for e in (".zip", ".7z", ".tar", ".iso")))
check("no AppliesTo on the folder menu", reg.get_value(sp.compress_class_key("Directory"), "AppliesTo") is None)

# ---- command-line modes preserved ----
expected_modes = {label: mode for _n, label, mode in sp.ARCHIVE_MENU_ITEMS}
check("modes are the existing ones plus Extract & Free Space",
      set(expected_modes.values()) == {"dialog", "here", "folder", "free", "test", "compress-quick",
                                       "compress-dialog"})
launcher = sp.launcher_command()
bad = [(l, c) for l, c in commands_of(effective_entries(reg, ".zip")).items()
       if c != '%s --mode %s "%%1"' % (launcher, expected_modes[l])]
check("every command launches the app with its --mode and the clicked file", not bad)
check("a mode string round-trips through menu_command", sp.menu_command("test").endswith('--mode test "%1"'))

# ---- unrelated keys untouched; same-named foreign keys neither deleted nor overwritten ----
check("unrelated keys untouched by install", unrelated_snapshot(reg) == before_unrelated)
check("a clean install has no warnings", warnings == [])
check("the shell was told to refresh", reg.notified >= 1)

# ---- repeated installation is idempotent ----
snap1 = reg.snapshot()
for _ in range(3):
    sp.install_menu()
check("installing again 3 times changes nothing", reg.snapshot() == snap1)
check("state stays current", sp.install_state() == "current")

# ---- removal ----
sp.uninstall_menu()
check("uninstall leaves only unrelated keys (plus their parents)", unrelated_snapshot(reg) == before_unrelated)
check("state is none after uninstall", sp.install_state() == "none")
leftover = [k for k in reg.snapshot() if "\\shell\\shredpack" in k.lower()]
check("no ShredPack keys remain", not leftover)
sp.uninstall_menu()
check("uninstalling twice is harmless", unrelated_snapshot(reg) == before_unrelated)

# ---- icon ----
sp.install_menu()
check("not frozen: no Icon value", reg.get_value(sp.ext_key(".zip"), "Icon") is None)
sp.uninstall_menu()
sys.frozen = True
try:
    sp.install_menu()
    want = '"%s",0' % sys.executable
    tops = [sp.ext_key(e) for e in sp.registry_extensions()] + [sp.compress_class_key("*")] \
        + [sp.ext_verb_key(e, n) for e in sp.registry_extensions() for n, _l, _m in sp.DIRECT_MENU_ITEMS]
    check("frozen build: every cascade keeps the app icon",
          all(reg.get_value(p, "Icon") == want for p in tops if reg.key_exists(p)))
    check("icon is part of the 'current' state", sp.install_state() == "current")
finally:
    del sys.frozen
check("state is stale if the icon setting differs", sp.install_state() == "stale")
sp.install_menu()
check("reinstall fixes it", sp.install_state() == "current")
sp.uninstall_menu()

# ---- migration from every earlier layout ----
for name, seeder in (("v1.1.0 (two cascades)", seed_v110), ("v1.0.x (cascade + folder item)", seed_v10x),
                     ("first version (single entry)", seed_first_layout)):
    r2 = FakeRegistry()
    sp.set_registry(r2)
    seed_unrelated(r2)
    base_unrelated = unrelated_snapshot(r2)
    seeder(r2)
    check("%s detected as stale" % name, sp.install_state() == "stale")
    sp.install_menu()
    check("%s -> migrated to current" % name, sp.install_state() == "current")
    check("%s: archives show the new layout" % name,
          labels_of(effective_entries(r2, ".zip")) == ARCHIVE_LABELS)
    check("%s: no old ShredPackCompress key left" % name,
          not r2.key_exists(CLS + "*\\shell\\ShredPackCompress"))
    check("%s: no ShredPack key left under '.zip.001'" % name,
          not r2.key_exists(SFA + ".zip.001\\shell\\ShredPack"))
    check("%s: no old 'Extract to folder' item left" % name,
          not any(k.lower().endswith("\\3folder") for k in r2.snapshot()))
    check("%s: unrelated keys survived" % name, unrelated_snapshot(r2) == base_unrelated)
    sp.uninstall_menu()
    check("%s: removal leaves only unrelated keys" % name, unrelated_snapshot(r2) == base_unrelated
          and sp.install_state() == "none")
sp.set_registry(reg)

# ---- another program's keys that share our names ----
r3 = FakeRegistry()
sp.set_registry(r3)
seed_unrelated(r3)
for path, values in COLLISIONS:
    r3.set_values(path, values)
foreign_before = unrelated_snapshot(r3, True)
warn = sp.install_menu()
check("foreign key in a spot we'd use: left untouched", unrelated_snapshot(r3, True) == foreign_before)
check("...and reported as skipped", any(".7z" in w for w in warn))
check("the old-name foreign key is ignored, not reported", not any("ShredPackCompress" in w for w in warn))
check("a skipped spot does not make the menu 'stale' forever", sp.install_state() == "current")
check("other archive types still get their menu",
      labels_of(effective_entries(r3, ".zip")) == ARCHIVE_LABELS)
check("folders still get their menu", len(effective_entries(r3, "FOLDER")) == 1)
sp.uninstall_menu()
check("uninstall leaves the foreign keys alone", unrelated_snapshot(r3, True) == foreign_before)
sp.set_registry(reg)

# ---- fallback switch ----
sp.GENERIC_MENU_EXCLUDES_ARCHIVES = False
sp.install_menu()
check("switch off: '*' menu carries no AppliesTo", reg.get_value(sp.compress_class_key("*"), "AppliesTo") is None)
check("switch off: ordinary files still get the menu", len(effective_entries(reg, ".txt")) == 1)
sp.GENERIC_MENU_EXCLUDES_ARCHIVES = True
sp.uninstall_menu()

# ---- the --install / --uninstall command-line modes ----
saved_argv, saved_hook = sys.argv, sys.excepthook
try:
    sys.argv = ["shredpack", "--install"]
    sp.main()
    check("--install registers the menu", sp.install_state() == "current")
    sys.argv = ["shredpack", "--uninstall"]
    sp.main()
    check("--uninstall removes it", sp.install_state() == "none")
finally:
    sys.argv, sys.excepthook = saved_argv, saved_hook
sp.set_registry(None)
sp._WinRegistry = REAL_CLASS
check("without an override and off Windows there is no registry", sys.platform == "win32" or sp.registry() is None)

# ======================================================================
# Part 2: Test Archive on ZIP files
# ======================================================================
WORK = tempfile.mkdtemp(prefix="shredpack_zip_tests_")
SCRATCH = os.path.join(WORK, "tmp")
os.makedirs(SCRATCH)
tempfile.tempdir = SCRATCH            # anything the code puts in temp lands here, so leaks are visible


def fresh(name):
    p = os.path.join(WORK, name)
    os.makedirs(p)
    return p


def scratch_clean():
    return os.listdir(SCRATCH) == []


def run_test(path, **kw):
    events = []
    ctx = sp.Ctx(kw.pop("cancel", None) or threading.Event(),
                 lambda p, d, t: events.append((p, d, t)), **kw)
    sp.test_archive(path, ctx)
    return ctx, events


def expect_error(path, exc_type=Exception, **kw):
    try:
        run_test(path, **kw)
    except exc_type as e:
        return e
    return None


# ---- valid ZIP: progress, byte accounting, nothing written ----
d = fresh("valid")
z = os.path.join(d, "ok.zip")
rnd = os.urandom(300000)
with zipfile.ZipFile(z, "w") as zf:
    zf.writestr("docs/", "")
    zf.writestr(zipfile.ZipInfo("docs/a.txt"), "alpha" * 1000, zipfile.ZIP_DEFLATED)
    zf.writestr(zipfile.ZipInfo("b.bin"), rnd, zipfile.ZIP_STORED)
    zf.writestr(zipfile.ZipInfo("empty.txt"), "")
    zf.writestr(zipfile.ZipInfo("__MACOSX/._junk"), "junk")
before = sorted(os.listdir(d))
ctx, events = run_test(z)
total = 5000 + len(rnd) + 4
check("valid zip: every file entry verified (including the junk entry)", ctx.files == 4)
check("valid zip: first progress report is 0%", events[0][0] == 0)
check("valid zip: progress ends at 100% with all bytes counted",
      events[-1][0] == 100.0 and events[-1][1] == total and events[-1][2] == total)
check("valid zip: progress never goes backwards", all(a[0] <= b[0] for a, b in zip(events, events[1:])))
check("valid zip: nothing written next to the archive", sorted(os.listdir(d)) == before)
check("valid zip: no temp files left behind", scratch_clean())

# ---- corrupted entries: clear message naming the entry ----
d = fresh("corrupt")
z = os.path.join(d, "c.zip")
payload = os.urandom(200000)
with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
    zf.writestr("first.bin", os.urandom(1000))
    zf.writestr("folder/second.bin", payload)
with zipfile.ZipFile(z) as zf:
    info = zf.getinfo("folder/second.bin")
    data_start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
raw = bytearray(open(z, "rb").read())
raw[data_start + 100000] ^= 0xFF
open(z, "wb").write(raw)
e = expect_error(z)
check("corrupted stored entry is reported", isinstance(e, sp.ArchiveError))
check("message names the entry and says CRC", e is not None and "folder/second.bin" in str(e) and "CRC" in str(e))
check("corruption: no temp files left behind", scratch_clean())

d = fresh("corrupt2")
z = os.path.join(d, "c2.zip")
with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("good.txt", "fine " * 5000)
    zf.writestr("bad.txt", os.urandom(4000).hex() * 4)
with zipfile.ZipFile(z) as zf:
    info = zf.getinfo("bad.txt")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
raw = bytearray(open(z, "rb").read())
raw[start + info.compress_size // 2] ^= 0xFF
open(z, "wb").write(raw)
e = expect_error(z)
check("corrupted deflate entry is reported by name", isinstance(e, sp.ArchiveError) and "bad.txt" in str(e))

d = fresh("trunc")
z = os.path.join(d, "t.zip")
with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("x.bin", os.urandom(50000))
raw = open(z, "rb").read()
open(z, "wb").write(raw[:len(raw) // 2])
e = expect_error(z)
check("truncated zip: reported cleanly, no crash", e is not None and sp.describe_error(e))

# ---- empty and unusual archives ----
d = fresh("empty")
z = os.path.join(d, "e.zip")
zipfile.ZipFile(z, "w").close()
ctx, events = run_test(z)
check("empty zip: reported as valid but empty", ctx.files == 0 and any("empty" in n for n in ctx.notes))
check("empty zip: no temp files left", scratch_clean())
z = os.path.join(d, "dirs.zip")
with zipfile.ZipFile(z, "w") as zf:
    zf.writestr("only/folders/", "")
ctx, _ = run_test(z)
check("folders-only zip: valid, zero files, noted", ctx.files == 0 and any("no files" in n for n in ctx.notes))
z = os.path.join(d, "zero.zip")
with zipfile.ZipFile(z, "w") as zf:
    for i in range(40):
        zf.writestr("z%d.txt" % i, "")
ctx, events = run_test(z)
check("zero-byte entries: all verified, progress counts entries", ctx.files == 40 and events[-1] == (100.0, 0, 0))

# ---- unsupported compression method ----
d = fresh("method")
z = os.path.join(d, "m.zip")
with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
    zf.writestr("x.txt", "hello")
with zipfile.ZipFile(z) as zf:
    hdr, cd = zf.getinfo("x.txt").header_offset, zf.start_dir
raw = bytearray(open(z, "rb").read())
struct.pack_into("<H", raw, hdr + 8, 98)       # PPMd, which neither zipfile nor ShredPack can read
struct.pack_into("<H", raw, cd + 10, 98)
open(z, "wb").write(raw)
e = expect_error(z)
check("unsupported method: clear message, no crash",
      isinstance(e, sp.ArchiveError) and "x.txt" in str(e) and "98" in str(e) and "PPMd" in str(e))

# ---- encrypted ZIPs ----
d = fresh("enc")
if shutil.which("zip"):
    cwd = os.getcwd()
    os.chdir(d)
    open("s.txt", "w").write("secret " * 100)
    subprocess.run(["zip", "-q", "-P", "right", "e.zip", "s.txt"], check=True)
    os.chdir(cwd)
    z = os.path.join(d, "e.zip")
    e = expect_error(z, sp.NeedPassword)
    check("encrypted, no password: asks for one", isinstance(e, sp.NeedPassword) and not e.retry)
    wrong = [expect_error(z, sp.NeedPassword, password="wrong%d" % i) for i in range(30)]
    check("30 different wrong passwords: always 'try again', never a crash or a pass",
          all(isinstance(w, sp.NeedPassword) and w.retry for w in wrong))
    ctx, _ = run_test(z, password="right")
    check("encrypted, right password: verified", ctx.files == 1)
    check("encrypted: no temp files left", scratch_clean())
else:
    skip("encrypted ZIP tests (the 'zip' program isn't installed)")

# A wrong ZipCrypto password passes the 1-byte check about 1 time in 256 and then decrypts to
# garbage. That has to read as "wrong password, try again" - never as a corrupted archive.
# (Deterministic version of the 30-password loop above, which only hit it by luck.)
import threading as _threading, zlib as _zlib


class _GarbageZip(object):
    def open(self, info, pwd=None):
        raise _zlib.error("Error -3 while decompressing data: invalid block type")


def _verify_entry(flag_bits, pw):
    info = zipfile.ZipInfo("s.txt")
    info.flag_bits = flag_bits
    try:
        sp._verify_zip_entry(_GarbageZip(), info, pw, sp.Ctx(_threading.Event(), lambda p, d, t: None))
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


e = _verify_entry(0x1, b"wrong")
check("encrypted entry + wrong password that decrypts to garbage -> 'try again'",
      isinstance(e, sp.NeedPassword) and e.retry)
e = _verify_entry(0x0, None)
check("a plain (unencrypted) entry with garbage data is still 'corrupted'",
      isinstance(e, sp.ArchiveError) and "corrupted" in str(e))

d = fresh("aes")
z = os.path.join(d, "aes.zip")
zi = zipfile.ZipInfo("a.txt")
zi.extra = struct.pack("<HHHHBH", 0x9901, 7, 2, 0x4541, 3, 8)       # looks like a WinZip AES entry
with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
    zf.writestr(zi, "x")
with zipfile.ZipFile(z) as zf:
    hdr, cd = zf.getinfo("a.txt").header_offset, zf.start_dir
raw = bytearray(open(z, "rb").read())
struct.pack_into("<H", raw, hdr + 6, raw[hdr + 6] | 1)               # set the 'encrypted' flag
struct.pack_into("<H", raw, cd + 8, raw[cd + 8] | 1)
open(z, "wb").write(raw)
if not sp.pyzipper:
    e = expect_error(z, sp.Unsupported, password="x")
    check("AES zip without pyzipper: clear 'not included' message", isinstance(e, sp.Unsupported)
          and "AES" in str(e))
else:
    skip("AES-without-pyzipper test (pyzipper is installed here)")

# ---- large files ----
BIG = os.path.join(WORK, "big_zeros.zip")
t0 = time.time()
with zipfile.ZipFile(BIG, "w", zipfile.ZIP_DEFLATED) as zf:
    with zf.open("huge.bin", "w", force_zip64=True) as w:
        block = b"\0" * (1 << 20)
        for _ in range(800):                      # 800 MiB once unpacked, ~1 MB on disk
            w.write(block)
    zf.writestr("small.txt", "tail")
BIG_BYTES = 800 * (1 << 20) + 4
print("  (built an %d MiB-expanded test zip in %.1fs)" % (BIG_BYTES >> 20, time.time() - t0))

t0 = time.time()
ctx, events = run_test(BIG)
elapsed = time.time() - t0
check("large zip (800 MiB unpacked): verified", ctx.files == 2 and events[-1] == (100.0, BIG_BYTES, BIG_BYTES))
check("large zip: progress reported regularly (%d reports in %.1fs)" % (len(events), elapsed),
      len(events) >= max(3, min(10, int(elapsed / 0.06))) and all(a[0] <= b[0] for a, b in zip(events, events[1:])))
check("large zip: nothing written, no temp files", scratch_clean())

rd = fresh("randbig")
RB = os.path.join(rd, "rand.zip")
with zipfile.ZipFile(RB, "w", zipfile.ZIP_STORED) as zf:
    with zf.open("rand.bin", "w", force_zip64=True) as w:
        for _ in range(96):
            w.write(os.urandom(1 << 20))
ctx, events = run_test(RB)
check("large stored zip (96 MiB incompressible): verified", ctx.files == 1 and events[-1][0] == 100.0)
with zipfile.ZipFile(RB) as zf:
    info = zf.getinfo("rand.bin")
    pos = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra) + 48 * (1 << 20)
raw = bytearray(open(RB, "rb").read())
raw[pos] ^= 0x01
open(RB, "wb").write(raw)
e = expect_error(RB)
check("a single flipped bit deep inside a large entry is caught and named",
      isinstance(e, sp.ArchiveError) and "rand.bin" in str(e) and "CRC" in str(e))

# ---- responsiveness: another thread (standing in for the UI loop) must keep running ----
ev = threading.Event()
res = {}
t0 = time.monotonic()


def worker():
    try:
        sp.test_archive(BIG, sp.Ctx(ev, lambda p, d, t: None))
        res["r"] = "ok"
    except Exception as exc:  # noqa: BLE001
        res["r"] = repr(exc)


th = threading.Thread(target=worker)
th.start()
gaps, last = [], time.monotonic()
while th.is_alive():
    time.sleep(0.01)
    now = time.monotonic()
    gaps.append(now - last)
    last = now
th.join()
check("UI stand-in thread never starved while verifying (worst pause %.0f ms)" % (max(gaps) * 1000),
      res["r"] == "ok" and max(gaps) < 1.0)

# ---- cancellation (each measured from the moment cancel is requested) ----


def cancel_when(pred, path, **kw):
    """Run a test in a worker; a controller thread sets cancel once pred(event) is true.
    -> (exception or None, seconds from cancel request to worker exit, ctx, request happened)"""
    ev = threading.Event()
    events, out = [], {}
    stamp = {}

    def progress(p, d_, t):
        events.append((p, d_, t))
        if "t" not in stamp and pred(p, d_, t):
            stamp["t"] = time.monotonic()
            ev.set()                         # exactly what the Cancel button does (on_cancel)

    ctx = sp.Ctx(ev, progress, **kw)

    def work():
        try:
            sp.test_archive(path, ctx)
            out["exc"] = None
        except Exception as exc:  # noqa: BLE001
            out["exc"] = exc
        out["end"] = time.monotonic()

    th = threading.Thread(target=work)
    th.start()
    th.join(30)
    assert not th.is_alive(), "worker did not stop after cancel (hang)"
    return out["exc"], (out["end"] - stamp["t"]) if "t" in stamp else None, ctx


exc, latency, ctx = cancel_when(lambda p, d_, t: p >= 5, BIG)
check("cancel mid-way through a large zip raises Cancelled", isinstance(exc, sp.Cancelled))
check("cancel stops the worker promptly (%.0f ms)" % ((latency or 9) * 1000), latency is not None and latency < 1.0)
check("cancel really stopped early (%.0f%% done)" % (ctx.done * 100.0 / ctx.total), ctx.done < ctx.total * 0.9)
check("cancel: temp files cleaned up", scratch_clean())

ZD = fresh("manyempty")
ZE = os.path.join(ZD, "many_empty.zip")
with zipfile.ZipFile(ZE, "w", zipfile.ZIP_STORED) as zf:
    for i in range(150000):
        zf.writestr("d%d/f%d.txt" % (i % 300, i), "")
exc, latency, ctx = cancel_when(lambda p, d_, t: True, ZE)
check("cancel works on an archive of 150,000 zero-byte entries (it used to be ignored)",
      isinstance(exc, sp.Cancelled) and ctx.files < 150000)
check("...and stops promptly (%.0f ms)" % ((latency or 9) * 1000), latency is not None and latency < 1.0)
check("cancel (zero-byte): temp files cleaned up", scratch_clean())

# ---- splitting: joining is cancellable too, and cleaned up by the caller ----
SD = fresh("split")
whole = open(RB.replace("rand.zip", "rand.zip"), "rb").read()      # corrupted, but fine for a join test
parts = []
step = len(whole) // 3 + 1
for i in range(3):
    p = os.path.join(SD, "big.zip.%03d" % (i + 1))
    open(p, "wb").write(whole[i * step:(i + 1) * step])
    parts.append(p)
join_dir = tempfile.mkdtemp(prefix=".shredpack_join_")
joined = sp.join_split_parts(parts, join_dir)
check("join without a Ctx still works and is byte-exact", open(joined, "rb").read() == whole)
shutil.rmtree(join_dir)

old_chunk, sp.CHUNK = sp.CHUNK, 64 * 1024                 # many small steps
join_events = []
join_dir = tempfile.mkdtemp(prefix=".shredpack_join_")
sp.join_split_parts(parts, join_dir, sp.Ctx(threading.Event(), lambda p, d_, t_: join_events.append((p, d_, t_))))
shutil.rmtree(join_dir)
check("join reports progress, starting at 0%", len(join_events) >= 2 and join_events[0][0] == 0)
check("join progress ends at 100% with every byte counted",
      join_events[-1] == (100.0, len(whole), len(whole)))


class CancelAfter(sp.Ctx):
    """Behaves like the Cancel button being pressed after N copy steps."""
    def __init__(self, *a, **k):
        sp.Ctx.__init__(self, *a, **k)
        self.calls = 0

    def add(self, n):
        self.calls += 1
        if self.calls == 20:
            self.cancel.set()
        sp.Ctx.add(self, n)


cctx = CancelAfter(threading.Event(), lambda *a: None)
join_dir = tempfile.mkdtemp(prefix=".shredpack_join_")
try:
    sp.join_split_parts(parts, join_dir, cctx)
    raised = False
except sp.Cancelled:
    raised = True
partial = os.path.getsize(os.path.join(join_dir, "joined.zip"))
sp.CHUNK = old_chunk
shutil.rmtree(join_dir, ignore_errors=True)       # what the worker's 'finally' does
check("cancelling while joining stops the copy part-way", raised and cctx.calls == 20 and 0 < partial < len(whole))
check("split join: temp files cleaned up", scratch_clean())


# ======================================================================
# Part 4: RAR extraction (needs the optional unrar-cffi package; skipped without it)
# ======================================================================
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rar_samples import SAMPLES  # noqa: E402

RARD = tempfile.mkdtemp(prefix="shredpack_rar_")


def rar_ctx(password=None, dry=False):
    return sp.Ctx(threading.Event(), lambda *a: None, password, dry_run=dry)


def rar_file(name):
    path = os.path.join(RARD, name)
    with open(path, "wb") as f:
        f.write(SAMPLES[name])
    return path


check("a .rar file is recognised as RAR even though the engine may lack the library",
      sp.detect_format(rar_file("rar5-crc.rar")) == "rar")
check(".rar is a registered extension", ".rar" in sp.EXTENSIONS)
if sp.rar_available():
    arc = rar_file("rar5-crc.rar")
    out = os.path.join(RARD, "out1")
    sp.extract_archive(arc, out, rar_ctx())
    check("RAR5: both files extracted", sorted(os.listdir(out)) == ["stest1.txt", "stest2.txt"]
          and os.path.getsize(os.path.join(out, "stest1.txt")) == 2048)
    c = rar_ctx(dry=True)
    sp.test_archive(arc, c)
    check("RAR: Test Archive verifies every file and writes nothing", c.files == 2)
    check("RAR: browsing lists the files", [e["path"] for e in sp.list_archive(arc)["entries"]]
          == ["stest1.txt", "stest2.txt"])
    arc3 = rar_file("rar3-subdirs.rar")
    out = os.path.join(RARD, "out3")
    sp.extract_archive(arc3, out, rar_ctx())
    got = sorted(os.path.relpath(os.path.join(r, f), out).replace(os.sep, "/")
                 for r, _d, fs in os.walk(out) for f in fs)
    check("RAR3: nested folders and non-ASCII names extracted", len(got) == 4 and "sub/dir1/file1.txt" in got)
    out = os.path.join(RARD, "out_sel")
    c = sp.Ctx(threading.Event(), lambda *a: None, selected={"stest2.txt"})
    sp.extract_archive(arc, out, c)
    check("RAR: extracting a selection only writes that file", os.listdir(out) == ["stest2.txt"])
    pw = rar_file("rar5-psw.rar")
    try:
        sp.extract_archive(pw, os.path.join(RARD, "o_nopw"), rar_ctx())
        asked = None
    except sp.NeedPassword as exc:
        asked = exc.retry
    check("RAR: a password-protected archive asks for the password", asked is False)
    try:
        sp.extract_archive(pw, os.path.join(RARD, "o_badpw"), rar_ctx("wrong"))
        asked = None
    except sp.NeedPassword as exc:
        asked = exc.retry
    check("RAR: a wrong password is reported as wrong (retry)", asked is True)
    out = os.path.join(RARD, "o_pw")
    sp.extract_archive(pw, out, rar_ctx("password"))
    check("RAR: the right password extracts", sorted(os.listdir(out)) == ["stest1.txt", "stest2.txt"])
    check("RAR: no folder is left behind after a failed attempt",
          not os.path.exists(os.path.join(RARD, "o_nopw")) and not os.path.exists(os.path.join(RARD, "o_badpw")))
    try:
        sp.extract_archive(rar_file("rar5-hpsw.rar"), os.path.join(RARD, "o_h"), rar_ctx("password"))
        msg = ""
    except sp.Unsupported as exc:
        msg = str(exc)
    check("RAR: encrypted file NAMES are reported as unsupported, not as a crash", "encrypted file names" in msg)
    for label, blob in (("truncated", SAMPLES["rar5-crc.rar"][:900]),
                        ("damaged", SAMPLES["rar5-crc.rar"][:300] + b"\xff" * 40 + SAMPLES["rar5-crc.rar"][340:]),
                        ("not really a RAR", b"Rar!\x1a\x07\x01\x00" + b"\x00" * 40)):
        bad_path = os.path.join(RARD, "bad.rar")
        with open(bad_path, "wb") as f:
            f.write(blob)
        dest = os.path.join(RARD, "o_bad")
        try:
            sp.extract_archive(bad_path, dest, rar_ctx())
            err = None
        except (sp.ArchiveError, sp.NeedPassword) as exc:
            err = exc
        check("RAR: a %s archive fails cleanly with no partial output" % label,
              isinstance(err, sp.ArchiveError) and not os.path.exists(dest))
    part2 = os.path.join(RARD, "set.part2.rar")
    shutil.copy(arc, part2)
    try:
        sp.extract_archive(part2, os.path.join(RARD, "o_p2"), rar_ctx())
        msg = ""
    except sp.ArchiveError as exc:
        msg = str(exc)
    check("RAR: a later part of a multi-part set says to open the first part", "first part" in msg)
else:
    skip("RAR extraction tests (unrar-cffi isn't installed here)")
    try:
        sp.extract_archive(rar_file("rar5-crc.rar"), os.path.join(RARD, "o_none"), rar_ctx())
        msg = ""
    except sp.Unsupported as exc:
        msg = str(exc)
    check("without the library RAR reports 'not included in this build'", "isn't included" in msg)
shutil.rmtree(RARD, ignore_errors=True)


# ======================================================================
# Part 5: Extract & Free Space - the checks that decide whether the archive may be deleted
# ======================================================================
FS = tempfile.mkdtemp(prefix="shredpack_free_")


def free_case(name, members, policy="rename", filtered=False, pre=None, tamper=None, dest_same_dir=False):
    """Extract a small zip the way the worker does, then apply the Extract & Free Space rules."""
    folder = os.path.join(FS, name)
    os.makedirs(folder)
    arc = os.path.join(folder, "pack.zip")
    with zipfile.ZipFile(arc, "w", zipfile.ZIP_STORED) as zf:
        for n, d in members.items():
            zf.writestr(n, d)
    dest = folder if dest_same_dir else os.path.join(folder, "pack")
    if pre:
        pre(folder, dest)
    before = [sp.file_signature(arc)]
    ctx = sp.Ctx(threading.Event(), lambda *a: None, overwrite=policy)
    outcome = sp.extract_archive(arc, dest, ctx)
    if tamper:
        tamper(arc)
    return arc, sp.free_space_after_extraction([arc], before, ctx, outcome, filtered), dest


arc, res, dest = free_case("ok", {"a.txt": "A" * 100, "sub/b.txt": "B"})
check("free space: a clean full extraction deletes the archive and reports the bytes freed",
      "freed" in res and res["freed"] > 0 and not os.path.exists(arc)
      and sorted(os.listdir(dest)) == ["a.txt", "sub"])
arc, res, dest = free_case("part", {"a.txt": "A"}, filtered=True)
check("free space: a partial extraction keeps the archive", "kept" in res and os.path.exists(arc))
arc, res, dest = free_case("skip", {"a.txt": "A"}, policy="skip",
                           pre=lambda folder, dest: (os.makedirs(dest), open(os.path.join(dest, "a.txt"), "w").write("old")))
check("free space: skipped (already existing) files keep the archive",
      "kept" in res and "skipped" in res["kept"] and os.path.exists(arc))
arc, res, dest = free_case("changed", {"a.txt": "A"}, tamper=lambda a: open(a, "ab").write(b"x"))
check("free space: an archive that changed during extraction is kept",
      "kept" in res and "changed" in res["kept"] and os.path.exists(arc))
arc, res, dest = free_case("empty", {"only/": ""})
check("free space: an archive with nothing to verify is kept", "kept" in res and os.path.exists(arc))
arc, res, dest = free_case("self", {"pack.zip": "I am the new pack.zip"}, policy="overwrite", dest_same_dir=True)
check("free space: if extraction replaced the archive itself it is NOT deleted",
      "kept" in res and "itself" in res["kept"] and os.path.exists(arc))
arc, res, dest = free_case("samedir", {"a.txt": "A"}, dest_same_dir=True)
check("free space: extracting next to the archive is fine when nothing overlaps",
      "freed" in res and not os.path.exists(arc) and os.path.isfile(os.path.join(dest, "a.txt")))

def vanish(a):
    os.remove(a)
    open(a, "wb").write(b"replaced")
arc, res, dest = free_case("swapped", {"a.txt": "A"}, tamper=vanish)
check("free space: a swapped-out archive is kept", "kept" in res and os.path.exists(arc))

real_delete = sp.delete_permanently
sp.delete_permanently = lambda path: "it is read-only or in use by another program"
try:
    arc, res, dest = free_case("indelete", {"a.txt": "A"})
finally:
    sp.delete_permanently = real_delete
check("free space: if deletion fails, the archive AND the extracted files stay and the reason is reported",
      "failed" in res and "in use" in res["failed"] and os.path.exists(arc)
      and os.path.isfile(os.path.join(dest, "a.txt")))

ctx_mv = sp.Ctx(threading.Event(), lambda *a: None)
ctx_mv.files, ctx_mv.multivolume = 1, True
check("free space: a multi-part RAR set is never deleted",
      "multi-part" in (sp.free_space_blocker(["x"], [(1, 1)], ctx_mv, {"dest": FS, "placed": ["a"], "skipped": 0}, False) or ""))
check("free space: normal extraction still returns what it did without deleting anything",
      os.path.exists(arc) and sp.extract_archive.__doc__ and "placed" in sp.extract_archive.__doc__)

# a 7z extraction must count its files, or Extract & Free Space could never verify it
if sp.py7zr:
    import py7zr as _p7
    d7 = os.path.join(FS, "sevenzip"); os.makedirs(d7)
    src7 = os.path.join(d7, "in.txt"); open(src7, "w").write("hello")
    a7 = os.path.join(d7, "t.7z")
    with _p7.SevenZipFile(a7, "w") as z7:
        z7.write(src7, "in.txt")
    c7 = sp.Ctx(threading.Event(), lambda *a: None)
    out7 = sp.extract_archive(a7, os.path.join(d7, "t"), c7)
    check("7z: extracted files are counted (needed to verify before freeing space)",
          c7.files == 1 and len(out7["placed"]) == 1)
else:
    skip("7z file-count test (py7zr isn't installed here)")

# ---- Extract & Free Space: the deletion order and the extra size verification ----
check("free space: the label names the archive without its extension (spaces, Unicode, .tar.gz)",
      [sp.extract_to_label(os.path.join("C:\\x", n)) for n in
       ("ShredPack.zip", "My Documents.zip", "Game Files.7z", "Backup.tar.gz", "Z\u00fcrich \u2013 \u65e5\u672c.tgz", "A.TAR.GZ", "rel.tar.xz")]
      == ["Extract to ShredPack\\", "Extract to My Documents\\", "Extract to Game Files\\",
          "Extract to Backup\\", "Extract to Z\u00fcrich \u2013 \u65e5\u672c\\", "Extract to A\\", "Extract to rel\\"])

def free_case_sizes(name, members, wrong=False):
    folder = os.path.join(FS, name); os.makedirs(folder)
    arc = os.path.join(folder, "pack.zip")
    with zipfile.ZipFile(arc, "w", zipfile.ZIP_STORED) as zf:
        for n, d in members.items(): zf.writestr(n, d)
    expected = sp.declared_file_sizes(arc)
    if wrong: expected = expected[:-1] + [expected[-1] + 1]
    before = [sp.file_signature(arc)]
    ctx = sp.Ctx(threading.Event(), lambda *a: None)
    outcome = sp.extract_archive(arc, os.path.join(folder, "pack"), ctx)
    return arc, sp.free_space_after_extraction([arc], before, ctx, outcome, False, expected)

arc, res = free_case_sizes("sz_ok", {"a.txt": "A" * 10, "b/c.txt": "C" * 3})
check("free space: sizes on disk equal the archive's listing -> archive deleted", "freed" in res and not os.path.exists(arc))
arc, res = free_case_sizes("sz_bad", {"a.txt": "A" * 10, "b/c.txt": "C" * 3}, wrong=True)
check("free space: a size that disagrees with the archive's listing keeps the archive",
      "kept" in res and "size" in res["kept"] and os.path.exists(arc))

# deletion happens only after every check has passed, and never on a failed or cancelled run
calls = []
real_delete = sp.delete_permanently
sp.delete_permanently = lambda path: calls.append(path)
try:
    arc, res, dest = free_case("order_ok", {"a.txt": "A"})
    ok_calls = list(calls); del calls[:]
    free_case("order_part", {"a.txt": "A"}, filtered=True)
    free_case("order_changed", {"a.txt": "A"}, tamper=lambda a: open(a, "ab").write(b"x"))
    free_case_sizes("order_size", {"a.txt": "AA"}, wrong=True)
finally:
    sp.delete_permanently = real_delete
check("free space: delete is called once for a verified extraction and never when a check fails",
      len(ok_calls) == 1 and calls == [])

# a failed extraction (damaged archive) raises before any deletion logic: the archive survives
bad = os.path.join(FS, "badzip.zip")
with zipfile.ZipFile(bad, "w", zipfile.ZIP_STORED) as zf: zf.writestr("a.txt", "A" * 5000)
raw = bytearray(open(bad, "rb").read()); raw[40:60] = b"\xff" * 20; open(bad, "wb").write(bytes(raw))
failed = False
try:
    cx = sp.Ctx(threading.Event(), lambda *a: None)
    sp.extract_archive(bad, os.path.join(FS, "badout"), cx)
except Exception:
    failed = True
check("free space: a damaged archive fails to extract, so the deletion step is never reached, and the file remains",
      failed and os.path.exists(bad))

# a cancelled run leaves the archive and raises Cancelled before verification
cancel_ev = threading.Event(); cancel_ev.set()
okc = os.path.join(FS, "cancelme.zip")
with zipfile.ZipFile(okc, "w", zipfile.ZIP_STORED) as zf: zf.writestr("a.txt", "A" * 5000)
try:
    sp.extract_archive(okc, os.path.join(FS, "cancelout"), sp.Ctx(cancel_ev, lambda *a: None)); cancelled = False
except sp.Cancelled:
    cancelled = True
check("free space: a cancelled extraction raises Cancelled and the archive stays", cancelled and os.path.exists(okc))


# ---- speed: no duplicate scans, and the engine is not slower than the stdlib ----
tgz = os.path.join(FS, "t.tar.gz")
with tarfile.open(tgz, "w:gz") as tf:
    info = tarfile.TarInfo("a.txt"); data = b"x" * 1000; info.size = len(data); tf.addfile(info, io.BytesIO(data))
check("free space: a TAR is not decompressed a second time just to list it", sp.declared_file_sizes(tgz) is None)
SP = os.path.join(FS, "speed.zip")
with zipfile.ZipFile(SP, "w", zipfile.ZIP_DEFLATED) as zf:
    for i in range(300):
        zf.writestr("d%d/f%d.txt" % (i % 10, i), (b"line of text %d\n" % i) * 20000)
t0 = time.time(); zipfile.ZipFile(SP).extractall(os.path.join(FS, "std_out")); t_std = time.time() - t0
t0 = time.time(); sp.extract_archive(SP, os.path.join(FS, "sp_out"), sp.Ctx(threading.Event(), lambda *a: None)); t_sp = time.time() - t0
check("extraction speed: ShredPack %.2fs vs stdlib extractall %.2fs (must stay within 2x)" % (t_sp, t_std),
      t_sp < max(2.0 * t_std, 0.3))
check("extraction speed: output identical to the stdlib's",
      sp._list_all_files(os.path.join(FS, "sp_out")).__len__() == 300)
shutil.rmtree(FS, ignore_errors=True)

tempfile.tempdir = None
shutil.rmtree(WORK, ignore_errors=True)

print("\nRESULT: %d/%d checks passed%s" % (sum(ok), len(ok), (", %d skipped" % len(skipped)) if skipped else ""))
sys.exit(0 if all(ok) else 1)
