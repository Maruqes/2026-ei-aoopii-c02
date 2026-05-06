package main

import (
	"log"
	"strings"
	"sync"

	"github.com/bwmarrin/discordgo"
)

type voiceConnectionState struct {
	vc                  *discordgo.VoiceConnection
	ssrcUsers           *SSRCUserMap
	recordingEvents     chan recordingControlEvent
	transcriptionClient *TranscriptionClient

	usersMu sync.RWMutex
	users   map[string]voiceUserInfo

	channelMu   sync.RWMutex
	channelName string
}

type voiceUserInfo struct {
	DiscordID   string
	Username    string
	DisplayName string
}

type recordingControlEvent struct {
	finishAll bool
	user      voiceUserInfo
}

var (
	voiceConnections = make(map[string]*voiceConnectionState)
	voiceMu          sync.Mutex
)

func newVoiceConnectionState(vc *discordgo.VoiceConnection, ssrcUsers *SSRCUserMap, channelName string) *voiceConnectionState {
	return &voiceConnectionState{
		vc:                  vc,
		ssrcUsers:           ssrcUsers,
		recordingEvents:     make(chan recordingControlEvent, 128),
		transcriptionClient: NewTranscriptionClientFromEnv(),
		users:               make(map[string]voiceUserInfo),
		channelName:         channelName,
	}
}

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

func (state *voiceConnectionState) rememberUser(info voiceUserInfo) {
	if state == nil {
		return
	}

	info = info.withFallbacks()
	if info.DiscordID == "" {
		return
	}

	state.usersMu.Lock()
	state.users[info.DiscordID] = mergeVoiceUserInfo(info, state.users[info.DiscordID])
	state.usersMu.Unlock()
}

func (state *voiceConnectionState) userInfo(discordID string) voiceUserInfo {
	if state == nil || discordID == "" {
		return voiceUserInfo{DiscordID: discordID}.withFallbacks()
	}

	state.usersMu.RLock()
	info := state.users[discordID]
	state.usersMu.RUnlock()

	info.DiscordID = discordID
	return info.withFallbacks()
}

func (state *voiceConnectionState) setChannelName(channelName string) {
	if state == nil {
		return
	}

	channelName = strings.TrimSpace(channelName)
	if channelName == "" {
		return
	}

	state.channelMu.Lock()
	state.channelName = channelName
	state.channelMu.Unlock()
}

func (state *voiceConnectionState) currentChannelName() string {
	if state == nil {
		return "voice"
	}

	state.channelMu.RLock()
	channelName := state.channelName
	state.channelMu.RUnlock()

	channelName = strings.TrimSpace(channelName)
	if channelName == "" {
		return "voice"
	}
	return channelName
}

func (state *voiceConnectionState) queueUserRecordingFinish(info voiceUserInfo) {
	if state == nil {
		return
	}

	info = mergeVoiceUserInfo(info, state.userInfo(info.DiscordID))
	state.queueRecordingEvent(recordingControlEvent{user: info})
}

func (state *voiceConnectionState) queueAllRecordingsFinish() {
	if state == nil {
		return
	}

	state.queueRecordingEvent(recordingControlEvent{finishAll: true})
}

