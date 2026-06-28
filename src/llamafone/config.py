"""
Configuration loader for Llamafone mod.
Reads from llamafone.cfg in the Mods folder.

Runtime settings (set via in-game commands) are stored separately in
Llamafone_Settings.json alongside the config file so that the config
file stays clean and user-edited. Settings in the JSON override config.
"""
import json
import os
import configparser

_config = None
_CONFIG_FILENAME = "llamafone.cfg"
_SETTINGS_FILENAME = "Llamafone_Settings.json"
_SECTION = "llamafone"


def _find_config_file():
    """Search for the config file in the Mods folder, then walk up from
    the script location as a dev-mode fallback. Returns the first
    existing file or None."""
    mods_folder = os.path.join(
        os.path.expanduser("~"), "Documents",
        "Electronic Arts", "The Sims 4", "Mods",
    )
    mods_path = os.path.join(mods_folder, _CONFIG_FILENAME)
    if os.path.isfile(mods_path):
        return os.path.abspath(mods_path)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for up in ("", "..", os.path.join("..", ".."), os.path.join("..", "..", "..")):
        path = os.path.join(script_dir, up, _CONFIG_FILENAME)
        if os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _settings_path():
    cfg = _find_config_file()
    if cfg:
        return os.path.join(os.path.dirname(cfg), _SETTINGS_FILENAME)
    return None


def _load_settings():
    path = _settings_path()
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_settings(data):
    path = _settings_path()
    if not path:
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def get_setting(key, fallback=None):
    """Read a runtime setting (set by in-game command). Falls back to config file.

    Reads Llamafone_Settings.json for backward compatibility with older
    installs that wrote there; the in-game Settings UI now writes
    changes back to llamafone.cfg directly so the JSON file ends up
    drained over time. Callers should still go through here because the
    JSON might hold values from older versions of the mod."""
    return _load_settings().get(key, fallback)


def set_setting(key, value):
    """Persist a setting change. Writes back to llamafone.cfg so the
    .cfg stays the single source of truth -- the comments the player
    added are preserved (we replace just the value on the matching line).
    Any leftover entry for this key in Llamafone_Settings.json is removed
    so the JSON layer doesn't shadow the new .cfg value."""
    cfg_ok = _set_cfg_value(key, value)
    # Drain any stale JSON value for this key so get_setting()'s JSON-
    # first lookup doesn't shadow the .cfg write.
    data = _load_settings()
    if key in data:
        del data[key]
        _save_settings(data)
    # Invalidate the cached configparser so the next get_config() reads
    # the new value off disk.
    if cfg_ok:
        reload_config()
    return cfg_ok


