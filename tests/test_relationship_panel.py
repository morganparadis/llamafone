import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
"""Relationships panel > click a sim > Llamafone > Call / Text: the entries
show only for sims who can use a phone, the clicked sim becomes the
recipient (no picker), sims outside the friendship-25 network still work,
the interactions go into the Sim's _relation_panel_affordances, and the
package ships them with their "Call" / "Text" labels in every language."""
import sys, types, os, re, struct, subprocess

REPO = _REPO
sys.path.insert(0, REPO + "/src")
fails = []


def chk(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)


def mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m


class LHT:
    @staticmethod
    def get_raw_text(t): return t


class SI:
    def __init__(self, sid, first, last="Woodruff", age="ADULT", ghost=False, pet=False):
        self.sim_id, self.first_name, self.last_name, self.age = sid, first, last, "Age." + age
        self.is_ghost, self.is_pet = ghost, pet
        self.relationship_tracker = object()


FRAN = SI(1, "Francesca", "Paradis")
LUCA, BEA, KID, CAT, GHOST = SI(2, "Luca"), SI(3, "Bea", "Stranger"), SI(4, "Kid", age="CHILD"), SI(5, "Cat", pet=True), SI(6, "Ghost", ghost=True)
SIMS = {s.sim_id: s for s in (FRAN, LUCA, BEA, KID, CAT, GHOST)}
mgr = types.SimpleNamespace(get=lambda i: SIMS.get(i), values=lambda: SIMS.values())

mod("sims4"); mod("sims4.localization", LocalizationHelperTuning=LHT); mod("sims4.commands"); mod("sims4.log")
mod("services", sim_info_manager=lambda: mgr)
mod("interactions"); mod("interactions.base")


class _BaseSI:
    TESTED = []
    def __init__(self, sim, interaction_parameters=None):
        self.sim, self.interaction_parameters = sim, interaction_parameters or {}
    @classmethod
    def _test(cls, target, context, **kwargs):
        cls.TESTED.append(kwargs)
        return True
    def _run_interaction_gen(self, timeline):
        if False:
            yield
        return True


mod("interactions.base.super_interaction", SuperInteraction=_BaseSI)


class TestResult:
    def __init__(self, ok, reason=""):
        self.ok, self.reason = ok, reason
    def __bool__(self): return self.ok


mod("event_testing"); mod("event_testing.results", TestResult=TestResult)

SHOWN = []


class TextInput:
    def __init__(self, sim, title, text, text_ok, text_cancel):
        self.title, self.listeners = title(), []
    def add_listener(self, f): self.listeners.append(f)
    def show_dialog(self, **k): SHOWN.append(self)
    def build_msg(self, **k): return None
    @classmethod
    def TunableFactory(cls):
        return types.SimpleNamespace(default=lambda *a, **k: cls(*a, **k))


mod("ui"); mod("ui.ui_dialog_generic", UiDialogTextInputOkCancel=TextInput)
mod("distributor"); mod("distributor.shared_messages", IconInfoData=lambda **k: None)

# The real phone.py helpers, run against stand-ins for the network and tracker.
src = open(os.path.join(REPO, "src", "llamafone", "phone.py"), encoding="utf-8").read()
NETWORK = {"rels": [{"sim_info": LUCA, "name": "Luca Woodruff", "friendship": 60}]}
sim_context = types.SimpleNamespace(
    get_main_sim_network=lambda si: ([], NETWORK["rels"]),
    _read_relationship_for_target=lambda rt, sid, sm: {"friendship": -9, "romance": 0, "status": "Acquaintances"})
ns = {"sim_context": sim_context}
for start, end in (("def _is_human_sim", "def _log_household_inspection"),
                   ("def _is_ghost", "def _is_age_appropriate_contact")):
    a, b = src.index(start), src.index(end)
    exec(compile(src[a:b], "phone_slice", "exec"), ns)
ns["_PHONE_ELIGIBLE_AGES"] = ("TEEN", "YOUNGADULT", "YOUNG_ADULT", "ADULT", "ELDER")
ns["_NON_HUMAN_SPECIES_RE"] = None
SENT = []
phone = mod("llamafone.phone", _is_phone_eligible=ns["_is_phone_eligible"], _is_ghost=ns["_is_ghost"],
            contact_for_sim_info=ns["contact_for_sim_info"],
            send_call=lambda c, m: SENT.append(("call", c["name"], m)),
            send_text=lambda c, m: SENT.append(("text", c["name"], m)))

pkg = types.ModuleType("llamafone"); pkg.__path__ = [REPO + "/src/llamafone"]; sys.modules["llamafone"] = pkg
NOTES = []
pkg.phone = phone
for n in ("sim_context", "dating"):
    setattr(pkg, n, mod("llamafone." + n))
pkg.notifications = mod("llamafone.notifications", show=lambda t, m, output=None: NOTES.append(m),
                        show_error=lambda m, *a, **k: NOTES.append("ERROR: " + m))
