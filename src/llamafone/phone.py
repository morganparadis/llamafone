"""
Phone calls and texts -- generates AI-powered messages from relationship sims.
Uses the fast model for quick generation.

Calls show as modal phone dialogs with the caller's portrait (ring).
Texts show as phone dialogs with buzz.
Players can reply with llama.reply <message> to continue the conversation.
"""
import random
import threading

from . import api_client, sim_context, config, journal, notifications, moodlets, events, interactions, past_events, group_texts, contact_prefs


def _log_error(message):
    """Log a reply-chain error to Llamafone_Log.txt. Used in place of
    silent `except Exception: pass` in the reply/text-input dialog
    callbacks -- a swallowed NameError there is exactly what hid the
    v3.2.0 -> v3.3.1 reply-chain bug (single typo, no user-visible
    error, mod appeared frozen after the first reply)."""
    import os as _os, datetime as _dt
    try:
        path = _os.path.join(_os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [phone] {message}\n")
    except Exception:
        pass


def _contact_prefs_block(household_sim, contact_sim_info, contact_name):
    """Render the per-pair contact prefs (state + note) as a prompt
    block ready to append to the END of a user prompt. Empty string
    when nothing is set.

    Placed at the END of the prompt (not inside _describe_relationship)
    so the AI sees this as the most-recent context before writing --
    recency is doing real work here. A 'paused' state or a player note
    should be able to override friendly tone from earlier blocks."""
    try:
        household_id = getattr(household_sim, "sim_id", None) if household_sim else None
        household_name = household_sim.first_name if household_sim else None
        contact_id = getattr(contact_sim_info, "sim_id", None) if contact_sim_info else None
        if not household_id or not contact_id:
            return ""
        block = contact_prefs.format_for_prompt(
            household_id, contact_id,
            other_name=contact_name, household_name=household_name,
        )
        if not block:
            return ""
        # Wrap with a leading blank line + header so this section
        # visually breaks from whatever came before it.
        return f"\n\n{block}"
    except Exception:
        return ""


def _maybe_auto_prefs_from_message(household_sim_id, other_sim_id, other_name, text, source_label=""):
    """Reactively set contact prefs based on distance signals in a
    message. Scoped to a specific (household_sim, other_sim) pair --
    Alice muting her ex must NOT bleed into Bob's phone activity with
    that same ex.

    Wrapped in a try because auto-prefs is convenience -- it should
    never break the message flow if it errors."""
    try:
        if not household_sim_id or not other_sim_id or not text:
            return
        applied = contact_prefs.maybe_auto_apply(
            household_sim_id, other_sim_id, other_name or "them",
            text, source_label=source_label,
        )
        if not applied:
            return
        if applied == "muted":
            body = (
                f"Auto-muted {other_name} based on that message. "
                f"They won't send you unprompted calls or texts. "
                f"Phone > Social > Llamafone Settings > Manage contacts "
                f"to review or undo."
            )
        else:  # paused
            body = (
                f"Auto-paused {other_name} based on that message. "
                f"They'll contact you much less, and the AI knows you "
                f"asked for space. Phone > Social > Llamafone Settings "
                f"> Manage contacts to review or undo."
            )
        try:
            notifications.show("Llamafone", body)
        except Exception:
            pass
    except Exception as _e:
        _log_error(f"_maybe_auto_prefs_from_message failed: {type(_e).__name__}: {_e}")


# Conversations keyed by a (anchor_sim_id, contact_sim_id) TUPLE so:
#   - Concurrent chats between different household members and different
#     contacts don't overwrite each other.
#   - Adelheid texting Francesca while a Daniel<->Francesca thread is
#     ongoing no longer clobbers Daniel's slot -- both coexist.
# Each value: {"contact": contact_dict, "recipient": sim_info,
#              "history": [{"role": "them"|"you", "text": str}, ...]}
_conversations = {}
# Most-recent (anchor_id, contact_id) tuple -- fallback when no explicit
# reply signal exists. Was a bare recipient id in v3.3.x; now a tuple to
# match the _conversations key shape.
_last_active_key = None
# Set when the player clicks the Reply button on a phone dialog -- tells
# the next llama.reply which (anchor, contact) conversation to continue.
# Was a bare recipient id in v3.3.x; now a tuple so replies route to the
# right contact even when a different sim has texted more recently.
_pending_reply_key = None

# Pending reply-delay Timers. Each Timer fires _show_reply after the
# "sim is thinking" delay. We track them so we can cancel any that are
# still pending when the player quits to main menu and loads a different
# save -- a stale Timer firing in the new save's context would write into
# the wrong conversation. See save_id._on_save_loaded.
_active_timers = []
_active_timers_lock = threading.Lock()

# Ephemeral per-group runtime state -- lives in memory only, rebuilt on
# demand as rounds run. Persisted state (participants, briefing, history)
# lives in group_texts.py's JSON file. Keeping these separate means a
# save-switch or crash mid-round loses the round-in-progress state but
# NEVER loses the thread history.
#
# Shape: _group_runtime_state[group_id] = {
#     "round_num": int,
#     "in_progress": bool,          # True while a round is generating replies
#     "queued_player_msg": str|None,  # message queued while in_progress
# }
_group_runtime_state = {}
_group_runtime_lock = threading.Lock()

# Which group thread the reply button should route to (if the player hits
# reply on a group dialog). Set when a group dialog surfaces; used by
# _take_conversation_for_reply. None => reply routes to 1:1 as before.
_last_active_group_id = None


def _track_timer(timer):
    """Register a started Timer for later cancellation. Also reaps any
    already-fired Timers from the list so it doesn't grow unbounded."""
    with _active_timers_lock:
        _active_timers[:] = [t for t in _active_timers if t.is_alive()]
        _active_timers.append(timer)


def _cancel_all_timers():
    """Cancel every pending reply-delay Timer. Called from save_id on
    save load so a Timer queued before the switch doesn't fire against
    the new save's _conversations dict.

    Also clears the ephemeral group-text runtime state so a mid-round
    cascade queued in save A doesn't try to keep running in save B."""
    with _active_timers_lock:
        for t in _active_timers:
            try:
                t.cancel()
            except Exception:
                pass
        _active_timers.clear()
    try:
        with _group_runtime_lock:
            _group_runtime_state.clear()
    except Exception:
        pass


_REPLY_TEXT_INPUT_NAME = "reply_text"


def _format_group_names(names):
    """Render a list of first names as a natural-English roster:
      1 name: 'Sarah'
      2 names: 'Sarah and Bob'
      3+: 'Sarah, Bob, Alice, and Kate'
    Used in group-text dialog titles/bodies so the player sees WHO the
    group is composed of on every message and every reply prompt."""
    names = [n for n in (names or []) if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _group_roster_names(group_id):
    """Return the list of participant first names for a group_id, or
    [] if the group can't be resolved. Used by dialog titles + reply
    dialog subtitles so the player always sees the whole roster."""
    try:
        g = group_texts.get_group(group_id)
        if not g:
            return []
        # Prefer snapshot names (stable across sim_info reloads); fall
        # back to live sim_info lookup if snapshot is missing/stale.
        snap = list(g.get("participant_names") or [])
        p_ids = list(g.get("participant_sim_ids") or [])
        # If snapshot has enough entries, use it. Otherwise resolve fresh.
        if len(snap) >= len(p_ids):
            return snap
        out = list(snap)
        while len(out) < len(p_ids):
            sid = p_ids[len(out)]
            si = _resolve_sim_info(sid)
            out.append(si.first_name if si else "?")
        return out
    except Exception:
        return []


def _show_reply_input_dialog(caller_sim_info, anchor_sim, group_id=None):
    """
    Open a text-input dialog so the player can type a reply inline,
    instead of having to use the cheat console.
    On submit, calls generate_reply() with the typed text (or
    generate_group_reply if group_id is set -- reply-button routing
    for group threads).
    """
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel
        from distributor.shared_messages import IconInfoData

        other_name = ""
        try:
            other_name = caller_sim_info.first_name or ""
        except Exception:
            pass

        # Group threads: title + body call out that the reply fans out
        # to every group member, so the player never thinks they're
        # replying only to whoever just spoke. All active participants
        # get a chance to respond.
        if group_id:
            roster = _group_roster_names(group_id)
            roster_str = _format_group_names(roster) or "the group"
            title_str = f"Reply to group: {roster_str}"
            text_str = ""
        else:
            title_str = f"Reply to {other_name}" if other_name else "Reply"
            text_str = ""

        loc_title = LocalizationHelperTuning.get_raw_text(title_str)
        loc_text = LocalizationHelperTuning.get_raw_text(text_str)
        loc_send = LocalizationHelperTuning.get_raw_text("Send")
        loc_cancel = LocalizationHelperTuning.get_raw_text("Cancel")

        # UiDialogTextInputOkCancel needs build_msg to add a text_input field to
        # the protobuf. We subclass to inject one named field directly instead
        # of relying on the tuning system's text_inputs TunableTuple.
        class _ReplyInputDialog(UiDialogTextInputOkCancel):
            def on_text_input(self, text_input_name='', text_input=''):
                self.text_input_responses[text_input_name] = text_input
                return True

            def build_msg(self, text_input_overrides=None, additional_tokens=(), **kwargs):
                # Let the parent chain build the OK/Cancel buttons etc; it will
                # also iterate self.text_inputs but that's an empty TunableTuple
                # for our factory, so nothing gets added there. Then inject our
                # one named field directly into the protobuf.
                msg = super().build_msg(additional_tokens=additional_tokens, **kwargs)
                ti = msg.text_input.add()
                ti.text_input_name = _REPLY_TEXT_INPUT_NAME
                # Tall text area so replies have room to breathe.
                ti.height = 100
                return msg

        dialog = _ReplyInputDialog.TunableFactory().default(
            anchor_sim,
            text=lambda *_a, **_kw: loc_text,
            title=lambda *_a, **_kw: loc_title,
            text_ok=lambda *_a, **_kw: loc_send,
            text_cancel=lambda *_a, **_kw: loc_cancel,
        )

        def _on_input_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    return
                reply_text = (response_dialog.text_input_responses or {}).get(
                    _REPLY_TEXT_INPUT_NAME, ""
                ).strip()
                if not reply_text:
                    return
                if group_id:
                    # Group thread reply -- route to the group engine
                    # which handles round accounting, drop-off, and
                    # in-progress queueing.
                    generate_group_reply(reply_text, group_id=group_id)
                else:
                    # Pass caller_sim_info so the intent locks onto
                    # the specific (anchor, contact) pair -- prevents
                    # the "replied to Daniel but got Adelheid" bug
                    # when a different contact texted the same anchor
                    # after Daniel's message.
                    _mark_reply_intent(anchor_sim, caller_sim_info=caller_sim_info)
                    generate_reply(reply_text)
            except Exception as e:
                # Log instead of silent pass -- a swallowed NameError
                # here is what caused the reply chain to appear frozen
                # after the first exchange in v3.2.0.
                _log_error(f"_on_input_response raised: {type(e).__name__}: {e}")

        dialog.add_listener(_on_input_response)
        icon = IconInfoData(obj_instance=caller_sim_info) if caller_sim_info else None
        if icon is not None:
            dialog.show_dialog(icon_override=icon)
        else:
            dialog.show_dialog()
        return True
    except Exception:
        return False


def _show_phone_dialog(caller_sim_info, title, message, ring=True, recipient_sim_info=None, group_id=None):
    """
    Show a phone dialog with the caller's portrait and Reply/Dismiss buttons.
    Anchored to `recipient_sim_info` -- the sim the call/text is FOR.
    If recipient_sim_info is None, refuses to show and returns False; callers
    then fall back to a plain notification (no anchoring, no phone ring).

    If `group_id` is set, the Reply button routes to generate_group_reply
    for that group thread instead of the 1:1 generate_reply. Callers for
    1:1 texts/calls omit it and get identical behavior to prior versions.

    Why no fallback to active_sim_info: a household's currently-selected sim
    is often not the recipient. If the player has a toddler selected when a
    call arrives for the adult sim, falling back to the toddler makes the
    toddler's (non-clickable) phone "ring" and the message is lost. Every
    call site now passes recipient_sim_info explicitly; a None here means
    a caller bug worth surfacing rather than silently papering over.
    """
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog import UiDialogOkCancel, PhoneRingType
        from distributor.shared_messages import IconInfoData
        import services

        client = services.client_manager().get_first_client()
        if not client:
            return False

        anchor_sim = recipient_sim_info
        if not anchor_sim:
            _log_error("_show_phone_dialog: no recipient_sim_info -- refusing to show; caller should fall back to notifications.show")
            return False

        loc_text = LocalizationHelperTuning.get_raw_text(message)
        loc_title = LocalizationHelperTuning.get_raw_text(title)
        loc_reply = LocalizationHelperTuning.get_raw_text("Reply")
        loc_dismiss = LocalizationHelperTuning.get_raw_text("Dismiss")

        dialog = UiDialogOkCancel.TunableFactory().default(
            anchor_sim,
            text=lambda: loc_text,
            title=lambda: loc_title,
            text_ok=lambda: loc_reply,
            text_cancel=lambda: loc_dismiss,
        )
        dialog.phone_ring_type = PhoneRingType.RING if ring else PhoneRingType.BUZZ

        def _on_response(response_dialog):
            try:
                if not response_dialog.accepted:
                    return
                # Lock in which specific (anchor, contact) conversation
                # the next reply belongs to BEFORE opening the text-input
                # dialog. caller_sim_info is the contact whose message
                # this dialog is showing -- pinning both anchor + contact
                # ensures the reply routes to THIS thread even if another
                # sim texts the same anchor between Reply click and submit.
                if not group_id:
                    _mark_reply_intent(anchor_sim, caller_sim_info=caller_sim_info)
                if not _show_reply_input_dialog(caller_sim_info, anchor_sim, group_id=group_id):
                    # Fallback to the cheat-console path if the text dialog
                    # fails to construct for any reason.
                    import sims4.commands
                    other_name = ""
                    try:
                        other_name = caller_sim_info.first_name
                    except Exception:
                        pass
                    sims4.commands.output(
                        f"[Llamafone] Reply to {other_name} (as {anchor_sim.first_name}):", None
                    )
                    sims4.commands.output(
                        "[Llamafone]   llama.reply <your message>", None
                    )
            except Exception:
                pass

        dialog.add_listener(_on_response)
        dialog.show_dialog(icon_override=IconInfoData(obj_instance=caller_sim_info))
        return True
    except Exception:
        pass
    return False

_CALL_SYSTEM = """You write one side of a Sims 4 phone call — what the caller says to the \
player's sim. Stay in character as the caller. Write in {language}.

# Whose data is whose
The context describes the CALLER. Career, traits, mood, aspiration, world, interests — \
all theirs, not the player's. Never confuse them.

CRITICAL: You know almost NOTHING about the player's sim beyond the relationship facts \
listed. Do NOT invent the player's career, hobbies, interests, activities, or past. \
Do NOT ask "are you still into X" or "how's your job going at Y" unless X or Y appears \
in the relationship facts. The caller can talk about THEIR OWN career and interests — \
they cannot assume the player shares them.

# Voice
The caller's family role to the player, age stage, and traits define how they speak.

FAMILY ROLE OVERRIDES TRAITS. If the sender info lists a family role (Father, Mother, \
Son, Daughter, Brother, Sister, Spouse, Grandparent, Grandchild, etc.), the family \
dynamic dominates the voice. Traits only flavor it — they do not turn a parent into \
the player's drinking buddy.

Parent → child (you are the player's Father/Mother): you are calling your OWN KID. \
Warm parental tone, even if your traits are Outgoing/Adventurous/Cheerful. Open with \
"hey kiddo", "hey son/honey", or just the topic. NEVER open with "hey man", "what's up \
bro", "yo", "dude" — parents don't talk to their own children like peers.

Child → parent: respectful, familiar. "hey mom", "hey dad", "hi". Asking advice, \
checking in, sharing news.

Sibling → sibling: teasing, candid, casual, no formality.

Spouse → spouse: intimate, shorthand, may use pet names.

Grandparent → grandchild: dotes, fuller sentences, may ramble warmly.

If NO family role is listed the caller is a friend/coworker/acquaintance — peer-style \
openers are fine and family terms like "mom"/"dad"/"son"/"daughter" are FORBIDDEN.

Friendship tone — CRITICAL. The "How they feel about the player" line is the LAW. \
If past chat contradicts the current label, assume a falling-out happened since — \
CURRENT status overrides past tone. By tier:
- friends / close / best friends: warm, glad to be in touch
- friendly acquaintances: polite, normal
- barely know each other: hesitant, confused, off-balance. You met the player \
  once or twice and barely remember them. Calling them feels random even to you. \
  Lead with confusion: "Hey... is this still the right number?", "Sorry, this is \
  awkward — we met at [event], right?", "Hi, I don't know if you remember me but...". \
  Keep it short and a little stilted. NEVER warm, NEVER familiar. This tier does \
  NOT apply if a family role is listed — family always remembers family.
- have some negative history: cool, brief, no warmth
- actively dislike each other: cold, dismissive, may snipe; no warmth ANYWHERE
- enemies: OPENLY HOSTILE — cutting, snarky, dismissive, contemptuous. NEVER \
  apologizes, NEVER mends, NEVER says "rooting for you" / "happy for you" / \
  "always have been" / "I would never" / "I miss you" / "hope you're well". \
  An enemy responding to good news is sarcastic or competitive, not supportive.

Age (match the caller's age stage, but family role wins where they conflict):
- Teen: dramatic, slang, lowercase OK
- Young Adult: casual but articulate
- Adult: measured sentences, no youth slang ("yo", "bro", "dude"), proper capitalization
- Elder: nostalgic, formal, long-winded. ALWAYS proper capitalization and complete \
  sentences — NEVER lowercase, NEVER abbreviations, NEVER "lol"/"omg"/"tbh"/"ngl". \
  An elder texting like a 20-year-old breaks character even if their traits are \
  Geek/Bookworm/Tech-savvy — those are interests, not voice. Computer-literate elders \
  still use punctuation.

Traits add flavor on top (Hot-Headed rants, Goofball jokes, Snob condescends, Loner is terse). \
Traits do NOT override the family-role register OR the age register above. Geek/Bookworm/Genius \
do NOT make an elder text like a millennial; Hot-Headed doesn't make a child act like an adult.

# What to write
2-3 SHORT lines of dialogue, no speaker prefix or label. Plain dialogue lines only. \
NEVER have the caller introduce themselves by name ("Apollo here!", "It's Dad!") — \
caller ID exists; family and friends know who's calling. Just speak naturally as if \
the recipient already knows who they are. One topic. Your FIRST line must contain a \
specific, concrete piece of information or question — NEVER a vague observation about \
feelings, distance, time, or the relationship's vibe.

The topic should usually be about the CALLER'S OWN LIFE — their day, a recent thought, \
something they did/saw/want, a question for the player. INVENT concrete specifics: \
locations they went, objects they bought, food they ate, jobs they screwed up, \
dreams they had, opinions, weird encounters with strangers, conspiracy theories, \
emergencies, regrets, ambitions, bad ideas, good ideas, hot takes. \
Be specific (a llama, a §10,000 mistake, a weird painting in the basement, \
a coworker named Brad — generic role names for non-mutuals are fine).

Topic variety guide (rotate, don't keep picking the same kind):
1. Their own life right now (most common)
2. A specific question or favor they need
3. A confession or admission
4. A reaction to something that happened to them
5. An invitation or plan (respect the geography rule)
6. A wild idea, opinion, or hot take
7. Gossip about a mutual (USE SPARINGLY — only ~1 in 5 calls, not the default)
Occasional Simlish (Sul sul, Dag dag, Nooboo) is fine.

# Geography rule (STRICT)
Look at the caller's world vs the player's world (both listed in the context).
- SAME world: in-person plans are fine ("come over", "let's grab drinks", "I ran into X").
- DIFFERENT worlds: NEVER suggest casual in-person meetups ("come over", "stop by", \
  "let's hang out", "let's grab drinks"). NEVER claim to have "run into" or "bumped into" \
  the player. Long-distance is the default — frame everything as a phone call, text, video \
  chat, or a PLANNED future visit ("when I come visit next month"). Same rule applies to \
  mentions of mutual contacts: only "ran into X" / "saw X" / "bumped into X" / \
  "got talking with X" if X's listed world matches the CALLER's world EXACTLY. \
  Each mutual entry includes "lives in <world>" -- check that string against \
  the caller's world before claiming any in-person encounter. If they don't \
  match, no in-person encounter happened; only phone/text/online contact, or \
  a recent visit you can explicitly justify ("when I was in Sulani last week").
- HOUSEHOLD / CLOSE RELATIONSHIPS: if the caller lives in the same household as the player \
  (spouse, partner, parent/child, sibling all in the same household), or is the player's \
  romantic partner regardless of address, they NEVER "run into" the player by chance. \
  They see each other every day. Frame their messages as scheduled: planning the evening, \
  asking what they want for dinner, coordinating pickup, telling them something that just \
  happened. Same rule for mentions of mutual sims: don't say "ran into your dad" if your \
  dad is your husband — you're married to him. Cohabiting sims share a life, not a \
  coincidental encounter.
- "WHEN YOU'RE BACK" / "when you get home" / "when you return" / "when you're in town" \
  framing ONLY makes sense if a [CURRENT LOCATION: ...] tag is present saying the \
  recipient is traveling away from home. And "back" / "home" means BACK TO THEIR OWN \
  HOME WORLD, never back to the caller's world. The recipient is not "returning" or \
  "coming back" to wherever the caller is unless they share a home world. If there's \
  no [CURRENT LOCATION] tag, the recipient is already at home -- don't use return- \
  framing at all. If there IS a [CURRENT LOCATION] tag and you and the recipient are \
  in different home worlds, you're still not the destination they're returning to.

# Sims 4 time
Sims 4 runs much faster than real life. By default ONE in-game week equals \
ONE SEASON (Spring/Summer/Autumn/Winter), and a sim's adult life is roughly 30 sim days. \
So when the calendar block says "in 2 weeks", that's TWO IN-GAME SEASONS away, not the \
real-world feel of a casual fortnight. Pace references accordingly -- "next season", \
"a while away", "soon" sound more natural than counting weeks. If an event line includes \
a season label like "(in 2 weeks, Winter)", frame timing by the season rather than by \
the week count.

# Hard rules
- NO EMOJI. Plain text only -- no Unicode emoji, no emoticons (":)", "<3", etc), no decorative symbols. Local AI models like Ollama mangle emoji into garbage in the game UI, so the mod strips them out anyway. Skip them entirely.
- The caller and player are on GOOD TERMS unless friendship is negative or the journal \
  shows actual conflict. Never invent past conflict. \
  BANNED FRAMES — do not open with ANY remark about how long it's been since the last \
  contact, even if the journal is empty. This includes the literal phrases AND any \
  rewording with "since we", "in a while", "in forever", "in ages", "in a minute", \
  "we never", "we don't": \
  "it's been forever / ages / so long / a minute", \
  "long time no talk / see", \
  "it feels like forever since [X]", \
  "we haven't talked / spoken / hung out / caught up in [X]", \
  "been ages / forever / a while since we [verb]", \
  "we never talk anymore", "we should catch up", "let's catch up sometime", \
  "been meaning to catch up", "do a video call sometime". \
  ALSO BANNED — invented past-conflict frames: \
  "things got weird between us", "after what happened", "we left things off badly", \
  "I know it's been weird". \
  Rule of thumb: if your first line is ABOUT the gap in contact instead of a concrete \
  topic, scrap it and start with the actual reason. An empty journal means you just \
  haven't logged the recent contact, NOT that this is your first contact in months.
- Family relationships are NEVER romantic, regardless of romance score.
- No profanity or explicit content.
- Only name sims listed in the mutual contacts block. For others, use a role like \
  "my coworker", "a friend of mine".
- Only reference sims in age-appropriate contexts (teens at school, adults at work, etc.). \
  The [SEASON: ...] tag is mainly for CONSISTENCY — don't write school-related content if \
  it's Summer, or summer-vacation content in Winter. The season can come up occasionally \
  when it fits, but it should NOT be the topic of most messages.
- Adults don't treat children/toddlers as peers — only as kids in their own/family/friends' lives.
- Sims with the same last name are NOT automatically related or in the same household.
- Stay in character. Never acknowledge being an AI or claim missing information. Improvise.
- NEVER break frame with meta-commentary. Do NOT explain the situation, describe options, \
  point out contradictions in the context, ask the player clarifying questions, or comment \
  on what you're about to write. The player sees ONLY the message text you produce -- \
  anything else (explanations, "the timeline is unclear", "I need to know when X happened", \
  "here are three ways this could go") is broken output that ships to the player as a text/call. \
  If the context seems contradictory, pick ONE interpretation silently and write ONE plausible \
  in-character message. When BOUNDARY STATE or PRIORITY STATE tags are present, trust them \
  as ground truth for the CURRENT relationship, regardless of what old history suggests.
- DECEASED sims (marked [DECEASED]) are GHOSTS. Never reference them as if alive. If they
  come up, talk about them in past tense, as memories, or as ghosts who appear sometimes.
  Don't suggest visiting them, calling them, hanging out with them, etc.
- PLAY ALONG with the player. If the player references things you don't have data for \
  (companies, hobbies, side businesses, mods, etc.), treat them as canon and \
  respond as if you know them. NEVER push back, correct, or say "I think you have me \
  confused" — the player is driving the story. If unsure, ask a curious in-character \
  question instead of disputing the premise.
- CALENDAR EVENTS are the EXCEPTION to "play along". The upcoming-events block is \
  ground truth pulled from the in-game calendar. Only reference events listed there, \
  and do NOT invent details the block doesn't state — who the event is for, who's \
  hosting, what's planned, where it is. If the block names an honoree ("in memory of \
  X", "for X and Y") use that exact framing; if it doesn't, stay vague ("the funeral \
  later", "the wedding next week") and do NOT guess whose it is from other context \
  like which mutual sim is deceased. \
  CRITICAL: calendar events are KNOWN to both sims — you live in the same world and \
  see the same calendar. NEVER deliver them as breaking news ("just heard X is coming \
  up", "did you know it's almost X"). Reference them as already-known: "you doing \
  anything for the holiday?", "you back in time for the wedding?", "see you at the \
  funeral later". Holidays and season changes especially are obviously known — \
  nobody "just hears" Summer is starting in 4 days.
- ASPIRATIONS are background context, NOT names sims would say out loud. The \
  aspiration string ("Renaissance Sim", "Track Knowledge", "Bestselling Author", \
  "Friend of the Animals", etc.) is a game-tuning label that real people would never \
  use in conversation. NEVER say "the [aspiration name] aspiration", "your [aspiration \
  name] aspiration", "working on Track Knowledge", "doing my Bestselling Author thing". \
  Instead, talk around it in natural language about the underlying goal -- "the book \
  you're working on", "your research", "how the studying is going", "your music \
  project", "the gardening business you're building". The aspiration tells you WHAT \
  the sim cares about; translate it into how a person would actually describe their \
  goals. Same goes for the CALLER's own aspiration -- never name-drop it as a label.
- MUTUAL PLAUSIBILITY -- if you're tempted to ask the recipient whether a mutual would \
  be into your topic ("would Francesca want to look at this?", "is this too far out of \
  her wheelhouse?", "do you think Apollo would care about...?"), that's a tell that \
  the mutual does NOT plausibly fit the topic and you know it. In that case, just \
  don't bring them up at all -- pick a mutual whose traits/career/age actually fit, \
  or skip the gossip and stay on your own topic. Never advertise the bad fit by \
  hedging in front of the recipient.

# Output format (STRICT)
PLAIN TEXT ONLY. No markdown. No `**bold**`, no `*italics*`, no `_emphasis_`, no headings, \
no `---` separators, no labels like "Message 1:" or "Reply:". Just the spoken lines.

Format your response as:
<line 1>
<line 2>
<line 3, optional>

Just the spoken lines, then OPTIONALLY one final line:
MOOD: <emotion>

ONLY include the MOOD line if this call would *genuinely* change how \
the recipient feels — big news, an argument, a confession, a flirty \
escalation, a death in the family, etc. SKIP the MOOD line for routine \
check-ins, mundane updates, gossip, casual catching-up, or small talk. \
Most calls should NOT emit a MOOD line. \
If you do include it, pick from: happy, sad, angry, confident, flirty, \
playful, energized, focused, inspired, embarrassed, tense, uncomfortable, \
bored, dazed."""

_TEXT_SYSTEM = """You write text messages from a Sim in The Sims 4 to the player's sim. \
Stay in character as the sender. Write in {language}.

# Whose data is whose
The context describes the SENDER. Career, traits, mood, aspiration, world, interests — \
all theirs, not the player's. Never confuse them.

CRITICAL: You know almost NOTHING about the player's sim beyond the relationship facts \
listed. Do NOT invent the player's career, hobbies, interests, activities, or past. \
Do NOT ask "are you still into X" or "how's your job going at Y" unless X or Y appears \
in the relationship facts. The sender can talk about THEIR OWN career and interests — \
they cannot assume the player shares them.

# Voice
The sender's family role to the player, age stage, and traits define how they text.

FAMILY ROLE OVERRIDES TRAITS. If the sender info lists a family role (Father, Mother, \
Son, Daughter, Brother, Sister, Spouse, Grandparent, Grandchild, etc.), the family \
dynamic is the dominant voice — traits only flavor it, they do NOT make a parent text \
like a peer.

Parent → child (you are the player's Father/Mother): you are texting your OWN KID. \
Warm and parental, even if you have outgoing/adventurous/cheerful traits. Openers: \
"hey kiddo", "hey son", "hey honey", or just the topic. NEVER use peer-style openers \
like "hey man", "hey bro", "dude", "yo" — that is how friends text, not how a parent \
texts their own child. You may invite your kid on activities, share news from your \
life, give light advice, ask how they're doing. Don't be cringe-formal — just dad/mom.

Child → parent (you are the player's Son/Daughter): respectful, familiar. Openers: \
"hey mom", "hey dad", or "hi". Adults asking parents for advice or just checking in.

Sibling → sibling: teasing, candid, no formality. Inside jokes welcome.

Spouse → spouse: intimate, casual, shorthand. Pet names if traits fit.

Grandparent → grandchild: dotes, asks how they're doing, maybe references the old days \
or sends an embarrassingly long message. Older sims text in fuller sentences.

If NO family role is listed, the sender is a friend/coworker/acquaintance — and the \
peer-style "hey man" / "what's up" register is fine.

Friendship tone — CRITICAL. The "How they feel about the player" line is the LAW. \
If past chat contradicts the current label, assume a falling-out happened since — \
CURRENT status overrides past tone. By tier:
- friends / close / best friends: warm, glad to be in touch
- friendly acquaintances: polite, normal
- barely know each other: hesitant, confused, off-balance. You met the player \
  once or twice and barely remember them. Texting them feels random even to you. \
  Lead with confusion: "hey... sorry is this [player first name]?", "wait who is \
  this lol", "is this the right number? we met at [event] right?", "hi! I don't \
  know if you remember me but...". Short, stilted, a little awkward. NEVER warm, \
  NEVER familiar. This tier does NOT apply if a family role is listed — family \
  always remembers family.
- have some negative history: cool, brief, no warmth
- actively dislike each other: cold, dismissive, may snipe; no warmth ANYWHERE
- enemies: OPENLY HOSTILE — cutting, snarky, dismissive, contemptuous. NEVER \
  apologizes, NEVER mends, NEVER says "rooting for you" / "happy for you" / \
  "always have been" / "I would never" / "I miss you" / "hope you're well". \
  An enemy responding to good news is sarcastic or competitive, not supportive.

Age (match the sender's age stage, but family role wins where they conflict):
- Teen: lowercase, abbreviations, dramatic slang. "omggg no way" / "stoppp" / "dyinggg"
- Young Adult: casual but articulate. "hey are you free tonight?"
- Adult: complete sentences, proper capitalization, no youth slang. "Hi! Are you free this weekend?"
- Elder: formal, warm, sometimes long-winded. "Hello dear, I hope you're well." \
  ALWAYS proper capitalization and complete sentences — NEVER lowercase, NEVER \
  abbreviations, NEVER "lol"/"omg"/"tbh"/"ngl". An elder texting like a 20-year-old \
  breaks character even if their traits are Geek/Bookworm/Tech-savvy — those are \
  interests, not voice. Computer-literate elders still use punctuation.

Traits add flavor on top (Hot-Headed = caps, Gloomy = ellipses, Snob = condescending grammar, \
Goofball = playful, Romantic = soft language, Loner = terse, Evil = passive aggressive). Traits \
do NOT override the family-role register OR the age register above. Geek/Bookworm/Genius do NOT \
make an elder text like a millennial; Hot-Headed doesn't make a child act like an adult.

# What to write
1-2 SHORT messages, max 2 sentences each. One topic. Your FIRST message must contain a \
specific, concrete piece of information or question — NEVER a vague observation about \
feelings, distance, time, or the relationship's vibe.

The topic should usually be about the SENDER'S OWN LIFE — their day, a recent thought, \
something they did/saw/want, a question for the player. INVENT concrete specifics: \
locations they went, objects they bought, food they ate, jobs they screwed up, \
dreams they had, opinions, weird encounters with strangers, conspiracy theories, \
emergencies, regrets, ambitions, bad ideas, hot takes. \
Be specific (a llama, a §10,000 mistake, a weird painting in the basement, \
a coworker named Brad — generic role names for non-mutuals are fine).

Topic variety guide (rotate, don't keep picking the same kind):
1. Their own life right now (most common)
2. A specific question or favor they need
3. A confession or admission
4. A reaction to something that happened to them
5. An invitation or plan (respect the geography rule)
6. A wild idea, opinion, or hot take
7. Gossip about a mutual (USE SPARINGLY — only ~1 in 5 messages, not the default)

# Geography rule (STRICT)
Look at the sender's world vs the player's world (both listed in the context).
- SAME world: in-person plans are fine ("come over", "let's grab coffee", "I saw X").
- DIFFERENT worlds: NEVER suggest casual in-person meetups ("come over", "stop by", \
  "let's hang out"). NEVER claim to have "run into" the player. Frame everything as long- \
  distance — texts, video chats, social media, or a PLANNED future visit. Same rule for \
  mentions of mutuals: only "ran into X" / "saw X" / "bumped into X" if X's \
  listed world matches the SENDER's world EXACTLY. Each mutual entry includes \
  "lives in <world>" -- check that against the sender's world before claiming \
  any in-person encounter with the mutual.
- HOUSEHOLD / CLOSE RELATIONSHIPS: cohabiting sims (same household) and romantic partners \
  NEVER "run into" each other or "bump into" each other — they live shared lives. Frame \
  their texts as coordinating the day, asking what's for dinner, reacting to something \
  that just happened, or planning what to do next. Same rule for mutuals: a wife doesn't \
  "run into" her own husband.

# Sims 4 time
Sims 4 runs much faster than real life. By default ONE in-game week equals \
ONE SEASON (Spring/Summer/Autumn/Winter), and a sim's adult life is roughly 30 sim days. \
So when the calendar block says "in 2 weeks", that's TWO IN-GAME SEASONS away, not the \
real-world feel of a casual fortnight. Pace references accordingly -- "next season", \
"a while away", "soon" sound more natural than counting weeks. If an event line includes \
a season label like "(in 2 weeks, Winter)", frame timing by the season rather than by \
the week count.

# Hard rules
- NO EMOJI. Plain text only -- no Unicode emoji, no emoticons (":)", "<3", etc), no decorative symbols. Local AI models like Ollama mangle emoji into garbage in the game UI, so the mod strips them out anyway. Skip them entirely.
- The sender and player are on GOOD TERMS unless friendship is negative or the journal \
  shows actual conflict. Never invent past conflict. \
  BANNED FRAMES — do not open with ANY remark about how long it's been since the last \
  contact, even if the journal is empty. This includes the literal phrases AND any \
  rewording with "since we", "in a while", "in forever", "in ages", "in a minute", \
  "we never", "we don't": \
  "it's been forever / ages / so long / a minute", \
  "long time no talk / see", \
  "it feels like forever since [X]", \
  "we haven't talked / spoken / hung out / caught up in [X]", \
  "been ages / forever / a while since we [verb]", \
  "we never talk anymore", "we should catch up", "let's catch up sometime", \
  "been meaning to catch up", "do a video call sometime". \
  ALSO BANNED — invented past-conflict frames: \
  "things got weird between us", "after what happened", "we left things off badly", \
  "I know it's been weird". \
  Rule of thumb: if your first line is ABOUT the gap in contact instead of a concrete \
  topic, scrap it and start with the actual reason. An empty journal means you just \
  haven't logged the recent contact, NOT that this is your first contact in months.
- Family relationships are NEVER romantic, regardless of romance score.
- No profanity or explicit content.
- Only name sims listed in the mutual contacts block. For others, use a role like \
  "my coworker", "a friend of mine".
- Only reference sims in age-appropriate contexts (teens at school, adults at work, etc.). \
  The [SEASON: ...] tag is mainly for CONSISTENCY — don't write school-related content if \
  it's Summer, or summer-vacation content in Winter. The season can come up occasionally \
  when it fits, but it should NOT be the topic of most messages.
- Adults don't treat children/toddlers as peers — only as kids in their own/family/friends' lives.
- Sims with the same last name are NOT automatically related or in the same household.
- Stay in character. Never acknowledge being an AI or claim missing information. Improvise.
- NEVER break frame with meta-commentary. Do NOT explain the situation, describe options, \
  point out contradictions in the context, ask the player clarifying questions, or comment \
  on what you're about to write. The player sees ONLY the message text you produce -- \
  anything else (explanations, "the timeline is unclear", "I need to know when X happened", \
  "here are three ways this could go") is broken output that ships to the player as a text/call. \
  If the context seems contradictory, pick ONE interpretation silently and write ONE plausible \
  in-character message. When BOUNDARY STATE or PRIORITY STATE tags are present, trust them \
  as ground truth for the CURRENT relationship, regardless of what old history suggests.
- DECEASED sims (marked [DECEASED]) are GHOSTS. Never reference them as if alive. If they
  come up, talk about them in past tense, as memories, or as ghosts who appear sometimes.
  Don't suggest visiting them, calling them, hanging out with them, etc.
- PLAY ALONG with the player. If the player references things you don't have data for \
  (companies, hobbies, side businesses, mods, etc.), treat them as canon and \
  respond as if you know them. NEVER push back, correct, or say "I think you have me \
  confused" — the player is driving the story. If unsure, ask a curious in-character \
  question instead of disputing the premise.
- CALENDAR EVENTS are the EXCEPTION to "play along". The upcoming-events block is \
  ground truth pulled from the in-game calendar. Only reference events listed there, \
  and do NOT invent details the block doesn't state — who the event is for, who's \
  hosting, what's planned, where it is. If the block names an honoree ("in memory of \
  X", "for X and Y") use that exact framing; if it doesn't, stay vague ("the funeral \
  later", "the wedding next week") and do NOT guess whose it is from other context \
  like which mutual sim is deceased. \
  CRITICAL: calendar events are KNOWN to both sims — you live in the same world and \
  see the same calendar. NEVER deliver them as breaking news ("just heard X is coming \
  up", "did you know it's almost X"). Reference them as already-known: "you doing \
  anything for the holiday?", "you back in time for the wedding?", "see you at the \
  funeral later". Holidays and season changes especially are obviously known — \
  nobody "just hears" Summer is starting in 4 days.
- ASPIRATIONS are background context, NOT names sims would say out loud. The \
  aspiration string ("Renaissance Sim", "Track Knowledge", "Bestselling Author", \
  "Friend of the Animals", etc.) is a game-tuning label that real people would never \
  use in conversation. NEVER say "the [aspiration name] aspiration", "your [aspiration \
  name] aspiration", "working on Track Knowledge", "doing my Bestselling Author thing". \
  Instead, talk around it in natural language about the underlying goal -- "the book \
  you're working on", "your research", "how the studying is going", "your music \
  project", "the gardening business you're building". The aspiration tells you WHAT \
  the sim cares about; translate it into how a person would actually describe their \
  goals. Same goes for the CALLER's own aspiration -- never name-drop it as a label.
- MUTUAL PLAUSIBILITY -- if you're tempted to ask the recipient whether a mutual would \
  be into your topic ("would Francesca want to look at this?", "is this too far out of \
  her wheelhouse?", "do you think Apollo would care about...?"), that's a tell that \
  the mutual does NOT plausibly fit the topic and you know it. In that case, just \
  don't bring them up at all -- pick a mutual whose traits/career/age actually fit, \
  or skip the gossip and stay on your own topic. Never advertise the bad fit by \
  hedging in front of the recipient.

# Output format (STRICT)
PLAIN TEXT ONLY. No markdown. No `**bold**`, no `*italics*`, no `_emphasis_`, no headings, \
no `---` separators, no labels like "Message 1:" or "Text 2:". Just the messages.

Format your response as:
<message 1 text>
<message 2 text, optional, on its own line>

Just the messages, then OPTIONALLY one final line:
MOOD: <emotion>

ONLY include the MOOD line if this text would *genuinely* change how \
the recipient feels — big news, an argument, a confession, a flirty \
escalation, etc. SKIP the MOOD line for routine check-ins, mundane \
updates, gossip, casual catching-up, or small talk. Most texts should \
NOT emit a MOOD line. \
If you do include it, pick from: happy, sad, angry, confident, flirty, \
playful, energized, focused, inspired, embarrassed, tense, uncomfortable, \
bored, dazed."""

# NOTE: _CALL_SYSTEM, _TEXT_SYSTEM, and _REPLY_SYSTEM share most rule sections
# (voice, friendship, geography, Sims 4 time, hard rules, calendar events,
# aspiration name-drop ban, mutual plausibility, etc.). When you add or
# change a rule in one, mirror it in the other two unless the rule is
# genuinely initiator-only (topic variety / "invent specifics" / "first
# line must be concrete" don't apply to reactive replies).
_REPLY_SYSTEM = """You write a Sim's reply to a message from the player's sim in The Sims 4. \
Stay in character as {other_name} replying to {main_name}. Write in {language}.

# Whose data is whose
The context describes {other_name} (the REPLIER). Career, traits, mood, aspiration, world, \
interests — all theirs, not the player's. Never confuse them.

You know almost NOTHING about {main_name}'s sim beyond the relationship facts listed and what \
{main_name} just said. Do NOT invent {main_name}'s career, hobbies, interests, activities, or \
past. {other_name} can talk about THEIR OWN career and interests — they cannot assume {main_name} \
shares them.

# Voice
{other_name}'s family role to {main_name}, age stage, and traits define how they reply.

FAMILY ROLE OVERRIDES TRAITS. If the context lists a family role (Father, Mother, Son, \
Daughter, Brother, Sister, Spouse, Grandparent, Grandchild, etc.), the family dynamic is \
the dominant voice — traits only flavor it, they do NOT make a parent reply like a peer.

Parent → child (you are {main_name}'s Father/Mother): you are replying to your OWN KID. \
Warm and parental, even if you have outgoing/adventurous/cheerful traits. NEVER use \
peer-style openers like "hey man", "hey bro", "dude", "yo" — that is how friends text, \
not how a parent texts their own child. Just dad/mom, no peer register.

Child → parent (you are {main_name}'s Son/Daughter): respectful, familiar.

Sibling → sibling: teasing, candid, no formality. Inside jokes welcome.

Spouse → spouse: intimate, casual, shorthand. Pet names if traits fit.

Grandparent → grandchild: dotes, fuller sentences, may ramble warmly.

If NO family role is listed, {other_name} is a friend/coworker/acquaintance — peer-style \
"hey", "what's up" register is fine and family terms like "mom"/"dad"/"son"/"daughter" \
are FORBIDDEN.

Friendship tone — CRITICAL. The "How they feel about the player" line is the LAW. \
If past chat contradicts the current label, assume a falling-out happened since — \
CURRENT status overrides past tone. By tier:
- friends / close / best friends: warm, glad to hear from them
- friendly acquaintances: polite, normal
- barely know each other: HESITANT, CONFUSED. You met {main_name} once or twice and \
  barely remember them. Getting a text from them feels random even to you. Lead with \
  confusion: "wait who is this lol", "sorry — is this {main_name}? remind me where we \
  met", "hi! I don't know if you remember me but...". Short, stilted, awkward. NEVER \
  warm, NEVER familiar. This tier does NOT apply if a family role is listed — family \
  always remembers family.
- have some negative history: cool, brief, no warmth
- actively dislike each other: cold, dismissive, may snipe; no warmth ANYWHERE
- enemies: OPENLY HOSTILE — cutting, snarky, dismissive, contemptuous. NEVER apologizes, \
  NEVER mends, NEVER says "rooting for you" / "happy for you" / "always have been" / \
  "I would never" / "I miss you" / "hope you're well". An enemy responding to good news \
  is sarcastic or competitive, not supportive.

Age (match {other_name}'s age stage, but family role wins where they conflict):
- Teen: lowercase, abbreviations, dramatic slang. "omggg no way" / "stoppp"
- Young Adult: casual but articulate. "hey are you free tonight?"
- Adult: complete sentences, proper capitalization, no youth slang. "Hi! Are you free this weekend?"
- Elder: formal, warm, sometimes long-winded. "Hello dear, I hope you're well." \
  ALWAYS proper capitalization and complete sentences — NEVER lowercase, NEVER \
  abbreviations, NEVER "lol"/"omg"/"tbh"/"ngl". An elder texting like a 20-year-old \
  breaks character even if their traits are Geek/Bookworm/Tech-savvy — those are \
  interests, not voice. Computer-literate elders still use punctuation.

Traits add flavor on top (Hot-Headed = caps, Gloomy = ellipses, Snob = condescending \
grammar, Goofball = playful, Romantic = soft language, Loner = terse, Evil = passive \
aggressive). Traits do NOT override the family-role register OR the age register above. \
Geek/Bookworm/Genius do NOT make an elder text like a millennial; Hot-Headed doesn't \
make a child act like an adult.

# What to write
1-2 SHORT messages, max 2 sentences each. React authentically to what {main_name} just \
said — no generic responses. Stay reactive (don't pivot to your own unrelated topic \
unless what {main_name} said genuinely doesn't warrant a substantive reply).

# Geography rule (STRICT)
Look at {other_name}'s world vs {main_name}'s world (both listed in the context).
- SAME world: in-person plans are fine ("come over", "let's grab coffee", "I saw X").
- DIFFERENT worlds: NEVER suggest casual in-person meetups ("come over", "stop by", \
  "let's hang out"). NEVER claim to have "run into" {main_name}. Frame everything as \
  long-distance — texts, video chats, social media, or a PLANNED future visit. Same \
  rule for mentions of mutuals: only "ran into X" / "saw X" / "bumped into X" if X's \
  listed world matches {other_name}'s world EXACTLY. Each mutual entry includes \
  "lives in <world>" -- check that against {other_name}'s world before claiming any \
  in-person encounter with the mutual.
- HOUSEHOLD / CLOSE RELATIONSHIPS: cohabiting sims (same household) and romantic partners \
  NEVER "run into" each other or "bump into" each other — they live shared lives. Frame \
  their replies as coordinating the day, asking what's for dinner, reacting to something \
  that just happened, or planning what to do next. Same rule for mutuals: a wife doesn't \
  "run into" her own husband.

# Sims 4 time
Sims 4 runs much faster than real life. By default ONE in-game week equals \
ONE SEASON (Spring/Summer/Autumn/Winter), and a sim's adult life is roughly 30 sim days. \
So when the calendar block says "in 2 weeks", that's TWO IN-GAME SEASONS away, not the \
real-world feel of a casual fortnight. Pace references accordingly -- "next season", \
"a while away", "soon" sound more natural than counting weeks. If an event line includes \
a season label like "(in 2 weeks, Winter)", frame timing by the season rather than by \
the week count.

# Hard rules
- NO EMOJI. Plain text only -- no Unicode emoji, no emoticons (":)", "<3", etc), no \
  decorative symbols. Local AI models like Ollama mangle emoji into garbage in the game \
  UI, so the mod strips them out anyway. Skip them entirely.
- {other_name} and {main_name} are on GOOD TERMS unless friendship is negative or the \
  journal shows actual conflict. Never invent past conflict. \
  BANNED FRAMES — do not open with ANY remark about how long it's been since the last \
  contact, even if the journal is empty. This includes the literal phrases AND any \
  rewording with "since we", "in a while", "in forever", "in ages", "in a minute", \
  "we never", "we don't": \
  "it's been forever / ages / so long / a minute", \
  "long time no talk / see", \
  "it feels like forever since [X]", \
  "we haven't talked / spoken / hung out / caught up in [X]", \
  "been ages / forever / a while since we [verb]", \
  "we never talk anymore", "we should catch up", "let's catch up sometime", \
  "been meaning to catch up", "do a video call sometime". \
  ALSO BANNED — invented past-conflict frames: \
  "things got weird between us", "after what happened", "we left things off badly", \
  "I know it's been weird". \
  Rule of thumb: if your first line is ABOUT the gap in contact instead of replying to \
  {main_name}'s actual message, scrap it and start over. An empty journal means recent \
  contact just wasn't logged, NOT that this is your first contact in months.
- Family relationships are NEVER romantic, regardless of romance score.
- No profanity or explicit content.
- Only name sims listed in the mutual contacts block. For others, use a role like \
  "my coworker", "a friend of mine".
- Only reference sims in age-appropriate contexts (teens at school, adults at work, etc.). \
  The [SEASON: ...] tag is mainly for CONSISTENCY — don't write school-related content if \
  it's Summer, or summer-vacation content in Winter. The season can come up occasionally \
  when it fits, but it should NOT be the topic of most replies.
- Adults don't treat children/toddlers as peers — only as kids in their own/family/friends' lives.
- Sims with the same last name are NOT automatically related or in the same household.
- Stay in character. Never acknowledge being an AI or claim missing information. Improvise.
- NEVER break frame with meta-commentary. Do NOT explain the situation, describe options, \
  point out contradictions in the context, ask the player clarifying questions, or comment \
  on what you're about to write. The player sees ONLY the message text you produce -- \
  anything else (explanations, "the timeline is unclear", "I need to know when X happened", \
  "here are three ways this could go") is broken output that ships to the player as a text/call. \
  If the context seems contradictory, pick ONE interpretation silently and write ONE plausible \
  in-character message. When BOUNDARY STATE or PRIORITY STATE tags are present, trust them \
  as ground truth for the CURRENT relationship, regardless of what old history suggests.
- DECEASED sims (marked [DECEASED]) are GHOSTS. Never reference them as if alive. If they \
  come up, talk about them in past tense, as memories, or as ghosts who appear sometimes. \
  Don't suggest visiting them, calling them, hanging out with them, etc.
- PLAY ALONG with {main_name}. If {main_name} references things you don't have data for \
  (companies, hobbies, side businesses, mods, etc.), treat them as canon and respond as \
  if you know them. NEVER push back, correct, or say "I think you have me confused" — \
  the player is driving the story. If unsure, ask a curious in-character question \
  instead of disputing the premise.
- CALENDAR EVENTS are the EXCEPTION to "play along". The upcoming-events block is ground \
  truth pulled from the in-game calendar. Only reference events listed there, and do NOT \
  invent details the block doesn't state — who the event is for, who's hosting, what's \
  planned, where it is. If the block names an honoree ("in memory of X", "for X and Y") \
  use that exact framing; if it doesn't, stay vague ("the funeral later", "the wedding \
  next week") and do NOT guess whose it is from other context like which mutual sim is \
  deceased. CRITICAL: calendar events are KNOWN to both sims — you live in the same \
  world and see the same calendar. NEVER deliver them as breaking news ("just heard X \
  is coming up", "did you know it's almost X"). Reference them as already-known: "you \
  doing anything for the holiday?", "see you at the funeral later". Holidays and season \
  changes especially are obviously known — nobody "just hears" Summer is starting in 4 days.
- ASPIRATIONS are background context, NOT names sims would say out loud. The aspiration \
  string ("Renaissance Sim", "Track Knowledge", "Bestselling Author", "Friend of the \
  Animals", etc.) is a game-tuning label that real people would never use in \
  conversation. NEVER say "the [aspiration name] aspiration", "your [aspiration name] \
  aspiration", "working on Track Knowledge", "doing my Bestselling Author thing". \
  Instead, talk around it in natural language about the underlying goal -- "the book \
  you're working on", "your research", "how the studying is going", "your music \
  project", "the gardening business you're building". Same goes for {other_name}'s own \
  aspiration -- never name-drop it as a label.
- MUTUAL PLAUSIBILITY -- if you're tempted to ask {main_name} whether a mutual would be \
  into your topic ("would Apollo even care about this?", "is this too far out of her \
  wheelhouse?"), that's a tell that the mutual does NOT plausibly fit and you know it. \
  In that case, just don't bring them up at all -- pick a mutual whose traits/career/age \
  actually fit, or skip the gossip entirely. Never advertise the bad fit by hedging in \
  front of {main_name}.

# Output format (STRICT)
PLAIN TEXT ONLY. No markdown. No `**bold**`, no `*italics*`, no `_emphasis_`, no \
headings, no `---` separators, no labels like "Message 1:" or "Reply:". Just the messages.

Format your response as:
<message 1 text>
<message 2 text, optional, on its own line>
<message 3 text, optional, on its own line>

Just the messages, then OPTIONALLY one final line:
MOOD: <emotion>

ONLY include the MOOD line if this reply would *genuinely* change how {main_name}'s sim \
feels — big news, an argument, a confession, a flirty escalation, etc. SKIP the MOOD \
line for routine check-ins, mundane updates, gossip, casual catching-up, or small talk. \
Most replies should NOT emit a MOOD line. If you do include it, pick from: happy, sad, \
angry, confident, flirty, playful, energized, focused, inspired, embarrassed, tense, \
uncomfortable, bored, dazed."""




# Ages eligible to receive phone calls and texts (teen and above)
_PHONE_ELIGIBLE_AGES = ("TEEN", "YOUNGADULT", "YOUNG_ADULT", "ADULT", "ELDER")

# Moods strong enough that applying a moodlet from a *reply* (not just
# an unsolicited incoming) still feels earned. Unsolicited incoming
# calls/texts apply moodlets for any mood that has a buff available.
_CHARGED_MOODS = frozenset({"sad", "angry", "flirty", "embarrassed",
                            "tense", "uncomfortable", "dazed"})

# Cooldown safety net: don't apply more than one moodlet per sim within
# this window of real-world seconds, even if the LLM emits MOOD on
# consecutive texts. Keeps stacking under control.
_MOODLET_COOLDOWN_SECONDS = 30 * 60  # 30 minutes
_last_moodlet_at = {}  # sim_id -> unix timestamp


def _moodlet_on_cooldown(sim_info):
    import time
    try:
        sid = getattr(sim_info, "sim_id", None)
        if sid is None:
            return False
        last = _last_moodlet_at.get(sid)
        return last is not None and (time.time() - last) < _MOODLET_COOLDOWN_SECONDS
    except Exception:
        return False


def _mark_moodlet_applied(sim_info):
    import time
    try:
        sid = getattr(sim_info, "sim_id", None)
        if sid is not None:
            _last_moodlet_at[sid] = time.time()
    except Exception:
        pass


def _refresh_milestones_for(contact, recipient_sim):
    """Run a targeted milestone scan on just the two sims about to appear
    in the prompt -- catches in-game events (job quit, divorce, etc.) that
    happened since the last full scan."""
    try:
        from . import milestones as _milestones
        sims = []
        ci = contact.get("sim_info") if isinstance(contact, dict) else None
        if ci is not None:
            sims.append(ci)
        if recipient_sim is not None:
            sims.append(recipient_sim)
        if sims:
            _milestones.scan_sims(sims)
    except Exception:
        pass


def _apply_mood_from_text(text, recipient=None, is_incoming=False):
    """Extract the MOOD: tag and apply the matching moodlet, with three gates:
      1. LLM only emits MOOD when the message is genuinely impactful
         (instructed via prompt -- not every text gets one).
      2. is_incoming=False (reply): only apply if mood is in _CHARGED_MOODS.
      3. Per-sim cooldown so back-to-back messages don't stack moodlets.
    """
    clean_text, mood = moodlets.extract_mood_tag(text)
    if mood and recipient is not None:
        try:
            should_apply = is_incoming or mood in _CHARGED_MOODS
            if should_apply and not _moodlet_on_cooldown(recipient):
                ok = moodlets.apply_mood(
                    recipient, mood,
                    reason="from incoming text/call" if is_incoming else "from reply",
                )
                if ok:
                    _mark_moodlet_applied(recipient)
        except Exception:
            pass
    return clean_text


# Personality traits that slow down or speed up text replies. Used by
# _calculate_reply_delay to make texting feel realistic for each sim.
_SLOW_REPLY_TRAITS = ("lazy", "loner", "gloomy", "snob", "unflirty", "perfectionist")
_FAST_REPLY_TRAITS = ("active", "outgoing", "cheerful", "goofball", "romantic",
                      "lovestruck", "hot_headed", "hotheaded")


def _calculate_reply_delay(contact):
    """How long (in seconds) the sim should 'think' before replying to a
    player-initiated text. Adjusts the configured base range by friendship
    closeness and personality traits. Returns 0 if delays are disabled."""
    try:
        if not config.get_reply_delay_enabled():
            return 0
    except Exception:
        return 0

    base_min = max(1, config.get_reply_delay_min_seconds())
    base_max = max(base_min, config.get_reply_delay_max_seconds())
    delay = random.randint(base_min, base_max)

    # Closer relationships reply faster; hostile ones drag.
    friendship = contact.get("friendship") or 0
    if friendship >= 75:
        delay = int(delay * 0.5)
    elif friendship >= 45:
        delay = int(delay * 0.7)
    elif friendship < -70:
        delay = int(delay * 2.0)
    elif friendship < 0:
        delay = int(delay * 1.5)

    # Trait modifiers — pulled from the contact's sim_info if we have it.
    sim_info = contact.get("sim_info")
    if sim_info is not None:
        try:
            trait_names = [t.lower().replace(" ", "").replace("-", "_")
                           for t in sim_context.get_sim_traits(sim_info, limit=10)]
            if any(t in trait_names for t in _SLOW_REPLY_TRAITS):
                delay = int(delay * 1.4)
            if any(t in trait_names for t in _FAST_REPLY_TRAITS):
                delay = int(delay * 0.7)
        except Exception:
            pass

    # Floor at 5s so even best-friend texts feel like real typing, not psychic.
    return max(5, delay)


# Non-human species names to reject. We check by suffix so this works with
# enum representations like SpeciesType.LARGE_DOG, Species.LARGE_DOG, etc.
_NON_HUMAN_SPECIES_SUFFIXES = (
    "LARGE_DOG", "SMALL_DOG", "DOG", "CAT", "HORSE", "FOX",
)


def _is_human_sim(sim_info):
    """Return True if this is a human sim (not a dog, cat, horse, fox).
    Sims 4 species enum repr varies across versions (SpeciesType.HUMAN vs
    Species.HUMAN vs SpeciesExtended.HUMAN), so we check by SUFFIX instead
    of an exact match. We blocklist known pet suffixes rather than
    allowlisting HUMAN, so anything unrecognized defaults to allowed
    (safer for old saves and edge cases)."""
    try:
        species = getattr(sim_info, "species", None)
        if species is None:
            return True
        species_str = str(species).upper().replace(" ", "")
        # If we recognize this as a non-human species, reject.
        for suffix in _NON_HUMAN_SPECIES_SUFFIXES:
            if species_str.endswith(suffix):
                return False
        return True
    except Exception:
        return True


def _is_phone_eligible(sim_info):
    """Return True if a sim is human and old enough to use a phone (teen+)."""
    try:
        if not _is_human_sim(sim_info):
            return False
        age_str = str(getattr(sim_info, "age", "")).replace("Age.", "").upper().replace(" ", "")
        return age_str in _PHONE_ELIGIBLE_AGES
    except Exception:
        return False


def _log_household_inspection():
    """Dump the active household's sims with age/species so we can diagnose
    why no eligible recipients were found. Called only when the picker fails."""
    try:
        import os, datetime, services
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hh = services.active_household()
        with open(path, "a", encoding="utf-8") as f:
            if not hh:
                f.write(f"[{ts}] [phone] _pick_recipient_sim: no active household.\n")
                return
            sims = list(hh.sim_info_gen())
            f.write(f"[{ts}] [phone] _pick_recipient_sim: scanned household with {len(sims)} sims:\n")
            for si in sims:
                try:
                    name = f"{si.first_name} {si.last_name}".strip()
                    age_repr = repr(getattr(si, "age", None))
                    species_repr = repr(getattr(si, "species", None))
                    eligible = _is_phone_eligible(si)
                    f.write(f"[{ts}] [phone]   - {name}: age={age_repr}, species={species_repr}, eligible={eligible}\n")
                except Exception as inner:
                    f.write(f"[{ts}] [phone]   - <error reading sim>: {type(inner).__name__}: {inner}\n")
    except Exception:
        pass


def _pick_recipient_sim():
    """
    Pick a random teen+ household member to receive an incoming call/text.
    The protagonist is included in the pool. Returns a sim_info or None.
    """
    try:
        import services, random as _random
        hh = services.active_household()
        if not hh:
            # Fallback to protagonist
            main = sim_context.get_main_sim_info()
            if main and _is_phone_eligible(main):
                return main
            _log_household_inspection()
            return None
        eligible = []
        for si in hh.sim_info_gen():
            if _is_phone_eligible(si):
                eligible.append(si)
        if not eligible:
            _log_household_inspection()
            return None
        return _random.choice(eligible)
    except Exception:
        # Fallback: protagonist or active sim
        main = sim_context.get_main_sim_info()
        if main and _is_phone_eligible(main):
            return main
        active = sim_context.get_active_sim()
        if active and active.sim_info and _is_phone_eligible(active.sim_info):
            return active.sim_info
        return None


def _eligible_recipients():
    """Return the full list of teen+ human sims in the active household,
    shuffled so iteration order is unbiased. Used by
    _pick_recipient_and_contact when one recipient has no eligible contacts
    and we want to try a different household member."""
    try:
        import services, random as _random
        hh = services.active_household()
        if not hh:
            main = sim_context.get_main_sim_info()
            return [main] if (main and _is_phone_eligible(main)) else []
        eligible = [si for si in hh.sim_info_gen() if _is_phone_eligible(si)]
        _random.shuffle(eligible)
        return eligible
    except Exception:
        return []


def _pick_recipient_and_contact():
    """Find a (recipient, contact) pair where the contact passes the strict
    filter. Tries each eligible recipient in random order; only fails if
    nobody in the household has any plausible caller. This lets us hold
    a higher bar on plausibility (no cross-gen acquaintances surfacing as
    text senders) without the mod going silent when one recipient happens
    to have only awkward contacts."""
    recipients = _eligible_recipients()
    if not recipients:
        _log_household_inspection()
        return None, None
    for recipient in recipients:
        contact = _pick_random_relationship_sim(recipient=recipient)
        if contact:
            return recipient, contact
    return None, None


def _get_sims_on_active_lot():
    """Return a set of sim_ids currently on the active lot."""
    sim_ids = set()
    try:
        import services
        zone = services.current_zone()
        if not zone:
            return sim_ids
        # Iterate through all sims currently instantiated on the lot
        sm = services.sim_info_manager()
        if sm:
            for si in sm.values():
                try:
                    sim = si.get_sim_instance()
                    if sim is not None and sim.zone_id == zone.id:
                        sim_ids.add(si.sim_id)
                except Exception:
                    continue
    except Exception:
        pass
    return sim_ids


def _is_ghost(sim_info):
    """Check if a sim is dead/ghost. Tries multiple methods for compatibility."""
    if sim_info is None:
        return False

    # Method 1: is_ghost can be a method OR a property in different game versions
    try:
        ig = getattr(sim_info, "is_ghost", None)
        if ig is not None:
            val = ig() if callable(ig) else ig
            if val:
                return True
    except Exception:
        pass

    # Method 2: is_dead attribute
    try:
        if getattr(sim_info, "is_dead", False):
            return True
    except Exception:
        pass

    # Method 3: death_type — None or NONE means alive
    try:
        death_type = getattr(sim_info, "death_type", None)
        if death_type is not None:
            dt_str = str(death_type)
            if dt_str and "NONE" not in dt_str.upper():
                return True
    except Exception:
        pass

    # Method 4: check for ghost traits on the sim
    try:
        traits = sim_context.get_sim_traits(sim_info, limit=20)
        for t in traits:
            if "ghost" in t.lower():
                return True
    except Exception:
        pass

    return False


def find_contact_by_name(full_name):
    """Find a specific sim by name from the protagonist's relationship network or sim manager."""
    name_lower = full_name.lower()

    # First check relationship network
    main_si = sim_context.get_main_sim_info()
    if main_si:
        hh_members, relationships = sim_context.get_main_sim_network(main_si)
        for contact in hh_members + relationships:
            if contact["name"].lower() == name_lower:
                return contact

    # Fallback: search the sim manager directly and build a contact dict.
    # The network search above filters by min_friendship=25, so low-friendship
    # sims (e.g. -9 acquaintances) never appear there. We still want to surface
    # those — and crucially we still need their friendship/romance scores so
    # the "barely know each other" tier applies. Read them directly from the
    # main sim's relationship tracker before returning.
    try:
        import services
        parts = full_name.strip().split(None, 1)
        first = parts[0].lower()
        last = parts[1].lower() if len(parts) > 1 else ""

        sm = services.sim_info_manager()
        for si in sm.values():
            if si.first_name.lower() != first:
                continue
            if last and si.last_name.lower() != last:
                continue

            friendship = None
            romance = None
            status = ""
            if main_si is not None:
                try:
                    rt = main_si.relationship_tracker
                    entry = sim_context._read_relationship_for_target(rt, si.sim_id, sm)
                    if entry:
                        friendship = entry.get("friendship")
                        romance = entry.get("romance")
                        status = entry.get("status", "") or ""
                except Exception:
                    pass

            return {
                "sim_info": si,
                "sim_id": si.sim_id,
                "name": f"{si.first_name} {si.last_name}".strip(),
                "status": status,
                "friendship": friendship,
                "romance": romance,
                "in_household": False,
            }
    except Exception:
        pass
    return None


def _is_age_appropriate_contact(contact, recipient):
    """
    Filter out implausible cross-generational acquaintance pairings.
    Teens don't randomly chat-text their friend's adult parents, and adults
    don't randomly chat-text their kid's teen friends. Such pairs exist in
    the relationship tracker (they've met) but they're not natural texters
    unless they're family OR have a genuinely close friendship.

    (The picker has a fallback tier that relaxes this filter when it would
    otherwise leave the recipient with nothing.)
    """
    contact_si = contact.get("sim_info")
    if not contact_si or not recipient:
        return True
    c_rank = _age_rank(contact_si)
    r_rank = _age_rank(recipient)
    if c_rank is None or r_rank is None:
        return True
    # Same or adjacent age stage — always plausible
    if abs(c_rank - r_rank) <= 1:
        return True
    # Family — always allowed (parent calling child, grandparent calling grandkid, etc.)
    family_label = _get_family_relationship(contact_si, contact, recipient=recipient)
    if family_label:
        return True
    # Cross-generational non-family — only allow if they're genuinely close
    friendship = abs(contact.get("friendship") or 0)
    romance = abs(contact.get("romance") or 0)
    if friendship >= 50 or romance >= 50:
        return True
    return False


def _log_picker(message):
    """Diagnostic logger for the contact picker. Goes to the main log file."""
    try:
        import os, datetime
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [picker] {message}\n")
    except Exception:
        pass


def _journal_obsolescence_note(contact):
    """If the current friendship label disagrees with the warmth of past
    journal entries, return a single-line note we append under the
    journal block. The model handles tone fine when the prompt isn't
    contradicting itself -- this note tells it that warm history
    predates the current state and should be treated as obsolete.

    Returns "" when the journal and current label are consistent
    (and so no annotation is needed)."""
    try:
        score = contact.get("friendship")
        if score is None:
            return ""
    except Exception:
        return ""
    if score < -70:
        return ("[NOTE: They are now ENEMIES. Any warmth or friendliness in the "
                "entries above is OBSOLETE -- treat as predating the falling-out.]")
    if score < -40:
        return ("[NOTE: They now actively DISLIKE each other. Warmth in entries "
                "above is OBSOLETE -- treat as predating the relationship souring.]")
    if score < -20:
        return ("[NOTE: They now have NEGATIVE history. Warm entries above are "
                "from before things cooled and should not shape the current tone.]")
    return ""


def _pick_random_relationship_sim(recipient=None):
    """Pick a random non-household sim from the recipient's relationship network.
    Hard filters: pets, ghosts (when disabled in config), sims currently on
    the active lot (no "calling from the next room"), and cross-generational
    acquaintances who aren't family or close friends. If nothing passes, we
    return None -- the caller (_pick_recipient_and_contact) will try a
    different household member rather than relax these rules."""
    recipient_name = recipient.first_name if recipient else "(no recipient)"

    base_si = recipient or sim_context.get_main_sim_info()
    if base_si:
        _household_members, relationships = sim_context.get_main_sim_network(base_si)
        contacts = relationships
    else:
        active = sim_context.get_active_sim()
        if not active or not active.sim_info:
            _log_picker(f"No base sim or active sim found for {recipient_name}.")
            return None
        rels = sim_context.get_sim_relationships(active.sim_info)
        contacts = [r for r in rels if not r.get("in_household")]

    initial_count = len(contacts)

    # Hard filters: pets, and ghosts when disabled in config.
    contacts = [c for c in contacts if _is_human_sim(c.get("sim_info"))]
    allow_ghosts = config.get_phone_allow_ghosts()
    if not allow_ghosts:
        contacts = [c for c in contacts if not _is_ghost(c.get("sim_info"))]

    # Strict filter: off-lot AND age-appropriate. Neither is relaxed --
    # if the recipient's only options are on-lot or cross-gen acquaintances,
    # _pick_recipient_and_contact tries a different household member instead.
    on_lot = _get_sims_on_active_lot()
    contacts_off_lot = [c for c in contacts if not on_lot or c.get("sim_id") not in on_lot]

    if recipient is not None:
        chosen_pool = [c for c in contacts_off_lot if _is_age_appropriate_contact(c, recipient)]
    else:
        chosen_pool = contacts_off_lot

    # Contact preferences (per-pair): drop contacts THIS household sim
    # has muted, and apply weight multipliers for paused (0.2) or
    # priority (2.0). Uses (recipient_sim_id, contact_sim_id) so Alice's
    # muted ex doesn't affect Bob's phone activity with that same sim.
    def _sid(c):
        si = c.get("sim_info")
        return getattr(si, "sim_id", None) if si else None
    _recipient_sid = getattr(base_si, "sim_id", None) if base_si else None
    chosen_pool = [
        c for c in chosen_pool
        if not contact_prefs.is_muted(_recipient_sid, _sid(c))
    ]

    if not chosen_pool:
        _log_picker(
            f"{recipient_name}: 0 strict contacts (initial {initial_count}). "
            f"Caller should try a different recipient."
        )
        return None

    weights = []
    for contact in chosen_pool:
        score = abs(contact.get("friendship") or 0) + abs(contact.get("romance") or 0)
        base = max(score, 10)
        mult = contact_prefs.auto_event_multiplier(_recipient_sid, _sid(contact))
        weights.append(base * mult)

    # If every remaining weight is 0 (unlikely once muted are filtered
    # out, but defensively) fall back to uniform pick so we don't hand
    # random.choices an all-zero weight list.
    if not any(w > 0 for w in weights):
        return random.choice(chosen_pool)
    return random.choices(chosen_pool, weights=weights, k=1)[0]


# Bits that signal a relationship is platonic (no longer romantic).
# Sims 4 adds these when a romance ends — they override any lingering romantic bits.
_PLATONIC_BIT_KEYWORDS = ("justfriends", "justgoodfriends", "platonic")


def _has_platonic_bit(bits):
    """Return True if any bit indicates the relationship is now platonic."""
    if not bits:
        return False
    for bit in bits:
        try:
            bn = sim_context._get_trait_name(bit).lower().replace("_", "")
            if any(kw in bn for kw in _PLATONIC_BIT_KEYWORDS):
                return True
        except Exception:
            pass
    return False


# Detection priority: highest commitment first. The first match wins so
# we don't downgrade a Married sim to "in a relationship with" if both
# bits happen to coexist.
_ROMANTIC_STATUS_PATTERNS = (
    # (canonical_status, [bit substring matches], [exclusion substrings])
    ("married to",            ("spouse", "married"),          ("unmarried", "divorced", "ex_", "former")),
    ("engaged to",            ("engaged",),                    ("dis", "broken")),
    ("in a relationship with",("goingsteady", "boyfriend",
                               "girlfriend", "significantother"), ("ex_", "former", "broke")),
)


def _get_romantic_partner_info(sim_info):
    """Return (partner_sim_info, status_string) if this sim is in a committed
    romantic relationship (married / engaged / going steady).
    Returns (None, None) otherwise."""
    if sim_info is None:
        return None, None
    try:
        rt = sim_info.relationship_tracker
        if rt is None:
            return None, None
    except Exception:
        return None, None

    try:
        import services
        sm = services.sim_info_manager()
    except Exception:
        sm = None

    for tid in rt.target_sim_gen():
        try:
            bits = list(rt.get_all_bits(tid))
            if not bits or _has_platonic_bit(bits):
                continue
            bit_names = [sim_context._get_trait_name(b).lower().replace("_", "") for b in bits]
            for status, include, exclude in _ROMANTIC_STATUS_PATTERNS:
                if not any(any(inc in bn for inc in include) for bn in bit_names):
                    continue
                if any(any(exc in bn for exc in exclude) for bn in bit_names):
                    continue
                partner_si = sm.get(tid) if sm else None
                return (partner_si, status)
        except Exception:
            continue
    return None, None


def _describe_recipient(recipient_sim, contact=None):
    """Build a short recipient block — just enough so the caller knows who they're addressing.
    Includes the recipient's household members with both their relationship to the recipient
    AND (if known) their relationship to the contact. Prevents the caller from inventing
    identities for household members like a spouse/child who don't appear in the contact's
    relationship tracker."""
    if not recipient_sim:
        return ""
    name = f"{recipient_sim.first_name} {recipient_sim.last_name}".strip()
    parts = [f"=== Recipient: {name} (the person receiving this) ==="]
    try:
        age = str(getattr(recipient_sim, "age", "")).replace("Age.", "")
        if age:
            parts.append(f"{recipient_sim.first_name}'s age: {age}")
    except Exception:
        pass
    try:
        gender = str(getattr(recipient_sim, "gender", "")).replace("Gender.", "")
        if gender:
            parts.append(f"{recipient_sim.first_name}'s gender: {gender}")
    except Exception:
        pass
    traits = sim_context.get_sim_traits(recipient_sim, limit=4)
    if traits:
        parts.append(f"{recipient_sim.first_name}'s traits: {', '.join(traits)}")

    # Recipient's current mood, career, aspiration -- the caller block has
    # these too, and the AI needs them on the recipient side so it can ask
    # about real things ("how's the doctor track going?", "still going for
    # Renaissance Sim?"). Mood was a regression from earlier versions.
    try:
        mood = sim_context.get_sim_mood(recipient_sim)
        if mood:
            parts.append(f"{recipient_sim.first_name}'s current mood: {mood}")
    except Exception:
        pass

    try:
        career = sim_context.get_sim_career(recipient_sim)
        if career:
            parts.append(f"{recipient_sim.first_name}'s career: {career}")
    except Exception:
        pass

    try:
        aspiration = sim_context.get_sim_aspiration(recipient_sim)
        if aspiration:
            parts.append(f"{recipient_sim.first_name}'s aspiration: {aspiration}")
    except Exception:
        pass

    # Top 3 skills -- enables save-aware callbacks like "how's the painting
    # going?" when the player has actually been raising Painting.
    try:
        skills = sim_context.get_sim_skills(recipient_sim, limit=3)
        if skills:
            skill_str = ", ".join(f"{sk} {lvl}" for sk, lvl in skills.items())
            parts.append(f"{recipient_sim.first_name}'s top skills: {skill_str}")
    except Exception:
        pass

    # Recipient's world -- the sender already gets this, and skipping it on
    # the recipient leaves the model guessing whether they live in the same
    # place or somewhere else, which produces phrases like "ran into your
    # mom at the store" when the recipient lives in a different world.
    try:
        home = _get_sim_home_world(recipient_sim)
        if home:
            parts.append(f"{recipient_sim.first_name} lives in: {home}")
    except Exception:
        pass

    clubs = sim_context.get_sim_clubs(recipient_sim)
    if clubs:
        parts.append(f"{recipient_sim.first_name}'s clubs: {', '.join(clubs)}")

    # Recipient's romantic / marital status -- so a caller doesn't write
    # the player's spouse a flirty "we should meet up" type message.
    try:
        partner_si, rstatus = _get_romantic_partner_info(recipient_sim)
        if partner_si and rstatus:
            pname = f"{partner_si.first_name} {partner_si.last_name}".strip()
            parts.append(f"{recipient_sim.first_name} is {rstatus} {pname}")
    except Exception:
        pass

    # Household members the recipient lives with — so the AI knows about kids/spouses/etc
    # who might come up in conversation but aren't in the contact's relationship tracker.
    household_lines = []
    contact_si = contact.get("sim_info") if contact else None
    try:
        import services
        hh = services.active_household()
        if hh:
            for si in hh.sim_info_gen():
                try:
                    if si.sim_id == recipient_sim.sim_id:
                        continue
                    mname = f"{si.first_name} {si.last_name}".strip()
                    mage = str(getattr(si, "age", "")).replace("Age.", "")
                    # How this household member relates to the recipient
                    rel_to_recipient = _get_family_relationship(si, {}, recipient=recipient_sim)
                    # How this household member relates to the contact (so the contact
                    # knows e.g. that the recipient's husband is also the contact's son)
                    rel_to_contact = None
                    if contact_si and contact_si.sim_id != si.sim_id:
                        rel_to_contact = _get_family_relationship(si, {}, recipient=contact_si)

                    pieces = []
                    if rel_to_recipient:
                        pieces.append(f"{recipient_sim.first_name}'s {rel_to_recipient}")
                    else:
                        pieces.append(f"lives with {recipient_sim.first_name}")
                    if rel_to_contact:
                        pieces.append(f"your {rel_to_contact}")
                    pieces.append(mage)
                    ghost_tag = " [DECEASED — only reference in past tense]" if _is_ghost(si) else ""
                    household_lines.append(f"  - {mname} ({', '.join(pieces)}){ghost_tag}")
                except Exception:
                    continue
    except Exception:
        pass

    if household_lines:
        parts.append(f"\n{recipient_sim.first_name}'s household:")
        parts.extend(household_lines)

    # Surface any recent milestones for the recipient so the caller can
    # plausibly reference them ("hey congrats on the promotion!").
    # Pass the caller's sim_id as contact_id so each milestone is only
    # surfaced to that contact ONCE -- prevents the same sim asking about
    # the same job-quit / promotion across multiple calls.
    try:
        from . import milestones as _milestones
        contact_sim_id = None
        if contact is not None:
            try:
                contact_sim_id = contact.get("sim_id") if isinstance(contact, dict) else None
            except Exception:
                pass
        mblock = _milestones.format_for_prompt(recipient_sim, contact_id=contact_sim_id)
        if mblock:
            # Re-label so the LLM understands these are events in the
            # recipient's life, not the caller's.
            mblock = mblock.replace(
                "Recent in their life:",
                f"Recent in {recipient_sim.first_name}'s life (you may know about these):",
            )
            parts.append("\n" + mblock)
    except Exception:
        pass

    return "\n".join(parts)


def _clean_bit_label(bn):
    """Extract a clean human-readable relationship label from a raw bit name.
    Example: 'familyRelationshipBitsAcquired_Sibling_NeutralRel_LowRival' -> 'Sibling'
             'RelationshipBit_Friend_Good' -> 'Good Friend'
             'Romantic_Lover' -> 'Lover'
    """
    if not bn:
        return ""
    # Strip noise tokens
    parts = bn.replace("RelationshipBit_", "").replace("Romantic_", "")
    parts = parts.split("_")
    # Token whitelist — keep only words that mean something to a player
    KEEP = ("Friend", "Friends", "Friendly", "Good", "Best", "BFF",
            "Enemy", "Hate", "Despise", "Rival",
            "Married", "Spouse", "Engaged", "Fiance",
            "Crush", "Lover", "Soulmate", "Sweetheart", "Dating",
            "Romantic", "Partner",
            "Broken", "BrokenUp", "Ex", "Former", "Divorced",
            "Sibling", "Brother", "Sister",
            "Parent", "Mother", "Father", "Mom", "Dad",
            "Child", "Son", "Daughter",
            "Grandparent", "Grandfather", "Grandmother", "Granny", "Grandpa",
            "Grandchild", "Grandson", "Granddaughter",
            "Aunt", "Uncle", "Niece", "Nephew", "Cousin",
            "Family", "Inlaw", "InLaw", "Acquaintance")
    # Detect specific in-law patterns from internal phrasing
    bn_compact = bn.replace("_", "").lower()
    if "siblinginlaw" in bn_compact or "issiblinginlaw" in bn_compact:
        return "Sibling-in-law"
    if "parentinlaw" in bn_compact or "isparentinlaw" in bn_compact:
        return "Parent-in-law"
    if "childinlaw" in bn_compact or "ischildinlaw" in bn_compact:
        return "Child-in-law"

    kept = [p for p in parts if p in KEEP]
    if kept:
        return " ".join(kept).strip()
    # If nothing matched, this is an internal/system bit — drop it
    return ""


_FAMILY_CATEGORY = {
    # Map a printed family label to a gender-neutral kinship category, for transitive inference.
    "father": "parent", "mother": "parent",
    "son": "child", "daughter": "child",
    "brother": "sibling", "sister": "sibling",
    "husband": "spouse", "wife": "spouse",
    "grandfather": "grandparent", "grandmother": "grandparent",
    "great-grandfather": "great-grandparent", "great-grandmother": "great-grandparent",
    "grandson": "grandchild", "granddaughter": "grandchild",
    "great-grandson": "great-grandchild", "great-granddaughter": "great-grandchild",
    "uncle": "auntuncle", "aunt": "auntuncle",
    "nephew": "niecenephew", "niece": "niecenephew",
    "cousin": "cousin",
    "father-in-law": "parentinlaw", "mother-in-law": "parentinlaw",
    "son-in-law": "childinlaw", "daughter-in-law": "childinlaw",
    "brother-in-law": "siblinginlaw", "sister-in-law": "siblinginlaw",
}

# Given (contact-to-player, mutual-to-player), what is mutual-to-contact?
# Keys and values are gender-neutral kinship categories from _FAMILY_CATEGORY.
_TRANSITIVE_KIN = {
    ("parent", "parent"): "spouse",
    ("parent", "sibling"): "child",       # Apollo (player's father) + Francesca (player's sister) -> Apollo's child
    ("parent", "child"): "grandchild",
    ("parent", "grandparent"): "parent",
    ("parent", "spouse"): "child",        # contact is player's parent, mutual is player's spouse -> mutual is contact's child-in-law (treat as child for warmth)
    ("child", "parent"): "spouse",
    ("child", "child"): "sibling",        # both are player's kids -> siblings to each other
    ("child", "sibling"): "auntuncle",
    ("child", "spouse"): "parent",        # contact is player's kid, mutual is player's spouse -> mutual is contact's other parent
    ("sibling", "parent"): "parent",
    ("sibling", "sibling"): "sibling",
    ("sibling", "child"): "niecenephew",
    ("sibling", "spouse"): "siblinginlaw",
    ("spouse", "child"): "child",
    ("spouse", "parent"): "parentinlaw",
    ("spouse", "sibling"): "siblinginlaw",
    ("grandparent", "parent"): "child",
    ("grandparent", "sibling"): "grandchild",
    ("grandparent", "grandparent"): "spouse",
    ("grandchild", "child"): "child",     # contact is player's grandkid, mutual is player's kid -> mutual is contact's parent (printed as "child" of the grandparent generation? actually parent)
    ("auntuncle", "parent"): "sibling",
    ("auntuncle", "sibling"): "niecenephew",
    ("niecenephew", "parent"): "siblinginlaw",  # contact is player's niece, mutual is player's parent -> mutual is contact's grandparent really, but skip
}
# Override: the (grandchild, child) case above is wrong -- correct is "parent of contact"
_TRANSITIVE_KIN[("grandchild", "child")] = "parent"

_KIN_TO_GENDERED = {
    "parent":          ("Father", "Mother"),
    "child":           ("Son", "Daughter"),
    "sibling":         ("Brother", "Sister"),
    "spouse":          ("Husband", "Wife"),
    "grandparent":     ("Grandfather", "Grandmother"),
    "grandchild":      ("Grandson", "Granddaughter"),
    "great-grandparent": ("Great-Grandfather", "Great-Grandmother"),
    "great-grandchild":  ("Great-Grandson", "Great-Granddaughter"),
    "auntuncle":       ("Uncle", "Aunt"),
    "niecenephew":     ("Nephew", "Niece"),
    "cousin":          ("Cousin", "Cousin"),
    "parentinlaw":     ("Father-in-law", "Mother-in-law"),
    "childinlaw":      ("Son-in-law", "Daughter-in-law"),
    "siblinginlaw":    ("Brother-in-law", "Sister-in-law"),
}


def _infer_kin_via_player(contact_role, mutual_role, mutual_si):
    """
    When direct genealogy lookup between contact and mutual fails (common when
    one sim's genealogy is incomplete), we can still derive their relationship
    if we know BOTH sims' relationships to the player.

    Example: contact is player's Father, mutual is player's Sister. Then mutual
    must be contact's Daughter.
    """
    if not contact_role or not mutual_role:
        return None
    c_cat = _FAMILY_CATEGORY.get(contact_role.lower())
    m_cat = _FAMILY_CATEGORY.get(mutual_role.lower())
    if not c_cat or not m_cat:
        return None
    out_cat = _TRANSITIVE_KIN.get((c_cat, m_cat))
    if not out_cat:
        return None
    gendered = _KIN_TO_GENDERED.get(out_cat)
    if not gendered:
        return None
    try:
        gender = str(getattr(mutual_si, "gender", "")).replace("Gender.", "")
    except Exception:
        gender = ""
    male_lbl, female_lbl = gendered
    return male_lbl if gender == "MALE" else female_lbl


def _format_mutual_block(mutuals, casual=True):
    """Build the mutual contacts block for a prompt. Two flavors: the
    longer 'gossip welcome' version used for incoming call/text prompts,
    and a tighter version for reply-style prompts. Both add the family-
    reference rule so the model says 'your dad' instead of 'Apollo' when
    a mutual is the recipient's parent."""
    if not mutuals:
        return ""
    header = (
        "\n\nPeople BOTH of you know "
        "(these are the ONLY mutual sims you can reference by name):\n"
        if casual else
        "\n\nPeople BOTH of you know (the ONLY mutual sims you can name):\n"
    )
    body = header + "\n".join(f"  - {m}" for m in mutuals)
    if casual:
        body += (
            "\nFeel free to gossip about, mention, or bring up any of these sims naturally. "
            "DO NOT invent any other sim names -- if you need to reference someone not on "
            "this list, use a generic reference like 'a coworker', 'my neighbor', "
            "'this friend of mine' instead."
        )
    else:
        body += (
            "\nDO NOT invent other sim names -- use generic references like 'a coworker' "
            "if needed."
        )
    body += (
        "\nWhen mentioning a mutual who is family of the message recipient, follow "
        "these rules based on whether the mutual is ALSO family of you (the sender):"
        "\n"
        "\n(a) MUTUAL IS FAMILY OF RECIPIENT ONLY (not related to you): "
        "refer to them by the family role from the recipient's perspective -- "
        "\"your dad\", \"your mom\", \"your sister\", \"your brother\", \"your son\", "
        "\"your daughter\" -- NOT by their first name. E.g. a coworker calling their "
        "friend says \"how's your dad?\", not \"how's Apollo?\"."
        "\n"
        "\n(b) MUTUAL IS SHARED FAMILY in the SAME ROLE (e.g. both of you are that "
        "person's kid -- you're both siblings-of-that-person's-kid, so it's your "
        "shared parent): use \"our mom / our dad / our sister / our brother\" or just "
        "\"mom / dad\" without a possessive. NEVER \"your mom\" when the mom is your "
        "mom too. Applies to shared parents, shared siblings, shared grandparents, "
        "shared kids (\"our kid\"). Check: if the mutual's entry lists you as e.g. "
        "'your Mother' AND the recipient as 'Francesca's Mother', both call her Mother, "
        "so use \"our mom\"."
        "\n"
        "\n(c) MUTUAL IS FAMILY OF BOTH but in DIFFERENT ROLES (e.g. your Brother is "
        "the recipient's Father -- i.e. you're the recipient's aunt/uncle): use YOUR "
        "OWN family label from your perspective (\"my brother\", \"my sister\"), or "
        "use the mutual's first name. NEVER contort into recipient's-perspective "
        "phrasing that creates weird loops like \"your dad's daughter\" -- if you "
        "find yourself constructing \"[recipient's-family-role]'s [recipient]\", stop "
        "and just use your own label or the first name."
        "\n"
        "\nFirst-name references are fine for non-family mutuals."
    )
    body += (
        "\nPLAUSIBILITY CHECK -- before involving a mutual in your topic, look at "
        "their age, career, and traits. A Young Adult Entertainer isn't co-researching "
        "archaeology with you; a Loner Bookworm Elder isn't your nightclub buddy; a "
        "Toddler isn't sharing investment tips. Only invoke a mutual as participating "
        "in something they'd plausibly do given who they are. If they don't fit your "
        "topic, just don't mention them -- pick a different mutual or skip the gossip."
    )
    body += (
        "\nINTERESTS / CLUBS / CAREERS DO NOT TRANSFER between mutuals. Each sim's "
        "clubs, career, and skills are listed individually in their own entry -- do "
        "NOT assume sim A shares sim B's clubs/interests just because they know each "
        "other. If you want to mention an activity, name the ONE sim whose listed "
        "entry actually has that club/career/skill. Saying \"you and Bob doing that "
        "board-game stuff\" is wrong if only Bob's clubs list \"Board\" and the "
        "recipient's doesn't."
    )
    return body


def _get_mutual_contacts(contact, recipient=None):
    """
    Find sims that both the recipient and the contact have relationships with.
    Returns a randomised subset of up to 4 — so different mutuals surface across
    calls rather than always the same fixed list.

    Labels prefer family-relationship detection (which knows about
    grandparents/in-laws/etc.) and only fall back to bit-name extraction.
    Marks ghosts explicitly so they're never written about as if alive.
    """
    mutuals = []
    try:
        main_si = recipient or sim_context.get_main_sim_info()
        other_si = contact.get("sim_info")
        if not main_si or not other_si:
            return mutuals

        # Pre-compute contact's family role to the recipient -- used for transitive
        # inference when direct genealogy between contact and a mutual is broken.
        contact_role_to_main = _get_family_relationship(other_si, contact, recipient=main_si)

        main_rt = main_si.relationship_tracker
        main_targets = set(main_rt.target_sim_gen())
        main_targets.discard(other_si.sim_id)

        other_rt = other_si.relationship_tracker
        other_targets = set(other_rt.target_sim_gen())
        other_targets.discard(main_si.sim_id)

        shared_ids = main_targets & other_targets
        if not shared_ids:
            return mutuals

        import services
        sm = services.sim_info_manager()

        # Partition mutuals into "family of the contact" and "everyone else".
        # Family must always appear -- the AI hallucinates worst when it
        # doesn't know a contact's own children/spouse/siblings/parents
        # ("who's Luca?" when Luca is the contact's son). Non-family
        # mutuals are randomised so variety still surfaces across calls.
        family_ids = []
        other_ids = []
        for sid in shared_ids:
            try:
                si_check = sm.get(sid)
                if si_check is not None and _get_family_relationship(si_check, {}, recipient=other_si):
                    family_ids.append(sid)
                else:
                    other_ids.append(sid)
            except Exception:
                other_ids.append(sid)
        random.shuffle(other_ids)
        # Family first (all of them), then random fill from the rest.
        # Cap total at 8 so we don't blow up the prompt on huge families.
        shared_list = family_ids + other_ids
        MAX_MUTUALS = 8

        def _short_relationship_label(rt, sid):
            """Best-effort relationship label from bits, used when family detection
            returns nothing. Filters platonic-overrides-romantic and noise."""
            try:
                raw = list(rt.get_all_bits(sid))
                is_platonic = _has_platonic_bit(raw)
                labels = []
                for bit in raw:
                    bn = sim_context._get_trait_name(bit)
                    bn_low = bn.lower().replace("_", "")
                    is_romantic = any(kw in bn_low for kw in ("romantic", "crush", "lover"))
                    if is_romantic and is_platonic:
                        continue
                    label = _clean_bit_label(bn)
                    if label and label not in labels:
                        labels.append(label)
                return ", ".join(labels[:2]) if labels else None
            except Exception:
                return None

        for sid in shared_list[:MAX_MUTUALS]:
            try:
                si = sm.get(sid)
                if not si:
                    continue
                name = f"{si.first_name} {si.last_name}".strip()

                # Prefer family detection — covers grandparent, great-grandparent,
                # in-laws, etc. that bit names don't surface cleanly.
                main_label = _get_family_relationship(si, {}, recipient=main_si)
                if not main_label:
                    main_label = _short_relationship_label(main_rt, sid)
                if not main_label:
                    main_label = "acquaintance"

                other_label = _get_family_relationship(si, {}, recipient=other_si)
                # Transitive inference: if direct genealogy contact<->mutual is
                # broken but we know both sides' relation to the player, derive
                # the missing edge. Catches cases like Apollo (player's Father)
                # being mislabeled as "Friendly" with Francesca (player's Sister)
                # when only the player's genealogy lists Apollo as parent.
                if not other_label and contact_role_to_main and main_label and main_label != "acquaintance":
                    other_label = _infer_kin_via_player(contact_role_to_main, main_label, si)
                if not other_label:
                    other_label = _short_relationship_label(other_rt, sid)
                if not other_label:
                    other_label = "acquaintance"

                # Age, career, and traits -- the AI needs these to reason
                # about whether the mutual would PLAUSIBLY be involved in a
                # given topic. A Young Adult Entertainer doesn't suddenly
                # take up archaeology research; a Loner Bookworm Elder
                # probably isn't your nightclub buddy.
                age = ""
                try:
                    age_str = str(getattr(si, "age", "")).replace("Age.", "")
                    if age_str:
                        age = f", {age_str}"
                except Exception:
                    pass

                career_part = ""
                try:
                    career = sim_context.get_sim_career(si)
                    if career:
                        career_part = f", {career}"
                except Exception:
                    pass

                traits_part = ""
                try:
                    traits = sim_context.get_sim_traits(si, limit=2)
                    if traits:
                        traits_part = f", {', '.join(traits)}"
                except Exception:
                    pass

                world = _get_sim_home_world(si)
                world_part = f", lives in {world}" if world else ", world unknown — treat as long-distance only"

                # Ghost marker — the AI must NEVER write about ghosts as if alive
                ghost_tag = ""
                if _is_ghost(si):
                    ghost_tag = " [DECEASED — only reference in past tense or as a memory]"

                # "your" in this block refers to the SENDER/CALLER (the one writing),
                # since the prompt addresses the model AS the sender. The recipient's
                # side is named explicitly to avoid pronoun confusion.
                recipient_first = main_si.first_name if main_si else "the recipient"
                mutuals.append(
                    f"{name} (your {other_label}, {recipient_first}'s {main_label}{age}"
                    f"{career_part}{traits_part}{world_part}){ghost_tag}"
                )
            except Exception:
                continue
    except Exception:
        pass
    return mutuals


# Map internal region names (and pack codes) to friendly world names.
# Sims 4 worlds are usually identified by EP/GP/SP pack codes.
_WORLD_NAMES = {
    # Base game
    "willowcreek": "Willow Creek",
    "oasissprings": "Oasis Springs",
    "newcrest": "Newcrest",
    # EP01 Get to Work
    "magnoliapromenade": "Magnolia Promenade",
    "ep01": "Magnolia Promenade",
    # EP02 Get Together -- internal region is "NorthEurope".
    "windenburg": "Windenburg",
    "northeurope": "Windenburg",
    "ep02": "Windenburg",
    # EP03 City Living -- internal region is "CityLife".
    "sanmyshuno": "San Myshuno",
    "citylife": "San Myshuno",
    "ep03": "San Myshuno",
    # EP04 Cats & Dogs -- internal region is "PetWorld".
    "brindletonbay": "Brindleton Bay",
    "petworld": "Brindleton Bay",
    "ep04": "Brindleton Bay",
    # EP05 Seasons (no new world)
    # EP06 Get Famous -- internal region is "FameWorld".
    "delsolvalley": "Del Sol Valley",
    "fameworld": "Del Sol Valley",
    "ep06": "Del Sol Valley",
    # EP07 Island Living -- internal region is "IslandWorld".
    "sulani": "Sulani",
    "islandworld": "Sulani",
    "ep07": "Sulani",
    # EP08 Discover University -- internal region is "UniversityWorld".
    "britechester": "Britechester",
    "universityworld": "Britechester",
    "ep08": "Britechester",
    # EP09 Eco Lifestyle -- internal region is "EcoWorld".
    "evergreenharbor": "Evergreen Harbor",
    "ecoworld": "Evergreen Harbor",
    "ep09": "Evergreen Harbor",
    # EP10 Snowy Escape -- internal region is "MountainWorld".
    "mtkomorebi": "Mt. Komorebi",
    "mountainworld": "Mt. Komorebi",
    "ep10": "Mt. Komorebi",
    # EP11 Cottage Living -- internal region is "CottageWorld".
    "henfordonbagley": "Henford-on-Bagley",
    "henford": "Henford-on-Bagley",
    "cottageworld": "Henford-on-Bagley",
    "ep11": "Henford-on-Bagley",
    # EP12 High School Years -- internal region is "HighSchoolWorld".
    "copperdale": "Copperdale",
    "highschoolworld": "Copperdale",
    "ep12": "Copperdale",
    # EP13 Growing Together -- internal region is "BayArea".
    "sansequoia": "San Sequoia",
    "bayarea": "San Sequoia",
    "ep13": "San Sequoia",
    # EP14 Horse Ranch -- internal region is "EP14World".
    "chestnutridge": "Chestnut Ridge",
    "ep14world": "Chestnut Ridge",
    "ep14": "Chestnut Ridge",
    # EP15 For Rent -- internal region is "MultiUnitWorld".
    "tomarang": "Tomarang",
    "multiunitworld": "Tomarang",
    "ep15": "Tomarang",
    # EP16 Life & Death -- internal region is "EP16World".
    "ravenwood": "Ravenwood",
    "ep16world": "Ravenwood",
    "ep16": "Ravenwood",
    # EP17 Lovestruck -- internal region is "EP17World".
    "ciudadenamorada": "Ciudad Enamorada",
    "ep17world": "Ciudad Enamorada",
    "ep17": "Ciudad Enamorada",
    # EP18 Businesses & Hobbies -- internal region is "EP18World".
    "nordhaven": "Nordhaven",
    "ep18world": "Nordhaven",
    "ep18": "Nordhaven",
    # EP20 Adventure Awaits -- internal region is "EP20World".
    "gibbipoint": "Gibbi Point",
    "ep20world": "Gibbi Point",
    "ep20": "Gibbi Point",
    # GP packs with worlds
    "granitefalls": "Granite Falls",
    "outdoorretreat": "Granite Falls",
    "gp01": "Granite Falls",
    "forgottenhollow": "Forgotten Hollow",
    "gp04": "Forgotten Hollow",
    "selvadorada": "Selvadorada",
    "gp06": "Selvadorada",
    "strangerville": "StrangerVille",
    "gp07": "StrangerVille",
    "glimmerbrook": "Glimmerbrook",
    "magicvenue": "Glimmerbrook",
    "gp08": "Glimmerbrook",
    "batuu": "Batuu",
    "gp09": "Batuu",
    "tartosa": "Tartosa",
    "weddingworld": "Tartosa",
    "gp11": "Tartosa",
    "moonwoodmill": "Moonwood Mill",
    "gp12": "Moonwood Mill",
    "innisgreen": "Innisgreen",
    "gp14": "Innisgreen",
    # Hidden worlds
    "alienworld": "Sixam",
    "sixam": "Sixam",
    "forgottengrotto": "Forgotten Grotto",
    "sylvanglade": "Sylvan Glade",
}


def _friendly_world_name(raw):
    """Convert internal region name to friendly name. Returns None for unknowns."""
    if not raw:
        return None
    import re
    full_key = raw.lower().replace(" ", "").replace("_", "")
    if not full_key:
        return None

    # 1. Try exact match on the full normalized key
    name = _WORLD_NAMES.get(full_key)
    if name:
        return name

    # 2. Try matching by pack code prefix (ep18, gp12, etc.)
    m = re.match(r'(ep|gp|sp|fp)(\d+)', full_key)
    if m:
        pack_code = m.group(1) + m.group(2)
        name = _WORLD_NAMES.get(pack_code)
        if name:
            return name

    # 3. Try after stripping the pack prefix
    stripped = re.sub(r'^(ep|gp|sp|fp)\d+', '', full_key)
    if stripped:
        name = _WORLD_NAMES.get(stripped)
        if name:
            return name

    # 4. Try partial matches
    for k, v in _WORLD_NAMES.items():
        if len(k) >= 4 and (k in full_key or k in stripped):
            return v

    return None


def _homeworld_log(message):
    """Diagnostic log for home-world resolution. Helps debug cases where
    a played sim's world isn't resolving (apartments, sub-region zones)."""
    try:
        import os, datetime
        path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
        with open(path, "a", encoding="utf-8") as f:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] [homeworld] {message}\n")
    except Exception:
        pass


def _world_name_from_zone_id(zone_id, debug_label=""):
    """Resolve a zone_id to a friendly world name, or None."""
    if not zone_id:
        return None
    try:
        from world.region import get_region_instance_from_zone_id
        region = get_region_instance_from_zone_id(zone_id)
        if not region:
            _homeworld_log(f"{debug_label} zone_id={zone_id}: get_region_instance_from_zone_id returned None")
            return None
        name = getattr(region, "__name__", "") or str(region)
        cleaned = (name
            .replace("Region_", "")
            .replace("region_", "")
            .replace("_", " ")
            .strip())
        friendly = _friendly_world_name(cleaned) if cleaned else None
        if not friendly:
            _homeworld_log(
                f"{debug_label} zone_id={zone_id} region.__name__={name!r} "
                f"cleaned={cleaned!r} -> _friendly_world_name returned None"
            )
        return friendly
    except Exception as e:
        _homeworld_log(f"{debug_label} zone_id={zone_id}: exception {type(e).__name__}: {e}")
        return None


def _get_sim_home_world(sim_info):
    """Get the world/neighborhood name where a sim lives.

    Tries `sim_info.household` first (works for active household members),
    then falls back to looking up `sim_info.household_id` via the household
    manager (works for NPC/townie sims whose Household object isn't fully
    loaded into the active session). Falls back further to the sim's
    persisted `_zone_id` (their last known zone) as a last resort.
    """
    if sim_info is None:
        return None

    name_for_log = ""
    try:
        name_for_log = f"sim={getattr(sim_info, 'first_name', '?')} {getattr(sim_info, 'last_name', '?')}"
    except Exception:
        pass

    # Path 1: direct household reference (active household, played sims).
    try:
        household = getattr(sim_info, "household", None)
        if household is not None:
            home_zone_id = getattr(household, "home_zone_id", None)
            world = _world_name_from_zone_id(home_zone_id, debug_label=f"{name_for_log} path=household")
            if world:
                return world
        else:
            _homeworld_log(f"{name_for_log} path=household: sim_info.household is None")
    except Exception as e:
        _homeworld_log(f"{name_for_log} path=household: exception {type(e).__name__}: {e}")

    # Path 2: look up by household_id (works for NPCs/townies).
    try:
        hh_id = getattr(sim_info, "household_id", None)
        if hh_id:
            import services
            hh_mgr = services.household_manager()
            if hh_mgr:
                household = hh_mgr.get(hh_id)
                if household is not None:
                    home_zone_id = getattr(household, "home_zone_id", None)
                    world = _world_name_from_zone_id(home_zone_id, debug_label=f"{name_for_log} path=hh_id={hh_id}")
                    if world:
                        return world
                else:
                    _homeworld_log(f"{name_for_log} path=hh_id={hh_id}: household_manager.get() returned None")
        else:
            _homeworld_log(f"{name_for_log} path=hh_id: household_id is missing")
    except Exception as e:
        _homeworld_log(f"{name_for_log} path=hh_id: exception {type(e).__name__}: {e}")

    # Path 3: sim's own persisted zone_id (last known location).
    try:
        sim_zone_id = getattr(sim_info, "zone_id", None) or getattr(sim_info, "_zone_id", None)
        if sim_zone_id:
            world = _world_name_from_zone_id(sim_zone_id, debug_label=f"{name_for_log} path=sim.zone_id")
            if world:
                return world
    except Exception as e:
        _homeworld_log(f"{name_for_log} path=sim.zone_id: exception {type(e).__name__}: {e}")

    _homeworld_log(f"{name_for_log}: ALL PATHS FAILED -- returning None")
    return None


def _season_context():
    """Return a minimal season context block.
    Just the season name — used as a consistency check, NOT as a topic suggestion.
    """
    season = sim_context.get_current_season()
    if not season:
        return ""
    return f"\n[SEASON: {season}]"


# Climate TYPE per world. The Sims 4 calendar is global -- Spring in Willow
# Creek means Spring in Oasis Springs too -- but each world's real-world
# climate analogue means different weather in the same season. We map each
# world to a climate "type" and use _CLIMATE_BY_SEASON below to build a
# season-aware description so the AI doesn't, say, invent snow in Sulani
# during a globally-Winter session.
_WORLD_CLIMATE_TYPE = {
    "Willow Creek": "humid_subtropical",
    "Oasis Springs": "desert",
    "Newcrest": "humid_subtropical",
    "Magnolia Promenade": "humid_subtropical",
    "Windenburg": "temperate_oceanic",
    "San Myshuno": "urban_continental",
    "Brindleton Bay": "new_england_coast",
    "Del Sol Valley": "mediterranean",
    "Sulani": "tropical",
    "Britechester": "humid_continental",
    "Evergreen Harbor": "pacific_northwest",
    "Mt. Komorebi": "alpine",
    "Henford-on-Bagley": "temperate_oceanic",
    "Copperdale": "humid_continental",
    "San Sequoia": "pacific_northwest",
    "Chestnut Ridge": "semi_arid_plains",
    "Tomarang": "tropical_monsoon",
    "Ravenwood": "pacific_northwest",
    "Ciudad Enamorada": "tropical",
    "Nordhaven": "subarctic",
    "Granite Falls": "mountain_forest",
    "Forgotten Hollow": "perpetual_gloom",
    "Selvadorada": "tropical",
    "StrangerVille": "desert_strange",
    "Glimmerbrook": "pacific_northwest",
    "Batuu": "alien_desert",
    "Tartosa": "mediterranean",
    "Moonwood Mill": "pacific_northwest",
    "Innisgreen": "temperate_oceanic",
    # EP20 Adventure Awaits -- New Zealand-inspired with all four seasons
    # AND geothermal hot springs that stay swimmable year-round.
    "Gibbi Point": "gibbi_geothermal",
}

# (climate_type, season) -> short description of likely current weather.
# Persistent climate constraints (e.g. "never snows") are included where
# they matter so the AI doesn't invent climatically-wrong weather.
_CLIMATE_BY_SEASON = {
    "desert": {
        "Spring": "mild and dry, sunny -- desert, no snow even in winter",
        "Summer": "very hot and dry, intensely sunny -- desert",
        "Fall": "warm and dry, sunny -- desert",
        "Winter": "mild and dry -- desert, doesn't snow",
    },
    "desert_strange": {
        "Spring": "warm and dry with a strange persistent haze -- desert, no snow",
        "Summer": "very hot and dry, eerie haze -- desert",
        "Fall": "warm and dry, haze lingers -- desert",
        "Winter": "mild and dry, haze -- desert, doesn't snow",
    },
    "alien_desert": {
        "Spring": "hot, dry, otherworldly -- alien desert, no Earth weather",
        "Summer": "very hot and dry -- alien desert",
        "Fall": "warm and dry -- alien desert",
        "Winter": "mild and dry -- alien desert, doesn't snow",
    },
    "tropical": {
        "Spring": "warm and humid, occasional rain -- tropical, never snows",
        "Summer": "hot and humid, frequent rain or storms -- tropical, never snows",
        "Fall": "warm and humid, rainy -- tropical, never snows",
        "Winter": "still warm and humid, occasional rain -- tropical, never snows",
    },
    "tropical_monsoon": {
        "Spring": "warm and humid -- tropical, never snows",
        "Summer": "hot, humid, heavy monsoon rains -- tropical, never snows",
        "Fall": "warm, humid, tail of monsoon rains -- never snows",
        "Winter": "still warm, drier season -- tropical, never snows",
    },
    "mediterranean": {
        "Spring": "warm and sunny, occasional rain -- Mediterranean",
        "Summer": "hot, dry, sunny -- Mediterranean",
        "Fall": "mild and pleasant, occasional rain -- Mediterranean",
        "Winter": "cool and sometimes rainy -- Mediterranean, rarely snows",
    },
    "semi_arid_plains": {
        "Spring": "mild, dry, big skies -- semi-arid",
        "Summer": "hot and dry, dusty -- semi-arid",
        "Fall": "warm and dry, crisp evenings -- semi-arid",
        "Winter": "cool and dry -- semi-arid, snow is uncommon",
    },
    "humid_subtropical": {
        "Spring": "mild, often rainy, humid",
        "Summer": "hot and humid, frequent thunderstorms",
        "Fall": "mild and pleasant",
        "Winter": "cool, occasional frost -- snow is rare",
    },
    "humid_continental": {
        "Spring": "mild, often rainy",
        "Summer": "warm and humid",
        "Fall": "crisp and cool",
        "Winter": "cold, often snowy",
    },
    "urban_continental": {
        "Spring": "mild, city showers",
        "Summer": "hot and humid in the concrete",
        "Fall": "cool, crisp",
        "Winter": "cold, snowy in the streets",
    },
    "temperate_oceanic": {
        "Spring": "cool, often rainy or misty",
        "Summer": "mild and pleasant, occasional rain",
        "Fall": "cool, damp, foggy mornings",
        "Winter": "cold, damp, frequent rain -- snow is occasional",
    },
    "new_england_coast": {
        "Spring": "cool, foggy, often rainy",
        "Summer": "warm, occasional sea fog",
        "Fall": "crisp, leaves turning, brisk wind off the coast",
        "Winter": "cold and snowy, coastal storms",
    },
    "pacific_northwest": {
        "Spring": "cool, rainy, often overcast",
        "Summer": "mild, occasional rain, sometimes sunny",
        "Fall": "cool, damp, leaves and rain",
        "Winter": "cold and rainy, occasional snow",
    },
    "mountain_forest": {
        "Spring": "cool, occasional rain, snow still lingering at altitude",
        "Summer": "mild and pleasant, cool nights",
        "Fall": "crisp and cold, leaves turning",
        "Winter": "cold and snowy",
    },
    "alpine": {
        "Spring": "still cold, snow lingering at altitude",
        "Summer": "mild lower down, can be chilly higher up",
        "Fall": "crisp, cold, first snow at altitude",
        "Winter": "deep snow, very cold -- snowy by default",
    },
    "subarctic": {
        "Spring": "cool, late thaw, long daylight building",
        "Summer": "mild with very long daylight hours",
        "Fall": "cool, damp, daylight shrinking",
        "Winter": "very cold and dark, snowy",
    },
    "perpetual_gloom": {
        "Spring": "perpetually overcast and foggy -- season barely matters",
        "Summer": "perpetually overcast and gloomy",
        "Fall": "perpetually overcast and foggy",
        "Winter": "perpetually overcast and gloomy -- cool, sometimes snow",
    },
    "gibbi_geothermal": {
        "Spring": "cool and damp, often misty -- geothermal hot springs stay swimmable year-round",
        "Summer": "mild and pleasant, lush green -- hot springs always warm",
        "Fall": "cool, foggy mornings, leaves turning -- hot springs still warm",
        "Winter": "cold with snow at altitude -- but the hot springs are still warm enough to swim in",
    },
}


def _get_world_climate(world_name, season=None):
    """Return a season-aware climate description for a world, or None.
    If `season` is None, falls back to a generic per-type description.
    """
    if not world_name:
        return None
    climate_type = _WORLD_CLIMATE_TYPE.get(world_name)
    if not climate_type:
        return None
    seasons = _CLIMATE_BY_SEASON.get(climate_type)
    if not seasons:
        return None
    if season and season in seasons:
        return seasons[season]
    # Generic fallback if season is unknown -- use Spring as a neutral default.
    return seasons.get("Spring")


def _weather_context(main_si, contact):
    """Build a [WEATHER: ...] block with two pieces of context:
      1. Player (callee) -- live weather where they actually are, since
         Sims 4 only simulates weather in the active zone. This is real,
         observable data and it's fair game to discuss.
      2. Caller -- climate-typical for their home world this season
         (we don't have live weather for non-active worlds).
    """
    other_si = contact.get("sim_info")
    other_home = _get_sim_home_world(other_si) if other_si else None

    current_world_raw = sim_context.get_current_world()
    current_world = _friendly_world_name(current_world_raw) if current_world_raw else None

    season = sim_context.get_current_season()
    current_weather = sim_context.get_current_weather()

    lines = []

    # Callee (player) -- live weather where they physically are. The
    # GEOGRAPHY / CURRENT LOCATION tags above already say WHERE they
    # are, so this just states what.
    if current_weather:
        lines.append(f"Live weather where the player is: {current_weather}.")
    elif current_world:
        climate = _get_world_climate(current_world, season)
        if climate:
            lines.append(
                f"Live weather not readable; climate-typical for player's "
                f"location ({current_world}): {climate}."
            )

    # Caller -- climate norms for their world (skip if same world as player).
    caller_reason = None
    if not other_home:
        caller_reason = "other_home=None (couldn't read caller's home world from sim_info)"
    elif current_world and other_home.lower() == current_world.lower():
        caller_reason = f"caller is in the same world as player ({other_home})"
    else:
        other_climate = _get_world_climate(other_home, season)
        if not other_climate:
            caller_reason = f"world {other_home!r} not in _WORLD_CLIMATE_TYPE map"
        else:
            season_label = f" this {season}" if season else ""
            lines.append(
                f"{contact['name']} is in {other_home}{season_label} -- "
                f"climate-typical: {other_climate}."
            )

    if caller_reason:
        try:
            import os, datetime
            path = os.path.join(os.path.expanduser("~"), "Documents", "Llamafone_Log.txt")
            with open(path, "a", encoding="utf-8") as f:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{ts}] [weather] caller-side skipped for {contact.get('name','?')}: {caller_reason}\n")
        except Exception:
            pass

    if not lines:
        return ""
    return (
        f"\n[WEATHER: {' '.join(lines)} "
        f"Background context. Dramatic weather (thunderstorm, heavy snow, "
        f"heatwave) is fair game to mention; routine weather usually isn't "
        f"worth bringing up. Don't open with weather.]"
    )


