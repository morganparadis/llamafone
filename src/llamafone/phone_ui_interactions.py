"""
Phone-wheel interactions for Llamafone.

We route through the in-game Social category on the phone (instance
0x19DBF, "phoneCategory_Social") instead of authoring our own custom
PieMenuCategory tile -- multiple invisible client-side filters made the
custom tile unreliable, and Social is a fine home for Call/Text/Settings.

Each interaction is a SuperInteraction (not ImmediateSuperInteraction --
the phone-wheel filter silently drops Immediate variants). The base-class
animation plays the brief phone gesture; our Python opens the player-input
dialog and fires the actual gameplay action via `_run_interaction_gen`.

Call/Text are OUTBOUND:
  1. Show a UiSimPicker scoped to the player's known sims, just like
     the base-game "text someone" flow.
  2. After selection, show a UiDialogTextInputOkCancel for the message.
  3. Dispatch to phone.send_call / phone.send_text.

If the picker fails to construct (rare -- e.g. the player has zero
known sims), the user gets a notification explaining why.
"""
import os
import datetime
import traceback

from interactions.base.super_interaction import SuperInteraction

from . import phone, notifications, config, sim_context


_MESSAGE_INPUT = "message"


def _log(msg):
    """Append to the mod log so we can debug what's happening in-game."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [phone_ui_interact] {msg}\n")
    except Exception:
        pass


def _log_exc(label):
    """Log the current traceback under a label."""
    try:
        _log(f"{label}: {traceback.format_exc()}")
    except Exception:
        pass


def _claude_ready():
    """Make sure the mod is configured before we trigger an API call."""
    try:
        if not config.get_api_key():
            notifications.show_error(
                "Claude AI is not configured yet. Open Documents/"
                "ClaudeAI/config.json and add your Anthropic API key, "
                "then reload the save."
            )
            return False
    except Exception:
        pass
    return True


def _gather_contact_choices(main_sim_info):
    """
    Build a list of (sim_id, contact_dict) pairs for sims the player can
    plausibly contact. Uses sim_context.get_main_sim_network() so the
    list matches what llama.text / llama.sendtext already consider --
    teen+ household + relationships above the friendship threshold.
    """
    if not main_sim_info:
        return []
    try:
        hh_members, relationships = sim_context.get_main_sim_network(main_sim_info)
    except Exception:
        hh_members, relationships = [], []
    out = []
    seen_ids = set()
    main_id = getattr(main_sim_info, "sim_id", None)
    for contact in list(hh_members) + list(relationships):
        si = contact.get("sim_info")
        if si is None:
            continue
        sid = getattr(si, "sim_id", None)
        if sid is None or sid == main_id or sid in seen_ids:
            continue
        seen_ids.add(sid)
        out.append((sid, contact))
    return out


def _show_message_input(kind, anchor_sim, contact, on_submit):
    """Open a single-field text-input dialog for the message body.
    Same protobuf-injection trick as the Reply dialog in phone.py."""
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel
        from distributor.shared_messages import IconInfoData

        verb = "Call" if kind == "call" else "Text"
        other_name = contact.get("name", "this sim")

        loc_title = LocalizationHelperTuning.get_raw_text(f"{verb} {other_name}")
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"What do you want to {verb.lower()} {other_name} about?"
        )
        loc_send = LocalizationHelperTuning.get_raw_text("Send")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        class _MessageDialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _MESSAGE_INPUT
                # Make this a multi-line text area instead of a single-line
                # box -- the player is composing a message body, not a name.
                # The protobuf's `height` field is what the client uses to
                # render a taller text area; UiTextInput tunings set this
                # via the FACTORY_TUNABLES `height` range, and we can write
                # it directly on the protobuf.
                ti.height = 100
                return msg

        dialog = _MessageDialog.TunableFactory().default(
            anchor_sim,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_send,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    return
                message = (response_dialog.text_input_responses or {}).get(
                    _MESSAGE_INPUT, ""
                ).strip()
                if not message:
                    return
                on_submit(message)
            except Exception:
                pass

        dialog.add_listener(_on_response)
        # Show the recipient's portrait alongside the message dialog so the
        # player has visual confirmation they're writing to the right sim.
        try:
            recipient_si = contact.get("sim_info")
            icon = IconInfoData(obj_instance=recipient_si) if recipient_si else None
            if icon is not None:
                dialog.show_dialog(icon_override=icon)
            else:
                dialog.show_dialog()
        except Exception:
            dialog.show_dialog()
        return True
    except Exception:
        return False


def _show_recipient_picker(kind, anchor_sim, on_picked):
    """
    Open a UiSimPicker scoped to the player's contacts. On selection,
    calls `on_picked(contact_dict)`. Returns True on dialog construction
    success, False otherwise (callers should report a friendly error).
    """
    _log(f"_show_recipient_picker(kind={kind}, anchor_sim={getattr(anchor_sim, 'first_name', '?')})")
    choices = _gather_contact_choices(anchor_sim)
    _log(f"  gathered {len(choices)} contact choices")
    if not choices:
        notifications.show_error(
            "You don't have any contacts to call or text yet. Meet "
            "some sims first, then come back here."
        )
        return False

    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiSimPicker, SimPickerRow
        _log("  imported UiSimPicker, SimPickerRow")

        verb = "Call" if kind == "call" else "Text"
        loc_title = LocalizationHelperTuning.get_raw_text(f"Who to {verb.lower()}?")
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"Pick a sim to {verb.lower()}. Claude will write the "
            f"{verb.lower()} once you tell it what to say."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text(verb)
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        try:
            dialog = UiSimPicker.TunableFactory().default(
                anchor_sim,
                title=lambda *_a, **_kw: loc_title,
                text=lambda *_a, **_kw: loc_text,
                text_ok=lambda *_a, **_kw: loc_ok,
                text_cancel=lambda *_a, **_kw: loc_cancel,
            )
            _log("  UiSimPicker dialog constructed")
        except Exception:
            _log_exc("  UiSimPicker.TunableFactory().default() failed")
            return False

        # Overlay sensible runtime values for things the tuning would
        # normally lock down (single-select, no min). The factory's
        # defaults already cover columns/cell type etc.
        try:
            dialog.max_selectable = 1
        except Exception:
            pass
        try:
            dialog.min_selectable = 1
        except Exception:
            pass

        # BasePickerRow.option_id maps to a protobuf u32 field; a raw
        # sim_id (64-bit) overflows and the dialog never opens. Use a
        # small per-row index as option_id instead, and remember the
        # contact in a dict keyed by that index.
        option_to_contact = {}
        added = 0
        for idx, (sid, contact) in enumerate(choices):
            try:
                row = SimPickerRow(sid)
                row.option_id = idx
                dialog.add_row(row)
                option_to_contact[idx] = contact
                added += 1
            except Exception:
                _log_exc(f"  add_row failed for sid={sid}")
                continue
        _log(f"  added {added}/{len(choices)} picker rows")

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    return
                picked = list(getattr(response_dialog, "picked_results", None) or ())
                if not picked:
                    return
                # picked_results contains the option_id of the chosen row.
                # We mapped that to the small index 0..N-1, so look up
                # the corresponding contact from our index dict.
                contact = option_to_contact.get(picked[0])
                if not contact:
                    notifications.show_error(
                        "Couldn't resolve the selected sim. Try again."
                    )
                    return
                on_picked(contact)
            except Exception:
                pass

        try:
            dialog.add_listener(_on_response)
            dialog.show_dialog()
            _log("  show_dialog() returned")
            return True
        except Exception:
            _log_exc("  show_dialog() failed")
            return False
    except Exception:
        _log_exc("_show_recipient_picker outer failure")
        return False


class _ClaudePhoneInteractionBase(SuperInteraction):
    """Shared shell: play the brief phone animation, then fire our action.

    Subclasses override `_fire()` with the actual gameplay call. We yield
    from the base `_run_interaction_gen` so the sim cleanly completes the
    phone gesture regardless of how long the underlying API call takes
    (the API call is async).
    """

    def _fire(self):
        raise NotImplementedError

    def _run_interaction_gen(self, timeline):
        _log(f"{type(self).__name__}._run_interaction_gen() entered, sim={getattr(self.sim, 'first_name', '?')}")
        try:
            ready = _claude_ready()
            _log(f"  _claude_ready() -> {ready}")
            if ready:
                self._fire()
                _log(f"  _fire() returned")
        except Exception:
            _log_exc(f"{type(self).__name__}._run_interaction_gen / _fire failed")
        result = yield from super()._run_interaction_gen(timeline)
        return result


def _start_outbound(kind, sim_info):
    """Recipient picker -> message dialog -> phone.send_call / send_text."""
    def _on_recipient(contact):
        def _on_message(message):
            if kind == "call":
                phone.send_call(contact, message)
            else:
                phone.send_text(contact, message)
        if not _show_message_input(kind, sim_info, contact, _on_message):
            notifications.show_error("Couldn't open the message dialog.")

    if not _show_recipient_picker(kind, sim_info, _on_recipient):
        # Picker reported its own error via notifications.show_error if
        # the cause was "no contacts"; only show a generic fallback if
        # construction itself failed.
        return


class LlamafoneCallInteraction(_ClaudePhoneInteractionBase):
    """Phone > Social > Claude Call -- pick a recipient, type a topic,
    Claude crafts and delivers the call."""

    def _fire(self):
        sim_info = getattr(self.sim, "sim_info", None) or self.sim
        _start_outbound("call", sim_info)


class LlamafoneTextInteraction(_ClaudePhoneInteractionBase):
    """Phone > Social > Claude Text -- pick a recipient, type a message,
    Claude crafts and sends the text."""

    def _fire(self):
        sim_info = getattr(self.sim, "sim_info", None) or self.sim
        _start_outbound("text", sim_info)


# ---------------------------------------------------------------------------
# Settings UI -- a picker dialog listing the runtime-toggleable settings and
# their current values. Tapping a row toggles a bool / opens a text-input
# dialog for a numeric value, then the picker re-opens to show the update.
#
# All values write to Llamafone_Settings.json via config.set_setting(), which
# the existing get_*() helpers in config.py and auto_events.py prefer over
# the static .cfg file. Static values (API key, model names, language) are
# intentionally left out -- those still live in llamafone.cfg because
# editing them in-game adds little value and increases the surface area for
# bad input.
# ---------------------------------------------------------------------------


def _setting_definitions():
    """Schema for the settings picker. Each entry describes one tunable:
      key        -- the JSON key used by config.set_setting / get_setting
      label      -- human title shown in the row name
      kind       -- 'bool' or 'int'
      getter     -- () -> current value (after runtime overrides)
      bounds     -- (min, max) for ints, ignored for bools
      hint       -- subtitle shown under the row
    Defined as a function so config / auto_events get re-read each open.
    """
    from . import auto_events
    return [
        {
            "key":    "auto_events_enabled",
            "label":  "Auto events: {value}",
            "kind":   "bool",
            "getter": auto_events.is_enabled,
            "hint":   "When on, the mod will spontaneously fire calls/texts/events while you play.",
        },
        {
            "key":    "auto_event_chance",
            "label":  "Auto-event chance: {value}%",
            "kind":   "int",
            "getter": auto_events.get_chance,
            "bounds": (0, 100),
            "hint":   "Percent chance each tick fires something (0-100).",
        },
        {
            "key":    "auto_event_interval_minutes",
            "label":  "Auto-event interval: {value} min",
            "kind":   "int",
            "getter": lambda: int(auto_events.get_interval_seconds() / 60),
            "bounds": (5, 600),
            "hint":   "Real-world minutes between auto-event checks (min 5).",
        },
        {
            "key":    "phone_allow_ghosts",
            "label":  "Ghost sims on phone: {value}",
            "kind":   "bool",
            "getter": config.get_phone_allow_ghosts,
            "hint":   "When off, ghost sims are filtered out of contact pickers and auto-call/auto-text pools.",
        },
        {
            "key":    "reply_delay_enabled",
            "label":  "Reply delay: {value}",
            "kind":   "bool",
            "getter": config.get_reply_delay_enabled,
            "hint":   "When on, sims pause a few seconds before replying to texts.",
        },
        {
            "key":    "reply_delay_min_seconds",
            "label":  "Reply delay min: {value}s",
            "kind":   "int",
            "getter": config.get_reply_delay_min_seconds,
            "bounds": (0, 600),
            "hint":   "Minimum delay before a sim replies (seconds).",
        },
        {
            "key":    "reply_delay_max_seconds",
            "label":  "Reply delay max: {value}s",
            "kind":   "int",
            "getter": config.get_reply_delay_max_seconds,
            "bounds": (0, 600),
            "hint":   "Maximum delay before a sim replies (seconds).",
        },
    ]


def _format_value(setting):
    """Render the current value for display in the row name."""
    try:
        val = setting["getter"]()
    except Exception:
        val = "?"
    if setting["kind"] == "bool":
        val = "ON" if val else "OFF"
    return setting["label"].format(value=val)


def _show_settings_picker(anchor_sim):
    """Open a UiItemPicker listing each toggleable setting + current value.
    Picking a row calls _on_setting_picked which either flips a bool or
    opens a text-input for a numeric value, then re-opens the picker."""
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiItemPicker, BasePickerRow

        settings = _setting_definitions()
        try:
            from . import MOD_VERSION as version
        except Exception:
            version = "?"

        loc_title = LocalizationHelperTuning.get_raw_text(f"Claude AI Settings (v{version})")
        loc_text = LocalizationHelperTuning.get_raw_text(
            "Pick a setting to change. Toggles flip on/off; numeric "
            "settings open a text box. Changes save instantly and apply "
            "without reloading. Static settings (API key, model, language) "
            "still live in llamafone.cfg."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Change")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Done")

        dialog = UiItemPicker.TunableFactory().default(
            anchor_sim,
            title=lambda *_a, **_kw: loc_title,
            text=lambda *_a, **_kw: loc_text,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )
        try:
            dialog.max_selectable = 1
        except Exception:
            pass
        try:
            dialog.min_selectable = 1
        except Exception:
            pass

        for idx, setting in enumerate(settings):
            try:
                name_text = _format_value(setting)
                hint_text = setting.get("hint", "")
                row = BasePickerRow(
                    option_id=idx,
                    name=LocalizationHelperTuning.get_raw_text(name_text),
                    row_description=LocalizationHelperTuning.get_raw_text(hint_text),
                    is_enable=True,
                )
                dialog.add_row(row)
            except Exception:
                _log_exc(f"settings: add_row {setting['key']} failed")
                continue

        def _on_settings_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    return
                picked = list(getattr(response_dialog, "picked_results", None) or ())
                if not picked:
                    return
                idx = picked[0]
                if idx is None or idx < 0 or idx >= len(settings):
                    return
                _on_setting_picked(anchor_sim, settings[idx])
            except Exception:
                _log_exc("settings: on_response failed")

        dialog.add_listener(_on_settings_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_settings_picker outer failure")
        return False


def _on_setting_picked(anchor_sim, setting):
    """Handler for a picked row -- either toggles a bool or opens a
    numeric text-input dialog, then re-opens the settings picker so the
    player can see the change and keep editing."""
    kind = setting["kind"]
    if kind == "bool":
        try:
            current = bool(setting["getter"]())
        except Exception:
            current = False
        config.set_setting(setting["key"], not current)
        # Re-open the picker to show the new state.
        _show_settings_picker(anchor_sim)
        return
    if kind == "int":
        _show_int_input(anchor_sim, setting)
        return


def _show_int_input(anchor_sim, setting):
    """Open a text-input dialog to edit a numeric setting. Same protobuf
    injection trick as the Reply / outbound-text dialogs."""
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel

        try:
            current = setting["getter"]()
        except Exception:
            current = ""
        lo, hi = setting.get("bounds", (None, None))
        range_hint = ""
        if lo is not None and hi is not None:
            range_hint = f" (allowed: {lo}-{hi})"

        loc_title = LocalizationHelperTuning.get_raw_text(setting["label"].format(value=current))
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"{setting.get('hint', '')}\nCurrent: {current}{range_hint}\n"
            f"Enter a new value:"
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Save")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        _FIELD = "value"

        class _IntInputDialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _FIELD
                return msg

        dialog = _IntInputDialog.TunableFactory().default(
            anchor_sim,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_input(response_dialog):
            try:
                if not response_dialog.accepted:
                    # Re-open the picker so the player isn't stranded.
                    _show_settings_picker(anchor_sim)
                    return
                raw = (response_dialog.text_input_responses or {}).get(_FIELD, "").strip()
                try:
                    new_val = int(raw)
                except Exception:
                    notifications.show_error(
                        f"'{raw}' isn't a number. {setting['label']} unchanged."
                    )
                    _show_settings_picker(anchor_sim)
                    return
                if lo is not None and new_val < lo:
                    new_val = lo
                if hi is not None and new_val > hi:
                    new_val = hi
                config.set_setting(setting["key"], new_val)
                _show_settings_picker(anchor_sim)
            except Exception:
                _log_exc(f"settings: int input handler for {setting['key']} failed")

        dialog.add_listener(_on_input)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_int_input failure")
        return False


class LlamafoneSettingsInteraction(_ClaudePhoneInteractionBase):
    """Phone > Social > Claude Settings -- opens an in-game settings
    panel listing the runtime-toggleable settings (auto events on/off,
    chance, interval, reply delay). Selecting a row flips a bool or
    opens a text-input dialog for a numeric value; changes are saved
    to Llamafone_Settings.json and apply immediately without reloading
    the save."""

    def _fire(self):
        sim_info = getattr(self.sim, "sim_info", None) or self.sim
        _show_settings_picker(sim_info)
