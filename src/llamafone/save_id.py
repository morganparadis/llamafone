"""
Per-save data location.

Minimal version: provides the save-specific folder where journal,
milestones, and settings live so multiple save files don't share
history. Data lives in the Sims 4 saves folder rather than the Mods
folder -- this is conventional for per-save mod state (WickedWhims
and similar mods do the same).

  get_current_save_id()    -- stable per-save GUID string, or None
                              when no save is loaded (main menu).
  data_dir()               -- the per-save folder, created on demand.
                              <Documents>/Electronic Arts/The Sims 4/saves/Llamafone/save_<guid>/
                              Returns None if no save is loaded yet.
  data_path(filename)      -- full path inside data_dir for a given
                              filename. Returns None if no save loaded.

DELIBERATELY MINIMAL:
- No migration code. Users on previous versions keep their existing
  Llamafone_*.json files in the Mods folder untouched; new per-save
  data lives in a separate location. Manual move is documented for
  users who care about continuity.
- No startup-time operations. Nothing fires at mod load. Helpers run
  only when journal/milestones/settings are actually accessed, which
  only happens after the save is fully loaded.
- No changes to log files, last-prompt dumps, diagnostic dumps, or
  the Mods folder structure. Only journal/milestones/settings paths
  change.
"""

import datetime
import os