def _location_context(main_si, contact):
    """Build a short string describing where each sim lives AND where the
    recipient currently is (in case they're on vacation, etc.)."""
    main_home = _get_sim_home_world(main_si) if main_si else None
    other_si = contact.get("sim_info")
    other_home = _get_sim_home_world(other_si) if other_si else None

    # Current location of the recipient — usually their home world, but if the
    # player is on vacation, it'll be the vacation world (e.g. Tartosa).
    current_world_raw = sim_context.get_current_world()
    current_world = _friendly_world_name(current_world_raw) if current_world_raw else None

    vacation_note = ""
    if main_si and current_world and main_home and current_world.lower() != main_home.lower():
        vacation_note = (
            f"\n[CURRENT LOCATION: {main_si.first_name} is currently in {current_world} "
            f"(traveling/on vacation — NOT at home in {main_home}). "
            f"Do not assume they are home or back from this trip.]"
        )

    if main_home and other_home:
        if main_home.lower() == other_home.lower():
            return f"\n[GEOGRAPHY: Both live in {main_home} — SAME world, in-person plans OK]{vacation_note}"
        return (
            f"\n[GEOGRAPHY: {main_si.first_name} lives in {main_home}, "
            f"{contact['name']} lives in {other_home} — DIFFERENT worlds, "
            f"NO casual in-person meetups, long-distance only]"
            f"{vacation_note}"
        )
    elif main_home:
        return f"\n[GEOGRAPHY: {main_si.first_name} lives in {main_home}]{vacation_note}"
    elif other_home:
        return f"\n[GEOGRAPHY: {contact['name']} lives in {other_home}]"
    return ""


