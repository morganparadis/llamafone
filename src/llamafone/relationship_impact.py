"""
Relationship-impact from message sentiment.

When a phone call or text exchange completes, we already extract the
LLM's MOOD: tag (used to apply a moodlet on the recipient side, see
phone._apply_mood_from_text). This module extends that signal into
the sim's actual relationship state -- warm exchanges nudge friendship
up, hostile ones nudge it down, flirty ones nudge romance up, and so
on. Deltas are:

  * BIDIRECTIONAL - both directions of the pair's relationship get
    the same delta. A warm exchange lifts sim_A's opinion of sim_B
    AND sim_B's opinion of sim_A. Matches real dynamics.

  * SMALL and HARD-CAPPED per message. The default cap is +/-3 --
    well below Sims 4's own casual-social interaction deltas (which
    run 3-8), so an AI text nudges things without one message
    reshaping a relationship.

  * OPT-OUT via config knob `message_relationship_impact_enabled`.
    Default on (feels more alive).

Direction handling detail: Sims 4 stores per-direction relationship
scores. sim_A's relationship_tracker has entries keyed by other_sim_id.
tracker.add_relationship_score(other_sim_id, delta, track) modifies
sim_A's opinion of other_sim_id. We call it on both sides.

Track references: RELATIONSHIP_TRACKS live in relationships.global_relationship_tuning
GlobalRelationshipTuning as class attributes FRIENDSHIP_TRACK and
ROMANCE_TRACK. Fallback -- some builds expose these directly on the
sim relationship as `friendship_track` / `romance_track` accessors.
"""

import os
import datetime


_MODULE_TAG = "[relationship_impact]"


def _log(message):
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {_MODULE_TAG} {message}\n")
    except Exception:
        pass


# Base deltas per mood. Applied to BOTH the friendship and romance
# tracks, in BOTH directions. Ratio matters -- flirty is heavy on
# romance and light on friendship; playful bumps both; angry hits
# friendship harder than romance because a hostile message damages
# social trust more than physical attraction. Final delta is clamped
# to +/- max_delta (config knob) at apply time.
_MOOD_DELTAS = {
    #                 friendship, romance
    # Friendship: any positive social mood adds +1. Small enough that
    # a few messages don't reshape the relationship, but common enough
    # that friendly conversations accumulate meaningfully. Focused
    # stays at 0 (task-mode, not social).
    # Romance: only flirty (+2). Other moods describe the sender's own
    # state, not romantic interest.
    # Negative deltas stay stronger because a hostile message damages
    # trust faster than a warm one builds it.
    "happy":         ( 1,  0),
    "sad":           (-1,  0),
    "angry":         (-3, -1),
    "confident":     ( 1,  0),
    "flirty":        ( 0,  2),
    "playful":       ( 1,  0),
    "energized":     ( 1,  0),
    "focused":       ( 0,  0),
    "inspired":      ( 1,  0),
    "embarrassed":   ( 0, -1),
    "tense":         (-1, -1),
    "uncomfortable": (-1, -1),
    "bored":         (-1, -1),
    "dazed":         ( 0,  0),
}


def _clamp(v, cap):
    """Clamp v to [-cap, +cap]."""
    if v > cap:
        return cap
    if v < -cap:
        return -cap
    return v


# Base-game GUIDs for the two relationship tracks. Mirrors dating.py's
# proven approach -- direct GUID -> STATISTIC lookup via the instance
# manager. GlobalRelationshipTuning class attributes don't resolve
# reliably across pack combos, so GUID is the safer path.
_LTR_FRIENDSHIP_GUIDS = (16650,)
_LTR_ROMANCE_GUIDS = (16651,)

# Cache: [friendship_track, romance_track, resolved_flag].
_TRACK_CACHE = [None, None, False]


def _resolve_track_by_guid(im, guids):
    """Look up a Statistic tuning by GUID via the instance manager."""
    try:
        import sims4.resources
        from sims4.resources import Types
        for guid in guids:
            try:
                key = sims4.resources.get_resource_key(guid, Types.STATISTIC)
                found = im.get(key)
                if found is not None:
                    return found, guid
            except Exception:
                continue
    except Exception:
        pass
    return None, None


def _resolve_track_by_name(im, names):
    """Fallback: look up by class name via the instance manager's
    reverse index. Slower and less reliable than GUID but covers
    builds where GUIDs have shifted."""
    try:
        get_key = getattr(im, "get_key_for_name", None)
        if not callable(get_key):
            return None, None
        for name in names:
            try:
                key = get_key(name)
                if key is None:
                    continue
                found = im.get(key)
                if found is not None:
                    return found, name
            except Exception:
                continue
    except Exception:
        pass
    return None, None


