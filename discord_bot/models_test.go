package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/bwmarrin/discordgo"
)

func TestExplicitModelSelectionChecksPermissionsAndSkipsCatalog(t *testing.T) {
	previous := botAPIClient
	defer func() { botAPIClient = previous }()
	for _, lang := range []botLanguage{botLanguagePT, botLanguageEN} {
		for _, command := range buildApplicationCommands(lang) {
			if command.Name == "models" && (len(command.Options) != 1 || command.Options[0].Name != "model" || command.Options[0].Required) {
				t.Fatal("models must accept an optional model ID")
			}
		}
	}
	for _, permissions := range []int64{0, discordgo.PermissionManageServer} {
		calls := 0
		acknowledged := false
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			var body map[string]string
			if !acknowledged || r.Method != http.MethodPost || r.URL.Path != "/v1/models/current" {
				t.Errorf("unexpected request before acknowledgement: %s %s", r.Method, r.URL.Path)
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body["model"] != "gpt-6-astra" {
				t.Errorf("incorrect model: %v (%v)", body, err)
			}
			_ = json.NewEncoder(w).Encode(SelectLLMModelResponse{Provider: "chatgpt", Model: "gpt-6-astra", TestResponse: "ok"})
		}))
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
				acknowledged = body["type"] == float64(discordgo.InteractionResponseDeferredChannelMessageWithSource)
			} else {
				content, _ = body["content"].(string)
			}
			return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"id":"reply"}`)), Request: r}, nil
		})}
		handleCommand(session, &discordgo.InteractionCreate{Interaction: &discordgo.Interaction{
			ID: "test", AppID: "test", Token: "test", Type: discordgo.InteractionApplicationCommand,
			Member: &discordgo.Member{Permissions: permissions},
			Data: discordgo.ApplicationCommandInteractionData{Name: "models", Options: []*discordgo.ApplicationCommandInteractionDataOption{
				{Name: "model", Type: discordgo.ApplicationCommandOptionString, Value: "gpt-6-astra"},
			}},
		}})
		server.Close()
		if (calls == 1) != (permissions != 0) || calls > 1 || content == "" {
			t.Fatalf("permissions=%d, backend calls=%d, content=%q", permissions, calls, content)
		}
	}
}

func TestChatGPTModelsResponseShowsEntireCatalogAcrossPages(t *testing.T) {
	catalog := make([]string, 57)
	for index := range catalog {
		catalog[index] = fmt.Sprintf("model-%02d", index)
	}
	models := &LLMModelsResponse{Provider: "chatgpt", CurrentModel: catalog[56], Models: catalog}
	var displayed []string
	for page := 0; page < 3; page++ {
		response := modelsResponse(models, page)
		if response == nil {
			t.Fatalf("page %d has no response", page)
		}
		menu := response.Data.Components[0].(discordgo.ActionsRow).Components[0].(discordgo.SelectMenu)
		if len(menu.Options) > 25 {
			t.Fatalf("page %d exceeds Discord's option limit", page)
		}
		for _, option := range menu.Options {
			displayed = append(displayed, option.Value)
			if option.Default != (option.Value == models.CurrentModel) {
				t.Fatalf("incorrect current model marker for %s", option.Value)
			}
		}
		buttons := response.Data.Components[1].(discordgo.ActionsRow).Components
		if buttons[0].(discordgo.Button).Disabled != (page == 0) || buttons[1].(discordgo.Button).Disabled != (page == 2) {
			t.Fatalf("incorrect navigation on page %d", page)
		}
	}
	if !reflect.DeepEqual(displayed, catalog) {
		t.Fatalf("displayed catalog = %v, want %v", displayed, catalog)
	}
}

func TestModelMenuItemsIncludesCurrentModel(t *testing.T) {
	models := []string{"a", "b", "c", "d"}

	got := modelMenuItems(models, "d", 3)
	want := []string{"a", "b", "d"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("modelMenuItems() = %v, want %v", got, want)
	}
}

func TestModelMenuItemsDoesNotMutateInput(t *testing.T) {
	models := []string{"a", "b", "c", "d"}

	_ = modelMenuItems(models, "d", 3)

	if !reflect.DeepEqual(models, []string{"a", "b", "c", "d"}) {
		t.Fatalf("modelMenuItems mutated input: %v", models)
	}
}

func TestModelMenuItemsExcludesValuesTooLongForDiscord(t *testing.T) {
	longModel := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	got := modelMenuItems([]string{"short", longModel}, "", 25)
	want := []string{"short"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("modelMenuItems() = %v, want %v", got, want)
	}
}

func TestModelMenuPageItemsReturnsRequestedPage(t *testing.T) {
	models := []string{"a", "b", "c", "d", "e"}

	got := modelMenuPageItems(models, 1, 2)
	want := []string{"c", "d"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("modelMenuPageItems() = %v, want %v", got, want)
	}
}

func TestModelMenuPageItemsClampsToLastPage(t *testing.T) {
	models := []string{"a", "b", "c", "d", "e"}

	got := modelMenuPageItems(models, 99, 2)
	want := []string{"e"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("modelMenuPageItems() = %v, want %v", got, want)
	}
}

func TestParseModelPage(t *testing.T) {
	page, ok := parseModelPage(modelPageCustomID(12))

	if !ok || page != 12 {
		t.Fatalf("parseModelPage() = %d, %v; want 12, true", page, ok)
	}
}

func TestSplitDiscordMessageSplitsWithoutDroppingWords(t *testing.T) {
	content := strings.Repeat("alpha beta gamma. ", 12)

	chunks := splitDiscordMessageAt(content, 45)

	if len(chunks) < 2 {
		t.Fatalf("splitDiscordMessageAt() returned %d chunks, want multiple", len(chunks))
	}
	for _, chunk := range chunks {
		if len(chunk) > 45 {
			t.Fatalf("chunk len = %d, want <= 45: %q", len(chunk), chunk)
		}
		if strings.Contains(chunk, "...") {
			t.Fatalf("chunk contains truncation marker: %q", chunk)
		}
	}
	got := strings.Join(strings.Fields(strings.Join(chunks, " ")), " ")
	want := strings.Join(strings.Fields(content), " ")
	if got != want {
		t.Fatalf("joined chunks = %q, want %q", got, want)
	}
}

func TestSplitDiscordMessageKeepsUTF8Valid(t *testing.T) {
	content := strings.Repeat("acao ", 20) + strings.Repeat("á", 20)

	chunks := splitDiscordMessageAt(content, 17)

	for _, chunk := range chunks {
		if len(chunk) > 17 {
			t.Fatalf("chunk len = %d, want <= 17: %q", len(chunk), chunk)
		}
		if !utf8.ValidString(chunk) {
			t.Fatalf("chunk is not valid utf8: %q", chunk)
		}
	}
}
