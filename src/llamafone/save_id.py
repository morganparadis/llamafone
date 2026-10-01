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
    if slot_id is None or slot_id <= 0 or slot_id >= 0xffffffff:
        return True
    try:
        auto = _auto_save_slot_id()  # the Autosave slot is never a real save
        return auto is not None and slot_id == auto
    except Exception:
        return False


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
            # `save_slot_data_id` / `_save_slot_data_id` are the REAL Zone
            # attributes (verified in zone.pyc 2026-09-26); the older
            # `save_slot_id` names never existed, so this fallback had
            # never actually fired.
            for attr in ("save_slot_data_id", "_save_slot_data_id",
                         "save_slot_id", "_save_slot_id", "active_household_id"):
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

    _log_resolution_failure_once()
    return None


_preferred_logged = [None]


def _get_preferred_slot_id_int():
    """The real slot this game belongs to, as recorded by the game inside
    the save (SaveSlotData.preferred_manual_slot_id), or None if unset or
    a sentinel."""
    try:
        import services
        svc = services.get_persistence_service()
        if svc is None:
            return None
        slot = svc.get_save_slot_proto_buff()
        if slot is None:
            return None
        try:
            if not slot.HasField("preferred_manual_slot_id"):
                raw = None
            else:
                raw = slot.preferred_manual_slot_id
        except Exception:
            raw = getattr(slot, "preferred_manual_slot_id", None)
        pid = int(raw) if raw is not None else None
    except Exception as e:
        if _preferred_logged[0] != "err":
            _preferred_logged[0] = "err"
            _log(f"preferred_manual_slot_id unreadable: {type(e).__name__}: {e}")
        return None
    if pid is None or _slot_id_is_sentinel(pid):
        if _preferred_logged[0] != ("none", pid):
            _preferred_logged[0] = ("none", pid)
            _log(f"save has no usable slot record (preferred_manual_slot_id={pid!r})")
        return None
    return pid


_resolution_failure_logged = [False]


