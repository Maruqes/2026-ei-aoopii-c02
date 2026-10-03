package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type assistantHarness struct {
	a         *assistantController
	audio     map[string]*realtimeAudioClient
	mu        sync.Mutex
	messages  []string
	questions []string
	answers   chan string
	now       time.Time
}

func newAssistantHarness(t *testing.T) *assistantHarness {
	t.Helper()
	h := &assistantHarness{audio: map[string]*realtimeAudioClient{}, answers: make(chan string, 10), now: time.Now()}
	state := &voiceConnectionState{sessionID: 1, summaryChannelID: "chat"}
	state.streaming = newStreamingController(state)
	h.a = newAssistantController(nil, state)
	h.a.send = func(channel, text string) error {
		h.mu.Lock()
		defer h.mu.Unlock()
		h.messages = append(h.messages, channel+":"+text)
		return nil
	}
	h.a.ask = func(ctx context.Context, text string) (string, error) {
		h.mu.Lock()
		h.questions = append(h.questions, text)
		h.mu.Unlock()
		select {
		case answer := <-h.answers:
			if answer == "error" {
				return "", errors.New("failure")
			}
			return answer, nil
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
	h.a.configure(assistantSettings{Enabled: true, Phrase: "Hey Bot"})
	for _, user := range []string{"ana", "bob"} {
		state.streaming.join(user)
		state.streaming.grants[user] = streamGrant{Token: user}
		audio := &realtimeAudioClient{startedAt: h.now}
		h.audio[user] = audio
		h.a.begin(user, audio)
		h.a.event(user, audio, realtimeEvent{Type: "ready", RecordingID: 10, Generation: 2}, h.now)
	}
	t.Cleanup(h.a.stop)
	return h
}
func (h *assistantHarness) final(user, id, text string, start, end float64) {
	audio := h.audio[user]
	audio.audioMu.Lock()
	audio.frames = int64(end * sampleRate)
	audio.speechFrame = audio.frames
	audio.lastSpeech = h.now.Add(time.Duration(end * float64(time.Second)))
	audio.audioMu.Unlock()
	h.a.event(user, audio, realtimeEvent{Type: "final", SessionID: 1, DiscordID: user, RecordingID: 10, Generation: 2, Identity: id, Text: text, Start: start, End: end}, h.now.Add(time.Duration(end*float64(time.Second))))
}
func (h *assistantHarness) wait(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("assistant effect did not finish")
}
func (h *assistantHarness) waiting() bool {
	h.a.mu.Lock()
	defer h.a.mu.Unlock()
	return h.a.request == nil
}
func TestAssistantSplitPhraseSameUtteranceAndRepeatRecording(t *testing.T) {
	h := newAssistantHarness(t)
	h.final("ana", "a", "Hey", 0, 1)
	h.final("bob", "a", "Bot unrelated", 0, 1)
	if !h.waiting() {
		t.Fatal("cross-speaker wake phrase")
	}
	h.final("ana", "b", "BOT, Explica polimorfismo?", 1, 2)
	h.a.mu.Lock()
	text := h.a.request.text
	h.a.mu.Unlock()
	if text != "Explica polimorfismo?" {
		t.Fatalf("question lost: %q", text)
	}
	h.final("bob", "b", "Hey Bot outra pergunta", 1, 2)
	h.final("ana", "c", "Hey Bot com exemplo.", 2, 3)
	h.a.tick(h.now.Add(8 * time.Second))
	h.answers <- "Primeira resposta"
	h.wait(t, h.waiting)
	h.final("ana", "d", "Hey Bot Segunda pergunta?", 3, 4)
	h.a.tick(h.now.Add(9 * time.Second))
	h.answers <- "Segunda resposta"
	h.wait(t, h.waiting)
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.questions) != 2 || h.questions[0] != "Explica polimorfismo? com exemplo." || h.questions[1] != "Segunda pergunta?" {
		t.Fatalf("questions: %v", h.questions)
	}
	count := 0
	for _, message := range h.messages {
		if strings.Contains(message, "**Pergunta:**") {
			count++
		}
	}
	if count != 2 {
		t.Fatalf("replies: %v", h.messages)
	}
}
func TestAssistantWaitsForAudioFinalsAndFailsIncompleteQuestion(t *testing.T) {
	h := newAssistantHarness(t)
	h.final("ana", "wake", "Hey Bot", 0, 1)
	h.audio["ana"].audioMu.Lock()
	h.audio["ana"].frames = 3 * sampleRate
	h.audio["ana"].speechFrame = 3 * sampleRate
	h.audio["ana"].lastSpeech = h.now.Add(3 * time.Second)
	h.audio["ana"].audioMu.Unlock()
	h.a.tick(h.now.Add(6 * time.Second))
	if h.waiting() {
		t.Fatal("closed before final speech arrived")
	}
	h.a.event("ana", h.audio["ana"], realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "q", Start: 1, End: 3, Text: "Uma pergunta?"}, h.now.Add(6*time.Second))
	h.a.tick(h.now.Add(11 * time.Second))
	h.answers <- "ok"
	h.wait(t, h.waiting)
	h.final("ana", "wake2", "Hey Bot", 3, 4)
	h.audio["ana"].audioMu.Lock()
	h.audio["ana"].frames = 5 * sampleRate
	h.audio["ana"].speechFrame = 5 * sampleRate
	h.audio["ana"].lastSpeech = h.now.Add(5 * time.Second)
	h.audio["ana"].audioMu.Unlock()
	h.a.tick(h.now.Add(15 * time.Second))
	if !h.waiting() {
		t.Fatal("missing finals did not cancel")
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.questions) != 1 {
		t.Fatalf("incomplete question sent: %v", h.questions)
	}
}
func TestAssistantCancelLimitsAndInvalidation(t *testing.T) {
	for _, mode := range []string{"cancel", "empty", "long", "disable", "phrase", "channel", "leave", "fallback", "stop", "llm-error"} {
		t.Run(mode, func(t *testing.T) {
			h := newAssistantHarness(t)
			h.final("ana", "wake", "Hey Bot", 0, 1)
			switch mode {
			case "cancel":
				h.final("ana", "cancel", "Cancela!", 1, 2)
			case "empty":
				h.a.tick(h.now.Add(12 * time.Second))
			case "long":
				h.final("ana", "q", "Pergunta", 1, 2)
				h.a.tick(h.now.Add(32 * time.Second))
			case "leave":
				h.a.state.streaming.leave("ana")
				h.a.tick(h.now.Add(4 * time.Second))
			case "fallback":
				h.a.fail("ana", h.audio["ana"])
			default:
				h.final("ana", "q", "Pergunta", 1, 2)
				h.a.tick(h.now.Add(7 * time.Second))
				h.wait(t, func() bool { h.mu.Lock(); defer h.mu.Unlock(); return len(h.questions) == 1 })
				config := assistantSettings{Enabled: true, Phrase: "Hey Bot", Revision: 1}
				switch mode {
				case "disable":
					config.Enabled = false
					h.a.configure(config)
				case "phrase":
					config.Phrase = "Olá amigo"
					h.a.configure(config)
				case "channel":
					channel := "other"
					config.ChannelID = &channel
					h.a.configure(config)
				case "stop":
					h.a.stop()
				case "llm-error":
				}
				h.answers <- "error"
			}
			h.wait(t, h.waiting)
			h.mu.Lock()
			defer h.mu.Unlock()
			for _, message := range h.messages {
				if strings.Contains(message, "**Pergunta:**") {
					t.Fatalf("cancelled reply: %s", message)
				}
			}
		})
	}
}
func TestAssistantRejectsOldDuplicateBatchAndUncoveredEvents(t *testing.T) {
	h := newAssistantHarness(t)
	for _, kind := range []string{"batch", "final"} {
		h.a.event("ana", h.audio["ana"], realtimeEvent{Type: kind, SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 1, Identity: "old", Text: "Hey Bot", End: 1}, h.now)
	}
	if !h.waiting() {
		t.Fatal("stale event activated")
	}
	h.final("ana", "a", "Bot Hey", 0, 1)
	if !h.waiting() {
		t.Fatal("unordered phrase activated")
	}
	h.final("ana", "b", "Hey Bot", 1, 2)
	h.final("ana", "c", "Cancela", 2, 3)
	h.a.event("ana", h.audio["ana"], realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "b", Text: "Hey Bot", Start: 1, End: 2}, h.now)
	if !h.waiting() {
		t.Fatal("duplicate reactivated")
	}
	h.a.state.streaming.mu.Lock()
	delete(h.a.state.streaming.grants, "ana")
	h.a.state.streaming.mu.Unlock()
	h.final("ana", "d", "Hey Bot", 3, 4)
	if !h.waiting() {
		t.Fatal("Batch user activated")
	}
}
func TestAssistantConfigDoesNotReplayBufferedAudio(t *testing.T) {
	h := newAssistantHarness(t)
	h.audio["ana"].frames = 2 * sampleRate
	h.a.configure(assistantSettings{Enabled: true, Phrase: "Hey Bot", Revision: 1})
	h.a.event("ana", h.audio["ana"], realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "old", Start: 0, End: 2, Text: "Hey Bot"}, h.now)
	if !h.waiting() {
		t.Fatal("preconfiguration audio activated")
	}
	h.a.configure(assistantSettings{Enabled: false, Phrase: "Hey Bot", Revision: 0})
	h.final("ana", "new", "Hey Bot", 2, 3)
	if h.waiting() {
		t.Fatal("older configuration replaced current revision")
	}
}

