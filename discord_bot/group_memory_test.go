package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
)

func giphyTestResult(id, media string) reactionGIF {
	gif := reactionGIF{ID: id}
	gif.Images.Original.URL = media
	return gif
}

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
		_ = json.NewEncoder(w).Encode(map[string]any{"data": []reactionGIF{
			giphyTestResult("evil", "https://evil.example/meme.gif"),
			giphyTestResult("insecure", "http://media.giphy.com/media/insecure/giphy.gif"),
			giphyTestResult("credentials", "https://user@media.giphy.com/media/credentials/giphy.gif"),
			giphyTestResult("spoof", "https://media.giphy.com.evil.example/media/spoof/giphy.gif"),
			giphyTestResult("juggling", "https://media.giphy.com/media/juggling/giphy.gif"),
			giphyTestResult("spinning-plates", "https://media1.giphy.com/media/spinning-plates/giphy.gif"),
		}})
	}))
	defer server.Close()
	seen := map[string]bool{}
	for range 2 {
		gif, err := searchMemeGIF(context.Background(), server.Client(), server.URL, "test-key", " juggling too many tasks ", t.Name())
		if err != nil || (gif != "https://media.giphy.com/media/juggling/giphy.gif" && gif != "https://media1.giphy.com/media/spinning-plates/giphy.gif") || seen[gif] {
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

func TestReactionGIFPrefersAnimatedMediaAndDeduplicatesByID(t *testing.T) {
	guild := t.Name()
	t.Cleanup(func() {
		recentReactionGIFs.Lock()
		delete(recentReactionGIFs.byGuild, guild)
		recentReactionGIFs.Unlock()
	})
	gif := giphyTestResult("same", "https://media2.giphy.com/media/same/giphy.gif")
	gif.Images.Downsized.URL = "https://media2.giphy.com/media/same/giphy-downsized.gif"
	if got := selectReactionGIF(guild, []reactionGIF{gif}); got != gif.Images.Downsized.URL {
		t.Fatalf("expected compact animated rendition, got %q", got)
	}
	gif.Images.Downsized.URL = "https://media3.giphy.com/media/same/giphy.gif?tracking=new"
	if got := selectReactionGIF(guild, []reactionGIF{gif}); got != "" {
		t.Fatalf("same GIF ID repeated through another rendition: %q", got)
	}
	for _, invalid := range []string{"https://giphy.com/gifs/page", "https://media.giphy.com/media/id/giphy.mp4", "https://media.giphy.com:8443/media/id/giphy.gif", "https://other.giphy.com/media/id/giphy.gif"} {
		gif = giphyTestResult("invalid", invalid)
		if got := gif.mediaURL(); got != "" {
			t.Errorf("accepted non-playable/untrusted URL %q", got)
		}
		gif.Images.FixedWidth.URL = "https://media4.giphy.com/media/valid/200w.gif"
		if got := gif.mediaURL(); got != gif.Images.FixedWidth.URL {
			t.Errorf("did not use valid alternate rendition: %q", got)
		}
	}
}

func TestReactionGIFSearchUsesNextPageWhenRecentResultsAreExhausted(t *testing.T) {
	guild := t.Name()
	t.Cleanup(func() {
		recentReactionGIFs.Lock()
		delete(recentReactionGIFs.byGuild, guild)
		recentReactionGIFs.Unlock()
	})
	var firstPage []reactionGIF
	for i := range 12 {
		id := fmt.Sprintf("recent-%d", i)
		firstPage = append(firstPage, giphyTestResult(id, "https://media.giphy.com/media/"+id+"/giphy.gif"))
		selectReactionGIF(guild, firstPage[i:i+1])
	}
	var offsets []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		offset := r.URL.Query().Get("offset")
		offsets = append(offsets, offset)
		if r.URL.Query().Get("q") != "spinning plates" || r.URL.Query().Get("lang") != "en" {
			t.Error("pagination changed the contextual query or language")
		}
		results := firstPage
		if offset == "12" {
			results = []reactionGIF{giphyTestResult("fresh", "https://media.giphy.com/media/fresh/giphy.gif")}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"data": results})
	}))
	defer server.Close()
	got, err := searchMemeGIF(context.Background(), server.Client(), server.URL, "key", "spinning plates", guild)
	if err != nil || got != "https://media.giphy.com/media/fresh/giphy.gif" || strings.Join(offsets, ",") != "0,12" {
		t.Fatalf("gif=%q err=%v offsets=%v", got, err, offsets)
	}
}

