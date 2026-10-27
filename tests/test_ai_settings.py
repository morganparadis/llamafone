import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
"""Click through the in-game AI settings with stubbed Sims 4 dialog classes
and a temp copy of the release cfg; check what the rows show and what gets
written back to the cfg."""
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
mod("interactions.base.super_interaction", SuperInteraction=object)

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
notif = mod("llamafone.notifications", show=lambda t, m, output=None: NOTES.append(m))
pkg.notifications = notif

import llamafone.config as config
config._find_config_file = lambda: CFG
config.reload_config()
import llamafone.api_client as api
CALLS = []
def fake_call(messages, system=None, use_fast_model=False, callback=None, **k):
    CALLS.append(config.get_fast_model()); callback("pong", None)
api.call_ai_async = fake_call
api.check_ollama_health = lambda ep: {"reachable": True, "models": ["llama3.2:3b", "gemma3:4b"]}
import llamafone.phone_ui_interactions as ui
ui._log = lambda *a, **k: None

fails = []
def check(label, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + label); cond or fails.append(label)

def last(): return SHOWN[-1]
def names(d): return [r.name for r in d.rows]
def pick(d, label_start):
    idx = next(i for i, r in enumerate(d.rows) if r.name.startswith(label_start))
    resp = types.SimpleNamespace(accepted=True, picked_results=[d.rows[idx].option_id])
    for f in d.listeners: f(resp)
def type_in(d, value):
    d.text_input_responses = {"value": value}
    d.accepted = True
    for f in d.listeners: f(d)
def cfgval(key):
    m = re.search(rf"^{key}\s*=\s*(.*)$", open(CFG, encoding="utf-8").read(), re.M)
    return m.group(1).strip() if m else None

# ---- main screen ---------------------------------------------------------
ui._show_settings_picker("sim")
main = last()
check("main: 'AI settings...' is the first row", main.rows[0].name == "AI settings...")
check("main: Llamagram rows moved out", not any(n.startswith(("How often friends post","Llamagram:","Friends' posts pop up","Comment / like")) for n in names(main)))
check("main: Llamagram... submenu row", "Llamagram..." in names(main))
print("  main screen rows:", names(main))
check("main: 8 rows", len(main.rows) == 8)
check("main: folders present", all(x in names(main) for x in ("Incoming calls & texts...", "Texts & replies...", "Llamagram...")))
pick(main, "Incoming calls & texts...")
inc = last(); print("  incoming rows:", names(inc))
check("incoming: title", "Incoming" in inc.title)
check("incoming: Birth announcements row shows ON", "Birth announcements: ON" in names(inc))
check("incoming: 6 rows", len(inc.rows) == 6)
pick(inc, "Birth announcements")
check("incoming: toggle returns to the folder, now OFF", "Birth announcements: OFF" in names(last()))
pick(last(), "Birth announcements")
pick(last(), "Auto-event chance"); type_in(last(), "40")
check("incoming: typed number returns to the folder", last().title == inc.title and any(x.startswith("Auto-event chance: 40") for x in names(last())))
pick(last(), "Incoming calls")
check("incoming: event toggle returns to the folder", last().title == inc.title)
pick(last(), "Incoming calls")
for f in last().listeners: f(types.SimpleNamespace(accepted=False, picked_results=[]))
check("incoming: Back returns to main", names(last())[0] == "AI settings...")
pick(last(), "Texts & replies...")
tx = last(); print("  texting rows:", names(tx))
check("texting: 9 rows", len(tx.rows) == 9)
pick(tx, "Reply delay max"); type_in(last(), "12")
check("texting: typed number returns to the folder", last().title == tx.title)
for f in last().listeners: f(types.SimpleNamespace(accepted=False, picked_results=[]))
main = last()
pick(main, "Llamagram...")
lg = last()
check("submenu has the 4 Llamagram rows", lg.title == "Llamagram" and len(lg.rows) == 4 and "How often friends post: Sometimes" in names(lg))
main = lg
check("main: intro no longer says model/language are cfg-only", "model, language" not in main.text)
pick(main, "How often friends post")
check("friends' posts -> Often written to cfg", cfgval("social_npc_post_weight") == "50")
check("stays in the Llamagram submenu after a tap", last().title == "Llamagram" and "How often friends post: Often" in names(last()))
pick(last(), "Llamagram:")
check("Llamagram off written, still in submenu", cfgval("social_enabled") == "false" and last().title == "Llamagram")
pick(last(), "Llamagram:")
for f in last().listeners: f(types.SimpleNamespace(accepted=False, picked_results=[]))
check("Back -> main screen", last().rows[0].name == "AI settings...")
pick(last(), "Incoming calls & texts..."); pick(last(), "Birth announcements")
check("birth announcements -> false written to cfg", cfgval("birth_announcements") == "false")
check("folder reopened showing OFF", "Birth announcements: OFF" in names(last()))
for f in last().listeners: f(types.SimpleNamespace(accepted=False, picked_results=[]))