func TestAssistantKeepsReadingWhileConfirmationPublishes(t *testing.T) {
	h := newAssistantHarness(t)
	publishing := make(chan struct{})
	release := make(chan struct{})
	h.a.send = func(channel, text string) error {
		if strings.Contains(text, "Diz") {
			close(publishing)
			<-release
		}
		return nil
	}
	defer close(release)
	h.final("ana", "wake", "Hey Bot", 0, 1)
	select {
	case <-publishing:
	case <-time.After(time.Second):
		t.Fatal("confirmation missing")
	}
	processed := make(chan struct{})
	go func() { h.final("ana", "question", "Explica Go?", 1, 2); close(processed) }()
	select {
	case <-processed:
	case <-time.After(time.Second):
		t.Fatal("Discord publication blocked final reader")
	}
	h.a.mu.Lock()
	defer h.a.mu.Unlock()
	if h.a.request.text != "Explica Go?" {
		t.Fatal("question was lost during confirmation")
	}
}

func TestAssistantEmptyDestinationAndReplacementStream(t *testing.T) {
	h := newAssistantHarness(t)
	empty := ""
	h.a.configure(assistantSettings{Enabled: true, Phrase: "Hey Bot", ChannelID: &empty, Revision: 1})
	h.final("ana", "wake", "Hey Bot", 0, 1)
	if !h.waiting() {
		t.Fatal("request activated without destination")
	}
	h.a.configure(assistantSettings{Enabled: true, Phrase: "Hey Bot", Revision: 2})
	h.final("ana", "wake2", "Hey Bot", 1, 2)
	old := h.audio["ana"]
	replacement := &realtimeAudioClient{startedAt: h.now}
	h.a.begin("ana", replacement)
	if !h.waiting() {
		t.Fatal("stream replacement did not cancel")
	}
	h.a.event("ana", old, realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "late", Text: "Hey Bot", Start: 2, End: 3}, h.now)
	if !h.waiting() {
		t.Fatal("replaced connection activated")
	}
	h.a.relocate()
	h.a.mu.Lock()
	defer h.a.mu.Unlock()
	if h.a.stopped || len(h.a.streams) != 0 {
		t.Fatal("moving must return to waiting for fresh streams")
	}
}

