# Plan: voice reply latency

Date: 2026-11-02. Status: implemented.

## Problem

A spoken greeting took roughly 20 seconds to answer and questions took longer.
Production logs for one turn (`session=409`) showed:

| Moment | Latency from the end of the question |
| --- | --- |
| `assistant turn closed` | 5.2 s (silence window) |
| `POST /v1/assistant/question` returned | 8.3 s (5.2 s silence + 3.2 s LLM) |
| `assistant delivery` (first audio) | 20.5 s |
| `assistant response` (playback finished) | 34.6 s |

So the question only needed ~3.2 s of LLM time; the remaining ten seconds were
spent before the first frame reached Discord, and 5.2 s of pure silence came
first.

## Measurements

- Edge TTS normally produces the first MP3 chunk after ~0.8 s and a full 13 s
  reply in ~1.3 s, but approximately one request in three stalls 3–5 s and
  occasional requests stall beyond 10 s. The old code buffered a whole segment
  (the neutral merge can make a whole reply a single segment) before decoding
  it, so any stall delayed every frame.
- The LLM (`deepseek-v4.1-flash` through OpenCode Go) answers in 3–4 s with a
  first token at ~2 s, even with an empty context.
- Discord's REST API answers in 0.3–0.6 s from the VPS.
- `ASSISTANT_SILENCE_SECONDS` defaulted to 5 s.

## Decision

1. Stream Edge audio instead of buffering a segment: pipe the MP3 chunks into
   ffmpeg as they arrive and write the decoded PCM immediately. The first frame
   no longer waits for the rest of the sentence.
2. Hedge a stalled first chunk: if Edge does not produce a first chunk within
   2.5 s, close the request and retry once. Piper only takes over when the
   retry also fails, so a normal reply keeps the natural voice.
3. Lower the default silence window to 2 s. The turn still waits for missing
   finals before asking, so a short window does not truncate a slow final.
4. Log the measured time to the first decoded frame (`speech first audio`) so
   later regressions are visible in production.

## Verification

- `go test ./...` and `python3 -m unittest test_speech_synthesis` (11 tests).
- Edge synthesis of a 13 s reply: first PCM at ~1.2 s (was 1.5 s), complete
  audio identical byte for byte (`ffmpeg -fflags nobuffer` was rejected because
  it drops the first 1.2 s).
- Deployed with `docker compose up -d --build discord-bot`.

## Follow-up

- The LLM is not streamed: the bot waits for the whole answer before
  synthesizing. Streaming the first sentence would remove another ~1.5 s.
- Background profile updates fail with `LLM output was truncated` every
  ~30–50 s (`LLM_MAX_OUTPUT_TOKENS=2500`), which wastes provider quota.
- One realtime recording stays in `fallback_pending`, so
  `Realtime recovery deferred unit=16670` logs every two seconds.
