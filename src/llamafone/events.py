"""
Read upcoming household calendar events that two sims are both attending.

The Sims 4 calendar is exposed via `services.calendar_service()`. Each entry
is a drama-node-derived object stored in `_event_data_map`. Useful fields:

  event.uid                      -- unique event id
  event.get_calendar_start_time()  -- TimeStamp (sim time)
  event.get_calendar_end_time()    -- TimeStamp or None
  event.get_calendar_sims()        -- iterable of SimInfo
  event.ui_display_data.name       -- LocalizedString (the player-facing label)

We use that to find events that BOTH the contact and the recipient are
invited to, so phone prompts can include lines like "I'll see you at the
funeral later." We never invent events that aren't actually on the calendar.
"""
import os
import datetime

from . import sim_context


def _log(msg):
    """Best-effort log line into Documents/Llamafone_Log.txt with an [events] tag."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [events] {msg}\n")
    except Exception:
        pass


def _get_calendar_service():
    try:
        import services
        return services.calendar_service()
    except Exception:
        return None


def _get_now():
    try:
        import services
        ts = services.time_service()
        if ts is None:
            return None
        return ts.sim_now
    except Exception:
        return None


def _sim_id(sim_info):
    try:
        return sim_info.id
    except Exception:
        try:
            return sim_info.sim_id
        except Exception:
            return None


# Map of situation-class attribute -> human-readable role label. The
# situation class declares each role as a SITUATION_JOB tunable; player-
# planned events (funerals, weddings) populate those jobs with specific
# sims when the player schedules the event. By checking these specific
# job slots we can tell the prompt who an event is FOR -- whose funeral
# it is, who the couple at the wedding are -- without guessing.
#
# Pack-locked situations (Funeral needs Life & Death, Wedding needs My
# Wedding Stories) won't even exist on installs without those packs, so
# the attribute access just falls through to None and we move on.
_HONORED_JOB_ATTRS = (
    ("departed_job",   "deceased"),     # CustomStateFuneralSituation
    ("betrothed",      "betrothed"),    # WeddingSituation
    ("celebrant",      "celebrant"),    # BirthdayPartySituation (the birthday sim;
                                        # host may be different -- e.g. parent
                                        # hosting kid's party -- so we report both
                                        # separately)
    ("guest_of_honor", "guest_of_honor"),  # LampoonPartySituation, and any other
                                        # "honored guest" event types that follow
                                        # the same convention. No BabyShower
                                        # situation exists in base game -- those
                                        # surface only as the calendar name +
                                        # host.
)


def _get_honored_sims(event, event_name_for_log=""):
    """For events with focal sims (funeral honoree, wedding couple, party
    host), return a list of {'name': str, 'role': str} dicts. For events
    that don't have any focal role -- holidays, world-level events --
    return []. We use this so the prompt can state who the event is for
    or who's running it when we know it, and stay silent (instructing
    the model not to guess) when we don't.

    Focal sims are sourced from the drama node's _situation_seed:
      - honored jobs (Funeral.departed_job, Wedding.betrothed) -> the
        sims the player explicitly picked when scheduling the event
      - guest_list.host_sim_info -> the sim who planned the event

    The host slot is also data-backed (we never guess); we just emit it
    on top of any role-specific honorees, deduped so the same sim isn't
    surfaced under two roles.
    """
    out = []
    # Diagnostic flag: only log when the event name suggests we SHOULD
    # have honored data, to avoid noisy log entries for holidays etc.
    log_misses = any(w in event_name_for_log.lower() for w in ("funeral", "wedding", "memorial"))
    try:
        seed = getattr(event, "_situation_seed", None)
        # situation_type lives on the seed, not the drama node itself
        # (PlayerPlannedDramaNode delegates to its seed for situation
        # info). Fall back to event.situation_type if some other drama
        # node type exposes it directly.
        situation_type = None
        if seed is not None:
            situation_type = getattr(seed, "situation_type", None)
        if situation_type is None:
            situation_type = getattr(event, "situation_type", None)
        if seed is None or situation_type is None:
            if log_misses:
                _log(f"honored[{event_name_for_log}]: no seed/situation_type "
                     f"(seed={seed is not None}, st={situation_type is not None}, "
                     f"event_cls={type(event).__name__})")
            return out
        guest_list = getattr(seed, "guest_list", None)
        if guest_list is None:
            if log_misses:
                _log(f"honored[{event_name_for_log}]: no guest_list on seed "
                     f"(seed_attrs={[a for a in dir(seed) if not a.startswith('_')][:15]})")
            return out
        # Probe which honored-job attributes exist on this situation type
        if log_misses:
            avail = []
            for attr_name, _ in _HONORED_JOB_ATTRS:
                if getattr(situation_type, attr_name, None) is not None:
                    avail.append(attr_name)
            st_name = getattr(situation_type, "__name__", "?")
            all_jobs = [a for a in dir(situation_type) if "job" in a.lower() or "betrothed" in a.lower() or "departed" in a.lower()]
            _log(f"honored[{event_name_for_log}]: st={st_name} "
                 f"matching_honored_attrs={avail} job_like_attrs={all_jobs[:10]}")
        for attr_name, role in _HONORED_JOB_ATTRS:
            job = getattr(situation_type, attr_name, None)
            if job is None:
                continue
            try:
                infos = guest_list.get_guest_infos_for_job(job) or ()
            except Exception as e:
                if log_misses:
                    _log(f"honored[{event_name_for_log}]: get_guest_infos_for_job({attr_name}) "
                         f"failed: {type(e).__name__}: {e}")
                infos = ()
            if log_misses:
                _log(f"honored[{event_name_for_log}]: {attr_name} -> {len(list(infos) if infos else [])} infos")
                infos = guest_list.get_guest_infos_for_job(job) or ()  # re-fetch since we consumed it
            for info in infos:
                si = None
                # SituationGuestInfo exposes either a sim_info handle or
                # a bare sim_id; pre-resolve the id through the sim
                # manager so we always end up with a SimInfo to read
                # first/last name off.
                try:
                    si = getattr(info, "sim_info", None)
                    if si is None:
                        sid = getattr(info, "sim_id", None)
                        if sid:
                            import services
                            sm = services.sim_info_manager()
                            si = sm.get(sid) if sm is not None else None
                except Exception:
                    si = None
                if si is None:
                    if log_misses:
                        _log(f"honored[{event_name_for_log}]: info {info!r} -> no sim_info")
                    continue
                try:
                    name = f"{si.first_name} {si.last_name}".strip()
                except Exception:
                    name = None
                if name:
                    out.append({"name": name, "role": role})

        # Host of the event -- the sim who scheduled it. Useful context
        # for parties/gatherings/dates where there's no specific honoree
        # job but there IS a host. Dedupe against role-specific entries
        # so we don't list the same sim as both deceased and host.
        try:
            host_si = getattr(guest_list, "host_sim_info", None)
            if host_si is not None:
                host_name = f"{host_si.first_name} {host_si.last_name}".strip()
                already_listed = any(h["name"] == host_name for h in out)
                if host_name and not already_listed:
                    out.append({"name": host_name, "role": "host"})
        except Exception as e:
            if log_misses:
                _log(f"honored[{event_name_for_log}]: host lookup failed: "
                     f"{type(e).__name__}: {e}")
    except Exception as e:
        _log(f"_get_honored_sims: {type(e).__name__}: {e}")
    return out


def _clean_event_name(raw):
    """Strip Sims-internal class-name noise from an event label.

    Tuned drama-node subclasses can present as 'playerPlannedDramaNode
    Funeral', 'PlayerPlannedDramaNode_Wedding', or even nested forms
    like 'PlayerPlannedDramaNode_Premadeholiday_Surprise_Pirateday'.
    We want the event-type suffix with underscores turned into spaces
    and consistent title-case. When the whole name IS the drama-node
    wrapper, we strip 'DramaNode' and clean what's left."""
    import re
    if not raw:
        return ""
    # If a "...DramaNode" segment appears followed by the real event
    # type, take everything after it. Handles snake/camel/space joiners.
    m = re.search(r'[Dd]rama\s*[Nn]ode[\s_]+(.+)$', raw)
    candidate = m.group(1) if m else re.sub(r'[\s_]*[Dd]rama\s*[Nn]ode$', '', raw)
    # camelCase -> "camel Case"
    candidate = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', candidate)
    candidate = candidate.replace('_', ' ').strip()
    # Strip any leading "Premadeholiday" / "Customholiday" wrapper that
    # gets baked into tuned-holiday class names ("premadeholiday Surprise
    # Pirateday" -> "Surprise Pirateday") -- the wrapper word is internal.
    candidate = re.sub(r'^(?:Premade|Custom)?holiday\s+', '', candidate, flags=re.IGNORECASE).strip()
    return candidate.title() if candidate else ""


