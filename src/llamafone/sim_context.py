"""
Collects information about the current game state to give the AI rich context.
All Sims 4 API calls are wrapped in try/except to handle version differences gracefully.
"""

# Mapping from sims4.common.Pack enum attribute name → friendly pack name.
# Each entry is tried individually so unknown/future packs don't crash anything.
_PACK_MAP = {
    # Expansion Packs
    "EP01": "Get to Work",
    "EP02": "Get Together",
    "EP03": "City Living",
    "EP04": "Cats & Dogs",
    "EP05": "Seasons",
    "EP06": "Get Famous",
    "EP07": "Island Living",
    "EP08": "Discover University",
    "EP09": "Eco Lifestyle",
    "EP10": "Snowy Escape",
    "EP11": "Cottage Living",
    "EP12": "High School Years",
    "EP13": "Growing Together",
    "EP14": "Horse Ranch",
    "EP15": "For Rent",
    "EP16": "Life & Death",
    # Game Packs
    "GP01": "Outdoor Retreat",
    "GP02": "Spa Day",
    "GP03": "Dine Out",
    "GP04": "Vampires",
    "GP05": "Parenthood",
    "GP06": "Jungle Adventure",
    "GP07": "StrangerVille",
    "GP08": "Realm of Magic",
    "GP09": "Star Wars: Journey to Batuu",
    "GP10": "Dream Home Decorator",
    "GP11": "My Wedding Stories",
    "GP12": "Werewolves",
    "GP13": "Lovestruck",
    "GP14": "Businesses & Hobbies",
}

# Cache so we only scan once per session
_installed_packs_cache = None

def get_installed_packs():
    """
    Return a list of friendly pack names the player has installed.
    Results are cached after the first call.
    """
    global _installed_packs_cache
    if _installed_packs_cache is not None:
        return _installed_packs_cache

    installed = []
    try:
        import sims4.common
        for attr, name in _PACK_MAP.items():
            try:
                pack = getattr(sims4.common.Pack, attr, None)
                if pack is not None and sims4.common.is_available_pack(pack):
                    installed.append(name)
            except Exception:
                continue
    except Exception:
        pass

    _installed_packs_cache = installed
    return installed


def get_anchor_sim():
    """
    Return a sim_info to anchor narrative context to. Prefers the active sim
    (whoever the player is currently controlling); falls back to the first
    teen+ household member if active sim is unavailable.
    """
    active = get_active_sim()
    if active and active.sim_info:
        return active.sim_info
    # Fallback: first teen+ member of the active household
    try:
        import services
        hh = services.active_household()
        if hh:
            for si in hh.sim_info_gen():
                try:
                    age_str = str(getattr(si, "age", "")).replace("Age.", "").upper().replace(" ", "")
                    if age_str in ("TEEN", "YOUNGADULT", "YOUNG_ADULT", "ADULT", "ELDER"):
                        return si
                except Exception:
                    continue
    except Exception:
        pass
    return None


# Backward-compatible alias — kept so any lingering callers still work.
def get_main_sim_info():
    return get_anchor_sim()


def _resolve_localized_string(loc_string):
    """Best-effort: pull a human-readable string out of a Sims 4
    LocalizedString protobuf. The protobuf has a `tokens` repeated field
    where each token may have a `raw_text` containing the actual text.
    Returns None if nothing usable can be extracted."""
    if loc_string is None:
        return None
    if isinstance(loc_string, str):
        return loc_string
    # Try the tokens path -- most club / household / sim names are stored
    # as a single RAW_TEXT token with the literal name.
    try:
        tokens = getattr(loc_string, "tokens", None)
        if tokens:
            for t in tokens:
                raw = getattr(t, "raw_text", None)
                if raw:
                    return str(raw)
    except Exception:
        pass
    # Last resort: try the LocalizationHelperTuning approach, but only
    # use the result if it's not still a protobuf-style string.
    try:
        from sims4.localization import LocalizationHelperTuning
        out = LocalizationHelperTuning.get_raw_text(loc_string)
        if isinstance(out, str) and "raw_text" not in out and not out.startswith("hash:"):
            return out
    except Exception:
        pass
    return None


def _get_trait_name(trait):
    """Extract a readable name from a trait object, trying multiple access patterns."""
    for attr in ("__name__", ):
        try:
            n = getattr(trait, attr, None)
            if n:
                return n
        except Exception:
            pass
    try:
        return type(trait).__name__
    except Exception:
        return str(trait)