func TestAssistantCoverageDoesNotAnnounceStartupOrRetiredFailure(t *testing.T) {
	for _, mode := range []string{"startup", "retired-failure"} {
		t.Run(mode, func(t *testing.T) {
			h := newAssistantHarness(t)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if strings.HasSuffix(r.URL.Path, "/streaming") {
					_, _ = w.Write([]byte(`{"enabled":true,"assignments":{"ana":{"token":"ana"},"bob":{"token":"bob"}}}`))
				} else {
					_, _ = w.Write([]byte(`{"enabled":true,"phrase":"Hey Bot","revision":0}`))
				}
			}))
			defer server.Close()
			h.a.state.transcriptionClient = testAPIClient(server)
			messages := make(chan string, 10)
			h.a.send = func(channel, text string) error { messages <- text; return nil }
			if mode == "startup" {
				h.a.state.streaming.mu.Lock()
				h.a.state.streaming.grants = nil
				h.a.state.streaming.mu.Unlock()
			} else {
				if _, err := h.a.state.streaming.sync(context.Background()); err != nil {
					t.Fatal(err)
				}
				h.a.fail("ana", h.audio["ana"])
				h.a.fail("bob", h.audio["bob"])
			}
			h.a.refresh("guild")
			h.a.updateCoverage(h.now.Add(20 * time.Second))
			select {
			case message := <-messages:
				t.Fatalf("false coverage alarm: %s", message)
			case <-time.After(50 * time.Millisecond):
			}
		})
	}
}

