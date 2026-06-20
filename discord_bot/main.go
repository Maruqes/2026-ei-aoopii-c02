package main

import (
	"context"
	"fmt"
	"log"
	"math"
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

	return overwriteCommands(dg, appID)
}

func overwriteCommands(dg *discordgo.Session, appID string) error {
	_, err := dg.ApplicationCommandBulkOverwrite(appID, "", buildApplicationCommands(currentBotLanguage()))
	if err != nil {
		return err
	}
	return nil
}

func handleCommand(s *discordgo.Session, i *discordgo.InteractionCreate) {
	data := i.ApplicationCommandData()
	name := strings.ToLower(data.Name)
	switch {
	case commandMatches(name, "ping"):
		pingHook(s, i)
	case commandMatches(name, "start"):
		startHook(s, i)
	case commandMatches(name, "stop"):
		stopHook(s, i)
	case commandMatches(name, "profile"):
		profileHook(s, i)
	case commandMatches(name, "prompt"):
		promptHook(s, i)
	case commandMatches(name, "sync"):
		syncHook(s, i)
	case commandMatches(name, "models"):
		modelsHook(s, i)
	case commandMatches(name, "health"):
		healthHook(s, i)
	case commandMatches(name, "keys"):
		keysHook(s, i)
	case commandMatches(name, "forget"):
		forgetHook(s, i)
	case commandMatches(name, "timeout"):
		timeoutHook(s, i)
	case commandMatches(name, "recap"):
		recapHook(s, i)
	case commandMatches(name, "oracle"):
		oracleHook(s, i)
	case commandMatches(name, "guess"):
		guessHook(s, i)
	case commandMatches(name, "language"):
		languageHook(s, i)
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
		Data: &discordgo.InteractionResponseData{Content: botText("PONGGG!", "PONGGG!")},
	})
}

func startHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	setBotEnabled(true)
	respondText(s, i, botText(
		"Bot reativado. Vou voltar a entrar nas calls normalmente.",
		"Bot reactivated. I will join calls normally again.",
	))
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
	respondText(s, i, botText(
		"Bot pausado. Sai de todas as calls e nao vou entrar em novas calls ate /comecar.",
		"Bot paused. I left all calls and will not join new calls until /start.",
	))
}

func profileHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	targetID, targetName := profileTarget(s, i)
	if targetID == "" {
		respondText(s, i, botText("Nao consegui identificar o utilizador.", "I could not identify the user."))
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	profile, err := client.GetUserProfile(context.Background(), targetID)
	if err != nil {
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, fmt.Sprintf(botText("Ainda nao ha perfil para %s.", "There is no profile for %s yet."), targetName))
			return
		}
		respondText(s, i, fmt.Sprintf(botText("Erro ao consultar perfil de %s: %v", "Error fetching profile for %s: %v"), targetName, err))
		return
	}
	if strings.TrimSpace(profile.Summary+profile.Interests+profile.CommunicationStyle+profile.PersonaNotes+profile.RecentUpdates) == "" {
		respondText(s, i, fmt.Sprintf(botText("Ainda nao ha perfil gerado para %s.", "There is no generated profile for %s yet."), displayProfileName(profile, targetName)))
		return
	}

	embed := profileEmbed(profile, targetName)
	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{Embeds: []*discordgo.MessageEmbed{embed}},
	})
}

func syncHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	respondText(s, i, textForLanguage(lang, "Sincronizacao de texto iniciada. Aviso aqui quando terminar.", "Text synchronization started. I will report back here when it finishes."))

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	channelID := i.ChannelID

	go func() {
		result, err := client.ForceTextProfileSync(context.Background())
		if err != nil {
			_, _ = s.ChannelMessageSend(channelID, fmt.Sprintf(textForLanguage(lang, "Sincronizacao de texto falhou: %v", "Text synchronization failed: %v"), err))
			return
		}
		_, _ = s.ChannelMessageSend(
			channelID,
			fmt.Sprintf(
				textForLanguage(lang, "Sincronizacao de texto concluida: %d perfis atualizados em %.1fs.", "Text synchronization complete: %d profiles updated in %.1fs."),
				result.UpdatedProfiles,
				float64(result.ProcessingMS)/1000,
			),
		)
	}()
}

func promptHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	targetID, targetName := promptTarget(s, i)
	question := promptQuestion(i)
	lang := currentBotLanguage()
	if targetID == "" {
		respondText(s, i, textForLanguage(lang, "Nao consegui identificar o utilizador.", "I could not identify the user."))
		return
	}
	if strings.TrimSpace(question) == "" {
		respondText(s, i, textForLanguage(lang, "Escreve uma pergunta para eu fazer ao antropologo.", "Write a question for me to ask the anthropologist."))
		return
	}

	respondText(s, i, fmt.Sprintf(textForLanguage(lang, "A consultar a lore de %s...", "Consulting %s's lore..."), targetName))

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	channelID := i.ChannelID

	go func() {
		response, err := client.PromptUserProfile(context.Background(), targetID, question, lang.apiValue())
		if err != nil {
			_, _ = s.ChannelMessageSend(channelID, fmt.Sprintf(textForLanguage(lang, "Nao consegui consultar a lore de %s: %v", "I could not consult %s's lore: %v"), targetName, err))
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
		respondText(s, i, fmt.Sprintf(botText("Nao consegui listar os modelos: %v", "I could not list the models: %v"), err))
		return
	}
	if len(models.Models) == 0 {
		respondText(s, i, botText("O provider nao devolveu modelos disponiveis.", "The provider returned no available models."))
		return
	}

	response := modelsResponse(models, 0)
	if response == nil {
		respondText(s, i, botText("Os IDs dos modelos devolvidos excedem o limite suportado pelo menu do Discord.", "The returned model IDs exceed Discord menu limits."))
		return
	}
	_ = s.InteractionRespond(i.Interaction, response)
}

func modelsPageHook(s *discordgo.Session, i *discordgo.InteractionCreate, customID string) {
	page, ok := parseModelPage(customID)
	if !ok {
		respondText(s, i, botText("Pagina de modelos invalida.", "Invalid model page."))
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	models, err := client.GetLLMModels(context.Background())
	if err != nil {
		respondText(s, i, fmt.Sprintf(botText("Nao consegui listar os modelos: %v", "I could not list the models: %v"), err))
		return
	}
	response := modelsResponse(models, page)
	if response == nil {
		respondText(s, i, botText("Os IDs dos modelos devolvidos excedem o limite suportado pelo menu do Discord.", "The returned model IDs exceed Discord menu limits."))
		return
	}
	_ = s.InteractionRespond(i.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseUpdateMessage,
		Data: response.Data,
	})
}

