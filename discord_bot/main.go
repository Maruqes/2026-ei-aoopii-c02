package main

import (
	"context"
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
var botAPIClient *TranscriptionClient

func registerCommands(dg *discordgo.Session, appID string) error {
	dg.AddHandler(func(s *discordgo.Session, i *discordgo.InteractionCreate) {
		if i.Type != discordgo.InteractionApplicationCommand {
			return
		}
		handleCommand(s, i)
	})

	_, err := dg.ApplicationCommandBulkOverwrite(appID, "", []*discordgo.ApplicationCommand{
		{
			Name:        "ping",
			Description: "Responde com PONG!",
		},
		{
			Name:        "start",
			Description: "Reativa o comportamento normal do bot.",
		},
		{
			Name:        "stop",
			Description: "Pausa o bot, derruba as calls e bloqueia novas entradas.",
		},
		{
			Name:        "profile",
			Description: "Mostra o perfil gerado de um utilizador.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionUser,
					Name:        "user",
					Description: "Utilizador a consultar. Se vazio, usa quem chamou o comando.",
					Required:    false,
				},
			},
		},
		{
			Name:        "prompt",
			Description: "Faz uma pergunta ao antropologo sobre a lore de um utilizador.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionUser,
					Name:        "user",
					Description: "Utilizador cuja lore deve ser consultada.",
					Required:    true,
				},
				{
					Type:        discordgo.ApplicationCommandOptionString,
					Name:        "question",
					Description: "Pergunta a responder com base no ficheiro de lore do utilizador.",
					Required:    true,
				},
			},
		},
		{
			Name:        "sync",
			Description: "Forca a sincronizacao dos perfis com as mensagens de texto guardadas.",
		},
	})
	if err != nil {
		return err
	}
	return nil
}

func handleCommand(s *discordgo.Session, i *discordgo.InteractionCreate) {
	data := i.ApplicationCommandData()
	switch strings.ToLower(data.Name) {
	case "ping":
		pingHook(s, i)
	case "start":
		startHook(s, i)
	case "stop":
		stopHook(s, i)
	case "profile":
		profileHook(s, i)
	case "prompt":
		promptHook(s, i)
	case "sync":
		syncHook(s, i)
	}
}

func pingHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{Content: "PONGGG!"},
	})
}

func startHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	setBotEnabled(true)
	respondText(s, i, "Bot reativado. Vou voltar a entrar nas calls normalmente.")
}

func stopHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	setBotEnabled(false)
	stopAllVoiceConnections()
	respondText(s, i, "Bot pausado. Sai de todas as calls e nao vai entrar em novas calls até /start.")
}

func profileHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	targetID, targetName := profileTarget(s, i)
	if targetID == "" {
		respondText(s, i, "Nao consegui identificar o utilizador.")
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	profile, err := client.GetUserProfile(context.Background(), targetID)
	if err != nil {
		respondText(s, i, fmt.Sprintf("Ainda nao ha perfil para %s.", targetName))
		return
	}
	if strings.TrimSpace(profile.Summary+profile.Interests+profile.CommunicationStyle+profile.PersonaNotes+profile.RecentUpdates) == "" {
		respondText(s, i, fmt.Sprintf("Ainda nao ha perfil gerado para %s.", displayProfileName(profile, targetName)))
		return
	}

	embed := profileEmbed(profile, targetName)
	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{Embeds: []*discordgo.MessageEmbed{embed}},
	})
}

func syncHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	respondText(s, i, "Sincronizacao de texto iniciada. Aviso aqui quando terminar.")

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	channelID := i.ChannelID

	go func() {
		result, err := client.ForceTextProfileSync(context.Background())
		if err != nil {
			_, _ = s.ChannelMessageSend(channelID, fmt.Sprintf("Sincronizacao de texto falhou: %v", err))
			return
		}
		_, _ = s.ChannelMessageSend(
			channelID,
			fmt.Sprintf(
				"Sincronizacao de texto concluida: %d perfis atualizados em %.1fs.",
				result.UpdatedProfiles,
				float64(result.ProcessingMS)/1000,
			),
		)
	}()
}

func promptHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	targetID, targetName := promptTarget(s, i)
	question := promptQuestion(i)
	if targetID == "" {
		respondText(s, i, "Nao consegui identificar o utilizador.")
		return
	}
	if strings.TrimSpace(question) == "" {
		respondText(s, i, "Escreve uma pergunta para eu fazer ao antropologo.")
		return
	}

	respondText(s, i, fmt.Sprintf("A consultar a lore de %s...", targetName))

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	channelID := i.ChannelID

	go func() {
		response, err := client.PromptUserProfile(context.Background(), targetID, question)
		if err != nil {
			_, _ = s.ChannelMessageSend(channelID, fmt.Sprintf("Nao consegui consultar a lore de %s: %v", targetName, err))
			return
		}
		name := firstNonEmpty(stringValue(response.DisplayName), response.Username, targetName, response.DiscordID)
		title := strings.TrimSpace(response.AnthropologistTitle)
		header := fmt.Sprintf("**%s**", name)
		if title != "" {
			header = fmt.Sprintf("%s - %s", header, title)
		}
		message := fmt.Sprintf("%s\n> %s\n\n%s", header, truncateDiscordMessage(response.Question), response.Answer)
		_, _ = s.ChannelMessageSend(channelID, truncateDiscordMessage(message))
	}()
}

func profileTarget(s *discordgo.Session, i *discordgo.InteractionCreate) (string, string) {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if option.Name == "user" {
			user := option.UserValue(s)
			return user.ID, firstNonEmpty(user.GlobalName, user.Username, user.ID)
		}
	}
	if i.Member != nil && i.Member.User != nil {
		return i.Member.User.ID, firstNonEmpty(i.Member.Nick, i.Member.User.GlobalName, i.Member.User.Username, i.Member.User.ID)
	}
	if i.User != nil {
		return i.User.ID, firstNonEmpty(i.User.GlobalName, i.User.Username, i.User.ID)
	}
	return "", ""
}

func promptTarget(s *discordgo.Session, i *discordgo.InteractionCreate) (string, string) {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if option.Name == "user" {
			user := option.UserValue(s)
			return user.ID, firstNonEmpty(user.GlobalName, user.Username, user.ID)
		}
	}
	return "", ""
}

func promptQuestion(i *discordgo.InteractionCreate) string {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if option.Name == "question" {
			return strings.TrimSpace(option.StringValue())
		}
	}
	return ""
}

func profileEmbed(profile *UserProfileResponse, fallbackName string) *discordgo.MessageEmbed {
	name := displayProfileName(profile, fallbackName)
	fields := []*discordgo.MessageEmbedField{
		profileField("Title", profile.AnthropologistTitle),
		profileField("Field Impression", profile.Summary),
		profileField("Interests and Artifacts", profile.Interests),
		profileField("Native Dialect", profile.CommunicationStyle),
		profileField("Social Role and Group Dynamics", profile.PersonaNotes),
		profileField("Current Pattern Notes", profile.RecentUpdates),
	}
	if strings.TrimSpace(stringValue(profile.ProfileFileURL)) != "" {
		fields = append(fields, profileField("Profile File", stringValue(profile.ProfileFileURL)))
	}
	return &discordgo.MessageEmbed{
		Title:  "Profile: " + name,
		Color:  0x2F80ED,
		Fields: fields,
	}
}

func profileField(name string, value string) *discordgo.MessageEmbedField {
	value = strings.TrimSpace(value)
	if value == "" {
		value = "No observations yet."
	}
	return &discordgo.MessageEmbedField{
		Name:   name,
		Value:  truncateDiscordField(value),
		Inline: false,
	}
}

func displayProfileName(profile *UserProfileResponse, fallback string) string {
	if profile == nil {
		return fallback
	}
	return firstNonEmpty(stringValue(profile.DisplayName), profile.Username, fallback, profile.DiscordID)
}

func respondText(s *discordgo.Session, i *discordgo.InteractionCreate, content string) {
	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{Content: content},
	})
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func truncateDiscordField(value string) string {
	if len(value) <= 1000 {
		return value
	}
	return value[:997] + "..."
}

func truncateDiscordMessage(value string) string {
	if len(value) <= 1900 {
		return value
	}
	return value[:1897] + "..."
}

func main() {
	_ = godotenv.Load("../.env", ".env")

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

	botAPIClient = NewTranscriptionClientFromEnv()

	err = registerCommands(dg, appID)
	if err != nil {
		log.Fatalf("erro ao registar comandos: %v", err)
	}
	dg.AddHandler(OnVoiceStateUpdate)
	dg.AddHandler(OnMessageCreate)

	dg.Identify.Intents = discordgo.IntentsGuilds |
		discordgo.IntentsGuildVoiceStates |
		discordgo.IntentsGuildMessages |
		discordgo.IntentsMessageContent

	if err := dg.Open(); err != nil {
		log.Fatalf("erro ao ligar bot: %v", err)
	}
	defer dg.Close()

	fmt.Println("Bot online. Comandos: /ping /start /stop /profile /sync")
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
}