_AGE_RANK = {
    "BABY": 0, "INFANT": 0, "TODDLER": 1, "CHILD": 2, "TEEN": 3,
    "YOUNGADULT": 4, "YOUNG_ADULT": 4,
    "ADULT": 5, "ELDER": 6,
}


def _age_rank(sim_si):
    """Return a numeric age rank (higher = older), or None if unknown."""
    if not sim_si:
        return None
    try:
        age_str = str(getattr(sim_si, "age", "")).replace("Age.", "").upper().replace(" ", "")
        return _AGE_RANK.get(age_str)
    except Exception:
        return None


def _get_family_relationship(other_si, contact, recipient=None):
    """
    Try to determine the precise family relationship between the recipient (or protagonist)
    and the other sim using genealogy tracker and relationship bits.
    Returns a string like "Father", "Daughter", "Grandfather", "Sibling" or None.
    Applies age sanity checks — a younger sim can never be labeled the parent of an
    older sim (catches corrupted genealogy data).
    """
    main_si = recipient or sim_context.get_main_sim_info()
    if not main_si or not other_si:
        return None

    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
    is_male = (gender == "MALE")

    def male_or(male_label, female_label):
        return male_label if is_male else female_label

    # Age sanity: a sim can never be the parent of someone the same age or older,
    # nor the child of someone the same age or younger.
    main_rank = _age_rank(main_si)
    other_rank = _age_rank(other_si)
    other_can_be_parent_of_main = (
        main_rank is None or other_rank is None or other_rank >= main_rank
    )
    other_can_be_child_of_main = (
        main_rank is None or other_rank is None or other_rank <= main_rank
    )
    # Strict comparators — used to flip direction when bits know a relationship
    # exists but can't tell us who's the parent.
    other_is_older = (
        main_rank is not None and other_rank is not None and other_rank > main_rank
    )
    other_is_younger = (
        main_rank is not None and other_rank is not None and other_rank < main_rank
    )

    # Try genealogy tracker first — most precise. Walks up to 2 generations.
    try:
        from sims.genealogy_tracker import FamilyRelationshipIndex
        gen = main_si.genealogy
        other_gen = other_si.genealogy

        def _parent_ids(g):
            ids = set()
            if not g:
                return ids
            for idx in (FamilyRelationshipIndex.MOTHER, FamilyRelationshipIndex.FATHER):
                try:
                    pid = g.get_family_relationship(idx)
                    if pid:
                        ids.add(pid)
                except Exception:
                    pass
            return ids

        my_parents = _parent_ids(gen)
        their_parents = _parent_ids(other_gen)

        # 1. Direct parent: other is one of my parents (age-gated)
        if other_si.sim_id in my_parents and other_can_be_parent_of_main:
            return male_or("Father", "Mother")

        # 2. Direct child: I am one of other's parents (age-gated)
        if main_si.sim_id in their_parents and other_can_be_child_of_main:
            return male_or("Son", "Daughter")

        # 3. Sibling: share a parent
        if my_parents and their_parents and (my_parents & their_parents):
            return male_or("Brother", "Sister")

        # 4. Grandparent: other is a parent of my parent
        try:
            import services
            sm = services.sim_info_manager()
            grandparent_ids = set()
            for pid in my_parents:
                psi = sm.get(pid)
                if psi:
                    grandparent_ids |= _parent_ids(psi.genealogy)
            if other_si.sim_id in grandparent_ids and other_can_be_parent_of_main:
                return male_or("Grandfather", "Grandmother")

            # 5. Grandchild: I am a parent of one of other's parents
            their_grandparent_ids = set()
            for pid in their_parents:
                psi = sm.get(pid)
                if psi:
                    their_grandparent_ids |= _parent_ids(psi.genealogy)
            if main_si.sim_id in their_grandparent_ids and other_can_be_child_of_main:
                return male_or("Grandson", "Granddaughter")

            # 5b. Great-grandparent: other is a parent of one of my grandparents
            great_grandparent_ids = set()
            for gpid in grandparent_ids:
                gpsi = sm.get(gpid)
                if gpsi:
                    great_grandparent_ids |= _parent_ids(gpsi.genealogy)
            if other_si.sim_id in great_grandparent_ids and other_can_be_parent_of_main:
                return male_or("Great-Grandfather", "Great-Grandmother")

            # 5c. Great-grandchild: I am a parent of one of other's grandparents
            their_great_grandparent_ids = set()
            for gpid in their_grandparent_ids:
                gpsi = sm.get(gpid)
                if gpsi:
                    their_great_grandparent_ids |= _parent_ids(gpsi.genealogy)
            if main_si.sim_id in their_great_grandparent_ids and other_can_be_child_of_main:
                return male_or("Great-Grandson", "Great-Granddaughter")

            # 6. Aunt/Uncle: other is a sibling of one of my parents
            for pid in my_parents:
                psi = sm.get(pid)
                if not psi:
                    continue
                p_parents = _parent_ids(psi.genealogy)
                o_parents = _parent_ids(other_gen)
                if p_parents and o_parents and (p_parents & o_parents) and pid != other_si.sim_id:
                    return male_or("Uncle", "Aunt")

            # 7. Niece/Nephew: I am a sibling of one of other's parents
            for pid in their_parents:
                psi = sm.get(pid)
                if not psi:
                    continue
                p_parents = _parent_ids(psi.genealogy)
                m_parents = _parent_ids(gen)
                if p_parents and m_parents and (p_parents & m_parents) and pid != main_si.sim_id:
                    return male_or("Nephew", "Niece")

            # --- In-law detection: find my spouse, then check other's relation to them ---
            spouse_id = None
            try:
                my_rt = main_si.relationship_tracker
                for tid in my_rt.target_sim_gen():
                    try:
                        bits = list(my_rt.get_all_bits(tid))
                        if not bits or _has_platonic_bit(bits):
                            continue
                        for b in bits:
                            bn = sim_context._get_trait_name(b).lower()
                            if "spouse" in bn or ("married" in bn and "unmarried" not in bn):
                                spouse_id = tid
                                break
                        if spouse_id:
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            if spouse_id and spouse_id != other_si.sim_id:
                spouse = sm.get(spouse_id)
                spouse_gen = spouse.genealogy if spouse else None
                spouse_parents = _parent_ids(spouse_gen)

                # 8. Parent-in-law: other is a parent of my spouse
                if other_si.sim_id in spouse_parents:
                    return male_or("Father-in-law", "Mother-in-law")

                # 9. Sibling-in-law: other and my spouse share a parent (and other isn't me)
                if spouse_parents and their_parents and (spouse_parents & their_parents) and other_si.sim_id != main_si.sim_id:
                    return male_or("Brother-in-law", "Sister-in-law")

                # 10. Child-in-law: my spouse is one of other's parents (other married my kid)
                # — covered by case 2 (Son/Daughter) if it's actually my kid; otherwise too rare
        except Exception:
            pass
    except Exception:
        pass

    # Fallback: check relationship bits, MOST SPECIFIC first
    # ("parent" matches "grandparent" as substring, so check grandparent first!)
    try:
        rt = main_si.relationship_tracker
        bits = rt.get_all_bits(other_si.sim_id)
        if bits:
            # Collect all bit names first so we can prioritise
            bit_names = []
            for bit in bits:
                try:
                    bit_names.append(sim_context._get_trait_name(bit).lower())
                except Exception:
                    pass

            def any_bit(*keywords):
                return any(any(kw in bn for kw in keywords) for bn in bit_names)

            # Detect in-law bits explicitly (use normalised compact form so
            # "Is_Parent_In_Law_Of" matches as "parentinlaw")
            compact_bits = [bn.replace("_", "").replace("-", "") for bn in bit_names]

            def has_compact(substr):
                return any(substr in cb for cb in compact_bits)

            # IN-LAWS — check before generic parent/child to avoid the substring bug.
            if has_compact("parentinlaw"):
                return male_or("Father-in-law", "Mother-in-law")
            if has_compact("childinlaw") or has_compact("inlawchild"):
                return male_or("Son-in-law", "Daughter-in-law")
            if has_compact("siblinginlaw") or has_compact("brotherinlaw") or has_compact("sisterinlaw"):
                return male_or("Brother-in-law", "Sister-in-law")

            # Other specific terms first (grandparent/grandchild gated by age too)
            if any_bit("grandparent") and other_can_be_parent_of_main:
                return male_or("Grandfather", "Grandmother")
            if any_bit("grandchild", "grandson", "granddaughter") and other_can_be_child_of_main:
                return male_or("Grandson", "Granddaughter")
            if any_bit("aunt", "uncle"):
                return male_or("Uncle", "Aunt")
            if any_bit("niece", "nephew"):
                return male_or("Nephew", "Niece")
            if any_bit("cousin"):
                return "Cousin"
            if any_bit("spouse", "married"):
                return male_or("Husband", "Wife")
            if any_bit("sibling", "brother", "sister"):
                return male_or("Brother", "Sister")
            # Directional family bits — the game encodes "Target IsXOf Actor" patterns
            # which tell us unambiguously which sim has which role. These trump age.
            if has_compact("targetisparentof") or has_compact("isparentof"):
                # other_si is the Target, recipient is the Actor — Target is Actor's parent
                return male_or("Father", "Mother")
            if has_compact("targetischildof") or has_compact("ischildof"):
                # Target is the child of Actor — other_si is the recipient's child
                return male_or("Son", "Daughter")
            if has_compact("targetissiblingof") or has_compact("issiblingof"):
                return male_or("Brother", "Sister")

            # Generic parent/child LAST — the bit only tells us a parent-child
            # relationship exists; it doesn't tell us who's the parent. Use age
            # to determine direction (a younger sim is the child, older is the parent).
            parent_child_bit = (
                any_bit("parent")
                and not any_bit("grandparent")
                and not has_compact("inlaw")
            ) or (
                (any_bit("offspring") or any(("child" in bn and "grandchild" not in bn) for bn in bit_names))
                and not has_compact("inlaw")
            )
            if parent_child_bit:
                if other_is_older:
                    return male_or("Father", "Mother")
                if other_is_younger:
                    return male_or("Son", "Daughter")
                # Same age or unknown — direction can't be inferred; skip rather than guess
    except Exception:
        pass

    # Last-resort: check bits from the OTHER side of the relationship.
    # Sims 4 normally writes family bits symmetrically, but saves can drift --
    # e.g. Francesca's tracker still says "Apollo is my Father" while Apollo's
    # tracker has dropped the matching "Francesca is my Daughter" bit. By
    # reading other_si's view of main_si and inverting it, we recover the
    # relationship without needing to mutate the save.
    try:
        other_rt = other_si.relationship_tracker
        other_bits = other_rt.get_all_bits(main_si.sim_id)
        if other_bits:
            other_bit_names = []
            for bit in other_bits:
                try:
                    other_bit_names.append(sim_context._get_trait_name(bit).lower())
                except Exception:
                    pass
            compact = [bn.replace("_", "").replace("-", "") for bn in other_bit_names]

            def other_has_compact(substr):
                return any(substr in cb for cb in compact)

            def other_any_bit(*keywords):
                return any(any(kw in bn for kw in keywords) for bn in other_bit_names)

            # If the OTHER sim's tracker says main_si is their parent,
            # then main_si is the parent => other_si is main_si's child.
            if other_has_compact("targetisparentof") or other_has_compact("isparentof"):
                if other_can_be_child_of_main:
                    return male_or("Son", "Daughter")
            # If the other sim's tracker says main_si is their child,
            # then main_si is the child => other_si is the parent.
            if other_has_compact("targetischildof") or other_has_compact("ischildof"):
                if other_can_be_parent_of_main:
                    return male_or("Father", "Mother")
            # Generic parent bit on the other side, direction inferred by age.
            if (other_any_bit("parent") and not other_any_bit("grandparent")
                    and not other_has_compact("inlaw")):
                if other_is_younger:
                    return male_or("Son", "Daughter")
                if other_is_older:
                    return male_or("Father", "Mother")
            # Generic child / offspring bit on the other side.
            if ((other_any_bit("offspring")
                 or any(("child" in bn and "grandchild" not in bn) for bn in other_bit_names))
                    and not other_has_compact("inlaw")):
                if other_is_younger:
                    return male_or("Son", "Daughter")
                if other_is_older:
                    return male_or("Father", "Mother")
            if other_any_bit("sibling", "brother", "sister"):
                return male_or("Brother", "Sister")
    except Exception:
        pass

    return None


