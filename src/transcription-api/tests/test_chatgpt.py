"""Offline OAuth, provider, setup and Discord model API regression checks."""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from app import chatgpt_llm, main
from app.chatgpt_auth import (
    ISSUER,
    ChatGPTAuth,
    ChatGPTError,
)
from app.chatgpt_llm import ChatGPTClient
from app.chatgpt_routes import CallbackLogFilter
from app.config import Settings
from app.model_selection import current_model, select_model
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def oauth(tmp_path, signing_key):
    settings = Settings(
        database_url="unused",
        llm_provider="chatgpt",
        chatgpt_auth_file=tmp_path / "auth" / "auth.json",
        chatgpt_admin_password="owner-secret",
        llm_model_selection_file=tmp_path / "models.json",
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk["kid"] = "test-key"
    state = {
        "nonce": "",
        "token_calls": [],
        "scope": "openid offline_access resource.invoke chatgpt.tokens.use.direct",
        "refresh_error": None,
        "identity": {},
        "revoked": False,
    }

    def token(cid, nonce):
        claims = {
            "iss": ISSUER,
            "aud": cid,
            "sub": "owner",
            "email": "owner@example.test",
            "exp": time.time() + 3600,
            "nonce": nonce,
            **state["identity"],
        }
        return jwt.encode(
            claims, signing_key, algorithm="RS256", headers={"kid": "test-key"}
        )

    def handler(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "jwks_uri": ISSUER + "/jwks",
                    "revocation_endpoint": ISSUER + "/revoke",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [jwk]})
        if request.url.path == "/api/accounts/oauth/token":
            form = parse_qs(request.content.decode())
            state["token_calls"].append(form)
            refresh = form["grant_type"] == ["refresh_token"]
            if refresh and state["refresh_error"]:
                return httpx.Response(
                    state["refresh_status"], json={"error": state["refresh_error"]}
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "replacement" if refresh else "access",
                    "refresh_token": "refresh-2" if refresh else "refresh-1",
                    "id_token": token(form["client_id"][0], state["nonce"]),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": state["scope"],
                },
            )
        if request.url.path == "/v1/models":
            assert request.headers["authorization"] in {
                "Bearer access",
                "Bearer replacement",
            }
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": "model-a",
                            "display_name": "Model A",
                            "visibility": "list",
                        },
                        {
                            "slug": "model-b",
                            "display_name": "Model B",
                            "visibility": "list",
                        },
                        {"slug": "hidden", "visibility": "hidden"},
                    ]
                },
            )
        if request.url.path == "/revoke":
            state["revoked"] = parse_qs(request.content.decode())["token"] == [
                "refresh-1"
            ]
            return httpx.Response(200)
        raise AssertionError(str(request.url))

    auth = ChatGPTAuth(
        settings, http=httpx.Client(transport=httpx.MockTransport(handler))
    )

    def begin(*, new_account=False):
        url, browser_state = auth.start_login(new_account=new_account)
        params = parse_qs(urlsplit(url).query)
        state["nonce"] = params["nonce"][0]
        return params, browser_state

    def finish(*, cid="oaiapp_test"):
        params, browser_state = begin()
        auth.finish_login(
            state=browser_state,
            browser_state=browser_state,
            code="one-use-code",
            client_id=cid,
        )
        return params, browser_state

    yield settings, auth, state, begin, finish
    auth.http.close()


def test_registration_storage_catalog_and_reauthorization(oauth):
    settings, auth, state, begin, finish = oauth
    params, browser_state = finish()
    assert params["client_id"] == ["dynamic_agent_client"]
    assert params["agent_name_hint"] == ["Discord Anthropologist"]
    assert params["code_challenge_method"] == ["S256"]
    assert state["token_calls"][0]["client_id"] == ["oaiapp_test"]
    assert state["token_calls"][0]["redirect_uri"] == params["redirect_uri"]
    assert settings.chatgpt_auth_file.stat().st_mode & 0o777 == 0o600
    assert auth.access_token() == "access"
    assert [item["slug"] for item in auth.models()] == ["model-a", "model-b"]
    assert "access_token" not in json.dumps(auth.status())
    host_id = params["ext_agent_host_id"]
    params, _ = begin()
    assert params["ext_agent_host_id"] == host_id
    assert params["client_id"] == ["oaiapp_test"]
    assert "agent_name_hint" not in params
    assert "id_token_hint" in params
    restarted = ChatGPTAuth(settings, http=auth.http)
    assert restarted.status()["connected"]
    with pytest.raises(ChatGPTError, match="expirado"):
        auth.finish_login(
            state=browser_state,
            browser_state=browser_state,
            code="replayed",
            client_id="oaiapp_test",
        )