func modelsResponse(models *LLMModelsResponse, page int) *discordgo.InteractionResponse {
	lang := currentBotLanguage()
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
			Description: modelDescription(model, models.CurrentModel, lang),
		})
	}
	content := fmt.Sprintf(
		textForLanguage(
			lang,
			"Provider: **%s**\nModelo atual: **%s**\nEscolhe um modelo para o testar com `Ola!` e ativar.\nPagina %d/%d. A mostrar %d de %d modelos selecionaveis.",
			"Provider: **%s**\nCurrent model: **%s**\nChoose a model to test it with `Hello!` and activate it.\nPage %d/%d. Showing %d of %d selectable models.",
		),
		models.Provider,
		models.CurrentModel,
		page+1,
		pageCount,
		len(displayedModels),
		len(eligibleModels),
	)
	if skipped := len(models.Models) - len(eligibleModels); skipped > 0 {
		content += fmt.Sprintf(textForLanguage(
			lang,
			"\n%d modelos foram omitidos porque excedem o limite de 100 caracteres do Discord.",
			"\n%d models were omitted because they exceed Discord's 100-character limit.",
		), skipped)
	}
	components := []discordgo.MessageComponent{
		discordgo.ActionsRow{
			Components: []discordgo.MessageComponent{
				discordgo.SelectMenu{
					MenuType:    discordgo.StringSelectMenu,
					CustomID:    modelSelectCustomID,
					Placeholder: textForLanguage(lang, "Seleciona o modelo LLM", "Select the LLM model"),
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
					Label:    textForLanguage(lang, "Anterior", "Previous"),
					Style:    discordgo.SecondaryButton,
					CustomID: modelPageCustomID(page - 1),
					Disabled: page == 0,
				},
				discordgo.Button{
					Label:    textForLanguage(lang, "Seguinte", "Next"),
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
	lang := currentBotLanguage()
	data := i.MessageComponentData()
	if len(data.Values) != 1 {
		respondText(s, i, textForLanguage(lang, "Seleciona exatamente um modelo.", "Select exactly one model."))
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
		content = fmt.Sprintf(textForLanguage(lang, "O modelo **%s** falhou o teste e nao foi ativado: %v", "Model **%s** failed the test and was not activated: %v"), model, err)
	} else {
		message := fmt.Sprintf(
			textForLanguage(lang, "Modelo ativo: **%s** (`%s`).\nTeste com `Ola!`:\n%s", "Active model: **%s** (`%s`).\nTest with `Hello!`:\n%s"),
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

func modelDescription(model string, current string, lang botLanguage) string {
	if model == current {
		return textForLanguage(lang, "Modelo ativo", "Active model")
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
	lang := currentBotLanguage()
	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	health, err := client.GetHealth(context.Background())
	if err != nil {
		respondText(s, i, fmt.Sprintf(textForLanguage(lang, "API indisponivel: %v", "API unavailable: %v"), err))
		return
	}

	lines := []string{
		fmt.Sprintf("**API:** %s", health.Status),
		fmt.Sprintf("**Postgres:** %s", health.Database),
		fmt.Sprintf("**Bot:** %s", voiceConnectionStatusForLanguage(lang)),
		fmt.Sprintf("**BOT_LEAVE:** %s", formatBotLeaveMinutes(botLeaveMinutes(), lang)),
		fmt.Sprintf(
			textForLanguage(lang, "**Transcricoes:** %d em curso, %d falhadas, %d concluidas", "**Transcriptions:** %d running, %d failed, %d completed"),
			health.RecordingsTranscribing,
			health.RecordingsFailed,
			health.RecordingsCompleted,
		),
	}

	if health.LastRecordingStatus != nil && strings.TrimSpace(*health.LastRecordingStatus) != "" {
		lastLine := fmt.Sprintf(textForLanguage(lang, "**Ultima transcricao:** %s", "**Latest transcription:** %s"), *health.LastRecordingStatus)
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

func keysHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	keys, err := client.GetSpeechmaticsKeys(context.Background())
	if err != nil {
		respondText(s, i, fmt.Sprintf(textForLanguage(lang, "Nao consegui consultar as chaves Speechmatics: %v", "I could not fetch Speechmatics keys: %v"), err))
		return
	}
	if keys.Provider != "speechmatics" {
		respondText(s, i, fmt.Sprintf(textForLanguage(lang, "Speechmatics nao esta ativo. Provider atual: %s", "Speechmatics is not active. Current provider: %s"), keys.Provider))
		return
	}
	if len(keys.Keys) == 0 {
		respondText(s, i, textForLanguage(lang, "Nao ha chaves API Speechmatics configuradas.", "No Speechmatics API keys are configured."))
		return
	}

	lines := []string{
		fmt.Sprintf(textForLanguage(lang, "**Chaves Speechmatics** limite=%s", "**Speechmatics keys** limit=%s"), formatAPIHoursMinutes(keys.LimitHours)),
	}
	if keys.SelectedKey != nil && strings.TrimSpace(*keys.SelectedKey) != "" {
		lines = append(lines, fmt.Sprintf(textForLanguage(lang, "**A usar agora:** %s", "**Currently using:** %s"), *keys.SelectedKey))
	}
	for _, key := range keys.Keys {
		lines = append(lines, formatSpeechmaticsKeyLine(key, lang))
	}
	respondLongText(s, i, strings.Join(lines, "\n"))
}

func formatSpeechmaticsKeyLine(key SpeechmaticsKeyUsageResponse, lang botLanguage) string {
	if key.Error != nil && strings.TrimSpace(*key.Error) != "" {
		return fmt.Sprintf(textForLanguage(lang, "**%s:** erro: %s", "**%s:** error: %s"), key.Name, *key.Error)
	}

	used := "?"
	if key.UsedHours != nil {
		used = formatAPIHoursMinutes(*key.UsedHours)
	}
	limit := formatAPIHoursMinutes(key.LimitHours)
	percent := "?"
	if key.PercentUsed != nil {
		percent = formatPercent(*key.PercentUsed)
	}
	jobs := "?"
	if key.JobCount != nil {
		jobs = strconv.Itoa(*key.JobCount)
	}
	return fmt.Sprintf(textForLanguage(lang, "**%s:** %s %s/%s (%s tarefas)", "**%s:** %s %s/%s (%s jobs)"), key.Name, percent, used, limit, jobs)
}

func formatAPIHoursMinutes(hours float64) string {
	if hours < 0 {
		hours = 0
	}
	totalMinutes := int(math.Round(hours * 60))
	h := totalMinutes / 60
	m := totalMinutes % 60
	return fmt.Sprintf("%dh %02dm", h, m)
}

func formatPercent(value float64) string {
	return strings.TrimRight(strings.TrimRight(fmt.Sprintf("%.1f", value), "0"), ".") + "%"
}

func formatBotLeaveMinutes(minutes int, lang botLanguage) string {
	if minutes == 0 {
		return textForLanguage(lang, "desativado (0)", "disabled (0)")
	}
	return fmt.Sprintf("%d min", minutes)
}

func forgetHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	targetID, targetName := forgetTarget(s, i)
	if targetID == "" {
		respondText(s, i, textForLanguage(lang, "Nao consegui identificar o utilizador.", "I could not identify the user."))
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	result, err := client.ForgetUser(context.Background(), targetID)
	if err != nil {
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, fmt.Sprintf(textForLanguage(lang, "Nao ha dados guardados para %s.", "There is no stored data for %s."), targetName))
			return
		}
		respondText(s, i, fmt.Sprintf(textForLanguage(lang, "Erro ao apagar dados de %s: %v", "Error deleting data for %s: %v"), targetName, err))
		return
	}

	loreNote := textForLanguage(lang, "lore removida", "lore removed")
	if !result.LoreFileDeleted {
		loreNote = textForLanguage(lang, "sem ficheiro de lore", "no lore file")
	}
	respondText(
		s,
		i,
		fmt.Sprintf(
			textForLanguage(lang, "Dados de **%s** apagados: %d mensagens, perfil e %s.", "Deleted data for **%s**: %d messages, profile, and %s."),
			targetName,
			result.MessagesDeleted,
			loreNote,
		),
	)
}

func timeoutHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	minutes := timeoutMinutes(i)
	if minutes < 0 {
		respondText(s, i, textForLanguage(lang, "Indica minutos validos (0 para desativar).", "Provide valid minutes (0 to disable)."))
		return
	}

	setBotLeaveMinutes(minutes)
	if minutes == 0 {
		respondText(s, i, textForLanguage(lang, "BOT_LEAVE desativado. O bot so sai quando a call fica vazia.", "BOT_LEAVE disabled. The bot only leaves when the call is empty."))
		return
	}
	respondText(s, i, fmt.Sprintf(textForLanguage(lang, "BOT_LEAVE configurado para %d min. Timer reiniciado nas calls ativas.", "BOT_LEAVE set to %d min. Timer restarted in active calls."), minutes))
}

func forgetTarget(s *discordgo.Session, i *discordgo.InteractionCreate) (string, string) {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if optionMatches(option.Name, "user") {
			user := option.UserValue(s)
			return user.ID, firstNonEmpty(user.GlobalName, user.Username, user.ID)
		}
	}
	return "", ""
}

func timeoutMinutes(i *discordgo.InteractionCreate) int {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if optionMatches(option.Name, "minutes") {
			return int(option.IntValue())
		}
	}
	return -1
}

func recapHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	guildID := i.GuildID
	if guildID == "" {
		respondText(s, i, textForLanguage(lang, "Este comando so funciona em um servidor.", "This command only works in a server."))
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	recap, err := client.GetGuildRecap(context.Background(), guildID, recapSessionID(i))
	if err != nil {
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, textForLanguage(lang, "Ainda nao ha sessoes de voz guardadas neste servidor.", "There are no stored voice sessions in this server yet."))
			return
		}
		respondText(s, i, fmt.Sprintf(textForLanguage(lang, "Nao consegui obter o resumo: %v", "I could not fetch the recap: %v"), err))
		return
	}

	header := fmt.Sprintf(
		textForLanguage(lang, "**Resumo** sessao #%d — %s", "**Recap** session #%d — %s"),
		recap.SessionID,
		recap.ChannelName,
	)
	if recap.EndedAt != nil {
		header += fmt.Sprintf(" (%s → %s)", recap.StartedAt.UTC().Format("2006-01-02 15:04"), recap.EndedAt.UTC().Format("15:04"))
	} else {
		header += fmt.Sprintf(textForLanguage(lang, " (inicio %s)", " (started %s)"), recap.StartedAt.UTC().Format("2006-01-02 15:04"))
	}
	if recap.RecapSource == "error" {
		respondLongText(s, i, header+"\n"+textForLanguage(lang, "Resumo falhou: ", "Recap failed: ")+recap.Recap)
		return
	}
	if recap.RecapSource == "pending" {
		respondText(s, i, header+"\n"+recap.Recap)
		return
	}

	sourceNote := ""
	switch recap.RecapSource {
	case "transcript":
		sourceNote = textForLanguage(lang, "_transcricao bruta_", "_raw transcript_")
	case "summary":
		sourceNote = textForLanguage(lang, "_resumo LLM_", "_LLM summary_")
	}
	message := fmt.Sprintf("%s %s\n\n%s", header, sourceNote, recap.Recap)
	respondLongText(s, i, message)
}

func oracleHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	guildID := i.GuildID
	if guildID == "" {
		respondText(s, i, textForLanguage(lang, "Este comando so funciona em um servidor.", "This command only works in a server."))
		return
	}

	question := oracleQuestion(i)
	if strings.TrimSpace(question) == "" {
		respondText(s, i, textForLanguage(lang, "Escreve uma pergunta para o oraculo.", "Write a question for the oracle."))
		return
	}

	respondText(s, i, textForLanguage(lang, "A consultar a memoria do grupo...", "Consulting the group's memory..."))

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	channelID := i.ChannelID

	go func() {
		response, err := client.AskGuildOracle(context.Background(), guildID, question, lang.apiValue())
		if err != nil {
			if strings.Contains(err.Error(), "404") {
				_, _ = s.ChannelMessageSend(channelID, textForLanguage(lang, "Ainda nao ha contexto guardado neste servidor para responder.", "There is no stored context in this server to answer yet."))
				return
			}
			_, _ = s.ChannelMessageSend(channelID, fmt.Sprintf(textForLanguage(lang, "O oraculo nao respondeu: %v", "The oracle did not answer: %v"), err))
			return
		}
		message := fmt.Sprintf(textForLanguage(lang, "**Oraculo**\n> %s\n\n%s", "**Oracle**\n> %s\n\n%s"), response.Question, response.Answer)
		if err := sendLongChannelMessage(s, channelID, message); err != nil {
			log.Printf("erro ao enviar resposta /oracle para canal %s: %v", channelID, err)
		}
	}()
}

func guessHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	guildID := i.GuildID
	if guildID == "" {
		respondText(s, i, textForLanguage(lang, "Este comando so funciona em um servidor.", "This command only works in a server."))
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}

	guess, err := client.GetGuildGuess(context.Background(), guildID)
	if err != nil {
		if strings.Contains(err.Error(), "404") {
			respondText(s, i, textForLanguage(lang, "Ainda nao ha frases de voz suficientes para o jogo neste servidor.", "There are not enough voice quotes for the game in this server yet."))
			return
		}
		respondText(s, i, fmt.Sprintf(textForLanguage(lang, "Nao consegui preparar o jogo: %v", "I could not prepare the game: %v"), err))
		return
	}

	lines := []string{
		textForLanguage(lang, "**Quem disse isto?**", "**Who said this?**"),
		"> " + guess.Quote,
	}
	if len(guess.Options) > 0 {
		optionLines := make([]string, 0, len(guess.Options))
		for index, option := range guess.Options {
			optionLines = append(optionLines, fmt.Sprintf("%d. %s", index+1, option))
		}
		lines = append(lines, "", strings.Join(optionLines, "\n"))
	}
	lines = append(lines, "", fmt.Sprintf(textForLanguage(lang, "||Resposta: %s||", "||Answer: %s||"), guess.CorrectDisplayName))
	if guess.ChannelName != nil && strings.TrimSpace(*guess.ChannelName) != "" {
		lines = append(lines, fmt.Sprintf(textForLanguage(lang, "_Canal: %s_", "_Channel: %s_"), strings.TrimSpace(*guess.ChannelName)))
	}

	respondLongText(s, i, strings.Join(lines, "\n"))
}

