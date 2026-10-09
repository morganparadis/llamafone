"""Check the current play session's real output against what 3.8.2 should do.

Reads Llamafone_Log.txt (from the last 'Starting mod load...'), the latest
Llamafone_LastPrompt.txt, and this save's mod data. Prints PASS / WARN /
FAIL lines. Real-world time is used here only to scope "this session".
"""
import glob, json, os, re, subprocess, sys

DOCS = r"C:/Users/Morgan/Documents"
MODS = DOCS + r"/Electronic Arts/The Sims 4/Mods"
LOG = DOCS + r"/Llamafone_Log.txt"
PROMPT = MODS + r"/Llamafone_LastPrompt.txt"
CFG = MODS + r"/llamafone.cfg"

results = {"PASS": 0, "WARN": 0, "FAIL": 0}


def out(level, msg):
    results[level] += 1
    print(f"{level:4}  {msg}")


REAL_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}\b|\b20\d\d-\d\d-\d\d\b|real[- ]time|real[- ]world date")
MOJIBAKE = re.compile("â€|Ã[\u00a0-\u00bf]|\ufffd")


def text_problems(s):
    probs = []
    if MOJIBAKE.search(s or ""):
        probs.append("garbled characters " + repr(MOJIBAKE.search(s).group(0)))
    if "<think>" in (s or "").lower():
        probs.append("leftover <think> block")
    return probs


# ------------------------------------------------------------------ log
lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()
start = max(i for i, l in enumerate(lines) if "Starting mod load..." in l) if len(sys.argv) < 2 else int(sys.argv[1])
sess = lines[start:]
session_start_iso = sess[0][1:20].replace(" ", "T")
print(f"== Session from {sess[0][1:20]} ({len(sess)} log lines)\n")

print("-- Log")
cfg_line = next((l for l in sess if "[config] loaded" in l), None)
if cfg_line:
    out("PASS", "cfg loaded: " + cfg_line.split("[config] ")[1][:150])
else:
    out("FAIL", "no '[config] loaded' line -- cfg not found/read")
mods = next((l for l in sess if "[config] Mods folder:" in l), None)
print("      Mods folder: " + (mods.split(": ", 1)[1] if mods else "(cfg reused; not re-resolved this session)"))
tick = next((l for l in sess if "ticks self-check" in l), None)
if tick and "OK" in tick:
    out("PASS", "game clock self-check: " + tick.split("self-check ")[1])
elif tick:
    out("FAIL", "game clock self-check: " + tick)
else:
    out("WARN", "game clock self-check not run yet")

fails = [l for l in sess if "[api]" in l and "failed after" in l]
for l in fails:
    out("FAIL", "AI request failed: " + l.split("[api] ")[1][:220])
if not fails:
    out("PASS", "no failed AI requests")

fired = [l.split("Firing auto-event: ")[1].strip() for l in sess if "Firing auto-event:" in l]
out("PASS" if fired else "WARN", f"auto-events fired: {fired or 'none yet'}")

errs = [l for l in sess if re.search(r"Traceback|Exception|Error:", l) and "Startup notification waiting" not in l]
for l in errs[:10]:
    out("WARN", "error in log: " + l[:200])

# --------------------------------------------------------------- prompt
print("\n-- Latest prompt (Llamafone_LastPrompt.txt)")
raw = open(PROMPT, encoding="utf-8", errors="replace").read()
hdr = dict(re.findall(r"^(Timestamp|Provider|Model):\s*(.*)$", raw, re.M))
cfg = open(CFG, encoding="utf-8-sig", errors="replace").read()
cfg_provider = (re.search(r"^provider\s*=\s*(\S+)", cfg, re.M) or [None, "?"])[1]
cfg_fast = (re.search(r"^fast_model\s*=\s*(\S+)", cfg, re.M) or [None, "?"])[1]
cfg_default = (re.search(r"^default_model\s*=\s*(\S+)", cfg, re.M) or [None, "?"])[1]
cfg_max = int((re.search(r"^max_tokens\s*=\s*(\d+)", cfg, re.M) or [None, "512"])[1])
print(f"      sent {hdr.get('Timestamp', '?')}  provider={hdr.get('Provider')}  model={hdr.get('Model')}")
if hdr.get("Timestamp", "") < session_start_iso:
    out("WARN", "latest prompt is from before this session -- trigger a text/call first")
out("PASS" if hdr.get("Provider") == cfg_provider else "FAIL", f"provider matches cfg ({cfg_provider})")
out("PASS" if hdr.get("Model") in (cfg_fast, cfg_default) else "FAIL", "model matches cfg")
body = raw.split("=== SYSTEM PROMPT ===", 1)[-1]
m = REAL_DATE.search(body)
out("FAIL" if m else "PASS", "no real-world dates in prompt" + (f" -- found {m.group(0)!r}: ..." + body[max(0, m.start() - 60):m.end() + 40].replace("\n", " ") + "..." if m else ""))
probs = text_problems(body)
out("FAIL" if probs else "PASS", "prompt text clean" + (": " + ", ".join(probs) if probs else ""))
est = len(body) // 4
print(f"      prompt size ~{est} tokens (+ up to {cfg_max} for the reply)")

