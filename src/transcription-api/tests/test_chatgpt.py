"""Offline OAuth, provider, setup and Discord model API regression checks."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import HTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from app import chatgpt_llm, chatgpt_login, main
from app.chatgpt_auth import (
    ISSUER,
    ChatGPTAuth,
    ChatGPTError,
)
from app.chatgpt_llm import ChatGPTClient
from app.config import Settings
from app.model_selection import (
    current_effort,
    current_model,
    select_effort,
    select_model,
)
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
    monkeypatch.setattr(main, "get_chatgpt_auth", lambda _: auth)
    application = main.create_app()
    application.dependency_overrides[main.get_settings] = lambda: settings
    main.build_llm_client.cache_clear()
    client = TestClient(application, base_url="http://127.0.0.1:8000")
    yield client
    client.close()
    main.build_llm_client.cache_clear()


def test_custom_setup_pages_are_removed(panel):
    assert panel.get("/chatgpt").status_code == 404
    assert panel.get("/auth/callback").status_code == 404


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
    monkeypatch.setenv("CHATGPT_REASONING_EFFORT", "low")
    settings = Settings.from_env()
    assert settings.llm_provider == "chatgpt"
    assert settings.chatgpt_admin_password == "password"
    assert settings.chatgpt_auth_file == tmp_path / "credentials.json"
    assert settings.chatgpt_reasoning_effort == "low"


def test_loopback_callback_requires_process_state_and_saves_login(oauth, capsys):
    settings, auth, _, begin, _ = oauth
    _, state = begin()
    result = {}
    with HTTPServer(
        ("127.0.0.1", 0), chatgpt_login.callback_handler(auth, state, result)
    ) as server:
        thread = threading.Thread(
            target=lambda: (server.handle_request(), server.handle_request())
        )
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/auth/callback"
        wrong = httpx.get(url, params={"state": "wrong", "code": "sensitive-code"})
        assert wrong.status_code == 400
        assert not result
        correct = httpx.get(
            url,
            params={
                "state": state,
                "code": "sensitive-code",
                "client_id": "oaiapp_test",
            },
        )
        assert correct.status_code == 200
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert result == {"done": True, "changed": True}
    assert auth.status()["connected"]
    assert settings.chatgpt_auth_file.exists()
    assert "sensitive-code" not in capsys.readouterr().err


@pytest.mark.parametrize("port,expected_port", [(None, 1455), (1456, 1456)])
def test_cli_ignores_old_api_callback_and_uses_selected_port(
    oauth, monkeypatch, port, expected_port
):
    settings, auth, _, _, _ = oauth
    settings = replace(
        settings, chatgpt_redirect_uri="http://127.0.0.1:8000/auth/callback"
    )
    bound = []
    redirects = []
    events = []

    class Listener:
        def __init__(self, address, handler):
            bound.append(address)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def get_auth(selected):
        redirects.append(selected.chatgpt_redirect_uri)
        auth.settings = selected
        return auth

    monkeypatch.setattr(chatgpt_login, "HTTPServer", Listener)
    monkeypatch.setattr(chatgpt_login, "get_chatgpt_auth", get_auth)
    args = SimpleNamespace(
        status=False,
        logout=False,
        account=None,
        port=port,
        timeout=0,
        new_account=False,
    )
    with pytest.raises(ChatGPTError, match="expirou"):
        chatgpt_login.run(args, settings=settings, emit=events.append)
    assert bound == [("127.0.0.1", expected_port)]
    expected = f"http://127.0.0.1:{expected_port}/auth/callback"
    assert redirects[-1] == expected
    assert parse_qs(urlsplit(events[0]["url"]).query)["redirect_uri"] == [expected]


def test_cli_status_and_switch_account_without_api(oauth, monkeypatch):
    settings, auth, _, begin, finish = oauth
    finish()
    _, state = begin(new_account=True)
    auth.finish_login(state=state, browser_state=state, code="code", client_id="second")
    select_model("chatgpt", "model-a", settings.llm_model_selection_file)
    monkeypatch.setattr(chatgpt_login, "get_chatgpt_auth", lambda _: auth)
    events = []
    args = SimpleNamespace(status=True, logout=False, account=None)
    assert chatgpt_login.run(args, settings=settings, emit=events.append) == 0
    assert events[0]["active_client_id"] == "second"
    assert "access_token" not in json.dumps(events)
    args.status, args.account = False, "oaiapp_test"
    assert chatgpt_login.run(args, settings=settings, emit=events.append) == 0
    assert auth.status()["active_client_id"] == "oaiapp_test"
    assert current_model(settings) == ""


def test_printable_reauthorization_url_contains_no_identity_token(oauth):
    _, auth, _, _, finish = oauth
    finish()
    url, _ = auth.start_login(include_id_token_hint=False)
    params = parse_qs(urlsplit(url).query)
    assert params["client_id"] == ["oaiapp_test"]
    assert "id_token_hint" not in params


def test_effort_is_tested_persisted_and_used_by_next_request(panel, oauth, monkeypatch):
    settings, auth, _, _, finish = oauth
    finish()
    select_model("chatgpt", "model-a", settings.llm_model_selection_file)
    assert main.get_llm_client(settings).reasoning_effort == "medium"
    _, calls = provider_stream(
        monkeypatch,
        auth,
        [
            SimpleNamespace(
                type="response.completed", response=SimpleNamespace(output_text="ok")
            )
        ],
    )
    assert panel.post("/v1/effort/current", json={"effort": "low"}).status_code == 401
    response = panel.post(
        "/v1/effort/current",
        json={"effort": "low"},
        headers={"X-ChatGPT-Admin-Key": "owner-secret"},
    )
    assert response.status_code == 200
    assert calls[-1]["reasoning"] == {"effort": "low"}
    assert current_effort(settings) == "low"
    assert main.get_llm_client(settings).reasoning_effort == "low"
    assert panel.get("/v1/effort").json()["current_effort"] == "low"
    # Persisted selection applies to freshly created clients and model testing.
    response = panel.post(
        "/v1/models/current",
        json={"model": "model-b"},
        headers={"X-ChatGPT-Admin-Key": "owner-secret"},
    )
    assert response.status_code == 200
    assert calls[-1]["reasoning"] == {"effort": "low"}
    assert main.get_llm_client(settings).model == "model-b"


def test_effort_can_be_reset_before_a_model_is_selected(panel, oauth, monkeypatch):
    settings, auth, _, _, finish = oauth
    finish()
    select_effort("max", settings.llm_model_selection_file)
    _, calls = provider_stream(
        monkeypatch,
        auth,
        [
            SimpleNamespace(
                type="response.completed", response=SimpleNamespace(output_text="ok")
            )
        ],
    )
    response = panel.post(
        "/v1/effort/current",
        json={"effort": "default"},
        headers={"X-ChatGPT-Admin-Key": "owner-secret"},
    )
    assert response.status_code == 200
    assert current_effort(settings) == "default"
    assert current_model(settings) == ""
    assert calls[-1]["model"] == "model-a"
    assert "reasoning" not in calls[-1]


def test_unsupported_effort_keeps_previous_setting(panel, oauth, monkeypatch):
    settings, auth, _, _, finish = oauth
    finish()
    select_model("chatgpt", "model-a", settings.llm_model_selection_file)
    select_effort("low", settings.llm_model_selection_file)
    provider_stream(
        monkeypatch, auth, [SimpleNamespace(type="error", code="unsupported_value")]
    )
    response = panel.post(
        "/v1/effort/current",
        json={"effort": "max"},
        headers={"X-ChatGPT-Admin-Key": "owner-secret"},
    )
    assert response.status_code == 502
    assert current_effort(settings) == "low"
    assert main.get_llm_client(settings).reasoning_effort == "low"
    assert (
        panel.post(
            "/v1/effort/current",
            json={"effort": "ultra"},
            headers={"X-ChatGPT-Admin-Key": "owner-secret"},
        ).status_code
        == 400
    )


def test_default_effort_omits_reasoning_override(oauth, monkeypatch):
    _, auth, _, _, finish = oauth
    finish()
    client, calls = provider_stream(
        monkeypatch,
        auth,
        [
            SimpleNamespace(
                type="response.completed", response=SimpleNamespace(output_text="ok")
            )
        ],
    )
    client.reasoning_effort = "default"
    assert client.test_model() == "ok"
    assert "reasoning" not in calls[-1]


def test_host_wrapper_opens_official_login_url(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "codex_login.py"
    spec = importlib.util.spec_from_file_location("codex_login_host", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DISPLAY", ":0")
    urls = []
    monkeypatch.setattr(module.webbrowser, "open", lambda url: urls.append(url) or True)
    module.handle_event(
        {
            "event": "authorize",
            "url": ISSUER + "/api/accounts/authorize?state=test",
            "port": 1455,
        }
    )
    assert urls == [ISSUER + "/api/accounts/authorize?state=test"]
    module.handle_event(
        {"event": "authorize", "url": "headless", "port": 1455}, no_browser=True
    )
    assert len(urls) == 1
    assert "1455" in capsys.readouterr().out


def test_host_wrapper_forwards_login_options_and_container_exit_code(
    monkeypatch, capsys
):
    import importlib.util
    import io
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "codex_login.py"
    spec = importlib.util.spec_from_file_location("codex_login_host_options", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands = []

    class Process:
        stdout = io.StringIO('{"event":"error","message":"login failed"}\n')

        def wait(self):
            return 1

    def launch(command, **kwargs):
        commands.append(command)
        return Process()

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "codex_login.py",
            "--no-browser",
            "--port",
            "1456",
            "--",
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
        ],
    )
    assert module.main() == 1
    assert commands == [
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "run",
            "--build",
            "--rm",
            "--no-deps",
            "-T",
            "codex",
            "python",
            "-m",
            "app.chatgpt_login",
            "--port",
            "1456",
        ]
    ]
    assert "login failed" in capsys.readouterr().out
