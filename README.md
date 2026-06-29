# Llamafone

A phone-first AI mod for The Sims 4. Random sims call and text you in character — voices shaped by their traits, mood, relationships, and what's actually happening in your save. Bring your own AI — Claude, OpenAI, Gemini, or a local Ollama model — and pick up calls and texts that read like they were written for the people in front of you.

Story updates, random events, and 3-act storylines are in the box too, for when you want them — but the headline is the phone.

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
| `ollama` | **No** — runs locally | whatever you've `ollama pull`-ed (`llama3.1`, `mistral`, `qwen2.5`) | [ollama.com](https://ollama.com) |

For Ollama, `ollama_endpoint` in the config points at your local server (default `http://localhost:11434`). No key, no cost, no internet required after the model download.

---

## How does Llamafone know about your Sims?

Every time you run a command, the mod reads live data from the game and sends it to the AI as context.

**What it reads:**
| Data | Example |
|---|---|
| Focus sim's name, age, mood | Lily Feng, Young Adult, Confident |
| Traits (up to 6) | Bookworm, Ambitious, Loner |
| Career and aspiration | Doctor, Renaissance Sim |
| Skill levels | Cooking 7, Programming 4, Fitness 2 |
| Household name, members, funds | The Feng Household, §42,800 |
| Current lot | Oakenstead |
| Current world and season | Oasis Springs, Summer |
| Relationship network | names, relationship types, friendship scores, family roles |
| Upcoming calendar events | funerals, weddings, parties, holidays — including who they're for |
| Recent life events | new jobs, breakups, babies, ageups — surfaced once per contact |
| Installed packs | used to keep suggestions relevant to what you own |
| Recent journal history | past events, storylines, and chats from previous sessions |

The journal gives the AI memory across sessions — generated events, stories, and chat responses are saved automatically and included in future prompts so the model can reference what happened before.

**Calendar awareness** — calls and texts reference real upcoming entries on your sim's calendar that BOTH sims are attending. "See you at the funeral later" / "can't wait for your wedding" instead of inventing meetups. The mod also reads the actual focal sims off each event so the AI knows the funeral is *in memory of Sawyer*, the wedding is *for Alex and Bailey*, the birthday is *for Apollo* — no more guessing whose event it is.

**Sims-time aware** — the prompt explains that one in-game week is one season, so "Christmas Vacation in 2 weeks" gets framed as "two seasons away" rather than a casual fortnight.

**Family-role references** — when one family member mentions another, they use the relationship from the recipient's perspective ("your dad", "your sister") instead of the first name.

---

## Commands

Open the cheat console with `Ctrl+Shift+C`, type a command, press Enter.

### Phone Calls & Texts (the main event)
| Command | What it does |
|---|---|
| `llama.call` | Incoming phone call from a random relationship sim |
| `llama.text` | Text message from a random relationship sim |
| `llama.sendtext Bella Goth hey!` | Text a specific sim — they'll reply in character |
| `llama.sendcall Bella Goth I have news` | Call a specific sim about a topic |
| `llama.reply <message>` | Continue any conversation — they'll respond back |

Calls and texts show as in-game phone dialogs with the sim's portrait. **Click Reply on the popup** to type a response directly in a text-input dialog (no need to drop to the cheat console). The full conversation history is tracked, so back-and-forth exchanges stay coherent.

Each sim has a unique voice based on their **age, traits, mood, career, and aspiration**. A Goofball Teen texts completely differently from a Snob Elder. Past interactions with that sim are also included, so they'll reference previous conversations naturally.

**Sims also remember big life events.** When something significant happens in your game — a sim quits their job, gets promoted, has a baby, gets married, ages up — the mod picks it up automatically and references it in calls and texts. Each contact only brings it up once, so the same sim won't keep asking how leaving your job is going across five different calls.

**Realistic reply delays** — when you text a sim, they "think" for a few seconds before responding instead of replying instantly. Close friends reply fast; lazy or hostile sims drag. Calls reply instantly. Toggle from the in-game Settings panel, or set `reply_delay_enabled = false` in the config.

**Weather-aware.** Every call and text knows what the weather is actually doing where your sim is — light rain, heavy snow, thunderstorm, heatwave. Dramatic weather is fair game to come up in conversation; routine weather stays out of the way. Off-world callers know their own world's climate too (Sulani is tropical, Mt. Komorebi is alpine, Oasis Springs is desert — none get snow misapplied just because it's globally Winter).