def _read_relationship_for_target(rt, target_sim_id, sim_manager):
    """Read relationship data for a specific target sim using the tracker API."""
    _MEANINGFUL_BITS = (
        "Friend", "Enemy", "Romantic", "Married", "Engaged",
        "Divorced", "BFF", "Crush", "Partner", "Hate",
        "Family", "Despise", "Sibling", "Parent", "Child",
    )
    _ROMANTIC_BITS = (
        "Romantic", "Married", "Engaged", "Crush", "Lover", "Partner",
        "Soulmate", "Sweetheart", "Dating",
    )

    other_si = sim_manager.get(target_sim_id)
    if not other_si:
        return None

    # Get relationship bits (collect raw bit names first, filter romantic ones later if romance==0)
    raw_bits = []
    try:
        bits = rt.get_all_bits(target_sim_id)
        if bits:
            for bit in bits:
                bn = _get_trait_name(bit)
                if any(kw in bn for kw in _MEANINGFUL_BITS):
                    raw_bits.append(bn)
    except Exception:
        pass

    # Get friendship/romance scores
    friendship, romance = 0, 0

    # Locate the Relationship object — try several APIs since the access pattern
    # varies across game versions.
    rel = None
    try:
        if hasattr(rt, "find_relationship"):
            rel = rt.find_relationship(target_sim_id)
    except Exception:
        rel = None
    if rel is None:
        try:
            rel = rt.relationships[target_sim_id]
        except Exception:
            try:
                rel = rt._relationships[target_sim_id]
            except Exception:
                pass
    if rel is None:
        # Last resort: iterate
        try:
            container = getattr(rt, "_relationships", None) or getattr(rt, "relationships", None)
            if container is not None:
                items = container.values() if hasattr(container, "values") else container
                for r in items:
                    try:
                        if (getattr(r, "sim_id_a", None) == target_sim_id or
                            getattr(r, "sim_id_b", None) == target_sim_id):
                            rel = r
                            break
                    except Exception:
                        continue
        except Exception:
            pass

    # Read both tracks from the relationship object
    if rel is not None:
        try:
            tracks = (getattr(rel, "_relationship_tracks", None) or
                      getattr(rel, "relationship_tracks", None) or
                      getattr(rel, "tracks", None))
            if tracks:
                track_iter = tracks.values() if hasattr(tracks, "values") else tracks
                for track in track_iter:
                    try:
                        tn = type(track).__name__.lower()
                        val = int(track.get_value())
                        if "romance" in tn:
                            romance = val
                        elif "friendship" in tn or "friend" in tn or "acquaint" in tn:
                            friendship = val
                    except Exception:
                        continue
        except Exception:
            pass

    # Fallback for friendship via the single-score API (doesn't help with romance)
    if friendship == 0:
        try:
            fn = getattr(rt, "get_relationship_score", None)
            if fn:
                try:
                    val = fn(target_sim_id)
                    if val is not None:
                        friendship = int(val)
                except Exception:
                    pass
        except Exception:
            pass

    # If any bit indicates the relationship is now platonic ("Just Friends" etc.),
    # treat the whole relationship as platonic and strip all romantic bits.
    is_platonic_now = False
    for bn in raw_bits:
        bn_low = bn.lower().replace("_", "")
        if "justfriends" in bn_low or "justgoodfriends" in bn_low or "platonic" in bn_low:
            is_platonic_now = True
            break

    # Whitelist of meaningful relationship words. Bits that don't match any
    # whitelisted word are internal/system bits and get dropped entirely
    # (instead of showing as "bit NoLongerFriends" or "sentimentBit Actor CloseTo Target...").
    _STATUS_WHITELIST = (
        "Friend", "Friends", "Friendly", "Good", "Best", "BFF",
        "Enemy", "Hate", "Despise", "Rival",
        "Married", "Spouse", "Engaged", "Fiance",
        "Crush", "Lover", "Soulmate", "Sweetheart", "Dating",
        "Romantic", "Partner",
        "Broken", "BrokenUp", "Ex", "Former", "Divorced",
        "NoLonger", "Estranged", "Lost", "HasBeen",
        "Sibling", "Brother", "Sister",
        "Parent", "Mother", "Father", "Mom", "Dad",
        "Child", "Son", "Daughter",
        "Grandparent", "Grandfather", "Grandmother", "Granny", "Grandpa",
        "Grandchild", "Grandson", "Granddaughter",
        "Aunt", "Uncle", "Niece", "Nephew", "Cousin",
        "Family", "Inlaw", "InLaw", "Acquaintance",
    )

    def _clean_status_label(bn):
        stripped = bn.replace("RelationshipBit_", "").replace("Romantic_", "")
        # Drop sentimentBit/familyRelationshipBitsAcquired and similar internal prefixes
        parts = stripped.split("_")
        kept = [p for p in parts if p in _STATUS_WHITELIST]
        if kept:
            # Combine adjacent tokens nicely (e.g. NoLonger + Friends -> "No Longer Friends")
            return " ".join(kept).replace("NoLonger", "No Longer").replace("HasBeen", "Has Been").strip()
        return ""

    # Filter out romantic bits if platonic-now is set OR romance score is 0
    bit_labels = []
    for bn in raw_bits:
        is_romantic = any(kw in bn for kw in _ROMANTIC_BITS)
        if is_romantic and (is_platonic_now or romance == 0):
            continue
        label = _clean_status_label(bn)
        if label and label not in bit_labels:
            bit_labels.append(label)

    return {
        "sim_info": other_si,
        "sim_id": target_sim_id,
        "name": f"{other_si.first_name} {other_si.last_name}".strip(),
        "status": ", ".join(bit_labels[:3]),
        "friendship": friendship,
        "romance": romance,
    }


