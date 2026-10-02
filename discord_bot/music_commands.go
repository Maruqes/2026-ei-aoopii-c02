package main

import (
	"errors"
	"fmt"
	"log"
	"strings"

	"github.com/bwmarrin/discordgo"
)

func isMusicCommand(name string) bool {
	switch name {
	case "play", "pause", "skip", "queue", "musicstop":
		return true
	}
	return false
}

func (state *voiceConnectionState) closeMusic() {
	if state != nil && state.music != nil {
		state.music.Close()
	}
}

// Only people sharing the bot's call can control its player.
func musicCallerChannel(s *discordgo.Session, i *discordgo.InteractionCreate) string {
	if s == nil || s.State == nil || i == nil || i.GuildID == "" || i.Member == nil || i.Member.User == nil {
		return ""
	}
	voiceState, err := s.State.VoiceState(i.GuildID, i.Member.User.ID)
	if err != nil || voiceState == nil {
		return ""
	}
	return voiceState.ChannelID
}

func voiceChannelID(state *voiceConnectionState) string {
	if state == nil || state.vc == nil {
		return ""
	}
	state.vc.RLock()
	defer state.vc.RUnlock()
	return state.vc.ChannelID
}

func musicHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	lang := currentBotLanguage()
	channelID := musicCallerChannel(s, i)
	if channelID == "" {
		respondText(s, i, textForLanguage(lang, "Entra numa call do servidor para usar os controlos de áudio.", "Join a server voice channel to use playback controls."))
		return
	}
	name := i.ApplicationCommandData().Name
	var rawURL string
	if name == "play" {
		for _, option := range i.ApplicationCommandData().Options {
			if option.Name == "url" {
				rawURL = option.StringValue()
			}
		}
		if _, err := canonicalYouTubeURL(rawURL); err != nil {
			respondText(s, i, musicErrorForLanguage(lang, err))
			return
		}
	}
	if !isBotEnabled() {
		respondText(s, i, textForLanguage(lang, "O bot está parado. Usa /start antes de tocar áudio.", "The bot is stopped. Use /start before playing audio."))
		return
	}
	state := getVoiceConnection(i.GuildID)
	if state == nil && name == "play" {
		OnVoiceStateUpdate(s, &discordgo.VoiceStateUpdate{VoiceState: &discordgo.VoiceState{
			GuildID: i.GuildID, ChannelID: channelID, UserID: i.Member.User.ID, Member: i.Member,
		}})
		state = getVoiceConnection(i.GuildID)
	}
	if state == nil || state.music == nil {
		respondText(s, i, textForLanguage(lang, "Não há um leitor ativo nesta call. Usa /play com um link do YouTube; o bot precisa das permissões Ligar e Falar.", "There is no active player in this call. Use /play with a YouTube link; the bot needs Connect and Speak permissions."))
		return
	}
	if voiceChannelID(state) != channelID {
		respondText(s, i, textForLanguage(lang, "Entra na mesma call que o bot para controlar o áudio.", "Join the bot's voice channel to control playback."))
		return
	}
	switch name {
	case "play":
		track, position, err := state.music.Enqueue(rawURL, i.Member.User.ID)
		if err != nil {
			log.Printf("YouTube queue failed: %v", err)
			respondText(s, i, fmt.Sprintf(textForLanguage(lang, "Não consegui adicionar esse áudio: %s", "Could not add that audio: %s"), musicErrorForLanguage(lang, err)))
			return
		}
		if position == 0 {
			respondText(s, i, fmt.Sprintf(textForLanguage(lang, "▶️ Vou tocar **%s** (%s).", "▶️ Preparing **%s** (%s)."), musicTitle(track.Title), musicDuration(track.DurationSeconds)))
		} else {
			respondText(s, i, fmt.Sprintf(textForLanguage(lang, "🎵 **%s** entrou na fila, posição %d (%s).", "🎵 **%s** queued at position %d (%s)."), musicTitle(track.Title), position, musicDuration(track.DurationSeconds)))
		}
	case "pause":
		paused, err := state.music.PauseToggle()
		if err != nil {
			respondText(s, i, textForLanguage(lang, "Não há áudio a tocar.", "No track is playing."))
			return
		}
		if paused {
			respondText(s, i, textForLanguage(lang, "⏸️ Áudio pausado. Usa /pause para retomar.", "⏸️ Playback paused. Use /pause to resume."))
		} else {
			respondText(s, i, textForLanguage(lang, "▶️ Áudio retomado.", "▶️ Playback resumed."))
		}
	case "skip":
		if state.music.Skip() {
			respondText(s, i, textForLanguage(lang, "⏭️ Áudio saltado.", "⏭️ Track skipped."))
		} else {
			respondText(s, i, textForLanguage(lang, "A fila está vazia.", "The queue is empty."))
		}
	case "musicstop":
		state.music.Clear()
		respondText(s, i, textForLanguage(lang, "⏹️ Música parada e fila limpa. A gravação da conversa continua.", "⏹️ Playback stopped and queue cleared. Conversation recording continues."))
	case "queue":
		respondLongText(s, i, musicQueueText(lang, state.music.Snapshot()))
	}
}

