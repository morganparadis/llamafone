"""
Trip memory: vacations and getaways the active household takes, who came
along, where, and when -- so the sims who went remember it together and
everyone else hears about it the way news spreads (see phone._heard_after_days).

Detection polls the game's travel groups (travel_group.TravelGroupManager.
get_travel_group_by_household) every 60 s on a background thread, like the
birth watcher. Polling, not hooking: nothing to get wrong about callback
signatures, and a trip lasts in-game days, so a minute's delay is nothing.

Group kinds come from the travel group's class:
  TravelGroup         -> "vacation" (rented lot in another world, Outdoor Retreat+)
  TravelGroupGetaway  -> "getaway"
  TravelGroupStayover -> guests staying at your house: not a trip, skipped.

Everything is measured on the game clock (sim ticks), never the real-world
calendar. Stored per save in Trips.json:
  {"trips": [{"id", "kind", "world", "lot", "zone_id", "household_id",
              "start_ticks", "planned_end_ticks", "end_ticks" (None while away),
              "members": {sim_id: "First Last", ...}}]}
"""
import datetime
import json
import os
import threading

from . import save_id as _save_id

_FILENAME = "Trips.json"
_POLL_SECONDS = 60
_PROMPT_DAYS = 7.0          # in-game days after getting back that a trip stays in prompts
_TICKS_PER_DAY = 1500 * 60 * 24

_lock = threading.RLock()
_watcher_started = False


def _log(msg):
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [trips] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _path():
    return _save_id.data_path(_FILENAME)


def _load():
    p = _path()
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("trips"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"load failed ({type(e).__name__}: {e}); not overwriting")
        return None
    return {"trips": []}


