package main

import (
	"context"
	"encoding/binary"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
	"github.com/gorilla/websocket"
	"gopkg.in/hraban/opus.v2"
)

func TestStreamingArrivalOrderAndReentry(t *testing.T) {
	c := newStreamingController(&voiceConnectionState{sessionID: 1})
	c.reset([]string{"10000000000000000000", "9", "10"})
	c.join("10") // mute/SSRC updates do not rejoin
	c.join("2")
	c.leave("9")
	c.join("9")
	got := c.roster()
	want := []string{"10", "10000000000000000000", "2", "9"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order %v", got)
		}
	}
}

func TestRealtimeCopiesStereoWithoutOverflowAndFlushes(t *testing.T) {
	var samples []int16
	var lock sync.Mutex
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		ws, err := upgrader.Upgrade(w, req, nil)
		if err != nil {
			return
		}
		defer ws.Close()
		var meta map[string]any
		if ws.ReadJSON(&meta) != nil {
			return
		}
		_ = ws.WriteJSON(map[string]any{"type": "ready"})
		_, data, err := ws.ReadMessage()
		if err != nil {
			return
		}
		if binary.LittleEndian.Uint64(data[:8]) != 1 {
			t.Error("sequence")
		}
		lock.Lock()
		for i := 8; i < len(data); i += 2 {
			samples = append(samples, int16(binary.LittleEndian.Uint16(data[i:])))
		}
		lock.Unlock()
		var end map[string]any
		if ws.ReadJSON(&end) != nil {
			return
		}
		if end["last_seq_no"] != float64(1) {
			t.Error("end boundary")
		}
		_ = ws.WriteJSON(map[string]any{"type": "completed"})
	}))
	defer server.Close()
	client := &TranscriptionClient{baseURL: server.URL}
	rt := newRealtimeAudioClient(client, TranscriptionRequest{SessionID: 1, DiscordID: "123", AudioPath: "unit.wav", RecordingStartedAt: time.Now()}, streamGrant{Token: "token"})
	pcm := []int16{32767, 32767, -32768, -32768, 32767, -32768}
	rt.enqueue(pcm)
	for i := range pcm {
		pcm[i] = 0
	} // Opus decoder buffer reuse must be safe.
	rt.close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	rt.wait(ctx)
	lock.Lock()
	defer lock.Unlock()
	if len(samples) != 3 || samples[0] != 32767 || samples[1] != -32768 || samples[2] != 0 {
		t.Fatalf("samples %v", samples)
	}
}

func TestRealtimeQueueOverflowDoesNotBlockCapture(t *testing.T) {
	r := &realtimeAudioClient{queue: make(chan []byte, 1), done: make(chan struct{}), abort: make(chan struct{})}
	r.enqueue([]int16{1, 1})
	r.enqueue([]int16{2, 2})
	select {
	case <-r.abort:
	default:
		t.Fatal("overflow must abort streaming")
	}
	if len(r.queue) != 1 {
		t.Fatal("unbounded queue")
	}
}

func TestInterruptedSafetyWAVRepair(t *testing.T) {
	path := filepath.Join(t.TempDir(), "unit.wav")
	w, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}
	if err := w.WritePCM([]int16{1, 1, 2, 2}); err != nil {
		t.Fatal(err)
	}
	_ = w.f.Close() // crash before normal WAV.Close
	if err := repairInterruptedWAV(path); err != nil {
		t.Fatal(err)
	}
	data := make([]byte, 44)
	// Check with the file header rather than mirroring the repair calculation.
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	_, _ = f.Read(data)
	if binary.LittleEndian.Uint32(data[40:44]) != 8 {
		t.Fatal("missing recovered audio")
	}
}

