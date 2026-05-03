"""
Phone calls and texts -- generates AI-powered messages from relationship sims.
Uses the fast model for quick generation.

Calls show as modal phone dialogs with the caller's portrait (ring).
Texts show as phone dialogs with buzz.
Players can reply with claude.reply <message> to continue the conversation.
"""
import random

from . import api_client, sim_context, config, journal, notifications, moodlets

# Tracks the current conversation so the player can reply
# Format: {"contact": contact_dict, "history": [{"role": "them"|"you", "text": str}, ...]}
_active_conversation = None


def _show_phone_dialog(caller_sim_info, title, message, ring=True):
    """
    Show a phone dialog with the caller's portrait and Reply/Dismiss buttons.
    If the player clicks Reply, a hint is shown in the cheat console.
    """
    try:
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog import UiDialogOkCancel, PhoneRingType
        from distributor.shared_messages import IconInfoData
        import services

        client = services.client_manager().get_first_client()
        if not client or not client.active_sim_info:
            return False

        loc_text = LocalizationHelperTuning.get_raw_text(message)
        loc_title = LocalizationHelperTuning.get_raw_text(title)
        loc_reply = LocalizationHelperTuning.get_raw_text("Reply")
        loc_dismiss = LocalizationHelperTuning.get_raw_text("Dismiss")

        dialog = UiDialogOkCancel.TunableFactory().default(
            client.active_sim_info,
            text=lambda: loc_text,
            title=lambda: loc_title,
            text_ok=lambda: loc_reply,
            text_cancel=lambda: loc_dismiss,
        )
        dialog.phone_ring_type = PhoneRingType.RING if ring else PhoneRingType.BUZZ

        def _on_response(response_dialog):
            try:
                if response_dialog.accepted:
                    import sims4.commands
                    sims4.commands.output(
                        "[Claude AI] Open the cheat console and type your reply:", None
                    )
                    sims4.commands.output(
                        "[Claude AI]   claude.reply <your message>", None
                    )
            except Exception:
                pass

        dialog.add_listener(_on_response)
        dialog.show_dialog(icon_override=IconInfoData(obj_instance=caller_sim_info))
        return True
    except Exception:
        pass
    return False

_CALL_SYSTEM = """You are writing one side of a phone call in The Sims 4. You are writing \
what the CALLER says (the player's sim is listening). Write in {language}.

CRITICAL -- The #1 rule is: FAMILY RELATIONSHIP AND AGE OVERRIDE EVERYTHING ELSE.
If the sim info says "Family relationship: Father" — this sim IS the recipient's dad. \
Speak EXACTLY like a father talking to his child. Not a buddy, not a peer. A DAD.
If it says "Family relationship: Mother" — speak like a mom. And so on for all family roles.

Family dynamics on the phone — but ALWAYS check the friendship/romance scores and \
relationship bits too. A Father with low friendship is distant, awkward, maybe estranged. \
A Mother who is also listed as "Enemy" is hostile or passive-aggressive. \
Family role sets the dynamic, but the SCORES show how healthy that relationship is.
- Close Father (high friendship): warm, proud, checking in. "Hey kiddo, how are things?"
- Distant Father (low friendship): awkward, stilted, maybe trying to reconnect. \
  "Hi... it's been a while. I was just thinking I should call."
- Estranged Parent (negative friendship): tense, defensive, guilt-tripping.
- Grandparent: doting, reminiscing, concerned about health.
- Sibling: teasing, competitive, inside references — unless they're rivals.
- Spouse: intimate, shorthand — unless the relationship is strained.

Age shapes HOW they speak:
- Teens: dramatic, slang, school drama. Young Adults: ambition, nightlife, dating.
- Adults: measured, career/family topics, NOT youth slang. No "yo" or "bro" or "dude".
- Elders: wisdom, nostalgia, health complaints, long stories.

Traits add flavor ON TOP of age and family role:
- Hot-Headed: rants. Romantic: flirts. Gloomy: sighs. Loner: keeps it short.
- Geek: references. Evil: backhanded. Mean: blunt. Good: warm.

Rules:
- Write 2-3 SHORT lines of dialogue (what the caller says). Keep it brief — like a real \
  quick phone call, not a monologue. ONE topic, not multiple.
- The call should have a reason: sharing news, asking for advice, inviting somewhere, \
  gossiping, complaining, celebrating, or just checking in
- Occasionally sprinkle in Simlish words naturally (Sul sul, Dag dag, Nooboo)
- Never use profanity or explicit content
- NEVER write romantic, flirty, or sexual content for FAMILY members \
  (parents, children, siblings, grandparents, grandchildren, in-laws, aunts/uncles, cousins). \
  Family relationships in the context are NEVER romantic regardless of romance score.
- Do NOT assume sims with the same last name are related or share a household — \
  only use what's explicitly stated in the relationship info above.
- Geography matters: ONLY mention "running into" or "bumping into" mutual contacts if \
  those mutuals live in the SAME world as the caller. Don't claim to randomly run into \
  someone who lives in a different world. For long-distance friends, reference seeing \
  them on social media, hearing from them, or planning visits — not chance encounters.
- Write dialogue lines only, prefixed with the caller's first name
- NEVER break character. NEVER say you don't have information or need more details. \
  You are the sim — always stay in character and improvise. If someone mentions a person \
  or event you weren't given details about, react naturally (curious, gossipy, confused, etc.) \
  but NEVER acknowledge that you are an AI.

IMPORTANT: On the very last line of your response, write MOOD: followed by the emotional \
impact this call would have on the RECIPIENT. Pick exactly one: \
happy, confident, flirty, inspired, focused, energized, playful, sad, angry, tense, \
embarrassed, bored, uncomfortable, dazed"""

