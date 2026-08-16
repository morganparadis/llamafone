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


def _open_url_in_browser(url):
    """Open a URL in the player's default browser. Layered fallback --
    webbrowser is stdlib but may not always work in Sims 4's embedded
    Python; os.startfile is the reliable Windows path; subprocess
    covers macOS and Linux Proton."""
    try:
        import webbrowser
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    try:
        import os
        os.startfile(url)  # noqa: E501 -- Windows-only, tested first
        return True
    except Exception:
        pass
    try:
        import subprocess, sys
        cmd = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
        subprocess.Popen(cmd)
        return True
    except Exception:
        return False


def show_update_prompt(current_version, latest_version, url):
    """Show an in-game Update dialog with 'Update now' / 'Later' buttons.
    'Update now' launches the URL in the player's default browser.
    Falls back to a plain notification with the URL when the dialog
    can't be constructed (early load / no client / API not ready)."""
    title = "Llamafone update available"
    message = (
        f"A newer version is available on CurseForge.\n\n"
        f"You have:   v{current_version}\n"
        f"Latest:     v{latest_version}\n\n"
        f"Click Update now to open the download page in your browser."
    )
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog import UiDialogOkCancel
        import services

        client = services.client_manager().get_first_client()
        if not client:
            raise RuntimeError("no active client")
        sim_info = client.active_sim_info
        if not sim_info:
            raise RuntimeError("no active sim on client")

        loc_title = LocalizationHelperTuning.get_raw_text(title)
        loc_text = LocalizationHelperTuning.get_raw_text(message)
        loc_ok = LocalizationHelperTuning.get_raw_text("Update now")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Later")

        dialog = UiDialogOkCancel.TunableFactory().default(
            sim_info,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_ok,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_response(response_dialog):
            try:
                if response_dialog.accepted:
                    _open_url_in_browser(url)
            except Exception:
                pass

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception:
        # Dialog path failed -- fall back to a plain notification so
        # the player still sees the update news, just without buttons.
        show(title, f"{message}\n\n{url}")
        return False


def show_result(feature_name, text, output=None):
    show(feature_name, text, output=output)
