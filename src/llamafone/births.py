"""
Birth announcements, event-driven.

Hooks `PregnancyTracker.complete_pregnancy` -- the one method every
birth passes through (player deliveries via the DeliverBaby
interactions AND NPC off-lot births via `_on_pregnancy_complete`).
When a sim outside the player's household gives birth, and that sim
(or the other parent) is family or a close friend of someone in the
player's household, we schedule an incoming text from them a few
real minutes later. The text goes through the normal
`phone.generate_text_for` pipeline, so the sender's household block
carries the "had a baby" milestone + circumstances and the
announce-once rule makes the birth the topic.

Signature verified against simulation.zip on 2026-09-26:
  PregnancyTracker.complete_pregnancy(self, sim_infos)
  -- `sim_infos` are the newborn SimInfos; `self._sim_info` is the
     parent who was pregnant. Called BEFORE clear_pregnancy, so the
     tracker's partner is still readable inside the hook.

Skipped when: the parent lives in the active household (the player
tells people themselves), no household sim has a qualifying tie, the
tie is muted in contact prefs, or the game isn't in a loaded zone
when the timer fires.
"""

import random
import threading

_hook_installed = False
_announced = set()  # (parent_sim_id, tuple(offspring_ids)) -- one text per birth

_CLOSE_FRIEND_MIN = 45   # matches phone._friendship_label "close friends"
_DELAY_RANGE_SECONDS = (5, 20)   # near-immediate: new parents text right away


def _log(message):
    try:
        from . import _log as root_log
        root_log(f"[births] {message}")
    except Exception:
        pass


def _name(si):
    try:
        return f"{si.first_name} {si.last_name}".strip()
    except Exception:
        return "?"


def _household_of(si):
    """`sim_info.household` is None for off-lot NPCs; fall back to the
    household manager by id (same workaround phone._get_sim_home_world
    uses)."""
    hh = getattr(si, "household", None)
    if hh is not None:
        return hh
    try:
        import services
        hh_id = getattr(si, "household_id", None)
        if hh_id:
            mgr = services.household_manager()
            return mgr.get(hh_id) if mgr else None
    except Exception:
        pass
    return None


def _is_teen_plus(si):
    age = str(getattr(si, "age", "")).replace("Age.", "")
    return age not in ("BABY", "INFANT", "TODDLER", "CHILD")


def _family_rank(label):
    """Closeness rank for a family label from _get_family_relationship
    ("Sister", "Brother-in-law", "Grandmother", ...). Immediate family
    outranks grandparents, who outrank aunts/uncles/cousins, who
    outrank in-laws and step relations. A flat 'family = 1000' let a
    brother-in-law tie with a sister and win on iteration order."""
    l = str(label or "").lower()
    if "in-law" in l or "inlaw" in l or "step" in l:
        return 700
    if any(k in l for k in ("sibling", "brother", "sister", "mother", "father",
                            "parent", "son", "daughter", "child", "spouse",
                            "wife", "husband")):
        return 1000
    if "grand" in l:
        return 900
    if any(k in l for k in ("aunt", "uncle", "niece", "nephew", "cousin")):
        return 800
    return 600


def _tie_strength(household_si, candidate_entry, candidate_si):
    """Return (score, label) for how strongly `candidate` is tied to
    `household_si`. Family beats friendship; within family, closeness
    rank + friendship as tiebreaker. None when no qualifying tie."""
    from . import phone
    try:
        fam = phone._get_family_relationship(candidate_si, candidate_entry, recipient=household_si)
    except Exception:
        fam = None
    friendship = candidate_entry.get("friendship")
    tiebreak = max(0.0, min(0.99, (friendship or 0) / 101.0))
    if fam:
        return (_family_rank(fam) + tiebreak, f"family ({fam}, friendship {friendship})")
    if friendship is not None and friendship >= _CLOSE_FRIEND_MIN:
        return (float(friendship), f"close friend (friendship {friendship})")
    return None


