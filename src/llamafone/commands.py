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
    from . import sim_context, dialogue, storyteller, event_generator, notifications, api_client, auto_events, journal, phone

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
        output("  llama.call                 — incoming call from a relationship sim")
        output("  llama.text                 — text message from a relationship sim")
        output("  llama.sendtext First Last msg — text a specific sim")
        output("  llama.sendcall First Last msg — call a specific sim")
        output("  llama.reply <message>      — reply to the last call or text")
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
        sim_history = journal.format_sim_history_for_prompt(contact["name"], recipient_name=recipient_name)
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
