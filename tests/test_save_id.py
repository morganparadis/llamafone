import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
"""Out-of-game simulation of save_id resolution across the scenarios
that broke: launch load, in-game load of another save (slot 0), first
save, reload both ways, Autosave, Save-As fork, scratch/autosave saves."""
import sys, types, tempfile, os, importlib, json

REPO = _os.path.join(_REPO, "src")

# ---- game state the stubs read -------------------------------------------
STATE = {"slot_id": None, "slot_name": "", "household": None, "zone": True, "pref": None}
AUTO = 0x7FFFFFFE  # arbitrary stand-in for AUTO_SAVE_SLOT_ID


class _Slot:
    @property
    def slot_id(self):
        return STATE["slot_id"]

    @property
    def slot_name(self):
        return STATE["slot_name"]

    @property
    def preferred_manual_slot_id(self):
        return STATE["pref"] or 0

    def HasField(self, name):
        assert name == "preferred_manual_slot_id"
        return STATE["pref"] is not None


class _PS:
    auto_save_slot_id = None

    def get_save_slot_proto_buff(self):
        return _Slot()

    def get_save_slot_proto_guid(self):
        return 3784179717  # the shared constant -- must NOT matter

    def save_game_gen(self, timeline, save_game_data, *a, **k):
        # stand-in generator mirroring the real one's contract
        yield "saving"
        if STATE.get("fail_next"):
            STATE["fail_next"] = False
            return "SAVE_FAILED"
        if save_game_data.slot_id not in (0, AUTO):
            STATE["slot_id"] = save_game_data.slot_id  # game writes proto on save
            STATE["slot_name"] = save_game_data.slot_name
            STATE["pref"] = save_game_data.slot_id
        targets = []
        if save_game_data.slot_id == AUTO:
            targets.append(AUTO)
        if save_game_data.auto_save_slot_id:
            targets.append(save_game_data.auto_save_slot_id)
        for t in targets:
            STATE["writes"] = STATE.get("writes", 0) + 1
            with open(os.path.join(TMP, f"Slot_{t:08x}.save"), "wb") as f:
                f.write(b"save#%d" % STATE["writes"])
        return "SAVE_OK"


class _Zone:
    @property
    def save_slot_data_id(self):
        return STATE["slot_id"] if STATE["slot_id"] is not None else 0


class _HH:
    @property
    def id(self):
        return STATE["household"]


services = types.ModuleType("services")
_ps_instance = _PS()
services.get_persistence_service = lambda: _ps_instance
services.current_zone = lambda: _Zone() if STATE["zone"] else None
services.active_household = lambda: _HH() if STATE["household"] else None
services.client_manager = None
ps_mod = types.ModuleType("services.persistence_service")
ps_mod.PersistenceService = _PS
ps_mod.AUTO_SAVE_SLOT_ID = AUTO
ps_mod.SaveGameResult = types.SimpleNamespace(SUCCESS="SAVE_OK")
services.persistence_service = ps_mod
sys.modules["services"] = services
sys.modules["services.persistence_service"] = ps_mod

pkg = types.ModuleType("llamafone")
pkg.__path__ = [os.path.join(REPO, "llamafone")]
sys.modules["llamafone"] = pkg
_ms_stub = types.ModuleType("llamafone.milestones"); _ms_stub.start_background_scan = lambda: None; _ms_stub.scan_sims = lambda sims: None
sys.modules["llamafone.milestones"] = _ms_stub
_soc_stub = types.ModuleType("llamafone.social"); _soc_stub.resume_pending = lambda: None
sys.modules["llamafone.social"] = _soc_stub
sid = importlib.import_module("llamafone.save_id")

LOG = []
sid._log = lambda m: LOG.append(m)
TMP = tempfile.mkdtemp()
sid._saves_folder = lambda: TMP

from collections import namedtuple
SaveGameData = namedtuple("SaveGameData", ("slot_id", "slot_name", "force_override", "auto_save_slot_id"))

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        FAILS.append(label)


def load(slot_id, name, household, pref=None):
    STATE.update(slot_id=slot_id, slot_name=name, household=household, zone=True, pref=pref)


