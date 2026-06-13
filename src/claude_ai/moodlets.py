"""
Moodlet system — applies emotional buffs to the recipient sim based on the
mood tag the LLM appended to its response.

Every step logs to ClaudeAI_Log.txt with a [moodlets] prefix so silent failures
are visible. Lookup strategy: try the configured buff ID first (fast path),
then fall back to fuzzy name search through the buff manager (handles cases
where buff IDs differ across Sims 4 versions or expansion packs).
"""

import os
import datetime


def _log(message):
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "ClaudeAI_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [moodlets] {message}\n")
    except Exception:
        pass


# Base-game buff tuning IDs. These are the "generic" social/emotional buffs
# that ship with every Sims 4 install. If a lookup fails the code falls back
# to a fuzzy name search, so wrong IDs aren't fatal -- just slower.
_MOOD_BUFF_IDS = {
    "happy":         27738,
    "confident":     27131,
    "flirty":        27468,
    "inspired":      27503,
    "focused":       27453,
    "energized":     27400,
    "playful":       27954,
    "sad":           28173,
    "angry":         26818,
    "tense":         28356,
    "embarrassed":   27363,
    "bored":         27049,
    "uncomfortable": 28395,
    "dazed":         27167,
}


def _find_buff_by_name(buff_manager, mood_key):
    """Fuzzy name search through loaded buff types -- finds e.g.
    'Buff_Happy_Generic' when looking for 'happy'."""
    candidates = []
    try:
        all_types = buff_manager.types
    except Exception as e:
        _log(f"buff_manager.types access failed: {type(e).__name__}: {e}")
        return None

    try:
        for buff_type in all_types.values():
            try:
                class_name = buff_type.__name__.lower()
                if mood_key in class_name and "generic" in class_name:
                    candidates.append(buff_type)
            except Exception:
                continue
    except Exception as e:
        _log(f"buff iteration failed: {type(e).__name__}: {e}")
        return None

    if not candidates:
        return None

    # Prefer the shortest match -- "Buff_Happy_Generic" wins over
    # "Buff_Happy_Generic_FromSocial_Reaction"
    candidates.sort(key=lambda b: len(b.__name__))
    return candidates[0]


def apply_mood(sim_info, mood_tag, reason=None):
    """
    Apply a moodlet buff to a sim based on a mood tag string.
    Returns True on success, False on any failure. Logs the outcome either way.
    """
    if not sim_info:
        _log("apply_mood called with no sim_info")
        return False
    if not mood_tag:
        _log("apply_mood called with no mood_tag")
        return False

    mood_key = mood_tag.strip().lower()
    if mood_key not in _MOOD_BUFF_IDS:
        _log(f"unknown mood '{mood_key}' -- not in mood table")
        return False

    sim_name = "?"
    try:
        sim_name = f"{sim_info.first_name} {sim_info.last_name}".strip()
    except Exception:
        pass

    try:
        import services
        import sims4.resources
    except Exception as e:
        _log(f"failed to import services/sims4.resources: {type(e).__name__}: {e}")
        return False

    try:
        buff_manager = services.get_instance_manager(sims4.resources.Types.BUFF)
    except Exception as e:
        _log(f"get_instance_manager(BUFF) failed: {type(e).__name__}: {e}")
        return False
    if not buff_manager:
        _log("buff_manager is None")
        return False

    # Try direct ID lookup
    buff_id = _MOOD_BUFF_IDS[mood_key]
    buff_type = None
    try:
        buff_type = buff_manager.get(buff_id)
    except Exception as e:
        _log(f"buff_manager.get({buff_id}) raised {type(e).__name__}: {e}")

    if not buff_type:
        _log(f"buff_id {buff_id} for '{mood_key}' not found; trying name search")
        buff_type = _find_buff_by_name(buff_manager, mood_key)
        if buff_type:
            _log(f"name search found: {buff_type.__name__}")

    if not buff_type:
        _log(f"no buff found for '{mood_key}' via ID or name -- giving up")
        return False

    # add_buff_from_op is the standard application method used by S4CL and
    # most working mods. It accepts the buff CLASS (not an instance).
    try:
        sim_info.add_buff_from_op(buff_type, buff_reason=None)
        _log(f"applied '{mood_key}' ({buff_type.__name__}) to {sim_name} -- reason: {reason}")
        return True
    except Exception as e:
        _log(f"add_buff_from_op failed for {sim_name}: {type(e).__name__}: {e}")

    # Last-resort fallback: try the simpler add_buff method
    try:
        sim_info.add_buff(buff_type, buff_reason=None)
        _log(f"applied via add_buff fallback: '{mood_key}' ({buff_type.__name__}) to {sim_name}")
        return True
    except Exception as e:
        _log(f"add_buff fallback also failed for {sim_name}: {type(e).__name__}: {e}")

    return False


def extract_mood_tag(text):
    """
    Extract a MOOD: tag from the end of generated text.
    Returns (clean_text, mood_tag) tuple.
    Also strips trailing separator lines (---, ===, ***) and markdown labels.
    """
    if not text:
        return text, None

    lines = text.rstrip().split("\n")
    mood = None

    # Walk backwards looking for the MOOD line and strip trailing junk along the way
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if last.upper().startswith("MOOD:"):
            mood = last.split(":", 1)[1].strip().lower()
            lines.pop()
            continue
        # Strip trailing separators like ---, ===, ***
        stripped_chars = set(last)
        if stripped_chars and stripped_chars.issubset(set("-=*_~ ")):
            lines.pop()
            continue
        break

    # Strip markdown formatting from remaining lines
    import re
    cleaned_lines = []
    for line in lines:
        # Remove **bold**, *italic*, __bold__, _italic_
        line = re.sub(r'\*\*(.+?)\*\*', r'\1', line)
        line = re.sub(r'__(.+?)__', r'\1', line)
        # Remove "Message 1:", "Message 2:" labels at line starts
        line = re.sub(r'^\s*\*?\*?Message\s*\d+\s*:?\*?\*?\s*', '', line, flags=re.IGNORECASE)
        cleaned_lines.append(line)

    clean_text = "\n".join(cleaned_lines).rstrip()
    return clean_text, mood
