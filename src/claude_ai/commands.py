"""
Cheat console commands for the Claude AI mod.
Open the cheat console with Ctrl+Shift+C, then type a command.

COMMANDS:
  claude.status                     Check config and list all commands
  claude.dialogue                   Generate dialogue for the active sim
  claude.dialogue_situation <text>  Generate dialogue for a specific situation
  claude.backstory                  Generate a backstory for the active sim
  claude.story                      Narrative update for your household
  claude.storyline                  Generate a 3-act storyline
  claude.storyline_theme <theme>    Generate a storyline with a specific theme
  claude.drama                      Generate relationship drama arc
  claude.event                      Generate a surprise random event
  claude.challenge                  Generate a medium challenge
  claude.challenge_easy             Generate an easy challenge
  claude.challenge_hard             Generate a hard challenge
  claude.goals                      Generate weekly goals for this session
  claude.chat <message>             Chat with Claude about your game
  claude.reload                     Reload config (after editing claude_config.cfg)
  claude.auto_events on|off         Enable/disable random auto-events mid-session
"""

try:
    import sims4.commands
    import sims4.resources
    import services
    from . import config, sim_context, dialogue, storyteller, event_generator, notifications, api_client, auto_events, journal, phone

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _require_config(output):
        if not config.is_configured():
            output("[Claude AI] Not configured. Edit claude_config.cfg and add your API key.")
            output("[Claude AI] Then run: claude.reload")
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

    @sims4.commands.Command("claude.status", command_type=sims4.commands.CommandType.Live)
    def cmd_status(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if config.is_configured():
            output(f"[Claude AI] ✓ Configured")
            output(f"[Claude AI]   Default model : {config.get_default_model()}")
            output(f"[Claude AI]   Fast model    : {config.get_fast_model()}")
            output(f"[Claude AI]   Max tokens    : {config.get_max_tokens()}")
            output(f"[Claude AI]   Language      : {config.get_language()}")
        else:
            output("[Claude AI] ✗ NOT configured — edit claude_config.cfg and add your API key")

        output(f"[Claude AI] {auto_events.status()}")
        output(f"[Claude AI] Journal: {journal.get_entry_count()} entries saved")
        output("")
        output("[Claude AI] Available commands:")
        output("  claude.dialogue             — active sim's dialogue")
        output("  claude.dialogue_situation   — dialogue for a specific situation")
        output("  claude.backstory            — active sim's backstory")
        output("  claude.story                — household narrative update")
        output("  claude.storyline            — 3-act storyline")
        output("  claude.storyline_theme X    — storyline with theme X")
        output("  claude.drama                — relationship drama arc")
        output("  claude.event                — surprise random event")
        output("  claude.challenge            — medium gameplay challenge")
        output("  claude.challenge_easy       — easy challenge")
        output("  claude.challenge_hard       — hard challenge")
        output("  claude.goals                — weekly session goals")
        output("  claude.call                 — incoming call from a relationship sim")
        output("  claude.text                 — text message from a relationship sim")
        output("  claude.sendtext First Last msg — text a specific sim")
        output("  claude.sendcall First Last msg — call a specific sim")
        output("  claude.reply <message>      — reply to the last call or text")
        output("  claude.chat <message>       — chat about your game")
        output("  claude.journal              — view recent journal entries")
        output("  claude.reload               — reload config file")

    @sims4.commands.Command("claude.reload", command_type=sims4.commands.CommandType.Live)
    def cmd_reload(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        config.reload_config()
        auto_events.restart()  # pick up any changes to auto-event settings
        if config.is_configured():
            output("[Claude AI] Config reloaded. API key found — you're good to go!")
        else:
            output("[Claude AI] Config reloaded. Still no API key found.")
            output("[Claude AI] Make sure claude_config.cfg is in your Mods folder.")

    @sims4.commands.Command("claude.auto_events", command_type=sims4.commands.CommandType.Live)
    def cmd_auto_events(toggle: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if toggle is None:
            output(f"[Claude AI] {auto_events.status()}")
            output("[Claude AI] Usage: claude.auto_events on  OR  claude.auto_events off")
            return
        if toggle.lower() in ("on", "true", "1", "yes"):
            auto_events.stop()
            # Temporarily force-enable regardless of config
            config.get_config().set("claude_ai", "auto_events_enabled", "true")
            auto_events.start()
            output(f"[Claude AI] Auto-events turned ON for this session.")
            output(f"[Claude AI] {auto_events.status()}")
            output("[Claude AI] To make this permanent, set auto_events_enabled = true in claude_config.cfg")
        elif toggle.lower() in ("off", "false", "0", "no"):
            auto_events.stop()
            output("[Claude AI] Auto-events turned OFF for this session.")

    # -------------------------------------------------------------------------
    # Dialogue
    # -------------------------------------------------------------------------

    @sims4.commands.Command("claude.dialogue", command_type=sims4.commands.CommandType.Live)
    def cmd_dialogue(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        sim = sim_context.get_active_sim()
        if not sim:
            output("[Claude AI] No active sim found.")
            return
        name = sim.sim_info.first_name
        output(f"[Claude AI] Generating dialogue for {name}…")
        dialogue.generate_sim_dialogue(sim=sim, callback=_on_result(f"{name}'s Dialogue", output))

    @sims4.commands.Command("claude.dialogue_situation", command_type=sims4.commands.CommandType.Live)
    def cmd_dialogue_situation(situation: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if not situation:
            output("[Claude AI] Usage: claude.dialogue_situation <situation description>")
            output("[Claude AI] Example: claude.dialogue_situation just got promoted")
            return
        sim = sim_context.get_active_sim()
        name = sim.sim_info.first_name if sim else "Your Sim"
        output(f"[Claude AI] Generating dialogue for: {situation}…")
        dialogue.generate_sim_dialogue(
            sim=sim,
            situation=situation,
            callback=_on_result(f"{name}'s Dialogue", output),
        )

    @sims4.commands.Command("claude.backstory", command_type=sims4.commands.CommandType.Live)
    def cmd_backstory(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        sim = sim_context.get_active_sim()
        name = sim.sim_info.first_name if sim else "Sim"
        output(f"[Claude AI] Generating backstory for {name}…")
        dialogue.generate_npc_backstory(sim=sim, callback=_on_result(f"{name}'s Backstory", output))

    # -------------------------------------------------------------------------
    # Storytelling
    # -------------------------------------------------------------------------

    @sims4.commands.Command("claude.story", command_type=sims4.commands.CommandType.Live)
    def cmd_story(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Generating household story update…")
        storyteller.generate_story_update(callback=_on_result("Household Story", output))

    @sims4.commands.Command("claude.storyline", command_type=sims4.commands.CommandType.Live)
    def cmd_storyline(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Generating 3-act storyline…")
        storyteller.generate_storyline(callback=_on_result("Storyline", output))

    @sims4.commands.Command("claude.storyline_theme", command_type=sims4.commands.CommandType.Live)
    def cmd_storyline_theme(theme: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if not theme:
            output("[Claude AI] Usage: claude.storyline_theme <theme>")
            output("[Claude AI] Examples: romance  |  rivalry  |  rags to riches  |  ghost mystery")
            return
        output(f"[Claude AI] Generating storyline with theme: {theme}…")
        storyteller.generate_storyline(theme=theme, callback=_on_result("Storyline", output))

    @sims4.commands.Command("claude.drama", command_type=sims4.commands.CommandType.Live)
    def cmd_drama(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Generating relationship drama arc…")
        storyteller.generate_relationship_drama(callback=_on_result("Relationship Drama", output))

    # -------------------------------------------------------------------------
    # Events & challenges
    # -------------------------------------------------------------------------

    @sims4.commands.Command("claude.event", command_type=sims4.commands.CommandType.Live)
    def cmd_event(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Rolling a random event…")
        event_generator.generate_random_event(callback=_on_result("Random Event!", output))

    @sims4.commands.Command("claude.challenge", command_type=sims4.commands.CommandType.Live)
    def cmd_challenge(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Generating medium challenge…")
        event_generator.generate_challenge(difficulty="medium", callback=_on_result("Challenge", output))

    @sims4.commands.Command("claude.challenge_easy", command_type=sims4.commands.CommandType.Live)
    def cmd_challenge_easy(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Generating easy challenge…")
        event_generator.generate_challenge(difficulty="easy", callback=_on_result("Easy Challenge", output))

    @sims4.commands.Command("claude.challenge_hard", command_type=sims4.commands.CommandType.Live)
    def cmd_challenge_hard(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Generating hard challenge…")
        event_generator.generate_challenge(difficulty="hard", callback=_on_result("Hard Challenge", output))

    @sims4.commands.Command("claude.goals", command_type=sims4.commands.CommandType.Live)
    def cmd_goals(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Generating weekly goals…")
        event_generator.generate_weekly_goals(callback=_on_result("Weekly Goals", output))

    # -------------------------------------------------------------------------
    # Freeform chat
    # -------------------------------------------------------------------------

    @sims4.commands.Command("claude.chat", command_type=sims4.commands.CommandType.Live)
    def cmd_chat(message: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if not message:
            output("[Claude AI] Usage: claude.chat <your message>")
            output("[Claude AI] Example: claude.chat what career should my sim pursue?")
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

        output("[Claude AI] Thinking…")

        def on_chat_result(text, error):
            if text:
                journal.add_entry("chat", f"Q: {message}\nA: {text}")
            _on_result("Claude AI", output)(text, error)

        api_client.call_claude_async(
            [{"role": "user", "content": prompt}],
            system=system,
            callback=on_chat_result,
        )

    # -------------------------------------------------------------------------
    # Phone calls & texts
    # -------------------------------------------------------------------------

    @sims4.commands.Command("claude.call", command_type=sims4.commands.CommandType.Live)
    def cmd_call(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Incoming call...")
        phone.generate_call(output=output)

    @sims4.commands.Command("claude.text", command_type=sims4.commands.CommandType.Live)
    def cmd_text(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        output("[Claude AI] Checking messages...")
        phone.generate_text(output=output)

    @sims4.commands.Command("claude.sendtext", command_type=sims4.commands.CommandType.Live)
    def cmd_sendtext(*args, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        # Parse: claude.sendtext FirstName LastName your message here
        # Need at least a name and a message
        if len(args) < 2:
            output("[Claude AI] Usage: claude.sendtext <First> <Last> <message>")
            output("[Claude AI] Example: claude.sendtext Bella Goth hey want to hang out?")
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
                output(f"[Claude AI] Could not find '{two_word_name}' or '{one_word_name}'.")
                return
        if not message:
            output("[Claude AI] You need to include a message.")
            return
        output(f"[Claude AI] Texting {contact['name']}...")
        phone.send_text(contact, message, output=output)

    @sims4.commands.Command("claude.sendcall", command_type=sims4.commands.CommandType.Live)
    def cmd_sendcall(*args, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        if len(args) < 2:
            output("[Claude AI] Usage: claude.sendcall <First> <Last> <topic>")
            output("[Claude AI] Example: claude.sendcall Bella Goth I need to tell you something")
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
                output(f"[Claude AI] Could not find '{two_word_name}' or '{one_word_name}'.")
                return
        if not message:
            output("[Claude AI] You need to include what you want to say.")
            return
        output(f"[Claude AI] Calling {contact['name']}...")
        phone.send_call(contact, message, output=output)

    @sims4.commands.Command("claude.reply", command_type=sims4.commands.CommandType.Live)
    def cmd_reply(*args, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not _require_config(output):
            return
        message = " ".join(args) if args else ""
        if not message:
            convo = phone.get_active_conversation()
            if convo:
                output(f"[Claude AI] Active conversation with {convo['contact']['name']}")
                output("[Claude AI] Usage: claude.reply <your message>")
            else:
                output("[Claude AI] No active conversation. Use claude.call or claude.text first.")
            return
        convo = phone.get_active_conversation()
        if convo:
            output(f"[Claude AI] Replying to {convo['contact']['name']}...")
        phone.generate_reply(message, output=output)

    # -------------------------------------------------------------------------
    # Debug
    # -------------------------------------------------------------------------

    @sims4.commands.Command("claude.debug", command_type=sims4.commands.CommandType.Live)
    def cmd_debug(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        main_si = sim_context.get_main_sim_info()
        if not main_si:
            active = sim_context.get_active_sim()
            main_si = active.sim_info if active else None
        if not main_si:
            output("[Claude AI] No sim found to debug.")
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
        result = notifications._show_game_notification("Claude AI Test", "If you see this popup, notifications work!")
        output(f"[Debug] Notification result: {result}")

        output("[Debug] Use claude.debugsim <First> <Last> to see a sim's prompt context")
        output("[Debug] Use claude.testbuffs to test the moodlet system")

    @sims4.commands.Command("claude.debugsim", command_type=sims4.commands.CommandType.Live)
    def cmd_debug_sim(first_name: str = None, last_name: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Claude AI] Usage: claude.debugsim <First> <Last>")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        contact = phone.find_contact_by_name(full_name)
        if not contact:
            output(f"[Claude AI] Could not find '{full_name}'.")
            return
        desc = phone._describe_relationship(contact)
        output(f"=== Prompt context for {contact['name']} ===")
        output(desc)

    @sims4.commands.Command("claude.testbuffs", command_type=sims4.commands.CommandType.Live)
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

    @sims4.commands.Command("claude.journal", command_type=sims4.commands.CommandType.Live)
    def cmd_journal(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        text = journal.format_recent_for_display(n=10)
        output(text)

    @sims4.commands.Command("claude.journal_sim", command_type=sims4.commands.CommandType.Live)
    def cmd_journal_sim(first_name: str = None, last_name: str = None, _connection=None):
        output = sims4.commands.CheatOutput(_connection)
        if not first_name:
            output("[Claude AI] Usage: claude.journal_sim <FirstName> <LastName>")
            return
        full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        entries = journal.get_sim_history(full_name, n=10)
        if not entries:
            output(f"[Claude AI] No journal entries for {full_name}.")
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

    @sims4.commands.Command("claude.journal_clear", command_type=sims4.commands.CommandType.Live)
    def cmd_journal_clear(_connection=None):
        output = sims4.commands.CheatOutput(_connection)
        count = journal.get_entry_count()
        journal.clear()
        output(f"[Claude AI] Journal cleared ({count} entries deleted).")

except Exception as e:
    # Running outside the Sims 4 game environment, or an error during load
    try:
        import sims4.commands
        sims4.commands.output(f"[Claude AI] Commands failed to register: {type(e).__name__}: {e}", None)
    except Exception:
        pass
