# Local DiscordGo patch

Source: `github.com/yeongaori/discordgo` at
`v0.0.0-20260321152711-3d3293e4c765` (the bot's existing pinned version).
This directory contains the Go packages and tests needed to build that version;
examples and upstream tooling are omitted. The upstream BSD license is retained.

## Received packet ownership

`voice.go`: decrypt incoming RTP payloads into independently allocated storage,
and copy the RTP extension preamble and payload into owned storage. Previously
`Packet.Opus` and `Packet.Extension` could reference the reusable UDP receive
buffer. The next datagram overwrote audio waiting in `OpusRecv` or the bot's
reordering queue, producing corrupted Opus or plausible but incorrect audio.
The DAVE passthrough and silence paths also returned these borrowed slices.

`voice_receive_test.go` reproduces this over encrypted loopback UDP for AES-GCM
and XChaCha20-Poly1305, with and without RTP extensions and DAVE passthrough.
Run it with `cd discord_bot/third_party/discordgo && go test -race ./...`.

Keep this local replacement until an upstream release fixes packet ownership;
run these regression tests against any proposed replacement before removing it.
