import os as _os
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
"""Sims in other worlds setting (world_distance = near / far / same): the
cfg and in-game value are read right, every call / text / reply prompt gets
the matching Geography rule with no leftover placeholder, the per-message
GEOGRAPHY tag agrees with it, and the Texts & replies row shows a readable
label and cycles through all three."""
import sys, os, types, re, configparser, importlib.util

REPO = _REPO
fails = []


def chk(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)


# Real config.py, reading the release cfg (and, separately, an old cfg without the line).
spec = importlib.util.spec_from_file_location("llamafone_config", os.path.join(REPO, "src", "llamafone", "config.py"))
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)
SETTINGS = {}
config.get_setting = lambda k, fallback=None: SETTINGS.get(k, fallback)
release_cfg = open(os.path.join(REPO, "llamafone.cfg"), encoding="utf-8").read()
CFG = {"text": release_cfg}


def _cfg():
    cp = configparser.ConfigParser(inline_comment_prefixes=None)
    cp.read_string(CFG["text"])
    return cp


config.get_config = _cfg
chk("release cfg: world_distance = near", re.search(r"^world_distance = near$", release_cfg, re.M) is not None)
chk("release cfg still has the placeholder key", "api_key = YOUR_API_KEY_HERE" in release_cfg)
tmpl = config._DEFAULT_CONFIG_TEMPLATE
chk("fresh-install template has world_distance = near", re.search(r"^world_distance = near$", tmpl, re.M) is not None)
chk("default (release cfg) is near", config.get_world_distance() == "near")
CFG["text"] = re.sub(r"^world_distance = .*\n", "", release_cfg, flags=re.M)
chk("older cfg without the line -> near", config.get_world_distance() == "near")
CFG["text"] = release_cfg.replace("world_distance = near", "world_distance = FAR ")
chk("cfg 'FAR ' -> far", config.get_world_distance() == "far")
CFG["text"] = release_cfg.replace("world_distance = near", "world_distance = sometimes")
chk("cfg typo -> near", config.get_world_distance() == "near")
SETTINGS["world_distance"] = "same"
chk("in-game setting beats the cfg", config.get_world_distance() == "same")
SETTINGS["world_distance"] = "bogus"
CFG["text"] = release_cfg.replace("world_distance = near", "world_distance = far")
chk("bad in-game value falls back to the cfg", config.get_world_distance() == "far")
SETTINGS.clear(); CFG["text"] = release_cfg

# Real prompt constants + _distance_rule from phone.py.
src = open(os.path.join(REPO, "src", "llamafone", "phone.py"), encoding="utf-8").read()
ns = {"config": config}
a, b = src.index("def _distance_rule"), src.index("_PHONE_ELIGIBLE_AGES = ")
exec(compile(src[a:b], "phone_prompts", "exec"), ns)


def prompts():
    return {
        "call": ns["_CALL_SYSTEM"].format(language="English",
                                          distance_rule=ns["_distance_rule"]("the caller", "the player")),
        "text": ns["_TEXT_SYSTEM"].format(language="English",
                                          distance_rule=ns["_distance_rule"]("the sender", "the player")),
        "reply": ns["_REPLY_SYSTEM"].format(language="English", other_name="Luca", main_name="Francesca",
                                            distance_rule=ns["_distance_rule"]("Luca", "Francesca")),
    }


def geo(p):
    """The Geography section of a prompt."""
    i = p.index("# Geography rule")
    return p[i:p.index("\n# ", i + 1)]


