package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/bwmarrin/discordgo"
)

type voiceConnectionState struct {
	vc                  *discordgo.VoiceConnection
	ssrcUsers           *SSRCUserMap
	recordingEvents     chan recordingControlEvent
	transcriptionClient *TranscriptionClient
	sessionID           int64
	summaryChannelID    string

	usersMu sync.RWMutex
	users   map[string]voiceUserInfo

	channelMu   sync.RWMutex
	channelName string

	leaveTimerMu sync.Mutex
	leaveTimer   *time.Timer
}

type voiceUserInfo struct {
	DiscordID   string
	Username    string
	DisplayName string
}

type recordingControlEvent struct {
	finishAll     bool
	stopListening bool
	user          voiceUserInfo
}

var (
	voiceConnections        = make(map[string]*voiceConnectionState)
	voiceMu                 sync.Mutex
	voiceJoinMu             sync.Mutex
	botEnabled              atomic.Bool
	botLeaveOverrideMinutes atomic.Int64
)

func init() {
	botEnabled.Store(true)
	botLeaveOverrideMinutes.Store(-1)
}

func newVoiceConnectionState(
	vc *discordgo.VoiceConnection,
	ssrcUsers *SSRCUserMap,
	channelName string,
	transcriptionClient *TranscriptionClient,
	sessionID int64,
	summaryChannelID string,
) *voiceConnectionState {
	return &voiceConnectionState{
		vc:                  vc,
		ssrcUsers:           ssrcUsers,
		recordingEvents:     make(chan recordingControlEvent, 128),
		transcriptionClient: transcriptionClient,
		sessionID:           sessionID,
		summaryChannelID:    summaryChannelID,
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
	current, ok := voiceConnections[guildID]
	if !ok {
		voiceMu.Unlock()
		return
	}
	if current.vc == vc {
		delete(voiceConnections, guildID)
	}
	voiceMu.Unlock()

	if current.vc == vc {
		current.stopLeaveTimer()
	}
}

func setBotEnabled(enabled bool) {
	botEnabled.Store(enabled)
}

func isBotEnabled() bool {
	return botEnabled.Load()
}

func stopAllVoiceConnections() {
	voiceMu.Lock()
	connections := voiceConnections
	voiceConnections = make(map[string]*voiceConnectionState)
	voiceMu.Unlock()

	for guildID, state := range connections {
		if state == nil || state.vc == nil {
			continue
		}

		state.stopLeaveTimer()
		state.queueAllRecordingsFinish()
		state.ssrcUsers.Reset()
		if err := state.vc.Disconnect(); err != nil {
			log.Printf("erro ao desligar bot do servidor %s: %v", guildID, err)
		}
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

	state.queueRecordingEvent(recordingControlEvent{finishAll: true, stopListening: true})
}

func (state *voiceConnectionState) queueCloseAllRecordings() {
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
	case <-time.After(5 * time.Second):
		log.Printf("fila de eventos de gravação cheia; a aguardar envio para user=%s finishAll=%v", event.user.DiscordID, event.finishAll)
		state.recordingEvents <- event
	}
}

func botLeaveDurationFromEnv(raw string) time.Duration {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0
	}

	minutes, err := strconv.Atoi(raw)
	if err != nil || minutes < 0 {
		log.Printf("BOT_LEAVE inválido %q; saída automática desativada", raw)
		return 0
	}
	if minutes == 0 {
		return 0
	}
	return time.Duration(minutes) * time.Minute
}

func botLeaveDuration() time.Duration {
	override := botLeaveOverrideMinutes.Load()
	if override >= 0 {
		if override == 0 {
			return 0
		}
		return time.Duration(override) * time.Minute
	}
	return botLeaveDurationFromEnv(os.Getenv("BOT_LEAVE"))
}

func botLeaveMinutes() int {
	override := botLeaveOverrideMinutes.Load()
	if override >= 0 {
		return int(override)
	}
	raw := strings.TrimSpace(os.Getenv("BOT_LEAVE"))
	if raw == "" {
		return 0
	}
	minutes, err := strconv.Atoi(raw)
	if err != nil || minutes < 0 {
		return 0
	}
	return minutes
}

func setBotLeaveMinutes(minutes int) {
	if minutes < 0 {
		minutes = 0
	}
	botLeaveOverrideMinutes.Store(int64(minutes))
	applyBotLeaveTimeoutToActiveConnections()
}