def do_save(slot_id, name, autosave=AUTO):
    gen = _PS.save_game_gen(_ps_instance, None, SaveGameData(slot_id, name, False, autosave))
    out = []
    try:
        while True:
            out.append(next(gen))
    except StopIteration as e:
        return out, e.value


HF, HT = 257293171454641379, 735785604000000001

print("install hook")
check("install_save_hook", sid.install_save_hook(), True)
import inspect
check("wrapper is a generator function", inspect.isgeneratorfunction(_PS.save_game_gen), True)

notif = types.ModuleType("llamafone.notifications")
NOTES = []
notif.show = lambda t, m, output=None: NOTES.append(t)
sys.modules["llamafone.notifications"] = notif
sid._cancel = None

notif = types.ModuleType("llamafone.notifications")
NOTES = []
notif.show = lambda t, m, output=None: NOTES.append(t)
sys.modules["llamafone.notifications"] = notif

def fresh():
    sid._last_real_save_id = None; sid._last_real_slot_name = None
    sid._last_handled_save_id = None

print("1. main-menu load Francesca (live slot 0x1115)")
fresh(); load(0x1115, "Francesca Legacy", HF, pref=0x1115)
check("live", sid.get_current_save_id(), "Slot_00001115")

print("2. Autosave from Francesca (slot 0, pref 0x1115)")
fresh(); load(0, "Autosave", HF, pref=0x1115)
check("-> Francesca", sid.get_current_save_id(), "Slot_00001115")
check("not dormant", sid.dormant_reason(), None)

print("3. Autosave from FAT reported as 0xffffffff (pref 3)")
fresh(); load(0xFFFFFFFF, "Autosave", HT, pref=3)
check("-> FAT", sid.get_current_save_id(), "Slot_00000003")

print("4. in-game load FAT while Francesca was live (slot 0, pref 3)")
fresh(); load(0x1115, "Francesca Legacy", HF, pref=0x1115); sid.get_current_save_id()
load(0, "FAT", HT, pref=3)
check("-> FAT, not Francesca", sid.get_current_save_id(), "Slot_00000003")

print("5. in-game load of a Save-As COPY (same name + household, pref 7)")
fresh(); load(0x1115, "Francesca Legacy", HF, pref=0x1115); sid.get_current_save_id()
load(0, "Francesca Legacy", HF, pref=7)
check("-> the copy's slot", sid.get_current_save_id(), "Slot_00000007")

print("6. old-patch save in-game loaded (no record) -> dormant, then save -> on")
fresh(); load(0, "Old Save", 999, pref=None)
check("dormant", sid.get_current_save_id(), None)
check("reason", sid.dormant_reason(), "unidentified")
NOTES.clear(); sid._show_dormant_notice()
check("notice", NOTES, ["Llamafone: save to enable"])
NOTES.clear(); out, result = do_save(0x20, "Old Save")
check("generator yields", out, ["saving"]); check("generator returns", result, "SAVE_OK")
check("'on' notice", NOTES, ["Llamafone is on"])
check("now resolves", sid.get_current_save_id(), "Slot_00000020")

print("7. old Autosave without record -> dormant with Autosave notice")
fresh(); load(0, "Autosave", HF, pref=None)
check("dormant", sid.get_current_save_id(), None)
check("reason", sid.dormant_reason(), "autosave")
NOTES.clear(); sid._show_dormant_notice()
check("notice", NOTES, ["Llamafone: unknown Autosave"])

print("8. sentinel pref values rejected")
fresh(); load(0, "Autosave", HF, pref=0xFFFFFFFF)
check("0xffffffff pref -> dormant", sid.get_current_save_id(), None)

print("9. scratch saves ignored")
fresh(); load(0, "Old Save", 999, pref=None)
NOTES.clear(); do_save(0, "scratch", autosave=None)
check("still dormant", sid.get_current_save_id(), None); check("no notices", NOTES, [])

print("10. main menu")
STATE["zone"] = False
check("None", sid.get_current_save_id(), None); check("no reason", sid.dormant_reason(), None)

print()
print("RESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAIL: {FAILS}")
print("--- log lines emitted:")
for l in LOG:
    print("   ", l[:170])
