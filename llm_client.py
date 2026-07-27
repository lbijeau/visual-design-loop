"""The single LLM transport: Ollama and OpenAI-compatible dialects, retries, fallback.

Every LLM call in the project goes through this module.  Provider resolution,
payload building, image encoding, retry, and fallback are all here.
"""

import base64
import json
import os
import sys
import time
from typing import Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from provider_config import GLOBAL_MODEL_ID, ProviderConfig

# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def build_ollama_payload(
    provider: dict,
    messages: list,
    model_id: str,
    response_format: str = "text",
) -> Tuple[str, dict, dict]:
    """Build an Ollama /api/chat request.  Returns (url, headers, payload_dict)."""
    url = f"{provider['baseUrl'].rstrip('/')}/api/chat"
    headers = {"Content-Type": "application/json"}
    api_key = provider.get("apiKey", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for k, v in (provider.get("customHeaders") or {}).items():
        headers[k] = v
    payload = {
        "model": model_id,
        "messages": messages,
        "stream": True,
    }
    # Ollama accepts only "json" or a JSON Schema here; "text" is rejected mid-stream by the
    # local inference path (the cloud proxy ignores the field, so this only bit local models).
    # Plain text is the default behaviour when `format` is absent.
    if response_format and response_format != "text":
        payload["format"] = response_format
    # Thinking tokens are discarded by _consume_stream_line, so they only consume the wall-clock
    # deadline. Some thinking-capable models never emit content at all with it on. Providers that
    # want it back can set "think": true.
    payload["think"] = bool(provider.get("think", False))
    return url, headers, payload


def build_openai_compatible_payload(
    provider: dict,
    messages: list,
    model_id: str,
) -> Tuple[str, dict, dict]:
    """Build an OpenAI-compatible /v1/chat/completions request.

    Strips exactly ``/v1`` from the base URL (3 characters) before appending
    the chat completions path, so ``http://host:9000/v1`` becomes
    ``http://host:9000/v1/chat/completions``.
    """
    base = provider["baseUrl"].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    url = f"{base}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    api_key = provider.get("apiKey", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for k, v in (provider.get("customHeaders") or {}).items():
        headers[k] = v
    payload = {
        "model": model_id,
        "messages": messages,
        "stream": True,
    }
    return url, headers, payload


def is_ollama_provider(provider: dict) -> bool:
    """Return True if the provider uses the Ollama /api/chat dialect."""
    pid = provider.get("id", "")
    return pid == "ollama" or pid.startswith("custom-ollama")


# ---------------------------------------------------------------------------
# Message building (image encoding per dialect)
# ---------------------------------------------------------------------------


def _build_messages(
    provider: dict,
    prompt: str,
    system_prompt: Optional[str],
    image_b64: Optional[str],
) -> list:
    """Build the messages list, encoding images in the correct dialect.

    * Ollama: ``"images"`` key on the user message.
    * OpenAI-compatible: content-array with ``image_url`` (data URI).
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    if image_b64 and is_ollama_provider(provider):
        messages.append({"role": "user", "content": prompt, "images": [image_b64]})
    elif image_b64:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }
        )
    else:
        messages.append({"role": "user", "content": prompt})

    return messages


# ---------------------------------------------------------------------------
# Low-level API call (retry + backoff)
# ---------------------------------------------------------------------------


def _consume_stream_line(line: str, parts: list, is_ollama: bool) -> bool:  # noqa: C901  (two-dialect parser; isinstance guards enforce the exception-safety invariant)
    """Parse one streamed line; append any content delta to *parts*; return True if done.

    Exception-safe by contract: benign lines (blank, SSE comment/event, unparseable,
    structurally weird, missing fields, empty choices) are skipped. The ONLY exception
    it raises is ``requests.exceptions.ConnectionError``, on an explicit provider
    ``{"error": ...}`` record — so the caller's retry/fallback machinery handles it
    like any transport error.
    """
    line = line.strip()
    if not line:
        return False
    if is_ollama:  # NDJSON: one JSON object per line
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(obj, dict):
            return False
        if obj.get("error"):
            raise requests.exceptions.ConnectionError(f"ollama stream error: {obj['error']}")
        msg = obj.get("message")
        if isinstance(msg, dict):
            parts.append(msg.get("content", "") or "")
        return bool(obj.get("done"))
    # OpenAI-compatible SSE: only 'data:' lines carry payloads
    if not line.startswith("data:"):
        return False  # skip ':' comments, 'event:'/'id:' lines
    data = line[5:].strip()
    if data == "[DONE]":
        return True
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    if obj.get("error"):
        raise requests.exceptions.ConnectionError(f"openai stream error: {obj['error']}")
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta")
        if isinstance(delta, dict):
            parts.append(delta.get("content") or "")
    return False


def _parse_nonstreaming_body(text: str, is_ollama: bool) -> str:
    """Best-effort parse of a full, non-streamed completion body.

    A provider may ignore ``stream: true`` and return a single completion object
    (``{"choices":[{"message":{"content":...}}]}`` / ``{"message":{"content":...}}``)
    instead of an NDJSON/SSE token stream. When the streaming pass assembles nothing,
    fall back to this. Returns ``""`` on any failure — never raises.
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    if is_ollama:
        msg = obj.get("message")
        return (msg.get("content", "") or "") if isinstance(msg, dict) else ""
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict):
            return msg.get("content", "") or ""
    return ""


