package main

import (
	"log"
	"sync"

	"github.com/bwmarrin/discordgo"
)

var (
	voiceConnections = make(map[string]*discordgo.VoiceConnection)
	voiceMu          sync.Mutex
)

func setVoiceConnection(guildID string, vc *discordgo.VoiceConnection) {
	voiceMu.Lock()
	voiceConnections[guildID] = vc
	voiceMu.Unlock()
}

func getVoiceConnection(guildID string) *discordgo.VoiceConnection {
	voiceMu.Lock()
	defer voiceMu.Unlock()
	return voiceConnections[guildID]
}

func clearVoiceConnection(guildID string, vc *discordgo.VoiceConnection) {
	voiceMu.Lock()
	defer voiceMu.Unlock()

	current, ok := voiceConnections[guildID]
	if !ok {
		return
	}
	if current == vc {
		delete(voiceConnections, guildID)
	}
}

func receiveAudio(guildID string, vc *discordgo.VoiceConnection) {
	log.Printf("à espera de áudio no servidor=%s canal=%s", guildID, vc.ChannelID)
	defer clearVoiceConnection(guildID, vc)

	if err := ListenAndWriteOpusToWAV(vc, "ola.wav"); err != nil {
		log.Printf("erro ao gravar áudio no servidor=%s: %v", guildID, err)
		return
	}

	log.Printf("captura finalizada no servidor=%s", guildID)
}

func OnVoiceStateUpdate(s *discordgo.Session, vs *discordgo.VoiceStateUpdate) {
	// Ignora o próprio bot
	if s.State != nil && s.State.User != nil && vs.UserID == s.State.User.ID {
		return
	}

	// Se ChannelID vazio, a pessoa saiu da call
	if vs.ChannelID == "" {
		return
	}

	guildID := vs.GuildID
	channelID := vs.ChannelID

	// Se já houver conexão no servidor, muda para o novo canal.
	if current := getVoiceConnection(guildID); current != nil {
		if current.ChannelID == channelID {
			return
		}

		if err := current.ChangeChannel(channelID, false, false); err != nil {
			log.Printf("erro ao mover para canal %s no servidor %s: %v", channelID, guildID, err)
			_ = current.Disconnect()
			clearVoiceConnection(guildID, current)
		} else {
			log.Printf("bot movido para canal %s no servidor %s", channelID, guildID)
		}
		return
	}

	log.Printf("utilizador %s entrou no canal %s do servidor %s", vs.UserID, channelID, guildID)

	vc, err := s.ChannelVoiceJoin(guildID, channelID, true, false)
	if err != nil {
		log.Println("erro ao entrar no voice channel:", err)
		return
	}

	setVoiceConnection(guildID, vc)
	log.Println("bot entrou na call")

	go receiveAudio(guildID, vc)
}