def _resolve_holiday_name(event):
    """For a HolidayDramaNode, prefer the holiday_service display name
    (the player's chosen name for custom holidays, or the localized
    label for built-in ones). Falls back to the holiday data's raw
    `_name` attribute if the localized lookup returns nothing usable.
    Returns "" if everything fails. Logs each step for diagnostics."""
    try:
        import services
        hs = services.holiday_service()
        if hs is None:
            _log("holiday_name: holiday_service is None")
            return ""
        hid = getattr(event, "holiday_id", None)
        if hid is None:
            _log("holiday_name: event.holiday_id is None")
            return ""

        # Try the service's display_name (LocalizedString)
        try:
            loc = hs.get_holiday_display_name(hid)
        except Exception as e:
            _log(f"holiday_name: get_holiday_display_name({hid}) raised: {e}")
            loc = None
        resolved = None
        if loc is not None:
            try:
                resolved = sim_context._resolve_localized_string(loc)
            except Exception as e:
                _log(f"holiday_name: resolve_localized_string failed: {e}")
        cleaned = _clean_event_name(resolved or "")
        if cleaned and cleaned.lower() not in ("custom holiday", "holiday"):
            _log(f"holiday_name: hid={hid} -> '{cleaned}' via display_name")
            return cleaned

        # Fallback: dig into the holiday data object directly.
        try:
            data = hs._get_holiday_data(hid)
        except Exception as e:
            _log(f"holiday_name: _get_holiday_data({hid}) raised: {e}")
            data = None
        if data is None:
            _log(f"holiday_name: no holiday data for hid={hid}")
            return cleaned  # whatever the localized lookup gave us, even if generic

        # CustomHoliday stores the player's raw text in ._name
        raw = getattr(data, "_name", None)
        if raw:
            try:
                raw_resolved = sim_context._resolve_localized_string(raw)
            except Exception:
                raw_resolved = raw if isinstance(raw, str) else None
            fallback_clean = _clean_event_name(raw_resolved or "") if raw_resolved else ""
            if fallback_clean:
                _log(f"holiday_name: hid={hid} -> '{fallback_clean}' via data._name")
                return fallback_clean

        # Built-in Sims 4 holidays (Love Day, Winterfest, Harvestfest,
        # etc) come back as HashedTunedInstanceMetaclass -- literally the
        # Holiday class itself, not a data instance. Two class-level
        # sources for the name:
        #   1. `data.display_name` -- TunableLocalizedStringFactory; if
        #      resolvable it's the localized human-readable name.
        #   2. `data.__name__` -- the raw class name (e.g. "Holiday_LoveDay"),
        #      cleaned into spaced English. Ugly but never wrong.
        try:
            disp = getattr(data, "display_name", None)
            if disp is not None:
                # Some are factories (call to get a LocalizedString), some
                # are LocalizedStrings directly. Try both.
                loc_val = None
                try:
                    if callable(disp):
                        loc_val = disp()
                    else:
                        loc_val = disp
                except Exception:
                    loc_val = disp
                if loc_val is not None:
                    try:
                        resolved_disp = sim_context._resolve_localized_string(loc_val)
                    except Exception:
                        resolved_disp = None
                    disp_clean = _clean_event_name(resolved_disp or "") if resolved_disp else ""
                    if disp_clean and disp_clean.lower() not in ("custom holiday", "holiday"):
                        _log(f"holiday_name: hid={hid} -> '{disp_clean}' via class.display_name")
                        return disp_clean
        except Exception as e:
            _log(f"holiday_name: hid={hid} class.display_name lookup raised: {type(e).__name__}: {e}")

        try:
            cls_name = getattr(data, "__name__", "") or ""
            if cls_name:
                # Strip common prefixes -- game classes are like
                # "Holiday_LoveDay" or "Premade_Holiday_Surprise_PirateDay".
                stripped = cls_name
                for prefix in ("Premade_Holiday_", "Holiday_", "PremadeHoliday_"):
                    if stripped.startswith(prefix):
                        stripped = stripped[len(prefix):]
                        break
                cleaned_cls = _clean_event_name(stripped)
                if cleaned_cls and cleaned_cls.lower() not in ("custom holiday", "holiday"):
                    _log(f"holiday_name: hid={hid} -> '{cleaned_cls}' via class.__name__ ({cls_name})")
                    return cleaned_cls
        except Exception as e:
            _log(f"holiday_name: hid={hid} class.__name__ fallback raised: {type(e).__name__}: {e}")

        _log(f"holiday_name: hid={hid} fell through; cleaned='{cleaned}', data type={type(data).__name__}")
        return cleaned
    except Exception as e:
        _log(f"holiday_name: unexpected error: {type(e).__name__}: {e}")
        return ""