CONNECT_TIMEOUT = 10  # seconds to establish the connection
IDLE_TIMEOUT = 120  # max seconds between bytes before requests raises


def _stream_api_call(url: str, headers: dict, payload: dict, is_ollama: bool, total_timeout: float) -> str:  # noqa: C901
    """One bounded streaming attempt.

    Enforces a per-chunk idle timeout (via the requests read-timeout) and a hard total
    wall-clock deadline. Returns assembled content, or raises a class the caller's retry /
    fallback machinery handles: ``ReadTimeout`` (total-deadline / header-wait), ``ConnectionError``
    (idle mid-body, connect, mid-stream disconnect, decode failure, provider error record), or a
    non-retriable ``HTTPError`` (4xx).
    """
    start = time.monotonic()  # clock covers connect + first-byte + body
    resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=(CONNECT_TIMEOUT, IDLE_TIMEOUT))
    try:
        resp.raise_for_status()  # 4xx available pre-body; stays non-retriable
        parts, buf, raw = [], b"", b""
        try:
            for chunk in resp.iter_content(chunk_size=1024):
                if time.monotonic() - start > total_timeout:
                    raise requests.exceptions.ReadTimeout(f"total deadline {total_timeout}s exceeded")
                raw += chunk
                buf += chunk
                *complete, buf = buf.split(b"\n")  # keep trailing partial line in buf
                for line in complete:
                    if _consume_stream_line(line.decode("utf-8", "replace"), parts, is_ollama):
                        return "".join(parts)  # done sentinel -> stop early
            if buf:  # final line without a trailing newline
                _consume_stream_line(buf.decode("utf-8", "replace"), parts, is_ollama)
            content = "".join(parts)
            if not content:  # provider may have ignored stream:true and sent a whole body
                content = _parse_nonstreaming_body(raw.decode("utf-8", "replace"), is_ollama)
            return content
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as e:
            raise requests.exceptions.ConnectionError(e)  # normalize transport failures
    finally:
        resp.close()  # clean abort on every exit path


