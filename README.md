# Llamafone

A phone-first AI mod for The Sims 4. Random sims call and text you in character — voices shaped by their traits, mood, relationships, and what's actually happening in your save. Bring your own AI — Claude, OpenAI, Gemini, or a local model through Ollama or LM Studio — and pick up calls and texts that read like they were written for the people in front of you.

**v3.8.2:** trip memory (sims remember vacations and who went, and everyone else hears about it the way news travels), LM Studio support (free, local, no key), and bug fixes, including Claude Haiku 5.5 support.

**v3.8 highlights:** **Llamagram**, social media for your sims. Post to friends or the whole world, and real sims from your save comment, like, and follow. Your friends post about their own lives, big accounts draw fans, posts carry into calls and texts ("I saw your post..."), and with Get Famous, posts that truly go viral raise your fame. Plus a round of birth-announcement fixes.

**v3.7 highlights:** birth announcements (a family member or close friend has a baby and texts you within seconds), pregnancy and birth news that spreads realistically (parents hear in hours, casual friends in days, nobody before the mom-to-be knows), and Autosave / in-game Load menu support for per-save data.

**v3.6 highlights:** save-level world notes (challenge rulesets, custom flavor), per-sim character bios (backstory / private context injected into every prompt), service-NPC roles (butlers / maids / babysitters / nannies / gardeners / repair techs write in their proper register), messages that actually move friendship and romance, in-game time-of-day in every prompt, Llamadate origin persists across sessions, plus OpenRouter as a fifth AI provider. Plus everything from v3.5 — Llamadate dating layer with Hinge-style profiles and reply-gated matches, standalone phone app, breakup context — and v3.4 — group texts, per-relationship contact preferences ("asked for space" auto-detected), weather + holiday awareness, past-event memory. Story updates, random events, and 3-act storylines are in the box too, for when you want them.

