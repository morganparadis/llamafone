import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
"""Llamafone > Messages and Relationships panel > Llamafone > Messages:
text threads (one-on-one from the journal, groups from GroupTexts.json)
for the active sim only, newest first on the game clock, each opened as a
chat with the oldest line at the top. Calls, posts, and other household
sims' texts stay out. Read-only: nothing is written, and there is no
delete anywhere. Real messages.py and phone_ui_interactions.py run; the
game's dialogs and the save files are stand-ins."""
import sys, types, os, re

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


SHOWN = []


class Row:
    def __init__(self, option_id, name, row_description="", is_enable=True, **k):
        self.option_id, self.name, self.desc = option_id, name, row_description


class Dialog:
    def __init__(self, sim, title, text, text_ok, text_cancel):
        self.title, self.text, self.ok, self.cancel = title(), text(), text_ok(), text_cancel()
        self.rows, self.listeners = [], []
    def add_row(self, r): self.rows.append(r)
    def add_listener(self, f): self.listeners.append(f)
    def show_dialog(self, **k): SHOWN.append(self)
    @classmethod
    def TunableFactory(cls):
        return types.SimpleNamespace(default=lambda *a, **k: cls(*a, **k))


def last(): return SHOWN[-1]


def press(d, ok, picked=None):
    for f in d.listeners:
        f(types.SimpleNamespace(accepted=ok, picked_results=picked or []))


def pick(d, name):
    row = [r for r in d.rows if r.name == name][0]
    press(d, True, [row.option_id])


mod("sims4"); mod("sims4.localization", LocalizationHelperTuning=LHT); mod("sims4.commands"); mod("sims4.log")
mod("services", sim_info_manager=lambda: types.SimpleNamespace(get=lambda i: SIMS.get(i)))
mod("interactions"); mod("interactions.base")


class _BaseSI:
    def __init__(self, sim, interaction_parameters=None):
        self.sim, self.interaction_parameters = sim, interaction_parameters or {}
    @classmethod
    def _test(cls, target, context, **kwargs): return True
    def _run_interaction_gen(self, timeline):
        if False:
            yield
        return True


mod("interactions.base.super_interaction", SuperInteraction=_BaseSI)
mod("ui"); mod("ui.ui_dialog_picker", UiItemPicker=Dialog, BasePickerRow=Row)
mod("ui.ui_dialog", UiDialogOkCancel=Dialog)
mod("ui.ui_dialog_generic", UiDialogTextInputOkCancel=Dialog)
mod("distributor"); mod("distributor.shared_messages", IconInfoData=lambda **k: None)
mod("event_testing"); mod("event_testing.results", TestResult=lambda ok, reason="": ok)


class SI:
    def __init__(self, sid, first, last):
        self.sim_id, self.first_name, self.last_name = sid, first, last
        self.sim_info = self


FRAN = SI(1, "Francesca", "Hansen")
AKSEL = SI(2, "Aksel", "Hansen")          # same household, plays too
OLIVIA = SI(3, "Olivia", "Eriksen")
LUCA = SI(4, "Luca", "Woodruff")
DAWN = SI(5, "Dawn", "Woods")
STRANGER = SI(6, "Bea", "Nobody")
SIMS = {s.sim_id: s for s in (FRAN, AKSEL, OLIVIA, LUCA, DAWN, STRANGER)}

DAY = 1500 * 60 * 24
NOW = 100 * DAY


def E(type_, sim, sid, content, ticks, recipient="Francesca", rid="1"):
    e = {"timestamp": "2026-01-01T00:00:00", "type": type_, "content": content, "sim": sim, "recipient": recipient}
    if ticks is not None:
        e["ticks"] = ticks
    if sid is not None:
        e["sim_id"] = sid
    if rid is not None:
        e["recipient_id"] = rid
    return e