def _do_api_call(
    provider: dict,
    messages: list,
    model_id: str,
    response_format: str = "text",
    timeout: int = 300,
    retries: int = 3,
) -> str:
    """Make the HTTP request with retry on timeout / connection errors.

    Retries: up to `retries` attempts; 30s/60s backoff between the first two.
    Raises the last exception on exhaustion.
    """
    is_ollama = is_ollama_provider(provider)
    if is_ollama:
        url, headers, payload = build_ollama_payload(provider, messages, model_id, response_format)
    else:
        url, headers, payload = build_openai_compatible_payload(provider, messages, model_id)

    last_exc = None
    for attempt in range(retries):
        try:
            return _stream_api_call(url, headers, payload, is_ollama, timeout)
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                backoff = 30 * (attempt + 1)
                print(f"  ⚠️ LLM call failed (attempt {attempt + 1}/{retries}): {e}. Retrying in {backoff}s...")
                time.sleep(backoff)
        except requests.exceptions.HTTPError as e:
            # Non-retriable HTTP errors (4xx) propagate immediately
            raise e
    raise last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def call_llm(
    role: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    image_path: Optional[str] = None,
    response_format: str = "text",
    timeout: int = 600,
    retries: int = 1,
    provider_config: Optional[ProviderConfig] = None,
) -> str:
    """Call the LLM for *role* (``"brain"`` or ``"eyes"``).

    Resolves the role to a provider via *provider_config* (or a fresh default),
    builds the dialect-correct payload, encodes the image if provided, and
    walks the fallback chain on failure.
    """
    cfg = provider_config or ProviderConfig()
    role_cfg = cfg.get_role(role)
    provider_id = role_cfg["providerId"]
    fallback_order = role_cfg.get("fallbackOrder", [])

    # Collect the candidate provider IDs: primary first, then fallbacks
    candidate_ids = [provider_id] + fallback_order

    image_b64 = None
    if image_path:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

    last_exc = None
    resolution_failed = False
    for pid in candidate_ids:
        try:
            provider = cfg.resolve(pid)
        except (KeyError, FileNotFoundError) as e:
            last_exc = e
            resolution_failed = True
            print(f"  ⚠️ Provider '{pid}' not resolvable for role '{role}': {e}")
            continue
        try:
            model_id = cfg.get_model_id(provider)
            messages = _build_messages(provider, prompt, system_prompt, image_b64)
            return _do_api_call(provider, messages, model_id, response_format, timeout, retries)
        except Exception as e:
            last_exc = e
            print(f"  ⚠️ Provider '{pid}' failed for role '{role}': {e}")

    if resolution_failed:
        # Legacy env-var fallback: pre-wizard behavior when providers.json
        # roles reference a provider that no longer exists.
        legacy = {"id": "ollama", "baseUrl": config.OLLAMA_HOST, "apiKey": config.OLLAMA_API_KEY, "customHeaders": {}}
        print(f"  ⚠️ Falling back to legacy env provider ({config.OLLAMA_HOST}) for role '{role}'")
        messages = _build_messages(legacy, prompt, system_prompt, image_b64)
        return _do_api_call(legacy, messages, GLOBAL_MODEL_ID, response_format, timeout, retries)

    raise last_exc or Exception(f"No provider configured for role '{role}'")


def probe_provider(provider: dict, timeout: int = 10) -> dict:  # noqa: C901  (legacy; refactor deferred)
    """Test connectivity for a single provider.

    Builtin providers get an authenticated GET probe (``/api/tags`` for the
    Ollama dialect, ``/v1/models`` for OpenAI-compatible).  Custom providers
    get a keyless HEAD reachability check only — the API key is never sent
    to an unverified host.

    Returns ``{"ok": bool, "errorType": None|"auth"|"timeout"|"unreachable", "message": str}``.
    Never raises.
    """
    pid = provider.get("id", "")
    base_url = provider.get("baseUrl", "").rstrip("/")
    api_key = provider.get("apiKey", "")
    result = {"ok": False, "errorType": None, "message": ""}

    if is_ollama_provider(provider) or pid in ("openrouter", "openai", "anthropic", "deepseek"):
        try:
            if is_ollama_provider(provider):
                url = f"{base_url}/api/tags"
                headers = {"Content-Type": "application/json"}
            else:
                url_base = base_url
                if url_base.endswith("/v1"):
                    url_base = url_base[:-3].rstrip("/")
                url = f"{url_base}/v1/models"
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                result.update({"ok": True, "message": "Connected"})
            elif resp.status_code in (401, 403):
                result.update({"errorType": "auth", "message": "Authentication failed"})
            else:
                result.update({"errorType": "unreachable", "message": f"Provider returned {resp.status_code}"})
        except requests.exceptions.Timeout:
            result.update({"errorType": "timeout", "message": f"Connection timed out ({timeout}s)"})
        except requests.exceptions.ConnectionError:
            result.update({"errorType": "unreachable", "message": "Provider unreachable"})
        except Exception:
            result.update({"errorType": "unreachable", "message": "Provider unreachable"})
    else:
        # Custom provider: keyless connectivity check only
        try:
            requests.head(base_url, timeout=timeout)
            result.update({"ok": True, "message": "Host reachable (key not verified)"})
        except requests.exceptions.Timeout:
            result.update({"errorType": "timeout", "message": f"Connection timed out ({timeout}s)"})
        except requests.exceptions.ConnectionError:
            result.update({"errorType": "unreachable", "message": "Host unreachable"})
        except Exception:
            result.update({"errorType": "unreachable", "message": "Host unreachable"})
    return result


