"""
Dating-app layer (v3.5).

Two flows, both about NEW sims (people the active sim has not met yet):

  1. INBOUND cold outreach -- an opted-in household sim occasionally
     receives a text from an unmet sim. The framing explains how the
     sender got their number: via a real mutual friend when one exists
     (someone in both sims' relationship trackers), otherwise via a
     "fun" made-up reason tied to the sender's traits/career/hobbies.

     When the player replies, the mod establishes a real Sims 4
     relationship between the two sims -- adds friendship score so the
     sender shows up in the relationship panel as an acquaintance.

  2. OUTBOUND intro -- new interaction on the Llamafone app that opens
     a sim picker filtered to eligible unmet sims. When the player
     picks one and writes their own intro text, the mod establishes
     the relationship and routes the message through the normal
     Llamafone text pipeline (LLM generates the recipient's reply).
     The mod NEVER generates the outgoing intro -- the player writes
     it themselves.

The feature is opt-in per played household sim. Opt-in state lives in
<save>/DatingOptIns.json so it travels with the save and doesn't
leak across households. Users who never toggle any sim on see zero
behavior change.

Design contract:
- No autonomous game-state mutation. Relationships get established
  only in response to a player action (replying to inbound, sending
  outbound).
- Orientation is read from CAS attraction preferences. No user-facing
  gender picker; sims whose prefs are empty just get no candidates
  (implicit opt-out).
- Committed sims (married / engaged / going steady) never appear on
  either side. Marrying a sim implicitly turns dating off for them.
"""

import json
import os
import random
import threading
import datetime

from . import config
from . import save_id as _save_id
from . import sim_context


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(msg):
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [dating] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-sim dating state (persisted in save folder)
# ---------------------------------------------------------------------------
#
# <save>/DatingOptIns.json now stores a dict keyed by sim_id, one entry per
# played household sim. Each entry captures:
#   - opted_in       : bool           # inbound cold outreach on/off
#   - gender_pref    : str            # 'auto'|'men'|'women'|'anyone'
#   - age_pref       : str            # 'auto'|'teen'|'young_adult'|'adult'|'elder'|'any'
#
# Schema v2. Load supports v1 (flat list of enabled sim_ids) so users
# who already opted a sim in don't lose that state after upgrade.
#
# The state lives in the save folder rather than llamafone.cfg because
# the same player may have several households across saves and
# shouldn't have to remember to toggle dating off for each.

_OPTIN_FILENAME = "DatingOptIns.json"
_optin_lock = threading.RLock()
_optin_cache = None
_optin_cached_for_save_id = None
# True when the in-memory cache has changes not yet written to disk.
# Every _optin_load() call checks this and retries the write if the
# save path is now resolvable, so a transient "no save id yet" at
# mutation time doesn't lose the user's preferences forever.
_optin_dirty = False

_DEFAULT_SIM_ENTRY = {
    "opted_in":         False,
    "gender_pref":      "anyone",
    "age_pref":         "auto",
    "bio":              "",   # player-written; sent with outbound intros
    "outreach_history": [],   # sim_ids that have already cold-outreached
                              # this sim. Excluded from future candidate
                              # pools so no sender reaches out twice.
}
_VALID_GENDER_PREFS = ("anyone", "men", "women")
_VALID_AGE_PREFS = ("auto", "teen", "young_adult", "adult", "elder", "any")
_MAX_PLAYER_BIO_LEN = 500


def _optin_path():
    return _save_id.data_path(_OPTIN_FILENAME)


def _default_optin_cache():
    """Empty schema-v2 state."""
    return {"schema_version": 2, "sims": {}}


def _optin_load():
    """Load per-sim dating state from disk. Cached per-save. Transient
    save_id=None during zone transitions preserves the last-known
    cache so a cold outreach fired mid-transition doesn't see an
    empty state.

    Handles schema v1 (flat list of enabled sim_ids) transparently
    by migrating in-memory on load; the next save call writes v2.

    Also opportunistically flushes any pending in-memory mutations to
    disk whenever a fresh call resolves a real save_id. This covers
    the case where save_id was unresolvable at the moment a preference
    was toggled (transient state during CAS / menu / etc.) -- the
    change sits in memory and gets persisted on the next call.
    """
    global _optin_cache, _optin_cached_for_save_id, _optin_dirty
    with _optin_lock:
        current = _save_id.get_current_save_id()
        if current is None and _optin_cache is not None:
            return _optin_cache
        # Retry a previously-failed save now that we might have a
        # resolvable save_id.
        if _optin_dirty and current is not None:
            _optin_save()
        if _optin_cache is not None and _optin_cached_for_save_id == current:
            return _optin_cache
        _optin_cached_for_save_id = current
        path = _optin_path()
        if path is None or not os.path.exists(path):
            _optin_cache = _default_optin_cache()
            return _optin_cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            _log(f"opt-in load failed: {type(e).__name__}: {e}")
            _optin_cache = _default_optin_cache()
            return _optin_cache
        if not isinstance(data, dict):
            _log(f"opt-in file at {path} malformed; treating as empty")
            _optin_cache = _default_optin_cache()
            return _optin_cache
        # v1: {"schema_version": 1, "enabled_sim_ids": [id, id, ...]}
        # v2: {"schema_version": 2, "sims": {id: {opted_in, gender_pref, age_pref}}}
        version = data.get("schema_version", 1)
        if version < 2 and isinstance(data.get("enabled_sim_ids"), list):
            sims = {}
            for sid in data["enabled_sim_ids"]:
                try:
                    sims[str(int(sid))] = dict(_DEFAULT_SIM_ENTRY, opted_in=True)
                except Exception:
                    continue
            _optin_cache = {"schema_version": 2, "sims": sims}
            _log(f"migrated opt-in schema v1 -> v2 ({len(sims)} sims)")
            return _optin_cache
        raw_sims = data.get("sims") if isinstance(data.get("sims"), dict) else {}
        sims = {}
        for sid_str, entry in raw_sims.items():
            if not isinstance(entry, dict):
                continue
            merged = dict(_DEFAULT_SIM_ENTRY)
            merged["opted_in"] = bool(entry.get("opted_in", False))
            gp = str(entry.get("gender_pref", "anyone")).lower()
            merged["gender_pref"] = gp if gp in _VALID_GENDER_PREFS else "anyone"
            ap = str(entry.get("age_pref", "auto")).lower()
            merged["age_pref"] = ap if ap in _VALID_AGE_PREFS else "auto"
            bio = str(entry.get("bio", ""))[:_MAX_PLAYER_BIO_LEN]
            merged["bio"] = bio
            raw_hist = entry.get("outreach_history")
            if isinstance(raw_hist, list):
                merged["outreach_history"] = [int(x) for x in raw_hist if x is not None]
            sims[str(sid_str)] = merged
        _optin_cache = {"schema_version": 2, "sims": sims}
        return _optin_cache


def _optin_save():
    """Persist the current per-sim dating state to disk. Callers should
    mutate _optin_cache under _optin_lock and then call this.

    When the save path isn't resolvable (transient no-save state
    during zone/CAS/menu transitions), sets the dirty flag instead
    of losing the mutation. _optin_load() opportunistically retries
    on subsequent calls when save_id becomes resolvable again."""
    global _optin_dirty
    with _optin_lock:
        path = _optin_path()
        if path is None:
            _optin_dirty = True
            _log("opt-in save deferred: no save path yet (will retry)")
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = _optin_cache or _default_optin_cache()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, path)
            _optin_dirty = False
        except Exception as e:
            _log(f"opt-in save failed: {type(e).__name__}: {e}")
            _optin_dirty = True


def get_sim_dating_entry(sim_id):
    """Return the full per-sim state dict (opted_in + prefs) for
    `sim_id`. Missing sims get the default (opted out, auto prefs)."""
    if sim_id is None:
        return dict(_DEFAULT_SIM_ENTRY)
    try:
        state = _optin_load()
        entry = state.get("sims", {}).get(str(int(sim_id)))
        if not entry:
            return dict(_DEFAULT_SIM_ENTRY)
        return dict(_DEFAULT_SIM_ENTRY, **entry)
    except Exception:
        return dict(_DEFAULT_SIM_ENTRY)


def _mutate_sim_entry(sim_id, **updates):
    """Apply `updates` to the per-sim entry for `sim_id`, persisting
    the change. Creates the entry from defaults if it doesn't exist."""
    if sim_id is None:
        return
    with _optin_lock:
        state = _optin_load()
        sims = state.setdefault("sims", {})
        sid = str(int(sim_id))
        entry = dict(_DEFAULT_SIM_ENTRY, **sims.get(sid, {}))
        entry.update(updates)
        # Sanity-clamp pref values so a bad caller can't wedge the file.
        if entry.get("gender_pref") not in _VALID_GENDER_PREFS:
            entry["gender_pref"] = "auto"
        if entry.get("age_pref") not in _VALID_AGE_PREFS:
            entry["age_pref"] = "auto"
        sims[sid] = entry
        _optin_save()


def is_sim_opted_in(sim_id):
    """True iff `sim_id` has opted in to inbound dating suggestions."""
    return bool(get_sim_dating_entry(sim_id).get("opted_in"))


def set_sim_opted_in(sim_id, enabled):
    """Toggle inbound opt-in for a specific sim. Persists immediately."""
    _mutate_sim_entry(sim_id, opted_in=bool(enabled))
    _log(f"opt-in set: sim_id={sim_id} -> {'ON' if enabled else 'OFF'}")


def get_sim_gender_pref(sim_id):
    """Read this sim's gender preference. Defaults to 'anyone' when
    the sim has never opened Llamadate settings -- we intentionally
    do NOT auto-detect from CAS. The user narrows the pool from the
    Llamadate settings picker if they want."""
    return get_sim_dating_entry(sim_id).get("gender_pref", "anyone")


def set_sim_gender_pref(sim_id, value):
    """Persist a gender preference for this sim. Value must be one
    of _VALID_GENDER_PREFS; anything else clamps to 'anyone'."""
    v = str(value).lower()
    if v not in _VALID_GENDER_PREFS:
        v = "anyone"
    _mutate_sim_entry(sim_id, gender_pref=v)
    _log(f"gender_pref set: sim_id={sim_id} -> {v}")


def get_sim_age_pref(sim_id):
    """Read this sim's age-preference override. 'auto' means the
    default age-gate rules apply (teens-teens; others within one tier)."""
    return get_sim_dating_entry(sim_id).get("age_pref", "auto")


def set_sim_age_pref(sim_id, value):
    """Persist an age-preference override for this sim."""
    v = str(value).lower()
    if v not in _VALID_AGE_PREFS:
        v = "auto"
    _mutate_sim_entry(sim_id, age_pref=v)
    _log(f"age_pref set: sim_id={sim_id} -> {v}")


