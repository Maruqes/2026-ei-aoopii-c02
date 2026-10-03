package main

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/bwmarrin/discordgo"
	"github.com/gorilla/websocket"
)

// Keep entries observed during voice/API connection setup ahead of later network work.
var pendingVoiceArrivals = struct {
	sync.Mutex
	order map[string][]string
}{order: map[string][]string{}}

func observePendingVoiceArrival(vs *discordgo.VoiceStateUpdate) {
	pendingVoiceArrivals.Lock()
	defer pendingVoiceArrivals.Unlock()
	if before := vs.BeforeUpdate; before != nil && before.ChannelID != vs.ChannelID {
		key := vs.GuildID + "/" + before.ChannelID
		for i, id := range pendingVoiceArrivals.order[key] {
			if id == vs.UserID {
				pendingVoiceArrivals.order[key] = append(pendingVoiceArrivals.order[key][:i], pendingVoiceArrivals.order[key][i+1:]...)
				break
			}
		}
	}
	if vs.ChannelID == "" || (vs.BeforeUpdate != nil && vs.BeforeUpdate.ChannelID == vs.ChannelID) {
		return
	}
	key := vs.GuildID + "/" + vs.ChannelID
	for _, id := range pendingVoiceArrivals.order[key] {
		if id == vs.UserID {
			return
		}
	}
	pendingVoiceArrivals.order[key] = append(pendingVoiceArrivals.order[key], vs.UserID)
}

func initializeVoiceArrivalOrder(c *streamingController, guildID, channelID string, snapshot []string) {
	pendingVoiceArrivals.Lock()
	known := pendingVoiceArrivals.order[guildID+"/"+channelID]
	delete(pendingVoiceArrivals.order, guildID+"/"+channelID)
	pendingVoiceArrivals.Unlock()
	present := map[string]bool{}
	for _, id := range snapshot {
		present[id] = true
	}
	observed := map[string]bool{}
	for _, id := range known {
		observed[id] = true
	}
	unknown := []string{}
	for _, id := range snapshot {
		if !observed[id] {
			unknown = append(unknown, id)
		}
	}
	c.reset(unknown, channelID)
	for _, id := range known {
		if present[id] {
			c.join(id)
		}
	}
}

type streamGrant struct {
	Token   string `json:"token"`
	KeyName string `json:"key_name"`
}

type streamingStatus struct {
	Enabled        bool                   `json:"enabled"`
	Preferred      bool                   `json:"preferred"`
	Available      bool                   `json:"available"`
	Occupied       int                    `json:"occupied"`
	Capacity       int                    `json:"capacity"`
	Queued         int                    `json:"queued"`
	Assignments    map[string]streamGrant `json:"assignments"`
	Exhausted      bool                   `json:"exhausted"`
	NoticeSent     bool                   `json:"notice_sent"`
	CleanupPending bool                   `json:"cleanup_pending"`
}

// Arrival order belongs to participants, never to speaking updates or SSRCs.
type streamingController struct {
	mu        sync.RWMutex
	syncMu    sync.Mutex
	order     []string
	present   map[string]bool
	grants    map[string]streamGrant
	exhausted bool
	queued    int
	channelID string
	stopped   bool
	state     *voiceConnectionState
}

func newStreamingController(state *voiceConnectionState) *streamingController {
	c := &streamingController{state: state, present: map[string]bool{}, grants: map[string]streamGrant{}}
	if state.vc != nil {
		c.channelID = state.vc.ChannelID
	}
	return c
}

func discordIDLess(a, b string) bool {
	a, b = strings.TrimLeft(a, "0"), strings.TrimLeft(b, "0")
	if len(a) != len(b) {
		return len(a) < len(b)
	}
	return a < b
}

