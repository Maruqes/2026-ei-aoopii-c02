package main

import (
	"context"
	"fmt"
	"net/url"
	"strings"
	"time"

	"github.com/bwmarrin/discordgo"
)

type transcriptionPreference struct {
	CaptureSuspended bool            `json:"capture_suspended"`
	CaptureReason    string          `json:"capture_reason"`
	Order            []string        `json:"order"`
	Source           string          `json:"source"`
	Configured       map[string]bool `json:"configured"`
	InUse            []string        `json:"in_use"`
	Streaming        bool            `json:"streaming"`
}

type transcriptionKeys struct {
	Order     []string `json:"order"`
	Source    string   `json:"source"`
	Since     string   `json:"since"`
	Until     string   `json:"until"`
	Providers []struct {
		Provider           string `json:"provider"`
		Configured         bool   `json:"configured"`
		Model              string `json:"model"`
		StreamingModel     string `json:"streaming_model"`
		StreamingAvailable bool   `json:"streaming_available"`
		BatchAvailable     bool   `json:"batch_available"`
		Keys               []struct {
			Name           string  `json:"name"`
			Group          string  `json:"group"`
			StreamingState string  `json:"streaming_state"`
			BatchState     string  `json:"batch_state"`
			LastError      string  `json:"last_error"`
			Occupied       int     `json:"occupied"`
			StreamingHours float64 `json:"local_streaming_hours"`
			BatchHours     float64 `json:"local_batch_hours"`
		} `json:"keys"`
		Groups []struct {
			Name              string   `json:"name"`
			Verified          bool     `json:"verified"`
			StreamingOccupied int      `json:"streaming_occupied"`
			StreamingLimit    int      `json:"streaming_limit"`
			BatchOccupied     int      `json:"batch_occupied"`
			BatchLimit        int      `json:"batch_limit"`
			Balance           *float64 `json:"balance_usd"`
			BalanceError      string   `json:"balance_error"`
			ReportedHours     *float64 `json:"reported_hours"`
			UpdatedAt         string   `json:"updated_at"`
		} `json:"groups"`
		CostItems []struct {
			Product string   `json:"product"`
			Rate    *float64 `json:"rate_usd_per_hour"`
			Cost    *float64 `json:"estimated_cost_usd"`
			Date    string   `json:"rate_date"`
			Source  string   `json:"rate_source"`
		} `json:"cost_items"`
		Usage *SpeechmaticsKeysResponse `json:"usage"`
	} `json:"providers"`
}

func sttCommand(lang botLanguage) *discordgo.ApplicationCommand {
	manage := int64(discordgo.PermissionManageServer)
	return &discordgo.ApplicationCommand{Name: "stt", Description: textForLanguage(lang, "Preferência de transcrição e fallback.", "Transcription preference and fallback."), DefaultMemberPermissions: &manage,
		Options: []*discordgo.ApplicationCommandOption{
			{Name: "status", Description: textForLanguage(lang, "Mostra a ordem e os fornecedores ativos.", "Show provider order and active providers."), Type: discordgo.ApplicationCommandOptionSubCommand},
			{Name: "order", Description: textForLanguage(lang, "Altera a ordem dos fornecedores.", "Change provider order."), Type: discordgo.ApplicationCommandOptionSubCommand,
				Options: []*discordgo.ApplicationCommandOption{{Name: "providers", Description: textForLanguage(lang, "Ordem de preferência e fallback.", "Preference and fallback order."), Type: discordgo.ApplicationCommandOptionString, Required: true,
					Choices: []*discordgo.ApplicationCommandOptionChoice{{Name: "Deepgram → Speechmatics", Value: "deepgram,speechmatics"}, {Name: "Speechmatics → Deepgram", Value: "speechmatics,deepgram"}, {Name: "Deepgram", Value: "deepgram"}, {Name: "Speechmatics", Value: "speechmatics"}}}}},
		}}
}

func sttHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	if i.GuildID == "" {
		respondText(s, i, botText("Usa este comando num servidor.", "Use this command in a server."))
		return
	}
	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	path := "/v1/guilds/" + url.PathEscape(i.GuildID) + "/transcription"
	var result transcriptionPreference
	var err error
	options := i.ApplicationCommandData().Options
	if len(options) > 0 && options[0].Name == "order" && len(options[0].Options) > 0 {
		err = client.postJSON(ctx, path, map[string]string{"providers": options[0].Options[0].StringValue()}, &result)
		if err == nil {
			if state := getVoiceConnection(i.GuildID); state != nil && state.streaming != nil {
				_, _ = state.streaming.sync(ctx)
			}
		}
	} else {
		err = client.getJSON(ctx, path, &result)
	}
	if err != nil {
		respondText(s, i, fmt.Sprintf("STT: %v", err))
		return
	}
	content := fmt.Sprintf(botText("**Ordem:** %s (%s)\n**Configurados:** Deepgram=%t · Speechmatics=%t\n**Em uso:** %s\n**Streaming:** %t", "**Order:** %s (%s)\n**Configured:** Deepgram=%t · Speechmatics=%t\n**In use:** %s\n**Streaming:** %t"),
		strings.Join(result.Order, " → "), result.Source, result.Configured["deepgram"], result.Configured["speechmatics"], strings.Join(result.InUse, ", "), result.Streaming)
	if result.CaptureSuspended {
		content += botText("\nNova captura suspensa: créditos ou credenciais indisponíveis. Consulta /keys.", "\nNew capture suspended: credits or credentials unavailable. Check /keys.")
	}
	respondText(s, i, content)
}

