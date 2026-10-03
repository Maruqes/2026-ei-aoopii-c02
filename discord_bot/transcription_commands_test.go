package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestSTTChoicesAndPermissions(t *testing.T) {
	for _, lang := range []botLanguage{botLanguagePT, botLanguageEN} {
		command := sttCommand(lang)
		if command.Name != "stt" || len(command.Options) != 2 || !requiresManageServer("stt") {
			t.Fatal("STT must expose status/order with Manage Server protection")
		}
		choices := command.Options[1].Options[0].Choices
		if len(choices) != 4 || choices[0].Value != "deepgram,speechmatics" || choices[1].Value != "speechmatics,deepgram" {
			t.Fatal("missing bidirectional provider choices")
		}
	}
}

func TestCombinedKeysUnknownBalanceAndDiscordPagination(t *testing.T) {
	var result transcriptionKeys
	payload := `{"order":["deepgram","speechmatics"],"providers":[{"provider":"deepgram","configured":true,"groups":[{"name":"shared","streaming_limit":150,"batch_limit":50,"balance_usd":null}],"keys":[{"name":"DEEPGRAM_API_KEY_01","group":"shared","streaming_state":"healthy","batch_state":"healthy"}]},{"provider":"speechmatics","configured":false}]}`
	if err := json.Unmarshal([]byte(payload), &result); err != nil {
		t.Fatal(err)
	}
	content := formatTranscriptionKeys(result, botLanguageEN)
	if !strings.Contains(content, "balance: unavailable") || strings.Contains(content, "balance: $0") || !strings.Contains(content, "speechmatics") {
		t.Fatalf("unknown/provider output: %s", content)
	}
	for range 30 {
		result.Providers[0].Keys = append(result.Providers[0].Keys, result.Providers[0].Keys[0])
	}
	parts := splitDiscordMessage(formatTranscriptionKeys(result, botLanguageEN))
	if len(parts) < 2 {
		t.Fatal("many keys must be split for Discord")
	}
}

func TestStreamFailureRevokesGrantAndImmediatelyRefreshesRoster(t *testing.T) {
	refreshed := make(chan struct{}, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/v1/streaming/audio" {
			_ = json.NewEncoder(w).Encode(streamingStatus{Assignments: map[string]streamGrant{"user": {Token: "next", Provider: "speechmatics"}}})
			refreshed <- struct{}{}
			return
		}
		ws, err := upgrader.Upgrade(w, req, nil)
		if err != nil {
			return
		}
		defer ws.Close()
		var meta map[string]any
		if ws.ReadJSON(&meta) != nil {
			return
		}
		_ = ws.WriteJSON(map[string]any{"type": "fallback"})
	}))
	defer server.Close()
	state := &voiceConnectionState{sessionID: 1, transcriptionClient: testAPIClient(server)}
	controller := newStreamingController(state)
	controller.join("user")
	controller.grants["user"] = streamGrant{Token: "old", Provider: "deepgram"}
	r := newRealtimeAudioClient(state.transcriptionClient, TranscriptionRequest{SessionID: 1, DiscordID: "user", AudioPath: "unit.wav", RecordingStartedAt: time.Now(), Streaming: controller}, controller.grants["user"])
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	r.wait(ctx)
	if !r.failed.Load() {
		t.Fatal("capture must see the failed WAV epoch")
	}
	select {
	case <-refreshed:
	case <-ctx.Done():
		t.Fatal("failure must refresh the assignment before periodic polling")
	}
}