def get_sim_network(main_si, min_friendship=25):
    """
    Return (household_members, relationships) for any sim.

    Household members come directly from the active household — they always
    appear even without relationship data. Outside relationships come from
    the sim's relationship tracker using target_sim_gen().
    """
    # --- Household members: always include everyone in the household ---
    household_members = []
    household_ids = set()
    try:
        import services
        hh = services.active_household()
        if hh:
            for si in hh.sim_info_gen():
                if si.sim_id == main_si.sim_id:
                    continue
                household_ids.add(si.sim_id)
                household_members.append({
                    "sim_info": si,
                    "sim_id": si.sim_id,
                    "name": f"{si.first_name} {si.last_name}".strip(),
                    "status": "",
                    "friendship": None,
                    "romance": None,
                    "in_household": True,
                })
    except Exception:
        pass

    # --- Relationships: iterate target_sim_gen() ---
    relationships = []
    seen_ids = set(household_ids)

    try:
        import services
        sm = services.sim_info_manager()
        rt = main_si.relationship_tracker

        for target_sim_id in rt.target_sim_gen():
            try:
                entry = _read_relationship_for_target(rt, target_sim_id, sm)
                if not entry:
                    continue

                sid = entry["sim_id"]

                # Enrich household member entries with relationship data
                if sid in household_ids:
                    for hm in household_members:
                        if hm["sim_id"] == sid:
                            hm["status"] = entry["status"]
                            hm["friendship"] = entry["friendship"]
                            hm["romance"] = entry["romance"]
                            break
                    continue

                if sid in seen_ids:
                    continue
                seen_ids.add(sid)

                # Filter non-household by significance
                if not entry["status"] and abs(entry.get("friendship") or 0) < min_friendship:
                    continue

                entry["in_household"] = False
                relationships.append(entry)
            except Exception:
                continue
    except Exception:
        pass

    household_members.sort(key=lambda x: x["name"])
    relationships.sort(key=lambda x: -(abs(x.get("friendship") or 0)))

    return household_members, relationships


# Backward-compatible alias
def get_main_sim_network(main_si, min_friendship=25):
    return get_sim_network(main_si, min_friendship=min_friendship)


def _safe(obj, attr, default=None):
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def get_active_sim():
    """Return the currently controlled Sim, or None."""
    try:
        import services
        client = services.client_manager().get_first_client()
        if client and client.active_sim:
            return client.active_sim
    except Exception:
        pass
    return None


_NOISE_TRAIT_KEYWORDS = (
    "basemental",       # Basemental Drugs / addiction mod traits
    "attraction",       # Wonderful/Wicked Whims attraction-system bits
    "turnon", "turnoff",
    "civicpolicy",      # Eco Lifestyle civic policy effects
    "familyrelationship",
    "neutralrel", "lowrival", "highrival",
    "acquired",
    "ftue",             # First-time user experience (tutorial-only)
    "hidden", "ghost",  # internal/occult flag traits
    "reputation",       # Get Famous reputation tracker
    "simpreference",    # Lovestruck preferences (likes/dislikes)
    "handedness",       # Left/Right-handed
    "hometurf",         # Snowy Escape lifestyle marker
    "lifestyle",        # Snowy Escape lifestyles (Energetic, Workaholic, etc.)
    "sentiment",        # Growing Together sentiment bits
    "relationship_track",
    "relbit",
)


