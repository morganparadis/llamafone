"""
Auto-events — randomly fires event/story prompts while you play without you having to ask.

Uses a real-time background thread (not Sims game-time) so "every 20 minutes" means
20 real-world minutes of having the game open, regardless of game speed.

The thread checks whether you're actually in an active household before firing anything,
so it won't trigger during loading screens, CAS, or build mode.

Config options (in llamafone.cfg):
  auto_events_enabled        = true / false
  auto_event_interval_minutes = 20        (real-world minutes between checks)
  auto_event_chance           = 40        (percent chance each check fires something)
  auto_event_types            = call,text   (comma-separated: call, text, event, goals, story, drama)
"""

import os
import datetime
import random
import threading
import time

from . import config
from . import event_generator, storyteller, notifications, phone

_thread = None
_running = False
_lock = threading.Lock()


def _log(message):
    """Write to Llamafone_Log.txt so we can diagnose silent failures."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [auto_events] {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def is_enabled():
    # Runtime override (set via the in-game Settings UI) wins over the
    # static config file, so the player can toggle this from the phone
    # without editing llamafone.cfg and reloading the save.
    val = config.get_setting("auto_events_enabled")
    if val is not None:
        return bool(val)
    return config.get_config().getboolean(config._SECTION, "auto_events_enabled", fallback=False)

def get_interval_seconds():
    val = config.get_setting("auto_event_interval_minutes")
    if val is not None:
        try:
            minutes = float(val)
        except Exception:
            minutes = 20.0
    else:
        minutes = config.get_config().getfloat(config._SECTION, "auto_event_interval_minutes", fallback=20.0)
    return max(5.0, minutes) * 60  # minimum 5 minutes

def get_chance():
    val = config.get_setting("auto_event_chance")
    if val is not None:
        try:
            return max(0, min(100, int(val)))
        except Exception:
            pass
    return config.get_config().getint(config._SECTION, "auto_event_chance", fallback=40)

def get_event_types():
    # Default to phone-only -- the mod is built around the phone, so
    # auto-events default to incoming calls and texts. Players who want
    # the full mix (events, goals, stories, drama) can override in cfg.
    raw = config.get_config().get(config._SECTION, "auto_event_types", fallback="call,text")
    return [t.strip().lower() for t in raw.split(",") if t.strip()]

def get_event_weights():
    """
    Read per-type weights from config. Format: event:30, call:40, text:20, goals:10
    Default favors phone-first behavior (call:50, text:50) -- matches
    the mod's positioning as a phone mod, not an AI-everywhere mod.
    """
    raw = config.get_config().get(config._SECTION, "auto_event_weights", fallback="call:50,text:50")
    weights = {}
    if raw.strip():
        for part in raw.split(","):
            part = part.strip()
            if ":" in part:
                name, val = part.split(":", 1)
                try:
                    weights[name.strip().lower()] = int(val.strip())
                except ValueError:
                    pass
    return weights


# ---------------------------------------------------------------------------
# Game state check
# ---------------------------------------------------------------------------

def _is_game_paused():
    """Return True if the game clock is paused."""
    try:
        import services
        from clock import ClockSpeedMode
        clock = services.game_clock_service()
        if clock and clock.clock_speed == ClockSpeedMode.PAUSED:
            return True
    except Exception:
        pass
    return False


def _active_game_reason():
    """Return 'ok' if the game is in a state where we can fire, or a short string
    explaining why not (for diagnostic logging)."""
    try:
        import services
        if services is None:
            return "services-none"
        zone = services.current_zone() if hasattr(services, "current_zone") else None
        if zone is None:
            return "no-zone"
        if services.active_household() is None:
            return "no-active-household"
        if getattr(zone, "is_in_build_buy", False):
            return "build-buy"
        if _is_game_paused():
            return "paused"
        return "ok"
    except Exception as e:
        return f"exception:{type(e).__name__}"


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------

def _pick_and_fire():
    """Choose a random event type using configured weights and fire it."""
    types = get_event_types()
    if not types:
        _log("No event types configured -- nothing to fire.")
        return

    weights_map = get_event_weights()
    if weights_map:
        # Use configured weights (types not in weights_map get weight 0 and are skipped)
        weighted_types = [t for t in types if weights_map.get(t, 0) > 0]
        if not weighted_types:
            weighted_types = types  # fallback if all weights are 0
        w = [weights_map.get(t, 1) for t in weighted_types]
        chosen = random.choices(weighted_types, weights=w, k=1)[0]
    else:
        chosen = random.choice(types)

    _log(f"Firing auto-event: {chosen}")

    def on_result(text, error):
        if error:
            _log(f"{chosen} failed: {error}")
            return
        if not text:
            _log(f"{chosen} returned empty -- no notification shown.")
            return
        label = {
            "event": "Random Event!",
            "goals": "Today's Goals",
            "story": "Story Update",
            "drama": "Household Drama",
        }.get(chosen, "Llamafone")
        notifications.show_result(label, text)

    def phone_done(text, error):
        if error:
            _log(f"{chosen} failed: {error}")
        elif not text:
            _log(f"{chosen} silently produced no message (no recipient or no contact).")

    if chosen == "event":
        event_generator.generate_random_event(callback=on_result)
    elif chosen == "goals":
        event_generator.generate_weekly_goals(callback=on_result)
    elif chosen == "story":
        storyteller.generate_story_update(callback=on_result)
    elif chosen == "drama":
        storyteller.generate_relationship_drama(callback=on_result)
    elif chosen == "call":
        phone.generate_call(callback=phone_done)
    elif chosen == "text":
        phone.generate_text(callback=phone_done)
    else:
        _log(f"Unknown event type: {chosen}")


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def _worker():
    """Sleeps for the configured interval, then maybe fires an event.
    Timer only ticks while the game is actively running (not paused)."""
    interval = get_interval_seconds()
    _log(f"Auto-events worker started. Interval={interval/60:.1f} min, chance={get_chance()}%, types={get_event_types()}, weights={get_event_weights()}")

    # Short initial delay -- just enough to let the game finish booting and the
    # player land on their household. No long stagger; the player wants action.
    time.sleep(10)

    while _running:
        reason = _active_game_reason()
        configured = config.is_configured()
        fired_full_cycle = False
        if reason == "ok" and configured:
            chance = get_chance()
            roll = random.randint(1, 100)
            if roll <= chance:
                try:
                    _pick_and_fire()
                    fired_full_cycle = True
                except Exception as e:
                    _log(f"_pick_and_fire raised: {type(e).__name__}: {e}")
            else:
                _log(f"Skipped this tick (rolled {roll} vs {chance}% chance).")
                fired_full_cycle = True  # the dice are the source of truth -- full wait
        else:
            _log(f"Skipped tick: reason={reason}, configured={configured}. Will retry in 30s.")

        # Pick the wait length: full interval after a real fire/roll, short retry
        # after a game-state miss so a single paused moment doesn't burn 5 minutes.
        elapsed = 0
        wait_target = get_interval_seconds() if fired_full_cycle else 30
        last_pause_log = 0
        pause_log_interval = 60  # log "waiting for unpause" at most once a minute
        while _running and elapsed < wait_target:
            time.sleep(5)
            if not _is_game_paused():
                elapsed += 5
            else:
                # Silent waits look like the thread died. Log periodically
                # so the user can see we're alive but pause-gated.
                last_pause_log += 5
                if last_pause_log >= pause_log_interval:
                    _log(f"Waiting for game to unpause -- {int(wait_target - elapsed)}s of active-play remaining before next tick.")
                    last_pause_log = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start():
    """Start the auto-event background thread if enabled in config."""
    global _thread, _running
    with _lock:
        if not is_enabled():
            return
        if _thread and _thread.is_alive():
            return  # already running
        _running = True
        _thread = threading.Thread(target=_worker, daemon=True, name="Llamafone-AutoEvents")
        _thread.start()


def stop():
    """Stop the auto-event background thread."""
    global _running
    _running = False


def restart():
    """Stop and restart — call this after reloading config."""
    stop()
    time.sleep(0.1)
    start()


def fire_now():
    """Manually trigger the auto-event picker right now. Used by llama.fire_auto."""
    _log("fire_now() called manually.")
    try:
        _pick_and_fire()
        return True
    except Exception as e:
        _log(f"fire_now raised: {type(e).__name__}: {e}")
        return False


def status():
    """Return a status string for llama.status output."""
    if not is_enabled():
        return "Auto-events: OFF  (set auto_events_enabled = true to turn on)"
    active = _thread is not None and _thread.is_alive()
    interval = get_interval_seconds() / 60
    types = ", ".join(get_event_types()) or "none"
    chance = get_chance()
    state = "running" if active else "stopped"
    return (
        f"Auto-events: ON ({state}) — "
        f"every ~{interval:.0f} min, {chance}% chance, types: {types}"
    )
