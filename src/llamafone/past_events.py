"""
Past-event memory for shared calendar events.

Sims 4's CalendarService removes one-off social events (funerals, weddings,
parties, dinners) from `_event_data_map` once they end -- so by the time
the AI generates dialogue the next day, the calendar can't tell us "what
funeral did we both just attend." Holidays repeat yearly and stay, but
social events vanish.

This module keeps a side-file per save (`PastEvents.json`) of events the
mod observed via drama-node lifecycle hooks, so we can surface "you both
attended X recently" for a few in-game days after the event ends.

Hook strategy:

  Monkey-patch `BaseDramaNode._setup` so EVERY drama node instance
  registers our recorder via `self.add_callback_on_complete_func`. The
  engine then calls our recorder when the node completes, regardless of
  the specific drama-node subclass (dinner parties, weddings, funerals,
  player-planned events, holidays — all flow through BaseDramaNode).

  The previous attempt patched `PlayerPlannedDramaNode._run` and
  `.cleanup` -- these are internal lifecycle methods that didn't fire
  for dinner parties. Verified the right approach against the game's
  drama_node.pyc on 2026-06-17:
    add_callback_on_complete_func(fn) ==> self._callbacks_on_complete.append(fn)
  i.e. the canonical public registration point.

Storage: `<save folder>/PastEvents.json`, atomic .tmp + os.replace writes,
RLock-guarded against concurrent record/read.
"""

import datetime
import json
import os
import threading

from . import save_id as _save_id


_FILENAME = "PastEvents.json"

# Retention window for old entries. In-game days, not real-world. After
# this many in-game days past the event start, the entry gets dropped on
# the next cleanup pass.
_RETENTION_IN_GAME_DAYS = 30

# How far back to surface events as "recent" when building a prompt.
# In-game days.
_RECENT_WINDOW_IN_GAME_DAYS = 5

# Ticks per minute in Sims 4's DateAndTime math. Documented in the
# game's date_and_time module; the conversion ratio is stable.
_TICKS_PER_MINUTE = 60000  # Sims 4 uses 1000 ticks/sim-sec (REAL_MILLISECONDS_PER_SIM_SECOND); 60 sim-sec = 1 sim-min


_cache = None
_cached_for_save_id = None
_lock = threading.RLock()
_hook_installed = False