func (c *streamingController) join(id string) {
	if c == nil || id == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.present[id] {
		c.present[id] = true
		c.order = append(c.order, id)
		log.Printf("participant joined session=%d user=%s position=%d", c.state.sessionID, id, len(c.order))
	}
}
func (c *streamingController) leave(id string) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.present, id)
	delete(c.grants, id)
	for i, existing := range c.order {
		if existing == id {
			c.order = append(c.order[:i], c.order[i+1:]...)
			break
		}
	}
}
func (c *streamingController) reset(ids []string, channelIDs ...string) {
	sort.Slice(ids, func(i, j int) bool { return discordIDLess(ids[i], ids[j]) })
	c.mu.Lock()
	if len(channelIDs) > 0 {
		c.channelID = channelIDs[0]
	}
	c.order = nil
	c.present = map[string]bool{}
	c.grants = map[string]streamGrant{}
	for _, id := range ids {
		if !c.present[id] {
			c.present[id] = true
			c.order = append(c.order, id)
		}
	}
	c.mu.Unlock()
}
func (c *streamingController) currentChannel() string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.channelID
}
func (c *streamingController) roster() []string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	ids := []string{}
	if c.stopped {
		return ids
	}
	for _, id := range c.order {
		if !isUserCapturePaused(id) {
			ids = append(ids, id)
		}
	}
	return ids
}
func (c *streamingController) grant(id string) streamGrant {
	if c == nil {
		return streamGrant{}
	}
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.grants[id]
}
func (c *streamingController) participantPresent(id string) bool {
	if c == nil {
		return true
	}
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.present[id]
}
func (c *streamingController) suspended() bool {
	if c == nil {
		return false
	}
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.exhausted
}
func (c *streamingController) sync(ctx context.Context) (*streamingStatus, error) {
	c.syncMu.Lock()
	defer c.syncMu.Unlock()
	var response streamingStatus
	state := c.state
	if state.transcriptionClient == nil || state.sessionID <= 0 {
		return &response, nil
	}
	if err := state.transcriptionClient.postJSON(ctx, fmt.Sprintf("/v1/sessions/%d/streaming", state.sessionID), map[string]any{"users": c.roster()}, &response); err != nil {
		return nil, err
	}
	c.mu.Lock()
	for id, grant := range response.Assignments {
		if c.grants[id].Token != grant.Token {
			log.Printf("streaming promotion session=%d user=%s key=%s", state.sessionID, id, grant.KeyName)
		}
	}
	if response.Queued != c.queued {
		log.Printf("streaming queue session=%d count=%d", state.sessionID, response.Queued)
	}
	c.queued = response.Queued
	c.grants = response.Assignments
	c.exhausted = response.Exhausted
	c.mu.Unlock()
	return &response, nil
}
func (c *streamingController) run() {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, err := c.sync(ctx)
		cancel()
		if err != nil {
			log.Printf("streaming control unavailable session=%d", c.state.sessionID)
		}

		select {
		case <-c.state.recordingDone:
			c.mu.Lock()
			c.stopped = true
			c.grants = nil
			c.mu.Unlock()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			_, _ = c.sync(ctx)
			cancel()
			return
		case <-ticker.C:
		}
	}
}

func isHumanVoiceMember(s *discordgo.Session, guildID, id string, member *discordgo.Member) bool {
	if member == nil && s != nil && s.State != nil {
		member, _ = s.State.Member(guildID, id)
	}
	return member == nil || member.User == nil || !member.User.Bot
}

func voiceSnapshot(s *discordgo.Session, guildID, channelID, botID string) []string {
	ids := []string{}
	if s == nil || s.State == nil {
		return ids
	}
	guild, err := s.State.Guild(guildID)
	if err != nil {
		return ids
	}
	for _, vs := range guild.VoiceStates {
		if vs == nil || vs.ChannelID != channelID || vs.UserID == botID {
			continue
		}
		member, _ := s.State.Member(guildID, vs.UserID)
		if member != nil && member.User != nil && member.User.Bot {
			continue
		}
		ids = append(ids, vs.UserID)
	}
	return ids
}

// One bounded worker per WAV/epoch. The capture loop only copies PCM and enqueues.
type realtimeAudioClient struct {
	queue     chan []byte
	done      chan struct{}
	finish    chan struct{}
	abort     chan struct{}
	once      sync.Once
	abortOnce sync.Once
}

