package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestChatGPTSelectionSendsAdminKeyOnlyToControlEndpoints(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Header.Get("X-ChatGPT-Admin-Key")
		if r.URL.Path == "/v1/models/current" || r.URL.Path == "/v1/effort/current" {
			if key != "owner-secret" {
				t.Errorf("model endpoint key = %q", key)
			}
		} else if key != "" {
			t.Errorf("admin key sent to unrelated endpoint %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"provider":"chatgpt","model":"selected","test_response":"Ola!"}`))
	}))
	defer server.Close()
	t.Setenv("TRANSCRIPTION_API_URL", server.URL)
	t.Setenv("CHATGPT_ADMIN_PASSWORD", "owner-secret")
	client := NewTranscriptionClientFromEnv()
	if _, err := client.SelectLLMModel(context.Background(), "selected"); err != nil {
		t.Fatal(err)
	}
	if _, err := client.SelectLLMEffort(context.Background(), "low"); err != nil {
		t.Fatal(err)
	}
	var unrelatedResponse map[string]any
	if err := client.postJSON(context.Background(), "/unrelated", map[string]string{}, &unrelatedResponse); err != nil {
		t.Fatal(err)
	}
}

func TestFinishSessionAndWaitContinuesUntilSummaryIsReady(t *testing.T) {
	t.Setenv("SESSION_SUMMARY_POLL_INTERVAL", "1ms")
	t.Setenv("RECORDINGS_DIR", t.TempDir())

	var summaryRequests int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")

		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/sessions/42/finish":
			_ = json.NewEncoder(w).Encode(VoiceSessionResponse{
				ID:     42,
				Status: "finished",
			})
		case r.Method == http.MethodGet && r.URL.Path == "/v1/sessions/42/summary":
			count := atomic.AddInt32(&summaryRequests, 1)
			status := "agent_running"
			var summary *string
			if count >= 3 {
				status = "agent_done"
				value := "Resumo pronto."
				summary = &value
			}
			_ = json.NewEncoder(w).Encode(SessionSummaryResponse{
				SessionID: 42,
				Status:    status,
				Summary:   summary,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := &TranscriptionClient{
		baseURL:    server.URL,
		httpClient: &http.Client{},
	}

	summary, err := client.FinishSessionAndWait(testContext(t), 42, "pt")
	if err != nil {
		t.Fatal(err)
	}
	if summary == nil || summary.Summary == nil || *summary.Summary != "Resumo pronto." {
		t.Fatalf("summary = %#v, want final summary", summary)
	}
	if got := atomic.LoadInt32(&summaryRequests); got < 3 {
		t.Fatalf("summary requests = %d, want at least 3", got)
	}
}
