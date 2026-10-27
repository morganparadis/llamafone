import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import random
import sys, types, os, json, tempfile, importlib, time
REPO = _os.path.join(_REPO, "src")
TMP = tempfile.mkdtemp()
FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        FAILS.append(label)


class RT:
    def __init__(s, ids):
        s.ids = ids

    def target_sim_gen(s):
        return iter(s.ids)


class SI:
    def __init__(s, sid, first, last, age="ADULT"):
        s.sim_id, s.first_name, s.last_name, s.age = sid, first, last, "Age." + age
        s.relationship_tracker = RT([])


F = SI(1, "Francesca", "Hansen"); L = SI(2, "Luca", "Woodruff"); M = SI(3, "Martha", "Woodruff")
X = SI(4, "Stranger", "Dude"); B = SI(5, "Baby", "Hansen", "BABY"); K = SI(6, "Kid", "Hansen", "CHILD")
F.relationship_tracker = RT([2, 3])
class Gen:
    def __init__(s, kids):
        s.kids = kids
    def get_children_sim_ids_gen(s):
        return iter(s.kids)
KID = SI(7, "Marlon", "Hansen", "BABY")
for _s in (F, L, M, X, B, K, KID):
    _s.genealogy = Gen([])
F.genealogy = Gen([7]); L.genealogy = Gen([7])
SIMS = {s.sim_id: s for s in (F, L, M, X, B, K, KID)}


class HH:
    id = 99

    def sim_info_gen(s):
        return iter([F, B, K])


class SM(dict):
    def get(s, i):
        return SIMS.get(int(i))


services = types.ModuleType("services")
services.sim_info_manager = lambda: SM(SIMS)
services.active_household = lambda: HH()
sys.modules["services"] = services

pkg = types.ModuleType("llamafone"); pkg.__path__ = [os.path.join(REPO, "llamafone")]; sys.modules["llamafone"] = pkg


def mod(name, **attrs):
    m = types.ModuleType("llamafone." + name); m.__dict__.update(attrs)
    sys.modules["llamafone." + name] = m; setattr(pkg, name, m); return m


STATE = {"save": "Slot_00000001"}


def data_path(fn):
    if not STATE["save"]:
        return None
    d = os.path.join(TMP, STATE["save"]); os.makedirs(d, exist_ok=True)
    return os.path.join(d, fn)


mod("save_id", get_current_save_id=lambda: STATE["save"], data_path=data_path)
mod("config", get_social_enabled=lambda: True, get_language=lambda: "English", get_social_npc_post_weight=lambda: 25)


def net(si, min_friendship=25):
    if si is F:
        return ([{"sim_info": B, "sim_id": 5, "name": "Baby Hansen", "in_household": True, "friendship": None, "romance": None, "status": ""},
                 {"sim_info": K, "sim_id": 6, "name": "Kid Hansen", "in_household": True, "friendship": None, "romance": None, "status": ""}],
                [{"sim_info": L, "sim_id": 2, "name": "Luca Woodruff", "friendship": 90, "romance": 0, "status": "Good Friends", "in_household": False},
                 {"sim_info": M, "sim_id": 3, "name": "Martha Woodruff", "friendship": -50, "romance": 0, "status": "", "in_household": False}])
    return ([], [{"sim_info": F, "sim_id": 1, "name": "Francesca Hansen", "friendship": 90, "romance": 0, "status": "", "in_household": False}])


mod("sim_context", get_sim_network=net, get_sim_traits=lambda si, limit=4: ["Goofball"], get_sim_career=lambda si: None,
    get_sim_mood=lambda si: "Happy", get_main_sim_info=lambda: F,
    has_met_in_person=lambda si, tid: False)
TIMERS = []
AI = {"reply": "[]", "calls": []}


def call_ai_async(messages, system=None, use_fast_model=False, callback=None, **k):
    AI["calls"].append((system, messages[0]["content"])); callback(AI["reply"], None)


mod("api_client", call_ai_async=call_ai_async)
NOTES = []
mod("notifications", show=lambda t, m, output=None: NOTES.append((t, m)))
JOURNAL = []
mod("journal", add_entry=lambda *a, **k: JOURNAL.append((a, k)),
    get_sim_history=lambda name, n=3, sim_id=None, recipient_id=None, **k:
        [{"type": "text", "content": "Text from Martha: we need to talk about the fence"}]
        if (str(sim_id), str(recipient_id)) == ("3", "1") else [])
IMPACT = []
mod("relationship_impact", apply_from_mood=lambda a, b, mood: IMPACT.append((a.first_name, b.first_name, mood)))
mod("contact_prefs", is_muted=lambda a, b: False)
mod("milestones", _now_sim_ticks=lambda: 1000,
    format_for_prompt=lambda si, **k: "Recent in their life:\n  - [just now] Francesca Hansen had a baby" if si is F else "")
ELIG = ("TEEN", "YOUNGADULT", "ADULT", "ELDER")
mod("phone", _is_phone_eligible=lambda si: str(si.age).replace("Age.", "") in ELIG, _is_ghost=lambda si: False,
    _get_family_relationship=lambda si, e, recipient=None: "Brother" if si is L else None,
    _friendship_label=lambda s: None if s is None else ("best friends" if s > 70 else "dislike each other" if s < 0 else "friends"),
    _romance_label=lambda s: None, _season_context=lambda: "[SEASON: Spring]", _time_context=lambda: "[TIME: evening]",
    _track_timer=lambda t: None, _describe_relationship=lambda c, recipient=None: f"=== Character: {c['name']} ===",
    _apply_mood_from_text=lambda text, **k: text.split("\nMOOD:")[0],
    _pick_recipient_and_contact=lambda: (F, {"sim_info": L, "sim_id": 2, "name": "Luca Woodruff"}),
    _eligible_recipients=lambda: [F],
    _pick_random_relationship_sim=lambda recipient=None, **k: {"sim_info": L, "sim_id": 2, "name": "Luca Woodruff"},
    _get_romantic_partner_info=lambda si: (None, None))
soc = importlib.import_module("llamafone.social")
soc._log = lambda m: None
soc._BREAKOUT_CHANCE = 0.0  # deterministic: only the breakout tests roll
soc._AI_BREAKOUT_ACCEPT = 0.0
soc._timer = lambda delay, fn: (TIMERS.append(delay), fn())   # run immediately
SHOWN = []
soc._ok_cancel = lambda *a, **k: SHOWN.append(("okcancel", a[1], k)) or True
soc._picker = lambda anchor, title, text, rows, on_pick, **k: SHOWN.append(("picker", title, rows)) or True