def _save(data):
    p = _path()
    if not p:
        return False
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception as e:
        _log(f"save failed: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Game reads
# ---------------------------------------------------------------------------

def _now_ticks():
    try:
        from . import journal
        return journal._now_ingame_ticks()
    except Exception:
        return None


def _ticks_of(dt):
    if dt is None:
        return None
    for attr in ("absolute_ticks", "value", "ticks"):
        fn = getattr(dt, attr, None)
        if fn is None:
            continue
        try:
            return int(fn() if callable(fn) else fn)
        except Exception:
            continue
    return None


def _kind_of(group):
    name = type(group).__name__
    if "Stayover" in name:
        return None
    if "Getaway" in name:
        return "getaway"
    return "vacation"


def _lot_name(zone_id):
    try:
        import services
        proto = services.get_persistence_service().get_zone_proto_buff(zone_id)
        name = getattr(proto, "name", "") if proto is not None else ""
        return str(name).strip() or None
    except Exception:
        return None


def _world_name(zone_id):
    try:
        from . import phone
        return phone._world_name_from_zone_id(zone_id) or None
    except Exception:
        return None


def _full_name(si):
    return f"{getattr(si, 'first_name', '')} {getattr(si, 'last_name', '')}".strip() or "Someone"


_MISSING_POLLS_TO_END = 3
_missing_polls = {}   # trip id -> consecutive polls without its group


def _household_at_home(hh):
    """True when the game is on this household's home lot."""
    try:
        import services
        home = getattr(hh, "home_zone_id", None)
        return bool(home) and services.current_zone_id() == home
    except Exception:
        return False


def _active_group():
    """(household, travel group or None) for the active household."""
    import services
    hh = services.active_household()
    if hh is None:
        return None, None
    mgr = services.travel_group_manager()
    if mgr is None:
        return hh, None
    return hh, mgr.get_travel_group_by_household(hh)


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

def poll_once():
    """Open a trip when the active household is in a travel group, add
    anyone who joins, and close it when they're back. Only the active
    household's trips are touched."""
    if _save_id.get_current_save_id() is None:
        return
    try:
        import services
        if services.client_manager() is None:
            return   # game still loading
    except Exception:
        return       # services not up yet during load -- try next poll
    try:
        hh, group = _active_group()
    except Exception as e:
        _log(f"travel group lookup failed: {type(e).__name__}: {e}")
        return
    if hh is None:
        return
    now = _now_ticks()
    if now is None:
        return
    hh_id = str(getattr(hh, "id", ""))
    kind = _kind_of(group) if group is not None else None
    gid = str(getattr(group, "id", "")) if kind else None
    at_home = _household_at_home(hh)

    with _lock:
        data = _load()
        if data is None:
            return
        changed = _merge_duplicates(data)
        # Close this household's open trip once they're really back. A
        # single poll without a group isn't enough: during a loading
        # screen (travelling between lots on the trip, a zone load) the
        # group can be briefly missing. Close when the household is on its
        # home lot, or after _MISSING_POLLS_TO_END polls in a row without
        # the group (e.g. the trip ended while another household was played).
        for t in data["trips"]:
            if t.get("household_id") != hh_id or t.get("end_ticks") is not None or t.get("id") == gid:
                continue
            misses = _missing_polls.get(t.get("id"), 0) + 1
            _missing_polls[t.get("id")] = misses
            if at_home or misses >= _MISSING_POLLS_TO_END:
                t["end_ticks"] = now
                changed = True
                _missing_polls.pop(t.get("id"), None)
                _log(f"trip ended: {t.get('kind')} to {t.get('world') or '?'} ({len(t.get('members') or {})} sims)"
                     f" -- {'back on the home lot' if at_home else f'group gone for {misses} checks'}")
            else:
                _log(f"trip group not found (check {misses}/{_MISSING_POLLS_TO_END}), household not home yet -- keeping trip open")
        if kind:
            _missing_polls.pop(gid, None)
            # A trip closed by mistake whose group is back: reopen it
            # rather than starting a duplicate.
            wrongly_closed = next((t for t in data["trips"] if t.get("id") == gid and t.get("end_ticks") is not None), None)
            if wrongly_closed is not None:
                wrongly_closed["end_ticks"] = None
                changed = True
                _log(f"trip to {wrongly_closed.get('world') or '?'} is still going -- reopened")
            trip = next((t for t in data["trips"] if t.get("id") == gid and t.get("end_ticks") is None), None)
            members = {}
            try:
                for si in group.sim_info_gen():
                    members[str(si.sim_id)] = _full_name(si)
            except Exception:
                pass
            if trip is None:
                zone_id = getattr(group, "zone_id", None)
                trip = {
                    "id": gid,
                    "kind": kind,
                    "world": _world_name(zone_id),
                    "lot": _lot_name(zone_id),
                    "zone_id": zone_id,
                    "household_id": hh_id,
                    "start_ticks": _ticks_of(getattr(group, "create_timestamp", None)) or now,
                    "planned_end_ticks": _ticks_of(getattr(group, "end_timestamp", None)),
                    "end_ticks": None,
                    "members": members,
                }
                data["trips"].append(trip)
                changed = True
                _log(f"trip started: {kind} to {trip['world'] or '?'} ({trip['lot'] or 'lot ?'}) "
                     f"with {', '.join(members.values()) or '?'}")
            else:
                new = {k: v for k, v in members.items() if k not in trip["members"]}
                if new:
                    trip["members"].update(new)   # joiners are kept; nobody is removed
                    changed = True
                    _log(f"trip: {', '.join(new.values())} joined")
                pe = _ticks_of(getattr(group, "end_timestamp", None))
                if pe and pe != trip.get("planned_end_ticks"):
                    trip["planned_end_ticks"] = pe   # vacation extended
                    changed = True
        if changed:
            _save(data)


def _merge_duplicates(data):
    """One record per travel group. An earlier build ended a trip during
    a loading screen and then recorded it again, leaving two records with
    the same game id. Merge them without losing anything: earliest start,
    everyone who was on either, still open if either is open (else the
    later end). Returns True if anything changed."""
    by_id = {}
    merged = []
    changed = False
    for t in data["trips"]:
        key = t.get("id")
        first = by_id.get(key)
        if first is None or key is None:
            by_id[key] = t
            merged.append(t)
            continue
        changed = True
        first["members"] = dict(first.get("members") or {}, **(t.get("members") or {}))
        starts = [x for x in (first.get("start_ticks"), t.get("start_ticks")) if x is not None]
        first["start_ticks"] = min(starts) if starts else None
        if first.get("end_ticks") is None or t.get("end_ticks") is None:
            first["end_ticks"] = None
        else:
            first["end_ticks"] = max(first["end_ticks"], t["end_ticks"])
        for k in ("planned_end_ticks", "world", "lot"):
            if t.get(k) and not first.get(k):
                first[k] = t[k]
    if changed:
        data["trips"] = merged
        _log(f"merged duplicate trip records ({len(merged)} trip(s) now)")
    return changed


def _watch_loop():
    import time
    while True:
        time.sleep(_POLL_SECONDS)
        try:
            poll_once()
        except Exception as e:
            _log(f"poll raised: {type(e).__name__}: {e}")


def start_watcher():
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True
    threading.Thread(target=_watch_loop, daemon=True, name="Llamafone-TripWatch").start()
    _log("trip watcher started")


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------

def _ago(ticks, now):
    try:
        from . import milestones
        return milestones.in_game_when(ticks, now) or "recently"
    except Exception:
        return "recently"


def _place(t):
    world, lot = t.get("world"), t.get("lot")
    if world and lot:
        return f"{world} (staying at {lot})"
    return world or lot or "somewhere away from home"


def _others(t, exclude_ids):
    return [n for sid, n in (t.get("members") or {}).items() if sid not in exclude_ids]


def recent_trips_for(sim_id, now=None):
    """Trips this sim is on now or got back from within _PROMPT_DAYS in-game days."""
    data = _load()
    if not data:
        return []
    now = now if now is not None else _now_ticks()
    if now is None:
        return []
    out = []
    for t in data["trips"]:
        if str(sim_id) not in (t.get("members") or {}):
            continue
        st = t.get("start_ticks")
        if st is None or st > now:
            continue   # rolled-back timeline
        end = t.get("end_ticks")
        if end is not None and (now - end) / _TICKS_PER_DAY > _PROMPT_DAYS:
            continue
        out.append(t)
    return out


def format_for_prompt(contact_si, recipient_si, heard_after_days=None):
    """Trip facts for a call / text between `contact_si` (the AI's voice)
    and household sim `recipient_si`. A contact who went on the trip
    remembers it together; anyone else only knows once word would have
    reached them (heard_after_days in-game days after it started; None =
    only if told)."""
    if contact_si is None or recipient_si is None:
        return ""
    now = _now_ticks()
    if now is None:
        return ""
    cid, rid = str(contact_si.sim_id), str(recipient_si.sim_id)
    cname = getattr(contact_si, "first_name", "They")
    rname = getattr(recipient_si, "first_name", "They")
    lines = []
    for t in recent_trips_for(rid, now):
        noun = "vacation" if t.get("kind") == "vacation" else "getaway"
        ongoing = t.get("end_ticks") is None
        if cid in (t.get("members") or {}):
            others = _others(t, {cid, rid})
            with_txt = f", along with {', '.join(others)}" if others else ""
            if ongoing:
                lines.append(f"- {cname} and {rname} are on a {noun} together right now in {_place(t)}{with_txt}. "
                             f"It started {_ago(t['start_ticks'], now)}.")
            else:
                lines.append(f"- {cname} and {rname} went on a {noun} together to {_place(t)}{with_txt}. "
                             f"They got back {_ago(t['end_ticks'], now)}. {cname} was there and remembers it.")
        else:
            if heard_after_days is None:
                continue
            if (now - t["start_ticks"]) / _TICKS_PER_DAY < heard_after_days:
                continue
            others = _others(t, {rid})
            with_txt = f" with {', '.join(others)}" if others else ""
            if ongoing:
                lines.append(f"- {rname} is away on a {noun} in {_place(t)}{with_txt}. {cname} was NOT on this trip.")
            else:
                lines.append(f"- {rname} went on a {noun} to {_place(t)}{with_txt}, and got back {_ago(t['end_ticks'], now)}. "
                             f"{cname} was NOT on this trip and only heard about it.")
    if not lines:
        return ""
    return ("Recent trips (in-game time; bring up naturally if it fits, don't recite):\n" + "\n".join(lines))


def debug_summary():
    """For llama.trips: every recorded trip, newest first."""
    data = _load()
    if data is None:
        return ["No save loaded."]
    now = _now_ticks()
    if not data["trips"]:
        return ["No trips recorded yet."]
    out = []
    for t in reversed(data["trips"]):
        status = "away now" if t.get("end_ticks") is None else (
            f"back {_ago(t['end_ticks'], now)}" if now else "back")
        out.append(f"{t.get('kind')} to {_place(t)} -- {status} -- "
                   f"{', '.join((t.get('members') or {}).values())}")
    return out
