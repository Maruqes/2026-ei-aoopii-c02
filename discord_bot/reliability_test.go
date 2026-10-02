package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
	"gopkg.in/hraban/opus.v2"
)

func testContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func testAPIClient(server *httptest.Server) *TranscriptionClient {
	return &TranscriptionClient{baseURL: server.URL, endpoint: server.URL + "/v1/transcriptions", httpClient: server.Client()}
}

func TestSessionFinishWaitsForRecordingAdmission(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	admitted := make(chan struct{})
	release := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(release) })
	finishCalled := make(chan struct{}, 1)
	var finishBeforeAdmission atomic.Bool
	var completed atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/v1/transcriptions":
			close(admitted)
			<-release
			completed.Store(true)
			w.WriteHeader(http.StatusAccepted)
		case "/v1/sessions/42/finish":
			finishCalled <- struct{}{}
			finishBeforeAdmission.Store(!completed.Load())
			_ = json.NewEncoder(w).Encode(VoiceSessionResponse{ID: 42, Status: "finished"})
		case "/v1/sessions/42/summary":
			_ = json.NewEncoder(w).Encode(SessionSummaryResponse{SessionID: 42, Status: "agent_done"})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := testAPIClient(server)
	request := TranscriptionRequest{SessionID: 42, AudioPath: filepath.Join(recordingsDirFromEnv(), "one.wav"), DiscordID: "user"}
	client.QueueTranscription(request)
	<-admitted
	// A duplicate delivery must share the pending admission, not double POST it.
	client.QueueTranscription(request)
	result := make(chan error, 1)
	go func() { _, err := client.FinishSessionAndWait(testContext(t), 42, "pt"); result <- err }()
	select {
	case <-finishCalled:
		releaseOnce.Do(func() { close(release) })
		t.Fatal("finish reached backend while admission was blocked")
	case <-time.After(30 * time.Millisecond):
	}
	releaseOnce.Do(func() { close(release) })
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	if finishBeforeAdmission.Load() {
		t.Fatal("session closed before recording admission")
	}
	if _, err := os.Stat(transcriptionOutboxPath(request)); !os.IsNotExist(err) {
		t.Fatalf("accepted outbox remains: %v", err)
	}
	if _, err := os.Stat(sessionFinishPath(42)); !os.IsNotExist(err) {
		t.Fatalf("finished session marker remains: %v", err)
	}
}

