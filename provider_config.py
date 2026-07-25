"""Provider config module — reads providers.json, resolves providers by role."""

import json
import os

BUILTIN_PROVIDERS = [
    {
        "id": "ollama",
        "name": "Ollama",
        "enabled": True,
        "type": "builtin",
        "baseUrl": "http://localhost:11434",
        "apiKey": "",
        "modelId": None,  # None means "use global MODEL_ID"
        "customHeaders": {},
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "enabled": False,
        "type": "builtin",
        "baseUrl": "https://openrouter.ai/api",
        "apiKey": "",
        "modelId": None,
        "customHeaders": {},
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "enabled": False,
        "type": "builtin",
        "baseUrl": "https://api.openai.com",
        "apiKey": "",
        "modelId": None,
        "customHeaders": {},
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "enabled": False,
        "type": "builtin",
        "baseUrl": "https://api.anthropic.com",
        "apiKey": "",
        "modelId": None,
        "customHeaders": {},
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "enabled": False,
        "type": "builtin",
        "baseUrl": "https://api.deepseek.com",
        "apiKey": "",
        "modelId": None,
        "customHeaders": {},
    },
]

DEFAULT_ROLES = {
    "brain": {"providerId": "ollama", "fallbackOrder": []},
    "eyes": {"providerId": "ollama", "fallbackOrder": []},
}

GLOBAL_MODEL_ID = "gemma4:31b-cloud"


def _config_path():
    """Path to providers.json relative to this file's directory."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.json")


def mask_key(key: str) -> str:
    """Mask an API key for display: first 4 ... last 4, or **** if short."""
    if not key:
        return ""
    if len(key) <= 12:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def is_key_required(provider: dict) -> bool:
    """Whether this provider requires an API key."""
    pid = provider.get("id", "")
    return pid not in ("ollama",)


class ProviderConfig:
    """Reads and resolves provider configuration from providers.json."""

    def __init__(self, config_path: str = None):
        self.config_path = config_path or _config_path()
        self._config = None
        self._reload()

    def _reload(self):
        """Load config from disk, falling back to builtins."""
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
            self._config = data
        except (FileNotFoundError, json.JSONDecodeError):
            self._config = {"providers": BUILTIN_PROVIDERS, "roles": DEFAULT_ROLES}

    @property
    def raw(self) -> dict:
        return self._config

    def resolve(self, provider_id: str) -> dict:
        """Look up a provider by id. Raises KeyError if not found."""
        for p in self._config.get("providers", []):
            if p.get("id") == provider_id:
                return p
        raise KeyError(f"Provider '{provider_id}' not found")

    def enabled_providers(self) -> list:
        """Return list of enabled provider dicts."""
        return [p for p in self._config.get("providers", []) if p.get("enabled")]

    def get_role(self, role: str) -> dict:
        """Get role config (providerId + fallbackOrder)."""
        return self._config.get("roles", DEFAULT_ROLES).get(role, {"providerId": "ollama", "fallbackOrder": []})

    def get_model_id(self, provider: dict) -> str:
        """Resolve model ID: provider-level override, then global default."""
        mid = provider.get("modelId")
        if mid:
            return mid
        return GLOBAL_MODEL_ID

    def save(self, config: dict):
        """Write config to disk with mode 0600."""
        fd = os.open(self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(config, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        self._config = config

    @staticmethod
    def default_config() -> dict:
        """Return a fresh default config dict."""
        import copy

        return {"providers": copy.deepcopy(BUILTIN_PROVIDERS), "roles": copy.deepcopy(DEFAULT_ROLES)}
