package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func offlineAssistantMusic(t *testing.T) *MusicPlayer {
	t.Helper()
	p := offlineMusicPlayer(t, func(ctx context.Context, _ MusicTrack) error {
		<-ctx.Done()
		return ctx.Err()
	})
	p.search = func(_ context.Context, query string) (string, error) {
		return testMusicURL, nil
	}
	return p
}

func TestAssistantPlayIntentEnqueuesLikeSlashPlay(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.music = offlineAssistantMusic(t)
	line := h.a.executeAssistantIntent("ana", assistantIntent{Action: "play", Query: "music1"})
	if !strings.Contains(line, "Vou tocar") && !strings.Contains(line, "Preparing") {
		t.Fatalf("play intent did not confirm like /play: %q", line)
	}
	if snapshot := h.a.state.music.Snapshot(); snapshot.Current == nil && len(snapshot.Queue) == 0 {
		t.Fatal("play intent did not enqueue")
	}
}

func TestAssistantPlayIntentReportsSearchErrors(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.music = offlineMusicPlayer(t, func(ctx context.Context, _ MusicTrack) error {
		<-ctx.Done()
		return ctx.Err()
	})
	h.a.state.music.search = func(context.Context, string) (string, error) {
		return "", ErrMusicNoResults
	}
	line := h.a.executeAssistantIntent("ana", assistantIntent{Action: "play", Query: "unknown song xyz"})
	if !strings.Contains(line, "Não consegui") && !strings.Contains(line, "Could not") {
		t.Fatalf("search failure did not speak like /play: %q", line)
	}
}

func TestAssistantStopIntentClearsLikeMusicStop(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.music = offlineAssistantMusic(t)
	if _, _, err := h.a.state.music.Enqueue(testMusicURL, "ana"); err != nil {
		t.Fatal(err)
	}
	line := h.a.executeAssistantIntent("ana", assistantIntent{Action: "stop"})
	if !strings.Contains(line, "fila limpa") && !strings.Contains(line, "queue cleared") {
		t.Fatalf("stop intent did not confirm like /musicstop: %q", line)
	}
	waitMusicCondition(t, func() bool { return !h.a.state.music.IsBusy() })
}

func TestAssistantUnknownIntentIgnored(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.music = offlineAssistantMusic(t)
	if line := h.a.executeAssistantIntent("ana", assistantIntent{Action: "dance"}); line != "" {
		t.Fatalf("unknown intent executed: %q", line)
	}
}

func TestAssistantIntentWithoutPlayerExplains(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.music = nil
	if line := h.a.executeAssistantIntent("ana", assistantIntent{Action: "play", Query: "music1"}); !strings.Contains(line, "leitor ativo") && !strings.Contains(line, "active player") {
		t.Fatalf("missing player did not explain: %q", line)
	}
	if line := h.a.executeAssistantIntent("ana", assistantIntent{Action: "stop"}); !strings.Contains(line, "leitor ativo") && !strings.Contains(line, "active player") {
		t.Fatalf("missing player did not explain: %q", line)
	}
}

func TestAssistantAskExecutesIntentsFromAPI(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.state.music = offlineAssistantMusic(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"question": "play music1",
			"answer":   "On it.",
			"intents":  []map[string]string{{"action": "play", "query": "music1"}},
		})
	}))
	defer server.Close()
	h.a.state.transcriptionClient = testAPIClient(server)
	ask := newAssistantController(nil, h.a.state).ask
	answer, err := ask(context.Background(), "ana", "play music1", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(answer, "On it.") {
		t.Fatalf("base answer lost: %q", answer)
	}
	if !strings.Contains(answer, "Vou tocar") && !strings.Contains(answer, "Preparing") {
		t.Fatalf("play outcome not appended: %q", answer)
	}
	if snapshot := h.a.state.music.Snapshot(); snapshot.Current == nil && len(snapshot.Queue) == 0 {
		t.Fatal("API intent did not enqueue")
	}
}

func TestAssistantAskKeepsBackwardCompatWithoutIntents(t *testing.T) {
	h := newAssistantHarness(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"answer":"Resposta."}`))
	}))
	defer server.Close()
	h.a.state.transcriptionClient = testAPIClient(server)
	ask := newAssistantController(nil, h.a.state).ask
	answer, err := ask(context.Background(), "ana", "pergunta", nil)
	if err != nil || answer != "Resposta." {
		t.Fatalf("old server shape broke: %q %v", answer, err)
	}
}

func TestAssistantStopIntentActionMapping(t *testing.T) {
	// Voice STOP_MUSIC == /musicstop (stop + clear, keep recording),
	// NOT /stop (pause bot + leave call).
	if !isMusicCommand("musicstop") {
		t.Fatal("musicstop is not a music command")
	}
	if isMusicCommand("stop") {
		t.Fatal("stop must not be treated as a music command")
	}
	h := newAssistantHarness(t)
	h.a.state.music = offlineAssistantMusic(t)
	if _, _, err := h.a.state.music.Enqueue(testMusicURL, "ana"); err != nil {
		if !errors.Is(err, ErrMusicClosed) {
			t.Fatal(err)
		}
	}
	line := h.a.executeAssistantIntent("ana", assistantIntent{Action: "stop"})
	if strings.Contains(strings.ToLower(line), "leave") || strings.Contains(strings.ToLower(line), "abandon") {
		t.Fatalf("stop intent must not leave the call: %q", line)
	}
}
