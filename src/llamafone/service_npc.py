"""
Service-NPC role detection.

When a hired butler, maid, babysitter (nanny), gardener, or repair tech
calls or texts one of the player's sims, the AI needs to know they're
speaking as a service provider -- formal register for a butler,
efficient/task-focused for a maid, kids-first for a babysitter, etc.
Without this signal every service NPC talks like a random townie.

Detection strategy is layered because Sims 4's service-NPC systems
vary across pack combos and patches. Order of preference:

  1. Query services.get_service_npc_service()._service_records for the
     active household. Each record maps a hired-service tuning class to
     the sim_ids currently or recently servicing that household. The
     tuning class name (e.g. `serviceNpc_Butler`) classifies the role.

  2. Query the zone situation manager for a role-named situation
     (`butler_situation`, `nanny_situation`, `gardener_situation`)
     that contains this sim. Sims 4 spins up a Situation while an
     NPC is on-shift servicing THIS specific zone -- confirmed real:
     Michelle Zeng, butler_situation, seen in past_events snapshot.

  3. Walk the active household's attributes for anything with a
     role-shaped NAME (e.g. `hired_butler_id`, `_gardener_ids`)
     that references this sim's id. Some packs store hires on the
     household object directly instead of ServiceNpcService.

  4. Check the sim's career name. Career-track service NPCs (e.g.
     "Career Adult NPC Butler SP09" from Vintage Glamour) are a
     distinct system from the one-off hires -- they show up as
     regular sims with a butler/maid/nanny career. Weaker signal
     because "butler by trade" != "this household's butler".

  5. Trait markers like `trait_ServiceNpc_Butler`. Rare / older
     pack conventions.

  6. Fall back to the sim_info first_name / last_name pattern.
     Weakest signal; last resort.

Silently returns None on any failure. A miss is fine: the sim just
gets described as a regular contact, same as v3.5.
"""

import re


# Map lowercase tuning-name / trait / title fragments to display roles.
# The fragment must appear as a substring; order matters only for
# ambiguous names (checked in dict-insertion order).
_ROLE_PATTERNS = (
    ("butler",              "butler"),
    ("maid",                "maid"),
    ("housekeeper",         "maid"),
    # Nanny (Growing Together) is distinct from babysitter -- longer-
    # term, embedded in family life. Kept as its own display role so
    # the AI can hit the right register instead of a one-off vibe.
    ("nanny",               "nanny"),
    ("babysitter",          "babysitter"),
    ("gardener",            "gardener"),
    ("repair",              "repair tech"),
    ("repairman",           "repair tech"),
    ("repairperson",        "repair tech"),
    ("pizza",               "pizza delivery"),
    ("mailcarrier",         "mail carrier"),
    ("mail_carrier",        "mail carrier"),
    ("personaltrainer",     "personal trainer"),
    ("personal_trainer",    "personal trainer"),
    ("massage",             "massage therapist"),
    ("statue",              "statue impersonator"),  # City Living busker
    ("chalet",              "chalet host"),  # Snowy Escape
    ("landlord",            "landlord"),
    ("cityrepair",          "repair tech"),
    ("burglar",             "burglar"),   # Not exactly a service, but same detection path
)


# Role-specific one-liner appended to the caller/sender block so the AI
# writes them in-character. Kept short -- the sim's traits and mood are
# still available; this just anchors the register.
_ROLE_FLAVOR = {
    "butler":              "professional and deferential; refers to the household as their employer, not a friend",
    "maid":                "task-focused; talks about cleaning schedules, supplies, or specific messes; not chatty",
    "nanny":               "warm and family-embedded; knows the kids by name, may reference routines, milestones, and household life -- longer-term than a one-off sitter",
    "babysitter":          "kids-first and occasional; checks in about a specific evening's coverage or scheduling",
    "gardener":            "talks plants, season, weather, and yard work",
    "repair tech":         "practical and quote-driven; talks parts, timing, and what's broken",
    "pizza delivery":      "brief and transactional; delivery ETA or confirmation",
    "mail carrier":        "quick and transactional; package/mail delivery",
    "personal trainer":    "workout-focused; may mention sessions, form, or accountability",
    "massage therapist":   "calm and appointment-focused",
    "chalet host":         "hospitality-focused; guest amenities, arrangements",
    "landlord":            "landlord-tenant register; rent, repairs, complaints",
    "statue impersonator": "detached / cryptic; still 'in character' even off-clock",
    "burglar":             "furtive, evasive -- do not treat as a friend",
}


def _log(msg):
    try:
        from . import _log as root_log
        root_log(f"[service_npc] {msg}")
    except Exception:
        pass


