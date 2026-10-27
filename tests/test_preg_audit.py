import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
"""Audit fixes for the v3.7 pregnancy / birth awareness code."""
import sys, types, os, importlib
REPO = _os.path.join(_REPO, "src")
FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        FAILS.append(label)


class HH:
    def __init__(s, m):
        s.m = m; s.funds = types.SimpleNamespace(money=5000); s.id = 1

    def sim_info_gen(s):
        return iter(s.m)


class Gen:
    def __init__(s, mother=None, father=None):
        s.m, s.f = mother, father

    def get_family_relationship(s, idx):
        return s.m if idx == "MOTHER" else s.f

    def get_children_sim_ids_gen(s):
        return iter([])


class SI:
    def __init__(s, sid, first, age="ADULT", household=None):
        s.sim_id, s.first_name, s.last_name, s.age = sid, first, "X", "Age." + age
        s.household = household; s.household_id = 1
        s.spouse_sim_id = None; s.pregnancy_tracker = None; s.is_pregnant = False; s.is_dead = False
        s.genealogy = Gen()


MOM = SI(1, "Ingrid"); DAD = SI(2, "Ben"); BABY = SI(3, "Marlon", "BABY"); BABY.age_progress = 0.04
BABY.genealogy = Gen(mother=1, father=2)
DAD.genealogy.get_children_sim_ids_gen = lambda: iter([3])
hh = HH([MOM, BABY]); MOM.household = hh; BABY.household = hh
SIMS = {1: MOM, 2: DAD, 3: BABY}
services = types.ModuleType("services")
services.sim_info_manager = lambda: types.SimpleNamespace(get=lambda i: SIMS.get(int(i)))
services.active_household = lambda: hh
services.household_manager = lambda: types.SimpleNamespace(get=lambda i: hh)
sys.modules["services"] = services
sims_pkg = types.ModuleType("sims"); gt = types.ModuleType("sims.genealogy_tracker")
gt.FamilyRelationshipIndex = types.SimpleNamespace(MOTHER="MOTHER", FATHER="FATHER")
sys.modules["sims"] = sims_pkg; sys.modules["sims.genealogy_tracker"] = gt

pkg = types.ModuleType("llamafone"); pkg.__path__ = [os.path.join(REPO, "llamafone")]; sys.modules["llamafone"] = pkg
LOG = []
pkg._log = lambda m: LOG.append(m)
for name in ("config", "sim_context", "save_id", "contact_prefs", "phone"):
    m = types.ModuleType("llamafone." + name); sys.modules["llamafone." + name] = m; setattr(pkg, name, m)
sys.modules["llamafone.sim_context"].get_sim_traits = lambda si, limit=12: []
sys.modules["llamafone.phone"]._get_sims_on_active_lot = lambda: set()
sys.modules["llamafone.save_id"].get_current_save_id = lambda: "Slot_00000001"
sys.modules["llamafone.save_id"].data_path = lambda fn: None
ms = importlib.import_module("llamafone.milestones")
births = importlib.import_module("llamafone.births")
ms._log = lambda m: LOG.append(m)

print("1. grandparents are the grandparent tier, not the parent tier")
ph_src = open(os.path.join(REPO, "llamafone", "phone.py"), encoding="utf-8").read()
start = ph_src.index("def _heard_after_days"); end = ph_src.index("def _contact_knowledge")
ns = {}
exec(compile(ph_src[start:end], "phone_tiers", "exec"), ns)
h = ns["_heard_after_days"]
check("Grandmother -> 1 day", h("Grandmother", 80), 1.0)
check("Grandson -> 1 day", h("Grandson", 80), 1.0)
check("Mother -> 0.1", h("Mother", 80), 0.1)
check("Mother-in-law -> 1.5", h("Mother-in-law", 80), 1.5)
check("births rank: Grandmother below Sister", births._family_rank("Grandmother") < births._family_rank("Sister"), True)

print("2. scan: a cleared pregnancy with no baby is not a birth")
prev = {"is_pregnant": True, "pregnancy_visibility": "visible", "is_dead": False}
curr = {"is_pregnant": False, "pregnancy_visibility": "none", "is_dead": False}
ev = ms._diff(prev, dict(curr), "Ingrid", sim_info=MOM)
check("mother with a baby in the household: pregnancy_end recorded", [e["type"] for e in ev], ["pregnancy_end"])
NOBABY = SI(9, "Culled"); NOBABY.household = HH([NOBABY])
ev = ms._diff(prev, dict(curr), "Culled", sim_info=NOBABY)
check("no baby of hers: no pregnancy_end", [e["type"] for e in ev], [])
check("logged as culled/cleared", any("culled / cleared" in l for l in LOG), True)
ev = ms._diff(prev, dict(curr), "Legacy")
check("no sim_info (legacy caller): behaves as before", [e["type"] for e in ev], ["pregnancy_end"])
BABY.genealogy = Gen(mother=7, father=8)   # someone else's baby in the house
ev = ms._diff(prev, dict(curr), "Ingrid", sim_info=MOM)
check("a roommate's baby doesn't count", [e["type"] for e in ev], [])
BABY.genealogy = Gen(mother=1, father=2)