# ----------------------------------------------------------- LM Studio
if cfg_provider == "lmstudio":
    print("\n-- LM Studio")
    try:
        r = subprocess.run(["curl", "-s", "-m", "5", "http://127.0.0.1:1234/api/v0/models"], capture_output=True, encoding="utf-8")
        models = json.loads(r.stdout).get("data", [])
        loaded = [x for x in models if x.get("state") == "loaded" and x.get("type") != "embeddings"]
        if not loaded:
            out("FAIL", "server up but no chat model loaded")
        for x in loaded:
            ctx = x.get("loaded_context_length") or 0
            ok = ctx >= est + cfg_max
            out("PASS" if ok else "FAIL", f"{x['id']} loaded with context {ctx} (needs >= ~{est + cfg_max} for this prompt; 16384 recommended)")
            out("PASS" if x["id"] in (cfg_fast, cfg_default) else "FAIL", f"cfg model name matches loaded model {x['id']!r}")
    except Exception as e:
        out("FAIL", f"LM Studio server not reachable on :1234 ({type(e).__name__})")

# ------------------------------------------------------------ save data
print("\n-- This session's saved mod data")
slot = None
for l in reversed(sess):
    mm = re.search(r"save loaded: id='([^']+)' folder='([^']+)'", l)
    if mm:
        slot = mm.group(2).replace("\\\\", "\\")
        break
if not slot or not os.path.isdir(slot):
    out("WARN", "no save loaded this session")
    slot = None


