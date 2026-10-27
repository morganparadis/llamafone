import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
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
        s.m = m
        s.funds = types.SimpleNamespace(money=50000)

    def sim_info_gen(s):
        return iter(s.m)


class SI:
    def __init__(s, sid, first, age="ADULT"):
        s.sim_id, s.first_name, s.last_name, s.age = sid, first, "X", "Age." + age
        s.spouse_sim_id = None; s.pregnancy_tracker = None


LUCA, MARTHA, MAX = SI(1, "Luca"), SI(2, "Martha"), SI(3, "Maxwell", "INFANT")
FRAN, AKSEL = SI(10, "Francesca"), SI(11, "Aksel")
woodruff = HH([LUCA, MARTHA, MAX]); hansen = HH([FRAN, AKSEL])
for s in (LUCA, MARTHA, MAX):
    s.household = woodruff
for s in (FRAN, AKSEL):
    s.household = hansen
MARTHA.spouse_sim_id = 1
SIMS = {s.sim_id: s for s in (LUCA, MARTHA, MAX, FRAN, AKSEL)}
services = types.ModuleType("services")
services.sim_info_manager = lambda: types.SimpleNamespace(get=lambda i: SIMS.get(int(i)))
sys.modules["services"] = services
pkg = types.ModuleType("llamafone"); pkg.__path__ = [os.path.join(REPO, "llamafone")]; sys.modules["llamafone"] = pkg
for name in ("config", "sim_context", "save_id", "births"):
    m = types.ModuleType("llamafone." + name); sys.modules["llamafone." + name] = m; setattr(pkg, name, m)
sys.modules["llamafone.sim_context"].get_sim_traits = lambda si, limit=12: []
sys.modules["llamafone.births"]._newborns = lambda si, **k: []
ms = importlib.import_module("llamafone.milestones")
STORE = [
    {"type": "pregnancy_end", "description": "Martha had a baby", "sim_id": "2", "sim_ticks": 500, "mirrored": True},
    {"type": "pregnancy_end", "description": "Luca and Martha had a baby", "sim_id": "1", "mirror_of": "2", "sim_ticks": 500},
]
ms._load_milestones = lambda: STORE
ms._save_milestones = lambda entries: None
ms._now_sim_ticks = lambda: 600
ms._log = lambda m: None

print("1. after-birth kid count includes the new baby")
facts = ms.pregnancy_circumstances(MARTHA, partner_id=1, after_birth=True)
check("first child (Maxwell is the only kid, now an infant)", "this is their first child" in facts, True)
check("no 'already has 1 kid'", any("already has" in f for f in facts), False)

print("2. record_told -> both the event and the mirror carry it")
check("record_told", ms.record_told(2, 1, 10), True)
check("mirror has told", STORE[1].get("told")[0]["to"], "10")
check("told_households by either parent", ms.told_households({"1", "2", "3"}), [("1", "10", 600, "text"), ("1", "10", 600, "text")])

print("3. phone helper: Martha -> Aksel sees Luca told Francesca")
ph_src = open(os.path.join(REPO, "llamafone", "phone.py"), encoding="utf-8").read()
start = ph_src.index("def _household_already_told_line")
end = ph_src.index("def _describe_relationship")
ns = {"__name__": "llamafone.phonestub", "__package__": "llamafone"}
exec(compile(ph_src[start:end], "phone_helper", "exec"), ns)
line = ns["_household_already_told_line"](MARTHA, AKSEL)
print("     ", line)
check("names who told whom", "Luca told Francesca (who lives with Aksel) about the baby" in line, True)
check("override wording", "do NOT announce it" in line, True)
check("unrelated household: no line", ns["_household_already_told_line"](MARTHA, SI(99, "Nobody")), "")
print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAIL: {FAILS}")