_TEXT_SYSTEM = """You are writing text messages from a Sim in The Sims 4. Write in {language}.

CRITICAL -- The #1 rule is: FAMILY RELATIONSHIP AND AGE OVERRIDE EVERYTHING ELSE.
If the sim info says "Family relationship: Father" — this sim IS the recipient's dad. \
Write EXACTLY like a father texting his child. Not a buddy, not a peer, not a bro. A DAD.
If it says "Family relationship: Mother" — write like a mom. And so on for all family roles.

Family dynamics — but ALWAYS check friendship/romance scores and relationship bits too. \
A Father with low friendship is distant or awkward. A Mother listed as "Enemy" is hostile. \
Family role sets the dynamic, but SCORES show how healthy that relationship actually is.
- Close Father/Mother (high friendship): warm, proud, maybe overbearing. \
  "That's wonderful news, son. I'm so proud of you."
- Distant Father/Mother (low friendship): stilted, brief, maybe guilt-tripping. \
  "Oh. That's... good to hear. Congratulations."
- Grandparent: doting, formal, might misuse emoji. "Dear [name], what lovely news!!"
- Sibling: teasing, inside jokes — unless they're rivals (check bits).
- Spouse: intimate, shorthand — unless strained.

Age shapes HOW they express themselves:
- Teens: abbreviations, lots of emoji, dramatic, lowercase. "omggg no way 😭😭"
- Young Adults: mix of casual and articulate. "hey are you free tonight?"
- Adults: complete sentences, proper punctuation, minimal emoji. NOT hip slang. NOT "yo" or "bro". \
  "Hi! Wanted to check in. Are you free this weekend?"
- Elders: formal, warm, sometimes overly detailed. May over-explain or write like an email. \
  "Hello dear, I hope you're doing well. I was thinking of you and wanted to reach out."

Traits add flavor ON TOP of age and family role:
- Hot-Headed: caps lock, exclamation marks. Gloomy: ellipses, sad emoji.
- Snob: proper grammar, condescending. Goofball: random, playful.
- Romantic: hearts, flirty. Loner: terse, minimal. Evil: passive aggressive.

Rules:
- Write 1-2 SHORT text messages — like a real text, not a paragraph. Max 2 sentences each. \
  ONE topic, not multiple updates jammed together.
- The text should have a purpose: making plans, sharing news/gossip, asking a question, \
  or reacting to something
- Never use profanity or explicit content
- NEVER write romantic, flirty, or sexual content for FAMILY members \
  (parents, children, siblings, grandparents, grandchildren, in-laws, aunts/uncles, cousins). \
  Family relationships in the context are NEVER romantic regardless of romance score.
- Do NOT assume sims with the same last name are related or share a household — \
  only use what's explicitly stated in the sender info above.
- Geography matters: ONLY mention "running into" or "bumping into" mutual contacts if \
  those mutuals live in the SAME world as the sender. For long-distance friends, \
  reference seeing them on social media, video calls, or planning visits.
- NEVER break character. NEVER say you don't have information, can't roleplay, or need more details. \
  You are the sim — always stay in character and improvise naturally. If someone mentions a person \
  or event you weren't given details about, react like the sim would (curious, gossipy, confused, etc.) \
  but NEVER acknowledge that you are an AI or that you lack information.

IMPORTANT: On the very last line of your response, write MOOD: followed by the emotional \
impact this text would have on the RECIPIENT. Pick exactly one: \
happy, confident, flirty, inspired, focused, energized, playful, sad, angry, tense, \
embarrassed, bored, uncomfortable, dazed"""