def _ollama_headers(provider: dict) -> dict:
    """Auth + custom headers for an Ollama request (same as build_ollama_payload)."""
    headers = {"Content-Type": "application/json"}
    api_key = provider.get("apiKey", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for k, v in (provider.get("customHeaders") or {}).items():
        headers[k] = v
    return headers


def check_model_liveness(provider: dict, model_id: str, attempts: int = 3, timeout: int = 5) -> Tuple[str, str]:  # noqa: C901
    """Probe whether *model_id* on *provider* responds. Returns ``(state, detail)`` where
    ``state`` is ``"live"``, ``"dead"``, or ``"inconclusive"``. Never raises.

    Ollama: ``POST /api/show`` (auth-headed) — 200 live, 410/404 dead, else inconclusive.
    Custom (llama.cpp): keyless ``GET /health`` — 200 live, anything else inconclusive
    (503 = model still loading, not dead). Cloud builtins: inconclusive, no network call.
    Retries up to *attempts*: returns ``live`` the instant any attempt succeeds.
    """
    pid = provider.get("id", "")
    base = provider.get("baseUrl", "").rstrip("/")

    if is_ollama_provider(provider):
        url = f"{base}/api/show"
        headers = _ollama_headers(provider)
        saw_dead = False
        for _ in range(attempts):
            try:
                resp = requests.post(url, headers=headers, json={"model": model_id}, timeout=timeout)
                if resp.status_code == 200:
                    return ("live", f"{model_id} on '{pid}' answered /api/show")
                if resp.status_code in (404, 410):
                    saw_dead = True
            except Exception:
                pass
        if saw_dead:
            return ("dead", "/api/show returned 410/404 (the model is likely deprecated or removed)")
        return ("inconclusive", f"could not confirm '{model_id}' on '{pid}'")

    if pid.startswith("custom-"):
        root = base[:-3].rstrip("/") if base.endswith("/v1") else base
        url = f"{root}/health"
        for _ in range(attempts):
            try:
                if requests.get(url, timeout=timeout).status_code == 200:
                    return ("live", f"'{pid}' /health answered 200")
            except Exception:
                pass
        return ("inconclusive", f"could not confirm '{pid}' via /health")

    # Cloud builtins (openrouter/openai/anthropic/deepseek): no cheap check — do not probe.
    return ("inconclusive", f"no liveness probe available for provider '{pid}'")


def preflight_roles(provider_config: ProviderConfig, roles: Tuple[str, ...] = ("brain", "eyes")) -> Tuple[bool, str]:
    """Confirm each role has a usable model before a run does any generation.

    Walks each role's candidate chain (primary + fallbackOrder), probing unique
    ``(provider_id, model_id)`` pairs. A role passes if any candidate is ``live`` or
    ``inconclusive``; it fails only if the whole chain is ``dead``. Unresolvable ids are
    treated as ``inconclusive`` (``call_llm`` would still run them via the legacy env
    fallback), so a stale config never crashes this check. Returns ``(ok, detail)``.
    """
    cache: dict = {}
    for role in roles:
        role_cfg = provider_config.get_role(role)
        candidate_ids = [role_cfg["providerId"]] + role_cfg.get("fallbackOrder", [])
        states, dead_detail = [], ""
        for pid in candidate_ids:
            try:
                provider = provider_config.resolve(pid)
                model_id = provider_config.get_model_id(provider)
            except (KeyError, FileNotFoundError):
                states.append("inconclusive")  # legacy env fallback would still run it
                continue
            key = (pid, model_id)
            if key not in cache:
                cache[key] = check_model_liveness(provider, model_id)
            state, detail = cache[key]
            states.append(state)
            if state == "dead" and not dead_detail:
                dead_detail = f"Model '{model_id}' for role '{role}' (provider '{pid}') is unavailable — {detail}"
        if "live" not in states and "inconclusive" not in states:
            return (False, dead_detail)
    return (True, "")