### Phone UI (Phone > Social)

The phone itself has three Llamafone items under the Social tile — no cheat console needed for everyday use:

| Tile | What it does |
|---|---|
| **Call Someone** | Opens a sim picker scoped to your contacts → pick a recipient → type a topic → Llamafone crafts and delivers the call |
| **Send Text** | Same flow as Call, but for texts |
| **Settings** | Opens an in-game settings panel with toggles for auto-events, reply delays, ghost contacts, etc. Picking a row flips a bool or opens a numeric input. Changes save instantly to `llamafone.cfg` (preserving your comments) and apply without reloading the save. |

The cheat commands (`llama.call`, `llama.sendtext`, etc.) still work the same way alongside the UI.

### …and the rest

Llamafone is a phone mod, but the AI plumbing is general-purpose. These commands ship in the box for when you want them — they're not the focus.

#### Dialogue
| Command | What it does |
|---|---|
| `llama.dialogue` | 4-5 in-character lines for your active sim |
| `llama.dialogue_situation just got promoted` | Dialogue for a specific situation |
| `llama.backstory` | A backstory and personality reveal for the active sim |

#### Storytelling
| Command | What it does |
|---|---|
| `llama.story` | 2-3 paragraph narrative update for the household |
| `llama.storyline` | Full 3-act storyline with gameplay goals |
| `llama.storyline_theme romance` | Storyline with a specific theme (try: rivalry, mystery, rags to riches, family drama, haunting) |
| `llama.drama` | Relationship drama arc between two household members |

#### Events & Challenges
| Command | What it does |
|---|---|
| `llama.event` | A surprise random event to shake up your session |
| `llama.goals` | 5 session goals (mixed easy/hard, with a stretch goal) |
| `llama.challenge` | Medium difficulty gameplay challenge |
| `llama.challenge_easy` | Easy challenge |
| `llama.challenge_hard` | Hard challenge with strict rules |

### General
| Command | What it does |
|---|---|
| `llama.chat <message>` | Freeform — ask anything about your game |
| `llama.journal` | View recent journal entries |
| `llama.journal_sim First Last` | View journal entries for a specific sim |
| `llama.journal_clear` | Clear the journal (no undo) |
| `llama.auto_events on` / `off` | Toggle random auto-events for this session |
| `llama.fire_auto <type>` | Fire one auto-event immediately for testing |
| `llama.status` | Show config, auto-event status, and all commands |
| `llama.reload` | Reload config file (after editing llamafone.cfg by hand) |
| `llama.debug` / `llama.debugsim` | Game API debug info |
| `llama.dumpphone` / `llama.dumpprompt` | Dump the most recent prompt or phone-affordance state for inspection |
| `llama.testweather` | Dump the WeatherService state for diagnosing the live-weather read |
| `llama.scanworlds` | Audit every household's home-region resolution (paste back if any are unresolved) |

---

## Auto-Events

Auto-events fire randomly while you play without you having to ask. They use **real-world time** — game speed doesn't affect them.

**How it works:**
- Every N real-world minutes, the mod rolls a random check
- If the roll succeeds (based on your configured chance %), it generates a random piece of content
- Content shows as a notification popup, or as a phone dialog for calls/texts
- It only fires when you're in an active household (not during loading screens, CAS, or build mode)
- Silent failures — if there's a network error, nothing happens, no interruption

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

**Or toggle mid-session** via the in-game Settings panel (Phone > Social > Settings) or via cheats:
```
llama.auto_events on
llama.auto_events off
```

---

## Configuration

Two paths to change settings:

1. **In-game Settings panel** — Phone > Social > Settings. Toggles + numeric inputs for runtime-tunable values. Writes back to `llamafone.cfg`, preserving your comments, and applies immediately.
2. **Edit `llamafone.cfg` by hand** — then run `llama.reload` to pick up changes without restarting.

### All settings

