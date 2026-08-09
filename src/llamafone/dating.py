"""
Dating-app layer (v3.5).

Adds two optional autonomous flows on top of the phone mod, both gated
on `dating_enabled = true` in llamafone.cfg:

  1. COLD OUTREACH -- a random single sim who matches the player's
     preferences occasionally texts out of the blue. Piggybacks on the
     auto_events cadence via a new event type ('dating'), so users who
     already tuned their auto_event_interval_minutes / _chance don't
     have to think about a separate schedule.

  2. FRIEND-SETUP CHAIN -- when the player texts a friend a phrase like
     "know any single guys?" / "set me up", the mod records a pending
     introduction, and a couple of sim-days later a new sim reaches out
     ("Alice gave me your number"). A couple of sim-days after that,
     the friend follows up asking how it went. State lives in the
     save's PendingIntroductions.json (per-save via save_id).

This module is intentionally opt-in and defensive. Nothing here mutates
game state or invents relationships -- it only picks plausible
candidates from the player's existing relationship network and hands
them to the existing phone-text engine. Users who never touch
`dating_enabled` see zero behavior change.
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
# Feature gate
# ---------------------------------------------------------------------------


def is_enabled():
    """True iff the dating layer is turned on. Every other function in
    this module short-circuits to a no-op / empty result when this is
    False, so callers can invoke us unconditionally and we stay
    silent for users who haven't opted in."""
    return config.get_dating_enabled()


def cold_outreach_enabled():
    """Cold outreach is on iff the master switch is on AND the weight
    is > 0. Returning both toggles as a single fn keeps the caller
    code readable (`if dating.cold_outreach_enabled(): ...`)."""
    return is_enabled() and config.get_dating_cold_outreach_weight() > 0


def setup_chain_enabled():
    """Friend-setup chain is on iff the master switch is on AND the
    per-flow toggle is on."""
    return is_enabled() and config.get_dating_setup_chain_enabled()


# ---------------------------------------------------------------------------
# Orientation resolver
# ---------------------------------------------------------------------------

# Values the cfg accepts alongside 'auto' -- these get used both by the
# candidate-pool filter (which genders are eligible) and by the intent
# detector (which "know any single X" phrasings match the player's
# preferences).
_ORIENTATION_MEN = "men"
_ORIENTATION_WOMEN = "women"
_ORIENTATION_ANYONE = "anyone"


def resolve_orientation(player_sim_info=None):
    """Return one of 'men', 'women', 'anyone' -- never 'auto'. If cfg
    says 'auto', inspect the player sim's CAS romantic-orientation
    preferences; fall back to 'anyone' when they aren't set or can't
    be read (some sims never had orientation configured in-game and
    that's fine -- 'anyone' means we don't gender-filter).

    `player_sim_info` should be the household protagonist. When omitted
    we read `sim_context.get_main_sim_info()`.
    """
    raw = config.get_dating_orientation()
    if raw in (_ORIENTATION_MEN, _ORIENTATION_WOMEN, _ORIENTATION_ANYONE):
        return raw
    # 'auto' -- try to read the sim's own preferences.
    si = player_sim_info if player_sim_info is not None else sim_context.get_main_sim_info()
    detected = _read_sim_orientation(si)
    if detected is not None:
        return detected
    return _ORIENTATION_ANYONE


def _read_sim_orientation(sim_info):
    """Best-effort read of a sim's CAS romantic-orientation preferences.
    Returns 'men' / 'women' / 'anyone' when we can determine it, or
    None if the sim doesn't have the preference data (older sims
    created before Growing Together / Lovestruck may not have it).

    Sims 4 stores romantic preferences on a separate WhimSet-style
    tracker off sim_info. Rather than pin to a specific attribute
    path that may shift across game builds, probe a few known
    locations defensively. When none of them yield a usable answer,
    return None and let the caller default to 'anyone'.
    """
    if sim_info is None:
        return None
    # Growing Together / Lovestruck expose the preferences via
    # `sim_info.get_all_gender_preferences()` and similar. Different
    # builds return different shapes; do a duck-typed read.
    for attr in ("gender_preference", "gender_preferences",
                 "get_gender_preferences", "romantic_preferences"):
        try:
            val = getattr(sim_info, attr, None)
            if callable(val):
                try:
                    val = val()
                except Exception:
                    continue
            if val is None:
                continue
            # If it's a set/list of gender enums, map to our strings.
            mapped = _map_gender_preference_value(val)
            if mapped is not None:
                return mapped
        except Exception:
            continue
    return None