# Names that come from class-name fallback and aren't meaningful enough
# to put in the prompt -- if the resolution path ends here we'd rather
# skip the event than surface a generic, possibly confusing label.
_GENERIC_FALLBACK_NAMES = {
    "custom holiday",
    "holiday",
    "event",
    "drama node",
    "player planned",
    "npc invite",
}


def _resolve_event_name(event):
    """Best-effort plain-string name for a calendar event.
    Returns "" when only a generic class-name fallback is available --
    callers drop those events rather than surface a useless label."""
    # Holiday-specific path -- query the holiday service for the player-
    # facing name (custom holidays carry the player's chosen label here,
    # which never makes it into ui_display_data.name as a clean string).
    try:
        if any("Holiday" in getattr(b, "__name__", "") for b in type(event).__mro__):
            hname = _resolve_holiday_name(event)
            if hname:
                return hname
    except Exception:
        pass

    raw = None
    try:
        ud = getattr(event, "ui_display_data", None)
        if ud is not None:
            name_attr = getattr(ud, "name", None)
            if name_attr is not None:
                raw = sim_context._resolve_localized_string(name_attr)
    except Exception:
        pass
    cleaned = _clean_event_name(raw or "")
    if cleaned and cleaned.lower() not in _GENERIC_FALLBACK_NAMES:
        return cleaned
    # Fallback: class name -- only return it if it's not generic.
    try:
        cls_name = type(event).__name__
        cleaned = _clean_event_name(cls_name)
        if cleaned and cleaned.lower() not in _GENERIC_FALLBACK_NAMES:
            return cleaned
    except Exception:
        pass
    return ""


