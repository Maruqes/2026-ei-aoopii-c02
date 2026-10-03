package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/bwmarrin/discordgo"
)

type assistantSettings struct {
	Enabled   bool    `json:"enabled"`
	Phrase    string  `json:"phrase"`
	ChannelID *string `json:"channel_id"`
	Revision  int64   `json:"revision"`
}

const assistantDefaultSilence = 5 * time.Second

type assistantWord struct {
	Text         string  `json:"text"`
	Raw          string  `json:"-"`
	Continuation bool    `json:"-"`
	Start        float64 `json:"start"`
	End          float64 `json:"end"`
}
type realtimeEvent struct {
	Type        string          `json:"type"`
	SessionID   int64           `json:"session_id"`
	DiscordID   string          `json:"discord_id"`
	RecordingID int64           `json:"recording_id"`
	Generation  int             `json:"generation"`
	Identity    string          `json:"identity"`
	Text        string          `json:"text"`
	Start       float64         `json:"start"`
	End         float64         `json:"end"`
	Words       []assistantWord `json:"words"`
}
type assistantStream struct {
	audio         *realtimeAudioClient
	recordingID   int64
	generation    int
	seen          map[string]bool
	window        []assistantWord
	through       float64
	wordThrough   float64
	speechThrough float64
	lastSpeechAt  time.Time
	speechFloor   float64
	failed        bool
}
type assistantRequest struct {
	id            uint64
	user          string
	stream        *assistantStream
	activation    time.Time
	openedAt      time.Time
	questionEnded time.Time
	boundary      float64
	text          string
	responding    bool
	cancel        context.CancelFunc
}
type assistantController struct {
	publishMu         sync.Mutex
	mu                sync.Mutex
	state             *voiceConnectionState
	session           *discordgo.Session
	settings          assistantSettings
	silence           time.Duration
	configured        bool
	stopped           bool
	streams           map[string]*assistantStream
	request           *assistantRequest
	serial            uint64
	busyAt            map[string]time.Time
	coverage          string
	coverageCandidate string
	coverageSince     time.Time
	coverageNoticeAt  time.Time
	// Injectable effects keep timing/state tests independent of Discord and paid providers.
	send func(string, string) error
	ask  func(context.Context, string) (string, error)
}