func TestAssistantCoverageWaitsForStableLossAndLimitsNotices(t *testing.T) {
	h := newAssistantHarness(t)
	c := h.a.state.streaming
	c.mu.Lock()
	c.synced = true
	c.mu.Unlock()
	messages := make(chan string, 10)
	h.a.send = func(channel, text string) error { messages <- text; return nil }
	setCovered := func(ana, bob bool) {
		c.mu.Lock()
		defer c.mu.Unlock()
		c.grants = map[string]streamGrant{}
		if ana {
			c.grants["ana"] = streamGrant{Token: "ana"}
		}
		if bob {
			c.grants["bob"] = streamGrant{Token: "bob"}
		}
	}
	assertQuiet := func() {
		t.Helper()
		select {
		case msg := <-messages:
			t.Fatalf("unexpected alarm: %s", msg)
		default:
		}
	}
	expectNotice := func(want string) {
		t.Helper()
		select {
		case msg := <-messages:
			if !strings.Contains(msg, want) {
				t.Fatalf("wrong coverage: %s", msg)
			}
		case <-time.After(time.Second):
			t.Fatal("persistent outage was not announced")
		}
	}
	h.a.updateCoverage(h.now)
	setCovered(false, false)
	h.a.updateCoverage(h.now.Add(time.Second))
	h.a.updateCoverage(h.now.Add(10 * time.Second))
	setCovered(true, true)
	h.a.updateCoverage(h.now.Add(12 * time.Second))
	h.a.updateCoverage(h.now.Add(30 * time.Second))
	assertQuiet()
	setCovered(true, false)
	h.a.updateCoverage(h.now.Add(31 * time.Second))
	h.a.updateCoverage(h.now.Add(46 * time.Second))
	expectNotice("<@bob>")
	h.a.updateCoverage(h.now.Add(50 * time.Second))
	assertQuiet()
	// A short recovery must not re-arm an identical outage warning.
	setCovered(true, true)
	h.a.updateCoverage(h.now.Add(51 * time.Second))
	setCovered(true, false)
	h.a.updateCoverage(h.now.Add(55 * time.Second))
	h.a.updateCoverage(h.now.Add(71 * time.Second))
	assertQuiet()
	// A changed persistent roster is announced, at most once per minute.
	setCovered(false, false)
	h.a.updateCoverage(h.now.Add(72 * time.Second))
	h.a.updateCoverage(h.now.Add(88 * time.Second))
	assertQuiet()
	h.a.updateCoverage(h.now.Add(106 * time.Second))
	expectNotice("<@ana>, <@bob>")
}

func TestAssistantCoverageRechecksQueuedWarningAfterRecovery(t *testing.T) {
	h := newAssistantHarness(t)
	c := h.a.state.streaming
	c.mu.Lock()
	c.synced = true
	c.grants = nil
	c.mu.Unlock()
	messages := make(chan string, 1)
	h.a.send = func(channel, text string) error { messages <- text; return nil }
	h.a.publishMu.Lock()
	h.a.updateCoverage(h.now)
	h.a.updateCoverage(h.now.Add(16 * time.Second))
	c.mu.Lock()
	c.grants = map[string]streamGrant{"ana": {Token: "ana"}, "bob": {Token: "bob"}}
	c.mu.Unlock()
	h.a.publishMu.Unlock()
	select {
	case message := <-messages:
		t.Fatalf("queued stale notice: %s", message)
	case <-time.After(50 * time.Millisecond):
	}
}

