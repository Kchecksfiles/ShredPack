# ShredPack

A fast, self-contained archive tool for Windows with a dark, minimal interface.
It reads and writes archives **with its own built-in engine** - it does not need
WinRAR or 7-Zip installed, and it never launches another program to do the work.

ShredPack only runs when you open it or pick one of its right-click options. It
does the job and exits; nothing stays running in the background.

## What it does

- **Extract** - right-click an archive, or drag it onto the window.
  *Extract Here* always unpacks into a new folder named after the archive, so a
  messy archive never spills loose files into the folder you're in. It starts at
  once - no confirmation screen - using the options saved in **Settings**, shows a
  small progress window only if the job takes a moment, and closes itself when it
  is done (with a short sound, which you can turn off). *Extract files...* still
  lets you choose the destination.
- **Browse and extract selected files** - open an archive's contents, search
  them, and extract only what you pick. Optional wildcard filters
  (e.g. `*.jpg`). What to do if a file already exists (keep both / overwrite /
  skip / overwrite if newer) is set once in **Settings**.
- **Test archive** - verifies the contents (checksums and decompression)
  without writing anything.
- **Create archives** - right-click any file or folder (*Add to ZIP* for a
  one-click `.zip`, or *Add to archive...* to choose format, name, level and
  password), or use *Create Archive...* in the app. Drag files onto the window
  to start.
- **Checksums** - SHA-256, SHA-1, MD5 and CRC32 of any file, with a compare box
  for a published checksum.
- **Settings** (link in the top-right of the window; saved automatically):
  whether *Extract Here* starts immediately, whether to **delete the original
  archive after extraction** (moved to the Recycle Bin when possible - if you
  extract only part of an archive, ShredPack asks first), what to do with files
  that already exist, and whether to play the finish sound.

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
(The installed app is a folder build, which opens faster than the single-file
portable exe - that one unpacks itself again on every start.)
Each release includes `SHA256SUMS.txt` (one line per file: the SHA-256 hash, two
spaces, the file name) so you can verify your download. The release workflow
generates it from the exact files it publishes, checks it against them, then
downloads the published assets back from the release and checks them again - the
build fails if any of the three files is missing, differs from what was built,
or doesn't verify. (Releases published before this workflow existed have no
`SHA256SUMS.txt`; the app's *Checksum...* button can hash those files.)

To verify, hash the file you downloaded and compare it with its line in
`SHA256SUMS.txt` (the app's own *Checksum...* button does the same):

```
Get-FileHash .\ShredPack-Setup.exe -Algorithm SHA256
```

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
- Browsing split archives and previewing files are not implemented yet.

## Build from source

```
pip install py7zr pycdlib pyzipper tkinterdnd2 send2trash pyinstaller
:: portable single file -> dist\ShredPack.exe
pyinstaller --noconsole --onefile --icon=shredpack.ico --add-data "shredpack.ico;." ^
  --collect-all py7zr --collect-all pycdlib --collect-all pyzipper --collect-all tkinterdnd2 ^
  --name ShredPack shredpack.py
:: the app the installer ships (a folder, opens faster) -> dist-app\ShredPack\
pyinstaller --noconsole --onedir --noconfirm --distpath dist-app --workpath build-app ^
  --icon=shredpack.ico --add-data "shredpack.ico;." ^
  --collect-all py7zr --collect-all pycdlib --collect-all pyzipper --collect-all tkinterdnd2 ^
  --name ShredPack shredpack.py
```

The installer is built from `setup.iss` with [Inno Setup](https://jrsoftware.org/isinfo.php).
Pass the version from `shredpack.py`: `ISCC.exe /DMyAppVersion=x.y.z setup.iss`
(x.y.z = `APP_VERSION`; the file's own fallback is only for quick local builds).

### Releases

`.github/workflows/build.yml` is the only workflow. It runs the tests, builds the
portable `dist/ShredPack.exe` and the installer `Output/ShredPack-Setup.exe`, writes and
verifies `SHA256SUMS.txt`, and uploads the three files as the `ShredPack-release`
workflow artifact. It attaches them to a version release (`vX.Y.Z`) only when asked, and
never touches the old rolling `latest` pre-release:

- **Pushes to `main`, pull requests, and manual runs with `publish` unticked (the
  default):** test build only. Nothing is published and no tag or release is created
  or changed.
- **Push a tag** such as `v1.2.0`: the tag must equal `v` + `APP_VERSION`, and must point
  at the commit being built. The release is created if the tag has none; if it exists
  with no files, the three files are attached to it; if it already has files, the run fails.
- **Actions > Build ShredPack > Run workflow** (on `main`) with `publish` ticked: the
  same rules. If the tag doesn't exist the run fails unless `create_tag` is also ticked
  (then it creates the tag at this commit and a new release). Files already on the release
  are only replaced if `overwrite` is ticked. A draft release for the tag is refused.

After publishing, the workflow downloads the release's assets back and checks them
against the build again. `VERSION.txt` must equal `APP_VERSION` (it drives the in-app
update link, so update both together).

## Tests

```
python tests/test_shredpack.py       # engine: every format, selection, links, integrity, menu registration, ...
python tests/ui_smoke.py             # drives the UI logic with stand-in widgets
python tests/test_ui_event_loop.py   # the real app inside a stand-in Tk event loop (incl. right-click launches)
```

The tests use only the standard library, so the optional 7Z/ISO/AES code paths are
exercised by hand rather than in CI.

## Licenses

See `LICENSE` for ShredPack itself. The release build bundles third-party packages
(`py7zr`, `pycdlib`, `pyzipper`, `tkinterdnd2`, `send2trash`) under their own
licenses; check each project's current terms before redistributing binaries.
