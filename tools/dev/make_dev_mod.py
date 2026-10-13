"""Compile tools/dev/llamafone_dev_titles.py with the bundled Python 3.7 and
zip it into the Mods folder as a .ts4script (dev-only; delete it from Mods
when done). The game only loads compiled .pyc files from .ts4script zips."""
import os, subprocess, tempfile, zipfile
here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "llamafone_dev_titles.py")
py37 = os.path.join(here, "..", "python37", "python.exe")
mods = os.path.join(os.path.expanduser("~"), "Documents", "Electronic Arts", "The Sims 4", "Mods")
out = os.path.join(mods, "llamafone_dev_titles.ts4script")
with tempfile.TemporaryDirectory() as tmp:
    pyc = os.path.join(tmp, "llamafone_dev_titles.pyc")
    subprocess.run([py37, "-c", f"import py_compile; py_compile.compile(r'{src}', r'{pyc}', doraise=True)"],
                   check=True)
    with zipfile.ZipFile(out, "w") as z:
        z.write(pyc, "llamafone_dev_titles.pyc")
print("wrote", out, "-- delete it after running llama.dev_career_titles / llama.dev_trait_names")
