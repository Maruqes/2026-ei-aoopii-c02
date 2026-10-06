# RTP / Realtime investigation

The VM checkout `/home/commov/2026-ei-aoopii-c02` was inspected over SSH through
the host shown in the user's terminal, `commov@vm.commov`. The deployed checkout
was at `81d51f3`. Bot/API logs were read; no remote source, database rows or
running services were changed.

The collected bot log contains 39 `Late RTP overlaps` warnings, 296 RTP recovery
messages, 45 streaming promotion messages and no `corrupted stream`. The API log
contains 41 Realtime fallbacks and 25 Batch `invalid_input` deferrals. PostgreSQL
also reports deadlocks between Batch admission and Realtime completion.

## Confirmed causes and changes

- Realtime padding counted only the unfilled silence remainder, ignoring the
  gap reconstructed by Opus FEC/PLC. A reconstructed lost packet could therefore
  trigger the same fallback as received speech arriving too late. The WAV now
  replaces speculative padding with reconstructed audio, while upstream skips
  the portion of the gap already sent as silence. Both timelines retain the
  same duration; actual overlapping received speech still falls back to Batch.
- Audio waiting in the per-speaker reordering buffer could be classified as
  inactivity. Padding now waits for that buffer to drain and allows 250 ms for
  jitter before filling DTX silence (previously 60 ms).
- Closing the bot's WebSocket after a local capture abort marked the upstream
  provider as unhealthy, causing cooldowns and provider/token churn. Client
  disconnects now preserve upstream health and the participant's reservation;
  the failed WAV still becomes eligible for Batch recovery.
- Batch admission locked `voice_sessions` with `FOR UPDATE`, then waited on a
  recording held by Realtime. Realtime's foreign-key check requested `KEY SHARE`
  on the session, completing the deadlock cycle. Admission, new Realtime units
  and session retry now use `FOR NO KEY UPDATE`, retaining serialization against
  session changes while allowing foreign-key checks.

## Verification and limits

Go tests verify queued audio, ordinary jitter, partial/complete overlap with
reconstructed loss, exact WAV samples and live duration, preservation of genuinely
late received speech, and reservation retention after a local abort. They run
with the race detector; `go vet` also passes.

Python tests use a disposable local PostgreSQL instance and a simulated provider.
They cover client disconnect without provider cooldown and a concurrent session
admission/Realtime FK lock for Batch, Realtime and retry. Existing streaming,
repository and Deepgram tests are included.

The separate Batch `invalid_input` problem is not confirmed as fixed. Five failed
WAVs inspected in the VM had valid stereo 48 kHz / 16-bit headers and exact
declared file lengths. The configured model/language was `nova-3` / `pt-PT`.
Provider response details are not retained in the current logs, so the cause of
these rejections needs additional evidence. Older PostgreSQL logs also contain
user/text-chunk write deadlocks, distinct from the session/recording lock cycle
addressed here.

The changes are local and require deploying both `api` and `discord-bot` to the
VM before a live voice-session comparison can confirm the effect.
