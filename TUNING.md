# Tuning

Measured on one NVIDIA DGX Spark (GB10, 128 GB unified memory) with a single resident model: Qwen3.8-Flash-Next, GGUF IQ3_S, served by Strata. The server context window is 131072. Do not load a second weight beside it. A second resident model previously cut decode by about half.

The harness does not use the whole server window.

| Knob | Value | Why |
|---|---|---|
| Compact chat after | 40000 tokens | Past this, the local model drifts. `PITG_COMPACT_AT`. |
| One step | 45000 tokens | Then the step is summarized and continued. `PITG_STEP_CTX`. |
| Foreground cap | about 88000 | `compact_at × 2.2`. Longer chats move to a background job. |
| Tool sampling | 0.7 | 0.2 makes this model loop the same tool call. Set this on the server or gateway. pi has no temperature flag. |
| Planner JSON | 0.2 | Short JSON only. Dashboard field `planner_temperature`. |
| Stuck tool | 180 s | Kill that process. The step continues. |
| Foreground wall | 600 s | Then the owner chat promotes the turn to a job. |

Qwen's own non-thinking recipe is temperature 0.7, top_p 0.8, top_k 20, presence penalty 1.5. The server in this measurement also uses int8 KV, speculative decoding 4, and MTP. Those are server flags. The dashboard does not change them and does not restart the GPU process.

An older llama.cpp launch used a 262144 context. That is not the current server. Do not turn the window up to that unless you measure memory yourself.

Above 60000 tokens the dashboard warns you. Compact earlier.

## When the local server is down

`PITG_PROVIDER=auto` (default) uses the local server while it answers. If it does not, and `XAI_API_KEY` is set, calls go to xAI. `local` never leaves the machine. `grok` uses xAI even when the local server is up.

Two tiers, one resident weight. They may be the same endpoint.

| Tier | Local | Grok (subscription) |
|---|---|---|
| fast | the `fast` alias on the same server | `grok-4-1-fast-non-reasoning` (2,000,000 context, about $0.20 / $0.50 per 1M input / output) |
| main | the model you selected in `/model` | `grok-build-0.1` (Grok Build, 256,000 context). Under 200k prompt tokens: $1 / $2 per 1M. At or above 200k: $2 / $4 per 1M, for the whole request. |

Override with `PI_GROK_FAST` and `PI_GROK_MAIN`. A heavier flagship is `grok-4.7` (500,000 context, $2 / $6 under 200k, $4 / $12 at or above). It is not the default.

Base URL is `https://api.x.ai/v1` (`PI_GROK_BASE`). The US regional host `https://us.api.x.ai/v1` currently serves `grok-4.7` and `grok-4.6` only. It does not serve the fast model or Grok Build. Stay on the global host.

Planner and router calls use `XAI_API_KEY` (or `GROK_API_KEY`) over HTTP. The pi tool loop needs a provider entry. Copy `examples/pi-grok.models.json` into your private pi config and replace `PUT_XAI_API_KEY_HERE`. Do not commit the key. Prices were read from docs.x.ai on 2026-10-11. Check them again before you rely on a bill.

Fast calls are not sent to Grok on every status check. A status ping only touches the local server.

## Dashboard

The bridge listens on `http://127.0.0.1:8787/` unless `PITG_DASH=0`. It refuses any other bind address. Use it to change the knobs above, send a short test prompt (default temperature 0.7), and read a scrubbed log. Set `PITG_LOG` to the log file. The page has an English / 繁體中文 toggle. Nothing on that page starts or stops the GPU server.

## 繁體中文

一台 DGX Spark、一顆常駐的 Qwen3.8-Flash-Next IQ3_S。伺服器上下文 131072。Harness 在 40000 token 壓縮聊天，單步 45000。工具呼叫的溫度用 0.7，短 JSON 用 0.2。不要在同一張卡再載第二顆模型。

沒有地端時，設 `XAI_API_KEY`，`PITG_PROVIDER` 維持 `auto`。短判斷走 `grok-4-1-fast-non-reasoning`，真正做事走 `grok-build-0.1`。金鑰只放本機，不要進 git。