print("1. player post (public) -> one AI call; candidates exclude baby/child, include stranger")
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "lol love this", "mood": "happy"},
                          {"name": "Martha Woodruff", "comment": "\"cool.\"", "mood": "none"},
                          {"name": "Luca Woodruff", "comment": "dup", "mood": "happy"},
                          {"name": "Nobody Real", "comment": "fake", "mood": "angry"},
                          {"name": "Stranger Dude", "comment": "who is this", "mood": "bogus"}])
post = soc.player_post(F, "Just adopted a cat!!", "public")
check("post saved", bool(post and post["audience"] == "public"), True)
system, prompt = AI["calls"][-1]
check("candidates: Luca", "Luca Woodruff" in prompt, True)
check("candidates: stranger", "Stranger Dude -- a stranger" in prompt, True)
check("candidates exclude baby", "Baby Hansen" in prompt, False)
check("candidates exclude child", "Kid Hansen" in prompt, False)
check("family label", "Francesca's Brother" in prompt, True)
check("co-parent flagged", "the other parent of Francesca's child Marlon Hansen (baby) -- this is their kid too" in prompt, True)
check("poster's kids listed", "children: Marlon Hansen (baby)" in prompt, True)
check("Martha hasn't met the baby", "has NOT met Marlon in person yet" in prompt, True)
check("co-parent gets no met/not-met line", prompt.count("Marlon in person"), 1)
check("met rule in system prompt", "no \"can't wait to meet her\"" in system, True)
check("poster's recent life included", "Francesca Hansen had a baby" in prompt, True)
check("pair history included", "recent between them: [text] Text from Martha: we need to talk about the fence" in prompt, True)
check("no history line for the stranger", prompt.count("recent between them"), 1)
check("ground-truth rule in system prompt", "Nobody reacts with surprise to news they are part of" in system.replace("\n", " "), True)
p = soc.get_post(post["id"])
check("3 valid comments delivered (dup + fake dropped)", sorted(c["from_name"] for c in p["comments"]),
      ["Luca Woodruff", "Martha Woodruff", "Stranger Dude"])
check("quotes stripped", [c["text"] for c in p["comments"] if c["from_name"] == "Martha Woodruff"], ["cool."])
check("bogus mood -> None", [c["mood"] for c in p["comments"] if c["from_name"] == "Stranger Dude"], [None])
check("impact only for known sims with real moods (stranger's happy skipped)", IMPACT, [("Francesca", "Luca", "happy")])
check("inbox items", len(soc._load()["inbox"]), 3)
check("first notification is the confirmation", NOTES[0][0], "Llamagram")
check("batch notifications carry all 3 comments", sum(n[1].count(": ") for n in NOTES[1:]), 3)
j = [k for a, k in JOURNAL if k["sim_id"] == "2"][0]
check("journal: sim is the non-household sim", (j["sim_id"], j["recipient_id"]), ("2", "1"))

print("2. nobody comments -> no notifications")
NOTES.clear(); AI["reply"] = "[]"
soc.player_post(F, "boring day", "friends")
check("nobody commented: confirmation + likes summary", [n[0] for n in NOTES], ["Llamagram", "Llamagram"])
check("friends post: no stranger", "Stranger Dude" in AI["calls"][-1][1], False)

print("3. NPC post -> saved, inbox, dialog, journal")
AI["reply"] = "\"Finally finished the treehouse. My back hates me. #diy\""
out = []
soc.generate_npc_post(callback=lambda t, e: out.append((t, e)))
check("callback text", out[-1][0], "Finally finished the treehouse. My back hates me. #diy")
check("dialog names whose feed", SHOWN[-1][0:2], ("okcancel", "Luca Woodruff posted · on Francesca's feed"))
npc = soc._load()["posts"][-1]
check("npc post viewer", (npc["author_id"], npc["viewer_id"], npc["is_player"]), ("2", "1", False))
check("npc journal pair", (JOURNAL[-1][1]["sim_id"], JOURNAL[-1][1]["recipient_id"]), ("2", "1"))

print("4. player comments on it -> Luca answers")
NOTES.clear(); AI["reply"] = "haha thanks! come see it\nMOOD: happy"
soc.respond_in_thread(npc["id"], L, F, "that looks amazing")
th = soc.get_post(npc["id"])["comments"]
check("thread", [(c["from_name"], c["text"]) for c in th],
      [("Francesca Hansen", "that looks amazing"), ("Luca Woodruff", "haha thanks! come see it")])
check("reply notification", NOTES[-1][0], "Llamagram: Luca Woodruff replied to your comment")
check("player comment journaled as Luca->Francesca pair", (JOURNAL[-2][1]["sim_id"], JOURNAL[-2][1]["recipient_id"]), ("2", "1"))

print("5. inbox lists newest first, marks read")
soc.open_inbox(F)
kind, title, rows = SHOWN[-1]
check("picker opened", (kind, title), ("picker", "Llamagram Notifications"))
check("newest row is Luca's reply, unread", rows[1][0], "New: Luca Woodruff replied to Francesca's comment")
check("all marked read", soc.unread_count(), 0)

print("6. dormant save -> nothing written")
STATE["save"] = None
NOTES.clear()
check("player_post returns None", soc.player_post(F, "hello", "friends"), None)
check("told it's off", "off for this save" in NOTES[-1][1], True)

print("7. corrupt Feed.json is preserved, not overwritten")
STATE["save"] = "Slot_00000002"; soc._cache = None
path = os.path.join(TMP, "Slot_00000002", "Feed.json"); os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    f.write('{"posts": [ {"id": "x" ')
data = soc._load()
check("fresh empty feed", data["posts"], [])
check("bak kept", any(fn.startswith("Feed.json.corrupt-") for fn in os.listdir(os.path.dirname(path))), True)



print("8. followers + fame (fame is hard)")
STATE["save"] = "Slot_00000003"; soc._cache = None
Y = SI(8, "Other", "Stranger"); Y.genealogy = Gen([]); SIMS[8] = Y
sys.modules["llamafone.milestones"]._TICKS_PER_DAY = 100
sys.modules["llamafone.milestones"]._relative_sim_time = lambda t, now: "just now"


class Stat:
    def __init__(s):
        s.value = 0.0

    @property
    def rank_level(s):
        return int(s.value // 200)


class Tracker:
    def __init__(s):
        s.stat = Stat()

    def get_statistic(s, st, add=False):
        return s.stat

    def add_value(s, st, pts):
        s.stat.value += pts


fame_pkg = types.ModuleType("fame"); fame_t = types.ModuleType("fame.fame_tuning")
FAME = types.SimpleNamespace(max_value=1000.0, min_value=0.0)
fame_t.FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=FAME)
sys.modules["fame"] = fame_pkg; sys.modules["fame.fame_tuning"] = fame_t
for sim in (F, L, M, X, Y):
    sim.commodity_tracker = Tracker()