def _format_cfg_value(value):
    """Render a Python value as a string suitable for an INI line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _set_cfg_value(key, value, section=None):
    """Update `key = value` in the .cfg under [section], preserving all
    comments and unrelated lines. Appends the key to the end of the
    section if it isn't present. Returns True on success.

    `section` defaults to whichever section actually exists in the .cfg
    (`_SECTION`), so a v2 user with `[claude_ai]` gets writes
    INTO that section -- we don't sprinkle a stray `[llamafone]` header
    underneath their config. Fresh installs write to `[llamafone]`.

    We do this line-by-line instead of using configparser.write() so the
    player's comments / blank lines / inline notes survive untouched.
    """
    if section is None:
        section = _SECTION
    path = _find_config_file()
    if not path:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return False

    formatted = _format_cfg_value(value)
    target_header = f"[{section}]"
    in_section = False
    saw_target_section = False
    key_replaced = False
    last_section_line_idx = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Section header?
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                # leaving our section -- if we never found the key, we'll
                # insert it just before this header
                if not key_replaced:
                    insertion_idx = i
                    # Skip back over any trailing blank lines so we
                    # insert before the blank separator, not in it.
                    while insertion_idx > 0 and lines[insertion_idx - 1].strip() == "":
                        insertion_idx -= 1
                    lines.insert(insertion_idx, f"{key} = {formatted}\n")
                    key_replaced = True
                in_section = False
            if stripped == target_header:
                in_section = True
                saw_target_section = True
                last_section_line_idx = i
            continue
        if not in_section or key_replaced:
            continue
        # Comments / blank lines are kept as-is.
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        # key = value line?
        if "=" in stripped:
            line_key = stripped.split("=", 1)[0].strip()
            if line_key == key:
                # Preserve the leading whitespace on the original line.
                leading = line[: len(line) - len(line.lstrip())]
                lines[i] = f"{leading}{key} = {formatted}\n"
                key_replaced = True

    if not key_replaced:
        # Section exists but key wasn't there, OR section didn't exist.
        if saw_target_section:
            # Append after the last line of the section block.
            insert_at = len(lines)
            for i in range(last_section_line_idx + 1, len(lines)):
                if lines[i].lstrip().startswith("["):
                    insert_at = i
                    break
            lines.insert(insert_at, f"{key} = {formatted}\n")
        else:
            # Whole section missing -- append at end.
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
            lines.append(f"\n[{section}]\n{key} = {formatted}\n")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True
    except Exception:
        return False


def get_config():
    global _config
    if _config is None:
        _config = configparser.ConfigParser()
        path = _find_config_file()
        if path:
            _config.read(path)
    return _config


def reload_config():
    global _config
    _config = None
    return get_config()


def get_api_key():
    return get_config().get(_SECTION, "api_key", fallback="")


def get_provider():
    """Which AI provider the api_client should route to. One of:
      claude (default) -- Anthropic Messages API
      openai           -- OpenAI Chat Completions API
      gemini           -- Google Gemini Generative Language API
      ollama           -- Local Ollama server (no API key needed)
    """
    raw = get_config().get(_SECTION, "provider", fallback="claude")
    return (raw or "claude").strip().lower()


def get_ollama_endpoint():
    """Base URL for a local Ollama server. Ignored unless provider=ollama."""
    return get_config().get(
        _SECTION, "ollama_endpoint",
        fallback="http://localhost:11434",
    )


def get_default_model():
    return get_config().get(_SECTION, "default_model", fallback="claude-haiku-4-5")


def get_fast_model():
    return get_config().get(_SECTION, "fast_model", fallback="claude-haiku-4-5")


def get_max_tokens():
    return get_config().getint(_SECTION, "max_tokens", fallback=512)


def get_language():
    return get_config().get(_SECTION, "language", fallback="English")


def is_configured():
    """A provider is configured if its credentials are present. Ollama
    needs no key (just a reachable endpoint); the cloud providers need
    a non-placeholder api_key."""
    if get_provider() == "ollama":
        return True
    key = get_api_key()
    return bool(key and key != "YOUR_API_KEY_HERE")


def _bool_setting_with_config_fallback(key, cfg_key, cfg_default):
    """Runtime override (Llamafone_Settings.json) takes precedence over the
    static config file, so the in-game settings UI can toggle behavior
    without the player having to edit and reload llamafone.cfg."""
    val = get_setting(key)
    if val is not None:
        return bool(val)
    return get_config().getboolean(_SECTION, cfg_key, fallback=cfg_default)


def _int_setting_with_config_fallback(key, cfg_key, cfg_default):
    val = get_setting(key)
    if val is not None:
        try:
            return int(val)
        except Exception:
            pass
    return get_config().getint(_SECTION, cfg_key, fallback=cfg_default)


def get_phone_allow_ghosts():
    """If False, ghost sims are filtered out of phone contact pickers and
    auto-call/auto-text recipient pools."""
    return _bool_setting_with_config_fallback("phone_allow_ghosts", "phone_allow_ghosts", True)


def get_reply_delay_enabled():
    """Should the sim 'think' for a few seconds before replying to player texts?"""
    return _bool_setting_with_config_fallback("reply_delay_enabled", "reply_delay_enabled", True)


def get_reply_delay_min_seconds():
    return _int_setting_with_config_fallback("reply_delay_min_seconds", "reply_delay_min_seconds", 15)


def get_reply_delay_max_seconds():
    return _int_setting_with_config_fallback("reply_delay_max_seconds", "reply_delay_max_seconds", 90)