def get_sim_traits(sim_info, limit=6):
    """Return a list of cleaned personality trait names — filtered to player-facing ones."""
    try:
        # Prefer personality_traits (just the picked-at-CAS traits), then fall back.
        raw = None
        for accessor in (
            lambda: list(sim_info.trait_tracker.personality_traits),
            lambda: list(sim_info.trait_tracker.equipped_traits),
            lambda: list(sim_info.trait_tracker.traits),
            lambda: list(sim_info.get_traits()),
        ):
            try:
                raw = accessor()
                if raw:
                    break
            except Exception:
                continue

        if not raw:
            return []

        names = []
        for t in raw:
            name = _get_trait_name(t)
            low = name.lower()
            if any(kw in low for kw in _NOISE_TRAIT_KEYWORDS):
                continue
            # Skip non-personality bits like "gender" markers
            if "gender" in low and "trait_" not in low:
                continue
            cleaned = (name
                .replace("trait_", "").replace("Trait_", "")
                .replace("_", " ").strip().title())
            # Drop anything left that still looks like a system bit
            if ":" in cleaned or any(kw in cleaned.lower() for kw in _NOISE_TRAIT_KEYWORDS):
                continue
            if cleaned and cleaned not in names:
                names.append(cleaned)
                if len(names) >= limit:
                    break
        return names
    except Exception:
        return []


def get_sim_mood(sim_info):
    """Return the sim's current mood as a readable string."""
    try:
        mood = sim_info.get_mood()
        if mood:
            name = _get_trait_name(mood)
            return name.replace("Mood_", "").replace("mood_", "").replace("_", " ").strip() or "Neutral"
    except Exception:
        pass
    return "Neutral"


def get_sim_skills(sim_info, min_level=1, limit=12):
    """
    Return a dict of {skill_name: level} for skills the sim has learned.
    Only includes skills at or above min_level. Sorted highest first.
    """
    skills = {}
    try:
        tracker = sim_info.skill_tracker
        if not tracker:
            return skills
        # Try multiple ways to iterate skills
        stat_items = None
        for accessor in (
            lambda: tracker._statistics.values(),
            lambda: tracker.statistics.values(),
            lambda: tracker._all_skills(),
            lambda: tracker.all_skills(),
        ):
            try:
                stat_items = list(accessor())
                if stat_items:
                    break
            except Exception:
                continue

        if not stat_items:
            return skills

        for stat_inst in stat_items:
            try:
                level = int(stat_inst.get_value())
                if level < min_level:
                    continue
                name = type(stat_inst).__name__
                cleaned = (name
                    .replace("Skill_Adult_", "")
                    .replace("Skill_Child_", "")
                    .replace("Skill_Toddler_", "")
                    .replace("Skill_Teen_", "")
                    .replace("Skill_", "")
                    .replace("_", " ")
                    .title())
                skills[cleaned] = level
            except Exception:
                continue
    except Exception:
        pass
    sorted_skills = dict(sorted(skills.items(), key=lambda x: -x[1])[:limit])
    return sorted_skills


def get_sim_relationships(sim_info, limit=8):
    """
    Return a list of relationship dicts for this sim's notable relationships.
    Each dict has: name, status (relationship bit labels), and optionally scores.
    """
    relationships = []
    try:
        import services
        rel_tracker = sim_info.relationship_tracker
        sim_manager = services.sim_info_manager()
        my_id = sim_info.sim_id

        for rel in rel_tracker.relationships.values():
            try:
                other_id = rel.sim_id_b if rel.sim_id_a == my_id else rel.sim_id_a
                other_si = sim_manager.get(other_id)
                if not other_si:
                    continue

                name = f"{other_si.first_name} {other_si.last_name}".strip()

                # Relationship bit labels (Friend, Enemy, Married, etc.)
                bit_labels = []
                try:
                    for bit in rel.relationship_bit_tracker.relationship_bits:
                        bit_name = bit.__name__
                        _visible_keywords = (
                            "Friend", "Enemy", "Romantic", "Married", "Divorced",
                            "BFF", "Acquaintance", "Hate", "Despise", "Crush",
                            "Partner", "Engaged", "FamilyRelationship",
                        )
                        if any(kw in bit_name for kw in _visible_keywords):
                            label = (bit_name
                                .replace("RelationshipBit_", "")
                                .replace("Romantic_", "")
                                .replace("_", " ")
                                .strip())
                            bit_labels.append(label)
                except Exception:
                    pass

                # Try to get numeric friendship/romance scores
                friendship = None
                romance = None
                try:
                    for track_stat in rel._relationship_tracks.values():
                        track_name = track_stat.__class__.__name__.lower()
                        val = int(track_stat.get_value())
                        if "romance" in track_name:
                            romance = val
                        elif "friend" in track_name or "acquaint" in track_name:
                            friendship = val
                except Exception:
                    pass

                entry = {"name": name}
                if bit_labels:
                    entry["status"] = ", ".join(bit_labels[:3])
                if friendship is not None:
                    entry["friendship"] = friendship
                if romance is not None and romance != 0:
                    entry["romance"] = romance

                # Only include relationships with some substance
                if bit_labels or (friendship is not None and abs(friendship) > 10):
                    relationships.append(entry)
                    if len(relationships) >= limit:
                        break
            except Exception:
                continue
    except Exception:
        pass
    return relationships