# Shapes copied from a real save's Journal.json, in file (time) order.
JOURNAL = [
    E("text", "Dawn Woods", None, "Text from Dawn Woods (to Francesca):\nhey, coffee sometime?", None, rid=None),
    E("text", "Olivia Eriksen", "3", "Text conversation with Olivia Eriksen:\nFrancesca: drinks later?\n"
      "Olivia Eriksen: Yes!\n\nWhat time works?", None),
    E("group_text", "Luca Woodruff", "4", "Group text from Luca Woodruff (to Francesca, group grp_1):\nhey all", None),
    E("call", "Olivia Eriksen", "3", "Call with Olivia Eriksen:\nFrancesca: call line\nOlivia Eriksen: on the phone", 90 * DAY),
    E("text", "Olivia Eriksen", "3", "Text from Olivia Eriksen (to Francesca):\nyou up?", 95 * DAY - 20),
    E("text", "Olivia Eriksen", "3", "Conversation with Olivia Eriksen:\nFrancesca: barely\nOlivia Eriksen: same lol", 95 * DAY - 10),
    E("post_comment", "Luca Woodruff", "4", "Luca Woodruff commented on Francesca Hansen's post (\"x\"):\nnice", 96 * DAY),
    E("text", "Olivia Eriksen", "3", "Text from Olivia Eriksen (to Aksel):\nfound my whistle?", 97 * DAY,
      recipient="Aksel", rid="2"),
    E("dating_outreach", "Luca Woodruff", "4", "Text from Luca Woodruff (to Francesca):\nsaw you on llamadate", 98 * DAY),
    E("text", "Dawn Woods", "5", "Text conversation with Dawn Woods:\nFrancesca: sure!\nDawn Woods: great", 99 * DAY),
]
GROUPS = [
    {"group_id": "grp_1", "anchor_sim_id": 1, "participant_names": ["Luca Woodruff", "Martha Woodruff"],
     "last_activity_ticks": 96 * DAY, "history": [
         {"role": "you", "text": "hey all", "ticks": 96 * DAY},
         {"role": "them", "from_id": 4, "from_name": "Luca Woodruff", "text": "hi!", "ticks": 96 * DAY},
         {"role": "them", "from_id": 7, "from_name": "Martha Woodruff", "text": "hello\n\nlove you", "ticks": 96 * DAY}]},
    {"group_id": "grp_old", "anchor_sim_id": 1, "participant_names": ["Apollo Woodruff", "Vivian Woodruff"],
     "history": [{"role": "you", "text": "old group", "ts": "2026-07-12"}]},
    {"group_id": "grp_aksel", "anchor_sim_id": 2, "participant_names": ["Luca Woodruff"],
     "last_activity_ticks": 99 * DAY, "history": [{"role": "you", "text": "aksel's group", "ticks": 99 * DAY}]},
]

pkg = types.ModuleType("llamafone"); pkg.__path__ = [REPO + "/src/llamafone"]; sys.modules["llamafone"] = pkg
WRITES = []


