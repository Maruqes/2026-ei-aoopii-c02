#!/usr/bin/env python3
"""Open the official ChatGPT login in the HOST browser, save tokens in api_state."""

import argparse
import json
import os
import subprocess
import sys
import webbrowser


def handle_event(event, *, no_browser=False):
    if event.get("event") == "authorize":
        url = event["url"]
        print("Login ChatGPT:\n" + url, flush=True)
        if not no_browser and (
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or sys.platform in {"win32", "darwin"}
        ):
            if webbrowser.open(url):
                print("Navegador aberto. Conclui o login para continuar.", flush=True)
                return
        print(
            f"Abre o link no teu navegador. Numa VM, encaminha a porta {event['port']} por SSH (ver CHATGPT_SETUP.md).",
            flush=True,
        )
    elif event.get("event") == "status":
        print(json.dumps(event, ensure_ascii=False, indent=2), flush=True)
    else:
        print(event.get("message", ""), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    args, remaining = parser.parse_known_args()
    if "--" not in remaining:
        parser.error("Expected '-- docker compose ...' after login options")
    separator = remaining.index("--")
    options, compose = remaining[:separator], remaining[separator + 1 :]
    command = (
        compose
        + [
            "run",
            "--build",
            "--rm",
            "--no-deps",
            "-T",
            "codex",
            "python",
            "-m",
            "app.chatgpt_login",
        ]
        + options
    )
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, text=True, bufsize=1
        )
    except OSError as exc:
        print(f"Não foi possível iniciar o login: {exc}", file=sys.stderr)
        return 1
    try:
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                print(line, end="", flush=True)
                continue
            if isinstance(event, dict) and "event" in event:
                handle_event(event, no_browser=args.no_browser)
            else:
                print(line, end="", flush=True)
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        return 130
    finally:
        process.stdout.close()


if __name__ == "__main__":
    sys.exit(main())
