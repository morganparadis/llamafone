"""
Build script — compiles .py to .pyc (Python 3.7) and packages into a .ts4script file.

The Sims 4 uses Python 3.7 and only loads compiled .pyc files from .ts4script zips.
This script uses a local Python 3.7 (in tools/python37/) to compile.

Usage:
  python build.py             Build and auto-install to Sims 4 Mods folder
  python build.py --build     Build only (don't install)
  python build.py --release   Build + create Llamafone.zip for CurseForge / release
                              upload. Zip contains ONLY the .ts4script and .package.
                              NEVER includes llamafone.cfg -- overwriting users'
                              existing configs would nuke their API key on update.
                              Auto-gen (v3.5+) writes a default cfg on first
                              launch for fresh installs.
"""
import os
import re
import sys
import subprocess
import zipfile
import shutil
import tempfile

MOD_NAME = "Llamafone"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(SCRIPT_DIR, "src")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"{MOD_NAME}.ts4script")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "llamafone.cfg")
PACKAGE_FILE = os.path.join(SCRIPT_DIR, f"{MOD_NAME}.package")


def _find_python37():
    """Locate the bundled Python 3.7 interpreter we use to compile .pyc.

    Default to tools/python37/python.exe (Windows -- shipping the
    embedded interpreter is by far the most common setup). On non-
    Windows hosts we look for a system `python3.7` so Linux/macOS
    contributors can build without a Windows interpreter in tree.
    """
    win_path = os.path.join(SCRIPT_DIR, "tools", "python37", "python.exe")
    if os.path.isfile(win_path):
        return win_path
    nix_local = os.path.join(SCRIPT_DIR, "tools", "python37", "python")
    if os.path.isfile(nix_local):
        return nix_local
    # System python3.7
    import shutil as _sh
    sys_py = _sh.which("python3.7")
    if sys_py:
        return sys_py
    return win_path  # fall through with the win path so the error message is useful


PYTHON37 = _find_python37()


