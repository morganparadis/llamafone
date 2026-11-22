"""
Messages: the player's text threads, read-only.

Builds threads for one household sim from what the mod already keeps:
  - one-on-one texts: Journal.json entries of type "text" (texts either
    side started, replies, Llamadate intros) and "dating_outreach"
    (a match texting first). Each entry is one exchange, not the whole
    conversation, so a thread is the entries stitched in file order.
  - group texts: GroupTexts.json, which holds every round of every group.
    (Old saves also have a few "group_text" journal lines; those are
    skipped -- GroupTexts.json has the full thread.)

Calls aren't included. Nothing here writes anything.

Recency is on the game clock only. Entries saved before in-game time was
recorded have none; they keep their file order and sort after every
thread with a time.
"""
from . import journal, group_texts

_TEXT_TYPES = ("text", "dating_outreach")


def _norm(s):
    return (s or "").strip().lower()


def _first(name):
    return (name or "").strip().split(" ")[0] or (name or "")


def _when(ticks):
    """'Yesterday', '3 days ago'... on the game clock; vague when unknown."""
    if ticks is None:
        return "A while ago"
    label = journal._ago_label({"ticks": ticks}, journal._now_ingame_ticks())
    label = label.replace(" (in-game)", "")
    return label[:1].upper() + label[1:]


def _is_anchor_recipient(e, anchor_id, anchor_names):
    rid = e.get("recipient_id")
    if rid:
        return anchor_id is not None and rid == str(anchor_id)
    return _norm(e.get("recipient")) in anchor_names


def _parse_entry(e, anchor_first, anchor_names):
    """Split one journal entry into [(speaker_first_name, text)].

    Shapes seen in real saves:
      'Text from Olivia Eriksen (to Francesca):\\n<body>'   -> one message
      'Conversation with X:' / 'Text conversation with X:' /
      'Sent Llamadate intro to X:' then 'Name: text' lines, where a line
      that doesn't start with a speaker continues the message above."""
    content = (e.get("content") or "").strip()
    other = e.get("sim") or ""
    first, sep, rest = content.partition("\n")
    if not sep or not first.endswith(":"):
        return [(_first(other), content)] if content else []
    if first.startswith("Text from ") or first.startswith("Group text from "):
        return [(_first(other), rest.strip())] if rest.strip() else []
    speakers = {}
    for n in anchor_names | {_norm(e.get("recipient"))}:
        if n:
            speakers[n] = anchor_first
    if other:
        speakers[_norm(other)] = _first(other)
        speakers.setdefault(_norm(_first(other)), _first(other))
    out = []
    for line in rest.splitlines():
        head, colon, tail = line.partition(":")
        if colon and _norm(head) in speakers:
            out.append([speakers[_norm(head)], tail.strip()])
        elif out:
            if line.strip():
                out[-1][1] = (out[-1][1] + "\n" + line.strip()).strip()
        elif line.strip():
            out.append([_first(other), line.strip()])
    return [(s, t) for s, t in out if t]


def _tidy(text):
    """One message's lines without the blank lines between paragraphs."""
    return "\n".join(l.strip() for l in (text or "").splitlines() if l.strip())


def _anchor_names(anchor_sim):
    first = getattr(anchor_sim, "first_name", "") or ""
    last = getattr(anchor_sim, "last_name", "") or ""
    names = {_norm(first), _norm(f"{first} {last}")}
    names.discard("")
    return first, names