import llamafone.phone_ui_interactions as ui
ui._log = ui._log_exc = lambda *a, **k: None

ctx = types.SimpleNamespace(sim=types.SimpleNamespace(sim_info=FRAN))
for cls in (ui.LlamafoneRelCallInteraction, ui.LlamafoneRelTextInteraction):
    n = cls.__name__
    chk(f"{n}: shown for a friend", bool(cls._test(None, ctx, picked_item_ids={2})))
    chk(f"{n}: shown for an acquaintance outside the network", bool(cls._test(None, ctx, picked_item_ids={3})))
    for sid, who in ((4, "a child"), (5, "a pet"), (6, "a ghost"), (1, "yourself"), (99, "an unknown id")):
        chk(f"{n}: hidden for {who}", not cls._test(None, ctx, picked_item_ids={sid}))
    chk(f"{n}: hidden with no picked sim", not cls._test(None, ctx))
chk("base _test still runs (and gets the picked ids)", {"picked_item_ids": {2}} in _BaseSI.TESTED)


def fire(cls, sid):
    SHOWN.clear(); NOTES.clear(); SENT.clear()
    it = cls(types.SimpleNamespace(sim_info=FRAN), {"picked_item_ids": frozenset([sid])})
    ui._mod_ready = lambda: True
    g = it._run_interaction_gen(None)
    try:
        while True:
            next(g)
    except StopIteration:
        pass


fire(ui.LlamafoneRelCallInteraction, 2)
chk("Call: goes straight to the message box for the clicked sim", [d.title for d in SHOWN] == ["Call Luca Woodruff"])
fire(ui.LlamafoneRelTextInteraction, 3)
chk("Text an acquaintance: message box for them", [d.title for d in SHOWN] == ["Text Bea Stranger"])
c = ns["contact_for_sim_info"](FRAN, BEA)
chk("acquaintance contact carries real scores (barely-know tier applies)",
    c["friendship"] == -9 and c["status"] == "Acquaintances" and c["sim_id"] == 3)
chk("network contact is reused as-is", ns["contact_for_sim_info"](FRAN, LUCA) is NETWORK["rels"][0])
fire(ui.LlamafoneRelTextInteraction, 99)
chk("vanished sim: friendly error, no dialog", not SHOWN and NOTES == ["ERROR: Couldn't tell which sim you picked. Try again."])
SHOWN.clear()
it = ui.LlamafoneRelCallInteraction(types.SimpleNamespace(sim_info=FRAN), {"picked_item_ids": {2}})
ui._mod_ready = lambda: False
for _ in it._run_interaction_gen(None):
    pass
chk("not set up yet: no dialog", not SHOWN)

# Injection goes into the Relationships-panel list, phone tiles stay on the phone.
import llamafone.phone_ui_injection as inj
inj._log = lambda m: None
found = {n: type(n, (), {}) for n in inj._PHONE_INTERACTION_NAMES + inj._RELATION_PANEL_INTERACTION_NAMES}
inj._find_interaction_tuning = lambda n: found.get(n)
Sim = type("Sim", (), {"__module__": "sims.sim", "_phone_affordances": (), "_relation_panel_affordances": ()})
mod("sims4.resources", Types=types.SimpleNamespace(OBJECT="OBJECT"))
sys.modules["services"].get_instance_manager = lambda t: types.SimpleNamespace(types={1: Sim})
inj._inject_affordances()
chk("Relationships panel gets RelCall + RelText + RelMessages",
    [c.__name__ for c in Sim._relation_panel_affordances] == ["Llamafone_RelCall", "Llamafone_RelText", "Llamafone_RelMessages"])
chk("phone tiles unchanged", [c.__name__ for c in Sim._phone_affordances] == list(inj._PHONE_INTERACTION_NAMES))

# Package: the tunings point at our classes and at strings every language has.
pb = open(os.path.join(REPO, "tools", "package_builder.py"), encoding="utf-8").read()
for xml, cls, s in (("Llamafone_RelCall.xml", "LlamafoneRelCallInteraction", "Call"),
                    ("Llamafone_RelText.xml", "LlamafoneRelTextInteraction", "Text")):
    x = open(os.path.join(REPO, "package_src", xml), encoding="utf-8").read()
    h = re.search(r'<T n="display_name">(0x[0-9A-F]+)</T>', x).group(1)
    chk(f"{xml}: class {cls} exists", f'c="{cls}"' in x and hasattr(ui, cls))
    chk(f"{xml}: label {h} is '{s}' in our string table", re.search(h + r':\s*"' + s + '"', pb) is not None)
    chk(f"{xml}: under the Llamafone category", '<T n="category">13371000010</T>' in x)
ids = [re.search(r' s="(\d+)"', open(os.path.join(REPO, "package_src", f), encoding="utf-8").read()).group(1)
       for f in os.listdir(os.path.join(REPO, "package_src")) if f.endswith(".xml")]
chk("tuning ids are unique", len(ids) == len(set(ids)))

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
