"""Loopback OAuth login, independent of the transcription API and database."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .chatgpt_auth import ChatGPTError, get_chatgpt_auth
from .config import Settings
from .model_selection import clear_selected_model


def callback_handler(auth, expected_state, result):
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Authorization codes and state must never enter access logs.

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/auth/callback":
                self.reply(404, "Callback desconhecido.")
                return
            # Compare against the state held by this login process, not a value
            # copied from the incoming request. PKCE and nonce are checked too.
            state = query.get("state", [""])[0]
            if state != expected_state or not state:
                self.reply(400, "Estado de login inválido. Volta ao link original.")
                return
            try:
                changed = auth.finish_login(
                    state=state,
                    browser_state=expected_state,
                    code=query.get("code", [""])[0],
                    client_id=query.get("client_id", [""])[0],
                    error=query.get("error", [""])[0],
                )
                result.update(done=True, changed=changed)
                self.reply(
                    200,
                    "Login concluído. Podes fechar este separador e voltar ao terminal.",
                )
            except ChatGPTError as exc:
                result.update(done=True, error=str(exc))
                self.reply(400, str(exc))

        def reply(self, status, message):
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Callback


def run(args, *, settings=None, emit=None):
    settings = settings or Settings.from_env()
    emit = emit or (
        lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
    )
    auth = get_chatgpt_auth(settings)
    if args.status:
        emit({"event": "status", **auth.status()})
        return 0
    if args.logout:
        revoked = auth.logout()
        emit(
            {
                "event": "message",
                "message": "Sessão local removida."
                + ("" if revoked else " Revogação remota não confirmada."),
            }
        )
        return 0
    if args.account:
        auth.switch_account(args.account)
        clear_selected_model("chatgpt", settings.llm_model_selection_file)
        emit(
            {
                "event": "message",
                "message": "Conta alterada. Escolhe o modelo com /models.",
            }
        )
        return 0
    parsed = urlsplit(settings.chatgpt_redirect_uri)
    port = args.port if args.port is not None else (parsed.port or 1455)
    if not 1 <= port <= 65535:
        raise ChatGPTError("Porta de callback inválida.")
    settings = replace(
        settings, chatgpt_redirect_uri=f"http://127.0.0.1:{port}/auth/callback"
    )
    auth = get_chatgpt_auth(settings)
    result = {}
    # Bind before advertising a login URL; a busy port must fail immediately.
    with HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler) as server:
        url, state = auth.start_login(
            new_account=args.new_account, include_id_token_hint=False
        )
        server.RequestHandlerClass = callback_handler(auth, state, result)
        server.timeout = 0.5
        emit({"event": "authorize", "url": url, "port": port})
        deadline = time.monotonic() + args.timeout
        while not result.get("done") and time.monotonic() < deadline:
            server.handle_request()
    if result.get("error"):
        raise ChatGPTError(result["error"])
    if not result.get("done"):
        raise ChatGPTError("O login expirou. Executa make codex novamente.")
    if result["changed"]:
        clear_selected_model("chatgpt", settings.llm_model_selection_file)
    emit(
        {
            "event": "message",
            "message": "Login concluído: "
            + (auth.status().get("email") or "ChatGPT")
            + ". Escolhe o modelo com /models e a effort com /effort.",
        }
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--new-account", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--logout", action="store_true")
    actions.add_argument("--account")
    args = parser.parse_args()
    try:
        return run(args)
    except (ChatGPTError, OSError, ValueError) as exc:
        print(
            json.dumps({"event": "error", "message": str(exc)}, ensure_ascii=False),
            flush=True,
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
