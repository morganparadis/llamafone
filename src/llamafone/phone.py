"""
Phone calls and texts -- generates AI-powered messages from relationship sims.
Uses the fast model for quick generation.

Calls show as modal phone dialogs with the caller's portrait (ring).
Texts show as phone dialogs with buzz.
Players can reply with llama.reply <message> to continue the conversation.
"""
import random
import threading

from . import api_client, sim_context, config, journal, notifications, moodlets, events

# Conversations keyed by recipient sim_id, so concurrent texts/calls to different
# household sims don't overwrite each other.
# Each value: {"contact": contact_dict, "recipient": sim_info,
#              "history": [{"role": "them"|"you", "text": str}, ...]}
_conversations = {}
# Most recent recipient that received a message (fallback if no specific signal)
_last_active_recipient_id = None
# Set when the player clicks the Reply button on a phone dialog — tells the next
# llama.reply which conversation to continue.
_pending_reply_recipient_id = None


_REPLY_TEXT_INPUT_NAME = "reply_text"


def _show_reply_input_dialog(caller_sim_info, anchor_sim):
    """
    Open a text-input dialog so the player can type a reply inline,
    instead of having to use the cheat console.
    On submit, calls generate_reply() with the typed text.
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

        loc_title = LocalizationHelperTuning.get_raw_text(
            f"Reply to {other_name}" if other_name else "Reply"
        )
        loc_text = LocalizationHelperTuning.get_raw_text(
            "Type what you want to say. Llamafone will craft a reply."
        )
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
                _mark_reply_intent(anchor_sim)
                generate_reply(reply_text)
            except Exception:
                pass

        dialog.add_listener(_on_input_response)
        icon = IconInfoData(obj_instance=caller_sim_info) if caller_sim_info else None
        if icon is not None:
            dialog.show_dialog(icon_override=icon)
        else:
            dialog.show_dialog()
        return True
    except Exception:
        return False


def _show_phone_dialog(caller_sim_info, title, message, ring=True, recipient_sim_info=None):
    """
    Show a phone dialog with the caller's portrait and Reply/Dismiss buttons.
    Anchored to recipient_sim_info if provided, else the protagonist, else active sim.
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
            anchor_sim = sim_context.get_main_sim_info()
        if not anchor_sim:
            anchor_sim = client.active_sim_info
        if not anchor_sim:
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
                # Lock in which conversation the next reply belongs to BEFORE
                # opening the text-input dialog. Without this, a text/call
                # arriving between Reply click and submit could steal context.
                _mark_reply_intent(anchor_sim)
                if not _show_reply_input_dialog(caller_sim_info, anchor_sim):
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
- Teen: dramatic, slang
- Young Adult: casual but articulate
- Adult: measured sentences, no youth slang ("yo", "bro", "dude")
- Elder: nostalgic, formal, long-winded

Traits add flavor on top (Hot-Headed rants, Goofball jokes, Snob condescends, Loner is terse). \
Traits do NOT override the family-role register above.

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
  mentions of mutual contacts: only "ran into X" if X lives in the CALLER's world.

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
  shows actual conflict. Never invent past conflict. BANNED phrases (and any variants): \
  "things got weird between us", "things have been weird", "things are weird between us", \
  "after what happened", "we left things off badly", "I know it's been weird", \
  "been meaning to catch up", "we should catch up properly", "we need to catch up", \
  "do a video call sometime", "let's catch up sometime". \
  If you find yourself reaching for any of these, you're writing filler — start over with \
  a concrete topic instead.
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
  like which mutual sim is deceased.

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
- Adult: complete sentences, no youth slang. "Hi! Are you free this weekend?"
- Elder: formal, warm, sometimes long-winded. "Hello dear, I hope you're well."

Traits add flavor on top (Hot-Headed = caps, Gloomy = ellipses, Snob = condescending grammar, \
Goofball = playful, Romantic = soft language, Loner = terse, Evil = passive aggressive). Traits \
do NOT override the family-role register above.

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
  mentions of mutuals: only "ran into X" if X lives in the SENDER's world.

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
  shows actual conflict. Never invent past conflict. BANNED phrases (and any variants): \
  "things got weird between us", "things have been weird", "things are weird between us", \
  "after what happened", "we left things off badly", "I know it's been weird", \
  "been meaning to catch up", "we should catch up properly", "we need to catch up", \
  "do a video call sometime", "let's catch up sometime". \
  If you find yourself reaching for any of these, start over with a concrete topic.
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
  like which mutual sim is deceased.

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

