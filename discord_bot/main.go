package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/bwmarrin/discordgo"
	"github.com/joho/godotenv"
)

var APP_ID string
var botAPIClient *TranscriptionClient

const modelSelectCustomID = "llm-model-select"
const modelPageCustomIDPrefix = "llm-models-page:"
const modelPageSize = 25
const discordMessageChunkLimit = 1900

func registerCommands(dg *discordgo.Session, appID string) error {
	dg.AddHandler(func(s *discordgo.Session, i *discordgo.InteractionCreate) {
		switch i.Type {
		case discordgo.InteractionApplicationCommand:
			handleCommand(s, i)
		case discordgo.InteractionMessageComponent:
			handleComponent(s, i)
		}
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
		{
			Name:        "models",
			Description: "Lista e permite alterar o modelo LLM ativo.",
		},
		{
			Name:        "health",
			Description: "Verifica API, Postgres e estado das transcricoes.",
		},
		{
			Name:        "forget",
			Description: "Apaga mensagens, perfil e lore de um utilizador.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionUser,
					Name:        "user",
					Description: "Utilizador a apagar da base de dados.",
					Required:    true,
				},
			},
		},
		{
			Name:        "timeout",
			Description: "Configura minutos antes de sair da call (BOT_LEAVE). 0 desativa.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionInteger,
					Name:        "minutes",
					Description: "Minutos na call antes de sair e processar. 0 = sem limite.",
					Required:    true,
					MinValue:    float64Pointer(0),
					MaxValue:    24 * 60,
				},
			},
		},
		{
			Name:        "recap",
			Description: "Mostra o resumo da ultima sessao de voz do servidor.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionInteger,
					Name:        "session",
					Description: "ID da sessao. Se vazio, usa a mais recente.",
					Required:    false,
					MinValue:    float64Pointer(1),
				},
			},
		},
		{
			Name:        "oracle",
			Description: "Pergunta ao antropologo sobre a historia do grupo.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionString,
					Name:        "question",
					Description: "Pergunta sobre decisoes, topicos ou lore do servidor.",
					Required:    true,
				},
			},
		},
		{
			Name:        "guess",
			Description: "Mini-jogo: adivinha quem disse uma frase da call.",
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
	case "models":
		modelsHook(s, i)
	case "health":
		healthHook(s, i)
	case "forget":
		forgetHook(s, i)
	case "timeout":
		timeoutHook(s, i)
	case "recap":
		recapHook(s, i)
	case "oracle":
		oracleHook(s, i)
	case "guess":
		guessHook(s, i)
	}
}

func handleComponent(s *discordgo.Session, i *discordgo.InteractionCreate) {
	customID := i.MessageComponentData().CustomID
	if customID == modelSelectCustomID {
		modelSelectHook(s, i)
		return
	}
	if strings.HasPrefix(customID, modelPageCustomIDPrefix) {
		modelsPageHook(s, i, customID)
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
	go rejoinActiveVoiceChannels(s)
}

func rejoinActiveVoiceChannels(s *discordgo.Session) {
	if s == nil || s.State == nil || s.State.User == nil {
		return
	}

	botUserID := s.State.User.ID
	for _, guild := range s.State.Guilds {
		if guild == nil || getVoiceConnection(guild.ID) != nil {
			continue
		}

		channelUsers := make(map[string]string)
		for _, voiceState := range guild.VoiceStates {
			if voiceState == nil || voiceState.ChannelID == "" || voiceState.UserID == botUserID {
				continue
			}
			if _, exists := channelUsers[voiceState.ChannelID]; !exists {
				channelUsers[voiceState.ChannelID] = voiceState.UserID
			}
		}

		for channelID, userID := range channelUsers {
			OnVoiceStateUpdate(s, &discordgo.VoiceStateUpdate{
				VoiceState: &discordgo.VoiceState{
					GuildID:   guild.ID,
					ChannelID: channelID,
					UserID:    userID,
				},
			})
			break
		}
	}
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
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, fmt.Sprintf("Ainda nao ha perfil para %s.", targetName))
			return
		}
		respondText(s, i, fmt.Sprintf("Erro ao consultar perfil de %s: %v", targetName, err))
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
		message := fmt.Sprintf("%s\n> %s\n\n%s", header, response.Question, response.Answer)
		if err := sendLongChannelMessage(s, channelID, message); err != nil {
			log.Printf("erro ao enviar resposta /prompt para canal %s: %v", channelID, err)
		}
	}()
}

func modelsHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	models, err := client.GetLLMModels(context.Background())
	if err != nil {
		respondText(s, i, fmt.Sprintf("Nao consegui listar os modelos: %v", err))
		return
	}
	if len(models.Models) == 0 {
		respondText(s, i, "O provider nao devolveu modelos disponiveis.")
		return
	}

	response := modelsResponse(models, 0)
	if response == nil {
		respondText(s, i, "Os IDs dos modelos devolvidos excedem o limite suportado pelo menu do Discord.")
		return
	}
	_ = s.InteractionRespond(i.Interaction, response)
}

func modelsPageHook(s *discordgo.Session, i *discordgo.InteractionCreate, customID string) {
	page, ok := parseModelPage(customID)
	if !ok {
		respondText(s, i, "Pagina de modelos invalida.")
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	models, err := client.GetLLMModels(context.Background())
	if err != nil {
		respondText(s, i, fmt.Sprintf("Nao consegui listar os modelos: %v", err))
		return
	}
	response := modelsResponse(models, page)
	if response == nil {
		respondText(s, i, "Os IDs dos modelos devolvidos excedem o limite suportado pelo menu do Discord.")
		return
	}
	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseUpdateMessage,
		Data: response.Data,
	})
}

func modelsResponse(models *LLMModelsResponse, page int) *discordgo.InteractionResponse {
	eligibleModels := selectableModels(models.Models)
	if len(eligibleModels) == 0 {
		return nil
	}
	pageCount := modelPageCount(eligibleModels, modelPageSize)
	page = clampModelPage(page, pageCount)
	displayedModels := modelMenuPageItems(eligibleModels, page, modelPageSize)
	options := make([]discordgo.SelectMenuOption, 0, len(displayedModels))
	for _, model := range displayedModels {
		options = append(options, discordgo.SelectMenuOption{
			Label:       truncateDiscordOption(model),
			Value:       model,
			Default:     model == models.CurrentModel,
			Description: modelDescription(model, models.CurrentModel),
		})
	}
	content := fmt.Sprintf(
		"Provider: **%s**\nModelo atual: **%s**\nEscolhe um modelo para o testar com `Ola!` e ativar.\nPagina %d/%d. A mostrar %d de %d modelos selecionaveis.",
		models.Provider,
		models.CurrentModel,
		page+1,
		pageCount,
		len(displayedModels),
		len(eligibleModels),
	)
	if skipped := len(models.Models) - len(eligibleModels); skipped > 0 {
		content += fmt.Sprintf("\n%d modelos foram omitidos porque excedem o limite de 100 caracteres do Discord.", skipped)
	}
	components := []discordgo.MessageComponent{
		discordgo.ActionsRow{
			Components: []discordgo.MessageComponent{
				discordgo.SelectMenu{
					MenuType:    discordgo.StringSelectMenu,
					CustomID:    modelSelectCustomID,
					Placeholder: "Seleciona o modelo LLM",
					MinValues:   intPointer(1),
					MaxValues:   1,
					Options:     options,
				},
			},
		},
	}
	if pageCount > 1 {
		components = append(components, discordgo.ActionsRow{
			Components: []discordgo.MessageComponent{
				discordgo.Button{
					Label:    "Anterior",
					Style:    discordgo.SecondaryButton,
					CustomID: modelPageCustomID(page - 1),
					Disabled: page == 0,
				},
				discordgo.Button{
					Label:    "Seguinte",
					Style:    discordgo.SecondaryButton,
					CustomID: modelPageCustomID(page + 1),
					Disabled: page >= pageCount-1,
				},
			},
		})
	}
	return &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{
			Content:    content,
			Components: components,
		},
	}
}

func modelSelectHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	data := i.MessageComponentData()
	if len(data.Values) != 1 {
		respondText(s, i, "Seleciona exatamente um modelo.")
		return
	}
	model := data.Values[0]
	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseDeferredMessageUpdate,
	})

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	result, err := client.SelectLLMModel(context.Background(), model)
	content := ""
	extraChunks := []string{}
	if err != nil {
		content = fmt.Sprintf("O modelo **%s** falhou o teste e nao foi ativado: %v", model, err)
	} else {
		message := fmt.Sprintf(
			"Modelo ativo: **%s** (`%s`).\nTeste com `Ola!`:\n%s",
			result.Model,
			result.Provider,
			result.TestResponse,
		)
		chunks := splitDiscordMessage(message)
		content = chunks[0]
		extraChunks = chunks[1:]
	}
	emptyComponents := []discordgo.MessageComponent{}
	_, _ = s.InteractionResponseEdit(i.Interaction, &discordgo.WebhookEdit{
		Content:    &content,
		Components: &emptyComponents,
	})
	for _, chunk := range extraChunks {
		if _, err := s.ChannelMessageSend(i.ChannelID, chunk); err != nil {
			log.Printf("erro ao enviar continuacao de /models para canal %s: %v", i.ChannelID, err)
			return
		}
	}
}