func applyBotLeaveTimeoutToActiveConnections() {
	duration := botLeaveDuration()

	voiceMu.Lock()
	connections := make(map[string]*voiceConnectionState, len(voiceConnections))
	for guildID, state := range voiceConnections {
		connections[guildID] = state
	}
	voiceMu.Unlock()

	for guildID, state := range connections {
		state.startLeaveTimer(guildID, duration)
	}
}

func voiceConnectionStatus() string {
	return voiceConnectionStatusForLanguage(currentBotLanguage())
}

func voiceConnectionStatusForLanguage(lang botLanguage) string {
	if !isBotEnabled() {
		return textForLanguage(lang, "pausado (/start para reativar)", "paused (/start to reactivate)")
	}

	voiceMu.Lock()
	defer voiceMu.Unlock()

	if len(voiceConnections) == 0 {
		return textForLanguage(lang, "fora de call", "not in a call")
	}

	parts := make([]string, 0, len(voiceConnections))
	for guildID, state := range voiceConnections {
		if state == nil || state.vc == nil {
			parts = append(parts, guildID+": "+textForLanguage(lang, "desconhecido", "unknown"))
			continue
		}
		channelName := state.currentChannelName()
		if channelName == "" {
			channelName = state.vc.ChannelID
		}
		parts = append(parts, fmt.Sprintf("%s (%s)", channelName, guildID))
	}
	sort.Strings(parts)
	return textForLanguage(lang, "em call: ", "in call: ") + strings.Join(parts, ", ")
}

func (state *voiceConnectionState) startLeaveTimer(guildID string, duration time.Duration) {
	if state == nil || duration <= 0 {
		return
	}

	state.leaveTimerMu.Lock()
	if state.leaveTimer != nil {
		state.leaveTimer.Stop()
	}
	state.leaveTimer = time.AfterFunc(duration, func() {
		log.Printf("limite de %s atingido no servidor %s, a desligar bot e iniciar processamento", duration, guildID)
		disconnectVoiceConnection(guildID, state)
	})
	state.leaveTimerMu.Unlock()
}

func (state *voiceConnectionState) stopLeaveTimer() {
	if state == nil {
		return
	}

	state.leaveTimerMu.Lock()
	if state.leaveTimer != nil {
		state.leaveTimer.Stop()
		state.leaveTimer = nil
	}
	state.leaveTimerMu.Unlock()
}

func disconnectVoiceConnection(guildID string, state *voiceConnectionState) bool {
	if state == nil || state.vc == nil {
		return false
	}

	voiceMu.Lock()
	current := voiceConnections[guildID]
	if current != state {
		voiceMu.Unlock()
		return false
	}
	delete(voiceConnections, guildID)
	voiceMu.Unlock()

	state.stopLeaveTimer()
	state.queueAllRecordingsFinish()
	state.ssrcUsers.Reset()
	if err := state.vc.Disconnect(); err != nil {
		log.Printf("erro ao desligar bot do servidor %s: %v", guildID, err)
	}
	return true
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
	return disconnectVoiceConnection(guildID, state)
}

func receiveAudio(s *discordgo.Session, guildID string, state *voiceConnectionState) {
	log.Printf("à espera de áudio no servidor=%s canal=%s", guildID, state.vc.ChannelID)
	defer clearVoiceConnection(guildID, state.vc)

	err := ListenAndWriteOpusToWAV(
		state.vc,
		recordingsDirFromEnv(),
		state.sessionID,
		state.ssrcUsers,
		state.recordingEvents,
		state.transcriptionClient,
		state.userInfo,
		state.currentChannelName,
	)
	if err != nil {
		log.Printf("erro ao gravar áudio no servidor=%s: %v", guildID, err)
	}

	log.Printf("captura finalizada no servidor=%s", guildID)
	finishSessionAndPostSummary(s, state)
}