func newAssistantController(s *discordgo.Session, state *voiceConnectionState) *assistantController {
	seconds, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv("ASSISTANT_SILENCE_SECONDS")), 64)
	if err != nil || math.IsNaN(seconds) || math.IsInf(seconds, 0) || seconds < 0.5 || seconds > 20 {
		seconds = assistantDefaultSilence.Seconds()
	}
	a := &assistantController{state: state, session: s, silence: time.Duration(seconds * float64(time.Second)), streams: map[string]*assistantStream{}, busyAt: map[string]time.Time{}}
	a.send = func(channel, text string) error {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		for _, part := range splitDiscordMessage(text) {
			_, err := s.ChannelMessageSendComplex(channel, &discordgo.MessageSend{Content: part, AllowedMentions: noMentions()}, discordgo.WithContext(ctx))
			if err != nil {
				return err
			}
		}
		return nil
	}
	a.ask = func(ctx context.Context, question string) (string, error) {
		var result struct {
			Answer string `json:"answer"`
		}
		err := state.transcriptionClient.postJSON(ctx, "/v1/assistant/question", map[string]string{"question": question}, &result)
		if err == nil && strings.TrimSpace(result.Answer) == "" {
			err = fmt.Errorf("empty assistant answer")
		}
		return result.Answer, err
	}
	return a
}
func assistantWords(text string) []string {
	// Fold Portuguese/Latin accents, including decomposed combining marks.
	text = strings.Map(func(r rune) rune {
		switch r {
		case 'á', 'à', 'â', 'ã', 'ä', 'å':
			return 'a'
		case 'é', 'è', 'ê', 'ë':
			return 'e'
		case 'í', 'ì', 'î', 'ï':
			return 'i'
		case 'ó', 'ò', 'ô', 'õ', 'ö':
			return 'o'
		case 'ú', 'ù', 'û', 'ü':
			return 'u'
		case 'ç':
			return 'c'
		case 'ñ':
			return 'n'
		case 'ý', 'ÿ':
			return 'y'
		}
		if unicode.Is(unicode.Mn, r) {
			return -1
		}
		return r
	}, strings.ToLower(text))
	return strings.FieldsFunc(text, func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsNumber(r) })
}
func (a *assistantController) destination() string {
	if a.settings.ChannelID != nil {
		return *a.settings.ChannelID
	}
	return a.state.summaryChannelID
}
func (a *assistantController) canRespond() bool {
	channelID := a.destination()
	if channelID == "" {
		return false
	}
	if a.session == nil {
		return true
	} // Tests inject the publication effect.
	channel, err := a.session.State.Channel(channelID)
	if err != nil || channel.GuildID != a.state.vc.GuildID || channel.Type != discordgo.ChannelTypeGuildText {
		return false
	}
	permissions, err := a.session.State.UserChannelPermissions(a.session.State.User.ID, channelID)
	return err == nil && permissions&(discordgo.PermissionViewChannel|discordgo.PermissionSendMessages) == (discordgo.PermissionViewChannel|discordgo.PermissionSendMessages)
}
func (a *assistantController) reset() {
	if a.request != nil && a.request.cancel != nil {
		a.request.cancel()
	}
	a.request = nil
	a.serial++
	for _, stream := range a.streams {
		stream.window = nil
		frames, _, _ := stream.audio.progress()
		stream.speechFloor = frames
	}
}
func (a *assistantController) configure(settings assistantSettings) {
	a.publishMu.Lock()
	defer a.publishMu.Unlock()
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.configured && settings.Revision <= a.settings.Revision {
		return
	}
	a.reset()
	a.settings, a.configured = settings, true
}
func (a *assistantController) stop() {
	if a == nil {
		return
	}
	a.publishMu.Lock()
	defer a.publishMu.Unlock()
	a.mu.Lock()
	defer a.mu.Unlock()
	a.stopped = true
	a.reset()
}
func (a *assistantController) relocate() {
	if a == nil {
		return
	}
	a.publishMu.Lock()
	defer a.publishMu.Unlock()
	a.mu.Lock()
	defer a.mu.Unlock()
	a.reset()
	a.streams = map[string]*assistantStream{}
	a.busyAt = map[string]time.Time{}
	a.coverage = ""
	a.coverageCandidate = ""
	a.coverageSince = time.Time{}
}
func (a *assistantController) begin(user string, audio *realtimeAudioClient) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if old := a.streams[user]; old != nil && a.request != nil && a.request.stream == old {
		a.reset()
		a.notice(user, "A captura mudou de stream. Diz a frase novamente.")
	}
	a.streams[user] = &assistantStream{audio: audio, seen: map[string]bool{}}
}
func (a *assistantController) eligible(user string) bool {
	return a.configured && a.settings.Enabled && !a.stopped && !isUserCapturePaused(user) && a.state.streaming.participantPresent(user) && a.state.streaming.grant(user).Token != ""
}
func (a *assistantController) notice(user, text string) {
	channel, serial := a.destination(), a.serial
	if channel == "" {
		return
	}
	go func() {
		a.publishMu.Lock()
		defer a.publishMu.Unlock()
		a.mu.Lock()
		valid := !a.stopped && serial == a.serial && a.settings.Enabled
		if user == "" {
			// Coverage can recover while this notice waits for another Discord send.
			_, uncovered, ready := a.state.streaming.coverageSnapshot()
			sort.Strings(uncovered)
			valid = valid && ready && strings.Join(uncovered, ", ") == a.coverage
		}
		a.mu.Unlock()
		if !valid {
			return
		}
		prefix := ""
		if user != "" {
			prefix = "<@" + user + ">, "
		}
		err := a.send(channel, prefix+text)
		if err != nil {
			log.Printf("assistant notice failed session=%d: %v", a.state.sessionID, err)
		}
		if text == "Diz" {
			a.mu.Lock()
			if a.request != nil && a.request.id == serial {
				log.Printf("assistant activation session=%d user=%s latency_ms=%d delivered=%t", a.state.sessionID, user, time.Since(a.request.activation).Milliseconds(), err == nil)
				if err != nil {
					a.reset()
				}
			}
			a.mu.Unlock()
		}
	}()
}
func (a *assistantController) fail(user string, audio *realtimeAudioClient) {
	a.mu.Lock()
	defer a.mu.Unlock()
	stream := a.streams[user]
	if stream == nil || stream.audio != audio {
		return
	}
	stream.failed = true
	stream.window = nil
	if a.request != nil && a.request.stream == stream {
		a.reset()
		a.notice(user, "O Realtime falhou. Tenta novamente quando estiver disponível.")
	}
}
func (a *assistantController) event(user string, audio *realtimeAudioClient, event realtimeEvent, now time.Time) {
	a.mu.Lock()
	defer a.mu.Unlock()
	stream := a.streams[user]
	if stream == nil || stream.audio != audio || stream.failed {
		return
	}
	if event.Type == "ready" {
		stream.recordingID, stream.generation = event.RecordingID, event.Generation
		return
	}
	frames, _, _ := audio.progress()
	if (event.Type != "final" && event.Type != "speech") || event.SessionID != a.state.sessionID || event.DiscordID != user || event.RecordingID != stream.recordingID || event.Generation != stream.generation {
		return
	}
	if event.Type == "final" && (event.Identity == "" || stream.seen[event.Identity]) {
		return
	}
	if math.IsNaN(event.Start) || math.IsNaN(event.End) || math.IsInf(event.Start, 0) || math.IsInf(event.End, 0) || event.Start < 0 || event.End < event.Start || event.End > frames+0.1 || len(stream.seen) >= 4096 {
		log.Printf("assistant invalid final session=%d user=%s recording=%d start=%f end=%f frames=%f", a.state.sessionID, user, event.RecordingID, event.Start, event.End, frames)
		stream.failed = true
		if a.request != nil && a.request.stream == stream {
			a.reset()
			a.notice(user, "Recebi tempos Realtime inválidos. Tenta novamente.")
		}
		return
	}
	if event.Type == "speech" {
		// Partial text never enters the question; its timing keeps quiet speech
		// and unfinished provider output from being mistaken for silence.
		if event.End > max(stream.through, stream.speechThrough) && event.End > stream.speechFloor {
			stream.speechThrough = event.End
			stream.lastSpeechAt = audio.startedAt.Add(time.Duration(event.End * float64(time.Second)))
		}
		return
	}
	stream.seen[event.Identity] = true
	// WebSocket delivery is ordered; approximate metadata envelopes may overlap.
	// Empty flush markers and entirely late finals must not cancel a live capture.
	if len(event.Words) == 0 && event.End <= stream.through {
		return
	}
	stream.through = max(stream.through, event.End)
	if !a.eligible(user) || !a.canRespond() || event.End <= stream.speechFloor {
		return
	}
	words := event.Words
	if len(words) == 0 {
		for _, word := range strings.Fields(event.Text) {
			words = append(words, assistantWord{Text: word, Start: event.Start, End: event.End})
		}
	}
	words = append([]assistantWord(nil), words...)
	sort.SliceStable(words, func(i, j int) bool { return words[i].Start < words[j].Start })
	tokens := []assistantWord{}
	priorWordThrough := stream.wordThrough
	for _, word := range words {
		if math.IsNaN(word.Start) || math.IsNaN(word.End) || math.IsInf(word.Start, 0) || math.IsInf(word.End, 0) || word.Start < 0 || word.End < word.Start || word.End > frames+0.1 {
			stream.failed = true
			if a.request != nil && a.request.stream == stream {
				a.reset()
				a.notice(user, "Recebi tempos Realtime inválidos. Tenta novamente.")
			}
			return
		}
		if len(event.Words) > 0 && word.End <= priorWordThrough {
			continue
		}
		stream.wordThrough = max(stream.wordThrough, word.End)
		stream.through = max(stream.through, word.End)
		if word.End <= stream.speechFloor {
			continue
		}
		normalized := assistantWords(word.Text)
		for part, text := range normalized {
			tokens = append(tokens, assistantWord{Text: text, Raw: word.Text, Continuation: part > 0, Start: word.Start, End: word.End})
		}
	}
	if len(tokens) == 0 {
		return
	}
	phrase := assistantWords(a.settings.Phrase)
	if len(phrase) < 2 {
		return
	}
	if a.request != nil && a.request.user == user {
		if a.request.stream != stream {
			return
		}
		if a.request.responding {
			return
		}
		if len(tokens) == 1 && tokens[0].Text == "cancela" {
			a.reset()
			a.notice(user, "Cancelado.")
			return
		}
		// A repeated wake phrase is acknowledgement, never another request.
		text := assistantQuestionTokens(tokens, phrase)
		a.request.text = strings.TrimSpace(a.request.text + " " + text)
		if len(a.request.text) > 2000 {
			a.reset()
			a.notice(user, "Faz uma pergunta mais curta.")
		}
		return
	}
	if len(stream.window) > 0 && tokens[0].Start > stream.window[len(stream.window)-1].End+a.silence.Seconds() {
		stream.window = nil
	}
	combined := append(stream.window, tokens...)
	// Retain the current utterance so a wake phrase at its end keeps the question.
	// 2001 words suffice to reject questions beyond the existing 2000-byte limit.
	windowStart := max(0, len(combined)-2001)
	for j := windowStart + 1; j < len(combined); j++ {
		if combined[j].Start > combined[j-1].End+a.silence.Seconds() {
			windowStart = j
		}
	}
	stream.window = append([]assistantWord(nil), combined[windowStart:]...)
	for i := 0; i < len(combined); i++ {
		end := assistantPhraseEnd(combined, i, phrase)
		if end < 0 {
			continue
		}
		contiguousSpeech := true
		for j := i + 1; j < end; j++ {
			if combined[j].Start > combined[j-1].End+a.silence.Seconds() {
				contiguousSpeech = false
			}
		}
		if !contiguousSpeech {
			continue
		}
		if a.request != nil {
			if now.Sub(a.busyAt[user]) >= 5*time.Second {
				a.busyAt[user] = now
				a.notice(user, "Estou ocupado. Diz a frase novamente depois da resposta.")
			}
			stream.window = nil
			return
		}
		a.serial++
		boundary := combined[end-1].End
		// Include speech on either side, without replaying an earlier utterance.
		prefixStart := i
		for prefixStart > 0 && combined[prefixStart].Start <= combined[prefixStart-1].End+a.silence.Seconds() {
			prefixStart--
		}
		question := append([]assistantWord(nil), combined[prefixStart:i]...)
		question = append(question, combined[end:]...)
		a.request = &assistantRequest{id: a.serial, user: user, stream: stream, activation: audio.startedAt.Add(time.Duration(boundary * float64(time.Second))), openedAt: now, boundary: boundary, text: assistantQuestionTokens(question, phrase)}
		stream.window = nil
		if len(a.request.text) > 2000 {
			a.reset()
			a.notice(user, "Faz uma pergunta mais curta.")
			return
		}
		a.notice(user, "Diz")
		return
	}
}

