# Claude AI for The Sims 4

Brings AI-generated dialogue, storylines, random events, phone calls, and challenges to your game using the Claude API. Results show as in-game notification popups and phone dialogs.

---

## Installation

1. You'll need [Python 3.7](https://www.python.org/ftp/python/3.7.9/python-3.7.9-embed-amd64.zip) for compilation. Extract it to `tools/python37/` in this project folder.
2. Run `python build.py` — it compiles the mod to `.pyc` and copies it to your Mods folder automatically.
3. Open `claude_config.cfg` in your Mods folder and replace `YOUR_API_KEY_HERE` with your API key.
   - Get a key at [console.anthropic.com](https://console.anthropic.com/) (free to sign up, pay per use)
4. In The Sims 4: **Game Options > Other > enable Custom Content and Script Mods**, then restart.
5. You'll see a notification popup when the mod loads. Type `claude.status` in the cheat console to see all commands.

---

## How does Claude know about your Sims?

Every time you run a command, the mod reads live data from the game and sends it to Claude as context.

**What it reads:**
| Data | Example |
|---|---|
| Focus sim's name, age, mood | Lily Feng, Young Adult, Confident |
| Traits (up to 6) | Bookworm, Ambitious, Loner |
| Career and aspiration | Doctor, Renaissance Sim |
| Skill levels | Cooking 7, Programming 4, Fitness 2 |
| Household name, members, funds | The Feng Household, §42,800 |
| Current lot | Oakenstead |
| Relationship network | names, relationship types, friendship scores |
| Installed packs | used to keep suggestions relevant to what you own |
| Recent journal history | past events, storylines, and chats from previous sessions |

The journal gives Claude memory across sessions — generated events, stories, and chat responses are saved automatically and included in future prompts so the AI can reference what happened before.

---

## Commands

Open the cheat console with `Ctrl+Shift+C`, type a command, press Enter.

### Dialogue
| Command | What it does |
|---|---|
| `claude.dialogue` | 4-5 in-character lines for your active sim |
| `claude.dialogue_situation just got promoted` | Dialogue for a specific situation |
| `claude.backstory` | A backstory and personality reveal for the active sim |

### Storytelling
| Command | What it does |
|---|---|
| `claude.story` | 2-3 paragraph narrative update for the household |
| `claude.storyline` | Full 3-act storyline with gameplay goals |
| `claude.storyline_theme romance` | Storyline with a specific theme (try: rivalry, mystery, rags to riches, family drama, haunting) |
| `claude.drama` | Relationship drama arc between two household members |

### Events & Challenges
| Command | What it does |
|---|---|
| `claude.event` | A surprise random event to shake up your session |
| `claude.goals` | 5 session goals (mixed easy/hard, with a stretch goal) |
| `claude.challenge` | Medium difficulty gameplay challenge |
| `claude.challenge_easy` | Easy challenge |
| `claude.challenge_hard` | Hard challenge with strict rules |

### Phone Calls & Texts
| Command | What it does |
|---|---|
| `claude.call` | Incoming phone call from a random relationship sim |
| `claude.text` | Text message from a random relationship sim |
| `claude.sendtext Bella Goth hey!` | Text a specific sim — they'll reply in character |
| `claude.sendcall Bella Goth I have news` | Call a specific sim about a topic |
| `claude.reply <message>` | Continue any conversation — they'll respond back |

Calls and texts show as in-game phone dialogs with the sim's portrait. Click **Reply** on the popup to continue the conversation via `claude.reply`. The full conversation history is tracked, so back-and-forth exchanges stay coherent.

Each sim has a unique voice based on their **age, traits, mood, career, and aspiration**. A Goofball Teen texts completely differently from a Snob Elder. Past interactions with that sim are also included, so they'll reference previous conversations naturally.

### General
| Command | What it does |
|---|---|
| `claude.chat <message>` | Freeform — ask anything about your game |
| `claude.journal` | View recent journal entries |
| `claude.journal_sim First Last` | View journal entries for a specific sim |
| `claude.auto_events on` | Turn on random auto-events for this session |
| `claude.auto_events off` | Turn them off |
| `claude.status` | Show config, auto-event status, and all commands |
| `claude.reload` | Reload config file (after editing claude_config.cfg) |
| `claude.debug` | Show game API debug info (for troubleshooting) |

---

## Auto-Events

Auto-events fire randomly while you play without you having to ask. They use **real-world time** — game speed doesn't affect them.

**How it works:**
- Every N real-world minutes, the mod rolls a random check
- If the roll succeeds (based on your configured chance %), it generates a random piece of content
- Content shows as a notification popup, or as a phone dialog for calls/texts
- It only fires when you're in an active household (not during loading screens, CAS, or build mode)
- Silent failures — if there's a network error, nothing happens, no interruption

**Turn on in `claude_config.cfg`:**
```ini
auto_events_enabled = true
auto_event_interval_minutes = 20   ; check every 20 real minutes
auto_event_chance = 40             ; 40% chance each check fires something
auto_event_types = event, goals, call, text   ; what can fire
```

Available auto-event types: `event`, `goals`, `story`, `drama`, `call`, `text`

With the defaults (20 min interval, 40% chance), you get something roughly every 50 real minutes on average.

**Or toggle mid-session** without editing the config:
```
claude.auto_events on
claude.auto_events off
```

---

## Configuration (`claude_config.cfg`)

| Setting | Default | Description |
|---|---|---|
| `api_key` | *(required)* | Your Anthropic API key |
| `default_model` | `claude-opus-4-6` | Model for stories and storylines |
| `fast_model` | `claude-haiku-4-5` | Model for dialogue, events, calls, texts |
| `max_tokens` | `512` | Max length of responses |
| `language` | `English` | Language for all generated content |
| `phone_allow_ghosts` | `true` | Allow ghost sims to call/text. Set `false` to only hear from the living. |
| `auto_events_enabled` | `false` | Turn on random auto-events |
| `auto_event_interval_minutes` | `20` | Real-world minutes between checks |
| `auto_event_chance` | `40` | Percent chance each check fires |
| `auto_event_types` | `event, goals, call, text` | Content types for auto-events |
| `auto_event_weights` | *(blank)* | Weight per type (e.g. `call:40, text:30, event:20, goals:10`). Blank = equal. |

After editing the config, type `claude.reload` in-game to apply changes without restarting.

---

## API Cost

Everything is very cheap. Rough estimates per command:

| Type | Model | Estimated cost |
|---|---|---|
| Dialogue, events, goals, calls, texts | Haiku | ~$0.001 |
| Story, storyline, drama | Opus | ~$0.01-0.02 |
| Chat | Opus | ~$0.005-0.01 |

A heavy play session with 30+ commands + auto-events would cost around $0.20-0.50.

**To reduce cost further**, set `default_model = claude-haiku-4-5` in the config. The quality drops a bit for long-form stories but is still good for events and dialogue.

---

## Technical Notes

- **Requires Python 3.7** for compilation — The Sims 4 only loads compiled `.pyc` bytecode, not `.py` source files
- **Uses curl for API calls** — The game's embedded Python lacks SSL support, so HTTP calls go through `curl` (built into Windows 10+)
- **All API calls run on background threads** to prevent game freezes
- **Notifications use the same pattern as MC Command Center** for compatibility
- **No pip packages required** — everything uses Python stdlib + game APIs

## Development

The source is in `src/claude_ai/`. After making changes, run `python build.py` to compile and reinstall.

```
src/
  claude_ai_loader.py  root-level entry point (game needs this)
  claude_ai/
    __init__.py        mod entry point, startup notification, auto-events
    config.py          reads claude_config.cfg from Mods folder
    api_client.py      Claude API calls via curl subprocess
    sim_context.py     reads sim data, protagonist system, relationship network
    dialogue.py        dialogue, conversation, backstory generation
    storyteller.py     story updates, storylines, relationship drama
    event_generator.py random events, challenges, weekly goals
    phone.py           AI-generated calls and texts from relationship sims
    auto_events.py     background thread for random auto-events
    notifications.py   in-game notification popups (top-right panel)
    commands.py        all claude.* cheat commands
    journal.py         persistent cross-session story memory
```