def get_sim_career(sim_info):
    """Return the sim's career name if employed."""
    try:
        career_tracker = sim_info.career_tracker
        if career_tracker:
            for career in career_tracker.careers.values():
                return career.__class__.__name__.replace("_", " ").title()
    except Exception:
        pass
    return None


def get_current_world():
    """Return the friendly name of the world the player is currently in, or None.
    This is the zone the active household is loaded into — for vacations, this differs
    from the sims' home world."""
    try:
        import services
        zone = services.current_zone()
        if not zone:
            return None
        zone_id = getattr(zone, "id", None)
        if not zone_id:
            return None
        from world.region import get_region_instance_from_zone_id
        region = get_region_instance_from_zone_id(zone_id)
        if not region:
            return None
        name = getattr(region, "__name__", "") or str(region)
        cleaned = (name
            .replace("Region_", "")
            .replace("region_", "")
            .replace("_", " ")
            .strip())
        return cleaned if cleaned else None
    except Exception:
        return None


def get_current_weather():
    """Return a short description of current weather in the active world,
    or None if Seasons isn't installed / weather isn't readable.

    Examples: "light rain, cool", "snowing, freezing", "thunderstorm",
    "overcast, cool", "clear, hot".

    Combines two signals from WeatherService:
      1. `_current_weather_types` -- set of WeatherType enums for temp,
         sky cover, and special events (thunderstorm).
      2. `_trans_info` -- dict mapping PrecipitationType.RAIN=1000 and
         .SNOW=1001 to a WeatherElementTuple whose start_value is the
         current precipitation intensity (0.0-1.0). The weather-types
         set only fires the "Rain"/"Snow" entry above a high threshold,
         so for light precipitation we need to read intensities directly.
    """
    try:
        import services
        ws = services.weather_service()
        if not ws:
            return None

        # --- Weather types: temperature, sky cover, thunder ---
        types = None
        try:
            types = ws.get_current_weather_types()
        except Exception:
            types = getattr(ws, "_current_weather_types", None)
        type_names = set()
        if types:
            for t in types:
                try:
                    raw = str(t).split(".")[-1].split(" ")[0].upper()
                    type_names.add(raw)
                except Exception:
                    continue

        temp = None
        if "BURNING" in type_names:
            temp = "scorching"
        elif "HOT" in type_names or "HEATWAVE" in type_names:
            temp = "hot"
        elif "WARM" in type_names:
            temp = "warm"
        elif "COOL" in type_names:
            temp = "cool"
        elif "COLD" in type_names:
            temp = "cold"
        elif "FREEZING" in type_names:
            temp = "freezing"

        # --- Precipitation intensity from _trans_info ---
        # PrecipitationType.RAIN = 1000, .SNOW = 1001 (Sims 4 enum values).
        # Each entry is a WeatherElementTuple(start_value, start_time,
        # end_value, end_time) describing a linear transition from start
        # to end. The "current" intensity is the linear interpolation
        # against now. We try to interpolate, but fall back to max(start,
        # end) on error so we don't miss rain that's transitioning.
        def _ticks(t):
            for attr in ("absolute_ticks", "value", "ticks"):
                fn = getattr(t, attr, None)
                if callable(fn):
                    try:
                        return float(fn())
                    except Exception:
                        continue
                if fn is not None:
                    try:
                        return float(fn)
                    except Exception:
                        continue
            try:
                # Last-resort: parse "DateAndTime(123456)" via str()
                raw = str(t)
                if "(" in raw and raw.endswith(")"):
                    return float(raw.split("(")[-1][:-1])
            except Exception:
                pass
            return None

        try:
            import services
            ts = services.time_service()
            now = getattr(ts, "sim_now", None) if ts else None
        except Exception:
            now = None
        now_ticks = _ticks(now) if now is not None else None

        def _read_intensity(elem_id):
            trans = getattr(ws, "_trans_info", None)
            if not trans:
                return 0.0
            entry = None
            try:
                entry = trans.get(elem_id)
            except Exception:
                entry = None
            if entry is None:
                # Some patches key by enum object instead of int.
                try:
                    for k, v in trans.items():
                        try:
                            if int(getattr(k, "value", k)) == elem_id:
                                entry = v
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
            if entry is None:
                return 0.0
            try:
                start_val = float(getattr(entry, "start_value", 0) or 0)
                end_val = float(getattr(entry, "end_value", start_val) or start_val)
            except Exception:
                return 0.0
            # Try to interpolate. If anything fails, take the max of
            # start/end so we don't silently miss rain in transition.
            if now_ticks is None:
                return max(start_val, end_val)
            t_start = _ticks(getattr(entry, "start_time", None))
            t_end = _ticks(getattr(entry, "end_time", None))
            if t_start is None or t_end is None or t_end <= t_start:
                return max(start_val, end_val)
            if now_ticks <= t_start:
                return start_val
            if now_ticks >= t_end:
                return end_val
            ratio = (now_ticks - t_start) / (t_end - t_start)
            return start_val + (end_val - start_val) * ratio

        rain_intensity = _read_intensity(1000)
        snow_intensity = _read_intensity(1001)

        # Thunder/lightning special case (any storm wins outright).
        has_thunder = any(("THUNDER" in n or "LIGHTNING" in n) for n in type_names)

        # Thresholds tuned to match the game's own "Light Rain" UI cutoff
        # (the OUTDOOR_AUTONOMY_RAIN_PENALTIES tuning starts a non-zero
        # rain penalty at 0.02, so anything at/near that is visible rain
        # to the player).
        precip = None
        if has_thunder:
            precip = "thunderstorm"
        elif snow_intensity > rain_intensity and snow_intensity > 0.01:
            if snow_intensity > 0.6:
                precip = "heavy snow"
            elif snow_intensity > 0.2:
                precip = "snowing"
            else:
                precip = "light snow"
        elif rain_intensity > 0.01:
            if rain_intensity > 0.6:
                precip = "heavy rain"
            elif rain_intensity > 0.2:
                precip = "raining"
            else:
                precip = "light rain"

        # Sky cover (only used when no precipitation headline).
        sky = None
        if not precip:
            if "CLEAR_SKIES" in type_names:
                sky = "clear"
            elif "CLOUDY_FULL" in type_names or "OVERCAST" in type_names:
                sky = "overcast"
            elif "PARTLY_CLOUDY" in type_names or "CLOUDY_PARTIAL" in type_names:
                sky = "partly cloudy"
            elif any("FOG" in n for n in type_names):
                sky = "foggy"

        if precip and temp:
            return f"{precip}, {temp}"
        if precip:
            return precip
        if sky and temp:
            return f"{sky}, {temp}"
        if sky:
            return sky
        if temp:
            return f"clear, {temp}"
        return None
    except Exception:
        return None


