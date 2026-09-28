"""
Cheat console commands for the Llamafone mod.
Open the cheat console with Ctrl+Shift+C, then type a command.

COMMANDS:
  llama.status                     Check config and list all commands
  llama.dialogue                   Generate dialogue for the active sim
  llama.dialogue_situation <text>  Generate dialogue for a specific situation
  llama.backstory                  Generate a backstory for the active sim
  llama.story                      Narrative update for your household
  llama.storyline                  Generate a 3-act storyline
  llama.storyline_theme <theme>    Generate a storyline with a specific theme
  llama.drama                      Generate relationship drama arc
  llama.event                      Generate a surprise random event
  llama.challenge                  Generate a medium challenge
  llama.challenge_easy             Generate an easy challenge
  llama.challenge_hard             Generate a hard challenge
  llama.goals                      Generate weekly goals for this session
  llama.chat <message>             Chat with the AI about your game
  llama.reload                     Reload config (after editing llamafone.cfg)
  llama.auto_events on|off         Enable/disable random auto-events mid-session
"""

try:
    import sims4.commands
    import sims4.resources
    import services
    from . import config
    from . import sim_context, dialogue, storyteller, event_generator, notifications, api_client, auto_events, journal, phone, contact_prefs

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _require_config(output):
        if not config.is_configured():
            output("[Llamafone] Not configured. Edit llamafone.cfg and add your API key.")
            output("[Llamafone] Then run: llama.reload")
            return False
        return True

    def _on_result(feature_name, output):
        """Returns a callback that displays the result via notifications."""
        def callback(text, error):
            if error:
                notifications.show_error(error, output=output)
            else:
                notifications.show_result(feature_name, text, output=output)
        return callback

    # -------------------------------------------------------------------------
    # Status / config
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.status", command_type=sims4.commands.CommandType.Live)
    def cmd_status(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if config.is_configured():
            output(f"[Llamafone] ✓ Configured")
            output(f"[Llamafone]   Default model : {config.get_default_model()}")
            output(f"[Llamafone]   Fast model    : {config.get_fast_model()}")
            output(f"[Llamafone]   Max tokens    : {config.get_max_tokens()}")
            output(f"[Llamafone]   Language      : {config.get_language()}")
        else:
            output("[Llamafone] ✗ NOT configured — edit llamafone.cfg and add your API key")

        output(f"[Llamafone] {auto_events.status()}")
        output(f"[Llamafone] Journal: {journal.get_entry_count()} entries saved")
        output("")
        output("[Llamafone] Available commands:")
        output("  llama.dialogue             — active sim's dialogue")
        output("  llama.dialogue_situation   — dialogue for a specific situation")
        output("  llama.backstory            — active sim's backstory")
        output("  llama.story                — household narrative update")
        output("  llama.storyline            — 3-act storyline")
        output("  llama.storyline_theme X    — storyline with theme X")
        output("  llama.drama                — relationship drama arc")
        output("  llama.event                — surprise random event")
        output("  llama.challenge            — medium gameplay challenge")
        output("  llama.challenge_easy       — easy challenge")
        output("  llama.challenge_hard       — hard challenge")
        output("  llama.goals                — weekly session goals")
        output("  llama.call                 — incoming call from a random relationship sim")
        output("  llama.text                 — incoming text from a random relationship sim")
        output("  llama.callfrom First Last  — incoming call from a specific sim")
        output("  llama.textfrom First Last  — incoming text from a specific sim")
        output("  llama.sendtext First Last msg — text a specific sim")
        output("  llama.sendcall First Last msg — call a specific sim")
        output("  llama.contact First Last ... — mute/pause/priority a contact")
        output("  llama.reply <message>      — reply to the last call or text")
        output("  llama.testconnection       — diagnose 'Network error' issues (esp. Ollama)")
        output("  llama.chat <message>       — chat about your game")
        output("  llama.journal              — view recent journal entries")
        output("  llama.reload               — reload config file")

    @sims4.commands.Command("llama.reload", command_type=sims4.commands.CommandType.Live)
    def cmd_reload(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        config.reload_config()
        auto_events.restart()  # pick up any changes to auto-event settings
        if config.is_configured():
            output("[Llamafone] Config reloaded. API key found — you're good to go!")
        else:
            output("[Llamafone] Config reloaded. Still no API key found.")
            output("[Llamafone] Make sure llamafone.cfg is in your Mods folder.")

    @sims4.commands.Command("llama.saveinfo", command_type=sims4.commands.CommandType.Live)
    def cmd_saveinfo(_connection=None):
        """Diagnostic dump for per-save data resolution. Shows the current
        slot_id (hex + decimal), the human-readable save name, the
        resolved data folder, and lists every Llamafone save-data folder
        on disk -- so users seeing "the mod forgot my bios" can compare
        the current slot to what's on disk and see whether their data
        is in a different Slot_ folder from a prior play session."""
        import os as _os
        from . import save_id as _sid
        out = sims4.commands.CheatOutput(_connection)
        sid_str = _sid.get_current_save_id() or "(none)"
        path_used = getattr(_sid, "_last_resolution_path", "?")
        slot_int = _sid._get_current_slot_id_int()
        slot_live = str(slot_int) if slot_int else "0 / sentinel (in-game load or Autosave)"
        save_name = _sid._get_current_slot_name() or "(unknown)"
        folder = _sid.data_dir() or "(none -- mod is DORMANT for this save)"
        out(f"[Llamafone] Current save")
        out(f"  resolved id:   {sid_str}")
        out(f"  resolved via:  {path_used}")
        out(f"  live slot_id:  {slot_live}")
        out(f"  save name:     {save_name!r}")
        out(f"  household:     {_sid._current_household_key()} (diagnostic only)")
        out(f"  data folder:   {folder}")
        out(f"  save hook:     {'installed' if getattr(_sid, '_save_hook_installed', False) else 'NOT installed'}")
        reason = _sid.dormant_reason()
        if reason == "autosave":
            out("[Llamafone] " + _sid.AUTOSAVE_NOTICE)
        elif reason == "unidentified":
            out("[Llamafone] " + _sid.UNIDENTIFIED_NOTICE)
        out(f"  save's slot record (preferred_manual_slot_id): {_sid._get_preferred_slot_id_int()}")
        out("")
        out(f"[Llamafone] All Llamafone save-data folders:")
        base_root = _os.path.join(_sid._saves_folder(), "Llamafone")
        if not _os.path.isdir(base_root):
            out("  (none -- no data written yet)")
            return
        try:
            entries = sorted(_os.listdir(base_root))
        except Exception as e:
            out(f"  (couldn't read: {type(e).__name__}: {e})")
            return
        any_slot = False
        for entry in entries:
            full = _os.path.join(base_root, entry)
            if not _os.path.isdir(full):
                continue
            if not entry.startswith("Slot_"):
                continue
            any_slot = True
            tag = "  <-- CURRENT" if entry == sid_str else ""
            out(f"  {entry}{tag}")
        if not any_slot:
            out("  (no Slot_ folders found)")

    @sims4.commands.Command("llama.testprovider", command_type=sims4.commands.CommandType.Live)
    def cmd_test_provider(_connection=None):
        """Fire a tiny round-trip against the configured AI provider so the
        player can verify their key + model name + endpoint are working
        without burning a full prompt's worth of tokens. Reports OK with
        the response text, or the raw error verbatim if it fails."""
        output = sims4.commands.CheatOutput(_connection)
        provider = config.get_provider()
        model = config.get_fast_model()
        output(f"[Llamafone] Testing provider={provider} model={model}...")

        def _done(text, error):
            if error:
                output(f"[Llamafone] FAILED: {error}")
                output("[Llamafone] Check provider, api_key, and default_model/fast_model in llamafone.cfg.")
            else:
                preview = (text or "").strip().replace("\n", " ")[:120]
                output(f"[Llamafone] OK -- response: {preview!r}")

        api_client.call_ai_async(
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
            system="You are a test ping. Reply with exactly one word.",
            use_fast_model=True,
            callback=_done,
        )

    @sims4.commands.Command("llama.testconnection", command_type=sims4.commands.CommandType.Live)
    def cmd_test_connection(_connection=None):
        """Provider-aware connectivity diagnostic. Aimed at non-technical
        users who see 'Network error: curl exited with code' and don't
        know what to do. Walks through each check with pass/fail and
        specific next steps."""
        output = sims4.commands.CheatOutput(_connection)
        provider = config.get_provider()
        default_model = config.get_default_model()
        fast_model = config.get_fast_model()

        output(f"[Llamafone] Provider: {provider}")
        output(f"[Llamafone] Default model: {default_model}")
        output(f"[Llamafone] Fast model:    {fast_model}")
        output("")

        if provider == "ollama":
            endpoint = config.get_ollama_endpoint()
            output(f"[Llamafone] Ollama endpoint: {endpoint}")
            output("[Llamafone] Checking if Ollama is reachable...")
            health = api_client.check_ollama_health(endpoint)
            if not health.get("reachable"):
                output("[Llamafone] X FAILED to reach Ollama.")
                output(f"[Llamafone]   {health.get('error')}")
                output("[Llamafone] Fix that, then run llama.testconnection again.")
                return
            output(f"[Llamafone] OK Reached Ollama at {health['endpoint']}.")
            models = health.get("models") or []
            output(f"[Llamafone] Installed models: {len(models)}")
            for m in models:
                output(f"[Llamafone]   - {m}")
            if not models:
                output("[Llamafone] X No models installed. Open Command Prompt and run:")
                output("[Llamafone]      ollama pull llama3.2:3b")
                output("[Llamafone] That downloads a small model (~2GB) suitable for the mod.")
                return
            # Verify the configured models are among the installed set.
            missing = [m for m in {default_model, fast_model} if m and m not in models]
            if missing:
                output("[Llamafone] X Models in llamafone.cfg are not installed:")
                for m in missing:
                    output(f"[Llamafone]      {m}")
                output("[Llamafone] Either change default_model / fast_model in llamafone.cfg to a")
                output("[Llamafone] model from the installed list above, or run:")
                for m in missing:
                    output(f"[Llamafone]      ollama pull {m}")
                output("[Llamafone] Then run llama.reload.")
                return
            output(f"[Llamafone] OK Both configured models ({fast_model}, {default_model}) are installed.")
            output("[Llamafone] Running a tiny generation to confirm end-to-end...")
        else:
            # Cloud provider -- check that an API key is set.
            if not config.is_configured():
                output(f"[Llamafone] X {provider} provider needs an API key but none is set.")
                output("[Llamafone] Edit llamafone.cfg -- add your key to api_key, then run llama.reload.")
                return
            output(f"[Llamafone] OK API key present.")
            output(f"[Llamafone] Running a tiny generation to verify the key + model + network...")

        def _done(text, error):
            if error:
                output(f"[Llamafone] X FAILED: {error}")
                if provider == "ollama":
                    output("[Llamafone] Ollama was reachable earlier, so this is likely a model-")
                    output("[Llamafone] specific issue (e.g. the model is too large for your RAM).")
                    output("[Llamafone] Try a smaller model like llama3.2:3b or qwen2.5:3b.")
                else:
                    output("[Llamafone] Check provider, api_key, default_model, and fast_model in llamafone.cfg.")
                return
            preview = (text or "").strip().replace("\n", " ")[:120]
            output(f"[Llamafone] OK End-to-end works. Response: {preview!r}")
            output("[Llamafone] You're good to go -- try llama.text or llama.call.")

        api_client.call_ai_async(
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
            system="You are a test ping. Reply with exactly one word.",
            use_fast_model=True,
            callback=_done,
        )

    @sims4.commands.Command("llama.auto_events", command_type=sims4.commands.CommandType.Live)
    def cmd_auto_events(toggle: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if toggle is None:
            output(f"[Llamafone] {auto_events.status()}")
            output("[Llamafone] Usage: llama.auto_events on  OR  llama.auto_events off")
            return
        if toggle.lower() in ("on", "true", "1", "yes"):
            auto_events.stop()
            # Temporarily force-enable regardless of config
            config.get_config().set(config._SECTION, "auto_events_enabled", "true")
            auto_events.start()
            output(f"[Llamafone] Auto-events turned ON for this session.")
            output(f"[Llamafone] {auto_events.status()}")
            output("[Llamafone] To make this permanent, set auto_events_enabled = true in llamafone.cfg")
        elif toggle.lower() in ("off", "false", "0", "no"):
            auto_events.stop()
            output("[Llamafone] Auto-events turned OFF for this session.")

    @sims4.commands.Command("llama.fire_auto", command_type=sims4.commands.CommandType.Live)
    def cmd_fire_auto(_connection=None):
        """Manually trigger the auto-event picker right now -- for diagnosing why nothing's firing."""
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Firing auto-event now... check Llamafone_Log.txt for details.")
        ok = auto_events.fire_now()
        if not ok:
            output("[Llamafone] fire_now() raised an exception -- see log.")

    # -------------------------------------------------------------------------
    # Dating (v3.5)
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.dating_status", command_type=sims4.commands.CommandType.Live)
    def cmd_dating_status(_connection=None):
        """Report the dating layer state: opt-ins, frequency, and how
        many candidates the active sim's world yields under the active
        CAS-preference filter."""
        from . import dating
        output = sims4.commands.CheatOutput(_connection)
        opted_ids = dating.get_opted_in_sim_ids()
        output(f"[Llamafone] opted-in sims: {len(opted_ids)}")
        try:
            weight = config.get_dating_cold_outreach_weight()
        except Exception:
            weight = 0
        output(f"[Llamafone] cold_outreach frequency weight: {weight}")
        output(f"[Llamafone] cold_outreach_enabled: {dating.cold_outreach_enabled()}")
        main = sim_context.get_main_sim_info()
        if main is None:
            output("[Llamafone] No main sim -- enter a household first.")
            return
        output(f"[Llamafone] active sim: {main.first_name} {main.last_name}")
        main_id = getattr(main, "sim_id", None)
        output(f"[Llamafone] active sim opted-in: {dating.is_sim_opted_in(main_id)}")
        output(f"[Llamafone] resolved orientation: {dating.resolve_orientation(main)}")
        candidates = dating.get_candidates_for(main)
        output(f"[Llamafone] unmet candidates: {len(candidates)}")
        # Also report mutual-friend eligibility -- helps testing the
        # "Alice gave me your number" narrative frame vs the "we've
        # never met, saw you on the app" fallback.
        with_mutual = []
        without_mutual = []
        for c in candidates:
            try:
                mutual = dating.find_mutual_friend(main, c.get("sim_info"))
            except Exception:
                mutual = None
            if mutual is not None:
                with_mutual.append((c.get("name", "?"),
                                    getattr(mutual, "first_name", "?")))
            else:
                without_mutual.append(c.get("name", "?"))
        output(f"[Llamafone]   with mutual friend: {len(with_mutual)}")
        for name, mutual in with_mutual[:5]:
            output(f"    - {name} (via {mutual})")
        if len(with_mutual) > 5:
            output(f"    ... and {len(with_mutual) - 5} more")
        output(f"[Llamafone]   solo (no mutual): {len(without_mutual)}")
        for name in without_mutual[:3]:
            output(f"    - {name}")
        if len(without_mutual) > 3:
            output(f"    ... and {len(without_mutual) - 3} more")

    @sims4.commands.Command("llama.dating_outreach", command_type=sims4.commands.CommandType.Live)
    def cmd_dating_outreach(_connection=None):
        """Force-fire one cold-outreach text now (skips the auto_events
        cadence). Useful for smoke-testing the dating layer without
        waiting for a random fire."""
        from . import dating
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if not dating.cold_outreach_enabled():
            output("[Llamafone] Cold outreach is disabled. Opt at least one sim in from "
                   "Llamafone Settings > Dating, and make sure frequency isn't Off.")
            return
        output("[Llamafone] Firing a cold-outreach text now... check the phone for an incoming text.")
        dating.generate_cold_outreach()

    # -------------------------------------------------------------------------
    # Moodlet investigation (debug)
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.dumpbuffs", command_type=sims4.commands.CommandType.Live)
    def cmd_dump_buffs(keyword: str = None, _connection=None):
        """List every loaded buff class whose name contains <keyword> --
        writes results to Documents/Llamafone_BuffList.txt for investigation."""
        from . import moodlets
        output = sims4.commands.CheatOutput(_connection)
        if not keyword:
            output("[Llamafone] Usage: llama.dumpbuffs <keyword>")
            output("[Llamafone] e.g. llama.dumpbuffs cheerful")
            output("[Llamafone] e.g. llama.dumpbuffs feeling")
            return
        count = moodlets.dump_buffs_matching(keyword)
        output(f"[Llamafone] Found {count} buff(s) matching '{keyword}'. See Documents/Llamafone_BuffList.txt")

    @sims4.commands.Command("llama.testmoodlet", command_type=sims4.commands.CommandType.Live)
    def cmd_test_moodlet(mood: str = None, _connection=None):
        """Try to apply a generic moodlet to the active sim for testing.
        Logs the outcome and reports success/failure in the console."""
        from . import moodlets
        output = sims4.commands.CheatOutput(_connection)
        if not mood:
            output("[Llamafone] Usage: llama.testmoodlet <emotion>")
            output("[Llamafone] Supported: happy, sad, angry, confident, playful, flirty")
            return
        active = sim_context.get_active_sim()
        sim_info = active.sim_info if active else None
        if not sim_info:
            output("[Llamafone] No active sim -- enter live mode in a household first.")
            return
        ok = moodlets.apply_mood(sim_info, mood, reason="manual test via llama.testmoodlet")
        if ok:
            output(f"[Llamafone] Applied a '{mood}' moodlet to {sim_info.first_name}. Check their moodlet panel.")
        else:
            output(f"[Llamafone] Could not apply '{mood}' -- see Llamafone_Log.txt.")
            output(f"[Llamafone] Try: llama.dumpbuffs {mood}  -- then llama.dumpbuffs feeling")

    @sims4.commands.Command("llama.scanworlds", command_type=sims4.commands.CommandType.Live)
    def cmd_scan_worlds(_connection=None):
        """Iterate every household in the save, resolve each household's
        home zone to a region name + friendly world, and report mismatches.
        Output goes to Documents/Llamafone_WorldScan.txt -- paste it back
        so unresolved regions can be added to the world-name alias map."""
        import os
        output = sims4.commands.CheatOutput(_connection)
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_WorldScan.txt")
        lines = []
        try:
            import services
            from . import phone as _phone

            hh_mgr = services.household_manager()
            if hh_mgr is None:
                lines.append("ERROR: household_manager() returned None.")
            else:
                regions_seen = {}
                households_total = 0
                households_with_home = 0
                for household in list(hh_mgr.values()):
                    households_total += 1
                    try:
                        home_zone_id = getattr(household, "home_zone_id", None)
                        if not home_zone_id:
                            continue
                        households_with_home += 1
                        try:
                            from world.region import get_region_instance_from_zone_id
                            region = get_region_instance_from_zone_id(home_zone_id)
                            err = None
                        except Exception as e:
                            region = None
                            err = f"{type(e).__name__}: {e}"
                        if region is None:
                            key = f"<none:zone_id={home_zone_id}>"
                            if key not in regions_seen:
                                regions_seen[key] = {
                                    "raw": "<no region>",
                                    "cleaned": "",
                                    "friendly": None,
                                    "zone_ids": set(),
                                    "sample_household": getattr(household, "name", "?"),
                                    "error": err,
                                }
                            regions_seen[key]["zone_ids"].add(home_zone_id)
                            continue
                        raw_name = getattr(region, "__name__", "") or str(region)
                        cleaned = (raw_name
                            .replace("Region_", "")
                            .replace("region_", "")
                            .replace("_", " ")
                            .strip())
                        friendly = _phone._friendly_world_name(cleaned) if cleaned else None
                        if raw_name not in regions_seen:
                            regions_seen[raw_name] = {
                                "raw": raw_name,
                                "cleaned": cleaned,
                                "friendly": friendly,
                                "zone_ids": set(),
                                "sample_household": getattr(household, "name", "?"),
                                "error": None,
                            }
                        regions_seen[raw_name]["zone_ids"].add(home_zone_id)
                    except Exception as inner:
                        lines.append(f"Per-household error: {type(inner).__name__}: {inner}")

                lines.append(f"Scanned {households_total} households "
                             f"({households_with_home} with a home zone).")
                lines.append(f"Distinct regions: {len(regions_seen)}.")
                lines.append("")

                resolved = sorted([r for r in regions_seen.values() if r["friendly"]],
                                  key=lambda r: r["friendly"])
                unresolved = sorted([r for r in regions_seen.values() if not r["friendly"]],
                                    key=lambda r: r["raw"])

                if unresolved:
                    lines.append(f"=== UNRESOLVED ({len(unresolved)}) -- need aliases ===")
                    for r in unresolved:
                        lines.append(
                            f"  raw={r['raw']!r}  cleaned={r['cleaned']!r}  "
                            f"sample_household={r['sample_household']!r}"
                            + (f"  error={r['error']}" if r.get("error") else "")
                        )
                    lines.append("")

                if resolved:
                    lines.append(f"=== RESOLVED ({len(resolved)}) ===")
                    for r in resolved:
                        lines.append(
                            f"  raw={r['raw']!r}  ->  {r['friendly']!r}  "
                            f"(zone_ids={len(r['zone_ids'])})"
                        )
        except Exception as e:
            lines.append(f"FATAL: {type(e).__name__}: {e}")

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            output(f"[Llamafone] World scan written to {path}")
        except Exception as e:
            output(f"[Llamafone] Could not write file: {e}")
            for ln in lines[:40]:
                output(ln)

    @sims4.commands.Command("llama.testweather", command_type=sims4.commands.CommandType.Live)
    def cmd_test_weather(_connection=None):
        """Dump everything we can read from the WeatherService. Use this when
        get_current_weather() returns None despite Seasons being installed --
        the output reveals which attribute names the current patch uses."""
        import os
        output = sims4.commands.CheatOutput(_connection)
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Weather.txt")
        lines = []
        try:
            import services
            ws = services.weather_service()
            lines.append(f"weather_service() -> {ws!r}")
            if ws is None:
                lines.append("Seasons pack is not installed (or weather service not started).")
            else:
                # Top-level attributes worth inspecting
                interesting = [a for a in dir(ws) if not a.startswith("__") and any(
                    keyword in a.lower() for keyword in ("weather", "temp", "info", "effect", "rain", "snow", "forecast", "season")
                )]
                lines.append(f"\nRelevant attributes on weather_service ({len(interesting)}):")
                for attr in sorted(interesting):
                    try:
                        val = getattr(ws, attr)
                        if callable(val):
                            lines.append(f"  {attr}() -- callable")
                        else:
                            preview = repr(val)
                            if len(preview) > 240:
                                preview = preview[:240] + "..."
                            lines.append(f"  {attr} = {preview}")
                    except Exception as e:
                        lines.append(f"  {attr} -- ERROR reading: {e}")

                # Drill into _weather_info if present
                wi = getattr(ws, "_weather_info", None) or getattr(ws, "weather_info", None)
                if wi is not None:
                    lines.append(f"\n_weather_info -> {type(wi).__name__}")
                    wi_attrs = [a for a in dir(wi) if not a.startswith("__")]
                    for attr in sorted(wi_attrs)[:40]:
                        try:
                            val = getattr(wi, attr)
                            if callable(val):
                                continue
                            preview = repr(val)
                            if len(preview) > 200:
                                preview = preview[:200] + "..."
                            lines.append(f"  weather_info.{attr} = {preview}")
                        except Exception as e:
                            lines.append(f"  weather_info.{attr} -- ERROR: {e}")
        except Exception as e:
            lines.append(f"ERROR: {type(e).__name__}: {e}")

        lines.append(f"\nsim_context.get_current_weather() -> {sim_context.get_current_weather()!r}")
        lines.append(f"sim_context.get_current_season() -> {sim_context.get_current_season()!r}")

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            output(f"[Llamafone] Wrote {len(lines)} lines to {path}")
        except Exception as e:
            output(f"[Llamafone] Could not write file: {e}")
            for ln in lines:
                output(ln)

    # -------------------------------------------------------------------------
    # Dialogue
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.dialogue", command_type=sims4.commands.CommandType.Live)
    def cmd_dialogue(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        sim = sim_context.get_active_sim()
        if not sim:
            output("[Llamafone] No active sim found.")
            return
        name = sim.sim_info.first_name
        output(f"[Llamafone] Generating dialogue for {name}…")
        dialogue.generate_sim_dialogue(sim=sim, callback=_on_result(f"{name}'s Dialogue", output))

    @sims4.commands.Command("llama.dialogue_situation", command_type=sims4.commands.CommandType.Live)
    def cmd_dialogue_situation(situation: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if not situation:
            output("[Llamafone] Usage: llama.dialogue_situation <situation description>")
            output("[Llamafone] Example: llama.dialogue_situation just got promoted")
            return
        sim = sim_context.get_active_sim()
        name = sim.sim_info.first_name if sim else "Your Sim"
        output(f"[Llamafone] Generating dialogue for: {situation}…")
        dialogue.generate_sim_dialogue(
            sim=sim,
            situation=situation,
            callback=_on_result(f"{name}'s Dialogue", output),
        )

    @sims4.commands.Command("llama.backstory", command_type=sims4.commands.CommandType.Live)
    def cmd_backstory(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        sim = sim_context.get_active_sim()
        name = sim.sim_info.first_name if sim else "Sim"
        output(f"[Llamafone] Generating backstory for {name}…")
        dialogue.generate_npc_backstory(sim=sim, callback=_on_result(f"{name}'s Backstory", output))

    # -------------------------------------------------------------------------
    # Storytelling
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.story", command_type=sims4.commands.CommandType.Live)
    def cmd_story(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Generating household story update…")
        storyteller.generate_story_update(callback=_on_result("Household Story", output))

    @sims4.commands.Command("llama.storyline", command_type=sims4.commands.CommandType.Live)
    def cmd_storyline(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Generating 3-act storyline…")
        storyteller.generate_storyline(callback=_on_result("Storyline", output))

    @sims4.commands.Command("llama.storyline_theme", command_type=sims4.commands.CommandType.Live)
    def cmd_storyline_theme(theme: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if not theme:
            output("[Llamafone] Usage: llama.storyline_theme <theme>")
            output("[Llamafone] Examples: romance  |  rivalry  |  rags to riches  |  ghost mystery")
            return
        output(f"[Llamafone] Generating storyline with theme: {theme}…")
        storyteller.generate_storyline(theme=theme, callback=_on_result("Storyline", output))

    @sims4.commands.Command("llama.drama", command_type=sims4.commands.CommandType.Live)
    def cmd_drama(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Generating relationship drama arc…")
        storyteller.generate_relationship_drama(callback=_on_result("Relationship Drama", output))

    # -------------------------------------------------------------------------
    # Events & challenges
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.event", command_type=sims4.commands.CommandType.Live)
    def cmd_event(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Rolling a random event…")
        event_generator.generate_random_event(callback=_on_result("Random Event!", output))

    @sims4.commands.Command("llama.challenge", command_type=sims4.commands.CommandType.Live)
    def cmd_challenge(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Generating medium challenge…")
        event_generator.generate_challenge(difficulty="medium", callback=_on_result("Challenge", output))

    @sims4.commands.Command("llama.challenge_easy", command_type=sims4.commands.CommandType.Live)
    def cmd_challenge_easy(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Generating easy challenge…")
        event_generator.generate_challenge(difficulty="easy", callback=_on_result("Easy Challenge", output))

    @sims4.commands.Command("llama.challenge_hard", command_type=sims4.commands.CommandType.Live)
    def cmd_challenge_hard(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Generating hard challenge…")
        event_generator.generate_challenge(difficulty="hard", callback=_on_result("Hard Challenge", output))

    @sims4.commands.Command("llama.goals", command_type=sims4.commands.CommandType.Live)
    def cmd_goals(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Generating weekly goals…")
        event_generator.generate_weekly_goals(callback=_on_result("Weekly Goals", output))

    # -------------------------------------------------------------------------
    # Freeform chat
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.chat", command_type=sims4.commands.CommandType.Live)
    def cmd_chat(message: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if not message:
            output("[Llamafone] Usage: llama.chat <your message>")
            output("[Llamafone] Example: llama.chat what career should my sim pursue?")
            return

        context = sim_context.build_context_string()
        language = config.get_language()

        system = (
            f"You are a helpful, enthusiastic Sims 4 advisor and storyteller. "
            f"The player is asking about their game. Respond in {language}. "
            f"Be helpful, creative, and reference Sims 4 gameplay naturally. "
            f"Keep your response focused and under 300 words."
        )
        prompt = f"Current game state:\n{context}\n\nPlayer: {message}"

        output("[Llamafone] Thinking…")

        def on_chat_result(text, error):
            if text:
                journal.add_entry("chat", f"Q: {message}\nA: {text}")
            _on_result("Llamafone", output)(text, error)

        api_client.call_ai_async(
            [{"role": "user", "content": prompt}],
            system=system,
            callback=on_chat_result,
        )

    # -------------------------------------------------------------------------
    # Phone calls & texts
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.call", command_type=sims4.commands.CommandType.Live)
    def cmd_call(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Incoming call...")
        phone.generate_call(output=output)

    @sims4.commands.Command("llama.text", command_type=sims4.commands.CommandType.Live)
    def cmd_text(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Llamafone] Checking messages...")
        phone.generate_text(output=output)

    def _incoming_from_named(args, output, kind):
        """Shared body for llama.textfrom / llama.callfrom: resolve the
        named contact relative to the active sim and fire an INCOMING
        message from them (the sim writes to the player). Distinct from
        sendtext/sendcall, which are the player writing to the sim."""
        if not _require_config(output):
            return
        if not args:
            output(f"[Llamafone] Usage: llama.{kind}from <First> [Last]")
            return
        two_word = f"{args[0]} {args[1]}" if len(args) > 1 else None
        contact = phone.find_contact_by_name(two_word) if two_word else None
        if not contact:
            contact = phone.find_contact_by_name(args[0])
        if not contact:
            output(f"[Llamafone] Could not find '{two_word or args[0]}' among the active sim's contacts.")
            return
        recipient = sim_context.get_main_sim_info()
        if recipient is None:
            output("[Llamafone] No active sim to receive the message.")
            return
        if (phone._age_rank(contact.get("sim_info")) or 0) < phone._AGE_RANK["CHILD"]:
            output(f"[Llamafone] {contact['name']} is too young to use a phone.")
            return
        output(f"[Llamafone] Incoming {kind} from {contact['name']}...")
        if kind == "text":
            phone.generate_text_for(recipient, contact, output=output)
        else:
            phone.generate_call_for(recipient, contact, output=output)

    @sims4.commands.Command("llama.textfrom", command_type=sims4.commands.CommandType.Live)
    def cmd_textfrom(*args, _connection=None):
        """Incoming text FROM a named sim to the active sim. Note that
        plain llama.text takes no arguments and picks a random sim."""
        _incoming_from_named(args, sims4.commands.CheatOutput(_connection), "text")

    @sims4.commands.Command("llama.callfrom", command_type=sims4.commands.CommandType.Live)
    def cmd_callfrom(*args, _connection=None):
        """Incoming call FROM a named sim to the active sim."""
        _incoming_from_named(args, sims4.commands.CheatOutput(_connection), "call")

    @sims4.commands.Command("llama.sendtext", command_type=sims4.commands.CommandType.Live)
    def cmd_sendtext(*args, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        # Parse: llama.sendtext FirstName LastName your message here
        # Need at least a name and a message
        if len(args) < 2:
            output("[Llamafone] Usage: llama.sendtext <First> <Last> <message>")
            output("[Llamafone] Example: llama.sendtext Bella Goth hey want to hang out?")
            return
        # Try two-word name first, then one-word
        two_word_name = f"{args[0]} {args[1]}"
        contact = phone.find_contact_by_name(two_word_name)
        if contact and len(args) > 2:
            message = " ".join(args[2:])
        else:
            one_word_name = args[0]
            contact = phone.find_contact_by_name(one_word_name)
            if contact:
                message = " ".join(args[1:])
            else:
                output(f"[Llamafone] Could not find '{two_word_name}' or '{one_word_name}'.")
                return
        if not message:
            output("[Llamafone] You need to include a message.")
            return
        output(f"[Llamafone] Texting {contact['name']}...")
        phone.send_text(contact, message, output=output)

    @sims4.commands.Command("llama.sendcall", command_type=sims4.commands.CommandType.Live)
    def cmd_sendcall(*args, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if len(args) < 2:
            output("[Llamafone] Usage: llama.sendcall <First> <Last> <topic>")
            output("[Llamafone] Example: llama.sendcall Bella Goth I need to tell you something")
            return
        two_word_name = f"{args[0]} {args[1]}"
        contact = phone.find_contact_by_name(two_word_name)
        if contact and len(args) > 2:
            message = " ".join(args[2:])
        else:
            one_word_name = args[0]
            contact = phone.find_contact_by_name(one_word_name)
            if contact:
                message = " ".join(args[1:])
            else:
                output(f"[Llamafone] Could not find '{two_word_name}' or '{one_word_name}'.")
                return
        if not message:
            output("[Llamafone] You need to include what you want to say.")
            return
        output(f"[Llamafone] Calling {contact['name']}...")
        phone.send_call(contact, message, output=output)

    @sims4.commands.Command("llama.contact", command_type=sims4.commands.CommandType.Live)
    def cmd_contact(*args, _connection=None):
        """Manage per-relationship preferences (household_sim <-> contact_sim).

        Preferences are SCOPED to the currently active/selected household
        sim. Switch to a different household member first to manage THEIR
        preferences with the same contact.

        Usage:
          llama.contact First Last                    -- show current prefs
          llama.contact First Last muted              -- silence this contact
          llama.contact First Last paused             -- 'asked for space'
          llama.contact First Last priority           -- favorite; more calls
          llama.contact First Last clear              -- back to normal
          llama.contact First Last note <text>        -- set a freeform note
          llama.contact First Last note               -- clear the note
          llama.contact list                          -- show all overrides
                                                         across every pair
        """
        output = sims4.commands.CheatOutput(_connection)

        if not args:
            output("[Llamafone] Usage: llama.contact First Last [muted|paused|priority|clear|note <text>]")
            output("[Llamafone]        llama.contact list -- show all current overrides")
            return

        # Resolve the current household sim (active in the client). All
        # per-pair reads/writes below use this as the household side.
        active_household_si = sim_context.get_main_sim_info()
        household_sim_id = getattr(active_household_si, "sim_id", None) if active_household_si else None
        household_name = ""
        if active_household_si:
            try:
                household_name = f"{active_household_si.first_name} {active_household_si.last_name}".strip()
            except Exception:
                household_name = ""

        # llama.contact list -- dump all overrides across every pair
        if len(args) == 1 and args[0].lower() == "list":
            entries = contact_prefs.list_all()
            if not entries:
                output("[Llamafone] No contact preferences set.")
                return
            output(f"[Llamafone] {len(entries)} pair override(s):")
            try:
                import services as _services
                mgr = _services.sim_info_manager()
            except Exception:
                mgr = None

            def _sim_name(sid):
                try:
                    if mgr and sid is not None:
                        si = mgr.get(int(sid))
                        if si:
                            return f"{si.first_name} {si.last_name}".strip()
                except Exception:
                    pass
                return f"[{sid}]"

            for key, e in entries.items():
                hh_id = e.get("household_sim_id")
                other_id = e.get("other_sim_id")
                state = e.get("state") or "-"
                note = e.get("note") or ""
                note_str = f" (note: {note!r})" if note else ""
                if e.get("_legacy"):
                    scope = f"[legacy, any household member] {_sim_name(other_id)}"
                else:
                    scope = f"{_sim_name(hh_id)} -> {_sim_name(other_id)}"
                output(f"[Llamafone]   {scope}  state={state}{note_str}")
            return

        # For per-sim commands, parse: <First> <Last> [action] [rest]
        if len(args) < 2:
            output("[Llamafone] Usage: llama.contact First Last [muted|paused|priority|clear|note <text>]")
            return

        if not household_sim_id:
            output("[Llamafone] No active household sim -- select one and try again.")
            return

        # Resolve the contact by first + last name
        two_word_name = f"{args[0]} {args[1]}"
        contact = phone.find_contact_by_name(two_word_name)
        if not contact:
            one_word_name = args[0]
            contact = phone.find_contact_by_name(one_word_name)
            rest_offset = 1
        else:
            rest_offset = 2
        if not contact:
            output(f"[Llamafone] Could not find a contact matching '{two_word_name}'.")
            return
        si = contact.get("sim_info")
        other_sim_id = getattr(si, "sim_id", None) if si else None
        if not other_sim_id:
            output(f"[Llamafone] Could not resolve sim id for {contact.get('name','?')}.")
            return
        name = contact.get("name", "?")
        output(f"[Llamafone] Scope: {household_name} -> {name}")

        # No action specified -- show current
        if len(args) <= rest_offset:
            e = contact_prefs.get_prefs(household_sim_id, other_sim_id) or {}
            state = e.get("state") or "no override (normal behavior)"
            note = e.get("note") or "(none)"
            output(f"[Llamafone]   state: {state}")
            since_ticks = e.get("state_since_ticks")
            if since_ticks is not None:
                try:
                    now = contact_prefs._now_ingame_ticks()
                    if now is not None and now >= since_ticks:
                        delta = contact_prefs._format_ingame_delta(now - since_ticks)
                        if delta:
                            output(f"[Llamafone]   since: {delta}")
                except Exception:
                    pass
            since_real = e.get("state_since", "")
            if since_real:
                output(f"[Llamafone]   set on (real time): {since_real[:19]}")
            output(f"[Llamafone]   note:  {note}")
            output("[Llamafone] Options: muted / paused / priority / clear / note <text>")
            return

        action = args[rest_offset].lower()
        if action in ("muted", "paused", "priority"):
            contact_prefs.set_state(household_sim_id, other_sim_id, action)
            output(f"[Llamafone] {household_name} -> {name}: {action}.")
            if action == "paused":
                output("[Llamafone] Auto-events for them will fire 5x less, and the AI knows you asked for space.")
            elif action == "muted":
                output("[Llamafone] No more auto-calls or auto-texts from them (for THIS household sim only).")
            elif action == "priority":
                output("[Llamafone] They'll get 2x more auto-events. AI will treat them warmly.")
        elif action == "clear":
            contact_prefs.set_state(household_sim_id, other_sim_id, None)
            output(f"[Llamafone] Cleared state for {household_name} -> {name}. (Any note is preserved -- use 'note' to clear it.)")
        elif action == "note":
            note_text = " ".join(args[rest_offset + 1:]).strip()
            contact_prefs.set_note(household_sim_id, other_sim_id, note_text)
            if note_text:
                output(f"[Llamafone] Note ({household_name} -> {name}): {note_text!r}")
            else:
                output(f"[Llamafone] Note ({household_name} -> {name}) cleared.")
        else:
            output(f"[Llamafone] Unknown action {action!r}. Use: muted / paused / priority / clear / note")

    @sims4.commands.Command("llama.reply", command_type=sims4.commands.CommandType.Live)
    def cmd_reply(*args, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        message = " ".join(args) if args else ""
        if not message:
            convo = phone.get_active_conversation()
            if convo:
                output(f"[Llamafone] Active conversation with {convo['contact']['name']}")
                output("[Llamafone] Usage: llama.reply <your message>")
            else:
                output("[Llamafone] No active conversation. Use llama.call or llama.text first.")
            return
        convo = phone.get_active_conversation()
        if convo:
            output(f"[Llamafone] Replying to {convo['contact']['name']}...")
        phone.generate_reply(message, output=output)

    # -------------------------------------------------------------------------
    # Debug
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.dumpphone", command_type=sims4.commands.CommandType.Live)
    def cmd_dumpphone(_connection=None):
        """Diagnostic: dump what the game thinks the active sim's
        phone affordances are right now. Writes to BOTH the cheat
        console and Documents/Llamafone_Log.txt under [dumpphone]."""
        out_console = sims4.commands.CheatOutput(_connection)
        import os, datetime
        log_path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")

        def out(msg):
            try:
                out_console(msg)
            except Exception:
                pass
            try:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] [dumpphone] {msg}\n")
            except Exception:
                pass

        try:
            active = sim_context.get_active_sim()
            sim = active if active is not None else None
            sim_info = getattr(sim, 'sim_info', None)
            if sim is None or sim_info is None:
                out("no active sim")
                return
            out(f"sim: {sim_info.first_name} {sim_info.last_name}")

            phone_affs = list(getattr(sim, "_phone_affordances", ()) or ())
            out(f"sim._phone_affordances: {len(phone_affs)} entries")

            # Is our Llamafone phone affordance in the runtime list?
            ours_present = False
            seen_categories = {}
            for a in phone_affs:
                name = getattr(a, '__name__', repr(a))
                cat = getattr(a, 'category', None)
                cat_name = getattr(cat, '__name__', None) if cat else None
                seen_categories[cat_name] = seen_categories.get(cat_name, 0) + 1
                if name in ("Llamafone_Call", "Llamafone_Text", "Llamafone_Settings"):
                    ours_present = True
                    out(f"  [OURS] {name}  category={cat_name}  "
                        f"target={getattr(a, 'target_type', None)}  "
                        f"appropriateness={getattr(a, 'appropriateness_tags', None)}  "
                        f"icat={getattr(a, 'interaction_category_tags', None)}")

            out(f"Llamafone phone affordances PRESENT in live list: {ours_present}")
            out(f"unique categories ({len(seen_categories)}): {sorted(seen_categories.items(), key=lambda x: -x[1])[:20]}")

            # Dump all entries so we can compare ours to what works
            out(f"all _phone_affordances entries:")
            for a in phone_affs:
                name = getattr(a, '__name__', repr(a))
                cat = getattr(a, 'category', None)
                cat_name = getattr(cat, '__name__', None) if cat else None
                out(f"  - {name}  category={cat_name}")
        except Exception as e:
            out(f"failed: {type(e).__name__}: {e}")

    @sims4.commands.Command("llama.debug", command_type=sims4.commands.CommandType.Live)
    def cmd_debug(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        main_si = sim_context.get_main_sim_info()
        if not main_si:
            active = sim_context.get_active_sim()
            main_si = active.sim_info if active else None
        if not main_si:
            output("[Llamafone] No sim found to debug.")
            return

        output(f"[Debug] Sim: {main_si.first_name} {main_si.last_name}")
        output(f"[Debug] sim_info type: {type(main_si).__name__}")

        # Trait tracker
        try:
            tt = main_si.trait_tracker
            output(f"[Debug] trait_tracker type: {type(tt).__name__}")
            output(f"[Debug] trait_tracker attrs: {[a for a in dir(tt) if 'trait' in a.lower()]}")
        except Exception as e:
            output(f"[Debug] trait_tracker error: {e}")

        # Relationship tracker
        try:
            rt = main_si.relationship_tracker
            output(f"[Debug] relationship_tracker type: {type(rt).__name__}")
            rel_attrs = [a for a in dir(rt) if not a.startswith('__')]
            output(f"[Debug] rel_tracker attrs (first 20): {rel_attrs[:20]}")
            output(f"[Debug] rel_tracker attrs (next 20): {rel_attrs[20:40]}")

            # Try to count relationships with various accessors
            for attr in ("relationships", "_relationships", "_relationship_objects",
                         "relationship_objects", "_all_bits"):
                try:
                    obj = getattr(rt, attr, None)
                    if obj is not None:
                        if hasattr(obj, '__len__'):
                            output(f"[Debug] rt.{attr}: {type(obj).__name__}, len={len(obj)}")
                        elif hasattr(obj, 'values'):
                            output(f"[Debug] rt.{attr}: dict-like, len={len(list(obj.values()))}")
                        else:
                            output(f"[Debug] rt.{attr}: {type(obj).__name__}")
                except Exception as e:
                    output(f"[Debug] rt.{attr}: error {e}")

            # Try method calls
            for method in ("get_all_sim_relationships", "get_relationships",
                           "get_all_bits", "target_sim_gen"):
                try:
                    fn = getattr(rt, method, None)
                    if fn and callable(fn):
                        result = list(fn())
                        output(f"[Debug] rt.{method}(): returned {len(result)} items")
                        if result:
                            output(f"[Debug]   first item type: {type(result[0]).__name__}")
                except Exception as e:
                    output(f"[Debug] rt.{method}(): error {e}")

        except Exception as e:
            output(f"[Debug] relationship_tracker error: {e}")

        # Skill tracker
        try:
            st = main_si.skill_tracker
            output(f"[Debug] skill_tracker type: {type(st).__name__}")
            skill_attrs = [a for a in dir(st) if 'skill' in a.lower() or 'stat' in a.lower()]
            output(f"[Debug] skill_tracker attrs: {skill_attrs}")
        except Exception as e:
            output(f"[Debug] skill_tracker error: {e}")

        # Test notification
        output("[Debug] Testing notification popup...")
        result = notifications._show_game_notification("Llamafone Test", "If you see this popup, notifications work!")
        output(f"[Debug] Notification result: {result}")

        output("[Debug] Use llama.debugsim <First> <Last> to see a sim's prompt context")
        output("[Debug] Use llama.testbuffs to test the moodlet system")

    @sims4.commands.Command("llama.debugsim", command_type=sims4.commands.CommandType.Live)
    def cmd_debug_sim(first_name: str = None, last_name: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Llamafone] Usage: llama.debugsim <First> <Last>")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        contact = phone.find_contact_by_name(full_name)
        if not contact:
            output(f"[Llamafone] Could not find '{full_name}'.")
            return
        desc = phone._describe_relationship(contact)
        output(f"=== Prompt context for {contact['name']} ===")
        output(desc)

    @sims4.commands.Command("llama.pastevents", command_type=sims4.commands.CommandType.Live)
    def cmd_past_events(_connection=None):
        """List every recorded past event (id, name, when, honored) so a
        spurious entry -- e.g. a canceled wedding recorded by an older
        version -- can be identified and removed with
        llama.pastevents_drop <id>."""
        output = sims4.commands.CheatOutput(_connection)
        from . import past_events as _pe
        items = _pe.list_all()
        if not items:
            output("[Llamafone] No past events recorded for this save.")
            return
        now_ticks = _pe._now_ticks()
        output(f"[Llamafone] {len(items)} recorded past event(s), newest first:")
        for key, e in items[:40]:
            when = "?"
            st = e.get("start_ticks")
            if st is not None and now_ticks is not None:
                days = (now_ticks - st) / (_pe._TICKS_PER_MINUTE * 60 * 24)
                when = f"{days:+.1f} sim days ago" if days >= 0 else f"in {-days:.1f} sim days"
            honored = ", ".join(
                f"{h.get('name')}({h.get('role')})" if isinstance(h, dict) else str(h)
                for h in (e.get("honored") or [])
            )
            output(f"  {key}  {e.get('name')!r}  {when}  attendees={len(e.get('attendees') or [])}"
                   f"{'  honored=' + honored if honored else ''}")

    @sims4.commands.Command("llama.pastevents_drop", command_type=sims4.commands.CommandType.Live)
    def cmd_past_events_drop(event_id: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not event_id:
            output("[Llamafone] Usage: llama.pastevents_drop <event_id>  (ids from llama.pastevents)")
            return
        from . import past_events as _pe
        if _pe.drop(event_id):
            output(f"[Llamafone] Dropped past event {event_id}.")
        else:
            output(f"[Llamafone] No past event with id {event_id}.")

    @sims4.commands.Command("llama.journal_undo", command_type=sims4.commands.CommandType.Live)
    def cmd_journal_undo(first_name: str = None, last_name: str = None, count: int = 1, _connection=None):
        """Remove the newest N journal entries involving a sim. Use after a
        test-mode message (llama.testbirth) so the pretend announcement
        doesn't become canon in every later prompt."""
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Llamafone] Usage: llama.journal_undo <First> <Last> [count]")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        sim_id = None
        try:
            contact = phone.find_contact_by_name(full_name)
            sim_id = contact.get("sim_id") if contact else None
        except Exception:
            pass
        removed = journal.remove_last_for_sim(full_name, n=max(1, int(count)), sim_id=sim_id)
        if not removed:
            output(f"[Llamafone] No journal entries found for '{full_name}'.")
            return
        output(f"[Llamafone] Removed {len(removed)} entry(ies) for {full_name}:")
        for e in removed:
            preview = str(e.get("content", "")).replace("\n", " ")[:90]
            output(f"  - {e.get('timestamp', '?')[:19]}  {preview}")

    @sims4.commands.Command("llama.birthwatch", command_type=sims4.commands.CommandType.Live)
    def cmd_birthwatch(_connection=None):
        """Run one birth-watcher pass now and show what it evaluated."""
        output = sims4.commands.CheatOutput(_connection)
        from . import births as _births
        for line in _births.watch_report():
            output(f"[Llamafone] {line}")

    @sims4.commands.Command("llama.testbirth", command_type=sims4.commands.CommandType.Live)
    def cmd_test_birth(first_name: str = None, last_name: str = None, _connection=None):
        """Simulate '<First> <Last> just gave birth' and run the real
        announcement selection: finds the strongest family / close-friend
        tie into the active household and schedules the incoming text
        (~10s instead of the usual 90-240s). The named sim does NOT have
        to actually be pregnant -- the prompt will still say 'had a baby'
        only if a birth milestone exists, so for a dry run of the tie
        selection just watch the cheat output and the log."""
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Llamafone] Usage: llama.testbirth <First> <Last>")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        si = None
        try:
            want = full_name.lower()
            for cand in services.sim_info_manager().get_all():
                nm = f"{getattr(cand, 'first_name', '')} {getattr(cand, 'last_name', '')}".strip().lower()
                if nm == want:
                    si = cand
                    break
        except Exception:
            pass
        if si is None:
            output(f"[Llamafone] Could not find '{full_name}'.")
            return
        from . import births as _births

        class _Shim:
            def __init__(self, s):
                self._sim_info = s
            def get_partner(self):
                try:
                    pt = getattr(self._sim_info, "pregnancy_tracker", None)
                    return pt.get_partner() if pt is not None else None
                except Exception:
                    return None

        pick = _births._best_announcement(si, _Shim(si).get_partner())
        if pick is None:
            output(f"[Llamafone] No family / close-friend tie from {full_name} (or partner) into the active household. Nothing to announce.")
            return
        recipient, contact, label = pick
        output(f"[Llamafone] Would announce: {contact.get('name')} -> {recipient.first_name} ({label}). Firing in ~10s...")
        import threading as _th
        t = _th.Timer(10, _births._fire_announcement, args=(recipient, contact, si, label, True))
        t.daemon = True
        try:
            phone._track_timer(t)
        except Exception:
            pass
        t.start()

    @sims4.commands.Command("llama.roledebug", command_type=sims4.commands.CommandType.Live)
    def cmd_role_debug(first_name: str = None, last_name: str = None, _connection=None):
        """Show what the service-NPC detector sees for a sim: role,
        whether it's a confirmed household hire, and every career
        class / trait / title string it examined. Answers 'why did it
        call my dad a nanny' directly."""
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Llamafone] Usage: llama.roledebug <First> <Last>")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        si = None
        try:
            contact = phone.find_contact_by_name(full_name)
            si = contact.get("sim_info") if contact else None
        except Exception:
            si = None
        if si is None:
            try:
                want = full_name.lower()
                for cand in services.sim_info_manager().get_all():
                    nm = f"{getattr(cand, 'first_name', '')} {getattr(cand, 'last_name', '')}".strip().lower()
                    if nm == want:
                        si = cand
                        break
            except Exception:
                pass
        if si is None:
            output(f"[Llamafone] Could not find '{full_name}'.")
            return
        from . import service_npc as _svc
        role, confirmed = _svc.get_service_role(si)
        output(f"=== Service-role debug: {si.first_name} {si.last_name} ===")
        output(f"  resolved role: {role!r}  confirmed_hire={confirmed}")
        try:
            ct = getattr(si, "career_tracker", None)
            careers = getattr(ct, "careers", None) if ct else None
            it = careers.values() if hasattr(careers, "values") else (careers or [])
            names = [getattr(type(c), "__name__", "?") for c in it if c is not None]
            output(f"  career classes: {names}")
        except Exception as e:
            output(f"  career classes: (read failed: {type(e).__name__}: {e})")
        try:
            traits = sim_context.get_sim_traits(si, limit=30) or []
            output(f"  traits: {traits}")
        except Exception:
            pass
        try:
            output(f"  title: {getattr(si, 'title', None)!r}  sim_id={getattr(si, 'sim_id', None)}")
        except Exception:
            pass

    @sims4.commands.Command("llama.pregdebug", command_type=sims4.commands.CommandType.Live)
    def cmd_preg_debug(first_name: str = None, last_name: str = None, _connection=None):
        """Show exactly what the pregnancy-visibility resolver sees for a
        sim: is_pregnant, active pregnancy buffs, the pregnancy commodity's
        value / state / state-buff, and the resolved tier. Searches every
        sim in the save (not just the active sim's contacts) so off-lot
        NPCs like a cousin's pregnant wife can be checked."""
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Llamafone] Usage: llama.pregdebug <First> <Last>")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        si = None
        try:
            contact = phone.find_contact_by_name(full_name)
            si = contact.get("sim_info") if contact else None
        except Exception:
            si = None
        if si is None:
            try:
                want = full_name.lower()
                for cand in services.sim_info_manager().get_all():
                    nm = f"{getattr(cand, 'first_name', '')} {getattr(cand, 'last_name', '')}".strip().lower()
                    if nm == want or (not last_name and getattr(cand, "first_name", "").lower() == want):
                        si = cand
                        break
            except Exception as e:
                output(f"[Llamafone] sim lookup failed: {type(e).__name__}: {e}")
        if si is None:
            output(f"[Llamafone] Could not find '{full_name}'.")
            return
        from . import milestones as _milestones
        info = _milestones.pregnancy_debug_info(si)
        output(f"=== Pregnancy debug: {si.first_name} {si.last_name} (id {getattr(si, 'sim_id', '?')}) ===")
        for k, v in info.items():
            output(f"  {k}: {v!r}")

    @sims4.commands.Command("llama.dumpprompt", command_type=sims4.commands.CommandType.Live)
    def cmd_dump_prompt(first_name: str = None, last_name: str = None, _connection=None):
        """Build a text prompt as if the named sim were texting the active sim,
        but DON'T send it. Just write it to a file so we can inspect what
        the AI would see."""
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Llamafone] Usage: llama.dumpprompt <First> <Last>")
            output("[Llamafone] Builds a text prompt as if that sim was texting you,")
            output("[Llamafone] writes it to Llamafone_LastPrompt.txt in your Mods folder.")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        contact = phone.find_contact_by_name(full_name)
        if not contact:
            output(f"[Llamafone] Could not find '{full_name}'.")
            return

        # Use the currently active sim as the "recipient" so we see exactly
        # what would be sent if they got a text from this contact right now.
        recipient = None
        active = sim_context.get_active_sim()
        if active:
            recipient = active.sim_info
        if not recipient:
            output("[Llamafone] No active sim to use as recipient.")
            return

        recipient_name = recipient.first_name
        rel_desc = phone._describe_relationship(contact, recipient=recipient)
        sim_history = journal.format_sim_history_for_prompt(
            contact["name"],
            recipient_name=recipient_name,
            sim_id=getattr(contact.get("sim_info"), "sim_id", None),
            recipient_id=getattr(recipient, "sim_id", None),
        )
        history_block = f"\n\n{sim_history}" if sim_history else ""
        mutuals = phone._get_mutual_contacts(contact, recipient=recipient)
        mutual_block = ""
        if mutuals:
            mutual_block = "\n\nPeople BOTH of you know:\n" + "\n".join(f"  - {m}" for m in mutuals)

        prompt = (
            f"Sender info:\n{rel_desc}{history_block}{mutual_block}\n\n"
            f"They are texting {recipient_name}{phone._location_context(recipient, contact)}.\n\n"
            f"Write 1-2 short text messages from {contact['name']}."
        )

        # Write to the same file the API client uses
        try:
            from . import api_client
            import datetime
            path = api_client._last_prompt_path()
            with open(path, "w", encoding="utf-8") as f:
                f.write("=== Llamafone — Simulated Prompt (NOT SENT) ===\n")
                f.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
                f.write(f"Recipient (active sim): {recipient.first_name} {recipient.last_name}\n")
                f.write(f"Sender: {contact['name']}\n\n")
                f.write("=== USER PROMPT ===\n")
                f.write(prompt + "\n")
            output(f"[Llamafone] Prompt written to: {path}")
        except Exception as e:
            output(f"[Llamafone] Error writing file: {e}")

    @sims4.commands.Command("llama.testbuffs", command_type=sims4.commands.CommandType.Live)
    def cmd_testbuffs(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        buff_mgr = services.get_instance_manager(sims4.resources.Types.BUFF)
        # Try .types dict (what MCCC uses)
        try:
            types_dict = buff_mgr.types
            output("types count: " + str(len(types_dict)))
            count = 0
            for bid, btype in types_dict.items():
                bn = type(btype).__name__
                if "happy" in bn.lower() and count < 5:
                    output("  " + str(bid) + " " + bn)
                    count += 1
        except BaseException as e:
            output("types failed: " + str(e))
        # Try getting MCCC's known IDs and applying
        si = sim_context.get_main_sim_info()
        if not si:
            active = sim_context.get_active_sim()
            if active:
                si = active.sim_info
        if si:
            # Try a bunch of known buff IDs
            for bid in (27738, 28389, 103481, 24858, 32424):
                bt = buff_mgr.get(bid)
                if bt:
                    output("found id " + str(bid) + ": " + type(bt).__name__)
                    try:
                        si.debug_add_buff_by_type(bt)
                        output("debug_add OK for " + str(bid))
                    except BaseException as e1:
                        output("debug_add fail: " + str(e1))
                    try:
                        si.add_buff_from_op(bt, buff_reason=None)
                        output("add_from_op OK for " + str(bid))
                    except BaseException as e2:
                        output("add_from_op fail: " + str(e2))
                    break
            else:
                output("no known IDs found")

    # -------------------------------------------------------------------------
    # Journal
    # -------------------------------------------------------------------------

    @sims4.commands.Command("llama.journal", command_type=sims4.commands.CommandType.Live)
    def cmd_journal(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        text = journal.format_recent_for_display(n=10)
        output(text)

    @sims4.commands.Command("llama.journal_sim", command_type=sims4.commands.CommandType.Live)
    def cmd_journal_sim(first_name: str = None, last_name: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Llamafone] Usage: llama.journal_sim <FirstName> <LastName>")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        entries = journal.get_sim_history(full_name, n=10)
        if not entries:
            output(f"[Llamafone] No journal entries for {full_name}.")
            return
        output(f"=== Journal entries for {full_name} ({len(entries)} shown) ===")
        for e in reversed(entries):
            try:
                import datetime
                dt = datetime.datetime.fromisoformat(e["timestamp"])
                date_str = dt.strftime("%b %d %H:%M")
            except Exception:
                date_str = "?"
            label = e.get("type", "note").replace("_", " ").title()
            preview = e.get("content", "").strip()[:400]
            output(f"\n[{date_str}] {label}")
            output(preview)

    @sims4.commands.Command("llama.journal_clear", command_type=sims4.commands.CommandType.Live)
    def cmd_journal_clear(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        count = journal.get_entry_count()
        journal.clear()
        output(f"[Llamafone] Journal cleared ({count} entries deleted).")

except Exception as e:
    # Running outside the Sims 4 game environment, or an error during load
    try:
        import sims4.commands
        sims4.commands.output(f"[Llamafone] Commands failed to register: {type(e).__name__}: {e}", None)
    except Exception:
        pass