_REPLY_SYSTEM = """You are writing text message replies from a Sim in The Sims 4. Write in {language}.

You are writing as {other_name}, replying to a message from {main_name}.
You will be given the conversation history so far.

CRITICAL -- The #1 rule: FAMILY RELATIONSHIP AND AGE OVERRIDE EVERYTHING.
If {other_name}'s info says "Family relationship: Father" — reply as a FATHER, not a friend.
A dad replying to his kid's text about pregnancy says "That's wonderful news" not "yo thats huge".
Match the family role FIRST, then check friendship/romance scores to gauge warmth vs distance. \
A distant father (low friendship) is awkward and stilted. A close father is warm and proud.

- Adults use complete sentences and proper punctuation. No youth slang.
- Parents are caring, proud, sometimes overbearing — unless the scores show distance or hostility.
- React authentically to what {main_name} said — don't be generic.

Rules:
- Write 1-2 SHORT text messages — like a real text, not an essay. Max 2 sentences each.
- Never use profanity or explicit content
- NEVER write romantic, flirty, or sexual content for FAMILY members \
  (parents, children, siblings, grandparents, in-laws, aunts/uncles, cousins).
- Do NOT assume sims with the same last name are related or share a household.
- NEVER break character. NEVER say you don't have information or need more details. \
  Always stay in character and improvise naturally. NEVER acknowledge that you are an AI.

IMPORTANT: On the very last line of your response, write MOOD: followed by the emotional \
impact this reply would have on {main_name}. Pick exactly one: \
happy, confident, flirty, inspired, focused, energized, playful, sad, angry, tense, \
embarrassed, bored, uncomfortable, dazed"""


def _apply_mood_from_text(text, reason=None):
    """Extract MOOD tag from text, apply the moodlet to protagonist, return cleaned text."""
    clean_text, mood_tag = moodlets.extract_mood_tag(text)
    if mood_tag:
        main_si = sim_context.get_main_sim_info()
        if not main_si:
            active = sim_context.get_active_sim()
            if active:
                main_si = active.sim_info
        if main_si:
            moodlets.apply_mood(main_si, mood_tag, reason=reason)
    return clean_text


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
    """Check if a sim is dead/ghost."""
    try:
        if sim_info.is_ghost():
            return True
    except Exception:
        pass
    try:
        # Fallback: check death type
        death_type = getattr(sim_info, "death_type", None)
        if death_type is not None and str(death_type) != "NONE":
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

    # Fallback: search the sim manager directly and build a contact dict
    try:
        import services
        parts = full_name.strip().split(None, 1)
        first = parts[0].lower()
        last = parts[1].lower() if len(parts) > 1 else ""

        for si in services.sim_info_manager().values():
            if si.first_name.lower() != first:
                continue
            if last and si.last_name.lower() != last:
                continue
            return {
                "sim_info": si,
                "sim_id": si.sim_id,
                "name": f"{si.first_name} {si.last_name}".strip(),
                "status": "",
                "friendship": None,
                "romance": None,
                "in_household": False,
            }
    except Exception:
        pass
    return None


def _pick_random_relationship_sim():
    """Pick a random non-household sim from the protagonist's relationship network."""
    main_si = sim_context.get_main_sim_info()
    if main_si:
        _household_members, relationships = sim_context.get_main_sim_network(main_si)
        contacts = relationships  # only non-household sims
    else:
        active = sim_context.get_active_sim()
        if not active:
            return None
        rels = sim_context.get_sim_relationships(active.sim_info)
        contacts = [r for r in rels if not r.get("in_household")]

    # Filter out ghosts unless config allows them
    allow_ghosts = config.get_config().getboolean("claude_ai", "phone_allow_ghosts", fallback=True)
    if not allow_ghosts:
        contacts = [c for c in contacts if not _is_ghost(c.get("sim_info"))]

    # Filter out sims currently on the same lot (no point texting/calling someone who's right there)
    on_lot = _get_sims_on_active_lot()
    if on_lot:
        contacts = [c for c in contacts if c.get("sim_id") not in on_lot]

    if not contacts:
        return None

    # Weight toward stronger relationships (but everyone has a chance)
    weights = []
    for contact in contacts:
        score = abs(contact.get("friendship") or 0) + abs(contact.get("romance") or 0)
        weights.append(max(score, 10))

    return random.choices(contacts, weights=weights, k=1)[0]