def _classify(text):
    """Return the display role for `text` if it contains a known service-
    role fragment, else None. `text` is any string -- tuning class name,
    trait name, sim title. Comparison is case-insensitive and ignores
    non-alphanumeric characters."""
    if not text:
        return None
    normalized = re.sub(r"[^a-z0-9]", "", text.lower())
    for fragment, role in _ROLE_PATTERNS:
        if fragment in normalized:
            return role
    return None


def _role_from_service_records(sim_id):
    """Path 1: ServiceNpcService. Returns the role for this sim_id if
    they're recorded as an active or recent hire for the active
    household. None if no service_npc_service or no match."""
    try:
        import services
        svc = None
        for accessor in ("get_service_npc_service", "service_npc_service"):
            fn = getattr(services, accessor, None)
            if callable(fn):
                try:
                    svc = fn()
                except Exception:
                    svc = None
                if svc is not None:
                    break
        if svc is None:
            return None
        hh = services.active_household()
        hh_id = getattr(hh, "id", None) if hh is not None else None
        if hh_id is None:
            return None
        records = getattr(svc, "_service_records", None)
        if not records:
            return None
        # `records` is a nested dict: {household_id: {service_tuning: record}}.
        # Some builds flatten to {(hh_id, service_tuning): record}. Handle both.
        candidates = []
        try:
            nested = records.get(hh_id) if hasattr(records, "get") else None
            if nested and hasattr(nested, "items"):
                for tuning, record in nested.items():
                    candidates.append((tuning, record))
        except Exception:
            pass
        if not candidates:
            # Flat variant
            try:
                for key, record in records.items():
                    if isinstance(key, tuple) and len(key) >= 2 and key[0] == hh_id:
                        candidates.append((key[1], record))
            except Exception:
                pass
        for tuning, record in candidates:
            role = _classify(getattr(tuning, "__name__", "") or str(tuning))
            if role is None:
                continue
            # A record's sim_ids live under different attribute names by
            # build: `hired_service_ids`, `_hired_service_ids`,
            # `recent_service_sim_ids`, `_service_sim_ids`. Try all.
            for attr in (
                "hired_service_ids", "_hired_service_ids",
                "recent_service_sim_ids", "_recent_service_sim_ids",
                "service_sim_ids", "_service_sim_ids",
                "sim_ids", "_sim_ids",
            ):
                ids = getattr(record, attr, None)
                if ids is None:
                    continue
                try:
                    if int(sim_id) in {int(x) for x in ids}:
                        return role
                except Exception:
                    continue
            # Some builds expose a single sim_id
            for attr in ("service_sim_id", "sim_id", "hired_sim_id"):
                sid = getattr(record, attr, None)
                if sid is None:
                    continue
                try:
                    if int(sid) == int(sim_id):
                        return role
                except Exception:
                    continue
    except Exception as e:
        _log(f"_role_from_service_records raised: {type(e).__name__}: {e}")
    return None


def _role_from_career(sim_info):
    """Path 3: check the sim's career track for a role name. Career-
    track service NPCs -- butler, maid, nanny (etc.) with a full
    career progression -- surface as regular sims running a career
    like `career_Adult_NPC_Butler_SP09` rather than as one-off hires
    in ServiceNpcService. The career tuning name usually contains
    the role fragment (`butler`, `maid`, `nanny`) even when it's
    padded with `Adult_NPC` / `_SP09` / other qualifiers."""
    try:
        career_tracker = getattr(sim_info, "career_tracker", None)
        if career_tracker is None:
            return None
        careers = getattr(career_tracker, "careers", None)
        if not careers:
            return None
        career_iter = careers.values() if hasattr(careers, "values") else careers
        for career in career_iter:
            if career is None:
                continue
            for accessor in (
                # Class name of the career tuning is the canonical
                # signal; guid-based lookups vary too much across
                # builds. The tuning class name lives on `type(career)`.
                lambda c: getattr(type(c), "__name__", ""),
                lambda c: getattr(c, "get_career_name", lambda: "")() if hasattr(c, "get_career_name") else "",
                lambda c: str(getattr(c, "current_track_tuning", "") or ""),
            ):
                try:
                    text = accessor(career)
                except Exception:
                    continue
                role = _classify(text)
                if role is not None:
                    return role
    except Exception as e:
        _log(f"_role_from_career raised: {type(e).__name__}: {e}")
    return None