def get_outreach_history(recipient_id):
    """Return the set of sim_ids that have already cold-outreached
    `recipient_id`. Used to prevent the same sender from ever
    reaching out twice."""
    if recipient_id is None:
        return set()
    hist = get_sim_dating_entry(recipient_id).get("outreach_history") or []
    try:
        return {int(x) for x in hist if x is not None}
    except Exception:
        return set()


def record_outreach(recipient_id, sender_id):
    """Persist that `sender_id` has now cold-outreached `recipient_id`.
    Future candidate pools for the recipient will exclude the sender."""
    if recipient_id is None or sender_id is None:
        return
    with _optin_lock:
        entry = get_sim_dating_entry(recipient_id)
        current = entry.get("outreach_history") or []
        try:
            sid = int(sender_id)
        except Exception:
            return
        if sid in current:
            return
        # De-dup + append
        new_hist = list({int(x) for x in current if x is not None} | {sid})
        _mutate_sim_entry(recipient_id, outreach_history=new_hist)
    _log(f"outreach recorded: recipient={recipient_id} sender={sender_id} "
         f"(total history: {len(new_hist)})")


def get_sim_player_bio(sim_id):
    """Read the player-authored Llamadate bio for this sim. Sent with
    every outbound Llamadate intro so the recipient's LLM has context
    for who's texting them. Empty string when the player hasn't set
    one -- the outbound flow just skips including a bio block in
    that case."""
    return get_sim_dating_entry(sim_id).get("bio", "") or ""


def set_sim_player_bio(sim_id, text):
    """Persist the player-written bio for this sim. Empty / whitespace
    input clears the bio. Trimmed to _MAX_PLAYER_BIO_LEN chars to
    keep the LLM prompt reasonable."""
    clean = (text or "").strip()[:_MAX_PLAYER_BIO_LEN]
    _mutate_sim_entry(sim_id, bio=clean)
    _log(f"player bio set: sim_id={sim_id} ({len(clean)} chars)")


def anyone_opted_in():
    """True iff at least one sim in the current save has opted in.
    Guards the cold-outreach cadence -- when no one's opted in, we
    skip the whole flow without picking a recipient."""
    try:
        for entry in _optin_load().get("sims", {}).values():
            if entry.get("opted_in"):
                return True
    except Exception:
        pass
    return False


def get_opted_in_sim_ids():
    """Return a set of sim_ids that have opted in (safe to iterate)."""
    out = set()
    try:
        for sid_str, entry in _optin_load().get("sims", {}).items():
            if entry.get("opted_in"):
                try:
                    out.add(int(sid_str))
                except Exception:
                    continue
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Orientation (auto-detect only)
# ---------------------------------------------------------------------------


def resolve_orientation(sim_info):
    """Return the sim's gender preference for filtering candidates:
    one of 'men', 'women', or 'anyone'.

    No CAS auto-detection -- we deliberately do NOT read attraction
    tracker or gender_preferences off the sim, because those APIs
    vary across pack combos (Lovestruck vs. Growing Together vs.
    base game) and the failure mode was silent empty pools when a
    probe path didn't match. The player sets a preference explicitly
    per sim from the Llamadate settings picker; default is 'anyone'.
    """
    if sim_info is None:
        return "anyone"
    sim_id = getattr(sim_info, "sim_id", None)
    if sim_id is None:
        return "anyone"
    try:
        return get_sim_gender_pref(sim_id)
    except Exception:
        return "anyone"


def _gender_matches_orientation(candidate_sim_info, orientation):
    """True iff the candidate's gender matches the orientation. None
    orientation blocks everyone (the seeker's prefs aren't readable)."""
    if orientation is None:
        return False
    if orientation == "anyone":
        return True
    try:
        g = str(getattr(candidate_sim_info, "gender", "")).upper()
        if "." in g:
            g = g.rsplit(".", 1)[-1]
        if orientation == "men":
            return g in ("MALE", "M")
        if orientation == "women":
            return g in ("FEMALE", "F")
    except Exception:
        pass
    return False


def _candidate_gender_str(candidate_si):
    """Return 'MALE' / 'FEMALE' / '' for the candidate. Empty string
    when the sim's gender attr is unreadable."""
    try:
        g = str(getattr(candidate_si, "gender", "")).upper()
        if "." in g:
            g = g.rsplit(".", 1)[-1]
        if g in ("MALE", "M"):
            return "MALE"
        if g in ("FEMALE", "F"):
            return "FEMALE"
    except Exception:
        pass
    return ""


def _candidate_is_attracted_to(candidate_si, seeker_gender):
    """True iff the candidate is (or plausibly could be) attracted to
    a sim of `seeker_gender`. Permissive: returns True whenever we
    can't confirm otherwise, so the pool doesn't collapse on sims
    with unreadable attraction data.

    We DO probe the standard Sims 4 gender-preference attribute
    paths here (unlike for the seeker, where we removed CAS
    auto-detection). Reason: the seeker's config lives in the player-
    facing Llamadate settings; candidates are unknown world sims and
    the only signal we have is their CAS preferences.

    Returns False ONLY when we successfully read the candidate's
    preferences AND the seeker's gender is definitively not in
    them."""
    if not seeker_gender:
        return True  # can't determine seeker gender -> don't filter
    # Try known attribute paths. Any that yields a mappable value
    # settles the question; if none do, we default to True.
    for attr in ("gender_preferences", "gender_preference",
                 "get_gender_preferences", "romantic_preferences",
                 "romantic_gender_preferences",
                 "get_romantic_gender_preferences"):
        try:
            val = getattr(candidate_si, attr, None)
            if callable(val):
                try:
                    val = val()
                except Exception:
                    continue
            if val is None:
                continue
            mapped = _map_gender_pref_to_seeker_match(val, seeker_gender)
            if mapped is not None:
                return mapped
        except Exception:
            continue
    for tracker_attr in ("attraction_tracker", "sim_attraction_tracker",
                         "_attraction_tracker"):
        try:
            tracker = getattr(candidate_si, tracker_attr, None)
            if tracker is None:
                continue
            for method_name in ("get_romantic_gender_preferences",
                                "get_romantic_gender_preference",
                                "get_gender_preferences",
                                "get_preferred_genders"):
                fn = getattr(tracker, method_name, None)
                if fn is None:
                    continue
                try:
                    val = fn() if callable(fn) else fn
                except Exception:
                    continue
                if val is None:
                    continue
                mapped = _map_gender_pref_to_seeker_match(val, seeker_gender)
                if mapped is not None:
                    return mapped
        except Exception:
            continue
    # Nothing readable -- assume they're open to the seeker's gender.
    return True


def _map_gender_pref_to_seeker_match(val, seeker_gender):
    """Given a raw gender-preference value (any of the shapes Sims 4
    uses) and the seeker's gender ('MALE'/'FEMALE'), return:
      True  -- the candidate IS attracted to seeker_gender
      False -- the candidate is NOT attracted to seeker_gender
      None  -- shape unrecognized; caller should treat as "unknown"

    Handles: enum set/list, dict {gender: bool}, single enum, orientation
    string like 'MEN_ONLY'."""
    try:
        # Dict shape: {gender: bool}. Interested-in genders have True.
        if hasattr(val, "items") and not isinstance(val, str):
            for k, v in val.items():
                s = str(k).upper()
                if "." in s:
                    s = s.rsplit(".", 1)[-1]
                if s in ("MALE", "M", "MAN", "MEN") and seeker_gender == "MALE" and bool(v):
                    return True
                if s in ("FEMALE", "F", "WOMAN", "WOMEN") and seeker_gender == "FEMALE" and bool(v):
                    return True
            # Fell through -- explicit dict, no True match for seeker_gender.
            # Only conclude False if the dict actually contained the seeker's
            # gender key (else the shape is ambiguous).
            for k in val.keys():
                s = str(k).upper()
                if "." in s:
                    s = s.rsplit(".", 1)[-1]
                if (seeker_gender == "MALE" and s in ("MALE", "M", "MAN", "MEN")) or \
                   (seeker_gender == "FEMALE" and s in ("FEMALE", "F", "WOMAN", "WOMEN")):
                    return False
            return None
        # String shape: named orientation
        if isinstance(val, str):
            s = val.upper()
            if "MEN_ONLY" in s or s == "MEN":
                return seeker_gender == "MALE"
            if "WOMEN_ONLY" in s or s == "WOMEN":
                return seeker_gender == "FEMALE"
            if "BOTH" in s or "ANYONE" in s or "BISEXUAL" in s:
                return True
            return None
        # Iterable shape: set/list/tuple of enum members
        if hasattr(val, "__iter__"):
            has_male = False
            has_female = False
            saw_any = False
            for v in val:
                if isinstance(v, tuple) and len(v) == 2:
                    if not bool(v[1]):
                        continue
                    v = v[0]
                s = str(v).upper()
                if "." in s:
                    s = s.rsplit(".", 1)[-1]
                if s in ("MALE", "M", "MAN", "MEN"):
                    has_male = True; saw_any = True
                elif s in ("FEMALE", "F", "WOMAN", "WOMEN"):
                    has_female = True; saw_any = True
            if not saw_any:
                return None
            if seeker_gender == "MALE":
                return has_male
            if seeker_gender == "FEMALE":
                return has_female
        # Single enum value
        s = str(val).upper()
        if "." in s:
            s = s.rsplit(".", 1)[-1]
        if s in ("MALE", "M", "MAN", "MEN"):
            return seeker_gender == "MALE"
        if s in ("FEMALE", "F", "WOMAN", "WOMEN"):
            return seeker_gender == "FEMALE"
        if s in ("BOTH", "ANYONE", "ANY", "BISEXUAL"):
            return True
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Age gate
# ---------------------------------------------------------------------------

_AGE_RANK = {
    "TEEN": 0,
    "YOUNGADULT": 1, "YOUNG_ADULT": 1,
    "ADULT": 2,
    "ELDER": 3,
}


def _age_rank(sim_info):
    """Return the sim's age rank (0..3) or None if child-or-below /
    unreadable. Children being None means _pass_age_gate skips them."""
    try:
        raw = str(getattr(sim_info, "age", "")).replace("Age.", "").upper()
        return _AGE_RANK.get(raw)
    except Exception:
        return None


def _pass_age_gate(seeker_rank, candidate_rank, max_tier_gap=1):
    """Teens can only match teens; everyone else matches within
    max_tier_gap tiers. None inputs (child-or-below) never pass."""
    if seeker_rank is None or candidate_rank is None:
        return False
    if seeker_rank == 0 or candidate_rank == 0:
        return seeker_rank == 0 and candidate_rank == 0
    return abs(seeker_rank - candidate_rank) <= max_tier_gap