_REPLY_SYSTEM = """You write a Sim's reply to a text from the player's sim in The Sims 4. \
Stay in character as {other_name} replying to {main_name}. Write in {language}.

# Voice
{other_name}'s family role to {main_name}, age stage, traits, AND the
"How they feel about the player" line define how they reply.

Family role locks the voice (Father = dad voice, Sibling = teasing, Spouse = intimate). \
Adults use full sentences and proper punctuation, no youth slang. Teens use lowercase \
and emoji. Elders are formal and warm.

# Friendship tone — CRITICAL, READ CAREFULLY
The "How they feel about the player" line is the LAW. Match it strictly. \
If past chat history contradicts the current label (e.g. old warm messages but \
they're now "enemies"), assume a falling-out happened SINCE then — the CURRENT \
status overrides past tone. Don't keep being warm because old messages were warm.

By tier:
- "best friends, very close" / "close friends" / "friends, get along well": warm, easy, glad to hear from them
- "friendly acquaintances": polite, friendly, normal
- "barely know each other": HESITANT, CONFUSED. You barely remember {main_name} — \
  you maybe met once or twice. Receiving this text out of the blue is weird. \
  Lead with something like "wait who is this", "sorry — is this [their name]? \
  how'd you get my number?", "hi! we've met right? remind me where...", "do I \
  know you? sorry brain blank". Be a little stilted and ask for a refresher. \
  NEVER warm, NEVER familiar, NEVER pretend you remember details you don't. \
  This tier does NOT apply if a family role is listed — family always knows family.
- "have some negative history": cool, brief, slightly stilted; no warmth
- "actively dislike each other": cold, dismissive, short replies, may snipe; no warmth ANYWHERE
- "enemies": OPENLY HOSTILE. Cutting, snarky, dismissive, may insult or mock. \
  Treats the message with contempt. Refuses help. Zero warmth. NEVER apologizes, \
  NEVER mends, NEVER says "rooting for you" / "happy for you" / "always have been" / \
  "I would never" / "I miss you/us" / "hope you're well". An enemy responding to "things \
  are great" would be sarcastic ("congrats, like I care") or competitive ("good for you, \
  meanwhile I'm crushing it") — never supportive.

# What to write
1-2 SHORT messages, max 2 sentences each. React authentically to what {main_name} said — \
no generic responses. If they mention someone or something not in the context, react in \
character (curious, confused, gossipy) — never refuse or ask for details.

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
- Family relationships are NEVER romantic, regardless of romance score.
- No profanity or explicit content.
- Don't assume same last name = related or in same household.
- DECEASED sims (marked [DECEASED]) are GHOSTS. Never reference them as if alive. \
  Talk about them in past tense or as memories.
- Stay in character. Never acknowledge being an AI or claim missing information.
- PLAY ALONG with the player. If {main_name} references things you don't have data for \
  (companies, hobbies, side businesses, etc.), treat them as canon. \
  NEVER push back, correct, or say "I think you have me confused" — the player is driving \
  the story. Roll with it, ask curious in-character questions if needed.
- CALENDAR EVENTS are the EXCEPTION to "play along". The upcoming-events block is \
  ground truth from the in-game calendar. Only reference events listed there, and do \
  NOT invent details the block doesn't state — who the event is for, who's hosting, \
  what's planned. If the block names an honoree ("in memory of X", "for X and Y"), use \
  that exact framing; if it doesn't, stay vague ("the funeral later") and do NOT guess \
  whose it is from other context like which mutual sim is deceased.

# Output format (STRICT)
PLAIN TEXT ONLY. No markdown, no `**bold**`, no `---` separators, no "Message 1:" labels.

Format your response as:
<message 1 text>
<message 2 text, optional>

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

    if not chosen_pool:
        _log_picker(
            f"{recipient_name}: 0 strict contacts (initial {initial_count}). "
            f"Caller should try a different recipient."
        )
        return None

    weights = []
    for contact in chosen_pool:
        score = abs(contact.get("friendship") or 0) + abs(contact.get("romance") or 0)
        weights.append(max(score, 10))

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
        "\nWhen mentioning a mutual who is family of the message recipient "
        "(listed as 'Francesca's Father', 'Daniel's Sister', etc.), refer to "
        "them by the family role from the recipient's perspective -- "
        "\"your dad\", \"your mom\", \"your sister\", \"your brother\", \"your son\", "
        "\"your daughter\" -- NOT by their first name. This is how real people talk "
        "to family about other family. First-name references are fine for non-family "
        "mutuals."
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

        # Randomise so different mutuals surface across calls
        shared_list = list(shared_ids)
        random.shuffle(shared_list)

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

        for sid in shared_list[:4]:
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

                # Age and world context
                age = ""
                try:
                    age_str = str(getattr(si, "age", "")).replace("Age.", "")
                    if age_str:
                        age = f", {age_str}"
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
                    f"{name} (your {other_label}, {recipient_first}'s {main_label}{age}{world_part}){ghost_tag}"
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
    # EP02 Get Together
    "windenburg": "Windenburg",
    "ep02": "Windenburg",
    # EP03 City Living
    "sanmyshuno": "San Myshuno",
    "ep03": "San Myshuno",
    # EP04 Cats & Dogs
    "brindletonbay": "Brindleton Bay",
    "ep04": "Brindleton Bay",
    # EP05 Seasons (no new world)
    # EP06 Get Famous
    "delsolvalley": "Del Sol Valley",
    "ep06": "Del Sol Valley",
    # EP07 Island Living
    "sulani": "Sulani",
    "ep07": "Sulani",
    # EP08 Discover University
    "britechester": "Britechester",
    "ep08": "Britechester",
    # EP09 Eco Lifestyle
    "evergreenharbor": "Evergreen Harbor",
    "ep09": "Evergreen Harbor",
    # EP10 Snowy Escape
    "mtkomorebi": "Mt. Komorebi",
    "ep10": "Mt. Komorebi",
    # EP11 Cottage Living
    "henfordonbagley": "Henford-on-Bagley",
    "henford": "Henford-on-Bagley",
    "ep11": "Henford-on-Bagley",
    # EP12 High School Years
    "copperdale": "Copperdale",
    "ep12": "Copperdale",
    # EP13 Growing Together
    "sansequoia": "San Sequoia",
    "ep13": "San Sequoia",
    # EP14 Horse Ranch
    "chestnutridge": "Chestnut Ridge",
    "ep14": "Chestnut Ridge",
    # EP15 For Rent
    "tomarang": "Tomarang",
    "ep15": "Tomarang",
    # EP16 Life & Death
    "ravenwood": "Ravenwood",
    "ep16": "Ravenwood",
    # EP17 Lovestruck
    "ciudadenamorada": "Ciudad Enamorada",
    "ep17": "Ciudad Enamorada",
    # EP18 Businesses & Hobbies
    "nordhaven": "Nordhaven",
    "ep18": "Nordhaven",
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


def _get_sim_home_world(sim_info):
    """Get the world/neighborhood name where a sim lives."""
    try:
        household = sim_info.household
        if household:
            home_zone_id = household.home_zone_id
            if home_zone_id:
                try:
                    from world.region import get_region_instance_from_zone_id
                    region = get_region_instance_from_zone_id(home_zone_id)
                    if region:
                        name = getattr(region, "__name__", "") or str(region)
                        cleaned = (name
                            .replace("Region_", "")
                            .replace("region_", "")
                            .replace("_", " ")
                            .strip())
                        if cleaned:
                            return _friendly_world_name(cleaned)
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _season_context():
    """Return a minimal season context block.
    Just the season name — used as a consistency check, NOT as a topic suggestion.
    """
    season = sim_context.get_current_season()
    if not season:
        return ""
    return f"\n[SEASON: {season}]"


# Static climate descriptors per world. Used so the AI knows whether a caller
# is calling from somewhere tropical, snowy, gloomy, etc. -- Sims 4 only
# simulates weather in the active world, so for off-world callers this is
# the best signal we have.
_WORLD_CLIMATES = {
    "Willow Creek": "humid Southern (mild winters, hot humid summers)",
    "Oasis Springs": "desert (hot dry days, mild winters)",
    "Newcrest": "temperate (mild four seasons)",
    "Magnolia Promenade": "humid Southern (mild)",
    "Windenburg": "temperate European (cool, often overcast, snowy winters)",
    "San Myshuno": "humid continental (hot summers, cold winters)",
    "Brindleton Bay": "New England coastal (cold winters, warm summers, foggy)",
    "Del Sol Valley": "Mediterranean (warm, dry, sunny)",
    "Sulani": "tropical (warm year-round, frequent rain, occasional storms)",
    "Britechester": "temperate college town (mild seasons, frequent rain)",
    "Evergreen Harbor": "Pacific Northwest (cool, often rainy, overcast)",
    "Mt. Komorebi": "cold alpine (snowy winters, mild summers)",
    "Henford-on-Bagley": "mild English countryside (cool, wet, foggy mornings)",
    "Copperdale": "temperate small-town American (four full seasons)",
    "San Sequoia": "Northern Californian (mild, occasional fog)",
    "Chestnut Ridge": "rural plains (hot summers, cold winters)",
    "Tomarang": "tropical monsoon (hot, humid, heavy seasonal rain)",
    "Ravenwood": "gothic Pacific Northwest (cool, misty, often overcast)",
    "Ciudad Enamorada": "tropical Latin American (warm, lively, occasional rain)",
    "Nordhaven": "Scandinavian coastal (cool, overcast, long dark winters)",
    "Granite Falls": "mountain forest (cool, occasional rain)",
    "Forgotten Hollow": "perpetually gloomy (overcast, foggy, cool)",
    "Selvadorada": "tropical jungle (hot and humid year-round)",
    "StrangerVille": "desert with a strange persistent haze (hot, dry, eerie)",
    "Glimmerbrook": "rainy Pacific Northwest (cool, wet, mossy)",
    "Batuu": "alien desert (hot, dry, otherworldly)",
    "Tartosa": "Mediterranean coastal (warm, sunny, dry summers)",
    "Moonwood Mill": "deep forest (cool, misty, frequent rain)",
    "Innisgreen": "Irish countryside (cool, often rainy, lush green)",
}


def _get_world_climate(world_name):
    """Return a short climate descriptor for a world, or None if unknown."""
    if not world_name:
        return None
    return _WORLD_CLIMATES.get(world_name)


def _weather_context(main_si, contact):
    """Build a [WEATHER: ...] block describing current conditions where the
    player is and the climate of the caller's home world (if different).
    The caller-side is static climate data since Sims 4 only simulates
    weather in the active world.
    """
    main_home = _get_sim_home_world(main_si) if main_si else None
    other_si = contact.get("sim_info")
    other_home = _get_sim_home_world(other_si) if other_si else None

    current_world_raw = sim_context.get_current_world()
    current_world = _friendly_world_name(current_world_raw) if current_world_raw else None
    player_loc = current_world or main_home

    current_weather = sim_context.get_current_weather()

    parts = []
    if player_loc and current_weather:
        parts.append(f"in {player_loc} it's currently {current_weather}")
    elif player_loc:
        climate = _get_world_climate(player_loc)
        if climate:
            parts.append(f"player is in {player_loc} ({climate})")

    if other_home and (not player_loc or other_home.lower() != player_loc.lower()):
        other_climate = _get_world_climate(other_home)
        if other_climate:
            parts.append(f"{contact['name']} is in {other_home} ({other_climate})")

    if not parts:
        return ""
    return f"\n[WEATHER: {'; '.join(parts)}. Reference naturally if it fits, never as a forced topic.]"


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


def _start_conversation(contact, first_message, recipient_sim=None, kind="text"):
    """Store a new conversation, keyed by recipient sim_id.

    `kind` is "call" or "text" -- generate_reply() uses it to decide
    whether to apply the artificial "sim is thinking" reply_delay. Texts
    feel weirder when they appear instantly, so they get the delay;
    calls are a live conversation, so they should hit the popup as
    soon as the AI returns."""
    global _last_active_recipient_id
    if recipient_sim is None or not getattr(recipient_sim, "sim_id", None):
        # No recipient — fall back to a sentinel key so this still works (e.g. send_text)
        rid = 0
    else:
        rid = recipient_sim.sim_id
    _conversations[rid] = {
        "contact": contact,
        "recipient": recipient_sim,
        "kind": kind,
        "history": [{"role": "them", "text": first_message}],
    }
    _last_active_recipient_id = rid


def _mark_reply_intent(recipient_sim):
    """Called when the player clicks the Reply button on a phone dialog.
    Locks in which conversation the next llama.reply should target."""
    global _pending_reply_recipient_id
    if recipient_sim and getattr(recipient_sim, "sim_id", None):
        _pending_reply_recipient_id = recipient_sim.sim_id


def _take_conversation_for_reply():
    """Pick the right conversation when the player runs llama.reply.
    Priority:
      1. Conversation flagged by the most recent Reply-button click (cleared after use).
      2. Conversation for the currently selected/active sim.
      3. Most-recently-started conversation.
    """
    global _pending_reply_recipient_id
    if _pending_reply_recipient_id and _pending_reply_recipient_id in _conversations:
        convo = _conversations[_pending_reply_recipient_id]
        _pending_reply_recipient_id = None
        return convo
    try:
        active = sim_context.get_active_sim()
        if active and active.sim_info:
            rid = active.sim_info.sim_id
            if rid in _conversations:
                return _conversations[rid]
    except Exception:
        pass
    if _last_active_recipient_id is not None and _last_active_recipient_id in _conversations:
        return _conversations[_last_active_recipient_id]
    return None


def get_active_conversation():
    """Return the conversation that llama.reply would currently target, or None.
    NOTE: does not consume the pending-reply flag."""
    if _pending_reply_recipient_id and _pending_reply_recipient_id in _conversations:
        return _conversations[_pending_reply_recipient_id]
    try:
        active = sim_context.get_active_sim()
        if active and active.sim_info:
            rid = active.sim_info.sim_id
            if rid in _conversations:
                return _conversations[rid]
    except Exception:
        pass
    if _last_active_recipient_id is not None and _last_active_recipient_id in _conversations:
        return _conversations[_last_active_recipient_id]
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

    sim_history = journal.format_sim_history_for_prompt(
        contact["name"],
        recipient_name=recipient_name,
        trailing_note=_journal_obsolescence_note(contact),
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact, recipient=recipient)
    mutual_block = _format_mutual_block(mutuals, casual=True)


    recipient_block = _describe_recipient(recipient, contact=contact)

    events_text = events.format_shared_events_for_prompt(recipient, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""

    prompt = (
        f"Caller info:\n{rel_desc}{history_block}{mutual_block}\n\n"
        f"{recipient_block}{events_block}\n\n"
        f"They are calling {recipient_name}{_location_context(recipient, contact)}.{_season_context()}{_weather_context(recipient, contact)}\n\n"
        f"Write what {contact['name']} says during this phone call."
    )

    def _on_result(text, error):
        title = f"Call from {contact['name']}"
        if text:
            text = _apply_mood_from_text(text, recipient=recipient, is_incoming=True)
            _start_conversation(contact, text, recipient_sim=recipient, kind="call")
            journal.add_entry("call", f"Call from {contact['name']} (to {recipient_name}):\n{text}", sim_name=contact["name"], recipient_name=recipient_name)
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

    sim_history = journal.format_sim_history_for_prompt(
        contact["name"],
        recipient_name=recipient_name,
        trailing_note=_journal_obsolescence_note(contact),
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact, recipient=recipient)
    mutual_block = _format_mutual_block(mutuals, casual=True)


    recipient_block = _describe_recipient(recipient, contact=contact)

    events_text = events.format_shared_events_for_prompt(recipient, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""

    prompt = (
        f"Sender info:\n{rel_desc}{history_block}{mutual_block}\n\n"
        f"{recipient_block}{events_block}\n\n"
        f"They are texting {recipient_name}{_location_context(recipient, contact)}.{_season_context()}{_weather_context(recipient, contact)}\n\n"
        f"Write 1-2 short text messages from {contact['name']}."
    )

    def _on_result(text, error):
        title = f"Text from {contact['name']}"
        if text:
            text = _apply_mood_from_text(text, recipient=recipient, is_incoming=True)
            _start_conversation(contact, text, recipient_sim=recipient)
            journal.add_entry("text", f"Text from {contact['name']} (to {recipient_name}):\n{text}", sim_name=contact["name"], recipient_name=recipient_name)
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
    sim_history = journal.format_sim_history_for_prompt(
        other_name,
        recipient_name=main_name,
        trailing_note=_journal_obsolescence_note(contact),
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact, recipient=recipient)
    mutual_block = _format_mutual_block(mutuals, casual=False)

    events_text = events.format_shared_events_for_prompt(recipient, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""

    prompt = (
        f"Relationship info:\n{rel_desc}{history_block}{mutual_block}{events_block}\n\n"
        f"Conversation so far:\n{convo_text}\n\n"
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
            history.append({"role": "them", "text": text_clean})
            journal.add_entry(
                kind,
                f"Conversation with {other_name}:\n"
                f"{main_name}: {player_message}\n"
                f"{other_name}: {text_clean}",
                sim_name=other_name,
                recipient_name=main_name,
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

        if delay > 0:
            threading.Timer(delay, _show_reply).start()
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

    # Seed the conversation with the player's outgoing message as turn 1
    _start_conversation(contact, "", recipient_sim=main_si)
    rid = main_si.sim_id if (main_si and getattr(main_si, "sim_id", None)) else 0
    _conversations[rid]["history"] = [{"role": "you", "text": player_message}]

    _refresh_milestones_for(contact, main_si)

    language = config.get_language()
    system = _REPLY_SYSTEM.format(
        language=language,
        other_name=other_name,
        main_name=main_name,
    )
    rel_desc = _describe_relationship(contact)
    sim_history = journal.format_sim_history_for_prompt(
        other_name,
        recipient_name=main_name,
        trailing_note=_journal_obsolescence_note(contact),
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""
    mutuals = _get_mutual_contacts(contact)
    mutual_block = _format_mutual_block(mutuals, casual=False)

    events_text = events.format_shared_events_for_prompt(main_si, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""

    prompt = (
        f"Relationship info:\n{rel_desc}{history_block}{mutual_block}{events_block}\n\n"
        f"{main_name} just texted {other_name}: \"{player_message}\"\n\n"
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
            if rid in _conversations:
                _conversations[rid]["history"].append({"role": "them", "text": text_clean})
            journal.add_entry(
                "text",
                f"Text conversation with {other_name}:\n"
                f"{main_name}: {player_message}\n"
                f"{other_name}: {text_clean}",
                sim_name=other_name,
                recipient_name=main_name,
            )
            title = f"Reply from {other_name}"
            sender_si = contact.get("sim_info")
            shown = False
            if sender_si:
                shown = _show_phone_dialog(sender_si, title, text_clean, ring=False)
            if not shown:
                notifications.show(title, text_clean, output=output)
            if callback:
                callback(text_clean, None)

        if delay > 0:
            threading.Timer(delay, _show_reply).start()
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

    _start_conversation(contact, "", recipient_sim=main_si, kind="call")
    rid = main_si.sim_id if (main_si and getattr(main_si, "sim_id", None)) else 0
    _conversations[rid]["history"] = [{"role": "you", "text": player_topic}]

    _refresh_milestones_for(contact, main_si)

    language = config.get_language()
    system = _CALL_SYSTEM.format(language=language)
    rel_desc = _describe_relationship(contact)
    sim_history = journal.format_sim_history_for_prompt(
        other_name,
        recipient_name=main_name,
        trailing_note=_journal_obsolescence_note(contact),
    )
    history_block = f"\n\n{sim_history}" if sim_history else ""
    mutuals = _get_mutual_contacts(contact)
    mutual_block = _format_mutual_block(mutuals, casual=False)

    events_text = events.format_shared_events_for_prompt(main_si, contact.get("sim_info"))
    events_block = f"\n\n{events_text}" if events_text else ""

    prompt = (
        f"Person being called:\n{rel_desc}{history_block}{mutual_block}{events_block}\n\n"
        f"{main_name} is calling {other_name}. {main_name} says: \"{player_topic}\"\n\n"
        f"Write what {other_name} says in response (3-5 lines of dialogue). "
        f"They should react naturally to what {main_name} said."
    )

    def _on_send_call_result(text, error):
        if text:
            text = _apply_mood_from_text(text, recipient=main_si, is_incoming=False)
            if rid in _conversations:
                _conversations[rid]["history"].append({"role": "them", "text": text})
            journal.add_entry(
                "call",
                f"Call with {other_name}:\n"
                f"{main_name}: {player_topic}\n"
                f"{other_name}: {text}",
                sim_name=other_name,
                recipient_name=main_name,
            )
            title = f"Call with {other_name}"
            caller_si = contact.get("sim_info")
            shown = False
            if caller_si:
                shown = _show_phone_dialog(caller_si, title, text)
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