def _describe_relationship(contact, recipient=None):
    """Build a detailed character description for the prompt.
    All facts are explicitly labeled as belonging to the contact, not the player,
    to prevent attribute bleed in the AI's response.
    recipient is the household sim being contacted (for family relationship lookup)."""
    name = contact['name']
    parts = [f"=== Character: {name} (THE CALLER/SENDER, NOT the player) ==="]

    si = contact.get("sim_info")
    if si:
        try:
            age = str(getattr(si, "age", "")).replace("Age.", "")
            if age:
                parts.append(f"{name}'s age: {age}")
        except Exception:
            pass

        try:
            gender = str(getattr(si, "gender", "")).replace("Gender.", "")
            if gender:
                parts.append(f"{name}'s gender: {gender}")
        except Exception:
            pass

        traits = sim_context.get_sim_traits(si, limit=6)
        if traits:
            parts.append(f"{name}'s traits: {', '.join(traits)}")

        mood = sim_context.get_sim_mood(si)
        parts.append(f"{name}'s current mood: {mood}")

        career = sim_context.get_sim_career(si)
        if career:
            parts.append(f"{name}'s career: {career}")

        aspiration = sim_context.get_sim_aspiration(si)
        if aspiration:
            parts.append(f"{name}'s aspiration: {aspiration}")

        try:
            skills = sim_context.get_sim_skills(si, limit=3)
            if skills:
                skill_str = ", ".join(f"{sk} {lvl}" for sk, lvl in skills.items())
                parts.append(f"{name}'s top skills: {skill_str}")
        except Exception:
            pass

        clubs = sim_context.get_sim_clubs(si)
        if clubs:
            parts.append(f"{name}'s clubs: {', '.join(clubs)}")

        home = _get_sim_home_world(si)
        if home:
            parts.append(f"{name} lives in: {home}")

    family_label = _get_family_relationship(si, contact, recipient=recipient) if si else None
    if family_label:
        parts.append(f"{name} is the player's {family_label}")

    if contact.get("status"):
        status = contact['status']
        if not family_label:
            parts.append(f"{name}'s relationship to the player: {status}")
        elif status and not any(kw in status for kw in ("Family", "Parent", "Child", "Sibling")):
            parts.append(f"{name} is also: {status}")
    f_score = contact.get("friendship")
    if f_score is not None:
        f_label = _friendship_label(f_score)
        if f_label:
            parts.append(f"How {name} feels about the player: {f_label}")
    romance = contact.get("romance")
    if romance is not None and romance != 0 and not family_label:
        r_label = _romance_label(romance)
        if r_label:
            parts.append(f"Romantic feelings: {r_label}")

    # If the relationship has explicitly changed (breakup, divorce, etc.) OR romance
    # has gone negative, surface that as a clear warning so the AI doesn't continue
    # treating them like a current partner based on past chat history.
    status = contact.get("status", "") or ""
    status_low = status.lower().replace("_", "").replace(" ", "")
    is_ex = ("broken" in status_low or "ex" in status_low.split() or
             "former" in status_low or "divorced" in status_low)
    is_estranged = ("nolonger" in status_low or "estranged" in status_low or
                    "hasbeenfriends" in status_low or "lostfriends" in status_low)
    if is_ex or is_estranged or (romance is not None and romance < 0):
        parts.append(
            "RELATIONSHIP STATUS NOTE: This is a former romantic relationship — "
            "they are NOT currently dating/together. Any past affectionate or flirty "
            "journal history is from BEFORE the breakup and should NOT shape the "
            "current tone. Treat the current dynamic as awkward, tense, or distant "
            "depending on the romance score."
        )

    if contact.get("in_household") is True:
        parts.append("Lives in the same household as the player")

    # Romantic / marital status -- crucial context so the LLM doesn't
    # write a married sim hitting dating apps, etc.
    if si:
        try:
            partner_si, rstatus = _get_romantic_partner_info(si)
            if partner_si and rstatus:
                pname = f"{partner_si.first_name} {partner_si.last_name}".strip()
                parts.append(f"{name} is {rstatus} {pname}")
        except Exception:
            pass

    # Recent life events the sim might want to talk about (or that the
    # player might bring up). Pulled from milestones tracker.
    if si:
        try:
            from . import milestones as _milestones
            mblock = _milestones.format_for_prompt(si)
            if mblock:
                parts.append(mblock)
        except Exception:
            pass

    # (Contact preferences used to be injected here, but were moved
    # to the END of the user prompt via _contact_prefs_block so recency
    # gives the AI maximum incentive to respect them. See callers of
    # _contact_prefs_block in generate_text / generate_call / etc.)

    # Shared group text context: if both this household sim AND the
    # contact were recently in the same group thread, surface an
    # excerpt so the AI can reference it naturally ('we were both in
    # that thread with Bob yesterday'). Cross-thread continuity is
    # exactly what makes 1:1 replies feel like real relationships.
    if si:
        try:
            main_si = recipient or sim_context.get_main_sim_info()
            household_id = getattr(main_si, "sim_id", None) if main_si else None
            household_name = main_si.first_name if main_si else None
            other_id = getattr(si, "sim_id", None)
            shared_block = group_texts.format_shared_for_prompt(
                household_id, other_id,
                sim_a_name=household_name, sim_b_name=name,
            )
            if shared_block:
                parts.append(shared_block)
        except Exception:
            pass

    return "\n".join(parts)