func musicTitle(title string) string {
	title = strings.Join(strings.Fields(title), " ")
	if len([]rune(title)) > 120 {
		title = string([]rune(title)[:120]) + "…"
	}
	return strings.NewReplacer("\\", "\\\\", "*", "\\*", "_", "\\_", "`", "\\`", "[", "\\[", "]", "\\]", "~", "\\~", "@", "＠", "<", "‹", ">", "›").Replace(title)
}

func musicDuration(seconds float64) string {
	whole := int(seconds)
	if whole >= 3600 {
		return fmt.Sprintf("%d:%02d:%02d", whole/3600, whole/60%60, whole%60)
	}
	return fmt.Sprintf("%d:%02d", whole/60, whole%60)
}

func musicQueueText(lang botLanguage, snapshot MusicSnapshot) string {
	if snapshot.Current == nil && len(snapshot.Queue) == 0 {
		return textForLanguage(lang, "A fila está vazia. Usa /play url:<link do YouTube>.", "The queue is empty. Use /play url:<YouTube link>.")
	}
	var lines []string
	if snapshot.Current != nil {
		label := textForLanguage(lang, "Agora", "Current")
		if snapshot.Paused {
			label = textForLanguage(lang, "Pausado", "Paused")
		}
		lines = append(lines, fmt.Sprintf("**%s:** %s (%s)", label, musicTitle(snapshot.Current.Title), musicDuration(snapshot.Current.DurationSeconds)))
	}
	for index, track := range snapshot.Queue {
		lines = append(lines, fmt.Sprintf("%d. %s (%s)", index+1, musicTitle(track.Title), musicDuration(track.DurationSeconds)))
	}
	return strings.Join(lines, "\n")
}

func musicErrorForLanguage(lang botLanguage, err error) string {
	// Provider logs can contain signed streaming URLs. Keep Discord replies generic.
	switch {
	case errors.Is(err, ErrMusicInvalidURL):
		return textForLanguage(lang, "Usa um link de vídeo do YouTube: youtube.com/watch?v=… ou youtu.be/… (também aceita Shorts).", "Use a YouTube video URL: youtube.com/watch?v=… or youtu.be/… (Shorts are also accepted).")
	case errors.Is(err, ErrMusicQueueFull):
		return textForLanguage(lang, "A fila está cheia (20 áudios). Usa /skip ou /musicstop para libertar espaço.", "The queue is full (20 tracks). Use /skip or /musicstop to make room.")
	case errors.Is(err, ErrMusicDuration):
		return textForLanguage(lang, "Escolhe um vídeo com duração conhecida até 1 hora.", "Choose a video with a known duration of up to 1 hour.")
	case errors.Is(err, ErrMusicLive):
		return textForLanguage(lang, "Transmissões em direto não são suportadas. Usa um vídeo normal.", "Live streams are not supported. Use a regular video.")
	case errors.Is(err, ErrMusicClosed):
		return textForLanguage(lang, "O leitor foi parado. Volta a entrar na call e tenta /play novamente.", "The player was stopped. Rejoin the call and try /play again.")
	case errors.Is(err, ErrMusicNothingPlaying):
		return textForLanguage(lang, "Não há áudio a tocar.", "No track is playing.")
	}
	return textForLanguage(lang, "verifica o link e tenta novamente. Usa um vídeo público até 1 hora; o leitor precisa de yt-dlp, Node.js e FFmpeg instalados.", "check the link and try again. Use a public video up to 1 hour; playback requires yt-dlp, Node.js and FFmpeg installed.")
}
