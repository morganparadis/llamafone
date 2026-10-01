"""
Social posts (v3.8).

Three flows, all with REAL sims from the save (never invented accounts):

  1. Player posts   Llamafone > Post: the player writes a post and picks
                    Friends or Public. After a delay, ONE AI call sees a
                    candidate list (the poster's network; for Public also
                    a few real sims they've never met) and decides who
                    comments -- often a few, sometimes nobody. Comments
                    arrive in batches, each batch one notification.

  2. NPC posts      An auto-event type ("post", injected at pick time like
                    Llamadate). A friend posts about their own life; the
                    household sim sees it with Comment / Scroll past.

  3. Threads        Commenting on a friend's post, or replying to a
                    comment on yours, gets that sim's answer back.

Everything lands in Llamafone > Notifications (an inbox), and in the
per-save Feed.json (hand-editable, like Journal.json):

  {"v": 1,
   "posts": [{"id", "author_id", "author_name", "is_player", "audience",
              "text", "ts", "ticks", "viewer_id",
              "comments": [{"id", "from_id", "from_name", "to_id", "text",
                            "ts", "ticks", "mood"}]}],
   "inbox": [{"id", "kind": "comment"|"npc_post"|"reply", "post_id",
              "comment_id", "household_id", "ts", "read"}]}

Comments are stored when they are DELIVERED, not when generated, so the
feed, the inbox, and later prompts only know what the player has seen.
Each comment is journaled for its pair so later texts and calls know
"you commented on my post". Reply moods move relationships through
relationship_impact, same as texts.
"""
import datetime
import json
import os
import random
import threading
import uuid

from . import config, sim_context
from . import save_id as _save_id

_FILENAME = "Feed.json"
_SCHEMA_VERSION = 1
_MAX_POSTS = 200
_MAX_INBOX = 200

_FRIENDS_MIN_FRIENDSHIP = 10     # network threshold for who sees a post
_MAX_FRIEND_CANDIDATES = 8
_MAX_STRANGER_CANDIDATES = 4
_FIRST_BATCH_DELAY = (60, 180)   # seconds after posting before comments start
_NEXT_BATCH_GAP = (120, 360)     # seconds between comment batches
_THREAD_REPLY_DELAY = (30, 120)  # seconds for a reply to your comment
_MAX_POST_CHARS = 400
_MAX_COMMENT_CHARS = 280

_VALID_MOODS = {
    "happy", "sad", "angry", "confident", "flirty", "playful", "energized",
    "focused", "inspired", "embarrassed", "tense", "uncomfortable", "bored",
    "dazed",
}

_lock = threading.RLock()
_cache = None
_cached_for = None


def _log(msg):
    try:
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [social] {msg}\n")
    except Exception:
        pass


def enabled():
    try:
        return bool(config.get_social_enabled())
    except Exception:
        return True


def _post_popups():
    try:
        return bool(config.get_social_post_popups())
    except Exception:
        return True


def _comment_popups():
    try:
        return bool(config.get_social_comment_popups())
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _empty():
    return {"v": _SCHEMA_VERSION, "posts": [], "inbox": [], "profiles": {}}


def _load():
    """The current save's feed, or None when no save is identified (the
    mod is dormant -- nothing is read or written)."""
    global _cache, _cached_for
    with _lock:
        sid = _save_id.get_current_save_id()
        if sid is None:
            return None
        if _cache is not None and _cached_for == sid:
            return _cache
        data = _empty()
        path = _save_id.data_path(_FILENAME)
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("top level is not an object")
                data["posts"] = [p for p in (raw.get("posts") or []) if isinstance(p, dict)]
                data["inbox"] = [i for i in (raw.get("inbox") or []) if isinstance(i, dict)]
                data["profiles"] = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
            except Exception as e:
                # Never overwrite a feed we couldn't read (e.g. a hand edit
                # broke the JSON): set it aside and start fresh.
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = f"{path}.corrupt-{stamp}.bak"
                try:
                    os.replace(path, bak)
                except Exception:
                    bak = "(could not move it)"
                _log(f"could not parse feed ({type(e).__name__}: {e}); preserved as {bak}")
                data = _empty()
        _cache = data
        _cached_for = sid
        return data


def _save(data):
    with _lock:
        sid = _save_id.get_current_save_id()
        if sid is None or sid != _cached_for:
            _log(f"save changed ({_cached_for!r} -> {sid!r}); not writing feed")
            return
        path = _save_id.data_path(_FILENAME)
        if not path:
            return
        for _prof in (data.get("profiles") or {}).values():
            _prof.pop("_dirty", None)
        data["posts"] = data["posts"][-_MAX_POSTS:]
        data["inbox"] = data["inbox"][-_MAX_INBOX:]
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:
            _log(f"feed save failed: {type(e).__name__}: {e}")
            try:
                os.remove(tmp)
            except Exception:
                pass


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _now_iso():
    return datetime.datetime.now().isoformat()


def _now_ticks():
    try:
        from . import milestones
        return milestones._now_sim_ticks()
    except Exception:
        return None


def get_post(post_id):
    data = _load()
    if not data:
        return None
    for p in data["posts"]:
        if p.get("id") == post_id:
            return p
    return None


def _add_post(author_si, text, audience, is_player, viewer_id=None):
    with _lock:
        data = _load()
        if data is None:
            return None
        post = {
            "id": _new_id("post"),
            "author_id": str(author_si.sim_id),
            "author_name": _name(author_si),
            "is_player": bool(is_player),
            "audience": audience,
            "text": text,
            "ts": _now_iso(),
            "ticks": _now_ticks(),
            "viewer_id": str(viewer_id) if viewer_id is not None else None,
            "comments": [],
        }
        data["posts"].append(post)
        _save(data)
        return post


def _add_comment(post_id, from_si, text, to_id=None, mood=None):
    with _lock:
        data = _load()
        post = get_post(post_id) if data else None
        if post is None:
            return None
        c = {
            "id": _new_id("c"),
            "from_id": None if _is_fan(from_si) else str(from_si.sim_id),
            "from_name": _name(from_si),
            "to_id": str(to_id) if to_id is not None else None,
            "text": text,
            "ts": _now_iso(),
            "ticks": _now_ticks(),
            "mood": mood,
        }
        post.setdefault("comments", []).append(c)
        _save(data)
        return c


def _add_inbox(kind, post_id, comment_id, household_id):
    with _lock:
        data = _load()
        if data is None:
            return None
        item = {
            "id": _new_id("n"),
            "kind": kind,
            "post_id": post_id,
            "comment_id": comment_id,
            "household_id": str(household_id),
            "ts": _now_iso(),
            "read": False,
        }
        data["inbox"].append(item)
        _save(data)
        return item


def _find_comment(post, comment_id):
    for c in post.get("comments") or []:
        if c.get("id") == comment_id:
            return c
    return None


def unread_count(household_ids=None):
    data = _load()
    if not data:
        return 0
    ids = {str(i) for i in household_ids} if household_ids else None
    return sum(1 for i in data["inbox"]
               if not i.get("read") and (ids is None or i.get("household_id") in ids))


# ---------------------------------------------------------------------------
# Sims
# ---------------------------------------------------------------------------

class _Fan(object):
    """A made-up-username follower who comments on a big account's post.
    Display only: no sim, no relationship, no journal, can't be replied to.
    Everyone who matters in the story is still a real save sim."""
    sim_id = None

    def __init__(self, handle):
        h = str(handle or "").strip()
        self.handle = h if h.startswith("@") else "@" + h
        self.first_name = self.handle
        self.last_name = ""


def _is_fan(si):
    return getattr(si, "handle", None) is not None


def _name(si):
    try:
        return f"{si.first_name} {si.last_name}".strip()
    except Exception:
        return "Someone"


def _first(si):
    try:
        return si.first_name or _name(si)
    except Exception:
        return _name(si)


def _resolve(sim_id):
    try:
        import services
        return services.sim_info_manager().get(int(sim_id))
    except Exception:
        return None


def _age(si):
    try:
        return str(getattr(si, "age", "")).replace("Age.", "").lower().replace("youngadult", "young adult")
    except Exception:
        return ""


def _can_post(si):
    """Teen+ human, not a ghost. Social media is a teen-and-up thing; the
    same rule decides who can comment."""
    try:
        from . import phone
        return phone._is_phone_eligible(si) and not phone._is_ghost(si)
    except Exception:
        return False


def _household_ids():
    try:
        import services
        hh = services.active_household()
        return {str(si.sim_id) for si in hh.sim_info_gen()} if hh else set()
    except Exception:
        return set()


def _children_ids(si):
    try:
        return {int(c) for c in si.genealogy.get_children_sim_ids_gen()}
    except Exception:
        return set()


def _kid_desc(child_id):
    c = _resolve(child_id)
    return f"{_name(c)} ({_age(c)})" if c is not None else None


def _shared_children(a_si, b_si):
    """Children a and b have together (genealogy), as 'Name (age)'."""
    both = _children_ids(a_si) & _children_ids(b_si)
    return [d for d in (_kid_desc(c) for c in sorted(both)) if d]


def _relation_text(entry, poster_si):
    """How `entry`'s sim relates to the poster, in plain words."""
    from . import phone
    si = entry.get("sim_info")
    pf = _first(poster_si)
    bits = []
    # Co-parents first: not family and maybe not together, so the labels
    # below miss it -- Ingrid commented "no way" on Ben's baby post, and
    # it's her baby too.
    shared = _shared_children(si, poster_si)
    if shared:
        bits.append(f"the other parent of {pf}'s {'child' if len(shared) == 1 else 'children'} "
                    f"{', '.join(shared)} -- this is their kid too")
    # The poster's young kids: has this sim met them in person? A
    # relationship in the game's tracker means yes (Francesca's dad had
    # held Miley, then commented "can't wait to meet Miley").
    try:
        for cid in sorted(_children_ids(poster_si) - _children_ids(si)):
            kid = _resolve(cid)
            if kid is None or _age(kid) not in ("baby", "infant", "toddler"):
                continue
            bits.append(f"has met {_first(kid)} in person" if sim_context.has_met_in_person(si, cid)
                        else f"has NOT met {_first(kid)} in person yet")
    except Exception:
        pass
    try:
        fam = phone._get_family_relationship(si, entry, recipient=poster_si)
    except Exception:
        fam = None
    if fam:
        bits.append(f"{pf}'s {fam}")
    if entry.get("in_household"):
        bits.append(f"lives with {pf}")
    try:
        fl = phone._friendship_label(entry.get("friendship"))
    except Exception:
        fl = None
    if fl:
        bits.append(fl)
    try:
        rl = phone._romance_label(entry.get("romance"))
    except Exception:
        rl = None
    if rl:
        bits.append(rl)
    status = str(entry.get("status") or "").strip()
    if status and not fam:
        bits.append(status)
    return "; ".join(bits) or f"knows {pf}"


def _sim_brief(si, with_kids=False):
    parts = [_age(si)]
    if with_kids:
        kids = [d for d in (_kid_desc(c) for c in sorted(_children_ids(si))) if d]
        if kids:
            parts.append("children: " + ", ".join(kids))
    try:
        traits = sim_context.get_sim_traits(si, limit=4) or []
        if traits:
            parts.append("traits: " + ", ".join(str(t) for t in traits))
    except Exception:
        pass
    try:
        career = sim_context.get_sim_career(si)
        if career:
            parts.append(f"career: {career}")
    except Exception:
        pass
    try:
        mood = sim_context.get_sim_mood(si)
        if mood:
            parts.append(f"mood: {mood}")
    except Exception:
        pass
    return "; ".join(p for p in parts if p)