def _get_mutual_contacts(contact):
    """
    Find sims that both the protagonist and the contact have relationships with.
    Returns a list of short descriptions like "Bella Goth (your Friend, their Crush)".
    """
    mutuals = []
    try:
        main_si = sim_context.get_main_sim_info()
        other_si = contact.get("sim_info")
        if not main_si or not other_si:
            return mutuals

        # Get the protagonist's relationship targets
        main_rt = main_si.relationship_tracker
        main_targets = set(main_rt.target_sim_gen())
        main_targets.discard(other_si.sim_id)

        # Get the contact's relationship targets
        other_rt = other_si.relationship_tracker
        other_targets = set(other_rt.target_sim_gen())
        other_targets.discard(main_si.sim_id)

        # Find overlap
        shared_ids = main_targets & other_targets
        if not shared_ids:
            return mutuals

        import services
        sm = services.sim_info_manager()

        for sid in list(shared_ids)[:6]:  # cap at 6 to keep prompt reasonable
            try:
                si = sm.get(sid)
                if not si:
                    continue
                name = f"{si.first_name} {si.last_name}".strip()

                # Get relationship bits from protagonist's perspective
                main_bits = []
                try:
                    for bit in main_rt.get_all_bits(sid):
                        bn = sim_context._get_trait_name(bit)
                        for kw in ("Friend", "Enemy", "Romantic", "Married", "BFF",
                                   "Crush", "Family", "Sibling", "Parent", "Child"):
                            if kw in bn:
                                label = bn.replace("RelationshipBit_", "").replace("Romantic_", "").replace("_", " ").strip()
                                main_bits.append(label)
                                break
                except Exception:
                    pass

                # Get relationship bits from contact's perspective
                other_bits = []
                try:
                    for bit in other_rt.get_all_bits(sid):
                        bn = sim_context._get_trait_name(bit)
                        for kw in ("Friend", "Enemy", "Romantic", "Married", "BFF",
                                   "Crush", "Family", "Sibling", "Parent", "Child"):
                            if kw in bn:
                                label = bn.replace("RelationshipBit_", "").replace("Romantic_", "").replace("_", " ").strip()
                                other_bits.append(label)
                                break
                except Exception:
                    pass

                # Include their world so Claude knows if they're nearby
                world = _get_sim_home_world(si)
                world_part = f", lives in {world}" if world else ""

                if main_bits or other_bits:
                    main_label = ", ".join(main_bits[:2]) if main_bits else "acquaintance"
                    other_label = ", ".join(other_bits[:2]) if other_bits else "acquaintance"
                    mutuals.append(f"{name} (your {main_label}, their {other_label}{world_part})")
                else:
                    mutuals.append(f"{name} (mutual acquaintance{world_part})")
            except Exception:
                continue
    except Exception:
        pass
    return mutuals


# Map internal region names to friendly world names
_WORLD_NAMES = {
    "willowcreek": "Willow Creek",
    "oasissprings": "Oasis Springs",
    "newcrest": "Newcrest",
    "windenburg": "Windenburg",
    "sanmyshuno": "San Myshuno",
    "brindletonbay": "Brindleton Bay",
    "delsolvalley": "Del Sol Valley",
    "sulani": "Sulani",
    "britechester": "Britechester",
    "evergreenharbor": "Evergreen Harbor",
    "forgottenhollow": "Forgotten Hollow",
    "strangerville": "StrangerVille",
    "magicvenue": "Glimmerbrook",
    "glimmerbrook": "Glimmerbrook",
    "mtkomorebi": "Mt. Komorebi",
    "henfordonbagley": "Henford-on-Bagley",
    "henford": "Henford-on-Bagley",
    "tartosa": "Tartosa",
    "weddingworld": "Tartosa",
    "magnoliapromenade": "Magnolia Promenade",
    "moonwoodmill": "Moonwood Mill",
    "copperdale": "Copperdale",
    "selvadorada": "Selvadorada",
    "batuu": "Batuu",
    "alienworld": "Sixam",
    "granitefalls": "Granite Falls",
    "outdoorretreat": "Granite Falls",
    "sansequoia": "San Sequoia",
    "chestnutridge": "Chestnut Ridge",
    "tomarang": "Tomarang",
    "ciudadenamorada": "Ciudad Enamorada",
    "ravenwood": "Ravenwood",
    "nordhaven": "Nordhaven",
    "innisgreen": "Innisgreen",
}


def _friendly_world_name(raw):
    """Convert internal region name like 'WeddingWorld' to 'Tartosa'."""
    if not raw:
        return None
    key = raw.lower().replace(" ", "").replace("_", "")
    return _WORLD_NAMES.get(key, raw)


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