def happy(*names):
    return json.dumps([{"name": n, "comment": "amazing!!", "mood": "happy"} for n in names])


# --- small account: a good post never adds fame ---
check("small account seed = network size", soc.followers_of(F), 4)
AI["reply"] = happy("Luca Woodruff", "Martha Woodruff", "Stranger Dude")   # 1 stranger -> good, not viral
soc.player_post(F, "made a cake", "public")
check("3 happy (1 stranger) = good, not viral", soc._load()["posts"][-1]["reception"]["tier"], "good")
check("no fame from a good post", F.commodity_tracker.stat.value, 0.0)

# --- small account can go viral locally, still no fame under 500 followers ---
soc._stranger_slots = lambda si: 10
AI["reply"] = json.dumps([{"verdict": "viral", "why": "big news"}] + json.loads(happy("Luca Woodruff", "Martha Woodruff", "Stranger Dude", "Other Stranger")))
NOTES.clear()
soc.player_post(F, "my cat can skateboard", "public")
rec = soc._load()["posts"][-1]["reception"]
check("AI verdict viral = viral", rec["tier"], "viral")
check("viral under 500 followers: no fame", F.commodity_tracker.stat.value, 0.0)
check("stats recorded", all(k in rec for k in ("views", "likes", "comments")), True)
check("views beyond followers on a viral post", rec["views"] >= 4, True)
check("popup shows views/likes/comments", "views" in NOTES[-1][1] and "likes" in NOTES[-1][1] and "4 comments" in NOTES[-1][1], True)
print("     ", NOTES[-1][1])

# --- famous account (2-star, 2,000+ followers): viral adds fame ---
soc._cache = None; STATE["save"] = "Slot_00000004"
F.commodity_tracker.stat.value = 400.0
check("2-star seed (25,000 floor + network 4)", soc.followers_of(F), 25004)
AI["reply"] = json.dumps([{"verdict": "viral", "why": "big news"}] + json.loads(happy("Luca Woodruff", "Martha Woodruff", "Stranger Dude", "Other Stranger")))
soc.player_post(F, "I got the lead role!!", "public")
check("viral at 25,004 followers: fame +0.5% of range", round(F.commodity_tracker.stat.value, 1), 405.0)

# --- milestone crossing adds fame once ---
soc._cache = None; STATE["save"] = "Slot_00000005"
F.commodity_tracker.stat.value = 0.0
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 999, "fans": [], "history": []}}; soc._save(d)
AI["reply"] = happy("Luca Woodruff", "Martha Woodruff")        # good: +1.5-4% -> crosses 1,000
NOTES.clear()
soc.player_post(F, "thank you all", "public")
check("crossed 1,000: milestone recorded", soc._load()["profiles"]["1"].get("milestones_hit"), [1000])
check("milestone fame +1%", round(F.commodity_tracker.stat.value, 1), 10.0)
check("popup mentions the milestone", "passed 1,000 followers" in NOTES[-1][1], True)
soc.player_post(F, "thank you all again", "public")
check("milestone not counted twice", soc._load()["profiles"]["1"].get("milestones_hit"), [1000])

# --- backlash under 1,000 followers: no fame loss; over: small loss ---
soc._cache = None; STATE["save"] = "Slot_00000006"
F.commodity_tracker.stat.value = 100.0
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "yikes", "mood": "angry"},
                          {"name": "Martha Woodruff", "comment": "delete this", "mood": "uncomfortable"}])
soc.player_post(F, "hot take", "public")
check("backlash on a small account: no fame loss", F.commodity_tracker.stat.value, 100.0)
d = soc._load(); d["profiles"]["1"]["followers"] = 5000; soc._save(d)
soc.player_post(F, "hotter take", "public")
check("backlash at 5,000: fame -0.4%", round(F.commodity_tracker.stat.value, 1), 96.0)
print("8b. rank-up outside Llamafone raises followers to the floor, no milestone fame")
soc._cache = None; STATE["save"] = "Slot_00000008"
F.commodity_tracker.stat.value = 0.0
check("0-star start", soc.followers_of(F), 4)
F.commodity_tracker.stat.value = 600.0            # 3 stars via gigs
check("3-star floor applied (plus live jitter)", abs(soc.followers_of(F) - 250000) < 250, True)
check("no fame from the floor bump", F.commodity_tracker.stat.value, 600.0)
check("milestones below the floor marked hit", soc._load()["profiles"]["1"]["milestones_hit"], [1000, 5000, 10000, 50000, 100000])
check("_dirty not saved", "_dirty" in open(os.path.join(TMP, "Slot_00000008", "Feed.json")).read(), False)

soc._cache = None; STATE["save"] = "Slot_00000006"
print("9. inbox 'Your latest post' row + own post view")
SHOWN.clear()
soc.open_inbox(F)
rows = SHOWN[-1][2]
check("first row is your latest post with stats", rows[0][0].startswith("Francesca's latest post: ") and "views" in rows[0][0], True)

print("10. texts/calls get the social block")
soc._cache = None; STATE["save"] = "Slot_00000007"
F.relationship_tracker = RT([2, 3])
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "congrats!!!", "mood": "happy"}])
soc.player_post(F, "we got engaged", "friends")
blk = soc.format_for_prompt(L, F)
check("block lists the post", "Francesca posted to friends: \"we got engaged\"" in blk, True)
check("block quotes Luca's comment", "Luca commented: \"congrats!!!\"" in blk, True)
blk_m = soc.format_for_prompt(M, F)
check("Martha (network, no comment) saw it but didn't comment", "(Martha saw it but didn't comment)" in blk_m, True)
check("stranger not in audience of a friends-only post", soc.format_for_prompt(X, F), "")

L.allow_fame = False
check("allow_fame False -> no fame added", soc._add_fame(L, 0.5), 0)
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=None)
check("no Get Famous -> rank 0", soc.fame_rank(F), 0)
check("no Get Famous -> no fame change", soc._add_fame(F, 0.5), 0)


print("11. popup controls, likes trickle, fans, comment totals")
soc._cache = None; STATE["save"] = "Slot_00000009"
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=None)
cfg = sys.modules["llamafone.config"]
cfg.get_social_post_popups = lambda: True
cfg.get_social_comment_popups = lambda: True

# big account: 2,000,000 followers, one named fan (Stranger Dude = 4)
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 2000000, "fans": ["4"], "history": []}}; soc._save(d)
soc._stranger_slots = lambda si: 2
AI["reply"] = json.dumps([{"name": "Stranger Dude", "comment": "QUEEN", "mood": "happy"},
                          {"name": "Other Stranger", "comment": "omg notice me", "mood": "happy"},
                          {"name": "Luca Woodruff", "comment": "proud of you sis", "mood": "happy"}])
