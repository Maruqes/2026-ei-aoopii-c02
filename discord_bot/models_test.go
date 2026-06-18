package main

import (
	"reflect"
	"strings"
	"testing"
	"unicode/utf8"
)

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