def test_state_cookie_and_client_id_are_validated_before_exchange(oauth):
    _, auth, state, begin, finish = oauth
    _, pending = begin()
    with pytest.raises(ChatGPTError, match="estado OAuth"):
        auth.finish_login(
            state=pending, browser_state="wrong", code="code", client_id="cid"
        )
    assert not state["token_calls"]
    with pytest.raises(ChatGPTError, match="incompleto"):
        auth.finish_login(state=pending, browser_state=pending, code="code")
    finish()
    _, pending = begin()
    count = len(state["token_calls"])
    with pytest.raises(ChatGPTError, match="conta diferente"):
        auth.finish_login(
            state=pending, browser_state=pending, code="code", client_id="other"
        )
    assert len(state["token_calls"]) == count


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "wrong"},
        {"iss": "https://attacker.invalid"},
        {"aud": "wrong-client"},
        {"exp": 1},
        {"sub": ""},
    ],
)
def test_invalid_signed_identity_never_replaces_credentials(oauth, claims):
    _, auth, state, _, finish = oauth
    state["identity"] = claims
    with pytest.raises(ChatGPTError, match="identidade"):
        finish()
    assert not auth.status()["connected"]


def test_missing_plan_consent_keeps_existing_account(oauth):
    _, auth, state, begin, finish = oauth
    finish()
    state["scope"] = "openid offline_access"
    _, pending = begin(new_account=True)
    with pytest.raises(ChatGPTError, match="Autoriza"):
        auth.finish_login(
            state=pending, browser_state=pending, code="code", client_id="second"
        )
    assert auth.status()["active_client_id"] == "oaiapp_test"
    assert len(auth.status()["accounts"]) == 1


def expire(auth):
    with auth._storage() as data:
        data["accounts"][data["active_client_id"]]["expires_at"] = time.time() - 1
        auth._save(data)


def test_refresh_rotation_is_persistent_and_uses_issued_client(oauth):
    _, auth, state, _, finish = oauth
    finish()
    expire(auth)
    assert auth.access_token() == "replacement"
    assert auth.access_token() == "replacement"
    assert len(state["token_calls"]) == 2
    form = state["token_calls"][1]
    assert form["refresh_token"] == ["refresh-1"]
    assert form["client_id"] == ["oaiapp_test"]
    assert "scope" not in form
    with auth._storage() as data:
        assert data["accounts"]["oaiapp_test"]["refresh_token"] == "refresh-2"


def test_concurrent_refreshes_use_rotating_token_only_once(oauth):
    settings, auth, state, _, finish = oauth
    finish()
    expire(auth)
    second = ChatGPTAuth(settings, http=auth.http)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(
            pool.map(
                lambda manager: manager.access_token(), [auth, second, auth, second]
            )
        )
    assert result == ["replacement"] * 4
    assert len(state["token_calls"]) == 2


@pytest.mark.parametrize(
    "code,status,connected",
    [("invalid_grant", 400, False), ("temporarily_unavailable", 503, True)],
)
def test_refresh_terminal_errors_clear_tokens_but_transient_errors_preserve_them(
    oauth, code, status, connected
):
    _, auth, state, _, finish = oauth
    finish()
    expire(auth)
    state.update(refresh_error=code, refresh_status=status)
    with pytest.raises(ChatGPTError):
        auth.access_token()
    assert auth.status()["connected"] is connected
    assert auth.status()["active_client_id"] == "oaiapp_test"


def test_logout_revokes_and_retains_host_and_registration(oauth):
    _, auth, state, begin, finish = oauth
    original, _ = finish()
    assert auth.logout()
    assert state["revoked"]
    assert not auth.status()["connected"]
    params, _ = begin()
    assert params["ext_agent_host_id"] == original["ext_agent_host_id"]
    assert params["client_id"] == ["oaiapp_test"]
    assert "id_token_hint" not in params


def provider_stream(monkeypatch, auth, events):
    calls = []

    class Stream:
        def __enter__(self):
            return iter(events)

        def __exit__(self, *_):
            pass

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.responses = self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def create(self, **kwargs):
            calls.append(kwargs)
            return Stream()

    monkeypatch.setattr(chatgpt_llm, "OpenAI", FakeOpenAI)
    return ChatGPTClient(auth=auth, model="model-a"), calls