def load(name):
    p = os.path.join(slot, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


if slot:
    j = load("Journal.json") or []
    j = j if isinstance(j, list) else j.get("entries", [])
    new = [e for e in j if e.get("timestamp", "") >= session_start_iso]
    print(f"      journal: {len(new)} new entries")
    for e in new:
        tag = f"journal {e.get('type')} {e.get('sim', '')}->{e.get('recipient', '')}"
        if e.get("ticks") is None:
            out("FAIL", tag + ": no in-game time")
        if not (e.get("content") or "").strip():
            out("FAIL", tag + ": empty content")
        for p in text_problems(e.get("content")):
            out("FAIL", tag + ": " + p)
    if new and all(e.get("ticks") is not None for e in new):
        out("PASS", "all new journal entries have in-game time")

    feed = load("Feed.json") or {"posts": []}
    newp = [p for p in feed.get("posts", []) if p.get("ts", "") >= session_start_iso]
    newc = [(p, c) for p in feed.get("posts", []) for c in (p.get("comments") or []) if c.get("ts", "") >= session_start_iso]
    print(f"      Llamagram: {len(newp)} new posts, {len(newc)} new comments")
    bad = 0
    for p in newp:
        if p.get("ticks") is None:
            out("FAIL", f"post {p.get('id')}: no in-game time"); bad += 1
        for pr in text_problems(p.get("text")):
            out("FAIL", f"post {p.get('id')}: {pr}"); bad += 1
    for p, c in newc:
        if c.get("ticks") is None:
            out("FAIL", f"comment on {p.get('id')}: no in-game time"); bad += 1
        for pr in text_problems(c.get("text")):
            out("FAIL", f"comment on {p.get('id')}: {pr}"); bad += 1
    if (newp or newc) and not bad:
        out("PASS", "new posts/comments have in-game time and clean text")

    gt = load("GroupTexts.json") or {"groups": {}}
    touched = [g for g in gt.get("groups", {}).values() if g.get("last_activity", "") >= session_start_iso]
    print(f"      group texts: {len(touched)} active this session")
    for g in touched:
        ok = g.get("last_activity_ticks") is not None
        out("PASS" if ok else "FAIL", f"group {g.get('group_id')} has in-game activity time")
        for t in g.get("history") or []:
            if t.get("ts", "") >= session_start_iso:
                if t.get("ticks") is None:
                    out("FAIL", f"group {g.get('group_id')} message without in-game time")
                for pr in text_problems(t.get("text")):
                    out("FAIL", f"group {g.get('group_id')} message: {pr}")

    ms = load("Milestones.json") or []
    ms = ms if isinstance(ms, list) else ms.get("milestones", [])
    newm = [e for e in ms if e.get("timestamp", "") >= session_start_iso]
    print(f"      life events: {len(newm)} new")
    for e in newm:
        out("PASS" if e.get("sim_ticks") is not None else "FAIL", f"life event {e.get('type')} ({e.get('sim_name')}) has in-game time")

    ix = load("Interactions.json") or {}
    newi = [v for v in ix.values() if isinstance(v, dict) and v.get("timestamp", "") >= session_start_iso]
    print(f"      in-person interactions: {len(newi)} new")
    if newi:
        missing = sum(1 for v in newi if v.get("ticks") is None)
        out("FAIL" if missing else "PASS", f"{len(newi) - missing}/{len(newi)} new interactions have in-game time")

# ------------------------------------------------------ time labels
# Independent of the mod's own wording code: find each dated line of the
# last prompt in the save data, work out the in-game CALENDAR day it
# happened from the stored ticks, and check the prompt's words agree.
print("\n-- Time labels in the latest prompt vs. stored in-game times")
MIN, HOUR = 1500, 1500 * 60
DAY = HOUR * 24


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _expected_ok(label, then, now):
    """Does `label` fit an event at tick `then`, seen at tick `now`?"""
    l = label.lower()
    delta = now - then
    day_diff = now // DAY - then // DAY
    then_hour = (then % DAY) // HOUR
    if delta < 0:
        return "earlier" in l or "recently" in l
    if delta < HOUR:
        return "min" in l or "just now" in l
    if day_diff == 0:
        if then_hour < 5:
            return "last night" in l or "today" in l
        return "today" in l or "few hours" in l or "h ago" in l and "yesterday" not in l
    if day_diff == 1:
        return "yesterday" in l or "last night" in l
    if day_diff == 2:
        return "two days" in l or "2 days" in l or "2 in-game days" in l
    if day_diff < 7:
        return f"{day_diff} days" in l or f"{day_diff} in-game days" in l
    if day_diff < 14:
        return "last week" in l or "days ago" in l
    return "week" in l or "while" in l


if slot and hdr.get("Timestamp", "") >= session_start_iso:
    j_all = load("Journal.json") or []
    j_all = j_all if isinstance(j_all, list) else j_all.get("entries", [])
    after = [e for e in j_all if e.get("ticks") is not None and e.get("timestamp", "") >= hdr["Timestamp"][:19]]
    before = [e for e in j_all if e.get("ticks") is not None and e.get("timestamp", "") < hdr["Timestamp"][:19]]
    now_t = after[0]["ticks"] if after else (max(e["ticks"] for e in before) if before else None)
    m = re.search(r"CURRENT IN-GAME TIME: (\d+):(\d+) (AM|PM)", body)
    if now_t is not None and m:
        h = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)
        clock_t = (now_t // DAY) * DAY + h * HOUR + int(m.group(2)) * MIN
        drift = abs(now_t - clock_t) / HOUR
        out("PASS" if drift < 1.5 else "FAIL", f"stored ticks agree with the prompt's game clock (off by {drift:.1f}h)")
    if now_t is None:
        out("WARN", "can't tell the current in-game time from the journal")
    else:
        checked = bad = 0
        entries = [(_norm(e.get("content", "")), e["ticks"]) for e in j_all if e.get("ticks") is not None]
        for line in body.splitlines():
            mm = re.match(r"\s*\[([^\]]+)\] [\w ]+: (.*)", line)
            if not mm:
                continue
            label, preview = mm.group(1), _norm(mm.group(2)).rstrip(".")[:60]
            hit = next((t for c, t in reversed(entries) if c.startswith(preview) or preview in c), None)
            if hit is None:
                continue
            checked += 1
            if not _expected_ok(label, hit, now_t):
                bad += 1
                d = now_t // DAY - hit // DAY
                out("FAIL", f"[{label}] but it was {('today' if d == 0 else 'yesterday' if d == 1 else f'{d} in-game days ago')} "
                            f"at {(hit % DAY) // HOUR:02d}:{(hit % DAY) % HOUR // MIN:02d} -- {preview[:50]!r}")
        ms_all = load("Milestones.json") or []
        ms_all = ms_all if isinstance(ms_all, list) else ms_all.get("milestones", [])
        for line in body.splitlines():
            mm = re.match(r"\s*- \[([^\]]+)\] (.+)", line)
            if not mm:
                continue
            ev = next((e for e in reversed(ms_all) if e.get("sim_ticks") is not None
                       and _norm(e.get("description")) and _norm(e.get("description")) in _norm(mm.group(2))), None)
            if ev is None:
                continue
            checked += 1
            if not _expected_ok(mm.group(1), ev["sim_ticks"], now_t):
                bad += 1
                d = now_t // DAY - ev["sim_ticks"] // DAY
                out("FAIL", f"life event [{mm.group(1)}] {ev.get('description')!r} was {d} in-game day(s) ago")
        if checked and not bad:
            out("PASS", f"all {checked} dated lines match their stored in-game times")
        elif not checked:
            out("WARN", "no dated history lines in this prompt to check")
else:
    print("      (latest prompt is from an earlier session -- trigger a text or call first)")

print(f"\n== {results['PASS']} pass, {results['WARN']} warn, {results['FAIL']} fail")