def _friendship_label(score):
    """
    Convert a Sims 4 friendship score to a closeness label.
    Positive scores only indicate degrees of closeness — never tension.
    Tension only appears at NEGATIVE scores.

    Scores between -19 and +9 collapse into "barely know each other" — they
    represent sims who've met once or twice but never developed a real
    relationship. Triggers "wait, who is this?" reactions in the prompts.
    """
    if score is None:
        return None
    if score >= 75:
        return "best friends, very close"
    if score >= 45:
        return "close friends"
    if score >= 20:
        return "friends, get along well"
    if score >= 10:
        return "friendly acquaintances"
    if score >= -19:
        return "barely know each other -- might not remember the player clearly"
    if score >= -40:
        return "have some negative history"
    if score >= -70:
        return "actively dislike each other"
    return "enemies"


def _romance_label(score):
    """Convert a Sims 4 romance score to a label.
    Positive = degrees of attraction. Negative = degrees of romantic friction
    (post-breakup tension, bitterness, etc.) — important to surface so the AI
    doesn't treat an ex like a current crush.
    """
    if score is None or score == 0:
        return None
    if score >= 75:
        return "deeply in love"
    if score >= 45:
        return "strong romantic attraction"
    if score >= 20:
        return "growing romantic interest"
    if score >= 1:
        return "mild attraction"
    # Negative romance — they were once romantic, now there's friction
    if score >= -19:
        return "lingering awkwardness from past romance"
    if score >= -49:
        return "post-breakup friction — recent or unresolved"
    return "bitter breakup / hostile former romance"