func TestCaptureModeBoundariesKeepEachPacketInOneWAV(t *testing.T) {
	directory := t.TempDir()
	t.Setenv("RECORDINGS_DIR", directory)
	ready := make(chan struct{}, 1)
	streamed := make(chan int, 1)
	admissions := make(chan int, 3)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path == "/v1/streaming/audio" {
			ws, err := upgrader.Upgrade(w, req, nil)
			if err != nil {
				return
			}
			defer ws.Close()
			var meta map[string]any
			if ws.ReadJSON(&meta) != nil {
				return
			}
			_ = ws.WriteJSON(map[string]any{"type": "ready"})
			ready <- struct{}{}
			frames := 0
			for {
				kind, packet, err := ws.ReadMessage()
				if err != nil {
					return
				}
				if kind == websocket.BinaryMessage {
					frames += (len(packet) - 8) / 2
				} else {
					break
				}
			}
			streamed <- frames
			_ = ws.WriteJSON(map[string]any{"type": "completed"})
			return
		}
		_ = req.ParseForm()
		path := filepath.Join(directory, req.FormValue("recording_filename"))
		data, err := os.ReadFile(path)
		if err != nil {
			t.Error(err)
			return
		}
		admissions <- int(binary.LittleEndian.Uint32(data[40:44])) / 4
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	client := testAPIClient(server)
	packets := make(chan *discordgo.Packet)
	events := make(chan recordingControlEvent)
	vc := &discordgo.VoiceConnection{OpusRecv: packets, UserID: "bot", ChannelID: "voice"}
	state := &voiceConnectionState{vc: vc, sessionID: 71}
	state.streaming = newStreamingController(state)
	state.streaming.join("123")
	setVoiceConnection("stream-test", state)
	defer clearVoiceConnection("stream-test", vc)
	users := NewSSRCUserMap()
	users.Set(10, "123")
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppVoIP)
	if err != nil {
		t.Fatal(err)
	}
	encoded := make([]byte, 4000)
	size, err := encoder.Encode(make([]int16, defaultOpusFrameSamples*channels), encoded)
	if err != nil {
		t.Fatal(err)
	}
	result := make(chan error, 1)
	go func() { result <- ListenAndWriteOpusToWAV(vc, directory, 71, users, events, client, nil, nil) }()
	send := func(sequence uint16) {
		packets <- &discordgo.Packet{SSRC: 10, Sequence: sequence, Timestamp: uint32(sequence) * 960, Opus: encoded[:size]}
	}
	// The capture loop has written the preceding packet before handling this ordered barrier.
	barrier := func() {
		ack := make(chan error, 1)
		events <- recordingControlEvent{user: voiceUserInfo{DiscordID: "barrier"}, ack: ack}
		if err := <-ack; err != nil {
			t.Fatal(err)
		}
	}
	send(1)
	barrier()
	state.streaming.mu.Lock()
	state.streaming.grants["123"] = streamGrant{Token: "reservation"}
	state.streaming.mu.Unlock()
	send(2)
	select {
	case <-ready:
	case <-time.After(5 * time.Second):
		t.Fatal("Realtime did not open")
	}
	barrier()
	state.streaming.mu.Lock()
	state.streaming.grants = nil
	state.streaming.mu.Unlock()
	send(3)
	barrier()
	close(packets)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	if err := client.waitForSubmissions(testContext(t), 71); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		select {
		case frames := <-admissions:
			if frames != 960 {
				t.Fatalf("overlapping or missing WAV: %d frames", frames)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("missing WAV")
		}
	}
	if frames := <-streamed; frames != 960 {
		t.Fatalf("stream resent Batch audio: %d", frames)
	}
}