def _partner_line(si):
    try:
        from . import phone
        partner, status = phone._get_romantic_partner_info(si)
        if partner is not None and status:
            return f"POSTER'S PARTNER: {_first(si)} is {status} {_name(partner)}"
    except Exception:
        pass
    return None


def _poster_life_block(si):
    """The poster's recent milestones (baby, wedding, new job...), the same
    block the phone prompts use. The poster knows all of it; the rules
    decide which commenters would."""
    try:
        from . import milestones
        block = milestones.format_for_prompt(si, contact_id=None, mark_seen=False, known_by_default=True)
        lines = [l for l in (block or "").splitlines() if l.strip() and not l.startswith("Recent in their life")]
        return "\n".join(lines[:12])
    except Exception:
        return ""


def _pair_history(cand_si, poster_si, n=3):
    """The last few journal entries between a candidate and the poster
    (texts, calls, earlier comments), one line each. Journal pairs are
    stored with the non-household sim as `sim`, so try both directions."""
    try:
        from . import journal
        entries = journal.get_sim_history(_name(cand_si), n, sim_id=cand_si.sim_id, recipient_id=poster_si.sim_id)
        if not entries:
            entries = journal.get_sim_history(_name(poster_si), n, sim_id=poster_si.sim_id,
                                              recipient_id=cand_si.sim_id)
        out = []
        for e in entries[-n:]:
            kind = str(e.get("type") or "note").replace("_", " ")
            out.append(f"[{kind}] {_snippet(e.get('content'), 140)}")
        return out
    except Exception:
        return []


def _network(poster_si):
    try:
        hh, rels = sim_context.get_sim_network(poster_si, min_friendship=_FRIENDS_MIN_FRIENDSHIP)
        return list(hh) + list(rels)
    except Exception:
        return []


def _network_ids(poster_si):
    """The poster, their household, and their network (friendship >= 10 or
    a named relationship). Anyone else is a stranger -- including a sim
    whose only tie is the +1 friendship a happy comment created. Keying
    this on every relationship made named fans vanish after one comment."""
    ids = {str(poster_si.sim_id)} | _household_ids()
    for e in _network(poster_si):
        if e.get("sim_id") is not None:
            ids.add(str(e["sim_id"]))
    return ids


def _hidden_sim(si):
    """Service NPCs, the Grim Reaper, Father Winter, and other special sims
    live in hidden households; they don't have Llamagram accounts."""
    try:
        hh = getattr(si, "household", None)
        return bool(hh is not None and getattr(hh, "hidden", False))
    except Exception:
        return False


def _audience_candidates(poster_si, audience):
    """[(sim_info, relation_text, is_stranger)] -- who might see and react
    to this post. Friends: a closeness-weighted sample of the network.
    Public: that, plus a few real sims in the save the poster has never
    met."""
    from . import contact_prefs
    poster_id = poster_si.sim_id
    pool = []
    for e in _network(poster_si):
        si = e.get("sim_info")
        if si is None or si.sim_id == poster_id or not _can_post(si):
            continue
        try:
            if contact_prefs.is_muted(poster_id, si.sim_id):
                continue
        except Exception:
            pass
        w = 60.0 if e.get("in_household") else max(abs(e.get("friendship") or 0) + abs(e.get("romance") or 0), 10.0)
        try:
            w *= contact_prefs.auto_event_multiplier(poster_id, si.sim_id)  # paused 0.2, favorite 2.0
        except Exception:
            pass
        if w <= 0:
            continue
        pool.append((w, e))
    chosen = []
    # A celebrity's public post is mostly fans and strangers; only the
    # closest few of their own people are in the running.
    big_public = audience == "public" and followers_of(poster_si) >= _FAN_ACCOUNT_MIN
    friend_cap = 4 if big_public else _MAX_FRIEND_CANDIDATES
    while pool and len(chosen) < friend_cap:
        total = sum(w for w, _ in pool)
        r = random.uniform(0, total)
        acc = 0.0
        for i, (w, e) in enumerate(pool):
            acc += w
            if acc >= r:
                chosen.append(pool.pop(i)[1])
                break
    out = [(e["sim_info"], _relation_text(e, poster_si), False) for e in chosen]

    if audience == "public":
        known = _network_ids(poster_si)
        strangers = []
        try:
            import services
            sm = services.sim_info_manager()
            everyone = list(sm.values()) if hasattr(sm, "values") else list(getattr(sm, "objects", []))
            for si in everyone:
                if str(getattr(si, "sim_id", None)) in known or not _can_post(si) or _hidden_sim(si):
                    continue
                strangers.append(si)
        except Exception as e:
            _log(f"stranger pool failed: {type(e).__name__}: {e}")
        random.shuffle(strangers)
        slots = _stranger_slots(poster_si)
        big = followers_of(poster_si) >= _FAN_ACCOUNT_MIN
        # Named fans (real sims who reacted well before) get first pick.
        fan_ids = set()
        try:
            data = _load()
            prof = (data.get("profiles") or {}).get(str(poster_id)) if data else None
            fan_ids = {int(i) for i in (prof or {}).get("fans") or []}
        except Exception:
            fan_ids = set()
        strangers.sort(key=lambda si: 0 if si.sim_id in fan_ids else 1)
        pf = _first(poster_si)
        for si in strangers[:slots]:
            if si.sim_id in fan_ids:
                rel = f"a fan who follows {pf} and has commented before -- has never met them"
            elif big:
                rel = f"a fan who follows {pf} -- has never met them"
            else:
                rel = f"a stranger -- has never met {pf}"
            out.append((si, rel, True))
    return out


def _contact_entry(viewer_si, target_si):
    """A phone-style contact dict for target, relative to viewer, so the
    phone prompt blocks can describe them."""
    tid = getattr(target_si, "sim_id", None)
    try:
        hh, rels = sim_context.get_sim_network(viewer_si, min_friendship=0)
        for e in list(hh) + list(rels):
            if e.get("sim_id") == tid:
                return e
    except Exception:
        pass
    return {"sim_info": target_si, "sim_id": tid, "name": _name(target_si),
            "status": "", "friendship": None, "romance": None, "in_household": False}


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------

def _timer(delay, fn):
    """A daemon timer tracked by phone so a save switch cancels it."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    try:
        from . import phone
        phone._track_timer(t)
    except Exception:
        pass
    t.start()
    return t


def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _snippet(text, n=90):
    return _clip((text or "").replace("\n", " "), n)


def _clean_single(text):
    """Strip quotes / speaker prefixes the model sometimes adds."""
    t = (text or "").strip()
    for _ in range(2):
        if len(t) >= 2 and t[0] in "\"'“" and t[-1] in "\"'”":
            t = t[1:-1].strip()
    return t


def _parse_json_array(text):
    """The comment list from a model reply. Tolerates code fences, chatter,
    and a reply cut off mid-list (keeps every COMPLETE object -- a long
    celebrity comment pass once ran past the length limit and the whole
    thing was thrown away)."""
    import re
    t = (text or "").strip()
    a, b = t.find("["), t.rfind("]")
    if a >= 0 and b > a:
        try:
            val = json.loads(t[a:b + 1])
            if isinstance(val, list):
                return val
        except Exception:
            pass
    out = []
    for m in re.finditer(r"\{[^{}]*\}", t[a:] if a >= 0 else t):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    if out:
        return out
    return [] if t.replace("`", "").replace("json", "").strip() in ("[]", "") else None


def _journal_comment(from_si, to_si, post, text, verb="commented on"):
    """Journal one comment for its pair. Journal convention (see
    journal.get_sim_history, which matches one direction only): `sim` is
    the NON-household sim and `recipient` the household sim, whoever
    wrote it -- same as texts the player sends."""
    if _is_fan(from_si) or _is_fan(to_si):
        return
    try:
        from . import journal
        hh = _household_ids()
        if str(from_si.sim_id) in hh and str(to_si.sim_id) not in hh:
            contact_si, household_si = to_si, from_si
        else:
            contact_si, household_si = from_si, to_si
        whose = "their own" if str(post.get("author_id")) == str(from_si.sim_id) else f"{post.get('author_name')}'s"
        journal.add_entry(
            "post_comment",
            f"{_name(from_si)} {verb} {whose} post (\"{_snippet(post.get('text'), 80)}\"):\n{text}",
            sim_name=_name(contact_si), recipient_name=_first(household_si),
            sim_id=str(contact_si.sim_id), recipient_id=str(household_si.sim_id),
        )
    except Exception as e:
        _log(f"journal comment failed: {type(e).__name__}: {e}")


def _apply_mood(poster_si, replier_si, mood):
    """Comment moods nudge friendship / romance -- but only between sims
    who already know each other. A stranger's "omg congrats" is not a
    relationship, and it shouldn't put them in the poster's panel."""
    if not mood or mood not in _VALID_MOODS or _is_fan(replier_si):
        return
    if _is_stranger(poster_si, replier_si):
        return
    try:
        from . import relationship_impact
        relationship_impact.apply_from_mood(poster_si, replier_si, mood)
    except Exception as e:
        _log(f"relationship impact failed: {type(e).__name__}: {e}")


def _language():
    try:
        return config.get_language() or "English"
    except Exception:
        return "English"


def _context_line():
    bits = []
    try:
        from . import phone
        for fn in (phone._season_context, phone._time_context):
            try:
                s = fn()
                if s:
                    bits.append(str(s).strip())
            except Exception:
                pass
    except Exception:
        pass
    return "\n".join(bits)


# ---------------------------------------------------------------------------
# Followers + Get Famous fame
# ---------------------------------------------------------------------------
#
# Two layers: the follower COUNT is an audience size that can reach the
# thousands; everyone who SPEAKS (comments, replies, texts) is still a
# real save sim. The count scales how many real strangers get a chance to
# see a public post, and it's in the prompt so a big account reads like
# one.

# Follower FLOOR per Get Famous star rank (Up and Comer, Rising Star,
# B-Lister, Celebrity, Icon). A new profile starts at least here, and an
# existing one is raised to it when the sim gains stars some other way.
_FAME_SEED = {0: 0, 1: 2000, 2: 25000, 3: 250000, 4: 1500000, 5: 10000000}
_POSITIVE_MOODS = {"happy", "confident", "energized", "playful", "inspired", "flirty"}
_NEGATIVE_MOODS = {"angry", "uncomfortable", "tense", "embarrassed", "bored"}
# (min_rate, max_rate) of current followers gained/lost, per reception tier
# Share of current followers gained/lost per public post. A breakout (views-
# based, 3-10x followers) must stay the biggest jump: at 3 stars a viral
# post is ~3.5-9%, a good one ~0.5-2%.
_TIER_RATES = {"viral": (0.02, 0.05), "good": (0.003, 0.012), "flat": (0.0, 0.002), "backlash": (-0.03, -0.01)}
_TIER_FLOOR = {"viral": (15, 60), "good": (2, 12), "flat": (0, 3), "backlash": (-6, -1)}
# Fame is HARD to earn: only a viral post on an account that already has an
# audience, or crossing a big follower milestone. A few happy friends on a
# 9-follower account is a good day, not fame. Values are shares of the fame
# stat's full range.
_VIRAL_FAME = 0.005
_FAME_COOLDOWN_DAYS = 1.0    # at most one viral fame award per sim per in-game day
# A viral post has a small chance to break out: millions of views no matter
# how small the account. Once a sim has had one, the odds drop tenfold.
_BREAKOUT_CHANCE = 0.03
_AI_BREAKOUT_ACCEPT = 0.6   # share of the AI's "huge"/"scandal" calls that truly break out
_BREAKOUT_FAME = 0.04
_VIRAL_FAME_MIN_FOLLOWERS = 500
_BACKLASH_FAME = -0.004
_BACKLASH_FAME_MIN_FOLLOWERS = 1000
_MILESTONES = (1000, 5000, 10000, 50000, 100000, 500000, 1000000, 5000000, 10000000, 50000000)
_MILESTONE_FAME = 0.01
# Viral = spread beyond the poster's own circle: public, strong reception,
# and at least this many strangers reacting well.
_VIRAL_MIN_SCORE = 4
_VIRAL_MIN_STRANGERS = 2


def _fame_stat_type():
    try:
        from fame.fame_tuning import FameTunables
        return FameTunables.FAME_RANKED_STATISTIC  # None without Get Famous
    except Exception:
        return None


def fame_rank(si):
    """Get Famous star rank 0-5 (0 without the pack or without fame)."""
    st = _fame_stat_type()
    if st is None or si is None:
        return 0
    try:
        stat = si.commodity_tracker.get_statistic(st)
        return int(getattr(stat, "rank_level", 0) or 0) if stat is not None else 0
    except Exception:
        return 0


def _add_fame(si, fraction):
    """Add (or remove) a fraction of the fame stat's range. Skips sims whose
    fame is turned off, and does nothing without Get Famous."""
    st = _fame_stat_type()
    if st is None or si is None or not fraction:
        return 0
    try:
        if getattr(si, "allow_fame", True) is False:
            return 0
        span = float(getattr(st, "max_value", 0) or 0) - float(getattr(st, "min_value", 0) or 0)
        if span <= 0:
            return 0
        points = span * fraction
        tracker = si.commodity_tracker
        try:
            tracker.add_value(st, points)
        except Exception:
            stat = tracker.get_statistic(st, add=True)
            stat.add_value(points)
        return points
    except Exception as e:
        _log(f"fame change failed for {_name(si)}: {type(e).__name__}: {e}")
        return 0


def _profile(data, si):
    """The follower profile for a sim, created on first use: their network
    size, plus a head start if they're already famous."""
    profiles = data.setdefault("profiles", {})
    key = str(si.sim_id)
    prof = profiles.get(key)
    rank = fame_rank(si)
    floor = _FAME_SEED.get(rank, 0)
    if prof is None:
        seed = len(_network(si)) + floor
        prof = {"name": _name(si), "followers": int(seed), "fans": [], "history": [],
                "milestones_hit": [m for m in _MILESTONES if m <= seed], "rank_seen": rank}
        profiles[key] = prof
    elif rank <= int(prof.get("rank_seen") or 0):
        pass   # no new star: the count is free to dip below the floor
    elif int(prof.get("followers") or 0) >= floor:
        prof["rank_seen"] = rank
        prof["_dirty"] = True
    else:
        # Gained stars outside Llamafone (gigs, the game's own fame): an
        # account that famous has at least this many followers. Milestones
        # below the floor are marked hit WITHOUT awarding fame, so fame and
        # followers can't feed each other.
        _log(f"{_name(si)} is now a {fame_rank(si)}-star celebrity: followers "
             f"{prof.get('followers')} -> {floor}")
        prof["followers"] = floor
        prof["milestones_hit"] = sorted(set(prof.get("milestones_hit") or []) |
                                        {m for m in _MILESTONES if m <= floor})
        prof["rank_seen"] = rank
        prof["_dirty"] = True
    return prof


