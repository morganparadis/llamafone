"""Run every tests/test_*.py with the bundled Python 3.7 (the game's version).

    python tests/run_all.py            # all suites
    python tests/run_all.py social     # suites whose name contains "social"

Each suite runs with USERPROFILE/HOME pointed at a temp folder, so nothing
can touch the real Documents/Llamafone_Log.txt, llamafone.cfg, or saves.
Tests stub the game's modules and never call a real AI service.
Not shipped: build.py only packages src/llamafone.
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY37 = os.path.join(REPO, "tools", "python37", "python.exe")
PASSED = re.compile(r"RESULT: ALL PASS|FAILS 0\b")


def main():
    pick = sys.argv[1] if len(sys.argv) > 1 else ""
    suites = sorted(p for p in glob.glob(os.path.join(HERE, "test_*.py")) if pick in os.path.basename(p))
    home = tempfile.mkdtemp(prefix="llamafone_tests_")
    os.makedirs(os.path.join(home, "Documents"), exist_ok=True)
    env = dict(os.environ, USERPROFILE=home, HOME=home, PYTHONIOENCODING="utf-8")
    failed = []
    for path in suites:
        name = os.path.basename(path)[:-3]
        try:
            r = subprocess.run([PY37, path], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, cwd=REPO, timeout=300)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            out = "timed out after 300s"
        ok = bool(PASSED.search(out)) and "Traceback" not in out
        last = [l for l in out.strip().splitlines() if l.strip()][-1:] or ["(no output)"]
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> {last[0][:150]}"))
        if not ok:
            failed.append(name)
    print(f"\n{len(suites) - len(failed)}/{len(suites)} suites passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