def _format_conversation_history(history, main_name, other_name):
    """Format conversation history into a prompt-readable string."""
    lines = []
    for msg in history:
        name = main_name if msg["role"] == "you" else other_name
        lines.append(f"{name}: {msg['text']}")
    return "\n".join(lines)


def _conv_key(recipient_sim, contact):
    """Compute the (anchor_sim_id, contact_sim_id) tuple used to key
    _conversations. Returns a stable tuple even when either input is
    partially resolvable -- falls back to 0 for the missing slot so
    the entry can still be inserted / looked up consistently."""
    anchor_id = getattr(recipient_sim, "sim_id", None) if recipient_sim else None
    contact_si = (contact or {}).get("sim_info")
    contact_id = getattr(contact_si, "sim_id", None) if contact_si else None
    return (int(anchor_id) if anchor_id else 0, int(contact_id) if contact_id else 0)


def _start_conversation(contact, first_message, recipient_sim=None, kind="text"):
    """Store a new conversation, keyed by (anchor_sim_id, contact_sim_id).

    `kind` is "call" or "text" -- generate_reply() uses it to decide
    whether to apply the artificial "sim is thinking" reply_delay. Texts
    feel weirder when they appear instantly, so they get the delay;
    calls are a live conversation, so they should hit the popup as
    soon as the AI returns."""
    global _last_active_key
    ckey = _conv_key(recipient_sim, contact)
    import datetime as _dt
    _conversations[ckey] = {
        "contact": contact,
        "recipient": recipient_sim,
        "kind": kind,
        "history": [{"role": "them", "text": first_message}],
        # Timestamp when this conversation began. Used by reply flows to
        # ask the journal for entries STRICTLY BEFORE this time -- so the
        # 'Past interactions' block doesn't duplicate what's already in
        # 'Conversation so far'. Every turn we send/receive gets logged
        # to the journal in real time, so without this filter, the last
        # 3-4 turns would show up in both blocks.
        "started_at": _dt.datetime.now().isoformat(),
    }
    _last_active_key = ckey


def _mark_reply_intent(recipient_sim, caller_sim_info=None):
    """Called when the player clicks the Reply button on a phone dialog.
    Locks in which SPECIFIC (anchor, contact) conversation the next
    llama.reply should target.

    Before v3.4.1 this only stored the anchor -- which broke when two
    contacts had texted the same household member in sequence
    (the more-recent contact's conversation clobbered the earlier one,
    so replying to the earlier one silently routed to the newer contact).
    Now the pair is stored so the correct thread is selected regardless
    of which one is "the most recent" in memory."""
    global _pending_reply_key
    anchor_id = getattr(recipient_sim, "sim_id", None) if recipient_sim else None
    contact_id = getattr(caller_sim_info, "sim_id", None) if caller_sim_info else None
    if not anchor_id:
        return
    _pending_reply_key = (int(anchor_id), int(contact_id) if contact_id else 0)


def _take_conversation_for_reply():
    """Pick the right conversation when the player runs llama.reply.
    Priority:
      1. Exact (anchor, contact) pair from the most recent Reply-button
         click (cleared after use). This is the strong signal -- the
         player pointed at a specific message.
      2. If Reply-button intent has only the anchor (contact unknown or
         legacy), fall back to any conversation for that anchor.
      3. Conversation for the currently selected/active sim (matches any
         contact for that anchor).
      4. Most-recently-started conversation across all pairs.
    """
    global _pending_reply_key
    if _pending_reply_key and _pending_reply_key in _conversations:
        convo = _conversations[_pending_reply_key]
        _pending_reply_key = None
        return convo
    # If we have an anchor but no matching contact key, search for
    # any conversation with that anchor (best-effort fallback).
    if _pending_reply_key:
        anchor_id = _pending_reply_key[0]
        _pending_reply_key = None
        for k, v in _conversations.items():
            if k[0] == anchor_id:
                return v
    try:
        active = sim_context.get_active_sim()
        if active and active.sim_info:
            aid = active.sim_info.sim_id
            for k, v in _conversations.items():
                if k[0] == aid:
                    return v
    except Exception:
        pass
    if _last_active_key is not None and _last_active_key in _conversations:
        return _conversations[_last_active_key]
    return None


def get_active_conversation():
    """Return the conversation that llama.reply would currently target, or None.
    NOTE: does not consume the pending-reply flag."""
    if _pending_reply_key and _pending_reply_key in _conversations:
        return _conversations[_pending_reply_key]
    if _pending_reply_key:
        anchor_id = _pending_reply_key[0]
        for k, v in _conversations.items():
            if k[0] == anchor_id:
                return v
    try:
        active = sim_context.get_active_sim()
        if active and active.sim_info:
            aid = active.sim_info.sim_id
            for k, v in _conversations.items():
                if k[0] == aid:
                    return v
    except Exception:
        pass
    if _last_active_key is not None and _last_active_key in _conversations:
        return _conversations[_last_active_key]
    return None