# Followers also move on their own over in-game time: famous accounts keep
# gaining a little, accounts that go quiet slowly slip, plus noise.
_DRIFT_MAX_DAYS = 30
_DRIFT_FAME_DAILY = 0.0015       # per star, per day
_DRIFT_BASE_DAILY = 0.0003
_DRIFT_IDLE_AFTER_DAYS = 5
_DRIFT_IDLE_DAILY = -0.002
_DRIFT_NOISE_DAILY = 0.0008


def _drift(prof, si):
    try:
        from . import milestones
        now = milestones._now_sim_ticks()
        per_day = float(milestones._TICKS_PER_DAY)
    except Exception:
        return False
    if now is None:
        return False
    last = prof.get("drift_ticks")
    if last is None or now < last:       # first look, or an older save loaded
        prof["drift_ticks"] = now
        return True
    days = min((now - last) / per_day, _DRIFT_MAX_DAYS)
    if days < 1.0:
        return False
    n = int(prof.get("followers") or 0)
    rate = _DRIFT_BASE_DAILY + _DRIFT_FAME_DAILY * fame_rank(si)
    last_post_ticks = None
    for h in reversed(prof.get("history") or []):
        if h.get("ticks") is not None:
            last_post_ticks = h["ticks"]
            break
    idle_days = (now - last_post_ticks) / per_day if last_post_ticks is not None else days
    if idle_days > _DRIFT_IDLE_AFTER_DAYS:
        rate += _DRIFT_IDLE_DAILY
    rate += random.uniform(-_DRIFT_NOISE_DAILY, _DRIFT_NOISE_DAILY)
    delta = int(round(n * rate * days))
    soft_floor = int(_FAME_SEED.get(fame_rank(si), 0) * 0.9)   # can dip, not crash
    after = max(soft_floor, n + delta, 0)
    prof["followers"] = after
    prof["drift_ticks"] = now
    if after != n:
        _log(f"followers drift for {prof.get('name')}: {n} -> {after} over {days:.1f} in-game day(s)")
    return True


def followers_of(si):
    data = _load()
    if data is None or si is None:
        return 0
    with _lock:
        new = str(si.sim_id) not in (data.get("profiles") or {})
        prof = _profile(data, si)
        drifted = _drift(prof, si)
        n_before = int(prof.get("followers") or 0)
        released = _release_pending_follow(prof, data)
        if released:
            # Milestones crossed as a breakout's followers pour in still count.
            hit = set(prof.get("milestones_hit") or [])
            n_after = int(prof.get("followers") or 0)
            crossed = [m for m in _MILESTONES if n_before < m <= n_after and m not in hit]
            if crossed:
                prof["milestones_hit"] = sorted(hit | set(crossed))
                for _m in crossed:
                    _add_fame(si, _MILESTONE_FAME)
                _log(f"{prof.get('name')} passed {_fmt(crossed[-1])} followers as a breakout spread")
        jittered = False if drifted else _jitter(prof)
        if new or drifted or released or jittered or prof.pop("_dirty", False):
            _save(data)
        return int(prof.get("followers") or 0)


def _release_pending_follow(prof, data):
    """Hand out a breakout's followers in step with the post's growth."""
    pend = prof.get("pending_follow") or []
    if not pend:
        return False
    changed = False
    keep = []
    for item in pend:
        post = None
        for p in data.get("posts") or []:
            if p.get("id") == item.get("post_id"):
                post = p
                break
        total = int(item.get("total") or 0)
        given = int(item.get("given") or 0)
        target = total if post is None else int(round(total * _growth(post)))
        if target > given:
            prof["followers"] = int(prof.get("followers") or 0) + (target - given)
            item["given"] = target
            changed = True
        if item["given"] < total:
            keep.append(item)
    prof["pending_follow"] = keep
    return changed


_JITTER_SECONDS = 30


def _jitter(prof):
    """A live account never sits on a round number: nudge it a hair each
    time it's looked at (at most every 30 real seconds), slightly upward
    on average."""
    import time
    now = time.time()
    if now - float(prof.get("jitter_ts") or 0) < _JITTER_SECONDS:
        return False
    prof["jitter_ts"] = now
    n = int(prof.get("followers") or 0)
    if n >= 200:
        n += int(round(n * random.uniform(-0.00015, 0.00025)))
    elif random.random() < 0.2:
        n += random.choice((-1, 1, 1))
    prof["followers"] = max(0, n)
    return True


def _fmt(n):
    return f"{int(n):,}"


_FAN_ACCOUNT_MIN = 10000    # from here on, strangers on a public post are fans


def _fan_comment_count(followers, audience):
    """Made-up-username fan comments to ask for, by account size."""
    if audience != "public" or followers < 1000:
        return 0
    if followers < 10000:
        return random.randint(1, 2)
    if followers < 100000:
        return random.randint(2, 4)
    if followers < 1000000:
        return random.randint(5, 8)
    return random.randint(7, 10)


def _stranger_slots(si):
    """Bigger accounts reach more strangers / fans (still real sims):
    ~4 under 1K followers, ~6 at 10K, ~8 at 100K, ~10 at 1M, 12 at 10M+."""
    import math
    n = followers_of(si)
    return max(_MAX_STRANGER_CANDIDATES, min(12, _MAX_STRANGER_CANDIDATES + int(2 * math.log10(max(n / 100.0, 1)))))


# Share of the base audience that saw the post, by tier (viral spreads past
# the poster's own followers; drama spreads too), and the share of viewers
# who liked it.
# Eventual totals; the displayed numbers grow toward them over in-game
# hours (_grown). A good post on a 250K account: ~30-75K views, ~1-6K likes.
_VIEW_RATES = {"viral": (0.8, 3.0), "good": (0.12, 0.3), "flat": (0.04, 0.12), "backlash": (0.2, 0.6)}
_LIKE_RATES = {"viral": (0.03, 0.06), "good": (0.04, 0.08), "flat": (0.01, 0.04), "backlash": (0.01, 0.03)}
# Engagement ramps over in-game hours with a soft start (at normal speed a
# real minute is about an in-game hour, so a steep curve looked instant).
# 0-star: ~3% at 1.5h, ~22% at 5h, ~60% at 12h, ~95% by a day and a half.
# 3-star (scale 8h): ~8% at 1.5h, ~30% at 4h, ~63% at 8h, ~94% at 16h.
_DISCOVERY_VIEWS = {"viral": (60, 300), "good": (10, 60), "flat": (3, 20), "backlash": (15, 80)}
_GROWTH_HOURS = 14.0
_GROWTH_SHAPE = 1.5


