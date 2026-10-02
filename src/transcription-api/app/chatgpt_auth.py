"""Official ChatGPT plan OAuth. Credentials never leave the backend storage."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from .config import Settings

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
TERMINAL_REFRESH_ERRORS = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


class ChatGPTError(RuntimeError):
    """Safe error text: never includes OAuth payloads, codes or credentials."""


def provider_error(code: str) -> ChatGPTError:
    messages = {
        "subscription_sharing_user_not_eligible": "Esta conta/plano não permite usar ChatGPT nesta aplicação.",
        "subscription_sharing_usage_limit_exceeded": "Limite ChatGPT atingido. Consulta https://chatgpt.com/settings/usage.",
        "subscription_sharing_usage_unavailable": "A disponibilidade ChatGPT não pôde ser verificada. Tenta mais tarde.",
        "subscription_sharing_user_unavailable": "Conta ChatGPT temporariamente indisponível. Tenta mais tarde.",
        "subscription_sharing_invalid_user": "Sessão ChatGPT inválida. Volta a iniciar sessão em /chatgpt.",
    }
    return ChatGPTError(
        messages.get(
            code,
            "O pedido ChatGPT falhou. Verifica a sessão, o modelo e os limites da conta.",
        )
    )


class ChatGPTAuth:
    def __init__(self, settings: Settings, *, http: httpx.Client | None = None):
        self.settings = settings
        self.http = http or httpx.Client(timeout=settings.llm_timeout_seconds)
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}

    @contextmanager
    def _storage(self):
        # The flock also serializes rotating refresh tokens across worker processes.
        path = self.settings.chatgpt_auth_file
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        data = {
                            "ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4()),
                            "accounts": {},
                            "active_client_id": None,
                        }
                        self._save(data)
                    except (ValueError, OSError) as exc:
                        raise ChatGPTError(
                            "Não foi possível ler a sessão ChatGPT guardada."
                        ) from exc
                    if (
                        not isinstance(data, dict)
                        or not data.get("ext_agent_host_id")
                        or not isinstance(data.get("accounts"), dict)
                    ):
                        raise ChatGPTError("O ficheiro da sessão ChatGPT é inválido.")
                    yield data
                finally:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _save(self, data: dict) -> None:
        path = self.settings.chatgpt_auth_file
        fd, name = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def status(self) -> dict:
        with self._storage() as data:
            cid = data.get("active_client_id")
            account = data["accounts"].get(cid, {})
            return {
                "connected": bool(account.get("access_token")),
                "email": account.get("email", ""),
                "active_client_id": cid,
                "accounts": [
                    {
                        "client_id": key,
                        "email": value.get("email", ""),
                        "connected": bool(value.get("access_token")),
                    }
                    for key, value in data["accounts"].items()
                ],
            }

    def start_login(self, *, new_account: bool = False) -> tuple[str, str]:
        redirect = self.settings.chatgpt_redirect_uri
        parsed = urlsplit(redirect)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path != "/auth/callback"
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ChatGPTError(
                "CHATGPT_REDIRECT_URI deve ser http://127.0.0.1:PORTA/auth/callback."
            )
        with self._storage() as data:
            cid = None if new_account else data.get("active_client_id")
            account = data["accounts"].get(cid, {})
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            params = {
                "client_id": cid or "dynamic_agent_client",
                "ext_agent_host_id": data["ext_agent_host_id"],
                "response_type": "code",
                "redirect_uri": redirect,
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()
                )
                .decode()
                .rstrip("="),
            }
            if cid:
                if account.get("id_token"):
                    params["id_token_hint"] = account["id_token"]
                if account.get("email"):
                    params["login_hint"] = account["email"]
            else:
                params["agent_name_hint"] = "Discord Anthropologist"
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if value["expires"] > time.time()
            }
            self._pending[state] = {
                "client_id": cid,
                "subject": account.get("subject"),
                "nonce": nonce,
                "verifier": verifier,
                "redirect": redirect,
                "expires": time.time() + 600,
            }
            return ISSUER + "/api/accounts/authorize?" + urlencode(params), state

    def _json(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self.http.request(method, url, **kwargs)
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChatGPTError(
                "Falha de rede ao contactar ChatGPT. Verifica a ligação e tenta novamente."
            ) from exc
        if not isinstance(result, dict):
            raise ChatGPTError("Resposta ChatGPT inválida.")
        if response.is_error:
            error = result.get("error")
            code = error.get("code", "") if isinstance(error, dict) else error
            if code in TERMINAL_REFRESH_ERRORS:
                raise InvalidRefreshError(
                    "Sessão expirada ou revogada. Volta a iniciar sessão em /chatgpt."
                )
            raise provider_error(str(code or ""))
        return result

    def _discovery(self) -> dict:
        config = self._json("GET", ISSUER + "/.well-known/openid-configuration")
        if config.get("issuer") != ISSUER:
            raise ChatGPTError("Emissor OAuth inválido.")
        for field in ("jwks_uri", "revocation_endpoint"):
            if field in config and not config[field].startswith(ISSUER + "/"):
                raise ChatGPTError("Endpoint OAuth inválido.")
        return config

    def _identity(self, token: str, cid: str, nonce: str | None = None) -> dict:
        try:
            config = self._discovery()
            jwks = self._json("GET", config["jwks_uri"])
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise ValueError("unsupported signature")
            key = next(
                item for item in jwks["keys"] if item.get("kid") == header.get("kid")
            )
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                audience=cid,
                issuer=ISSUER,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
            if not claims["sub"] or (
                nonce is not None
                and not secrets.compare_digest(str(claims.get("nonce", "")), nonce)
            ):
                raise ValueError("invalid identity")
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, StopIteration, TypeError) as exc:
            raise ChatGPTError(
                "Não foi possível validar a identidade ChatGPT."
            ) from exc

    def finish_login(
        self,
        *,
        state: str,
        browser_state: str,
        code: str = "",
        client_id: str = "",
        error: str = "",
    ) -> bool:
        if (
            not state
            or not browser_state
            or not secrets.compare_digest(state, browser_state)
        ):
            raise ChatGPTError(
                "Login inválido: estado OAuth não corresponde ao navegador."
            )
        with self._lock:
            attempt = self._pending.pop(state, None)
            if not attempt or attempt["expires"] <= time.time():
                raise ChatGPTError("Login expirado. Inicia uma nova tentativa.")
            if error:
                raise ChatGPTError("Login cancelado ou permissão ChatGPT recusada.")
            cid = client_id or attempt["client_id"]
            if (
                not code
                or not cid
                or cid == "dynamic_agent_client"
                or (attempt["client_id"] and cid != attempt["client_id"])
            ):
                raise ChatGPTError(
                    "Registo OAuth incompleto ou conta diferente da selecionada."
                )
            tokens = self._json(
                "POST",
                ISSUER + "/api/accounts/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": cid,
                    "code": code,
                    "code_verifier": attempt["verifier"],
                    "redirect_uri": attempt["redirect"],
                    "resource": RESOURCE,
                },
            )
            identity = self._identity(tokens.get("id_token", ""), cid, attempt["nonce"])
            if attempt["subject"] and identity["sub"] != attempt["subject"]:
                raise ChatGPTError(
                    "A identidade recebida não corresponde à conta selecionada."
                )
            account = self._credentials(
                tokens,
                {
                    "client_id": cid,
                    "subject": identity["sub"],
                    "email": identity.get("email", ""),
                    "issuer": ISSUER,
                },
            )
            with self._storage() as data:
                changed_account = data.get("active_client_id") != cid
                existing = data["accounts"].get(cid)
                if existing and existing["subject"] != identity["sub"]:
                    raise ChatGPTError("O registo pertence a outra conta.")
                data["accounts"][cid] = account
                data["active_client_id"] = cid
                self._save(data)
            return changed_account

    def _credentials(self, tokens: dict, account: dict) -> dict:
        scopes = tokens.get("scope", " ".join(account.get("scopes", []))).split()
        if not {"chatgpt.tokens.use.direct", "resource.invoke"}.issubset(scopes):
            raise ChatGPTError("Autoriza o uso do plano ChatGPT durante o login.")
        if (
            not tokens.get("access_token")
            or not tokens.get("refresh_token")
            or tokens.get("token_type", "").lower() != "bearer"
        ):
            raise ChatGPTError("O login não devolveu uma sessão renovável válida.")
        try:
            expires = float(tokens["expires_in"])
            earliest = float(tokens.get("earliest_refresh_at", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ChatGPTError("Validade da sessão ChatGPT inválida.") from exc
        if not math.isfinite(expires) or not math.isfinite(earliest) or expires <= 0:
            raise ChatGPTError("A sessão ChatGPT já expirou.")
        return {
            **account,
            **{
                key: tokens[key]
                for key in ("access_token", "refresh_token", "id_token")
                if key in tokens
            },
            "scopes": scopes,
            "expires_at": time.time() + expires,
            "earliest_refresh_at": earliest,
        }

    def access_token(self) -> str:
        with self._storage() as data:
            cid = data.get("active_client_id")
            account = data["accounts"].get(cid, {})
            if not account.get("access_token"):
                raise ChatGPTError("Inicia sessão e escolhe um modelo em /chatgpt.")
            if account["expires_at"] <= time.time() + 60:
                if account.get("earliest_refresh_at", 0) > time.time():
                    if account["expires_at"] <= time.time():
                        raise ChatGPTError(
                            "A sessão expirou e ainda não pode ser renovada. Tenta mais tarde."
                        )
                    return account["access_token"]
                try:
                    tokens = self._json(
                        "POST",
                        ISSUER + "/api/accounts/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": cid,
                            "refresh_token": account["refresh_token"],
                            "resource": RESOURCE,
                        },
                    )
                except InvalidRefreshError:
                    self._clear_tokens(account)
                    self._save(data)
                    raise
                if tokens.get("id_token"):
                    identity = self._identity(tokens["id_token"], cid)
                    if identity["sub"] != account["subject"]:
                        raise ChatGPTError(
                            "A renovação devolveu uma identidade diferente."
                        )
                account = self._credentials(tokens, account)
                data["accounts"][cid] = account
                self._save(data)
            return account["access_token"]

    @staticmethod
    def _clear_tokens(account: dict) -> None:
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "expires_at",
            "earliest_refresh_at",
            "scopes",
        ):
            account.pop(key, None)

    def switch_account(self, cid: str) -> None:
        with self._storage() as data:
            if cid not in data["accounts"]:
                raise ChatGPTError("Conta desconhecida.")
            data["active_client_id"] = cid
            self._save(data)

    def logout(self) -> bool:
        confirmed = True
        with self._storage() as data:
            account = data["accounts"].get(data.get("active_client_id"), {})
            if account.get("refresh_token"):
                try:
                    config = self._discovery()
                    response = self.http.post(
                        config["revocation_endpoint"],
                        data={
                            "token": account["refresh_token"],
                            "token_type_hint": "refresh_token",
                            "client_id": account["client_id"],
                        },
                    )
                    confirmed = response.status_code == 200
                except (ChatGPTError, httpx.HTTPError, KeyError):
                    confirmed = False
            self._clear_tokens(account)
            self._pending.clear()
            self._save(data)
        return confirmed

    def models(self) -> list[dict[str, str]]:
        data = self._json(
            "GET",
            RESOURCE + "/models",
            headers={"Authorization": "Bearer " + self.access_token()},
        )
        models = data.get("models")
        if not isinstance(models, list):
            raise ChatGPTError("O catálogo ChatGPT devolvido é inválido.")
        return [
            {
                "slug": item["slug"],
                "display_name": str(item.get("display_name") or item["slug"]),
            }
            for item in models
            if isinstance(item, dict)
            and item.get("visibility") == "list"
            and isinstance(item.get("slug"), str)
            and item["slug"].strip()
        ]


class InvalidRefreshError(ChatGPTError):
    pass


@lru_cache(maxsize=8)
def get_chatgpt_auth(settings: Settings) -> ChatGPTAuth:
    return ChatGPTAuth(settings)
