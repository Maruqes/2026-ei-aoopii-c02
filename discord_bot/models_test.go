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
