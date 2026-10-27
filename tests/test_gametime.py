import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import os, sys, types, tempfile, datetime

sys.path.insert(0, _os.path.join(_REPO, "src"))
pkg = types.ModuleType("llamafone")
pkg.__path__ = [_os.path.join(_REPO, "src", "llamafone")]
sys.modules["llamafone"] = pkg
tmp = tempfile.mkdtemp()
os.environ["USERPROFILE"] = tmp
os.environ["HOME"] = tmp
os.makedirs(os.path.join(tmp, "Documents"))

# Minimal game stubs some modules import at load.
for name in ("services", "sims4", "sims4.commands", "sims4.log"):
    sys.modules.setdefault(name, types.ModuleType(name))

fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + repr(extra)))
    if not cond:
        fails += 1


MIN = 1500
HOUR = 60 * MIN
DAY = 24 * HOUR
NOW = 1000 * DAY + 14 * HOUR   # 2 PM
REAL_LONG_AGO = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()
REAL_NOW = datetime.datetime.now().isoformat()

# ---------------------------------------------------------------- milestones
import llamafone.milestones as ms
ms._now_sim_ticks = lambda: NOW
store = [
    # Played a month ago in REAL time, but only 1 in-game day ago -> recent.
    {"type": "career_promotion", "sim_id": "5", "timestamp": REAL_LONG_AGO + "a", "sim_ticks": NOW - 1 * DAY, "description": "promoted"},
    # Real time: just now. In-game: 8 days ago -> old.
    {"type": "job_quit", "sim_id": "5", "timestamp": REAL_NOW + "b", "sim_ticks": NOW - 8 * DAY, "description": "quit"},
    # No in-game time (very old entry) -> old.
    {"type": "marriage", "sim_id": "5", "timestamp": REAL_NOW + "c", "description": "married"},
    # From a rolled-back future -> excluded.
    {"type": "breakup", "sim_id": "5", "timestamp": REAL_NOW + "d", "sim_ticks": NOW + 2 * DAY, "description": "breakup"},
    # Exactly now -> recent.
    {"type": "aged_up", "sim_id": "5", "timestamp": REAL_NOW + "e", "sim_ticks": NOW, "description": "aged up"},
]
ms._load_milestones = lambda: store
ms._referenced_timestamps = lambda *a, **k: set()
got = [e["type"] for e in ms.get_recent_for_sim(5)]
check("milestone 1 in-game day old, 30 real days old: included", "career_promotion" in got, got)
check("milestone 8 in-game days old, real-time fresh: excluded", "job_quit" not in got, got)
check("milestone with no in-game time: excluded", "marriage" not in got, got)
check("milestone from rolled-back future: excluded", "breakup" not in got, got)
check("milestone at this exact tick: included", "aged_up" in got, got)
check("7-day window is in-game", ms._is_recent({"sim_ticks": NOW - 7 * DAY + 1}, NOW, 7) and not ms._is_recent({"sim_ticks": NOW - 7 * DAY - HOUR}, NOW, 7))