def _best_announcement(parent_si, partner_si):
    """Pick the single strongest (recipient, sender_contact, label)
    across the active household x {parent, partner}. None if nothing
    qualifies."""
    import services
    from . import sim_context, contact_prefs
    hh = services.active_household()
    if hh is None:
        return None
    parent_hh = getattr(parent_si, "household_id", None)
    if parent_hh is not None and parent_hh == getattr(hh, "id", None):
        _log(f"{_name(parent_si)} is in the active household -- player announces their own birth; skipping")
        return None
    senders = [s for s in (parent_si, partner_si) if s is not None]
    best = None
    for hs in hh.sim_info_gen():
        if not _is_teen_plus(hs):
            continue
        try:
            _members, rels = sim_context.get_sim_network(hs, min_friendship=0)
        except Exception:
            continue
        by_id = {r.get("sim_id"): r for r in rels}
        for sender in senders:
            entry = by_id.get(getattr(sender, "sim_id", None))
            if not entry:
                continue
            try:
                if contact_prefs.is_muted(getattr(hs, "sim_id", None), getattr(sender, "sim_id", None)):
                    continue
            except Exception:
                pass
            tie = _tie_strength(hs, entry, sender)
            if tie is None:
                continue
            if best is None or tie[0] > best[0]:
                best = (tie[0], hs, entry, tie[1])
    if best is None:
        return None
    _score, recipient, contact, label = best
    return recipient, contact, label


def _newborns(parent_si, max_age_days=None, require_parent=False):
    """BABY-stage sim_infos in the parent's household. With
    `max_age_days`, only babies younger than that (by `age_progress`,
    days into the current life stage) -- so an older sibling who is
    still a BABY doesn't get mistaken for this birth. With
    `require_parent`, the baby's genealogy must list the parent (or
    their pregnancy partner) when genealogy is readable."""
    out = []
    try:
        hh = _household_of(parent_si)
        if hh is None:
            return out
        parent_ids = {getattr(parent_si, "sim_id", None)}
        try:
            pt = getattr(parent_si, "pregnancy_tracker", None)
            pid = pt.get_partner_id() if pt is not None else None
            if pid:
                parent_ids.add(pid)
        except Exception:
            pass
        try:
            parent_ids.add(getattr(parent_si, "spouse_sim_id", None))
        except Exception:
            pass
        for si in hh.sim_info_gen():
            if str(getattr(si, "age", "")).replace("Age.", "") != "BABY":
                continue
            if max_age_days is not None:
                try:
                    ap = getattr(si, "age_progress", None)
                    ap = float(ap() if callable(ap) else ap)
                    if ap > max_age_days:
                        continue
                except Exception:
                    continue  # can't read the age: don't count it as a newborn
            if require_parent:
                try:
                    from sims.genealogy_tracker import FamilyRelationshipIndex
                    gen = getattr(si, "genealogy", None)
                    pids = set()
                    if gen is not None:
                        for idx in (FamilyRelationshipIndex.MOTHER, FamilyRelationshipIndex.FATHER):
                            try:
                                pid = gen.get_family_relationship(idx)
                                if pid:
                                    pids.add(pid)
                            except Exception:
                                pass
                    if pids and not (pids & parent_ids):
                        continue  # somebody else's newborn (e.g. a roommate's)
                except Exception:
                    pass  # genealogy unreadable: accept on age alone
            out.append(si)
    except Exception:
        pass
    return out


def _newborn_names(parent_si, max_age_days=None):
    return [_name(si) for si in _newborns(parent_si, max_age_days=max_age_days)]


def _fire_announcement(recipient, contact, parent_si, label, dry_run=False):
    try:
        import services
        if services.current_zone() is None:
            _log("zone not loaded at fire time; dropping announcement")
            return
        hh = services.active_household()
        if hh is None or all(getattr(s, "sim_id", None) != getattr(recipient, "sim_id", None) for s in hh.sim_info_gen()):
            _log("recipient no longer in active household; dropping announcement")
            return
        # Make sure the birth milestone exists before the prompt builds.
        try:
            from . import milestones
            milestones.scan_sims([parent_si, contact.get("sim_info")])
        except Exception as e:
            _log(f"milestone refresh failed: {type(e).__name__}: {e}")
        from . import phone
        # Name WHOSE baby. Without this, when the recipient has also just
        # given birth, the model merged the two and announced the
        # recipient's own baby back to her.
        parent_name = _name(parent_si)
        babies = _newborn_names(parent_si)
        baby_part = f" The baby: {', '.join(babies)}." if babies else ""
        sender_is_parent = getattr(contact.get("sim_info"), "sim_id", None) == getattr(parent_si, "sim_id", None)
        who = "YOU" if sender_is_parent else f"your household member {parent_name.upper()}"
        if dry_run:
            suffix = (
                f"\n\n[TEST MODE -- pretend that {who} JUST GAVE BIRTH, right now, "
                f"even if the household block above still shows the pregnancy in "
                f"progress. This message is the birth announcement for {parent_name}'s "
                f"baby. Lead with it. Do NOT confuse it with any baby the recipient "
                f"may have -- this is about {parent_name}'s baby.]"
            )
        else:
            suffix = (
                f"\n\n[THIS MESSAGE EXISTS BECAUSE {who} JUST HAD THE BABY.{baby_part} "
                f"This is the announcement of {parent_name}'s baby -- lead with it. "
                f"Do NOT confuse it with any baby the recipient may have. Tone per "
                f"the circumstances.]"
            )
        _log(f"announcing birth of {_name(parent_si)}'s baby: {contact.get('name')} -> {_name(recipient)} ({label})")
        # Test mode never journals: the pretend announcement must not
        # become canon for later prompts.
        phone.generate_text_for(recipient, contact, prompt_suffix=suffix, skip_journal=dry_run)
    except Exception as e:
        _log(f"_fire_announcement raised: {type(e).__name__}: {e}")


