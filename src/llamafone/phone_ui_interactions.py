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

from . import phone, notifications, config, sim_context, dating


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


def _mod_ready():
    """Make sure the mod is configured before we trigger an API call."""
    try:
        if not config.is_configured():
            notifications.show_error(
                "Llamafone is not configured yet. Open llamafone.cfg in "
                "your Mods folder, pick a provider, and add your API key "
                "(or use provider=ollama for no-key local AI). Then run "
                "llama.reload."
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
        loc_text = LocalizationHelperTuning.get_raw_text("")
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
        loc_text = LocalizationHelperTuning.get_raw_text("")
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


class _LlamafonePhoneInteractionBase(SuperInteraction):
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
            ready = _mod_ready()
            _log(f"  _mod_ready() -> {ready}")
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
        _log(f"_start_outbound picked kind={kind} name={contact.get('name','?')} sid={getattr(contact.get('sim_info'), 'sim_id', '?')}")
        def _on_message(message):
            _log(f"_start_outbound dispatching kind={kind} to name={contact.get('name','?')}")
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


# ---------------------------------------------------------------------------
# Multi-select text flow (v3.4.0): the "Send Text" phone entry uses a
# picker that permits 1-N recipients. 1 selected routes to the existing
# 1:1 send_text (unchanged); 2+ routes to send_group_text.
#
# Call stays single-select via the original _start_outbound above --
# group calls are out of scope for this feature.
# ---------------------------------------------------------------------------


def _get_group_max_participants():
    """Config-driven cap on group text size. Hard-capped at 8 in code
    even if the config value is higher, to keep API costs bounded."""
    try:
        raw = int(config.get_setting("group_text_max_participants", 4))
    except Exception:
        raw = 4
    return max(2, min(8, raw))


def _show_recipient_picker_multi(sim_info, max_pick, on_picked_list):
    """Multi-select variant of _show_recipient_picker. Callback gets a
    LIST of contact dicts (1..max_pick). Returns True on dialog
    construction, False otherwise. Same picker construction as the
    single-select version, differences flagged inline."""
    _log(f"_show_recipient_picker_multi(max_pick={max_pick}, anchor={getattr(sim_info, 'first_name', '?')})")
    choices = _gather_contact_choices(sim_info)
    if not choices:
        notifications.show_error(
            "You don't have any contacts to text yet. Meet some sims "
            "first, then come back here."
        )
        return False

    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiSimPicker, SimPickerRow

        loc_title = LocalizationHelperTuning.get_raw_text("Text")
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"Pick 1 for a text, or up to {max_pick} for a group text."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Text")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        try:
            dialog = UiSimPicker.TunableFactory().default(
                sim_info,
                title=lambda *_a, **_kw: loc_title,
                text=lambda *_a, **_kw: loc_text,
                text_ok=lambda *_a, **_kw: loc_ok,
                text_cancel=lambda *_a, **_kw: loc_cancel,
            )
        except Exception:
            _log_exc("_show_recipient_picker_multi: default() failed")
            return False

        # Multi-select mode requires the CLIENT to render checkboxes.
        # UiDialogObjectPicker.multi_select returns True when either
        # min_selectable < 1 OR max_selectable_num > 1, but in practice
        # the client-side renderer keys off min_selectable=0 (MCCC's
        # multi-select dialogs all set this explicitly for the same
        # reason -- max_selectable alone isn't sufficient to flip the
        # UI from radio-buttons to checkboxes).
        #
        # Semantically we WANT min=1 (must pick at least one recipient),
        # but the game reserves that for single-select mode. We work
        # around by setting min=0 and validating in _on_response --
        # picked_results empty => user tapped OK with nothing selected,
        # treat as cancel.
        try:
            dialog.min_selectable = 0
        except Exception:
            pass
        try:
            dialog.max_selectable = int(max_pick)
        except Exception:
            pass
        try:
            dialog.max_selectable_num = int(max_pick)
        except Exception:
            pass

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
        _log(f"  added {added}/{len(choices)} picker rows (max_pick={max_pick})")

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    return
                picked = list(getattr(response_dialog, "picked_results", None) or ())
                if not picked:
                    return
                contacts = []
                for opt in picked:
                    c = option_to_contact.get(opt)
                    if c:
                        contacts.append(c)
                if not contacts:
                    notifications.show_error("Couldn't resolve the selected sims. Try again.")
                    return
                on_picked_list(contacts)
            except Exception as _e:
                # Log the error rather than swallowing it -- v3.3.1
                # taught us silent-swallow in a picker callback is what
                # hides "the mod appears frozen" bugs from users.
                _log_exc("_show_recipient_picker_multi._on_response failed")

        try:
            dialog.add_listener(_on_response)
            dialog.show_dialog()
            return True
        except Exception:
            _log_exc("_show_recipient_picker_multi.show_dialog failed")
            return False
    except Exception:
        _log_exc("_show_recipient_picker_multi outer failure")
        return False


def _show_group_message_input(sim_info, contacts, on_message):
    """Message-input dialog for a group text. Same dialog construction
    pattern as _show_message_input (subclass UiDialogTextInputOkCancel
    and inject the text_input protobuf directly) -- factory kwargs
    don't accept text_inputs, so we mirror the proven approach used by
    1:1 texts. Only the labels differ."""
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel

        names = [c.get("name", "?") for c in contacts]
        if len(names) == 2:
            recipient_label = f"{names[0]} and {names[1]}"
        else:
            recipient_label = ", ".join(names[:-1]) + f", and {names[-1]}"

        loc_title = LocalizationHelperTuning.get_raw_text(f"Group text to {recipient_label}")
        loc_text = LocalizationHelperTuning.get_raw_text("")
        loc_send = LocalizationHelperTuning.get_raw_text("Send")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        class _GroupMessageDialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _MESSAGE_INPUT
                ti.height = 100
                return msg

        dialog = _GroupMessageDialog.TunableFactory().default(
            sim_info,
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
                on_message(message)
            except Exception:
                _log_exc("_show_group_message_input._on_response failed")

        dialog.add_listener(_on_response)
        try:
            dialog.show_dialog()
        except Exception:
            _log_exc("_show_group_message_input.show_dialog failed")
            return False
        return True
    except Exception:
        _log_exc("_show_group_message_input outer failure")
        return False


def _start_outbound_text_or_group(sim_info):
    """Send-Text entry point: multi-select picker -> route by count.
    1 contact  -> phone.send_text (existing 1:1 path, unchanged)
    2+ contacts -> phone.send_group_text (new)."""
    max_pick = _get_group_max_participants()

    def _on_picked(contacts):
        picked_debug = ", ".join(
            f"{c.get('name','?')}#{getattr(c.get('sim_info'), 'sim_id', '?')}"
            for c in contacts
        )
        _log(f"_start_outbound_text_or_group picked {len(contacts)} contact(s): {picked_debug}")
        if len(contacts) == 1:
            # Route to existing 1:1 path exactly as _start_outbound would
            contact = contacts[0]
            def _on_message(message):
                _log(f"_start_outbound_text_or_group dispatching TEXT to name={contact.get('name','?')}")
                phone.send_text(contact, message)
            if not _show_message_input("text", sim_info, contact, _on_message):
                notifications.show_error("Couldn't open the message dialog.")
            return

        # Group text path
        def _on_group_message(message):
            _log(f"_start_outbound_text_or_group dispatching GROUP TEXT to {picked_debug}")
            phone.send_group_text(contacts, message)
        if not _show_group_message_input(sim_info, contacts, _on_group_message):
            notifications.show_error("Couldn't open the group-text message dialog.")

    if not _show_recipient_picker_multi(sim_info, max_pick, _on_picked):
        return


class LlamafoneCallInteraction(_LlamafonePhoneInteractionBase):
    """Phone > Social > Call Someone -- pick a recipient, type a topic,
    Llamafone crafts and delivers the call."""

    def _fire(self):
        sim_info = getattr(self.sim, "sim_info", None) or self.sim
        _start_outbound("call", sim_info)


def _group_text_feature_enabled():
    """Master toggle for the group-text feature. When off, Send Text
    reverts to the single-select-only picker exactly as pre-3.4.
    Default ON -- the feature ships enabled but users can disable it
    from the Settings menu if they don't want the multi-select UI."""
    try:
        return bool(config.get_setting("group_text_enabled", True))
    except Exception:
        return True


class LlamafoneTextInteraction(_LlamafonePhoneInteractionBase):
    """Phone > Social > Send Text -- pick 1 recipient for a direct text
    or up to N for a group text. Llamafone routes based on selection
    count (see _start_outbound_text_or_group). Falls back to the
    original single-select flow when group_text_enabled=False."""

    def _fire(self):
        sim_info = getattr(self.sim, "sim_info", None) or self.sim
        if _group_text_feature_enabled():
            _start_outbound_text_or_group(sim_info)
        else:
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
        {
            "key":    "group_text_enabled",
            "label":  "Group texts: {value}",
            "kind":   "bool",
            "getter": lambda: bool(config.get_setting("group_text_enabled", True)),
            "hint":   "When on, Send Text uses a multi-select picker (1 = normal text, 2+ = group text). When off, Send Text is single-select only.",
        },
        {
            "key":    "group_text_max_participants",
            "label":  "Group text max size: {value}",
            "kind":   "int",
            "getter": lambda: int(config.get_setting("group_text_max_participants", 4)),
            "bounds": (2, 8),
            "hint":   "Max recipients in a group text (2-8). Each recipient reply is a separate AI call.",
        },
        {
            "key":    "group_text_dropoff_enabled",
            "label":  "Group text dropoff: {value}",
            "kind":   "bool",
            "getter": lambda: bool(config.get_setting("group_text_dropoff_enabled", True)),
            "hint":   "When on, participants may silently skip a round after the first (gentle 20/40/60%).",
        },
        {
            "key":    "message_relationship_impact_enabled",
            "label":  "Messages affect relationships: {value}",
            "kind":   "bool",
            "getter": lambda: bool(config.get_setting("message_relationship_impact_enabled", True)),
            "hint":   "When on, phone messages nudge the actual friendship / romance scores. Warm exchange = small +friendship, hostile = -friendship, flirty = +romance, etc. Both directions of the pair get the same nudge. Hard-capped per message so no single text reshapes a relationship.",
        },
        {
            "key":    "message_relationship_max_delta",
            "label":  "Relationship impact cap: {value}",
            "kind":   "int",
            "getter": lambda: int(config.get_setting("message_relationship_max_delta", 3)),
            "bounds": (0, 15),
            "hint":   "Hard cap on friendship / romance change per message. Default 3 keeps a single message well below the game's own social-action deltas (3-8). 0 disables the effect without touching the toggle above.",
        },
        {
            "key":    "manage_contacts",
            "label":  "Manage contacts...",
            "kind":   "action",
            "getter": lambda: "",
            "hint":   "Set per-contact preferences: mute, paused ('asked for space'), priority, or freeform notes.",
        },
        {
            "key":    "dating_submenu",
            "label":  "Dating...",
            "kind":   "action",
            "getter": lambda: "",
            "hint":   "Turn dating suggestions on/off per household sim, and set how often they arrive. Dating is off by default for every sim -- you have to opt each one in.",
        },
        {
            "key":    "save_notes",
            "label":  "Save notes...",
            "kind":   "action",
            "getter": lambda: "",
            "hint":   "Free-form world context prepended to EVERY AI prompt in this save. Use for challenge rulesets, ongoing story context, or house rules the AI should treat as universally binding.",
        },
        {
            "key":    "sim_bios",
            "label":  "Sim bios...",
            "kind":   "action",
            "getter": lambda: "",
            "hint":   "Per-sim character notes / backstory. Injected into that sim's descriptor block anywhere they appear in a prompt. Distinct from the Llamadate bio (dating-facing) and the contact_prefs note (scoped to one relationship).",
        },
    ]


def _format_value(setting):
    """Render the current value for display in the row name."""
    try:
        val = setting["getter"]()
    except Exception:
        val = "?"
    kind = setting["kind"]
    if kind == "bool":
        val = "ON" if val else "OFF"
    elif kind == "enum":
        # Show value as-is; the enum-cycle handler stringifies before
        # writing so the getter always returns a display-ready string.
        val = str(val).upper() if val is not None else "?"
    elif kind == "preset_int":
        # Map the stored int back to its preset label. Pick the preset
        # whose value is closest to (but not exceeding) the current
        # value; a stored value larger than the biggest preset shows
        # the biggest preset's label. Keeps the display honest even
        # if a user edits the JSON directly.
        presets = setting.get("presets") or ()
        try:
            current = int(val)
        except Exception:
            current = 0
        chosen_label = presets[0][0] if presets else "?"
        for label, preset_val in presets:
            if current >= preset_val:
                chosen_label = label
        val = chosen_label
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

        loc_title = LocalizationHelperTuning.get_raw_text(f"Llamafone Settings (v{version})")
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


# Frequency presets shared between the sub-picker row and the setting
# writer. Duplicated in the format helper so the display label matches
# _on_setting_picked's cycle logic. Keep in sync.
_DATING_FREQUENCY_PRESETS = (
    ("Off",       0),
    ("Rarely",    10),
    ("Sometimes", 25),
    ("Often",     50),
)


def _current_dating_frequency_label():
    try:
        current = int(config.get_dating_cold_outreach_weight())
    except Exception:
        current = 0
    label = _DATING_FREQUENCY_PRESETS[0][0]
    for lbl, val in _DATING_FREQUENCY_PRESETS:
        if current >= val:
            label = lbl
    return label


# Cycle order for the per-sim gender preference. Default is 'anyone'
# (no filter) -- we don't auto-detect from CAS because that API is
# unreliable across pack combos. Players narrow the pool from here.
_GENDER_PREF_CYCLE = ("anyone", "men", "women")
_GENDER_PREF_LABELS = {
    "anyone": "Anyone",
    "men":    "Men",
    "women":  "Women",
}

# Full cycle order for the per-sim age preference. The picker filters
# this down based on the acting sim's own age band -- teens can only
# match teens, adults never match teens, so it makes no sense to
# offer 'teen only' to an adult (they'd get zero candidates).
_AGE_PREF_CYCLE_FULL = ("auto", "teen", "young_adult", "adult", "elder", "any")
_AGE_PREF_LABELS = {
    "auto":        "Auto (same age tier)",
    "teen":        "Teen only",
    "young_adult": "Young Adult only",
    "adult":       "Adult only",
    "elder":       "Elder only",
    "any":         "Any adult age",
}


def _age_pref_cycle_for_sim(sim_info):
    """Which age_pref values make sense to offer this sim. Teens
    only ever match teens (hard rule in dating._pass_age_pref), so
    the picker shows just 'auto' and 'teen'. Adults get every adult-
    tier option plus 'any'. If we can't determine the seeker's age,
    fall through to the full list (permissive)."""
    from . import dating as _d
    try:
        rank = _d._age_rank(sim_info)
    except Exception:
        rank = None
    if rank == 0:
        return ("auto", "teen")
    if rank is None:
        return _AGE_PREF_CYCLE_FULL
    return ("auto", "young_adult", "adult", "elder", "any")


def _extract_sim_info(anchor):
    """The Sims 4 interaction system hands us the Sim (game object)
    on `self.sim`; the sim_info is the persistent representation of
    that Sim's data. Some helpers pass one, some the other -- normalize
    so downstream code just gets sim_info."""
    if anchor is None:
        return None
    si = getattr(anchor, "sim_info", None)
    return si if si is not None else anchor


def _show_bio_edit_dialog(anchor_sim, sim_id):
    """Text-input dialog for editing the player's Llamadate bio for
    the current sim. Reuses the multi-line height trick from
    _show_message_input so the player has room to write a paragraph.
    On submit or cancel, re-opens the dating settings picker so the
    player stays in the flow."""
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel

        sim_info = _extract_sim_info(anchor_sim)
        first = getattr(sim_info, "first_name", "you")
        existing = dating.get_sim_player_bio(sim_id)

        loc_title = LocalizationHelperTuning.get_raw_text(f"{first}'s Llamadate bio")
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"Write a bio for {first}'s Llamadate profile. Matches you "
            "message will see this alongside your first text, so make "
            "it a good pitch. Leave blank to clear."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Save")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        _FIELD = "bio"

        class _BioInputDialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _FIELD
                # Multi-line height so a paragraph doesn't feel cramped.
                ti.height = 140
                if existing:
                    try:
                        ti.initial_value = LocalizationHelperTuning.get_raw_text(existing)
                    except Exception:
                        pass
                return msg

        dialog = _BioInputDialog.TunableFactory().default(
            anchor_sim,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_response(response_dialog):
            try:
                if response_dialog.accepted:
                    new_bio = (response_dialog.text_input_responses or {}).get(_FIELD, "")
                    dating.set_sim_player_bio(sim_id, new_bio)
            except Exception:
                _log_exc("bio edit on_response failed")
            # Re-open the settings picker whether they saved or canceled.
            _show_dating_settings_picker(anchor_sim)

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_bio_edit_dialog outer failure")
        _show_dating_settings_picker(anchor_sim)
        return False


# ---------------------------------------------------------------------------
# Save-level notes editor
# ---------------------------------------------------------------------------
#
# Single free-form text field, saved to <save>/SaveNotes.json via
# save_notes.set_notes(). Prepended to EVERY AI prompt as WORLD CONTEXT
# via api_client.call_ai_async, so downstream builders don't have to
# know about it. Meant for challenge rulesets and per-save flavor.


def _show_save_notes_dialog(anchor_sim):
    """Multi-line text input for the save-level notes. Same protobuf-
    injection trick as _show_bio_edit_dialog: build a subclass of
    UiDialogTextInputOkCancel that adds a text_input row of tunable
    height, so the player has room for a paragraph."""
    from . import save_notes
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel

        existing = save_notes.get_notes()

        loc_title = LocalizationHelperTuning.get_raw_text("Save notes")
        loc_text = LocalizationHelperTuning.get_raw_text(
            "Free-form world context. Prepended to every AI-generated "
            "call, text, story, and event in this save. Great for "
            "challenge rulesets, long-running narrative context, or "
            "house rules the AI should treat as universally binding. "
            "Leave blank to clear."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Save")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        _FIELD = "save_notes"

        class _SaveNotesDialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _FIELD
                ti.height = 180
                if existing:
                    try:
                        ti.initial_value = LocalizationHelperTuning.get_raw_text(existing)
                    except Exception:
                        pass
                return msg

        dialog = _SaveNotesDialog.TunableFactory().default(
            anchor_sim,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_response(response_dialog):
            try:
                if response_dialog.accepted:
                    new_notes = (response_dialog.text_input_responses or {}).get(_FIELD, "")
                    save_notes.set_notes(new_notes)
            except Exception:
                _log_exc("save-notes on_response failed")
            _show_settings_picker(anchor_sim)

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_save_notes_dialog outer failure")
        _show_settings_picker(anchor_sim)
        return False


# ---------------------------------------------------------------------------
# Per-sim bios picker + editor
# ---------------------------------------------------------------------------
#
# Two-level flow: pick a sim, then edit their bio.
# Level 1 picker lists the anchor sim + household + known contacts,
# sorted so sims with an existing bio surface first (matching the
# contact-manager pattern).


def _show_sim_bios_picker(anchor_sim):
    """Level 1: pick which sim to edit a bio for. Includes the anchor
    sim themselves (so the player can write their own protagonist's
    backstory), plus every eligible contact from the same network
    manage-contacts uses."""
    from . import sim_bios
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiItemPicker, BasePickerRow
    except Exception:
        _log_exc("_show_sim_bios_picker: import failed")
        return False

    anchor_id = getattr(anchor_sim, "sim_id", None) if anchor_sim else None
    anchor_name = ""
    if anchor_sim:
        try:
            anchor_name = f"{anchor_sim.first_name} {anchor_sim.last_name}".strip()
        except Exception:
            anchor_name = ""

    # Build the pick list: anchor first, then everyone in their network.
    entries = []  # (sim_id, sim_info, display_name)
    if anchor_id is not None and anchor_sim is not None:
        entries.append((anchor_id, anchor_sim, anchor_name or "(this sim)"))

    seen = {anchor_id} if anchor_id is not None else set()
    for sid, contact in _gather_contact_choices(anchor_sim):
        if sid in seen:
            continue
        seen.add(sid)
        entries.append((sid, contact.get("sim_info"), contact.get("name") or "?"))

    if not entries:
        notifications.show_error(
            "No sims to write bios for. Meet some sims first, then come back."
        )
        _show_settings_picker(anchor_sim)
        return False

    # Sort: bios-set first, then alphabetical. Anchor sim stays at the
    # very top regardless -- their bio is the most-frequently-referenced.
    def _sort_key(entry):
        sid, _si, name = entry
        is_anchor = 0 if sid == anchor_id else 1
        has_bio = 0 if sim_bios.get_bio(sid) else 1
        return (is_anchor, has_bio, (name or "").lower())
    entries = sorted(entries, key=_sort_key)

    try:
        loc_title = LocalizationHelperTuning.get_raw_text("Sim bios")
        loc_text = LocalizationHelperTuning.get_raw_text(
            "Pick a sim to edit their bio. Sims with a bio set are "
            "marked [bio] and surface first. Bios apply anywhere the "
            "sim appears in a prompt."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Open")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Back")

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

        idx_to_entry = {}
        for idx, (sid, si, name) in enumerate(entries):
            existing_bio = sim_bios.get_bio(sid)
            marker = "  [bio]" if existing_bio else ""
            name_text = f"{name}{marker}"
            desc_text = ""
            if existing_bio:
                desc_text = existing_bio[:80] + ("…" if len(existing_bio) > 80 else "")
            try:
                row = BasePickerRow(
                    option_id=idx,
                    name=LocalizationHelperTuning.get_raw_text(name_text),
                    row_description=LocalizationHelperTuning.get_raw_text(desc_text),
                    is_enable=True,
                )
                dialog.add_row(row)
                idx_to_entry[idx] = (sid, si, name)
            except Exception:
                _log_exc(f"_show_sim_bios_picker row {idx}")

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    _show_settings_picker(anchor_sim)
                    return
                picked_ids = list(response_dialog.picked_results or ())
                if not picked_ids:
                    _show_settings_picker(anchor_sim)
                    return
                picked = idx_to_entry.get(picked_ids[0])
                if not picked:
                    _show_settings_picker(anchor_sim)
                    return
                sid, si, name = picked
                _show_sim_bio_edit_dialog(anchor_sim, sid, name)
            except Exception:
                _log_exc("_show_sim_bios_picker on_response failed")
                _show_settings_picker(anchor_sim)

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_sim_bios_picker outer failure")
        _show_settings_picker(anchor_sim)
        return False


def _show_sim_bio_edit_dialog(anchor_sim, sim_id, sim_name):
    """Level 2: multi-line text input for a specific sim's bio.
    Distinct from _show_bio_edit_dialog which edits the Llamadate bio
    (dating-facing pitch); this one is for the private character
    context (backstory, motivations, secrets)."""
    from . import sim_bios
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel

        existing = sim_bios.get_bio(sim_id)
        display_name = sim_name or "this sim"

        loc_title = LocalizationHelperTuning.get_raw_text(f"{display_name} bio")
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"Character notes for {display_name}. Backstory, motivations, "
            "secrets, quirks — anything you want the AI to know about "
            f"who {display_name} really is. Shapes their voice wherever "
            "they show up in a prompt. Distinct from any Llamadate "
            "profile bio. Leave blank to clear."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Save")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        _FIELD = "sim_bio"

        class _SimBioDialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _FIELD
                ti.height = 180
                if existing:
                    try:
                        ti.initial_value = LocalizationHelperTuning.get_raw_text(existing)
                    except Exception:
                        pass
                return msg

        dialog = _SimBioDialog.TunableFactory().default(
            anchor_sim,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_response(response_dialog):
            try:
                if response_dialog.accepted:
                    new_bio = (response_dialog.text_input_responses or {}).get(_FIELD, "")
                    sim_bios.set_bio(sim_id, new_bio)
            except Exception:
                _log_exc("sim-bio on_response failed")
            _show_sim_bios_picker(anchor_sim)

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_sim_bio_edit_dialog outer failure")
        _show_sim_bios_picker(anchor_sim)
        return False


def _show_dating_settings_picker(anchor_sim):
    """Llamadate settings for the sim whose phone this is. Per-sim,
    not per-household: opening Llamadate on Ingrid's phone shows
    Ingrid's settings; opening it on Aksel's phone shows Aksel's.

    Rows:
      1. Llamadate: ON/OFF -- inbound cold outreach toggle for THIS sim
      2. Interested in: Auto (CAS) / Men / Women / Anyone
      3. Age range: Auto / Teen / Young Adult / Adult / Elder / Any
      4. Frequency: Off / Rarely / Sometimes / Often (global; affects
         all opted-in sims because it's the auto-events cadence knob)
    """
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiItemPicker, BasePickerRow

        sim_info = _extract_sim_info(anchor_sim)
        sim_id = getattr(sim_info, "sim_id", None)
        first = getattr(sim_info, "first_name", "you")
        entry = dating.get_sim_dating_entry(sim_id)

        loc_title = LocalizationHelperTuning.get_raw_text(f"Llamadate — {first}")
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"Turn Llamadate on to start getting messages from local "
            f"singles {first} hasn't met -- each text explains how they "
            "got the number. Filters below narrow the pool."
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

        # row_actions[idx] = (kind, payload)
        row_actions = []

        opted_in = entry.get("opted_in", False)
        row_actions.append(("toggle_optin", sim_id))
        dialog.add_row(BasePickerRow(
            option_id=len(row_actions) - 1,
            name=LocalizationHelperTuning.get_raw_text(
                f"Llamadate: {'ON' if opted_in else 'OFF'}"
            ),
            row_description=LocalizationHelperTuning.get_raw_text(
                f"When on, {first} occasionally receives a first-time "
                "text from an unmet sim. Off by default."
            ),
            is_enable=True,
        ))

        gp = entry.get("gender_pref", "anyone")
        gp_label = _GENDER_PREF_LABELS.get(gp, gp)
        row_actions.append(("cycle_gender", sim_id))
        dialog.add_row(BasePickerRow(
            option_id=len(row_actions) - 1,
            name=LocalizationHelperTuning.get_raw_text(f"Interested in: {gp_label}"),
            row_description=LocalizationHelperTuning.get_raw_text(
                "Filter your dating pool by gender."
            ),
            is_enable=True,
        ))

        ap = entry.get("age_pref", "auto")
        # Filter the offered age options to what makes sense for this
        # sim's age band -- teens see auto/teen only; adults see the
        # adult tiers (no teen). Prevents "0 candidates" states where
        # the user set a value that can never match anyone.
        age_cycle = _age_pref_cycle_for_sim(sim_info)
        # If the stored pref isn't in the cycle for this sim, snap it
        # to 'auto' so the display isn't lying.
        if ap not in age_cycle:
            ap = "auto"
        ap_label = _AGE_PREF_LABELS.get(ap, ap)
        row_actions.append(("cycle_age", sim_id))
        dialog.add_row(BasePickerRow(
            option_id=len(row_actions) - 1,
            name=LocalizationHelperTuning.get_raw_text(f"Age range: {ap_label}"),
            row_description=LocalizationHelperTuning.get_raw_text(
                "Filter your dating pool by age. Auto uses the default "
                "rule (teens match teens; adults match within one age "
                "tier of their own)."
            ),
            is_enable=True,
        ))

        # Player-written bio -- gets sent alongside every outbound
        # Llamadate intro so the recipient's LLM has context to react
        # to. Shows a short preview of the current bio in the row
        # label, or "(not set)" when empty.
        current_bio = (entry.get("bio") or "").strip()
        bio_preview = current_bio[:40] + ("..." if len(current_bio) > 40 else "") if current_bio else "(not set)"
        row_actions.append(("edit_bio", sim_id))
        dialog.add_row(BasePickerRow(
            option_id=len(row_actions) - 1,
            name=LocalizationHelperTuning.get_raw_text(f"Your bio: {bio_preview}"),
            row_description=LocalizationHelperTuning.get_raw_text(
                f"Write {first}'s Llamadate bio. This gets sent along "
                "with every intro you send, so your matches see who "
                "they're talking to. Tap to edit."
            ),
            is_enable=True,
        ))

        freq_label = _current_dating_frequency_label()
        row_actions.append(("cycle_frequency", None))
        dialog.add_row(BasePickerRow(
            option_id=len(row_actions) - 1,
            name=LocalizationHelperTuning.get_raw_text(f"Frequency: {freq_label}"),
            row_description=LocalizationHelperTuning.get_raw_text(
                "How often Llamadate texts arrive. Global setting -- "
                "Off silences the feature even for opted-in sims."
            ),
            is_enable=True,
        ))

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    _show_settings_picker(anchor_sim)
                    return
                picked = list(getattr(response_dialog, "picked_results", None) or ())
                if not picked:
                    _show_settings_picker(anchor_sim)
                    return
                idx = picked[0]
                if idx is None or idx < 0 or idx >= len(row_actions):
                    _show_dating_settings_picker(anchor_sim)
                    return
                kind, payload = row_actions[idx]
                if kind == "toggle_optin":
                    dating.set_sim_opted_in(payload, not dating.is_sim_opted_in(payload))
                elif kind == "cycle_gender":
                    current = dating.get_sim_gender_pref(payload)
                    try:
                        i = _GENDER_PREF_CYCLE.index(current)
                    except ValueError:
                        i = -1
                    dating.set_sim_gender_pref(payload, _GENDER_PREF_CYCLE[(i + 1) % len(_GENDER_PREF_CYCLE)])
                elif kind == "cycle_age":
                    current = dating.get_sim_age_pref(payload)
                    cycle = _age_pref_cycle_for_sim(_extract_sim_info(anchor_sim))
                    try:
                        i = cycle.index(current)
                    except ValueError:
                        i = -1
                    dating.set_sim_age_pref(payload, cycle[(i + 1) % len(cycle)])
                elif kind == "edit_bio":
                    _show_bio_edit_dialog(anchor_sim, payload)
                    return  # dialog reopens itself on submit / cancel
                elif kind == "cycle_frequency":
                    try:
                        current = int(config.get_dating_cold_outreach_weight())
                    except Exception:
                        current = 0
                    cur_idx = 0
                    for i, (_l, v) in enumerate(_DATING_FREQUENCY_PRESETS):
                        if current >= v:
                            cur_idx = i
                    next_idx = (cur_idx + 1) % len(_DATING_FREQUENCY_PRESETS)
                    _lbl, next_val = _DATING_FREQUENCY_PRESETS[next_idx]
                    config.set_setting("dating_cold_outreach_weight", int(next_val))
                _show_dating_settings_picker(anchor_sim)
            except Exception:
                _log_exc("dating settings: on_response failed")

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_dating_settings_picker outer failure")
        return False


def _on_setting_picked(anchor_sim, setting):
    """Handler for a picked row -- toggle a bool, open a numeric input,
    or invoke a named action. Re-opens the settings picker after so the
    player can keep editing."""
    kind = setting["kind"]
    if kind == "bool":
        try:
            current = bool(setting["getter"]())
        except Exception:
            current = False
        config.set_setting(setting["key"], not current)
        # Re-open whichever picker this setting belongs to.
        _reopen_home_for(anchor_sim, setting)
        return
    if kind == "int":
        _show_int_input(anchor_sim, setting)
        return
    if kind == "enum":
        # Cycle to the next value in the tuple. No sub-dialog: tapping
        # again just advances the cycle, and the row label re-renders
        # to show the new selection.
        values = setting.get("values") or ()
        if not values:
            _show_settings_picker(anchor_sim)
            return
        try:
            current = str(setting["getter"]()).lower()
        except Exception:
            current = ""
        try:
            idx = list(values).index(current)
        except ValueError:
            idx = -1
        next_val = values[(idx + 1) % len(values)]
        config.set_setting(setting["key"], next_val)
        # Re-open whichever picker the setting lives in. Dating rows
        # live in the dating sub-picker; everything else in the main
        # settings picker.
        _reopen_home_for(anchor_sim, setting)
        return
    if kind == "preset_int":
        # Cycle to the next preset value. Compares CURRENT int to the
        # preset values (not labels) so a stored 25 correctly advances
        # from "Sometimes" to "Often" even if the config was hand-
        # edited to a non-preset value like 27.
        presets = setting.get("presets") or ()
        if not presets:
            _reopen_home_for(anchor_sim, setting)
            return
        try:
            current = int(setting["getter"]())
        except Exception:
            current = 0
        # Find the preset closest to (but not exceeding) current; that's
        # the "logical" current selection.
        idx = 0
        for i, (_label, pval) in enumerate(presets):
            if current >= pval:
                idx = i
        next_idx = (idx + 1) % len(presets)
        _next_label, next_val = presets[next_idx]
        config.set_setting(setting["key"], int(next_val))
        _reopen_home_for(anchor_sim, setting)
        return
    if kind == "action":
        # Action items open a sub-flow. Route by key -- we intentionally
        # avoid a big generic dispatch table so each action stays wired
        # explicitly and dead entries can't linger silently.
        key = setting.get("key")
        if key == "manage_contacts":
            _show_contact_manager_picker(anchor_sim)
            return
        if key == "dating_submenu":
            _show_dating_settings_picker(anchor_sim)
            return
        if key == "save_notes":
            _show_save_notes_dialog(anchor_sim)
            return
        if key == "sim_bios":
            _show_sim_bios_picker(anchor_sim)
            return
        # Unknown action -- log and re-open the picker
        _log(f"_on_setting_picked: unknown action key {key!r}")
        _show_settings_picker(anchor_sim)
        return


def _reopen_home_for(anchor_sim, setting):
    """After editing a setting row, re-open the picker it lives in.
    Dating rows are all handled by _show_dating_settings_picker
    directly (they don't go through _on_setting_picked), so every
    remaining setting belongs to the main picker."""
    _show_settings_picker(anchor_sim)


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


# ---------------------------------------------------------------------------
# Contact manager -- picks a contact, opens per-contact action picker,
# lets the player set state (muted/paused/priority/clear) or edit a
# freeform note. All three levels re-open their parent picker after a
# save so the player can chain edits without re-navigating.
#
# Reached via Phone > Social > Llamafone Settings > "Manage contacts..."
# for v1. A pie-menu-on-sim entry point (Phase 2) will call the same
# flow starting from _show_contact_actions(anchor_sim, contact).
# ---------------------------------------------------------------------------


def _show_contact_manager_picker(anchor_sim):
    """Level 1: pick which contact to manage. Row names show a short
    indicator for contacts that already have a state / note set, so
    the player can see at a glance what they've configured.

    Scoped to the anchor sim (the household member on whose phone this
    was opened). Preferences are (anchor_sim, contact_sim) pairs -- if
    another household member has different prefs for the same contact,
    they don't collide."""
    from . import contact_prefs
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiItemPicker, BasePickerRow
    except Exception:
        _log_exc("_show_contact_manager_picker: import failed")
        return False

    anchor_id = getattr(anchor_sim, "sim_id", None) if anchor_sim else None
    anchor_name = ""
    if anchor_sim:
        try:
            anchor_name = anchor_sim.first_name
        except Exception:
            anchor_name = ""

    choices = _gather_contact_choices(anchor_sim)
    if not choices:
        notifications.show_error(
            "You don't have any contacts yet. Meet some sims first, "
            "then come back here."
        )
        _show_settings_picker(anchor_sim)
        return False

    # Sort: contacts with prefs first, then alphabetical.
    def _sort_key(entry):
        sid, contact = entry
        e = contact_prefs.get_prefs(anchor_id, sid) or {}
        has_state = 0 if e.get("state") else 1
        has_note = 0 if e.get("note") else 1
        name = (contact.get("name") or "").lower()
        return (has_state, has_note, name)
    choices = sorted(choices, key=_sort_key)

    try:
        # Keep title short -- Sims 4 dialogs wrap long titles into the
        # description area on lower-resolution / non-ultrawide displays,
        # which collides with the close (X) button. Scope info goes in
        # the description body instead.
        loc_title = LocalizationHelperTuning.get_raw_text("Manage contacts")
        loc_text = LocalizationHelperTuning.get_raw_text(
            f"Preferences here apply only to {anchor_name or 'this sim'}. "
            f"Contacts with an existing state or note are marked in brackets."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Open")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Back")

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

        idx_to_choice = {}
        for idx, (sid, contact) in enumerate(choices):
            e = contact_prefs.get_prefs(anchor_id, sid) or {}
            state = e.get("state")
            note = e.get("note")
            markers = []
            if state:
                markers.append(state)
            if note:
                markers.append("note")
            marker = f"  [{', '.join(markers)}]" if markers else ""
            name_text = f"{contact.get('name', '?')}{marker}"
            desc_text = ""
            if note:
                # First 60 chars of the note as row description
                desc_text = note[:60] + ("…" if len(note) > 60 else "")

            try:
                row = BasePickerRow(
                    option_id=idx,
                    name=LocalizationHelperTuning.get_raw_text(name_text),
                    row_description=LocalizationHelperTuning.get_raw_text(desc_text),
                    is_enable=True,
                )
                dialog.add_row(row)
                idx_to_choice[idx] = (sid, contact)
            except Exception:
                _log_exc(f"contact_manager: add_row failed for sid={sid}")
                continue

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    # Back out to the settings picker
                    _show_settings_picker(anchor_sim)
                    return
                picked = list(getattr(response_dialog, "picked_results", None) or ())
                if not picked:
                    _show_settings_picker(anchor_sim)
                    return
                entry = idx_to_choice.get(picked[0])
                if not entry:
                    _show_settings_picker(anchor_sim)
                    return
                sid, contact = entry
                _show_contact_actions(anchor_sim, sid, contact)
            except Exception:
                _log_exc("contact_manager: on_response failed")

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_contact_manager_picker outer failure")
        return False


def _show_contact_actions(anchor_sim, sim_id, contact):
    """Level 2: for a chosen contact, pick an action. Scoped to the
    (anchor_sim, contact_sim) pair -- edits only affect this household
    member's view of this contact."""
    from . import contact_prefs
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiItemPicker, BasePickerRow
    except Exception:
        _log_exc("_show_contact_actions: import failed")
        return False

    anchor_id = getattr(anchor_sim, "sim_id", None) if anchor_sim else None
    anchor_name = ""
    if anchor_sim:
        try:
            anchor_name = anchor_sim.first_name
        except Exception:
            anchor_name = ""

    name = contact.get("name", "?")
    e = contact_prefs.get_prefs(anchor_id, sim_id) or {}
    current_state = e.get("state") or "no state"
    current_note = e.get("note") or ""

    # Assemble the "you're editing X" header text. Show the in-game
    # delta if a state was set (matches how the AI prompt sees it).
    header_bits = []
    if anchor_name:
        header_bits.append(f"Preferences: {anchor_name} -> {name}")
    else:
        header_bits.append(f"Contact: {name}")
    header_bits.append(f"Current state: {current_state}")
    since_ticks = e.get("state_since_ticks")
    if since_ticks is not None:
        try:
            now = contact_prefs._now_ingame_ticks()
            if now is not None and now >= since_ticks:
                delta = contact_prefs._format_ingame_delta(now - since_ticks)
                if delta:
                    header_bits.append(f"State set: {delta}")
        except Exception:
            pass
    if current_note:
        note_preview = current_note[:100] + ("…" if len(current_note) > 100 else "")
        header_bits.append(f"Note: {note_preview}")
    header_text = "\n".join(header_bits)

    # Action rows. Order: state changes first (most common), then note,
    # then clear-all last so it's not accidentally picked.
    actions = [
        ("muted",    "Mute",                "No auto-calls or auto-texts from them."),
        ("paused",   "Ask for space",       "5x fewer auto-events. AI knows you asked for space."),
        ("priority", "Favorite",            "2x more auto-events. AI treats them warmly."),
        ("clear",    "Clear state",         "Back to normal (keeps any freeform note)."),
        ("note",     "Edit note...",        "Freeform text about this contact, surfaced in AI prompts."),
        ("wipe",     "Wipe all preferences", "Remove state AND note. Full reset for this contact."),
    ]

    try:
        # Keep title short so it doesn't wrap into the close-button
        # region on smaller displays. Scope + current-state details
        # go in the description body below.
        loc_title = LocalizationHelperTuning.get_raw_text(name)
        loc_text = LocalizationHelperTuning.get_raw_text(header_text)
        loc_ok = LocalizationHelperTuning.get_raw_text("Apply")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Back")

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

        for idx, (key, label, hint) in enumerate(actions):
            # Highlight the current state so the player sees which one
            # is already active.
            display_label = label
            if key in ("muted", "paused", "priority") and current_state == key:
                display_label = f"{label}  ← current"
            try:
                row = BasePickerRow(
                    option_id=idx,
                    name=LocalizationHelperTuning.get_raw_text(display_label),
                    row_description=LocalizationHelperTuning.get_raw_text(hint),
                    is_enable=True,
                )
                dialog.add_row(row)
            except Exception:
                _log_exc(f"contact_actions: add_row failed for {key}")
                continue

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    _show_contact_manager_picker(anchor_sim)
                    return
                picked = list(getattr(response_dialog, "picked_results", None) or ())
                if not picked:
                    _show_contact_manager_picker(anchor_sim)
                    return
                idx = picked[0]
                if idx is None or idx < 0 or idx >= len(actions):
                    _show_contact_manager_picker(anchor_sim)
                    return
                key = actions[idx][0]
                scope_label = f"{anchor_name} -> {name}" if anchor_name else name
                if key in ("muted", "paused", "priority"):
                    contact_prefs.set_state(anchor_id, sim_id, key)
                    notifications.show(
                        "Llamafone",
                        f"{scope_label}: {key}.",
                    )
                    _show_contact_actions(anchor_sim, sim_id, contact)
                elif key == "clear":
                    contact_prefs.set_state(anchor_id, sim_id, None)
                    notifications.show(
                        "Llamafone",
                        f"Cleared state for {scope_label} (note preserved).",
                    )
                    _show_contact_actions(anchor_sim, sim_id, contact)
                elif key == "note":
                    _show_contact_note_input(anchor_sim, sim_id, contact)
                elif key == "wipe":
                    contact_prefs.clear_prefs(anchor_id, sim_id)
                    notifications.show(
                        "Llamafone",
                        f"Wiped all preferences for {scope_label}.",
                    )
                    _show_contact_manager_picker(anchor_sim)
                else:
                    _show_contact_manager_picker(anchor_sim)
            except Exception:
                _log_exc("contact_actions: on_response failed")

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_contact_actions outer failure")
        return False


def _show_contact_note_input(anchor_sim, sim_id, contact):
    """Level 3: text input for the freeform note. Same protobuf
    injection pattern as the other text-input dialogs."""
    from . import contact_prefs
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel
    except Exception:
        _log_exc("_show_contact_note_input: import failed")
        return False

    anchor_id = getattr(anchor_sim, "sim_id", None) if anchor_sim else None
    anchor_name = ""
    if anchor_sim:
        try:
            anchor_name = anchor_sim.first_name
        except Exception:
            anchor_name = ""

    name = contact.get("name", "?")
    existing_note = contact_prefs.get_note(anchor_id, sim_id) or ""

    # Short title to avoid wrap-into-close-button on smaller displays.
    # The description body carries the scope + full context.
    loc_title = LocalizationHelperTuning.get_raw_text(f"Note about {name}")
    scope_line = f"For {anchor_name}'s view only.\n" if anchor_name else ""
    loc_text = LocalizationHelperTuning.get_raw_text(
        f"{scope_line}"
        "Freeform text about this contact -- surfaced in AI prompts as "
        "context. E.g. 'kid's teacher', 'asked for space after breakup', "
        "'coworker on the marketing team'. Clear the text to remove."
    )
    loc_ok = LocalizationHelperTuning.get_raw_text("Save")
    loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

    _FIELD = "note"

    class _NoteInputDialog(UiDialogTextInputOkCancel):
        def on_text_input(self, text_input_name='', text_input=''):
            self.text_input_responses[text_input_name] = text_input
            return True

        def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
            msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
            ti = msg.text_input.add()
            ti.text_input_name = _FIELD
            # Multi-line -- notes can be a couple sentences.
            ti.height = 120
            # Seed the field with any existing note so edits are incremental.
            if existing_note:
                try:
                    ti.initial_value = LocalizationHelperTuning.get_raw_text(existing_note)
                except Exception:
                    pass
            return msg

    try:
        dialog = _NoteInputDialog.TunableFactory().default(
            anchor_sim,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )
    except Exception:
        _log_exc("_show_contact_note_input: dialog build failed")
        _show_contact_actions(anchor_sim, sim_id, contact)
        return False

    def _on_response(response_dialog):
        try:
            if not response_dialog.accepted:
                _show_contact_actions(anchor_sim, sim_id, contact)
                return
            new_note = (response_dialog.text_input_responses or {}).get(_FIELD, "").strip()
            contact_prefs.set_note(anchor_id, sim_id, new_note)
            scope_label = f"{anchor_name} -> {name}" if anchor_name else name
            if new_note:
                notifications.show("Llamafone", f"Note saved: {scope_label}.")
            else:
                notifications.show("Llamafone", f"Note cleared: {scope_label}.")
            _show_contact_actions(anchor_sim, sim_id, contact)
        except Exception:
            _log_exc("_show_contact_note_input: on_response failed")

    dialog.add_listener(_on_response)
    try:
        dialog.show_dialog()
    except Exception:
        _log_exc("_show_contact_note_input: show_dialog failed")
        _show_contact_actions(anchor_sim, sim_id, contact)
        return False
    return True


# ---------------------------------------------------------------------------
# Outbound Send-Intro flow (v3.5 dating)
# ---------------------------------------------------------------------------
#
# Player picks an unmet sim from a filtered sim picker, then writes their
# OWN intro text (the mod never generates outbound intros). On submit we
# establish a real Sims 4 relationship between the two sims and route the
# message through phone.send_text -- from that point the recipient is in
# the seeker's relationship tracker like any other contact.


def _gather_intro_target_choices(seeker_sim_info):
    """(sim_id, contact_dict) pairs for sims eligible as new-intro
    targets. Wraps dating.eligible_intro_targets_for(); kept here so
    the picker uses the same option_id -> contact index trick the
    existing recipient picker uses."""
    if not seeker_sim_info:
        return []
    try:
        candidates = dating.eligible_intro_targets_for(seeker_sim_info)
    except Exception:
        _log_exc("_gather_intro_target_choices: eligible_intro_targets_for raised")
        return []
    out = []
    seen = set()
    for contact in candidates:
        si = contact.get("sim_info")
        sid = getattr(si, "sim_id", None) if si is not None else None
        if sid is None or sid in seen:
            continue
        seen.add(sid)
        out.append((sid, contact))
    return out


def _show_intro_target_picker(seeker_sim_info, on_picked):
    """Sim picker filtered to unmet eligible dating candidates. Mirrors
    _show_recipient_picker's structure but pulls from the dating
    candidate pool instead of the seeker's existing contacts."""
    _log(f"_show_intro_target_picker(seeker={getattr(seeker_sim_info, 'first_name', '?')})")
    choices = _gather_intro_target_choices(seeker_sim_info)
    _log(f"  {len(choices)} intro-target choices")
    if not choices:
        notifications.show_error(
            "No eligible sims available to introduce yourself to. This "
            "usually means either the world has no unmet sims matching "
            "your CAS romantic preferences, or your active sim doesn't "
            "have romantic preferences set. Set them in Change Sim and "
            "try again."
        )
        return False
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_picker import UiSimPicker, SimPickerRow

        loc_title = LocalizationHelperTuning.get_raw_text("Llamadate")
        loc_text = LocalizationHelperTuning.get_raw_text(
            "Swipe through your feed. Someone catching your eye? Tap "
            "to peek at their profile before you slide into their DMs."
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("View profile")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Not tonight")

        dialog = UiSimPicker.TunableFactory().default(
            seeker_sim_info,
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

        option_to_contact = {}
        for idx, (sid, contact) in enumerate(choices):
            try:
                row = SimPickerRow(sid)
                row.option_id = idx
                dialog.add_row(row)
                option_to_contact[idx] = contact
            except Exception:
                _log_exc(f"intro-picker add_row failed for sid={sid}")
                continue

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    return
                picked = list(getattr(response_dialog, "picked_results", None) or ())
                if not picked:
                    return
                contact = option_to_contact.get(picked[0])
                if not contact:
                    notifications.show_error("Couldn't resolve the selected sim. Try again.")
                    return
                on_picked(contact)
            except Exception:
                _log_exc("intro-picker on_response failed")

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_intro_target_picker outer failure")
        return False


def _show_bio_dialog(seeker_sim_info, contact, bio_text, on_write_intro, on_back=None):
    """Present the picked sim's dating-app bio with two options:
      OK     -> proceed to the text-input dialog (calls on_write_intro).
      Cancel -> Back to the picker (calls on_back if provided) -- the
                player didn't like this profile and wants to swipe on.

    The bio text is shown in the dialog body; the sim's portrait is
    used as the dialog icon so the player has a visual anchor.
    """
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog import UiDialogOkCancel
        from distributor.shared_messages import IconInfoData

        target_si = contact.get("sim_info")
        target_name = contact.get("name", "this sim")
        loc_title = LocalizationHelperTuning.get_raw_text(f"{target_name}")
        loc_text = LocalizationHelperTuning.get_raw_text(
            bio_text if bio_text else
            "(no bio yet -- their profile is pretty sparse.)"
        )
        loc_ok = LocalizationHelperTuning.get_raw_text("Message")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Back")

        dialog = UiDialogOkCancel.TunableFactory().default(
            seeker_sim_info,
            title=lambda *_a, **_kw: loc_title,
            text=lambda *_a, **_kw: loc_text,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_response(response_dialog):
            try:
                if getattr(response_dialog, "accepted", False):
                    on_write_intro()
                elif on_back is not None:
                    on_back()
            except Exception:
                _log_exc("bio dialog on_response failed")

        dialog.add_listener(_on_response)
        try:
            icon = IconInfoData(obj_instance=target_si) if target_si is not None else None
            if icon is not None:
                dialog.show_dialog(icon_override=icon)
            else:
                dialog.show_dialog()
        except Exception:
            dialog.show_dialog()
        return True
    except Exception:
        _log_exc("_show_bio_dialog outer failure")
        return False


def _start_outbound_intro(sim_info):
    """Target picker -> generate bio -> bio dialog -> text-input ->
    establish relationship -> phone.send_text. Player writes their own
    intro; the mod never generates the outbound message body.

    The Back button on the bio dialog re-opens the target picker
    rather than dropping the player back to the phone home -- that
    matches what a real dating app does (swipe left != quit).
    """
    def _reopen_picker():
        _show_intro_target_picker(sim_info, _on_target)

    def _on_target(contact):
        target_si = contact.get("sim_info")
        _log(f"_start_outbound_intro picked target name={contact.get('name','?')} "
             f"sid={getattr(target_si, 'sim_id', '?')}")

        def _on_message(message):
            _log(f"_start_outbound_intro dispatching intro to {contact.get('name','?')}")
            # DO NOT establish the relationship on send. We wait for the
            # recipient's LLM reply and only add them to contacts if the
            # reply signals interest -- matches real dating-app behavior
            # where a rejection doesn't turn into an ongoing relationship.
            # Journal the outbound intro NOW so it's recorded even if the
            # reply LLM never comes back or fails. phone.send_text writes
            # a second, fuller entry when the reply arrives; the two
            # coexist (send record + full-exchange record) and give us
            # the outbound history either way.
            try:
                from . import journal
                seeker_name = getattr(sim_info, "first_name", "?")
                target_name = contact.get("name", "?")
                target_id = getattr(target_si, "sim_id", None) if target_si else None
                seeker_id = getattr(sim_info, "sim_id", None)
                journal.add_entry(
                    "text",
                    f"Sent Llamadate intro to {target_name}:\n"
                    f"{seeker_name}: {message}",
                    sim_name=target_name,
                    recipient_name=seeker_name,
                    sim_id=target_id,
                    recipient_id=seeker_id,
                )
            except Exception:
                _log_exc("journal.add_entry (outbound intro) raised")

            try:
                intro_suffix = dating.build_outbound_intro_suffix(sim_info, target_si)
            except Exception:
                _log_exc("build_outbound_intro_suffix raised")
                intro_suffix = None
            try:
                rel_override = dating.build_outbound_relationship_override(sim_info, target_si)
            except Exception:
                _log_exc("build_outbound_relationship_override raised")
                rel_override = None
            # When the player has authored a Llamadate bio, that bio is
            # the ONLY thing about the player's sim the recipient LLM
            # should see -- no career, no aspiration, no traits. When
            # they haven't, this returns None and the default
            # _describe_recipient block runs (the fallback behavior).
            try:
                recipient_override = dating.build_outbound_recipient_override(sim_info)
            except Exception:
                _log_exc("build_outbound_recipient_override raised")
                recipient_override = None

            def _on_reply_generated(reply_text, error):
                if error or not reply_text:
                    _log(f"outbound reply not generated (error={error!r}); "
                         "skipping relationship classify")
                    return
                try:
                    dating.classify_reply_and_maybe_establish(
                        seeker_si=sim_info,
                        target_si=target_si,
                        reply_text=reply_text,
                    )
                except Exception:
                    _log_exc("classify_reply_and_maybe_establish raised")

            phone.send_text(
                contact, message,
                callback=_on_reply_generated,
                prompt_suffix=intro_suffix,
                relationship_override=rel_override,
                recipient_override=recipient_override,
                first_contact=True,
            )

        def _open_intro_dialog():
            if not _show_message_input("text", sim_info, contact, _on_message):
                notifications.show_error(
                    "Couldn't open the text input dialog. Try again, and if "
                    "it keeps failing, check Llamafone_Log.txt."
                )

        def _on_bio(bio_text, error):
            # Callback fires from the api_client background thread. The
            # Sims 4 dialog API is threadsafe enough for direct calls
            # here -- phone.py's reply flow does the same thing from
            # the same callback context (see phone.py _show_reply).
            if error:
                _log(f"bio generation error: {error}; showing empty-bio dialog")
            shown = _show_bio_dialog(
                sim_info, contact, bio_text,
                on_write_intro=_open_intro_dialog,
                on_back=_reopen_picker,
            )
            if not shown:
                # Bio dialog failed to open -- fall back to going
                # straight to the intro so the flow doesn't dead-end.
                _open_intro_dialog()

        try:
            dating.generate_bio_for(target_si, _on_bio)
        except Exception:
            _log_exc("generate_bio_for raised synchronously")
            # If bio generation blows up synchronously, skip the bio
            # step entirely and go straight to the intro dialog rather
            # than leaving the player stranded.
            _open_intro_dialog()

    if not _show_intro_target_picker(sim_info, _on_target):
        # The picker itself already surfaced an error notification if
        # applicable; nothing more to do here.
        pass


class LlamafoneDatingInteraction(_LlamafonePhoneInteractionBase):
    """Llamafone > Send Intro -- outbound flow for meeting NEW sims.
    Opens a sim picker filtered to unmet, age/orientation-appropriate,
    uncommitted sims. Player picks one, writes their own intro text,
    and the mod establishes the relationship + routes the message
    through the normal Llamafone text pipeline."""

    def _fire(self):
        sim_info = getattr(self.sim, "sim_info", None) or self.sim
        _start_outbound_intro(sim_info)


class LlamafoneSettingsInteraction(_LlamafonePhoneInteractionBase):
    """Phone > Social > Settings -- opens the in-game settings
    panel listing the runtime-toggleable settings (auto events on/off,
    chance, interval, reply delay). Selecting a row flips a bool or
    opens a text-input dialog for a numeric value; changes are saved
    to Llamafone_Settings.json and apply immediately without reloading
    the save."""

    def _fire(self):
        sim_info = getattr(self.sim, "sim_info", None) or self.sim
        _show_settings_picker(sim_info)