// Permit one recognition edit and up to two intervening words, in phrase order.
// Joining also accepts recognition that merges or splits words ("olamacaco").
func assistantPhraseEnd(tokens []assistantWord, start int, phrase []string) int {
	if len(phrase) == 0 {
		return -1
	}
	joined := ""
	for end := start; end < len(tokens) && end < start+len(phrase)+2; end++ {
		joined += tokens[end].Text
		if assistantNearWord(joined, strings.Join(phrase, "")) {
			return end + 1
		}
	}
	var match func(int, int, int, int) int
	match = func(index, word, edits, gaps int) int {
		if word == len(phrase) {
			return index
		}
		gapLimit := 2 - gaps
		if word == 0 {
			gapLimit = 0
		}
		for gap := 0; gap <= gapLimit && index+gap < len(tokens); gap++ {
			candidate := tokens[index+gap].Text
			cost := 0
			if candidate != phrase[word] {
				if edits != 0 || !assistantNearWord(candidate, phrase[word]) {
					continue
				}
				cost = 1
			}
			if end := match(index+gap+1, word+1, edits+cost, gaps+gap); end >= 0 {
				return end
			}
		}
		return -1
	}
	return match(start, 0, 0, 0)
}

func assistantNearWord(a, b string) bool {
	if a == b {
		return true
	}
	left, right := []rune(a), []rune(b)
	if min(len(left), len(right)) < 3 || len(left)-len(right) > 1 || len(right)-len(left) > 1 {
		return false
	}
	if len(left) == len(right) {
		differences := []int{}
		for i := range left {
			if left[i] != right[i] {
				differences = append(differences, i)
			}
		}
		if len(differences) == 1 {
			return true
		}
		return len(differences) == 2 && differences[1] == differences[0]+1 && left[differences[0]] == right[differences[1]] && left[differences[1]] == right[differences[0]]
	}
	if len(left) < len(right) {
		left, right = right, left
	}
	for i, j, edits := 0, 0, 0; j < len(right); {
		if left[i] == right[j] {
			i++
			j++
		} else {
			edits++
			i++
			if edits > 1 {
				return false
			}
		}
	}
	return true
}

