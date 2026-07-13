# Llamafone

A phone-first AI mod for The Sims 4. Random sims call and text you in character — voices shaped by their traits, mood, relationships, and what's actually happening in your save. Bring your own AI — Claude, OpenAI, Gemini, or a local Ollama model — and pick up calls and texts that read like they were written for the people in front of you.

**v3.4 highlights:** group texts, per-relationship contact preferences ("asked for space" auto-detected), weather + holiday awareness, past-event memory. Story updates, random events, and 3-act storylines are in the box too, for when you want them.

**Site:** [morganparadis.github.io/llamafone](https://morganparadis.github.io/llamafone/)

---

## Installation

1. **Download the latest release** from [Releases](https://github.com/morganparadis/llamafone/releases) — grab `Llamafone.ts4script`, `Llamafone.package`, and `llamafone.cfg`.
2. **Drop all three into your Mods folder:**
   - **Windows:** `Documents\Electronic Arts\The Sims 4\Mods\`
   - **macOS:** `~/Documents/Electronic Arts/The Sims 4/Mods/`
   - **Linux (Steam Proton):** `~/.steam/steam/steamapps/compatdata/<sims-4-app-id>/pfx/drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods/`
3. **Open `llamafone.cfg`** in any text editor, pick your `provider`, and paste your API key in `api_key`.
4. **In The Sims 4:** **Game Options > Other > enable Custom Content and Script Mods**, then restart the game.
5. You'll see a notification popup when the mod loads. Type `llama.status` in the cheat console to confirm setup and see all commands. Or tap your sim's phone → Social → Settings.

No Python install required for end users — the release ships compiled `.pyc` bytecode.

---

## Choose your AI provider

`provider` in `llamafone.cfg` picks where the messages come from:

| Provider | API key needed | Model examples | Where to get a key |
|---|---|---|---|
| `claude` | Yes | `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-8` | [console.anthropic.com](https://console.anthropic.com/) |
| `openai` | Yes | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `gemini` | Yes | `gemini-1.5-pro`, `gemini-1.5-flash` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `ollama` (techy) | **No** — runs locally | whatever you've `ollama pull`-ed (`llama3.2:3b` recommended for most hardware) | [ollama.com](https://ollama.com) |

For Ollama, `ollama_endpoint` in the config points at your local server (default `http://localhost:11434`). No key, no cost, no internet required after the model download. It's the most technical option — you install Ollama, keep the tray icon running, and `ollama pull` a model before the mod can use it. Run `llama.testconnection` in-game to verify setup end-to-end (checks reachability, lists installed models, verifies your configured models match, runs a tiny generation).

---

## How does Llamafone know about your Sims?

Every call and text reads live save state and sends it to the AI as context. Nobody calls about a job they don't have, nobody references a sim who doesn't exist, and nobody says "haven't seen you in ages" to someone you were with 20 minutes ago.

**What lands in the prompt:**
| Data | Example |
|---|---|
| Sim identity | name, age stage, gender, up to 6 traits, current mood, career, aspiration, top 3 skills, clubs, home world |
| Family role (dominant over traits) | "Vivian is Francesca's Mother" — parents text like parents, not peers |
| Friendship / romance labels | "best friends" / "recent breakup" / "actively dislike" — current status overrides old chat history |
| Household composition | who Francesca lives with and what those people are up to |
| Live weather | light rain, thunderstorm, heatwave; dramatic weather is fair game, routine weather stays background. Off-world callers know THEIR climate (Sulani stays tropical during global winter) |
| Recent milestones | promotions, breakups, new babies, aging up — surfaced once per contact so nobody keeps asking about a job you quit five sim-days ago |
| Past shared events | parties, weddings, funerals, birthdays, holidays — the AI can say "great party last night" the morning after |
| Today's holidays | Love Day, Winterfest, Talk Like A Pirate Day, custom holidays — surfaced as `HAPPENING TODAY` |
| Upcoming calendar events | with real focal sims ("in memory of Sawyer", "for Alex and Bailey"), no invented details |
| Recent in-person contact | in-game chats/kisses/arguments/co-presence on the same lot get logged and shown as recency (`~15 in-game min ago`, `2 in-game days ago`) |
| Group text history | when Alice and Sarah were both in a group thread with Bob, that thread's excerpt lands in Alice↔Sarah 1:1 prompts |
| Mutual contacts | shared friends/family with their careers, traits, ages, home worlds — with plausibility rules so nobody name-drops a mutual whose vibe doesn't fit the topic |
| Contact preferences you set | mute / paused / priority state + your freeform note about the contact — hoisted to the END of the prompt so the AI weights it heavily |
| Journal history for this pair | recent calls and texts between the two sims — filtered to entries BEFORE the current in-progress conversation so nothing double-counts |

**Sims-time aware.** The prompt explains that one in-game week is one season, so "Christmas Vacation in 2 weeks" gets framed as "two seasons away" rather than a casual fortnight.

**Family-role references.** When one family member mentions another, they use the relationship from the recipient's perspective ("your dad", "your sister") instead of the first name.

**In-game timestamps everywhere.** Contact preferences, interactions, and journal entries all use sim-world time in prompts. Shelving the game for real weeks doesn't rot the state — the AI sees "you asked for space 3 in-game days ago", not "3 real weeks ago".

---

## Group texts (v3.4)

Send one message to 2–4 sims at once. Each recipient replies in their own voice, staggered like real texts arriving over a few minutes. Every reply sees what the others just said, so responses stay distinct instead of a chorus of "same".

- **Phone → Social → Send Text** — the picker now allows multi-select. Pick 1 sim for a normal text, 2–4 for a group thread.
- Dialog titles and reply prompts list the full roster: *"Group text with Sarah, Bob, Alice, and Kate"*.
- Reply button fans out to the whole group — every active participant may respond.
- A one-time briefing call at group creation (uses the default model) synthesizes each participant's voice and cross-relationships, then gets cached for every subsequent reply. Per-turn cost stays sane.
- Group turns land in 1:1 prompts between shared participants as a `SHARED GROUP TEXT` excerpt block. When Alice later texts Sarah 1:1, the AI knows they were both just in a group with Bob and Kate.
- Restarting a group with the same people resumes the existing thread (cached briefing intact) instead of spawning a duplicate.

Configurable via **Phone → Social → Llamafone Settings**: `group_text_max_participants` (default 4, hard-cap 8), `group_text_enabled` (master toggle), `group_text_dropoff_enabled` (gentle "someone got busy" drop-off in later rounds, default on).

---

## Per-relationship contact preferences (v3.4)

Mute, "asked for space" (paused), or favorite (priority), plus a freeform note field — all scoped to a specific (household sim, contact) pair. Alice muting her ex doesn't affect Bob's phone activity with the same sim.

- **Phone → Social → Llamafone Settings → Manage contacts** — pick a sim, pick an action. Contacts with an existing state or note surface first, tagged in brackets ([paused], [muted], note).
- **Cheat command:** `llama.contact First Last muted|paused|priority|clear|note <text>` (scoped to the currently active household sim).
- **The paused state is the interesting one.** Instead of blocking the contact, it drops their auto-event rate to 20% AND injects a boundary note into every AI prompt for that pair. A love-heavy ex might ignore it and keep calling; a decent friend apologizes; a mean one guilt-trips. The drama plays out based on who they are.
- **Auto-detects distance signals.** "Leave me alone", "we're done", "need space", "back off", etc. in either direction (their message OR yours) auto-applies the matching state. Priority-tier contacts are exempt.
- **Freeform notes carry heavy weight in the prompt.** Write "kid's teacher" or "asked for space after breakup" or "boss's boss" and the AI reads that on every prompt for the pair.

---

## Commands

Open the cheat console with `Ctrl+Shift+C`, type a command, press Enter.

### Phone Calls & Texts
| Command | What it does |
|---|---|
| `llama.call` | Incoming phone call from a random relationship sim |
| `llama.text` | Text message from a random relationship sim |
| `llama.sendtext Bella Goth hey!` | Text a specific sim — they'll reply in character |
| `llama.sendcall Bella Goth I have news` | Call a specific sim about a topic |
| `llama.reply <message>` | Continue any conversation — routes to the specific `(household sim, contact)` pair you last surfaced a dialog for |
| `llama.contact First Last muted\|paused\|priority\|clear\|note <text>` | Set per-contact preferences (scoped to the active household sim) |

Calls and texts show as in-game phone dialogs with the sim's portrait. **Click Reply on the popup** to type a response directly in a text-input dialog. Realistic reply delays make texts feel asynchronous; calls fire instantly. Weather, holidays, past shared events, in-person recency, and your contact preferences all shape the voice.

### Phone UI (Phone → Social)
The phone itself has three Llamafone items under the Social tile — no cheat console needed for everyday use:

| Tile | What it does |
|---|---|
| **Call Someone** | Sim picker → recipient → topic input → Llamafone crafts and delivers the call |
| **Send Text** | Same flow, but for texts. **Picker allows multi-select — 2 to 4 sims starts a group text.** |
| **Llamafone Settings** | In-game settings panel with toggles for auto-events, reply delays, ghost contacts, group text size, plus a **Manage contacts** entry for per-relationship prefs |

### Storytelling
| Command | What it does |
|---|---|
| `llama.dialogue` | 4-5 in-character lines for your active sim |
| `llama.dialogue_situation just got promoted` | Dialogue for a specific situation |
| `llama.backstory` | Backstory + personality reveal for the active sim |
| `llama.story` | 2-3 paragraph narrative update for the household |
| `llama.storyline` | Full 3-act storyline with gameplay goals |
| `llama.storyline_theme romance` | Storyline with a specific theme (rivalry, mystery, rags to riches, family drama, haunting…) |
| `llama.drama` | Relationship drama arc between two household members |

### Events & Challenges
| Command | What it does |
|---|---|
| `llama.event` | Surprise random event |
| `llama.goals` | 5 session goals (mixed easy/hard) |
| `llama.challenge` / `llama.challenge_easy` / `llama.challenge_hard` | Gameplay challenge at your chosen difficulty |

### System / Diagnostics
| Command | What it does |
|---|---|
| `llama.status` | Show config, auto-event status, and all commands |
| `llama.chat <message>` | Freeform — ask anything about your game |
| `llama.journal` / `llama.journal_sim First Last` / `llama.journal_clear` | View or clear journal entries |
| `llama.auto_events on\|off` / `llama.fire_auto <type>` | Toggle or fire auto-events |
| `llama.reload` | Reload config file after editing `llamafone.cfg` by hand |
| `llama.testconnection` | Provider-aware diagnostic — for Ollama users, walks through reachability, installed models, and end-to-end generation |
| `llama.testprovider` / `llama.testweather` / `llama.scanworlds` | Provider ping / weather-service dump / household world audit |
| `llama.debug` / `llama.debugsim` / `llama.dumpphone` / `llama.dumpprompt` | Internal state dumps for diagnostics |

---

## Auto-Events

Auto-events fire randomly while you play without you having to ask. They use **real-world time** — game speed doesn't affect them.

**How it works:**
- Every N real-world minutes, the mod rolls a random check
- If the roll succeeds (based on your configured chance %), it generates a random piece of content
- Content shows as a notification popup, or as a phone dialog for calls/texts
- It only fires when you're in an active household (not during loading screens, CAS, or build mode)
- Silent failures — if there's a network error, nothing happens, no interruption
- **Contact preferences apply.** Muted contacts are skipped; paused contacts fire at 20% rate; priority contacts fire at 200%

**Turn on in `llamafone.cfg`:**
```ini
auto_events_enabled = true
auto_event_interval_minutes = 20      ; check every 20 real minutes
auto_event_chance = 40                ; 40% chance each check fires something
auto_event_types = call, text         ; phone-first default -- random calls and texts
auto_event_weights = call:50, text:50 ; 50/50 mix
```

Available auto-event types: `call`, `text`, `event`, `goals`, `story`, `drama`. The default is **phone-only** (`call, text`) to match the mod's focus — add the others to your `auto_event_types` if you want the full mix.

With the defaults (20 min interval, 40% chance), you get something roughly every 50 real minutes on average.

**Or toggle mid-session** via the in-game Settings panel (Phone → Social → Llamafone Settings) or via cheats:
```
llama.auto_events on
llama.auto_events off
```

---

## Configuration

Two paths to change settings:

1. **In-game Settings panel** — Phone → Social → Llamafone Settings. Toggles + numeric inputs for runtime-tunable values. Writes back to `llamafone.cfg`, preserving your comments, and applies immediately.
2. **Edit `llamafone.cfg` by hand** — then run `llama.reload` to pick up changes without restarting.

### Key settings

| Setting | Default | Editable in UI | Description |
|---|---|---|---|
| `provider` | `claude` | ❌ | `claude`, `openai`, `gemini`, or `ollama` |
| `api_key` | *(required for cloud providers)* | ❌ | Blank for Ollama |
| `default_model` | `claude-haiku-4-5` | ❌ | Used for briefings and storyline generation |
| `fast_model` | `claude-haiku-4-5` | ❌ | Used for calls, texts, and reply generation |
| `ollama_endpoint` | `http://localhost:11434` | ❌ | Only used when provider = `ollama` |
| `max_tokens` | `512` | ❌ | Max length of responses |
| `language` | `English` | ❌ | Language for all generated content |
| `main_sim_name` | *(blank)* | ❌ | Your protagonist's full name. Blank = active sim. |
| `phone_allow_ghosts` | `true` | ✅ | Allow ghost sims to call/text |
| `auto_events_enabled` | `false` | ✅ | Turn on random auto-events |
| `auto_event_interval_minutes` | `20` | ✅ | Real-world minutes between checks |
| `auto_event_chance` | `40` | ✅ | Percent chance each check fires |
| `reply_delay_enabled` | `true` | ✅ | Sims "think" for a few seconds before replying |
| `reply_delay_min_seconds` / `reply_delay_max_seconds` | `15` / `90` | ✅ | Reply delay range |
| `group_text_enabled` | `true` | ✅ | Master toggle for group texts (multi-select in Send Text) |
| `group_text_max_participants` | `4` | ✅ | Max group size (2-8) |
| `group_text_dropoff_enabled` | `true` | ✅ | Gentle "someone got busy" drop-off after round 1 |

Per-save data (journal, milestones, group threads, contact preferences, past events, interactions) lives in `Documents/Electronic Arts/The Sims 4/saves/Llamafone/Slot_NNNNNNNN/`. Multiple saves get their own folders — no cross-contamination.

---

## Cost

You pay your AI provider directly for what the mod uses — no subscription to the mod itself.

| Provider | Model | Typical call cost |
|---|---|---|
| Claude | Haiku 4.5 | ~$0.005 |
| Claude | Sonnet / Opus | ~$0.05 – $0.15 |
| OpenAI | gpt-4o-mini | ~$0.005 |
| OpenAI | gpt-4o | ~$0.03 |
| Gemini | Flash | free tier covers most casual play |
| Ollama | any local model | **free** (uses your GPU) |

A typical session with ~30 Haiku or gpt-4o-mini commands lands around **$0.15**. Heavy sessions with long-form storyline generation run **$0.50 – $1.50** on premium models. Gemini's free tier covers most casual play. Ollama is fully free if you have the hardware.

**To minimize cost** on Claude/OpenAI, keep `default_model` and `fast_model` on the cheap tier (Haiku / gpt-4o-mini). Quality dips slightly for long-form stories but stays strong for calls, texts, dialogue, and short narratives — and cost drops ~20×.

---

## Technical Notes

- **Uses curl for API calls** — the game's embedded Python lacks SSL support, so HTTP calls go through `curl` (built into Windows 10+, available on macOS and most Linux distros by default)
- **All API calls run on background threads** to prevent game freezes
- **Notifications use the same pattern as MC Command Center** for compatibility
- **No pip packages required at runtime** — everything uses Python stdlib + game APIs
- **Per-save data** lives under `Documents/Electronic Arts/The Sims 4/saves/Llamafone/Slot_NNNNNNNN/` with atomic writes and RLock protection against concurrent access from background threads

---

## Development

End users don't need any of this — just download the release artifacts and drop them in your Mods folder (see Installation). The build scaffolding here is for contributing changes.

**Build prerequisites:**
- Python 3.12+ for the build script (the host script that drives compilation and packaging)
- Python 3.7 for compiling mod bytecode — Sims 4 loads compiled `.pyc` from Python 3.7 specifically. Download [python-3.7.9-embed-amd64.zip](https://www.python.org/ftp/python/3.7.9/python-3.7.9-embed-amd64.zip) and extract to `tools/python37/`.

**Build:**
```
python build.py            # builds + auto-installs to Sims 4 Mods folder
python build.py --build    # builds only, no install
```

`build.py` does two things: compiles every `.py` in `src/` to Python-3.7 `.pyc` and zips them as `Llamafone.ts4script`, then runs `tools/package_builder.py` to bundle the XML tunings in `package_src/` into `Llamafone.package`. Both artifacts land at the repo root and (without `--build`) get copied into the Sims 4 Mods folder.

**Linux note:** the build script's auto-install step expects a Windows-style Mods folder path. On Linux you can use `--build` to skip install and copy the artifacts to your Proton/Lutris prefix manually. The compiled bytecode itself is platform-agnostic — Python 3.7 `.pyc` runs the same on Windows, macOS, and Linux Proton.

### Source layout

```
src/
  llamafone_loader.py           root-level entry point (game needs this)
  llamafone/
    __init__.py                 mod entry point, startup notification, save-load hooks
    config.py                   reads & writes llamafone.cfg, runtime settings layer
    api_client.py               AI provider HTTP calls (Claude/OpenAI/Gemini/Ollama) via curl
    sim_context.py              reads sim data, protagonist system, relationship network
    save_id.py                  per-save data folder resolution + save-switch hook
    dialogue.py                 dialogue, conversation, backstory generation
    storyteller.py              story updates, storylines, relationship drama
    event_generator.py          random events, challenges, weekly goals
    phone.py                    AI-generated calls, texts, group texts
    phone_ui_injection.py       grafts SuperInteractions onto Sim _phone_affordances
    phone_ui_interactions.py    Phone > Social > Call / Text / Settings handlers + multi-select picker
    auto_events.py              background thread for random auto-events
    events.py                   reads upcoming + ongoing calendar events with focal sims
    past_events.py              logs shared calendar events after they end (per save)
    milestones.py               detects & dedups life events (job, marriage, birth, ...)
    interactions.py             logs in-person interactions via Relationship.add_relationship_bit
    group_texts.py              persistent group thread storage (per save)
    contact_prefs.py            per-pair contact preferences (state + note) + auto-detection
    notifications.py            in-game notification popups (top-right panel)
    commands.py                 all llama.* cheat commands
    journal.py                  persistent cross-session story memory (per save)

package_src/                    XML tunings packed into Llamafone.package
  Llamafone_Call.xml            SuperInteraction for Phone > Social > Call Someone
  Llamafone_Text.xml            SuperInteraction for Phone > Social > Send Text
  Llamafone_Settings.xml        SuperInteraction for Phone > Social > Llamafone Settings

tools/
  package_builder.py            DBPF v2.1 packer (no S4S dependency)
  python37/                     embedded Python 3.7 for compiling .pyc

docs/                           GitHub Pages site (morganparadis.github.io/llamafone)
```