def _role_from_active_situations(sim_id):
    """Path 1.7: check the zone's active situations for a service-role
    situation the sim is part of. Sims 4 uses Situations to model
    active service work -- `butler_situation`, `nanny_situation`,
    `gardener_situation`, etc. -- and the situation includes its
    hired sim_ids. When we find our sim_id in a situation whose
    class name classifies to a role, we've confirmed they're
    actively servicing THIS household (this zone). Strong signal:
    the situation is only spun up while the NPC is on-shift for
    this specific household.
    """
    try:
        import services
        sm = services.get_zone_situation_manager()
        if sm is None:
            return None
        try:
            sid = int(sim_id)
        except Exception:
            return None
        # `running_situations` yields Situation instances; each has
        # `_situations` guests/roles internally. We check both the
        # situation's class name AND the sim_ids it wraps.
        situations = getattr(sm, "running_situations", None)
        iter_situations = situations() if callable(situations) else situations
        if not iter_situations:
            return None
        for sit in iter_situations:
            if sit is None:
                continue
            cls_name = getattr(type(sit), "__name__", "") or ""
            role = _classify(cls_name)
            if role is None:
                continue
            # Does the situation contain our sim_id? Check the sims
            # currently in any of its assigned roles.
            try:
                sim_iter = getattr(sit, "all_sims_in_situation_gen", None)
                if callable(sim_iter):
                    for si_in in sim_iter():
                        try:
                            if int(getattr(si_in, "sim_id", 0)) == sid:
                                return role
                        except Exception:
                            continue
            except Exception:
                pass
            # Fallback: some Situation subclasses expose a `_guest_list`
            # / `_situation_sims` / `_sim_ids` collection instead.
            for attr in ("_guest_list", "_situation_sims",
                         "_sim_ids", "_actor_sim_ids"):
                coll = getattr(sit, attr, None)
                if coll is None:
                    continue
                try:
                    for item in coll:
                        try:
                            iid = int(getattr(item, "sim_id", item))
                            if iid == sid:
                                return role
                        except Exception:
                            continue
                except Exception:
                    continue
    except Exception as e:
        _log(f"_role_from_active_situations raised: {type(e).__name__}: {e}")
    return None


def _role_from_household_attrs(sim_id):
    """Path 1.5: probe the active household for direct butler/gardener
    tie-back attributes. Some packs (notably Vintage Glamour for the
    butler, Seasons for the gardener) store the hired NPC's sim_id
    directly on the household object rather than going through
    ServiceNpcService. Naming varies -- try the common candidates.

    Returns (role, confirmed_hire=True) when we find the sim_id
    referenced by an attribute whose NAME classifies to a role. That
    name-match is what makes this a household hire signal: the
    attribute `hired_butler_id` semantically ties this sim to the
    household as a butler.
    """
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return None
        try:
            sid = int(sim_id)
        except Exception:
            return None
        # Walk the household's attributes looking for anything that
        # (a) classifies to a service role via its NAME, and (b) points
        # at (or contains) our sim_id.
        for attr_name in dir(hh):
            if attr_name.startswith("__"):
                continue
            role = _classify(attr_name)
            if role is None:
                continue
            try:
                val = getattr(hh, attr_name)
            except Exception:
                continue
            if val is None:
                continue
            try:
                if isinstance(val, (int,)):
                    if int(val) == sid:
                        return role
                elif hasattr(val, "__iter__") and not isinstance(val, str):
                    for item in val:
                        try:
                            if int(item) == sid:
                                return role
                        except Exception:
                            continue
            except Exception:
                continue
    except Exception as e:
        _log(f"_role_from_household_attrs raised: {type(e).__name__}: {e}")
    return None


def _role_from_traits(sim_info):
    """Path 2: check the sim's traits for a role-marker trait name."""
    try:
        tracker = getattr(sim_info, "trait_tracker", None)
        if tracker is None:
            return None
        for iter_name in ("personality_traits", "equipped_traits", "_equipped_traits"):
            it = getattr(tracker, iter_name, None)
            if it is None:
                continue
            try:
                traits = list(it) if hasattr(it, "__iter__") else list(it())
            except Exception:
                continue
            for t in traits:
                name = getattr(t, "__name__", "") or str(t)
                role = _classify(name)
                if role is not None:
                    return role
    except Exception as e:
        _log(f"_role_from_traits raised: {type(e).__name__}: {e}")
    return None


def _role_from_sim_title(sim_info):
    """Path 3: last-resort match against the sim's own generated name.
    Sims 4 occasionally names service NPCs like `The Butler` -- weak
    but non-zero signal when other paths return nothing."""
    try:
        parts = [
            str(getattr(sim_info, "first_name", "") or ""),
            str(getattr(sim_info, "last_name", "") or ""),
        ]
        joined = " ".join(parts).strip()
        return _classify(joined)
    except Exception:
        return None