def _map_gender_preference_value(val):
    """Turn a Sims 4 gender-preference value (enum, set of enums,
    dict) into our 'men' / 'women' / 'anyone' string, or None if the
    shape is unrecognized. Kept isolated so we can extend it as we
    discover new pack-specific shapes without touching the caller."""
    try:
        # Common shape: a set-like of Gender enum members.
        vals = list(val) if hasattr(val, "__iter__") and not isinstance(val, str) else [val]
        as_strs = []
        for v in vals:
            s = str(v).upper()
            # Strip enum prefix -- "Gender.MALE" -> "MALE"
            if "." in s:
                s = s.rsplit(".", 1)[-1]
            as_strs.append(s)
        has_male = any(s in ("MALE", "M") for s in as_strs)
        has_female = any(s in ("FEMALE", "F") for s in as_strs)
        if has_male and has_female:
            return _ORIENTATION_ANYONE
        if has_male:
            return _ORIENTATION_MEN
        if has_female:
            return _ORIENTATION_WOMEN
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Candidate pool
# ---------------------------------------------------------------------------

# Age enum values (Sims 4 Age enum stringifies as "Age.TEEN" / "Age.YOUNGADULT"
# etc.). Storing the tier as an int rank so proximity checks are just
# subtraction. Child and below aren't listed -- children never make it
# past the teen+ gate below.
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


def _pass_age_gate(player_rank, candidate_rank, max_tier_gap=1):
    """Teens can only match teens (no cross-tier). Everyone else can
    match within `max_tier_gap` tiers of themselves (default 1, so
    young-adult<->adult and adult<->elder OK; young-adult<->elder is
    not). Returns False for any None input (child-or-below or unreadable)."""
    if player_rank is None or candidate_rank is None:
        return False
    if player_rank == 0 or candidate_rank == 0:
        return player_rank == 0 and candidate_rank == 0
    return abs(player_rank - candidate_rank) <= max_tier_gap


# Relationship-status keywords that mean "this sim is committed to
# someone" -- if any of these appear in a candidate's status bits,
# they're off-limits. Cast wide on purpose; a false-negative here
# would surface someone dating another sim as a dating candidate,
# which is the exact 'weird outcome' we want to avoid.
_COMMITTED_KEYWORDS = (
    "married", "spouse", "engaged", "fiance", "fiancé",
    "partner",       # domestic partner / life partner
    "soulmate", "sweetheart",
    "dating",        # Lovestruck 'Going Steady' shows as Dating bit
    "goingsteady",
    "boyfriend", "girlfriend",
)

# Family-role keywords -- ANY of these in a candidate's status
# disqualifies them regardless of romance. Household members are
# always filtered too (via the in_household flag) so cousins /
# in-laws / step-siblings that don't share the household but do
# share family bits still get caught here.
_FAMILY_KEYWORDS = (
    "sibling", "brother", "sister",
    "parent", "father", "mother", "dad", "mom",
    "child", "son", "daughter",
    "grandparent", "grandfather", "grandmother",
    "grandchild", "grandson", "granddaughter",
    "aunt", "uncle", "niece", "nephew", "cousin",
    "stepparent", "stepchild", "stepsibling",
    "family",     # generic Family relationship bit
)


def _status_has_any(status, keywords):
    """Case-insensitive substring test across space-separated status bits."""
    if not status:
        return False
    s = str(status).lower()
    return any(k in s for k in keywords)


def _gender_matches_orientation(candidate_sim_info, orientation):
    """orientation is one of 'men' / 'women' / 'anyone' (already
    resolved -- no 'auto' here). 'anyone' matches everyone."""
    if orientation == _ORIENTATION_ANYONE:
        return True
    try:
        g = str(getattr(candidate_sim_info, "gender", "")).replace("Gender.", "").upper()
    except Exception:
        return False
    if orientation == _ORIENTATION_MEN:
        return g in ("MALE", "M")
    if orientation == _ORIENTATION_WOMEN:
        return g in ("FEMALE", "F")
    return False