for d in ("near", "far", "same"):
    SETTINGS["world_distance"] = d
    for name, p in prompts().items():
        g = geo(p)
        tag = f"{d}/{name}"
        chk(f"{tag}: no leftover placeholder", "{distance_rule}" not in p and "{" not in g)
        chk(f"{tag}: one DIFFERENT-worlds rule", g.count("- DIFFERENT worlds:") == 1)
        chk(f"{tag}: SAME-world and HOUSEHOLD rules kept", "- SAME world:" in g and "- HOUSEHOLD / CLOSE" in g)
        if d == "near":
            chk(f"{tag}: short trip, dinner/dates OK, no travel excuses",
                "A SHORT TRIP" in g and "dinner, a date" in g and "No excuses about distance or travel" in g)
            chk(f"{tag}: still no chance run-ins across worlds", "never claim to have \"run into\"" in g)
            chk(f"{tag}: nothing says long-distance", "long-distance" not in g.lower() and "EXCEPTION" not in g)
        elif d == "far":
            chk(f"{tag}: long-distance rule + EXCEPTION as before",
                "LONG-DISTANCE" in g and "PLANNED visit" in g and "EXCEPTION --" in g
                and "Distance is an obstacle to work around" in g)
        else:
            chk(f"{tag}: one big town, run-ins OK", "ONE BIG TOWN" in g and "ran into X at the" in g
                and "EXCEPTION" not in g)
    SETTINGS["world_distance"] = d
    r = geo(prompts()["reply"])
    if d != "same":
        chk(f"{d}/reply: names filled in", "Francesca" in r and "Luca's world EXACTLY" in r)
SETTINGS.clear()

# The per-message GEOGRAPHY tag follows the same setting.
a = src.index("def _location_context")
b = src.index("\ndef ", a + 1)
HOMES = {}
lns = {"config": config, "_get_sim_home_world": lambda si: HOMES.get(si),
       "_friendly_world_name": lambda w: w,
       "sim_context": types.SimpleNamespace(get_current_world=lambda: "Willow Creek")}
exec(compile(src[a:b], "phone_loc", "exec"), lns)
FRAN = type("SI", (), {"first_name": "Francesca"})()
LUCA, NEIGH = object(), object()
HOMES.update({FRAN: "Willow Creek", LUCA: "San Myshuno", NEIGH: "Willow Creek"})
for d, want in (("near", "a short trip apart: in-person plans OK"),
                ("far", "NO casual in-person meetups, long-distance only"),
                ("same", "one town: in-person plans and run-ins OK")):
    SETTINGS["world_distance"] = d
    t = lns["_location_context"](FRAN, {"name": "Luca Woodruff", "sim_info": LUCA})
    chk(f"tag ({d}): {want}", want in t and "DIFFERENT worlds" in t)
    t2 = lns["_location_context"](FRAN, {"name": "Bea", "sim_info": NEIGH})
    chk(f"tag ({d}): same world unchanged", "SAME world, in-person plans OK" in t2)
SETTINGS.clear()

# The settings row: readable label, and tapping cycles near -> far -> same -> near.
usrc = open(os.path.join(REPO, "src", "llamafone", "phone_ui_interactions.py"), encoding="utf-8").read()
row = re.search(r'\{\s*"key":\s*"world_distance".*?\n        \},', usrc, re.S)
chk("settings row exists under Texts & replies", row is not None and '"home":   "texting"' in row.group(0))
uns = {"config": config, "REOPENED": []}
for fn in ("def _format_value", "def _on_setting_picked"):
    a = usrc.index(fn)
    b = usrc.index("\ndef ", a + 1)
    exec(compile(usrc[a:b], "ui_slice", "exec"), uns)
uns["_reopen_home_for"] = lambda sim, s: uns["REOPENED"].append(s["key"])
setting = {"key": "world_distance", "label": "Sims in other worlds: {value}", "kind": "enum",
           "values": config.WORLD_DISTANCES,
           "labels": {"near": "A SHORT TRIP", "far": "FAR AWAY", "same": "SAME TOWN"},
           "getter": config.get_world_distance}
config.set_setting = lambda k, v: SETTINGS.__setitem__(k, v)
seen = [uns["_format_value"](setting)]
for _ in range(3):
    uns["_on_setting_picked"](None, setting)
    seen.append(uns["_format_value"](setting))
chk("row cycles short trip -> far away -> same town -> short trip",
    seen == ["Sims in other worlds: A SHORT TRIP", "Sims in other worlds: FAR AWAY",
             "Sims in other worlds: SAME TOWN", "Sims in other worlds: A SHORT TRIP"])
chk("row reopens Texts & replies after each tap", uns["REOPENED"] == ["world_distance"] * 3)
chk("the row in the file uses the same labels",
    row is not None and all(x in row.group(0) for x in ("A SHORT TRIP", "FAR AWAY", "SAME TOWN")))

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}")
