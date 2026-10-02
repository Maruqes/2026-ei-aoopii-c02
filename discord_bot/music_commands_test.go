package main

import (
	"encoding/json"
	"io"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

	"github.com/bwmarrin/discordgo"
)

func TestMusicCommandsAcknowledgeAndRegister(t *testing.T) {
	for _, lang := range []botLanguage{botLanguagePT, botLanguageEN} {
		commands := make(map[string]*discordgo.ApplicationCommand)
		for _, command := range buildApplicationCommands(lang) {
			commands[command.Name] = command
		}
		for _, name := range []string{"play", "pause", "skip", "queue", "musicstop"} {
			if commands[name] == nil || !isMusicCommand(name) {
				t.Fatalf("missing command %s for %s", name, lang)
			}
			interaction := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{
				Type: discordgo.InteractionApplicationCommand,
				Data: discordgo.ApplicationCommandInteractionData{Name: name},
			}}
			if !needsDeferredResponse(interaction) {
				t.Fatalf("%s does not acknowledge before playback calls", name)
			}
		}
		if len(commands["play"].Options) != 1 || !commands["play"].Options[0].Required || commands["play"].Options[0].Name != "url" {
			t.Fatal("play needs a required video URL")
		}
	}
}

func TestMusicCallerMustBeInServerVoiceChannel(t *testing.T) {
	s := &discordgo.Session{State: discordgo.NewState()}
	if err := s.State.GuildAdd(&discordgo.Guild{ID: "guild", VoiceStates: []*discordgo.VoiceState{
		{UserID: "caller", ChannelID: "call", GuildID: "guild"},
	}}); err != nil {
		t.Fatal(err)
	}
	i := &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{GuildID: "guild", Member: &discordgo.Member{User: &discordgo.User{ID: "caller"}}}}
	if got := musicCallerChannel(s, i); got != "call" {
		t.Fatalf("channel = %q", got)
	}
	i.Member.User.ID = "outsider"
	if musicCallerChannel(s, i) != "" {
		t.Fatal("outsider can control audio")
	}
	i.GuildID = ""
	if musicCallerChannel(s, i) != "" || musicCallerChannel(s, nil) != "" {
		t.Fatal("DM or absent caller accepted")
	}
}

func TestMusicQueueTitlesCannotMentionOrInjectMarkdown(t *testing.T) {
	text := musicQueueText(botLanguagePT, MusicSnapshot{Current: &MusicTrack{Title: "**loud**\n@everyone", DurationSeconds: 65}, Paused: true})
	if strings.Contains(text, "@everyone") || strings.Contains(text, "**loud**") || !strings.Contains(text, "1:05") || !strings.Contains(text, "Pausado") {
		t.Fatalf("unsafe or misleading queue: %s", text)
	}
	if !strings.Contains(musicQueueText(botLanguageEN, MusicSnapshot{}), "/play") {
		t.Fatal("empty queue should explain how to enqueue")
	}
}

func TestRecorderExcludesBotPlaybackPackets(t *testing.T) {
	users := NewSSRCUserMap()
	users.Set(123, "bot")
	packets := make(chan *discordgo.Packet, 1)
	packets <- &discordgo.Packet{SSRC: 123, Opus: []byte{0xff, 0xff}}
	close(packets)
	dir := t.TempDir()
	vc := &discordgo.VoiceConnection{UserID: "bot", OpusRecv: packets}
	if err := ListenAndWriteOpusToWAV(vc, dir, 1, users, nil, nil, nil, nil); err != nil {
		t.Fatalf("bot packet reached decoder: %v", err)
	}
	files, err := filepath.Glob(filepath.Join(dir, "*.wav"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) > 0 {
		t.Fatal("bot playback was recorded")
	}
}

func TestMusicStopRejectsCallerInDifferentCall(t *testing.T) {
	s, err := discordgo.New("Bot fake-test-token")
	if err != nil {
		t.Fatal(err)
	}
	if err := s.State.GuildAdd(&discordgo.Guild{ID: "music-test-guild", VoiceStates: []*discordgo.VoiceState{
		{UserID: "caller", ChannelID: "other-call"},
	}}); err != nil {
		t.Fatal(err)
	}
	state := newVoiceConnectionState(&discordgo.VoiceConnection{ChannelID: "bot-call"}, nil, "call", nil, 0, "")
	t.Cleanup(state.closeMusic)
	state.music.mu.Lock()
	state.music.queue = []MusicTrack{{Title: "keep this track"}}
	state.music.mu.Unlock()
	// Use an isolated guild ID so the shared connection map remains untouched.
	setVoiceConnection("music-test-guild", state)
	t.Cleanup(func() {
		voiceMu.Lock()
		delete(voiceConnections, "music-test-guild")
		voiceMu.Unlock()
	})
	var content string
	s.Client = &http.Client{Transport: testRoundTripper(func(r *http.Request) (*http.Response, error) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if r.Method == http.MethodPatch {
			content, _ = body["content"].(string)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"id":"reply"}`)), Request: r}, nil
	})}
	handleCommand(s, &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{
		ID: "test", AppID: "test", Token: "test", GuildID: "music-test-guild", Member: &discordgo.Member{User: &discordgo.User{ID: "caller"}},
		Type: discordgo.InteractionApplicationCommand, Data: discordgo.ApplicationCommandInteractionData{Name: "musicstop"},
	}})
	if content == "" {
		t.Fatal("missing explanation when caller is outside bot call")
	}
	// The player may already have consumed its queue; generation changes only on Clear.
	state.music.mu.Lock()
	defer state.music.mu.Unlock()
	if state.music.generation != 0 {
		t.Fatal("caller outside bot call cleared playback")
	}
}