def get_candidates(player_sim_info=None, exclude_sim_ids=None, min_friendship=25):
    """Return a list of dating-eligible contact dicts drawn from the
    player sim's existing relationship network. Filters:
      1. Not the player themselves (obvious).
      2. Not in the player's household (never date roommates the
         mod picked for you -- these are family / roommates the
         player chose).
      3. Not tagged with any family relationship bit (siblings,
         parents, cousins, etc. -- covers extended family not in
         the household).
      4. Not already committed to someone else (married, engaged,
         going steady, boyfriend/girlfriend, etc.).
      5. Gender matches the player's resolved orientation.
      6. Age-appropriate: teens only match teens; adults can match
         within one age tier (young-adult<->adult<->elder).
      7. Not in the exclude_sim_ids set (used to skip candidates the
         player has already been introduced to, or ones a queued
         intro has already claimed).

    The returned dicts are the same shape used by phone.send_text /
    the auto-events pool: {"sim_info", "sim_id", "name",
    "friendship", "romance", "status", "in_household"}.
    """
    player_si = player_sim_info if player_sim_info is not None else sim_context.get_main_sim_info()
    if player_si is None:
        return []
    exclude = {int(x) for x in (exclude_sim_ids or []) if x is not None}
    player_id = getattr(player_si, "sim_id", None)
    if player_id is not None:
        exclude.add(int(player_id))

    orientation = resolve_orientation(player_si)
    player_age_rank = _age_rank(player_si)

    try:
        hh_members, relationships = sim_context.get_main_sim_network(
            player_si, min_friendship=min_friendship,
        )
    except Exception:
        hh_members, relationships = [], []

    # Household members are never dating candidates -- the player either
    # picked them at CAS or moved them in; if they wanted to date one
    # they wouldn't need a dating layer.
    candidates = []
    for entry in relationships:
        try:
            sid = entry.get("sim_id")
            if sid is None or int(sid) in exclude:
                continue
            if entry.get("in_household"):
                continue
            si = entry.get("sim_info")
            if si is None:
                continue
            status = entry.get("status", "") or ""
            if _status_has_any(status, _FAMILY_KEYWORDS):
                continue
            if _status_has_any(status, _COMMITTED_KEYWORDS):
                continue
            if not _gender_matches_orientation(si, orientation):
                continue
            if not _pass_age_gate(player_age_rank, _age_rank(si)):
                continue
            candidates.append(entry)
        except Exception:
            continue
    return candidates


# ---------------------------------------------------------------------------
# Cold outreach flow
# ---------------------------------------------------------------------------

# Additional instructions grafted onto the base _TEXT_SYSTEM when a
# dating cold outreach fires. Keeps the base rules (voice, age gates,
# banned frames) and adds the dating framing.
_COLD_OUTREACH_SUFFIX = (
    "\n\nDATING CONTEXT: This is a light, low-stakes text where you're "
    "reaching out with a hint of romantic interest -- treat it like "
    "you noticed the recipient and wanted to say hi. Keep it short "
    "and warm, no over-explanation. Do NOT invent a dating-app "
    "profile, mutual friend introduction, or event that isn't stated "
    "elsewhere in the context. Adjust register to fit YOUR (the "
    "sender's) traits: an outgoing sim opens confidently, a shy sim "
    "hedges a little, etc. Never write anything creepy, forward, or "
    "pressuring -- this is a first-contact opener, not a proposition."
)


def _pick_cold_outreach_recipient():
    """Pick which household member the cold outreach is aimed at. Prefers
    the current active sim; falls back to any teen+ household member.

    Three gates, all required:
      1. Age is teen+ (children never see cold outreach).
      2. Sim is NOT already committed (married / engaged / going
         steady / has a partner-level romantic bit with anyone).
         Getting hit on by a stranger while happily married reads
         as disruptive rather than immersive, so we skip. If a
         player specifically wants that temptation-drama flavor
         it'll need a separate opt-in knob -- default is 'no'.
      3. Sim has at least one candidate in their own network --
         no point picking a recipient when no plausible sender
         exists for them.

    Returns None when no household member passes all three gates."""
    def _eligible(si):
        if si is None:
            return False
        try:
            age = str(getattr(si, "age", "")).replace("Age.", "").upper()
            if age not in ("TEEN", "YOUNGADULT", "YOUNG_ADULT", "ADULT", "ELDER"):
                return False
        except Exception:
            return False
        if _sim_is_committed(si):
            return False
        if not get_candidates(si):
            return False
        return True

    try:
        main = sim_context.get_main_sim_info()
        if _eligible(main):
            return main
    except Exception:
        pass
    # Fallback: iterate active household teen+ members.
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return None
        for si in hh.sim_info_gen():
            try:
                if _eligible(si):
                    return si
            except Exception:
                continue
    except Exception:
        pass
    return None