def test_provider_uses_responses_and_refreshes_credentials_between_calls(
    oauth, monkeypatch
):
    _, auth, _, _, finish = oauth
    finish()
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="Ola!"),
        SimpleNamespace(
            type="response.completed", response=SimpleNamespace(output_text="Ola!")
        ),
    ]
    client, calls = provider_stream(monkeypatch, auth, events)
    assert client._chat(system="Instructions", user="Hello", json_format=True) == "Ola!"
    body = calls[1]
    assert body["instructions"] == "Instructions"
    assert body["stream"] is True and body["store"] is False
    assert body["input"] == [{"role": "user", "content": "Hello"}]
    assert body["text"] == {"format": {"type": "json_object"}}
    assert "max_output_tokens" not in body and "temperature" not in body
    expire(auth)
    assert client.test_model() == "Ola!"
    assert calls[0]["api_key"] == "access"
    assert calls[2]["api_key"] == "replacement"


@pytest.mark.parametrize("terminal", [None, "response.incomplete", "response.failed"])
def test_partial_output_is_never_accepted(oauth, monkeypatch, terminal):
    _, auth, _, _, finish = oauth
    finish()
    events = [SimpleNamespace(type="response.output_text.delta", delta="partial")]
    if terminal:
        events.append(
            SimpleNamespace(
                type=terminal,
                response=SimpleNamespace(
                    error=SimpleNamespace(
                        code="subscription_sharing_usage_limit_exceeded"
                    )
                ),
            )
        )
    client, _ = provider_stream(monkeypatch, auth, events)
    with pytest.raises(ChatGPTError):
        client.test_model()


@pytest.fixture
def panel(oauth, monkeypatch):
    settings, auth, _, _, _ = oauth
    monkeypatch.setattr("app.chatgpt_routes.get_chatgpt_auth", lambda _: auth)
    monkeypatch.setattr(main, "get_chatgpt_auth", lambda _: auth)
    application = main.create_app()
    application.dependency_overrides[main.get_settings] = lambda: settings
    main.build_llm_client.cache_clear()
    client = TestClient(application, base_url="http://127.0.0.1:8000")
    yield client
    client.close()
    main.build_llm_client.cache_clear()


def test_setup_is_protected_and_requires_same_origin(panel):
    assert panel.get("/chatgpt").status_code == 401
    assert panel.get("/chatgpt", auth=("admin", "wrong")).status_code == 401
    assert panel.get("/chatgpt", auth=("admin", "owner-secret")).status_code == 200
    assert (
        panel.post(
            "/chatgpt/login",
            auth=("admin", "owner-secret"),
            headers={"Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    response = panel.post(
        "/chatgpt/login",
        auth=("admin", "owner-secret"),
        headers={"Origin": "http://127.0.0.1:8000"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(ISSUER + "/api/accounts/authorize?")
    assert "HttpOnly" in response.headers["set-cookie"]


def test_callback_rejects_requests_without_browser_cookie(panel):
    assert (
        panel.get("/auth/callback?state=stolen&code=secret&client_id=other").status_code
        == 400
    )


def test_browser_login_completes_and_model_can_be_selected_in_panel(
    panel, oauth, monkeypatch
):
    settings, auth, state, _, _ = oauth
    start = panel.post(
        "/chatgpt/login",
        auth=("admin", "owner-secret"),
        headers={"Origin": "http://127.0.0.1:8000"},
        follow_redirects=False,
    )
    query = parse_qs(urlsplit(start.headers["location"]).query)
    state["nonce"] = query["nonce"][0]
    response = panel.get(
        "/auth/callback",
        params={"state": query["state"][0], "code": "code", "client_id": "oaiapp_test"},
    )
    assert response.status_code == 200
    provider_stream(
        monkeypatch,
        auth,
        [
            SimpleNamespace(
                type="response.completed", response=SimpleNamespace(output_text="Ola!")
            )
        ],
    )
    response = panel.post(
        "/chatgpt/model",
        data={"model": "model-b"},
        auth=("admin", "owner-secret"),
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert response.status_code == 200
    assert "Ola!" in response.text
    assert current_model(settings) == "model-b"
    html = panel.get("/chatgpt", auth=("admin", "owner-secret")).text
    assert "Model A" in html and "Model B" in html
    assert auth.access_token() not in html
    assert "refresh-1" not in html


def test_switching_account_clears_model_selection(panel, oauth):
    settings, auth, _, begin, finish = oauth
    finish()
    _, state = begin(new_account=True)
    auth.finish_login(state=state, browser_state=state, code="code", client_id="second")
    select_model("chatgpt", "model-a", settings.llm_model_selection_file)
    response = panel.post(
        "/chatgpt/account",
        data={"client_id": "oaiapp_test"},
        auth=("admin", "owner-secret"),
        headers={"Origin": "http://127.0.0.1:8000"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert auth.status()["active_client_id"] == "oaiapp_test"
    assert current_model(settings) == ""


def test_real_sdk_decodes_responses_sse_and_json_requests(oauth, monkeypatch):
    _, auth, _, _, finish = oauth
    finish()
    real_client = chatgpt_llm.OpenAI
    sent = []
    response = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "model-a",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"answer":"ok"}',
                        "annotations": [],
                    }
                ],
            }
        ],
    }

    def transport(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer access"
        sent.append(json.loads(request.content))
        data = (
            "data: "
            + json.dumps({"type": "response.completed", "response": response})
            + "\n\n"
        )
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=data.encode()
        )

    monkeypatch.setattr(
        chatgpt_llm,
        "OpenAI",
        lambda **kwargs: real_client(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport))
        ),
    )
    client = ChatGPTClient(auth=auth, model="model-a")
    assert json.loads(
        client._chat(system="Return JSON", user="Hello", json_format=True)
    ) == {"answer": "ok"}
    assert sent[0]["text"]["format"]["type"] == "json_object"
    assert sent[0]["store"] is False