func (state *voiceConnectionState) queueRecordingEvent(event recordingControlEvent) {
	if state == nil || state.recordingEvents == nil {
		return
	}
	if !event.finishAll && event.user.DiscordID == "" {
		return
	}

	select {
	case state.recordingEvents <- event:
	default:
		log.Printf("fila de eventos de gravação cheia; evento ignorado para user=%s", event.user.DiscordID)
	}
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
	state.queueAllRecordingsFinish()
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

	if err := ListenAndWriteOpusToWAV(
		state.vc,
		"recordings",
		state.ssrcUsers,
		state.recordingEvents,
		state.transcriptionClient,
		state.userInfo,
		state.currentChannelName,
	); err != nil {
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
	userInfo := resolveVoiceUserInfo(s, guildID, vs.UserID, vs.Member)
	if current != nil {
		current.rememberUser(userInfo)
	}

	// Se ChannelID vazio, a pessoa saiu da call
	if vs.ChannelID == "" {
		if current != nil {
			current.ssrcUsers.DeleteByDiscordID(vs.UserID)
			current.queueUserRecordingFinish(userInfo)
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

		current.queueAllRecordingsFinish()
		if err := current.vc.ChangeChannel(channelID, false, false); err != nil {
			log.Printf("erro ao mover para canal %s no servidor %s: %v", channelID, guildID, err)
			_ = current.vc.Disconnect()
			clearVoiceConnection(guildID, current.vc)
		} else {
			current.ssrcUsers.Reset()
			current.setChannelName(resolveVoiceChannelName(s, channelID))
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
	state := newVoiceConnectionState(vc, ssrcUsers, resolveVoiceChannelName(s, channelID))
	state.rememberUser(userInfo)

	vc.AddHandler(func(vc *discordgo.VoiceConnection, vs *discordgo.VoiceSpeakingUpdate) {
		if vs == nil || vs.UserID == "" || vs.SSRC == 0 {
			return
		}
		ssrcUsers.Set(uint32(vs.SSRC), vs.UserID)
		if !state.hasUserInfo(vs.UserID) {
			state.rememberUser(resolveVoiceUserInfo(s, guildID, vs.UserID, nil))
		}
	})

	setVoiceConnection(guildID, state)
	log.Println("bot entrou na call")

	go receiveAudio(guildID, state)
}

func (state *voiceConnectionState) hasUserInfo(discordID string) bool {
	if state == nil || discordID == "" {
		return false
	}

	state.usersMu.RLock()
	_, ok := state.users[discordID]
	state.usersMu.RUnlock()

	return ok
}

func resolveVoiceUserInfo(s *discordgo.Session, guildID string, userID string, member *discordgo.Member) voiceUserInfo {
	info := voiceUserInfo{DiscordID: userID}
	applyMemberInfo(&info, member)

	if info.Username == "" && s != nil && s.State != nil {
		if cachedMember, err := s.State.Member(guildID, userID); err == nil {
			applyMemberInfo(&info, cachedMember)
		}
	}

	if info.Username == "" && s != nil && userID != "" {
		user, err := s.User(userID)
		if err != nil {
			log.Printf("erro ao obter username do user=%s: %v", userID, err)
		} else {
			applyUserInfo(&info, user)
		}
	}

	return info.withFallbacks()
}

func applyMemberInfo(info *voiceUserInfo, member *discordgo.Member) {
	if info == nil || member == nil {
		return
	}
	if member.User != nil {
		applyUserInfo(info, member.User)
	}
	if strings.TrimSpace(member.Nick) != "" {
		info.DisplayName = member.Nick
	}
}

func applyUserInfo(info *voiceUserInfo, user *discordgo.User) {
	if info == nil || user == nil {
		return
	}
	if strings.TrimSpace(user.ID) != "" {
		info.DiscordID = user.ID
	}
	if strings.TrimSpace(user.Username) != "" {
		info.Username = user.Username
	}
	if strings.TrimSpace(user.GlobalName) != "" && strings.TrimSpace(info.DisplayName) == "" {
		info.DisplayName = user.GlobalName
	}
}

func resolveVoiceChannelName(s *discordgo.Session, channelID string) string {
	channelID = strings.TrimSpace(channelID)
	if channelID == "" {
		return "voice"
	}

	if s != nil && s.State != nil {
		if channel, err := s.State.Channel(channelID); err == nil && strings.TrimSpace(channel.Name) != "" {
			return channel.Name
		}
	}

	if s != nil {
		channel, err := s.Channel(channelID)
		if err != nil {
			log.Printf("erro ao obter nome do canal=%s: %v", channelID, err)
		} else if strings.TrimSpace(channel.Name) != "" {
			return channel.Name
		}
	}

	return channelID
}

func (info voiceUserInfo) withFallbacks() voiceUserInfo {
	info.DiscordID = strings.TrimSpace(info.DiscordID)
	info.Username = strings.TrimSpace(info.Username)
	info.DisplayName = strings.TrimSpace(info.DisplayName)

	if info.Username == "" {
		info.Username = info.DiscordID
	}
	return info
}

func mergeVoiceUserInfo(primary voiceUserInfo, fallback voiceUserInfo) voiceUserInfo {
	primary = primary.withFallbacks()
	fallback = fallback.withFallbacks()

	if primary.DiscordID == "" {
		primary.DiscordID = fallback.DiscordID
	}
	if primary.Username == "" || (primary.Username == primary.DiscordID && fallback.Username != "" && fallback.Username != fallback.DiscordID) {
		primary.Username = fallback.Username
	}
	if primary.DisplayName == "" {
		primary.DisplayName = fallback.DisplayName
	}

	return primary.withFallbacks()
}
