module discord_bot-discord-bot

go 1.22.0

require (
	github.com/bwmarrin/discordgo v0.29.0
	github.com/google/uuid v1.6.0
	github.com/gorilla/websocket v1.4.2
	github.com/joho/godotenv v1.5.1
	gopkg.in/hraban/opus.v2 v2.0.0-20230925203106-0188a62cb302
)

require (
	github.com/cloudflare/circl v1.6.3 // indirect
	golang.org/x/crypto v0.32.0 // indirect
	golang.org/x/sys v0.29.0 // indirect
)

// Pinned yeongaori fork with the receive-buffer ownership fix;
// see third_party/discordgo/PATCHES.md.
replace github.com/bwmarrin/discordgo => ./third_party/discordgo
