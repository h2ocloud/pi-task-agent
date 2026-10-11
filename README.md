# pi-task-agent

Lightweight local AI agent. Standard-library Python, one [pi](https://github.com/badlogic/pi-mono) process per step, any OpenAI-compatible model behind it.

A small local model is allowed to take longer and use more steps. The job still has to finish, or stop with an honest reason. It must not wander off and report work it never did.

## What it does

- **Extensible.** A skill is a directory with `SKILL.md`. Drop it in `~/.pi-tg/skills/` and it can be loaded. Nothing else in the bridge changes. See `examples/skills/note/`.
- **Loaded on demand.** Default `PITG_SKILLS=ondemand`. Skills marked `always: true` are always included. Others load only when the message shares keywords with the name, description, or `triggers` (Chinese two-character overlap, so「查天氣」matches「天氣」). Cap is `PITG_SKILL_LIMIT` (default 3). `PITG_SKILLS=all` loads the whole directory. `off` loads none. pi is started with `--no-skills` plus one `--skill` per selected directory, so it does not discover everything else on the machine.
- **Compresses its own context.** Chat history past `PITG_COMPACT_AT` tokens (default 40000) is summarized. The original goal and the user's explicit requirements stay verbatim (pins). Paths, URLs, and ids dropped by the summary are put back by a regex, not by the model. If the compactor fails, the fallback is the recent messages plus those anchors.
- **Stays aligned.** Every step is a fresh session that repeats the original goal. The planner is not told to assume hosts, accounts, or directories. A finished step that cites a path or URL absent from the goal, the pins, and the tool trace is rejected and retried. The checker model can also reject a step that missed its done-condition. If that checker itself fails, the step is kept — see limits below.
- **Finishes.** Two tries, then the step is split (depth 3). A stuck tool is killed; the run continues. Context overflow compacts and resumes (up to 3 times) instead of failing the job. The whole job has a 3-hour cap. What cannot be done is skipped and reported. Only a real block (password, permission, an explicit decision) stops to ask.

## Layout

| File | Role |
|---|---|
| `pi_tg_bridge.py` | Telegram and LINE: ack first, route follow-ups, slash commands, restart resume |
| `orchestrator.py` | Plan, run one small step per fresh session, verify, split, report |
| `context.py` | Compaction, pins, anchor restore |
| `skills.py` | On-demand skill selection |
| `pirun.py` | One `pi` run: stall, loop, round cap, context cap, wall clock |
| `line.py` | LINE webhook, free reply-token path, push quota |
| `providers.py` | fast / main tiers, local server or Grok |
| `lang.py` | Reply language follows the user (Traditional Chinese or English) |
| `dashboard.py` | Loopback page for knobs, a short test prompt, and the log |

Behavior worth keeping when you change it: answer before the long work, move a task to the background after two tool rounds, four status words only (`開始了` / `還在跑` / `做完` / `被擋住`). Slowness is fine. Drifting is not.

## Run

```bash
cp config.example.env ~/.config/pi-tg-bridge/env
chmod 600 ~/.config/pi-tg-bridge/env
# fill BOT_TOKEN, OWNER_ID, and point PI_PROVIDER / the model base URL at your model
mkdir -p ~/.pi-tg/skills
cp -R examples/skills/note ~/.pi-tg/skills/
python3 pi_tg_bridge.py
```

Python 3.10+. No pip packages. `pi` must be on `PATH` (or set `PI_BIN`).

After start, a loopback page is at `http://127.0.0.1:8787/` (`PITG_DASH=0` turns it off). It tunes the harness, sends a short test prompt, and shows a scrubbed log. It does not load a model.

Slash commands are applied directly, not by the model: `/jobs /stop /resume /new /compact /status /model /thinking /skills /help`.

Replies follow the user's language. Traditional Chinese when they write Chinese, English otherwise. `PITG_LANG=zh` sets the fallback for slash commands before any message. The public default is English.

## Local model, or Grok when you have none

`PITG_PROVIDER=auto` uses your OpenAI-compatible server while it answers. If it does not, and `XAI_API_KEY` is set, the same calls go to xAI. `local` stays on your server. `grok` uses xAI even when the server is up.

Fast calls (routing, short checks) and main calls (planning, the pi tool loop, the final report) are two tiers. On a local server they are two aliases of the one resident model. On Grok, fast is `grok-4-1-fast-non-reasoning` and main is `grok-build-0.1`. Do not load a second GPU weight to get the fast tier.

The pi tool loop needs a provider block. See `examples/pi-grok.models.json`. Replace the placeholder key in your private pi config. Direct planner calls only need `XAI_API_KEY` in the env file.

Measured context sizes, temperatures, and the Grok prices are in [TUNING.md](TUNING.md).

## Limits

- The step checker fails open. A broken checker does not block a job that reported a grounded result. The path/URL check does not fail open.
- "Do not send mail or post messages" is in the step prompt. It is not a separate permission deny on the shell tool.
- Keyword skill selection misses a skill whose name and description never use the user's words. Put those words in `description` or `triggers`, or set `PITG_SKILLS=all`.

## Tests

```bash
python3 tests/test_pirun.py
python3 tests/test_orchestrator.py
python3 tests/test_skills.py
python3 tests/test_line.py
python3 tests/test_bridge.py
python3 tests/test_providers.py
```

`tests/selftest.py` and `tests/e2e_task.py` call a real `pi` and a real model. They are not part of the offline suite.
