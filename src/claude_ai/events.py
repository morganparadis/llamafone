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
    """Best-effort log line into Documents/ClaudeAI_Log.txt with an [events] tag."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "ClaudeAI_Log.txt")
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

        _log(f"holiday_name: hid={hid} fell through; cleaned='{cleaned}', data type={type(data).__name__}")
        return cleaned
    except Exception as e:
        _log(f"holiday_name: unexpected error: {type(e).__name__}: {e}")
        return ""


def _resolve_event_name(event):
    """Best-effort plain-string name for a calendar event."""
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
    if cleaned:
        return cleaned
    # Fallback: class name
    try:
        cls_name = type(event).__name__
        cleaned = _clean_event_name(cls_name)
        if cleaned:
            return cleaned
    except Exception:
        pass
    return "Event"


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
        _log(f"Lookup r={recipient_id} c={contact_id}: empty _event_data_map.")
        return []

    _log(
        f"Lookup r={recipient_id} c={contact_id}: scanning "
        f"{len(data_map)} calendar entries."
    )

    results = []
    try:
        for event_ref in data_map.values():
            try:
                event = event_ref() if callable(event_ref) else event_ref
            except Exception:
                continue
            if event is None:
                _log("  - entry: weakref dead, skipped")
                continue
            cls_name = type(event).__name__
            try:
                start = event.get_calendar_start_time()
            except Exception as e:
                _log(f"  - {cls_name}: get_calendar_start_time failed: {e}")
                continue
            if start is None:
                _log(f"  - {cls_name}: start=None, skipped")
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

            # Diagnostic dump for this entry
            try:
                start_ticks = start.absolute_ticks() if hasattr(start, "absolute_ticks") else "?"
                now_ticks = now.absolute_ticks() if hasattr(now, "absolute_ticks") else "?"
                delta = start - now
                delta_min = (
                    int(delta.in_minutes()) if hasattr(delta, "in_minutes")
                    else int(delta.in_hours() * 60) if hasattr(delta, "in_hours")
                    else "?"
                )
            except Exception:
                start_ticks = now_ticks = delta_min = "?"

            try:
                sims = event.get_calendar_sims() or ()
                attendee_ids = [_sim_id(si) for si in sims if si is not None]
            except Exception as e:
                _log(f"  - {cls_name}: get_calendar_sims failed: {e}")
                attendee_ids = []

            _log(
                f"  - {cls_name}: holiday={is_holiday}, start={start_ticks}, "
                f"now={now_ticks}, delta_min={delta_min}, "
                f"attendees={attendee_ids}, want=[{recipient_id},{contact_id}]"
            )

            # Past events are irrelevant -- once the event has started,
            # both sims should be at the lot together and would not be
            # texting each other across it.
            try:
                if start < now:
                    _log("    -> dropped (past/in-progress)")
                    continue
            except Exception:
                pass

            if not is_holiday:
                attendee_set = set(attendee_ids)
                if recipient_id not in attendee_set or contact_id not in attendee_set:
                    _log("    -> dropped (one or both sims not in attendee list)")
                    continue

            name = _resolve_event_name(event)
            when = _format_time_until(start, now) or "soon"
            _log(f"    -> KEPT as '{name}' ({when})")
            results.append({
                "name": name,
                "when": when,
                "start": start,
                "is_holiday": is_holiday,
            })
    except Exception as e:
        _log(f"Iter error: {type(e).__name__}: {e}")
        return results

    # Sort by soonest first and cap
    try:
        results.sort(key=lambda r: r["start"])
    except Exception:
        pass
    return results[:max_events]


def format_shared_events_for_prompt(recipient_sim_info, contact_sim_info):
    """Build a small prompt block listing upcoming events both sims are
    invited to, or "" if there aren't any. The model can use this to
    naturally reference shared upcoming plans ('see you at the funeral
    later') without inventing events that aren't actually scheduled."""
    events = get_shared_upcoming_events(recipient_sim_info, contact_sim_info)
    if not events:
        return ""
    lines = [
        "Upcoming on the calendar (feel free to reference these naturally; do "
        "not invent events not listed here -- and match the tone hint when "
        "one is given):"
    ]
    for ev in events:
        hint = _tone_hint(ev["name"])
        hint_part = f" {hint}" if hint else ""
        kind = "holiday" if ev.get("is_holiday") else "event you are both attending"
        lines.append(f"  - {ev['name']} ({kind}, {ev['when']}){hint_part}")
    return "\n".join(lines)
