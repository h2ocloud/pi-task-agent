"""Two model tiers and two providers.

fast  — short judgment: routing, language-neutral classification, compaction.
main  — the task itself: planning, tool steps, checking.

local — the OpenAI-compatible server you run (the measured setup is one model on a DGX Spark).
grok  — xAI subscription. Fast calls use grok-4-1-fast-non-reasoning.
        Task calls use grok-build-0.1 (Grok Build) when the local server is down.

auto (default): use local while it answers; if it does not, and XAI_API_KEY is set, use Grok.
Do not load a second weight next to the resident local model. The fast local tier is an alias
on the same server, or the same model with a short reply.
"""
from __future__ import annotations

GROK_BASE = "https://api.x.ai/v1"
GROK_FAST = "grok-4-1-fast-non-reasoning"
GROK_MAIN = "grok-build-0.1"


def grok_key(env: dict) -> str:
    return (env.get("XAI_API_KEY") or env.get("GROK_API_KEY") or "").strip()


def resolve(env: dict, tier: str, local_ok: bool, local_model: str, local_base: str, local_key: str) -> dict:
    """Return provider, base, key, model, pi_provider. tier is 'fast' or 'main'."""
    mode = (env.get("PITG_PROVIDER") or "auto").strip().lower()
    key = grok_key(env)
    use_grok = mode == "grok" or (mode == "auto" and not local_ok and bool(key))
    if mode == "local":
        use_grok = False
    if use_grok and key:
        model = (env.get("PI_GROK_FAST") if tier == "fast" else env.get("PI_GROK_MAIN")) or ""
        model = model.strip() or (GROK_FAST if tier == "fast" else GROK_MAIN)
        return {
            "provider": "grok",
            "base": (env.get("PI_GROK_BASE") or GROK_BASE).rstrip("/"),
            "key": key,
            "model": model,
            "pi_provider": (env.get("PI_GROK_PI_PROVIDER") or "grok").strip(),
        }
    return {
        "provider": "local",
        "base": (local_base or "").rstrip("/"),
        "key": local_key or "",
        "model": local_model,
        "pi_provider": (env.get("PI_PROVIDER") or "spark").strip(),
    }
