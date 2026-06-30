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


def get_current_save_id():
    """Return a stable per-save-FILE identifier, or None if no save is loaded.

    Uses `save_slot.slot_id` (matches the on-disk filename
    `Slot_<id>.save`). NOT `get_save_slot_proto_guid()` -- testing showed
    that returns the same value for different saves (likely the world guid,
    shared across saves of the same world)."""
    try:
        import services
    except Exception:
        return None
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
    if svc is None:
        return None
    try:
        # `get_save_slot_proto_buff()` returns the save_slot proto directly
        # (decompiled body: `return self._save_game_data_proto.save_slot`),
        # so slot_id lives at the top level -- NOT proto.save_slot.slot_id.
        slot = svc.get_save_slot_proto_buff()
        if slot is not None:
            slot_id = getattr(slot, "slot_id", None)
            # In proto3, an unset int32 reads as 0 rather than None, which
            # would falsely match the main menu / pre-load state. Treat 0
            # as "no save loaded" -- real saves get nonzero slot ids.
            if slot_id:
                return f"Slot_{int(slot_id):08d}"
    except Exception:
        pass
    return None


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
    """Return `<saves>/Llamafone/<Slot_NNNNNNNN>[__name]/`, creating it on
    demand. Returns None when no save is loaded -- callers MUST handle
    None and silently skip writes in that case (no fallback location).

    The folder name leads with the slot id (matches the in-saves filename
    `Slot_NNNNNNNN.save`) and appends the player-facing slot name when
    available so the folder is easy to spot in Explorer."""
    save_id = get_current_save_id()
    if not save_id:
        return None
    slot_name = _sanitize_for_path(_get_current_slot_name())
    folder_name = f"{save_id}__{slot_name}" if slot_name else save_id
    base = os.path.join(_saves_folder(), "Llamafone", folder_name)
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        return None
    return base


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