def _get_tracks():
    """Return (friendship_track, romance_track) or (None, None). Cached
    after first successful resolve so we don't pay the lookup cost on
    every message. Logs which path succeeded so failures are diagnosable."""
    if _TRACK_CACHE[2]:
        return _TRACK_CACHE[0], _TRACK_CACHE[1]
    _TRACK_CACHE[2] = True
    try:
        import services
        from sims4.resources import Types
        im = services.get_instance_manager(Types.STATISTIC)
    except Exception as e:
        _log(f"instance_manager unavailable: {type(e).__name__}: {e}")
        return None, None
    # NAME-first now: 16650 for friendship was documented; 16651 for
    # romance was extrapolated and empirically may resolve to the WRONG
    # statistic. Name-based lookup via the instance manager is more
    # authoritative when it works. GUID stays as a fallback.
    f_track, tag = _resolve_track_by_name(im, (
        "LTR_Friendship_Main", "LTR_Friendship",
        "relationship_LTR_Friendship_Main",
    ))
    if f_track is not None:
        _log(f"friendship track resolved by name {tag!r} -> {getattr(f_track, '__name__', str(f_track))!r}")
    else:
        f_track, tag = _resolve_track_by_guid(im, _LTR_FRIENDSHIP_GUIDS)
        if f_track is not None:
            _log(f"friendship track resolved by GUID {tag} -> {getattr(f_track, '__name__', str(f_track))!r}")
        else:
            _log("friendship track unresolved via name or GUID")
    r_track, tag = _resolve_track_by_name(im, (
        "LTR_Romance_Main", "LTR_Romance",
        "relationship_LTR_Romance_Main",
    ))
    if r_track is not None:
        _log(f"romance track resolved by name {tag!r} -> {getattr(r_track, '__name__', str(r_track))!r}")
    else:
        r_track, tag = _resolve_track_by_guid(im, _LTR_ROMANCE_GUIDS)
        if r_track is not None:
            _log(f"romance track resolved by GUID {tag} -> {getattr(r_track, '__name__', str(r_track))!r}")
        else:
            _log("romance track unresolved via name or GUID")
            # Diagnostic: dump every STATISTIC whose name contains
            # 'romance' or 'ltr' so we can see what's available in
            # this build and pick the right one.
            try:
                candidates = []
                for stype in im.types.values():
                    try:
                        n = getattr(stype, "__name__", "") or ""
                        low = n.lower()
                        if "romance" in low or "ltr" in low:
                            candidates.append(n)
                    except Exception:
                        continue
                _log(
                    f"romance-track diagnostic: statistics matching "
                    f"'romance' or 'ltr' -> {candidates[:40]}"
                )
            except Exception:
                pass
    _TRACK_CACHE[0] = f_track
    _TRACK_CACHE[1] = r_track
    return f_track, r_track


def _apply_one_direction(source_si, target_si, friendship_delta, romance_delta):
    """Apply deltas to source_si's opinion of target_si.
    Uses relationship_tracker.add_relationship_score on the correct
    track. Silent no-op on any failure -- relationship-impact is a
    nice-to-have, never a load-critical operation."""
    if source_si is None or target_si is None:
        return
    try:
        target_id = getattr(target_si, "sim_id", None)
        if target_id is None:
            return
        tracker = getattr(source_si, "relationship_tracker", None)
        if tracker is None:
            return
        friendship_track, romance_track = _get_tracks()
        if friendship_delta and friendship_track is not None:
            try:
                tracker.add_relationship_score(int(target_id), friendship_delta, friendship_track)
            except Exception as e:
                _log(f"friendship apply failed source={getattr(source_si, 'first_name', '?')} "
                     f"target={getattr(target_si, 'first_name', '?')}: {type(e).__name__}: {e}")
        if romance_delta and romance_track is not None:
            try:
                tracker.add_relationship_score(int(target_id), romance_delta, romance_track)
            except Exception as e:
                _log(f"romance apply failed source={getattr(source_si, 'first_name', '?')} "
                     f"target={getattr(target_si, 'first_name', '?')}: {type(e).__name__}: {e}")
    except Exception as e:
        _log(f"_apply_one_direction outer failure: {type(e).__name__}: {e}")


def apply_from_mood(sim_a, sim_b, mood):
    """Look up the delta for `mood`, cap it against config, and apply
    to BOTH directions of the sim_a<->sim_b relationship. Bidirectional
    because a phone conversation affects how each sim feels about the
    other; one-directional application would make relationships drift
    asymmetrically over time.

    Silent no-op when:
      * config disables the feature
      * mood isn't in _MOOD_DELTAS (unknown / neutral)
      * either sim_info is None
      * the sims are the same sim (self-affinity)
      * relationship tracks aren't resolvable on this build
    """
    if sim_a is None or sim_b is None:
        return
    try:
        a_id = getattr(sim_a, "sim_id", None)
        b_id = getattr(sim_b, "sim_id", None)
        if a_id is None or b_id is None or int(a_id) == int(b_id):
            return
    except Exception:
        return
    if not mood:
        return
    mood_key = mood.strip().lower()
    delta = _MOOD_DELTAS.get(mood_key)
    if delta is None:
        return
    friendship_base, romance_base = delta
    if friendship_base == 0 and romance_base == 0:
        return

    from . import config as _config
    try:
        if not _config.get_message_relationship_impact_enabled():
            return
        cap = _config.get_message_relationship_max_delta()
    except Exception:
        cap = 3
    if cap <= 0:
        return

    friendship_delta = _clamp(friendship_base, cap)
    romance_delta = _clamp(romance_base, cap)
    if friendship_delta == 0 and romance_delta == 0:
        return

    _apply_one_direction(sim_a, sim_b, friendship_delta, romance_delta)
    _apply_one_direction(sim_b, sim_a, friendship_delta, romance_delta)

    _log(
        f"applied mood={mood_key!r} "
        f"friendship={friendship_delta:+d} romance={romance_delta:+d} "
        f"(cap={cap}) between "
        f"{getattr(sim_a, 'first_name', '?')} and "
        f"{getattr(sim_b, 'first_name', '?')}"
    )