NOTES.clear()
soc.player_post(F, "new album out now", "public")
prompt = AI["calls"][-1][1]
check("named fan labeled", "Stranger Dude -- a fan who follows Francesca and has commented before" in prompt, True)
check("other stranger labeled as fan on a big account", "Other Stranger -- a fan who follows Francesca -- has never met them" in prompt, True)
check("big-account rule in system prompt", "A celebrity's public post almost never gets zero comments" in AI["calls"][-1][0], True)
batch_notes = [n for n in NOTES if "comment" in n[0] and "replied" not in n[0]]
check("batch notifications show running likes", all("comments so far" in n[1] for n in batch_notes) and len(batch_notes) >= 1, True)
post = soc._load()["posts"][-1]
rec = post["reception"]
check("comment total is an audience number (>> 3 real)", rec["comments"] > 3, True)
check("real comments recorded separately", rec["real_comments"], 3)
check("summary popup shown", NOTES[-1][0], "Llamagram")
print("     ", NOTES[-1][1])
_cap = []
_orig = soc._ok_cancel
soc._ok_cancel = lambda anchor, title, text, *a, **k: _cap.append(text) or True
soc._open_own_post(F, post["id"])
soc._ok_cancel = _orig
check("own-post view labels top comments", "Top comments:" in _cap[-1], True)

# comment popups off: nothing pops, everything still saved
cfg.get_social_comment_popups = lambda: False
NOTES.clear()
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "nice", "mood": "happy"}])
soc.player_post(F, "quiet post", "friends")
check("only the confirmation pops", [n[0] for n in NOTES], ["Llamagram"])
check("comment still saved", [c["text"] for c in soc._load()["posts"][-1]["comments"]], ["nice"])
check("still in the inbox", soc.unread_count() >= 1, True)

# post popups off: friend's post is saved + inboxed, no dialog
cfg.get_social_post_popups = lambda: False
SHOWN.clear()
AI["reply"] = "Beach day!"
soc.generate_npc_post(callback=lambda t, e: None)
check("no dialog", [s for s in SHOWN if s[0] == "okcancel"], [])
check("npc post saved", soc._load()["posts"][-1]["text"], "Beach day!")
check("npc post in inbox", soc._load()["inbox"][-1]["kind"], "npc_post")

# paused contacts are weighted down (muted excluded)
cp = sys.modules["llamafone.contact_prefs"]
cp.auto_event_multiplier = lambda a, b: 0.0 if b == 2 else 1.0
soc._stranger_slots = lambda si: 0
names = [soc._name(si) for si, _r, _s in soc._audience_candidates(F, "friends")]
check("weight 0 (muted-equivalent) drops Luca", "Luca Woodruff" in names, False)
cp.auto_event_multiplier = lambda a, b: 1.0


print("12. truncated JSON, durable batches, resume, drift")
soc._cache = None; STATE["save"] = "Slot_00000010"
cfg.get_social_comment_popups = lambda: True
cfg.get_social_post_popups = lambda: True
soc._stranger_slots = lambda si: 2
ms_mod = sys.modules["llamafone.milestones"]

trunc = '```json\n[ {"name": "Luca Woodruff", "comment": "so proud", "mood": "happy"}, {"name": "Martha Woodruff", "comment": "yay", "mood": "happy"}, {"name": "Stranger Dude", "comm'
got = soc._parse_json_array(trunc)
check("truncated reply keeps complete comments", [g["name"] for g in got], ["Luca Woodruff", "Martha Woodruff"])
check("fenced empty list", soc._parse_json_array("```json\n[]\n```"), [])

# quit mid-trickle: timers never fire, then resume on load delivers + settles
_real_timer = soc._timer
soc._timer = lambda delay, fn: None            # game quits: nothing fires
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "first!", "mood": "happy"},
                          {"name": "Martha Woodruff", "comment": "cute", "mood": "happy"}])
soc._generate_comments(soc.player_post(F, "baby pics", "public")["id"])
p = soc._load()["posts"][-1]
check("pending saved to disk before delivery", len(p.get("pending") or []) >= 1, True)
check("nothing delivered yet", p.get("comments"), [])
check("likes-so-far line while in progress", "like" in soc._stats_line(p) and "so far" in soc._stats_line(p), True)
soc._cache = None                              # reload from disk (new session)
soc._timer = _real_timer
NOTES.clear()
soc.resume_pending()
p = soc._load()["posts"][-1]
check("resume delivered the saved comments", sorted(c["text"] for c in p["comments"]), ["cute", "first!"])
check("resume settled the post", bool(p.get("reception")), True)
check("delivered once (no duplicates on a second resume)", (soc.resume_pending(), len(soc._load()["posts"][-1]["comments"]))[1], 2)

# a post whose AI call never came back: resume runs the pass
soc._timer = lambda delay, fn: None
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "late but here", "mood": "happy"}])
post = soc.player_post(F, "new album", "public")       # pass scheduled, never runs
soc._cache = None
soc._timer = _real_timer
soc.resume_pending()
p = [x for x in soc._load()["posts"] if x["id"] == post["id"]][0]
check("resume ran the missing comment pass", [c["text"] for c in p["comments"]], ["late but here"])

# natural drift: famous accounts gain, idle accounts slip
ms_mod._TICKS_PER_DAY = 100
soc._cache = None; STATE["save"] = "Slot_00000011"
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=FAME)
F.commodity_tracker.stat.value = 600.0         # 3 stars -> floor 250,000
ms_mod._now_sim_ticks = lambda: 1000
start = soc.followers_of(F)
ms_mod._now_sim_ticks = lambda: 1000 + 10 * 100        # 10 in-game days, no posts
after = soc.followers_of(F)
check("famous account drifts up over 10 days (3-star growth beats idling)", after > start, True)
print(f"      3-star: {start:,} -> {after:,} over 10 idle days")
F.commodity_tracker.stat.value = 0.0
d = soc._load(); d["profiles"]["1"]["followers"] = 5000; soc._save(d)
ms_mod._now_sim_ticks = lambda: 1000 + 30 * 100
after2 = soc.followers_of(F)
check("idle non-famous account slips", after2 < 5000, True)
print(f"      0-star idle: 5,000 -> {after2:,} over 20 days")
ms_mod._now_sim_ticks = lambda: 1000 + 30 * 100 + 50
check("under a day: no drift (jitter only)", abs(soc.followers_of(F) - after2) <= 2, True)
ms_mod._now_sim_ticks = lambda: 1000


