# ChatGPT: login, models and reasoning effort

This option uses the ChatGPT account to generate summaries, update profiles and answer
AI commands. Transcription keeps using `TRANSCRIPTION_PROVIDER`.
There is one active account per bot instance.

## Configuration

You need Docker Compose, Python 3 on the machine where you run `make`, a
ChatGPT account eligible to authorise plan usage and HTTPS access to
`auth.openai.com` and `api.openai.com`. Eligibility, models and limits
depend on the account and OpenAI policies; this option does not use `OPENAI_API_KEY`.

Keep the Discord, Postgres and transcription variables and add to `.env`:

```dotenv
LLM_PROVIDER=chatgpt
CHATGPT_ADMIN_PASSWORD=put_a_long_unique_secret_here
CHATGPT_MODEL=
CHATGPT_REASONING_EFFORT=medium
CHATGPT_REDIRECT_URI=http://127.0.0.1:1455/auth/callback
```

You can generate the secret with `openssl rand -hex 32`. `CHATGPT_ADMIN_PASSWORD`
authorises the bot's model and effort change requests to the API; it is not the
ChatGPT account password and is not requested in the browser.

Leave `CHATGPT_MODEL` empty to choose a model in Discord through
`/models`. `CHATGPT_REASONING_EFFORT` sets the initial level. A choice saved
by `/models` or `/effort` takes priority over `.env` and survives restarts.
After editing `.env`, recreate the services:

```bash
docker compose up -d --build
```

## Login on your own computer

At the project root:

```bash
make codex
```

The command builds a small login container, opens the official
OpenAI address in your computer's browser and waits for authorisation. Log in and authorise
ChatGPT plan usage. After confirmation, you can close the tab.
The command does not install or run the Codex CLI: it uses the official Sign in with
ChatGPT flow directly for this project.

Login works even with the API stopped. The callback listens only on
`127.0.0.1:1455`; there is no administration page on port 8000. The login
container uses the host network on Linux and exits when login finishes or is cancelled.
`make codex` uses port 1455 by default, even if `.env` still has an old
`CHATGPT_REDIRECT_URI`. Choose another port with `CODEX_ARGS="--port 1456"`.

Credentials are stored in `/app/.tmp/state/chatgpt/auth.json`, in the `api_state`
volume shared with the API. The session renews automatically. The file
has restricted permissions and must not be shared. `docker compose down -v`
deletes this volume, including the session and saved choices.

## Login on your VM

On your computer, open a terminal and keep this tunnel active:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:1455:127.0.0.1:1455 commov@vm.commov
```

In another terminal, enter the VM and run the login in the project folder:

```bash
ssh commov@vm.commov
cd ~/2026-ei-aoopii-c02
make codex
```

On a VM without a graphical environment, the command prints the link. Copy it into the
browser on your computer and log in. The browser returns to `127.0.0.1:1455`, and the tunnel
forwards the callback to the login process on the VM. Keep `make codex` active
until confirmation appears; afterwards you can close the tunnel.

There are two simultaneous processes: the tunnel on your computer and `make codex` on the
VM. Do not close the login to open the tunnel. If `make codex` runs on your
computer, the credentials stay in that computer's Docker; the tunnel to the VM
does not carry the callback to that process.

If the port is busy, use the same port on both sides, for example:

```bash
# On your computer:
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:1456:127.0.0.1:1456 commov@vm.commov

