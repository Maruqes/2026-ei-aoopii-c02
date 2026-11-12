package main

import (
	"fmt"
	"log"
	"strings"
)

// Voice function intents emitted by the Python voice assistant.
//
// The Python API (`POST /v1/assistant/question` -> `execute_assistant_answer`)
// validates `USE{PLAY_MUSIC(...)}` / `USE{STOP_MUSIC()}` and returns intents.
// This file executes them with the same player calls as the slash commands:
//
//	PLAY_MUSIC -> MusicPlayer.EnqueueText, same as /play
//	STOP_MUSIC -> MusicPlayer.Clear, same as /musicstop (stop music + clear
//	queue, keep recording the conversation). This is NOT /stop, which pauses
//	the bot and leaves the call.
//
// Python stays silent on validated intents (spoken == ""); the confirmations
// below are the spoken outcome, reusing the /play and /musicstop strings.

type assistantIntent struct {
	Action string `json:"action"`
	Query  string `json:"query,omitempty"`
}

type assistantQuestionResponse struct {
	Answer  string            `json:"answer"`
	Intents []assistantIntent `json:"intents"`
}

// runAssistantIntents executes validated voice intents in order and returns
// spoken confirmations to append to the voice reply. Unknown actions are
// ignored and never executed.
func (a *assistantController) runAssistantIntents(userID string, intents []assistantIntent) string {
	if a == nil || a.state == nil || len(intents) == 0 {
		return ""
	}
	lines := make([]string, 0, len(intents))
	for _, intent := range intents {
		if line := a.executeAssistantIntent(userID, intent); line != "" {
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n")
}

func (a *assistantController) executeAssistantIntent(userID string, intent assistantIntent) string {
	lang := currentBotLanguage()
	switch strings.ToLower(strings.TrimSpace(intent.Action)) {
	case "play":
		return a.executeAssistantPlay(userID, strings.TrimSpace(intent.Query), lang)
	case "stop":
		return a.executeAssistantStop(lang)
	default:
		log.Printf("assistant ignoring unknown intent action=%q user=%s", intent.Action, userID)
		return ""
	}
}

func (a *assistantController) executeAssistantPlay(userID, query string, lang botLanguage) string {
	if query == "" {
		return fmt.Sprintf(textForLanguage(lang, "Não consegui adicionar esse áudio: %s", "Could not add that audio: %s"), musicErrorForLanguage(lang, ErrMusicInvalidURL))
	}
	if !isBotEnabled() {
		return textForLanguage(lang, "O bot está parado. Usa /start antes de tocar áudio.", "The bot is stopped. Use /start before playing audio.")
	}
	state := a.state
	if state == nil || state.music == nil {
		return textForLanguage(lang, "Não há um leitor ativo nesta call. Usa /play com um link do YouTube ou pesquisa; o bot precisa das permissões Ligar e Falar.", "There is no active player in this call. Use /play with a YouTube link or search; the bot needs Connect and Speak permissions.")
	}
	if msg := assistantMusicVoiceCheck(a, userID, lang); msg != "" {
		return msg
	}
	// Synchronous like /play: YouTube search + resolve usually take seconds,
	// well within the 30s assistant request budget. No retry; failures speak.
	track, position, err := state.music.EnqueueText(query, userID)
	if err != nil {
		log.Printf("assistant play failed user=%s query=%q: %v", userID, query, err)
		return fmt.Sprintf(textForLanguage(lang, "Não consegui adicionar esse áudio: %s", "Could not add that audio: %s"), musicErrorForLanguage(lang, err))
	}
	if position == 0 {
		return fmt.Sprintf(textForLanguage(lang, "▶️ Vou tocar **%s** (%s).", "▶️ Preparing **%s** (%s)."), musicTitle(track.Title), musicDuration(track.DurationSeconds))
	}
	return fmt.Sprintf(textForLanguage(lang, "🎵 **%s** entrou na fila, posição %d (%s).", "🎵 **%s** queued at position %d (%s)."), musicTitle(track.Title), position, musicDuration(track.DurationSeconds))
}

func (a *assistantController) executeAssistantStop(lang botLanguage) string {
	if !isBotEnabled() {
		return textForLanguage(lang, "O bot está parado. Usa /start antes de tocar áudio.", "The bot is stopped. Use /start before playing audio.")
	}
	state := a.state
	if state == nil || state.music == nil {
		return textForLanguage(lang, "Não há um leitor ativo nesta call. Usa /play com um link do YouTube ou pesquisa; o bot precisa das permissões Ligar e Falar.", "There is no active player in this call. Use /play with a YouTube link or search; the bot needs Connect and Speak permissions.")
	}
	state.music.Clear()
	return textForLanguage(lang, "⏹️ Música parada e fila limpa. A gravação da conversa continua.", "⏹️ Playback stopped and queue cleared. Conversation recording continues.")
}

// assistantMusicVoiceCheck ensures the speaker is in the bot's call, mirroring
// /play and /musicstop. Returns "" when playback may proceed, otherwise the
// spoken explanation. A nil session (unit tests) skips the Discord lookup.
func assistantMusicVoiceCheck(a *assistantController, userID string, lang botLanguage) string {
	if a == nil || a.state == nil {
		return textForLanguage(lang, "Não há um leitor ativo nesta call. Usa /play com um link do YouTube ou pesquisa; o bot precisa das permissões Ligar e Falar.", "There is no active player in this call. Use /play with a YouTube link or search; the bot needs Connect and Speak permissions.")
	}
	s := a.session
	guildID := ""
	if a.state.vc != nil {
		guildID = a.state.vc.GuildID
	}
	if s == nil || s.State == nil || guildID == "" {
		return ""
	}
	botChannel := voiceChannelID(a.state)
	voiceState, err := s.State.VoiceState(guildID, userID)
	if err != nil || voiceState == nil || voiceState.ChannelID == "" {
		return textForLanguage(lang, "Entra numa call do servidor para usar os controlos de áudio.", "Join a server voice channel to use playback controls.")
	}
	if botChannel != "" && voiceState.ChannelID != botChannel {
		return textForLanguage(lang, "Entra na mesma call que o bot para controlar o áudio.", "Join the bot's voice channel to control playback.")
	}
	return ""
}