def _log(message):
    """Best-effort log line into the main Llamafone_Log.txt."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [past_events] {message}\n")
    except Exception:
        pass


def _path():
    """Per-save PastEvents.json path; None when no save is loaded."""
    return _save_id.data_path(_FILENAME)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _load():
    with _lock:
        global _cache, _cached_for_save_id
        current = _save_id.get_current_save_id()
        if _cache is not None and _cached_for_save_id == current:
            return _cache
        _cached_for_save_id = current
        path = _path()
        if path is None or not os.path.exists(path):
            _cache = {}
            return _cache
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
        except Exception as e:
            _log(f"_load: parse failed ({type(e).__name__}: {e}); starting fresh")
            _cache = {}
        return _cache


def _save(data):
    with _lock:
        global _cache, _cached_for_save_id
        _cache = data
        _cached_for_save_id = _save_id.get_current_save_id()
        path = _path()
        if path is None:
            # Save wasn't loaded when this fired; drop silently rather
            # than log-spam on every _destroy that hits us pre-load.
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, path)
        except Exception as e:
            _log(f"_save failed: {type(e).__name__}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# DateAndTime helpers
# ---------------------------------------------------------------------------

def _ticks_of(start_time):
    """Pull a numeric tick value out of a Sims 4 DateAndTime object so we
    can persist and compare across sessions. Different versions expose
    the same number through slightly different attrs."""
    if start_time is None:
        return None
    for attr in ("absolute_ticks", "value", "ticks"):
        fn = getattr(start_time, attr, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                continue
        if fn is not None:
            try:
                return int(fn)
            except Exception:
                continue
    # Last-ditch: many DateAndTime reprs look like "DateAndTime(123456)"
    try:
        raw = str(start_time)
        if "(" in raw and raw.endswith(")"):
            return int(raw.split("(")[-1][:-1])
    except Exception:
        pass
    return None


def _now_ticks():
    try:
        import services
        ts = services.time_service()
        now = getattr(ts, "sim_now", None) if ts else None
        return _ticks_of(now)
    except Exception:
        return None


def _ticks_to_minutes(ticks):
    if ticks is None:
        return None
    try:
        return ticks // _TICKS_PER_MINUTE
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_seen(event_id, name, start_time, attendee_ids, honored=None, is_holiday=False):
    """Record/update a single event. Keyed by event_id so repeated calls
    for the same event just refresh the snapshot (newer attendee list etc.)"""
    if event_id is None or not name:
        return
    start_ticks = _ticks_of(start_time)
    if start_ticks is None:
        return
    try:
        with _lock:
            cache = _load()
            cache[str(event_id)] = {
                "event_id": str(event_id),
                "name": name,
                "start_ticks": start_ticks,
                "attendees": list(attendee_ids or []),
                "honored": list(honored or []),
                "is_holiday": bool(is_holiday),
                "logged_at": datetime.datetime.now().isoformat(),
            }
            _save(cache)
    except Exception as e:
        _log(f"record_seen failed: {type(e).__name__}: {e}")


def get_recent_for(sim_a_id, sim_b_id, max_days=_RECENT_WINDOW_IN_GAME_DAYS):
    """Return events where (a) start_ticks is in the past, (b) start is
    within max_days in-game days of now, and (c) BOTH sims appear in
    the attendees list. Newest first."""
    if sim_a_id is None or sim_b_id is None:
        return []
    cache = _load()
    if not cache:
        return []
    now_ticks = _now_ticks()
    if now_ticks is None:
        return []
    cutoff_minutes = max_days * 24 * 60
    matches = []
    for entry in cache.values():
        try:
            start_ticks = entry.get("start_ticks")
            if start_ticks is None or start_ticks >= now_ticks:
                continue
            mins_ago = _ticks_to_minutes(now_ticks - start_ticks)
            if mins_ago is None or mins_ago > cutoff_minutes:
                continue
            attendees = entry.get("attendees") or []
            if sim_a_id not in attendees or sim_b_id not in attendees:
                continue
            entry_copy = dict(entry)
            entry_copy["_mins_ago"] = mins_ago
            matches.append(entry_copy)
        except Exception:
            continue
    matches.sort(key=lambda e: e.get("start_ticks", 0), reverse=True)
    return matches


def cleanup_old(max_days=_RETENTION_IN_GAME_DAYS):
    """Drop entries with start_ticks older than max_days in-game days."""
    try:
        with _lock:
            cache = _load()
            if not cache:
                return 0
            now_ticks = _now_ticks()
            if now_ticks is None:
                return 0
            cutoff_minutes = max_days * 24 * 60
            before = len(cache)
            keep = {}
            for key, entry in cache.items():
                try:
                    start_ticks = entry.get("start_ticks")
                    if start_ticks is None:
                        keep[key] = entry
                        continue
                    if start_ticks >= now_ticks:
                        keep[key] = entry
                        continue
                    mins_ago = _ticks_to_minutes(now_ticks - start_ticks)
                    if mins_ago is None or mins_ago <= cutoff_minutes:
                        keep[key] = entry
                except Exception:
                    keep[key] = entry
            dropped = before - len(keep)
            if dropped:
                _save(keep)
                _log(f"cleanup_old: dropped {dropped}, kept {len(keep)}")
            return dropped
    except Exception as e:
        _log(f"cleanup_old failed: {type(e).__name__}: {e}")
        return 0


# ---------------------------------------------------------------------------
# Prompt-helper
# ---------------------------------------------------------------------------

import re as _re_prompt


def _prettify_event_name(raw):
    """Clean a stored event name for prompt display. Drama-node names
    come out of _resolve_event_name already-prose ("Grandpa's Funeral")
    and pass through unchanged. Situation class names come raw as
    e.g. "Situation_HouseParty" or "Situation_DinnerParty_Formal" --
    strip the prefix and split CamelCase into spaced words."""
    if not raw:
        return "Event"
    # Situation class names: strip common prefixes, split CamelCase.
    stripped = raw
    for prefix in ("Situation_", "situation_"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    else:
        # Already prose-looking (has spaces or lowercase letters) --
        # return as-is.
        if " " in raw or raw[0:1].islower():
            return raw
    # Split CamelCase (insert space before each interior uppercase),
    # replace underscores with spaces, collapse multi-space, strip.
    spaced = _re_prompt.sub(r"(?<!^)(?=[A-Z])", " ", stripped).replace("_", " ")
    spaced = _re_prompt.sub(r"\s+", " ", spaced).strip()
    return spaced or "Event"


def format_for_prompt(sim_a_id, sim_b_id):
    """Return a multi-line block listing recent events both sims attended,
    or empty string if none. Used by the phone prompt builders."""
    events = get_recent_for(sim_a_id, sim_b_id)
    if not events:
        return ""
    lines = ["Recent events you both attended:"]
    for e in events[:4]:  # cap at 4 most-recent so the prompt doesn't bloat
        name = _prettify_event_name(e.get("name") or "")
        mins_ago = e.get("_mins_ago", 0)
        days_ago = mins_ago // (24 * 60)
        if days_ago == 0:
            when = "today"
        elif days_ago == 1:
            when = "yesterday"
        else:
            when = f"{days_ago} sim days ago"
        honored = e.get("honored") or []
        honor_str = f" (in memory of {', '.join(honored)})" if honored else ""
        lines.append(f"  - {name}{honor_str} -- {when}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hook installation
# ---------------------------------------------------------------------------

def _record_from_drama_node(node, phase="complete"):
    """Pull metadata off a completed/cleaned-up drama node and write it
    to disk. Called from BOTH the on_complete and on_cleanup callbacks
    so we catch events that end early too."""
    cls_name = type(node).__name__
    try:
        from . import events as _events
    except Exception as e:
        _log(f"events module import failed on {cls_name}: {type(e).__name__}: {e}")
        return
    # Silent skips on the common "not worth recording" cases -- Sims 4
    # fires complete/cleanup on many internal drama nodes (odd jobs,
    # neighbor dialogs, walkbys) that either have no uid, no start
    # time, or no attendees. Only real events with all fields get
    # logged (as RECORDED) or reported (as errors).
    try:
        event_id = getattr(node, "uid", None)
        if not event_id:
            return
        try:
            start = node.get_calendar_start_time()
        except Exception:
            return
        if start is None:
            return
        name = _events._resolve_event_name(node)
        if not name:
            return
        attendees = set()
        try:
            sims = node.get_calendar_sims() or ()
            attendees = {_events._sim_id(si) for si in sims if si is not None}
        except Exception:
            pass
        if not attendees:
            return
        honored = []
        try:
            honored = _events._get_honored_sims(node, event_name_for_log=name)
        except Exception:
            pass
        record_seen(
            event_id=event_id,
            name=name,
            start_time=start,
            attendee_ids=list(attendees),
            honored=honored,
            is_holiday=False,
        )
        _log(f"RECORDED drama event: {name!r} attendees={len(attendees)}")
    except Exception as e:
        _log(f"_record_from_drama_node failed on {cls_name}: {type(e).__name__}: {e}")


def _situation_active_household_check(sim_ids):
    """True if at least one sim_id in `sim_ids` belongs to the currently
    active household. Filters out random NPC-only situations we don't
    care about."""
    if not sim_ids:
        return False
    try:
        import services
        hh = services.active_household()
        if hh is None:
            return False
        hh_ids = {getattr(si, "sim_id", None) for si in hh.sim_info_gen()}
        hh_ids.discard(None)
        for sid in sim_ids:
            if sid in hh_ids:
                return True
    except Exception:
        pass
    return False


def _record_from_situation(sit):
    """Called when Situation.on_remove fires. Player-planned parties end
    the moment the player clicks End Party (or the situation naturally
    finishes), well before the CalendarEventDramaNode.cleanup fires --
    so this catches the "just ended a party" moment for immediate
    prompt-time relevance. Filters:
      - at least 2 sim participants (skips solo work/errand situations)
      - at least one participant in the active household (skips NPC-only)
    """
    cls_name = type(sit).__name__
    try:
        # Collect attendees from every attribute that might hold them.
        # Different subclasses populate different ones and by _destroy
        # time some may already be cleared -- union whatever survives.
        sim_ids_attr = ()
        guest_ids_attr = ()
        sims_attr_ids = ()
        host_id = None
        try:
            sim_ids_attr = tuple(getattr(sit, "sim_ids", ()) or ())
        except Exception as e:
            _log(f"sit.sim_ids read failed on {cls_name}: {type(e).__name__}: {e}")
        try:
            guest_ids_attr = tuple(getattr(sit, "guest_ids", ()) or ())
        except Exception as e:
            _log(f"sit.guest_ids read failed on {cls_name}: {type(e).__name__}: {e}")
        try:
            sims_raw = getattr(sit, "sims", None)
            if sims_raw is not None:
                # sims can be Sim objects or SimInfos; grab whatever id
                # attribute they expose.
                collected = []
                for s in sims_raw:
                    sid = getattr(s, "sim_id", None) or getattr(s, "id", None)
                    if sid:
                        collected.append(sid)
                sims_attr_ids = tuple(collected)
        except Exception as e:
            _log(f"sit.sims read failed on {cls_name}: {type(e).__name__}: {e}")
        try:
            host_info = getattr(sit, "host_sim_info", None)
            if host_info is not None:
                host_id = getattr(host_info, "sim_id", None)
        except Exception:
            pass

        # Accumulated attendees from our SituationManager.remove_sim_from_
        # situation hook -- this is the RELIABLE source. Every sim who
        # ever left the situation gets stashed here as they're evicted.
        # For a party that ended, this equals the guest list.
        try:
            llf_attendees = set(getattr(sit, "_llamafone_attendees", None) or ())
        except Exception:
            llf_attendees = set()

        sim_ids = set()
        sim_ids.update(sim_ids_attr)
        sim_ids.update(guest_ids_attr)
        sim_ids.update(sims_attr_ids)
        sim_ids.update(llf_attendees)
        if host_id:
            sim_ids.add(host_id)
        sim_ids.discard(None)
        sim_ids.discard(0)

        in_hh = _situation_active_household_check(sim_ids)

        # Silent on the common "not worth recording" cases -- walkby
        # situations, NPC-only interactions, and solo-sim errands all
        # end every few seconds during normal play.
        if len(sim_ids) < 2:
            return
        if not in_hh:
            return

        # Prefer live sit.id, fall back to the id we stashed during the
        # snapshot pass. Sims 4 clears sit.id during teardown alongside
        # sim_ids -- if we didn't stash during a snapshot (party lasted
        # < 15s and no poll caught it), skip silently.
        sit_id = getattr(sit, "id", None) or getattr(sit, "_llamafone_sit_id", None)
        if not sit_id:
            return

        name = cls_name

        now = None
        try:
            import services
            ts = services.time_service()
            if ts:
                now = ts.sim_now
        except Exception as e:
            _log(f"  time_service read failed on {cls_name}: {type(e).__name__}: {e}")
        if now is None:
            return

        record_seen(
            event_id=sit_id,
            name=name,
            start_time=now,
            attendee_ids=list(sim_ids),
            honored=[],
            is_holiday=False,
        )
        _log(f"RECORDED situation event: cls={cls_name} attendees={len(sim_ids)}")
    except Exception as e:
        _log(f"_record_from_situation failed on {cls_name}: {type(e).__name__}: {e}")


def install_hook():
    """Monkey-patch `BaseDramaNode.complete` and `.cleanup` on the class
    directly. Every drama node instance -- including ones already alive
    when the mod loaded -- goes through the patched methods, because
    method resolution reads the class dict live. Subclasses that
    override cleanup (like CalendarEventDramaNode) still work as long
    as they call super().cleanup(...) at the end, which they do
    (verified against calendar_event_drama_node.pyc on 2026-06-17).

    Why NOT `_callbacks_on_complete` callback registration: that only
    works for drama nodes whose _setup ran AFTER our patch installed.
    An event already in progress when the mod loads has an empty
    callback list -- when it ends, nothing fires. Patching the class
    method itself has no such per-instance state.

    Signatures verified against drama_node.pyc:
      complete(self, from_shutdown)
      cleanup(self, from_service_stop)
    We use *args/**kwargs to absorb both so we don't break if a future
    patch adds params.
    """
    global _hook_installed
    if _hook_installed:
        return True
    try:
        from drama_scheduler.drama_node import BaseDramaNode
    except Exception as e:
        _log(f"install_hook: BaseDramaNode not importable: {type(e).__name__}: {e}")
        return False
    if getattr(BaseDramaNode, "_llamafone_past_events_hooked", False):
        _hook_installed = True
        return True

    original_complete = BaseDramaNode.complete
    original_cleanup = BaseDramaNode.cleanup

    def _patched_complete(self, *args, **kwargs):
        # Record BEFORE calling super so we grab node state before
        # cleanup nulls out sim refs. cleanup zeroes _sender_sim_info /
        # _receiver_sim_info / _selected_time so post-super reads are
        # empty. get_calendar_sims still works if sims_of_interest is
        # populated (calendar events keep that around), but we play it
        # safe and record on entry.
        try:
            _record_from_drama_node(self, phase="complete")
        except Exception as e:
            _log(f"complete-hook handler failed on {type(self).__name__}: {type(e).__name__}: {e}")
        return original_complete(self, *args, **kwargs)

    def _patched_cleanup(self, *args, **kwargs):
        try:
            _record_from_drama_node(self, phase="cleanup")
        except Exception as e:
            _log(f"cleanup-hook handler failed on {type(self).__name__}: {type(e).__name__}: {e}")
        return original_cleanup(self, *args, **kwargs)

    BaseDramaNode.complete = _patched_complete
    BaseDramaNode.cleanup = _patched_cleanup
    BaseDramaNode._llamafone_past_events_hooked = True

    # Also hook Situation._destroy for the "party ended immediately"
    # case. Calendar events end the SITUATION when the player clicks
    # End Party; the associated CalendarEventDramaNode doesn't cleanup
    # until later, so a drama-node-only hook misses the immediate
    # signal.
    #
    # Why _destroy not on_remove: verified in-game that Situation.on_remove
    # fires AFTER sim_ids has been cleared (a party's on_remove logs
    # sim_ids=0 despite N guests). _destroy is called before on_remove
    # and sim_ids is still populated at the START of _destroy. We wrap
    # to capture attendees before calling through.
    #
    # Signature (verified drama_node.pyc + situation.pyc): _destroy(self).
    # Wrapped with *args/**kwargs for patch-safety.
    try:
        from situations.situation import Situation
    except Exception as e:
        _log(f"situation module not importable: {type(e).__name__}: {e}")
    else:
        if not getattr(Situation, "_llamafone_past_events_hooked", False):
            original_destroy = Situation._destroy

            def _patched_destroy(self, *args, **kwargs):
                # Capture BEFORE destroy runs. The reliable attendee
                # source is `self._llamafone_attendees`, populated by the
                # snapshot thread via SituationManager.get_situations_-
                # sim_is_in(). At _destroy time Sims 4 has cleared
                # sim_ids/guest_ids/host and even sit.id, so we rely on
                # our own stashed attributes to survive teardown.
                try:
                    _record_from_situation(self)
                except Exception as e:
                    _log(f"situation-hook handler failed on {type(self).__name__}: {type(e).__name__}: {e}")
                return original_destroy(self, *args, **kwargs)

            Situation._destroy = _patched_destroy
            Situation._llamafone_past_events_hooked = True

    # Instead of trying to catch attendees at situation teardown (Sims 4
    # clears sim_ids / guest_ids / sims / host_sim_info before every
    # hook point we've tried), we POLL live situations from a background
    # thread. Every 15 seconds the thread walks
    #   services.situation_manager()
    # and for each live situation with 2+ sims, caches the sim_ids in
    # `situation._llamafone_attendees`. When _destroy fires later with
    # everything wiped, we read that cached set.
    #
    # Trade-off: parties that start AND end in under one poll cycle
    # (15s) get missed. In practice player-planned events run for
    # sim-minutes to sim-hours, so a single poll always catches them.
    _start_snapshot_thread()

    _hook_installed = True
    _log("installed BaseDramaNode.complete/.cleanup + Situation._destroy hooks + attendee-snapshot thread")
    return True


# ---------------------------------------------------------------------------
# Attendee snapshot thread
# ---------------------------------------------------------------------------

_snapshot_thread_started = False


def _snapshot_active_situations():
    """One pass over the SituationManager, caching sim_ids on every live
    situation. Runs in a daemon thread on a fixed cadence -- see
    _start_snapshot_thread."""
    try:
        import services
    except Exception:
        return
    try:
        sm = services.get_zone_situation_manager() if hasattr(services, "get_zone_situation_manager") else None
        if sm is None:
            # Fall back to whichever accessor exists in this game version.
            for name in ("situation_manager", "get_situation_manager"):
                fn = getattr(services, name, None)
                if callable(fn):
                    sm = fn()
                    if sm is not None:
                        break
        if sm is None:
            return
    except Exception:
        return
    # Inverse query: iterate all sims (via sim_info_manager) and ask
    # SituationManager which situations each sim is in. That's what
    # MCCC does and it works. Situation.sim_ids returns empty for
    # reasons we couldn't figure out, but the sim->situations lookup
    # works. Attribution: sim S is in situation SIT -> add S's id to
    # SIT._llamafone_attendees. Union across polls builds the true
    # attendee history.
    try:
        import services as _svc
    except Exception:
        return
    try:
        sim_info_mgr = _svc.sim_info_manager()
    except Exception as e:
        _log(f"snapshot: sim_info_manager unavailable: {type(e).__name__}: {e}")
        return
    if sim_info_mgr is None:
        return

    scanned_sims = 0
    scanned_pairs = 0
    stashed_pairs = 0
    situation_hits = {}  # sit_id -> (cls_name, count_new)
    try:
        for si in sim_info_mgr.values():
            if si is None:
                continue
            scanned_sims += 1
            sid = getattr(si, "sim_id", None)
            if not sid:
                continue
            # We can pass either the Sim instance or the SimInfo -- MCCC
            # uses whichever it has. Try Sim first (more common in the
            # game's own code paths), fall back to sim_info.
            sim_arg = None
            try:
                sim_arg = si.get_sim_instance() if hasattr(si, "get_sim_instance") else None
            except Exception:
                sim_arg = None
            if sim_arg is None:
                sim_arg = si
            try:
                sits = sm.get_situations_sim_is_in(sim_arg)
            except TypeError:
                # Some game versions want (sim, stage). Try with None.
                try:
                    sits = sm.get_situations_sim_is_in(sim_arg, None)
                except Exception:
                    continue
            except Exception:
                continue
            if not sits:
                continue
            for sit in sits:
                if sit is None:
                    continue
                scanned_pairs += 1
                # Stash the sit id NOW while it's still populated -- at
                # _destroy time Sims 4 clears sit.id along with sim_ids /
                # guest_ids / etc. Our own attribute survives.
                if not getattr(sit, "_llamafone_sit_id", None):
                    real_id = getattr(sit, "id", None)
                    if real_id:
                        sit._llamafone_sit_id = real_id
                existing = getattr(sit, "_llamafone_attendees", None)
                if existing is None:
                    existing = set()
                    sit._llamafone_attendees = existing
                if sid not in existing:
                    existing.add(sid)
                    stashed_pairs += 1
                    sit_id = getattr(sit, "id", None) or getattr(sit, "_llamafone_sit_id", None) or id(sit)
                    cls_name = type(sit).__name__
                    key = f"{cls_name}#{sit_id}"
                    if key not in situation_hits:
                        situation_hits[key] = 1
                    else:
                        situation_hits[key] += 1
    except Exception as e:
        _log(f"snapshot pass failed: {type(e).__name__}: {e}")
        return

    # Only log when we actually attribute new sim<->situation pairs.
    # Silent otherwise -- during normal play the snapshot fires every
    # 15s and would spam the log with zero-attribution lines.
    if stashed_pairs > 0:
        summary = "; ".join(f"{k}+{v}" for k, v in situation_hits.items())
        _log(f"snapshot: {stashed_pairs} new attributions :: {summary}")


def _start_snapshot_thread():
    """Idempotent -- starts a single daemon thread that runs
    _snapshot_active_situations on a loop. Interval is short enough that
    any player-planned event (which runs for sim-minutes+) is caught by
    at least one pass, but long enough to be effectively free CPU."""
    global _snapshot_thread_started
    if _snapshot_thread_started:
        return
    _snapshot_thread_started = True
    import threading as _threading
    import time as _time

    def _loop():
        # First pass after 5s (let the save finish loading), then every
        # 15s. Cheap: iterating a dict of 5-20 situations and reading a
        # frozenset. No I/O on the hot path.
        _time.sleep(5)
        while True:
            try:
                _snapshot_active_situations()
            except Exception as e:
                _log(f"snapshot loop iteration raised: {type(e).__name__}: {e}")
            _time.sleep(15)

    _threading.Thread(target=_loop, daemon=True, name="Llamafone-PastEvents-Snapshot").start()