def _location_context(main_si, contact):
    """Build a short string describing where each sim lives, if known."""
    main_home = _get_sim_home_world(main_si) if main_si else None
    other_si = contact.get("sim_info")
    other_home = _get_sim_home_world(other_si) if other_si else None

    if main_home and other_home:
        if main_home.lower() == other_home.lower():
            return f" (both live in {main_home})"
        return f" ({main_si.first_name} lives in {main_home}, {contact['name']} lives in {other_home})"
    elif main_home:
        return f" ({main_si.first_name} lives in {main_home})"
    elif other_home:
        return f" ({contact['name']} lives in {other_home})"
    return ""


def _get_family_relationship(other_si, contact):
    """
    Try to determine the precise family relationship between the protagonist
    and the other sim using genealogy tracker and relationship bits.
    Returns a string like "Father", "Daughter", "Sibling" or None.
    """
    main_si = sim_context.get_main_sim_info()
    if not main_si or not other_si:
        return None

    # Try genealogy tracker first — most precise
    try:
        from sims.genealogy_tracker import FamilyRelationshipIndex
        gen = main_si.genealogy
        if gen:
            # Check if other_si is a parent
            for parent_idx in (FamilyRelationshipIndex.MOTHER, FamilyRelationshipIndex.FATHER):
                try:
                    parent_id = gen.get_family_relationship(parent_idx)
                    if parent_id == other_si.sim_id:
                        gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                        return "Father" if gender == "MALE" else "Mother"
                except Exception:
                    pass

        # Check if protagonist is a parent of other_si
        other_gen = other_si.genealogy
        if other_gen:
            for parent_idx in (FamilyRelationshipIndex.MOTHER, FamilyRelationshipIndex.FATHER):
                try:
                    parent_id = other_gen.get_family_relationship(parent_idx)
                    if parent_id == main_si.sim_id:
                        gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                        return "Son" if gender == "MALE" else "Daughter"
                except Exception:
                    pass

        # Check for siblings (share a parent)
        if gen and other_gen:
            try:
                for idx in (FamilyRelationshipIndex.MOTHER, FamilyRelationshipIndex.FATHER):
                    my_parent = gen.get_family_relationship(idx)
                    their_parent = other_gen.get_family_relationship(idx)
                    if my_parent and my_parent == their_parent:
                        gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                        return "Brother" if gender == "MALE" else "Sister"
            except Exception:
                pass
    except Exception:
        pass

    # Fallback: check relationship bits for family keywords
    try:
        rt = main_si.relationship_tracker
        bits = rt.get_all_bits(other_si.sim_id)
        if bits:
            for bit in bits:
                bn = sim_context._get_trait_name(bit)
                bn_lower = bn.lower()
                if "parent" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Father" if gender == "MALE" else "Mother"
                if "child" in bn_lower or "offspring" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Son" if gender == "MALE" else "Daughter"
                if "sibling" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Brother" if gender == "MALE" else "Sister"
                if "grandparent" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Grandfather" if gender == "MALE" else "Grandmother"
                if "grandchild" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Grandson" if gender == "MALE" else "Granddaughter"
                if "spouse" in bn_lower or "married" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Husband" if gender == "MALE" else "Wife"
                if "uncle" in bn_lower or "aunt" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Uncle" if gender == "MALE" else "Aunt"
                if "cousin" in bn_lower:
                    return "Cousin"
                if "niece" in bn_lower or "nephew" in bn_lower:
                    gender = str(getattr(other_si, "gender", "")).replace("Gender.", "")
                    return "Nephew" if gender == "MALE" else "Niece"
    except Exception:
        pass

    return None


def _get_protagonist_relationships():
    """
    Build an unambiguous summary of the protagonist's key relationships.
    Uses explicit "X is married to Y" phrasing to avoid confusion.
    """
    main_si = sim_context.get_main_sim_info()
    if not main_si:
        return ""

    main_name = main_si.first_name + " " + main_si.last_name
    facts = []

    try:
        rt = main_si.relationship_tracker
        import services
        sm = services.sim_info_manager()

        for target_id in rt.target_sim_gen():
            other_si = sm.get(target_id)
            if not other_si:
                continue
            other_name = other_si.first_name + " " + other_si.last_name

            try:
                bits = rt.get_all_bits(target_id)
                if not bits:
                    continue
                for bit in bits:
                    bn = sim_context._get_trait_name(bit).lower()
                    if "spouse" in bn or "married" in bn:
                        facts.append(main_name + " is married to " + other_name)
                        break
                    elif "parent" in bn:
                        facts.append(main_name + " is " + other_name + "'s parent")
                        break
                    elif "child" in bn or "offspring" in bn:
                        facts.append(main_name + " is " + other_name + "'s child")
                        break
                    elif "sibling" in bn:
                        facts.append(main_name + " and " + other_name + " are siblings")
                        break
                    elif "romantic" in bn and "married" not in bn:
                        facts.append(main_name + " is in a romantic relationship with " + other_name)
                        break
            except Exception:
                pass

            if len(facts) >= 10:
                break
    except Exception:
        pass

    if not facts:
        return ""

    return "IMPORTANT — the player's sim's relationships (these are FACTS, do not contradict them):\n" + "\n".join("- " + f for f in facts)