func TestFailedRecordingReplayClearsItsOwnSubmissionError(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if attempts.Add(1) == 1 {
			http.Error(w, "validation", http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	client := testAPIClient(server)
	request := TranscriptionRequest{SessionID: 8, AudioPath: filepath.Join(recordingsDirFromEnv(), "retry.wav"), DiscordID: "user"}
	client.QueueTranscription(request)
	if err := client.waitForSubmissions(testContext(t), 8); err == nil {
		t.Fatal("failure should be reported")
	}
	if _, err := os.Stat(transcriptionOutboxPath(request)); err != nil {
		t.Fatal("failed request lost from outbox", err)
	}
	client.QueueTranscription(request)
	if err := client.waitForSubmissions(testContext(t), 8); err != nil {
		t.Fatal("replayed success remained marked failed", err)
	}
}

func TestSubmissionWaitCancelsWithoutBlockingAnotherWait(t *testing.T) {
	client := &TranscriptionClient{}
	client.submissionMu.Lock()
	group := client.submissionGroupLocked(3)
	group.pending = 1
	client.submissionMu.Unlock()
	ctx, cancel := context.WithCancel(testContext(t))
	cancel()
	if err := client.waitForSubmissions(ctx, 3); !errors.Is(err, context.Canceled) {
		t.Fatalf("got %v", err)
	}
	client.submissionMu.Lock()
	group.pending = 0
	group.notify()
	client.submissionMu.Unlock()
	if err := client.waitForSubmissions(testContext(t), 3); err != nil {
		t.Fatal(err)
	}
}

func TestRestartFinalizesSessionWhoseAudioWasAlreadyAccepted(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	if err := persistSessionFinish(55, "en"); err != nil {
		t.Fatal(err)
	}
	finished := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/sessions/55/finish" {
			t.Errorf("unexpected %s", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		_ = json.NewEncoder(w).Encode(VoiceSessionResponse{ID: 55, Status: "finished"})
		finished <- body["language"]
	}))
	defer server.Close()
	client := testAPIClient(server)
	client.ReplayTranscriptions()
	select {
	case language := <-finished:
		if language != "en" {
			t.Fatalf("saved language lost: %s", language)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("recovery ignored a session with no remaining recording sidecars")
	}
	// Wait for cleanup before the test removes its temporary directory.
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(sessionFinishPath(55)); os.IsNotExist(err) {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("recovery marker was not removed")
}

func TestConcurrentOutboxWritesStayValidAndPrivate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "request.json")
	var group sync.WaitGroup
	for n := 0; n < 25; n++ {
		group.Add(1)
		go func(value int) {
			defer group.Done()
			if err := persistJSONAtomically(path, map[string]int{"value": value}); err != nil {
				t.Error(err)
			}
		}(n)
	}
	group.Wait()
	payload, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]int
	if err := json.Unmarshal(payload, &decoded); err != nil {
		t.Fatal("torn outbox", err)
	}
	stat, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if stat.Mode().Perm() != 0600 {
		t.Fatalf("mode %v", stat.Mode())
	}
	temps, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".outbox-*.tmp"))
	if len(temps) != 0 {
		t.Fatalf("temporary files leaked: %v", temps)
	}
}

func TestAudioRTPDiscontinuityDoesNotWriteHoursOfSilence(t *testing.T) {
	t.Setenv("RECORDING_IDLE_SECONDS", "10")
	dir := t.TempDir()
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppVoIP)
	if err != nil {
		t.Fatal(err)
	}
	encoded := make([]byte, 4000)
	size, err := encoder.Encode(make([]int16, defaultOpusFrameSamples*channels), encoded)
	if err != nil {
		t.Fatal(err)
	}
	packets := make(chan *discordgo.Packet, 2)
	packets <- &discordgo.Packet{SSRC: 10, Sequence: 1, Timestamp: 100, Opus: encoded[:size]}
	packets <- &discordgo.Packet{SSRC: 10, Sequence: 2, Timestamp: 1 << 30, Opus: encoded[:size]}
	close(packets)
	users := NewSSRCUserMap()
	users.Set(10, "user")
	vc := &discordgo.VoiceConnection{OpusRecv: packets}
	if err := ListenAndWriteOpusToWAV(vc, dir, 1, users, nil, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.wav"))
	if len(files) != 2 {
		t.Fatalf("expected separate clips, got %d", len(files))
	}
	for _, path := range files {
		stat, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if stat.Size() > 44+4*sampleRate {
			t.Fatalf("timestamp gap expanded WAV to %d bytes", stat.Size())
		}
	}
}

func TestStopBypassesFullRecordingControlQueue(t *testing.T) {
	state := newVoiceConnectionState(nil, nil, "voice", nil, 0, "")
	t.Cleanup(state.closeMusic)
	for len(state.recordingEvents) < cap(state.recordingEvents) {
		state.recordingEvents <- recordingControlEvent{finishAll: true}
	}
	state.queueAllRecordingsFinish()
	select {
	case <-state.recordingStop:
	default:
		t.Fatal("stop queued behind full event channel")
	}
	state.queueAllRecordingsFinish() // Concurrent/repeated disconnects must not close twice.
}

type testRoundTripper func(*http.Request) (*http.Response, error)