func OnVoiceStateUpdate(s *discordgo.Session, vs *discordgo.VoiceStateUpdate) {
	// Ignora o próprio bot
	if s.State != nil && s.State.User != nil && vs.UserID == s.State.User.ID {
		return
	}
	if !isBotEnabled() {
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

	// Se já houver conexão no servidor, muda para o novo canal.
	if current != nil {
		if current.vc.ChannelID == channelID {
			return
		}

		current.queueCloseAllRecordings()
		if err := current.vc.ChangeChannel(channelID, false, false); err != nil {
			log.Printf("erro ao mover para canal %s no servidor %s: %v", channelID, guildID, err)
			_ = current.vc.Disconnect()
			clearVoiceConnection(guildID, current.vc)
		} else {
			current.ssrcUsers.Reset()
			current.setChannelName(resolveVoiceChannelName(s, channelID))
			current.startLeaveTimer(guildID, botLeaveDuration())
			log.Printf("bot movido para canal %s no servidor %s", channelID, guildID)
		}
		return
	}

	voiceJoinMu.Lock()
	defer voiceJoinMu.Unlock()

	if existing := getVoiceConnection(guildID); existing != nil {
		return
	}

	log.Printf("utilizador %s entrou no canal %s do servidor %s", vs.UserID, channelID, guildID)

	vc, err := s.ChannelVoiceJoin(guildID, channelID, true, false)
	if err != nil {
		log.Println("erro ao entrar no voice channel:", err)
		return
	}

	ssrcUsers := NewSSRCUserMap()
	channelName := resolveVoiceChannelName(s, channelID)
	transcriptionClient := botAPIClient
	if transcriptionClient == nil {
		transcriptionClient = NewTranscriptionClientFromEnv()
	}
	summaryChannelID := resolveSummaryTextChannelID(s, guildID)
	if summaryChannelID == "" {
		log.Printf("nao foi possivel resolver canal de texto para resumo no servidor %s", guildID)
	}
	sessionID := createAPISession(transcriptionClient, guildID, channelID, channelName, summaryChannelID)
	state := newVoiceConnectionState(vc, ssrcUsers, channelName, transcriptionClient, sessionID, summaryChannelID)
	state.rememberUser(userInfo)

	vc.AddHandler(func(vc *discordgo.VoiceConnection, vs *discordgo.VoiceSpeakingUpdate) {
		if vs == nil || vs.UserID == "" || vs.SSRC == 0 {
			return
		}
		ssrcUsers.Set(uint32(vs.SSRC), vs.UserID)
		log.Printf("SSRC associado user=%s ssrc=%d", vs.UserID, vs.SSRC)
		if !state.hasUserInfo(vs.UserID) {
			state.rememberUser(resolveVoiceUserInfo(s, guildID, vs.UserID, nil))
		}
	})

	setVoiceConnection(guildID, state)
	log.Println("bot entrou na call")

	state.startLeaveTimer(guildID, botLeaveDuration())
	go receiveAudio(s, guildID, state)
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

func resolveSummaryTextChannelID(s *discordgo.Session, guildID string) string {
	guild, err := summaryGuild(s, guildID)
	if err != nil {
		log.Printf("erro ao obter servidor %s para resolver canal de resumo: %v", guildID, err)
		return ""
	}

	channels, err := summaryGuildChannels(s, guild)
	if err != nil {
		log.Printf("erro ao obter canais do servidor %s para resolver canal de resumo: %v", guildID, err)
		return ""
	}

	systemChannelID := strings.TrimSpace(guild.SystemChannelID)
	if systemChannelID != "" && summaryTextChannelByID(channels, systemChannelID) == nil {
		channel, err := s.Channel(systemChannelID)
		if err != nil {
			log.Printf("erro ao obter canal de sistema %s do servidor %s: %v", systemChannelID, guildID, err)
		} else {
			channels = append(channels, channel)
		}
	}

	channel := selectSummaryTextChannel(channels, systemChannelID, func(channel *discordgo.Channel) bool {
		return canSendSummaryToChannel(s, channel)
	})
	if channel == nil {
		return ""
	}

	if channel.ID == systemChannelID {
		log.Printf("canal de resumo resolvido pelo canal de sistema servidor=%s canal=%s", guildID, channel.ID)
	} else {
		log.Printf("canal de resumo resolvido pelo primeiro canal de texto servidor=%s canal=%s", guildID, channel.ID)
	}
	return channel.ID
}

func summaryGuild(s *discordgo.Session, guildID string) (*discordgo.Guild, error) {
	if s != nil && s.State != nil {
		if guild, err := s.State.Guild(guildID); err == nil {
			return guild, nil
		}
	}
	return s.Guild(guildID)
}

func summaryGuildChannels(s *discordgo.Session, guild *discordgo.Guild) ([]*discordgo.Channel, error) {
	if guild != nil && len(guild.Channels) > 0 {
		return guild.Channels, nil
	}
	return s.GuildChannels(guild.ID)
}

func selectSummaryTextChannel(
	channels []*discordgo.Channel,
	systemChannelID string,
	canSend func(*discordgo.Channel) bool,
) *discordgo.Channel {
	if channel := summaryTextChannelByID(channels, systemChannelID); channel != nil && summaryChannelAllowed(channel, canSend) {
		return channel
	}

	candidates := make([]*discordgo.Channel, 0, len(channels))
	for _, channel := range channels {
		if isSummaryTextChannel(channel) && summaryChannelAllowed(channel, canSend) {
			candidates = append(candidates, channel)
		}
	}
	if len(candidates) == 0 {
		return nil
	}

	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].Position == candidates[j].Position {
			return candidates[i].ID < candidates[j].ID
		}
		return candidates[i].Position < candidates[j].Position
	})
	return candidates[0]
}