**Site:** [morganparadis.github.io/llamafone](https://morganparadis.github.io/llamafone/)

---

## Installation

1. **Download the latest release** from [Releases](https://github.com/morganparadis/llamafone/releases) (also on [CurseForge](https://www.curseforge.com/sims4/mods/llamafone) and [Nexus Mods](https://www.nexusmods.com/thesims4/mods/7232)) — grab `Llamafone.ts4script`, `Llamafone.package`, and `llamafone.cfg`.
2. **Drop all three into your Mods folder:**
   - **Windows:** `Documents\Electronic Arts\The Sims 4\Mods\`
   - **macOS:** `~/Documents/Electronic Arts/The Sims 4/Mods/`
   - **Linux (Steam Proton):** `~/.steam/steam/steamapps/compatdata/<sims-4-app-id>/pfx/drive_c/users/steamuser/Documents/Electronic Arts/The Sims 4/Mods/`
3. **Open `llamafone.cfg`** in any text editor, pick your `provider`, and paste your API key in `api_key`.
4. **In The Sims 4:** **Game Options > Other > enable Custom Content and Script Mods**, then restart the game.
5. You'll see a notification popup when the mod loads. Type `llama.status` in the cheat console to confirm setup and see all commands. Or tap your sim's phone → the Llamafone tile.

If you forget the cfg, the mod writes a default one to your Mods folder on first launch with `api_key = YOUR_API_KEY_HERE`. You'll get a "not configured yet" notification directing you to edit it. This works wherever your Sims 4 folder lives (OneDrive, another drive, non-English folder names). Type `llama.status` to see the provider, the cfg file, and the Mods folder Llamafone is using.

No Python install required for end users — the release ships compiled `.pyc` bytecode.

---

## Choose your AI provider

`provider` in `llamafone.cfg` picks where the messages come from:

| Provider | API key needed | Model examples | Where to get a key |
|---|---|---|---|
| `claude` | Yes | `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-8` | [console.anthropic.com](https://console.anthropic.com/) |
| `openai` | Yes | `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `gemini` | Yes | `gemini-1.5-pro`, `gemini-1.5-flash` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `openrouter` | Yes | `anthropic/claude-haiku-4-5`, `openai/gpt-4o-mini`, `meta-llama/llama-3.1-8b-instruct`, `deepseek/deepseek-chat` — [full catalog](https://openrouter.ai/models) | [openrouter.ai/keys](https://openrouter.ai/keys) |
| `ollama` (techy) | **No** — runs locally | whatever you've `ollama pull`-ed (`llama3.2:3b` recommended for most hardware) | [ollama.com](https://ollama.com) |
| `lmstudio` (techy) | **No** — runs locally | whatever model you've loaded in LM Studio (e.g. `llama-3.2-3b-instruct`) | [lmstudio.ai](https://lmstudio.ai) |

For Ollama, `ollama_endpoint` in the config points at your local server (default `http://localhost:11434`). No key, no cost, no internet required after the model download. It's the most technical option — you install Ollama, keep the tray icon running, and `ollama pull` a model before the mod can use it. Run `llama.testconnection` in-game to verify setup end-to-end (checks reachability, lists installed models, verifies your configured models match, runs a tiny generation).

For LM Studio, set `provider = lmstudio`. In LM Studio, load a model with **Context Length 16384** (12288 at the very least), then switch the server on in the Developer tab. `lmstudio_endpoint` defaults to `http://localhost:1234`; the address LM Studio shows (ending in `/v1`) works too. Put the model's name in `default_model` and `fast_model`; `llama.testconnection` lists the names LM Studio offers.

**Context size:** a call or text prompt is about 8,000 tokens (relationships, history, life events, the calendar), plus room for the reply. Give local models a context length of **16k** (12k at the very least): in LM Studio, set it when loading the model; in Ollama, set **Context length** in its settings (its default is too small, and it quietly cuts long prompts). With less, replies fail or the model loses part of what it was told.

Local models get up to 5 minutes per reply (cloud providers: 60 seconds). Without a supported graphics card, a local model can take minutes per text; a small model (around 3B) is the most practical.

**A note on local models:** they're free, but slower and less capable than the cloud options. Each reply takes anywhere from about 15 seconds with a decent graphics card to a minute or more without one, where cloud models answer in a few seconds. Small models also sometimes mix up details, like when an event is or what to call a family member. The cloud models follow the mod's instructions much more closely.

For OpenRouter, model names use the `vendor/model` form so `default_model = anthropic/claude-haiku-4-5` gets you Claude via OpenRouter's proxy, `default_model = openai/gpt-4o-mini` gets you GPT, etc. One key covers everything — useful if you want to try several models without juggling separate accounts.

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

## Llamagram (v3.8)

Social media for your sims. Everyone who comments, replies, or posts is a real sim from your save.

- **Phone → Llamafone → Llamagram Post** — write a post as any household sim, then choose **Friends** (people your sim knows) or **Public** (anyone in the world). Comments trickle in over the next little while from family, friends, exes, and — on public posts — strangers. Sometimes nobody comments. Comments arrive a few at a time, batched into one notification per batch.
- **Phone → Llamafone → Llamagram Notifications** — each household sim's own inbox: comments on their posts, replies to their comments, and friends' posts. Shows their follower count and their latest post's views, likes, and comments. Open a comment and reply; that sim answers back.
- **Friends post on their own** through the auto-event timer (type `post`, injected automatically — no cfg edit needed). A friend's post pops up with **Comment** / **Scroll past**.
- **The AI judges reach** from what the post actually says: `good`, `flat`, `viral`, or `backlash`. Viral is rare, and a celebrity's casual "what are you all up to" is good at most. Very rarely the AI calls a post `huge` or a `scandal` — a **breakout** that reaches hundreds of thousands to millions of views on any account.
- **Followers** grow or drop with how posts land, drift over in-game time, and never sit on a round number. Get Famous celebrities start at a floor for their star rank (2K / 25K / 250K / 1.5M / 10M). Views, likes, and comment totals are audience numbers that grow over in-game hours; the comments you read are real sims (plus display-only fan comments from made-up usernames on big accounts).
- **Fame (Get Famous)** is hard to earn: only a viral post (at most once per in-game day), a breakout, or passing a big follower milestone raises it.
- **Posts carry into calls and texts.** The "Recent social media" block tells a contact which of your recent posts they saw and what the two of you commented.
- **Relationships:** comment moods nudge friendship / romance only between sims who already know each other. Strangers' comments never change relationships and never add anyone to the relationship panel. No romance changes across the teen / adult line.
- **Controls:** Llamafone Settings has toggles for Llamagram itself, friends'-post pop-ups, and comment / like pop-ups (off = quiet, still in the inbox). Muted contacts never post to you or comment on your posts.
- **Durable:** pending comments are saved to `Feed.json` (per save, hand-editable) and delivered quietly after a quit, with one "while you were away" notification.

## Trip memory (v3.8.2)

When your household goes on a vacation or getaway, Llamafone records where, when, and who came along (it reads the game's own travel group; guests staying over at your house don't count).

- **Sims on the trip** talk about being away together while it's happening, and remember it together afterward ("Apollo and Francesca went to Gibbi Point together… got back yesterday").
- **Everyone else** hears about it the way news travels (see below): parents within a few hours, close friends in a couple of days, acquaintances only if they're told.
- **People who join mid-trip** are added; restarts and loading screens don't end a trip early. A trip ends once the household is back home.
- Trips stay in conversations for 7 in-game days after you get home, and are kept in `Trips.json` in the save's Llamafone folder.
- `llama.trips` lists every recorded trip.

## Birth announcements & news that spreads (v3.7)

- **Birth announcements.** When a sim who's family or a close friend of your household has a baby, the new parent (or their partner) texts your sim within seconds — baby's name included, tone shaped by the circumstances (married or not, money, existing kids, traits). Works for game births and MCCC "complete pregnancy". Off switch: `birth_announcements = false`.
  - The baby's other parent always gets the announcement, even on bad terms. Anyone on the lot when the baby arrived is treated as already knowing.
  - Born in the household you're playing? Switch to the father's or grandparents' household and the announcement arrives on the next watcher pass — unless the two households already talked through Llamafone since the birth.
  - One announcement per household per birth, across restarts and rolled-back sessions. Clearing a pregnancy with no baby isn't a birth.
- **News-spread model.** Contacts "hear" about a pregnancy or birth after a delay set by closeness: parents and the baby's father within hours, siblings in half a day, grandparents in a day, aunts / uncles / in-laws in a day and a half, close friends in two days, casual friends in four, acquaintances only if told. A visibly showing pregnancy is public.
- **Pregnancy visibility ladder:** hidden (pre-test — nobody knows, not even her), confirmed (test taken), visible (showing).
- **Calendar awareness:** events the caller isn't invited to are listed as exactly that, so nobody asks what to bring to a party they weren't invited to.

### Save identity (v3.7)

Per-save data lives in `saves/Llamafone/Slot_NNNNNNNN/`. Loading the Autosave or switching saves from the in-game Load menu now finds the right folder using the slot the game records inside every save. A save last written by an older game patch has no record yet; Llamafone stays off for it (and says so) until you save once. `llama.saveinfo` shows what was resolved and how.

## Save notes, sim bios, and structural context (v3.6)

Two new player-authored context surfaces plus one automatic one, all persistent per save.

- **Save notes** — Phone → Llamafone → Settings → Save notes. One free-form text field per save, prepended to every AI prompt as `WORLD CONTEXT`. Use it for challenge rulesets ("100 baby challenge — Cara is trying to keep the count above the previous generation"), custom setting overrides ("historical challenge — all messages are letters delivered by mail"), long-running narrative context, or house rules the AI should treat as universally binding.
- **Sim bios** — Phone → Llamafone → Settings → Sim bios. Per-sim character notes — backstory, motivations, secrets, quirks — injected into that sim's descriptor block anywhere they appear in a prompt. Distinct from the Llamadate bio (dating-facing) and the per-relationship contact note (scoped to one pair). The sim picker sorts sims-with-bios to the top and marks them `[bio]` so you can see at a glance what's set.
- **Relationship origin persistence** — when two sims connect via Llamadate (either directly or through the mutual-friend intro flow), that origin fact is now stored on both sides of the pair and referenced in every future prompt between them. Text a match three in-game weeks later and the AI still knows how you met. Storage sits alongside contact preferences in a directional per-pair schema ready for future asymmetric events (who kissed who, who proposed, etc.).

## Service NPC roles (v3.6)

Hired service NPCs — butlers, maids, babysitters, nannies, gardeners, repair techs, pizza delivery, mail carriers, personal trainers, and more — now write in their proper professional register instead of sounding like generic townies.

- **Layered detection** across the game's several service-NPC systems: hire records, active zone situations, direct household attributes, career track, marker traits, sim name pattern. If any layer confirms this sim is *your household's* hire, they get the "the household's butler" framing; if they're a service NPC by career but not confirmed as yours, the AI gets a softer "professional butler by trade" hint.
- **Role-specific voice guidance** appended to their descriptor: butlers are professional and deferential, maids task-focused (schedules, supplies, specific messes), nannies warm and family-embedded (know the kids by name), babysitters kids-first and occasional, gardeners talk plants and season, repair techs quote parts and timing.
- **"On shift" phrasing rewritten** for confirmed hires. When Sims 4 reports your butler as "at work right now," the prompt now says "currently on shift as the household's butler" instead — so the AI doesn't read it as two separate contradictory facts and break frame.

## Messages move friendship and romance (v3.6)

Emotionally-charged phone messages now nudge the actual Sims 4 friendship and romance tracks — bidirectional, hard-capped, tunable.

- **Warm exchanges** lift friendship. **Flirty** ones bump romance. **Hostile** or **rejecting** messages drop the relevant track. Neutral / low-stakes messages don't move anything.
- **Bidirectional:** both sides of the pair get the same delta, so relationships don't drift asymmetrically over time.
- **Hard cap per message** (default ±3, tunable via `message_relationship_max_delta` in `llamafone.cfg` or under Settings). Well below Sims 4's own casual-social interaction deltas (3-8), so a single text nudges without reshaping.
- **Default on**, toggleable per save via `message_relationship_impact_enabled` config knob or the Settings menu.

Fires whenever the AI emits a `MOOD:` tag in its response — same signal used to apply moodlets, so mood and relationship impact stay in sync.

## In-game time-of-day in every prompt (v3.6)

Every call and text prompt now carries the current in-game clock — e.g. `[CURRENT IN-GAME TIME: 7:15 PM (evening)]`. Same season tag as before, plus this. AI replies won't say "dinner ready by six" when it's already 7 PM, and future-tense references (tonight, tomorrow, next week) land in the right window.

---

## Llamadate (v3.5)

An opt-in dating layer, per played sim. Set your bio, browse Hinge-style profiles of eligible strangers in your world, and either send an intro yourself or wait for a match to text you first.

- **Phone → Llamafone → Llamadate** — pick a played sim, then Set Bio, Browse Profiles, or Send Intro. Each household sim opts in independently.
- **Opt-in state is per-sim, per-save.** Turn it on / off individually under **Llamafone Settings → Dating**. Also configurable there: gender preference, age preference, and cold-outreach frequency (Off / Rarely / Sometimes / Often).
- **Bio-only privacy.** If a sim has written a Llamadate bio, matches see *only* that bio — not their career, engagement, aspirations, kids, or traits. Skip the bio and matches see the usual sim context.
- **Reply-gated relationships.** Replying to a match's intro (whether inbound cold outreach or your outbound intro) adds them to your contacts. A polite decline classifier means uninterested replies don't force the relationship.
- **Mutual-friend intros when they exist.** When a real shared friend (friendship ≥ 20 on both sides) exists between sender and recipient, the outreach frames as a warm friend-of-a-friend intro without ever mentioning Llamadate. When there's no mutual, it frames as an explicit dating-app match with an invented plausible detail tied to the sender's traits.
- **Committed sims filtered on both sides.** Married / engaged sims don't appear as candidates and don't send outreach. Age filter is symmetric — teens only match teens, adults default to within one age tier.
- **Sims you already know are filtered out.** Exes, old friends, and coworkers don't appear in the Llamadate pool — it's for meeting new people.
- **Same sim can't cold-outreach you twice.** Once a stranger has reached out, they're permanently excluded from your future pool for that save.

Config: `dating_cold_outreach_weight` in `llamafone.cfg` (default `20`) controls global frequency of dating cold outreach in the auto-events pool. Set to `0` to disable inbound outreach entirely; outbound intros still work.

---

## Group texts (v3.4)

Send one message to 2–4 sims at once. Each recipient replies in their own voice, staggered like real texts arriving over a few minutes. Every reply sees what the others just said, so responses stay distinct instead of a chorus of "same".

- **Phone → Llamafone → Send Text** — the picker now allows multi-select. Pick 1 sim for a normal text, 2–4 for a group thread.
- Dialog titles and reply prompts list the full roster: *"Group text with Sarah, Bob, Alice, and Kate"*.
- Reply button fans out to the whole group — every active participant may respond.
- A one-time briefing call at group creation (uses the default model) synthesizes each participant's voice and cross-relationships, then gets cached for every subsequent reply. Per-turn cost stays sane.
- Group turns land in 1:1 prompts between shared participants as a `SHARED GROUP TEXT` excerpt block. When Alice later texts Sarah 1:1, the AI knows they were both just in a group with Bob and Kate.
- Restarting a group with the same people resumes the existing thread (cached briefing intact) instead of spawning a duplicate.

Configurable via **Phone → Llamafone → Settings**: `group_text_max_participants` (default 4, hard-cap 8), `group_text_enabled` (master toggle), `group_text_dropoff_enabled` (gentle "someone got busy" drop-off in later rounds, default on).

---

## Per-relationship contact preferences (v3.4)

Mute, "asked for space" (paused), or favorite (priority), plus a freeform note field — all scoped to a specific (household sim, contact) pair. Alice muting her ex doesn't affect Bob's phone activity with the same sim.

- **Phone → Llamafone → Settings → Manage contacts** — pick a sim, pick an action. Contacts with an existing state or note surface first, tagged in brackets ([paused], [muted], note).
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
| `llama.textfrom First Last` / `llama.callfrom First Last` | Incoming text / call from a specific sim |
| `llama.contact First Last muted\|paused\|priority\|clear\|note <text>` | Set per-contact preferences (scoped to the active household sim) |

### Llamagram
| Command | What it does |
|---|---|
| `llama.post [public] text` | Post as the active sim (friends-only unless the first word is `public`) |
| `llama.npcpost [First Last]` | A friend posts now (random friend if no name) |
| `llama.inbox` | Open Llamagram Notifications |
| `llama.feed` | Follower counts and recent posts with stats |

Calls and texts show as in-game phone dialogs with the sim's portrait. **Click Reply on the popup** to type a response directly in a text-input dialog. Realistic reply delays make texts feel asynchronous; calls fire instantly. Weather, holidays, past shared events, in-person recency, and your contact preferences all shape the voice.

### Phone UI (Phone → Llamafone)
Llamafone has its own home-screen tile on the phone — no more digging through Social. Tap it and you get:

| Item | What it does |
|---|---|
| **Call Someone** | Sim picker → recipient → topic input → Llamafone crafts and delivers the call |
| **Send Text** | Same flow, but for texts. **Picker allows multi-select — 2 to 4 sims starts a group text.** |
| **Llamadate** | Opt-in dating layer (see below). Set your bio, browse profiles, send intros. |
| **Llamagram Post** | Write a post as this sim, to Friends or Public (see Llamagram above) |
| **Llamagram Notifications** | This sim's inbox: comments, replies, friends' posts, followers, latest post stats |
| **Llamafone Settings** | In-game settings panel with toggles for auto-events, reply delays, ghost contacts, group text size, Llamagram and its pop-ups, per-sim Llamadate opt-ins, plus a **Manage contacts** entry for per-relationship prefs |

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
| `llama.testconnection` | Provider-aware diagnostic — for Ollama and LM Studio users, walks through reachability, available models, and end-to-end generation |
| `llama.testprovider` / `llama.testweather` / `llama.scanworlds` | Provider ping / weather-service dump / household world audit |
| `llama.debug` / `llama.debugsim` / `llama.dumpphone` / `llama.dumpprompt` | Internal state dumps for diagnostics |
| `llama.saveinfo` | Which per-save folder is in use and how it was resolved |
| `llama.birthwatch` | Run a birth-watcher pass now and show what it saw |
| `llama.trips` | Check for a trip now and list every recorded vacation / getaway |
| `llama.pregdebug First Last` / `llama.testbirth First Last` | Pregnancy visibility details / dry-run a birth announcement (never journaled) |
| `llama.journal_undo First Last [count]` | Remove the last journal entries for a sim |

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

Available auto-event types: `call`, `text`, `event`, `goals`, `story`, `drama`. Llamadate outreach (`dating`) and friends' Llamagram posts (`post`) are added automatically when enabled — set `social_npc_post_weight = 0` to keep friends from posting. The default is **phone-only** (`call, text`) to match the mod's focus — add the others to your `auto_event_types` if you want the full mix.

With the defaults (20 min interval, 40% chance), you get something roughly every 50 real minutes on average.

**Or toggle mid-session** via the in-game Settings panel (Phone → Llamafone → Settings) or via cheats:
```
llama.auto_events on
llama.auto_events off
```

---

## Configuration

Two paths to change settings:

1. **In-game Settings panel** — Phone → Llamafone → Settings. Toggles + numeric inputs for runtime-tunable values. Writes back to `llamafone.cfg`, preserving your comments, and applies immediately.
2. **Edit `llamafone.cfg` by hand** — then run `llama.reload` to pick up changes without restarting.

### Key settings

| Setting | Default | Editable in UI | Description |
|---|---|---|---|
| `provider` | `claude` | ❌ | `claude`, `openai`, `gemini`, `openrouter`, `ollama`, or `lmstudio` |
| `api_key` | *(required for cloud providers)* | ❌ | Blank for Ollama and LM Studio |
| `default_model` | `claude-haiku-4-5` | ❌ | Used for briefings and storyline generation |
| `fast_model` | `claude-haiku-4-5` | ❌ | Used for calls, texts, and reply generation |
| `ollama_endpoint` | `http://localhost:11434` | ❌ | Only used when provider = `ollama` |
| `lmstudio_endpoint` | `http://localhost:1234` | ❌ | Only used when provider = `lmstudio` |
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
| `birth_announcements` | `true` | ❌ | Texts from new parents when someone close has a baby |
| `social_enabled` | `true` | ✅ | Llamagram on / off |
| `social_post_popups` | `true` | ✅ | Friends' new posts pop up (off = inbox only) |
| `social_comment_popups` | `true` | ✅ | Comment / like / follower pop-ups on your posts (off = inbox only) |
| `social_npc_post_weight` | `25` | ❌ | How often friends post, relative to call:50 / text:50. 0 = never |

Per-save data (journal, milestones, group threads, contact preferences, past events, interactions, Llamagram feed, trips) lives in `Documents/Electronic Arts/The Sims 4/saves/Llamafone/Slot_NNNNNNNN/`. Multiple saves get their own folders — no cross-contamination.

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
| Ollama / LM Studio | any local model | **free** (uses your GPU) |

A typical session with ~30 Haiku or gpt-4o-mini commands lands around **$0.15**. Heavy sessions with long-form storyline generation run **$0.50 – $1.50** on premium models. Gemini's free tier covers most casual play. Ollama is fully free if you have the hardware.

**Llamagram adds a little:** each of your posts is one larger call (it writes all the comments at once), each reply to a comment is one more, and with auto-events on, friends' posts are about one in five auto-events at the default weight.

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
python build.py --release  # builds + Llamafone_vX.Y.Z.zip for CurseForge (no cfg inside)
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
    api_client.py               AI provider HTTP calls (Claude/OpenAI/Gemini/OpenRouter/Ollama/LM Studio) via curl
    sim_context.py              reads sim data, protagonist system, relationship network
    save_id.py                  per-save data folder resolution + save-switch hook
    dialogue.py                 dialogue, conversation, backstory generation
    storyteller.py              story updates, storylines, relationship drama
    event_generator.py          random events, challenges, weekly goals
    phone.py                    AI-generated calls, texts, group texts
    phone_ui_injection.py       grafts SuperInteractions onto Sim _phone_affordances
    phone_ui_interactions.py    Phone > Llamafone > Call / Text / Llamadate / Llamagram / Settings handlers + multi-select picker
    auto_events.py              background thread for random auto-events
    events.py                   reads upcoming + ongoing calendar events with focal sims
    past_events.py              logs shared calendar events after they end (per save)
    milestones.py               detects & dedups life events (job, marriage, birth, ...), pregnancy visibility, news-spread gating
    births.py                   birth announcements: complete_pregnancy hook, snapshot watcher, household-switch sweep
    social.py                   Llamagram: posts, comment passes, followers / fame, inbox, Feed.json
    trips.py                    trip memory: travel-group watcher, Trips.json, trip lines in prompts
    interactions.py             logs in-person interactions via Relationship.add_relationship_bit
    group_texts.py              persistent group thread storage (per save)
    contact_prefs.py            per-pair contact prefs (state + note) + relationship_events (origin, etc.) + auto-detection
    dating.py                   Llamadate: opt-ins, bios, candidate filtering, cold outreach, reply classifier
    save_notes.py               save-level world-context notes prepended to every prompt
    sim_bios.py                 per-sim character notes (backstory / private context) injected into descriptor blocks
    service_npc.py              butler / maid / nanny / gardener / repair tech role detection + register-anchoring flavor
    relationship_impact.py      sentiment-driven friendship / romance nudges (bidirectional, capped)
    moodlets.py                 buff/moodlet application from AI messages (per-recipient reactions)
    notifications.py            in-game notification popups (top-right panel)
    commands.py                 all llama.* cheat commands
    journal.py                  persistent cross-session story memory (per save)

package_src/                    XML tunings packed into Llamafone.package
  phoneCategory_Llamafone.xml   PieMenuCategory for the standalone Llamafone tile
  Llamafone_Call.xml            SuperInteraction for Llamafone > Call Someone
  Llamafone_Text.xml            SuperInteraction for Llamafone > Send Text
  Llamafone_Dating.xml          SuperInteraction for Llamafone > Llamadate
  Llamafone_Settings.xml        SuperInteraction for Llamafone > Settings
  Llamafone_Post.xml            SuperInteraction for Llamafone > Llamagram Post
  Llamafone_Notifications.xml   SuperInteraction for Llamafone > Llamagram Notifications

tools/
  package_builder.py            DBPF v2.1 packer (no S4S dependency)
  python37/                     embedded Python 3.7 for compiling .pyc

docs/                           GitHub Pages site (morganparadis.github.io/llamafone)
```
