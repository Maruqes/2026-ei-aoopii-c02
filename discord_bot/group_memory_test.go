package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestMemeGIFSearchUsesContextAndAvoidsRecentResults(t *testing.T) {
	t.Cleanup(func() {
		recentReactionGIFs.Lock()
		defer recentReactionGIFs.Unlock()
		delete(recentReactionGIFs.byGuild, t.Name())
		delete(recentReactionGIFs.byGuild, t.Name()+"-other-guild")
	})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("q") != "juggling too many tasks" || r.URL.Query().Get("api_key") != "test-key" || r.URL.Query().Get("limit") != "12" || r.URL.Query().Get("rating") != "pg-13" {
			t.Errorf("incorrect contextual GIF search")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": []map[string]string{
			{"url": "https://evil.example/meme"},
			{"url": "http://giphy.com/gifs/insecure"},
			{"url": "https://user@giphy.com/gifs/credentials"},
			{"url": "https://giphy.com.evil.example/gifs/spoof"},
			{"url": "https://giphy.com/gifs/juggling"},
			{"url": "https://giphy.com/gifs/spinning-plates"},
		}})
	}))
	defer server.Close()
	seen := map[string]bool{}
	for range 2 {
		gif, err := searchMemeGIF(context.Background(), server.Client(), server.URL, "test-key", " juggling too many tasks ", t.Name())
		if err != nil || (gif != "https://giphy.com/gifs/juggling" && gif != "https://giphy.com/gifs/spinning-plates") || seen[gif] {
			t.Fatalf("repeated or invalid gif=%q err=%v", gif, err)
		}
		seen[gif] = true
	}
	gif, err := searchMemeGIF(context.Background(), server.Client(), server.URL, "test-key", "juggling too many tasks", t.Name())
	if err != nil || gif != "" {
		t.Fatalf("exhausted results should omit GIF: gif=%q err=%v", gif, err)
	}
	gif, err = searchMemeGIF(context.Background(), server.Client(), server.URL, "test-key", "juggling too many tasks", t.Name()+"-other-guild")
	if err != nil || !seen[gif] {
		t.Fatalf("another guild should have independent history: gif=%q err=%v", gif, err)
	}
}

func TestProactiveReactionWaitsForQuestionMusicAndSpeech(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.voiceEnabled = true
	h.a.state.streaming.synced = true
	h.a.mu.Lock()
	if !h.a.proactiveReady(h.now, true) {
		h.a.mu.Unlock()
		t.Fatal("quiet monitored call should be ready")
	}
	h.a.request = &assistantRequest{}
	if h.a.proactiveReady(h.now, false) {
		h.a.mu.Unlock()
		t.Fatal("reaction interrupted a question")
	}
	h.a.request = nil
	h.a.mu.Unlock()
	h.final("ana", "speech", "Falamos de Go", 0, 2)
	h.a.mu.Lock()
	if h.a.proactiveReady(h.now.Add(3*time.Second), true) {
		h.a.mu.Unlock()
		t.Fatal("reaction ignored live speech")
	}
	if !h.a.proactiveReady(h.now.Add(10*time.Second), true) {
		h.a.mu.Unlock()
		t.Fatal("reaction did not wait for silence")
	}
	h.a.mu.Unlock()
	h.a.state.music = &MusicPlayer{queue: []MusicTrack{{Title: "music"}}}
	h.a.mu.Lock()
	defer h.a.mu.Unlock()
	if h.a.proactiveReady(h.now.Add(10*time.Second), true) {
		t.Fatal("reaction spoke over music")
	}
}

func TestHumanSpeechCancelsProactiveVoice(t *testing.T) {
	h := newAssistantHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h.a.proactiveCancel = cancel
	h.final("ana", "new", "Olá macaco explica Go", 0, 2)
	if ctx.Err() == nil {
		t.Fatal("human interruption did not cancel proactive voice")
	}
}

