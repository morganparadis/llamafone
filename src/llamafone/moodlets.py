"""
Response cleaner + moodlet investigation utilities.

The moodlet feature was removed in v1.0.0 because every Sims 4 buff
carried baked-in narrative text (e.g. a "Happy" buff might say "New
Baby"). Returning here to investigate whether the base-game generic /
"Misc" emotion buffs are usable as neutral mood applications.

This module currently exports:
  - clean_response(text): strips markdown + stray MOOD: lines (used
    everywhere; the prompts don't ask for MOOD anymore).
  - apply_mood(sim_info, mood_tag, ...): tries an exact-name whitelist
    of generic-emotion buff classes. Returns True on success, False
    otherwise. NOT yet wired into the main flow — used only by the
    llama.testmoodlet debug command for now.
  - dump_buffs_matching(keyword, mood_type=None): writes every loaded
    buff class whose name contains `keyword` (case-insensitive) to
    Llamafone_BuffList.txt for offline review.
"""

import os
import datetime
import re


def _log(message):
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [moodlets] {message}\n")
    except Exception:
        pass


def extract_mood_tag(text):
    """Strip trailing junk and pull out any MOOD: <emotion> tag.
    Returns (clean_text, mood_or_None). Used by phone.py to apply a
    matching moodlet."""
    if not text:
        return text, None

    lines = text.rstrip().split("\n")
    mood = None

    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        upper = last.upper()
        if upper.startswith("MOOD_TOPIC:") or upper.startswith("MOOD TOPIC:"):
            lines.pop()
            continue
        if upper.startswith("MOOD:"):
            mood = last.split(":", 1)[1].strip().lower()
            lines.pop()
            continue
        stripped_chars = set(last)
        if stripped_chars and stripped_chars.issubset(set("-=*_~ ")):
            lines.pop()
            continue
        break

    cleaned_lines = []
    for line in lines:
        line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
        line = re.sub(r'__(.+?)__', r'\1', line)
        line = re.sub(r'^\s*\*?\*?Message\s*\d+\s*:?\*?\*?\s*', '', line, flags=re.IGNORECASE)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).rstrip(), mood


def clean_response(text):
    """Backwards-compat wrapper -- returns just the cleaned text and
    discards the mood tag. Use extract_mood_tag() when you need the mood."""
    clean, _ = extract_mood_tag(text)
    return clean


# ---------------------------------------------------------------------------
# Moodlet investigation
# ---------------------------------------------------------------------------

# Confirmed via `llama.dumpbuffs feeling` in a real Sims 4 install.
# Each entry maps an emotion -> the exact Buff_Trait_Feeling<X> class
# that provides a clean +1 mood with neutral "Feeling X" display text.
# These don't carry narrative content like "New Baby" / "Workout" / etc.,
# so they're safe for AI-driven mood application.
#
# 5 emotions (embarrassed, tense, uncomfortable, bored, dazed) don't have
# a Buff_Trait_Feeling<X> equivalent in the base game -- they're never
# trait-induced moods. Listed as empty so apply_mood skips rather than
# falls back on a narrative buff.
_GENERIC_BUFF_CANDIDATES = {
    "happy":         ["Buff_Trait_FeelingHappy"],
    "sad":           ["Buff_Trait_FeelingSad"],
    "angry":         ["Buff_Trait_FeelingAngry"],
    "confident":     ["Buff_Trait_FeelingConfident"],
    "flirty":        ["Buff_Trait_FeelingFlirty"],
    "playful":       ["Buff_Trait_FeelingPlayful"],
    "energized":     ["Buff_Trait_FeelingEnergized"],
    "focused":       ["Buff_Trait_FeelingFocused"],
    "inspired":      ["Buff_Trait_FeelingInspired"],
    # No safe generic buff exists for these moods. Skip cleanly.
    "embarrassed":   [],
    "tense":         [],
    "uncomfortable": [],
    "bored":         [],
    "dazed":         [],
}


