"""Dev-only Sims 4 script: dumps every career level's title hash so
tools/gen_career_titles.py can rebuild src/llamafone/career_titles.py.
NOT part of the Llamafone release.

Use (after new packs come out):
  1. Zip this file into Mods:   python tools/dev/make_dev_mod.py
  2. Start the game, load any save, cheat console:  llama.dev_career_titles
     -> writes Documents/Llamafone_CareerTitleRefs.json
  3. Quit, delete Mods/llamafone_dev_titles.ts4script
  4. python tools/gen_career_titles.py   (then commit career_titles.py)

Verified in careers/career_tuning.pyc: Career.start_track,
TunableCareerTrack.career_levels / .branches, CareerLevel.title.
"""
import json
import os

import services
import sims4.commands
from sims4.resources import Types


@sims4.commands.Command("llama.dev_career_titles", command_type=sims4.commands.CommandType.Live)
def cmd_dev_career_titles(_connection=None):
    output = sims4.commands.CheatOutput(_connection)
    try:
        client = services.client_manager().get_first_client()
        sim_info = client.active_sim_info if client else None
    except Exception:
        sim_info = None
    refs, seen = [], set()

    def _walk(career_name, track, depth=0):
        if track is None or id(track) in seen or depth > 6:
            return
        seen.add(id(track))
        for i, level in enumerate(getattr(track, "career_levels", None) or ()):
            try:
                h = int(level.title(sim_info).hash)
            except Exception:
                h = 0
            refs.append({"career": career_name, "track": getattr(track, "__name__", str(track)),
                         "level": i + 1, "hash": "%08X" % h})
        for br in getattr(track, "branches", None) or ():
            _walk(career_name, br, depth + 1)

    try:
        careers = list(services.get_instance_manager(Types.CAREER).types.values())
        for career in careers:
            _walk(getattr(career, "__name__", str(career)), getattr(career, "start_track", None))
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_CareerTitleRefs.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(refs, f, indent=1)
        output("[Llamafone dev] %d careers, %d levels -> %s" % (len(careers), len(refs), path))
    except Exception as e:
        output("[Llamafone dev] failed: %s: %s" % (type(e).__name__, e))


@sims4.commands.Command("llama.dev_trait_names", command_type=sims4.commands.CommandType.Live)
def cmd_dev_trait_names(_connection=None):
    """Every trait's display-name string id, for tools/gen_trait_names.py.
    Verified in traits/traits.pyc: Trait.display_name is a
    TunableLocalizedStringFactory (its ._string_id is the hash, the game's
    own _verify_tuning_callback reads it), Trait.trait_type is a TraitType."""
    output = sims4.commands.CheatOutput(_connection)
    refs = []
    try:
        traits = list(services.get_instance_manager(Types.TRAIT).types.values())
        for trait in traits:
            try:
                h = int(getattr(trait.display_name, "_string_id", 0) or 0)
            except Exception:
                h = 0
            refs.append({"trait": getattr(trait, "__name__", str(trait)),
                         "type": str(getattr(trait, "trait_type", "")),
                         "hash": "%08X" % h})
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_TraitNameRefs.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(refs, f, indent=1)
        output("[Llamafone dev] %d traits -> %s" % (len(refs), path))
    except Exception as e:
        output("[Llamafone dev] failed: %s: %s" % (type(e).__name__, e))