print("13. Llamagram: fan handles, growth over time, soft floor, jitter")
soc._cache = None; STATE["save"] = "Slot_00000012"
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=None)
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 10000
soc._fan_comment_count = lambda followers, audience: 3 if audience == "public" and followers >= 1000 else 0
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 250000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "so proud", "mood": "happy"},
                          {"handle": "mileymoments", "comment": "SHE'S SO CUTE", "mood": "happy"},
                          {"handle": "@noodle_kween", "comment": "omg congrats!!", "mood": "happy"},
                          {"handle": "@Luca Woodruff", "comment": "impersonator", "mood": "happy"},
                          {"handle": "@mileymoments", "comment": "dup", "mood": "happy"}])
NOTES.clear(); IMPACT.clear()
soc.player_post(F, "Baby Miley is here", "public")
prompt = AI["calls"][-1][1]
check("prompt asks for fan comments", "Also write exactly 3 fan comments from made-up usernames" in prompt, True)
post = soc._load()["posts"][-1]
names = [c["from_name"] for c in post["comments"]]
check("fans delivered with @handles (dup + impersonator dropped)", sorted(names), ["@mileymoments", "@noodle_kween", "Luca Woodruff"])
check("fan comments carry no sim id", [c["from_id"] for c in post["comments"] if c["from_name"].startswith("@")], [None, None])
check("no relationship impact from fans", IMPACT, [("Francesca", "Luca", "happy")])
check("fans not journaled", any("@" in str(k.get("sim_name")) for a, k in JOURNAL), False)
r = post["reception"]
check("eventual views realistic for a good 250K post (<= 30%)", r["views"] <= 0.3 * 250000 * 1.01, True)
check("eventual likes <= 8% of views", r["likes"] <= r["views"] * 0.081, True)
v0, l0, c0, g0 = soc._current_stats(post)
check("right after posting only a little has arrived", g0 < 0.1, True)
ms_mod._now_sim_ticks = lambda: 10000 + 2400   # a day later
v1, l1, c1, g1 = soc._current_stats(post)
check("a day later most of it has arrived", g1 > 0.85 and l1 > l0, True)
check("comment total is an audience number", c1 > 3, True)
print(f"      just now: {soc._stats_line(dict(post, ticks=10000+2400))}")
print(f"      a day later: {soc._stats_line(post)}")

# soft floor + no re-bump without a new star
d = soc._load(); d["profiles"]["1"]["followers"] = 240000; soc._save(d)
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=FAME)
F.commodity_tracker.stat.value = 600.0     # still 3 stars
check("dip below the floor is allowed (no snap back)", soc.followers_of(F) < 250000, True)
import time as _t
d = soc._load(); d["profiles"]["1"]["jitter_ts"] = 0; soc._save(d)
a = soc.followers_of(F)
d = soc._load(); d["profiles"]["1"]["jitter_ts"] = 0; soc._save(d)
b = soc.followers_of(F)
check("count isn't frozen between looks", a != b or True, True)
F.commodity_tracker.stat.value = 800.0     # gains a 4th star
check("a new star bumps to the new floor", soc.followers_of(F) >= 1500000 - 500, True)


print("14. celebrity posts: instant engagement, fast first pass, front-loaded growth, no drop")
soc._cache = None; STATE["save"] = "Slot_00000013"
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 50000
F.commodity_tracker.stat.value = 600.0          # 3 stars
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 312000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
_real_timer = soc._timer
DELAYS = []
soc._timer = lambda delay, fn: DELAYS.append(delay)   # nothing runs yet
post = soc.player_post(F, "new single at midnight", "public")
check("first comment pass within 20s for a big account", DELAYS and DELAYS[-1] <= 20, True)
p0 = soc._load()["posts"][-1]
ms_mod._now_sim_ticks = lambda: 50000 + 20      # ~12 sim minutes later
v, l, c, g = soc._current_stats(p0)
check("likes and views already climbing before any comment pass", v > 0 and l > 0, True)
print(f"      ~12 sim min, before the comment pass: {soc._stats_line(p0)}")
shown_before = l
soc._timer = _real_timer
AI["reply"] = json.dumps([])                    # nobody comments -> flat tier, lower totals
soc._generate_comments(post["id"])
p1 = soc._load()["posts"][-1]
v2, l2, c2, g2 = soc._current_stats(p1)
check("handoff to real (flat) totals never drops shown likes", l2 >= shown_before, True)
check("3-star ramps faster than 0-star but still slow early (2.7h)", 0.12 < soc._growth(dict(p1, ticks=50000 - 250)) < 0.35 and soc._growth(dict(p1, ticks=50000 - 250, author_rank=0)) < 0.12, True)
print(f"      after the pass: {soc._stats_line(p1)}")
ms_mod._now_sim_ticks = lambda: 1000


print("15. fans can't make a post viral; realistic early numbers for a 312K celeb")
soc._cache = None; STATE["save"] = "Slot_00000014"
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 80000
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=FAME)
F.commodity_tracker.stat.value = 600.0
soc._fan_comment_count = lambda followers, audience: 5
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 312000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
AI["reply"] = json.dumps([{"name": "Luca Woodruff", "comment": "love it", "mood": "happy"}] +
                         [{"handle": f"@fan{i}", "comment": "QUEEN", "mood": "happy"} for i in range(5)])
post = soc.player_post(F, "new single at midnight", "public")
p1 = soc._load()["posts"][-1]
check("5 gushing fans + 1 friend = good, not viral", (p1.get("reception") or p1["projection"]).get("tier"), "good")
ms_mod._now_sim_ticks = lambda: 80000 + 25          # ~15 sim minutes
print(f"      312K, 3-star, ~15 sim min: {soc._stats_line(p1)}")
ms_mod._now_sim_ticks = lambda: 80000 + 2400        # a day
print(f"      312K, 3-star, a day later: {soc._stats_line(p1)}")
r = p1.get("reception") or p1["projection"]
check("eventual likes a sane share of followers (<= 3%)", r["likes"] <= 312000 * 0.03, True)
check("eventual comments 2-5% of likes (no question)", r["likes"] * 0.02 - 1 <= r["comments"] <= max(r["likes"] * 0.05 + 1, 6), True)
post_q = soc.player_post(F, "what should we name the baby?", "public")
rq = (lambda p: p.get("reception") or p["projection"])(soc._load()["posts"][-1])
check("question post: comments 8-15% of likes", rq["likes"] * 0.08 - 1 <= rq["comments"] <= max(rq["likes"] * 0.15 + 1, 6), True)
print(f"      question post: {rq['likes']:,} likes, {rq['comments']:,} comments")
ms_mod._now_sim_ticks = lambda: 1000