func TestAssistantPhraseContainsIgnoresAccentsCaseAndPunctuation(t *testing.T) {
	for _, text := range []string{"ola macaco", "Olá, Macaco", "Antes: (OLÁ, MACACO!), Explica Go?", "Ola\u0301, Macaco"} {
		t.Run(text, func(t *testing.T) {
			h := newAssistantHarness(t)
			h.a.configure(assistantSettings{Enabled: true, Phrase: "Olá macaco", Revision: 1})
			h.final("ana", "wake", text, 0, 1)
			if h.waiting() {
				t.Fatalf("phrase did not trigger: %s", text)
			}
			if strings.HasPrefix(text, "Antes:") {
				h.a.mu.Lock()
				question := h.a.request.text
				h.a.mu.Unlock()
				if question != "Antes: Explica Go?" {
					t.Fatalf("wrong contains suffix: %q", question)
				}
			}
		})
	}
	for _, text := range []string{"ola macacolas", "macaco ola", "ola sem qualquer relacao macaco"} {
		t.Run(text, func(t *testing.T) {
			h := newAssistantHarness(t)
			h.a.configure(assistantSettings{Enabled: true, Phrase: "Olá macaco", Revision: 1})
			h.final("ana", "other", text, 0, 1)
			if !h.waiting() {
				t.Fatalf("partial/out-of-order phrase triggered: %s", text)
			}
		})
	}
	h := newAssistantHarness(t)
	h.a.configure(assistantSettings{Enabled: true, Phrase: "Olá macaco", Revision: 1})
	h.final("ana", "first", "OLA,", 0, 1)
	h.final("ana", "second", "Macaco! Explica Go?", 1, 2)
	if h.waiting() {
		t.Fatal("accent-insensitive phrase failed across segments")
	}
	h.a.configure(assistantSettings{Enabled: true, Phrase: "Ó amigo", Revision: 2})
	h.final("ana", "old", "Olá macaco", 2, 3)
	if !h.waiting() {
		t.Fatal("old phrase still activated after configuration change")
	}
	h.final("ana", "new", "O, AMIGO!", 3, 4)
	if h.waiting() {
		t.Fatal("changed phrase did not activate")
	}
}

func TestAssistantDoesNotCancelForOverlappingOrEmptyFinals(t *testing.T) {
	for _, kind := range []string{"overlap", "empty"} {
		t.Run(kind, func(t *testing.T) {
			h := newAssistantHarness(t)
			h.final("ana", "wake", "Hey Bot", 0, 1)
			if kind == "overlap" {
				h.final("ana", "question", "Explica Go?", 0.8, 2)
			} else {
				h.a.event("ana", h.audio["ana"], realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "empty", Start: 0, End: 0, Text: ""}, h.now)
				h.final("ana", "question", "Explica Go?", 1, 2)
			}
			if h.waiting() {
				t.Fatal("a valid capture was cancelled by final metadata")
			}
			h.a.tick(h.now.Add(7 * time.Second))
			h.answers <- "Go é uma linguagem."
			h.wait(t, h.waiting)
			h.mu.Lock()
			defer h.mu.Unlock()
			if len(h.questions) != 1 || h.questions[0] != "Explica Go?" {
				t.Fatalf("question lost or duplicated: %v", h.questions)
			}
		})
	}
}

func TestAssistantTolerantActivationPreservesQuestionAtAnyPosition(t *testing.T) {
	cases := []struct{ input, question string }{
		{"Olá macaco explica Go?", "explica Go?"},
		{"Explica, olá macaco, Go?", "Explica, Go?"},
		{"Explica Go? Olá macaco", "Explica Go?"},
		{"Explica Go? Olha macaco", "Explica Go?"},
		{"Olá meu macaco, explica Go?", "explica Go?"},
		{"Olamacaco explica Go?", "explica Go?"},
		{"Olá ma caco explica Go?", "explica Go?"},
		{"Olá macacoo explica Go?", "explica Go?"},
		{"Olá mcaaco explica Go?", "explica Go?"},
	}
	for _, tc := range cases {
		t.Run(tc.input, func(t *testing.T) {
			h := newAssistantHarness(t)
			h.a.configure(assistantSettings{Enabled: true, Phrase: "Olá macaco", Revision: 1})
			h.final("ana", "wake", tc.input, 0, 2)
			if h.waiting() {
				t.Fatalf("did not trigger: %s", tc.input)
			}
			h.a.mu.Lock()
			question := h.a.request.text
			h.a.mu.Unlock()
			if question != tc.question {
				t.Fatalf("question=%q want=%q", question, tc.question)
			}
			h.a.tick(h.now.Add(7 * time.Second))
			h.answers <- "Resposta."
			h.wait(t, h.waiting)
			h.mu.Lock()
			defer h.mu.Unlock()
			if len(h.questions) != 1 || h.questions[0] != tc.question {
				t.Fatalf("bad LLM request: %v", h.questions)
			}
		})
	}
}