func TestProactiveVoiceUsesCurrentCallSilenceWithoutWaitingForFinals(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.voiceEnabled = true
	h.a.state.streaming.synced = true
	// PCM can detect noise or trailing speech that the provider never transcribes.
	h.audio["ana"].frames = 2 * sampleRate
	h.audio["ana"].speechFrame = 2 * sampleRate
	h.audio["ana"].lastSpeech = h.now.Add(2 * time.Second)
	h.a.mu.Lock()
	defer h.a.mu.Unlock()
	if h.a.proactiveReady(h.now.Add(3*time.Second), true) {
		t.Fatal("spoke before the silence interval")
	}
	if !h.a.proactiveReady(h.now.Add(10*time.Second), true) {
		t.Fatal("quiet call was blocked by a missing final")
	}
	// Departure cancels conversations immediately and removes the current stream.
	retired := h.a.streams["bob"]
	h.a.mu.Unlock()
	h.a.state.streaming.leave("bob")
	h.a.mu.Lock()
	retired.failed = true
	if !h.a.proactiveReady(h.now.Add(10*time.Second), true) {
		t.Fatal("departed participant blocked voice")
	}
}

func TestEmptyFinalDoesNotInterruptProactiveVoice(t *testing.T) {
	h := newAssistantHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h.a.proactiveCancel = cancel
	h.audio["ana"].frames = 2 * sampleRate
	h.a.event("ana", h.audio["ana"], realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "flush", End: 2}, h.now)
	if ctx.Err() != nil {
		t.Fatal("empty provider flush interrupted voice")
	}
}

func TestDelayedTranscriptDoesNotCutCurrentProactiveVoice(t *testing.T) {
	h := newAssistantHarness(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h.a.proactiveCancel = cancel
	h.a.proactiveStartedAt = h.now.Add(10 * time.Second)
	h.final("ana", "delayed", "Falámos de Go", 0, 2)
	if ctx.Err() != nil {
		t.Fatal("old transcript interrupted current voice")
	}
	h.final("ana", "new", "Agora estou a falar", 10, 12)
	if ctx.Err() == nil {
		t.Fatal("new human speech did not interrupt voice")
	}
}

func TestProactiveReactionClaimsOnceBeforePublishing(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.assistant = h.a
	h.a.state.streaming.synced = true
	h.a.voiceEnabled = true
	var spoken int
	h.a.speak = func(ctx context.Context, text string, ready func() error) error {
		if ready != nil {
			if err := ready(); err != nil {
				return err
			}
		}
		spoken++
		deadline, ok := ctx.Deadline()
		if text != "Esse plano está no modo this is fine." || !ok || time.Until(deadline) < time.Minute {
			t.Fatal("voice lost reaction text or sufficient synthesis/playback time")
		}
		return nil
	}
	setVoiceConnection("proactive-test", h.a.state)
	t.Cleanup(func() { voiceMu.Lock(); delete(voiceConnections, "proactive-test"); voiceMu.Unlock() })
	var claims, results atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/claim") {
			if claims.Add(1) > 1 {
				w.WriteHeader(http.StatusConflict)
				return
			}
			_, _ = w.Write([]byte(`{"status":"claimed"}`))
		} else if strings.HasSuffix(r.URL.Path, "/result") {
			results.Add(1)
			_, _ = w.Write([]byte(`{"status":"sent"}`))
		}
	}))
	defer server.Close()
	reaction := groupReaction{ID: 1, GuildID: "proactive-test", ChannelID: "chat", Text: "Esse plano está no modo this is fine.", Speak: true}
	client := testAPIClient(server)
	deliverGroupReaction(context.Background(), nil, client, reaction)
	deliverGroupReaction(context.Background(), nil, client, reaction)
	if claims.Load() != 2 || results.Load() != 1 || len(h.messages) != 1 || spoken != 1 {
		t.Fatalf("claims=%d results=%d messages=%v spoken=%d", claims.Load(), results.Load(), h.messages, spoken)
	}
}