def _log(message):
    """Diagnostic to Llamafone_Log.txt -- on by default during the per-save
    feature rollout so we can confirm hooks fire and ids change correctly."""
    try:
        log_path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(log_path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [save_id] {message}\n")
    except Exception:
        pass


_LAST_LOGGED_SLOT_ID_MISS = [None]  # single-slot cache to rate-limit noise


def _slot_id_is_sentinel(slot_id):
    """A slot_id we can't use as a real save folder identifier."""
    return slot_id is None or slot_id <= 0 or slot_id >= 0xffffffff


def _get_current_slot_id_int():
    """Resolve the loaded save's slot id as a Python int, or None when
    no save is loaded.

    Tries multiple accessor paths because a transient state (CAS
    session, main-menu roundtrip, some Lovestruck/Growing Together
    interaction hooks) can leave the persistence service with an
    unset slot proto while the game is otherwise fully in-world.
    Zone-level accessors sometimes still hold the real slot id in
    those windows, so we fall back to them before giving up.
    """
    try:
        import services
    except Exception:
        return None

    # Path 1: persistence service's save_slot proto (the canonical
    # source; works most of the time, misses the transient windows).
    svc = None
    for accessor_name in ("get_persistence_service", "persistence_service"):
        accessor = getattr(services, accessor_name, None)
        if accessor is None:
            continue
        try:
            svc = accessor()
            if svc is not None:
                break
        except Exception:
            continue
    if svc is not None:
        try:
            slot = svc.get_save_slot_proto_buff()
            if slot is not None:
                slot_id_raw = getattr(slot, "slot_id", None)
                try:
                    slot_id = int(slot_id_raw) if slot_id_raw is not None else None
                except (TypeError, ValueError):
                    slot_id = None
                if not _slot_id_is_sentinel(slot_id):
                    return slot_id
                # Sentinel or missing -- fall through to zone.
                if slot_id != _LAST_LOGGED_SLOT_ID_MISS[0]:
                    _log(f"persistence_service returned sentinel/missing slot_id={slot_id_raw!r}; "
                         "trying zone fallback")
                    _LAST_LOGGED_SLOT_ID_MISS[0] = slot_id
        except Exception:
            pass

    # Path 2: current zone's save_slot_id (populated during gameplay
    # even when the persistence service's proto is transiently empty).
    try:
        zone = services.current_zone()
        if zone is not None:
            for attr in ("save_slot_id", "_save_slot_id", "active_household_id"):
                # active_household_id is a last-ditch discriminator we
                # avoid unless nothing else works; households sometimes
                # persist under a slot_id-shaped identifier in older
                # builds, but this is imprecise. Skip it here -- only
                # try the true slot_id attributes.
                if attr == "active_household_id":
                    continue
                try:
                    val = getattr(zone, attr, None)
                except Exception:
                    val = None
                if val is None:
                    continue
                try:
                    slot_id = int(val)
                except (TypeError, ValueError):
                    continue
                if not _slot_id_is_sentinel(slot_id):
                    return slot_id
    except Exception:
        pass

    # Path 3: game_services.current_client / active session slot id.
    # This is the last resort; some builds expose it via different
    # paths that we simply don't know about.
    try:
        cs = getattr(services, "client_manager", None)
        if callable(cs):
            mgr = cs()
            if mgr is not None:
                for client in list(getattr(mgr, "_clients", {}).values()):
                    for attr in ("save_slot_id", "_save_slot_id"):
                        try:
                            val = getattr(client, attr, None)
                        except Exception:
                            val = None
                        if val is None:
                            continue
                        try:
                            slot_id = int(val)
                        except (TypeError, ValueError):
                            continue
                        if not _slot_id_is_sentinel(slot_id):
                            return slot_id
    except Exception:
        pass

    return None


def get_current_save_id():
    """Return a stable per-save identifier matching the on-disk .save
    filename, or None if no save is loaded.

    The persistence service stores slot_id as a decimal int, but Sims 4
    names the actual save files in LOWERCASE HEX (8 zero-padded digits).
    Slot id 4373 decimal -> "Slot_00001115.save" on disk because
    0x1115 == 4373. We format with `:08x` to match the filename exactly.

    Falls back to the last save_id observed at save-load time when the
    live persistence service reports a sentinel value. Some Sims 4
    build/mod combos leave the service with an unset slot_id during
    long stretches of gameplay (CAS sessions, menu roundtrips,
    interaction hooks in packs like Growing Together / Lovestruck).
    Without this fallback every write to per-save files (dating opt-
    ins, journal, past_events, etc.) is dropped for those windows.

    The fallback only applies WHILE a zone is loaded, so a player at
    the main menu doesn't accidentally get their prior save's cached
    id used for phantom writes."""
    slot_id = _get_current_slot_id_int()
    if slot_id is not None:
        return f"Slot_{slot_id:08x}"
    # Fallback: last-known-good id from the save-load hook. Guarded
    # by an active-zone check so writes are only allowed when we're
    # genuinely still in the same session as the cached id.
    if _last_handled_save_id is None:
        return None
    try:
        import services
        if services.current_zone() is None:
            return None
    except Exception:
        return None
    return _last_handled_save_id


def _get_current_slot_name():
    """Best-effort player-facing save name (for human-recognizable folder
    labels). Returns "" when unavailable. Never used for identity -- only
    for the folder suffix so users can spot the right folder in Explorer."""
    try:
        import services
        svc = services.get_persistence_service()
        if svc is None:
            return ""
        slot = svc.get_save_slot_proto_buff()
        if slot is None:
            return ""
        return str(getattr(slot, "slot_name", "") or "")
    except Exception:
        return ""


def _sanitize_for_path(name):
    """Strip filesystem-unfriendly characters from a save name so it can
    safely be part of a folder name on Windows."""
    if not name:
        return ""
    bad = '<>:"/\\|?*\t\r\n'
    cleaned = "".join("_" if c in bad else c for c in name).strip().strip(".")
    return cleaned[:48]


def _saves_folder():
    """Sims 4's saves folder. Stable path on Windows + macOS."""
    return os.path.join(
        os.path.expanduser("~"), "Documents",
        "Electronic Arts", "The Sims 4", "saves",
    )


def data_dir():
    """Return `<saves>/Llamafone/Slot_NNNNNNNN/` (lowercase hex matching
    the on-disk .save filename), creating it on demand. Returns None
    when no save is loaded -- callers MUST handle None and silently
    skip writes in that case (no fallback location).

    One-time migration for v3.1.2/3 -> v3.1.4: earlier versions formatted
    the slot id as DECIMAL (e.g. `Slot_00004373` for what should be
    `Slot_00001115`), and v3.1.3 also appended the player-facing save
    name (e.g. `Slot_00004373__My Saved Game 32 [Recovered]`). When
    data_dir runs and the new hex-format folder doesn't exist yet, we
    look for any of those legacy variants for THIS save's slot id and
    rename it. Preserves history across the upgrade."""
    save_id = get_current_save_id()
    if not save_id:
        return None
    base_root = os.path.join(_saves_folder(), "Llamafone")
    base = os.path.join(base_root, save_id)
    if not os.path.exists(base):
        slot_id_int = _get_current_slot_id_int()
        legacy = _find_legacy_folder(base_root, slot_id_int) if slot_id_int else None
        if legacy is not None:
            try:
                os.rename(legacy, base)
                _log(f"migrated legacy folder {os.path.basename(legacy)!r} -> {save_id!r}")
            except Exception as e:
                _log(f"legacy migration failed: {type(e).__name__}: {e}")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            return None
    return base


def _find_legacy_folder(base_root, slot_id_decimal):
    """Find the v3.1.2/3 folder for this save -- it was named with the
    DECIMAL slot id (`Slot_00004373` for slot_id=4373), optionally with
    a `__<sanitized-slot-name>` suffix added by v3.1.3. Returns the
    path of the first match, or None.

    The legacy decimal-formatted name CANNOT collide with the new
    hex-formatted name for any save because both formats are 8 digits
    and 0-9 only -- a decimal "00004373" is unambiguously NOT a hex
    representation of itself (which would need `4373` hex == 17267
    decimal). So matching by decimal prefix is safe."""
    if not os.path.isdir(base_root):
        return None
    legacy_prefix = f"Slot_{slot_id_decimal:08d}"
    try:
        for entry in os.listdir(base_root):
            full = os.path.join(base_root, entry)
            if not os.path.isdir(full):
                continue
            # Exact match (v3.1.2 naming) or decimal-prefix__name (v3.1.3)
            if entry == legacy_prefix or entry.startswith(legacy_prefix + "__"):
                return full
    except Exception:
        pass
    return None


def data_path(filename):
    """Full path to a per-save data file. Returns None if no save loaded."""
    d = data_dir()
    if d is None:
        return None
    return os.path.join(d, filename)


# ---------------------------------------------------------------------------
# Save-load hook
# ---------------------------------------------------------------------------
#
# When Sims 4 finishes loading a save, the Zone class fires
# `on_loading_screen_animation_finished`. We monkey-patch that method to
# also call our handler, which:
#   1. Materializes the per-save data folder so the user can verify it in
#      Explorer immediately.
#   2. Kicks off a milestone scan against the new save's baseline.
#
# Verified in simulation.zip/zone.pyc on the game version installed at
# 2026-05-14: `Zone.on_loading_screen_animation_finished` is a real
# instance method on the Zone class, called by the engine once per load.
# Patching it on the class itself covers every Zone instance, including
# ones created when the player returns to the main menu and loads a
# different save.

_hook_installed = False
_last_handled_save_id = None


def _on_save_loaded(save_id):
    """Fired once per save-load event, deduplicated by save_id so build-
    mode re-spinups within the same save don't redo the work."""
    global _last_handled_save_id
    if save_id == _last_handled_save_id:
        return
    _last_handled_save_id = save_id
    folder = data_dir()
    _log(f"save loaded: id={save_id!r} folder={folder!r}")
    # Cancel any pending reply-delay Timers from the previous save. A
    # stale Timer firing in the new save's context would write into the
    # wrong conversation. Lazy import keeps save_id importable from
    # anywhere without dragging in phone.
    try:
        from . import phone
        phone._cancel_all_timers()
    except Exception as e:
        _log(f"phone Timer cancel failed: {type(e).__name__}: {e}")
    try:
        from . import milestones
        milestones.start_background_scan()
    except Exception as e:
        _log(f"milestone scan failed: {type(e).__name__}: {e}")


def install_save_load_hook():
    """Monkey-patch `Zone.on_loading_screen_animation_finished` so our
    handler fires after the engine's own logic. Idempotent. Returns True
    if the hook is in place after the call (whether installed by this
    call or a previous one), False if the Zone class isn't importable."""
    global _hook_installed
    if _hook_installed:
        return True
    try:
        import zone
    except Exception as e:
        _log(f"install_save_load_hook: cannot import zone module: {type(e).__name__}: {e}")
        return False
    Zone = getattr(zone, "Zone", None)
    if Zone is None:
        _log("install_save_load_hook: zone module has no Zone class")
        return False
    if getattr(Zone, "_llamafone_save_hook_installed", False):
        _log("install_save_load_hook: already patched by a prior install")
        _hook_installed = True
        return True
    original = Zone.on_loading_screen_animation_finished

    def _patched(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        try:
            sid = get_current_save_id()
            if sid:
                _on_save_loaded(sid)
        except Exception as e:
            _log(f"save-load hook handler raised: {type(e).__name__}: {e}")
        return result

    Zone.on_loading_screen_animation_finished = _patched
    Zone._llamafone_save_hook_installed = True
    _hook_installed = True
    return True