func recapSessionID(i *discordgo.InteractionCreate) int64 {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if optionMatches(option.Name, "session") {
			return option.IntValue()
		}
	}
	return 0
}

func oracleQuestion(i *discordgo.InteractionCreate) string {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if optionMatches(option.Name, "question") {
			return strings.TrimSpace(option.StringValue())
		}
	}
	return ""
}

func profileTarget(s *discordgo.Session, i *discordgo.InteractionCreate) (string, string) {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if optionMatches(option.Name, "user") {
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
		if optionMatches(option.Name, "user") {
			user := option.UserValue(s)
			return user.ID, firstNonEmpty(user.GlobalName, user.Username, user.ID)
		}
	}
	return "", ""
}

func promptQuestion(i *discordgo.InteractionCreate) string {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if optionMatches(option.Name, "question") {
			return strings.TrimSpace(option.StringValue())
		}
	}
	return ""
}

func languageHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang, ok := selectedLanguage(i)
	if !ok {
		respondText(s, i, botText("Escolhe `pt` ou `en`.", "Choose `pt` or `en`."))
		return
	}

	setBotLanguage(lang)
	if APP_ID != "" {
		if err := overwriteCommands(s, APP_ID); err != nil {
			respondText(
				s,
				i,
				fmt.Sprintf(
					textForLanguage(lang, "Lingua alterada para **%s**, mas nao consegui atualizar a lista de comandos: %v", "Language changed to **%s**, but I could not update the command list: %v"),
					lang.label(),
					err,
				),
			)
			return
		}
	}

	respondText(
		s,
		i,
		fmt.Sprintf(textForLanguage(lang, "Lingua alterada para **%s**.", "Language changed to **%s**."), lang.label()),
	)
}

