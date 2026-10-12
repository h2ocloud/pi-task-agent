"""Two model tiers and two providers.

fast  — short judgment: routing, language-neutral classification, compaction.
main  — the task itself: planning, tool steps, checking.

local — the OpenAI-compatible server you run (the measured setup is one model on a DGX Spark).
grok  — Grok Build subscription. Fast calls use grok-4-1-fast-non-reasoning.
        Task calls use grok-build-0.1. If that call fails, the bridge uses the local model.
chatgpt — ChatGPT subscription (Codex OAuth, not an API key). Fast calls use gpt-5.4-mini.
        Task calls use gpt-5.6-luna. If that call fails, the bridge uses the local model.
        auto does not select this. Set the mode to chatgpt.

local (default): never leave the machine. This is the spark-main model.
auto: use local while it answers; if it does not, and a Grok credential is set, use Grok.
grok: Grok Build first. The local model is the fallback.
chatgpt: ChatGPT subscription first. The local model is the fallback.

A grok or chatgpt run that fails before its first tool call (401/403, OAuth or token errors,
timeouts, or no output within PITG_FIRST_TOKEN_S seconds, default 30) is retried on the local
model. See fallback_reason.

Do not load a second weight next to the resident local model. The fast local tier is an alias
on the same server, or the same model with a short reply.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request

GROK_BASE = "https://api.x.ai/v1"


def join_url(base: str, path: str) -> str:
    """Join an API root and a path. A root that already ends in /v1 is not doubled."""
    root = (base or "").rstrip("/")
    suffix = path if path.startswith("/") else "/" + path
    if root.endswith("/v1") and (suffix == "/v1" or suffix.startswith("/v1/")):
        suffix = suffix[3:]
    return root + suffix
GROK_FAST = "grok-4-1-fast-non-reasoning"
GROK_MAIN = "grok-build-0.1"
TOKEN_URL = "https://auth.x.ai/oauth2/token"


def grok_key(env: dict) -> str:
    static = (env.get("XAI_API_KEY") or env.get("GROK_API_KEY") or "").strip()
    if static:
        return static
    if (env.get("PITG_GROK_SESSION") or "1").strip() == "0":
        return ""
    return session_access_token(env.get("PITG_GROK_AUTH") or "")


def resolve(env: dict, tier: str, local_ok: bool, local_model: str, local_base: str, local_key: str) -> dict:
    """Return provider, base, key, model, pi_provider. tier is 'fast' or 'main'."""
    mode = (env.get("PITG_PROVIDER") or "local").strip().lower()
    if mode == "chatgpt":
        cred = chatgpt_credential(env)
        if cred:
            picked = (env.get("PI_CHATGPT_FAST") if tier == "fast" else env.get("PI_CHATGPT_MAIN")) or ""
            picked = picked.strip() or (CHATGPT_FAST if tier == "fast" else CHATGPT_MAIN)
            return {
                "provider": "chatgpt",
                "base": (env.get("PI_CHATGPT_BASE") or CHATGPT_BASE).rstrip("/"),
                "key": cred["access"],
                "account": cred["account"],
                "refresh": cred["refresh"],
                "expires": cred["expires"],
                "model": picked,
                "pi_provider": "openai-codex",
            }
        return _local_choice(env, local_model, local_base, local_key)
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
    return _local_choice(env, local_model, local_base, local_key)


def _local_choice(env: dict, local_model: str, local_base: str, local_key: str) -> dict:
    return {
        "provider": "local",
        "base": (local_base or "").rstrip("/"),
        "key": local_key or "",
        "model": local_model,
        "pi_provider": (env.get("PI_PROVIDER") or "spark").strip(),
    }


AUTH_ERR = re.compile(r"\b40[013]\b|unauthori[sz]|authori[sz]|authenticat|\bauth\b|oauth|access token|refresh token|invalid_grant|could not be validated|api[ _-]?key", re.I)
TIMEOUT_ERR = re.compile(r"timed? ?out|ETIMEDOUT|deadline exceeded", re.I)


def first_token_s(provider: str, env: dict) -> int:
    """Seconds a grok or chatgpt run may stay silent before its first output. 0 = no watchdog."""
    if provider not in ("grok", "chatgpt"):
        return 0
    return max(0, int(env.get("PITG_FIRST_TOKEN_S") or 30))


def fallback_reason(provider: str, kind: str, text: str, rounds: int) -> str:
    """Why a grok or chatgpt run should be retried on the local model, or "" for no retry.
    Only before the first tool call: a retry after tool calls could repeat their side effects."""
    if provider not in ("grok", "chatgpt") or rounds or kind not in ("error", "timeout", "first_token"):
        return ""
    if kind == "first_token":
        return text                                      # pirun 已寫好原因：first-token timeout 30s
    if kind == "timeout" or TIMEOUT_ERR.search(text or ""):
        return "timeout"
    if AUTH_ERR.search(text or ""):
        return "auth"
    return "error"


CHATGPT_FAST = "gpt-5.4-mini"
CHATGPT_MAIN = "gpt-5.6-luna"
CHATGPT_BASE = "https://chatgpt.com/backend-api"
CHATGPT_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_CLIENT = "app_EMoamEEZ73f0CkXaXp7hrann"
_chatgpt_block: dict[str, float] = {}


def _jwt_payload(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    return _b64json(parts[1])


def _jwt_exp(token: str) -> int:
    exp = _jwt_payload(token).get("exp") or 0
    return int(exp) if isinstance(exp, (int, float)) else 0


def _account_from_jwt(token: str) -> str:
    auth = _jwt_payload(token).get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return str(auth.get("chatgpt_account_id") or "")
    return ""


def _codex_file(env: dict) -> str:
    return env.get("PITG_CODEX_AUTH") or os.path.expanduser("~/.codex/auth.json")


def _read_codex(path: str) -> dict | None:
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    access = str(tokens.get("access_token") or "")
    refresh = str(tokens.get("refresh_token") or "")
    account = str(tokens.get("account_id") or "") or _account_from_jwt(access)
    if not access and not refresh:
        return None
    return {"access": access, "refresh": refresh, "account": account}


def _chatgpt_state_path(env: dict) -> str:
    return env.get("PITG_CHATGPT_STATE") or os.path.expanduser("~/.pi-tg/state/chatgpt.json")


def _last_refresh(path: str) -> str:
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("last_refresh") or "")
    return ""


def _mark_refresh_dead(env: dict, auth_path: str) -> None:
    """Remember that this subscription file was rejected. A newer login clears it."""
    path = _chatgpt_state_path(env)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"refresh_failed": True, "last_refresh": _last_refresh(auth_path)}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _refresh_dead(env: dict, auth_path: str) -> bool:
    path = _chatgpt_state_path(env)
    try:
        mark = json.load(open(path, encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(mark, dict) or not mark.get("refresh_failed"):
        return False
    return str(mark.get("last_refresh") or "") == _last_refresh(auth_path)


def chatgpt_view(env: dict) -> dict:
    """Whether a ChatGPT subscription login is on disk. Does not refresh."""
    if (env.get("PITG_CHATGPT") or "1").strip() == "0":
        return {"signed": False, "fresh": False, "dead": False}
    path = _codex_file(env)
    tokens = _read_codex(path)
    if not tokens or not tokens.get("refresh"):
        return {"signed": False, "fresh": False, "dead": False}
    dead = _refresh_dead(env, path)
    exp = _jwt_exp(tokens.get("access") or "")
    return {"signed": not dead, "fresh": bool(not dead and exp and exp - time.time() > 60), "dead": dead}


def chatgpt_credential(env: dict) -> dict | None:
    """Access token for the subscription. Refreshes only when the access token is near expiry."""
    if (env.get("PITG_CHATGPT") or "1").strip() == "0":
        return None
    path = _codex_file(env)
    tokens = _read_codex(path)
    if not tokens:
        return None
    access = tokens.get("access") or ""
    refresh = tokens.get("refresh") or ""
    account = tokens.get("account") or ""
    exp = _jwt_exp(access)
    if access and account and exp - time.time() > 300:
        return {"access": access, "refresh": refresh, "account": account, "expires": int(exp * 1000)}
    if _refresh_dead(env, path) or time.time() < _chatgpt_block.get(path, 0):
        return None
    if not refresh:
        return None
    fresh = _refresh_codex(env, path, refresh, account)
    if not fresh:
        _chatgpt_block[path] = time.time() + 600
        return None
    _chatgpt_block.pop(path, None)
    return fresh


def _refresh_codex(env: dict, path: str, refresh: str, account: str) -> dict | None:
    url = (env.get("PITG_CHATGPT_TOKEN_URL") or CHATGPT_TOKEN_URL).strip()
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CHATGPT_CLIENT,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            _mark_refresh_dead(env, path)
        return None
    except Exception:
        return None
    access = str(data.get("access_token") or "").strip()
    if not access:
        return None
    new_refresh = str(data.get("refresh_token") or refresh)
    expires_in = data.get("expires_in")
    exp = _jwt_exp(access)
    if not exp and isinstance(expires_in, (int, float)):
        exp = int(time.time() + int(expires_in))
    account = account or _account_from_jwt(access)
    if not account:
        return None
    _write_codex(path, access, new_refresh)
    return {"access": access, "refresh": new_refresh, "account": account, "expires": int(exp * 1000)}


def _write_codex(path: str, access: str, refresh: str) -> None:
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        return
    data["tokens"]["access_token"] = access
    data["tokens"]["refresh_token"] = refresh
    data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sync_pi_codex(auth_path: str, cred: dict) -> None:
    """Write the ChatGPT subscription into pi's auth file. The token stays out of models.json."""
    access = str(cred.get("key") or cred.get("access") or "")
    if not access or not auth_path:
        return
    try:
        data = json.load(open(auth_path, encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["openai-codex"] = {
        "type": "oauth",
        "access": access,
        "refresh": str(cred.get("refresh") or ""),
        "expires": int(cred.get("expires") or 0),
        "accountId": str(cred.get("account") or ""),
    }
    tmp = auth_path + ".tmp"
    os.makedirs(os.path.dirname(auth_path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, auth_path)


def codex_body(model: str, system: str, user: str, max_tokens: int) -> dict:
    return {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": system or "You are a helpful assistant.",
        "input": user or "",
        "max_output_tokens": max(int(max_tokens or 0), 1024),
        "text": {"verbosity": "low"},
    }


def codex_headers(account: str, token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account,
        "originator": "pi",
        "OpenAI-Beta": "responses=experimental",
        "Accept": "text/event-stream",
    }


def codex_text(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    chunks = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_text" and item.get("text"):
            chunks.append(str(item["text"]))
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text") and part.get("text"):
                chunks.append(str(part["text"]))
    text = "".join(chunks).strip()
    return text or None


def codex_reply(raw: str) -> tuple[str | None, str | None]:
    raw = raw or ""
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, dict):
            return codex_text(data), data.get("model")
    text, model = [], None
    for block in raw.split("\n\n"):
        blob = "\n".join(ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:"))
        if not blob or blob == "[DONE]":
            continue
        try:
            ev = json.loads(blob)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "response.output_text.delta" and ev.get("delta"):
            text.append(str(ev["delta"]))
        resp = ev.get("response") if isinstance(ev.get("response"), dict) else None
        if resp and resp.get("model"):
            model = resp.get("model")
        if not text:
            got = codex_text(resp or ev)
            if got:
                text.append(got)
    return ("".join(text).strip() or None), model


def _b64json(segment: str) -> dict:
    pad = segment + "=" * ((4 - len(segment) % 4) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


def _account(path: str) -> tuple[str, dict] | None:
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for name, acc in data.items():
        if isinstance(acc, dict) and acc.get("key"):
            return name, acc
    return None


def _refresh(path: str, name: str, acc: dict) -> str:
    token = acc.get("refresh_token") or ""
    client = acc.get("oidc_client_id") or ""
    if not token or not client:
        return ""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": client,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            fresh = json.loads(r.read().decode())
    except Exception:
        return ""
    access = (fresh.get("access_token") or "").strip()
    if not access:
        return ""
    acc["key"] = access
    if fresh.get("refresh_token"):
        acc["refresh_token"] = fresh["refresh_token"]
    exp = _b64json(access.split(".")[1]).get("exp") if access.count(".") >= 2 else None
    if isinstance(exp, (int, float)):
        acc["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp))
    try:
        data = json.load(open(path, encoding="utf-8"))
        data[name] = acc
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        return access
    return access


def session_access_token(path: str = "") -> str:
    """Grok CLI login (~/.grok/auth.json). Refreshes when under five minutes remain."""
    path = path or os.path.expanduser("~/.grok/auth.json")
    found = _account(path)
    if not found:
        return ""
    name, acc = found
    access = str(acc.get("key") or "")
    exp = 0
    parts = access.split(".")
    if len(parts) >= 2:
        exp = int(_b64json(parts[1]).get("exp") or 0)
    if exp and exp - time.time() > 300:
        return access
    refreshed = _refresh(path, name, acc)
    return refreshed or (access if exp and exp > time.time() else "")


def sync_pi_provider(models_path: str, key: str, base: str = GROK_BASE) -> None:
    """Put the current Grok credential into the private pi config. Does not print it."""
    if not key or not models_path:
        return
    try:
        data = json.load(open(models_path, encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    providers = data.setdefault("providers", {})
    providers["grok"] = {
        "baseUrl": base.rstrip("/"),
        "api": "openai-completions",
        "apiKey": key,
        "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
        "models": [
            {"id": GROK_FAST, "name": "Grok fast", "contextWindow": 2000000, "input": ["text"]},
            {"id": GROK_MAIN, "name": "Grok Build", "reasoning": True, "contextWindow": 256000, "input": ["text", "image"]},
        ],
    }
    tmp = models_path + ".tmp"
    os.makedirs(os.path.dirname(models_path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, models_path)
