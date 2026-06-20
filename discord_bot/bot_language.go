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
	"ping":     {botLanguagePT: "ping", botLanguageEN: "ping"},
	"start":    {botLanguagePT: "comecar", botLanguageEN: "start"},
	"stop":     {botLanguagePT: "parar", botLanguageEN: "stop"},
	"profile":  {botLanguagePT: "perfil", botLanguageEN: "profile"},
	"prompt":   {botLanguagePT: "perguntar", botLanguageEN: "prompt"},
	"sync":     {botLanguagePT: "sincronizar", botLanguageEN: "sync"},
	"models":   {botLanguagePT: "modelos", botLanguageEN: "models"},
	"health":   {botLanguagePT: "estado", botLanguageEN: "health"},
	"keys":     {botLanguagePT: "chaves", botLanguageEN: "keys"},
	"forget":   {botLanguagePT: "esquecer", botLanguageEN: "forget"},
	"timeout":  {botLanguagePT: "temporizador", botLanguageEN: "timeout"},
	"recap":    {botLanguagePT: "resumo", botLanguageEN: "recap"},
	"oracle":   {botLanguagePT: "oraculo", botLanguageEN: "oracle"},
	"guess":    {botLanguagePT: "adivinhar", botLanguageEN: "guess"},
	"language": {botLanguagePT: "idioma", botLanguageEN: "language"},
}

var optionNames = map[string]map[botLanguage]string{
	"user":     {botLanguagePT: "utilizador", botLanguageEN: "user"},
	"question": {botLanguagePT: "pergunta", botLanguageEN: "question"},
	"minutes":  {botLanguagePT: "minutos", botLanguageEN: "minutes"},
	"session":  {botLanguagePT: "sessao", botLanguageEN: "session"},
	"language": {botLanguagePT: "idioma", botLanguageEN: "language"},
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
	opt := func(key string, optionType discordgo.ApplicationCommandOptionType, ptDescription string, enDescription string, required bool) *discordgo.ApplicationCommandOption {
		return &discordgo.ApplicationCommandOption{
			Type:        optionType,
			Name:        optionName(lang, key),
			Description: textForLanguage(lang, ptDescription, enDescription),
			Required:    required,
		}
	}

	return []*discordgo.ApplicationCommand{
		{
			Name:        commandName(lang, "ping"),
			Description: textForLanguage(lang, "Responde com PONG!", "Replies with PONG!"),
		},
		{
			Name:        commandName(lang, "start"),
			Description: textForLanguage(lang, "Reativa o comportamento normal do bot.", "Restores the bot's normal behavior."),
		},
		{
			Name:        commandName(lang, "stop"),
			Description: textForLanguage(lang, "Pausa o bot, sai das calls e bloqueia novas entradas.", "Pauses the bot, leaves calls, and blocks new joins."),
		},
		{
			Name:        commandName(lang, "profile"),
			Description: textForLanguage(lang, "Mostra o perfil gerado de um utilizador.", "Shows a generated user profile."),
			Options: []*discordgo.ApplicationCommandOption{
				opt("user", discordgo.ApplicationCommandOptionUser, "Utilizador a consultar. Se vazio, usa quem chamou o comando.", "User to inspect. If empty, uses the caller.", false),
			},
		},
		{
			Name:        commandName(lang, "prompt"),
			Description: textForLanguage(lang, "Faz uma pergunta ao antropologo sobre a lore de um utilizador.", "Asks the anthropologist about a user's lore."),
			Options: []*discordgo.ApplicationCommandOption{
				opt("user", discordgo.ApplicationCommandOptionUser, "Utilizador cuja lore deve ser consultada.", "User whose lore should be consulted.", true),
				opt("question", discordgo.ApplicationCommandOptionString, "Pergunta a responder com base no ficheiro de lore do utilizador.", "Question to answer from the user's lore file.", true),
			},
		},
		{
			Name:        commandName(lang, "sync"),
			Description: textForLanguage(lang, "Forca a sincronizacao dos perfis com as mensagens de texto guardadas.", "Forces profile synchronization from stored text messages."),
		},
		{
			Name:        commandName(lang, "models"),
			Description: textForLanguage(lang, "Lista e permite alterar o modelo LLM ativo.", "Lists and changes the active LLM model."),
		},
		{
			Name:        commandName(lang, "health"),
			Description: textForLanguage(lang, "Verifica API, Postgres e estado das transcricoes.", "Checks the API, Postgres, and transcription status."),
		},
		{
			Name:        commandName(lang, "keys"),
			Description: textForLanguage(lang, "Mostra o uso das chaves API Speechmatics.", "Shows Speechmatics API key usage."),
		},
		{
			Name:        commandName(lang, "forget"),
			Description: textForLanguage(lang, "Apaga mensagens, perfil e lore de um utilizador.", "Deletes a user's messages, profile, and lore."),
			Options: []*discordgo.ApplicationCommandOption{
				opt("user", discordgo.ApplicationCommandOptionUser, "Utilizador a apagar da base de dados.", "User to delete from the database.", true),
			},
		},
		{
			Name:        commandName(lang, "timeout"),
			Description: textForLanguage(lang, "Configura minutos antes de sair da call (BOT_LEAVE). 0 desativa.", "Sets minutes before leaving the call (BOT_LEAVE). 0 disables it."),
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionInteger,
					Name:        optionName(lang, "minutes"),
					Description: textForLanguage(lang, "Minutos na call antes de sair e processar. 0 = sem limite.", "Minutes in the call before leaving and processing. 0 = no limit."),
					Required:    true,
					MinValue:    float64Pointer(0),
					MaxValue:    24 * 60,
				},
			},
		},
		{
			Name:        commandName(lang, "recap"),
			Description: textForLanguage(lang, "Mostra o resumo da ultima sessao de voz do servidor.", "Shows the server's latest voice session recap."),
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionInteger,
					Name:        optionName(lang, "session"),
					Description: textForLanguage(lang, "ID da sessao. Se vazio, usa a mais recente.", "Session ID. If empty, uses the latest one."),
					Required:    false,
					MinValue:    float64Pointer(1),
				},
			},
		},
		{
			Name:        commandName(lang, "oracle"),
			Description: textForLanguage(lang, "Pergunta ao antropologo sobre a historia do grupo.", "Asks the anthropologist about the group's history."),
			Options: []*discordgo.ApplicationCommandOption{
				opt("question", discordgo.ApplicationCommandOptionString, "Pergunta sobre decisoes, topicos ou lore do servidor.", "Question about decisions, topics, or server lore.", true),
			},
		},
		{
			Name:        commandName(lang, "guess"),
			Description: textForLanguage(lang, "Mini-jogo: adivinha quem disse uma frase da call.", "Mini-game: guess who said a voice-call quote."),
		},
		{
			Name:        commandName(lang, "language"),
			Description: textForLanguage(lang, "Muda a lingua global do bot.", "Changes the bot's global language."),
			Options: []*discordgo.ApplicationCommandOption{
				{
					Type:        discordgo.ApplicationCommandOptionString,
					Name:        optionName(lang, "language"),
					Description: textForLanguage(lang, "Lingua usada em todos os comandos e respostas do bot.", "Language used for all bot commands and replies."),
					Required:    true,
					Choices: []*discordgo.ApplicationCommandOptionChoice{
						{Name: textForLanguage(lang, "Portugues", "Portuguese"), Value: string(botLanguagePT)},
						{Name: "English", Value: string(botLanguageEN)},
					},
				},
			},
		},
	}
}
