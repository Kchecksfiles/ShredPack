"""Tests for (1) the Explorer right-click menu registration and (2) Test Archive on ZIP files.

Menu registration is exercised against an in-memory stand-in for the registry
(sp.set_registry), so this never touches the real Windows registry - on any OS.
What it cannot check is how Explorer itself renders the menus; see the notes in README.

Standard library only. Run:  python tests/test_menu_and_zip.py
"""
import os
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
    (SFA + ".7z\\shell\\ShredPack", {"MUIVerb": "Other Tool"}),                 # a spot we'd install into
    (SFA + ".7z\\shell\\ShredPack\\command", {"": '"C:\\othertool.exe" "%1"'}),
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


def effective_menus(reg, target):
    """The top-level 'ShredPack' menus Explorer would show for an extension ('.zip') or 'FOLDER'.
    Returns a list of menus, each a list of (label, command)."""
    paths = [sp.compress_class_key("Directory")] if target == "FOLDER" \
        else [sp.ext_key(target), sp.compress_class_key("*")]
    menus = []
    for path in paths:
        if not reg.key_exists(path) or reg.get_value(path, "MUIVerb") != "ShredPack":
            continue
        if target != "FOLDER" and not aqs_matches(reg.get_value(path, "AppliesTo"), target):
            continue
        shell = path + "\\shell\\"
        menus.append([(reg.get_value(shell + s), reg.get_value(shell + s + "\\command"))
                      for s in sorted(reg.subkeys(path + "\\shell"))])
    return menus


ARCHIVE_LABELS = ["Extract files...", "Extract Here", "Test archive", "Add to ZIP", "Add to archive..."]
COMPRESS_LABELS = ["Add to ZIP", "Add to archive..."]

reg = FakeRegistry()
sp.set_registry(reg)
check("tests use the fake registry, not the real one", isinstance(sp.registry(), FakeRegistry))

# ---- fresh install ----
seed_unrelated(reg)
before_unrelated = unrelated_snapshot(reg)
warnings = sp.install_menu()
check("install state is current after install", sp.install_state() == "current")
for ext in sp.registry_extensions():
    menus = effective_menus(reg, ext)
    if not (len(menus) == 1 and [l for l, _c in menus[0]] == ARCHIVE_LABELS):
        check("archive %s shows exactly one menu with the 5 commands (got %r)" % (
            ext, [[l for l, _c in m] for m in menus]), False)
        break
else:
    check("every archive type shows ONE ShredPack menu with all 5 commands in order", True)
for ext in (".txt", ".exe", ".docx", ".png"):
    menus = effective_menus(reg, ext)
    check("ordinary file %s: one menu, compression options only" % ext,
          len(menus) == 1 and [l for l, _c in menus[0]] == COMPRESS_LABELS)
menus = effective_menus(reg, "FOLDER")
check("folder: one menu, compression options retained",
      len(menus) == 1 and [l for l, _c in menus[0]] == COMPRESS_LABELS)
check("the generic '*' menu is hidden on archives via AppliesTo",
      all(e in reg.get_value(sp.compress_class_key("*"), "AppliesTo") for e in (".zip", ".7z", ".tar", ".iso")))
check("no AppliesTo on the folder menu", reg.get_value(sp.compress_class_key("Directory"), "AppliesTo") is None)

# ---- command-line modes preserved ----
expected_modes = {label: mode for _n, label, mode in sp.ARCHIVE_MENU_ITEMS}
check("modes are the existing ones",
      set(expected_modes.values()) == {"dialog", "here", "test", "compress-quick", "compress-dialog"})
launcher = sp.launcher_command()
bad = [(l, c) for l, c in effective_menus(reg, ".zip")[0]
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
leftover = [k for k in reg.snapshot() if k.lower().endswith("\\shell\\shredpack") and "7z" not in k.lower()
            and "directory" not in k.lower()]
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
    tops = [sp.ext_key(e) for e in sp.registry_extensions()] + [sp.compress_class_key("*")]
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
    check("%s: archives show ONE menu with 5 commands" % name,
          [[l for l, _c in m] for m in effective_menus(r2, ".zip")] == [ARCHIVE_LABELS])
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
      [[l for l, _c in m] for m in effective_menus(r3, ".zip")] == [ARCHIVE_LABELS])
check("folders still get their menu", len(effective_menus(r3, "FOLDER")) == 1)
sp.uninstall_menu()
check("uninstall leaves the foreign keys alone", unrelated_snapshot(r3, True) == foreign_before)
sp.set_registry(reg)

# ---- fallback switch ----
sp.GENERIC_MENU_EXCLUDES_ARCHIVES = False
sp.install_menu()
check("switch off: '*' menu carries no AppliesTo", reg.get_value(sp.compress_class_key("*"), "AppliesTo") is None)
check("switch off: ordinary files still get the menu", len(effective_menus(reg, ".txt")) == 1)
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
      len(events) >= 10 and all(a[0] <= b[0] for a, b in zip(events, events[1:])))
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

tempfile.tempdir = None
shutil.rmtree(WORK, ignore_errors=True)

print("\nRESULT: %d/%d checks passed%s" % (sum(ok), len(ok), (", %d skipped" % len(skipped)) if skipped else ""))
sys.exit(0 if all(ok) else 1)
