import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os, sys, types, tempfile, collections, random

sys.path.insert(0, _os.path.join(_REPO, "src"))
tmp = tempfile.mkdtemp()
os.environ["USERPROFILE"] = tmp
os.environ["HOME"] = tmp
os.makedirs(os.path.join(tmp, "Documents"))


class _Any(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any(name)

    def __call__(self, *a, **k):
        return _Any("call")


for name in ("services", "sims4", "sims4.commands", "sims4.log", "sims4.resources", "sims4.localization",
             "ui", "ui.ui_dialog", "ui.ui_dialog_notification", "ui.ui_dialog_picker", "date_and_time",
             "interactions", "interactions.base", "interactions.base.super_interaction", "event_testing",
             "relationships", "relationships.relationship_track", "statistics", "sims", "sims.sim_info_types",
             "distributor", "distributor.shared_messages", "alarms", "clock", "objects", "enum", "tunable_utils"):
    if name not in sys.modules:
        sys.modules[name] = _Any(name)

pkg = types.ModuleType("llamafone")
pkg.__path__ = [_os.path.join(_REPO, "src", "llamafone")]
sys.modules["llamafone"] = pkg
import llamafone.phone as phone
import llamafone.social as social

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(extra)))
    if not cond:
        fails += 1


class SI:
    def __init__(self, sid, name, age):
        self.sim_id, self.first_name, self.last_name, self.age = sid, name, "X", age


FRAN = SI(1, "Francesca", "YOUNGADULT")
AKS = SI(2, "Aksel", "YOUNGADULT")
fran_contacts = [  # mostly kids, very close; one casual adult friend
    {"sim_info": SI(10, "Kid1", "CHILD"), "sim_id": 10, "friendship": 100, "romance": 0},
    {"sim_info": SI(11, "Kid2", "CHILD"), "sim_id": 11, "friendship": 100, "romance": 0},
    {"sim_info": SI(12, "Kid3", "CHILD"), "sim_id": 12, "friendship": 90, "romance": 0},
    {"sim_info": SI(13, "Adultfriend", "ADULT"), "sim_id": 13, "friendship": 15, "romance": 0},
]
aks_contacts = [
    {"sim_info": SI(20, "Grandma", "ELDER"), "sim_id": 20, "friendship": 100, "romance": 0},
    {"sim_info": SI(21, "Buddy", "YOUNGADULT"), "sim_id": 21, "friendship": 10, "romance": 0},
    {"sim_info": SI(22, "Cousin", "ADULT"), "sim_id": 22, "friendship": 40, "romance": 0},
]
NET = {1: fran_contacts, 2: aks_contacts}

phone.sim_context.get_main_sim_network = lambda si: ([], [dict(c) for c in NET[si.sim_id]])
phone._is_human_sim = lambda si: True
phone._is_ghost = lambda si: False
phone._get_sims_on_active_lot = lambda: set()
phone._is_age_appropriate_contact = lambda c, r: True
phone.config.get_phone_allow_ghosts = lambda: True
phone.contact_prefs.is_muted = lambda a, b: False
phone.contact_prefs.auto_event_multiplier = lambda a, b: 1.0
phone._log_picker = lambda m: None
social._log = lambda m: None
phone._eligible_recipients = lambda **_k: random.sample([FRAN, AKS], 2)

POSTS = []
social._load = lambda: {"posts": POSTS}
social._now_ticks = lambda: 10 ** 9

random.seed(7)
viewers = collections.Counter()
posters = collections.Counter()
for _ in range(2000):
    v, c = social._pick_npc_poster()
    viewers[v.first_name] += 1
    posters[c["sim_info"].first_name] += 1
print("viewers:", dict(viewers))
print("posters:", dict(posters))
check("Francesca gets about half the posts", 0.42 < viewers["Francesca"] / 2000 < 0.58, viewers)
check("no kid ever posts", not any(k.startswith("Kid") for k in posters), posters)
check("Francesca's only teen+ friend posts to her", posters["Adultfriend"] == viewers["Francesca"], (posters, viewers))
check("casual buddy ~16% of Aksel posts (old weighting: ~7%)", posters["Buddy"] / viewers["Aksel"] > 0.12, posters)

# Old picker, for comparison: the same household under the call/text picker + teen+ recheck.
random.seed(7)
old = collections.Counter()
for _ in range(2000):
    contact = None
    for _ in range(4):
        v, contact = phone._pick_recipient_and_contact()
        if contact is not None and social._can_post(contact.get("sim_info")):
            break
        contact = None
    if contact is not None:
        old[v.first_name] += 1
print("old picker viewers:", dict(old))
check("old picker reproduces the bug (Francesca rarely)", old["Francesca"] / max(1, sum(old.values())) < 0.2, old)

# No repeats: Grandma posted to Aksel within the last in-game day.
POSTS.append({"is_player": False, "viewer_id": 2, "author_id": "20", "ticks": 10 ** 9 - 1000})
phone._eligible_recipients = lambda **_k: [AKS]
picks = collections.Counter(social._pick_npc_poster()[1]["sim_info"].first_name for _ in range(300))
check("recent poster skipped while others exist", picks["Grandma"] == 0, picks)
aks_contacts[:] = aks_contacts[:1]   # only Grandma left
v, c = social._pick_npc_poster()
check("falls back to the recent poster when nobody else", c is not None and c["sim_info"].first_name == "Grandma")

# Calls/texts unchanged: no filter/weight/exclude -> kids still reachable.
random.seed(1)
call = collections.Counter(phone._pick_random_relationship_sim(recipient=FRAN)["sim_info"].first_name for _ in range(500))
check("call/text picker still includes kids (unchanged)", call["Kid1"] > 0, call)
print("FAILS", fails)