func modelDescription(model string, current string) string {
	if model == current {
		return "Modelo ativo"
	}
	return ""
}

func modelMenuItems(models []string, current string, limit int) []string {
	if limit <= 0 {
		return nil
	}
	eligible := selectableModels(models)
	if len(eligible) <= limit {
		return eligible
	}
	items := append([]string(nil), eligible[:limit]...)
	for _, model := range items {
		if model == current {
			return items
		}
	}
	for _, model := range eligible[limit:] {
		if model == current {
			items[limit-1] = current
			break
		}
	}
	return items
}

func selectableModels(models []string) []string {
	eligible := make([]string, 0, len(models))
	for _, model := range models {
		if len(model) <= 100 {
			eligible = append(eligible, model)
		}
	}
	return eligible
}

func modelMenuPageItems(models []string, page int, pageSize int) []string {
	if pageSize <= 0 {
		return nil
	}
	pageCount := modelPageCount(models, pageSize)
	if pageCount == 0 {
		return nil
	}
	page = clampModelPage(page, pageCount)
	start := page * pageSize
	end := start + pageSize
	if end > len(models) {
		end = len(models)
	}
	return append([]string(nil), models[start:end]...)
}

func modelPageCount(models []string, pageSize int) int {
	if pageSize <= 0 || len(models) == 0 {
		return 0
	}
	return (len(models) + pageSize - 1) / pageSize
}

func clampModelPage(page int, pageCount int) int {
	if page <= 0 || pageCount <= 0 {
		return 0
	}
	if page >= pageCount {
		return pageCount - 1
	}
	return page
}

func modelPageCustomID(page int) string {
	return fmt.Sprintf("%s%d", modelPageCustomIDPrefix, page)
}

func parseModelPage(customID string) (int, bool) {
	raw := strings.TrimPrefix(customID, modelPageCustomIDPrefix)
	if raw == customID || raw == "" {
		return 0, false
	}
	page, err := strconv.Atoi(raw)
	if err != nil || page < 0 {
		return 0, false
	}
	return page, true
}

func truncateDiscordOption(value string) string {
	if len(value) <= 100 {
		return value
	}
	return value[:97] + "..."
}

func intPointer(value int) *int {
	return &value
}

func float64Pointer(value float64) *float64 {
	return &value
}

func healthHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	health, err := client.GetHealth(context.Background())
	if err != nil {
		respondText(s, i, fmt.Sprintf("API indisponivel: %v", err))
		return
	}

	lines := []string{
		fmt.Sprintf("**API:** %s", health.Status),
		fmt.Sprintf("**Postgres:** %s", health.Database),
		fmt.Sprintf("**Bot:** %s", voiceConnectionStatus()),
		fmt.Sprintf("**BOT_LEAVE:** %s", formatBotLeaveMinutes(botLeaveMinutes())),
		fmt.Sprintf(
			"**Transcricoes:** %d em curso, %d falhadas, %d concluidas",
			health.RecordingsTranscribing,
			health.RecordingsFailed,
			health.RecordingsCompleted,
		),
	}

	if health.LastRecordingStatus != nil && strings.TrimSpace(*health.LastRecordingStatus) != "" {
		lastLine := fmt.Sprintf("**Ultima transcricao:** %s", *health.LastRecordingStatus)
		if health.LastRecordingFilename != nil && strings.TrimSpace(*health.LastRecordingFilename) != "" {
			lastLine += " (" + *health.LastRecordingFilename + ")"
		}
		if health.LastRecordingAt != nil {
			lastLine += " @ " + health.LastRecordingAt.UTC().Format(time.RFC3339)
		}
		lines = append(lines, lastLine)
	}

	respondText(s, i, strings.Join(lines, "\n"))
}

func formatBotLeaveMinutes(minutes int) string {
	if minutes == 0 {
		return "desativado (0)"
	}
	return fmt.Sprintf("%d min", minutes)
}

func forgetHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	targetID, targetName := forgetTarget(s, i)
	if targetID == "" {
		respondText(s, i, "Nao consegui identificar o utilizador.")
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	result, err := client.ForgetUser(context.Background(), targetID)
	if err != nil {
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, fmt.Sprintf("Nao ha dados guardados para %s.", targetName))
			return
		}
		respondText(s, i, fmt.Sprintf("Erro ao apagar dados de %s: %v", targetName, err))
		return
	}

	loreNote := "lore removida"
	if !result.LoreFileDeleted {
		loreNote = "sem ficheiro de lore"
	}
	respondText(
		s,
		i,
		fmt.Sprintf(
			"Dados de **%s** apagados: %d mensagens, perfil e %s.",
			targetName,
			result.MessagesDeleted,
			loreNote,
		),
	)
}

func timeoutHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	minutes := timeoutMinutes(i)
	if minutes < 0 {
		respondText(s, i, "Indica minutos validos (0 para desativar).")
		return
	}

	setBotLeaveMinutes(minutes)
	if minutes == 0 {
		respondText(s, i, "BOT_LEAVE desativado. O bot so sai quando a call fica vazia.")
		return
	}
	respondText(s, i, fmt.Sprintf("BOT_LEAVE configurado para %d min. Timer reiniciado nas calls ativas.", minutes))
}

func forgetTarget(s *discordgo.Session, i *discordgo.InteractionCreate) (string, string) {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if option.Name == "user" {
			user := option.UserValue(s)
			return user.ID, firstNonEmpty(user.GlobalName, user.Username, user.ID)
		}
	}
	return "", ""
}

func timeoutMinutes(i *discordgo.InteractionCreate) int {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if option.Name == "minutes" {
			return int(option.IntValue())
		}
	}
	return -1
}

func recapHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	guildID := i.GuildID
	if guildID == "" {
		respondText(s, i, "Este comando so funciona em um servidor.")
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	recap, err := client.GetGuildRecap(context.Background(), guildID, recapSessionID(i))
	if err != nil {
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, "Ainda nao ha sessoes de voz guardadas neste servidor.")
			return
		}
		respondText(s, i, fmt.Sprintf("Nao consegui obter o recap: %v", err))
		return
	}

	header := fmt.Sprintf(
		"**Recap** sessao #%d — %s",
		recap.SessionID,
		recap.ChannelName,
	)
	if recap.EndedAt != nil {
		header += fmt.Sprintf(" (%s → %s)", recap.StartedAt.UTC().Format("2006-01-02 15:04"), recap.EndedAt.UTC().Format("15:04"))
	} else {
		header += fmt.Sprintf(" (inicio %s)", recap.StartedAt.UTC().Format("2006-01-02 15:04"))
	}
	if recap.RecapSource == "error" {
		respondLongText(s, i, header+"\nResumo falhou: "+recap.Recap)
		return
	}
	if recap.RecapSource == "pending" {
		respondText(s, i, header+"\n"+recap.Recap)
		return
	}

	sourceNote := ""
	switch recap.RecapSource {
	case "transcript":
		sourceNote = "_transcricao bruta_"
	case "summary":
		sourceNote = "_resumo LLM_"
	}
	message := fmt.Sprintf("%s %s\n\n%s", header, sourceNote, recap.Recap)
	respondLongText(s, i, message)
}

func oracleHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	guildID := i.GuildID
	if guildID == "" {
		respondText(s, i, "Este comando so funciona em um servidor.")
		return
	}

	question := oracleQuestion(i)
	if strings.TrimSpace(question) == "" {
		respondText(s, i, "Escreve uma pergunta para o oraculo.")
		return
	}

	respondText(s, i, "A consultar a memoria do grupo...")

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	channelID := i.ChannelID

	go func() {
		response, err := client.AskGuildOracle(context.Background(), guildID, question)
		if err != nil {
			if strings.Contains(err.Error(), "404") {
				_, _ = s.ChannelMessageSend(channelID, "Ainda nao ha contexto guardado neste servidor para responder.")
				return
			}
			_, _ = s.ChannelMessageSend(channelID, fmt.Sprintf("O oraculo nao respondeu: %v", err))
			return
		}
		message := fmt.Sprintf("**Oraculo**\n> %s\n\n%s", response.Question, response.Answer)
		if err := sendLongChannelMessage(s, channelID, message); err != nil {
			log.Printf("erro ao enviar resposta /oracle para canal %s: %v", channelID, err)
		}
	}()
}

func guessHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	guildID := i.GuildID
	if guildID == "" {
		respondText(s, i, "Este comando so funciona em um servidor.")
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	guess, err := client.GetGuildGuess(context.Background(), guildID)
	if err != nil {
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, "Ainda nao ha frases de voz suficientes para o jogo neste servidor.")
			return
		}
		respondText(s, i, fmt.Sprintf("Nao consegui preparar o jogo: %v", err))
		return
	}

	lines := []string{
		"**Quem disse isto?**",
		"> " + guess.Quote,
	}
	if len(guess.Options) > 0 {
		optionLines := make([]string, 0, len(guess.Options))
		for index, option := range guess.Options {
			optionLines = append(optionLines, fmt.Sprintf("%d. %s", index+1, option))
		}
		lines = append(lines, "", strings.Join(optionLines, "\n"))
	}
	lines = append(lines, "", fmt.Sprintf("||Resposta: %s||", guess.CorrectDisplayName))
	if guess.ChannelName != nil && strings.TrimSpace(*guess.ChannelName) != "" {
		lines = append(lines, fmt.Sprintf("_Canal: %s_", strings.TrimSpace(*guess.ChannelName)))
	}

	respondLongText(s, i, strings.Join(lines, "\n"))
}

func recapSessionID(i *discordgo.InteractionCreate) int64 {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if option.Name == "session" {
			return option.IntValue()
		}
	}
	return 0
}

func oracleQuestion(i *discordgo.InteractionCreate) string {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if option.Name == "question" {
			return strings.TrimSpace(option.StringValue())
		}
	}
	return ""
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

func respondLongText(s *discordgo.Session, i *discordgo.InteractionCreate, content string) {
	chunks := splitDiscordMessage(content)
	if len(chunks) == 0 {
		respondText(s, i, "")
		return
	}
	respondText(s, i, chunks[0])
	for _, chunk := range chunks[1:] {
		if _, err := s.ChannelMessageSend(i.ChannelID, chunk); err != nil {
			log.Printf("erro ao enviar continuacao para canal %s: %v", i.ChannelID, err)
			return
		}
	}
}

func sendLongChannelMessage(s *discordgo.Session, channelID string, content string) error {
	for _, chunk := range splitDiscordMessage(content) {
		if _, err := s.ChannelMessageSend(channelID, chunk); err != nil {
			return err
		}
	}
	return nil
}

func splitDiscordMessage(value string) []string {
	return splitDiscordMessageAt(value, discordMessageChunkLimit)
}

func splitDiscordMessageAt(value string, limit int) []string {
	value = strings.TrimSpace(value)
	if value == "" {
		return []string{""}
	}
	if limit <= 0 {
		return []string{value}
	}

	chunks := []string{}
	for len(value) > limit {
		split := bestDiscordSplit(value, limit)
		chunk := strings.TrimSpace(value[:split])
		if chunk != "" {
			chunks = append(chunks, chunk)
		}
		value = strings.TrimSpace(value[split:])
	}
	if value != "" {
		chunks = append(chunks, value)
	}
	return chunks
}

func bestDiscordSplit(value string, limit int) int {
	safeLimit := safeByteLimit(value, limit)
	if safeLimit <= 0 {
		return len(value)
	}
	window := value[:safeLimit]
	minUsefulSplit := safeLimit / 2
	for _, separator := range []string{"\n\n", "\n", ". ", "; ", ", ", " "} {
		if index := strings.LastIndex(window, separator); index >= minUsefulSplit {
			if strings.TrimSpace(window[:index]) != "" {
				return index + separatorLenToKeep(separator)
			}
		}
	}
	return safeLimit
}

func separatorLenToKeep(separator string) int {
	if strings.TrimSpace(separator) == "" || strings.Contains(separator, "\n") {
		return 0
	}
	return len(separator)
}

func safeByteLimit(value string, limit int) int {
	if len(value) <= limit {
		return len(value)
	}
	safe := 0
	for index := range value {
		if index > limit {
			break
		}
		safe = index
	}
	if safe == 0 {
		for index := range value {
			if index > 0 {
				return index
			}
		}
		return len(value)
	}
	return safe
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

	fmt.Println("Bot online. Comandos: /ping /start /stop /profile /prompt /sync /models /health /forget /timeout /recap /oracle /guess")
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
}
