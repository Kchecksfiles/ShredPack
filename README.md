# ShredPack

A fast, self-contained archive tool for Windows with a dark, minimal interface.
It reads and writes archives **with its own built-in engine** - it does not need
WinRAR or 7-Zip installed, and it never launches another program to do the work.

ShredPack only runs when you open it or pick one of its right-click options. It
does the job and exits; nothing stays running in the background.

## What it does

- **Extract** - right-click an archive, or drag it onto the window.
  *Extract Here* always unpacks into a new folder named after the archive, so a
  messy archive never spills loose files into the folder you're in.
- **Browse and extract selected files** - open an archive's contents, search
  them, and extract only what you pick. Optional wildcard filters
  (e.g. `*.jpg`) and a "what to do if a file already exists" setting
  (keep both / overwrite / skip / overwrite if newer).
- **Test archive** - verifies the contents (checksums and decompression)
  without writing anything.
- **Create archives** - right-click any file or folder (*Add to ZIP* for a
  one-click `.zip`, or *Add to archive...* to choose format, name, level and
  password), or use *Create Archive...* in the app. Drag files onto the window
  to start.
- **Checksums** - SHA-256, SHA-1, MD5 and CRC32 of any file, with a compare box
  for a published checksum.
- Optional **Delete original after extraction** (moved to the Recycle Bin when
  possible). If you extract only part of an archive, ShredPack asks before
  deleting the original.

## Formats

| Format | Extract | Create | Notes |
|---|---|---|---|
| ZIP | yes | yes | Password-protected (including AES) when `pyzipper` is bundled; split ZIPs (`.z01`/`.zip`, `.zip.001`) |
| 7Z | yes | yes | Needs `py7zr` (bundled in the release build) |
| TAR, TAR.GZ/TGZ, TAR.BZ2, TAR.XZ | yes | yes | |
| GZ, BZ2, XZ (single file) | yes | - | |
| ISO | yes | - | Needs `pycdlib` (bundled in the release build) |
| CAB | yes | - | Stored and MSZIP only (not LZX/Quantum); multi-part cabinets unsupported |
| XAR | yes | - | |

**Not supported:** RAR, WIM, DMG, VHD/VHDX. ShredPack deliberately doesn't depend
on other programs, and there is no free pure-Python RAR extractor.

## Install

Download **`ShredPack-Setup.exe`** from the
[Releases](https://github.com/Kchecksfiles/ShredPack/releases) page and run it.
It installs per user (no administrator rights needed), adds the right-click menu
(optional) and an entry in *Settings > Apps* for uninstalling. A portable
`ShredPack.exe` is published too; run it once and use the *Add to menu* button.
Each release includes `SHA256SUMS.txt` so you can verify your download (the app's
own *Checksum...* button works for that).

On Windows 11 the entries appear under **Show more options** in the right-click menu.

> **Windows SmartScreen:** the builds are not code-signed yet, so Windows may show
> "Windows protected your PC" the first time. Choose *More info > Run anyway* if
> you trust the source (verify the checksum above).

## Safety

- File names are sanitised: `..` and absolute paths, invalid or reserved Windows
  names, and invisible text-direction characters used to disguise `.exe` as
  `.jpg` are neutralised.
- Existing files are never overwritten unless you choose *Overwrite*.
- Extraction happens in a temporary folder first; a failed or cancelled run
  leaves nothing behind.
- Symbolic links in archives are never created (links to a file inside the same
  archive become copies); links and junctions are never followed when creating
  archives.
- Extracted files inherit the archive's "downloaded from the internet" mark, so
  SmartScreen still warns before an extracted download runs.
- Archives that would expand enormously or not fit on the disk trigger a warning
  first.
- Nothing is sent anywhere. On a normal launch (not from the right-click menu) the
  app fetches a small version file from GitHub to show an "update available"
  link. Errors are logged locally to `%APPDATA%\ShredPack\log.txt`.

## Known limitations

- The Explorer menu text is fixed (it can't show the archive's name) and works on
  **one selected item at a time**; combining many items into one archive is done
  in the app with *Add Files... / Add Folder...* or by dragging them in. A
  multi-select, name-aware menu needs a compiled shell extension.
- Executable bits and ownership aren't restored (they don't exist on Windows).
- Browsing split archives, previewing files and a settings screen are not
  implemented yet.

## Build from source

```
pip install py7zr pycdlib pyzipper tkinterdnd2 send2trash pyinstaller
pyinstaller --noconsole --onefile --icon=shredpack.ico --add-data "shredpack.ico;." ^
  --collect-all py7zr --collect-all pycdlib --collect-all pyzipper --collect-all tkinterdnd2 ^
  --name ShredPack shredpack.py
```

The installer is built from `setup.iss` with [Inno Setup](https://jrsoftware.org/isinfo.php).
The GitHub workflow does all of this; push a tag like `v1.1.0` (it must match
`APP_VERSION` in `shredpack.py`) to publish a release, or use *Actions > Run workflow*
for a test build.

## Tests

```
python tests/test_shredpack.py   # engine: every format, selection, links, integrity, ...
python tests/ui_smoke.py         # drives the UI logic with stand-in widgets
```

The tests use only the standard library, so the optional 7Z/ISO/AES code paths are
exercised by hand rather than in CI.

## Licenses

See `LICENSE` for ShredPack itself. The release build bundles third-party packages
(`py7zr`, `pycdlib`, `pyzipper`, `tkinterdnd2`, `send2trash`) under their own
licenses; check each project's current terms before redistributing binaries.
