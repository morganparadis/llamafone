"""New-player walkthrough for each of the six providers, from the release
llamafone.cfg: (cloud only) paste the key into the cfg and reload, open
Settings from the phone tile, AI settings > Provider > pick it, Model > type
a name, Test connection, then the Call tile is no longer held back. Real
api_client code runs; only curl is faked. Also checks no key ever goes in a
request URL."""
import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import sys, types, os, shutil, tempfile, re, threading, time

REPO = _REPO
sys.path.insert(0, REPO + "/src")
TMP = tempfile.mkdtemp()
CFG = os.path.join(TMP, "llamafone.cfg")
shutil.copy(REPO + "/llamafone.cfg", CFG)
# A pre-3.8.4 player: no thinking_effort line in their cfg.
txt = open(CFG, encoding="utf-8").read()
txt = re.sub(r"\n; How hard Claude thinks.*?thinking_effort = auto\n", "\n", txt, flags=re.S)
assert "thinking_effort" not in txt
open(CFG, "w", encoding="utf-8").write(txt)

# ---- stub game modules ------------------------------------------------------
def mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m

class LHT:
    @staticmethod
    def get_raw_text(t): return t
mod("sims4"); mod("sims4.localization", LocalizationHelperTuning=LHT); mod("sims4.commands"); mod("sims4.log")
mod("services"); mod("interactions"); mod("interactions.base")
class _SI:
    def __init__(self, sim): self.sim = sim
    def _run_interaction_gen(self, timeline):
        if False: yield
        return True
mod("interactions.base.super_interaction", SuperInteraction=_SI)

SHOWN = []   # every dialog shown, in order

class Row:
    def __init__(self, option_id, name, row_description, is_enable=True):
        self.option_id, self.name, self.desc = option_id, name, row_description

class Picker:
    def __init__(self, sim, title, text, text_ok, text_cancel):
        self.title, self.text, self.rows, self.listeners = title(), text(), [], []
    def add_row(self, r): self.rows.append(r)
    def add_listener(self, f): self.listeners.append(f)
    def show_dialog(self): SHOWN.append(self)
    @classmethod
    def TunableFactory(cls):
        return types.SimpleNamespace(default=lambda *a, **k: cls(*a, **k))

class TextInput:
    def __init__(self, sim, text, title, text_ok, text_cancel):
        self.title, self.text, self.rows, self.listeners = title(), text(), [], []
        self.text_input_responses = {}
    def add_listener(self, f): self.listeners.append(f)
    def show_dialog(self): SHOWN.append(self)
    def build_msg(self, **k): return None
    @classmethod
    def TunableFactory(cls):
        return types.SimpleNamespace(default=lambda *a, **k: cls(*a, **k))

mod("ui"); mod("ui.ui_dialog_picker", UiItemPicker=Picker, BasePickerRow=Row)
mod("ui.ui_dialog_generic", UiDialogTextInputOkCancel=TextInput)

pkg = types.ModuleType("llamafone"); pkg.__path__ = [REPO + "/src/llamafone"]; sys.modules["llamafone"] = pkg
pkg.MOD_VERSION = "test"
NOTES = []
for n in ("phone", "sim_context", "dating"):
    m = mod("llamafone." + n); setattr(pkg, n, m)
notif = mod("llamafone.notifications", show=lambda t, m, output=None: NOTES.append(m), show_error=lambda m, *a, **k: NOTES.append("ERROR: " + m))
pkg.notifications = notif

import llamafone.config as config
config._find_config_file = lambda: CFG
config.reload_config()
import llamafone.api_client as api


import shutil, json, threading
import llamafone.api_client as api
import llamafone.phone_ui_interactions as ui
ui._log = lambda *a, **k: None
api._log_failure = lambda *a, **k: None
api._log_prompt = lambda *a, **k: None
RELEASE = os.path.join(_REPO, "llamafone.cfg")
KEY = "test-key-0123456789"
REQ = []
OPENAI_SHAPE = json.dumps({"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]})
def fake_curl(url, headers, body, **k):
    REQ.append((url, dict(headers or {}), body))
    if "anthropic" in url:
        return json.dumps({"content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn"}), None, 0
    if "googleapis" in url:
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}, "finishReason": "STOP"}]}), None, 0
    if "/api/chat" in url:
        return json.dumps({"message": {"content": "pong"}, "done": True}), None, 0
    return OPENAI_SHAPE, None, 0
api._curl = fake_curl
_orig_call = api.call_ai_async
def _call_and_wait(*a, **k):
    t = _orig_call(*a, **k)
    if t is not None:
        t.join(10)
    return t
api.call_ai_async = _call_and_wait

fails = []
def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        fails.append(label)
def last(): return SHOWN[-1]
def pick(d, label_start):
    idx = next(i for i, r in enumerate(d.rows) if r.name.startswith(label_start))
    resp = types.SimpleNamespace(accepted=True, picked_results=[d.rows[idx].option_id])
    for f in d.listeners: f(resp)
def type_in(d, value):
    d.text_input_responses = {"value": value}; d.accepted = True
    for f in d.listeners: f(d)
def run_tile(cls):
    SHOWN.clear(); NOTES.clear()
    it = cls(types.SimpleNamespace(first_name="Fran", sim_info=types.SimpleNamespace(first_name="Fran")))
    g = it._run_interaction_gen(None)
    try:
        while True: next(g)
    except StopIteration:
        pass

CASES = [("claude", "Claude", "claude-haiku-5-5", True, "anthropic.com"),
         ("openai", "OpenAI", "gpt-4o-mini", True, "openai.com"),
         ("gemini", "Gemini", "gemini-2.5-flash", True, "googleapis.com"),
         ("openrouter", "OpenRouter", "deepseek/deepseek-chat", True, "openrouter.ai"),
         ("ollama", "Ollama", "llama3.2:3b", False, ":11434/"),
         ("lmstudio", "LM Studio", "llama-3.2-3b-instruct", False, ":1234/")]
for prov, label, model, needs_key, host in CASES:
    print(f"--- {label}")
    shutil.copy(RELEASE, CFG); config.reload_config()
    if needs_key:   # the player pastes their key into llamafone.cfg, then llama.reload
        txt = open(CFG, encoding="utf-8").read().replace("api_key = YOUR_API_KEY_HERE", f"api_key = {KEY}")
        open(CFG, "w", encoding="utf-8").write(txt); config.reload_config()
    run_tile(ui.LlamafoneSettingsInteraction)
    check(f"{label}: Settings opens", bool(SHOWN) and "Llamafone Settings" in SHOWN[0].title and not NOTES)
    if not SHOWN:
        continue
    pick(last(), "AI settings...")
    check(f"{label}: AI settings opens", last().title == "AI settings")
    pick(last(), "Provider"); pick(last(), label)
    check(f"{label}: provider saved", config.get_provider() == prov and last().title == "AI settings")
    pick(last(), "Model"); type_in(last(), model)
    check(f"{label}: model saved", config.get_model() == model)
    REQ.clear(); NOTES.clear()
    pick(last(), "Test connection")
    ok_note = any("Connection OK" in n for n in NOTES)
    check(f"{label}: Test connection OK", ok_note)
    if not ok_note: print("     notes:", NOTES)
    check(f"{label}: request went to {host}", bool(REQ) and host in REQ[-1][0])
    check(f"{label}: key never in a URL", all(KEY not in u for u, _h, _b in REQ))
    run_tile(ui.LlamafoneCallInteraction)
    check(f"{label}: Call tile no longer held back", not any("set up yet" in n for n in NOTES))
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