# ---- AI submenu ------------------------------------------------------------
pick(last(), "AI settings...")
ai = last()
print("   AI rows:", names(ai))
check("AI: model row shows claude-haiku-5-5", "Model: claude-haiku-5-5" in names(ai))
check("AI: thinking row shows AUTO (no line in cfg)", "Thinking: AUTO" in names(ai))
check("AI: thinking hint says xhigh in use", "Now using: xhigh" in next(r.desc for r in ai.rows if r.name.startswith("Thinking")))
check("AI: max tokens 1,024 (new default)", "Max tokens: 1,024" in names(ai))
check("AI: language English", "Language: English" in names(ai))

pick(ai, "Thinking")
check("thinking -> low, line appended to cfg", cfgval("thinking_effort") == "low")
check("AI reopened showing LOW", "Thinking: LOW" in names(last()))
for _ in range(4): pick(last(), "Thinking")
check("thinking cycles back to auto after 5 taps (no max)", cfgval("thinking_effort") == "auto")

pick(last(), "Max tokens"); type_in(last(), "2,048")
check("max tokens typed 2,048 -> 2048", cfgval("max_tokens") == "2048" and "Max tokens: 2,048" in names(last()))
pick(last(), "Max tokens"); type_in(last(), "abc")
check("max tokens: non-number ignored", cfgval("max_tokens") == "2048")
pick(last(), "Max tokens"); type_in(last(), "100")
check("max tokens: tiny value raised to 256", cfgval("max_tokens") == "256")
pick(last(), "Max tokens"); type_in(last(), "1024")
check("max tokens back to 1024", cfgval("max_tokens") == "1024")

pick(last(), "Language")
check("language opens a text box prefilled", isinstance(last(), TextInput))
type_in(last(), "  Spanish ")
check("language saved trimmed", cfgval("language") == "Spanish" and "Language: Spanish" in names(last()))
pick(last(), "Language"); type_in(last(), "   ")
check("blank language ignored", cfgval("language") == "Spanish")

# ---- model: text box (one model: `model`) --------------------------------------
pick(last(), "Model:")
check("model opens a text box", isinstance(last(), TextInput))
type_in(last(), "claude-sonnet-5-5")
check("typed model saved to `model`", cfgval("model") == "claude-sonnet-5-5")
check("notification after model change", any("Model set to claude-sonnet-5-5" in n for n in NOTES))
check("row shows the model", "Model: claude-sonnet-5-5" in names(last()))
check("thinking row: Sonnet auto -> model default", "Now using: model default" in next(r.desc for r in last().rows if r.name.startswith("Thinking")))
pick(last(), "Model:"); type_in(last(), "   ")
check("blank model ignored", cfgval("model") == "claude-sonnet-5-5")
pick(last(), "Model:"); type_in(last(), "claude-haiku-4-5")
check("second change saved", cfgval("model") == "claude-haiku-4-5" and config.get_model() == "claude-haiku-4-5")
pick(last(), "Model:")
for f in last().listeners: f(types.SimpleNamespace(accepted=False, text_input_responses={}))
check("Cancel on model box -> back to AI settings, unchanged", last().title == "AI settings" and cfgval("model") == "claude-haiku-4-5")