print("3. durable announce dedup")
STORE = [{"type": "pregnancy_end", "sim_id": "1", "sim_ticks": 1000, "told": [{"by": "1", "to": "2"}]}]
ms._load_milestones = lambda: STORE
ms._now_sim_ticks = lambda: 1000 + 10
check("told record on a fresh birth -> already announced", ms.birth_already_announced(1), True)
ms._now_sim_ticks = lambda: 1000 + 5 * ms._TICKS_PER_DAY
check("told record on an OLD birth doesn't block a new one", ms.birth_already_announced(1), False)
STORE[0].pop("told")
ms._now_sim_ticks = lambda: 1000 + 10
check("no told record -> not announced", ms.birth_already_announced(1), False)

import datetime as _dt
NOW_ISO = _dt.datetime.now().isoformat()  # prompts only include the last 7 real days
print("4. the other parent is NOT assumed to know -- but hears at parent speed")
STORE = [{"type": "pregnancy_end", "description": "Ingrid had a baby", "sim_id": "1", "sim_name": "Ingrid", "sim_ticks": 1000, "timestamp": NOW_ISO, "mirrored": True},
         {"type": "pregnancy_end", "description": "Ben and Ingrid had a baby", "sim_id": "2", "sim_name": "Ben", "mirror_of": "1", "sim_ticks": 1000, "timestamp": NOW_ISO}]
ms._load_milestones = lambda: STORE
ms._get_pregnancy_visibility = lambda si: "none"
ms.mark_referenced = lambda *a, **k: None
sys.modules["llamafone.births"]._newborns = lambda si, **k: []
ms._now_sim_ticks = lambda: 1000 + int(0.05 * ms._TICKS_PER_DAY)    # ~1 sim hour after the birth
block = ms.format_for_prompt(MOM, contact_id=2, knowledge={"heard_after_days": 0.1, "cold": True})
check("an hour later, cold: Ben hasn't heard yet -> withheld", "had a baby" in block, False)
ms._now_sim_ticks = lambda: 1000 + int(0.2 * ms._TICKS_PER_DAY)     # ~5 sim hours
block = ms.format_for_prompt(MOM, contact_id=2, knowledge={"heard_after_days": 0.1, "cold": True})
check("5 hours later: Ben has heard", "had a baby" in block and "You KNOW about this" in block, True)
block = ms.format_for_prompt(MOM, contact_id=55, knowledge={"heard_after_days": 2.0, "cold": True})
check("an acquaintance-tier contact still hasn't", "had a baby" in block, False)
start = ph_src.index("def _heard_after_days"); end = ph_src.index("def _describe_recipient")
sys.modules["llamafone.milestones"] = ms
ns2 = {"_get_family_relationship": lambda si, c, recipient=None: None, "__package__": "llamafone", "__name__": "llamafone._phone_slice"}
exec(compile(ph_src[start:end], "phone_knowledge", "exec"), ns2)
k = ns2["_contact_knowledge"](MOM, {"sim_info": DAD, "friendship": 30})
check("father elsewhere, friendship 30 -> hears in 0.1 day (not 4)", k["heard_after_days"], 0.1)
k = ns2["_contact_knowledge"](MOM, {"sim_info": SI(77, "Rando"), "friendship": 30})
check("unrelated friend keeps the 4-day tier", k["heard_after_days"], 4.0)
ms._now_sim_ticks = lambda: 1000 + int(0.05 * ms._TICKS_PER_DAY)
check("newborn hidden from the father on a cold prompt until he's heard",
      ns2["_contact_has_heard_birth"](MOM, {"sim_info": DAD, "friendship": 30}), False)