func (f testRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestHealthInteractionAcknowledgesBeforeBackendAndDisablesMentions(t *testing.T) {
	previous := botAPIClient
	defer func() { botAPIClient = previous }()
	var acknowledged atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !acknowledged.Load() {
			t.Error("backend called before Discord acknowledgement")
		}
		_ = json.NewEncoder(w).Encode(HealthResponse{Status: "ok", Database: "ok"})
	}))
	defer server.Close()
	botAPIClient = testAPIClient(server)
	session, err := discordgo.New("Bot fake-test-token")
	if err != nil {
		t.Fatal(err)
	}
	var responseEdited atomic.Bool
	session.Client = &http.Client{Transport: testRoundTripper(func(r *http.Request) (*http.Response, error) {
		var body map[string]any
		if r.Body != nil {
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
		}
		if strings.HasSuffix(r.URL.Path, "/callback") {
			if body["type"] != float64(discordgo.InteractionResponseDeferredChannelMessageWithSource) {
				t.Errorf("response type %v", body["type"])
			}
			acknowledged.Store(true)
			return &http.Response{StatusCode: 204, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("")), Request: r}, nil
		}
		if r.Method == http.MethodPatch {
			mentions, ok := body["allowed_mentions"].(map[string]any)
			if !ok {
				t.Error("allowed mentions absent")
			} else if values, ok := mentions["parse"].([]any); !ok || len(values) != 0 {
				t.Error("mentions were not disabled")
			}
			responseEdited.Store(true)
			return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"id":"reply"}`)), Request: r}, nil
		}
		t.Errorf("unexpected Discord request %s %s", r.Method, r.URL.Path)
		return nil, errors.New("unexpected request")
	})}
	handleCommand(session, &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{ID: "test", AppID: "test", Token: "test", Type: discordgo.InteractionApplicationCommand, Data: discordgo.ApplicationCommandInteractionData{Name: "health"}}})
	if !responseEdited.Load() {
		t.Fatal("deferred response was never completed")
	}
}

func TestManagementCommandsAdvertiseRequiredDiscordPermissions(t *testing.T) {
	for _, command := range buildApplicationCommands(botLanguagePT) {
		if requiresManageServer(command.Name) && (command.DefaultMemberPermissions == nil || *command.DefaultMemberPermissions&discordgo.PermissionManageServer == 0) {
			t.Errorf("%s lacks default permissions", command.Name)
		}
	}
}

func TestRetryDoesNotCallWithCanceledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(testContext(t))
	cancel()
	called := false
	err := retryAPI(ctx, func() error { called = true; return nil })
	if called || !errors.Is(err, context.Canceled) {
		t.Fatalf("called=%v err=%v", called, err)
	}
}

func TestRetrySessionReplaysMissingAdmissionBeforeBackendRecovery(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	request := TranscriptionRequest{SessionID: 9, AudioPath: filepath.Join(recordingsDirFromEnv(), "missing.wav"), DiscordID: "user"}.withFallbacks()
	if err := persistTranscription(request); err != nil {
		t.Fatal(err)
	}
	var admitted atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/transcriptions":
			admitted.Store(true)
			w.WriteHeader(http.StatusAccepted)
		case "/v1/guilds/guild/sessions/9/retry":
			if !admitted.Load() {
				t.Error("backend recovery before bot outbox admission")
			}
			_ = json.NewEncoder(w).Encode(SessionSummaryResponse{SessionID: 9, Status: "finished"})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client := testAPIClient(server)
	if err := client.RetrySession(testContext(t), "guild", 9); err != nil {
		t.Fatal(err)
	}
	if !admitted.Load() {
		t.Fatal("retry never submitted bot outbox")
	}
}

func TestAPIRequestDeadlineCancelsHangingBackend(t *testing.T) {
	t.Setenv("API_REQUEST_TIMEOUT", "20ms")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer server.Close()
	client := testAPIClient(server)
	_, err := client.GetHealth(testContext(t))
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("request did not enforce finite deadline: %v", err)
	}
}
