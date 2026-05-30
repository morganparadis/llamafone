"""
Configuration loader for Claude AI mod.
Reads from claude_config.cfg in the Mods folder.

Runtime settings (set via in-game commands) are stored separately in
ClaudeAI_Settings.json alongside the config file so that the config
file stays clean and user-edited. Settings in the JSON override config.
"""
import json
import os
import configparser

_config = None
_CONFIG_FILENAME = "claude_config.cfg"
_SETTINGS_FILENAME = "ClaudeAI_Settings.json"


def _find_config_file():
    """Search for config file in the Mods folder and relative to this script."""
    # Primary: check the known Mods folder location directly
    # (walking up from __file__ doesn't work inside a .ts4script zip)
    mods_folder = os.path.join(
        os.path.expanduser("~"), "Documents",
        "Electronic Arts", "The Sims 4", "Mods",
    )
    mods_path = os.path.join(mods_folder, _CONFIG_FILENAME)
    if os.path.isfile(mods_path):
        return os.path.abspath(mods_path)

    # Fallback: walk up from script location (works during development)
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
    """Read a runtime setting (set by in-game command). Falls back to config file."""
    return _load_settings().get(key, fallback)


def set_setting(key, value):
    """Persist a runtime setting to ClaudeAI_Settings.json."""
    data = _load_settings()
    data[key] = value
    return _save_settings(data)


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
    return get_config().get("claude_ai", "api_key", fallback="")


def get_default_model():
    return get_config().get("claude_ai", "default_model", fallback="claude-opus-4-6")


def get_fast_model():
    return get_config().get("claude_ai", "fast_model", fallback="claude-haiku-4-5")


def get_max_tokens():
    return get_config().getint("claude_ai", "max_tokens", fallback=512)


def get_language():
    return get_config().get("claude_ai", "language", fallback="English")


def is_configured():
    key = get_api_key()
    return bool(key and key != "YOUR_API_KEY_HERE")


