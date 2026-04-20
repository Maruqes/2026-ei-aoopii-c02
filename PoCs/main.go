package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/bwmarrin/discordgo"
	"github.com/joho/godotenv"
)

func main() {
	_ = godotenv.Load()

	token := strings.TrimSpace(os.Getenv("DISCORD_TOKEN"))
	appID := strings.TrimSpace(os.Getenv("DISCORD_APP_ID"))
	if token == "" {
		log.Fatal("DISCORD_TOKEN nao definido")
	}
	if appID == "" {
		log.Fatal("DISCORD_APP_ID nao definido")
	}

	dg, err := discordgo.New("Bot " + token)
	if err != nil {
		log.Fatalf("erro ao criar sessao do bot: %v", err)
	}

	dg.AddHandler(func(s *discordgo.Session, i *discordgo.InteractionCreate) {
		if i.Type != discordgo.InteractionApplicationCommand {
			return
		}

		data := i.ApplicationCommandData()
		if strings.ToLower(data.Name) != "ping" {
			return
		}

		_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
			Type: discordgo.InteractionResponseChannelMessageWithSource,
			Data: &discordgo.InteractionResponseData{Content: "Pong!"},
		})
	})

	dg.Identify.Intents = discordgo.IntentsGuilds

	if err := dg.Open(); err != nil {
		log.Fatalf("erro ao ligar bot: %v", err)
	}
	defer dg.Close()

	_, err = dg.ApplicationCommandBulkOverwrite(appID, "", []*discordgo.ApplicationCommand{
		{
			Name:        "ping",
			Description: "Responde com Pong!",
		},
	})
	if err != nil {
		log.Fatalf("erro ao registar comando /ping: %v", err)
	}

	fmt.Println("Bot online. Comando: /ping")
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
}