def _estimate_reach(prof, followers, audience, tier, n_comments, poster_si):
    """(views, likes) for a post: audience numbers on the same layer as the
    follower count. Friends-only posts reach the poster's network."""
    base = followers if audience == "public" else max(len(_network(poster_si)), 1)
    lo, hi = _VIEW_RATES[tier]
    views = int(round(base * random.uniform(lo, hi)))
    if audience != "public":
        views = min(views, base)
    else:
        # Discovery: anyone can stumble on a public post (hashtags, shares,
        # explore). Barely noticeable on a celebrity; it's what keeps a
        # 26-follower sim's public post from topping out at 3-8 views.
        dlo, dhi = _DISCOVERY_VIEWS[tier]
        views += random.randint(dlo, dhi)
    views = max(views, n_comments, 1)
    llo, lhi = _LIKE_RATES[tier]
    likes = int(round(views * random.uniform(llo, lhi)))
    likes = max(likes, sum(1 for _ in range(n_comments)) // 2)
    return views, min(likes, views)


def _growth(post):
    """Share of the eventual totals reached by now, by in-game hours since
    the post went up."""
    import math
    try:
        from . import milestones
        now = milestones._now_sim_ticks()
        per_hour = float(milestones._TICKS_PER_DAY) / 24.0
        t = post.get("ticks")
        if now is None or t is None or now < t:
            return 1.0
        hours = (now - t) / per_hour
        # Celebrity posts are front-loaded: most engagement in the first
        # few hours (3 stars -> half by ~2.5 in-game hours, not ~6).
        scale = _GROWTH_HOURS / (1.0 + 0.25 * int(post.get("author_rank") or 0))
        if (post.get("projection") or {}).get("breakout"):
            scale /= 3.0   # a breakout races across the internet
        return max(0.005, 1.0 - math.exp(-((hours / scale) ** _GROWTH_SHAPE)))
    except Exception:
        return 1.0


def _current_stats(post):
    """(views, likes, comments, settled_fraction) as of now."""
    import math
    base = post.get("reception") or post.get("projection") or {}
    g = _growth(post)
    real = len(post.get("comments") or [])
    if not base:
        return 0, 0, real, g
    total_c = int(base.get("comments") or base.get("comments_total") or 0)
    pr = post.get("projection") or {}
    views = max(int(pr.get("floor_views") or 0), int(round(int(base.get("views") or 0) * g)))
    likes = max(int(pr.get("floor_likes") or 0), int(round(int(base.get("likes") or 0) * g)))
    comments = max(real, int(pr.get("floor_comments") or 0), int(round(total_c * g)))
    # Keep the three consistent: commenters usually like it too, plus people
    # who only like; and far more people see a post than react to it.
    if comments:
        likes = max(likes, int(math.ceil(comments * 1.3)))
    if likes or comments:
        views = max(views, int(math.ceil(likes * 1.4)) + 1, comments + 1)
    # Never more people than could see it: a friends-only post reaches the
    # poster's own network (a loner with no friends gets almost nothing).
    if post.get("audience") != "public":
        cap = max(int(pr.get("audience_size") or 0), real)
        if cap:
            views = min(views, cap)
            comments = min(comments, views)
            likes = min(likes, views)
    return views, likes, comments, g


def _stats_line(post):
    views, likes, comments, g = _current_stats(post)

    def _n(v, word):
        return f"{_fmt(v)} {word}{'' if v == 1 else 's'}"
    tail = " so far" if g < 0.95 else ""
    if not (post.get("reception") or post.get("projection")):
        return _n(comments, "comment") + " so far"
    return f"{_n(views, 'view')}, {_n(likes, 'like')}, {_n(comments, 'comment')}{tail}"


def _decide_tier(verdict, picked, audience, poster_si):
    """How far the post travels. The AI's reach verdict decides (it reads
    what the post actually says -- "what are you all up to" from a
    celebrity isn't viral however nicely fans reply). Without a verdict,
    fall back to comment moods, capped at "good"."""
    if verdict in _TIER_RATES:
        if verdict == "viral" and audience != "public":
            return "good"
        return verdict
    tier = _reception_tier(picked, audience, poster_si)
    return "good" if tier == "viral" else tier


def _is_stranger(poster_si, si, known=None):
    if _is_fan(si):
        return True
    try:
        if known is None:
            known = _network_ids(poster_si)
        return str(si.sim_id) not in known
    except Exception:
        return False


def _reception_tier(picked, audience, poster_si=None):
    pos = sum(1 for _si, _b, m in picked if m in _POSITIVE_MOODS)
    neg = sum(1 for _si, _b, m in picked if m in _NEGATIVE_MOODS)
    neutral = len(picked) - pos - neg
    score = pos - 1.5 * neg + 0.3 * neutral
    if neg >= 2 and neg > pos:
        return "backlash"
    # Only REAL sims who've never met the poster prove a post spread. Fan
    # handles are requested by the mod and almost always gush, so counting
    # them made every celebrity post "viral".
    known = _network_ids(poster_si) if poster_si is not None else set()
    strangers_pos = sum(1 for si, _b, m in picked
                        if m in _POSITIVE_MOODS and not _is_fan(si)
                        and poster_si is not None and _is_stranger(poster_si, si, known))
    if audience == "public" and score >= _VIRAL_MIN_SCORE and strangers_pos >= _VIRAL_MIN_STRANGERS:
        return "viral"
    if score >= 1:
        return "good"
    if score < 0:
        return "backlash"
    return "flat"


def _settle_reception(post_id, poster_si, picked):
    """After a post's comments are in: move the follower count (and Get
    Famous fame), and tell the player how it landed."""
    from . import notifications
    try:
        with _lock:
            data = _load()
            post = get_post(post_id) if data else None
            if post is None:
                return
            audience = post.get("audience")
            # The tier was decided once (AI verdict) in _project; use it for
            # the follower math too. Recounting moods here let a post the AI
            # judged "good" grow followers like a viral one.
            tier = (post.get("projection") or {}).get("tier") or _decide_tier(None, picked, audience, poster_si)
            breakout = bool((post.get("projection") or {}).get("breakout"))
            prof = _profile(data, poster_si)
            before = int(prof.get("followers") or 0)
            lo, hi = _TIER_RATES[tier]
            rate = random.uniform(lo, hi)
            if rate > 0:
                rate *= 1 + 0.25 * fame_rank(poster_si)
            if audience != "public":
                rate *= 0.15          # friends-only: only a few shares
            delta = int(round(before * rate))
            flo, fhi = _TIER_FLOOR[tier]
            floor = random.randint(min(flo, fhi), max(flo, fhi))
            if audience != "public":
                floor = int(round(floor * 0.3))
            delta = max(delta, floor) if tier != "backlash" else min(delta, floor)
            if audience != "public":
                # Friends-only reaches people who already follow you: barely
                # moves the count (a 5-view post gained 1,864 followers).
                delta = (random.choice((0, 0, 1)) if tier in ("good", "viral")
                         else -random.choice((0, 1)) if tier == "backlash" else 0)
            if breakout:
                bviews = int((post.get("projection") or {}).get("views") or 0)
                if (post.get("projection") or {}).get("scandal"):
                    # Some followers leave; notoriety brings new ones.
                    delta = (-int(round(before * random.uniform(0.01, 0.05)))
                             + int(round(bviews * random.uniform(0.001, 0.005))))
                else:
                    # A slice of everyone who saw it follows.
                    delta = max(delta, int(round(bviews * random.uniform(0.005, 0.02))))
                prof["breakouts"] = int(prof.get("breakouts") or 0) + 1
            # Real sims who reacted well become named fans; bad reactions unfollow.
            fans = set(prof.get("fans") or [])
            for si, _b, mood in picked:
                if _is_fan(si):
                    continue
                if mood in _POSITIVE_MOODS:
                    fans.add(str(si.sim_id))
                elif mood in _NEGATIVE_MOODS:
                    fans.discard(str(si.sim_id))
            if breakout and delta > 0:
                # Followers arrive as the post spreads, not all at once: give
                # what the post has reached so far, release the rest later.
                g_now = _growth(post)
                now_part = int(round(delta * g_now))
                prof.setdefault("pending_follow", []).append(
                    {"post_id": post_id, "total": delta, "given": now_part})
                delta = now_part
            after = max(0, before + delta)
            prof["followers"] = after
            prof["fans"] = sorted(fans)[-200:]
            prof.setdefault("history", []).append(
                {"ts": _now_iso(), "ticks": _now_ticks(), "post_id": post_id, "tier": tier, "delta": after - before})
            prof["history"] = prof["history"][-50:]
            pr = post.get("projection") or {}
            if pr:
                tier = pr.get("tier") or tier
                views, likes = int(pr.get("views") or 0), int(pr.get("likes") or 0)
                total = int(pr.get("comments_total") or len(picked))
            else:
                views, likes = _estimate_reach(prof, before, audience, tier, len(picked), poster_si)
                total = len(picked)
            if pr.get("audience_size") is not None:
                post.setdefault("projection", pr)
            post["reception"] = {"tier": tier, "follower_delta": after - before,
                                 "views": views, "likes": likes, "comments": total,
                                 "real_comments": len(picked)}
            # Follower milestones crossed by this post (each counts once).
            hit = set(prof.get("milestones_hit") or [])
            crossed = [m for m in _MILESTONES if before < m <= after and m not in hit]
            prof["milestones_hit"] = sorted(hit | set(crossed))
            # Fame from a viral post: at most once per in-game day. Three
            # "viral" posts in one afternoon pushed a sim up a whole star.
            fame_viral_ok = False
            if audience == "public" and tier == "viral" and before >= _VIRAL_FAME_MIN_FOLLOWERS and not breakout:
                nowt = _now_ticks()
                lastt = prof.get("last_fame_ticks")
                try:
                    from . import milestones as _ms_mod
                    per_day = float(_ms_mod._TICKS_PER_DAY)
                except Exception:
                    per_day = None
                if nowt is None or lastt is None or per_day is None or (nowt - lastt) / per_day >= _FAME_COOLDOWN_DAYS:
                    fame_viral_ok = True
                    prof["last_fame_ticks"] = nowt
                else:
                    _log(f"post {post_id}: viral, but fame was already awarded today -- followers only")
            _save(data)
        pts = 0
        if breakout:
            pts += _add_fame(poster_si, _BREAKOUT_FAME)
        elif fame_viral_ok:
            pts += _add_fame(poster_si, _VIRAL_FAME)
        if audience == "public" and tier == "backlash" and before >= _BACKLASH_FAME_MIN_FOLLOWERS:
            pts += _add_fame(poster_si, _BACKLASH_FAME)
        for _m in crossed:
            pts += _add_fame(poster_si, _MILESTONE_FAME)
        fame_note = ""
        if crossed:
            fame_note = f" You passed {_fmt(crossed[-1])} followers!"
        if pts > 0:
            fame_note += " Your fame went up."
        elif pts < 0:
            fame_note += " Your fame took a small hit."
        _log(f"post {post_id} landed '{tier}': followers {before} -> {after}; "
             f"milestones {crossed or '-'}; fame {pts:+.1f} pts")
        if not _comment_popups() or post.get("quiet_resume"):
            return
        verb = {"viral": "Your post went viral!", "good": "People liked your post.",
                "flat": "Your post got a little attention.", "backlash": "Your post got backlash."}[tier]
        if breakout:
            verb = ("Your post is a SCANDAL. It's everywhere, and people are furious."
                    if (post.get("projection") or {}).get("scandal")
                    else "Your post is BLOWING UP. It's spreading way beyond your followers!")
        sign = "+" if after >= before else "-"
        follow = (f" {sign}{_fmt(abs(after - before))} followers ({_fmt(after)} total)."
                  if after != before else f" {_fmt(after)} followers.")
        if breakout and not (post.get("projection") or {}).get("scandal"):
            follow = f" {_fmt(after)} followers and climbing fast."
        notifications.show("Llamagram", f"{verb} {_stats_line(post)}.{follow}{fame_note}")
    except Exception as e:
        _log(f"settle reception for {post_id} raised: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 1. Player posts
# ---------------------------------------------------------------------------

_COMMENTS_SYSTEM = """You simulate the comment section of a social media post in \
The Sims 4. Decide which of the listed sims (if any) comment on the post, and \
write their comments. Write in {language}.

Rules:
- Only sims from the CANDIDATES list may comment. Use their names exactly as written.
- Most people scroll past. Pick only the ones who would realistically comment, \
given their relationship with the poster, their traits, and what the post says. \
Zero comments is a fine outcome for an ordinary post; an exciting, emotional, or \
provocative post draws more.
- Strangers only appear on PUBLIC posts. They comment rarely, and they don't know \
the poster: a reaction, a joke, a question, the occasional troll.
- TEENS: if the POSTER is a teen, nobody who isn't also a teen flirts, compliments \
their looks, or comments romantically -- adults comment the way adults comment on \
a kid's post (supportive, parental, or not at all), and the "flirty" mood is \
never used between a teen and an adult.
- Big accounts are different. When POSTER'S FOLLOWERS is in the tens of \
thousands or more, the candidates marked as fans DO comment -- several of them: \
excited, starstruck, parasocial, asking for a follow-back, sometimes weird or \
demanding. A celebrity's public post almost never gets zero comments. On a big account's PUBLIC post, MOST comments come from fans and strangers -- at most one or two from the poster's own family and friends.
- Each comment is one short social-media comment (a few words to two sentences) \
in that sim's own voice. Casual; lowercase is fine. No hashtags unless it fits.
- Family and close friends react like family and close friends. Exes, rivals, and \
people with negative history can be cold, snarky, or say nothing at all.
- Don't invent facts about the poster beyond the post and the information given.
- Each candidate's line is GROUND TRUTH about what they already know. Nobody \
reacts with surprise to news they are part of: the other parent of the poster's \
baby knows about the baby (it's theirs too), a spouse knows about their own \
wedding, a sibling knows about their shared parents. "Has met Miley in person" \
means they already have -- no "can't wait to meet her".
- Use the POSTER'S RECENT LIFE and each candidate's "recent between them" lines \
so comments fit what's actually going on (a friend they just argued with, a \
mom who already knows about the promotion). A candidate brings up something \
from the poster's recent life only if it's in the post, it's public (e.g. a \
visible pregnancy), they're involved, or they're family / a close friend who \
would have heard. Strangers know only the post.
- "mood" is the emotion the comment would leave the POSTER feeling, from: happy, \
sad, angry, confident, flirty, playful, energized, inspired, embarrassed, tense, \
uncomfortable, bored, or none. Use none for ordinary friendly comments.

- FAN COMMENTS: when the prompt asks for N fan comments, ALSO write exactly N \
comments from random followers with made-up usernames (like @mileymoments or \
@noodle_kween). They don't know the poster personally: excited, starstruck, \
jokes, questions, the odd hater. Never use a real sim's name as a username.

- REACH VERDICT: judge how far this post would REALISTICALLY travel, from what \
it actually says and who posted it -- not from how nice the comments are.
    "viral"    genuinely shareable: big news from a well-known poster (a baby, \
an engagement, a breakup), something hilarious, shocking, or touching, a hot \
take, a scandal. RARE -- most posts, even from celebrities, are not viral. \
The poster's fame or follower count is NEVER the reason: "experimenting with \
new genres" from a star is "good", not "viral", however big the account.
    "good"     people liked it and engaged. A casual check-in, a selfie, or a \
"what are you all up to" from a celebrity is "good" at most.
    "flat"     an ordinary post that didn't land.
    "backlash" it started a fight or offended people.
    "huge"     ONCE-IN-A-LIFETIME: news or a moment so big it gets shared \
everywhere and picked up beyond the platform (a celebrity's surprise \
engagement or pregnancy, a jaw-dropping stunt, a world-stopping announcement). \
Almost never -- a normal viral post is "viral", not "huge".
    "scandal"  a genuinely shocking controversy that blows up (a cheating \
confession, a cruel or offensive take, leaked drama). Almost never.
  Account size alone never makes a post "huge" or "scandal"; content does.
  Friends-only posts can't be "viral", "huge", or "scandal".

Output ONLY a JSON array, no other text. The FIRST element is the verdict; then \
the comments (sims use "name", fans use "handle"):
[{{"verdict": "good", "why": "few words"}}, {{"name": "Exact Name", "comment": "text", "mood": "none"}}, {{"handle": "@username", "comment": "text", "mood": "happy"}}]
If nobody comments, output just the verdict element."""


def player_post(poster_si, text, audience="friends", output=None):
    """Publish the player's post and schedule the comment pass."""
    from . import notifications
    text = _clip(text, _MAX_POST_CHARS)
    if not text:
        return None
    if not enabled():
        notifications.show("Llamafone", "Llamagram is turned off (social_enabled in llamafone.cfg).", output=output)
        return None
    audience = "public" if str(audience).lower().startswith("pub") else "friends"
    post = _add_post(poster_si, text, audience, is_player=True)
    if post is None:
        notifications.show("Llamafone", "Llamafone is off for this save, so the post wasn't saved. "
                                        "Save the game once to turn it on.", output=output)
        return None
    _log(f"player post {post['id']} by {post['author_name']} ({audience}): {_snippet(text)}")
    notifications.show(
        "Llamagram",
        (f"Posted to your {_fmt(followers_of(poster_si))} followers. " if audience == "public"
         else "Posted for your friends. ") +
        f"Comments, if anyone has something to say, will show up over the next little while "
        f"and in Llamafone > Llamagram Notifications.",
        output=output,
    )
    # Engagement starts right away: placeholder totals from the follower
    # count (a typical reception), replaced when the comment pass decides
    # how the post actually landed. A 312K celebrity's post sat at "0
    # comments so far" for minutes before.
    _provisional_projection(post["id"], poster_si)
    n = followers_of(poster_si)
    first = (8, 20) if n >= 10000 else (30, 60) if n >= 1000 else _FIRST_BATCH_DELAY
    _timer(random.uniform(*first), lambda: _generate_comments(post["id"]))
    return post


def _generate_comments(post_id):
    try:
        post = get_post(post_id)
        poster_si = _resolve(post.get("author_id")) if post else None
        if post is None or poster_si is None:
            _log(f"comment pass for {post_id}: post or poster gone (save switched?); skipping")
            return
        cands = _audience_candidates(poster_si, post.get("audience"))
        if not cands:
            _log(f"comment pass for {post_id}: nobody in {post['author_name']}'s audience")
            _project(post_id, poster_si, [], "flat")
            _mark_pass_done(post_id)
            _settle_reception(post_id, poster_si, [])
            return
        by_name = {_name(si).lower(): (si, stranger) for si, _rel, stranger in cands}
        lines = [
            f"POSTER: {_name(poster_si)} ({_sim_brief(poster_si, with_kids=True)})",
            f"POSTER'S FOLLOWERS: {_fmt(followers_of(poster_si))}"
            + (f" (Get Famous: {fame_rank(poster_si)}-star celebrity)" if fame_rank(poster_si) else ""),
            f"AUDIENCE: {'PUBLIC -- anyone can see it' if post.get('audience') == 'public' else 'FRIENDS ONLY -- people the poster knows'}",
        ]
        partner = _partner_line(poster_si)
        if partner:
            lines.append(partner)
        ctx = _context_line()
        if ctx:
            lines.append(ctx)
        life = _poster_life_block(poster_si)
        if life:
            lines += ["", "POSTER'S RECENT LIFE (facts; see the rules for who would know them):", life]
        n_followers = followers_of(poster_si)
        cap = 12 if n_followers >= _FAN_ACCOUNT_MIN else 10
        n_fans = _fan_comment_count(n_followers, post.get("audience"))
        fan_line = f" Also write exactly {n_fans} fan comments from made-up usernames." if n_fans else ""
        lines += ["", f"(At most {cap} comments from the candidates.{fan_line})", "POST:", post.get("text", ""), "", "CANDIDATES:"]
        for si, rel, stranger in cands:
            lines.append(f"- {_name(si)} -- {rel}; {_sim_brief(si)}")
            if not stranger:
                for h in _pair_history(si, poster_si):
                    lines.append(f"    recent between them: {h}")
        prompt = "\n".join(lines)
        system = _COMMENTS_SYSTEM.format(language=_language())

        def _on_result(text, err):
            if err or not text:
                _log(f"comment pass for {post_id} failed: {err}")
                return
            items = _parse_json_array(text)
            if items is None:
                _log(f"comment pass for {post_id}: unparseable response: {_snippet(text, 200)}")
                return
            picked, seen = [], set()
            verdict, ai_breakout = None, None
            for it in items:
                if isinstance(it, dict) and it.get("verdict"):
                    v = str(it.get("verdict")).strip().lower()
                    if v in ("huge", "scandal"):
                        ai_breakout = v
                        v = "viral" if v == "huge" else "backlash"
                    if v in _TIER_RATES:
                        verdict = v
                        _log(f"comment pass for {post_id}: AI reach verdict "
                             f"'{ai_breakout or v}' ({_snippet(str(it.get('why') or ''), 80)})")
                    break
            fan_cap = n_fans + 2
            real_names = {_name(si).lower() for si, _r, _s in cands} | {_name(poster_si).lower()}
            for it in items:
                if not isinstance(it, dict):
                    continue
                handle = str(it.get("handle") or "").strip()
                if handle:
                    body = _clip(_clean_single(str(it.get("comment") or "")), _MAX_COMMENT_CHARS)
                    fan = _Fan(handle)
                    key = fan.handle.lower()
                    if (not body or key in seen or fan_cap <= 0
                            or fan.handle.lstrip("@").lower() in real_names):
                        continue
                    seen.add(key)
                    fan_cap -= 1
                    mood = str(it.get("mood") or "none").strip().lower()
                    picked.append((fan, body, mood if mood in _VALID_MOODS else None))
                    continue
                nm = str(it.get("name") or "").strip().lower()
                body = _clip(_clean_single(str(it.get("comment") or "")), _MAX_COMMENT_CHARS)
                if nm not in by_name or nm in seen or not body:
                    continue
                seen.add(nm)
                mood = str(it.get("mood") or "none").strip().lower()
                picked.append((by_name[nm][0], body, mood if mood in _VALID_MOODS else None))
            _log(f"comment pass for {post_id}: {len(picked)} of {len(cands)} candidates commented")
            _project(post_id, poster_si, picked, verdict, ai_breakout)
            if picked:
                _schedule_batches(post_id, poster_si, picked)   # marks pass_done with the pending save
            else:
                _mark_pass_done(post_id)
                _settle_reception(post_id, poster_si, [])

        from . import api_client
        api_client.call_ai_async([{"role": "user", "content": prompt}], system=system,
                                 use_fast_model=True, callback=_on_result, max_tokens=1500)
    except Exception as e:
        _log(f"comment pass for {post_id} raised: {type(e).__name__}: {e}")


def _project(post_id, poster_si, picked, verdict=None, ai_breakout=None):
    """Decide how the post lands and how far it travels, once, so the
    trickle of notifications and the final summary agree. Views, likes,
    and the comment TOTAL are audience numbers (same layer as followers);
    the comments you read are the real sims'."""
    with _lock:
        data = _load()
        post = get_post(post_id) if data else None
        if post is None:
            return
        audience = post.get("audience")
        tier = _decide_tier(verdict, picked, audience, poster_si)
        followers = followers_of(poster_si)
        views, likes = _estimate_reach(None, followers, audience, tier, len(picked), poster_si)
        total = len(picked)
        if audience == "public" and followers >= 100:
            total = max(total, int(round(likes * random.uniform(0.005, 0.02))))
        breakout, scandal = False, False
        if audience == "public" and tier in ("viral", "backlash"):
            prof = (data.get("profiles") or {}).get(str(poster_si.sim_id)) or {}
            had_one = bool(prof.get("breakouts"))
            if ai_breakout in ("huge", "scandal") and (tier == "viral") == (ai_breakout == "huge"):
                # The AI called it huge or a scandal from the CONTENT; most of
                # those really break out (any account size).
                breakout = random.random() < (_AI_BREAKOUT_ACCEPT / (3.0 if had_one else 1.0))
            elif tier == "viral":
                # The rare lucky break: an ordinary viral post that just explodes.
                breakout = random.random() < (_BREAKOUT_CHANCE / (10.0 if had_one else 1.0))
            if breakout:
                scandal = tier == "backlash"
                views = max(views, int(10 ** random.uniform(5.0, 6.7)),           # ~100K-5M
                            int(followers * random.uniform(3.0, 10.0)))
                like_rate = (0.01, 0.03) if scandal else (0.03, 0.08)
                likes = int(round(views * random.uniform(*like_rate)))
                c_rate = (0.02, 0.06) if scandal else (0.005, 0.02)   # scandals get argued about
                total = max(len(picked), int(round(likes * random.uniform(*c_rate))))
                _log(f"post {post_id}: BREAKOUT{' (scandal)' if scandal else ''} -- "
                     f"{views:,} views on a {followers:,}-follower account")
        prev = post.get("projection") or {}
        floors = {}
        if prev.get("provisional"):
            # Never show fewer than what the placeholder was already showing.
            sv, sl, sc, _g = _current_stats(post)
            floors = {"floor_views": sv, "floor_likes": sl, "floor_comments": sc}
        post.setdefault("author_rank", fame_rank(poster_si))
        audience_size = followers if audience == "public" else max(len(_network(poster_si)), 1)
        post["projection"] = dict({"tier": tier, "views": views, "likes": likes, "comments_total": total,
                                   "batches": 0, "delivered": 0, "audience_size": audience_size,
                                   "breakout": breakout, "scandal": scandal}, **floors)
        _save(data)


def _mark_pass_done(post_id):
    with _lock:
        data = _load()
        post = get_post(post_id) if data else None
        if post is not None:
            post["pass_done"] = True
            _save(data)


def _provisional_projection(post_id, poster_si):
    """Typical-reception placeholder totals for a brand-new post, so likes
    and views start climbing immediately. _project() overwrites them."""
    with _lock:
        data = _load()
        post = get_post(post_id) if data else None
        if post is None or post.get("projection"):
            return
        followers = followers_of(poster_si)
        audience = post.get("audience")
        base = followers if audience == "public" else max(len(_network(poster_si)), 1)
        views = max(1, int(round(base * (0.2 if audience == "public" else 0.6))))
        if audience == "public":
            views += 20   # discovery, as in _estimate_reach
        likes = int(round(views * 0.06))
        total = int(round(likes * 0.01)) if audience == "public" and followers >= 100 else 0
        post["author_rank"] = fame_rank(poster_si)
        post["projection"] = {"tier": None, "views": views, "likes": likes, "comments_total": total,
                              "batches": 0, "delivered": 0, "provisional": True, "audience_size": base}
        _save(data)


def _likes_so_far(post, delivered, n_batches):
    pr = post.get("projection") or {}
    likes = int(pr.get("likes") or 0)
    if not n_batches:
        return likes
    # Likes run ahead of comments: most arrive early.
    frac = min(1.0, (delivered / float(n_batches)) ** 0.6)
    return int(round(likes * frac))


def _schedule_batches(post_id, poster_si, picked):
    """Split comments into batches of 1-3 and deliver each batch as ONE
    notification, spaced out like comments trickling in. The batches are
    SAVED to the post first ("pending"), so quitting the game mid-trickle
    doesn't lose them -- resume_pending() delivers what's left on the next
    load. (A 7-comment celebrity post was lost to a restart this way.)"""
    import time
    post0 = get_post(post_id) or {}
    big_public = post0.get("audience") == "public" and followers_of(poster_si) >= _FAN_ACCOUNT_MIN
    if big_public:
        # Fans and strangers pile in first; the poster's own people trickle
        # in later.
        known = _network_ids(poster_si)
        picked = sorted(picked, key=lambda t: 0 if _is_stranger(poster_si, t[0], known) else 1)
    batches, i = [], 0
    while i < len(picked):
        n = random.randint(1, 3)
        batches.append(picked[i:i + n])
        i += n
    now = time.time()
    # The first comment pop-up shouldn't land the instant the pass returns
    # (likes and views already climb from the moment of posting).
    delay = random.uniform(120, 240) if big_public else random.uniform(60, 150)
    pending = []
    for b in batches:
        pending.append({"due": now + delay, "done": False,
                        "items": [({"handle": si.handle} if _is_fan(si) else {"from_id": str(si.sim_id)})
                                  for si, body, mood in b]})
        for it, (_si, body, mood) in zip(pending[-1]["items"], b):
            it["text"], it["mood"] = body, mood
        delay += random.uniform(180, 480) if big_public else random.uniform(*_NEXT_BATCH_GAP)
    with _lock:
        data = _load()
        post = get_post(post_id) if data else None
        if post is None:
            return
        post["pending"] = pending
        post["pass_done"] = True
        if post.get("projection") is not None:
            post["projection"]["batches"] = len(batches)
        _save(data)
    _schedule_pending(post_id, poster_si)


_resume_slot = [0]          # global stagger for overdue batches across ALL resumed posts
_digest = {"comments": 0, "posts": set()}


def _schedule_pending(post_id, poster_si, quiet=False):
    """Timers for every not-yet-delivered batch (now, or when due), then
    the summary once the last one is in. `quiet` (resume after a quit):
    overdue batches land silently, spaced a few seconds apart across
    every resumed post, and one digest pop-up covers them all -- three
    posts resuming at once used to drop seven pop-ups in two minutes.
    Returns the longest wait scheduled."""
    import time
    post = get_post(post_id)
    if post is None:
        return 0.0
    now = time.time()
    last = 0.0
    for idx, b in enumerate(post.get("pending") or []):
        if b.get("done"):
            continue
        wait = max(0.0, float(b.get("due") or now) - now)
        if wait <= 0.0:
            wait = 20.0 + 15.0 * _resume_slot[0]
            _resume_slot[0] += 1
        last = max(last, wait)
        _timer(wait, lambda idx=idx: _deliver_pending(post_id, poster_si, idx))
    _timer(last + 20.0, lambda: _maybe_settle(post_id, poster_si))
    return last


def _show_resume_digest():
    """One pop-up for everything that landed quietly after a load."""
    from . import notifications
    try:
        n, posts = _digest["comments"], len(_digest["posts"])
        _digest["comments"] = 0
        _digest["posts"] = set()
        with _lock:
            data = _load()
            if data:
                for p in data["posts"]:
                    p.pop("quiet_resume", None)
                _save(data)
        if n and _comment_popups():
            notifications.show(
                "Llamagram",
                f"While you were away, {n} new comment{'' if n == 1 else 's'} landed on "
                f"{posts} of your post{'' if posts == 1 else 's'}. They're in Llamagram Notifications.")
    except Exception as e:
        _log(f"resume digest raised: {type(e).__name__}: {e}")


def _deliver_pending(post_id, poster_si, idx):
    with _lock:
        post = get_post(post_id)
        if post is None:
            return
        pend = post.get("pending") or []
        if idx >= len(pend) or pend[idx].get("done"):
            return
        b = pend[idx]
        b["done"] = True       # claim it before delivering: never deliver twice
        data = _load()
        if data is not None:
            _save(data)
    batch = []
    for it in b.get("items") or []:
        si = _Fan(it["handle"]) if it.get("handle") else _resolve(it.get("from_id"))
        if si is not None:
            batch.append((si, it.get("text") or "", it.get("mood")))
    if batch:
        _deliver_batch(post_id, poster_si, batch)


def _maybe_settle(post_id, poster_si):
    """Settle once every batch is delivered (and only once)."""
    post = get_post(post_id)
    if post is None or post.get("reception"):
        return
    pend = post.get("pending") or []
    if any(not b.get("done") for b in pend):
        return
    picked = []
    for b in pend:
        for it in b.get("items") or []:
            si = _Fan(it["handle"]) if it.get("handle") else _resolve(it.get("from_id"))
            if si is not None:
                picked.append((si, it.get("text") or "", it.get("mood")))
    _settle_reception(post_id, poster_si, picked)


_RESUME_MAX_AGE_SECONDS = 2 * 24 * 3600


def resume_pending():
    """On save load: finish what a quit interrupted. Deliver saved comment
    batches that hadn't arrived yet, settle posts whose comments are all
    in, and run the comment pass for recent posts that never got one (the
    AI call was in flight, or failed). Older posts are left alone."""
    import time
    data = _load()
    if not data:
        return
    hh = _household_ids()
    now = time.time()
    resumed = 0
    _resume_slot[0] = 0
    longest = 0.0
    quiet_any = False
    for post in list(data["posts"]):
        if not post.get("is_player") or post.get("reception") or post.get("author_id") not in hh:
            continue
        try:
            age = now - datetime.datetime.fromisoformat(post.get("ts")).timestamp()
        except Exception:
            age = 0
        if age > _RESUME_MAX_AGE_SECONDS:
            continue
        poster_si = _resolve(post.get("author_id"))
        if poster_si is None:
            continue
        if post.get("pending"):
            if any(not b.get("done") for b in post["pending"]):
                with _lock:
                    post["quiet_resume"] = True
                    _save(data)
                quiet_any = True
            longest = max(longest, _schedule_pending(post["id"], poster_si, quiet=True))
        elif not post.get("pass_done"):
            _timer(random.uniform(20, 60), lambda pid=post["id"]: _generate_comments(pid))
        else:
            _timer(20.0, lambda pid=post["id"], s=poster_si: _settle_reception(pid, s, []))
        resumed += 1
    if quiet_any:
        _timer(longest + 45.0, _show_resume_digest)
    if resumed:
        _log(f"resume: picked up {resumed} post(s) a quit or failed AI call had interrupted")


def _deliver_batch(post_id, poster_si, batch):
    from . import notifications
    try:
        post = get_post(post_id)
        if post is None:
            return
        shown = []
        for replier_si, body, mood in batch:
            c = _add_comment(post_id, replier_si, body, to_id=poster_si.sim_id, mood=mood)
            if c is None:
                continue
            _add_inbox("comment", post_id, c["id"], poster_si.sim_id)
            _journal_comment(replier_si, poster_si, post, body)
            _apply_mood(poster_si, replier_si, mood)
            shown.append(f"{_name(replier_si)}: {body}")
        if not shown:
            return
        with _lock:
            pr = post.get("projection") or {}
            pr["delivered"] = int(pr.get("delivered") or 0) + 1
            data = _load()
            if data is not None:
                _save(data)
        if post.get("quiet_resume"):
            _digest["comments"] += len(shown)
            _digest["posts"].add(post_id)
            return
        if not _comment_popups():
            return
        _v, likes_now, comments_now, _g = _current_stats(post)
        title = ("Llamagram: new comment on your post" if len(shown) == 1
                 else f"Llamagram: {len(shown)} new comments on your post")
        notifications.show(title, "\n\n".join(shown) +
                           f"\n\n{_fmt(likes_now)} likes, {_fmt(comments_now)} comments so far")
    except Exception as e:
        _log(f"deliver batch for {post_id} raised: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 2. NPC posts
# ---------------------------------------------------------------------------

_NPC_POST_SYSTEM = """You write ONE social media post by {name}, a character in \
The Sims 4, for their friends to see. Write in {language}.

- Write in {name}'s own voice, about their own life: something recent, their \
work, family, mood, traits, hobbies, the season, or the time of day. Pick ONE \
thing.
- It's a post to their whole feed, not a message to anyone. Don't address the \
player or any one person.
- One to three short sentences, like a real post. Casual. At most one or two \
hashtags, and only if it fits the sim.
- Don't reveal things they wouldn't share publicly: no pregnancy that isn't \
visibly showing yet, no secrets from their bio, nothing about other people's \
private business.
- Don't repeat the topics of their recent posts listed below.
- Output only the post text."""


def _recent_posts_by(author_id, n=3):
    data = _load()
    if not data:
        return []
    mine = [p for p in data["posts"] if p.get("author_id") == str(author_id)]
    return mine[-n:]


def generate_npc_post(callback=None, author_si=None, viewer_si=None, output=None):
    """A friend of the household posts about their own life; the viewer
    (a household sim who follows them) sees it with Comment / Scroll past."""
    from . import phone
    if not enabled():
        if callback:
            callback(None, "social posts disabled")
        return
    if _save_id.get_current_save_id() is None:
        if callback:
            callback(None, "save not identified")
        return
    try:
        if author_si is None or viewer_si is None:
            contact = None
            for _ in range(4):
                viewer_si, contact = phone._pick_recipient_and_contact()
                if contact is not None and _can_post(contact.get("sim_info")):
                    break
                contact = None
            if contact is None:
                _log("npc post: no teen+ contact available")
                if callback:
                    callback(None, "no eligible poster")
                return
        else:
            contact = _contact_entry(viewer_si, author_si)
        author_si = contact["sim_info"]
        name = _name(author_si)
        blocks = []
        try:
            blocks.append(phone._describe_relationship(contact, recipient=viewer_si))
        except Exception:
            blocks.append(f"=== {name} ===\n{_sim_brief(author_si)}")
        ctx = _context_line()
        if ctx:
            blocks.append(ctx)
        recent = _recent_posts_by(author_si.sim_id)
        if recent:
            blocks.append("Their recent posts (pick a different topic):\n" +
                          "\n".join(f"- {_snippet(p.get('text'), 120)}" for p in recent))
        blocks.append(f"Write {_first(author_si)}'s post.")
        system = _NPC_POST_SYSTEM.format(name=name, language=_language())

        def _on_result(text, err):
            if err or not text:
                _log(f"npc post by {name} failed: {err}")
                if callback:
                    callback(None, err)
                return
            body = _clip(_clean_single(text), _MAX_POST_CHARS)
            post = _add_post(author_si, body, "friends", is_player=False, viewer_id=viewer_si.sim_id)
            if post is None:
                if callback:
                    callback(None, "save not identified")
                return
            _add_inbox("npc_post", post["id"], None, viewer_si.sim_id)
            try:
                from . import journal
                journal.add_entry("post", f"{name} posted (seen by {_first(viewer_si)}):\n{body}",
                                  sim_name=name, recipient_name=_first(viewer_si),
                                  sim_id=str(author_si.sim_id), recipient_id=str(viewer_si.sim_id))
            except Exception:
                pass
            _log(f"npc post {post['id']} by {name}, seen by {_name(viewer_si)}: {_snippet(body)}")
            if _post_popups():
                show_post_dialog(viewer_si, post["id"])
            if callback:
                callback(body, None)

        from . import api_client
        api_client.call_ai_async([{"role": "user", "content": "\n\n".join(blocks)}], system=system,
                                 use_fast_model=True, callback=_on_result)
    except Exception as e:
        _log(f"npc post raised: {type(e).__name__}: {e}")
        if callback:
            callback(None, str(e))


# ---------------------------------------------------------------------------
# 3. Threads: the player comments / replies, and that sim answers
# ---------------------------------------------------------------------------

_THREAD_SYSTEM = """You write {name}'s reply in the comments of a social media \
post in The Sims 4. {player} just commented to {name}. Stay in character as \
{name}. Write in {language}.

- One short social-media comment (a few words to two sentences), casual, in \
{name}'s voice, answering what {player} said.
- Fit the relationship: warm with people they like, cool or snarky with people \
they don't. A stranger doesn't know {player}.
- If one of you is a teen and the other isn't, nothing flirty or romantic, and \
never the "flirty" mood.
- Don't invent facts beyond the post, the thread, and the information given.

Output the comment, then OPTIONALLY one final line:
MOOD: <emotion>
Only include MOOD if the reply would genuinely change how {player} feels. Pick \
from: happy, sad, angry, confident, flirty, playful, energized, focused, inspired, \
embarrassed, tense, uncomfortable, bored, dazed."""


def _thread_lines(post, limit=8):
    out = []
    for c in (post.get("comments") or [])[-limit:]:
        out.append(f"{c.get('from_name')}: {c.get('text')}")
    return out


def respond_in_thread(post_id, target_si, player_si, player_text):
    """Store the player's comment to target on this post, then have
    target answer after a short delay."""
    from . import phone
    player_text = _clip(player_text, _MAX_COMMENT_CHARS)
    post = get_post(post_id)
    if post is None or not player_text or target_si is None or player_si is None:
        return
    mine = _add_comment(post_id, player_si, player_text, to_id=target_si.sim_id)
    if mine is None:
        return
    _journal_comment(player_si, target_si, post, player_text, verb="replied on")
    name, pname = _name(target_si), _first(player_si)
    blocks = []
    contact = _contact_entry(player_si, target_si)
    try:
        blocks.append(phone._describe_relationship(contact, recipient=player_si))
    except Exception:
        blocks.append(f"=== {name} ===\n{_sim_brief(target_si)}")
    who = "(the player's own post)" if post.get("is_player") and post.get("author_id") == str(player_si.sim_id) \
        else f"(posted by {post.get('author_name')})"
    author_si = _resolve(post.get("author_id"))
    if author_si is not None and author_si.sim_id != target_si.sim_id:
        # target is replying on someone else's post: give them the poster's
        # situation and how they relate to the poster (co-parents etc.).
        rel = _relation_text(_contact_entry(author_si, target_si), author_si)
        blocks.append(f"{name}'s relationship to the poster, {_name(author_si)}: {rel}")
        life = _poster_life_block(author_si)
        if life:
            blocks.append(f"{_first(author_si)}'s recent life (facts):\n{life}")
    blocks.append(f"THE POST {who}:\n{post.get('text')}")
    thread = _thread_lines(get_post(post_id) or post)
    if thread:
        blocks.append("COMMENTS SO FAR (oldest first):\n" + "\n".join(thread))
    blocks.append(f"{pname} just wrote to {name}: {player_text}\n\nWrite {name}'s reply.")
    system = _THREAD_SYSTEM.format(name=name, player=pname, language=_language())

    def _on_result(text, err):
        if err or not text:
            _log(f"thread reply from {name} failed: {err}")
            return
        try:
            # The moodlet lands on the player either way; the relationship
            # nudge only if they already know each other (contact=None
            # skips it in phone._apply_mood_from_text).
            known = not _is_stranger(player_si, target_si)
            cleaned = phone._apply_mood_from_text(text, recipient=player_si, is_incoming=True,
                                                  contact={"sim_info": target_si, "name": name} if known else None)
        except Exception:
            cleaned = text
        body = _clip(_clean_single(cleaned), _MAX_COMMENT_CHARS)
        if not body:
            return

        def _deliver():
            from . import notifications
            p = get_post(post_id)
            if p is None:
                return
            c = _add_comment(post_id, target_si, body, to_id=player_si.sim_id)
            if c is None:
                return
            _add_inbox("reply", post_id, c["id"], player_si.sim_id)
            _journal_comment(target_si, player_si, p, body, verb="replied on")
            if _comment_popups():
                notifications.show(f"Llamagram: {name} replied to your comment", body)

        _timer(random.uniform(*_THREAD_REPLY_DELAY), _deliver)

    from . import api_client
    api_client.call_ai_async([{"role": "user", "content": "\n\n".join(blocks)}], system=system,
                             use_fast_model=True, callback=_on_result)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

_INPUT_NAME = "message"


def _raw(text):
    from sims4.localization import LocalizationHelperTuning
    return LocalizationHelperTuning.get_raw_text(text)


def _show(dialog, icon_si):
    try:
        from distributor.shared_messages import IconInfoData
        if icon_si is not None:
            dialog.show_dialog(icon_override=IconInfoData(obj_instance=icon_si))
            return
    except Exception:
        pass
    dialog.show_dialog()


def _ok_cancel(anchor_si, title, text, ok, cancel, icon_si=None, on_ok=None, on_cancel=None):
    try:
        from ui.ui_dialog import UiDialogOkCancel
        lt, lx, lo, lc = _raw(title), _raw(text), _raw(ok), _raw(cancel)
        dialog = UiDialogOkCancel.TunableFactory().default(
            anchor_si, title=lambda *_a, **_k: lt, text=lambda *_a, **_k: lx,
            text_ok=lambda *_a, **_k: lo, text_cancel=lambda *_a, **_k: lc,
        )

        def _on_response(d):
            try:
                if getattr(d, "accepted", False):
                    if on_ok:
                        on_ok()
                elif on_cancel:
                    on_cancel()
            except Exception as e:
                _log(f"dialog response raised: {type(e).__name__}: {e}")

        dialog.add_listener(_on_response)
        _show(dialog, icon_si)
        return True
    except Exception as e:
        _log(f"ok/cancel dialog failed: {type(e).__name__}: {e}")
        return False


def _text_input(anchor_si, title, ok, icon_si, on_submit):
    try:
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel
        lt, lx, lo, lc = _raw(title), _raw(""), _raw(ok), _raw("Cancel")

        class _Dialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _INPUT_NAME
                ti.height = 100
                return msg

        dialog = _Dialog.TunableFactory().default(
            anchor_si, title=lambda *_a, **_k: lt, text=lambda *_a, **_k: lx,
            text_ok=lambda *_a, **_k: lo, text_cancel=lambda *_a, **_k: lc,
        )

        def _on_response(d):
            try:
                if not d.accepted:
                    return
                msg = (d.text_input_responses or {}).get(_INPUT_NAME, "").strip()
                if msg:
                    on_submit(msg)
            except Exception as e:
                _log(f"text input response raised: {type(e).__name__}: {e}")

        dialog.add_listener(_on_response)
        _show(dialog, icon_si)
        return True
    except Exception as e:
        _log(f"text input dialog failed: {type(e).__name__}: {e}")
        return False


def _picker(anchor_si, title, text, rows, on_pick, ok="Open", cancel="Close"):
    """rows: [(name, description)]; on_pick(index)."""
    try:
        from ui.ui_dialog_picker import UiItemPicker, BasePickerRow
        lt, lx, lo, lc = _raw(title), _raw(text), _raw(ok), _raw(cancel)
        dialog = UiItemPicker.TunableFactory().default(
            anchor_si, title=lambda *_a, **_k: lt, text=lambda *_a, **_k: lx,
            text_ok=lambda *_a, **_k: lo, text_cancel=lambda *_a, **_k: lc,
        )
        for attr in ("max_selectable", "min_selectable"):
            try:
                setattr(dialog, attr, 1)
            except Exception:
                pass
        for idx, (name, desc) in enumerate(rows):
            dialog.add_row(BasePickerRow(option_id=idx, name=_raw(name),
                                         row_description=_raw(desc), is_enable=True))

        def _on_response(d):
            try:
                if not d.accepted:
                    return
                picked = list(getattr(d, "picked_results", None) or ())
                if picked and picked[0] is not None and 0 <= picked[0] < len(rows):
                    on_pick(picked[0])
            except Exception as e:
                _log(f"picker response raised: {type(e).__name__}: {e}")

        dialog.add_listener(_on_response)
        dialog.show_dialog()
        return True
    except Exception as e:
        _log(f"picker failed: {type(e).__name__}: {e}")
        return False


def start_post_flow(poster_si):
    """Llamafone > Post: write the post, then pick who can see it."""
    from . import notifications
    if not enabled():
        notifications.show("Llamafone", "Llamagram is turned off (social_enabled in llamafone.cfg).")
        return
    if _save_id.get_current_save_id() is None:
        notifications.show("Llamafone", "Llamafone is off for this save until you save it once.")
        return

    def _on_text(text):
        rows = [("Friends", "People your sim knows."),
                ("Public", "Anyone in the world, including sims you've never met.")]
        _picker(poster_si, f"Who can see {_first(poster_si)}'s post?", _snippet(text, 160), rows,
                lambda i: player_post(poster_si, text, "public" if i == 1 else "friends"),
                ok="Post", cancel="Cancel")

    if not _text_input(poster_si, f"New Llamagram post as {_first(poster_si)}", "Next", poster_si, _on_text):
        notifications.show("Llamafone", "Couldn't open the post box. Use llama.post <text> in the cheat console.")


def show_post_dialog(viewer_si, post_id):
    """A friend's post, with Comment / Scroll past."""
    post = get_post(post_id)
    author_si = _resolve(post.get("author_id")) if post else None
    if post is None or author_si is None:
        return False
    body = post.get("text", "")
    thread = _thread_lines(post, limit=4)
    if thread:
        body += "\n\n" + "\n".join(thread)
    # Name the household sim who's seeing it -- they're the one who
    # comments (not necessarily the sim you're controlling).
    vf = _first(viewer_si)
    return _ok_cancel(
        viewer_si, f"{post.get('author_name')} posted · on {vf}'s feed", body, f"Comment as {vf}", "Scroll past",
        icon_si=author_si,
        on_ok=lambda: _text_input(viewer_si, f"{vf}: comment on {_first(author_si)}'s post", "Post", viewer_si,
                                  lambda t: respond_in_thread(post_id, author_si, viewer_si, t)),
    )


def _inbox_row(item, post, comment):
    kind = item.get("kind")
    hh_si = _resolve(item.get("household_id"))
    whose = f"{_first(hh_si)}'s" if hh_si is not None else "your"
    if kind == "npc_post":
        return (f"{post.get('author_name')} posted (on {whose} feed)", _snippet(post.get("text")))
    who = comment.get("from_name") if comment else "Someone"
    if kind == "reply":
        return (f"{who} replied to {whose} comment", _snippet(comment.get("text") if comment else ""))
    return (f"{who} commented on {whose} post", _snippet(comment.get("text") if comment else ""))


def open_inbox(anchor_si):
    """Llamafone > Notifications: newest first; unread marked. Opening the
    list marks what it shows as read."""
    from . import notifications
    if not enabled():
        notifications.show("Llamafone", "Llamagram is turned off (social_enabled in llamafone.cfg).")
        return
    data = _load()
    if data is None:
        notifications.show("Llamafone", "Llamafone is off for this save until you save it once.")
        return
    # This sim's notifications only -- each household member has their own
    # feed (Francesca's comments were showing up on Aksel's).
    hh = {str(anchor_si.sim_id)}
    entries = []
    for item in reversed(data["inbox"]):
        if item.get("household_id") not in hh:
            continue
        post = get_post(item.get("post_id"))
        if post is None:
            continue
        comment = _find_comment(post, item.get("comment_id")) if item.get("comment_id") else None
        entries.append((item, post, comment))
        if len(entries) >= 20:
            break
    latest = None
    for p in reversed(data["posts"]):
        if p.get("is_player") and p.get("author_id") == str(anchor_si.sim_id):
            latest = p
            break
    if not entries and latest is None:
        notifications.show("Llamagram", "Nothing yet. Post something from Llamafone > Llamagram Post, "
                                         "or wait for your friends to post.")
        return
    rows = []
    if latest is not None:
        rows.append((f"{_first(anchor_si)}'s latest post: {_stats_line(latest)}", _snippet(latest.get("text"))))
    for item, post, comment in entries:
        name, desc = _inbox_row(item, post, comment)
        rows.append((("New: " if not item.get("read") else "") + name, desc))
    with _lock:
        for item, _p, _c in entries:
            item["read"] = True
        _save(data)
    unread = sum(1 for r in rows if r[0].startswith("New: "))

    def _on_pick(i):
        if latest is not None:
            if i == 0:
                _open_own_post(anchor_si, latest["id"])
                return
            i -= 1
        item, post, comment = entries[i]
        _open_item(anchor_si, item, post, comment)

    header = f"{_first(anchor_si)}: {_fmt(followers_of(anchor_si))} followers"
    if fame_rank(anchor_si):
        header += f", {fame_rank(anchor_si)}-star celebrity"
    header += f". {unread} new." if unread else ". No new notifications."
    _picker(anchor_si, "Llamagram Notifications", header, rows, _on_pick)


def _open_own_post(anchor_si, post_id):
    """Your post: its stats and every comment, newest last."""
    post = get_post(post_id)
    if post is None:
        return
    r = post.get("reception") or {}
    body = '"' + _snippet(post.get("text"), 240) + '"\n\n' + _stats_line(post)
    if r.get("follower_delta"):
        d = int(r["follower_delta"])
        body += f", {'+' if d > 0 else '-'}{_fmt(abs(d))} followers"
    body += f" ({'public' if post.get('audience') == 'public' else 'friends only'})"
    thread = _thread_lines(post, limit=12)
    if thread and int(r.get("comments") or 0) > len(post.get("comments") or []):
        body += "\n\nTop comments:\n" + "\n".join(thread)
    else:
        body += "\n\n" + ("\n".join(thread) if thread else "No comments yet.")
    _ok_cancel(anchor_si, f"{_first(anchor_si)}'s post", body, "OK", "Back",
               on_cancel=lambda: open_inbox(anchor_si))


def _open_item(anchor_si, item, post, comment):
    """One notification: the post and its thread, with Reply / Comment."""
    household_si = _resolve(item.get("household_id")) or anchor_si
    if item.get("kind") == "npc_post":
        show_post_dialog(household_si, post["id"])
        return
    target_si = _resolve(comment.get("from_id")) if comment else None
    hf = _first(household_si)
    head = f"{hf}'s post" if post.get("author_id") == str(household_si.sim_id) else f"{post.get('author_name')}'s post"
    body = f"{head}: \"{_snippet(post.get('text'), 200)}\""
    if post.get("is_player"):
        body += f"\n{_stats_line(post)}"
    body += "\n\n" + "\n".join(_thread_lines(post, limit=8))
    if target_si is None:
        _ok_cancel(household_si, head, body, "OK", "Close")
        return
    _ok_cancel(
        household_si, f"{comment.get('from_name')}", body, f"Reply as {hf}", "Back", icon_si=target_si,
        on_ok=lambda: _text_input(household_si, f"{hf}: reply to {_first(target_si)}", "Post", household_si,
                                  lambda t: respond_in_thread(post["id"], target_si, household_si, t)),
        on_cancel=lambda: open_inbox(household_si),
    )


_PROMPT_WINDOW_DAYS = 3.0
_PROMPT_MAX_POSTS = 4


def _knows(poster_si, other_si):
    """Is `other` in the audience of the poster's friends-only posts?"""
    try:
        return str(other_si.sim_id) in _network_ids(poster_si)
    except Exception:
        return False


def format_for_prompt(contact_si, recipient_si):
    """'Recent social media' block for a call / text / reply between the
    contact (the AI's voice) and the household sim: posts either of them
    made in the last few in-game days that the contact would have seen,
    with the comments the two of them exchanged. Empty when there's
    nothing, or when social posts are off / no save is identified."""
    if not enabled() or contact_si is None or recipient_si is None:
        return ""
    data = _load()
    if not data or not data["posts"]:
        return ""
    try:
        from . import milestones
        now = milestones._now_sim_ticks()
        per_day = milestones._TICKS_PER_DAY
        rel_time = milestones._relative_sim_time
    except Exception:
        now, per_day, rel_time = None, None, None
    cid, rid = str(contact_si.sim_id), str(recipient_si.sim_id)
    cn, rn = _first(contact_si), _first(recipient_si)
    picked = []
    for p in reversed(data["posts"]):
        if len(picked) >= _PROMPT_MAX_POSTS:
            break
        author = p.get("author_id")
        if author not in (cid, rid):
            continue
        t = p.get("ticks")
        if now is not None and per_day and t is not None and (now - t) / per_day > _PROMPT_WINDOW_DAYS:
            continue
        if author == rid and p.get("audience") != "public" and not _knows(recipient_si, contact_si):
            continue  # friends-only post the contact isn't in the audience for
        if author == cid and p.get("viewer_id") not in (None, rid) and p.get("audience") != "public" \
                and not _knows(contact_si, recipient_si):
            continue
        picked.append(p)
    if not picked:
        return ""
    lines = [f"Recent social media between {cn} and {rn} (in-game, last few days):"]
    for p in picked:
        when = (rel_time(p.get("ticks"), now) if rel_time and p.get("ticks") is not None else None) or "recently"
        who = cn if p.get("author_id") == cid else rn
        aud = "publicly" if p.get("audience") == "public" else "to friends"
        stats = f" ({_stats_line(p)})" if p.get("reception") else ""
        lines.append(f"  - [{when}] {who} posted {aud}: \"{_snippet(p.get('text'), 160)}\"{stats}")
        between = [c for c in (p.get("comments") or []) if c.get("from_id") in (cid, rid)]
        for c in between[-3:]:
            speaker = cn if c.get("from_id") == cid else rn
            lines.append(f"      {speaker} commented: \"{_snippet(c.get('text'), 140)}\"")
        if p.get("author_id") == rid and not any(c.get("from_id") == cid for c in between):
            lines.append(f"      ({cn} saw it but didn't comment)")
        if p.get("author_id") == cid and p.get("viewer_id") == rid and not any(c.get("from_id") == rid for c in between):
            lines.append(f"      ({rn} saw it but didn't comment)")
    lines.append(f"  [{cn} can bring these up naturally if they fit -- \"saw your post\", a follow-up "
                 f"to a comment -- but shouldn't recite them.]")
    return "\n".join(lines)


def debug_summary(limit=8):
    """For llama.feed: the last few posts with comment counts."""
    data = _load()
    if data is None:
        return ["(no save identified -- Llamafone is dormant)"]
    out = [f"{len(data['posts'])} post(s), {sum(1 for i in data['inbox'] if not i.get('read'))} unread notification(s)"]
    for _sid, prof in (data.get("profiles") or {}).items():
        hist = (prof.get("history") or [])[-7:]
        gained = sum(h.get("delta", 0) for h in hist)
        out.append(f"  {prof.get('name')}: {_fmt(prof.get('followers') or 0)} followers "
                   f"({'+' if gained >= 0 else ''}{_fmt(gained)} over the last {len(hist)} post(s)), "
                   f"{len(prof.get('fans') or [])} named fan(s)")
    for p in data["posts"][-limit:]:
        out.append(f"  [{p.get('audience')}] {p.get('author_name')}: {_snippet(p.get('text'), 70)} "
                   f"({_stats_line(p)})")
    return out