def list_threads(anchor_sim):
    """Every text thread for this household sim, newest first. Each is a
    dict: key, kind ('direct' or 'group'), title, other_id (direct only),
    messages [(when_ticks, speaker, text)] oldest first, last_ticks."""
    anchor_id = getattr(anchor_sim, "sim_id", None)
    anchor_first, anchor_names = _anchor_names(anchor_sim)

    entries = journal._load()
    # Legacy name-only entries join the thread of the sim with that name.
    id_for_name = {}
    for e in entries:
        if e.get("sim_id") and e.get("sim"):
            id_for_name.setdefault(_norm(e["sim"]), e["sim_id"])

    threads = {}
    for idx, e in enumerate(entries):
        if e.get("type") not in _TEXT_TYPES:
            continue
        if not _is_anchor_recipient(e, anchor_id, anchor_names):
            continue
        sid = e.get("sim_id") or id_for_name.get(_norm(e.get("sim")))
        key = "d:" + (sid or "name:" + _norm(e.get("sim")))
        msgs = _parse_entry(e, anchor_first, anchor_names)
        if not msgs:
            continue
        t = threads.setdefault(key, {"key": key, "kind": "direct", "title": e.get("sim") or "?",
                                     "other_id": sid, "messages": [], "last_ticks": None, "order": idx})
        t["title"] = e.get("sim") or t["title"]
        t["order"] = idx
        ticks = e.get("ticks")
        for speaker, text in msgs:
            t["messages"].append((ticks, speaker, _tidy(text)))
        if ticks is not None:
            t["last_ticks"] = ticks

    try:
        groups = group_texts.list_active_groups(anchor_id) if anchor_id is not None else []
    except Exception:
        groups = []
    for gi, g in enumerate(groups):
        msgs = []
        last = None
        for h in g.get("history") or ():
            text = _tidy(h.get("text"))
            if not text:
                continue
            speaker = anchor_first if h.get("role") == "you" else _first(h.get("from_name"))
            msgs.append((h.get("ticks"), speaker, text))
            if h.get("ticks") is not None:
                last = h["ticks"]
        if not msgs:
            continue
        if last is None:
            last = g.get("last_activity_ticks")
        names = [_first(n) for n in g.get("participant_names") or ()]
        key = "g:" + str(g.get("group_id"))
        threads[key] = {"key": key, "kind": "group", "group_id": g.get("group_id"),
                        "title": "Group: " + ", ".join(names), "other_id": None,
                        "messages": msgs, "last_ticks": last,
                        # untimed groups go after untimed direct threads
                        "order": -1 - gi}

    out = list(threads.values())
    out.sort(key=lambda t: (t["last_ticks"] is not None, t["last_ticks"] or 0, t["order"]), reverse=True)
    return out


def direct_thread(anchor_sim, other_sim_id):
    """The one-on-one thread with this sim, or None if they've never texted."""
    key = "d:" + str(other_sim_id)
    for t in list_threads(anchor_sim):
        if t["key"] == key:
            return t
    return None


def preview(thread, limit=90):
    """'Olivia: see you at 7!' for the thread list row."""
    if not thread["messages"]:
        return ""
    _ticks, speaker, text = thread["messages"][-1]
    line = " ".join(text.split())
    if len(line) > limit:
        line = line[:limit - 1].rstrip() + "…"
    return f"{speaker}: {line}"


def last_when(thread):
    return _when(thread["last_ticks"])


PAGE_MESSAGES = 12
PAGE_CHARS = 1600


def pages(thread):
    """Split the thread into pages, newest page first. Each page is a
    list of messages in reading order (oldest at top). A page holds up to
    PAGE_MESSAGES messages or about PAGE_CHARS characters."""
    msgs = thread["messages"]
    out, cur, size = [], [], 0
    for m in reversed(msgs):
        n = len(m[2]) + len(m[1]) + 2
        if cur and (len(cur) >= PAGE_MESSAGES or size + n > PAGE_CHARS):
            out.append(list(reversed(cur)))
            cur, size = [], 0
        cur.append(m)
        size += n
    if cur:
        out.append(list(reversed(cur)))
    return out


def _esc(text):
    """Message text is shown as the dialog's markup, so a sim writing
    '<3' or 'A&W' must not be read as a tag."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(page_msgs, me):
    """One page as a phone-style chat, in the markup the game's dialog
    renders (checked in game: bold, font size, <p align>, <br> work;
    font color and <div> don't). `me` is the player's sim's first name:
    their messages are right-aligned, everyone else's left. A small
    centered time line starts each new day, and the name goes on top of
    a message only when the speaker changes:

        <p align="center"><font size="11">Yesterday</font></p>
        <p align="left"><b>Luca</b><br>you up?</p>
        <p align="right"><b>Francesca</b><br>barely</p>"""
    out, last_when, last_speaker = [], object(), None
    for ticks, speaker, text in page_msgs:
        when = _when(ticks)
        if when != last_when:
            gap = "<br>" if out else ""
            out.append(f'<p align="center">{gap}<font size="11">{_esc(when)}</font></p>')
            last_when, last_speaker = when, None
        side = "right" if speaker == me else "left"
        body = "<br>".join(_esc(l) for l in text.splitlines())
        if speaker != last_speaker:
            # New speaker: a blank line, then their name (none right
            # under a day line, which already sets this block apart).
            gap = "<br>" if last_speaker is not None else ""
            body = f"{gap}<b>{_esc(speaker)}</b><br>{body}"
        else:
            # Same speaker again: a small gap so separate texts don't
            # read as one (checked in game: they ran together).
            body = f'<font size="7"> </font><br>{body}'
        out.append(f'<p align="{side}">{body}</p>')
        last_speaker = speaker
    return "".join(out)