def generate_call(callback=None, output=None):
    """Generate an incoming phone call to a random teen+ household member."""
    recipient, contact = _pick_recipient_and_contact()
    if not recipient:
        msg = "No eligible household members (teen or older) found to receive a call."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return
    if not contact:
        msg = "No household member has any plausible contacts to call them right now."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    recipient_name = recipient.first_name

    _refresh_milestones_for(contact, recipient)

    language = config.get_language()
    system = _CALL_SYSTEM.format(language=language)
    rel_desc = _describe_relationship(contact, recipient=recipient)

    contact_id = getattr(contact.get("sim_info"), "sim_id", None)
    recipient_sim_id = getattr(recipient, "sim_id", None)
    sim_history = journal.format_sim_history_for_prompt(
        contact["name"],
        recipient_name=recipient_name,
        trailing_note=_journal_obsolescence_note(contact),
        sim_id=contact_id,
        recipient_id=recipient_sim_id,
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact, recipient=recipient)
    mutual_block = _format_mutual_block(mutuals, casual=True)


    recipient_block = _describe_recipient(recipient, contact=contact)

    events_text = events.format_shared_events_for_prompt(recipient, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""
    past_events_text = past_events.format_for_prompt(contact_id, recipient_sim_id)
    past_events_block = f"\n\n{past_events_text}" if past_events_text else ""

    last_conv_iso = journal.last_entry_timestamp_for_pair(contact_id, recipient_sim_id)
    interaction_tag = interactions.format_for_prompt(contact_id, recipient_sim_id, last_conv_iso=last_conv_iso)

    from . import LOAD_TIMESTAMP as _LT
    prompt = (
        f"[llamafone build loaded at {_LT}]\n\n"
        f"Caller info:\n{rel_desc}{history_block}{mutual_block}\n\n"
        f"{recipient_block}{events_block}{past_events_block}\n\n"
        f"They are calling {recipient_name}{_location_context(recipient, contact)}.{_season_context()}{_weather_context(recipient, contact)}{interaction_tag}"
        f"{_contact_prefs_block(recipient, contact.get('sim_info'), contact['name'])}\n\n"
        f"Write what {contact['name']} says during this phone call."
    )

    def _on_result(text, error):
        title = f"Call from {contact['name']}"
        if text:
            text = _apply_mood_from_text(text, recipient=recipient, is_incoming=True)
            _start_conversation(contact, text, recipient_sim=recipient, kind="call")
            journal.add_entry(
                "call",
                f"Call from {contact['name']} (to {recipient_name}):\n{text}",
                sim_name=contact["name"],
                recipient_name=recipient_name,
                sim_id=contact_id,
                recipient_id=recipient_sim_id,
            )
            _maybe_auto_prefs_from_message(
                recipient_sim_id, contact_id, contact["name"], text,
                source_label="they called",
            )
            caller_si = contact.get("sim_info")
            shown = False
            if caller_si:
                shown = _show_phone_dialog(caller_si, title, text, recipient_sim_info=recipient)
            if not shown:
                notifications.show(title, text, output=output)
        elif error:
            notifications.show_error(error, output=output)
        if callback:
            callback(text, error)

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_result,
    )


def generate_text(callback=None, output=None):
    """Generate an incoming text to a random teen+ household member."""
    recipient, contact = _pick_recipient_and_contact()
    if not recipient:
        msg = "No eligible household members (teen or older) found to receive a text."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return
    if not contact:
        msg = "No household member has any plausible contacts to text them right now."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    recipient_name = recipient.first_name

    _refresh_milestones_for(contact, recipient)

    language = config.get_language()
    system = _TEXT_SYSTEM.format(language=language)
    rel_desc = _describe_relationship(contact, recipient=recipient)

    contact_id = getattr(contact.get("sim_info"), "sim_id", None)
    recipient_sim_id = getattr(recipient, "sim_id", None)
    sim_history = journal.format_sim_history_for_prompt(
        contact["name"],
        recipient_name=recipient_name,
        trailing_note=_journal_obsolescence_note(contact),
        sim_id=contact_id,
        recipient_id=recipient_sim_id,
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact, recipient=recipient)
    mutual_block = _format_mutual_block(mutuals, casual=True)


    recipient_block = _describe_recipient(recipient, contact=contact)

    events_text = events.format_shared_events_for_prompt(recipient, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""
    past_events_text = past_events.format_for_prompt(contact_id, recipient_sim_id)
    past_events_block = f"\n\n{past_events_text}" if past_events_text else ""

    last_conv_iso = journal.last_entry_timestamp_for_pair(contact_id, recipient_sim_id)
    interaction_tag = interactions.format_for_prompt(contact_id, recipient_sim_id, last_conv_iso=last_conv_iso)

    prompt = (
        f"Sender info:\n{rel_desc}{history_block}{mutual_block}\n\n"
        f"{recipient_block}{events_block}{past_events_block}\n\n"
        f"They are texting {recipient_name}{_location_context(recipient, contact)}.{_season_context()}{_weather_context(recipient, contact)}{interaction_tag}"
        f"{_contact_prefs_block(recipient, contact.get('sim_info'), contact['name'])}\n\n"
        f"Write 1-2 short text messages from {contact['name']}."
    )

    def _on_result(text, error):
        title = f"Text from {contact['name']}"
        if text:
            text = _apply_mood_from_text(text, recipient=recipient, is_incoming=True)
            _start_conversation(contact, text, recipient_sim=recipient)
            journal.add_entry(
                "text",
                f"Text from {contact['name']} (to {recipient_name}):\n{text}",
                sim_name=contact["name"],
                recipient_name=recipient_name,
                sim_id=contact_id,
                recipient_id=recipient_sim_id,
            )
            _maybe_auto_prefs_from_message(
                recipient_sim_id, contact_id, contact["name"], text,
                source_label="they texted",
            )
            sender_si = contact.get("sim_info")
            shown = False
            if sender_si:
                shown = _show_phone_dialog(sender_si, title, text, ring=False, recipient_sim_info=recipient)
            if not shown:
                notifications.show(title, text, output=output)
        elif error:
            notifications.show_error(error, output=output)
        if callback:
            callback(text, error)

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_result,
    )


def generate_reply(player_message, callback=None, output=None):
    """
    Reply to the conversation the player most recently signalled intent for
    (via Reply button or active sim selection), and the other sim responds.
    """
    conversation = _take_conversation_for_reply()
    if not conversation:
        msg = "No active conversation. Use llama.call or llama.text first to start one."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    contact = conversation["contact"]
    history = conversation["history"]
    recipient = conversation.get("recipient")

    # Add the player's message to history
    history.append({"role": "you", "text": player_message})

    # Auto-detect distance signals in what the PLAYER just typed.
    # Scoped to (household_sim, contact_sim): the "recipient" on the
    # conversation dict is the household side; fall back to main sim
    # if the conversation didn't stash one.
    _contact_id_for_auto = getattr(contact.get("sim_info"), "sim_id", None) if contact else None
    _household_for_auto = recipient or sim_context.get_main_sim_info()
    _household_id_for_auto = getattr(_household_for_auto, "sim_id", None) if _household_for_auto else None
    _maybe_auto_prefs_from_message(
        _household_id_for_auto, _contact_id_for_auto,
        contact.get("name", "them"), player_message,
        source_label="you replied",
    )

    # The "main_name" here is the household member who received the original message
    if recipient:
        main_name = recipient.first_name
    else:
        main_si = sim_context.get_main_sim_info()
        main_name = main_si.first_name if main_si else "your Sim"
    other_name = contact["name"]

    _refresh_milestones_for(contact, recipient)

    language = config.get_language()
    system = _REPLY_SYSTEM.format(
        language=language,
        other_name=other_name,
        main_name=main_name,
    )
    rel_desc = _describe_relationship(contact, recipient=recipient)
    convo_text = _format_conversation_history(history, main_name, other_name)
    contact_id = getattr(contact.get("sim_info"), "sim_id", None)
    main_sim_id = getattr(recipient, "sim_id", None) if recipient else (
        getattr(sim_context.get_main_sim_info(), "sim_id", None)
    )
    # Filter journal entries to those STRICTLY BEFORE this conversation
    # started -- otherwise the ongoing turns (logged live as each reply
    # generates) get duplicated between 'Past interactions' and
    # 'Conversation so far'.
    _convo_start = conversation.get("started_at")
    sim_history = journal.format_sim_history_for_prompt(
        other_name,
        recipient_name=main_name,
        trailing_note=_journal_obsolescence_note(contact),
        sim_id=contact_id,
        recipient_id=main_sim_id,
        before_iso=_convo_start,
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact, recipient=recipient)
    mutual_block = _format_mutual_block(mutuals, casual=False)

    events_text = events.format_shared_events_for_prompt(recipient, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""
    past_events_text = past_events.format_for_prompt(contact_id, main_sim_id)
    past_events_block = f"\n\n{past_events_text}" if past_events_text else ""

    geo_main = recipient if recipient else sim_context.get_main_sim_info()
    last_conv_iso = journal.last_entry_timestamp_for_pair(contact_id, main_sim_id)
    interaction_tag = interactions.format_for_prompt(contact_id, main_sim_id, last_conv_iso=last_conv_iso)
    context_tags = (
        f"{_location_context(geo_main, contact)}"
        f"{_season_context()}"
        f"{_weather_context(geo_main, contact)}"
        f"{interaction_tag}"
    )

    prompt = (
        f"Relationship info:\n{rel_desc}{history_block}{mutual_block}{events_block}{past_events_block}\n\n"
        f"Conversation so far:\n{convo_text}"
        f"{context_tags}"
        f"{_contact_prefs_block(recipient, contact.get('sim_info'), other_name)}\n\n"
        f"Write {other_name}'s reply (1-3 short text messages)."
    )

    def _on_result(text, error):
        if error:
            # Errors fire immediately; no point delaying a failure popup.
            if history and history[-1]["role"] == "you":
                history.pop()
            notifications.show_error(error, output=output)
            if callback:
                callback(text, error)
            return
        if not text:
            if callback:
                callback(text, error)
            return

        text_clean = _apply_mood_from_text(text, recipient=recipient, is_incoming=False)
        # Calls are a live two-way conversation -- the recipient is on the
        # other end of the line, so the reply should land the instant the
        # AI returns. Texts get the artificial "sim is thinking" delay so
        # they feel asynchronous and natural.
        kind = conversation.get("kind", "text")
        delay = 0 if kind == "call" else _calculate_reply_delay(contact)

        def _show_reply():
            # Wrapped in try/except so an exception on this Timer thread
            # doesn't silently kill the reply-delay callback -- Python's
            # threading.Timer swallows exceptions from its target and
            # provides no visibility. Log any raise so we see it.
            try:
                history.append({"role": "them", "text": text_clean})
                journal.add_entry(
                    kind,
                    f"Conversation with {other_name}:\n"
                    f"{main_name}: {player_message}\n"
                    f"{other_name}: {text_clean}",
                    sim_name=other_name,
                    recipient_name=main_name,
                    sim_id=contact_id,
                    recipient_id=main_sim_id,
                )
                # Auto-detect distance signals in this reply. Scoped
                # to (household_sim, contact_sim). main_sim_id is the
                # household side computed earlier in this function.
                _maybe_auto_prefs_from_message(
                    main_sim_id, contact_id, other_name, text_clean,
                    source_label=f"they {kind}ed",
                )
                title = f"Reply from {other_name}"
                sender_si = contact.get("sim_info")
                shown = False
                if sender_si:
                    # Calls ring; texts buzz quietly. Mirrors how the original
                    # incoming-call vs incoming-text dialogs feel.
                    ring = (kind == "call")
                    shown = _show_phone_dialog(sender_si, title, text_clean, ring=ring, recipient_sim_info=recipient)
                if not shown:
                    notifications.show(title, text_clean, output=output)
                if callback:
                    callback(text_clean, None)
            except Exception as _e:
                _log_error(f"generate_reply._show_reply raised: {type(_e).__name__}: {_e}")

        if delay > 0:
            t = threading.Timer(delay, _show_reply)
            t.daemon = True
            _track_timer(t)
            t.start()
        else:
            _show_reply()

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_result,
    )


def send_text(contact, player_message, callback=None, output=None):
    """
    Send a text TO a specific sim. The player writes the message,
    and the sim responds in character.
    """
    main_si = sim_context.get_main_sim_info()
    main_name = main_si.first_name if main_si else "your Sim"
    other_name = contact["name"]

    # Auto-detect distance signals in the player's outgoing message.
    # Scoped to (main_si, contact_sim) so Alice's prefs don't affect
    # Bob's phone activity.
    _contact_id_auto = getattr(contact.get("sim_info"), "sim_id", None) if contact else None
    _main_id_auto = getattr(main_si, "sim_id", None) if main_si else None
    _maybe_auto_prefs_from_message(
        _main_id_auto, _contact_id_auto, other_name, player_message,
        source_label="you texted",
    )

    # Seed the conversation with the player's outgoing message as turn 1
    _start_conversation(contact, "", recipient_sim=main_si)
    ckey = _conv_key(main_si, contact)
    _conversations[ckey]["history"] = [{"role": "you", "text": player_message}]

    _refresh_milestones_for(contact, main_si)

    language = config.get_language()
    system = _REPLY_SYSTEM.format(
        language=language,
        other_name=other_name,
        main_name=main_name,
    )
    rel_desc = _describe_relationship(contact)
    contact_id = getattr(contact.get("sim_info"), "sim_id", None)
    main_sim_id = getattr(main_si, "sim_id", None)
    sim_history = journal.format_sim_history_for_prompt(
        other_name,
        recipient_name=main_name,
        trailing_note=_journal_obsolescence_note(contact),
        sim_id=contact_id,
        recipient_id=main_sim_id,
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""
    mutuals = _get_mutual_contacts(contact)
    mutual_block = _format_mutual_block(mutuals, casual=False)

    # Describe the PLAYER (main_si) -- the person the contact is texting
    # back to. Without this, the AI generates the contact's reply knowing
    # nothing about who they're talking to (no career callback, no mood
    # awareness, no aspiration context, no skills).
    recipient_block = _describe_recipient(main_si, contact=contact)

    events_text = events.format_shared_events_for_prompt(main_si, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""
    past_events_text = past_events.format_for_prompt(contact_id, main_sim_id)
    past_events_block = f"\n\n{past_events_text}" if past_events_text else ""

    last_conv_iso = journal.last_entry_timestamp_for_pair(contact_id, main_sim_id)
    interaction_tag = interactions.format_for_prompt(contact_id, main_sim_id, last_conv_iso=last_conv_iso)
    context_tags = (
        f"{_location_context(main_si, contact)}"
        f"{_season_context()}"
        f"{_weather_context(main_si, contact)}"
        f"{interaction_tag}"
    )

    prompt = (
        f"Relationship info:\n{rel_desc}{history_block}{mutual_block}\n\n"
        f"{recipient_block}{events_block}{past_events_block}\n\n"
        f"{main_name} just texted {other_name}: \"{player_message}\""
        f"{context_tags}"
        f"{_contact_prefs_block(main_si, contact.get('sim_info'), other_name)}\n\n"
        f"Write {other_name}'s reply (1-3 short text messages). "
        f"If {main_name} mentions people or events you don't have details about, "
        f"improvise naturally as {other_name} would — react in character, never refuse."
    )

    def _on_send_text_result(text, error):
        if error:
            notifications.show_error(error, output=output)
            if callback:
                callback(text, error)
            return
        if not text:
            if callback:
                callback(text, error)
            return

        text_clean = _apply_mood_from_text(text, recipient=main_si, is_incoming=False)
        delay = _calculate_reply_delay(contact)

        def _show_reply():
            if ckey in _conversations:
                _conversations[ckey]["history"].append({"role": "them", "text": text_clean})
            journal.add_entry(
                "text",
                f"Text conversation with {other_name}:\n"
                f"{main_name}: {player_message}\n"
                f"{other_name}: {text_clean}",
                sim_name=other_name,
                recipient_name=main_name,
                sim_id=contact_id,
                recipient_id=main_sim_id,
            )
            _maybe_auto_prefs_from_message(
                main_sim_id, contact_id, other_name, text_clean,
                source_label="they replied",
            )
            title = f"Reply from {other_name}"
            sender_si = contact.get("sim_info")
            shown = False
            if sender_si:
                shown = _show_phone_dialog(sender_si, title, text_clean, ring=False, recipient_sim_info=main_si)
            if not shown:
                notifications.show(title, text_clean, output=output)
            if callback:
                callback(text_clean, None)

        if delay > 0:
            t = threading.Timer(delay, _show_reply)
            t.daemon = True
            _track_timer(t)
            t.start()
        else:
            _show_reply()

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_send_text_result,
    )


def send_call(contact, player_topic, callback=None, output=None):
    """
    Call a specific sim about a topic. The player describes what they want
    to talk about, and the sim responds in character.
    """
    main_si = sim_context.get_main_sim_info()
    main_name = main_si.first_name if main_si else "your Sim"
    other_name = contact["name"]

    # Auto-detect: if the player's topic contains a distance signal,
    # apply the state before spending an API call. Scoped per-pair.
    _contact_id_auto = getattr(contact.get("sim_info"), "sim_id", None) if contact else None
    _main_id_auto = getattr(main_si, "sim_id", None) if main_si else None
    _maybe_auto_prefs_from_message(
        _main_id_auto, _contact_id_auto, other_name, player_topic,
        source_label="you called",
    )

    _start_conversation(contact, "", recipient_sim=main_si, kind="call")
    ckey = _conv_key(main_si, contact)
    _conversations[ckey]["history"] = [{"role": "you", "text": player_topic}]

    _refresh_milestones_for(contact, main_si)

    language = config.get_language()
    # Outgoing call from the player: the AI sim is answering, not
    # initiating. _REPLY_SYSTEM frames them as REPLYING to the player's
    # call -- using _CALL_SYSTEM here would (wrongly) tell them they're
    # the one placing the call.
    system = _REPLY_SYSTEM.format(
        language=language,
        other_name=other_name,
        main_name=main_name,
    )
    rel_desc = _describe_relationship(contact)
    contact_id = getattr(contact.get("sim_info"), "sim_id", None)
    main_sim_id = getattr(main_si, "sim_id", None)
    sim_history = journal.format_sim_history_for_prompt(
        other_name,
        recipient_name=main_name,
        trailing_note=_journal_obsolescence_note(contact),
        sim_id=contact_id,
        recipient_id=main_sim_id,
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""
    mutuals = _get_mutual_contacts(contact)
    mutual_block = _format_mutual_block(mutuals, casual=False)

    # Describe the PLAYER (main_si) -- the person the contact is replying
    # to on the call. Without this, the AI generates the contact's
    # response knowing nothing about the player's career, mood,
    # aspiration, or skills.
    recipient_block = _describe_recipient(main_si, contact=contact)

    events_text = events.format_shared_events_for_prompt(main_si, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""
    past_events_text = past_events.format_for_prompt(contact_id, main_sim_id)
    past_events_block = f"\n\n{past_events_text}" if past_events_text else ""

    last_conv_iso = journal.last_entry_timestamp_for_pair(contact_id, main_sim_id)
    interaction_tag = interactions.format_for_prompt(contact_id, main_sim_id, last_conv_iso=last_conv_iso)
    context_tags = (
        f"{_location_context(main_si, contact)}"
        f"{_season_context()}"
        f"{_weather_context(main_si, contact)}"
        f"{interaction_tag}"
    )

    prompt = (
        f"Person being called:\n{rel_desc}{history_block}{mutual_block}\n\n"
        f"{recipient_block}{events_block}{past_events_block}\n\n"
        f"{main_name} is calling {other_name}. {main_name} says: \"{player_topic}\""
        f"{context_tags}"
        f"{_contact_prefs_block(main_si, contact.get('sim_info'), other_name)}\n\n"
        f"Write what {other_name} says in response (3-5 lines of dialogue). "
        f"They should react naturally to what {main_name} said."
    )

    def _on_send_call_result(text, error):
        if text:
            text = _apply_mood_from_text(text, recipient=main_si, is_incoming=False)
            if ckey in _conversations:
                _conversations[ckey]["history"].append({"role": "them", "text": text})
            journal.add_entry(
                "call",
                f"Call with {other_name}:\n"
                f"{main_name}: {player_topic}\n"
                f"{other_name}: {text}",
                sim_name=other_name,
                recipient_name=main_name,
                sim_id=contact_id,
                recipient_id=main_sim_id,
            )
            _maybe_auto_prefs_from_message(
                main_sim_id, contact_id, other_name, text,
                source_label="they said on call",
            )
            title = f"Call with {other_name}"
            caller_si = contact.get("sim_info")
            shown = False
            if caller_si:
                shown = _show_phone_dialog(caller_si, title, text, recipient_sim_info=main_si)
            if not shown:
                notifications.show(title, text, output=output)
        elif error:
            notifications.show_error(error, output=output)
        if callback:
            callback(text, error)

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_send_call_result,
    )


# ===========================================================================
# GROUP TEXTS
# ===========================================================================
# Two-phase design:
#   Phase 1 (once per group, at creation): a "briefing" call using the
#     DEFAULT model synthesizes each participant's voice + the web of
#     relationships between them. Cached on the group forever.
#   Phase 2 (once per participant per round): a "reply" call using the
#     FAST model generates that participant's next messages. Prompt is
#     trimmed vs 1:1 texts -- no journal history, no per-pair past
#     events -- because private context leaking into a group text is
#     socially wrong. Shared context (weather / season / group-attended
#     past events) is added at the GROUP tier instead of per-pair.
#
# Serial-not-parallel: each participant N's prompt includes what
# participants 1..N-1 already said this round, so replies stay distinct.
#
# Gentle drop-off: after round 1, each participant has a small chance
# of "not replying this round" -- realistic AND cost-saving.
#
# Persisted state (participants, briefing, history): group_texts.py
# In-memory-only state (round in progress, queue): _group_runtime_state


# ---- System prompts --------------------------------------------------------

_GROUP_BRIEFING_SYSTEM = """You are helping a Sims 4 mod build context for a \
group-text roleplay. The player has just started a group chat between one of \
their household sims (the "anchor") and 2-4 other sims (the "participants"). \
Write a compact briefing (under 200 words) that will be reused as context for \
every reply in this thread. Write in {language}.

Structure the briefing as terse bullet points:
- One 1-2 sentence bullet per participant: their voice/register (age, personality, \
family role if any), their relationship to the anchor, and the ONE thing that \
would most distinguish their texts from the others'.
- One "Between them:" section noting any interesting cross-relationships \
between participants (best friends, exes, rivals, coworkers, family).

Rules:
- No commentary, no preamble, no closing. Just the briefing.
- Never invent facts not present in the input.
- Do not describe the anchor themselves -- they are the "you" the participants \
are texting.
- If a participant is a family member of the anchor, LEAD with that family role."""


_GROUP_REPLY_SYSTEM = """You are {name} in a group text with {other_names} and \
{anchor_name}. Write in {language}.

STRICT RULES:
- Reply ONLY as {name}. Never write dialogue for {other_names} or {anchor_name}.
- Never prefix your reply with a name -- no "{name}:" at the start.
- Never quote or paraphrase what others just said as if you're voicing them.
- Write 1-2 SHORT text messages, max 30 words each.
- Do not repeat what others just said. Say something distinct.
- If it fits, you may briefly react to what someone else in the group just said, \
but from {name}'s perspective only.

Voice: {name}'s traits, mood, career, and family role to {anchor_name} define \
how {name} texts. FAMILY ROLE OVERRIDES traits -- a parent texting their kid \
never uses peer slang.

Age register (match {name}'s age):
- Teen: lowercase, abbreviations, dramatic slang
- Young Adult: casual but articulate
- Adult: complete sentences, proper capitalization, no youth slang
- Elder: fuller sentences, proper punctuation, warmth

Never invent facts about {anchor_name}'s life or the other participants' lives \
beyond what the briefing lists."""


# ---- Prompt builders -------------------------------------------------------

def _build_group_briefing_prompt(anchor_si, participant_contacts):
    """Compose the user prompt for briefing generation.

    anchor_si: the household sim who's initiating the group (referenced
      by name; briefing does NOT describe them, they are 'you').
    participant_contacts: list of contact dicts (same shape as elsewhere).
    """
    anchor_name = anchor_si.first_name if anchor_si else "the player"
    parts = [f"=== Anchor sim (they are texting the group -- do not describe them, they are 'you'): {anchor_name} ==="]

    # Compact per-participant descriptions -- rely on the same helpers 1:1
    # texts use, but rendered as a group-context bundle rather than a lone
    # sender description.
    parts.append("\n=== Participants ===")
    for contact in participant_contacts:
        try:
            desc = _describe_relationship(contact, recipient=anchor_si)
            parts.append(desc)
        except Exception as _e:
            _log_error(f"_build_group_briefing_prompt: _describe_relationship raised: {type(_e).__name__}: {_e}")
            # Fall back to name + status so the briefing still gets *something*
            parts.append(f"=== Character: {contact.get('name', '?')} ===\nStatus: {contact.get('status', 'unknown')}")

    # Cross-relationships between participants. This is the highest-signal
    # information for group dynamics -- who knows whom, who has beef with
    # whom -- and it's ONLY visible in group context.
    cross_lines = _build_participant_cross_relations(participant_contacts)
    if cross_lines:
        parts.append("\n=== Cross-relationships between participants ===")
        parts.extend(cross_lines)

    # Group-level shared context: season/weather. Location context is
    # anchor-specific and gets omitted (participants may be scattered
    # across worlds). Past events shared by the WHOLE group would be
    # ideal but past_events is pair-indexed; skip for v1.
    parts.append(f"\n=== World context ==={_season_context()}{_weather_context(anchor_si, {})}")

    parts.append(f"\nWrite the briefing for this group of {len(participant_contacts)} participants.")
    return "\n".join(p for p in parts if p)


def _build_participant_cross_relations(participant_contacts):
    """For every unordered pair of participants, describe their
    relationship if known. Uses Sims 4's Relationship service to look
    up friendship + romance scores between two sim_ids. Returns a list
    of one-line strings, or [] if nothing interesting."""
    lines = []
    n = len(participant_contacts)
    if n < 2:
        return lines
    for i in range(n):
        for j in range(i + 1, n):
            a = participant_contacts[i]
            b = participant_contacts[j]
            a_si = a.get("sim_info")
            b_si = b.get("sim_info")
            if not a_si or not b_si:
                continue
            desc = _pair_relationship_summary(a_si, b_si)
            if desc:
                lines.append(f"- {a.get('name','?')} & {b.get('name','?')}: {desc}")
    return lines


def _pair_relationship_summary(a_si, b_si):
    """Return a one-line label for how sim A relates to sim B, or None
    if unremarkable. v1 keeps this simple: family (via genealogy) is
    the only signal surfaced -- friendship/romance scores across an
    arbitrary sim pair are cheap to fetch but noisy without threshold
    tuning, so we skip them for now and rely on the briefing model to
    pick up on group dynamics from the raw participant descriptions."""
    try:
        # Use the game's family relationship helper with b as the frame
        # of reference. Returns None for non-family pairs.
        return _get_family_relationship(a_si, {"sim_info": a_si}, recipient=b_si)
    except Exception:
        return None


def _build_group_reply_prompt(group, participant_index, this_round_replies):
    """Compose the user prompt for a single participant's reply this round.

    group: the persisted group dict from group_texts.get_group.
    participant_index: index into group["participant_sim_ids"] of the
      participant whose reply we're generating.
    this_round_replies: list of {"from_name": ..., "text": ...} for
      replies already generated in this round (participant_index goes
      in order 0..N-1). Used to steer the model away from parroting.
    """
    briefing = group.get("briefing") or ""
    history = group.get("history") or []
    p_ids = group.get("participant_sim_ids") or []
    p_names = group.get("participant_names") or []

    if participant_index >= len(p_ids):
        return None

    my_id = p_ids[participant_index]
    my_name = p_names[participant_index] if participant_index < len(p_names) else "?"

    # Resolve the participant's sim_info fresh -- name in group is a
    # snapshot; traits/mood/career should come from live sim_info.
    my_si = _resolve_sim_info(my_id)

    parts = []
    parts.append(f"=== Group briefing ===\n{briefing}" if briefing else "")

    # Just your own live personality snapshot -- keeps voice tight without
    # ballooning the prompt. Skip the full _describe_relationship treatment
    # because the briefing already covers relationship-to-anchor.
    if my_si:
        traits = sim_context.get_sim_traits(my_si, limit=6)
        mood = sim_context.get_sim_mood(my_si)
        career = sim_context.get_sim_career(my_si)
        aspiration = sim_context.get_sim_aspiration(my_si)
        own = [f"=== You are: {my_name} ==="]
        if traits:
            own.append(f"traits: {', '.join(traits)}")
        if mood:
            own.append(f"current mood: {mood}")
        if career:
            own.append(f"career: {career}")
        if aspiration:
            own.append(f"aspiration: {aspiration}")
        parts.append("\n".join(own))

    # Thread history: render last N turns as "Name: text" lines. Cap
    # so a long thread doesn't run away with prompt tokens.
    THREAD_TAIL = 12
    tail = history[-THREAD_TAIL:] if len(history) > THREAD_TAIL else history
    anchor_author = group.get("_anchor_name") or "Anchor"
    thread_lines = ["=== Thread so far ==="]
    for turn in tail:
        if turn.get("role") == "you":
            thread_lines.append(f"{anchor_author}: {turn.get('text','')}")
        else:
            thread_lines.append(f"{turn.get('from_name','?')}: {turn.get('text','')}")
    parts.append("\n".join(thread_lines))

    # Find the MOST RECENT player-turn and quote it explicitly. Without
    # this, when the thread has multiple player-turns (e.g. an initial
    # invite, then later a thank-you), the model can respond to the
    # earlier topic instead of the fresh one -- leading to Vivian/Luca
    # replying about "tomorrow at 5" when the player has moved on to
    # thanking them for last night's party. Pin the target explicitly.
    latest_player_turn = None
    for turn in reversed(history):
        if turn.get("role") == "you":
            latest_player_turn = turn.get("text", "")
            break
    if latest_player_turn:
        parts.append(
            f"=== Reply TO THIS specific message ===\n"
            f"{anchor_author}'s most recent message (this is what "
            f"{my_name} should respond to -- NOT any earlier topic in "
            f"the thread, even if it looks unresolved):\n"
            f"  \"{latest_player_turn}\""
        )

    # What others JUST said this round -- explicit call-out to steer
    # the reply toward distinctness.
    if this_round_replies:
        just_said = ["=== Others just replied this round ==="]
        for r in this_round_replies:
            just_said.append(f"- {r.get('from_name','?')}: {r.get('text','')}")
        just_said.append("Say something distinct -- don't parrot them.")
        parts.append("\n".join(just_said))

    parts.append(
        f"Now write {my_name}'s next 1-2 short messages, responding "
        f"specifically to {anchor_author}'s most recent message above."
    )
    return "\n\n".join(p for p in parts if p)


def _resolve_sim_info(sim_id):
    """Lookup a sim_info by sim_id. Returns None if not resolvable
    (moved out, deleted, or engine not ready)."""
    if not sim_id:
        return None
    try:
        import services
        mgr = services.sim_info_manager()
        if mgr is None:
            return None
        return mgr.get(int(sim_id))
    except Exception:
        return None


# ---- Post-process guards ---------------------------------------------------

import re as _re_group

# Detects "Alice:" style speaker prefixes anywhere in a reply. The
# model sometimes slips into narrating a whole exchange when it sees
# a "Name: text" formatted thread history. If detected, we trim the
# offending speaker's line and everything after -- keeping only the
# intended participant's own message(s).
def _strip_speaker_prefixes(text, other_names_lower):
    """Trim the reply at the first occurrence of another participant's
    name used as a speaker prefix. Returns (cleaned_text, was_stripped).
    other_names_lower: pre-lowercased list of the other participants'
    first names (including the anchor)."""
    if not text or not other_names_lower:
        return text, False
    # Build one regex: any of the other names, colon at start of line
    # (after optional space). Case-insensitive; captures line start.
    pattern = r"(?im)^\s*(?:" + "|".join(_re_group.escape(n) for n in other_names_lower) + r")\s*:"
    m = _re_group.search(pattern, text)
    if not m:
        return text, False
    # Trim before the offending prefix. Strip trailing whitespace/newlines.
    trimmed = text[:m.start()].rstrip()
    return trimmed, True


def _cap_reply_length(text, max_words=60):
    """Truncate at nearest sentence boundary before max_words. Never
    cuts mid-word; if no sentence boundary fits, hard-cuts at max_words."""
    if not text:
        return text
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words])
    # Prefer a sentence boundary just before the cutoff
    for punct in (". ", "! ", "? ", "\n"):
        idx = truncated.rfind(punct)
        if idx > 0 and idx > len(truncated) * 0.5:
            return truncated[:idx + 1].rstrip()
    return truncated + "…"


