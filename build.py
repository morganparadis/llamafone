"""
Build script — compiles .py to .pyc (Python 3.7) and packages into a .ts4script file.

The Sims 4 uses Python 3.7 and only loads compiled .pyc files from .ts4script zips.
This script uses a local Python 3.7 (in tools/python37/) to compile.

Usage:
  python build.py           Build and auto-install to Sims 4 Mods folder
  python build.py --build   Build only (don't install)
"""
import os
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


def build_package():
    """Build Llamafone.package from XML sources in package_src/.
    Uses our own DBPF writer (tools/package_builder.py) -- no S4S required."""
    builder_path = os.path.join(SCRIPT_DIR, "tools", "package_builder.py")
    if not os.path.isfile(builder_path):
        print(f"  WARN: {builder_path} not found, skipping package build")
        return
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

    dest_script = os.path.join(mods_folder, os.path.basename(script_file))
    try:
        shutil.copy2(script_file, dest_script)
        print(f"  Installed: {os.path.basename(dest_script)}")
    except PermissionError:
        print(f"  ERROR: could not write {dest_script} -- close The Sims 4 and try again")

    # Companion .package -- tuning resources for the pie-menu interactions.
    # Built from package_src/ by tools/package_builder.py.
    if os.path.exists(PACKAGE_FILE):
        dest_package = os.path.join(mods_folder, os.path.basename(PACKAGE_FILE))
        try:
            shutil.copy2(PACKAGE_FILE, dest_package)
            print(f"  Installed: {os.path.basename(dest_package)}")
        except PermissionError:
            print(f"  ERROR: could not write {dest_package} -- close The Sims 4 and try again")
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

    # Clean up the previous-build leftover and any pre-rename ClaudeAI
    # files so nothing stale survives in the Mods folder.
    for stale in (
        "ClaudeAI_Test.ts4script",
        "ClaudeAI.ts4script",
        "ClaudeAI.package",
        "ClaudeAI_Journal.json",
        "ClaudeAI_Settings.json",
        "ClaudeAI_SimSnapshots.json",
        "ClaudeAI_Milestones.json",
        "ClaudeAI_MilestoneRefs.json",
        "ClaudeAI_LastPrompt.txt",
        "ClaudeAI_BuffList.txt",
        "ClaudeAI_Log.txt",
        "claude_config.cfg",
    ):
        stale_path = os.path.join(mods_folder, stale)
        if os.path.exists(stale_path):
            try:
                os.remove(stale_path)
                print(f"  Cleaned up: {stale}")
            except OSError:
                pass

    print()
    print("Installation complete! Restart The Sims 4 to load the mod.")
    print("Then open the cheat console (Ctrl+Shift+C) and type: llama.status")


if __name__ == "__main__":
    build_only = "--build" in sys.argv
    script = build()
    build_package()
    if not build_only:
        install(script)
