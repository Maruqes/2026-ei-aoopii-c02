module discord_bot-discord-bot

go 1.22.0

require (
	github.com/bwmarrin/discordgo v0.29.0
	github.com/google/uuid v1.6.0
	github.com/joho/godotenv v1.5.1
	gopkg.in/hraban/opus.v2 v2.0.0-20230925203106-0188a62cb302
)

require (
	github.com/cloudflare/circl v1.6.3 // indirect
	github.com/gorilla/websocket v1.4.2 // indirect
	golang.org/x/crypto v0.32.0 // indirect
	golang.org/x/sys v0.29.0 // indirect
)

replace github.com/bwmarrin/discordgo => github.com/yeongaori/discordgo v0.0.0-20260321152711-3d3293e4c765