def _on_birth(tracker, offspring_sim_infos):
    try:
        parent_si = getattr(tracker, "_sim_info", None)
        if parent_si is None:
            return
        key = (getattr(parent_si, "sim_id", None),
               tuple(sorted(getattr(s, "sim_id", 0) for s in (offspring_sim_infos or ()) if s is not None)))
        partner_si = None
        try:
            partner_si = tracker.get_partner()
        except Exception:
            partner_si = None
        announce_birth(parent_si, partner_si, dedup_key=key)
    except Exception as e:
        _log(f"_on_birth raised: {type(e).__name__}: {e}")


def announce_birth(parent_si, partner_si, dedup_key=None):
    """Shared entry for every birth detector (complete_pregnancy hook,
    snapshot watcher). Picks the strongest tie into the active household
    and schedules the announcement text. `dedup_key` guards against the
    same birth being reported by more than one detector."""
    try:
        key = dedup_key or ("parent", getattr(parent_si, "sim_id", None))
        pkey = ("parent", getattr(parent_si, "sim_id", None))
        if key in _announced or pkey in _announced:
            return
        _announced.add(key)
        _announced.add(pkey)
        # Config knob (llamafone.cfg: birth_announcements). Checked here
        # rather than at hook install so `llama.reload` toggles it live.
        # The birth itself is still recorded as a milestone either way.
        try:
            from . import config
            if not config.get_birth_announcements_enabled():
                _log(f"birth of {_name(parent_si)}'s baby recorded; announcements disabled in config -- no text")
                return
        except Exception:
            pass
        pick = _best_announcement(parent_si, partner_si)
        if pick is None:
            _log(f"birth: {_name(parent_si)} -- no family / close-friend tie to the active household; no announcement")
            return
        recipient, contact, label = pick
        delay = random.randint(*_DELAY_RANGE_SECONDS)
        _log(f"birth: {_name(parent_si)} -> scheduling announcement from {contact.get('name')} to {_name(recipient)} in {delay}s ({label})")
        t = threading.Timer(delay, _fire_announcement, args=(recipient, contact, parent_si, label))
        t.daemon = True
        try:
            from . import phone
            phone._track_timer(t)
        except Exception:
            pass
        t.start()
    except Exception as e:
        _log(f"announce_birth raised: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Snapshot watcher -- catches births that never pass through the hook
# ---------------------------------------------------------------------------
#
# MCCC's "complete pregnancy" and the game's off-lot NPC birth path can
# create the baby without calling PregnancyTracker.complete_pregnancy
# (observed 2026-09-26: Martha delivered via MCCC in-labor, baby in the
# household roster, no hook fire). So every couple of real minutes we
# check the sims our milestone snapshots last saw as pregnant; any that
# are no longer pregnant get a targeted milestone scan (which records
# pregnancy_end + the partner mirror) and the same announcement.

_WATCH_INTERVAL_SECONDS = 120
_watcher_started = False


def _watch_once():
    import services
    from . import milestones, save_id
    if services.current_zone() is None or save_id.get_current_save_id() is None:
        return
    snaps = milestones._load_snapshots()
    sm = services.sim_info_manager()
    heartbeat = []
    for sid_key, snap in list(snaps.items()):
        if not snap.get("is_pregnant"):
            continue
        try:
            si = sm.get(int(sid_key))
        except Exception:
            si = None
        if si is None:
            continue
        try:
            still = bool(getattr(si, "is_pregnant", False))
        except Exception:
            continue
        newborns = _newborns(si, max_age_days=1.0, require_parent=True)
        heartbeat.append(
            f"{_name(si)}={'pregnant' if still else 'NOT pregnant'}"
            + (f" +newborn({', '.join(_name(b) for b in newborns)})" if newborns else "")
        )
        if ("parent", getattr(si, "sim_id", None)) in _announced:
            continue
        if getattr(si, "is_dead", False):
            continue
        if still and not newborns:
            continue
        # Two birth signals:
        #   A. flag flipped (pregnant -> not) AND a newborn is present
        #      (the game also clears a pregnancy when it culls an off-lot
        #      sim to minimum LOD -- no baby, not a birth);
        #   B. flag STILL says pregnant but a newborn under a day old with
        #      her as parent is in the household (MCCC / off-lot paths can
        #      create the baby without clearing the tracker promptly).
        if not newborns:
            _log(f"watcher: {_name(si)} no longer pregnant but no NEWBORN (<1 day) in household -- ignoring (culled / cleared / older sibling)")
            continue
        partner_si = None
        pid = snap.get("pregnancy_partner_id") or snap.get("spouse_id")
        if pid:
            try:
                partner_si = sm.get(int(pid))
            except Exception:
                partner_si = None
        if still:
            _log(f"watcher: {_name(si)} still flagged pregnant but has newborn {[_name(b) for b in newborns]} -- treating as a birth (tracker not cleared)")
            try:
                milestones.record_birth_now(si, pid)
            except Exception as e:
                _log(f"watcher: record_birth_now failed: {type(e).__name__}: {e}")
        else:
            _log(f"watcher: {_name(si)} is no longer pregnant (snapshot said pregnant) -- treating as a birth")
            try:
                milestones.scan_sims([si] + ([partner_si] if partner_si else []))
            except Exception as e:
                _log(f"watcher: milestone scan failed: {type(e).__name__}: {e}")
        announce_birth(si, partner_si)
    if heartbeat:
        _log("watcher pass: " + "; ".join(heartbeat))


def watch_report():
    """One watcher pass with a line per pregnant-flagged sim explaining
    what it saw. For llama.birthwatch. Also performs the announcement
    when a birth is detected (same as the background pass)."""
    out = []
    try:
        import services
        from . import milestones, save_id
        if services.current_zone() is None or save_id.get_current_save_id() is None:
            return ["zone not loaded / no save id -- watcher idle"]
        snaps = milestones._load_snapshots()
        sm = services.sim_info_manager()
        flagged = [(k, v) for k, v in snaps.items() if v.get("is_pregnant")]
        out.append(f"{len(flagged)} sim(s) flagged pregnant in the last snapshot")
        for sid_key, snap in flagged:
            try:
                si = sm.get(int(sid_key))
            except Exception:
                si = None
            if si is None:
                out.append(f"  {snap.get('name')}: sim_info not found")
                continue
            live = bool(getattr(si, "is_pregnant", False))
            babies = _newborn_names(si, max_age_days=1.0)
            all_babies = _newborn_names(si)
            done = ("parent", getattr(si, "sim_id", None)) in _announced
            out.append(
                f"  {_name(si)}: live is_pregnant={live}; babies in household={all_babies}; "
                f"newborn(<1d)={babies}; already_announced={done}"
            )
    except Exception as e:
        out.append(f"report failed: {type(e).__name__}: {e}")
    try:
        _watch_once()
    except Exception as e:
        out.append(f"watch pass raised: {type(e).__name__}: {e}")
    return out


def _watch_loop():
    import time
    while True:
        time.sleep(_WATCH_INTERVAL_SECONDS)
        try:
            _watch_once()
        except Exception as e:
            _log(f"watcher tick raised: {type(e).__name__}: {e}")


def start_watcher():
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True
    threading.Thread(target=_watch_loop, daemon=True, name="Llamafone-BirthWatch").start()
    _log("birth watcher started")


def install_hook():
    """Monkey-patch PregnancyTracker.complete_pregnancy on the class.
    Idempotent. Returns True if in place, False if not importable yet."""
    global _hook_installed
    if _hook_installed:
        return True
    try:
        from sims.pregnancy.pregnancy_tracker import PregnancyTracker
    except Exception as e:
        _log(f"install_hook: PregnancyTracker not importable: {type(e).__name__}: {e}")
        return False
    if getattr(PregnancyTracker, "_llamafone_births_hooked", False):
        _hook_installed = True
        return True
    original = PregnancyTracker.complete_pregnancy

    def _patched(self, sim_infos, *args, **kwargs):
        # Partner is readable before the original runs (clear_pregnancy
        # comes later in the caller), but let the game create/place the
        # babies first so the household list is populated when we fire.
        result = original(self, sim_infos, *args, **kwargs)
        try:
            _on_birth(self, sim_infos)
        except Exception as e:
            _log(f"birth hook handler raised: {type(e).__name__}: {e}")
        return result

    PregnancyTracker.complete_pregnancy = _patched
    PregnancyTracker._llamafone_births_hooked = True
    _hook_installed = True
    _log("install_hook: PregnancyTracker.complete_pregnancy patched")
    return True
