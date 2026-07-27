"""Tests for llm_client: payload dialects, image encoding, role routing, fallback, retry."""

import base64
import json
import os
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_client
from provider_config import ProviderConfig


def _write_config(mutate):
    config = ProviderConfig.default_config()
    mutate(config)
    tf = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    json.dump(config, tf)
    tf.close()
    return tf.name


def test_build_ollama_payload():
    print("  test_build_ollama_payload...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    messages = [{"role": "user", "content": "hello"}]
    url, headers, payload = llm_client.build_ollama_payload(provider, messages, "gemma4:9b")
    assert url == "http://localhost:11434/api/chat"
    assert payload["model"] == "gemma4:9b"
    assert payload["stream"] is True
    assert "format" not in payload  # plain text -> field omitted; "text" is not a valid Ollama format
    assert payload["messages"] == messages
    url, headers, payload = llm_client.build_ollama_payload(provider, messages, "gemma4:9b", response_format="json")
    assert payload["format"] == "json"
    print("✅")


def test_ollama_payload_omits_text_format():
    """Regression: Ollama's local inference path rejects format:"text" mid-stream.

    Only "json" or a JSON Schema are valid; the field must be absent for plain text.
    The cloud proxy ignores `format`, so this only ever bit locally-served models.
    """
    print("  test_ollama_payload_omits_text_format...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    _, _, payload = llm_client.build_ollama_payload(provider, [], "qwen3.5:4b", response_format="text")
    assert "format" not in payload
    _, _, payload = llm_client.build_ollama_payload(provider, [], "qwen3.5:4b")  # default is "text"
    assert "format" not in payload
    print("✅")


def test_ollama_payload_disables_thinking_by_default():
    """Thinking tokens are discarded by _consume_stream_line, so they only burn the deadline.

    Some thinking-capable models (e.g. qwen3.5:4b) never emit content at all with thinking
    on. Default it off; a provider may opt back in with "think": true.
    """
    print("  test_ollama_payload_disables_thinking_by_default...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    _, _, payload = llm_client.build_ollama_payload(provider, [], "qwen3.5:4b")
    assert payload["think"] is False
    _, _, payload = llm_client.build_ollama_payload({**provider, "think": True}, [], "qwen3.5:4b")
    assert payload["think"] is True
    print("✅")


def test_build_openai_compatible_payload():
    print("  test_build_openai_compatible_payload...", end=" ")
    provider = {
        "id": "openrouter",
        "baseUrl": "https://openrouter.ai/api",
        "apiKey": "sk-test-key",
        "customHeaders": {"HTTP-Referer": "http://localhost"},
    }
    messages = [{"role": "user", "content": "hello"}]
    url, headers, payload = llm_client.build_openai_compatible_payload(provider, messages, "anthropic/claude-3.5-sonnet")
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert headers["Content-Type"] == "application/json"
    assert headers["HTTP-Referer"] == "http://localhost"
    assert payload["model"] == "anthropic/claude-3.5-sonnet"
    assert payload["messages"] == messages
    print("✅")


def test_v1_suffix_stripped_exactly():
    print("  test_v1_suffix_stripped_exactly...", end=" ")
    # Regression: the old copies used base[:-4], eating one extra character.
    provider = {"id": "custom-x", "baseUrl": "http://myhost:9000/v1", "apiKey": "", "customHeaders": {}}
    url, _, _ = llm_client.build_openai_compatible_payload(provider, [], "m")
    assert url == "http://myhost:9000/v1/chat/completions", f"got {url}"
    print("✅")


def test_image_encoding_per_dialect():
    print("  test_image_encoding_per_dialect...", end=" ")
    b64 = base64.b64encode(b"fakepng").decode("utf-8")
    ollama = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    openai_like = {"id": "openai", "baseUrl": "https://api.openai.com", "apiKey": "k", "customHeaders": {}}

    msgs = llm_client._build_messages(ollama, "look", "sys", b64)
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[1]["content"] == "look"
    assert msgs[1]["images"] == [b64]

    msgs = llm_client._build_messages(openai_like, "look", "sys", b64)
    content = msgs[1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{b64}"

    # No image, no system prompt -> plain user message only
    msgs = llm_client._build_messages(openai_like, "hi", None, None)
    assert msgs == [{"role": "user", "content": "hi"}]
    print("✅")


def test_role_routing():
    print("  test_role_routing...", end=" ")
    tf_path = _write_config(
        lambda c: (
            c["providers"][1].update(enabled=True),
            c["roles"]["brain"].update(providerId="openrouter"),
            c["roles"]["eyes"].update(providerId="ollama"),
        )
    )
    call_log = []
    original = llm_client._do_api_call

    def mock_api_call(provider, messages, model_id, response_format="text", timeout=300, retries=3):
        call_log.append({"provider_id": provider["id"], "model_id": model_id})
        return "mocked response"

    llm_client._do_api_call = mock_api_call
    try:
        cfg = ProviderConfig(tf_path)
        result = llm_client.call_llm("brain", "test prompt", provider_config=cfg)
        assert result == "mocked response"
        assert call_log[0]["provider_id"] == "openrouter"
        call_log.clear()
        llm_client.call_llm("eyes", "test prompt", provider_config=cfg)
        assert call_log[0]["provider_id"] == "ollama"
        print("✅")
    finally:
        llm_client._do_api_call = original
        os.remove(tf_path)


def test_fallback_on_failure():
    print("  test_fallback_on_failure...", end=" ")
    tf_path = _write_config(
        lambda c: (
            c["providers"][1].update(enabled=True),
            c["roles"]["brain"].update(providerId="openrouter", fallbackOrder=["ollama"]),
        )
    )
    call_log = []
    original = llm_client._do_api_call

    def mock_api_call(provider, messages, model_id, response_format="text", timeout=300, retries=3):
        call_log.append(provider["id"])
        if provider["id"] == "openrouter":
            raise Exception("OpenRouter down")
        return "mocked response"

    llm_client._do_api_call = mock_api_call
    try:
        cfg = ProviderConfig(tf_path)
        result = llm_client.call_llm("brain", "test prompt", provider_config=cfg)
        assert result == "mocked response"
        assert call_log == ["openrouter", "ollama"]
        print("✅")
    finally:
        llm_client._do_api_call = original
        os.remove(tf_path)


def test_all_fallbacks_exhausted():
    print("  test_all_fallbacks_exhausted...", end=" ")
    tf_path = _write_config(
        lambda c: (
            c["providers"][1].update(enabled=True),
            c["providers"][2].update(enabled=True),
            c["roles"]["brain"].update(providerId="openrouter", fallbackOrder=["openai"]),
        )
    )
    call_log = []
    original = llm_client._do_api_call

    def mock_api_call(provider, messages, model_id, response_format="text", timeout=300, retries=3):
        call_log.append(provider["id"])
        raise Exception(f"{provider['id']} down")

    llm_client._do_api_call = mock_api_call
    try:
        cfg = ProviderConfig(tf_path)
        try:
            llm_client.call_llm("brain", "test prompt", provider_config=cfg)
            assert False, "Should have raised"
        except Exception:
            pass
        assert call_log == ["openrouter", "openai"]
        print("✅")
    finally:
        llm_client._do_api_call = original
        os.remove(tf_path)


def test_ollama_auth_header():
    """Regression: hosted/cloud Ollama needs Authorization when apiKey is set."""
    print("  test_ollama_auth_header...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "sk-cloud", "customHeaders": {}}
    _, headers, _ = llm_client.build_ollama_payload(provider, [], "m")
    assert headers["Authorization"] == "Bearer sk-cloud"
    provider["apiKey"] = ""
    _, headers, _ = llm_client.build_ollama_payload(provider, [], "m")
    assert "Authorization" not in headers
    print("✅")


def test_openai_no_empty_auth_header():
    """No apiKey -> no Authorization header (not an empty 'Bearer ')."""
    print("  test_openai_no_empty_auth_header...", end=" ")
    provider = {"id": "openai", "baseUrl": "https://api.openai.com", "apiKey": "", "customHeaders": {}}
    _, headers, _ = llm_client.build_openai_compatible_payload(provider, [], "m")
    assert "Authorization" not in headers
    print("✅")


def test_probe_builtin_v1_strip_and_auth_mapping():
    """Probe must strip /v1 exactly and map 401/403 to errorType 'auth'."""
    print("  test_probe_builtin_v1_strip_and_auth_mapping...", end=" ")
    calls = {}

    class Resp:
        status_code = 401

    def fake_get(url, headers=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        return Resp()

    original_get = llm_client.requests.get
    llm_client.requests.get = fake_get
    try:
        r = llm_client.probe_provider({"id": "openai", "baseUrl": "https://api.openai.com/v1", "apiKey": "sk-x", "customHeaders": {}})
        assert calls["url"] == "https://api.openai.com/v1/models", f"got {calls['url']}"
        assert calls["headers"]["Authorization"] == "Bearer sk-x"
        assert r["ok"] is False
        assert r["errorType"] == "auth"
        print("✅")
    finally:
        llm_client.requests.get = original_get


def test_probe_custom_provider_keyless_head():
    """Custom providers get a keyless HEAD check — the key is never sent to unverified hosts."""
    print("  test_probe_custom_provider_keyless_head...", end=" ")
    calls = {}

    class Resp:
        status_code = 200

    def fake_head(url, timeout=None):
        calls["url"] = url
        return Resp()

    def forbidden_get(*args, **kwargs):
        raise AssertionError("custom providers must not receive an authenticated GET")

    original_head = llm_client.requests.head
    original_get = llm_client.requests.get
    llm_client.requests.head = fake_head
    llm_client.requests.get = forbidden_get
    try:
        r = llm_client.probe_provider({"id": "my-gateway", "baseUrl": "http://gw.internal:9000", "apiKey": "secret", "customHeaders": {}})
        assert calls["url"] == "http://gw.internal:9000"
        assert r["ok"] is True
        assert "key not verified" in r["message"]
        print("✅")
    finally:
        llm_client.requests.head = original_head
        llm_client.requests.get = original_get


def test_disabled_provider_still_attempted():
    """A role's provider is used even when enabled=false (pre-refactor behavior)."""
    print("  test_disabled_provider_still_attempted...", end=" ")
    tf_path = _write_config(
        lambda c: (
            c["roles"]["brain"].update(providerId="openrouter"),  # openrouter is disabled by default
        )
    )
    original = llm_client._do_api_call

    def mock_api_call(provider, messages, model_id, response_format="text", timeout=300, retries=3):
        return f"answered by {provider['id']}"

    llm_client._do_api_call = mock_api_call
    try:
        cfg = ProviderConfig(tf_path)
        result = llm_client.call_llm("brain", "hi", provider_config=cfg)
        assert result == "answered by openrouter", f"got {result!r}"
        print("✅")
    finally:
        llm_client._do_api_call = original
        os.remove(tf_path)


def test_retry_on_timeout():
    """_do_api_call retries timeouts with backoff and succeeds on a later attempt."""
    print("  test_retry_on_timeout...", end=" ")
    import requests as _requests

    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    attempts = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=1):
            yield b'{"message":{"content":"ok after retry"},"done":true}\n'

        def json(self):  # retained; unused by the streaming path
            return {"message": {"content": "ok after retry"}}

        def close(self):
            pass

    def fake_post(url, headers=None, json=None, timeout=None, stream=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise _requests.exceptions.ReadTimeout("slow")
        return FakeResponse()

    original_post = llm_client.requests.post
    original_sleep = llm_client.time.sleep
    llm_client.requests.post = fake_post
    llm_client.time.sleep = lambda s: None
    try:
        result = llm_client._do_api_call(provider, [{"role": "user", "content": "x"}], "m")
        assert result == "ok after retry"
        assert len(attempts) == 3
        print("✅")
    finally:
        llm_client.requests.post = original_post
        llm_client.time.sleep = original_sleep


def test_legacy_env_fallback_on_unresolvable_role():
    """A role pointing at a nonexistent provider falls back to the legacy env provider."""
    print("  test_legacy_env_fallback_on_unresolvable_role...", end=" ")
    tf_path = _write_config(lambda c: (c["roles"]["brain"].update(providerId="ghost-provider"),))
    seen = {}
    original = llm_client._do_api_call

    def mock_api_call(provider, messages, model_id, response_format="text", timeout=300, retries=3):
        seen["provider"] = provider
        return "legacy ok"

    llm_client._do_api_call = mock_api_call
    try:
        cfg = ProviderConfig(tf_path)
        result = llm_client.call_llm("brain", "hi", provider_config=cfg)
        assert result == "legacy ok"
        assert seen["provider"]["baseUrl"] == llm_client.config.OLLAMA_HOST
        print("✅")
    finally:
        llm_client._do_api_call = original
        os.remove(tf_path)


def test_call_llm_default_timeout_and_retries():
    """Hygiene: call_llm defaults are timeout=600, retries=1 (no 300s/3x stalls)."""
    print("  test_call_llm_default_timeout_and_retries...", end=" ")
    import inspect

    sig = inspect.signature(llm_client.call_llm)
    assert sig.parameters["timeout"].default == 600, sig.parameters["timeout"].default
    assert sig.parameters["retries"].default == 1, sig.parameters["retries"].default
    print("✅")


def test_call_llm_default_single_attempt():
    """call_llm's default retries=1 makes exactly one provider attempt (no retry).

    Uses the default brain role -> ollama with fallbackOrder=[] so there is a single
    candidate provider; mocks requests.post to raise ReadTimeout and asserts it is
    called once. This exercises call_llm's DEFAULTS (not _do_api_call's).
    """
    print("  test_call_llm_default_single_attempt...", end=" ")
    import requests as _requests

    tf_path = _write_config(lambda c: c["roles"]["brain"].update(providerId="ollama", fallbackOrder=[]))
    posts = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None, stream=None):
        posts["n"] += 1
        raise _requests.exceptions.ReadTimeout("slow")

    original_post = llm_client.requests.post
    original_sleep = llm_client.time.sleep
    llm_client.requests.post = fake_post
    llm_client.time.sleep = lambda s: None
    try:
        cfg = ProviderConfig(tf_path)
        try:
            llm_client.call_llm("brain", "hi", provider_config=cfg)  # default timeout/retries
            assert False, "should have raised ReadTimeout"
        except _requests.exceptions.ReadTimeout:
            pass
        assert posts["n"] == 1, f"expected exactly 1 attempt, got {posts['n']}"
    finally:
        llm_client.requests.post = original_post
        llm_client.time.sleep = original_sleep
        os.remove(tf_path)
    print("✅")


def test_consume_stream_line():
    """_consume_stream_line: per-dialect deltas, done flag, and exception-safety."""
    print("  test_consume_stream_line...", end=" ")
    import requests as _requests

    csl = llm_client._consume_stream_line

    # Ollama NDJSON: content accumulates; done flag is the return value.
    parts = []
    assert csl('{"message":{"content":"He"}}', parts, True) is False
    assert csl('{"message":{"content":"llo"},"done":true}', parts, True) is True
    assert "".join(parts) == "Hello"

    # Ollama: unparseable line and blank line are skipped (no raise, no append).
    parts = []
    assert csl("not json at all", parts, True) is False
    assert csl("", parts, True) is False
    assert parts == []

    # Ollama: an explicit error record raises ConnectionError.
    try:
        csl('{"error":"model gone"}', [], True)
        assert False, "expected ConnectionError"
    except _requests.exceptions.ConnectionError:
        pass

    # SSE: only 'data:' lines are parsed; comments, blank lines, [DONE] handled.
    parts = []
    assert csl(": OPENROUTER PROCESSING", parts, False) is False  # SSE comment keepalive
    assert csl("", parts, False) is False
    assert csl('data: {"choices":[{"delta":{"content":"Hi"}}]}', parts, False) is False
    assert csl("data: [DONE]", parts, False) is True
    assert "".join(parts) == "Hi"

    # SSE: empty-choices keepalive chunk is skipped, not an IndexError.
    parts = []
    assert csl('data: {"choices":[]}', parts, False) is False
    assert parts == []

    # SSE: an error record raises ConnectionError.
    try:
        csl('data: {"error":{"message":"bad"}}', [], False)
        assert False, "expected ConnectionError"
    except _requests.exceptions.ConnectionError:
        pass

    # Invariant: valid-JSON-non-object and structurally-weird lines must be
    # SKIPPED (return False), never raise anything other than ConnectionError.
    parts = []
    assert csl("[1,2,3]", parts, True) is False  # ollama top-level list
    assert csl("data: null", parts, False) is False  # SSE top-level null
    assert csl('{"message":"str"}', parts, True) is False  # ollama message not a dict
    assert csl('data: {"choices":"x"}', parts, False) is False  # SSE choices not a list
    assert parts == []
    print("✅")


class _FakeStream:
    """Fake streaming requests.Response for _stream_api_call / _do_api_call tests."""

    status_code = 200

    def __init__(self, chunks=None, iter_exc=None):
        self._chunks = chunks or []
        self._iter_exc = iter_exc
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        if self._iter_exc is not None:
            raise self._iter_exc
        for c in self._chunks:
            yield c

    def close(self):
        self.closed = True


def test_stream_assembly_ollama():
    """Ollama NDJSON deltas across chunk boundaries assemble to the full content."""
    print("  test_stream_assembly_ollama...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    fake = _FakeStream(
        chunks=[
            b'{"message":{"content":"He"}}\n{"message":{"content":"ll',
            b'o"}}\n{"message":{"content":"!"},"done":true}\n',
        ]
    )
    original = llm_client.requests.post
    llm_client.requests.post = lambda *a, **k: fake
    try:
        result = llm_client._do_api_call(provider, [{"role": "user", "content": "x"}], "m")
        assert result == "Hello!", result
        assert fake.closed
    finally:
        llm_client.requests.post = original
    print("✅")


def test_stream_assembly_openai():
    """OpenAI SSE deltas assemble; comments, blank lines and [DONE] are tolerated."""
    print("  test_stream_assembly_openai...", end=" ")
    provider = {"id": "openai", "baseUrl": "https://api.openai.com/v1", "apiKey": "k", "customHeaders": {}}
    fake = _FakeStream(
        chunks=[
            b": OPENROUTER PROCESSING\n\n",
            b'data: {"choices":[{"delta":{"content":"He"}}]}\n',
            b'data: {"choices":[]}\n',  # empty-choices keepalive
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n',
            b"data: [DONE]\n",
        ]
    )
    original = llm_client.requests.post
    llm_client.requests.post = lambda *a, **k: fake
    try:
        result = llm_client._do_api_call(provider, [{"role": "user", "content": "x"}], "m")
        assert result == "Hello", result
        assert fake.closed
    finally:
        llm_client.requests.post = original
    print("✅")


def test_stream_multibyte_across_chunks():
    """A multibyte char split across byte-chunks decodes intact (bytes buffered, split on \\n)."""
    print("  test_stream_multibyte_across_chunks...", end=" ")
    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    line = '{"message":{"content":"café"},"done":true}\n'.encode("utf-8")
    fake = _FakeStream(chunks=[line[k : k + 1] for k in range(len(line))])  # one byte per chunk
    original = llm_client.requests.post
    llm_client.requests.post = lambda *a, **k: fake
    try:
        result = llm_client._do_api_call(provider, [{"role": "user", "content": "x"}], "m")
        assert result == "café", repr(result)
    finally:
        llm_client.requests.post = original
    print("✅")


def test_stream_total_deadline():
    """A stream that never finishes raises ReadTimeout at TOTAL and closes the response."""
    print("  test_stream_total_deadline...", end=" ")
    import requests as _requests

    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    fake = _FakeStream(chunks=[b'{"message":{"content":""}}\n'])  # one chunk is enough

    vals = iter([0.0])  # first monotonic() -> 0 (start); subsequent -> huge

    def fake_monotonic():
        try:
            return next(vals)
        except StopIteration:
            return 10_000.0

    original_post = llm_client.requests.post
    original_mono = llm_client.time.monotonic
    original_sleep = llm_client.time.sleep
    llm_client.requests.post = lambda *a, **k: fake
    llm_client.time.monotonic = fake_monotonic
    llm_client.time.sleep = lambda s: None
    try:
        try:
            llm_client._do_api_call(provider, [{"role": "user", "content": "x"}], "m", timeout=1, retries=1)
            assert False, "expected ReadTimeout"
        except _requests.exceptions.ReadTimeout:
            pass
        assert fake.closed
    finally:
        llm_client.requests.post = original_post
        llm_client.time.monotonic = original_mono
        llm_client.time.sleep = original_sleep
    print("✅")


def test_stream_chunked_encoding_normalized():
    """A mid-stream ChunkedEncodingError is normalized to ConnectionError (loop.py catches it)."""
    print("  test_stream_chunked_encoding_normalized...", end=" ")
    import requests as _requests

    provider = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    fake = _FakeStream(iter_exc=_requests.exceptions.ChunkedEncodingError("mid-stream disconnect"))

    original_post = llm_client.requests.post
    original_sleep = llm_client.time.sleep
    llm_client.requests.post = lambda *a, **k: fake
    llm_client.time.sleep = lambda s: None
    try:
        try:
            llm_client._do_api_call(provider, [{"role": "user", "content": "x"}], "m", retries=1)
            assert False, "expected ConnectionError"
        except _requests.exceptions.ConnectionError:
            pass
        except _requests.exceptions.ChunkedEncodingError:
            assert False, "ChunkedEncodingError escaped un-normalized"
        assert fake.closed
    finally:
        llm_client.requests.post = original_post
        llm_client.time.sleep = original_sleep
    print("✅")


def test_stream_nonstreaming_body_fallback():
    """A provider that ignores stream:true and returns a whole completion body is still parsed."""
    print("  test_stream_nonstreaming_body_fallback...", end=" ")

    # OpenAI-compatible dialect: non-SSE completion body (no 'data:' lines).
    openai = {"id": "openai", "baseUrl": "https://api.example.com/v1", "apiKey": "k", "customHeaders": {}}
    fake = _FakeStream(chunks=[b'{"choices":[{"message":{"content":"hello world"}}]}'])
    original = llm_client.requests.post
    llm_client.requests.post = lambda *a, **k: fake
    try:
        result = llm_client._do_api_call(openai, [{"role": "user", "content": "x"}], "m")
        assert result == "hello world", repr(result)
    finally:
        llm_client.requests.post = original

    # Ollama dialect: pretty-printed (multi-line) non-streamed body -> line-by-line
    # NDJSON parsing yields nothing, so the whole-body fallback recovers it.
    ollama = {"id": "ollama", "baseUrl": "http://x", "apiKey": "", "customHeaders": {}}
    fake2 = _FakeStream(chunks=[b'{\n  "message": {\n    "content": "hi there"\n  }\n}'])
    llm_client.requests.post = lambda *a, **k: fake2
    try:
        result = llm_client._do_api_call(ollama, [{"role": "user", "content": "x"}], "m")
        assert result == "hi there", repr(result)
    finally:
        llm_client.requests.post = original
    print("✅")


def test_do_api_call_retries_knob(monkeypatch):
    print("  test_do_api_call_retries_knob...", end=" ")
    import requests

    import llm_client

    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("simulated")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    provider = {"baseUrl": "http://localhost:11434", "type": "ollama"}
    try:
        llm_client._do_api_call(provider, [{"role": "user", "content": "x"}], "m", retries=1)
        assert False, "should have raised"
    except requests.exceptions.ReadTimeout:
        pass
    assert calls["n"] == 1, f"expected exactly 1 attempt, got {calls['n']}"
    print("✅")


class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_check_model_liveness_ollama_states():
    print("  test_check_model_liveness_ollama_states...", end=" ")
    import requests as _requests

    prov = {"id": "ollama", "baseUrl": "http://localhost:11434", "apiKey": "", "customHeaders": {}}
    orig = llm_client.requests.post
    try:
        for code, expected in [(200, "live"), (410, "dead"), (404, "dead"), (401, "inconclusive"), (500, "inconclusive")]:
            llm_client.requests.post = (lambda c: lambda *a, **k: _Resp(c))(code)
            assert llm_client.check_model_liveness(prov, "m")[0] == expected, code

        def raise_conn(*a, **k):
            raise _requests.exceptions.ConnectionError("x")

        llm_client.requests.post = raise_conn
        assert llm_client.check_model_liveness(prov, "m")[0] == "inconclusive"
    finally:
        llm_client.requests.post = orig
    print("✅")


def test_check_model_liveness_ollama_sends_auth():
    print("  test_check_model_liveness_ollama_sends_auth...", end=" ")
    prov = {"id": "ollama", "baseUrl": "http://h:11434/", "apiKey": "k", "customHeaders": {"X-Env": "prod"}}
    seen = {}

    def fake(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, json=json)
        return _Resp(200)

    orig = llm_client.requests.post
    llm_client.requests.post = fake
    try:
        assert llm_client.check_model_liveness(prov, "gemma")[0] == "live"
        assert seen["url"] == "http://h:11434/api/show"
        assert seen["headers"]["Authorization"] == "Bearer k"
        assert seen["headers"]["X-Env"] == "prod"
        assert seen["json"] == {"model": "gemma"}
    finally:
        llm_client.requests.post = orig
    print("✅")


def test_check_model_liveness_custom_health():
    print("  test_check_model_liveness_custom_health...", end=" ")
    import requests as _requests

    prov = {"id": "custom-123", "baseUrl": "http://localhost:8080/v1", "apiKey": "k", "customHeaders": {}}
    seen = {}

    def get_code(code):
        def f(url, timeout=None, **kw):
            seen.update(url=url, kw=kw)
            return _Resp(code)

        return f

    og, op = llm_client.requests.get, llm_client.requests.post
    try:
        llm_client.requests.get = get_code(200)
        assert llm_client.check_model_liveness(prov, "m")[0] == "live"
        assert seen["url"] == "http://localhost:8080/health"  # /v1 stripped, /health appended
        assert seen["kw"].get("headers") is None  # keyless
        for code in (503, 404):
            llm_client.requests.get = get_code(code)
            assert llm_client.check_model_liveness(prov, "m")[0] == "inconclusive", code

        def raise_conn(*a, **k):
            raise _requests.exceptions.ConnectionError("x")

        llm_client.requests.get = raise_conn
        assert llm_client.check_model_liveness(prov, "m")[0] == "inconclusive"
    finally:
        llm_client.requests.get, llm_client.requests.post = og, op
    print("✅")


def test_check_model_liveness_cloud_skips():
    print("  test_check_model_liveness_cloud_skips...", end=" ")
    prov = {"id": "openrouter", "baseUrl": "https://openrouter.ai/api", "apiKey": "k", "customHeaders": {}}

    def boom(*a, **k):
        raise AssertionError("cloud builtin must not be probed")

    op, og = llm_client.requests.post, llm_client.requests.get
    llm_client.requests.post = llm_client.requests.get = boom
    try:
        assert llm_client.check_model_liveness(prov, "m")[0] == "inconclusive"
    finally:
        llm_client.requests.post, llm_client.requests.get = op, og
    print("✅")


def test_check_model_liveness_retry_live_if_any():
    print("  test_check_model_liveness_retry_live_if_any...", end=" ")
    import requests as _requests

    prov = {"id": "ollama", "baseUrl": "http://x", "apiKey": "", "customHeaders": {}}
    orig = llm_client.requests.post
    try:
        seq = [410, 410, 200]
        i = {"n": 0}

        def flap(*a, **k):
            r = _Resp(seq[i["n"]])
            i["n"] += 1
            return r

        llm_client.requests.post = flap
        assert llm_client.check_model_liveness(prov, "m", attempts=3)[0] == "live"

        llm_client.requests.post = lambda *a, **k: _Resp(410)
        assert llm_client.check_model_liveness(prov, "m", attempts=3)[0] == "dead"

        def to(*a, **k):
            raise _requests.exceptions.Timeout("x")

        llm_client.requests.post = to
        assert llm_client.check_model_liveness(prov, "m", attempts=3)[0] == "inconclusive"
    finally:
        llm_client.requests.post = orig
    print("✅")


def test_check_model_liveness_null_custom_headers():
    """Never-raises holds when customHeaders is null (present but not a dict)."""
    print("  test_check_model_liveness_null_custom_headers...", end=" ")
    prov = {"id": "ollama", "baseUrl": "http://x", "apiKey": "k", "customHeaders": None}
    orig = llm_client.requests.post
    try:
        llm_client.requests.post = lambda *a, **k: _Resp(200)
        assert llm_client.check_model_liveness(prov, "m")[0] == "live"
    finally:
        llm_client.requests.post = orig
    print("✅")


def test_preflight_roles_dead_then_live():
    print("  test_preflight_roles_dead_then_live...", end=" ")
    tf = _write_config(lambda c: c["providers"][0].update(modelId="gemma3:12b-cloud"))
    orig = llm_client.requests.post
    try:
        llm_client.requests.post = lambda *a, **k: _Resp(410)  # dead
        cfg = ProviderConfig(tf)
        ok, detail = llm_client.preflight_roles(cfg)
        assert ok is False
        assert "gemma3:12b-cloud" in detail and "brain" in detail
        llm_client.requests.post = lambda *a, **k: _Resp(200)  # live
        assert llm_client.preflight_roles(cfg)[0] is True
    finally:
        llm_client.requests.post = orig
        os.remove(tf)
    print("✅")


def test_preflight_roles_inconclusive_passes():
    print("  test_preflight_roles_inconclusive_passes...", end=" ")
    tf = _write_config(lambda c: c)
    orig = llm_client.requests.post
    try:
        llm_client.requests.post = lambda *a, **k: _Resp(500)  # inconclusive, never blocks
        assert llm_client.preflight_roles(ProviderConfig(tf))[0] is True
    finally:
        llm_client.requests.post = orig
        os.remove(tf)
    print("✅")


def test_preflight_roles_unresolvable_no_crash():
    print("  test_preflight_roles_unresolvable_no_crash...", end=" ")
    tf = _write_config(
        lambda c: (
            c["roles"]["brain"].update(providerId="ghost", fallbackOrder=[]),
            c["roles"]["eyes"].update(providerId="ghost", fallbackOrder=[]),
        )
    )
    try:
        # resolve('ghost') raises KeyError -> mapped to inconclusive -> no crash, passes
        assert llm_client.preflight_roles(ProviderConfig(tf))[0] is True
    finally:
        os.remove(tf)
    print("✅")


def test_preflight_roles_dedupe():
    print("  test_preflight_roles_dedupe...", end=" ")
    tf = _write_config(lambda c: c)  # brain & eyes both -> (ollama, GLOBAL_MODEL_ID)
    calls = {"n": 0}
    orig = llm_client.check_model_liveness
    try:

        def counting(provider, model_id, **kw):
            calls["n"] += 1
            return ("live", "ok")

        llm_client.check_model_liveness = counting
        assert llm_client.preflight_roles(ProviderConfig(tf))[0] is True
        assert calls["n"] == 1  # shared (provider, model) probed once
    finally:
        llm_client.check_model_liveness = orig
        os.remove(tf)
    print("✅")


if __name__ == "__main__":
    print("\n=== Multi-Provider LLM Caller Tests ===")
    test_build_ollama_payload()
    test_build_openai_compatible_payload()
    test_v1_suffix_stripped_exactly()
    test_image_encoding_per_dialect()
    test_role_routing()
    test_fallback_on_failure()
    test_all_fallbacks_exhausted()
    test_ollama_auth_header()
    test_openai_no_empty_auth_header()
    test_probe_builtin_v1_strip_and_auth_mapping()
    test_probe_custom_provider_keyless_head()
    test_disabled_provider_still_attempted()
    test_retry_on_timeout()
    test_legacy_env_fallback_on_unresolvable_role()
    test_call_llm_default_timeout_and_retries()
    test_call_llm_default_single_attempt()
    test_consume_stream_line()
    test_stream_assembly_ollama()
    test_stream_assembly_openai()
    test_stream_multibyte_across_chunks()
    test_stream_total_deadline()
    test_stream_chunked_encoding_normalized()
    test_stream_nonstreaming_body_fallback()
    test_check_model_liveness_ollama_states()
    test_check_model_liveness_ollama_sends_auth()
    test_check_model_liveness_custom_health()
    test_check_model_liveness_cloud_skips()
    test_check_model_liveness_retry_live_if_any()
    test_check_model_liveness_null_custom_headers()
    test_preflight_roles_dead_then_live()
    test_preflight_roles_inconclusive_passes()
    test_preflight_roles_unresolvable_no_crash()
    test_preflight_roles_dedupe()
    print("\nAll tests passed ✅")
    # Note: test_do_api_call_retries_knob uses pytest's monkeypatch fixture
    # and is run separately via pytest