_AGE_PREF_TO_RANK = {
    "teen": 0,
    "young_adult": 1,
    "adult": 2,
    "elder": 3,
}


def _pass_age_pref(age_pref, candidate_rank, seeker_rank):
    """Apply the per-sim age preference override with a hard safety
    rule on top: teens never match non-teens, and non-teens never
    match teens -- regardless of what age_pref is set to. This
    can't be overridden from settings; adult-teen matches are a
    non-starter for a dating app.

    'auto' uses the standard rule (teens-teens; adults within one
    tier of their own age). An explicit `age_pref` narrows within
    the seeker's own age band only.
    """
    if candidate_rank is None or seeker_rank is None:
        return False
    # Hard cross-tier block: teen <-> non-teen is never allowed.
    if (seeker_rank == 0) != (candidate_rank == 0):
        return False
    # Teen seekers only ever see other teens; age_pref is ignored.
    if seeker_rank == 0:
        return candidate_rank == 0
    # Adult seekers -- apply age_pref within adult tiers only.
    if age_pref == "auto" or not age_pref:
        return abs(seeker_rank - candidate_rank) <= 1
    if age_pref == "any":
        return candidate_rank >= 1
    target = _AGE_PREF_TO_RANK.get(age_pref)
    if target is None or target == 0:
        # Configured target is 'teen' but seeker is adult -- blocked
        # by the hard rule above. Return False for consistency.
        return False
    return candidate_rank == target


# ---------------------------------------------------------------------------
# "Sim is committed" check (uses relationship_tracker, not text status)
# ---------------------------------------------------------------------------
#
# We check relationship BITS directly on the sim_info's relationship
# tracker rather than a text-status keyword scan, because the candidate
# pool now comes from sim_info_manager rather than the seeker's
# relationship network -- there's no "status" string to inspect.

_COMMITTED_BIT_NAME_HINTS = (
    "married", "spouse", "engaged", "fiance",
    "partner", "goingsteady", "going_steady",
    "boyfriend", "girlfriend", "sweetheart",
    "soulmate", "dating",
)