print("5. watcher: one birth, one announcement, even across a restart")
services.current_zone = lambda: object()
sys.modules["llamafone.save_id"].get_current_save_id = lambda: "Slot_00000001"
MOM.is_pregnant = True                      # MCCC-style: baby here, flag not cleared
BABY.age_progress = 0.2
BABY.genealogy = Gen(mother=1, father=2)
births._newborns = lambda si, max_age_days=None, require_parent=False: [BABY] if si is MOM else []
SNAPS = {"1": {"is_pregnant": True, "name": "Ingrid", "pregnancy_partner_id": "2"}}
ms._load_snapshots = lambda: SNAPS
RECORDED, ANNOUNCED = [], []
ms.record_birth_now = lambda si, pid=None, baby_ids=None: (RECORDED.append(si.first_name), SNAPS["1"].update({"birth_recorded": True, "birth_recorded_babies": sorted(baby_ids or [])}))
_real_announce = births.announce_birth
births.announce_birth = lambda si, partner, dedup_key=None: ANNOUNCED.append(si.first_name)
births._announced.clear()
births._watch_once()
check("first pass records + announces once", (RECORDED, ANNOUNCED), (["Ingrid"], ["Ingrid"]))
births._announced.clear()                   # simulate a restart: in-memory set gone
births._watch_once()
check("after a restart the recorded birth is not re-fired", (RECORDED, ANNOUNCED), (["Ingrid"], ["Ingrid"]))
MOM.is_pregnant = False


print("6. rollback: a DIFFERENT baby after a recorded birth is a new birth")
births._announced.clear()
MOM.is_pregnant = True          # MCCC-style again: tracker not cleared, twins present
TWIN1 = SI(41, "Nikolas", "BABY"); TWIN2 = SI(42, "Dan", "BABY")
for b in (TWIN1, TWIN2):
    b.genealogy = Gen(mother=1, father=2); b.age_progress = 0.1
births._newborns = lambda si, max_age_days=None, require_parent=False: [TWIN1, TWIN2] if si is MOM else []
births._watch_once()            # snapshot still says birth_recorded for Marlon (3)
check("twins after rolled-back Marlon: recorded + announced", (RECORDED, ANNOUNCED), (["Ingrid", "Ingrid"], ["Ingrid", "Ingrid"]))
check("snapshot now remembers the twins", SNAPS["1"]["birth_recorded_babies"], [41, 42])
births._announced.clear()
births._watch_once()
check("same twins again (restart): not re-fired", len(RECORDED), 2)
# _diff: recorded flag only blocks events for the same babies
hh.m = [MOM, TWIN1, TWIN2]
prev = {"is_pregnant": True, "pregnancy_visibility": "visible", "is_dead": False, "birth_recorded": True, "birth_recorded_babies": [3]}
curr = {"is_pregnant": False, "pregnancy_visibility": "none", "is_dead": False}
ev = ms._diff(prev, dict(curr), "Ingrid", sim_info=MOM)
check("flag flips with different babies present: pregnancy_end recorded", [e["type"] for e in ev], ["pregnancy_end"])
prev["birth_recorded_babies"] = [41, 42]
ev = ms._diff(prev, dict(curr), "Ingrid", sim_info=MOM)
check("flag flips for the already-recorded babies: nothing", [e["type"] for e in ev], [])
hh.m = [MOM, BABY]
MOM.is_pregnant = False

_real_best = births._best_announcement
print("7. birth while the parent's household is played, then a switch")
births._announced.clear(); births._sweep_tried.clear()
import llamafone.births as _b
_b.announce_birth = _real_announce
real_announce = _real_announce
SCHEDULED = []
_b._best_announcement = lambda parent, partner, exclude=None: None          # Ingrid is the active household
ms._load_milestones = lambda: [{"type": "pregnancy_end", "sim_id": "1", "sim_ticks": 5000},
                               {"type": "pregnancy_end", "sim_id": "2", "mirror_of": "1", "sim_ticks": 5000}]
ms._now_sim_ticks = lambda: 5000 + 100
sys.modules["llamafone.config"].get_birth_announcements_enabled = lambda: True
real_announce(MOM, DAD)
check("skipped while her household is active: NOT marked announced", ("parent", 1) in births._announced, False)
# player switches to Ben's household
BEN_HH = HH([DAD]); BEN_HH.id = 2; DAD.household = BEN_HH; DAD.household_id = 2
services.active_household = lambda: BEN_HH
_b._best_announcement = lambda parent, partner, exclude=None: (DAD, {"sim_info": MOM, "name": "Ingrid"}, "close friend")
_b._fire_announcement = lambda *a, **k: SCHEDULED.append(a[2].first_name)
import threading as _th
_orig_timer = _th.Timer
_th.Timer = lambda delay, fn, args=(): types.SimpleNamespace(start=lambda: fn(*args), cancel=lambda: None, daemon=True)
_b._sweep_unannounced()
check("first watcher pass after the switch announces to Ben", SCHEDULED, ["Ingrid"])
check("now marked announced", ("parent", 1) in births._announced, True)
_b._sweep_unannounced()
check("sweep doesn't fire twice", SCHEDULED, ["Ingrid"])
_th.Timer = _orig_timer
services.active_household = lambda: hh