# ---- test connection ---------------------------------------------------------
NOTES.clear(); pick(last(), "Test connection")
check("test connection uses current model", CALLS[-1] == "claude-haiku-4-5")
check("test connection reports OK", any(n.startswith("Connection OK") for n in NOTES))

# ---- other providers ---------------------------------------------------------
config.set_setting("provider", "ollama")
ui._show_ai_settings_picker("sim")
check("ollama: no Thinking row", not any(n.startswith("Thinking") for n in names(last())))
check("header says how to change provider", "edit llamafone.cfg" in last().text and "llama.reload" in last().text)

# Back from AI settings -> main screen
config.set_setting("provider", "claude")
ui._show_ai_settings_picker("sim")
for f in last().listeners: f(types.SimpleNamespace(accepted=False, picked_results=[]))
check("Back from AI settings -> main settings", last().rows[0].name == "AI settings...")

# The cfg is still a valid cfg with comments intact
import configparser
cp = configparser.ConfigParser(); cp.read(CFG, encoding="utf-8")
check("cfg still parses", cp.get("llamafone", "model") == "claude-haiku-4-5")
check("cfg comments kept", "; The AI model that writes everything" in open(CFG, encoding="utf-8").read())

# ---- provider / server / big size -------------------------------------------
config.set_setting("provider", "claude"); NOTES.clear()
ui._show_ai_settings_picker("sim")
check("provider row shows Claude", "Provider: Claude" in names(last()))
check("no server row for a cloud provider", not any(n.startswith("Server address") for n in names(last())))
pick(last(), "Provider")
check("Provider opens a list", last().title == "Choose a provider" and "Claude  (current)" in names(last()))
check("list has all six", len(last().rows) == 6)
for f in last().listeners: f(types.SimpleNamespace(accepted=False, picked_results=[]))
check("Back changes nothing", cfgval("provider") == "claude" and last().title == "AI settings")
pick(last(), "Provider"); pick(last(), "Claude")
check("picking the current one changes nothing", cfgval("provider") == "claude")
pick(last(), "Provider"); pick(last(), "OpenAI")
check("pick OpenAI -> saved, back on AI settings, no model box", cfgval("provider") == "openai" and last().title == "AI settings")
check("reminder after switching", any("Provider set to OpenAI. Now set Model to an OpenAI model" in n for n in NOTES))
check("model unchanged by the switch", cfgval("model") == "claude-haiku-4-5")
check("row says what OpenAI needs", any("an OpenAI API key" in r.desc for r in last().rows if r.name.startswith("Provider")))
pick(last(), "Provider"); pick(last(), "Ollama")
check("switched to ollama", cfgval("provider") == "ollama")
check("local: row says no key", any("no API key" in r.desc for r in last().rows if r.name.startswith("Provider")))
check("server row shown for ollama", any(n.startswith("Server address") for n in names(last())))
pick(last(), "Server address"); type_in(last(), "http://127.0.0.1:11500")
check("ollama address saved", cfgval("ollama_endpoint") == "http://127.0.0.1:11500")
config.set_setting("provider", "claude")

# ---- incoming calls / texts --------------------------------------------------
config.set_setting("auto_event_types", "call, text, story"); config.set_setting("auto_event_weights", "call:50, text:50, story:10")
ui._show_settings_picker("sim"); pick(last(), "Incoming calls & texts...")
check("calls/texts rows ON", "Incoming calls: ON" in names(last()) and "Incoming texts: ON" in names(last()))
pick(last(), "Incoming calls")
check("calls off, story kept", cfgval("auto_event_types") == "text, story" and "Incoming calls: OFF" in names(last()))
pick(last(), "Incoming calls")
check("calls back on with a weight", "call" in cfgval("auto_event_types") and "call:50" in cfgval("auto_event_weights"))
config.set_setting("auto_event_weights", "text:50, story:10")
pick(last(), "Incoming calls"); pick(last(), "Incoming calls")
check("re-enabling adds a missing weight", "call:50" in cfgval("auto_event_weights"))

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
