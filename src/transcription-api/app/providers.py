"""Provider order and single-process admission, shared by REST workers and WS."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from urllib.parse import urlsplit

from .speechmatics_errors import key_health

REMOTE_PROVIDERS = ("deepgram", "speechmatics")


def parse_order(value) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else value
    order = tuple(str(item).strip().lower() for item in items)
    if (
        not order
        or any(p not in REMOTE_PROVIDERS for p in order)
        or len(set(order)) != len(order)
    ):
        raise ValueError(
            "Use deepgram,speechmatics; speechmatics,deepgram; or one provider"
        )
    return order


def environment_order(settings) -> tuple[str, ...]:
    if settings.transcription_provider_order:
        return parse_order(settings.transcription_provider_order)
    if settings.transcription_provider not in (*REMOTE_PROVIDERS, "whisper"):
        raise ValueError("Unsupported transcription provider")
    return (settings.transcription_provider,)


def effective_order(settings, repository=None, guild_id=None):
    saved = (
        repository.transcription_order(guild_id) if repository and guild_id else None
    )
    return (
        (tuple(saved), "guild")
        if saved
        else (environment_order(settings), "environment")
    )


@dataclass(frozen=True)
class ProviderKey:
    name: str
    value: str = field(repr=False)
    provider: str = "speechmatics"
    group: str = ""
    streaming_limit: int = 2
    batch_limit: int = 8


class ProviderRegistry:
    # ponytail: process-local counters; multiple API processes require a shared coordinator.
    def __init__(self, settings=None, keys=()):
        self.settings = settings
        self.lock = threading.RLock()
        self.keys = tuple(keys)
        self.leases: dict[str, tuple[ProviderKey, str]] = {}
        self.key_states: dict[tuple[str, str], str] = {}
        self.group_states: dict[tuple[str, str, str], tuple[str, float]] = {}
        self.projects: dict[str, str] = {}
        self.verified: set[str] = set()
        self.management_cache = {}
        self.last_errors = {}
        if settings is not None:
            configured = []
            for provider in REMOTE_PROVIDERS:
                entries = getattr(settings, provider + "_api_keys")
                single = getattr(settings, provider + "_api_key")
                if not entries and single:
                    entries = ((provider.upper() + "_API_KEY", single),)
                seen = set()
                associations = {row[0]: row[1:] for row in settings.deepgram_key_groups}
                for name, secret in entries:
                    secret = secret.strip()
                    if not secret:
                        continue
                    project, streams, batch = associations.get(
                        name,
                        (
                            "",
                            settings.deepgram_streaming_limit,
                            settings.deepgram_batch_limit,
                        ),
                    )
                    if secret in seen:
                        index = next(
                            i
                            for i, k in enumerate(configured)
                            if k.provider == provider and k.value == secret
                        )
                        previous = configured[index]
                        configured[index] = replace(
                            previous,
                            streaming_limit=min(previous.streaming_limit, streams)
                            if provider == "deepgram"
                            else previous.streaming_limit,
                            batch_limit=min(previous.batch_limit, batch)
                            if provider == "deepgram"
                            else previous.batch_limit,
                        )
                        continue
                    seen.add(secret)
                    configured.append(
                        ProviderKey(
                            name,
                            secret,
                            provider,
                            (project or "unverified")
                            if provider == "deepgram"
                            else name,
                            streams if provider == "deepgram" else 2,
                            batch
                            if provider == "deepgram"
                            else settings.transcription_workers,
                        )
                    )
            self.keys = tuple(configured)
            rest, ws = (
                urlsplit(settings.deepgram_api_base_url),
                urlsplit(settings.deepgram_realtime_url),
            )
            if (
                any(k.provider == "deepgram" for k in self.keys)
                and rest.hostname != ws.hostname
            ):
                raise ValueError(
                    "Deepgram REST and WebSocket endpoints must use the same region/host"
                )

    def group(self, key):
        return self.projects.get(key.name, key.group or key.name)

    def for_provider(self, provider):
        return tuple(k for k in self.keys if k.provider == provider)

    def limits(self, key, product):
        group = self.group(key)
        return min(
            getattr(k, product + "_limit")
            for k in self.keys
            if k.provider == key.provider and self.group(k) == group
        )

    def state(self, key, product):
        with self.lock:
            if key.provider == "speechmatics" and not key_health.healthy(key.value):
                return "no_credits" if key_health.exhausted([key]) else "invalid_key"
            state = self.key_states.get((key.name, product))
            group_state, until = self.group_states.get(
                (key.provider, self.group(key), product), ("healthy", 0)
            )
            if group_state == "no_credits" or until > time.monotonic():
                return group_state
            return state or "healthy"

    def mark(self, key, product, kind):
        with self.lock:
            if kind in {"invalid_key", "no_credits", "capacity", "recoverable"}:
                self.last_errors[key.name, product] = kind
            if key.provider == "speechmatics":
                key_health.mark(key.value, kind)
            if kind == "invalid_key":
                self.key_states[key.name, product] = kind
            elif kind in {"capacity", "recoverable", "no_credits"}:
                products = (
                    ("streaming", "batch") if kind == "no_credits" else (product,)
                )
                for mode in products:
                    self.group_states[key.provider, self.group(key), mode] = (
                        kind if kind == "no_credits" else "cooldown",
                        time.monotonic() + 10,
                    )

    def reset(self):
        with self.lock:
            key_health.reset(self.keys)
            self.key_states.clear()
            self.group_states.clear()
            self.last_errors.clear()

    def exhausted(self, order):
        selected = [k for k in self.keys if k.provider in order]
        with self.lock:
            # Remote jobs already admitted may still finish despite a later 402.
            return (
                bool(selected)
                and not any(
                    k.provider in order and mode == "batch"
                    for k, mode in self.leases.values()
                )
                and all(
                    self.state(k, "batch") == "no_credits"
                    and self.state(k, "streaming") == "no_credits"
                    for k in selected
                )
            )

    def active_providers(self):
        with self.lock:
            return sorted({k.provider for k, _ in self.leases.values()})

    def supports(self, provider, product):
        if not self.settings or provider == "deepgram":
            return True
        if product == "streaming":
            return (
                self.settings.speechmatics_realtime_model in {"enhanced", "standard"}
                and self.settings.speechmatics_realtime_language == "pt"
            )
        return self.settings.speechmatics_model in {"standard", "enhanced", "melia-1"}

    def usable(self, order):
        return any(
            self.supports(k.provider, mode)
            and self.state(k, mode) not in {"no_credits", "invalid_key"}
            for k in self.keys
            if k.provider in order
            for mode in ("batch", "streaming")
        )

    def occupancy(self, key, product, *, by_key=False):
        return sum(
            mode == product
            and k.provider == key.provider
            and (k.name == key.name if by_key else self.group(k) == self.group(key))
            for k, mode in self.leases.values()
        )

    def reserve(self, token, order, product, attempted=()):
        with self.lock:
            for provider in order:
                candidates = [
                    k
                    for k in self.keys
                    if k.provider == provider
                    and self.supports(provider, product)
                    and k.name not in attempted
                    and self.state(k, product) == "healthy"
                    and self.occupancy(k, product) < self.limits(k, product)
                ]
                if (
                    provider == "deepgram"
                    and self.settings
                    and any(
                        k.provider == "deepgram" and k.name not in self.verified
                        for k in self.keys
                    )
                ):
                    total = sum(
                        k.provider == provider and mode == product
                        for k, mode in self.leases.values()
                    )
                    if total >= getattr(
                        self.settings, "deepgram_" + product + "_limit"
                    ):
                        candidates = []
                if candidates:
                    key = min(
                        candidates,
                        key=lambda k: (
                            self.occupancy(k, product),
                            self.occupancy(k, product, by_key=True),
                            self.keys.index(k),
                        ),
                    )
                    self.leases[token] = (key, product)
                    return key
        return None

    def release(self, token):
        with self.lock:
            self.leases.pop(token, None)

    def reconcile_project(self, key, project):
        with self.lock:
            old = self.group(key)
            self.projects[key.name] = project
            self.verified.add(key.name)
            # Merge known failures as well as counters, including live reservations.
            for product in ("streaming", "batch"):
                state = self.group_states.get(("deepgram", old, product))
                if state:
                    current = self.group_states.get(("deepgram", project, product))
                    if not current or state[0] == "no_credits" or state[1] > current[1]:
                        self.group_states["deepgram", project, product] = state


@lru_cache
def registry_for(settings):
    return ProviderRegistry(settings)
