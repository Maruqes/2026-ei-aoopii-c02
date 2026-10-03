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

func TestMemeGIFSearchUsesProviderResultsAndMemeQuery(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("q") != "this is fine dog meme" || r.URL.Query().Get("api_key") != "test-key" {
			t.Errorf("incorrect meme search")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": []map[string]string{{"url": "https://evil.example/meme"}, {"url": "https://giphy.com/gifs/this-is-fine"}}})
	}))
	defer server.Close()
	gif, err := searchMemeGIF(context.Background(), server.Client(), server.URL, "test-key", "this is fine dog")
	if err != nil || gif != "https://giphy.com/gifs/this-is-fine" {
		t.Fatalf("gif=%q err=%v", gif, err)
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

func TestProactiveReactionClaimsOnceBeforePublishing(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.assistant = h.a
	h.a.state.streaming.synced = true
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
	reaction := groupReaction{ID: 1, GuildID: "proactive-test", ChannelID: "chat", Text: "Esse plano está no modo this is fine."}
	client := testAPIClient(server)
	deliverGroupReaction(context.Background(), nil, client, reaction)
	deliverGroupReaction(context.Background(), nil, client, reaction)
	if claims.Load() != 2 || results.Load() != 1 || len(h.messages) != 1 {
		t.Fatalf("claims=%d results=%d messages=%v", claims.Load(), results.Load(), h.messages)
	}
}