def generate_cold_outreach(callback=None, output=None):
    """Fire one cold-outreach text. Called by auto_events when the
    'dating' event type is chosen. Returns silently if the feature
    is off or no candidates exist."""
    if not cold_outreach_enabled():
        return
    recipient = _pick_cold_outreach_recipient()
    if recipient is None:
        _log("cold outreach skipped: no eligible recipient in household")
        return
    candidates = get_candidates(recipient)
    if not candidates:
        _log(f"cold outreach skipped: no candidates for {getattr(recipient, 'first_name', '?')}")
        return
    contact = random.choice(candidates)
    contact_name = contact.get("name", "?")
    recipient_name = getattr(recipient, "first_name", "?")
    _log(f"cold outreach: {contact_name} -> {recipient_name}")
    from . import phone
    phone.generate_text_for(
        recipient=recipient,
        contact=contact,
        callback=callback,
        output=output,
        prompt_suffix=_COLD_OUTREACH_SUFFIX,
        journal_type_override="dating_outreach",
    )


# ---------------------------------------------------------------------------
# Setup-request intent detection
# ---------------------------------------------------------------------------
#
# Deliberately narrow. False positives are the failure mode we care
# most about: the setup-chain fires an unexpected incoming text and
# introduces a stranger into the player's save without them realizing
# they asked for it. Every rule below is written to bias toward
# under-triggering.
#
# Design gates (all must hold):
#   1. Feature is enabled.
#   2. Player sim is NOT already in a committed relationship (married,
#      engaged, going steady, etc.) -- asking a friend to set you up
#      when you're already partnered is almost always a joke.
#   3. Recipient of the outgoing text is NOT family / NOT a romantic
#      partner (a real setup request goes to a friend, not to your
#      mom or your fiance).
#   4. Text matches an unambiguous whole-phrase pattern (see the list
#      below). Not a substring of a longer phrase; not a keyword hit.
#   5. Text does NOT match a known false-positive phrase (see the
#      counter-patterns list -- e.g. "set me up with a" followed by a
#      non-person word like "recipe" / "meeting").
#
# When all gates pass, return the matched trigger string so the caller
# can log which phrase triggered.

import re as _re

# Whole-phrase triggers -- surrounded by word boundaries. Case-
# insensitive. Every one has been sanity-checked against phrases that
# might come up in casual sim conversation. If a new phrase should be
# added, verify it can't credibly appear in a non-setup context.
_SETUP_REQUEST_PATTERNS = [
    _re.compile(r"\b(set|hook|match)\s+me\s+up\b", _re.IGNORECASE),
    _re.compile(r"\bknow\s+any(one|body)\s+(single|available|cute|hot|good\s+looking)\b", _re.IGNORECASE),
    _re.compile(r"\bknow\s+any\s+(single|available)\s+(guys?|girls?|men|women|people)\b", _re.IGNORECASE),
    _re.compile(r"\bgot\s+any\s+(single|available)\s+(friends?|guys?|girls?)\b", _re.IGNORECASE),
    _re.compile(r"\b(blind\s+date|set(\s|-)up\s+a\s+date)\b", _re.IGNORECASE),
    _re.compile(r"\bfix\s+me\s+up\s+with\s+someone\b", _re.IGNORECASE),
    _re.compile(r"\bset\s+me\s+up\s+with\s+someone\b", _re.IGNORECASE),
]

# Explicit counter-patterns that veto a trigger match. e.g. "set me
# up with a meeting" contains "set me up" but is scheduling, not
# dating. Add here as false positives get reported.
_SETUP_COUNTER_PATTERNS = [
    _re.compile(r"\b(set|hook)\s+me\s+up\s+with\s+(a\s+)?(meeting|call|appointment|reservation|recipe|drink|snack|game|match|workout|lesson|deal|discount|deal|the\s+wifi)\b", _re.IGNORECASE),
    _re.compile(r"\bknow\s+any(one|body)\s+(?!single|available|cute|hot|good)", _re.IGNORECASE),
]


