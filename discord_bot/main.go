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

var APP_ID string

func registerCommand(dg *discordgo.Session, appID, command, desc string, hook func(s *discordgo.Session, i *discordgo.InteractionCreate)) error {

	dg.AddHandler(func(s *discordgo.Session, i *discordgo.InteractionCreate) {
		if i.Type != discordgo.InteractionApplicationCommand {
			return
		}

		hook(s, i)
	})

	_, err := dg.ApplicationCommandBulkOverwrite(appID, "", []*discordgo.ApplicationCommand{
		{
			Name:        command,
			Description: desc,
		},
	})
	if err != nil {
		return err
	}
	return nil
}

func pingHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	if i.Type != discordgo.InteractionApplicationCommand {
		return
	}

	data := i.ApplicationCommandData()
	if strings.ToLower(data.Name) != "ping" {
		return
	}

	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{Content: "PONGGG!"},
	})
}

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

	err = registerCommand(dg, appID, "ping", "Responde com PONG!", pingHook)
	if err != nil {
		log.Fatalf("erro ao registar comandos: %v", err)
	}
	dg.AddHandler(OnVoiceStateUpdate)

	dg.Identify.Intents = discordgo.IntentsGuilds | discordgo.IntentsGuildVoiceStates

	if err := dg.Open(); err != nil {
		log.Fatalf("erro ao ligar bot: %v", err)
	}
	defer dg.Close()

	fmt.Println("Bot online. Comando: /ping")
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
}