def find_mods_folder():
    """Attempt to locate the Sims 4 Mods folder on this machine.

    Checks Windows/macOS native paths first, then common Linux
    Proton/Wine prefix locations (Steam, Lutris, Heroic) so a Linux
    contributor running through Proton gets auto-install too.
    """
    home = os.path.expanduser("~")
    native = [
        os.path.join(home, "Documents", "Electronic Arts", "The Sims 4", "Mods"),
    ]
    # Linux Proton/Wine — the Sims 4 prefix's compatdata id varies per
    # install, so glob for any directory that has the expected layout.
    import glob as _g
    proton_glob = [
        # Steam Proton
        os.path.join(home, ".steam/steam/steamapps/compatdata/*/pfx/"
                     "drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods"),
        os.path.join(home, ".local/share/Steam/steamapps/compatdata/*/pfx/"
                     "drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods"),
        # Lutris / Heroic / generic Wine — user-configured prefix paths
        os.path.join(home, "Games/*/drive_c/users/*/Documents/Electronic Arts/The Sims 4/Mods"),
        os.path.join(home, ".wine/drive_c/users/*/Documents/Electronic Arts/The Sims 4/Mods"),
    ]
    candidates = list(native)
    for pattern in proton_glob:
        candidates.extend(_g.glob(pattern))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def compile_py_to_pyc(py_path, pyc_path):
    """Compile a .py file to .pyc using Python 3.7."""
    result = subprocess.run(
        [PYTHON37, "-c", f"import py_compile; py_compile.compile(r'{py_path}', r'{pyc_path}', doraise=True)"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  COMPILE ERROR: {py_path}")
        print(f"    {result.stderr.strip()}")
        sys.exit(1)


def _refresh_icon_dds():
    """Regenerate package_src/llamafone_icon.dds from the source PNG in
    assets/. Auto-runs before the .package build so any icon swap by
    the user just requires overwriting the source PNG and rebuilding --
    no manual conversion step.

    Source lives in assets/ (not docs/img/) because docs/ is purely
    website content -- images there get published via GitHub Pages,
    they never ship with the mod. Keeping the mod's authoritative
    icon source under assets/ makes it obvious what belongs where.

    The source PNG typically has transparent padding around a rounded-
    square design; we crop to the tight non-transparent bounding box
    before resizing so the icon fills its phone tile the same way the
    other apps' icons fill theirs. Skipped silently when Pillow isn't
    available (dev machines that don't have it installed still get a
    build using whatever .dds is already in package_src/).
    """
    src_png = os.path.join(SCRIPT_DIR, "assets", "llamafone-icon.png")
    dst_dds = os.path.join(SCRIPT_DIR, "package_src", "llamafone_icon.dds")
    if not os.path.isfile(src_png):
        return
    try:
        from PIL import Image
    except ImportError:
        print("  WARN: Pillow not installed -- skipping icon regeneration.")
        print("        Existing package_src/llamafone_icon.dds will be reused.")
        print("        pip install Pillow to enable auto-regeneration.")
        return
    img = Image.open(src_png).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    if w != h:
        # Pad the shorter side to keep the design centered; Sims 4
        # phone tiles are square and rectangle icons would render
        # stretched.
        size = max(w, h)
        square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        square.paste(img, ((size - w) // 2, (size - h) // 2))
        img = square
    # Bake in ~10% transparent padding on each side so the phone tile's
    # green selection highlight has room to sit OUTSIDE the artwork
    # instead of overlapping it. Base game / mod icons all have this
    # safe-area border built in; without it, the icon reads as
    # oversized relative to its neighbors. Content ~205x205 in a
    # 256x256 canvas.
    CONTENT_SIZE = 244
    img = img.resize((CONTENT_SIZE, CONTENT_SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    canvas.paste(img, ((256 - CONTENT_SIZE) // 2, (256 - CONTENT_SIZE) // 2))
    img = canvas
    img.save(dst_dds, format="DDS")
    print(f"  + icon DDS regenerated from assets/llamafone-icon.png ({os.path.getsize(dst_dds):,} bytes)")


def build_package():
    """Build Llamafone.package from XML sources in package_src/.
    Uses our own DBPF writer (tools/package_builder.py) -- no S4S required."""
    builder_path = os.path.join(SCRIPT_DIR, "tools", "package_builder.py")
    if not os.path.isfile(builder_path):
        print(f"  WARN: {builder_path} not found, skipping package build")
        return
    _refresh_icon_dds()
    print()  # blank line between script and package output
    result = subprocess.run(
        [sys.executable, builder_path],
        cwd=SCRIPT_DIR,
    )
    if result.returncode != 0:
        print("  WARN: package build failed -- shipping .ts4script only")


def build():
    if not os.path.isdir(SRC_DIR):
        print(f"ERROR: src/ directory not found at {SRC_DIR}")
        sys.exit(1)

    if not os.path.isfile(PYTHON37):
        print(f"ERROR: Python 3.7 not found at {PYTHON37}")
        print("Run this once to set it up:")
        print("  1. Download https://www.python.org/ftp/python/3.7.9/python-3.7.9-embed-amd64.zip")
        print("  2. Extract to tools/python37/ in this project folder")
        sys.exit(1)

    py_files = []
    for root, _dirs, files in os.walk(SRC_DIR):
        for fname in files:
            if fname.endswith(".py"):
                full_path = os.path.join(root, fname)
                arc_path = os.path.relpath(full_path, SRC_DIR)
                py_files.append((full_path, arc_path))

    if not py_files:
        print("ERROR: No .py files found in src/")
        sys.exit(1)

    # Compile to a temp directory, then zip
    with tempfile.TemporaryDirectory() as tmp:
        print(f"Building {MOD_NAME}.ts4script ...")
        print(f"  Compiling {len(py_files)} files with Python 3.7...")

        compiled = []
        for full_path, arc_path in sorted(py_files, key=lambda x: x[1]):
            # .py -> .pyc in archive path
            pyc_arc = arc_path.replace(".py", ".pyc")
            pyc_tmp = os.path.join(tmp, pyc_arc)
            os.makedirs(os.path.dirname(pyc_tmp) or tmp, exist_ok=True)

            compile_py_to_pyc(full_path, pyc_tmp)
            compiled.append((pyc_tmp, pyc_arc))
            print(f"  + {pyc_arc}")

        with zipfile.ZipFile(OUTPUT_FILE, "w", zipfile.ZIP_DEFLATED) as zf:
            for pyc_tmp, pyc_arc in compiled:
                zf.write(pyc_tmp, pyc_arc)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"\nBuilt: {OUTPUT_FILE} ({size_kb:.1f} KB, {len(compiled)} files)")
    return OUTPUT_FILE


def install(script_file):
    mods_folder = find_mods_folder()
    if not mods_folder:
        print("\nCould not auto-detect Sims 4 Mods folder.")
        print(f"Manually copy these files to your Mods folder:")
        print(f"  {script_file}")
        if os.path.exists(CONFIG_FILE):
            print(f"  {CONFIG_FILE}")
        return

    print(f"\nInstalling to: {mods_folder}")

    def _verified_copy(src, dest, label):
        """Copy src to dest, then verify the destination file actually
        matches by size + recent mtime. Prints clear success / failure.
        Catches the case where shutil.copy2 silently succeeds but Sims 4
        holds the file open and the new bytes don't actually land."""
        try:
            src_size = os.path.getsize(src)
        except Exception as e:
            print(f"  ERROR: source missing for {label}: {e}")
            return False
        try:
            shutil.copy2(src, dest)
        except PermissionError:
            print(f"  ERROR: could not write {dest} -- close The Sims 4 and try again")
            return False
        except Exception as e:
            print(f"  ERROR: copy {label} failed: {type(e).__name__}: {e}")
            return False
        # Verify the destination actually reflects the new file.
        try:
            dest_size = os.path.getsize(dest)
            dest_mtime = os.path.getmtime(dest)
        except Exception as e:
            print(f"  ERROR: cannot stat dest after copy: {e}")
            return False
        now = time.time()
        if dest_size != src_size:
            print(f"  ERROR: {label} size mismatch after copy "
                  f"(src={src_size}, dest={dest_size}) -- the file is probably locked. Close The Sims 4.")
            return False
        if now - dest_mtime > 5:
            print(f"  ERROR: {label} mtime didn't update (still "
                  f"{int(now - dest_mtime)}s old) -- the copy didn't actually land. Close The Sims 4.")
            return False
        print(f"  Installed: {label} ({dest_size:,} bytes)")
        return True

    import time
    dest_script = os.path.join(mods_folder, os.path.basename(script_file))
    _verified_copy(script_file, dest_script, os.path.basename(dest_script))

    # Companion .package -- tuning resources for the pie-menu interactions.
    # Built from package_src/ by tools/package_builder.py.
    if os.path.exists(PACKAGE_FILE):
        dest_package = os.path.join(mods_folder, os.path.basename(PACKAGE_FILE))
        _verified_copy(PACKAGE_FILE, dest_package, os.path.basename(dest_package))
    else:
        print(f"  Skipped package (no Llamafone.package at repo root)")

    dest_config = os.path.join(mods_folder, "llamafone.cfg")
    if not os.path.exists(dest_config):
        if os.path.exists(CONFIG_FILE):
            shutil.copy2(CONFIG_FILE, dest_config)
            print(f"  Installed: llamafone.cfg")
            print()
            print("=" * 60)
            print("  NEXT STEP: Edit llamafone.cfg in your Mods folder")
            print("  and replace YOUR_API_KEY_HERE with your real API key.")
            print("=" * 60)
    else:
        print(f"  Skipped config (already exists -- your API key is safe)")

    print()
    print("Installation complete! Restart The Sims 4 to load the mod.")
    print("Then open the cheat console (Ctrl+Shift+C) and type: llama.status")


def _read_mod_version():
    """Read MOD_VERSION from src/llamafone/__init__.py so the release
    zip name always matches the shipped version. Falls back to
    'unversioned' if we can't parse it -- shouldn't happen in practice."""
    init_path = os.path.join(SRC_DIR, "llamafone", "__init__.py")
    try:
        with open(init_path, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*MOD_VERSION\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "unversioned"


def make_release_zip(script_file):
    """Bundle the built .ts4script and .package into a versioned
    Llamafone_v<X.Y.Z>.zip for CurseForge / GitHub Release upload.
    Explicitly excludes llamafone.cfg -- shipping a cfg in the release
    zip overwrites each user's real config (including their API key)
    on update, which is a nasty regression. The mod's auto-gen (v3.5+)
    writes a default cfg to Mods/ on first launch for fresh installs,
    so the cfg does not need to be in the release archive.

    The zip filename includes the version so uploaders can visually
    confirm they're grabbing the right build, and CurseForge users
    downloading the raw asset from a GitHub Release see the version
    in the filename."""
    version = _read_mod_version()
    zip_name = f"{MOD_NAME}_v{version}.zip"
    zip_path = os.path.join(SCRIPT_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isfile(script_file):
            zf.write(script_file, os.path.basename(script_file))
        else:
            print(f"  WARN: script file missing at {script_file}; zip will be incomplete")
        if os.path.isfile(PACKAGE_FILE):
            zf.write(PACKAGE_FILE, os.path.basename(PACKAGE_FILE))
        else:
            print(f"  WARN: package file missing at {PACKAGE_FILE}; zip will be incomplete")
    size_kb = os.path.getsize(zip_path) / 1024
    print(f"\nRelease zip: {zip_path} ({size_kb:.1f} KB)")
    print(f"  Contents: {MOD_NAME}.ts4script + {MOD_NAME}.package")
    print(f"  Version:  v{version} (from src/llamafone/__init__.py)")
    print("  llamafone.cfg deliberately EXCLUDED -- would overwrite users' API keys.")
    print("  Mod auto-generates a default cfg on first launch for fresh installs.")
    return zip_path


if __name__ == "__main__":
    build_only = "--build" in sys.argv
    release_mode = "--release" in sys.argv
    script = build()
    build_package()
    if release_mode:
        make_release_zip(script)
    if not build_only and not release_mode:
        install(script)