func TestAssistantKeepsQuestionAcrossFinalsWithinSameUtterance(t *testing.T) {
	for _, pause := range []float64{0, 3, 6} {
		t.Run(time.Duration(pause*float64(time.Second)).String(), func(t *testing.T) {
			h := newAssistantHarness(t)
			h.a.configure(assistantSettings{Enabled: true, Phrase: "Olá macaco", Revision: 1})
			h.final("ana", "prefix", "Explica Go?", 0, 1)
			h.final("ana", "wake", "Olá meu", 1+pause, 2+pause)
			h.final("ana", "end", "macaco", 2+pause, 3+pause)
			if h.waiting() {
				t.Fatal("split tolerant phrase did not activate")
			}
			h.a.mu.Lock()
			defer h.a.mu.Unlock()
			want := "Explica Go?"
			if pause > assistantSilence.Seconds() {
				want = ""
			}
			if h.a.request.text != want {
				t.Fatalf("question=%q want=%q", h.a.request.text, want)
			}
		})
	}
}

func TestAssistantOverlappingWordTimesAndCumulativeFinals(t *testing.T) {
	h := newAssistantHarness(t)
	audio := h.audio["ana"]
	audio.audioMu.Lock()
	audio.frames, audio.speechFrame = 2*sampleRate, 2*sampleRate
	audio.lastSpeech = h.now.Add(2 * time.Second)
	audio.audioMu.Unlock()
	send := func(id string, end float64, words []assistantWord) {
		h.a.event("ana", audio, realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: id, End: end, Words: words}, h.now.Add(2*time.Second))
	}
	wake := []assistantWord{{Text: "Hey", Start: 0, End: 0.6}, {Text: "Bot", Start: 0.5, End: 1}}
	send("wake", 1, wake)
	// Real words overlap both the metadata envelope and each other, and arrive unsorted.
	words := append(wake, assistantWord{Text: "Go?", Start: 1.4, End: 2}, assistantWord{Text: "Explica", Start: 0.9, End: 1.5})
	send("question", 1.8, words)
	send("replay", 2, words)
	send("old", 0.6, wake[:1])
	send("empty", 0, nil)
	if h.waiting() {
		t.Fatal("overlapping words cancelled capture")
	}
	h.a.tick(h.now.Add(7 * time.Second))
	h.answers <- "Resposta."
	h.wait(t, h.waiting)
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.questions) != 1 || h.questions[0] != "Explica Go?" {
		t.Fatalf("question lost or duplicated: %v", h.questions)
	}
}

func TestAssistantStillRejectsWordTimesBeyondReceivedAudio(t *testing.T) {
	h := newAssistantHarness(t)
	h.final("ana", "wake", "Hey Bot", 0, 1)
	h.audio["ana"].audioMu.Lock()
	h.audio["ana"].frames = 2 * sampleRate
	h.audio["ana"].audioMu.Unlock()
	h.a.event("ana", h.audio["ana"], realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "invalid", Start: 1, End: 2, Words: []assistantWord{{Text: "Pergunta", Start: 1, End: 3}}}, h.now)
	if !h.waiting() {
		t.Fatal("invalid audio times were accepted")
	}
}

