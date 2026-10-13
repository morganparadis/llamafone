"""Dev-only: build src/llamafone/trait_names.py (trait display-name hash ->
English text), so the mod can name a sim's traits the way the game shows
them ("Music Lover", not "Musiclover") with a plain dict lookup.

1. python tools/dev/make_dev_mod.py, then in game run the cheat:
   llama.dev_trait_names   (writes Documents/Llamafone_TraitNameRefs.json)
   Remove Mods/llamafone_dev_titles.ts4script afterwards.
2. Run:  python tools/gen_trait_names.py ["C:/path/to/The Sims 4"]

Only personality traits (the ones picked in CAS or earned, shown in the
Sim's trait panel) go in the table. Re-run after new packs come out.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from gen_career_titles import ROOT, stbl_strings  # noqa: E402

REFS = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_TraitNameRefs.json")
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "llamafone", "trait_names.py")


def main():
    refs = json.load(open(REFS, encoding="utf-8"))
    personality = [r for r in refs if r.get("type", "").endswith("PERSONALITY")]
    hashes = {int(r["hash"], 16) for r in personality if r.get("hash") and r["hash"] != "00000000"}
    print(f"{len(refs)} traits, {len(personality)} personality, {len(hashes)} distinct name hashes")
    texts = {}
    files = sorted(glob.glob(os.path.join(ROOT, "**", "Strings_ENG_US.package"), recursive=True),
                   key=lambda p: (os.sep + "Delta" + os.sep) in p)   # patches last, so they win
    for path in files:
        for key, text in stbl_strings(path):
            if key in hashes:
                texts[key] = text
    print(f"{len(texts)} names with English text, {len(hashes - set(texts))} without")
    lines = ['"""Personality trait names (display-name hash -> English text). Generated',
             'by tools/gen_trait_names.py from the game\'s own tuning and string files;',
             'do not edit by hand."""', "", "NAMES = {"]
    for k in sorted(texts):
        lines.append(f"    0x{k:08X}: {texts[k]!r},")
    lines.append("}")
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {os.path.normpath(OUT)} ({os.path.getsize(OUT):,} bytes)")
    for r in personality:
        if any(s in r["trait"].lower() for s in ("hos_ev009", "thatssojordy", "musiclover", "practicemakesperfect")):
            print(f"  check: {r['trait']} -> {texts.get(int(r['hash'], 16))!r}")


if __name__ == "__main__":
    main()