def _clean_group_reply(text, other_names):
    """Full post-process pipeline for a participant's reply. Returns
    (cleaned_text, notes) where notes is a list of what was fixed
    (for logging)."""
    notes = []
    if not text:
        return text, notes
    other_names_lower = [n.lower() for n in (other_names or []) if n]
    cleaned, stripped = _strip_speaker_prefixes(text, other_names_lower)
    if stripped:
        notes.append("stripped speaker-name leak")
    if not cleaned.strip():
        # Whole reply was "Alice: hey / Bob: hi" -- nothing left after strip.
        # Fall back to a benign short line so the thread doesn't stall.
        return "(no reply)", notes + ["reply was entirely voice-leak; substituted placeholder"]
    capped = _cap_reply_length(cleaned)
    if capped != cleaned:
        notes.append("truncated to length cap")
    return capped, notes


# ---- Drop-off --------------------------------------------------------------

def _dropoff_probability(round_num):
    """Chance a participant silently doesn't reply this round. Round 1
    always replies. Grows: round 2 = 20%, round 3 = 40%, round 4+ = 60%."""
    if round_num <= 1:
        return 0.0
    if round_num == 2:
        return 0.20
    if round_num == 3:
        return 0.40
    return 0.60


def _dropoff_enabled():
    """Config toggle. Default on."""
    try:
        return bool(config.get_setting("group_text_dropoff_enabled", True))
    except Exception:
        return True


# ---- Orchestration ---------------------------------------------------------

def send_group_text(participant_contacts, player_message, callback=None, output=None):
    """Player initiates a group text with N recipients (2..cap).

    participant_contacts: list of contact dicts (already resolved by the
      picker).
    player_message: the opening text.

    Flow: create persisted group -> record player's opening turn ->
    generate briefing (main model) -> kick off round 1 of participant
    replies (fast model, serial with staggered delays)."""
    if not participant_contacts or len(participant_contacts) < 2:
        msg = "Group texts need at least 2 recipients."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return
    if not player_message:
        msg = "Group text needs a message."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    anchor_si = sim_context.get_main_sim_info()
    if not anchor_si:
        msg = "No active household sim to send from."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    anchor_id = getattr(anchor_si, "sim_id", None)
    if not anchor_id:
        msg = "Couldn't resolve the anchor sim's id."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    participant_ids = []
    participant_names = []
    for c in participant_contacts:
        si = c.get("sim_info")
        sid = getattr(si, "sim_id", None) if si else None
        if not sid:
            continue
        participant_ids.append(int(sid))
        participant_names.append(c.get("name") or (si.first_name if si else "?"))

    if len(participant_ids) < 2:
        msg = "Couldn't resolve enough participants to form a group."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    group_id = group_texts.create_group(anchor_id, participant_ids, participant_names)
    if not group_id:
        msg = "Couldn't create the group thread (no save loaded?)."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    # Record the anchor name on the group so the reply-prompt builder
    # can render turn lines as "Anchor: ..." without another lookup.
    # This is a minor extension to the group dict, harmless if absent.
    try:
        g = group_texts.get_group(group_id)
        if g is not None:
            g["_anchor_name"] = anchor_si.first_name
    except Exception:
        pass

    # Register runtime state up front so a Timer that fires later can
    # find its group.
    with _group_runtime_lock:
        _group_runtime_state[group_id] = {
            "round_num": 1,
            "in_progress": True,
            "queued_player_msg": None,
        }

    group_texts.add_player_turn(group_id, player_message, round_num=1)

    # If create_group RESUMED an existing thread, the cached briefing
    # is still valid -- no need to spend another main-model API call
    # regenerating it. Just skip straight to round 1.
    existing_briefing = ""
    try:
        _existing_group = group_texts.get_group(group_id)
        if _existing_group:
            existing_briefing = (_existing_group.get("briefing") or "").strip()
    except Exception:
        existing_briefing = ""

    if existing_briefing:
        _log_error(
            f"send_group_text: RESUMED group={group_id} participants={participant_names} "
            f"anchor={anchor_si.first_name} (skipping briefing regeneration)"
        )
        _run_group_round(
            group_id,
            anchor_si=anchor_si,
            participant_contacts=participant_contacts,
            callback=callback,
            output=output,
        )
        return

    _log_error(f"send_group_text: NEW group={group_id} participants={participant_names} anchor={anchor_si.first_name}")

    language = config.get_language()
    briefing_system = _GROUP_BRIEFING_SYSTEM.format(language=language)
    briefing_prompt = _build_group_briefing_prompt(anchor_si, participant_contacts)

    def _on_briefing(text, error):
        if error or not text:
            _log_error(f"group briefing failed: {error!r}; falling back to bare cross-relations")
            # Fallback briefing: no AI, just the raw participant list. Keeps
            # the thread alive even if the briefing call blows up.
            fallback = "Participants: " + ", ".join(participant_names)
            group_texts.set_briefing(group_id, fallback)
        else:
            group_texts.set_briefing(group_id, text)
        # Kick off round 1 immediately -- no reply-delay before the FIRST
        # participant, so the player sees activity as soon as the briefing
        # returns.
        _run_group_round(
            group_id,
            anchor_si=anchor_si,
            participant_contacts=participant_contacts,
            callback=callback,
            output=output,
        )

    api_client.call_ai_async(
        [{"role": "user", "content": briefing_prompt}],
        system=briefing_system,
        # Briefing uses the DEFAULT (main) model per design decision --
        # one call amortized across many replies; worth spending on.
        use_fast_model=False,
        callback=_on_briefing,
    )


def _run_group_round(group_id, anchor_si=None, participant_contacts=None,
                     callback=None, output=None):
    """Serially generate replies from each participant this round.
    Each participant N's prompt includes what participants 1..N-1
    said this round, so replies stay distinct.

    anchor_si + participant_contacts are optional -- passed on the
    first round to avoid re-resolving. On subsequent rounds we
    re-resolve from group state."""
    group = group_texts.get_group(group_id)
    if not group:
        _log_error(f"_run_group_round: no group_id={group_id}")
        return

    with _group_runtime_lock:
        state = _group_runtime_state.get(group_id)
        if not state:
            state = {"round_num": 1, "in_progress": True, "queued_player_msg": None}
            _group_runtime_state[group_id] = state
        state["in_progress"] = True
        round_num = state["round_num"]

    # Resolve current participant sim_infos and names. Any that don't
    # resolve (moved out / deleted / dead) are skipped this round.
    p_ids = group.get("participant_sim_ids") or []
    p_names_snap = group.get("participant_names") or []

    active = []
    for idx, sid in enumerate(p_ids):
        si = _resolve_sim_info(sid)
        if not si:
            _log_error(f"_run_group_round: participant sim_id={sid} not resolvable; skipping this round")
            continue
        name = p_names_snap[idx] if idx < len(p_names_snap) else si.first_name
        # Roll drop-off dice (only after round 1)
        if _dropoff_enabled() and round_num > 1:
            p = _dropoff_probability(round_num)
            if random.random() < p:
                _log_error(f"_run_group_round: participant {name} silently drops out this round (p={p:.2f})")
                continue
        active.append((idx, sid, name, si))

    if not active:
        _log_error(f"_run_group_round: no active participants for round {round_num}; ending round")
        with _group_runtime_lock:
            st = _group_runtime_state.get(group_id)
            if st:
                st["in_progress"] = False
        if callback:
            callback(None, None)
        return

    # Anchor sim + name for reply prompts + dialog anchoring
    if anchor_si is None:
        anchor_si = _resolve_sim_info(group.get("anchor_sim_id"))
    anchor_name = anchor_si.first_name if anchor_si else "Anchor"

    # Kick off the serial chain
    _generate_next_group_reply(
        group_id=group_id,
        active=active,
        cursor=0,
        this_round_replies=[],
        anchor_si=anchor_si,
        anchor_name=anchor_name,
        round_num=round_num,
        callback=callback,
        output=output,
    )


def _generate_next_group_reply(group_id, active, cursor, this_round_replies,
                                anchor_si, anchor_name, round_num, callback, output):
    """Generate reply for active[cursor] (the next participant this round).
    On completion, schedules the following participant via Timer for a
    naturalistic staggered feel."""
    if cursor >= len(active):
        # Round complete
        _on_group_round_complete(group_id, anchor_si, callback, output)
        return

    _idx_in_group, sim_id, my_name, my_si = active[cursor]

    # Fresh snapshot of the group after prior replies
    group = group_texts.get_group(group_id)
    if not group:
        _log_error(f"_generate_next_group_reply: group vanished mid-round")
        return

    # Other names (for the "you are NOT them" system prompt)
    other_names = [n for (_i, _sid, n, _si) in active if n != my_name]
    # Include anchor in the not-you list so speaker-name leak strip
    # catches "Bob:" AND "Anchor:" prefixes.
    other_names_including_anchor = list(other_names) + [anchor_name]

    language = config.get_language()
    system = _GROUP_REPLY_SYSTEM.format(
        language=language,
        name=my_name,
        other_names=", ".join(other_names) if other_names else "(nobody else)",
        anchor_name=anchor_name,
    )
    user_prompt = _build_group_reply_prompt(
        group=group,
        participant_index=[i for (i, _sid, _n, _s) in [active[cursor]]][0],
        this_round_replies=this_round_replies,
    )
    if not user_prompt:
        _log_error(f"_generate_next_group_reply: prompt build returned None for cursor={cursor}")
        _generate_next_group_reply(
            group_id, active, cursor + 1, this_round_replies,
            anchor_si, anchor_name, round_num, callback, output,
        )
        return

    def _on_reply(text, error):
        if error or not text:
            _log_error(f"group reply from {my_name} failed: error={error!r}")
            # Skip this participant this round; continue chain.
            _schedule_next(group_id, active, cursor + 1, this_round_replies,
                           anchor_si, anchor_name, round_num, callback, output)
            return

        cleaned, notes = _clean_group_reply(text, other_names_including_anchor)
        if notes:
            _log_error(f"group reply from {my_name} cleaned: {notes}")

        # Apply mood side-effects (matches 1:1 texts)
        try:
            cleaned = _apply_mood_from_text(cleaned, recipient=anchor_si, is_incoming=True)
        except Exception as _me:
            _log_error(f"apply_mood on group reply failed: {type(_me).__name__}: {_me}")

        # Persist the reply
        group_texts.add_participant_reply(group_id, sim_id, my_name, cleaned, round_num=round_num)

        # Record in this-round buffer so subsequent participants see it
        this_round_replies.append({"from_name": my_name, "text": cleaned})

        # Show the phone dialog for this reply. Anchor to the sender's
        # portrait; ring=False (texts, not calls). Pass group_id so the
        # Reply button routes to generate_group_reply for THIS thread.
        #
        # Title format: "Group text with Sarah, Bob, Alice, and Kate" so
        # the player sees the whole roster on every message. The sender's
        # portrait (from _show_phone_dialog anchoring) plus the message
        # body tell them WHO just spoke; the title tells them the GROUP.
        roster_names = [n for (_i, _sid, n, _s) in active]
        # Include the anchor (household member) in the roster string
        # so the player understands "the group" includes them.
        full_roster = [anchor_name] + roster_names if anchor_name not in roster_names else list(roster_names)
        roster_str = _format_group_names(full_roster)
        title = f"Group text with {roster_str}" if roster_str else f"Group text: {my_name}"
        # Prefix the message body with the sender's name so it's crystal
        # clear who's speaking on this specific line -- the title now
        # shows the group not the sender, so we surface the sender here.
        display_text = f"{my_name}: {cleaned}"
        shown = False
        try:
            shown = _show_phone_dialog(
                my_si, title, display_text, ring=False,
                recipient_sim_info=anchor_si, group_id=group_id,
            )
        except Exception as _de:
            _log_error(f"group reply dialog failed for {my_name}: {type(_de).__name__}: {_de}")
        if not shown:
            notifications.show(title, display_text, output=output)

        # Update the reply-button routing pointer so the next player
        # reply routes to THIS group, not any lingering 1:1 conversation.
        global _last_active_group_id
        _last_active_group_id = group_id

        # Group text turns are NOT written to the journal. They live in
        # GroupTexts.json and get rendered into 1:1 prompts via
        # group_texts.format_shared_for_prompt when applicable. Writing
        # them to the journal too produced duplicated 'Past interactions'
        # entries that overlapped with the SHARED GROUP TEXT block --
        # same content, two sources.

        # Schedule the next participant with the standard reply delay
        _schedule_next(group_id, active, cursor + 1, this_round_replies,
                       anchor_si, anchor_name, round_num, callback, output)

    api_client.call_ai_async(
        [{"role": "user", "content": user_prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_reply,
    )


def _schedule_next(group_id, active, cursor, this_round_replies,
                    anchor_si, anchor_name, round_num, callback, output):
    """After one participant replies, wait a randomized delay before
    the NEXT participant's dialog surfaces. Delay uses the same
    _calculate_reply_delay helper as 1:1 texts so the feel is consistent.
    """
    if cursor >= len(active):
        _on_group_round_complete(group_id, anchor_si, callback, output)
        return

    # Use the standard reply delay so groups feel like real texts
    # arriving one at a time. Use the NEXT participant's contact for
    # the delay calc (traits/mood shape the delay).
    next_name = active[cursor][2]
    next_si = active[cursor][3]
    try:
        # _calculate_reply_delay takes a "contact" dict shape
        delay = _calculate_reply_delay({"sim_info": next_si, "name": next_name})
    except Exception:
        delay = 0

    def _fire():
        try:
            _generate_next_group_reply(group_id, active, cursor, this_round_replies,
                                        anchor_si, anchor_name, round_num, callback, output)
        except Exception as _e:
            _log_error(f"_schedule_next fire raised: {type(_e).__name__}: {_e}")

    if delay > 0:
        t = threading.Timer(delay, _fire)
        t.daemon = True
        _track_timer(t)
        t.start()
    else:
        _fire()


def _on_group_round_complete(group_id, anchor_si, callback, output):
    """Round finished. If the player queued a message mid-round, drain
    the queue and start the next round. Otherwise mark idle."""
    with _group_runtime_lock:
        state = _group_runtime_state.get(group_id)
        if not state:
            return
        state["in_progress"] = False
        queued = state.get("queued_player_msg")
        state["queued_player_msg"] = None
        if queued:
            state["round_num"] = state.get("round_num", 1) + 1
            state["in_progress"] = True

    if queued:
        # Player sent a message while the previous round was still
        # generating -- record it and kick off the next round.
        group_texts.add_player_turn(group_id, queued, round_num=state["round_num"])
        _run_group_round(group_id, anchor_si=anchor_si, callback=callback, output=output)
        return

    if callback:
        callback(None, None)


def generate_group_reply(player_message, group_id=None, callback=None, output=None):
    """Called by the reply button when the active thread is a group.
    If group_id is None, use the most-recently-active group."""
    if group_id is None:
        group_id = _last_active_group_id
    if not group_id:
        msg = "No active group text to reply to."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    group = group_texts.get_group(group_id)
    if not group:
        msg = "That group thread no longer exists."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    if not player_message:
        msg = "Type a message to send to the group."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    # If a round is already running for this group, queue the player's
    # message. It'll drain when the current round finishes -- prevents
    # racing turns.
    with _group_runtime_lock:
        state = _group_runtime_state.setdefault(group_id, {
            "round_num": 1, "in_progress": False, "queued_player_msg": None,
        })
        if state.get("in_progress"):
            state["queued_player_msg"] = player_message
            return
        state["round_num"] = state.get("round_num", 1) + 1
        state["in_progress"] = True
        next_round = state["round_num"]

    group_texts.add_player_turn(group_id, player_message, round_num=next_round)
    anchor_si = _resolve_sim_info(group.get("anchor_sim_id"))
    _run_group_round(group_id, anchor_si=anchor_si, callback=callback, output=output)