def _sim_is_committed(sim_info):
    """True when the sim has a spouse / engaged / going-steady bit
    with anyone. Committed sims don't get setup requests firing --
    the intent detector is meant for single sims asking friends for
    an introduction."""
    if sim_info is None:
        return False
    try:
        rt = getattr(sim_info, "relationship_tracker", None)
        if rt is None:
            return False
        targets = list(rt.target_sim_gen())
        for tid in targets:
            try:
                bits = list(rt.get_all_bits(tid))
                for b in bits:
                    name = str(getattr(b, "__name__", b)).lower()
                    if _status_has_any(name, _COMMITTED_KEYWORDS):
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _recipient_is_eligible_friend(sender_sim_info, recipient_sim_info):
    """True when the outgoing-text recipient is a plausible person to
    ask for a setup: NOT family, NOT already the sender's romantic
    partner. Uses the sender's own relationship tracker to check the
    recipient's bits."""
    if sender_sim_info is None or recipient_sim_info is None:
        return False
    if getattr(sender_sim_info, "sim_id", None) == getattr(recipient_sim_info, "sim_id", None):
        return False
    try:
        rt = getattr(sender_sim_info, "relationship_tracker", None)
        rid = getattr(recipient_sim_info, "sim_id", None)
        if rt is None or rid is None:
            return True  # unable to read -- default to allowing (worst case a family member gets asked; the setup will still gate on candidate pool)
        bits = list(rt.get_all_bits(rid))
        for b in bits:
            name = str(getattr(b, "__name__", b)).lower()
            if _status_has_any(name, _FAMILY_KEYWORDS):
                return False
            if _status_has_any(name, _COMMITTED_KEYWORDS):
                # Recipient is the sender's romantic partner -- weird
                # place to ask for a setup, block.
                return False
    except Exception:
        pass
    return True


def detect_setup_request(outgoing_text, sender_sim_info, recipient_sim_info):
    """Return the matched trigger phrase (str) if the outgoing text is
    unambiguously a request from `sender` to `recipient` for a
    romantic setup, else None.

    Runs the full gate stack. Every rejection logs its reason so
    false positives / negatives can be diagnosed from Llamafone_Log.
    """
    if not setup_chain_enabled():
        return None
    if not outgoing_text or not isinstance(outgoing_text, str):
        return None

    text = outgoing_text.strip()
    if len(text) < 6:
        return None  # too short to be a coherent request

    # Gate 2: player not already committed.
    if _sim_is_committed(sender_sim_info):
        return None

    # Gate 3: recipient is a plausible friend to ask.
    if not _recipient_is_eligible_friend(sender_sim_info, recipient_sim_info):
        return None

    # Gate 5 (counter-patterns): explicit vetoes for known false
    # positives. Check BEFORE the positive patterns so a phrase that
    # matches both loses.
    for cp in _SETUP_COUNTER_PATTERNS:
        if cp.search(text):
            _log(f"setup-request veto (counter-pattern {cp.pattern!r}) on {text[:80]!r}")
            return None

    # Gate 4: positive pattern match.
    for p in _SETUP_REQUEST_PATTERNS:
        m = p.search(text)
        if m:
            matched = m.group(0)
            _log(f"setup-request detected {matched!r} in text {text[:120]!r}")
            return matched
    return None


# ---------------------------------------------------------------------------
# Pending-introduction queue (per-save)
# ---------------------------------------------------------------------------
#
# Persistent JSON file at <save>/PendingIntroductions.json. Entries are
# fired when their fire_at_ticks <= current sim time. Two entry kinds:
#   - "intro"    : introducee texts the player ("X gave me your number")
#   - "followup" : setter checks in with the player ("how did it go?")
# A setup-chain event creates BOTH: an intro entry at +2 sim days, a
# follow-up at +5 sim days (both approximate; slight jitter added to
# avoid all introductions firing at the same tick).

_QUEUE_FILENAME = "PendingIntroductions.json"
_queue_lock = threading.RLock()
_queue_cache = None
_queue_cached_for_save_id = None

# Constants for tick math -- must match past_events / journal / etc.
# (all 25 ticks per sim-second per the ground-truth check).
_TICKS_PER_MINUTE_DATING = 1500
_TICKS_PER_HOUR_DATING = 60 * _TICKS_PER_MINUTE_DATING
_TICKS_PER_DAY_DATING = 24 * _TICKS_PER_HOUR_DATING

