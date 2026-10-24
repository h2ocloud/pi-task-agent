# Architecture

Three layers. Each one can be tested without the others.

```
message → pi_tg_bridge → orchestrator → pirun → pi → model
                │              │
                │              └─ context.Compactor (summary, pins, anchors)
                └─ skills.skills_for (which SKILL.md directories this turn gets)
```

## Why the steps are small

A long chat on a local model drifts. The orchestrator therefore plans 1–6 concrete steps and runs each in a new session. The prompt for a step always contains:

1. The original goal, verbatim.
2. The user's later requirements, verbatim (pins). The compactor is not allowed to rewrite them.
3. A short digest of finished steps.
4. The current step and its done-condition.
5. An alignment block: do not assume machines, paths, or numbers that are not in the task or in a tool result; if this step is no longer the goal, stop and change approach.

That is the alignment mechanism. It is not a second model trained to be honest. It is the same model, given less room to invent, plus a check that does not trust it.

## Alignment check

After a step returns a normal answer, `context.ungrounded` extracts paths and URLs from the answer. A claim is grounded only if it already appears in the goal, the pins, the digest, the step text, or the tool trace (a truncated trace path still counts when one is a prefix of the other). Anything else rejects the step. The next attempt is told the missing paths and told not to repeat the same command. Two failures and the step is split into smaller ones. At depth 3 it is skipped and the final report says so.

The checker model (`VERIFIER`) can also reject a step that missed `done_when` or answered a different task. If that call errors or returns nonsense, the step is accepted. The path check still applies.

The planner prompt does not list hosts, home directories, or services. Inventing that environment was a reliable way to make a local model hallucinate a successful run.

## Compaction

Two places:

- **Chat.** When the session crosses the token mark, the narrative is summarized and the session id rotates. The last few turns are copied verbatim. The next user message is prefixed with the summary. Pins for a job are not stored in that summary; they live on the job.
- **A job.** Step results accumulate into `digest`. When the digest no longer fits, it is compressed recursively: `compress(old digest, new result)`. Anchors missing from the new summary are appended by regex. A context overflow inside one step (`ctx_full`) becomes a carry summary and a new session, up to three times, and is not a failure.

## Skills

`skills.catalog` reads only `SKILL.md` frontmatter (`name`, `description`, `triggers`, `always`). `skills_for` picks directories. `pirun` passes them as repeated `--skill` flags and sets `--no-skills`, so pi does not also load every skill it can discover. Adding a capability is adding a directory. Removing one is deleting it or leaving it unmatched.

## Finishing, slowly

| Condition | What happens |
|---|---|
| Tool silent for 180s | Kill that tool process only. The model gets the error and can try another command. A second stall ends the step. |
| Same tool call 3 times | End the step, then split it. |
| More than 14 tool rounds, or the step clock runs out | Split. Do not kill the whole job. |
| Context over 45000 tokens | Compact and continue the same step. |
| Backend connection error | Wait and retry the same step. Say so at most once per 30 minutes. |
| Bridge process restarts | Resume the job from the first unfinished step, up to 3 automatic resumes. |
| Needs a secret or a decision | Stop, ask once, pin the answer, continue. |
| Job older than 3 hours | Stop. Progress stays on disk. `/resume` continues. |

Status shown to the user is one of four words: `開始了`, `還在跑`, `做完`, `被擋住`.

## Two tiers, two providers

`providers.resolve` picks an OpenAI-compatible base URL. `fast` is for routing and other short calls. `main` is for planning, the pi tool loop, and the final report. `PITG_PROVIDER=auto` stays on the local server while a one-token ping succeeds, and moves to Grok only when that ping fails and `XAI_API_KEY` is set. `grok` forces the subscription. The fast local alias is the same server, not a second loaded model.

Grok defaults: fast `grok-4-1-fast-non-reasoning`, main `grok-build-0.1`, base `https://api.x.ai/v1`. The pi process only follows that switch after you add the provider in your own models file (`examples/pi-grok.models.json`).

## Language

`lang.detect` is a character heuristic so a slash command still works when the fast model is down. Model prompts end with a rule: answer in the user's language, Traditional Chinese or English, and do not default to Chinese. Chrome strings use the same choice. Stored job status stays the four Chinese words.

## Conversation

One path for Telegram and LINE (`convo.py`).

- **Burst merge.** Messages from one person within `PITG_DEBOUNCE` seconds (default 2.5, capped at 8 from the first) become one message. Slash commands skip the wait.
- **Shared recent turns.** The bridge keeps the last ~12 exchanges per chat (`state/convo.json`). The fast model, the triage call, side sessions, and a short promoted goal all get them, plus what is running now. So 查, 好, 那明天呢 continue the last request instead of being asked back. The main pi session gets what happened outside it since its last turn, once.
- **No queue.** The fast model answers before any lock. If the main session is busy, a new task runs in a side session (`side-<chat>-<id>`, recent turns prepended) under the same `MAX_RUNS` limit. Nothing says "the previous one is still running".
- **Messages about running work.** 好了沒 / 進度 gets the status of foreground turns and jobs at once. 停 / 取消 stops the one running thing, or asks which when there are several. Anything else is classified against the running items: `same` (a nudge) gets one short line, `addto` is handed to that item (after the turn, or as a job supplement), `other` runs in parallel. No 加/新 question.
- **One message per job.** A job's start, plan, and milestones carry a key. Telegram edits that one message while it is still the latest, and sends a fresh one once other messages bury it. The bridge skips a message identical to its last one within a minute unless the user spoke in between.

## Channels

Telegram long-polling, optional LINE webhook. LINE spends the free reply token first. Later pushes are only a job's start, the result, a block, or a question. Keyed progress is never pushed on LINE and never takes the reply token, so a long job costs two pushes. Under five pushes remaining, the other channel is used so the result is not dropped. Chat-only users get `--no-tools`.

Outbound mail is a prompt rule on the step, not a shell deny-list. Treat that as a gap if you run this against an untrusted chat.