func selectedLanguage(i *discordgo.InteractionCreate) (botLanguage, bool) {
	data := i.ApplicationCommandData()
	for _, option := range data.Options {
		if optionMatches(option.Name, "language") {
			return parseBotLanguage(option.StringValue())
		}
	}
	return "", false
}

func profileEmbed(profile *UserProfileResponse, fallbackName string) *discordgo.MessageEmbed {
	lang := currentBotLanguage()
	name := displayProfileName(profile, fallbackName)
	fields := []*discordgo.MessageEmbedField{
		profileField(textForLanguage(lang, "Titulo", "Title"), profile.AnthropologistTitle, lang),
		profileField(textForLanguage(lang, "Impressao de campo", "Field Impression"), profile.Summary, lang),
		profileField(textForLanguage(lang, "Interesses e artefactos", "Interests and Artifacts"), profile.Interests, lang),
		profileField(textForLanguage(lang, "Dialeto nativo", "Native Dialect"), profile.CommunicationStyle, lang),
		profileField(textForLanguage(lang, "Papel social e dinamicas de grupo", "Social Role and Group Dynamics"), profile.PersonaNotes, lang),
		profileField(textForLanguage(lang, "Notas de padrao atual", "Current Pattern Notes"), profile.RecentUpdates, lang),
	}
	return &discordgo.MessageEmbed{
		Title:  textForLanguage(lang, "Perfil: ", "Profile: ") + name,
		Color:  0x2F80ED,
		Fields: fields,
	}
}

func profileField(name string, value string, lang botLanguage) *discordgo.MessageEmbedField {
	value = strings.TrimSpace(value)
	if value == "" {
		value = textForLanguage(lang, "Ainda sem observacoes.", "No observations yet.")
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
	initBotLanguageFromEnv()

	token := strings.TrimSpace(os.Getenv("DISCORD_TOKEN"))
	appID := strings.TrimSpace(os.Getenv("DISCORD_APP_ID"))
	if token == "" {
		log.Fatal("DISCORD_TOKEN nao definido")
	}
	if appID == "" {
		log.Fatal("DISCORD_APP_ID nao definido")
	}
	APP_ID = appID

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

	fmt.Printf("%s %s\n", botText("Bot online. Lingua:", "Bot online. Language:"), currentBotLanguage().label())
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
}
