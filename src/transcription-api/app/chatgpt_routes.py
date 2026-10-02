"""Owner-only setup panel and loopback callback for ChatGPT OAuth."""

from __future__ import annotations

import base64
import binascii
import html
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .chatgpt_auth import ChatGPTError, get_chatgpt_auth
from .config import Settings
from .model_selection import clear_selected_model, current_model, select_model


class CallbackLogFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn normally logs the full callback query, including one-use codes.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if (
                isinstance(args[2], str)
                and args[2].split("?", 1)[0] == "/auth/callback"
            ):
                args[2] = "/auth/callback"
                record.args = tuple(args)
        return True


def require_chatgpt_admin(request: Request, settings: Settings) -> None:
    expected = settings.chatgpt_admin_password.encode()
    if not expected:
        raise HTTPException(
            503, "Configura CHATGPT_ADMIN_PASSWORD no .env para abrir o painel."
        )
    key = request.headers.get("x-chatgpt-admin-key", "").encode()
    if key and secrets.compare_digest(key, expected):
        return  # Trusted bot-to-API request; no browser credentials involved.
    try:
        scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
        if scheme.lower() != "basic":
            raise ValueError
        username, password = base64.b64decode(encoded, validate=True).split(b":", 1)
        authorized = secrets.compare_digest(
            username, b"admin"
        ) and secrets.compare_digest(password, expected)
    except (ValueError, binascii.Error):
        authorized = False
    if not authorized:
        raise HTTPException(
            401,
            "Autenticação do administrador necessária.",
            headers={"WWW-Authenticate": 'Basic realm="ChatGPT setup"'},
        )
    if request.method not in {"GET", "HEAD"}:
        origin = request.headers.get("origin")
        if origin != str(request.base_url).rstrip("/"):
            raise HTTPException(
                403, "Origem do pedido inválida. Usa o formulário do painel."
            )