def _sim_is_committed(sim_info):
    """True iff `sim_info` is in a committed romantic relationship
    (married, engaged, going steady, boyfriend/girlfriend).

    Delegates to phone._get_romantic_partner_info -- that function
    is the mod's canonical committed-partner detector, used by the
    reply system to describe relationships correctly. It uses
    tracker.target_sim_gen() + get_all_bits() + explicit spouse /
    engaged / going-steady bit-name matching, which has proven
    reliable across pack combos where our own bit scan missed.
    """
    if sim_info is None:
        return False
    try:
        from . import phone as _phone
        partner, _status = _phone._get_romantic_partner_info(sim_info)
        return partner is not None
    except Exception as e:
        _log(f"_sim_is_committed: phone._get_romantic_partner_info raised: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Candidate pool: unmet sims filtered for eligibility
# ---------------------------------------------------------------------------
#
# "Unmet" means: not currently in the seeker's relationship_tracker.
# Sims 4 populates the tracker whenever two sims interact at all, so
# anyone the player has met even briefly gets excluded. Household
# members and family are also excluded (as an extra guard beyond the
# tracker check).


def _seeker_known_sim_ids(seeker_si):
    """IDs of sims the seeker has any relationship with -- these are
    already 'met' and get filtered out of the candidate pool.

    Duck-types multiple access patterns: the private `_relationships`
    dict works on most builds, but some pack updates shift the tracker
    layout, so we also try the iterable + generator forms.
    """
    ids = set()
    tracker = getattr(seeker_si, "relationship_tracker", None)
    if tracker is None:
        return ids
    # Path 1: private _relationships dict (works on most Sims 4 builds)
    try:
        for other_id in getattr(tracker, "_relationships", {}).keys():
            try:
                ids.add(int(other_id))
            except Exception:
                continue
    except Exception:
        pass
    # Path 2: target_sim_id_gen() -- public generator when available
    try:
        gen = getattr(tracker, "target_sim_id_gen", None)
        if callable(gen):
            for sid in gen():
                if sid is not None:
                    try:
                        ids.add(int(sid))
                    except Exception:
                        continue
    except Exception:
        pass
    # Path 3: iterate the tracker directly; each Relationship exposes
    # its target as get_other_sim_info / other_sim_info_id / similar.
    try:
        for rel in list(tracker):
            for attr in ("target_sim_id", "sim_id_b", "other_sim_id"):
                try:
                    v = getattr(rel, attr, None)
                    if v is not None:
                        ids.add(int(v))
                        break
                except Exception:
                    continue
    except Exception:
        pass
    return ids


def _seeker_knows_candidate(seeker_si, candidate_id, known_ids):
    """True if the seeker has any relationship record with the candidate.
    Belt-and-suspenders over `_seeker_known_sim_ids`: enumeration paths
    on the tracker occasionally miss entries (varies by pack combo /
    Sims 4 version), so also probe the tracker directly per-candidate
    via get_relationship_score. A non-zero score is proof of an
    existing relationship even if enumeration didn't surface the ID.
    """
    try:
        cid = int(candidate_id)
    except Exception:
        return False
    if cid in known_ids:
        return True
    tracker = getattr(seeker_si, "relationship_tracker", None)
    if tracker is None:
        return False
    for method in ("get_relationship_score", "get_friendship_track_score",
                   "get_romance_track_score"):
        fn = getattr(tracker, method, None)
        if not callable(fn):
            continue
        try:
            val = fn(cid)
            if val:  # non-zero, non-None
                return True
        except Exception:
            continue
    fn = getattr(tracker, "has_relationship", None)
    if callable(fn):
        try:
            if fn(cid):
                return True
        except Exception:
            pass
    return False


def _in_seeker_household(seeker_si, candidate_si):
    try:
        hh_a = getattr(seeker_si, "household", None)
        hh_b = getattr(candidate_si, "household", None)
        if hh_a is None or hh_b is None:
            return False
        return getattr(hh_a, "id", None) == getattr(hh_b, "id", None)
    except Exception:
        return False


def _all_world_sims():
    """Iterate every sim_info the game currently has loaded. Includes
    townies, non-household played sims, and NPCs. Returns [] if the
    services module isn't importable (shouldn't happen in-game)."""
    try:
        import services
        mgr = services.sim_info_manager()
        if mgr is None:
            return []
        return list(mgr.get_all())
    except Exception:
        return []


def get_candidates_for(seeker_si):
    """Return a list of contact dicts for sims eligible to text
    `seeker_si` out of the blue. Filters:

      1. Not the seeker.
      2. Not in the seeker's household.
      3. Not already in the seeker's relationship tracker (unmet).
      4. Age-appropriate (teens only match teens; others within one
         tier of the seeker's age).
      5. Gender matches the seeker's orientation.
      6. Not committed to anyone.

    Contact dict shape matches what phone.generate_text_for expects:
      {"sim_info", "sim_id", "name", "friendship", "romance", "status",
       "in_household"}.

    Emits a per-filter breakdown to Llamafone_Log.txt so a "0 candidates"
    result is diagnosable without adding runtime probes -- otherwise
    every empty-pool complaint requires guessing which filter dropped
    everyone.
    """
    if seeker_si is None:
        _log("candidates: seeker_si is None; returning []")
        return []
    seeker_name = getattr(seeker_si, "first_name", "?")
    seeker_id = getattr(seeker_si, "sim_id", None)
    orientation = resolve_orientation(seeker_si)
    age_pref = get_sim_age_pref(seeker_id) if seeker_id is not None else "auto"
    seeker_rank = _age_rank(seeker_si)
    known_ids = _seeker_known_sim_ids(seeker_si)
    # Also exclude any sim that has already cold-outreached this
    # recipient in a prior session -- no sim reaches out twice.
    prior_outreach = get_outreach_history(seeker_id) if seeker_id is not None else set()
    world = _all_world_sims()

    seeker_gender = _candidate_gender_str(seeker_si)
    counts = {
        "world": len(world),
        "known": len(known_ids),
        "prior_outreach": len(prior_outreach),
        "self": 0, "non_human": 0, "already_known": 0, "same_household": 0,
        "age_gate": 0, "gender_gate": 0, "orientation_mismatch": 0,
        "committed": 0, "already_outreached": 0,
        "error": 0, "eligible": 0,
    }

    candidates = []
    for si in world:
        try:
            sid = getattr(si, "sim_id", None)
            if sid is None or sid == seeker_id:
                counts["self"] += 1
                continue
            if not _is_human_sim(si):
                counts["non_human"] += 1
                continue
            if _seeker_knows_candidate(seeker_si, sid, known_ids):
                counts["already_known"] += 1
                continue
            if int(sid) in prior_outreach:
                counts["already_outreached"] += 1
                continue
            if _in_seeker_household(seeker_si, si):
                counts["same_household"] += 1
                continue
            if not _pass_age_pref(age_pref, _age_rank(si), seeker_rank):
                counts["age_gate"] += 1
                continue
            if not _gender_matches_orientation(si, orientation):
                counts["gender_gate"] += 1
                continue
            # Mutual orientation check: the candidate must be plausibly
            # attracted to the seeker's gender. Permissive fallback --
            # if we can't read the candidate's CAS preferences (base
            # game or older sims), we include them and let the LLM
            # decide interest based on bios.
            if not _candidate_is_attracted_to(si, seeker_gender):
                counts["orientation_mismatch"] += 1
                continue
            if _sim_is_committed(si):
                counts["committed"] += 1
                continue
            counts["eligible"] += 1
            candidates.append({
                "sim_info": si,
                "sim_id": sid,
                "name": f"{getattr(si, 'first_name', '?')} {getattr(si, 'last_name', '')}".strip(),
                "friendship": 0,
                "romance": 0,
                "status": "",
                "in_household": False,
            })
        except Exception:
            counts["error"] += 1
            continue
    _log(f"candidates for {seeker_name} (age_rank={seeker_rank}, "
         f"orientation={orientation}, age_pref={age_pref}): {counts}")
    # Cap the pool so the sim picker isn't rendering hundreds of
    # thumbnails at once. Random sample so revisiting the picker
    # eventually surfaces different candidates instead of showing
    # the same top-of-list every time.
    if len(candidates) > _CANDIDATE_PICKER_CAP:
        candidates = random.sample(candidates, _CANDIDATE_PICKER_CAP)
        _log(f"candidates capped from {counts['eligible']} to "
             f"{_CANDIDATE_PICKER_CAP} for picker perf")
    return candidates


_CANDIDATE_PICKER_CAP = 30


def _is_human_sim(sim_info):
    """True iff the sim is a human (as opposed to a pet or horse).

    Detection strategy is permissive: default to True unless we get
    a definitive "yes, this is a pet" signal. Better to occasionally
    let a pet through than to reject the entire world because the
    species API returns something unexpected.

    Uses is_pet / is_dog / is_cat / is_horse properties -- those are
    stable across Cats & Dogs and Horse Ranch. The species enum
    check is a last-resort fallback because its stringification
    varies wildly across pack combos (sometimes 'Species.HUMAN',
    sometimes just the int value, sometimes SpeciesExtended.*).
    """
    # Definite pet? Reject.
    for attr in ("is_pet", "is_dog", "is_cat", "is_horse"):
        try:
            val = getattr(sim_info, attr, None)
            if val is None:
                continue
            is_animal = bool(val() if callable(val) else val)
            if is_animal:
                return False
        except Exception:
            continue
    # Explicit is_human? Trust it.
    try:
        val = getattr(sim_info, "is_human", None)
        if val is not None:
            return bool(val() if callable(val) else val)
    except Exception:
        pass
    # Fallback: species enum. Only REJECT if the string clearly
    # matches a known pet species; otherwise trust it's human.
    try:
        species = getattr(sim_info, "species", None)
        if species is None:
            return True
        s = str(species).upper()
        if "." in s:
            s = s.rsplit(".", 1)[-1]
        pet_names = ("SMALL_DOG", "SMALLDOG", "LARGE_DOG", "LARGEDOG",
                     "DOG", "CAT", "HORSE", "FOX")
        if s in pet_names:
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Mutual-friend lookup
# ---------------------------------------------------------------------------


def find_mutual_friend(seeker_si, other_si):
    """Return a sim_info that has a REAL friendship with BOTH
    seeker_si and other_si (someone who could plausibly have shared
    the number). None when no such sim exists.

    A "mutual" requires more than being in both sims' relationship
    trackers -- Sims 4 populates the tracker for anyone the sim has
    ever brushed past, and using bare tracker entries surfaced
    strangers like "Sergio gave me your number" when the player had
    never actually interacted with Sergio. Both directions must have
    at least MIN_MUTUAL_FRIENDSHIP_SCORE friendship for the candidate
    to count.

    When multiple qualifying mutuals exist, prefers the one with the
    highest combined friendship score across the two directions.
    """
    if seeker_si is None or other_si is None:
        return None
    seeker_id = getattr(seeker_si, "sim_id", None)
    other_id = getattr(other_si, "sim_id", None)
    if seeker_id is None or other_id is None:
        return None
    seeker_id = int(seeker_id)
    other_id = int(other_id)
    seeker_known = _seeker_known_sim_ids(seeker_si)
    other_known = _seeker_known_sim_ids(other_si)
    # Exclude the seeker and target themselves from the mutual pool.
    # Some Sims 4 build combos surface self-entries in _relationships,
    # which used to make the target show up as their own "mutual."
    mutual_ids = (seeker_known & other_known) - {seeker_id, other_id}
    if not mutual_ids:
        return None
    # Resolve to sim_infos and score by friendship if we can.
    try:
        import services
        mgr = services.sim_info_manager()
    except Exception:
        mgr = None
    best = None
    best_score = -1
    for mid in mutual_ids:
        try:
            si = mgr.get(mid) if mgr is not None else None
            if si is None:
                continue
            # Skip family / household members as mutuals -- if my mom
            # gave someone my number, that's a weirdly parental setup
            # and not the flavor we want.
            if _in_seeker_household(seeker_si, si):
                continue
            # Both sides must have a real friendship for this sim to
            # plausibly be "our mutual friend." Sims 4 populates the
            # relationship_tracker for any sim you've ever brushed
            # past, so bare tracker entries surfaced strangers like
            # "Sergio gave me your number" when the player had never
            # actually built a relationship with Sergio.
            seeker_score = _friendship_score(seeker_si, mid)
            other_score = _friendship_score(other_si, mid)
            if seeker_score < _MIN_MUTUAL_FRIENDSHIP_SCORE:
                continue
            if other_score < _MIN_MUTUAL_FRIENDSHIP_SCORE:
                continue
            score = seeker_score + other_score
            if score > best_score:
                best = si
                best_score = score
        except Exception:
            continue
    return best


# Minimum friendship on BOTH sides for a mutual to count. Sims 4
# scores run -100..100; 20 puts us above the "just met" tier but
# below "close friend." Effectively: "you've actually hung out."
_MIN_MUTUAL_FRIENDSHIP_SCORE = 20


def _friendship_score(sim_info, other_id):
    """Best-effort readout of the friendship score between sim_info
    and other_id. Returns 0 on any failure -- we only use this for
    ranking mutuals, so 0 just deprioritizes rather than crashes."""
    try:
        tracker = getattr(sim_info, "relationship_tracker", None)
        if tracker is None:
            return 0
        # Try the public API first, fall back to internal storage.
        get_score = getattr(tracker, "get_friendship_score", None)
        if callable(get_score):
            try:
                return int(get_score(other_id))
            except Exception:
                pass
        rel = getattr(tracker, "_relationships", {}).get(int(other_id))
        if rel is None:
            return 0
        return int(getattr(rel, "friendship_score", 0) or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Narrative frame -- explains how the sender got the seeker's number
# ---------------------------------------------------------------------------


def _is_personality_trait(trait):
    """True iff this equipped trait is a CAS personality trait (the ones
    the player picks: Outgoing, Neat, Genius, etc.). Filters out
    hidden system traits, attraction-preference traits, aspiration-
    reward traits, and mod-injected "trait" tunings that pollute the
    tracker (WW / Lovestruck / etc. inject dozens of these).

    Detection is permissive on the trait_type side (accept
    PERSONALITY, GAMEPLAY, LIFESTYLE, QUIRK) but strict on the
    name side: reject anything whose tuning name has telltale
    substrings for hidden/system traits.
    """
    if trait is None:
        return False
    # Cheap name blocklist first -- catches almost everything junky.
    name = str(getattr(trait, "__name__", "")).lower()
    if not name:
        return False
    junk_hints = (
        "attraction", "preference", "hidden", "reward",
        "injection", "inject", "loyalty", "characteristics",
        "genderoptions", "relexpectations", "occult_no", "nooccult",
        "cannotimpregnate", "canimpregnate", "canbeimpregnated",
        "toddlerskill", "skill_",  # skill-derived personality bumps
        "invisible",
    )
    for h in junk_hints:
        if h in name:
            return False
    # Prefer type-based check when the trait exposes one.
    try:
        for attr in ("is_personality_trait",):
            v = getattr(trait, attr, None)
            if v is not None:
                if bool(v() if callable(v) else v):
                    return True
        ttype = getattr(trait, "trait_type", None)
        if ttype is not None:
            ttype_str = str(ttype).upper()
            if any(k in ttype_str for k in ("PERSONALITY", "GAMEPLAY", "LIFESTYLE", "QUIRK")):
                return True
            # If we recognized the type as something else (HIDDEN,
            # REWARD, ATTRACTION), reject explicitly rather than
            # falling through.
            return False
    except Exception:
        pass
    # No type info and passed the name filter -- probably a real
    # personality trait. Better to include than miss.
    return True


def _clean_trait_name(raw_name):
    """Turn a trait tuning name like `trait_Outgoing_Main` into a
    display-friendly `Outgoing`. Strips common Sims 4 tuning prefixes/
    suffixes and title-cases the rest."""
    s = str(raw_name or "")
    # Common prefixes to strip
    for prefix in ("trait_", "Trait_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # Common suffixes to strip (Sims 4 tunings often end with _Main,
    # _Human, _NoCastMotivating, etc. -- keep just the concept)
    for suffix in ("_Main", "_Human", "_Trait"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.replace("_", " ").strip()
    return " ".join(w.capitalize() for w in s.split()) if s else ""


def _sender_flavor_facts(sender_si):
    """Collect concrete facts about the sender the LLM can build a
    plausible bio (or number-getting reason) from. Career, aspiration,
    top traits, standout skills. Anything that anchors the LLM in
    something the sim already IS instead of pure invention."""
    facts = []
    try:
        career_track = None
        careers = getattr(sender_si, "career_tracker", None)
        if careers is not None:
            for c in list(getattr(careers, "_careers", {}).values()):
                if getattr(c, "guid64", None) is not None:
                    career_track = c
                    break
        if career_track is not None:
            level = getattr(career_track, "level", None)
            name = str(getattr(career_track, "__name__", "")).replace("_", " ").title()
            if name:
                facts.append(f"career: {name}" + (f" (level {level})" if level else ""))
    except Exception:
        pass
    try:
        # Aspiration -- Sims 4's big-picture goal. Adds a lot of flavor
        # (Bodybuilder vs Party Animal vs Successful Lineage). Reads
        # from primary_aspiration on sim_info; the tracker also lists
        # any completed / current aspirations but primary is enough.
        asp = getattr(sender_si, "primary_aspiration", None)
        if asp is not None:
            asp_name = str(getattr(asp, "__name__", "")).replace("aspiration_", "").replace("_", " ").title()
            if asp_name:
                facts.append(f"aspiration: {asp_name}")
    except Exception:
        pass
    try:
        trait_tracker = getattr(sender_si, "trait_tracker", None)
        if trait_tracker is not None:
            trait_names = []
            for t in list(getattr(trait_tracker, "_equipped_traits", [])):
                if not _is_personality_trait(t):
                    continue
                nm = _clean_trait_name(getattr(t, "__name__", ""))
                if nm:
                    trait_names.append(nm)
            if trait_names:
                facts.append("traits: " + ", ".join(trait_names[:4]))
    except Exception:
        pass
    try:
        skills = []
        stat_tracker = getattr(sender_si, "statistic_tracker", None)
        if stat_tracker is not None:
            for stat in list(getattr(stat_tracker, "_statistics", {}).values()):
                cls = type(stat).__name__
                if "Skill" in cls:
                    level = int(getattr(stat, "get_user_value", lambda: 0)() or 0)
                    if level >= 5:
                        nm = str(getattr(stat, "__name__", cls)).replace("statistic_skill_", "").replace("_", " ").title()
                        skills.append((level, nm))
        if skills:
            skills.sort(reverse=True)
            top = [f"{n} (lv {l})" for l, n in skills[:3]]
            facts.append("top skills: " + ", ".join(top))
    except Exception:
        pass
    return facts


# Variety directive. LLMs default to warm-hedge openers that any
# sim could send to any other -- "love the energy already", "your
# bio is amazing", "hey stranger". Rather than enumerate a list of
# banned phrases (whack-a-mole -- the model just picks the next
# generic warm-hedge), tell it to pick something SPECIFIC to this
# sender's personality/career/situation.
_BANNED_OPENERS_BLOCK = (
    "\nOPENER VARIETY: Vary opener shape, register, and specificity. "
    "Do NOT open with a generic warm-hedge or compliment that any "
    "sim could send to any other -- pick something rooted in this "
    "specific sender's personality, career, or situation. An opener "
    "only they would write. If you find yourself reaching for a "
    "universally-safe warm opener, scrap it and try again with "
    "something particular to this sim."
)


def build_narrative_frame(seeker_si, sender_si):
    """Build the prompt suffix passed to phone.generate_text_for.
    Two very different framings depending on whether there's a real
    mutual friend or not:

      - MUTUAL mode: {mutual} introduced you IRL. Warm friend-of-a-
        friend vibe. NEVER mentions Llamadate, dating apps, matching,
        or profiles -- feels like a normal social introduction.

      - SOLO mode: pure stranger via Llamadate. Explicit dating-app
        framing with an invented "how I saw your profile / where I
        heard about you" reason tied to the sender's traits.
    """
    seeker_name = getattr(seeker_si, "first_name", "the recipient") if seeker_si else "the recipient"
    sender_name = getattr(sender_si, "first_name", "the sender") if sender_si else "the sender"
    mutual = find_mutual_friend(seeker_si, sender_si)

    if mutual is not None:
        mutual_name = getattr(mutual, "first_name", None) or "a mutual friend"
        _log(f"narrative: mutual '{mutual_name}' between "
             f"{seeker_name} and {sender_name}")
        # Friend-of-friend intro -- warm, personal, NOT a dating-app
        # framing. The LLM should sound like someone reaching out
        # because a shared friend suggested they'd hit it off, not
        # like a match from an app.
        return (
            "\n\n=== FIRST-CONTACT: MUTUAL-FRIEND INTRO ===\n"
            f"This is the FIRST time {sender_name} has ever texted "
            f"{seeker_name}. {mutual_name}, a friend of both of "
            f"them, gave {sender_name} the number. Frame this like "
            "an organic friend-of-a-friend introduction -- warm, "
            "low-stakes, unforced.\n\n"
            "HARD RULES for this message:\n"
            f"- Mention {mutual_name} naturally in the opener "
            "(e.g. 'hey, {mutual_name} gave me your number', 'hi -- "
            f"{mutual_name} thought we should meet').\n"
            "- Do NOT mention Llamadate, dating apps, matching, "
            "profiles, or 'saw your bio' anywhere. This is NOT an "
            "app match, it's a personal referral.\n"
            "- Keep it short: 1-2 texts, natural voice.\n"
            "- Tone tracks the sender's actual traits. A Cheerful "
            "sim is warm, a Snob is picky, a Mean sim is dismissive "
            "or provocative, a Romantic gets soft, a Flirty sim can "
            "be forward, a Sleaze/Slob can be sketchy or crass. Don't "
            "sand off the personality just because this is a first "
            "message -- their voice is the point.\n"
            f"{_BANNED_OPENERS_BLOCK}"
        )

    # No mutual -- pure Llamadate cold outreach.
    facts = _sender_flavor_facts(sender_si)
    facts_block = "; ".join(facts) if facts else "(nothing specific known about you)"
    _log(f"narrative: solo (no mutual) with facts={facts}")
    return (
        "\n\n=== FIRST-CONTACT: LLAMADATE MATCH ===\n"
        f"This is the FIRST time {sender_name} has ever texted "
        f"{seeker_name}. They matched on Llamadate (a dating app) "
        f"and {sender_name} is reaching out based on the profile "
        "they saw. Frame this like a first message on a dating app.\n\n"
        "HARD RULES for this message:\n"
        "- Reference how you saw them on Llamadate / their profile "
        "somewhere in the opener -- casually, not stiffly.\n"
        f"- You have NEVER met this person before. Do NOT invent "
        "shared history or say things like 'I've seen you around', "
        "'I think I saw you at [place]', 'you look familiar', 'we "
        "might do the same gym / coffee shop / bar'. You know them "
        "only from their profile. Speak like someone who found an "
        "interesting stranger online, NOT like someone reintroducing "
        "themselves.\n"
        f"- Ground the opener in something SPECIFIC -- either a "
        "detail you're reacting to from their profile, or something "
        f"real about YOU that says who you are. Your facts: {facts_block}. "
        "Use these to color your voice; don't claim they overlap with "
        "the recipient's world.\n"
        "- Keep it short (1-2 texts).\n"
        "- Tone tracks the sender's actual traits. A Cheerful sim is "
        "warm, a Snob is picky, a Mean sim is provocative, a "
        "Romantic gets soft, a Flirty sim can be forward, a Sleaze "
        "or Slob can be sketchy or crass. Don't sand off the "
        "personality to sound universally polite -- their voice is "
        "the point.\n"
        f"{_BANNED_OPENERS_BLOCK}"
    )


# ---------------------------------------------------------------------------
# Cold outreach entrypoint (called by auto_events)
# ---------------------------------------------------------------------------


def cold_outreach_enabled():
    """True iff at least one household sim is opted in AND the
    frequency weight is > 0. Auto_events checks this before rolling
    for a dating event."""
    if not anyone_opted_in():
        return False
    try:
        return config.get_dating_cold_outreach_weight() > 0
    except Exception:
        return True


def _pick_recipient():
    """Pick which opted-in household sim receives the cold outreach.
    Preferences (in order):
      1. Active sim if opted-in AND has candidates.
      2. Any opted-in household sim with candidates.
      3. None (skip this cycle).

    We deliberately do NOT filter out committed (married / engaged)
    recipients here. Opting a sim in is an explicit player choice --
    if the player wants their engaged sim to receive Llamadate
    matches for roleplay / drama reasons, we respect that. The
    committed status is hidden from the sender via the bio-only
    recipient_override so the incoming message doesn't reference
    the engagement. Committed CANDIDATES (senders) are still
    filtered out in get_candidates_for -- a married stranger
    cold-outreaching from an affair is a different flavor and
    isn't part of this feature.
    """
    def _eligible(si):
        if si is None:
            return False
        sid = getattr(si, "sim_id", None)
        if not is_sim_opted_in(sid):
            return False
        return bool(get_candidates_for(si))

    try:
        active = sim_context.get_main_sim_info()
        if _eligible(active):
            return active
    except Exception:
        pass
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return None
        for si in hh.sim_info_gen():
            if _eligible(si):
                return si
    except Exception:
        pass
    return None


# Percent chance we pick a candidate WITH a mutual friend when any
# such candidate exists in the pool. The rest of the time we pick
# uniformly at random (which may still surface a mutual candidate --
# this bias just weights the roll toward "someone your friend
# introduced" over "pure stranger from the app".
_MUTUAL_PREFERENCE_PCT = 70


def _pick_candidate_biased(recipient, candidates):
    """Pick one candidate from `candidates`. Biases the roll toward
    candidates who share a real mutual friend with `recipient` --
    those messages carry the "Alice gave me your number" narrative
    which feels more organic than pure-stranger cold outreach.

    Selection rules:
      - If any candidate has a mutual AND the roll (0..100) is under
        _MUTUAL_PREFERENCE_PCT: pick uniformly from the mutual subset.
      - Otherwise: pick uniformly from the full pool.

    Logging emits the bucket the picked candidate came from so the
    user can see how often mutual vs solo actually fires.
    """
    if not candidates:
        return None
    mutual_pool = []
    for c in candidates:
        try:
            if find_mutual_friend(recipient, c.get("sim_info")) is not None:
                mutual_pool.append(c)
        except Exception:
            continue
    if mutual_pool and random.randint(1, 100) <= _MUTUAL_PREFERENCE_PCT:
        pick = random.choice(mutual_pool)
        _log(f"candidate pick: MUTUAL bucket "
             f"({len(mutual_pool)} of {len(candidates)} candidates), "
             f"chose {pick.get('name', '?')}")
        return pick
    pick = random.choice(candidates)
    bucket = "mutual" if pick in mutual_pool else "solo"
    _log(f"candidate pick: RANDOM roll (bucket ended up: {bucket}, "
         f"mutual_pool={len(mutual_pool)}), chose {pick.get('name', '?')}")
    return pick


def generate_cold_outreach(callback=None, output=None):
    """Fire one inbound cold-outreach text. Called by auto_events when
    the 'dating' event type rolls. Returns silently if no opted-in
    sim has any candidates.

    Same recipient_override philosophy as outbound: when the recipient
    has authored a Llamadate bio, the sender's LLM sees ONLY that bio
    -- not the recipient's career / engagement / mood / world. Prevents
    a stranger from referencing your engagement, kids, job title, etc.
    when all they "know" is your dating-app profile.
    """
    if not cold_outreach_enabled():
        return
    recipient = _pick_recipient()
    if recipient is None:
        _log("cold outreach skipped: no opted-in sim has candidates")
        return
    candidates = get_candidates_for(recipient)
    if not candidates:
        return  # _pick_recipient already checked; belt and suspenders
    contact = _pick_candidate_biased(recipient, candidates)
    sender_si = contact.get("sim_info")
    _log(f"cold outreach: {contact.get('name', '?')} -> "
         f"{getattr(recipient, 'first_name', '?')}")
    frame = build_narrative_frame(recipient, sender_si)
    # Bio-only recipient view (falls back to full context if no bio).
    try:
        recipient_override = build_outbound_recipient_override(recipient)
    except Exception as e:
        _log(f"build_outbound_recipient_override raised: {type(e).__name__}: {e}")
        recipient_override = None
    _stash_pending_new_relationship(recipient, sender_si)
    # Record the outreach so this sender is never picked again for
    # this recipient. Persisted per-save; survives across sessions.
    try:
        record_outreach(getattr(recipient, "sim_id", None),
                        getattr(sender_si, "sim_id", None))
    except Exception as e:
        _log(f"record_outreach raised: {type(e).__name__}: {e}")
    from . import phone
    phone.generate_text_for(
        recipient=recipient,
        contact=contact,
        callback=callback,
        output=output,
        prompt_suffix=frame,
        journal_type_override="dating_outreach",
        recipient_override=recipient_override,
        first_contact=True,
    )


# ---------------------------------------------------------------------------
# Relationship establishment (called on player action)
# ---------------------------------------------------------------------------
#
# Rather than mutate the game the moment cold outreach fires, we stash
# a "pending new-relationship" record between (recipient_id, sender_id).
# When the player takes an action that engages with this sender -- either
# replying to the inbound, or the outbound Send-Intro flow -- we
# consume the pending record and add the actual game relationship.
#
# The stash lives in memory only. If the player closes the game without
# replying, the pending record is dropped and no state change occurs
# (which matches the intent: cold outreach is opt-in per-engagement).

_pending_new_lock = threading.RLock()
_pending_new = {}  # {frozenset({recipient_id, sender_id}) -> (rid, sid)}


def _pair_key(a_id, b_id):
    return frozenset({int(a_id), int(b_id)})


def _stash_pending_new_relationship(recipient_si, sender_si):
    rid = getattr(recipient_si, "sim_id", None)
    sid = getattr(sender_si, "sim_id", None)
    if rid is None or sid is None:
        return
    with _pending_new_lock:
        _pending_new[_pair_key(rid, sid)] = (int(rid), int(sid))


def has_pending_new_relationship(a_id, b_id):
    """True iff a pending new-relationship is stashed for this pair.
    Used by phone.py's reply handler to decide whether to fire
    establish_relationship_on_engagement()."""
    if a_id is None or b_id is None:
        return False
    with _pending_new_lock:
        return _pair_key(a_id, b_id) in _pending_new


def establish_relationship_on_engagement(actor_si, other_si, mutual_friend_name=None):
    """Called when the player takes an action that turns a stashed
    cold-outreach pair into a real relationship. Idempotent: safe to
    call multiple times; the second call is a no-op because the pair
    is already in the tracker.

    Also called by the outbound Send-Intro flow, where there was no
    prior stash but the player is initiating contact -- in that case
    we skip the stash lookup and just add the relationship.

    `mutual_friend_name` (optional) records that this pair was
    introduced via Llamadate's mutual-friend intro flow. Persisted to
    relationship_context so future prompts frame their history as
    friend-of-a-friend instead of app-match. When None, the origin is
    recorded as a direct Llamadate match.
    """
    actor_id = getattr(actor_si, "sim_id", None)
    other_id = getattr(other_si, "sim_id", None)
    if actor_id is None or other_id is None:
        return
    with _pending_new_lock:
        _pending_new.pop(_pair_key(actor_id, other_id), None)
    _bump_relationship(actor_si, other_id, delta=5)
    _bump_relationship(other_si, actor_id, delta=5)
    # No romance seed on establish. Matching on a dating app means
    # "we noticed each other's profiles" -- not "there is romantic
    # attraction between us." Romance only builds when the two
    # actually flirt via messages (the mood-based relationship_impact
    # feature handles that once conversations start). Keeps the
    # romance bar honest: hidden until earned.
    try:
        from . import contact_prefs
        contact_prefs.set_llamadate_origin(
            actor_id, other_id, mutual_friend_name=mutual_friend_name,
        )
    except Exception as e:
        _log(f"contact_prefs.set_llamadate_origin raised: "
             f"{type(e).__name__}: {e}")
    _log(f"established relationship: "
         f"{getattr(actor_si, 'first_name', '?')} <-> "
         f"{getattr(other_si, 'first_name', '?')}")


def _bump_relationship(sim_info, other_id, delta=10):
    """Establish (or reinforce) a friendship relationship between
    sim_info and other_id. Every step is logged so a failure to
    show up in the relationship panel is diagnosable from
    Llamafone_Log.txt.

    Strategy:
      1. Force the tracker entry to exist via the first available of
         check_and_track_relationship / _add_relationship /
         create_relationship. Any one of them is enough; we stop after
         the first success.
      2. Bump the friendship track by `delta`. Uses
         add_relationship_score(other_id, delta, LTR_Friendship_Main).
         If the track resolver returns None the score can't be bumped,
         but step 1 has already created the entry so the sim shows up
         as an acquaintance.
      3. Sims 4 relationships are BIDIRECTIONAL -- adding on the
         seeker's side auto-mirrors on the target's side. We still
         call from both ends via establish_relationship_on_engagement
         for safety.
    """
    tracker = getattr(sim_info, "relationship_tracker", None)
    name = getattr(sim_info, "first_name", "?")
    if tracker is None:
        _log(f"_bump_relationship: no relationship_tracker on {name}")
        return
    other_id = int(other_id)
    _log(f"_bump_relationship: {name} <-> sim_id={other_id} by {delta}")

    # Step 1: force the tracker entry to exist.
    created = False
    for method_name in ("check_and_track_relationship",
                        "_add_relationship",
                        "create_relationship"):
        fn = getattr(tracker, method_name, None)
        if not callable(fn):
            continue
        try:
            fn(other_id)
            _log(f"  {method_name}({other_id}) OK")
            created = True
            break
        except Exception as e:
            _log(f"  {method_name} raised: {type(e).__name__}: {e}")
            continue
    if not created:
        _log(f"  none of the tracker-create APIs worked; will try score bump anyway")

    # Step 2: bump the friendship score.
    track = _friendship_track_tunable()
    if track is None:
        _log("  friendship track unresolved -- entry exists but score not bumped")
        return
    fn = getattr(tracker, "add_relationship_score", None)
    if not callable(fn):
        _log("  add_relationship_score missing on tracker; giving up")
        return
    try:
        fn(other_id, delta, track)
        _log(f"  add_relationship_score({other_id}, {delta}, {track!r}) OK")
    except Exception as e:
        _log(f"  add_relationship_score raised: {type(e).__name__}: {e}")


_FRIENDSHIP_TRACK_CACHE = [None, False]  # [tunable, resolved-yet]

# LTR_Friendship_Main GUID64 in base game. This is what Sims 4 uses
# internally when two sims meet and start accruing friendship.
_LTR_FRIENDSHIP_GUIDS = (16650,)


def _friendship_track_tunable():
    """Resolve the LTR_Friendship_Main tunable. Cached so we don't
    pay lookup cost on every relationship bump. Logs what path
    succeeded / failed so a "friendship track unresolved" failure
    tells us where to look next."""
    if _FRIENDSHIP_TRACK_CACHE[1]:
        return _FRIENDSHIP_TRACK_CACHE[0]
    _FRIENDSHIP_TRACK_CACHE[1] = True
    try:
        import services
        import sims4.resources
        from sims4.resources import Types
        im = services.get_instance_manager(Types.STATISTIC)
        # 1. Direct GUID lookup (most reliable across builds).
        for guid in _LTR_FRIENDSHIP_GUIDS:
            try:
                key = sims4.resources.get_resource_key(guid, Types.STATISTIC)
                found = im.get(key)
                if found is not None:
                    _FRIENDSHIP_TRACK_CACHE[0] = found
                    _log(f"friendship track resolved by GUID {guid}")
                    return found
            except Exception as e:
                _log(f"friendship track GUID {guid} lookup failed: {e}")
        # 2. Name-based fallback.
        for name in ("LTR_Friendship_Main", "LTR_Friendship",
                     "relationship_LTR_Friendship_Main"):
            try:
                get_key = getattr(im, "get_key_for_name", None)
                if not callable(get_key):
                    continue
                key = get_key(name)
                if key is None:
                    continue
                found = im.get(key)
                if found is not None:
                    _FRIENDSHIP_TRACK_CACHE[0] = found
                    _log(f"friendship track resolved by name {name}")
                    return found
            except Exception as e:
                _log(f"friendship track name {name} lookup failed: {e}")
    except Exception as e:
        _log(f"friendship track resolver crashed: {type(e).__name__}: {e}")
    _log("friendship track resolution failed -- score bumps will no-op")
    return None


# ---------------------------------------------------------------------------
# Contact-list helper used by the outbound Dating sim picker.
# ---------------------------------------------------------------------------


def eligible_intro_targets_for(seeker_si):
    """Wrapper around get_candidates_for so the phone_ui module has a
    stable name to import. Purely for readability at the call site."""
    return get_candidates_for(seeker_si)


# ---------------------------------------------------------------------------
# Reply-interest classifier (post-send)
# ---------------------------------------------------------------------------


_INTEREST_SYSTEM = (
    "Classify whether a fictional sim's text-message reply signals "
    "romantic/dating interest in the sender or not.\n"
    "\n"
    "Output EXACTLY one token: YES or NO.\n"
    "- YES: any warmth, curiosity, engagement, follow-up question, "
    "openness to chatting more, playful banter, or ambiguous-but-"
    "not-rejecting response.\n"
    "- NO: explicit disinterest, cool one-liner declining, 'not for me', "
    "'not my type', 'wish you the best', 'good luck', 'no thanks', "
    "or any dismissal.\n"
    "\n"
    "Default to YES if the reply is neutral or hard to read. Only "
    "output NO when disinterest is clearly signaled."
)


def classify_reply_and_maybe_establish(seeker_si, target_si, reply_text):
    """Fire a fast LLM call to classify the recipient's reply as
    interested-or-not. If interested (or ambiguous), establish the
    real Sims 4 relationship so the target appears in the seeker's
    contacts. If explicitly not interested, no relationship is
    created and the target stays a stranger.

    Runs async; the establish call fires from the classifier's
    callback thread. Failure modes default to 'interested' so a
    classification error doesn't silently swallow the outreach.
    """
    if not reply_text or not seeker_si or not target_si:
        return
    seeker_name = getattr(seeker_si, "first_name", "?")
    target_name = getattr(target_si, "first_name", "?")

    def _on_verdict(text, error):
        # Default to interested on ANY classifier failure -- better to
        # establish a relationship we shouldn't than to silently drop
        # a real match.
        interested = True
        if not error and text:
            interested = "NO" not in text.strip().upper().split()
        _log(f"reply-interest classify: {target_name} -> {seeker_name} "
             f"verdict={'INTERESTED' if interested else 'NOT INTERESTED'} "
             f"(raw={text!r}, err={error!r})")
        if interested:
            try:
                # Look up mutual friend at establish-time so relationship_context
                # can record whether this pair came through the mutual-friend
                # intro flow vs a direct Llamadate match.
                try:
                    mutual = find_mutual_friend(seeker_si, target_si)
                    mutual_name = None
                    if mutual is not None:
                        first = getattr(mutual, "first_name", None) or ""
                        last = getattr(mutual, "last_name", None) or ""
                        mutual_name = f"{first} {last}".strip() or None
                except Exception:
                    mutual_name = None
                establish_relationship_on_engagement(
                    seeker_si, target_si, mutual_friend_name=mutual_name,
                )
            except Exception as e:
                _log(f"establish after positive classify raised: {type(e).__name__}: {e}")
        else:
            _log(f"skipping relationship establishment: {target_name} not interested")

    user_msg = (
        f"Reply from {target_name} to {seeker_name}: \"{reply_text.strip()}\"\n\n"
        "Was this reply interested (YES) or not interested (NO)?"
    )
    try:
        from . import api_client
        api_client.call_ai_async(
            messages=[{"role": "user", "content": user_msg}],
            system=_INTEREST_SYSTEM,
            use_fast_model=True,
            callback=_on_verdict,
        )
    except Exception as e:
        _log(f"classify_reply_and_maybe_establish: api_client raised {type(e).__name__}: {e}")
        # Fall back to establishing anyway to avoid silently swallowing.
        try:
            establish_relationship_on_engagement(seeker_si, target_si)
        except Exception:
            pass


def build_outbound_recipient_override(seeker_si):
    """Return a recipient-context block that shows ONLY the player-
    written Llamadate bio (if set). This replaces phone.py's default
    `_describe_recipient` block, which would otherwise expose the
    player's career / aspiration / traits / mood / world to the
    recipient LLM. On Llamadate, matches see the profile the player
    chose to write -- not the mod's auto-derived data.

    Returns None when the player hasn't written a bio; the caller
    should pass None to send_text so the default block runs (the
    fallback behavior the user opted into: no bio = existing
    context is passed through as-is).
    """
    if seeker_si is None:
        return None
    seeker_id = getattr(seeker_si, "sim_id", None)
    if seeker_id is None:
        return None
    try:
        bio = get_sim_player_bio(seeker_id) or ""
    except Exception:
        bio = ""
    bio = bio.strip()
    if not bio:
        return None
    seeker_name = getattr(seeker_si, "first_name", "the sender")
    return (
        f"=== Character: {seeker_name} (matched with you on Llamadate) ===\n"
        f"You know {seeker_name}'s FIRST NAME (from their Llamadate profile) "
        f"and the bio they wrote. Nothing else -- no career, no where they "
        f"live, no traits, no shared history. Do NOT invent details beyond "
        f"their name and this bio, and do NOT ask their name (you already "
        f"have it -- it's {seeker_name}).\n"
        f"{seeker_name}'s Llamadate bio:\n"
        f"\"{bio}\""
    )


def build_outbound_relationship_override(seeker_si, target_si):
    """Return a replacement for the "Relationship info" block that
    phone.py normally inserts. Removes the "How X feels: barely know
    each other" line that would otherwise trigger the reply system's
    confused-first-message tier ("wait, is this Ingrid? remind me
    where we met"). Frames the exchange explicitly as a Llamadate
    first-contact so the LLM can react like someone reading a
    dating-app match's opener."""
    seeker_name = getattr(seeker_si, "first_name", "the sender") if seeker_si else "the sender"
    target_name = getattr(target_si, "first_name", "the recipient") if target_si else "the recipient"
    return (
        f"=== Character: {target_name} (THE REPLIER) ===\n"
        f"How {target_name} feels about the player: this is a "
        f"LLAMADATE FIRST-CONTACT MATCH -- {target_name} and "
        f"{seeker_name} have NEVER met in person. They matched on "
        "Llamadate (a dating app) and this is the first message. "
        "Do NOT use the 'barely know each other' tier and do NOT say "
        "phrases like 'wait, is this X?', 'remind me where we met', "
        "'sorry, do I know you' -- those imply prior contact that "
        f"does not exist. Instead, react like a real person reading "
        "a first-time dating-app opener from a stranger whose "
        "profile was attached: engage warmly if interested, decline "
        f"coolly if not, based on the sender's bio + profile info "
        "in the user prompt below."
    )


def build_outbound_intro_suffix(seeker_si, target_si):
    """Prompt suffix appended to the reply-generation prompt when the
    player sends an outbound Llamadate intro. Frames the interaction
    as a dating-app first-contact and gives the recipient LLM the
    context it needs to make a real interest decision:

      - Sender's player-written bio (what the sender is pitching).
      - Sender's flavor facts (career, aspiration, top traits, top
        skills) as a "profile" summary.
      - Recipient's cached LLM-generated bio if we have one -- gives
        the recipient LLM a consistent voice for the sim beyond the
        raw trait list phone.py already inserts.
      - Explicit instruction to weigh compatibility instead of just
        answering politely."""
    seeker_name = getattr(seeker_si, "first_name", "someone") if seeker_si else "someone"
    target_name = getattr(target_si, "first_name", "you") if target_si else "you"
    seeker_id = getattr(seeker_si, "sim_id", None) if seeker_si else None
    # Outbound intros are ALWAYS framed as a Llamadate app match --
    # even when a real mutual exists between the two sims. The player
    # opened the app and picked this profile; the mutual friend didn't
    # "mention" or "introduce" anyone. Injecting the mutual here made
    # the recipient LLM reply with "so-and-so mentioned you might
    # reach out" which reads wrong for an app-driven first-contact.
    connection = (
        f"This is a Llamadate app match. {seeker_name} saw {target_name}'s "
        f"profile and reached out through the app. Do NOT reference any "
        "mutual friends by name, and do NOT imply someone introduced them "
        "or 'mentioned' the sender -- the app is the vehicle here."
    )

    # NOTE: sender's bio + flavor facts used to be included here. They
    # now flow through `recipient_override` in send_text -- passed via
    # build_outbound_recipient_override() -- which REPLACES the default
    # recipient-context block entirely when the player has written a
    # bio. When they haven't, the mod deliberately falls back to
    # phone.py's normal _describe_recipient block so the LLM still has
    # something to react to.
    #
    # This suffix stays focused on framing (dating-app first-contact,
    # decision guidance, tone options) and does not leak the sender's
    # auto-derived context here.

    # Recipient's own cached LLM-generated bio (from when the player
    # viewed their profile) -- gives THIS sim a coherent voice.
    target_bio_block = ""
    try:
        target_cached = _cached_bio_for(target_si) if target_si is not None else None
    except Exception:
        target_cached = None
    if target_cached and target_cached.strip():
        target_bio_block = (
            f"\n\nFor context, THIS is your ({target_name}'s) own Llamadate "
            f"profile (bio + prompts). Stay consistent with how you present "
            f"yourself here:\n\"{target_cached.strip()}\""
        )

    return (
        f"=== LLAMADATE FIRST CONTACT ===\n"
        f"CRITICAL context: {seeker_name} and {target_name} matched on "
        "Llamadate, a dating app. This is the FIRST message they have "
        f"ever exchanged. {target_name} has never met {seeker_name} in "
        "person. Do NOT act like there is prior history, do NOT "
        "reference past conversations, do NOT be surprised they are "
        "texting -- matching on a dating app and messaging is the "
        "entire point of the app.\n\n"
        f"{connection}"
        f"{target_bio_block}\n\n"
        f"INTEREST DECISION: Weigh whether {target_name} would actually "
        f"be into {seeker_name} based on {target_name}'s personality "
        f"and what {seeker_name}'s profile signals. If their vibes "
        "clash (e.g. a Neat sim reading a Slob's bio, an ambitious "
        "career sim reading a couch-potato bio), lean toward polite "
        "disinterest. If the profiles complement each other (shared "
        "aspiration, complementary traits, an interest they'd geek "
        f"out over together), lean into it. Match {target_name}'s "
        "personality in HOW they express interest or disinterest: a "
        "Cheerful sim is warm, a Snob is picky, a Loner is guarded. "
        "Options for how to respond:\n"
        "- Genuine interest: engage with something from their bio, "
        "ask a follow-up question.\n"
        "- Polite lukewarm: acknowledge but keep it short, don't "
        "commit to more.\n"
        "- Not interested: cool one-liner declining, no drama.\n"
        f"\nPick the reaction that best fits {target_name}'s actual "
        "personality and what the sender's profile shows. Keep the "
        "reply short (1-2 texts). Never narrate that this is a "
        "dating app -- react in the frame."
        f"\n{_BANNED_OPENERS_BLOCK}"
    )


# ---------------------------------------------------------------------------
# Dating-app bio generation for the outbound Dating flow
# ---------------------------------------------------------------------------
#
# When the player picks an unmet sim from the Dating picker, we synthesize
# a short first-person dating-app bio using that sim's traits/career/skills.
# The bio is shown as a preview BEFORE the player writes their intro so
# they have context for who they're contacting. Bio generation is async
# (LLM roundtrip); the UI shows a "reading their profile..." indicator
# while it runs and calls the caller-supplied callback when the bio is
# ready.

_BIO_SYSTEM = (
    "You are writing a full dating-app profile for a fictional sim in "
    "a Hinge-style format: a short bio at the top followed by three "
    "prompt-and-answer pairs.\n"
    "\n"
    "OUTPUT FORMAT -- follow exactly, no extra text:\n"
    "\n"
    "BIO\n"
    "[bio paragraph goes here, 2 to 3 sentences]\n"
    "\n"
    "[first prompt goes here]\n"
    "[first answer goes here, 1 to 2 sentences]\n"
    "\n"
    "[second prompt goes here]\n"
    "[second answer goes here, 1 to 2 sentences]\n"
    "\n"
    "[third prompt goes here]\n"
    "[third answer goes here, 1 to 2 sentences]\n"
    "\n"
    "BIO CONTENT:\n"
    "- Opener that captures their vibe in their voice. Not 'I am a...' "
    "-- more like 'Recovering perfectionist. Currently perfecting "
    "recovery.' or 'Espresso runs on me, I run on espresso.'\n"
    "- One concrete detail from their actual career / skill / trait.\n"
    "- A dating-intent hint: what they're looking for. Phrase it "
    "naturally ('hopeless romantic', 'not looking to settle down', "
    "'here for the plot', 'seeing what's out there') -- never say "
    "'short-term' or 'long-term' literally.\n"
    "- 2 to 3 sentences, 40 to 70 words. No formatting, no 'BIO:' "
    "restatement.\n"
    "\n"
    "PROMPTS AND ANSWERS:\n"
    "- Prompts should feel like real Hinge prompts. Examples: 'The way "
    "to my heart is', 'A shower thought I recently had', 'My simple "
    "pleasures', 'Two truths and a lie', 'My most controversial "
    "opinion is', 'I go crazy for', 'If loving X is wrong, I don't "
    "want to be right', 'Dating me is like', 'Green flags I look for', "
    "'My love language is', 'A perfect first date is', 'The last thing "
    "I geeked out about', 'You should NOT go out with me if', 'My "
    "toxic trait is'. Pick THREE that fit this sim's personality; you "
    "can also invent prompts in that style.\n"
    "- Answers must be in the sim's voice and pull from their actual "
    "facts. Never generic.\n"
    "- Prompts stay short (under 8 words). Answers stay short (1-2 "
    "sentences).\n"
    "\n"
    "VOICE:\n"
    "- Cheerful sim = warm and open. Snob = witty and picky. Goofball "
    "= self-aware and dorky. Loner = cagey and dry. Hot-headed = "
    "spiky. Whatever their top personality traits are, sound like "
    "THAT throughout.\n"
    "- Don't invent specifics beyond what's on the facts sheet. If "
    "the facts are sparse, write a vibe-based profile ('new in town, "
    "figuring it out') and pick prompts that don't require specifics.\n"
    "- No emojis, no hashtags, no bullet points, no headers other than "
    "'BIO' at the top. Small typos or lowercase-only OK if it fits.\n"
    "- Never state age. Never restate their traits as a list.\n"
)


# Between the bio and each prompt/answer block we just want breathing
# room. Tried a "─" horizontal rule; the Sims 4 dialog renderer wrapped
# it onto two lines and it looked worse than nothing.
_HINGE_SEPARATOR = "\n\n\n"


def _parse_bio_and_prompts(raw_text):
    """Split the LLM's raw output into (bio_text, [(prompt, answer), ...]).

    Robust to minor formatting drift: tolerates missing 'BIO' header,
    extra blank lines, trailing whitespace. If we can't find any
    prompt/answer chunks, returns the whole text as bio and an empty
    prompt list -- caller shows raw output rather than nothing.
    """
    if not raw_text:
        return "", []
    text = raw_text.strip()
    # Split on blank-line boundaries.
    chunks = [c.strip() for c in text.split("\n\n") if c.strip()]
    if not chunks:
        return text, []
    # First chunk is the bio. Strip an optional 'BIO' header line.
    first = chunks[0]
    first_lines = first.split("\n", 1)
    if first_lines and first_lines[0].strip().upper().rstrip(":").strip() == "BIO":
        bio = first_lines[1].strip() if len(first_lines) > 1 else ""
    else:
        bio = first
    # Remaining chunks are prompt+answer pairs. First line is the
    # prompt; the rest is the answer.
    prompts = []
    for chunk in chunks[1:]:
        lines = chunk.split("\n", 1)
        prompt = lines[0].strip().rstrip(":").strip()
        answer = lines[1].strip() if len(lines) > 1 else ""
        if prompt or answer:
            prompts.append((prompt, answer))
    return bio, prompts[:3]


def _format_profile_for_dialog(bio, prompts):
    """Render the parsed profile as the string Sims 4 will show in the
    dialog body. Bio at the top; each prompt/answer pair separated by
    generous whitespace so the sections read as distinct. Prompts get
    a colon suffix (we stripped it during parse to normalize)."""
    parts = [bio] if bio else []
    for prompt, answer in prompts:
        if prompt:
            parts.append(f"{prompt}:\n{answer}")
        elif answer:
            parts.append(answer)
    return _HINGE_SEPARATOR.join(parts) if parts else ""


def _bio_context_lines(sim_info):
    """Assemble everything the LLM needs to write a plausible bio.
    Returns a multi-line string that becomes the user message. Uses
    _sender_flavor_facts under the hood plus a name + age header."""
    lines = []
    try:
        first = getattr(sim_info, "first_name", "?") or "?"
        last = getattr(sim_info, "last_name", "") or ""
        name = (first + " " + last).strip()
        lines.append(f"Name: {name}")
    except Exception:
        pass
    try:
        age = str(getattr(sim_info, "age", "")).replace("Age.", "").title()
        if age:
            lines.append(f"Age: {age}")
    except Exception:
        pass
    try:
        gender = str(getattr(sim_info, "gender", "")).replace("Gender.", "").title()
        if gender:
            lines.append(f"Gender: {gender}")
    except Exception:
        pass
    facts = _sender_flavor_facts(sim_info)
    if facts:
        lines.extend(facts)
    return "\n".join(lines) if lines else "(nothing specific known)"


# ---------------------------------------------------------------------------
# Bio cache (persisted in save folder)
# ---------------------------------------------------------------------------
#
# Once a sim's profile has been generated, we keep it around keyed by
# their sim_id plus a fingerprint of the "flavor facts" that fed the
# generation (career, aspiration, top traits, top skills). Revisiting
# the same sim reuses the cached profile -- saves an LLM call and,
# more importantly, keeps the sim's dating presentation stable across
# sessions instead of shifting every time the player opens their card.
#
# The fingerprint means a career promotion or new skill invalidates
# the cache automatically: the next open regenerates a profile that
# reflects who this sim now is.
#
# File: <save>/DatingBios.json
#   {"sim_id_str": {"profile": "...", "fingerprint": "abcd1234"}}

_BIO_CACHE_FILENAME = "DatingBios.json"
_bio_cache_lock = threading.RLock()
_bio_cache_data = None
_bio_cache_save_id = None


def _bio_cache_path():
    return _save_id.data_path(_BIO_CACHE_FILENAME)


def _bio_cache_load():
    global _bio_cache_data, _bio_cache_save_id
    with _bio_cache_lock:
        current = _save_id.get_current_save_id()
        if current is None and _bio_cache_data is not None:
            return _bio_cache_data
        if _bio_cache_data is not None and _bio_cache_save_id == current:
            return _bio_cache_data
        _bio_cache_save_id = current
        path = _bio_cache_path()
        if path is None or not os.path.exists(path):
            _bio_cache_data = {}
            return _bio_cache_data
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                _bio_cache_data = {}
            else:
                _bio_cache_data = data
            return _bio_cache_data
        except Exception as e:
            _log(f"bio cache load failed: {type(e).__name__}: {e}")
            _bio_cache_data = {}
            return _bio_cache_data


def _bio_cache_save():
    with _bio_cache_lock:
        path = _bio_cache_path()
        if path is None:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_bio_cache_data or {}, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            _log(f"bio cache save failed: {type(e).__name__}: {e}")


# Cache version marker. Bump when the bio format changes (separator,
# colon rules, structure) so already-cached bios in the old format
# get regenerated the next time their sim is opened.
_BIO_CACHE_VERSION = "v2"


def _bio_fingerprint(sim_info):
    """Deterministic short hash of the flavor facts that feed bio
    generation. When any of career/aspiration/traits/skills change,
    the fingerprint changes and the cache is invalidated on next read.
    Also mixes in _BIO_CACHE_VERSION so a format change invalidates
    every cached bio in one go."""
    import hashlib
    ctx = _BIO_CACHE_VERSION + "|" + _bio_context_lines(sim_info)
    return hashlib.sha1(ctx.encode("utf-8", errors="replace")).hexdigest()[:16]


def _cached_bio_for(sim_info):
    """Return the cached profile text for this sim if the fingerprint
    still matches; None otherwise."""
    sid = getattr(sim_info, "sim_id", None)
    if sid is None:
        return None
    try:
        entry = _bio_cache_load().get(str(int(sid)))
        if not entry or "profile" not in entry:
            return None
        if entry.get("fingerprint") != _bio_fingerprint(sim_info):
            return None
        return entry["profile"]
    except Exception:
        return None


def _cache_bio(sim_info, profile_text):
    """Persist the generated profile + current fingerprint for this
    sim. Overwrites any prior entry for the same sim_id."""
    sid = getattr(sim_info, "sim_id", None)
    if sid is None or not profile_text:
        return
    with _bio_cache_lock:
        cache = _bio_cache_load()
        cache[str(int(sid))] = {
            "profile": profile_text,
            "fingerprint": _bio_fingerprint(sim_info),
        }
        _bio_cache_save()


def generate_bio_for(sim_info, callback):
    """Async: generate a Hinge-style dating profile (short bio + three
    prompt/answer pairs) for `sim_info` and pass the formatted display
    text to callback(profile_text, error).

    Runs on the api_client's background thread; the callback fires
    from that thread, so any UI work in the callback needs to be safe
    to do off the sims loop (phone.py's reply flow does this and it
    works, so we follow the same pattern).

    Uses the fast model -- bio quality doesn't need the flagship, and
    the player is waiting on this dialog before writing their intro.
    Parses the LLM's structured output into bio + prompts and formats
    for the Sims 4 dialog before calling back; parse failures fall
    through to raw text so the flow never hard-fails on formatting.
    """
    if sim_info is None:
        try:
            callback(None, "no sim_info")
        except Exception:
            pass
        return

    # Cache hit -- return the previously generated profile instantly.
    # Fingerprint match means the sim's career/aspiration/traits/skills
    # haven't changed since we last generated, so the profile still
    # accurately represents who they are.
    try:
        cached = _cached_bio_for(sim_info)
    except Exception:
        cached = None
    if cached:
        first = getattr(sim_info, "first_name", "?")
        _log(f"generate_bio_for: {first} -> cache hit")
        try:
            callback(cached, None)
        except Exception:
            pass
        return

    ctx = _bio_context_lines(sim_info)
    first = getattr(sim_info, "first_name", "this sim") or "this sim"
    user_msg = (
        f"Write a Hinge-style dating profile for {first}, in {first}'s "
        f"own voice.\n\n"
        f"Facts on file:\n{ctx}\n\n"
        f"Follow the exact output format from the system prompt: BIO "
        f"header, blank line, bio paragraph, blank line, then three "
        f"prompt/answer pairs each separated by blank lines. No extra "
        f"text before or after."
    )
    _log(f"generate_bio_for: {first} (facts={ctx!r})")

    def _on_llm(raw_text, error):
        if error or not raw_text:
            try:
                callback(None, error or "empty bio")
            except Exception:
                pass
            return
        try:
            bio, prompts = _parse_bio_and_prompts(raw_text)
            profile_text = _format_profile_for_dialog(bio, prompts)
            if not profile_text.strip():
                # Parse produced nothing useful -- fall through to the
                # raw LLM output so the player still sees something.
                profile_text = raw_text.strip()
            _log(f"bio parsed: bio={len(bio)} chars, prompts={len(prompts)}")
            # Cache the formatted display text so re-opening this sim's
            # profile shows the same content instead of a new generation.
            try:
                _cache_bio(sim_info, profile_text)
            except Exception:
                _log_exc = _log  # local alias for consistency
                _log("_cache_bio raised; continuing without cache")
            callback(profile_text, None)
        except Exception as e:
            _log(f"bio parse failed: {type(e).__name__}: {e}; using raw")
            try:
                callback(raw_text.strip(), None)
            except Exception:
                pass

    try:
        from . import api_client
        api_client.call_ai_async(
            messages=[{"role": "user", "content": user_msg}],
            system=_BIO_SYSTEM,
            use_fast_model=True,
            callback=_on_llm,
        )
    except Exception as e:
        _log(f"generate_bio_for: api_client raised {type(e).__name__}: {e}")
        try:
            callback(None, f"{type(e).__name__}: {e}")
        except Exception:
            pass