print("8. the father was on the lot for the birth: no text, recorded as present")
births._announced.clear(); births._sweep_tried.clear(); births._present_at_birth.clear()
services.active_household = lambda: hh                         # playing Ingrid's household, Ben visiting
DAD.household = BEN_HH; DAD.household_id = 2
STORE8 = [{"type": "pregnancy_end", "sim_id": "1", "sim_ticks": 7000},
          {"type": "pregnancy_end", "sim_id": "2", "mirror_of": "1", "sim_ticks": 7000}]
ms._load_milestones = lambda: STORE8
ms._save_milestones = lambda entries: None
ms._now_sim_ticks = lambda: 7000 + 50
sys.modules["llamafone.phone"]._get_sims_on_active_lot = lambda: {1, 2, 3}      # Ingrid, Ben, the baby
EXCL = []
_b._best_announcement = lambda parent, partner, exclude=None: EXCL.append(set(exclude or ())) or None
_b.announce_birth(MOM, DAD)
check("Ben recorded as present (the baby isn't)", [(t["to"], t["how"]) for t in STORE8[0].get("told", [])], [("2", "present")])
check("Ben (and his household) excluded as recipients", 2 in EXCL[-1], True)
# player switches to Ben's household: the sweep must NOT text him
services.active_household = lambda: BEN_HH
sys.modules["llamafone.phone"]._get_sims_on_active_lot = lambda: set()
SCHEDULED.clear()
_b._best_announcement = lambda parent, partner, exclude=None: (DAD, {"sim_info": MOM, "name": "Ingrid"}, "close friend")
_b._sweep_unannounced()
check("sweep skips Ben's household: he was there", SCHEDULED, [])
# prompt wording
start = ph_src.index("def _household_already_told_line"); end = ph_src.index("def _describe_relationship")
ns3 = {"__package__": "llamafone", "__name__": "llamafone._phone_slice2", "services": services}
exec(compile(ph_src[start:end], "phone_told", "exec"), ns3)
MOM.household = hh
line = ns3["_household_already_told_line"](MOM, DAD)
check("told line says he was there", "Ben was there when the baby was born" in line, True)
services.active_household = lambda: hh


print("9. the sweep never invents presence; deferred presence is flushed")
births._announced.clear(); births._sweep_tried.clear(); births._present_at_birth.clear()
STORE9 = [{"type": "pregnancy_end", "sim_id": "1", "sim_ticks": 9000},
          {"type": "pregnancy_end", "sim_id": "2", "mirror_of": "1", "sim_ticks": 9000}]
ms._load_milestones = lambda: STORE9
ms._now_sim_ticks = lambda: 9000 + 50
# Ingrid delivered off-lot (nobody there). Later the player is on Ben's lot and Ingrid is visiting.
services.active_household = lambda: BEN_HH
sys.modules["llamafone.phone"]._get_sims_on_active_lot = lambda: {1, 2}
SCHEDULED.clear()
_th.Timer = lambda delay, fn, args=(): types.SimpleNamespace(start=lambda: fn(*args), cancel=lambda: None, daemon=True)
_b._best_announcement = lambda parent, partner, exclude=None: None if (exclude and 2 in exclude) else (DAD, {"sim_info": MOM, "name": "Ingrid"}, "close friend")
_b._sweep_unannounced()
check("sweep with Ingrid visiting Ben's lot: Ben still gets the text", SCHEDULED, ["Ingrid"])
check("no bogus 'present' record from the sweep", STORE9[0].get("told"), None)
_th.Timer = _orig_timer
# hook path: presence captured before the milestone exists, flushed later
births._announced.clear(); births._present_at_birth.clear()
ms._load_milestones = lambda: []                      # no pregnancy_end yet (hook fires first)
services.active_household = lambda: hh
sys.modules["llamafone.phone"]._get_sims_on_active_lot = lambda: {1, 2, 3}
_b._best_announcement = lambda parent, partner, exclude=None: None
_b.announce_birth(MOM, DAD)
check("presence deferred (no milestone yet)", births._present_at_birth.get(1), {2})
ms._load_milestones = lambda: STORE9                  # the scan has now recorded the birth
_b.announce_birth(MOM, DAD, capture_presence=False)
check("flushed onto the milestone on the next detection", [(t["to"], t["how"]) for t in STORE9[0].get("told", [])], [("2", "present")])
check("deferred entry cleared", births._present_at_birth.get(1), None)
sys.modules["llamafone.phone"]._get_sims_on_active_lot = lambda: set()


