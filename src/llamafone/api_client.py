"""
AI provider HTTP client.

Routes `call_ai_async()` to one of five providers based on the
`provider` knob in llamafone.cfg:

  claude      -> Anthropic Messages API
  openai      -> OpenAI Chat Completions API
  gemini      -> Google Gemini Generative Language API
  openrouter  -> OpenRouter (OpenAI-compatible aggregator, any hosted model)
  ollama      -> Local Ollama server (no API key needed)
  lmstudio    -> LM Studio's local server (OpenAI-compatible, no key)

We talk to every provider via `curl` because the Sims 4's embedded
Python 3.7 lacks SSL support. Each provider's request/response shape
gets normalized at this boundary -- callers only see:

    in:  messages=[{"role": "user"|"assistant", "content": str}, ...],
         system=str | None, use_fast_model=bool
    out: callback(text: str | None, error: str | None)

"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time

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
            f.write(f"Model:     {model}\n")
            try:
                from . import LOAD_TIMESTAMP, MOD_VERSION
                f.write(f"Build:     v{MOD_VERSION}, loaded {LOAD_TIMESTAMP} (not sent to the AI)\n\n")
            except Exception:
                f.write("\n")
            f.write("=== SYSTEM PROMPT ===\n")
            f.write((system or "(none)") + "\n\n")
            f.write("=== USER MESSAGES ===\n")
            for m in messages:
                f.write(f"--- role: {m.get('role')} ---\n")
                f.write(str(m.get("content", "")) + "\n\n")
    except Exception:
        pass


def _log_failure(provider, model, error, elapsed):
    """One line in Llamafone_Log.txt per failed request, so a bug report
    that includes the log already says what failed and how long it took
    (a 60s+ elapsed on a local model means 'too slow', not 'broken')."""
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [api] {provider}/{model} failed after {elapsed:.1f}s: {error}\n")
    except Exception:
        pass


# Local models (Ollama, LM Studio) run on the player's own PC. Without a
# supported GPU, reading our ~7K-token prompts can take minutes; 60s (fine
# for cloud APIs) made every text silently time out on those PCs.
_LOCAL_TIMEOUT = 300


# ---------------------------------------------------------------------------
# Curl wrapper -- hides the terminal window on Windows so the player doesn't
# see a black box flash every time the mod calls an API.
# ---------------------------------------------------------------------------

def _curl(url, headers, body_json, timeout=60, method="POST"):
    """Run curl, return (stdout, error, returncode). On success error
    is None. On failure error is a human-readable string. returncode
    is curl's exit code (or None if we never even got to invoke it --
    e.g. curl-not-found / timeout / other Python-side failure).

    Callers that want to diagnose specific curl failures (like exit
    code 7 = 'connection refused' -> Ollama not running) can inspect
    the returncode to produce provider-specific error messages.

    The request body is streamed via stdin (`--data-binary @-`) rather
    than passed as `-d '<json>'`. Windows' CreateProcess caps the total
    command line at ~32KB, so a phone prompt with full relationship
    history + save notes + sim bios can blow past the limit and fail
    -- sometimes as FileNotFoundError, presenting as a bogus "curl not
    found" even though curl is fine and small prompts (events, bios)
    succeed on the exact same PATH. Stdin has no size limit."""
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    args = ["curl", "-s"]
    if method != "GET":
        args += ["-X", method]
    for k, v in headers.items():
        args += ["-H", f"{k}: {v}"]
    if body_json is not None:
        args += ["--data-binary", "@-"]
    args += [url]
    try:
        result = subprocess.run(
            args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
            startupinfo=startupinfo,
            input=body_json if body_json is not None else None,
        )
    except subprocess.TimeoutExpired:
        return "", f"Request timed out after {timeout}s.", None
    except FileNotFoundError:
        return "", "curl not found. Llamafone needs curl on PATH.", None
    except Exception as e:
        return "", f"curl invocation failed: {type(e).__name__}: {e}", None
    if result.returncode != 0:
        err = result.stderr.strip() or f"curl exited with code {result.returncode}"
        return result.stdout, f"Network error: {err}", result.returncode
    return result.stdout, None, 0


# Curl exit code -> user-friendly summary. curl documents these under
# EXIT CODES in `man curl`. We cover the ones that show up in practice
# for the providers we support; unrecognized codes fall through to a
# generic message.
_CURL_EXIT_HINTS = {
    6:  "couldn't resolve host (DNS lookup failed).",
    7:  "connection refused -- the server isn't listening on that port.",
    28: "timed out.",
    35: "SSL/TLS handshake failed.",
    52: "server sent an empty response.",
    56: "connection reset.",
    60: "SSL certificate could not be verified.",
    77: "SSL certificate file missing.",
}


def _friendly_ollama_error(err, returncode, endpoint):
    """Turn a raw curl error into a plain-English message for Ollama
    users. The single most common issue: Ollama isn't running. We
    detect curl exit code 7 and provide step-by-step guidance instead
    of the raw 'curl exited with code 7'."""
    base = f"Can't reach Ollama at {endpoint}."
    if returncode == 7:
        return (
            f"{base} Is Ollama running? On Windows, look for the "
            f"llama icon in your system tray (bottom-right, click the ^). "
            f"If it's not there, open Ollama from the Start menu and "
            f"wait ~10 seconds for it to start, then try again. "
            f"You can also verify by running `ollama list` in Command "
            f"Prompt -- if that command fails, Ollama isn't installed "
            f"or isn't on your PATH."
        )
    if returncode == 6:
        return (
            f"{base} The hostname in ollama_endpoint (in llamafone.cfg) "
            f"couldn't be resolved. Default should be "
            f"http://localhost:11434 -- check your config."
        )
    if returncode == 28:
        return (
            f"{base} The request timed out. Ollama may be busy loading "
            f"a large model, or running without a graphics card, which "
            f"is slow for long prompts. Try again in a moment; if it "
            f"keeps timing out, try a smaller model (e.g. llama3.2:3b)."
        )
    hint = _CURL_EXIT_HINTS.get(returncode)
    if hint:
        return f"{base} {hint} (curl exit code {returncode})"
    return f"{base} {err}"


# ---------------------------------------------------------------------------
# Provider implementations -- each returns (text, error).
# ---------------------------------------------------------------------------

# Extra output room on Claude requests for models that think before
# answering (adaptive thinking -- e.g. Claude Haiku 5.5 thought for ~350
# tokens on a real Llamafone prompt). Thinking counts against max_tokens,
# so without headroom a 512-token cap could be spent before any message.
# Only tokens actually generated are billed.
_CLAUDE_THINKING_HEADROOM = 2048


def _call_claude(api_key, model, max_tokens, system, messages):
    body = {"model": model, "max_tokens": int(max_tokens) + _CLAUDE_THINKING_HEADROOM, "messages": messages}
    if system:
        body["system"] = system
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    stdout, err, _rc = _curl("https://api.anthropic.com/v1/messages", headers, json.dumps(body))
    if err:
        return "", err
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from API: {stdout[:200]}"
    if "error" in data:
        msg = data["error"].get("message", str(data["error"])) if isinstance(data.get("error"), dict) else str(data["error"])
        return "", f"API error: {msg}"
    # A reply is a list of content blocks; models that think put a
    # "thinking" block before the "text" block. Read every text block --
    # content[0] alone was the thinking block on Haiku 5.5 ("Empty
    # response from Claude." on every long prompt).
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
    if text:
        return text, None
    stop = data.get("stop_reason")
    if stop == "refusal":
        return "", "Claude declined to write this reply."
    if stop == "max_tokens":
        return "", ("Claude ran out of room before writing the message. Raise max_tokens in "
                    "llamafone.cfg (e.g. 1024).")
    return "", f"Empty response from Claude (stop reason: {stop or 'unknown'})."


def _call_openai(api_key, model, max_tokens, system, messages):
    # OpenAI uses the same "messages" shape but the system prompt is
    # a normal message with role="system" prepended, not a separate field.
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    # max_completion_tokens: OpenAI deprecated max_tokens in favor of it, and
    # reasoning models (the GPT-6 line) count their thinking against it --
    # so give the same headroom as Claude, or the reply can come back empty.
    # Older models (gpt-4o, gpt-4o-mini) accept it too. Only tokens actually
    # generated are billed.
    body = {"model": model, "messages": full,
            "max_completion_tokens": int(max_tokens) + _CLAUDE_THINKING_HEADROOM}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    stdout, err, _rc = _curl("https://api.openai.com/v1/chat/completions", headers, json.dumps(body))
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
        choice = data["choices"][0]
        text = (choice.get("message", {}).get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return "", "Empty response from OpenAI."
    if text:
        return text, None
    if choice.get("finish_reason") == "length":
        return "", ("OpenAI ran out of room before writing the message (likely spent it "
                    "thinking). Raise max_tokens in llamafone.cfg (e.g. 1024), or use a "
                    "model that doesn't reason, like gpt-4o-mini.")
    if choice.get("finish_reason") == "content_filter":
        return "", "OpenAI declined to write this reply."
    return "", "Empty response from OpenAI."


def _call_openrouter(api_key, model, max_tokens, system, messages):
    # OpenRouter proxies dozens of models (Anthropic, OpenAI, Meta,
    # Mistral, etc.) behind an OpenAI-compatible Chat Completions API.
    # Same request/response shape as _call_openai; only the base URL and
    # optional attribution headers differ. Model names use the
    # "vendor/model" form -- e.g. "anthropic/claude-haiku-4.5",
    # "openai/gpt-4o-mini", "meta-llama/llama-3.1-8b-instruct".
    #
    # HTTP-Referer / X-Title are optional and used purely for OpenRouter's
    # public "top apps" leaderboard; they don't gate access. We send them
    # so the mod shows up as a coherent user-agent rather than "unknown".
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    body = {"model": model, "messages": full, "max_tokens": max_tokens}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://morganparadis.github.io/llamafone/",
        "X-Title": "Llamafone (Sims 4)",
    }
    stdout, err, _rc = _curl("https://openrouter.ai/api/v1/chat/completions", headers, json.dumps(body))
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
        return "", "Empty response from OpenRouter."


def _gemini_takes_thinking_budget(model):
    """True for Gemini 2.x and older, where thinkingBudget=0 turns thinking
    off. Gemini 3+ reject it ("Request contains an invalid argument")."""
    m = re.search(r"gemini-(\d+)", str(model or "").lower())
    return bool(m) and int(m.group(1)) <= 2


def _call_gemini(api_key, model, max_tokens, system, messages):
    # Gemini uses "contents" with parts. System prompt goes in a separate
    # systemInstruction field. Roles: "user" and "model" (assistant->model).
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": str(m.get("content", ""))}]})
    # Gemini 2.5 flash/pro count "thinking" tokens against maxOutputTokens,
    # so a 512 budget can be entirely eaten by silent reasoning and the
    # visible reply comes back empty or truncated mid-sentence. Disable
    # thinking (flash/flash-lite honor thinkingBudget=0; pro ignores it,
    # which is fine) and give the visible reply enough headroom.
    #
    # Gemini 3 models changed thinking control (thinking levels, not a
    # budget) and can't all turn it off; sending thinkingBudget=0 to
    # gemini-3.5-flash-lite came back "Request contains an invalid
    # argument". So: only Gemini 2.x and older get the budget; newer models
    # get no thinking setting and extra room instead (only tokens actually
    # used are billed). And if Google rejects the request as an invalid
    # argument while a thinking setting was sent, retry once without it.
    generation_config = {"maxOutputTokens": max(int(max_tokens), 1024)}
    if _gemini_takes_thinking_budget(model):
        generation_config["thinkingConfig"] = {"thinkingBudget": 0}
    else:
        generation_config["maxOutputTokens"] = int(max_tokens) + _CLAUDE_THINKING_HEADROOM
    body = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    headers = {"Content-Type": "application/json"}
    stdout, err, _rc = _curl(url, headers, json.dumps(body))
    if err:
        return "", err
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from API: {stdout[:200]}"
    if "error" in data and "thinkingConfig" in generation_config and \
            "invalid argument" in str(data["error"]).lower():
        generation_config.pop("thinkingConfig", None)
        generation_config["maxOutputTokens"] = int(max_tokens) + _CLAUDE_THINKING_HEADROOM
        stdout, err, _rc = _curl(url, headers, json.dumps(body))
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
        candidate = data["candidates"][0]
    except (KeyError, IndexError, TypeError):
        return "", "Empty response from Gemini."
    # Concatenate ALL text parts -- Gemini can split a single reply across
    # multiple parts, and grabbing only parts[0] silently drops the tail.
    text_chunks = []
    try:
        for part in candidate.get("content", {}).get("parts", []) or []:
            t = part.get("text")
            if t:
                text_chunks.append(t)
    except (AttributeError, TypeError):
        pass
    text = "".join(text_chunks).strip()
    finish = candidate.get("finishReason", "")
    if not text:
        if finish == "MAX_TOKENS":
            return "", ("Gemini hit maxOutputTokens before producing any "
                       "visible text (likely spent the whole budget on "
                       "internal reasoning). Try a larger max_tokens in "
                       "llamafone.cfg or a different Gemini model.")
        if finish == "SAFETY":
            return "", "Gemini blocked the response for safety."
        return "", "Empty response from Gemini."
    return text, None


def check_ollama_health(endpoint=None):
    """Diagnostic: verify an Ollama server is reachable and list its
    available models. Called by llama.testconnection so non-technical
    users can pinpoint exactly what's wrong.

    Returns a dict:
      {
        "reachable": bool,
        "endpoint": str,
        "models": [str, ...],       -- present when reachable
        "error": str | None,        -- friendly error when not reachable
        "curl_returncode": int|None
      }
    """
    from . import config as _config
    base = (endpoint or _config.get_ollama_endpoint() or "http://localhost:11434").rstrip("/")
    # /api/tags is the standard Ollama listing endpoint. Uses GET so
    # it's a lightweight probe -- no model download or generation.
    stdout, err, rc = _curl(f"{base}/api/tags", headers={}, body_json=None, timeout=5, method="GET")
    out = {
        "reachable": False,
        "endpoint": base,
        "models": [],
        "error": None,
        "curl_returncode": rc,
    }
    if err:
        out["error"] = _friendly_ollama_error(err, rc, base)
        return out
    try:
        data = json.loads(stdout)
        models = data.get("models") or []
        out["models"] = [m.get("name", "") for m in models if isinstance(m, dict)]
        out["reachable"] = True
    except Exception as e:
        out["error"] = f"Ollama replied but the response wasn't valid JSON: {type(e).__name__}"
    return out


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
    stdout, err, rc = _curl(f"{base}/api/chat", headers, json.dumps(body), timeout=_LOCAL_TIMEOUT)
    if err:
        # The #1 reported Ollama issue from non-technical users is
        # 'Network error: curl exited with code' -- opaque and offers
        # no direction. Swap in a step-by-step message when we can
        # identify the failure mode.
        return "", _friendly_ollama_error(err, rc, base)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from Ollama: {stdout[:200]}"
    if "error" in data:
        return "", f"Ollama error: {data['error']}"
    try:
        msg = data["message"]
        text = _strip_thinking(msg.get("content") or "")
    except (KeyError, TypeError, AttributeError):
        return "", "Empty response from Ollama."
    if not text and msg.get("thinking"):
        return "", _THINKING_ONLY_ERROR
    return text, None


def _lmstudio_base(endpoint):
    """LM Studio's server root. Players often paste the address LM Studio
    shows, which ends in /v1 -- accept it with or without."""
    base = (endpoint or "http://localhost:1234").strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3]
    return base


def _friendly_lmstudio_error(err, returncode, endpoint):
    base = f"Can't reach LM Studio at {endpoint}."
    if returncode == 7:
        return (
            f"{base} Is LM Studio's server running? Open LM Studio, go to "
            f"the Developer tab, and switch the server on (Status: Running). "
            f"The address it shows should match lmstudio_endpoint in "
            f"llamafone.cfg (default http://localhost:1234)."
        )
    if returncode == 6:
        return (
            f"{base} The address in lmstudio_endpoint (in llamafone.cfg) "
            f"couldn't be resolved. Default should be http://localhost:1234."
        )
    if returncode == 28 or "timed out" in (err or "").lower():
        return (
            f"{base} The request timed out. Without a graphics card, long "
            f"prompts can be very slow; try a smaller model."
        )
    hint = _CURL_EXIT_HINTS.get(returncode)
    if hint:
        return f"{base} {hint} (curl exit code {returncode})"
    return f"{base} {err}"


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def _strip_thinking(text):
    """Drop a reasoning model's inline <think>...</think> block, if the
    server left it in the reply text."""
    if not text:
        return text
    return _THINK_RE.sub("", text).strip()


_THINKING_ONLY_ERROR = (
    "The model spent its whole reply 'thinking' and wrote no message. "
    "Use a non-reasoning model, or turn thinking off for this model."
)


_LMSTUDIO_CONTEXT_HINT = (
    "The conversation is longer than the model's context length. In LM Studio, "
    "reload the model with Context Length set to 16384 (12288 at the very least)."
)


# A model the provider has retired (or a misspelled name) comes back as a
# raw API error like "models/gemini-2.5-flash is not found for API version
# v1beta, or is not supported for generateContent" -- which doesn't tell a
# player what to do. Providers retire models every few months (Gemini 2.5
# on Oct 20, 2026), so rewrite those into one plain instruction.
_SUGGESTED_MODEL = {
    "claude": "claude-haiku-5-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-3.5-flash-lite",
    "openrouter": "anthropic/claude-haiku-4.5",
}
_PROVIDER_NAME = {"claude": "Anthropic", "openai": "OpenAI", "gemini": "Google", "openrouter": "OpenRouter"}
_MODEL_GONE = (
    "no longer available", "deprecated", "has been shut down", "shut down",
    "decommissioned", "retired", "discontinued", "not a valid model",
    "not supported for generatecontent", "model_not_found",
)


def _explain_model_gone(provider, model, err):
    if provider not in _SUGGESTED_MODEL or not err or not err.startswith("API error:"):
        return err
    low = err.lower()
    gone = ("model" in low and any(p in low for p in _MODEL_GONE + ("not found", "does not exist")))         or low.startswith("api error: model:")    # Claude's not_found_error
    if not gone:
        return err
    return (f'{_PROVIDER_NAME[provider]} no longer offers the model "{model}" '
            f"(it was retired, or the name is misspelled). Open llamafone.cfg, set "
            f"default_model and fast_model to a current model, like "
            f"{_SUGGESTED_MODEL[provider]}, then type llama.reload in the cheat console.")


_PROVIDER_LABEL = {"claude": "Claude", "openai": "OpenAI", "gemini": "Gemini", "openrouter": "OpenRouter"}
_BUSY = ("high demand", "overloaded", "service unavailable", "temporarily unavailable",
         "currently unavailable")


def _explain_busy(provider, err):
    """Cloud provider overloaded or too slow: say it's on their side and
    what to do, instead of the raw API text (texts, calls, Llamagram)."""
    if provider not in _PROVIDER_LABEL or not err:
        return err
    low = err.lower()
    label = _PROVIDER_LABEL[provider]
    if err.startswith("API error:") and any(k in low for k in _BUSY):
        return (f"{label} is too busy right now (a problem on {_PROVIDER_NAME[provider]}'s "
                f"side, not Llamafone). Try again in a minute. If it keeps happening, "
                f"switch to a different model in llamafone.cfg.")
    if err.startswith("Request timed out"):
        return (f"{label} took too long to answer and Llamafone gave up after 60 seconds. "
                f"Try again in a minute. If it keeps happening, switch to a different "
                f"model in llamafone.cfg.")
    return err


def _clean_lmstudio_error(raw):
    """A player-readable LM Studio error. The server often wraps the real
    message in engine noise -- 'Engine protocol predict stream returned an
    error: {"code":500,"message":"Context size has been exceeded.",...}' --
    so pull out the innermost "message", then swap known problems for the
    fix."""
    msg = str(raw or "").strip()
    inner = re.findall(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', msg)
    if inner:
        msg = inner[-1].replace('\\"', '"').strip()
    low = msg.lower()
    if "context" in low or "n_ctx" in low or "too long" in low:
        return _LMSTUDIO_CONTEXT_HINT
    if "no models loaded" in low or "model not found" in low or "not loaded" in low:
        return ("No model is loaded. Load one in LM Studio, or set default_model / fast_model "
                "in llamafone.cfg to a loaded model's name (llama.testconnection lists them).")
    return msg or "the server returned an error with no message."


def check_lmstudio_health(endpoint=None):
    """Diagnostic for llama.testconnection: is LM Studio's server up, and
    which models does it offer? Same return shape as check_ollama_health."""
    from . import config as _config
    base = _lmstudio_base(endpoint or _config.get_lmstudio_endpoint())
    stdout, err, rc = _curl(f"{base}/v1/models", headers={}, body_json=None, timeout=5, method="GET")
    out = {"reachable": False, "endpoint": base, "models": [], "error": None, "curl_returncode": rc}
    if err:
        out["error"] = _friendly_lmstudio_error(err, rc, base)
        return out
    try:
        data = json.loads(stdout)
        models = data.get("data") or []
        out["models"] = [m.get("id", "") for m in models if isinstance(m, dict)]
        out["reachable"] = True
    except Exception as e:
        out["error"] = f"LM Studio replied but the response wasn't valid JSON: {type(e).__name__}"
    return out


def _call_lmstudio(endpoint, model, max_tokens, system, messages):
    # LM Studio's server speaks the OpenAI chat-completions format. No key:
    # it's a local server.
    full = []
    if system:
        full.append({"role": "system", "content": system})
    full.extend(messages)
    # reasoning_effort "none": reasoning models (Gemma 4, Qwen 3, ...)
    # otherwise think before answering, and the thinking counts against
    # max_tokens -- with our long prompts they used the whole budget and
    # returned an empty message. Tested on Gemma 4: 0 reasoning tokens.
    body = {"model": model, "messages": full, "max_tokens": max_tokens, "stream": False,
            "reasoning_effort": "none"}
    base = _lmstudio_base(endpoint)
    headers = {"Content-Type": "application/json"}
    url = f"{base}/v1/chat/completions"
    stdout, err, rc = _curl(url, headers, json.dumps(body), timeout=_LOCAL_TIMEOUT)
    if err:
        return "", _friendly_lmstudio_error(err, rc, base)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "", f"Invalid response from LM Studio: {stdout[:200]}"
    if "error" in data and "reasoning" in str(data["error"]).lower():
        # A server/model that rejects the parameter: retry without it.
        body.pop("reasoning_effort", None)
        stdout, err, rc = _curl(url, headers, json.dumps(body), timeout=_LOCAL_TIMEOUT)
        if err:
            return "", _friendly_lmstudio_error(err, rc, base)
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return "", f"Invalid response from LM Studio: {stdout[:200]}"
    if "error" in data:
        e = data["error"]
        msg = e.get("message", str(e)) if isinstance(e, dict) else str(e)
        return "", "LM Studio: " + _clean_lmstudio_error(msg)
    try:
        msg = data["choices"][0]["message"]
        text = _strip_thinking(msg.get("content") or "")
    except (KeyError, IndexError, TypeError):
        return "", "Empty response from LM Studio."
    if not text and msg.get("reasoning_content"):
        return "", _THINKING_ONLY_ERROR + " (LM Studio: pick a model without reasoning, or lower its reasoning setting.)"
    return text, None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def call_ai_async(messages, system=None, use_fast_model=False, callback=None, max_tokens=None):
    max_tokens_override = max_tokens  # per-call limit (e.g. long JSON comment passes)
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
        max_tokens = max_tokens_override or config.get_max_tokens()

        # Save-level notes prepended to the system prompt so world context
        # binds every downstream call/text/story path without each builder
        # having to know about the feature. Import lazily to avoid a
        # module-load cycle -- save_notes -> save_id -> services, and
        # api_client is imported very early.
        #
        # Uses a distinct `effective_system` local instead of rebinding
        # the outer `system` param: any assignment to `system` inside
        # this nested function marks it as function-local for the WHOLE
        # scope (Python's binding rule doesn't care that the assign is
        # gated by `if world:`), which would crash with UnboundLocalError
        # on every subsequent read of `system` when world is empty.
        # World context appended at the END of the system prompt so
        # it's the last thing the model reads before the user message
        # -- LLMs treat later instructions as authoritative when they
        # conflict with earlier ones. Placing it at the top drowns it
        # under hundreds of specific rules below; placing it at the
        # end keeps it high-priority without duplication.
        try:
            from . import save_notes as _save_notes
            world = _save_notes.format_for_prompt()
        except Exception:
            world = ""
        if world:
            effective_system = f"{system}\n\n{world}" if system else world
        else:
            effective_system = system
        effective_messages = messages

        # Log the prompt so we can debug what the AI actually saw
        _log_prompt(effective_system, effective_messages, model, provider)
        started = time.time()

        try:
            if provider == "claude":
                text, err = _call_claude(config.get_api_key(), model, max_tokens, effective_system, effective_messages)
            elif provider == "openai":
                text, err = _call_openai(config.get_api_key(), model, max_tokens, effective_system, effective_messages)
            elif provider == "gemini":
                text, err = _call_gemini(config.get_api_key(), model, max_tokens, effective_system, effective_messages)
            elif provider == "openrouter":
                text, err = _call_openrouter(config.get_api_key(), model, max_tokens, effective_system, effective_messages)
            elif provider == "ollama":
                text, err = _call_ollama(config.get_ollama_endpoint(), model, max_tokens, effective_system, effective_messages)
            elif provider == "lmstudio":
                text, err = _call_lmstudio(config.get_lmstudio_endpoint(), model, max_tokens, effective_system, effective_messages)
            else:
                if callback:
                    callback(None, f"Unknown provider '{provider}'. Set provider to claude/openai/gemini/openrouter/ollama/lmstudio in llamafone.cfg.")
                return
        except Exception as e:
            err = f"Unexpected error: {type(e).__name__}: {e}"
            _log_failure(provider, model, err, time.time() - started)
            if callback:
                callback(None, err)
            return

        # Strip emojis from every successful response. Done at the
        # client boundary so it covers all features (phone, story,
        # event, etc.) without each call site having to remember.
        if text and not err:
            text = _strip_emojis(text)
        # An empty reply used to reach callers as ("", None): no message
        # AND no error, so the player saw nothing at all.
        if not err and not (text or "").strip():
            err = "The AI sent back an empty reply. Try again, or try a different model."
        if err:
            _log_failure(provider, model, err, time.time() - started)
            err = _explain_model_gone(provider, model, err)
            err = _explain_busy(provider, err)
        if callback:
            callback(text, err)

    thread = threading.Thread(target=_request, daemon=True, name="Llamafone-Request")
    thread.start()
    return thread

