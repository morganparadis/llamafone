"""
Llamafone for The Sims 4
Brings AI-powered dialogue, storytelling, and dynamic events to your game.

Commands (open cheat console with Ctrl+Shift+C):
  llama.status     -- check setup and see all commands
  llama.dialogue   -- generate sim dialogue
  llama.story      -- household narrative update
  llama.storyline  -- 3-act storyline
  llama.event      -- surprise random event
  llama.challenge  -- gameplay challenge
  llama.call       -- incoming call from a relationship sim
  llama.text       -- text message from a relationship sim
  llama.chat <msg> -- chat about your game
"""

MOD_NAME = "Llamafone"
MOD_VERSION = "3.1.2"

# Captured at module-load time -- the moment Sims 4 imported this build.
# Used in prompts so a llama.dumpprompt definitively shows which load
# generated the prompt (no more "did the new code load" guessing games).
import datetime as _dt
LOAD_TIMESTAMP = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(message):
    """Write to a log file in Documents -- the only reliable way to surface errors."""
    import os, datetime
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def _parse_semver(v):
    """Parse 'v1.2.3' or '1.2.3' into a tuple for comparison. Bad input -> (0,0,0)."""
    try:
        parts = v.strip().lstrip("v").split(".")
        return tuple(int(p) for p in parts[:3])
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _check_for_update():
    """Hit the GitHub Releases API and return the latest version tag string
    (e.g. '1.0.1') if it's newer than MOD_VERSION. Returns None if up-to-date,
    network fails, or anything else goes wrong -- never raises."""
    import subprocess, json, sys
    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        result = subprocess.run(
            [
                "curl", "-s", "-L",
                "-H", "Accept: application/vnd.github+json",
                "-H", "User-Agent: Llamafone-Sims4-Mod",
                "https://api.github.com/repos/morganparadis/llamafone/releases/latest",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            startupinfo=startupinfo,
        )
        if result.returncode != 0:
            _log(f"Update check: curl exited {result.returncode}")
            return None
        data = json.loads(result.stdout)
        tag = data.get("tag_name", "")
        if not tag:
            return None
        latest = _parse_semver(tag)
        current = _parse_semver(MOD_VERSION)
        if latest > current:
            _log(f"Update check: newer version available ({tag} vs current {MOD_VERSION}).")
            return tag.lstrip("v")
        _log(f"Update check: up to date ({MOD_VERSION}).")
        return None
    except Exception as e:
        _log(f"Update check failed: {type(e).__name__}: {e}")
        return None


try:
    _log("Starting mod load...")

    import sims4.commands
    import threading
    from . import commands   # noqa: F401 -- registers all llama.* cheat commands
    from . import auto_events
    from . import save_id

    auto_events.start()  # starts only if auto_events_enabled = true in config

    # Hook Zone.on_loading_screen_animation_finished so the per-save data
    # folder is materialized and a milestone re-scan is kicked off the
    # moment a save finishes loading (initial load OR quit-to-main-menu
    # then load different save). No polling.
    if not save_id.install_save_load_hook():
        _log("save_id.install_save_load_hook: zone class not ready at mod-load; will retry from startup thread")

    # Wire up phone-UI injection BEFORE object tunings finish loading.
    # The companion .package supplies the interaction tunings (which are
    # PieMenuCategory + SuperInteraction in the game's internal type system,
    # but they target the phone wheel UI); this hook appends them to the
    # sim's affordance list at load complete.
    try:
        from . import phone_ui_injection
        phone_ui_injection.register()
    except Exception as _e:
        _log(f"phone_ui_injection.register failed: {type(_e).__name__}: {_e}")

    _log("All modules imported, commands registered.")

    # Console output fires immediately as a fallback
    sims4.commands.output(
        f"[{MOD_NAME}] v{MOD_VERSION} loaded -- type 'llama.status' to get started.",
        None,
    )

    # Deferred in-game notification -- waits for the game client to be ready
    def _deferred_startup_notification():
        import time
        last_error = None
        # Up to ~10 minutes -- the player may sit on the main menu before loading a save
        for attempt in range(300):
            time.sleep(2)
            try:
                # Re-import each iteration. On cold start the Sims 4 runtime can
                # bind `services` to None until the world is loaded; once it's
                # ready, the next import resolves to the real module.
                import services as _services
                if _services is None:
                    continue
                cm_fn = getattr(_services, "client_manager", None)
                if cm_fn is None:
                    continue
                cm = cm_fn()
                if cm is None:
                    continue
                client = cm.get_first_client()
                if not client or not getattr(client, "active_sim", None):
                    continue

                # Belt-and-braces: if the Zone hook wasn't installable at
                # mod-load (rare -- the zone module is usually imported by
                # the engine before mods run), install it now.
                save_id.install_save_load_hook()

                from . import notifications, config, milestones
                _log(f"Game client ready (attempt {attempt + 1}), showing startup notification.")

                # Scan the household + relationship network for life events
                # that happened since last play session. Runs on its own
                # daemon thread; non-blocking.
                milestones.start_background_scan()

                # Check for a newer release on GitHub. Short timeout so the
                # popup doesn't hang waiting on a slow network.
                latest = _check_for_update()
                update_line = ""
                if latest:
                    update_line = (
                        f"\n\n** UPDATE AVAILABLE: v{latest} **\n"
                        f"Download at morganparadis.github.io/llamafone/"
                    )

                if config.is_configured():
                    body = (
                        f"v{MOD_VERSION} ready!\n"
                        f"Model: {config.get_default_model()}\n"
                        f"Type 'llama.status' in the cheat console for all commands."
                        f"{update_line}"
                    )
                    notifications.show(MOD_NAME, body)
                else:
                    body = (
                        f"v{MOD_VERSION} loaded but NOT configured.\n"
                        f"Edit llamafone.cfg and add your API key,\n"
                        f"then type 'llama.reload' in the cheat console."
                        f"{update_line}"
                    )
                    notifications.show(MOD_NAME, body)
                return
            except Exception as inner:
                # Log only when the error message changes -- avoids spamming the
                # log with the same "not ready yet" line every 2 seconds.
                err_sig = f"{type(inner).__name__}: {inner}"
                if err_sig != last_error:
                    _log(f"Startup notification waiting (attempt {attempt + 1}): {err_sig}")
                    last_error = err_sig
        _log("Startup notification gave up after 10 minutes -- no active sim found.")

    threading.Thread(
        target=_deferred_startup_notification,
        daemon=True,
        name="Llamafone-Startup",
    ).start()

except Exception as e:
    _log(f"FAILED TO LOAD: {type(e).__name__}: {e}")
    try:
        import sims4.commands
        sims4.commands.output(f"[{MOD_NAME}] FAILED TO LOAD: {type(e).__name__}: {e}", None)
    except Exception:
        pass