func assistantQuestionTokens(tokens []assistantWord, phrase []string) string {
	words := []string{}
	for i := 0; i < len(tokens); {
		if end := assistantPhraseEnd(tokens, i, phrase); end >= 0 {
			i = end
			continue
		}
		if tokens[i].Continuation {
			i++
			continue
		}
		raw := tokens[i].Raw
		if raw == "" {
			raw = tokens[i].Text
		}
		words = append(words, raw)
		i++
	}
	return strings.Join(words, " ")
}
func (a *assistantController) tick(now time.Time) {
	a.mu.Lock()
	defer a.mu.Unlock()
	request := a.request
	if request == nil {
		return
	}
	if !a.eligible(request.user) || request.stream.failed || !a.canRespond() {
		a.reset()
		a.notice(request.user, "Pedido cancelado: autor ou Realtime indisponível.")
		return
	}
	if request.responding {
		return
	}
	_, speech, lastSpeech := request.stream.audio.progress()
	speech = max(speech, request.stream.speechThrough)
	hasSpeech := speech > request.boundary+0.02 || request.text != ""
	if !hasSpeech && now.Sub(request.openedAt) >= 10*time.Second {
		a.reset()
		a.notice(request.user, "Não ouvi uma pergunta. Diz a frase para tentar novamente.")
		return
	}
	if now.Sub(request.openedAt) >= 30*time.Second {
		a.reset()
		a.notice(request.user, "Faz uma pergunta mais curta.")
		return
	}
	quietSince := lastSpeech
	recognizedEnd := request.stream.audio.startedAt.Add(time.Duration(request.stream.wordThrough * float64(time.Second)))
	// These are audio times, not transcript arrival times. Late results do not
	// restart the author's silence interval; recognized words cover quiet speech.
	for _, activity := range []time.Time{request.stream.lastSpeechAt, recognizedEnd} {
		if activity.After(quietSince) {
			quietSince = activity
		}
	}
	if !hasSpeech || now.Sub(quietSince) < a.silence {
		return
	}
	if request.stream.through+0.02 < speech {
		if now.Sub(quietSince) >= a.silence+5*time.Second {
			a.reset()
			a.notice(request.user, "Os finais Realtime não chegaram a tempo. Repete a pergunta.")
		}
		return
	}
	if strings.TrimSpace(request.text) == "" {
		a.reset()
		a.notice(request.user, "Não consegui reconhecer a pergunta. Tenta novamente.")
		return
	}
	request.responding = true
	request.questionEnded = lastSpeech
	if recognizedEnd.After(request.questionEnded) {
		request.questionEnded = recognizedEnd
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	request.cancel = cancel
	go func() {
		answer, err := a.ask(ctx, request.text)
		cancel()
		a.publishMu.Lock()
		defer a.publishMu.Unlock()
		a.mu.Lock()
		if a.request != request || !a.eligible(request.user) || !a.canRespond() {
			if a.request == request {
				a.reset()
			}
			a.mu.Unlock()
			return
		}
		channel := a.destination()
		a.mu.Unlock()
		content := "<@" + request.user + "> · **Pergunta:** " + request.text + "\n\n" + answer
		if err != nil {
			content = "<@" + request.user + ">, não consegui responder a tempo. Tenta novamente."
		}
		sendErr := a.send(channel, content)
		if sendErr != nil {
			log.Printf("assistant reply failed session=%d: %v", a.state.sessionID, sendErr)
		}
		log.Printf("assistant response session=%d user=%s latency_ms=%d answered=%t delivered=%t", a.state.sessionID, request.user, time.Since(request.questionEnded).Milliseconds(), err == nil, sendErr == nil)
		// No retry: a Discord timeout can mean that the answer was already published.
		a.mu.Lock()
		defer a.mu.Unlock()
		if a.request == request {
			a.reset()
		}
	}()
}
func (a *assistantController) run(guildID string) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	refresh := time.NewTicker(2 * time.Second)
	defer refresh.Stop()
	go a.refresh(guildID)
	for {
		select {
		case <-a.state.recordingStop:
			a.stop()
			return
		case <-a.state.recordingDone:
			a.stop()
			return
		case now := <-ticker.C:
			a.tick(now)
		case <-refresh.C:
			go a.refresh(guildID)
		}
	}
}
func (a *assistantController) refresh(guildID string) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	var config assistantSettings
	if a.state.transcriptionClient.getJSON(ctx, "/v1/guilds/"+url.PathEscape(guildID)+"/assistant", &config) == nil {
		a.configure(config)
	}
	a.updateCoverage(time.Now())
}