def get_current_season():
    """Return the current season name (Spring/Summer/Fall/Winter), or None if Seasons isn't installed."""
    try:
        import services
        season_service = services.season_service()
        if not season_service:
            return None
        # Try a few common APIs
        season = None
        for attr in ("season", "_season", "current_season"):
            season = getattr(season_service, attr, None)
            if season is not None:
                break
        if season is None:
            try:
                season = season_service.get_season()
            except Exception:
                pass
        if season is None:
            return None
        # Convert enum to readable name
        name = str(season).split(".")[-1].title()
        # Common Sims 4 season enum values: SPRING, SUMMER, FALL, WINTER
        mapping = {"Spring": "Spring", "Summer": "Summer", "Fall": "Fall", "Winter": "Winter",
                   "Autumn": "Fall"}
        return mapping.get(name, name)
    except Exception:
        return None


def get_sim_clubs(sim_info, limit=4):
    """Return a list of club names the sim is a member of (Get Together packs)."""
    clubs = []
    if not sim_info:
        return clubs
    try:
        import services
        club_service = services.get_club_service()
        if not club_service:
            return clubs
        sid = getattr(sim_info, "sim_id", None)
        # The service exposes clubs via .clubs or a generator; try both
        all_clubs = None
        try:
            all_clubs = list(club_service.clubs)
        except Exception:
            try:
                all_clubs = list(getattr(club_service, "_clubs", {}).values())
            except Exception:
                pass
        if not all_clubs:
            return clubs
        for club in all_clubs:
            try:
                members = getattr(club, "members", None) or []
                # Check membership both ways (sim_info object, or sim_id)
                member_ids = set()
                for m in members:
                    mid = getattr(m, "sim_id", None) or getattr(getattr(m, "sim_info", None), "sim_id", None)
                    if mid:
                        member_ids.add(mid)
                if sid is not None and sid not in member_ids:
                    continue
                # Get the club name. In Sims 4 the club name is a LocalizedString
                # protobuf with a `tokens` list -- str()ing it produces the raw
                # protobuf dump ("hash: 12345 tokens { raw_text: 'Lumina' }"),
                # not the human-readable name. Extract the raw_text field directly.
                name = None
                try:
                    cname = getattr(club, "name", None)
                    if cname:
                        name = _resolve_localized_string(cname)
                except Exception:
                    pass
                if not name:
                    name = type(club).__name__
                name = str(name).strip()
                # Strip leftover protobuf debris if anything snuck through
                if "raw_text" in name or name.startswith("hash:"):
                    name = ""
                if not name:
                    continue
                if name and name not in clubs:
                    clubs.append(name)
                    if len(clubs) >= limit:
                        break
            except Exception:
                continue
    except Exception:
        pass
    return clubs