# Tone hints for in-game events where the wrong register would be jarring
# (funeral texted casually, wedding texted flatly). Keyed by case-insensitive
# substring of the resolved event name; first match wins. Only events that
# actually exist in The Sims 4 are listed.
#
# Planned events (sim-host events on the calendar):
#   Funeral (Life & Death) -- solemn
#   Wedding / Wedding Ceremony / Wedding Reception (My Wedding Stories) -- celebratory
#   Bachelor / Bachelorette Party (My Wedding Stories) -- celebratory
#   Birthday Party (base) -- celebratory
#   Anniversary Party (base) -- warm celebratory
#   Baby Shower (Growing Together) -- warm celebratory
#   Graduation (Discover University / High School Years) -- celebratory
#   House Warming (For Rent) -- warm casual
#   Family Dinner / Family Brunch (Growing Together) -- warm, family-focused
#   Sleepover (For Rent / Growing Together) -- casual, fun
#
# Holidays (Seasons pack + custom):
#   Love Day -- romantic
#   Winterfest -- warm, family, gift-giving
#   Harvestfest -- cozy, family, food
#   New Year's Day -- hopeful, looking-forward
#   Spooky Day -- playful, spooky-fun
#   Father's Day / Mother's Day -- sentimental, family
_EVENT_TONE_HINTS = {
    # Planned events
    "funeral":           "(solemn tone -- grieving occasion, not a casual hangout)",
    "wedding":           "(warm, celebratory tone)",
    "bachelor":          "(rowdy, celebratory tone)",
    "bachelorette":      "(rowdy, celebratory tone)",
    "birthday":          "(celebratory tone)",
    "anniversary":       "(warm, celebratory tone)",
    "baby shower":       "(warm, celebratory tone)",
    "graduation":        "(celebratory, proud tone)",
    "house warming":     "(warm, casual celebratory tone)",
    "housewarming":      "(warm, casual celebratory tone)",
    "family dinner":     "(warm, family tone)",
    "family brunch":     "(warm, family tone)",
    "sleepover":         "(casual, fun tone)",
    # Holidays
    "love day":          "(romantic tone -- it's Love Day)",
    "winterfest":        "(warm, family-and-gifts tone -- it's Winterfest)",
    "harvestfest":       "(cozy, family-and-food tone -- it's Harvestfest)",
    "new year":          "(hopeful, looking-forward tone -- it's New Year's)",
    "spooky day":        "(playful, spooky-fun tone -- it's Spooky Day)",
    "father's day":      "(sentimental, family tone)",
    "mother's day":      "(sentimental, family tone)",
}


