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


# Default llamafone.cfg contents. Written to the Mods folder on first
# mod load if no cfg exists yet -- users don't need to include a cfg
# in the download anymore, dropping just the .ts4script + .package
# into Mods is enough to get a working default config on first launch.
# The api_key stays "YOUR_API_KEY_HERE" so is_configured() correctly
# reports "not configured yet" and the mod's error notifications tell
# the user to open the file and add their key.
_DEFAULT_CONFIG_TEMPLATE = """[llamafone]
; ── AI provider ────────────────────────────────────────────────────────────
; Which AI service does the mod talk to? Pick one:
;   claude      -- Anthropic (default, what the mod was built on)
;   openai      -- OpenAI's chat completions API (GPT-4, GPT-4o, etc.)
;   gemini      -- Google Gemini
;   openrouter  -- OpenRouter aggregator (one key, dozens of models: Claude,
;                  GPT, Llama, Mistral, DeepSeek, etc. -- pick per model name)
;   ollama      -- A local Ollama server (no API key required)
provider = claude

; API key for whichever provider you picked above. Only the matching one
; is read -- the others can stay blank. Get a key:
;   claude      -> https://console.anthropic.com/
;   openai      -> https://platform.openai.com/api-keys
;   gemini      -> https://aistudio.google.com/apikey
;   openrouter  -> https://openrouter.ai/keys
;   ollama      -> not needed; runs locally
api_key = YOUR_API_KEY_HERE

; If using Ollama, point at your local server:
ollama_endpoint = http://localhost:11434

; ── Models ─────────────────────────────────────────────────────────────────
; Model for detailed tasks (stories, storylines, drama)
; Examples per provider:
;   claude      -> claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5
;   openai      -> gpt-4o, gpt-4o-mini, gpt-4-turbo
;   gemini      -> gemini-1.5-pro, gemini-1.5-flash
;   openrouter  -> anthropic/claude-haiku-4-5, openai/gpt-4o-mini,
;                  meta-llama/llama-3.1-8b-instruct, deepseek/deepseek-chat
;                  (browse full catalog at https://openrouter.ai/models)
;   ollama      -> llama3.1, mistral, qwen2.5 (whatever you've `ollama pull`-ed)
default_model = claude-haiku-4-5

; Model for quick tasks (dialogue, events, calls, texts). Cheaper/faster.
fast_model = claude-haiku-4-5

; Maximum tokens per response (higher = longer text, more API cost)
; 512 is good for dialogue, bump to 1024+ for stories
max_tokens = 512

; Language for all generated content
language = English

; ── Misc ───────────────────────────────────────────────────────────────────

; Allow ghosts to call/text your sim? (true or false)
phone_allow_ghosts = true

; ── Reply delays ──────────────────────────────────────────────────────────
; When you text a sim (via Reply or llama.sendtext), how long should they
; "think" before responding? Instant replies feel uncanny -- this adds a small
; realistic delay scaled by friendship + traits.
;   - Best friends + outgoing traits reply faster (often near the minimum)
;   - Enemies + lazy/loner traits drag (often 2x or more the max)
;   - Set reply_delay_enabled = false to restore instant replies
reply_delay_enabled = true
reply_delay_min_seconds = 15
reply_delay_max_seconds = 90


; ── Message relationship impact (v3.6) ───────────────────────────────────
; When true, phone messages nudge the ACTUAL friendship / romance
; tracks between the two sims. Warm exchanges lift friendship, hostile
; ones drop it, flirty texts lift romance, etc. Both directions of the
; pair get the same delta. Default on -- makes messages feel like they
; matter. Turn off to keep AI messages purely narrative.
message_relationship_impact_enabled = true

; Hard cap on how much friendship or romance can change per message.
; Applied to both directions independently. Default 5 keeps a single
; message well below the game's own casual-social action deltas (3-8),
; so a text nudges relationships without reshaping them. Set to 0 to
; disable without touching the enabled flag above.
message_relationship_max_delta = 5


; ── Auto-events ────────────────────────────────────────────────────────────
; Randomly fires events/content while you play without you having to ask.
; Uses real-world time (not Sims game speed).

; Set to true to turn on random auto-events
auto_events_enabled = false

; How many real-world minutes between each check
; (actual firing also depends on the chance below)
auto_event_interval_minutes = 20

; Percent chance (1-100) that something fires each check
; 40 = fires roughly every 50 real minutes on average
auto_event_chance = 40

; Which types of content can fire automatically
; Options: event, goals, story, drama, call, text  (comma-separated)
; Default is phone-only -- the mod is built around the phone, so auto-events
; default to incoming calls/texts. Add event/goals/story/drama if you want
; the full mix.
auto_event_types = call, text

; Weight per event type -- higher = more likely to be picked.
; Format: type:weight, type:weight  (leave blank for equal chance)
; Example: call:40, text:30, event:20, goals:10
auto_event_weights = call:50, text:50


; ── Dating (v3.5) ─────────────────────────────────────────────────────────
; Optional inbound + outbound dating layer:
;   INBOUND  -- opted-in sims occasionally receive texts from unmet
;               sims who explain how they got the number (mutual
;               friend if one exists, else a fun made-up reason based
;               on the sender's traits/career/hobbies).
;   OUTBOUND -- new "Send Intro" interaction on the Llamafone app.
;               Player picks an eligible unmet sim from a filtered
;               picker and writes their own intro text. The mod never
;               generates outbound intros.
;
; Feature is opt-in PER PLAYED SIM. Turn it on inside the game via
; Llamafone Settings > Dating > (per-sim toggle). Opt-in state lives
; in <save>/DatingOptIns.json so it travels with the save.

; Frequency weight for cold-outreach texts inside the auto_events pool.
; 0 = off globally (even if sims are opted in). This is added on TOP
; of auto_event_weights above -- so with call:50, text:50 and
; dating_cold_outreach_weight = 20, roughly 17% (20 / 120) of
; auto-events will be dating. Also toggleable in-game as
; Off / Rarely / Sometimes / Often under Llamafone Settings > Dating.
dating_cold_outreach_weight = 20
"""


