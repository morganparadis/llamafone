"""
AI provider HTTP client.

Routes `call_ai_async()` to one of four providers based on the
`provider` knob in llamafone.cfg:

  claude  -> Anthropic Messages API
  openai  -> OpenAI Chat Completions API
  gemini  -> Google Gemini Generative Language API
  ollama  -> Local Ollama server (no API key needed)

We talk to every provider via `curl` because the Sims 4's embedded
Python 3.7 lacks SSL support. Each provider's request/response shape
gets normalized at this boundary -- callers only see:

    in:  messages=[{"role": "user"|"assistant", "content": str}, ...],
         system=str | None, use_fast_model=bool
    out: callback(text: str | None, error: str | None)

`call_claude_async` is kept as a thin alias of `call_ai_async` for
backward compatibility with code paths still using the v2 name.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading

from . import config


# Strip Unicode emoji from every AI response before it reaches the game.
#
# Two reasons:
#   1. Local models (Ollama on smaller llama3/mistral/qwen variants) tend
#      to output mojibake or stray control bytes around emoji codepoints,
#      which show up as garbage rectangles in the Sims 4 cheat console
#      and phone dialogs.
#   2. Even when the codepoints render correctly, the mod's voice prompts
#      treat the messages as plain text -- emoji clash with the dialogue
#      style guidance ("complete sentences, no decorative glyphs").
#
# The pattern targets the standard Unicode emoji blocks only -- CJK
# letters (U+4E00...) and other non-Latin scripts are NOT touched, so
# players using `language = Chinese` / `Japanese` / etc. don't see their
# generated text stripped.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"   # misc symbols & pictographs
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"   # alchemical
    "\U0001F780-\U0001F7FF"   # geometric
    "\U0001F800-\U0001F8FF"   # supplemental arrows-C
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"   # chess symbols
    "\U0001FA70-\U0001FAFF"   # symbols & pictographs extended-A
    "\U0001F1E0-\U0001F1FF"   # regional indicator (flags)
    "☀-⛿"           # misc symbols
    "✀-➿"           # dingbats
    "⬀-⯿"           # misc symbols & arrows
    "️"                  # variation selector-16
    "‍"                  # zero-width joiner (emoji sequence glue)
    "]+",
    flags=re.UNICODE,
)
# Catches text-style emoticons too: :) :-) :( :-D ;) <3 etc.
# Conservative -- only the common, unambiguous shapes.
_TEXT_EMOTICON_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))(?::-?[)(DPpoO/\\|*$3]|;-?[)Dp]|<3+|</3|XD|xD|\^_?\^)(?=$|\s|[.,!?])"
)


def _strip_emojis(text):
    if not text:
        return text
    out = _EMOJI_RE.sub("", text)
    out = _TEXT_EMOTICON_RE.sub("", out)
    # Collapse the double spaces left behind by removed emoji
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


_LAST_PROMPT_FILENAME = "Llamafone_LastPrompt.txt"


def _last_prompt_path():
    """Path to the last-prompt log file (next to llamafone.cfg)."""
    cfg = config._find_config_file()
    if cfg:
        return os.path.join(os.path.dirname(cfg), _LAST_PROMPT_FILENAME)
    return os.path.join(os.path.expanduser("~"), "Documents", _LAST_PROMPT_FILENAME)


def _log_prompt(system, messages, model, provider):
    """Write the most recent prompt to a file for debugging."""
    try:
        path = _last_prompt_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("=== Llamafone - Last Prompt ===\n")
            f.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
            f.write(f"Provider:  {provider}\n")
            f.write(f"Model:     {model}\n\n")
            f.write("=== SYSTEM PROMPT ===\n")
            f.write((system or "(none)") + "\n\n")
            f.write("=== USER MESSAGES ===\n")
            for m in messages:
                f.write(f"--- role: {m.get('role')} ---\n")
                f.write(str(m.get("content", "")) + "\n\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Curl wrapper -- hides the terminal window on Windows so the player doesn't
# see a black box flash every time the mod calls an API.
# ---------------------------------------------------------------------------

def _curl(url, headers, body_json, timeout=60):
    """Run curl POST, return (stdout, error). On any error the second
    value is a human-readable string and stdout is whatever was captured."""
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    args = ["curl", "-s", "-X", "POST"]
    for k, v in headers.items():
        args += ["-H", f"{k}: {v}"]
    args += ["-d", body_json, url]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            startupinfo=startupinfo,
        )
    except subprocess.TimeoutExpired:
        return "", f"Request timed out after {timeout}s."
    except FileNotFoundError:
        return "", "curl not found. Llamafone needs curl on PATH."
    except Exception as e:
        return "", f"curl invocation failed: {type(e).__name__}: {e}"
    if result.returncode != 0:
        err = result.stderr.strip() or f"curl exited with code {result.returncode}"
        return result.stdout, f"Network error: {err}"
    return result.stdout, None


# ---------------------------------------------------------------------------
# Provider implementations -- each returns (text, error).
# ---------------------------------------------------------------------------

def _call_claude(api_key, model, max_tokens, system, messages):
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    stdout, err = _curl("https://api.anthropic.com/v1/messages", headers, json.dumps(body))
    if err:
        return "", err
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from API: {stdout[:200]}"
    if "error" in data:
        msg = data["error"].get("message", str(data["error"])) if isinstance(data.get("error"), dict) else str(data["error"])
        return "", f"API error: {msg}"
    try:
        return data["content"][0]["text"], None
    except (KeyError, IndexError, TypeError):
        return "", "Empty response from Claude."


def _call_openai(api_key, model, max_tokens, system, messages):
    # OpenAI uses the same "messages" shape but the system prompt is
    # a normal message with role="system" prepended, not a separate field.
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    body = {"model": model, "messages": full, "max_tokens": max_tokens}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    stdout, err = _curl("https://api.openai.com/v1/chat/completions", headers, json.dumps(body))
    if err:
        return "", err
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from API: {stdout[:200]}"
    if "error" in data:
        e = data["error"]
        msg = e.get("message", str(e)) if isinstance(e, dict) else str(e)
        return "", f"API error: {msg}"
    try:
        return data["choices"][0]["message"]["content"], None
    except (KeyError, IndexError, TypeError):
        return "", "Empty response from OpenAI."


def _call_gemini(api_key, model, max_tokens, system, messages):
    # Gemini uses "contents" with parts. System prompt goes in a separate
    # systemInstruction field. Roles: "user" and "model" (assistant->model).
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})
    body = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    headers = {"Content-Type": "application/json"}
    stdout, err = _curl(url, headers, json.dumps(body))
    if err:
        return "", err
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from API: {stdout[:200]}"
    if "error" in data:
        e = data["error"]
        msg = e.get("message", str(e)) if isinstance(e, dict) else str(e)
        return "", f"API error: {msg}"
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"], None
    except (KeyError, IndexError, TypeError):
        return "", "Empty response from Gemini."


def _call_ollama(endpoint, model, max_tokens, system, messages):
    # Ollama exposes /api/chat with an OpenAI-ish shape, plus a "stream"
    # flag we set to false so we get a single response object. No API
    # key -- Ollama is a local server.
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    body = {
        "model": model,
        "messages": full,
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    base = (endpoint or "http://localhost:11434").rstrip("/")
    headers = {"Content-Type": "application/json"}
    stdout, err = _curl(f"{base}/api/chat", headers, json.dumps(body))
    if err:
        return "", err
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from Ollama: {stdout[:200]}"
    if "error" in data:
        return "", f"Ollama error: {data['error']}"
    try:
        return data["message"]["content"], None
    except (KeyError, TypeError):
        return "", "Empty response from Ollama."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def call_ai_async(messages, system=None, use_fast_model=False, callback=None):
    """
    Make an async call to the configured AI provider on a background thread.

    Args:
        messages: list of {"role": "user"|"assistant", "content": str}
        system:   optional system prompt string
        use_fast_model: if True, uses fast_model from config instead of default
        callback: function(text: str | None, error: str | None) called when done

    Returns the background Thread object.
    """
    def _request():
        if not config.is_configured():
            if callback:
                callback(None, "No API key configured. Edit llamafone.cfg in your Mods folder.")
            return

        provider = config.get_provider()
        model = config.get_fast_model() if use_fast_model else config.get_default_model()
        max_tokens = config.get_max_tokens()

        # Log the prompt so we can debug what the AI actually saw
        _log_prompt(system, messages, model, provider)

        try:
            if provider == "claude":
                text, err = _call_claude(config.get_api_key(), model, max_tokens, system, messages)
            elif provider == "openai":
                text, err = _call_openai(config.get_api_key(), model, max_tokens, system, messages)
            elif provider == "gemini":
                text, err = _call_gemini(config.get_api_key(), model, max_tokens, system, messages)
            elif provider == "ollama":
                text, err = _call_ollama(config.get_ollama_endpoint(), model, max_tokens, system, messages)
            else:
                if callback:
                    callback(None, f"Unknown provider '{provider}'. Set provider to claude/openai/gemini/ollama in llamafone.cfg.")
                return
        except Exception as e:
            if callback:
                callback(None, f"Unexpected error: {type(e).__name__}: {e}")
            return

        if callback:
            # Strip emojis from every successful response. Done at the
            # client boundary so it covers all features (phone, story,
            # event, etc.) without each call site having to remember.
            if text and not err:
                text = _strip_emojis(text)
            callback(text, err)

    thread = threading.Thread(target=_request, daemon=True, name="Llamafone-Request")
    thread.start()
    return thread


# Backward-compat alias so legacy call sites (or anyone forking from v2)
# don't break mid-migration. New code should call_ai_async directly.
call_claude_async = call_ai_async