func newRealtimeAudioClient(client *TranscriptionClient, request TranscriptionRequest, grant streamGrant) *realtimeAudioClient {
	r := &realtimeAudioClient{queue: make(chan []byte, 100), done: make(chan struct{}), finish: make(chan struct{}), abort: make(chan struct{})}
	go r.run(client, request, grant)
	return r
}
func (r *realtimeAudioClient) enqueue(pcm []int16) {
	if r == nil || len(pcm) == 0 {
		return
	}
	select {
	case <-r.done:
		return
	case <-r.abort:
		return
	default:
	}
	data := make([]byte, len(pcm)/channels*2)
	for i := 0; i+1 < len(pcm); i += 2 {
		sample := (int32(pcm[i]) + int32(pcm[i+1])) / 2
		binary.LittleEndian.PutUint16(data[i:], uint16(int16(sample)))
	}
	select {
	case r.queue <- data:
	default:
		r.abortOnce.Do(func() { close(r.abort) })
		log.Printf("Realtime queue full; WAV retained for Batch")
	}
}
func (r *realtimeAudioClient) silence(frames int) {
	for frames > 0 {
		size := min(frames, sampleRate)
		r.enqueue(make([]int16, size*channels))
		frames -= size
	}
}
func (r *realtimeAudioClient) close() {
	if r != nil {
		r.once.Do(func() { close(r.finish) })
	}
}
func (r *realtimeAudioClient) wait(ctx context.Context) {
	if r == nil {
		return
	}
	select {
	case <-r.done:
	case <-ctx.Done():
		r.abortOnce.Do(func() { close(r.abort) })
	}
}
func (r *realtimeAudioClient) run(client *TranscriptionClient, request TranscriptionRequest, grant streamGrant) {
	defer close(r.done)
	address, err := url.Parse(client.baseURL + "/v1/streaming/audio")
	if err != nil {
		return
	}
	if address.Scheme == "https" {
		address.Scheme = "wss"
	} else {
		address.Scheme = "ws"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	ws, _, err := dialer.DialContext(ctx, address.String(), nil)
	if err != nil {
		log.Printf("Realtime transport failed user=%s", request.DiscordID)
		return
	}
	defer ws.Close()
	ws.SetReadLimit(1 << 20)
	_ = ws.SetWriteDeadline(time.Now().Add(10 * time.Second))
	meta := map[string]any{"session_id": request.SessionID, "discord_id": request.DiscordID,
		"username": request.Username, "display_name": request.DisplayName, "channel_name": request.ChannelName,
		"recording_filename": filepath.Base(request.AudioPath), "recording_started_at": request.RecordingStartedAt.UTC().Format(time.RFC3339Nano), "token": grant.Token}
	if ws.WriteJSON(meta) != nil {
		return
	}
	_ = ws.SetReadDeadline(time.Now().Add(15 * time.Second))
	var status map[string]any
	if ws.ReadJSON(&status) != nil || status["type"] != "ready" {
		return
	}
	_ = ws.SetReadDeadline(time.Time{})
	readDone := make(chan struct{})
	go func() {
		defer close(readDone)
		for {
			if ws.ReadJSON(&status) != nil || status["type"] == "completed" || status["type"] == "fallback" {
				return
			}
		}
	}()
	var sequence uint64
	send := func(data []byte) error {
		sequence++
		packet := make([]byte, 8+len(data))
		binary.LittleEndian.PutUint64(packet, sequence)
		copy(packet[8:], data)
		_ = ws.SetWriteDeadline(time.Now().Add(5 * time.Second))
		return ws.WriteMessage(websocket.BinaryMessage, packet)
	}
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-readDone:
			return
		case <-r.abort:
			return
		case data := <-r.queue:
			if send(data) != nil {
				return
			}
		case <-ticker.C:
			if ws.WriteControl(websocket.PingMessage, nil, time.Now().Add(time.Second)) != nil {
				return
			}
		case <-r.finish:
			for {
				select {
				case data := <-r.queue:
					if send(data) != nil {
						return
					}
				default:
					goto drained
				}
			}
		drained:
			_ = ws.SetWriteDeadline(time.Now().Add(5 * time.Second))
			if ws.WriteJSON(map[string]any{"type": "end", "last_seq_no": sequence}) != nil {
				return
			}
			select {
			case <-readDone:
			case <-r.abort:
			case <-time.After(25 * time.Second):
			}
			return
		}
	}
}

func streamingHook(s *discordgo.Session, i *discordgo.InteractionCreate) {
	if i.GuildID == "" {
		respondText(s, i, "Use this command in a server.")
		return
	}
	mode := "status"
	for _, option := range i.ApplicationCommandData().Options {
		if option.Name == "mode" {
			mode = option.StringValue()
		}
	}
	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var result streamingStatus
	path := "/v1/guilds/" + url.PathEscape(i.GuildID) + "/streaming"
	var err error
	if mode == "status" {
		err = client.getJSON(ctx, path, &result)
	} else {
		err = client.postJSON(ctx, path, map[string]string{"mode": mode}, &result)
	}
	if err == nil {
		if state := getVoiceConnection(i.GuildID); state != nil && state.streaming != nil {
			if updated, syncErr := state.streaming.sync(ctx); syncErr == nil {
				result = *updated
			}
		}
	}
	if err != nil {
		respondText(s, i, fmt.Sprintf("Streaming: %v", err))
		return
	}
	respondText(s, i, fmt.Sprintf("Streaming: %t · %d/%d · FIFO: %d", result.Enabled && !result.Exhausted, result.Occupied, result.Capacity, result.Queued))
}

