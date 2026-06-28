"""
Dialogue generation — creates in-character lines for Sims based on their traits and mood.
Uses the fast model (Haiku) for snappy response times.
"""
from . import api_client, sim_context, config

_SYSTEM = """You are a creative writer for The Sims 4. Your job is to write authentic, \
entertaining dialogue that sounds like it could come from a Sim.

Rules:
- Write in {language}
- Keep each line short (1–3 sentences max)
- Match the sim's personality traits, age, and current mood closely. Match texting/speaking \
  style to age: teens use slang, adults use complete sentences, elders are more formal.
- Occasionally sprinkle in Simlish flavor words naturally:
    Sul sul! = Hello  |  Dag dag! = Goodbye  |  Nooboo = Baby  |  Freezer bunny = good luck charm
- Be playful, dramatic, or funny based on the situation
- Never use profanity or explicit content
- Write dialogue lines only — no stage directions, no quotation marks, no speaker labels
- NEVER break character or acknowledge you are an AI. Always improvise naturally.
- Only name sims explicitly listed in the context. Use generic references otherwise.
- DECEASED sims are ghosts — reference them only in past tense or as ghosts.
- Plain text output. No markdown formatting."""


def generate_sim_dialogue(sim=None, situation=None, callback=None):
    """
    Generate 3–5 lines of dialogue for a sim.

    Args:
        sim:       Sims 4 Sim object (uses active sim if None)
        situation: optional string describing what's happening
        callback:  function(text, error)
    """
    target = sim or sim_context.get_active_sim()
    language = config.get_language()
    system = _SYSTEM.format(language=language)

    if target:
        info = sim_context.get_sim_info_dict(target)
        sim_desc = (
            f"Sim: {info['name']}\n"
            f"Age: {info.get('age', 'unknown')}\n"
            f"Mood: {info.get('mood', 'unknown')}\n"
            f"Traits: {', '.join(info.get('traits', [])) or 'none known'}"
        )
        if info.get("career"):
            sim_desc += f"\nCareer: {info['career']}"
    else:
        sim_desc = "A typical Sim with no specific traits on record."

    situation_line = f"\nCurrent situation: {situation}" if situation else ""

    prompt = (
        f"{sim_desc}{situation_line}\n\n"
        "Write 4–5 lines of dialogue this Sim might say right now. "
        "Make each line feel distinct — vary the tone across the lines."
    )

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=callback,
    )


def generate_conversation(sim1, sim2, topic=None, callback=None):
    """
    Generate a back-and-forth conversation between two Sims.

    Args:
        sim1, sim2: Sims 4 Sim objects
        topic:      optional string for the conversation topic
        callback:   function(text, error)
    """
    info1 = sim_context.get_sim_info_dict(sim1)
    info2 = sim_context.get_sim_info_dict(sim2)
    language = config.get_language()
    system = _SYSTEM.format(language=language)

    name1 = info1.get("name", "Sim 1")
    name2 = info2.get("name", "Sim 2")
    topic_line = f" about {topic}" if topic else ""

    prompt = (
        f"Write a short, entertaining conversation{topic_line} between these two Sims.\n\n"
        f"{name1}:\n"
        f"  Mood: {info1.get('mood', 'unknown')}\n"
        f"  Traits: {', '.join(info1.get('traits', [])) or 'none known'}\n\n"
        f"{name2}:\n"
        f"  Mood: {info2.get('mood', 'unknown')}\n"
        f"  Traits: {', '.join(info2.get('traits', [])) or 'none known'}\n\n"
        f"Write 5–7 lines of dialogue, alternating between them. "
        f"Format each line as exactly:\n"
        f"{name1}: [line]\n"
        f"{name2}: [line]"
    )

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        use_fast_model=True,
        callback=callback,
    )


def generate_npc_backstory(sim=None, callback=None):
    """
    Generate a short backstory and personality description for a Sim.
    Great for NPCs the player just met.
    """
    target = sim or sim_context.get_active_sim()
    language = config.get_language()
    system = _SYSTEM.format(language=language)

    if target:
        info = sim_context.get_sim_info_dict(target)
        desc = (
            f"Sim: {info['name']}\n"
            f"Age: {info.get('age', 'unknown')}\n"
            f"Traits: {', '.join(info.get('traits', [])) or 'none known'}"
        )
    else:
        desc = "A mysterious Sim with unknown traits."

    prompt = (
        f"{desc}\n\n"
        "Write a short, entertaining backstory for this Sim (3–4 sentences). "
        "Then write 2 lines of dialogue that reveal their personality. "
        "Format as:\n"
        "BACKSTORY: [text]\n"
        "THEY MIGHT SAY:\n"
        "[line 1]\n"
        "[line 2]"
    )

    return api_client.call_ai_async(
        [{"role": "user", "content": prompt}],
        system=system,
        callback=callback,
    )