def _log(msg):
    """Best-effort log write to Documents/Llamafone_Log.txt. Config is
    load-critical; we can't rely on the package's `_log` back here."""
    try:
        import datetime
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [config] {msg}\n")
    except Exception:
        pass


def _write_default_config(target_path):
    """Materialize the default llamafone.cfg at `target_path`. Called
    when no cfg exists anywhere yet -- lets users just drop the two
    mod files into their Mods folder and have a working config the
    first time they launch. Non-destructive: only fires when nothing
    else was found."""
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_CONFIG_TEMPLATE)
        _log(f"wrote default cfg to {target_path}")
        return True
    except Exception as e:
        _log(f"could not write default cfg to {target_path}: {type(e).__name__}: {e}")
        return False


def _find_config_file():
    """Search for the config file in the Mods folder, then walk up from
    the script location as a dev-mode fallback. If nothing exists AND
    the Mods folder is present, materialize a default cfg there so a
    fresh install has a working config without the user having to
    include one in the download. Returns the first existing / newly-
    created file, or None if neither the Mods folder nor a dev-mode
    parent directory is writable."""
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
    # Nothing on disk yet. If the Mods folder exists (real game
    # install), create a default cfg there so the mod comes up
    # configured with placeholders on first launch. Skip when the
    # folder isn't present (headless / dev-only environment).
    if os.path.isdir(mods_folder):
        if _write_default_config(mods_path):
            return os.path.abspath(mods_path)
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


# ---------------------------------------------------------------------------
# Dating (v3.5): opt-in dating-app layer.
#
# The feature is gated per-played-household-sim, not by a global switch.
# Per-sim opt-in state lives in <save>/DatingOptIns.json (see dating.py);
# users who never toggle any sim on see zero behavior change.
#
# The only knob in config is the cold-outreach frequency weight, which
# tunes how often inbound dating texts fire relative to the other
# auto-event types. Set to 0 to keep the feature disabled globally
# without touching individual sim opt-ins.
# ---------------------------------------------------------------------------


def get_dating_cold_outreach_weight():
    """Weight for cold-outreach auto-events relative to call/text. 0
    disables the flow globally even if sims are opted-in. Default 20
    lands at roughly 17% of firings alongside the stock call:50 /
    text:50 weights."""
    val = _int_setting_with_config_fallback("dating_cold_outreach_weight", "dating_cold_outreach_weight", 20)
    return max(0, val)


def get_message_relationship_impact_enabled():
    """Whether call/text mood tags nudge the actual friendship / romance
    tracks. Default on -- makes messages feel like they matter. Turn
    off if you want AI messages to be purely narrative and leave
    relationship stats untouched by the mod."""
    return _bool_setting_with_config_fallback(
        "message_relationship_impact_enabled",
        "message_relationship_impact_enabled",
        True,
    )


def get_message_relationship_max_delta():
    """Hard cap on per-message friendship/romance change. Applied to
    both directions independently. Default 5 keeps a single message
    well below Sims 4's own casual-social action deltas (3-8), so the
    feature nudges relationships without one text reshaping them.
    Set to 0 to disable while leaving the enabled flag alone."""
    val = _int_setting_with_config_fallback(
        "message_relationship_max_delta",
        "message_relationship_max_delta",
        5,
    )
    return max(0, val)