def get_service_role(sim_info):
    """Return (role, confirmed_hire) for a sim, or (None, False).
    `confirmed_hire=True` means detection verified this sim is actively
    tied to the active household (via ServiceNpcService records or a
    household attribute like `hired_butler_id`). `confirmed_hire=False`
    means we detected the ROLE (career track, trait, or title
    heuristic) but couldn't confirm they're this household's specific
    hire -- they might be someone else's butler you happen to know.

    Detection order, most-reliable first:
      1. ServiceNpcService hire records -> role, confirmed=True
      2. Active `butler_situation` / `nanny_situation` / etc. in the
         zone situation manager containing this sim -> role, confirmed=True
      3. Household direct attributes (butler_id / gardener_id / etc.)
         -> role, confirmed=True
      4. Career track name -> role, confirmed=False
      5. Trait markers -> role, confirmed=False
      6. Sim title / generated name pattern -> role, confirmed=False
    """
    if sim_info is None:
        return None, False
    sim_id = getattr(sim_info, "sim_id", None)
    if sim_id is not None:
        role = _role_from_service_records(sim_id)
        if role:
            return role, True
        role = _role_from_active_situations(sim_id)
        if role:
            return role, True
        role = _role_from_household_attrs(sim_id)
        if role:
            return role, True
    role = _role_from_career(sim_info)
    if role:
        return role, False
    role = _role_from_traits(sim_info)
    if role:
        return role, False
    role = _role_from_sim_title(sim_info)
    if role:
        return role, False
    return None, False


def _log_household_role_attrs_for_diagnosis(sim_id, role):
    """When we detect a service role from a WEAK signal (career /
    trait / title) but couldn't confirm household hire, dump the
    active household's role-shaped attribute names to the log. Lets
    us iterate on which pack stores butler / gardener / etc. IDs
    where, without having to guess or query the game live."""
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return
        candidate_names = []
        for attr_name in dir(hh):
            if attr_name.startswith("__"):
                continue
            if _classify(attr_name) is None:
                continue
            candidate_names.append(attr_name)
        if candidate_names:
            _log(
                f"role={role!r} detected for sim_id={sim_id} via weak signal "
                f"(no confirmed hire). Household has these role-named "
                f"attributes to consider: {candidate_names}"
            )
    except Exception:
        pass


def format_for_prompt(sim_info, sim_name):
    """Return a prompt-ready line describing the sim's service role,
    or empty string when they're not a service NPC (or detection
    failed). Callers embed this into the sender/caller descriptor
    block so the AI writes them in-character.

    Wording depends on whether detection could confirm the sim is
    tied to THIS household's hire vs "they have a service career":
      - confirmed hire: "X is the household's Y" (definite)
      - career-only:    "X is a professional Y" (still gives the AI
        the register but doesn't overclaim who employs them)

    The "at work" / "supposed to be at work" phrasing for confirmed
    hires is handled separately by transform_work_status() so the
    role tag itself stays short and non-contradictory."""
    role, confirmed_hire = get_service_role(sim_info)
    if not role or not sim_name:
        return ""
    if not confirmed_hire:
        _log_household_role_attrs_for_diagnosis(
            getattr(sim_info, "sim_id", None), role,
        )
    flavor = _ROLE_FLAVOR.get(role, "")
    if confirmed_hire:
        line = (
            f"{sim_name} is the household's {role} (a hired service NPC, "
            f"NOT a friend or family)."
        )
    else:
        line = (
            f"{sim_name} is a professional {role} by trade (a service "
            f"NPC role, not a friend or family)."
        )
    if flavor:
        line += f" Register: {flavor}."
    return line


def transform_work_status(work_status_text, sim_info):
    """Rewrite a sim's generic 'at work' / 'supposed to be at work'
    phrase into role-aware phrasing when they're a CONFIRMED service
    NPC hire for this household. Fixes the case where Sims 4's raw
    work-status reads as if the sim has a separate off-site job when
    their actual work IS this household.

    Returns the input unchanged when detection can't confirm the sim
    is this household's hire, or when the input isn't a work-status
    phrase we care about (school venues, etc.), or when detection
    fails entirely. Silent no-op on any error.
    """
    if not work_status_text or sim_info is None:
        return work_status_text
    try:
        role, confirmed_hire = get_service_role(sim_info)
    except Exception:
        return work_status_text
    if not confirmed_hire or not role:
        return work_status_text
    if "at work" in work_status_text:
        # Both "at work right now" and "supposed to be at work right now"
        # collapse to the same fact for a household hire: their shift
        # for this household is active. The Sims 4 distinction (clocked
        # in vs scheduled but not yet started) doesn't matter for
        # dialogue purposes.
        return f"currently on shift as the household's {role}"
    return work_status_text
