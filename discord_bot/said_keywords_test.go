package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
)

func TestSaidKeywordsMatchWholeWordsPerSpeakerAndRecording(t *testing.T) {
	saved := saidKeywordTriggers
	saidKeywordTriggers = nil
	t.Cleanup(func() { saidKeywordTriggers = saved })
	var pijama, audio, attempts int
	triggerSaidKeyworkd(func(event SaidKeywordContext) error {
		pijama++
		return nil
	}, "pijama")
	triggerSaidKeyworkd(func(event SaidKeywordContext) error {
		attempts++
		if attempts == 1 {
			return errors.New("temporary failure")
		}
		audio++
		return nil
	}, []string{"BOT", "audio"}...)
	fired := map[string]bool{}
	dispatch := func(messages ...saidVoiceMessage) {
		dispatchSaidKeywords(SaidKeywordContext{}, messages, saidKeywordTriggers, fired)
	}
	dispatch(saidVoiceMessage{1, 1, "alice", "pijamas robot audio"})
	dispatch(saidVoiceMessage{2, 2, "alice", "bot"}, saidVoiceMessage{3, 3, "alice", "audio"})
	dispatch(saidVoiceMessage{4, 4, "alice", "bot"}, saidVoiceMessage{5, 4, "bob", "audio"})
	if pijama != 0 || attempts != 0 {
		t.Fatal("matched a substring, different recordings, or different speakers")
	}
	messages := []saidVoiceMessage{{6, 5, "alice", "PIJAMA! pijama"}, {7, 5, "alice", "audio, BOT."}}
	dispatch(messages...)
	dispatch(messages...) // Retry failed audio callback, never repeat successful pijama.
	dispatch(messages...)
	// Batch replaces Realtime message IDs but retains the recording ID.
	dispatch(saidVoiceMessage{8, 5, "alice", "bot audio pijama"})
	dispatch(saidVoiceMessage{9, 6, "alice", "pijama"})
	if pijama != 2 || audio != 1 || attempts != 2 {
		t.Fatalf("pijama=%d audio=%d attempts=%d", pijama, audio, attempts)
	}
}

func TestSaidKeywordWorkerPostsPijamaAndReadsFinalBatch(t *testing.T) {
	saved := saidKeywordTriggers
	saidKeywordTriggers = nil
	t.Cleanup(func() { saidKeywordTriggers = saved })
	triggerSaidKeyworkd(sayPijama, "pijama")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	requests := 0
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sessions/42/voice-messages" {
			t.Errorf("unexpected API path %s", r.URL.Path)
		}
		requests++
		if requests == 1 {
			_ = json.NewEncoder(w).Encode([]saidVoiceMessage{})
			cancel() // Final Batch result arrives as capture/summary finishes.
			return
		}
		_ = json.NewEncoder(w).Encode([]saidVoiceMessage{{1, 1, "alice", "Pijama!"}})
	}))
	defer api.Close()
	s, err := discordgo.New("Bot test-token")
	if err != nil {
		t.Fatal(err)
	}
	posts := 0
	s.Client = &http.Client{Transport: testRoundTripper(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost || !strings.HasSuffix(r.URL.Path, "/channels/main/messages") {
			t.Errorf("unexpected Discord request %s %s", r.Method, r.URL.Path)
		}
		var body discordgo.MessageSend
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Content != "pijama" || body.AllowedMentions == nil || len(body.AllowedMentions.Parse) != 0 {
			t.Errorf("unexpected message %#v", body)
		}
		posts++
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"id":"sent","content":"pijama"}`))}, nil
	})}
	state := &voiceConnectionState{sessionID: 42, summaryChannelID: "main", transcriptionClient: &TranscriptionClient{baseURL: api.URL, httpClient: api.Client()}}
	done := make(chan struct{})
	go func() {
		defer close(done)
		runSaidKeywordTriggers(ctx, s, "guild", state)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("trigger worker did not stop")
	}
	if requests != 2 || posts != 1 {
		t.Fatalf("requests=%d posts=%d", requests, posts)
	}
}