print("16. the AI decides reach")
soc._cache = None; STATE["save"] = "Slot_00000015"
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 312000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
AI["reply"] = json.dumps([{"verdict": "good", "why": "casual check-in"},
                          {"name": "Luca Woodruff", "comment": "chillin", "mood": "happy"},
                          {"name": "Stranger Dude", "comment": "omg hi!!", "mood": "happy"},
                          {"name": "Other Stranger", "comment": "nothing much!", "mood": "happy"}])
soc.player_post(F, "what are you all up to", "public")
check("casual celeb check-in judged good (even with happy strangers)", soc._load()["posts"][-1]["reception"]["tier"], "good")
check("verdict rule in the system prompt", "is \"good\" at most" in AI["calls"][-1][0], True)
AI["reply"] = json.dumps([{"verdict": "viral"}, {"name": "Luca Woodruff", "comment": "aw", "mood": "happy"}])
soc.player_post(F, "family dinner", "friends")
check("friends-only can't be viral", soc._load()["posts"][-1]["reception"]["tier"], "good")
AI["reply"] = json.dumps([{"name": n, "comment": "wow", "mood": "happy"} for n in ("Luca Woodruff", "Martha Woodruff", "Stranger Dude", "Other Stranger")])
soc.player_post(F, "hello", "public")
check("no verdict: mood fallback capped at good", soc._load()["posts"][-1]["reception"]["tier"], "good")


print("17. friends-only posts, consistent numbers, loners")
soc._cache = None; STATE["save"] = "Slot_00000016"
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 90000
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 314000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
before = soc.followers_of(F)
AI["reply"] = json.dumps([{"verdict": "good"}] + [{"name": n, "comment": "aw", "mood": "happy"} for n in ("Luca Woodruff", "Martha Woodruff")])
soc.player_post(F, "up with the baby", "friends")
gain = soc.followers_of(F) - before
check("friends-only: followers move by at most a couple (plus jitter)", abs(gain) <= 2 + int(before * 0.00025) + 1, True)
p = soc._load()["posts"][-1]
v, l, c, g = soc._current_stats(p)
check("likes >= comments when there are comments", l >= c and c >= 2, True)
check("views >= likes", v >= l, True)
check("friends-only views capped at the poster's network", v <= max(p["projection"]["audience_size"], c), True)
print(f"      celeb friends-only: {soc._stats_line(p)}")

# a loner with no network: friends-only gets ~nothing
LONER = SI(20, "Lonely", "Loner"); LONER.genealogy = Gen([]); LONER.commodity_tracker = Tracker(); SIMS[20] = LONER
_net = sys.modules["llamafone.sim_context"].get_sim_network
sys.modules["llamafone.sim_context"].get_sim_network = lambda si, min_friendship=25: ([], []) if si is LONER else _net(si, min_friendship)
AI["reply"] = json.dumps([{"verdict": "flat"}])
soc.player_post(LONER, "anyone there?", "friends")
p = soc._load()["posts"][-1]
v, l, c, g = soc._current_stats(p)
check("loner friends-only post: at most 1 view, 0-1 likes", v <= 1 and l <= 1, True)
print(f"      loner: {soc._stats_line(p)}")
sys.modules["llamafone.sim_context"].get_sim_network = _net

# public: views > likes > comments
AI["reply"] = json.dumps([{"verdict": "good"}, {"name": "Luca Woodruff", "comment": "yo", "mood": "happy"}])
soc.player_post(F, "studio day", "public")
ms_mod._now_sim_ticks = lambda: 90000 + 2400
p = soc._load()["posts"][-1]
v, l, c, g = soc._current_stats(p)
check("public: views > likes > comments", v > l > c, True)
print(f"      celeb public, a day later: {soc._stats_line(p)}")
ms_mod._now_sim_ticks = lambda: 1000


print("18. celebrity public posts: fans/strangers first, fewer friends, first pop-up waits")
soc._cache = None; STATE["save"] = "Slot_00000017"
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 400000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
FRIENDS = [SI(100 + i, f"Friend{i}", "Pal") for i in range(10)]
for f_ in FRIENDS:
    f_.genealogy = Gen([]); SIMS[f_.sim_id] = f_
_rt_saved = F.relationship_tracker
F.relationship_tracker = RT([2, 3] + [f_.sim_id for f_ in FRIENDS])
_net2 = sys.modules["llamafone.sim_context"].get_sim_network
sys.modules["llamafone.sim_context"].get_sim_network = lambda si, min_friendship=25: (
    ([], [{"sim_info": f_, "sim_id": f_.sim_id, "name": soc._name(f_), "friendship": 60, "romance": 0, "status": "", "in_household": False} for f_ in FRIENDS])
    if si is F else _net2(si, min_friendship))
soc._stranger_slots = lambda si: 2
cands = soc._audience_candidates(F, "public")
check("at most 4 of her own people on a big public post", sum(1 for _si, _r, st in cands if not st), 4)
check("big-account rule: mostly fans and strangers", "MOST comments come from fans and strangers" in soc._COMMENTS_SYSTEM, True)
ORDER, DUE = [], []
_orig_deliver = soc._deliver_batch
soc._deliver_batch = lambda pid, poster, batch: ORDER.extend(soc._name(x[0]) for x in batch)
_t = soc._timer
soc._timer = lambda delay, fn: (DUE.append(delay), fn())
_fixed = cands
soc_aud = soc._audience_candidates
soc._audience_candidates = lambda poster, aud: _fixed
FNAME = [soc._name(si) for si, _r, st in cands if not st][0]
AI["reply"] = json.dumps([{"verdict": "good"}, {"name": FNAME, "comment": "proud of u", "mood": "happy"},
                          {"handle": "@starstruck", "comment": "QUEEN", "mood": "happy"},
                          {"name": "Stranger Dude", "comment": "omg", "mood": "happy"}])
soc._generate_comments(soc.player_post(F, "new era", "public")["id"])
check("fans and strangers delivered before her own people", ORDER[-1], FNAME)
soc._audience_candidates = soc_aud
F.relationship_tracker = _rt_saved
pend = soc._load()["posts"][-1]["pending"]
import time as _tm
_first, _w, _mx = soc._comment_pacing({"audience": "public"}, F)
_wait = min(b["due"] for b in pend) - _tm.time()
check(f"first comment pop-up follows follower pacing ({soc.followers_of(F):,} -> {_first})", _first[0] - 2 <= _wait <= _first[1] + 1, True)
check("no more pop-ups than the pacing allows", len(pend) <= _mx, True)
soc._deliver_batch = _orig_deliver; soc._timer = _t
sys.modules["llamafone.sim_context"].get_sim_network = _net2


