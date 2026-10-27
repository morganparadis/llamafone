import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os, sys, types, tempfile

sys.path.insert(0, _os.path.join(_REPO, "src"))
tmp = tempfile.mkdtemp()
os.environ["USERPROFILE"] = tmp
os.environ["HOME"] = tmp
os.makedirs(os.path.join(tmp, "Documents"))
pkg = types.ModuleType("llamafone")
pkg.__path__ = [_os.path.join(_REPO, "src", "llamafone")]
sys.modules["llamafone"] = pkg

DATA = os.path.join(tmp, "Slot")
os.makedirs(DATA)
sid = types.ModuleType("llamafone.save_id")
sid.data_path = lambda name: os.path.join(DATA, name)
sid.get_current_save_id = lambda: "Slot"
sys.modules["llamafone.save_id"] = sid

svc = types.ModuleType("services"); svc.client_manager = lambda: object(); sys.modules["services"] = svc
import llamafone.trips as trips

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + repr(extra)))
    if not cond:
        fails += 1


DAY = trips._TICKS_PER_DAY
NOW = [100 * DAY]
trips._now_ticks = lambda: NOW[0]


class SI:
    def __init__(self, i, f, l="Hansen"):
        self.sim_id, self.first_name, self.last_name = i, f, l


FRAN, AKS, APOLLO, MARTHA, GRANDMA = SI(1, "Francesca"), SI(2, "Aksel"), SI(3, "Apollo", "Woodruff"), SI(4, "Martha", "Woodruff"), SI(5, "Gabriela")


class TS:
    def __init__(self, t): self.t = t
    def absolute_ticks(self): return self.t


class TravelGroup:
    def __init__(self, gid, sims, zone):
        self.id, self._s, self.zone_id = gid, sims, zone
        self.create_timestamp, self.end_timestamp = TS(NOW[0]), TS(NOW[0] + 3 * DAY)
    def sim_info_gen(self): return iter(self._s)


class TravelGroupStayover(TravelGroup):
    pass


class HH:
    id = 77


GROUP = [None]
trips._active_group = lambda: (HH(), GROUP[0])
trips._world_name = lambda z: "Sulani"
trips._lot_name = lambda z: "Gibbi Point Rental"
AT_HOME = [False]
trips._household_at_home = lambda hh: AT_HOME[0]

# Not traveling: nothing recorded.
trips.poll_once()
check("no group, no trip", trips._load()["trips"] == [])

# Stayover is not a trip.
GROUP[0] = TravelGroupStayover(9, [FRAN, AKS], 1)
trips.poll_once()
check("stayover ignored", trips._load()["trips"] == [])

# Vacation starts with household + companions.
GROUP[0] = TravelGroup(10, [FRAN, AKS, APOLLO], 555)
trips.poll_once()
t = trips._load()["trips"]
check("vacation recorded once", len(t) == 1 and t[0]["kind"] == "vacation" and t[0]["world"] == "Sulani", t)
trips.poll_once()
check("polling again doesn't duplicate", len(trips._load()["trips"]) == 1)

# Martha joins mid-trip.
GROUP[0]._s.append(MARTHA)
NOW[0] += DAY // 2
trips.poll_once()
check("joiner added", "4" in trips._load()["trips"][0]["members"])

# During the trip: companion vs outsider.
block = trips.format_for_prompt(APOLLO, FRAN, heard_after_days=0.1)
check("companion: on vacation together right now", "together right now in Sulani (staying at Gibbi Point Rental)" in block and "Martha" in block, block)
block = trips.format_for_prompt(GRANDMA, FRAN, heard_after_days=1.0)
check("grandma hasn't heard yet (half a day in, tier 1 day)", block == "", block)
NOW[0] += DAY
block = trips.format_for_prompt(GRANDMA, FRAN, heard_after_days=1.0)
check("grandma has heard, knows she wasn't there", "is away on a vacation in Sulani" in block and "NOT on this trip" in block, block)
check("acquaintance (only if told) never hears", trips.format_for_prompt(GRANDMA, FRAN, heard_after_days=None) == "")

# Loading screen mid-trip: group briefly missing, not home -> stays open.
saved_group = GROUP[0]
GROUP[0] = None
trips.poll_once(); trips.poll_once()
t = trips._load()["trips"]
check("group missing 2 checks while away: trip stays open", len(t) == 1 and t[0]["end_ticks"] is None, t)
GROUP[0] = saved_group
trips.poll_once()
check("group back: same trip, no duplicate", len(trips._load()["trips"]) == 1)

# Back home.
GROUP[0] = None
AT_HOME[0] = True
NOW[0] += DAY
trips.poll_once()
t = trips._load()["trips"][0]
check("trip closed once home", t["end_ticks"] == NOW[0], t)

# A wrongly closed trip whose group reappears is reopened, not duplicated.
GROUP[0] = saved_group
AT_HOME[0] = False
trips.poll_once()
t = trips._load()["trips"]
check("reopened, not duplicated", len(t) == 1 and t[0]["end_ticks"] is None, t)
GROUP[0] = None
AT_HOME[0] = True
trips.poll_once()
check("closed again at home", trips._load()["trips"][0]["end_ticks"] == NOW[0])

# Fallback: trip ended while another household was played (never 'home').
AT_HOME[0] = False
GROUP[0] = TravelGroup(11, [FRAN, AKS], 556)
trips.poll_once()
GROUP[0] = None
for _ in range(3):
    trips.poll_once()
t2 = [x for x in trips._load()["trips"] if x["id"] == "11"][0]
check("fallback closes after 3 checks without the group", t2["end_ticks"] is not None, t2)
d = trips._load(); d["trips"] = [x for x in d["trips"] if x["id"] != "11"]; trips._save(d)
NOW[0] += 2 * DAY
block = trips.format_for_prompt(APOLLO, FRAN, heard_after_days=0.1)
check("companion remembers after", "went on a vacation together to Sulani" in block and "two days ago" in block, block)
NOW[0] += 6 * DAY
check("forgotten from prompts after 7 in-game days back", trips.format_for_prompt(APOLLO, FRAN, 0.1) == "")
check("still kept on disk (never deleted)", len(trips._load()["trips"]) == 1)
check("debug summary lists it", "Sulani" in trips.debug_summary()[0], trips.debug_summary())
print("FAILS", fails)