def ago(e, now):
    t = e.get("ticks")
    if t is None:
        return "a while ago"
    d = int((now - t) // DAY)
    return "today (in-game)" if d == 0 else ("yesterday (in-game)" if d == 1 else f"{d} days ago (in-game)")


journal = mod("llamafone.journal", _load=lambda: JOURNAL, _ago_label=ago, _now_ingame_ticks=lambda: NOW,
              add_entry=lambda *a, **k: WRITES.append(a), _save=lambda *a: WRITES.append(a))
gt = mod("llamafone.group_texts",
         list_active_groups=lambda aid=None: [g for g in GROUPS if aid is None or g["anchor_sim_id"] == int(aid)],
         _save=lambda *a: WRITES.append(a))
pkg.journal, pkg.group_texts = journal, gt
NOTES = []
pkg.notifications = mod("llamafone.notifications", show=lambda t, m, output=None: NOTES.append(m),
                        show_error=lambda m, *a, **k: NOTES.append("ERROR: " + m))
phone = mod("llamafone.phone", _is_phone_eligible=lambda si: True, _is_ghost=lambda si: False)
pkg.phone = phone
for n in ("sim_context", "dating"):
    setattr(pkg, n, mod("llamafone." + n))

import llamafone.messages as M
import llamafone.phone_ui_interactions as ui
ui._log = ui._log_exc = lambda *a, **k: None

# ---- threads -----------------------------------------------------------------
ts = M.list_threads(FRAN)
titles = [t["title"] for t in ts]
print("  threads:", titles)
chk("newest first on the game clock, untimed threads last",
    titles == ["Dawn Woods", "Luca Woodruff", "Group: Luca, Martha", "Olivia Eriksen", "Group: Apollo, Vivian"])
chk("only Francesca's threads (Aksel's text and group stay out)",
    all("whistle" not in m[2] and "aksel's group" not in m[2] for t in ts for m in t["messages"]))
olivia = [t for t in ts if t["title"] == "Olivia Eriksen"][0]
chk("Olivia thread: texts only, oldest first, no call lines",
    [(m[1], m[2]) for m in olivia["messages"]] == [
        ("Francesca", "drinks later?"), ("Olivia", "Yes!\nWhat time works?"),
        ("Olivia", "you up?"), ("Francesca", "barely"), ("Olivia", "same lol")])
dawn = [t for t in ts if t["title"] == "Dawn Woods"][0]
chk("legacy name-only text joins the sim's thread", [m[2] for m in dawn["messages"]] == ["hey, coffee sometime?", "sure!", "great"])
luca = [t for t in ts if t["title"] == "Luca Woodruff"][0]
chk("a Llamadate match's first text counts; comments and old group lines don't",
    [m[2] for m in luca["messages"]] == ["saw you on llamadate"])
grp = [t for t in ts if t["title"] == "Group: Luca, Martha"][0]
chk("group thread from GroupTexts.json, speakers by first name",
    [(m[1], m[2]) for m in grp["messages"]] == [("Francesca", "hey all"), ("Luca", "hi!"), ("Martha", "hello\nlove you")])
chk("row text: when + last line", M.last_when(dawn) == "Yesterday" and M.preview(dawn) == "Dawn: great")
chk("untimed thread says 'A while ago'", M.last_when(ts[-1]) == "A while ago")
chk("Aksel sees his own group", [t["title"] for t in M.list_threads(AKSEL)] == ["Group: Luca", "Olivia Eriksen"])

page = M.render(M.pages(olivia)[0], "Francesca")
print("  ---\n" + page.replace("</p>", "</p>\n") + "  ---")
DAYLINE = '<p align="center"><font size="11">{}</font></p>'
chk("chat markup: day lines centered, mine right, theirs left, bold name on top, oldest first",
    page == DAYLINE.format("A while ago")
    + '<p align="right"><b>Francesca</b><br>drinks later?</p>'
    + '<p align="left"><br><b>Olivia</b><br>Yes!<br>What time works?</p>'
    + '<p align="center"><br><font size="11">5 days ago</font></p>'
    + '<p align="left"><b>Olivia</b><br>you up?</p>'
    + '<p align="right"><br><b>Francesca</b><br>barely</p>'
    + '<p align="left"><br><b>Olivia</b><br>same lol</p>')
two = M.render([(DAY, "Olivia", "one"), (DAY, "Olivia", "two"), (DAY, "Francesca", "three")], "Francesca")
chk("same speaker twice in a row: name once, small gap between the texts",
    two.count("<b>Olivia</b>") == 1 and '<p align="left"><font size="7"> </font><br>two</p>' in two
    and '<p align="right"><br><b>Francesca</b><br>three</p>' in two)
esc = M.render([(DAY, "Olivia", "love you <3 & A&W <b>")], "Francesca")
chk("message text can't turn into markup", "love you &lt;3 &amp; A&amp;W &lt;b&gt;" in esc and esc.count("<b>") == 1)
grp_page = M.render(M.pages(grp)[0], "Francesca")
chk("group chat: every other sim on the left with their name",
    '<p align="left"><br><b>Luca</b><br>hi!</p>' in grp_page
    and '<p align="left"><br><b>Martha</b><br>hello<br>love you</p>' in grp_page
    and '<p align="right"><b>Francesca</b><br>hey all</p>' in grp_page)
long_t = {"messages": [(i * DAY, "Olivia", f"msg {i}") for i in range(45)]}
ps = M.pages(long_t)
chk("long thread: pages of 12, newest page first", [len(p) for p in ps] == [12, 12, 12, 9]
    and ps[0][-1][2] == "msg 44" and ps[0][0][2] == "msg 33" and ps[3][0][2] == "msg 0")
big = {"messages": [(DAY, "Olivia", "x" * 1000)] * 5}
chk("very long messages split by size too", all(sum(len(m[2]) for m in p) <= M.PAGE_CHARS for p in M.pages(big)))
chk("preview is one line, trimmed", M.preview({"messages": [(0, "Olivia", "a\nb " + "z" * 200)]}).startswith("Olivia: a b z")
    and len(M.preview({"messages": [(0, "Olivia", "z" * 200)]})) <= 100)

# ---- the phone tile ------------------------------------------------------------
SHOWN.clear()
ui._mod_ready = lambda: False          # not set up yet: reading still works
for _ in ui.LlamafoneMessagesInteraction(FRAN)._run_interaction_gen(None):
    pass
lst = last()
chk("Messages tile opens before setup", lst.title == "Messages")
chk("thread rows newest first with when + preview",
    [r.name for r in lst.rows] == titles and lst.rows[0].desc == "Yesterday. Dawn: great")
chk("buttons: Open / Close", (lst.ok, lst.cancel) == ("Open", "Close"))
pick(lst, "Olivia Eriksen")
th = last()
chk("opens the chat", th.title == "Olivia Eriksen" and th.text == page)
chk("short thread: Back / Close, no delete", (th.ok, th.cancel) == ("Back", "Close")
    and "Delete" not in (th.ok, th.cancel))
press(th, True)
chk("Back returns to the thread list", last().title == "Messages")
n = len(SHOWN); press(last(), False)
chk("Close on the list closes", len(SHOWN) == n)

# Long thread paging inside the dialog.
JOURNAL.append(E("text", "Olivia Eriksen", "3", "Conversation with Olivia Eriksen:\n" +
                 "\n".join(f"Olivia Eriksen: line {i}" for i in range(30)), NOW))
SHOWN.clear()
ui._show_threads(FRAN)
pick(last(), "Olivia Eriksen")
p1 = last()
chk("long thread: newest page first, 'Earlier messages' / Back",
    p1.title == "Olivia Eriksen (1/3)" and (p1.ok, p1.cancel) == ("Earlier messages", "Back")
    and p1.text.endswith("line 29</p>"))
press(p1, True)
p2 = last()
chk("Earlier messages: middle page", p2.title == "Olivia Eriksen (2/3)"
    and (p2.ok, p2.cancel) == ("Earlier messages", "Back"))
press(p2, True)
p3 = last()
chk("oldest page has the start of the thread", p3.title == "Olivia Eriksen (3/3)" and "drinks later?" in p3.text
    and (p3.ok, p3.cancel) == ("Back", "Close"))
press(p3, True)
chk("Back from an older page goes to the newer page", last().title == "Olivia Eriksen (2/3)")
press(last(), False)
press(last(), False)
chk("Back from the newest page goes to the list", last().title == "Messages")
JOURNAL.pop()

# Many threads -> pages of 25.
for i in range(30):
    JOURNAL.append(E("text", f"Extra Sim{i}", str(1000 + i), f"Text from Extra Sim{i} (to Francesca):\nhi {i}", 50 * DAY + i))
SHOWN.clear()
ui._show_threads(FRAN)
chk("25 threads + 'Older threads...' row", len(last().rows) == 26 and last().rows[-1].name == "Older threads..."
    and last().rows[-1].desc == "10 more")
pick(last(), "Older threads...")
chk("older page has the rest", last().title == "Messages (page 2)" and len(last().rows) == 10
    and last().cancel == "Back")
press(last(), False)
chk("Back from page 2 goes to page 1", last().title == "Messages")
del JOURNAL[-30:]

# No threads at all.
SHOWN.clear(); NOTES.clear()
ui._show_threads(STRANGER)
chk("no texts yet: a note, no empty window", not SHOWN and NOTES and NOTES[0].startswith("No texts yet."))

# ---- Relationships panel > Llamafone > Messages --------------------------------
def rel(sid):
    SHOWN.clear(); NOTES.clear()
    for _ in ui.LlamafoneRelMessagesInteraction(FRAN, {"picked_item_ids": frozenset([sid])})._run_interaction_gen(None):
        pass


rel(3)
chk("pie menu opens the thread with the clicked sim", SHOWN and last().title == "Olivia Eriksen" and last().text == page)
press(last(), True)
chk("Back from a pie-menu thread just closes", len(SHOWN) == 1)
rel(6)
chk("never texted: a note", not SHOWN and NOTES == ["No texts with Bea yet."])
rel(99)
chk("vanished sim: friendly error", not SHOWN and NOTES == ["ERROR: Couldn't tell which sim you picked. Try again."])
chk("pie-menu Messages hidden where Call/Text are (same test)",
    ui.LlamafoneRelMessagesInteraction._test.__func__ is ui.LlamafoneRelTextInteraction._test.__func__)

# ---- read-only, no delete --------------------------------------------------------
chk("nothing was written", WRITES == [])
usrc = open(os.path.join(REPO, "src", "llamafone", "phone_ui_interactions.py"), encoding="utf-8").read()
chk("no delete or Message history row left in the UI",
    "delete_entry" not in usrc and "Message history" not in usrc and "_confirm_history_delete" not in usrc)
jsrc = open(os.path.join(REPO, "src", "llamafone", "journal.py"), encoding="utf-8").read()
chk("journal has no delete-entry code", "def delete_entry" not in jsrc and "Journal_Deleted" not in jsrc)

# ---- package + injection -----------------------------------------------------------
import llamafone.phone_ui_injection as inj
chk("Messages tile injected right after Text", list(inj._PHONE_INTERACTION_NAMES[:3]) ==
    ["Llamafone_Call", "Llamafone_Text", "Llamafone_Messages"])
chk("Relationships panel gets Messages", "Llamafone_RelMessages" in inj._RELATION_PANEL_INTERACTION_NAMES)
pb = open(os.path.join(REPO, "tools", "package_builder.py"), encoding="utf-8").read()
chk("'Messages' string in the table", re.search(r'0x00CA2400:\s*"Messages"', pb) is not None)
for xml, cls in (("Llamafone_Messages.xml", "LlamafoneMessagesInteraction"),
                 ("Llamafone_RelMessages.xml", "LlamafoneRelMessagesInteraction")):
    x = open(os.path.join(REPO, "package_src", xml), encoding="utf-8").read()
    chk(f"{xml}: class {cls}, 'Messages' label, Llamafone category",
        f'c="{cls}"' in x and hasattr(ui, cls) and '<T n="display_name">0x00CA2400</T>' in x
        and '<T n="category">13371000010</T>' in x)

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