func (a *assistantController) updateCoverage(now time.Time) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.configured || !a.settings.Enabled || a.stopped {
		return
	}
	_, uncovered, ready := a.state.streaming.coverageSnapshot()
	if !ready {
		a.coverageCandidate = ""
		a.coverageSince = time.Time{}
		return
	}
	sort.Strings(uncovered)
	coverage := strings.Join(uncovered, ", ")
	if a.coverageSince.IsZero() || coverage != a.coverageCandidate {
		a.coverageCandidate = coverage
		a.coverageSince = now
		return
	}
	// Provider recovery suppresses assignments for 10s; don't announce that transient.
	if now.Sub(a.coverageSince) < 15*time.Second || coverage == a.coverage {
		return
	}
	if coverage == "" {
		a.coverage = ""
		return
	}
	if a.destination() == "" || (!a.coverageNoticeAt.IsZero() && now.Sub(a.coverageNoticeAt) < time.Minute) {
		return
	}
	a.coverage = coverage
	a.coverageNoticeAt = now
	a.notice("", "Assistente sem Realtime para: "+coverage+". Estas pessoas não podem ativar o assistente.")
}

func assistantCommand() *discordgo.ApplicationCommand {
	options := []*discordgo.ApplicationCommandOption{}
	for _, name := range []string{"status", "phrase", "channel", "enable", "disable"} {
		option := &discordgo.ApplicationCommandOption{Type: discordgo.ApplicationCommandOptionSubCommand, Name: name, Description: "Assistente: " + name}
		if name == "phrase" {
			option.Options = []*discordgo.ApplicationCommandOption{{Type: discordgo.ApplicationCommandOptionString, Name: "value", Description: "Wake phrase, 2 to 5 words", Required: true, MaxLength: 50}}
		}
		if name == "channel" {
			option.Options = []*discordgo.ApplicationCommandOption{{Type: discordgo.ApplicationCommandOptionChannel, Name: "value", Description: "Response text channel", Required: true, ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildText}}}
		}
		options = append(options, option)
	}
	return &discordgo.ApplicationCommand{Name: "assistant", Description: "Configure the voice assistant or inspect Realtime coverage", Options: options}
}
func assistantHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	if i.GuildID == "" {
		respondText(s, i, "Usa este comando num servidor.")
		return
	}
	options := i.ApplicationCommandData().Options
	if len(options) != 1 {
		respondText(s, i, "Escolhe uma opção do assistente.")
		return
	}
	option := options[0]
	if option.Name != "status" && !canManageServer(i) {
		respondText(s, i, "Este comando requer a permissão Gerir Servidor.")
		return
	}
	changes := map[string]any{}
	switch option.Name {
	case "enable":
		changes["enabled"] = true
	case "disable":
		changes["enabled"] = false
	case "phrase":
		changes["phrase"] = option.Options[0].StringValue()
	case "channel":
		channel := option.Options[0].ChannelValue(s)
		if channel == nil || channel.GuildID != i.GuildID || channel.Type != discordgo.ChannelTypeGuildText {
			respondText(s, i, "Escolhe um canal de texto deste servidor.")
			return
		}
		permissions, err := s.State.UserChannelPermissions(s.State.User.ID, channel.ID)
		if err != nil || permissions&(discordgo.PermissionViewChannel|discordgo.PermissionSendMessages) != (discordgo.PermissionViewChannel|discordgo.PermissionSendMessages) {
			respondText(s, i, "Preciso de acesso e permissão para escrever nesse canal.")
			return
		}
		changes["channel_id"] = channel.ID
	}
	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	path := "/v1/guilds/" + url.PathEscape(i.GuildID) + "/assistant"
	var config assistantSettings
	var err error
	if option.Name == "status" {
		err = client.getJSON(ctx, path, &config)
	} else {
		err = client.postJSON(ctx, path, changes, &config)
	}
	if err != nil {
		respondText(s, i, fmt.Sprintf("Assistente: %v", err))
		return
	}
	channel := "Configura /assistant channel"
	coverage := "Sem chamada ativa"
	if config.ChannelID != nil {
		channel = "<#" + *config.ChannelID + ">"
	}
	if state := getVoiceConnection(i.GuildID); state != nil {
		if state.assistant != nil {
			state.assistant.configure(config)
		}
		if config.ChannelID == nil && state.summaryChannelID != "" {
			channel = "<#" + state.summaryChannelID + ">"
		}
		covered, uncovered, ready := state.streaming.coverageSnapshot()
		if state.assistant != nil {
			state.assistant.mu.Lock()
		}
		if state.assistant != nil {
			if !state.assistant.canRespond() {
				channel += " (indisponível; configura /assistant channel)"
			}
			state.assistant.mu.Unlock()
		}
		coverage = fmt.Sprintf("Realtime: %d/%d · Cobertos: %s · Sem Realtime: %s", len(covered), len(covered)+len(uncovered), strings.Join(covered, ", "), strings.Join(uncovered, ", "))
		if !ready {
			coverage = "Realtime: a aguardar sincronização das reservas."
		}
	}
	respondLongText(s, i, fmt.Sprintf("Assistente ativo: %t · Frase: %s · Destino: %s\n%s", config.Enabled, config.Phrase, channel, coverage))
}