def _log_resolution_failure_once():
    """Dump every save-identity value the game exposes, once per session,
    when all resolver paths fail -- so a session running with no save id
    (journal skipping writes, milestones not persisting, birth watcher
    idle) leaves a diagnosis instead of silence. slot_id 0 with a valid
    slot_name typically means the game is running from the scratch /
    autosave slot (player clicked Resume after quitting unsaved)."""
    if _resolution_failure_logged[0]:
        return
    _resolution_failure_logged[0] = True
    info = {}
    try:
        import services
        svc = None
        for accessor_name in ("get_persistence_service", "persistence_service"):
            accessor = getattr(services, accessor_name, None)
            if accessor:
                try:
                    svc = accessor()
                    if svc is not None:
                        break
                except Exception:
                    continue
        if svc is not None:
            try:
                slot = svc.get_save_slot_proto_buff()
                info["proto.slot_id"] = getattr(slot, "slot_id", None)
                info["proto.slot_name"] = getattr(slot, "slot_name", None)
                info["proto.preferred_manual_slot_id"] = getattr(slot, "preferred_manual_slot_id", "<missing>")
                info["proto.preferred_manual_slot_name"] = getattr(slot, "preferred_manual_slot_name", "<missing>")
            except Exception as e:
                info["proto"] = f"{type(e).__name__}: {e}"
            try:
                info["proto_guid"] = svc.get_save_slot_proto_guid()
            except Exception as e:
                info["proto_guid"] = f"{type(e).__name__}: {e}"
            info["auto_save_slot_id"] = getattr(svc, "auto_save_slot_id", None)
        zone = services.current_zone()
        if zone is not None:
            for attr in ("save_slot_data_id", "_save_slot_data_id", "id"):
                info[f"zone.{attr}"] = getattr(zone, attr, "<missing>")
        try:
            hh = services.active_household()
            info["active_household_id"] = getattr(hh, "id", None) if hh is not None else None
        except Exception:
            pass
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    _log(f"live slot id unavailable (normal for Autosave / in-game loads; the save's "
         f"own slot record is checked next). Values seen: {info}")


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
    global _last_real_save_id, _last_real_slot_name, _last_resolution_path
    slot_id = _get_current_slot_id_int()
    if slot_id is not None:
        sid = f"Slot_{slot_id:08x}"
        # Anchor the fallback to THIS real resolution: remember the id
        # and the save's name so a later sentinel window can only reuse
        # it while we're demonstrably still on the same save.
        _last_real_save_id = sid
        _last_real_slot_name = (_get_current_slot_name() or "").strip()
        _last_resolution_path = "live slot id"
        return sid
    # No real slot id: the game was loaded from the Autosave or from the
    # in-game Load menu (both report the scratch slot 0 / 0xffffffff).
    # The game itself records the real slot inside every save it writes:
    # SaveSlotData.preferred_manual_slot_id (field 20). Read from the save
    # files on 2026-09-28: the Autosave (slot_id 0) carried 4373 = 0x1115,
    # the Francesca save it came from; Slot_00000003 carries 3; Autosave
    # backups written from FAT carry 3. A Save As writes the new slot's
    # number, so copies are distinct. We use it, and NEVER guess from
    # household id or save name (copies share both; a wrong save's folder
    # is worse than no data). Saves last written by an older game patch
    # lack the field -> dormant until the player saves once.
    try:
        import services
        if services.current_zone() is None:
            _last_resolution_path = "none (main menu)"
            return None
    except Exception:
        return None
    # Transient-sentinel fallback -- a sentinel window on the SAME save we
    # last resolved for real this session (CAS, travel, household-manage
    # roundtrips). Guarded by name: a different save loaded in-game has a
    # different name and must NOT inherit this anchor.
    cur_name = (_get_current_slot_name() or "").strip()
    pref = _get_preferred_slot_id_int()
    if pref is not None:
        sid = f"Slot_{pref:08x}"
        _last_real_save_id = sid
        _last_real_slot_name = cur_name
        _last_resolution_path = (f"save's own slot record (preferred_manual_slot_id={pref}"
                                 f"{', Autosave' if _current_slot_is_autosave() else ''})")
        return sid
    if _last_real_save_id is not None and _last_real_slot_name and cur_name == _last_real_slot_name:
        _last_resolution_path = "same-save sentinel fallback"
        return _last_real_save_id
    reason = ("Autosave has no slot record" if _current_slot_is_autosave()
              else "in-game load, save has no slot record")
    if _sentinel_refused_logged[0] != cur_name:
        _sentinel_refused_logged[0] = cur_name
        _log(f"save {cur_name!r}: {reason} -- Llamafone DORMANT (no texts, calls, or "
             f"memory) until the game is saved to a real slot or loaded from the "
             f"main-menu Load screen")
    _last_resolution_path = f"DORMANT ({reason}; save {cur_name!r})"
    return None


def dormant_reason():
    """'autosave' / 'unidentified' when a game is loaded but the mod can't
    know its slot; None when resolved or at the main menu."""
    if get_current_save_id() is not None:
        return None
    try:
        import services
        if services.current_zone() is None:
            return None
    except Exception:
        return None
    return "autosave" if _current_slot_is_autosave() else "unidentified"


def _current_household_key():
    """Diagnostic only (logs / llama.saveinfo). NEVER used to pick a folder:
    Save-As copies share a household id with their original."""
    try:
        import services
        hh = services.active_household()
        hid = getattr(hh, "id", None) if hh is not None else None
        return f"household:{hid}" if hid else None
    except Exception:
        return None


def _current_slot_is_autosave():
    try:
        return (_get_current_slot_name() or "").strip().lower() == "autosave"
    except Exception:
        return False


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
        # The RESOLVED slot, not the live one: an Autosave / in-game load
        # has no live slot id but resolves via the save's own slot record,
        # and must still pick up this save's legacy folder instead of
        # creating an empty new one next to it.
        try:
            slot_id_int = int(save_id.split("_", 1)[1], 16)
        except Exception:
            slot_id_int = None
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
# Anchor for the transient-sentinel fallback: the id and save name from
# the last time a REAL slot id resolved. The fallback only reuses the id
# while the current save name still matches -- see get_current_save_id.
_last_real_save_id = None
_last_real_slot_name = None
_sentinel_refused_logged = [None]
_last_resolution_path = "not yet resolved"


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
    # Finish social comment batches / passes a quit interrupted.
    try:
        from . import social
        social.resume_pending()
    except Exception as e:
        _log(f"social resume failed: {type(e).__name__}: {e}")


