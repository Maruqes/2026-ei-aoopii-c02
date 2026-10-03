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
	if !strings.Contains(content, "Credits: unavailable") || strings.Contains(content, "Credits: $0") || !strings.Contains(content, "Speechmatics") {
		t.Fatalf("unknown/provider output: %s", content)
	}
	for range 150 {
		result.Providers[0].Keys = append(result.Providers[0].Keys, result.Providers[0].Keys[0])
	}
	parts := splitDiscordMessage(formatTranscriptionKeys(result, botLanguageEN))
	if len(parts) < 2 {
		t.Fatal("many keys must be split for Discord")
	}
}

func TestKeysAreCompactAndExplainMissingBalancePermission(t *testing.T) {
	payload := `{"order":["deepgram","speechmatics"],"source":"environment","since":"2026-10-01","until":"2026-10-03T20:00:00Z","providers":[{"provider":"deepgram","configured":true,"model":"nova-3","streaming_model":"nova-3","groups":[{"name":"private-project-id","verified":true,"streaming_occupied":2,"streaming_limit":150,"balance_usd":null,"balance_error":"forbidden"}],"keys":[{"name":"DEEPGRAM_API_KEY_01","streaming_state":"healthy","batch_state":"healthy"},{"name":"DEEPGRAM_API_KEY_02","streaming_state":"healthy","batch_state":"no_credits"}],"cost_items":[{"estimated_cost_usd":0.18},{"estimated_cost_usd":0.19}]},{"provider":"speechmatics","configured":true,"groups":[{"streaming_limit":2},{"streaming_limit":2}],"keys":[{"name":"SPEECHMATICS_API_KEY_01","streaming_state":"healthy","batch_state":"healthy"}],"usage":{"keys":[{"name":"SPEECHMATICS_API_KEY_01","estimated_cost_usd":2.30}]}}]}`
	var result transcriptionKeys
	if err := json.Unmarshal([]byte(payload), &result); err != nil {
		t.Fatal(err)
	}
	for _, lang := range []botLanguage{botLanguagePT, botLanguageEN} {
		content := formatTranscriptionKeys(result, lang)
		for _, want := range []string{"billing:read", "Streams: 2/150", "Streams: 0/4", "$0.37", "$2.30", "Key 01:", "Key 02:"} {
			if !strings.Contains(content, want) {
				t.Fatalf("missing %q: %s", want, content)
			}
		}
		for _, unwanted := range []string{"private-project-id", "API_KEY", "UTC:", "WAV", "WS ", "environment", "nova-3", "· nível ", "· level ", "2026-10-03T"} {
			if strings.Contains(content, unwanted) {
				t.Fatalf("output contains noise %q: %s", unwanted, content)
			}
		}
		if len(content) > 1000 || strings.Count(content, "Key 01:") != 2 || !strings.Contains(content, textForLanguage(lang, "ficheiros sem créditos", "files no credits")) {
			t.Fatalf("compact output must keep key health without duplicates: %s", content)
		}
	}
	zero := 0.0
	result.Providers[0].Groups[0].Balance = &zero
	content := formatTranscriptionKeys(result, botLanguageEN)
	if !strings.Contains(content, "Credits: $0.00") || strings.Contains(content, "billing:read") {
		t.Fatalf("a known zero balance must take precedence: %s", content)
	}
	result.Providers[0].CostItems[1].Cost = nil
	if strings.Contains(formatTranscriptionKeys(result, botLanguageEN), "Estimated cost this month") {
		t.Fatal("a partial estimate must not be presented as a complete monthly cost")
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