# Delay windows (sim days). Randomized within these bounds so
# introductions don't feel scripted -- one setup fires 2 sim days
# out, the next fires 3, etc.
_INTRO_DELAY_MIN_DAYS = 2
_INTRO_DELAY_MAX_DAYS = 3
_FOLLOWUP_DELAY_MIN_DAYS = 2  # ADDITIONAL days after the intro fires
_FOLLOWUP_DELAY_MAX_DAYS = 3


def _queue_path():
    return _save_id.data_path(_QUEUE_FILENAME)


def _queue_load():
    """Load the queue from disk. Cached per-save. Transient save_id=None
    during zone transitions preserves the last-known cache."""
    global _queue_cache, _queue_cached_for_save_id
    with _queue_lock:
        current = _save_id.get_current_save_id()
        if current is None and _queue_cache is not None:
            return _queue_cache
        if _queue_cache is not None and _queue_cached_for_save_id == current:
            return _queue_cache
        _queue_cached_for_save_id = current
        path = _queue_path()
        if path is None or not os.path.exists(path):
            _queue_cache = {"schema_version": 1, "pending": []}
            return _queue_cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("pending"), list):
                _log(f"queue file at {path} malformed; treating as empty")
                _queue_cache = {"schema_version": 1, "pending": []}
            else:
                _queue_cache = data
        except Exception as e:
            _log(f"queue load failed: {type(e).__name__}: {e}")
            _queue_cache = {"schema_version": 1, "pending": []}
        return _queue_cache


