"""
Displays text to the player via Sims 4 UI notifications.
Falls back to cheat console output if the UI isn't available.
"""

_NOTIFICATION_MAX_CHARS = 800


def _truncate(text, max_chars=_NOTIFICATION_MAX_CHARS):
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "... [See cheat console for full text]"


def _show_game_notification(title, message):
    """
    Show an in-game notification popup (top-right notification panel).
    Uses the same pattern as MC Command Center.
    Returns True on success, False if unavailable.
    """
    display_text = _truncate(message)

    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_notification import UiDialogNotification
        import services

        client = services.client_manager().get_first_client()
        if not client:
            return False

        # Anchor to protagonist if set, otherwise active sim.
        # Toddlers/kids/pets shouldn't appear to receive calls/texts.
        sim_info = None
        try:
            from . import sim_context
            sim_info = sim_context.get_main_sim_info()
        except Exception:
            pass
        if not sim_info:
            sim_info = client.active_sim_info
        if not sim_info:
            return False

        # Build localized text as lambdas (not lambda **_:, just lambda:)
        loc_text = LocalizationHelperTuning.get_raw_text(display_text)
        loc_title = LocalizationHelperTuning.get_raw_text(title)

        notification = UiDialogNotification.TunableFactory().default(
            sim_info,
            text=lambda: loc_text,
            title=lambda: loc_title,
        )
        notification.show_dialog()
        return True
    except Exception:
        pass

    return False


def show(title, message, output=None):
    """
    Show a message to the player.
    Tries the in-game notification popup first.
    Always echoes to the cheat console so nothing is lost.
    """
    _show_game_notification(title, message)

    full_text = f"[Llamafone - {title}]\n{message}"
    if output:
        output(full_text)
    else:
        try:
            import sims4.commands
            sims4.commands.output(full_text, None)
        except Exception:
            pass


def show_error(message, output=None):
    show("Error", message, output=output)


def show_result(feature_name, text, output=None):
    show(feature_name, text, output=output)