def _find_buff_by_exact_name(buff_manager, candidates):
    """Return the first buff type whose class name exactly matches one of
    the candidates (case-insensitive). None if no match."""
    targets = {c.lower() for c in candidates}
    try:
        for buff_type in buff_manager.types.values():
            try:
                if buff_type.__name__.lower() in targets:
                    return buff_type
            except Exception:
                continue
    except Exception as e:
        _log(f"buff scan error: {type(e).__name__}: {e}")
    return None


def apply_mood(sim_info, mood_tag, reason=None):
    """Try to apply a generic-emotion moodlet to the sim. Returns True if a
    buff was successfully applied. Logs the outcome either way."""
    if not sim_info:
        _log("apply_mood: sim_info is None")
        return False
    if not mood_tag:
        _log("apply_mood: empty mood_tag")
        return False

    mood = mood_tag.strip().lower()
    candidates = _GENERIC_BUFF_CANDIDATES.get(mood)
    if not candidates:
        _log(f"apply_mood: no candidates for mood '{mood}'")
        return False

    try:
        import services
        import sims4.resources
    except Exception as e:
        _log(f"apply_mood: import failed: {type(e).__name__}: {e}")
        return False

    try:
        buff_manager = services.get_instance_manager(sims4.resources.Types.BUFF)
    except Exception as e:
        _log(f"apply_mood: get_instance_manager failed: {type(e).__name__}: {e}")
        return False
    if not buff_manager:
        _log("apply_mood: buff_manager is None")
        return False

    buff_type = _find_buff_by_exact_name(buff_manager, candidates)
    if not buff_type:
        _log(f"apply_mood: no generic buff found for '{mood}' (tried {len(candidates)} candidates). Run llama.dumpbuffs {mood} to investigate.")
        return False

    try:
        sim_info.add_buff_from_op(buff_type, buff_reason=None)
        _log(f"apply_mood: applied {buff_type.__name__} for '{mood}' (reason: {reason})")
        return True
    except Exception as e:
        _log(f"apply_mood: add_buff_from_op failed: {type(e).__name__}: {e}")

    try:
        sim_info.add_buff(buff_type, buff_reason=None)
        _log(f"apply_mood: applied via add_buff fallback: {buff_type.__name__}")
        return True
    except Exception as e:
        _log(f"apply_mood: add_buff fallback failed: {type(e).__name__}: {e}")
    return False


def dump_buffs_matching(keyword, limit=200):
    """Write every loaded buff class whose name contains `keyword`
    (case-insensitive) to Llamafone_BuffList.txt. Used by the
    llama.dumpbuffs cheat command. Returns the number of matches."""
    out_path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_BuffList.txt")
    try:
        import services
        import sims4.resources
        buff_manager = services.get_instance_manager(sims4.resources.Types.BUFF)
        if not buff_manager:
            with open(out_path, "a", encoding="utf-8") as f:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n[{ts}] dumpbuffs '{keyword}' -- buff_manager is None\n")
            return 0
    except Exception as e:
        with open(out_path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n[{ts}] dumpbuffs '{keyword}' -- import failed: {type(e).__name__}: {e}\n")
        return 0

    kw_low = keyword.lower()
    matches = []
    try:
        for buff_type in buff_manager.types.values():
            try:
                name = buff_type.__name__
                if kw_low in name.lower():
                    mood_type = getattr(buff_type, "mood_type", None)
                    mood_name = getattr(mood_type, "__name__", "") if mood_type else "(no mood)"
                    matches.append((name, mood_name))
            except Exception:
                continue
    except Exception as e:
        with open(out_path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n[{ts}] dumpbuffs '{keyword}' -- scan error: {type(e).__name__}: {e}\n")
        return 0

    matches.sort()
    try:
        with open(out_path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n[{ts}] dumpbuffs '{keyword}' -- {len(matches)} matches:\n")
            for name, mood in matches[:limit]:
                f.write(f"  - {name}  [mood_type: {mood}]\n")
            if len(matches) > limit:
                f.write(f"  ... ({len(matches) - limit} more, truncated)\n")
    except Exception:
        pass
    return len(matches)