def _describe_relationship(contact):
    """Build a detailed character description for the prompt."""
    parts = [f"Name: {contact['name']}"]

    si = contact.get("sim_info")
    if si:
        # Age — critical for voice
        try:
            age = str(getattr(si, "age", "")).replace("Age.", "")
            if age:
                parts.append(f"Age: {age}")
        except Exception:
            pass

        # Gender
        try:
            gender = str(getattr(si, "gender", "")).replace("Gender.", "")
            if gender:
                parts.append(f"Gender: {gender}")
        except Exception:
            pass

        # Traits — the core of personality
        traits = sim_context.get_sim_traits(si, limit=6)
        if traits:
            parts.append(f"Traits: {', '.join(traits)}")

        # Mood — affects tone right now
        mood = sim_context.get_sim_mood(si)
        parts.append(f"Current mood: {mood}")

        # Career — gives them something to talk about
        career = sim_context.get_sim_career(si)
        if career:
            parts.append(f"Career: {career}")

        # Aspiration — what drives them
        aspiration = sim_context.get_sim_aspiration(si)
        if aspiration:
            parts.append(f"Aspiration: {aspiration}")

        # Home world — affects what they suggest doing together
        home = _get_sim_home_world(si)
        if home:
            parts.append(f"Lives in: {home}")

    # Family relationship — check genealogy for precise label
    family_label = _get_family_relationship(si, contact) if si else None
    if family_label:
        parts.append(f"Family relationship: {family_label}")

    if contact.get("status"):
        # Don't repeat if family label already covers it
        status = contact['status']
        if not family_label:
            parts.append(f"Relationship to your sim: {status}")
        elif status and not any(kw in status for kw in ("Family", "Parent", "Child", "Sibling")):
            # Include non-family bits alongside family label (e.g. also Friends)
            parts.append(f"Also: {status}")
    if contact.get("friendship") is not None:
        parts.append(f"Friendship level: {contact['friendship']}")
    if contact.get("romance") is not None and not family_label:
        parts.append(f"Romance level: {contact['romance']}")
    if contact.get("in_household") is True:
        parts.append("Lives in the same household as the player")

    return "\n".join(parts)


def _format_conversation_history(history, main_name, other_name):
    """Format conversation history into a prompt-readable string."""
    lines = []
    for msg in history:
        name = main_name if msg["role"] == "you" else other_name
        lines.append(f"{name}: {msg['text']}")
    return "\n".join(lines)


def _start_conversation(contact, first_message):
    """Start tracking a new conversation."""
    global _active_conversation
    _active_conversation = {
        "contact": contact,
        "history": [{"role": "them", "text": first_message}],
    }


def get_active_conversation():
    """Return the active conversation, or None."""
    return _active_conversation


def generate_call(callback=None, output=None):
    """Generate an incoming phone call from a random relationship sim."""
    contact = _pick_random_relationship_sim()
    if not contact:
        msg = "No relationship sims found. Set a protagonist with claude.set_main or build some relationships first."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    main_si = sim_context.get_main_sim_info()
    main_name = main_si.first_name if main_si else "your Sim"

    language = config.get_language()
    system = _CALL_SYSTEM.format(language=language)
    rel_desc = _describe_relationship(contact)

    sim_history = journal.format_sim_history_for_prompt(contact["name"])
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact)
    mutual_block = ""
    if mutuals:
        mutual_block = "\n\nPeople you both know:\n" + "\n".join(f"  - {m}" for m in mutuals)
        mutual_block += "\nFeel free to gossip about, mention, or bring up any of these sims naturally."

    protag_rels = _get_protagonist_relationships()
    protag_block = f"\n\n{protag_rels}" if protag_rels else ""

    prompt = (
        f"Caller info:\n{rel_desc}{history_block}{mutual_block}{protag_block}\n\n"
        f"They are calling {main_name}{_location_context(main_si, contact)}.\n\n"
        f"Write what {contact['name']} says during this phone call. "
        f"If there is past interaction history, reference or build on it naturally. "
        f"If they live in different worlds, acknowledge the distance naturally "
        f"(e.g. ask how things are there, suggest visiting, reference their world). "
        f"When referring to other sims, use correct relationship labels "
        f"(e.g. say 'your wife' not 'Vivian's spouse' if the player IS Vivian's spouse). "
        f"Make the reason for calling feel natural given their relationship."
    )

    def _on_result(text, error):
        title = f"Call from {contact['name']}"
        if text:
            text = _apply_mood_from_text(text, reason="Call from " + contact["name"])
            _start_conversation(contact, text)
            journal.add_entry("call", f"Call from {contact['name']}:\n{text}", sim_name=contact["name"])
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

    return api_client.call_claude_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_result,
    )


