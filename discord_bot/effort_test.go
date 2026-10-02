package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/bwmarrin/discordgo"
)

func TestEffortCommandRegistersOptionalLevelAndPermissions(t *testing.T) {
	for _, lang := range []botLanguage{botLanguagePT, botLanguageEN} {
		found := false
		for _, command := range buildApplicationCommands(lang) {
			if command.Name != "effort" {
				continue
			}
			found = true
			if command.DefaultMemberPermissions == nil || *command.DefaultMemberPermissions&discordgo.PermissionManageServer == 0 {
				t.Fatal("effort needs Manage Server permission")
			}
			if len(command.Options) != 1 || command.Options[0].Name != "level" || command.Options[0].Required {
				t.Fatal("effort must support reading the current setting without a level")
			}
			if len(command.Options[0].Choices) != 8 {
				t.Fatal("missing reasoning effort choices")
			}
		}
		if !found {
			t.Fatalf("effort is missing for %s", lang)
		}
	}
}

func TestEffortInteractionAcknowledgesAndChecksPermissionsBeforeBackend(t *testing.T) {
	previous := botAPIClient
	defer func() { botAPIClient = previous }()
	for _, tc := range []struct {
		name        string
		permissions int64
		level       string
		wantPath    string
	}{
		{"read", discordgo.PermissionManageServer, "", "/v1/effort"},
		{"change", discordgo.PermissionManageServer, "low", "/v1/effort/current"},
		{"denied", 0, "low", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			acknowledged := false
			called := false
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				called = true
				if !acknowledged || r.URL.Path != tc.wantPath {
					t.Errorf("backend called before acknowledgement or at wrong endpoint: %s", r.URL.Path)
				}
				if tc.level != "" {
					var body map[string]string
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body["effort"] != tc.level {
						t.Errorf("unexpected effort body: %#v (%v)", body, err)
					}
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"provider": "chatgpt", "model": "model-a", "current_effort": "low", "effort": "low", "efforts": []string{"low", "medium"}, "test_response": "ok"})
			}))
			defer server.Close()
			botAPIClient = testAPIClient(server)
			session, err := discordgo.New("Bot fake-test-token")
			if err != nil {
				t.Fatal(err)
			}
			var content string
			session.Client = &http.Client{Transport: testRoundTripper(func(r *http.Request) (*http.Response, error) {
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				if strings.HasSuffix(r.URL.Path, "/callback") {
					if body["type"] != float64(discordgo.InteractionResponseDeferredChannelMessageWithSource) {
						t.Error("effort did not defer its response")
					}
					acknowledged = true
				} else {
					content, _ = body["content"].(string)
				}
				return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"id":"reply"}`)), Request: r}, nil
			})}
			data := discordgo.ApplicationCommandInteractionData{Name: "effort"}
			if tc.level != "" {
				data.Options = []*discordgo.ApplicationCommandInteractionDataOption{{Name: "level", Type: discordgo.ApplicationCommandOptionString, Value: tc.level}}
			}
			handleCommand(session, &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{
				ID: "test", AppID: "test", Token: "test", Type: discordgo.InteractionApplicationCommand,
				Data: data, Member: &discordgo.Member{Permissions: tc.permissions},
			}})
			if called != (tc.wantPath != "") || content == "" {
				t.Fatalf("called=%v, content=%q", called, content)
			}
		})
	}
}