print("10. the baby's other parent always qualifies, however bad the relationship")
_b._best_announcement = _real_best
services.active_household = lambda: BEN_HH
BABY.genealogy = Gen(mother=1, father=2)
births._newborns = lambda si, max_age_days=None, require_parent=False: [BABY] if si is MOM else []
sys.modules["llamafone.sim_context"].get_sim_network = lambda hs, min_friendship=25: ([], [
    {"sim_info": MOM, "sim_id": 1, "name": "Ingrid X", "friendship": -40, "romance": -20, "status": "Enemies", "in_household": False}])
sys.modules["llamafone.contact_prefs"].is_muted = lambda a, b: False
sys.modules["llamafone.phone"]._get_family_relationship = lambda si, entry, recipient=None: None
pick = _b._best_announcement(MOM, DAD)
check("Ben (friendship -40, no family label) is picked as the recipient", pick[0] is DAD if pick else None, True)
check("label says why", "other parent" in (pick[2] if pick else ""), True)
check("sender is Ingrid with her real network entry", pick[1]["friendship"] if pick else None, -40)
pick = _b._best_announcement(MOM, None)           # no partner passed: genealogy alone finds him
check("found through the baby's genealogy even without a partner id", pick[0] is DAD if pick else None, True)
check("excluded when he already knows", _b._best_announcement(MOM, DAD, exclude={2}), None)
sys.modules["llamafone.contact_prefs"].is_muted = lambda a, b: (a, b) == (2, 1)
check("respects Ben having muted Ingrid", _b._best_announcement(MOM, DAD), None)
sys.modules["llamafone.contact_prefs"].is_muted = lambda a, b: False
STRANGER_HH = HH([SI(60, "Rando")]); STRANGER_HH.id = 6
services.active_household = lambda: STRANGER_HH
check("an unrelated low-friendship household still gets nothing", _b._best_announcement(MOM, DAD), None)
services.active_household = lambda: hh


print("11. she already called him after the birth -> no separate announcement on the switch")
births._announced.clear(); births._sweep_tried.clear(); births._present_at_birth.clear()
JOURNAL_ENTRIES = []
jmod = types.ModuleType("llamafone.journal"); jmod._load = lambda: JOURNAL_ENTRIES
sys.modules["llamafone.journal"] = jmod; pkg.journal = jmod
STORE11 = [{"type": "pregnancy_end", "sim_id": "1", "sim_ticks": 20000},
           {"type": "pregnancy_end", "sim_id": "2", "mirror_of": "1", "sim_ticks": 20000}]
ms._load_milestones = lambda: STORE11
ms._now_sim_ticks = lambda: 20000 + 200
services.active_household = lambda: BEN_HH
sys.modules["llamafone.phone"]._get_sims_on_active_lot = lambda: set()
_b._best_announcement = lambda parent, partner, exclude=None: (DAD, {"sim_info": MOM, "name": "Ingrid"}, "the baby's other parent")
_th.Timer = lambda delay, fn, args=(): types.SimpleNamespace(start=lambda: fn(*args), cancel=lambda: None, daemon=True)
SCHEDULED.clear()
JOURNAL_ENTRIES[:] = [{"type": "call", "sim_id": "2", "recipient_id": "1", "ticks": 20100}]   # Ingrid (played) called Ben after the birth
_b._sweep_unannounced()
check("call after the birth: no announcement text", SCHEDULED, [])
births._sweep_tried.clear()
JOURNAL_ENTRIES[:] = [{"type": "call", "sim_id": "2", "recipient_id": "1", "ticks": 19000}]   # before the birth
_b._sweep_unannounced()
check("call from BEFORE the birth doesn't count: announcement sent", SCHEDULED, ["Ingrid"])
births._announced.clear(); births._sweep_tried.clear(); SCHEDULED.clear()
JOURNAL_ENTRIES[:] = [{"type": "post", "sim_id": "1", "recipient_id": "2", "ticks": 20100}]    # he only saw her post
_b._sweep_unannounced()
check("merely seeing her post isn't contact: announcement sent", SCHEDULED, ["Ingrid"])
_th.Timer = _orig_timer
services.active_household = lambda: hh

print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAIL: {FAILS}")
