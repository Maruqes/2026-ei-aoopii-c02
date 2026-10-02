package main

import (
	"os"
	"strings"
	"sync"

	"github.com/bwmarrin/discordgo"
)

type botLanguage string

const (
	botLanguagePT botLanguage = "pt"
	botLanguageEN botLanguage = "en"
)

var (
	botLanguageMu sync.RWMutex
	botLang       = botLanguagePT
)

func initBotLanguageFromEnv() {
	if lang, ok := parseBotLanguage(os.Getenv("BOT_LANGUAGE")); ok {
		setBotLanguage(lang)
	}
}

func currentBotLanguage() botLanguage {
	botLanguageMu.RLock()
	defer botLanguageMu.RUnlock()
	return botLang
}

func setBotLanguage(lang botLanguage) {
	if lang != botLanguageEN {
		lang = botLanguagePT
	}
	botLanguageMu.Lock()
	botLang = lang
	botLanguageMu.Unlock()
}

func parseBotLanguage(raw string) (botLanguage, bool) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "pt", "pt-pt", "portuguese", "portugues", "portuguesa":
		return botLanguagePT, true
	case "en", "en-us", "en-gb", "english", "ingles":
		return botLanguageEN, true
	default:
		return "", false
	}
}

func (lang botLanguage) apiValue() string {
	if lang == botLanguageEN {
		return "en"
	}
	return "pt"
}

func (lang botLanguage) label() string {
	return textForLanguage(lang, "Portugues", "English")
}

func botText(pt string, en string) string {
	return textForLanguage(currentBotLanguage(), pt, en)
}

func textForLanguage(lang botLanguage, pt string, en string) string {
	if lang == botLanguageEN {
		return en
	}
	return pt
}

var commandNames = map[string]map[botLanguage]string{
	"digest":   {botLanguagePT: "digest", botLanguageEN: "digest"},
	"retry":    {botLanguagePT: "retry", botLanguageEN: "retry"},
	"ping":     {botLanguagePT: "ping", botLanguageEN: "ping"},
	"start":    {botLanguagePT: "start", botLanguageEN: "start"},
	"stop":     {botLanguagePT: "stop", botLanguageEN: "stop"},
	"profile":  {botLanguagePT: "profile", botLanguageEN: "profile"},
	"prompt":   {botLanguagePT: "prompt", botLanguageEN: "prompt"},
	"sync":     {botLanguagePT: "sync", botLanguageEN: "sync"},
	"models":   {botLanguagePT: "models", botLanguageEN: "models"},
	"effort":   {botLanguagePT: "effort", botLanguageEN: "effort"},
	"health":   {botLanguagePT: "health", botLanguageEN: "health"},
	"keys":     {botLanguagePT: "keys", botLanguageEN: "keys"},
	"forget":   {botLanguagePT: "forget", botLanguageEN: "forget"},
	"timeout":  {botLanguagePT: "timeout", botLanguageEN: "timeout"},
	"recap":    {botLanguagePT: "recap", botLanguageEN: "recap"},
	"oracle":   {botLanguagePT: "oracle", botLanguageEN: "oracle"},
	"guess":    {botLanguagePT: "guess", botLanguageEN: "guess"},
	"language": {botLanguagePT: "language", botLanguageEN: "language"},
}

var optionNames = map[string]map[botLanguage]string{
	"user":     {botLanguagePT: "user", botLanguageEN: "user"},
	"question": {botLanguagePT: "question", botLanguageEN: "question"},
	"minutes":  {botLanguagePT: "minutes", botLanguageEN: "minutes"},
	"session":  {botLanguagePT: "session", botLanguageEN: "session"},
	"language": {botLanguagePT: "language", botLanguageEN: "language"},
}

func commandName(lang botLanguage, key string) string {
	if names, ok := commandNames[key]; ok {
		if name := names[lang]; name != "" {
			return name
		}
	}
	return key
}

func optionName(lang botLanguage, key string) string {
	if names, ok := optionNames[key]; ok {
		if name := names[lang]; name != "" {
			return name
		}
	}
	return key
}

func commandMatches(name string, key string) bool {
	name = strings.ToLower(strings.TrimSpace(name))
	for _, candidate := range commandNames[key] {
		if name == candidate {
			return true
		}
	}
	return false
}

func optionMatches(name string, key string) bool {
	name = strings.ToLower(strings.TrimSpace(name))
	for _, candidate := range optionNames[key] {
		if name == candidate {
			return true
		}
	}
	return false
}