# On the VM:
make codex CODEX_ARGS="--port 1456"
```

For an already registered session, the port can change; keep the
`/auth/callback` path. Update the old `CHATGPT_REDIRECT_URI` in `.env` if it still
points to port 8000.

## Model and effort in Discord

After logging in, use `/models` to query the account catalog, choose a
model and test a short request before saving it. Changing the model requires
**Manage Server**. The selected model is then used by all AI functions.

If an identifier does not appear in the catalog, use `/models model:<id>` (for
example, `/models model:gpt-6-astra`). The bot tests access with the current account and
effort and only activates the model if the request succeeds. A name shown in
ChatGPT/Codex may not be a valid model identifier on this endpoint.

```text
/effort
/effort level:low
/effort level:medium
/effort level:high
/effort level:default
```

`/effort` shows the current model and level. With `level`, it tests the proposed level
on the active model and only saves the change if the request works. The command requires
**Manage Server** and the change applies to the next requests without a restart.
If you have not chosen a model yet, the test uses the first model in the catalog,
without selecting it. This lets you fix the initial effort before using `/models`.

The configurable values are `default`, `none`, `minimal`, `low`, `medium`,
`high`, `xhigh` and `max`; each model accepts only some of them. `default`
lets the model choose its level. If the model rejects the level, the bot keeps
the previous choice. `/models` also tests the new model with the current effort.

In `.env`, the equivalent configuration is:

```dotenv
CHATGPT_REASONING_EFFORT=low
```

Lower effort can reduce response time and reasoning usage.
Try `low` for faster answers if the model accepts it.
To switch to a model that does not accept the current effort, first use
`/effort level:default`, then `/models`, and choose the appropriate level.

## Checking status, switching accounts and logging out

```bash
make codex CODEX_ARGS="--status"
make codex CODEX_ARGS="--new-account"
make codex CODEX_ARGS="--account SAVED_CLIENT_ID"
make codex CODEX_ARGS="--logout"
```

`--status` shows saved accounts and their IDs, without tokens. `--new-account`
opens a new login; `--account` activates an existing registration. When switching accounts,
the saved model choice is removed: choose again through `/models`.
The effort level is kept.

`--logout` tries to revoke the active session on OpenAI and removes its local tokens.
If remote revocation is not confirmed, the command tells you. You can manage
access and check limits at [ChatGPT usage](https://chatgpt.com/settings/usage).

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `channel open failed` in the tunnel | Run `make codex` on the VM before opening the link; confirm that the tunnel port matches the callback port. |
| Callback answers on the VM but fails through SSH on Fedora 44 | Check the `selinux-policy` version; 44.7 has an SSH forwarding regression fixed in 44.9. Update the policy packages as shown below. |
| Port 1455 busy | Use `--port 1456` and the matching tunnel. |
| Login expires or invalid callback | Run `make codex` again and use the new link. |
| `/effort` does not appear yet | Rebuild and restart the bot to register the new Discord command. |
| Model rejects the effort | Use an accepted level or `default`; the previous choice is kept. |
| `No route to host` / DNS failure | Fix outbound connectivity for the VM and containers. Login over the host network does not fix the API network. |
| ChatGPT limit reached | Check account usage and wait for limits to reset. |
| Expired or revoked session | Run `make codex` again. |

With `make codex CODEX_ARGS="--port 1456"` still open on the VM, check the listener
in a second VM terminal:

```bash
ss -ltn 'sport = :1456'
curl --max-time 3 -i http://127.0.0.1:1456/auth/callback
```

You should see `LISTEN` and `curl` should receive **HTTP 400** with an invalid login
state, because this diagnostic request has no OAuth state. That
confirms the callback is reachable without consuming the login attempt.
If the connection is refused, login is not active on that VM/port. Start again with
`make codex`, keep the command open and use the new link.

On Fedora 44, `selinux-policy-44.7-1.fc44` has a
[known SSH forwarding regression](https://bugzilla.redhat.com/show_bug.cgi?id=2523732),
fixed in `selinux-policy-44.9-1.fc44`. If the callback answers inside the VM
but the tunnel still shows `connect failed`, check and update the policy on the VM:

```bash
rpm -q selinux-policy selinux-policy-targeted
sudo dnf upgrade --refresh selinux-policy selinux-policy-targeted
```

Then open a new SSH connection, start the login again and use the new link.
If it still fails after the update, reproduce the error and check recent
blocks with `sudo ausearch -m AVC -ts recent -c sshd-session`. This lets you
confirm the cause before changing any security configuration.

Login and changes were not performed on your account during testing;
the tests use simulated OAuth and inference responses.

## Official references

- [Registration and login](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Models and inference with the ChatGPT plan](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Reasoning effort](https://developers.openai.com/api/docs/guides/reasoning)