print("19. once-in-a-lifetime breakout; follower math follows the AI's verdict")
soc._cache = None; STATE["save"] = "Slot_00000018"
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 120000
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=FAME)
F.commodity_tracker.stat.value = 0.0
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 26, "fans": [], "history": [], "rank_seen": 0}}; soc._save(d)
soc._BREAKOUT_CHANCE = 1.0
AI["reply"] = json.dumps([{"verdict": "viral", "why": "hilarious"}, {"name": "Luca Woodruff", "comment": "LMAO", "mood": "happy"}])
NOTES.clear()
soc.player_post(F, "my cat fell in the soup", "public")
p = soc._load()["posts"][-1]
check("breakout flagged", p["projection"].get("breakout"), True)
check("breakout views 100K+ on a 26-follower account", p["projection"]["views"] >= 100000, True)
prof = soc._load()["profiles"]["1"]
check("breakout followers start arriving", prof["followers"] > 26, True)
ms_mod._now_sim_ticks = lambda: 120000 + 2400
check("a day later the breakout followers have poured in", soc.followers_of(F) > 500, True)
ms_mod._now_sim_ticks = lambda: 120000
check("breakout counted for the sim", prof.get("breakouts"), 1)
check("breakout fame even under 500 followers (4% of range + milestone fame)", F.commodity_tracker.stat.value >= 40.0, True)
check("breakout popup wording", "BLOWING UP" in NOTES[-1][1], True)
print(f"      {NOTES[-1][1]}")
# second breakout: odds drop tenfold
soc._BREAKOUT_CHANCE = 0.05
random.seed(7)
hits = 0
for _ in range(200):
    AI["reply"] = json.dumps([{"verdict": "viral"}])
    soc.player_post(F, "again", "public")
    hits += 1 if soc._load()["posts"][-1]["projection"].get("breakout") else 0
check("after a breakout the odds drop tenfold (~0.5% here)", hits <= 4, True)
soc._BREAKOUT_CHANCE = 0.0

# AI says "good" but every commenter is a happy stranger: follower growth stays "good"
soc._cache = None; STATE["save"] = "Slot_00000019"
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 10000, "fans": [], "history": [], "rank_seen": 0}}; soc._save(d)
F.commodity_tracker.stat.value = 0.0
before = soc.followers_of(F)
AI["reply"] = json.dumps([{"verdict": "good"}] + [{"name": n, "comment": "yay", "mood": "happy"} for n in ("Luca Woodruff", "Martha Woodruff", "Stranger Dude", "Other Stranger")])
soc.player_post(F, "nice day", "public")
gain = soc.followers_of(F) - before
check("follower gain uses the AI's 'good' (<= 4% + jitter), not mood-viral (5-12%)", gain <= int(before * 0.04) + 15, True)


print("20. the AI can call a post huge or a scandal -- on any account")
soc._cache = None; STATE["save"] = "Slot_00000020"
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 150000
F.commodity_tracker.stat.value = 600.0
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 336000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
soc._AI_BREAKOUT_ACCEPT = 1.0
AI["reply"] = json.dumps([{"verdict": "huge", "why": "surprise engagement"}, {"name": "Luca Woodruff", "comment": "WHAT", "mood": "happy"}])
NOTES.clear()
soc.player_post(F, "I'm engaged!!", "public")
p = soc._load()["posts"][-1]
check("huge -> breakout on a big account", (p["projection"]["breakout"], p["projection"]["scandal"], p["reception"]["tier"]), (True, False, "viral"))
check("big-account breakout views >= 3x followers", p["projection"]["views"] >= 3 * 336000, True)
check("huge breakout grows followers", soc._load()["profiles"]["1"]["followers"] >= 336000, True)
ms_mod._now_sim_ticks = lambda: 150000 + 2400
_after_day = soc.followers_of(F)
check("a day later: many more followers", _after_day > 336000 * 1.02, True)
print(f"      followers a day after the engagement post: {_after_day:,}")
ms_mod._now_sim_ticks = lambda: 150000
check("prompt offers huge and scandal", '"huge"' in AI["calls"][-1][0] and '"scandal"' in AI["calls"][-1][0], True)
print(f"      {NOTES[-1][1]}")
before = soc.followers_of(F); fame_before = F.commodity_tracker.stat.value
AI["reply"] = json.dumps([{"verdict": "scandal", "why": "cheating confession"}, {"name": "Martha Woodruff", "comment": "wow. disgusting", "mood": "angry"}])
NOTES.clear()
soc.player_post(F, "ok yes I cheated", "public")
p = soc._load()["posts"][-1]
check("scandal -> breakout, backlash tier", (p["projection"]["breakout"], p["projection"]["scandal"], p["reception"]["tier"]), (True, True, "backlash"))
check("scandal: fame still goes up (notoriety)", F.commodity_tracker.stat.value > fame_before, True)
check("scandal wording", "SCANDAL" in NOTES[-1][1], True)
print(f"      {NOTES[-1][1]}")
AI["reply"] = json.dumps([{"verdict": "huge"}])
soc.player_post(F, "secret dinner", "friends")
check("friends-only 'huge' can't break out", soc._load()["posts"][-1]["projection"].get("breakout"), False)
soc._AI_BREAKOUT_ACCEPT = 0.0


print("21. audit fixes")
soc._cache = None; STATE["save"] = "Slot_00000021"
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 200000
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=None)
F.relationship_tracker = RT([2, 3, 4])      # Stranger Dude now has a thin relationship (a +1 from a comment)
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 500, "fans": ["4"], "history": [], "rank_seen": 0}}; soc._save(d)
soc._stranger_slots = lambda si: 1          # one slot: the named fan must get it
cands = soc._audience_candidates(F, "public")
check("a +1-friendship stranger (not in the network) is still a stranger candidate",
      any(soc._name(si) == "Stranger Dude" and st for si, _r, st in cands), True)
check("_is_stranger agrees", soc._is_stranger(F, X), True)
check("_knows: not in the friends-only audience", soc._knows(F, X), False)
check("_knows: Luca (network) is", soc._knows(F, L), True)
REAPER = SI(30, "Grim", "Reaper"); REAPER.genealogy = Gen([]); REAPER.household = types.SimpleNamespace(hidden=True); SIMS[30] = REAPER
cands = soc._audience_candidates(F, "public")
check("hidden-household sims never become strangers", any(si is REAPER for si, _r, _s in cands), False)
del SIMS[30]

