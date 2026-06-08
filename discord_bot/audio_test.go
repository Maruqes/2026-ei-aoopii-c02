package main

import (
	"testing"
	"time"
)

func TestBotLeaveDurationFromEnv(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		want time.Duration
	}{
		{name: "empty disables timer", raw: "", want: 0},
		{name: "zero disables timer", raw: "0", want: 0},
		{name: "negative disables timer", raw: "-1", want: 0},
		{name: "invalid disables timer", raw: "abc", want: 0},
		{name: "minutes", raw: "60", want: 60 * time.Minute},
		{name: "trims spaces", raw: " 15 ", want: 15 * time.Minute},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := botLeaveDurationFromEnv(tt.raw); got != tt.want {
				t.Fatalf("botLeaveDurationFromEnv(%q) = %s, want %s", tt.raw, got, tt.want)
			}
		})
	}
}