func TestAssistantSendsWholeSlowQuestionAcrossDelayedFinals(t *testing.T) {
	h := newAssistantHarness(t)
	h.a.configure(assistantSettings{Enabled: true, Phrase: "Olá macaco", Revision: 1})
	received := make(chan string, 2)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/assistant/question" {
			t.Errorf("wrong endpoint: %s", r.URL.Path)
		}
		var body struct {
			Question string `json:"question"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		received <- body.Question
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"answer":"Resposta."}`))
	}))
	defer server.Close()
	h.a.state.transcriptionClient = testAPIClient(server)
	h.a.ask = newAssistantController(nil, h.a.state).ask
	assertCapturing := func() {
		t.Helper()
		h.a.mu.Lock()
		defer h.a.mu.Unlock()
		if h.a.request == nil || h.a.request.responding {
			t.Fatal("closed the question before all fragments arrived")
		}
	}
	h.final("ana", "wake", "Olá macaco", 0, 1)
	h.final("ana", "first", "Explica", 2, 3)
	h.a.tick(h.now.Add(5250 * time.Millisecond))
	assertCapturing()
	h.final("ana", "second", "polimorfismo", 6, 7)
	h.a.tick(h.now.Add(9250 * time.Millisecond))
	assertCapturing()
	// The last words are quieter than the PCM energy threshold and their final arrives late.
	audio := h.audio["ana"]
	audio.audioMu.Lock()
	audio.frames = 15 * sampleRate
	audio.audioMu.Unlock()
	h.a.event("ana", audio, realtimeEvent{Type: "speech", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Start: 10, End: 11}, h.now.Add(11*time.Second))
	for second := 11; second < 15; second++ {
		h.a.tick(h.now.Add(time.Duration(second) * time.Second))
		assertCapturing()
	}
	h.a.event("ana", audio, realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "last", Start: 10, End: 11, Text: "com um exemplo em 3 de outubro.", Words: []assistantWord{
		{Text: "com", Start: 10, End: 10.1},
		{Text: "um", Start: 10.1, End: 10.2},
		{Text: "exemplo", Start: 10.2, End: 10.5},
		{Text: "em", Start: 10.5, End: 10.6},
		{Text: "3 de outubro.", Start: 10.6, End: 11},
	}}, h.now.Add(15*time.Second))
	h.a.tick(h.now.Add(18 * time.Second))
	assertCapturing()
	h.a.tick(h.now.Add(20 * time.Second))
	h.wait(t, h.waiting)
	select {
	case text := <-received:
		if text != "Explica polimorfismo com um exemplo em 3 de outubro." {
			t.Fatalf("incomplete question sent to AI: %q", text)
		}
	default:
		t.Fatal("the AI did not receive the question")
	}
	select {
	case text := <-received:
		t.Fatalf("sent more than one AI request: %q", text)
	default:
	}
}

func TestAssistantDelayedWakeDoesNotExpireBeforeQuestion(t *testing.T) {
	h := newAssistantHarness(t)
	audio := h.audio["ana"]
	audio.audioMu.Lock()
	audio.frames, audio.speechFrame = 15*sampleRate, sampleRate
	audio.lastSpeech = h.now.Add(time.Second)
	audio.audioMu.Unlock()
	h.a.event("ana", audio, realtimeEvent{Type: "final", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Identity: "wake", Start: 0, End: 1, Text: "Hey Bot"}, h.now.Add(15*time.Second))
	h.a.tick(h.now.Add(15 * time.Second))
	if h.waiting() {
		t.Fatal("expired before the wake confirmation was delivered")
	}
}

func TestAssistantPartialTimingCannotActivateOrSupplyQuestion(t *testing.T) {
	h := newAssistantHarness(t)
	audio := h.audio["ana"]
	audio.frames = 4 * sampleRate
	partial := realtimeEvent{Type: "speech", SessionID: 1, DiscordID: "ana", RecordingID: 10, Generation: 2, Start: 0, End: 1, Text: "Hey Bot wrong question"}
	h.a.event("ana", audio, partial, h.now.Add(time.Second))
	if !h.waiting() {
		t.Fatal("partial activated the assistant")
	}
	h.final("ana", "wake", "Hey Bot", 1, 2)
	audio.audioMu.Lock()
	audio.frames = 4 * sampleRate
	audio.audioMu.Unlock()
	partial.Start, partial.End = 2, 4
	partial.Generation = 1
	h.a.event("ana", audio, partial, h.now.Add(4*time.Second))
	h.a.mu.Lock()
	if h.a.streams["ana"].speechThrough != 1 {
		t.Error("old generation changed speech progress")
	}
	h.a.mu.Unlock()
	partial.Generation = 2
	h.a.event("ana", audio, partial, h.now.Add(4*time.Second))
	for _, second := range []int{9, 13} {
		h.a.tick(h.now.Add(time.Duration(second) * time.Second))
		h.a.mu.Lock()
		capturing := h.a.request != nil && !h.a.request.responding && h.a.request.text == ""
		h.a.mu.Unlock()
		if !capturing {
			t.Fatal("partial text reached the question or missing finals closed capture early")
		}
	}
	h.a.tick(h.now.Add(14 * time.Second))
	if !h.waiting() {
		t.Fatal("missing final did not time out")
	}
}