type creditNotice struct {
	SessionID      int64  `json:"session_id"`
	GuildID        string `json:"guild_id"`
	ChannelID      string `json:"channel_id"`
	Episode        int    `json:"episode"`
	CleanupPending bool   `json:"cleanup_pending"`
}

// One worker also recovers unsent notices belonging to calls from an earlier bot process.
func runCreditNotices(ctx context.Context, s *discordgo.Session, client *TranscriptionClient) {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	sent := map[string]bool{}
	retryAt := map[string]time.Time{}
	for {
		var notices []creditNotice
		requestCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		err := client.getJSON(requestCtx, "/v1/credits/notices", &notices)
		cancel()
		if err == nil {
			for _, notice := range notices {
				identity := fmt.Sprintf("%d:%d", notice.SessionID, notice.Episode)
				if time.Now().Before(retryAt[identity]) {
					continue
				}
				if state := getVoiceConnection(notice.GuildID); state != nil && state.sessionID == notice.SessionID && state.streaming != nil {
					state.streaming.mu.Lock()
					state.streaming.exhausted = true
					state.streaming.grants = nil
					state.streaming.mu.Unlock()
					ack := make(chan error, 1)
					select {
					case state.recordingEvents <- recordingControlEvent{finishAll: true, ack: ack}:
						select {
						case <-ack:
						case <-state.recordingDone:
						case <-ctx.Done():
							return
						}
					case <-state.recordingDone:
					case <-ctx.Done():
						return
					}
				}
				if !sent[identity] {
					message := botText("Os créditos da Speechmatics esgotaram. A transcrição desta chamada foi interrompida e o áudio pendente foi eliminado. O texto já transcrito foi preservado.", "Speechmatics credits ran out. Transcription stopped and pending audio was deleted. Previously transcribed text was preserved.")
					if notice.CleanupPending {
						message = botText("Os créditos da Speechmatics esgotaram. A transcrição foi interrompida; o áudio pendente será eliminado. O texto já transcrito foi preservado.", "Speechmatics credits ran out. Transcription stopped; pending audio will be deleted. Previously transcribed text was preserved.")
					}
					digest := sha256.Sum256([]byte(identity))
					nonce := fmt.Sprintf("%x", digest)[:24]
					endpoint := discordgo.EndpointChannelMessages(notice.ChannelID)
					// This discordgo version lacks nonce fields, so use its existing REST transport.
					_, err = s.RequestWithBucketID("POST", endpoint, map[string]any{"content": message, "allowed_mentions": noMentions(), "nonce": nonce, "enforce_nonce": true}, endpoint)
					if err != nil {
						log.Printf("credits notice failed session=%d; retrying later", notice.SessionID)
						retryAt[identity] = time.Now().Add(time.Minute)
						continue
					}
					sent[identity] = true
				}
				var response map[string]any
				requestCtx, cancel = context.WithTimeout(ctx, 5*time.Second)
				_ = client.postJSON(requestCtx, fmt.Sprintf("/v1/sessions/%d/credits/notice", notice.SessionID), map[string]any{"episode": notice.Episode}, &response)
				cancel()
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// Repair our fixed-format WAV header only during startup replay, when no writer is alive.
func repairInterruptedWAV(path string) error {
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	header := make([]byte, 44)
	if _, err = f.ReadAt(header, 0); err != nil {
		return err
	}
	if string(header[:4]) != "RIFF" || string(header[8:12]) != "WAVE" || string(header[36:40]) != "data" {
		return fmt.Errorf("invalid WAV header")
	}
	stat, err := f.Stat()
	if err != nil {
		return err
	}
	bytes := stat.Size() - 44
	if bytes < 0 || bytes > int64(^uint32(0))-36 || bytes%4 != 0 {
		return fmt.Errorf("invalid WAV size")
	}
	binary.LittleEndian.PutUint32(header[4:8], uint32(bytes)+36)
	binary.LittleEndian.PutUint32(header[40:44], uint32(bytes))
	if _, err := f.WriteAt(header, 0); err != nil {
		return err
	}
	return f.Sync()
}