| Setting | Default | Editable in UI | Description |
|---|---|---|---|
| `api_key` | *(required)* | ❌ | Your Anthropic API key |
| `default_model` | `claude-haiku-4-5` | ❌ | Model for stories and storylines |
| `fast_model` | `claude-haiku-4-5` | ❌ | Model for dialogue, events, calls, texts |
| `max_tokens` | `512` | ❌ | Max length of responses |
| `language` | `English` | ❌ | Language for all generated content |
| `main_sim_name` | *(blank)* | ❌ | Your protagonist's full name. Falls back to currently selected sim if blank. |
| `phone_allow_ghosts` | `true` | ✅ | Allow ghost sims to call/text |
| `auto_events_enabled` | `false` | ✅ | Turn on random auto-events |
| `auto_event_interval_minutes` | `20` | ✅ | Real-world minutes between checks |
| `auto_event_chance` | `40` | ✅ | Percent chance each check fires |
| `auto_event_types` | `call, text` | ❌ | Content types for auto-events. Add `event, goals, story, drama` for the full mix. |
| `auto_event_weights` | `call:50, text:50` | ❌ | Weight per type (e.g. `call:40, text:30, event:20, goals:10`). |
| `reply_delay_enabled` | `true` | ✅ | Sims "think" for a few seconds before replying to texts |
| `reply_delay_min_seconds` | `15` | ✅ | Floor of the reply delay range |
| `reply_delay_max_seconds` | `90` | ✅ | Ceiling of the reply delay range |

---

## API Cost

You pay Anthropic per-token for what the mod actually uses. Rough estimates per command, assuming typical sim context size:

| Type | Model | Estimated cost |
|---|---|---|
| Dialogue, events, goals, calls, texts | Haiku | ~$0.005 |
| Story, storyline, drama | Opus | ~$0.05–0.15 |
| Chat | Opus | ~$0.02–0.05 |

A typical session with ~30 Haiku commands is about **$0.15**. A heavy session that also includes several Opus story/storyline commands typically lands **between $0.50 and $1.50**.

**To minimize cost**, set `default_model = claude-haiku-4-5` in the config so every command uses Haiku. The quality drops a bit for long-form stories but stays good for events, dialogue, and short narratives — and your bill drops by roughly 20×.

---

## Technical Notes

- **Uses curl for API calls** — The game's embedded Python lacks SSL support, so HTTP calls go through `curl` (built into Windows 10+, available on macOS and most Linux distros by default)
- **All API calls run on background threads** to prevent game freezes
- **Notifications use the same pattern as MC Command Center** for compatibility
- **No pip packages required at runtime** — everything uses Python stdlib + game APIs

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

**Linux note:** the build script's auto-install step expects a Windows-style Mods folder path. On Linux you can use `--build` to skip install and copy the artifacts to your Proton/Lutris prefix manually:

```
~/.steam/steam/steamapps/compatdata/<id>/pfx/drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods/
```

The compiled bytecode itself is platform-agnostic — Python 3.7 `.pyc` runs the same on Windows, macOS, and Linux Proton.

```
src/
  llamafone_loader.py        root-level entry point (game needs this)
  llamafone/
    __init__.py              mod entry point, startup notification, auto-events
    config.py                reads & writes llamafone.cfg, runtime settings layer
    api_client.py            AI provider HTTP calls (Claude/OpenAI/Gemini/Ollama) via curl
    sim_context.py           reads sim data, protagonist system, relationship network
    dialogue.py              dialogue, conversation, backstory generation
    storyteller.py           story updates, storylines, relationship drama
    event_generator.py       random events, challenges, weekly goals
    phone.py                 AI-generated calls and texts from relationship sims
    phone_ui_injection.py    grafts our SuperInteractions onto Sim _phone_affordances
    phone_ui_interactions.py Phone > Social > Call / Text / Settings handlers
    auto_events.py           background thread for random auto-events
    events.py                reads upcoming calendar events + honoree/host data
    milestones.py            detects & dedups life events (job, marriage, birth, ...)
    notifications.py         in-game notification popups (top-right panel)
    commands.py              all llama.* cheat commands
    journal.py               persistent cross-session story memory

package_src/                 XML tunings packed into Llamafone.package
  Llamafone_Call.xml            SuperInteraction for Phone > Social > Call Someone
  Llamafone_Text.xml            SuperInteraction for Phone > Social > Send Text
  Llamafone_Settings.xml        SuperInteraction for Phone > Social > Settings

tools/
  package_builder.py         DBPF v2.1 packer (no S4S dependency)
  python37/                  embedded Python 3.7 for compiling .pyc
```
