package main

import (
	"reflect"
	"testing"
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