# ------------------------------------------------------------------- journal
import llamafone.journal as jr
check("journal: 30 in-game min", jr._ago_label({"ticks": NOW - 30 * MIN}, NOW) == "~30 min ago (in-game)", jr._ago_label({"ticks": NOW - 30 * MIN}, NOW))
check("journal: 3h", jr._ago_label({"ticks": NOW - 3 * HOUR}, NOW) == "~3h ago, earlier today (in-game)", jr._ago_label({"ticks": NOW - 3 * HOUR}, NOW))
check("journal: 16h at 2 PM is last night, not today", jr._ago_label({"ticks": NOW - 16 * HOUR}, NOW).startswith("late last night"), jr._ago_label({"ticks": NOW - 16 * HOUR}, NOW))
check("journal: 43h at 2 PM is two days ago, not yesterday", jr._ago_label({"ticks": NOW - 43 * HOUR}, NOW).startswith("two days ago"), jr._ago_label({"ticks": NOW - 43 * HOUR}, NOW))
check("journal: yesterday", jr._ago_label({"ticks": NOW - 26 * HOUR}, NOW).startswith("yesterday afternoon"), jr._ago_label({"ticks": NOW - 26 * HOUR}, NOW))
check("journal: 10 days, no real date", jr._ago_label({"ticks": NOW - 10 * DAY, "timestamp": REAL_LONG_AGO}, NOW) == "last week (in-game)", jr._ago_label({"ticks": NOW - 10 * DAY}, NOW))
check("journal: 30 days -> weeks", jr._ago_label({"ticks": NOW - 20 * DAY}, NOW) == "a couple of weeks ago (in-game)", jr._ago_label({"ticks": NOW - 20 * DAY}, NOW))
check("journal: no ticks -> vague, no real date", jr._ago_label({"timestamp": REAL_LONG_AGO}, NOW) == "a while ago")
check("journal: rolled back -> 'earlier'", jr._ago_label({"ticks": NOW + DAY}, NOW) == "earlier")
jr._now_ingame_ticks = lambda: NOW
jr.get_recent = lambda n: [{"type": "text", "sim": "Bob", "content": "hi", "ticks": NOW - 2 * HOUR, "timestamp": REAL_LONG_AGO}]
block = jr.format_for_prompt()
month = datetime.datetime.fromisoformat(REAL_LONG_AGO).strftime("%b")
check("story-so-far shows in-game age, no calendar date", "~2h ago, earlier today" in block and month not in block, block)
jr.get_sim_history = lambda *a, **k: [{"type": "text", "content": "hey", "ticks": NOW - 3 * DAY, "timestamp": REAL_NOW}]
block = jr.format_sim_history_for_prompt("Bob")
check("past interactions use in-game days", "3 days ago" in block, block)

# -------------------------------------------------------------- interactions
import llamafone.interactions as ix
ix._now_ingame_ticks = lambda: NOW
fmt = None
for n in dir(ix):
    f = getattr(ix, n)
    if callable(f) and getattr(f, "__doc__", "") and "Render 'how long ago' for an interaction entry" in (f.__doc__ or ""):
        fmt = f
check("found interaction age renderer", fmt is not None)
if fmt:
    check("interaction legacy entry: no real-time label", fmt({"timestamp": REAL_LONG_AGO}) == "", fmt({"timestamp": REAL_LONG_AGO}))
    check("interaction 2 in-game hours", "2 in-game hours" in fmt({"ticks": NOW - 2 * HOUR}), fmt({"ticks": NOW - 2 * HOUR}))
saved = {}
ix._load = lambda: {"a": {"ticks": NOW - 10 * DAY}, "b": {"ticks": NOW - 40 * DAY}, "legacy": {"timestamp": REAL_LONG_AGO}}
ix._save = lambda d: saved.update({"d": d})
ix.cleanup_old(days=30)
kept = set(saved.get("d", {}).keys())
check("interaction cleanup: 10 in-game days kept (was deleted at 100 ticks/min)", "a" in kept, kept)
check("interaction cleanup: 40 in-game days trimmed", "b" not in kept, kept)
check("interaction cleanup: legacy never deleted by real date", "legacy" in kept, kept)

# --------------------------------------------------------------- group texts
import llamafone.group_texts as gt
gt._now_ticks = lambda: NOW
groups = {
    "recent": {"anchor_sim_id": 1, "participant_sim_ids": [2, 3], "last_activity": REAL_LONG_AGO, "last_activity_ticks": NOW - 2 * DAY, "history": [{"role": "them", "from_name": "Bob", "text": "yo"}]},
    "old": {"anchor_sim_id": 1, "participant_sim_ids": [2, 4], "last_activity": REAL_NOW, "last_activity_ticks": NOW - 5 * DAY, "history": []},
    "legacy": {"anchor_sim_id": 1, "participant_sim_ids": [2, 5], "last_activity": REAL_NOW, "history": []},
}
gt.list_active_groups = lambda anchor_sim_id=None: list(groups.values())
found = gt.find_shared_groups(1, 2)
check("group 2 in-game days old (30 real days): shared", groups["recent"] in found)
check("group 5 in-game days old (real-time fresh): not shared", groups["old"] not in found)
check("group with no in-game time: not shared", groups["legacy"] not in found)
block = gt.format_shared_for_prompt(1, 3, "Alice", "Sarah")
check("group recency in in-game days", "2 in-game days ago" in block and month not in block, block)

print("FAILS", fails)
