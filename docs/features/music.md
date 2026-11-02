# F12 — Music in the call

Plays audio from public YouTube videos and keeps a queue in the call. It uses yt-dlp and FFmpeg; conversation recording continues during playback.

**Usage:** `/play url:...`, `/pause`, `/skip`, `/queue` and `/musicstop`. Controls require being in the same voice channel as the bot.

**Where it lives:**

- [music.go](../../discord_bot/music.go): queue, playback and coordination with synthesized voice.
- [music_commands.go](../../discord_bot/music_commands.go): commands, messages and access validation.
- [bot_language.go](../../discord_bot/bot_language.go): command registration.
- [Dockerfile](../../discord_bot/Dockerfile): tools required for playback.

**Context:** videos up to one hour and up to 20 tracks in the queue. The queue is kept in memory and disappears when the bot shuts down/restarts.