def get_sim_aspiration(sim_info):
    """Return the sim's current aspiration name, or None for tutorial/placeholder ones."""
    try:
        asp = sim_info.primary_aspiration
        if asp:
            raw = asp.__name__
            low = raw.lower()
            # Filter out non-aspiration internal placeholders:
            #   FTUE = tutorial; Track_Location_A/B = quest tracking; Test/Debug = internal
            if any(kw in low for kw in ("ftue", "track_location", "track location",
                                        "_test", "_debug", "placeholder")):
                return None
            cleaned = raw.replace("aspiration_", "").replace("Aspiration_", "").replace("_", " ").title()
            # Strip trailing single letters from labels like "Track Location A"
            words = cleaned.split()
            if words and len(words[-1]) == 1 and words[-1].isalpha():
                words = words[:-1]
            return " ".join(words) if words else None
    except Exception:
        pass
    return None


def get_sim_info_dict(sim):
    """Build a context dict for a single sim."""
    info = {"name": "Unknown Sim"}
    try:
        si = sim.sim_info
        first = _safe(si, "first_name", "")
        last = _safe(si, "last_name", "")
        info["name"] = f"{first} {last}".strip() or "Unknown Sim"
        info["age"] = str(_safe(si, "age", "Unknown")).replace("Age.", "")
        info["gender"] = str(_safe(si, "gender", "Unknown")).replace("Gender.", "")
        info["mood"] = get_sim_mood(si)
        info["traits"] = get_sim_traits(si)

        career = get_sim_career(si)
        if career:
            info["career"] = career

        aspiration = get_sim_aspiration(si)
        if aspiration:
            info["aspiration"] = aspiration

        info["skills"] = get_sim_skills(si)
        info["relationships"] = get_sim_relationships(si)

    except Exception:
        pass
    return info


def get_household_context():
    """Build a context dict for the active household."""
    try:
        import services
        household = services.active_household()
        if not household:
            return {}

        members = []
        for si in household.sim_info_gen():
            try:
                first = _safe(si, "first_name", "")
                last = _safe(si, "last_name", "")
                name = f"{first} {last}".strip() or "Unknown"
                age = str(_safe(si, "age", "")).replace("Age.", "")
                mood = get_sim_mood(si)
                traits = get_sim_traits(si, limit=4)
                career = get_sim_career(si)
                entry = {"name": name, "age": age, "mood": mood, "traits": traits}
                if career:
                    entry["career"] = career
                members.append(entry)
            except Exception:
                continue

        funds = "unknown"
        try:
            funds = str(household.funds.money)
        except Exception:
            pass

        return {
            "household_name": str(_safe(household, "name", "Unknown Household")),
            "members": members,
            "funds": funds,
        }
    except Exception:
        return {}


def get_current_lot_name():
    """Return the name of the current lot/venue."""
    try:
        import services
        zone = services.current_zone()
        if zone:
            lot = zone.lot
            if lot:
                return str(_safe(lot, "lot_name", "Unknown Lot"))
    except Exception:
        pass
    return None


def _format_rel_entry(r):
    """Format a single relationship entry from get_main_sim_network() for a prompt."""
    line = f"    - {r['name']}"
    if r.get("status"):
        line += f" ({r['status']})"
    if r.get("friendship") is not None:
        line += f" [friendship: {r['friendship']}]"
    if r.get("romance") is not None:
        line += f" [romance: {r['romance']}]"
    return line


