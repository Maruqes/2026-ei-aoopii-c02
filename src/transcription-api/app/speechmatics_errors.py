"""Only concrete provider responses can exhaust a key; budgets are advisory."""

from __future__ import annotations

import threading


class ProviderError(RuntimeError):
    def __init__(self, kind: str):
        super().__init__(kind)  # Never include a provider body, URL or credentials.
        self.kind = kind


class NoCredits(ProviderError):
    def __init__(self):
        super().__init__("no_credits")


def error_kind(error) -> str:
    # The Batch SDK wraps TransportError in BatchError; inspect structured causes only.
    for _ in range(8):
        if isinstance(error, ProviderError):
            return error.kind
        code = getattr(
            getattr(error, "rcvd", None), "code", getattr(error, "code", None)
        )
        if code == 4006:
            return "no_credits"
        if code == 4005:
            return "capacity"
        if code in {4001, 4003, 4004}:
            return "invalid_key"
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None) or getattr(
            error, "status_code", None
        )
        if status == 402:
            return "no_credits"
        if status in {401, 403}:
            return "invalid_key"
        if status == 429:
            return "capacity"
        if (
            type(error).__module__.startswith("speechmatics.")
            and type(error).__name__ == "AuthenticationError"
        ):
            return "invalid_key"
        error = getattr(error, "__cause__", None)
        if error is None:
            break
    return "recoverable"


def message_error(message: dict) -> ProviderError:
    kind = message.get("type")
    if kind == "timelimit_exceeded":
        return ProviderError("no_credits")
    if kind == "quota_exceeded":
        return ProviderError("capacity")
    if kind in {
        "not_authorised",
        "not_allowed",
        "invalid_model",
        "invalid_language",
        "invalid_config",
        "invalid_audio_type",
    }:
        return ProviderError("invalid_key")
    return ProviderError("recoverable")


class KeyHealth:
    def __init__(self):
        self.lock = threading.Lock()
        self.states: dict[str, str] = {}

    def mark(self, secret: str, kind: str) -> None:
        if kind in {"no_credits", "invalid_key"}:
            with self.lock:
                self.states[secret] = kind

    def healthy(self, secret: str) -> bool:
        with self.lock:
            return secret not in self.states

    def exhausted(self, keys) -> bool:
        with self.lock:
            return bool(keys) and all(
                self.states.get(key.value) == "no_credits" for key in keys
            )

    def reset(self, keys) -> None:
        with self.lock:
            for key in keys:
                self.states.pop(key.value, None)


key_health = KeyHealth()