def generate_text(callback=None, output=None):
    """Generate a text message from a relationship sim."""
    contact = _pick_random_relationship_sim()
    if not contact:
        msg = "No relationship sims found. Set a protagonist with claude.set_main or build some relationships first."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    main_si = sim_context.get_main_sim_info()
    main_name = main_si.first_name if main_si else "your Sim"

    language = config.get_language()
    system = _TEXT_SYSTEM.format(language=language)
    rel_desc = _describe_relationship(contact)

    sim_history = journal.format_sim_history_for_prompt(contact["name"])
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact)
    mutual_block = ""
    if mutuals:
        mutual_block = "\n\nPeople you both know:\n" + "\n".join(f"  - {m}" for m in mutuals)
        mutual_block += "\nFeel free to gossip about, mention, or bring up any of these sims naturally."

    protag_rels = _get_protagonist_relationships()
    protag_block = f"\n\n{protag_rels}" if protag_rels else ""

    prompt = (
        f"Sender info:\n{rel_desc}{history_block}{mutual_block}{protag_block}\n\n"
        f"They are texting {main_name}{_location_context(main_si, contact)}.\n\n"
        f"Write 1-3 text messages from {contact['name']}. "
        f"If there is past interaction history, reference or build on it naturally. "
        f"If they live in different worlds, acknowledge the distance naturally. "
        f"When referring to other sims, use correct relationship labels "
        f"(e.g. say 'your wife' not 'Vivian's spouse' if the player IS Vivian's spouse). "
        f"Make the content feel natural given their relationship and current mood."
    )

    def _on_result(text, error):
        title = f"Text from {contact['name']}"
        if text:
            text = _apply_mood_from_text(text, reason="Text from " + contact["name"])
            _start_conversation(contact, text)
            journal.add_entry("text", f"Text from {contact['name']}:\n{text}", sim_name=contact["name"])
            sender_si = contact.get("sim_info")
            shown = False
            if sender_si:
                shown = _show_phone_dialog(sender_si, title, text, ring=False)
            if not shown:
                notifications.show(title, text, output=output)
        elif error:
            notifications.show_error(error, output=output)
        if callback:
            callback(text, error)

    return api_client.call_claude_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_result,
    )