def page(content: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="pt"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT · Discord Anthropologist</title><style>
body{margin:0;background:#f5f3ed;color:#242c26;font:16px/1.6 system-ui,sans-serif}
main{max-width:700px;margin:5vh auto;padding:32px}h1{font-size:36px;line-height:1.15}
.card{background:white;border:1px solid #d9ddd4;border-radius:12px;padding:24px;margin:20px 0}
label,select{display:block}select{width:100%;padding:12px;margin:8px 0 16px}
button{background:#174b38;color:white;border:0;border-radius:6px;padding:12px 18px;cursor:pointer}
button.secondary{background:#e6ece7;color:#174b38}a{color:#174b38}small{color:#556259}
.notice{border-left:4px solid #c79c34;padding:12px;background:#fff6d8}
</style><main><small>DISCORD ANTHROPOLOGIST / CONFIGURAÇÃO</small>
<h1>Usar o teu plano ChatGPT</h1>"""
        + content
        + "</main></html>",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


def register_chatgpt_routes(service, settings_provider, build_client):
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, CallbackLogFilter) for item in access_logger.filters):
        access_logger.addFilter(CallbackLogFilter())

    def reset_model(settings):
        clear_selected_model("chatgpt", settings.llm_model_selection_file)

    @service.get("/chatgpt", response_class=HTMLResponse)
    def panel(request: Request, settings: Settings = Depends(settings_provider)):
        require_chatgpt_admin(request, settings)
        auth = get_chatgpt_auth(settings)
        status = auth.status()
        escape = html.escape
        notice = ""
        if settings.llm_provider != "chatgpt":
            notice = '<p class="notice">Para ativar esta opção nos comandos, configura <code>LLM_PROVIDER=chatgpt</code> e reinicia a API.</p>'
        account = escape(status["email"] or "Sem conta ligada")
        content = notice + f'<section class="card"><h2>Conta</h2><p>{account}</p>'
        content += '<form method="post" action="/chatgpt/login"><button>Continue with ChatGPT</button></form>'
        if status["accounts"]:
            content += '<form method="post" action="/chatgpt/login"><input type="hidden" name="new_account" value="true"><p><button class="secondary">Adicionar outra conta</button></p></form>'
            choices = "".join(
                f'<option value="{escape(item["client_id"])}" {"selected" if item["client_id"] == status["active_client_id"] else ""}>{escape(item["email"] or "Conta")} · {escape(item["client_id"][-8:])}</option>'
                for item in status["accounts"]
            )
            content += (
                '<form method="post" action="/chatgpt/account"><label>Contas guardadas<select name="client_id">'
                + choices
                + '</select></label><button class="secondary">Usar esta conta</button></form>'
            )
        content += "</section>"
        if status["connected"]:
            try:
                models = auth.models()
                selected = (
                    current_model(settings)
                    if settings.llm_provider == "chatgpt"
                    else settings.chatgpt_model
                )
                choices = "".join(
                    f'<option value="{escape(item["slug"])}" {"selected" if item["slug"] == selected else ""}>{escape(item["display_name"])} ({escape(item["slug"])})</option>'
                    for item in models
                )
                content += (
                    '<section class="card"><h2>Modelo</h2><p>Modelo ativo: <strong>'
                    + escape(selected or "Ainda não escolhido")
                    + "</strong></p>"
                )
                if choices:
                    content += (
                        '<form method="post" action="/chatgpt/model"><label>Modelos disponíveis à tua conta<select name="model">'
                        + choices
                        + "</select></label><button>Testar e ativar modelo</button></form>"
                    )
                else:
                    content += "<p>Esta conta não devolveu modelos disponíveis.</p>"
                content += "</section>"
            except ChatGPTError as exc:
                content += '<p class="notice">' + escape(str(exc)) + "</p>"
            content += '<form method="post" action="/chatgpt/logout"><button class="secondary">Terminar sessão</button></form>'
        content += '<p><a href="https://chatgpt.com/settings/usage" rel="noreferrer">Gerir utilização no ChatGPT</a></p><small>A conta escolhida serve os comandos, resumos e perfis deste bot. O plano mantém os seus limites de utilização.</small>'
        return page(content)

    @service.post("/chatgpt/login")
    def login(
        request: Request,
        new_account: bool = Form(False),
        settings: Settings = Depends(settings_provider),
    ):
        require_chatgpt_admin(request, settings)
        # Match the browser host to the supported loopback callback. On a VM,
        # open the panel through SSH forwarding rather than the VM's IP address.
        if (
            str(request.base_url).rstrip("/")
            != urlsplit(settings.chatgpt_redirect_uri)
            ._replace(path="", query="", fragment="")
            .geturl()
        ):
            raise HTTPException(
                400,
                "Abre o painel na origem de CHATGPT_REDIRECT_URI, através do túnel SSH se necessário.",
            )
        try:
            url, state = get_chatgpt_auth(settings).start_login(new_account=new_account)
        except ChatGPTError as exc:
            return page(
                '<p class="notice">'
                + html.escape(str(exc))
                + '</p><a href="/chatgpt">Voltar</a>',
                status_code=400,
            )
        response = RedirectResponse(
            url,
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.set_cookie(
            "chatgpt_oauth_state",
            state,
            max_age=600,
            httponly=True,
            samesite="lax",
            path="/auth/callback",
        )
        return response

    @service.get("/auth/callback", response_class=HTMLResponse)
    def callback(request: Request, settings: Settings = Depends(settings_provider)):
        query = request.query_params
        try:
            changed = get_chatgpt_auth(settings).finish_login(
                state=query.get("state", ""),
                browser_state=request.cookies.get("chatgpt_oauth_state", ""),
                code=query.get("code", ""),
                client_id=query.get("client_id", ""),
                error=query.get("error", ""),
            )
            if changed:
                reset_model(settings)
            response = page(
                '<p>Sessão ChatGPT ligada. Escolhe e testa o modelo para o bot.</p><a href="/chatgpt">Continuar para a escolha do modelo</a>'
            )
        except ChatGPTError as exc:
            response = page(
                '<p class="notice">'
                + html.escape(str(exc))
                + '</p><a href="/chatgpt">Voltar ao login</a>',
                status_code=400,
            )
        response.delete_cookie("chatgpt_oauth_state", path="/auth/callback")
        return response

    @service.post("/chatgpt/account")
    def account(
        request: Request,
        client_id: str = Form(...),
        settings: Settings = Depends(settings_provider),
    ):
        require_chatgpt_admin(request, settings)
        try:
            get_chatgpt_auth(settings).switch_account(client_id)
            reset_model(settings)
        except ChatGPTError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/chatgpt", status_code=303)

    @service.post("/chatgpt/model")
    def model(
        request: Request,
        model: str = Form(...),
        settings: Settings = Depends(settings_provider),
    ):
        require_chatgpt_admin(request, settings)
        if settings.llm_provider != "chatgpt":
            raise HTTPException(409, "Configura LLM_PROVIDER=chatgpt e reinicia a API.")
        try:
            candidate = build_client(settings, model)
            if model not in candidate.list_models():
                raise ChatGPTError("O modelo não está disponível à conta selecionada.")
            answer = candidate.test_model()
            select_model("chatgpt", model, settings.llm_model_selection_file)
        except (ChatGPTError, RuntimeError) as exc:
            return page(
                '<p class="notice">'
                + html.escape(str(exc))
                + '</p><a href="/chatgpt">Voltar</a>',
                status_code=502,
            )
        return page(
            "<p>Modelo ativo: <strong>"
            + html.escape(model)
            + "</strong></p><p>Teste: "
            + html.escape(answer)
            + '</p><a href="/chatgpt">Voltar ao painel</a>'
        )

    @service.post("/chatgpt/logout")
    def logout(request: Request, settings: Settings = Depends(settings_provider)):
        require_chatgpt_admin(request, settings)
        confirmed = get_chatgpt_auth(settings).logout()
        message = (
            "Sessão terminada."
            if confirmed
            else "Sessão terminada localmente. A revogação remota não foi confirmada; desliga a aplicação nas definições do ChatGPT."
        )
        return page("<p>" + message + '</p><a href="/chatgpt">Voltar</a>')