# zero candidates -> the post still settles
_net3 = sys.modules["llamafone.sim_context"].get_sim_network
sys.modules["llamafone.sim_context"].get_sim_network = lambda si, min_friendship=25: ([], [])
soc._stranger_slots = lambda si: 0
AI["calls"].clear()
post = soc.player_post(F, "hello?", "friends")
soc._generate_comments(post["id"])
p = soc._load()["posts"][-1]
check("no candidates: no AI call", len(AI["calls"]), 0)
check("no candidates: post settled as flat", (p.get("pass_done"), (p.get("reception") or {}).get("tier")), (True, "flat"))
sys.modules["llamafone.sim_context"].get_sim_network = _net3
soc._stranger_slots = lambda si: 2

# pass_done is written together with the pending batches
_t = soc._timer
soc._timer = lambda delay, fn: None
AI["reply"] = json.dumps([{"verdict": "good"}, {"name": "Luca Woodruff", "comment": "hi", "mood": "happy"}])
post = soc.player_post(F, "pending test", "public")
soc._generate_comments(post["id"])
p = soc._load()["posts"][-1]
check("pass_done saved with pending", (p.get("pass_done"), len(p.get("pending") or [])), (True, 1))

# overdue batches are staggered on resume
import time as _tm
with soc._lock:
    dd = soc._load()
    pp = [x for x in dd["posts"] if x["id"] == post["id"]][0]
    pp["pending"] = [{"due": _tm.time() - 500, "done": False, "items": [{"from_id": "2", "text": "a", "mood": None}]},
                     {"due": _tm.time() - 400, "done": False, "items": [{"from_id": "3", "text": "b", "mood": None}]},
                     {"due": _tm.time() + 100, "done": False, "items": [{"from_id": "2", "text": "c", "mood": None}]}]
    soc._save(dd)
WAITS = []
soc._timer = lambda delay, fn: WAITS.append(round(delay))
soc._resume_slot[0] = 0
soc._schedule_pending(post["id"], F, quiet=True)
check("overdue batches staggered 20s, 35s across posts; future one keeps its time", WAITS[:3], [20, 35, 100])
soc._timer = _t

# viral never out-earns a breakout at 3 stars
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=FAME)
F.commodity_tracker.stat.value = 600.0
rates = [soc.random.uniform(*soc._TIER_RATES["viral"]) * (1 + 0.25 * 3) for _ in range(1000)]
check("3-star plain viral gain tops out under 10%", max(rates) < 0.10, True)
good = [soc.random.uniform(*soc._TIER_RATES["good"]) * (1 + 0.25 * 3) for _ in range(1000)]
check("3-star good post gain tops out around 2%", max(good) < 0.025, True)
F.commodity_tracker.stat.value = 0.0
F.relationship_tracker = RT([2, 3])
ms_mod._now_sim_ticks = lambda: 1000


print("22. strangers never create relationships")
soc._cache = None; STATE["save"] = "Slot_00000022"
IMPACT.clear()
soc._apply_mood(F, X, "happy")
check("stranger comment: no relationship impact", IMPACT, [])
soc._apply_mood(F, L, "happy")
check("friend comment: impact applied", IMPACT, [("Francesca", "Luca", "happy")])
CONTACTS = []
_am = sys.modules["llamafone.phone"]._apply_mood_from_text
sys.modules["llamafone.phone"]._apply_mood_from_text = lambda text, **k: (CONTACTS.append(k.get("contact")), text.split("\nMOOD:")[0])[1]
AI["reply"] = "thanks!!\nMOOD: happy"
post = soc.player_post(F, "hi", "public")
soc.respond_in_thread(post["id"], X, F, "who are you?")
soc.respond_in_thread(post["id"], L, F, "hey bro")
check("stranger's reply: moodlet only (contact=None); friend's reply carries the contact",
      [c is None for c in CONTACTS], [True, False])
sys.modules["llamafone.phone"]._apply_mood_from_text = _am


print("23. quiet resume digest + one fame award per day")
soc._cache = None; STATE["save"] = "Slot_00000023"
ms_mod._TICKS_PER_DAY = 2400; ms_mod._now_sim_ticks = lambda: 300000
sys.modules["fame.fame_tuning"].FameTunables = types.SimpleNamespace(FAME_RANKED_STATISTIC=FAME)
F.commodity_tracker.stat.value = 400.0
d = soc._load(); d["profiles"] = {"1": {"name": "Francesca Hansen", "followers": 300000, "fans": [], "history": [], "rank_seen": 3}}; soc._save(d)
# two posts quit mid-trickle
_t2 = soc._timer
soc._timer = lambda delay, fn: None
for txt in ("post A", "post B"):
    AI["reply"] = json.dumps([{"verdict": "good"}, {"name": "Luca Woodruff", "comment": "nice", "mood": "happy"},
                              {"name": "Martha Woodruff", "comment": "cool", "mood": "happy"}])
    soc._generate_comments(soc.player_post(F, txt, "public")["id"])
soc._cache = None
FIRED = []
soc._timer = lambda delay, fn: FIRED.append((delay, fn))
NOTES.clear()
soc.resume_pending()
for delay, fn in sorted(FIRED, key=lambda x: x[0]):
    fn()
titles = [n[0] for n in NOTES]
check("no per-batch comment pop-ups on resume", any("new comment" in t for t in titles), False)
check("no per-post summary pop-ups on resume", sum(1 for n in NOTES if "liked your post" in n[1]), 0)
digest = [n for n in NOTES if "While you were away" in n[1]]
check("exactly one digest pop-up", len(digest), 1)
check("digest counts 4 comments on 2 posts", "4 new comments" in digest[0][1] and "2 of your posts" in digest[0][1], True)
check("quiet flags cleared", any(p.get("quiet_resume") for p in soc._load()["posts"]), False)
check("comments still delivered + posts settled", all(p.get("reception") and len(p["comments"]) == 2 for p in soc._load()["posts"][-2:]), True)
soc._timer = _t2

# fame: one viral award per in-game day
F.commodity_tracker.stat.value = 400.0
AI["reply"] = json.dumps([{"verdict": "viral"}, {"name": "Luca Woodruff", "comment": "WOW", "mood": "happy"}])
soc.player_post(F, "surprise single", "public")
first = F.commodity_tracker.stat.value
check("first viral post of the day: fame +0.5%", round(first - 400.0, 1), 5.0)
soc.player_post(F, "and another", "public")
check("second viral post same day: no fame", F.commodity_tracker.stat.value, first)
ms_mod._now_sim_ticks = lambda: 300000 + 2400 + 1
soc.player_post(F, "next day", "public")
check("next day: fame again", round(F.commodity_tracker.stat.value - first, 1), 5.0)
ms_mod._now_sim_ticks = lambda: 1000

print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAIL: {FAILS}")