func TestReactionGIFErrorsPreserveTextAndDoNotLeakKey(t *testing.T) {
	for _, tc := range []struct {
		name, body, expected string
		status               int
	}{
		{"unauthorized", `{}`, "HTTP 403", 403},
		{"rate-limit", `{}`, "HTTP 429", 429},
		{"provider-meta", `{"meta":{"status":403},"data":[]}`, "status 403", 200},
		{"invalid-json", `not JSON`, "invalid GIF response", 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			}))
			defer server.Close()
			_, err := searchMemeGIF(context.Background(), server.Client(), server.URL, "secret-key", "juggling tasks", t.Name())
			if err == nil || !strings.Contains(err.Error(), tc.expected) || strings.Contains(err.Error(), "secret-key") {
				t.Fatalf("unsafe or missing diagnostic: %v", err)
			}
			reaction := groupReaction{Text: "O plano já faz malabarismo.", GIFQuery: "juggling tasks"}
			if got := groupReactionContent(context.Background(), server.Client(), server.URL, "secret-key", reaction); got != reaction.Text {
				t.Fatalf("provider failure lost text: %q", got)
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := searchMemeGIF(ctx, http.DefaultClient, "https://api.giphy.com/v1/gifs/search", "secret-key", "juggling", t.Name())
	if err == nil || !strings.Contains(err.Error(), "context canceled") || strings.Contains(err.Error(), "secret-key") {
		t.Fatalf("cancellation was lost or exposed key: %v", err)
	}
}

func TestGIFOnlyReactionWithoutMediaFailsWithoutEmptyMessageOrVoice(t *testing.T) {
	t.Setenv("GIPHY_API_KEY", "")
	h := newAssistantHarness(t)
	h.a.voiceEnabled = true
	h.a.state.streaming.synced = true
	h.a.speak = func(context.Context, string, func() error) error {
		t.Fatal("GIF-only reaction attempted voice")
		return nil
	}
	setVoiceConnection(t.Name(), h.a.state)
	t.Cleanup(func() { voiceMu.Lock(); delete(voiceConnections, t.Name()); voiceMu.Unlock() })
	var status string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/result") {
			var data map[string]string
			_ = json.NewDecoder(r.Body).Decode(&data)
			status = data["status"]
		}
		_, _ = io.WriteString(w, `{"status":"claimed"}`)
	}))
	defer server.Close()
	deliverGroupReaction(context.Background(), nil, testAPIClient(server), groupReaction{ID: 8, GuildID: t.Name(), ChannelID: "chat", GIFQuery: "juggling tasks", Speak: true})
	if status != "failed" || len(h.messages) != 0 {
		t.Fatalf("empty reaction was published: status=%q messages=%v", status, h.messages)
	}
}

func TestReactionGIFDeliveredToDiscordWithTextOrAlone(t *testing.T) {
	for _, text := range []string{"O plano entrou em combustão.", ""} {
		t.Run(fmt.Sprintf("text=%v", text != ""), func(t *testing.T) {
			t.Setenv("GIPHY_API_KEY", "test-key")
			guild := t.Name()
			t.Cleanup(func() {
				recentReactionGIFs.Lock()
				delete(recentReactionGIFs.byGuild, guild)
				recentReactionGIFs.Unlock()
			})
			var sent discordgo.MessageSend
			var result string
			var requests []string
			transport := testRoundTripper(func(r *http.Request) (*http.Response, error) {
				body := `{}`
				if r.URL.Host == "api.giphy.com" {
					requests = append(requests, "giphy")
					if r.URL.Query().Get("q") != "this is fine dog" {
						t.Error("lost GIF query")
					}
					body = `{"data":[{"id":"fire","url":"https://giphy.com/gifs/fire","images":{"original":{"url":"https://media.giphy.com/media/fire/giphy.gif"}}}]}`
				} else {
					requests = append(requests, "discord")
					if err := json.NewDecoder(r.Body).Decode(&sent); err != nil {
						t.Fatal(err)
					}
					body = `{"id":"sent"}`
				}
				return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(body))}, nil
			})
			previous := http.DefaultClient
			http.DefaultClient = &http.Client{Transport: transport}
			t.Cleanup(func() { http.DefaultClient = previous })
			session, _ := discordgo.New("Bot test-token")
			session.Client = &http.Client{Transport: transport}
			session.State.User = &discordgo.User{ID: "bot"}
			_ = session.State.GuildAdd(&discordgo.Guild{ID: guild, Roles: []*discordgo.Role{{ID: guild, Permissions: discordgo.PermissionViewChannel | discordgo.PermissionSendMessages | discordgo.PermissionEmbedLinks}}, Channels: []*discordgo.Channel{{ID: "chat", GuildID: guild, Type: discordgo.ChannelTypeGuildText}}})
			_ = session.State.MemberAdd(&discordgo.Member{GuildID: guild, User: session.State.User})
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if strings.HasSuffix(r.URL.Path, "/claim") {
					requests = append(requests, "claim")
					_, _ = io.WriteString(w, `{"status":"claimed"}`)
				} else {
					requests = append(requests, "result")
					var data map[string]string
					_ = json.NewDecoder(r.Body).Decode(&data)
					result = data["status"]
					_, _ = io.WriteString(w, `{}`)
				}
			}))
			defer api.Close()
			deliverGroupReaction(context.Background(), session, testAPIClient(api), groupReaction{ID: 7, GuildID: guild, ChannelID: "chat", Text: text, GIFQuery: "this is fine dog", Speak: true})
			expected := "https://media.giphy.com/media/fire/giphy.gif"
			if text != "" {
				expected = text + "\n\n" + expected
			}
			if sent.Content != expected || sent.AllowedMentions == nil || result != "sent" || strings.Join(requests, ",") != "claim,giphy,discord,result" {
				t.Fatalf("content=%q result=%q requests=%v", sent.Content, result, requests)
			}
		})
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