def _queue_save():
    """Atomic write of the in-memory queue to disk. No-op when no save
    is loaded (data stays in _queue_cache until save_id resolves)."""
    with _queue_lock:
        path = _queue_path()
        if path is None or _queue_cache is None:
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_queue_cache, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, path)
        except Exception as e:
            _log(f"queue save failed: {type(e).__name__}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def _now_ticks():
    try:
        import services
        ts = services.time_service()
        now = getattr(ts, "sim_now", None) if ts else None
        if now is None:
            return None
        try:
            return int(now)
        except Exception:
            try:
                return int(now.absolute_ticks())
            except Exception:
                return None
    except Exception:
        return None


def _contact_dict_from_sim_info(si):
    """Build a phone-style contact dict from a raw sim_info. Used when
    firing an intro/followup so generate_text_for gets the same shape
    the rest of the mod uses."""
    if si is None:
        return None
    try:
        name = f"{getattr(si, 'first_name', '')} {getattr(si, 'last_name', '')}".strip()
        return {
            "sim_info": si,
            "sim_id": getattr(si, "sim_id", None),
            "name": name or "Someone",
            "status": "",
            "friendship": None,
            "romance": None,
            "in_household": False,
        }
    except Exception:
        return None


def _resolve_sim_info(sim_id):
    """Look up a sim_info by sim_id via the sim_info_manager. Returns
    None when the sim has been removed from the world (moved out,
    deleted, gone) -- callers should treat that as 'entry stale,
    drop it'."""
    if sim_id is None:
        return None
    try:
        import services
        sm = services.sim_info_manager()
        return sm.get(int(sim_id)) if sm else None
    except Exception:
        return None


def enqueue_setup(setter_contact, introducee_contact, target_sim_info,
                  matched_phrase=""):
    """Add an intro + follow-up pair to the queue. Called after
    detect_setup_request fires. `setter_contact` is who the player
    asked; `introducee_contact` is the candidate the mod picked to
    introduce; `target_sim_info` is the household member the intro
    text will land on."""
    now = _now_ticks()
    if now is None:
        _log("enqueue_setup skipped: no sim clock available")
        return False
    intro_delay = random.randint(_INTRO_DELAY_MIN_DAYS, _INTRO_DELAY_MAX_DAYS) * _TICKS_PER_DAY_DATING
    followup_extra = random.randint(_FOLLOWUP_DELAY_MIN_DAYS, _FOLLOWUP_DELAY_MAX_DAYS) * _TICKS_PER_DAY_DATING
    now_iso = datetime.datetime.now().isoformat()
    setter_id = setter_contact.get("sim_id") if isinstance(setter_contact, dict) else None
    introducee_id = introducee_contact.get("sim_id") if isinstance(introducee_contact, dict) else None
    target_id = getattr(target_sim_info, "sim_id", None) if target_sim_info else None
    if setter_id is None or introducee_id is None or target_id is None:
        _log(f"enqueue_setup skipped: missing id(s) setter={setter_id} introducee={introducee_id} target={target_id}")
        return False
    with _queue_lock:
        data = _queue_load()
        data["pending"].append({
            "kind": "intro",
            "setter_id": str(setter_id),
            "setter_name": setter_contact.get("name") if isinstance(setter_contact, dict) else "",
            "introducee_id": str(introducee_id),
            "introducee_name": introducee_contact.get("name") if isinstance(introducee_contact, dict) else "",
            "target_id": str(target_id),
            "target_name": getattr(target_sim_info, "first_name", ""),
            "fire_at_ticks": int(now + intro_delay),
            "created_at": now_iso,
            "matched_phrase": matched_phrase,
        })
        data["pending"].append({
            "kind": "followup",
            "setter_id": str(setter_id),
            "setter_name": setter_contact.get("name") if isinstance(setter_contact, dict) else "",
            "introducee_id": str(introducee_id),
            "introducee_name": introducee_contact.get("name") if isinstance(introducee_contact, dict) else "",
            "target_id": str(target_id),
            "target_name": getattr(target_sim_info, "first_name", ""),
            "fire_at_ticks": int(now + intro_delay + followup_extra),
            "created_at": now_iso,
            "matched_phrase": matched_phrase,
        })
        _queue_save()
    _log(f"enqueued setup: setter={setter_contact.get('name')} introduces {introducee_contact.get('name')} to {getattr(target_sim_info, 'first_name', '?')} "
         f"(intro in {intro_delay // _TICKS_PER_DAY_DATING}d, followup {followup_extra // _TICKS_PER_DAY_DATING}d after)")
    return True


def _already_pending_between(setter_id, target_id):
    """Prevent stacking multiple simultaneous setup chains from the
    same friend to the same target -- if the player asks the same
    friend twice, only the first request enqueues. Returns True iff
    a live entry exists."""
    with _queue_lock:
        data = _queue_load()
        for entry in data.get("pending", []):
            if entry.get("setter_id") == str(setter_id) and entry.get("target_id") == str(target_id):
                return True
    return False


def maybe_start_setup_chain(outgoing_text, sender_sim_info, recipient_contact):
    """High-level entry point called from phone.send_text after an
    outgoing message. Runs the intent detector; if it triggers,
    picks a candidate to be introduced and enqueues the pair.

    `sender_sim_info` = the household sim who sent the text.
    `recipient_contact` = the contact-dict the text went to (the
    friend being asked for a setup).
    """
    if not setup_chain_enabled():
        return
    recipient_si = recipient_contact.get("sim_info") if isinstance(recipient_contact, dict) else None
    matched = detect_setup_request(outgoing_text, sender_sim_info, recipient_si)
    if not matched:
        return
    # Dedupe: one live chain per (setter, target) pair.
    setter_id = getattr(recipient_si, "sim_id", None)
    target_id = getattr(sender_sim_info, "sim_id", None)
    if setter_id is None or target_id is None:
        return
    if _already_pending_between(setter_id, target_id):
        _log(f"setup chain suppressed: already-pending entry between "
             f"{getattr(recipient_si, 'first_name', '?')} and "
             f"{getattr(sender_sim_info, 'first_name', '?')}")
        return
    # Pick a candidate not already claimed by any live intro to avoid
    # the same person getting introduced twice concurrently.
    exclude = set()
    with _queue_lock:
        data = _queue_load()
        for entry in data.get("pending", []):
            try:
                exclude.add(int(entry.get("introducee_id")))
            except Exception:
                pass
    candidates = get_candidates(sender_sim_info, exclude_sim_ids=exclude)
    if not candidates:
        _log("setup-request detected but no unclaimed candidates in the pool")
        return
    introducee = random.choice(candidates)
    enqueue_setup(recipient_contact, introducee, sender_sim_info,
                  matched_phrase=matched)


# ---------------------------------------------------------------------------
# Intro / follow-up firing
# ---------------------------------------------------------------------------

_INTRO_SUFFIX_TEMPLATE = (
    "\n\nDATING CONTEXT: You (the sender) were introduced to the "
    "recipient by {setter_name}, a mutual acquaintance who suggested "
    "you two might get along. This is your FIRST message to them -- "
    "keep it warm, low-key, and mention that {setter_name} passed "
    "along their number. Do NOT invent details about the recipient's "
    "life, hobbies, or looks; you only know what {setter_name} said, "
    "which the message shouldn't quote verbatim. One short paragraph, "
    "not a wall of text."
)

_FOLLOWUP_SUFFIX_TEMPLATE = (
    "\n\nDATING CONTEXT: A couple of sim-days ago you introduced the "
    "recipient to {introducee_name}. You're now following up to see "
    "how it went. Keep it casual and curious, no pressure. Don't "
    "assume the outcome -- ask; don't declare. Two short sentences "
    "max."
)


def _fire_intro(entry):
    """Send an introducee-to-target text using the intro suffix.

    Runs the same 'target is not committed' guard as cold outreach --
    if the target got married/engaged in the 2-3 sim-day gap between
    when the setup was requested and now, drop the intro silently.
    A romantic intro landing on a freshly-engaged sim reads as
    disruptive; better to no-op than surprise the player."""
    introducee_si = _resolve_sim_info(entry.get("introducee_id"))
    target_si = _resolve_sim_info(entry.get("target_id"))
    if introducee_si is None or target_si is None:
        _log(f"intro entry stale (sim_info missing): {entry}")
        return False
    if _sim_is_committed(target_si):
        _log(f"intro skipped: target {getattr(target_si, 'first_name', '?')} is now committed "
             f"(was single when the setup was queued)")
        # Return True so the queue entry gets removed -- we don't want
        # to retry when the target's still committed 15s from now.
        return True
    if _sim_is_committed(introducee_si):
        _log(f"intro skipped: introducee {getattr(introducee_si, 'first_name', '?')} is now committed "
             f"(was single when picked)")
        return True
    contact = _contact_dict_from_sim_info(introducee_si)
    if contact is None:
        return False
    suffix = _INTRO_SUFFIX_TEMPLATE.format(setter_name=entry.get("setter_name", "a friend"))
    _log(f"firing intro: {contact['name']} -> {getattr(target_si, 'first_name', '?')} (via {entry.get('setter_name')})")
    from . import phone
    phone.generate_text_for(
        recipient=target_si,
        contact=contact,
        prompt_suffix=suffix,
        journal_type_override="dating_intro",
    )
    return True


def _fire_followup(entry):
    """Send a setter check-in text using the follow-up suffix."""
    setter_si = _resolve_sim_info(entry.get("setter_id"))
    target_si = _resolve_sim_info(entry.get("target_id"))
    if setter_si is None or target_si is None:
        _log(f"followup entry stale (sim_info missing): {entry}")
        return False
    contact = _contact_dict_from_sim_info(setter_si)
    if contact is None:
        return False
    suffix = _FOLLOWUP_SUFFIX_TEMPLATE.format(introducee_name=entry.get("introducee_name", "them"))
    _log(f"firing followup: {contact['name']} -> {getattr(target_si, 'first_name', '?')} about {entry.get('introducee_name')}")
    from . import phone
    phone.generate_text_for(
        recipient=target_si,
        contact=contact,
        prompt_suffix=suffix,
        journal_type_override="dating_followup",
    )
    return True


def tick_queue():
    """Iterate the queue. Fire any entry whose fire_at_ticks has
    elapsed. Successful fires are removed from the queue; stale
    entries (sims removed from the world) are also removed."""
    if not setup_chain_enabled():
        return
    now = _now_ticks()
    if now is None:
        return
    with _queue_lock:
        data = _queue_load()
        pending = data.get("pending", [])
        if not pending:
            return
        keep = []
        fired_any = False
        for entry in pending:
            try:
                fire_at = int(entry.get("fire_at_ticks", 0))
            except Exception:
                fire_at = 0
            if fire_at > now:
                keep.append(entry)
                continue
            kind = entry.get("kind")
            fired = False
            try:
                if kind == "intro":
                    fired = _fire_intro(entry)
                elif kind == "followup":
                    fired = _fire_followup(entry)
                else:
                    _log(f"unknown queue entry kind: {kind!r}")
            except Exception as e:
                _log(f"tick_queue firing raised: {type(e).__name__}: {e}")
                fired = False
            if fired:
                fired_any = True
            # Whether or not it fired successfully, drop the entry --
            # stale sims won't come back, and re-attempting every 15s
            # would just log-spam.
        # Persist whenever the entry list changed -- fired entries and
        # stale entries both got removed, and either case should hit
        # disk so a restart doesn't retry them from square one.
        if len(keep) != len(pending):
            data["pending"] = keep
            _queue_save()