def _tone_hint(event_name):
    """Return a tone hint string for an event, or '' if none applies."""
    if not event_name:
        return ""
    lower = event_name.lower()
    for keyword, hint in _EVENT_TONE_HINTS.items():
        if keyword in lower:
            return hint
    return ""


# Sims 4 SeasonType enum order (from seasons.seasons_enums.SeasonType):
# SUMMER=0, FALL=1, WINTER=2, SPRING=3. We carry an index->name table so
# we don't have to import the enum (which may not be available pre-Seasons).
_SEASON_NAMES = ["Summer", "Fall", "Winter", "Spring"]


def _season_for_time(target_time):
    """Return the season-name that `target_time` falls in, or None if the
    season service isn't available (Seasons pack not installed, or save
    not loaded yet). Uses current-season + elapsed-season math so we
    don't have to know future season boundaries directly.

    Sims 4 default = 1 sim week per season (7 sim days); the player can
    set 7/14/21/28-day seasons in gameplay options. We read whatever the
    current season service has tuned and roll forward."""
    try:
        import services
        ss = services.season_service()
        if ss is None:
            return None
        current_season = getattr(ss, "season", None) or getattr(ss, "_season", None)
        season_content = getattr(ss, "_season_content", None)
        season_span = getattr(ss, "_season_length_span", None)
        if current_season is None or season_content is None or season_span is None:
            return None
        season_start = getattr(season_content, "start_time", None)
        if season_start is None:
            return None
        delta = target_time - season_start
        delta_ticks = delta.in_ticks() if hasattr(delta, "in_ticks") else None
        span_ticks = season_span.in_ticks() if hasattr(season_span, "in_ticks") else None
        if not delta_ticks or not span_ticks:
            return None
        seasons_advanced = int(delta_ticks // span_ticks)
        current_idx = int(getattr(current_season, "value", current_season))
        target_idx = (current_idx + seasons_advanced) % len(_SEASON_NAMES)
        return _SEASON_NAMES[target_idx]
    except Exception:
        return None


def _format_time_until(start_time, now):
    """Return a short human-readable 'in X hours' / 'tomorrow' string.
    Returns None if the time math fails or the event is in the past."""
    try:
        delta = start_time - now
        # date_and_time.TimeSpan -- get total minutes
        if hasattr(delta, "in_minutes"):
            mins = int(delta.in_minutes())
        elif hasattr(delta, "in_hours"):
            mins = int(delta.in_hours() * 60)
        else:
            return None
        if mins <= 0:
            return "happening now"
        if mins < 60:
            return f"in {mins} minutes"
        hours = mins // 60
        if hours < 24:
            return f"in {hours} hours" if hours > 1 else "in about an hour"
        days = hours // 24
        if days == 1:
            return "tomorrow"
        if days < 7:
            return f"in {days} days"
        return f"in about {days // 7} week" + ("s" if days // 7 != 1 else "")
    except Exception:
        return None


def get_shared_upcoming_events(recipient_sim_info, contact_sim_info, max_events=3):
    """Return a list of upcoming calendar events that both sims are
    invited to. Each item is a dict with name + when_string.

    Quietly returns [] if the calendar service isn't ready, either sim
    is missing, or no shared events exist. The caller drops the result
    into the prompt only when non-empty.
    """
    if recipient_sim_info is None or contact_sim_info is None:
        return []
    recipient_id = _sim_id(recipient_sim_info)
    contact_id = _sim_id(contact_sim_info)
    if recipient_id is None or contact_id is None:
        return []

    cal = _get_calendar_service()
    if cal is None:
        return []
    now = _get_now()
    if now is None:
        return []

    data_map = getattr(cal, "_event_data_map", None)
    if not data_map:
        return []

    # Summary counters so we don't spam the log with one entry per drama node.
    counts = {"scanned": 0, "past": 0, "attendee_mismatch": 0, "kept": 0, "errors": 0}
    kept_lines = []  # only KEPT events get a line each

    results = []
    try:
        for event_ref in data_map.values():
            counts["scanned"] += 1
            try:
                event = event_ref() if callable(event_ref) else event_ref
            except Exception:
                counts["errors"] += 1
                continue
            if event is None:
                continue
            try:
                start = event.get_calendar_start_time()
            except Exception:
                counts["errors"] += 1
                continue
            if start is None:
                continue

            # Holidays are world-level and have no invitee list -- both
            # sims experience them by default. Detect by class name so
            # we don't have to import HolidayDramaNode (which may not
            # be available pre-Seasons).
            is_holiday = False
            try:
                for base in type(event).__mro__:
                    if "Holiday" in getattr(base, "__name__", ""):
                        is_holiday = True
                        break
            except Exception:
                pass

            # Past events are irrelevant for non-holidays -- once the
            # event has started, both sims should be at the lot together
            # and would not be texting each other across it.
            #
            # HOLIDAYS are different: they run the whole in-game day,
            # nobody "attends" them, and if today IS the holiday the
            # AI absolutely needs to know. So we keep holidays whose
            # start was in the last 24 in-game hours (~144000 ticks).
            # Non-holidays keep the strict "past = drop" rule.
            _HOLIDAY_ACTIVE_TICKS = 24 * 60 * 100  # 24 hours * 60 mins * 100 ticks/min
            try:
                if start < now:
                    if is_holiday:
                        try:
                            # DateAndTime subtraction returns a TimeSpan;
                            # we want raw absolute-tick delta. Fall back
                            # to str-repr parsing if the subtraction API
                            # differs across game versions.
                            hours_since = None
                            try:
                                diff = now - start
                                for attr in ("in_ticks", "absolute_ticks", "value", "ticks"):
                                    fn = getattr(diff, attr, None)
                                    if callable(fn):
                                        hours_since = int(fn())
                                        break
                                    if fn is not None:
                                        hours_since = int(fn)
                                        break
                            except Exception:
                                hours_since = None
                            if hours_since is None or hours_since > _HOLIDAY_ACTIVE_TICKS:
                                counts["past"] += 1
                                continue
                            # Mark this holiday as currently-active so
                            # the prompt can label it "TODAY".
                            _active_today = True
                        except Exception:
                            counts["past"] += 1
                            continue
                    else:
                        counts["past"] += 1
                        continue
                else:
                    _active_today = False
            except Exception:
                _active_today = False

            if not is_holiday:
                try:
                    sims = event.get_calendar_sims() or ()
                    attendee_set = {_sim_id(si) for si in sims if si is not None}
                except Exception:
                    counts["errors"] += 1
                    continue
                if recipient_id not in attendee_set or contact_id not in attendee_set:
                    counts["attendee_mismatch"] += 1
                    continue

            name = _resolve_event_name(event)
            if not name:
                # No meaningful label -- usually an unconfigured holiday
                # template (HashedTunedInstanceMetaclass data with no
                # player-set _name). Drop rather than surface "Custom
                # Holiday" or similar generic placeholders.
                counts.setdefault("unnamed", 0)
                counts["unnamed"] += 1
                continue
            when = _format_time_until(start, now) or "soon"
            season = _season_for_time(start)
            honored = _get_honored_sims(event, event_name_for_log=name)
            counts["kept"] += 1
            honored_log = ""
            if honored:
                honored_log = " honored=" + ",".join(f"{h['name']}({h['role']})" for h in honored)
            kept_lines.append(
                f"    KEPT: '{name}' ({when}, holiday={is_holiday}, season={season}){honored_log}"
            )
            results.append({
                "name": name,
                "when": when,
                "start": start,
                "is_holiday": is_holiday,
                "season": season,
                "honored": honored,
                "active_today": _active_today,
            })
    except Exception as e:
        _log(f"Iter error: {type(e).__name__}: {e}")
        return results

    _log(
        f"Lookup r={recipient_id} c={contact_id}: "
        f"scanned={counts['scanned']}, past={counts['past']}, "
        f"attendee_mismatch={counts['attendee_mismatch']}, "
        f"unnamed={counts.get('unnamed', 0)}, "
        f"kept={counts['kept']}, errors={counts['errors']}"
    )
    for line in kept_lines:
        _log(line)

    # Sort by soonest first and cap
    try:
        results.sort(key=lambda r: r["start"])
    except Exception:
        pass
    return results[:max_events]


def _format_honored(honored):
    """Render the honored-sim part of an event line. Returns "" when
    nothing is known so the caller can omit the segment entirely; we
    never invent who an event is about.
    """
    if not honored:
        return ""
    # Group by role so two betrothed sims read naturally as "Alex and
    # Bailey are getting married", not as two separate entries.
    by_role = {}
    for h in honored:
        by_role.setdefault(h["role"], []).append(h["name"])

    parts = []
    if "deceased" in by_role:
        names = by_role["deceased"]
        if len(names) == 1:
            parts.append(f"in memory of {names[0]}")
        else:
            parts.append("in memory of " + ", ".join(names))
    if "betrothed" in by_role:
        names = by_role["betrothed"]
        if len(names) == 2:
            parts.append(f"for {names[0]} and {names[1]}")
        elif names:
            parts.append("for " + ", ".join(names))
    if "celebrant" in by_role:
        names = by_role["celebrant"]
        if len(names) == 1:
            parts.append(f"for {names[0]}'s birthday")
        else:
            parts.append("for " + " and ".join(names) + "'s birthday")
    if "guest_of_honor" in by_role:
        names = by_role["guest_of_honor"]
        if len(names) == 1:
            parts.append(f"in honor of {names[0]}")
        else:
            parts.append("in honor of " + ", ".join(names))
    if "host" in by_role:
        names = by_role["host"]
        if len(names) == 1:
            parts.append(f"hosted by {names[0]}")
        else:
            parts.append("hosted by " + " and ".join(names))
    return " — " + "; ".join(parts) if parts else ""


def format_shared_events_for_prompt(recipient_sim_info, contact_sim_info):
    """Build a small prompt block listing upcoming events both sims are
    invited to, or "" if there aren't any. The model can use this to
    naturally reference shared upcoming plans ('see you at the funeral
    later') without inventing events that aren't actually scheduled.

    Each event line includes who it's FOR only when the calendar
    actually records that information (e.g. the player picked the
    deceased sim when planning the funeral). When no honoree is recorded
    we leave it off entirely and the trailing instruction tells the
    model not to guess.
    """
    events = get_shared_upcoming_events(recipient_sim_info, contact_sim_info)
    if not events:
        return ""
    lines = [
        "Upcoming on the calendar (you may reference these naturally; do "
        "NOT invent events not listed here, and do NOT invent details "
        "about an event -- who it's for, who's hosting, what's planned -- "
        "beyond what is explicitly stated below). Match the tone hint "
        "when one is given:"
    ]
    # Sort: today's active holidays first (highest priority), then
    # future events by soonest. Ongoing holidays are the AI's most
    # relevant calendar signal -- "you know it's Talk Like A Pirate
    # Day right now" beats "there's a wedding in 3 weeks."
    events = sorted(events, key=lambda e: (0 if e.get("active_today") else 1, e.get("when") or ""))
    for ev in events:
        hint = _tone_hint(ev["name"])
        hint_part = f" {hint}" if hint else ""
        if ev.get("active_today"):
            kind = "holiday HAPPENING TODAY"
            when_str = "today, currently ongoing"
        else:
            kind = "holiday" if ev.get("is_holiday") else "event you are both attending"
            when_str = ev["when"]
        season = ev.get("season")
        # Include the season the event falls in. In Sims 4 a "week" is
        # one in-game season, so "in 2 weeks" can mean "two seasons from
        # now"; the season label keeps the model's framing accurate.
        season_part = f", {season}" if season else ""
        honored_part = _format_honored(ev.get("honored"))
        lines.append(
            f"  - {ev['name']} ({kind}, {when_str}{season_part}){honored_part}{hint_part}"
        )
    return "\n".join(lines)