AUTOSAVE_NOTICE = (
    "Llamafone can't tell which save this Autosave came from: the game didn't "
    "record it (this happens with saves last written by an older game patch). "
    "Texts, calls, and memory are OFF until you save this game to a save slot."
)
UNIDENTIFIED_NOTICE = (
    "Llamafone can't tell which save this is: the game didn't record its save "
    "slot (this happens with saves last written by an older game patch). "
    "Texts, calls, and memory are OFF until you save the game once."
)


def _show_dormant_notice():
    try:
        reason = dormant_reason()
        if reason is None:
            return
        from . import notifications
        if reason == "autosave":
            notifications.show("Llamafone: unknown Autosave", AUTOSAVE_NOTICE)
        else:
            notifications.show("Llamafone: save to enable", UNIDENTIFIED_NOTICE)
    except Exception as e:
        _log(f"dormant notice failed: {type(e).__name__}: {e}")


_RETRY_DELAYS = (5, 10, 20, 30, 60, 60, 120, 120, 300)


def _schedule_save_id_retry(attempt):
    import threading
    if attempt > len(_RETRY_DELAYS):
        _log("save-load hook: gave up resolving save id after retries")
        return
    delay = _RETRY_DELAYS[attempt - 1]

    def _try():
        try:
            import services
            if services.current_zone() is None:
                return  # back at the main menu; the next load re-arms the hook
            sid = get_current_save_id()
            if sid:
                _log(f"save-load hook: save id resolved on retry {attempt}: {sid!r}")
                _on_save_loaded(sid)
            else:
                _schedule_save_id_retry(attempt + 1)
        except Exception as e:
            _log(f"save id retry {attempt} raised: {type(e).__name__}: {e}")

    t = threading.Timer(delay, _try)
    t.daemon = True
    t.start()


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
        global _last_handled_save_id
        result = original(self, *args, **kwargs)
        try:
            sid = get_current_save_id()
            _log(f"zone load finished: save name={(_get_current_slot_name() or '')!r} "
                 f"household={_current_household_key()} -> save id {sid!r} "
                 f"via {_last_resolution_path}")
            if sid:
                _on_save_loaded(sid)
            else:
                # Unresolved at load. Most often: a DIFFERENT save was loaded
                # from the in-game menu (reports slot_id 0) and isn't in the
                # identity map yet. Pending phone timers belong to the
                # previous save -- a Luca reply firing into another save's
                # session -- so cancel them, and clear the dedup id so that
                # switching BACK to the previous save re-runs its load work.
                _last_handled_save_id = None
                try:
                    from . import phone
                    phone._cancel_all_timers()
                except Exception as e:
                    _log(f"phone Timer cancel failed: {type(e).__name__}: {e}")
                _log("save-load hook: save id unresolved at load; pending phone timers "
                     "cancelled; scheduling retries")
                _show_dormant_notice()
                _schedule_save_id_retry(attempt=1)
        except Exception as e:
            _log(f"save-load hook handler raised: {type(e).__name__}: {e}")
        return result

    Zone.on_loading_screen_animation_finished = _patched
    Zone._llamafone_save_hook_installed = True
    _hook_installed = True
    return True


# ---------------------------------------------------------------------------
# Save hook: learn the REAL slot id at the moment a save is committed
# ---------------------------------------------------------------------------
#
# An in-game-loaded save reports slot_id 0 until it's saved, so the only
# dependable moment to learn its true slot is the save itself -- the same
# technique MCCC uses (mc_shared_alarms.inject_save_game_gen). Verified
# 2026-09-28 against services/persistence_service.pyc:
#   PersistenceService.save_game_gen(self, timeline, save_game_data,
#       send_save_message, check_cooldown, ignore_callback)
#   -- a GENERATOR. Callers override_save_slot / save_to_new_slot /
#   save_game_with_autosave (server_commands/persistence_commands.pyc)
#   build save_game_data with the target slot_id; save_to_scratch_slot_gen
#   passes slot_id 0 ('scratch'). The game's own real-slot test is
#   `slot_id > 0 and slot_id != AUTO_SAVE_SLOT_ID`; mirrored below.

