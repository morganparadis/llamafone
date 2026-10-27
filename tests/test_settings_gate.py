import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
"""A fresh install (release cfg: provider = claude, placeholder key) can open
Llamafone Settings from the phone tile; Call stays behind the "not set up"
message; on Ollama no key is needed. Drives the real phone-tile
interactions, not the picker functions directly."""
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

import llamafone.phone_ui_interactions as ui
ui._log = lambda *a, **k: None
config.set_setting("provider", "claude")
assert not config.is_configured(), "release cfg should start unconfigured"
fails = []


def run(cls):
    SHOWN.clear(); NOTES.clear()
    it = cls(types.SimpleNamespace(first_name="Fran", sim_info=types.SimpleNamespace(first_name="Fran")))
    g = it._run_interaction_gen(None)
    try:
        while True:
            next(g)
    except StopIteration:
        pass
    return [d.title for d in SHOWN], list(NOTES)


shown, notes = run(ui.LlamafoneSettingsInteraction)
print("Settings ->", shown, notes)
if not shown or "Llamafone Settings" not in shown[0] or notes:
    fails.append("Settings did not open on a fresh install")
shown, notes = run(ui.LlamafoneCallInteraction)
print("Call ->", shown, [n[:60] for n in notes])
if shown or not any("set up yet" in n for n in notes):
    fails.append("Call was not held back on a fresh install")
config.set_setting("provider", "ollama")
shown, notes = run(ui.LlamafoneCallInteraction)
print("Call on Ollama ->", [n[:60] for n in notes])
if any("set up yet" in n for n in notes):
    fails.append("Ollama (no key) was wrongly held back")
print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
