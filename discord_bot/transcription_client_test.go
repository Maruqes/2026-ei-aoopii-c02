package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestFinishSessionAndWaitContinuesAfterSummaryTimeout(t *testing.T) {
	t.Setenv("SESSION_SUMMARY_TIMEOUT", "1ms")
	t.Setenv("SESSION_SUMMARY_MAX_WAIT", "5s")
	t.Setenv("SESSION_SUMMARY_POLL_INTERVAL", "1ms")

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
		baseURL: server.URL,
		httpClient: &http.Client{
			Timeout: time.Second,
		},
	}

	summary, err := client.FinishSessionAndWait(t.Context(), 42)
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