def _is_sim_ghost(sim_info):
    """True if the sim is a ghost / deceased. Best-effort, tries multiple APIs."""
    if not sim_info:
        return False
    try:
        ig = getattr(sim_info, "is_ghost", None)
        if ig is not None:
            val = ig() if callable(ig) else ig
            if val:
                return True
    except Exception:
        pass
    try:
        if getattr(sim_info, "is_dead", False):
            return True
    except Exception:
        pass
    try:
        death_type = getattr(sim_info, "death_type", None)
        if death_type is not None:
            dt_str = str(death_type)
            if dt_str and "NONE" not in dt_str.upper():
                return True
    except Exception:
        pass
    return False


def build_context_string(sim=None):
    """
    Build a human-readable context string to include in prompts.

    Centred on the focus sim — the explicit `sim` argument if given,
    otherwise the anchor sim (active sim, falling back to first teen+ in
    the household). Household members and the focus sim's relationship
    network are included.
    """
    lines = []

    if sim:
        focus_si = sim.sim_info if hasattr(sim, "sim_info") else None
    else:
        focus_si = get_anchor_sim()

    if focus_si:
        name = f"{focus_si.first_name} {focus_si.last_name}".strip()
        lines.append(f"Focus Sim: {name}")
        lines.append(f"  Age: {str(_safe(focus_si, 'age', '')).replace('Age.', '')}")
        lines.append(f"  Mood: {get_sim_mood(focus_si)}")

        traits = get_sim_traits(focus_si)
        if traits:
            lines.append(f"  Traits: {', '.join(traits)}")

        career = get_sim_career(focus_si)
        if career:
            lines.append(f"  Career: {career}")

        asp = get_sim_aspiration(focus_si)
        if asp:
            lines.append(f"  Aspiration: {asp}")

        skills = get_sim_skills(focus_si)
        if skills:
            lines.append(f"  Skills: {', '.join(f'{k} {v}' for k, v in skills.items())}")

        household_members, relationships = get_sim_network(focus_si)

        if household_members:
            lines.append("\nHousehold:")
            for m in household_members:
                msi = m["sim_info"]
                m_mood = get_sim_mood(msi)
                m_traits = get_sim_traits(msi, limit=3)
                m_line = f"  - {m['name']} ({str(_safe(msi, 'age', '')).replace('Age.', '')}, {m_mood} mood)"
                if m_traits:
                    m_line += f", traits: {', '.join(m_traits)}"
                if m.get("status"):
                    m_line += f" [{m['status']}]"
                if _is_sim_ghost(msi):
                    m_line += " [DECEASED — only reference in past tense]"
                lines.append(m_line)

        try:
            import services
            hh = services.active_household()
            if hh:
                funds = str(_safe(hh.funds, "money", "?"))
                lines.append(f"  Household funds: §{funds}")
        except Exception:
            pass

        if relationships:
            lines.append(f"\n{name}'s Relationships (outside household):")
            for r in relationships:
                entry = _format_rel_entry(r)
                rsi = r.get("sim_info")
                if rsi and _is_sim_ghost(rsi):
                    entry += " [DECEASED — only reference in past tense]"
                lines.append(entry)

    # Current location vs home — flag if traveling/on vacation
    current_world = get_current_world()
    home_world = None
    if focus_si:
        try:
            household = focus_si.household
            if household and household.home_zone_id:
                from world.region import get_region_instance_from_zone_id
                region = get_region_instance_from_zone_id(household.home_zone_id)
                if region:
                    raw = getattr(region, "__name__", "") or str(region)
                    home_world = raw.replace("Region_", "").replace("region_", "").replace("_", " ").strip()
        except Exception:
            pass
    if current_world and home_world and current_world.lower() != home_world.lower():
        lines.append(f"\nCURRENT LOCATION: {focus_si.first_name} is in {current_world} (on vacation — NOT at home in {home_world})")
    else:
        lot = get_current_lot_name()
        if lot:
            lines.append(f"\nCurrent Location: {lot}")

    # Season for narrative consistency
    season = get_current_season()
    if season:
        lines.append(f"Season: {season}")

    packs = get_installed_packs()
    if packs:
        lines.append(f"\nInstalled Packs: {', '.join(packs)}")
    else:
        lines.append("\nInstalled Packs: base game only (or could not detect)")

    return "\n".join(lines) if lines else "No game context available (not in an active save)."


def build_context_string_with_journal(sim=None):
    """
    Full context string including recent journal history.
    Use this for story, event, and chat prompts where past events matter.
    Skip it for quick dialogue prompts where latency is more important.
    """
    from . import journal  # local import to avoid circular dependency
    ctx = build_context_string(sim=sim)
    history = journal.format_for_prompt()
    if history:
        return f"{ctx}\n\n{history}"
    return ctx