func formatTranscriptionKeys(result transcriptionKeys, lang botLanguage) string {
	lines := []string{fmt.Sprintf(textForLanguage(lang, "**Ordem:** %s", "**Order:** %s"), strings.Join(result.Order, " → "))}
	for _, provider := range result.Providers {
		title := "Deepgram"
		if provider.Provider == "speechmatics" {
			title = "Speechmatics"
		}
		lines = append(lines, "", "**"+title+"**")
		if !provider.Configured {
			lines = append(lines, textForLanguage(lang, "Não configurado", "Not configured"))
			continue
		}
		if provider.Provider == "deepgram" {
			if len(provider.Groups) == 0 {
				lines = append(lines, textForLanguage(lang, "Créditos: indisponíveis", "Credits: unavailable"))
			}
			for index, group := range provider.Groups {
				if len(provider.Groups) > 1 {
					lines = append(lines, fmt.Sprintf(textForLanguage(lang, "Projeto %d", "Project %d"), index+1))
				}
				balance := textForLanguage(lang, "indisponíveis", "unavailable")
				if group.Balance != nil {
					balance = formatSpeechmaticsUSD(*group.Balance)
				} else if group.BalanceError == "forbidden" {
					balance = textForLanguage(lang, "sem permissão para consultar (billing:read)", "no permission to read (billing:read)")
				}
				lines = append(lines, textForLanguage(lang, "Créditos: ", "Credits: ")+balance)
				lines = append(lines, fmt.Sprintf(textForLanguage(lang, "Streams: %d/%d (limite local)", "Streams: %d/%d (local limit)"), group.StreamingOccupied, group.StreamingLimit))
			}
			cost, known := 0.0, len(provider.CostItems) > 0
			for _, item := range provider.CostItems {
				if item.Cost == nil {
					known = false
				} else {
					cost += *item.Cost
				}
			}
			if known {
				lines = append(lines, textForLanguage(lang, "Custo estimado este mês: ≈ ", "Estimated cost this month: ≈ ")+formatSpeechmaticsUSD(cost))
			}
		} else {
			occupied, limit := 0, 0
			for _, group := range provider.Groups {
				occupied += group.StreamingOccupied
				limit += group.StreamingLimit
			}
			lines = append(lines, textForLanguage(lang, "Créditos: indisponíveis", "Credits: unavailable"), fmt.Sprintf(textForLanguage(lang, "Streams: %d/%d (limite local)", "Streams: %d/%d (local limit)"), occupied, limit))
		}
		for _, key := range provider.Keys {
			name := strings.TrimPrefix(key.Name, strings.ToUpper(provider.Provider)+"_API_KEY")
			name = strings.TrimPrefix(name, "_")
			if name == "" {
				name = textForLanguage(lang, "principal", "main")
			}
			state := sttState(key.StreamingState, lang)
			if key.StreamingState != key.BatchState {
				state = fmt.Sprintf(textForLanguage(lang, "streams %s · ficheiros %s", "streams %s · files %s"), state, sttState(key.BatchState, lang))
			}
			line := fmt.Sprintf("Key %s: %s", name, state)
			if provider.Usage != nil {
				for _, usage := range provider.Usage.Keys {
					if usage.Name == key.Name {
						line += textForLanguage(lang, " · custo ", " · cost ") + speechmaticsKeyCost(usage, lang)
					}
				}
			}
			lines = append(lines, line)
		}
		if provider.Usage != nil {
			lines = append(lines, textForLanguage(lang, "Custos estimados; consumo pode ser partilhado entre keys.", "Estimated costs; usage may be shared across keys."))
		}
	}
	return strings.Join(lines, "\n")
}

func withStreamingController(request TranscriptionRequest, controller *streamingController) TranscriptionRequest {
	request.Streaming = controller
	return request
}

func sttState(state string, lang botLanguage) string {
	switch state {
	case "healthy":
		return textForLanguage(lang, "disponível", "available")
	case "cooldown":
		return textForLanguage(lang, "aguarda retry", "waiting to retry")
	case "no_credits":
		return textForLanguage(lang, "sem créditos", "no credits")
	case "invalid_key":
		return textForLanguage(lang, "key inválida", "invalid key")
	}
	return textForLanguage(lang, "indisponível", "unavailable")
}