func buildApplicationCommands(lang botLanguage) []*discordgo.ApplicationCommand {
	opt := func(key string, optionType discordgo.ApplicationCommandOptionType, enDescription string, required bool) *discordgo.ApplicationCommandOption {
		return &discordgo.ApplicationCommandOption{
			Type:        optionType,
			Name:        optionName(lang, key),
			Description: enDescription,
			Required:    required,
		}
	}

	commands := []*discordgo.ApplicationCommand{
		{Name: "play", Description: textForLanguage(lang, "Toca o áudio de um vídeo do YouTube na tua call.", "Play a YouTube video's audio in your voice channel."), Options: []*discordgo.ApplicationCommandOption{
			{Type: discordgo.ApplicationCommandOptionString, Name: "url", Description: textForLanguage(lang, "Link do vídeo do YouTube (até 1 hora).", "YouTube video link (up to 1 hour)."), Required: true},
		}},
		{Name: "pause", Description: textForLanguage(lang, "Pausa ou retoma o áudio na call.", "Pause or resume voice playback.")},
		{Name: "skip", Description: textForLanguage(lang, "Salta para o próximo áudio da fila.", "Skip to the next track in the queue.")},
		{Name: "queue", Description: textForLanguage(lang, "Mostra o áudio atual e a fila da call.", "Show the current track and voice queue.")},
		{Name: "musicstop", Description: textForLanguage(lang, "Para a música e limpa a fila; continua a gravar a conversa.", "Stop playback and clear the queue; keep recording the conversation.")},
		{Name: "digest", Description: "Catch up on this text channel with decisions, highlights and lore.", Options: []*discordgo.ApplicationCommandOption{
			{Type: discordgo.ApplicationCommandOptionInteger, Name: "hours", Description: "Hours to recap (1–168). Default: 24.", MinValue: float64Pointer(1), MaxValue: 168},
		}},
		{Name: "retry", Description: "Recover failed recordings or a failed summary.", Options: []*discordgo.ApplicationCommandOption{
			{Type: discordgo.ApplicationCommandOptionInteger, Name: "session", Description: "Session ID to recover.", Required: true, MinValue: float64Pointer(1)},
		}},
		{
			Name:        commandName(lang, "ping"),
			Description: "Replies with PONG!",
		},
		{
			Name:        commandName(lang, "start"),
			Description: "Restores the bot's normal behavior.",
		},
		{
			Name:        commandName(lang, "stop"),
			Description: "Pauses the bot, leaves calls, and blocks new joins.",
		},
		{
			Name:        commandName(lang, "profile"),
			Description: "Shows a generated user profile.",
			Options: []*discordgo.ApplicationCommandOption{
				opt("user", discordgo.ApplicationCommandOptionUser, "User to inspect. If empty, uses the caller.", false),
			},
		},
		{
			Name:        commandName(lang, "prompt"),
			Description: "Asks the anthropologist about a user's lore.",
			Options: []*discordgo.ApplicationCommandOption{
				opt("user", discordgo.ApplicationCommandOptionUser, "User whose lore should be consulted.", true),
				opt("question", discordgo.ApplicationCommandOptionString, "Question to answer from the user's lore file.", true),
			},
		},
		{
			Name:        commandName(lang, "sync"),
			Description: "Forces profile synchronization from stored text messages.",
		},
		{
			Name:        commandName(lang, "models"),
			Description: "Lists and changes the active LLM model.",
		},
		{
			Name:        commandName(lang, "effort"),
			Description: "Shows or changes ChatGPT reasoning effort (tested before saving).",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Name: "level", Type: discordgo.ApplicationCommandOptionString,
					Description: "Reasoning effort; supported levels depend on the model.",
					Choices: []*discordgo.ApplicationCommandOptionChoice{
						{Name: "default", Value: "default"},
						{Name: "none", Value: "none"},
						{Name: "minimal", Value: "minimal"},
						{Name: "low", Value: "low"},
						{Name: "medium", Value: "medium"},
						{Name: "high", Value: "high"},
						{Name: "xhigh", Value: "xhigh"},
						{Name: "max", Value: "max"},
					},
				},
			},
		},
		{
			Name:        commandName(lang, "health"),
			Description: "Checks the API, Postgres, and transcription status.",
		},
		{
			Name:        commandName(lang, "keys"),
			Description: "Shows Speechmatics API key usage.",
		},
		{
			Name:        commandName(lang, "forget"),
			Description: "Deletes a user's messages, profile, and lore.",
			Options: []*discordgo.ApplicationCommandOption{
				opt("user", discordgo.ApplicationCommandOptionUser, "User to delete from the database.", true),
			},
		},
		{
			Name:        commandName(lang, "timeout"),
			Description: "Sets minutes before leaving the call (BOT_LEAVE). 0 disables it.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionInteger,
					Name:        optionName(lang, "minutes"),
					Description: "Minutes in the call before leaving and processing. 0 = no limit.",
					Required:    true,
					MinValue:    float64Pointer(0),
					MaxValue:    24 * 60,
				},
			},
		},
		{
			Name:        commandName(lang, "recap"),
			Description: "Shows the server's latest voice session recap.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionInteger,
					Name:        optionName(lang, "session"),
					Description: "Session ID. If empty, uses the latest one.",
					Required:    false,
					MinValue:    float64Pointer(1),
				},
			},
		},
		{
			Name:        commandName(lang, "oracle"),
			Description: "Asks the anthropologist about the group's history.",
			Options: []*discordgo.ApplicationCommandOption{
				opt("question", discordgo.ApplicationCommandOptionString, "Question about decisions, topics, or server lore.", true),
			},
		},
		{
			Name:        commandName(lang, "guess"),
			Description: "Mini-game: guess who said a voice-call quote.",
		},
		{
			Name:        commandName(lang, "language"),
			Description: "Changes the bot's response language.",
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionString,
					Name:        optionName(lang, "language"),
					Description: "Language used for bot replies.",
					Required:    true,
					Choices: []*discordgo.ApplicationCommandOptionChoice{
						{Name: "Portuguese", Value: string(botLanguagePT)},
						{Name: "English", Value: string(botLanguageEN)},
					},
				},
			},
		},
	}
	for _, command := range commands {
		if requiresManageServer(command.Name) {
			permissions := int64(discordgo.PermissionManageServer)
			command.DefaultMemberPermissions = &permissions
		}
	}
	return commands
}