def generate_reply(player_message, callback=None, output=None):
    """
    Reply to the active conversation. The player's message is sent as their sim,
    and the other sim responds in character.
    """
    global _active_conversation
    if not _active_conversation:
        msg = "No active conversation. Use claude.call or claude.text first to start one."
        if callback:
            callback(None, msg)
        elif output:
            notifications.show_error(msg, output=output)
        return

    contact = _active_conversation["contact"]
    history = _active_conversation["history"]

    # Add the player's message to history
    history.append({"role": "you", "text": player_message})

    main_si = sim_context.get_main_sim_info()
    main_name = main_si.first_name if main_si else "your Sim"
    other_name = contact["name"]

    language = config.get_language()
    system = _REPLY_SYSTEM.format(
        language=language,
        other_name=other_name,
        main_name=main_name,
    )
    rel_desc = _describe_relationship(contact)
    convo_text = _format_conversation_history(history, main_name, other_name)
    sim_history = journal.format_sim_history_for_prompt(other_name)
    history_block = f"\n\n{sim_history}" if sim_history else ""

    mutuals = _get_mutual_contacts(contact)
    mutual_block = ""
    if mutuals:
        mutual_block = "\n\nPeople you both know:\n" + "\n".join(f"  - {m}" for m in mutuals)

    protag_rels = _get_protagonist_relationships()
    protag_block = f"\n\n{protag_rels}" if protag_rels else ""

    prompt = (
        f"Relationship info:\n{rel_desc}{history_block}{mutual_block}{protag_block}\n\n"
        f"Conversation so far:\n{convo_text}\n\n"
        f"Write {other_name}'s reply (1-3 short text messages)."
    )

    def _on_result(text, error):
        if text:
            text = _apply_mood_from_text(text, reason="Reply from " + other_name)
            history.append({"role": "them", "text": text})
            journal.add_entry(
                "text",
                f"Conversation with {other_name}:\n"
                f"{main_name}: {player_message}\n"
                f"{other_name}: {text}",
                sim_name=other_name,
            )
            title = f"Reply from {other_name}"
            sender_si = contact.get("sim_info")
            shown = False
            if sender_si:
                shown = _show_phone_dialog(sender_si, title, text, ring=False)
            if not shown:
                notifications.show(title, text, output=output)
        elif error:
            # Remove player message from history since the reply failed
            if history and history[-1]["role"] == "you":
                history.pop()
            notifications.show_error(error, output=output)
        if callback:
            callback(text, error)

    return api_client.call_claude_async(
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

    _start_conversation(contact, "")
    _active_conversation["history"] = [{"role": "you", "text": player_message}]

    language = config.get_language()
    system = _REPLY_SYSTEM.format(
        language=language,
        other_name=other_name,
        main_name=main_name,
    )
    rel_desc = _describe_relationship(contact)
    sim_history = journal.format_sim_history_for_prompt(other_name)
    history_block = f"\n\n{sim_history}" if sim_history else ""
    mutuals = _get_mutual_contacts(contact)
    mutual_block = ""
    if mutuals:
        mutual_block = "\n\nPeople you both know:\n" + "\n".join(f"  - {m}" for m in mutuals)

    protag_rels = _get_protagonist_relationships()
    protag_block = f"\n\n{protag_rels}" if protag_rels else ""

    prompt = (
        f"Relationship info:\n{rel_desc}{history_block}{mutual_block}{protag_block}\n\n"
        f"{main_name} just texted {other_name}: \"{player_message}\"\n\n"
        f"Write {other_name}'s reply (1-3 short text messages). "
        f"If {main_name} mentions people or events you don't have details about, "
        f"improvise naturally as {other_name} would — react in character, never refuse."
    )

    def _on_send_text_result(text, error):
        if text:
            text = _apply_mood_from_text(text, reason="Text from " + other_name)
            _active_conversation["history"].append({"role": "them", "text": text})
            journal.add_entry(
                "text",
                f"Text conversation with {other_name}:\n"
                f"{main_name}: {player_message}\n"
                f"{other_name}: {text}",
                sim_name=other_name,
            )
            title = f"Reply from {other_name}"
            sender_si = contact.get("sim_info")
            shown = False
            if sender_si:
                shown = _show_phone_dialog(sender_si, title, text, ring=False)
            if not shown:
                notifications.show(title, text, output=output)
        elif error:
            notifications.show_error(error, output=output)
        if callback:
            callback(text, error)

    return api_client.call_claude_async(
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

    _start_conversation(contact, "")
    _active_conversation["history"] = [{"role": "you", "text": player_topic}]

    language = config.get_language()
    system = _CALL_SYSTEM.format(language=language)
    rel_desc = _describe_relationship(contact)
    sim_history = journal.format_sim_history_for_prompt(other_name)
    history_block = f"\n\n{sim_history}" if sim_history else ""
    mutuals = _get_mutual_contacts(contact)
    mutual_block = ""
    if mutuals:
        mutual_block = "\n\nPeople you both know:\n" + "\n".join(f"  - {m}" for m in mutuals)

    protag_rels = _get_protagonist_relationships()
    protag_block = f"\n\n{protag_rels}" if protag_rels else ""

    prompt = (
        f"Person being called:\n{rel_desc}{history_block}{mutual_block}{protag_block}\n\n"
        f"{main_name} is calling {other_name}. {main_name} says: \"{player_topic}\"\n\n"
        f"Write what {other_name} says in response (3-5 lines of dialogue). "
        f"They should react naturally to what {main_name} said."
    )

    def _on_send_call_result(text, error):
        if text:
            text = _apply_mood_from_text(text, reason="Call with " + other_name)
            _active_conversation["history"].append({"role": "them", "text": text})
            journal.add_entry(
                "call",
                f"Call with {other_name}:\n"
                f"{main_name}: {player_topic}\n"
                f"{other_name}: {text}",
                sim_name=other_name,
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

    return api_client.call_claude_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=_on_send_call_result,
    )