_save_hook_installed = False
_auto_save_slot_const = [None, False]  # [value, looked_up]


def _auto_save_slot_id():
    if not _auto_save_slot_const[1]:
        _auto_save_slot_const[1] = True
        try:
            from services import persistence_service as _ps
            _auto_save_slot_const[0] = getattr(_ps, "AUTO_SAVE_SLOT_ID", None)
        except Exception:
            _auto_save_slot_const[0] = None
    return _auto_save_slot_const[0]


def _on_real_save(save_game_data):
    """Record identity -> real slot when a save is committed to a real slot."""
    global _last_real_save_id, _last_real_slot_name, _last_handled_save_id
    raw = getattr(save_game_data, "slot_id", None)
    try:
        slot_id = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        slot_id = None
    auto = _auto_save_slot_id()
    if slot_id is None or _slot_id_is_sentinel(slot_id) or (auto is not None and slot_id == auto):
        _log(f"save hook: save to slot_id={raw!r} (scratch/autosave, AUTO_SAVE_SLOT_ID={auto!r}) "
             f"-- not a real slot commit, ignoring")
        return
    sid = f"Slot_{slot_id:08x}"
    saved_name = getattr(save_game_data, "slot_name", None)
    if not saved_name:
        saved_name = _get_current_slot_name()
    saved_name = str(saved_name or "").strip()
    was = _last_real_save_id
    _log(f"save hook: REAL save committed to {sid} (slot_id={slot_id}, name={saved_name!r}, "
         f"household={_current_household_key()}); previous anchor was {was!r}")
    _last_real_save_id = sid
    _last_real_slot_name = saved_name
    was_dormant = _last_handled_save_id is None
    if _last_handled_save_id != sid:
        # First identification of this save this session (e.g. an
        # in-game-loaded save that was dormant). Don't call _on_save_loaded
        # -- it cancels pending phone timers, and a save is not a save
        # switch. Just mark it handled and establish its milestone baseline.
        _last_handled_save_id = sid
        _log(f"save hook: {sid} is now the active save id -- per-save features live")
        if was_dormant:
            try:
                from . import notifications
                notifications.show("Llamafone is on",
                                   f"Saved to a slot, so Llamafone knows this save now. "
                                   f"Texts, calls, and memory are on.")
            except Exception:
                pass
        try:
            data_dir()
        except Exception:
            pass
        try:
            from . import milestones
            milestones.start_background_scan()
        except Exception as e:
            _log(f"save hook: milestone scan failed: {type(e).__name__}: {e}")


def install_save_hook():
    """Patch PersistenceService.save_game_gen. Idempotent. Returns True if
    the hook is in place, False if the class isn't importable yet."""
    global _save_hook_installed
    if _save_hook_installed:
        return True
    try:
        from services import persistence_service as _ps
        PersistenceService = _ps.PersistenceService
    except Exception as e:
        _log(f"install_save_hook: PersistenceService not importable: {type(e).__name__}: {e}")
        return False
    if getattr(PersistenceService, "_llamafone_save_game_hook", False):
        _save_hook_installed = True
        return True
    original = PersistenceService.save_game_gen

    # MUST stay a generator: callers drive save_game_gen with `yield from`
    # / element wrappers, and MCCC wraps it too. `yield from` passes
    # send/throw through and returns the original's result.
    def _patched(self, timeline, save_game_data, *args, **kwargs):
        try:
            _on_real_save(save_game_data)
        except Exception as e:
            _log(f"save hook: capture raised: {type(e).__name__}: {e}")
        result = yield from original(self, timeline, save_game_data, *args, **kwargs)
        return result

    PersistenceService.save_game_gen = _patched
    PersistenceService._llamafone_save_game_hook = True
    _save_hook_installed = True
    _log(f"install_save_hook: PersistenceService.save_game_gen patched "
         f"(AUTO_SAVE_SLOT_ID={_auto_save_slot_id()!r})")
    return True
