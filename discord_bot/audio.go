package main

import (
	"log"
	"sync"

	"github.com/bwmarrin/discordgo"
)

type voiceConnectionState struct {
	vc        *discordgo.VoiceConnection
	ssrcUsers *SSRCUserMap
}

var (
	voiceConnections = make(map[string]*voiceConnectionState)
	voiceMu          sync.Mutex
)

func setVoiceConnection(guildID string, state *voiceConnectionState) {
	voiceMu.Lock()
	voiceConnections[guildID] = state
	voiceMu.Unlock()
}

func getVoiceConnection(guildID string) *voiceConnectionState {
	voiceMu.Lock()
	defer voiceMu.Unlock()
	return voiceConnections[guildID]
}

func getSSRCUserMap(guildID string) *SSRCUserMap {
	current := getVoiceConnection(guildID)
	if current == nil {
		return nil
	}
	return current.ssrcUsers
}

func getSSRCByDiscordID(guildID string, discordID string) (uint32, bool) {
	ssrcUsers := getSSRCUserMap(guildID)
	if ssrcUsers == nil {
		return 0, false
	}
	return ssrcUsers.SSRCByDiscordID(discordID)
}

func getDiscordIDBySSRC(guildID string, ssrc uint32) (string, bool) {
	ssrcUsers := getSSRCUserMap(guildID)
	if ssrcUsers == nil {
		return "", false
	}
	return ssrcUsers.DiscordIDBySSRC(ssrc)
}

func clearVoiceConnection(guildID string, vc *discordgo.VoiceConnection) {
	voiceMu.Lock()
	defer voiceMu.Unlock()

	current, ok := voiceConnections[guildID]
	if !ok {
		return
	}
	if current.vc == vc {
		delete(voiceConnections, guildID)
	}
}

func hasOtherUsersInVoiceChannel(s *discordgo.Session, guildID string, channelID string, botUserID string) (bool, error) {
	if s == nil || s.State == nil || guildID == "" || channelID == "" {
		return false, nil
	}

	guild, err := s.State.Guild(guildID)
	if err != nil {
		return false, err
	}

	for _, voiceState := range guild.VoiceStates {
		if voiceState == nil {
			continue
		}
		if voiceState.ChannelID != channelID {
			continue
		}
		if voiceState.UserID == botUserID {
			continue
		}
		return true, nil
	}

	return false, nil
}

func disconnectIfBotIsAlone(s *discordgo.Session, guildID string, state *voiceConnectionState) bool {
	if state == nil || state.vc == nil {
		return false
	}

	botUserID := state.vc.UserID
	if botUserID == "" && s != nil && s.State != nil && s.State.User != nil {
		botUserID = s.State.User.ID
	}

	hasOtherUsers, err := hasOtherUsersInVoiceChannel(s, guildID, state.vc.ChannelID, botUserID)
	if err != nil {
		log.Printf("erro ao verificar utilizadores no canal %s do servidor %s: %v", state.vc.ChannelID, guildID, err)
		return false
	}
	if hasOtherUsers {
		return false
	}

	log.Printf("sem utilizadores no canal %s do servidor %s, a desligar bot", state.vc.ChannelID, guildID)
	state.ssrcUsers.Reset()
	if err := state.vc.Disconnect(); err != nil {
		log.Printf("erro ao desligar bot do servidor %s: %v", guildID, err)
	}
	clearVoiceConnection(guildID, state.vc)

	return true
}

func receiveAudio(guildID string, state *voiceConnectionState) {
	log.Printf("à espera de áudio no servidor=%s canal=%s", guildID, state.vc.ChannelID)
	defer clearVoiceConnection(guildID, state.vc)

	if err := ListenAndWriteOpusToWAV(state.vc, "recordings", state.ssrcUsers); err != nil {
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

	guildID := vs.GuildID
	current := getVoiceConnection(guildID)

	// Se ChannelID vazio, a pessoa saiu da call
	if vs.ChannelID == "" {
		if current != nil {
			current.ssrcUsers.DeleteByDiscordID(vs.UserID)
			if disconnectIfBotIsAlone(s, guildID, current) {
				return
			}
		}
		return
	}

	channelID := vs.ChannelID

	if current != nil && disconnectIfBotIsAlone(s, guildID, current) {
		return
	}

	// Se já houver conexão no servidor, muda para o novo canal.
	if current != nil {
		if current.vc.ChannelID == channelID {
			return
		}

		if err := current.vc.ChangeChannel(channelID, false, false); err != nil {
			log.Printf("erro ao mover para canal %s no servidor %s: %v", channelID, guildID, err)
			_ = current.vc.Disconnect()
			clearVoiceConnection(guildID, current.vc)
		} else {
			current.ssrcUsers.Reset()
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

	ssrcUsers := NewSSRCUserMap()
	vc.AddHandler(func(vc *discordgo.VoiceConnection, vs *discordgo.VoiceSpeakingUpdate) {
		if vs == nil || vs.UserID == "" || vs.SSRC == 0 {
			return
		}
		ssrcUsers.Set(uint32(vs.SSRC), vs.UserID)
	})

	state := &voiceConnectionState{
		vc:        vc,
		ssrcUsers: ssrcUsers,
	}

	setVoiceConnection(guildID, state)
	log.Println("bot entrou na call")

	go receiveAudio(guildID, state)
}