def test_callback_access_logs_do_not_contain_authorization_code():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        1,
        '%s - "%s %s HTTP/%s" %s',
        ("client", "GET", "/auth/callback?code=secret&state=secret", "1.1", 200),
        None,
    )
    assert CallbackLogFilter().filter(record)
    assert "secret" not in record.getMessage()


def test_discord_models_api_tests_and_persists_chatgpt_selection(
    panel, oauth, monkeypatch
):
    settings, auth, _, _, finish = oauth
    finish()
    provider_stream(
        monkeypatch,
        auth,
        [
            SimpleNamespace(
                type="response.completed", response=SimpleNamespace(output_text="Ola!")
            )
        ],
    )
    models = panel.get("/v1/models")
    assert models.status_code == 200
    assert models.json()["provider"] == "chatgpt"
    assert models.json()["models"] == ["model-a", "model-b"]
    assert (
        panel.post("/v1/models/current", json={"model": "model-b"}).status_code == 401
    )
    response = panel.post(
        "/v1/models/current",
        json={"model": "model-b"},
        headers={"X-ChatGPT-Admin-Key": "owner-secret"},
    )
    assert response.status_code == 200
    assert response.json()["test_response"] == "Ola!"
    assert current_model(settings) == "model-b"
    assert (
        json.loads(settings.llm_model_selection_file.read_text())["chatgpt"]
        == "model-b"
    )
    assert main.get_llm_client(settings).model == "model-b"
    assert isinstance(main.get_llm_client(settings), ChatGPTClient)


def test_failed_model_test_keeps_previous_choice(panel, oauth, monkeypatch):
    settings, auth, _, _, finish = oauth
    finish()
    select_model("chatgpt", "model-a", settings.llm_model_selection_file)
    provider_stream(monkeypatch, auth, [SimpleNamespace(type="response.incomplete")])
    response = panel.post(
        "/v1/models/current",
        json={"model": "model-b"},
        headers={"X-ChatGPT-Admin-Key": "owner-secret"},
    )
    assert response.status_code == 502
    assert current_model(settings) == "model-a"


def test_existing_providers_are_still_available(oauth):
    settings, *_ = oauth
    for name, model in [
        ("openai", "gpt-4o-mini"),
        ("groq", "llama-3.3-70b-versatile"),
        ("ollama", "qwen3.5:2b"),
    ]:
        selected = replace(settings, llm_provider=name)
        assert current_model(selected) == model
        assert main.build_llm_client(selected, model).model == model


def test_settings_read_chatgpt_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "chatgpt")
    monkeypatch.setenv("CHATGPT_AUTH_FILE", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("CHATGPT_ADMIN_PASSWORD", "password")
    settings = Settings.from_env()
    assert settings.llm_provider == "chatgpt"
    assert settings.chatgpt_admin_password == "password"
    assert settings.chatgpt_auth_file == tmp_path / "credentials.json"