func summaryTextChannelByID(channels []*discordgo.Channel, channelID string) *discordgo.Channel {
	channelID = strings.TrimSpace(channelID)
	if channelID == "" {
		return nil
	}
	for _, channel := range channels {
		if channel != nil && channel.ID == channelID && isSummaryTextChannel(channel) {
			return channel
		}
	}
	return nil
}

func isSummaryTextChannel(channel *discordgo.Channel) bool {
	return channel != nil && channel.Type == discordgo.ChannelTypeGuildText
}

func summaryChannelAllowed(channel *discordgo.Channel, canSend func(*discordgo.Channel) bool) bool {
	return canSend == nil || canSend(channel)
}

func canSendSummaryToChannel(s *discordgo.Session, channel *discordgo.Channel) bool {
	if s == nil || channel == nil {
		return false
	}

	if s.State == nil || s.State.User == nil || strings.TrimSpace(s.State.User.ID) == "" {
		return true
	}

	permissions, err := s.UserChannelPermissions(s.State.User.ID, channel.ID)
	if err != nil {
		log.Printf("erro ao verificar permissao de envio no canal de resumo %s: %v", channel.ID, err)
		return false
	}
	return permissions&discordgo.PermissionSendMessages != 0
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

func createAPISession(
	client *TranscriptionClient,
	guildID string,
	channelID string,
	channelName string,
	summaryChannelID string,
) int64 {
	if client == nil {
		return 0
	}

	session, err := client.CreateSession(context.Background(), CreateSessionRequest{
		GuildID:          guildID,
		VoiceChannelID:   channelID,
		ChannelName:      channelName,
		SummaryChannelID: summaryChannelID,
		StartedAt:        nowUTC(),
	})
	if err != nil {
		log.Printf("erro ao criar sessao de voz na API: %v", err)
		return 0
	}
	log.Printf("sessao de voz criada id=%d channel=%s", session.ID, channelName)
	return session.ID
}

func finishSessionAndPostSummary(s *discordgo.Session, state *voiceConnectionState) {
	if state == nil || state.transcriptionClient == nil || state.sessionID <= 0 {
		return
	}

	lang := currentBotLanguage()
	summary, err := state.transcriptionClient.FinishSessionAndWait(context.Background(), state.sessionID, lang.apiValue())
	if err != nil {
		log.Printf("erro ao finalizar sessao id=%d: %v", state.sessionID, err)
	}
	if summary == nil {
		return
	}
	if strings.TrimSpace(state.summaryChannelID) == "" {
		log.Printf("resumo pronto para sessao id=%d mas nao ha canal de texto resolvido", state.sessionID)
		return
	}

	checkAndSendSpeechmaticsUsageAlerts(s, state.summaryChannelID, state.transcriptionClient, lang)

	summaryText := strings.TrimSpace(stringValue(summary.Summary))
	if summary.Status == "agent_failed" {
		errText := strings.TrimSpace(stringValue(summary.AgentError))
		if errText == "" {
			errText = textForLanguage(lang, "erro desconhecido no agente de resumo", "unknown summary agent error")
		}
		if _, err := s.ChannelMessageSend(state.summaryChannelID, textForLanguage(lang, "Resumo da sessao falhou: ", "Session summary failed: ")+errText); err != nil {
			log.Printf("erro ao publicar falha da sessao id=%d no canal %s: %v", state.sessionID, state.summaryChannelID, err)
		}
		return
	}
	if summaryText == "" {
		return
	}

	if err := sendLongChannelMessage(s, state.summaryChannelID, summaryText); err != nil {
		log.Printf("erro ao publicar resumo da sessao id=%d no canal %s: %v", state.sessionID, state.summaryChannelID, err)
	}
}

func recordingsDirFromEnv() string {
	if dir := strings.TrimSpace(os.Getenv("RECORDINGS_DIR")); dir != "" {
		return dir
	}
	return "recordings"
}

func nowUTC() time.Time {
	return time.Now().UTC()
}

func stringValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