func TestRetrySkipsLiveStreamingSafetyOutbox(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	var submissions int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/transcriptions" {
			submissions++
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"finished"}`))
	}))
	defer server.Close()
	client := testAPIClient(server)
	request := TranscriptionRequest{SessionID: 99, DiscordID: "123", AudioPath: filepath.Join(recordingsDirFromEnv(), "live.wav"), SafetyWAV: true}
	if err := persistTranscription(request); err != nil {
		t.Fatal(err)
	}
	client.liveAudio = map[string]bool{transcriptionOutboxPath(request): true}
	if err := client.RetrySession(testContext(t), "guild", 99); err != nil {
		t.Fatal(err)
	}
	if submissions != 0 {
		t.Fatal("retry submitted open safety WAV")
	}
}

func TestKnownEntriesDuringJoinKeepEventOrderAfterUnknownSnapshot(t *testing.T) {
	guild := "arrival-test"
	for _, id := range []string{"99", "8"} {
		observePendingVoiceArrival(&discordgo.VoiceStateUpdate{VoiceState: &discordgo.VoiceState{GuildID: guild, ChannelID: "voice", UserID: id}})
	}
	// A mute update must not append another entry.
	observePendingVoiceArrival(&discordgo.VoiceStateUpdate{VoiceState: &discordgo.VoiceState{GuildID: guild, ChannelID: "voice", UserID: "99"}, BeforeUpdate: &discordgo.VoiceState{ChannelID: "voice"}})
	c := newStreamingController(&voiceConnectionState{sessionID: 1})
	initializeVoiceArrivalOrder(c, guild, "voice", []string{"99", "100", "8", "10"})
	want := []string{"10", "100", "99", "8"}
	got := c.roster()
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order %v", got)
		}
	}
}

func TestRealtimeAssistantFinalDeliveryAndFallback(t *testing.T) {
	h := newAssistantHarness(t)
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseProvider := func() { releaseOnce.Do(func() { close(release) }) }
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		ws, err := upgrader.Upgrade(w, request, nil)
		if err != nil {
			return
		}
		defer ws.Close()
		var meta map[string]any
		if ws.ReadJSON(&meta) != nil {
			return
		}
		_ = ws.WriteJSON(realtimeEvent{Type: "ready", RecordingID: 10, Generation: 2})
		if _, _, err = ws.ReadMessage(); err != nil {
			return
		}
		final := realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "one", Start: 0, End: 1, Text: "Hey Bot Pergunta?", Words: []assistantWord{{Text: "Hey", Start: 0, End: 0.2}, {Text: "Bot", Start: 0.2, End: 0.4}, {Text: "Pergunta?", Start: 0.4, End: 1}}}
		_ = ws.WriteJSON(final)
		_ = ws.WriteJSON(final)
		<-release
		_ = ws.WriteJSON(realtimeEvent{Type: "fallback"})
	}))
	defer server.Close()
	defer releaseProvider()
	rt := newRealtimeAudioClient(testAPIClient(server), TranscriptionRequest{SessionID: 1, DiscordID: "ana", AudioPath: "unit.wav", RecordingStartedAt: h.now}, streamGrant{Token: "ana"}, h.a)
	pcm := make([]int16, sampleRate*channels)
	for i := range pcm {
		pcm[i] = 1000
	}
	rt.enqueue(pcm)
	h.wait(t, func() bool { h.a.mu.Lock(); defer h.a.mu.Unlock(); return h.a.request != nil })
	h.a.mu.Lock()
	text := h.a.request.text
	h.a.mu.Unlock()
	releaseProvider()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	rt.wait(ctx)
	if text != "Pergunta?" || !h.waiting() {
		t.Fatalf("final/fallback handling: text=%q waiting=%t", text, h.waiting())
	}
}

func TestRealtimeDTXSilenceUsesSameWAVClockAndKeepsSpeechBoundary(t *testing.T) {
	writer, err := NewWAVWriter(filepath.Join(t.TempDir(), "silence.wav"), sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	rt := &realtimeAudioClient{queue: make(chan []byte, 10), done: make(chan struct{}), abort: make(chan struct{})}
	recording := &userAudioRecording{wav: writer, realtime: rt, lastPacketAt: time.Now()}
	pcm := make([]int16, defaultOpusFrameSamples*channels)
	for i := range pcm {
		pcm[i] = 1000
	}
	if err = recording.writeRTPPacket(10, 1, 0, 0, nil, pcm, defaultOpusFrameSamples); err != nil {
		t.Fatal(err)
	}
	if err = recording.padRealtimeSilence(recording.lastPacketAt.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	frames, speech, _ := rt.progress()
	if frames != 1 || speech != 0.02 || writer.FramesWritten() != sampleRate {
		t.Fatalf("audio clocks: frames=%f speech=%f WAV=%d", frames, speech, writer.FramesWritten())
	}
	if err = recording.padRealtimeSilence(recording.lastPacketAt.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if writer.FramesWritten() != sampleRate {
		t.Fatal("duplicated silence")
	}
	if err = recording.padRealtimeSilence(recording.lastPacketAt.Add(2 * time.Second)); err != nil {
		t.Fatal(err)
	}
	if writer.FramesWritten() != 2*sampleRate {
		t.Fatal("missing continuous DTX")
	}
}
