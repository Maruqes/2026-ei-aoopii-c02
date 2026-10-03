package main

import (
	"strings"
	"testing"
)

func TestSpeechmaticsCostLevels(t *testing.T) {
	for _, tc := range []struct {
		cost float64
		want string
	}{
		{0, "nível 1 (< $1)"},
		{0.99, "nível 1 (< $1)"},
		{1, "nível 2 ($1–5)"},
		{4.99, "nível 2 ($1–5)"},
		{5, "nível 3 ($5–10)"},
		{9.99, "nível 3 ($5–10)"},
		{10, "nível 4 (≥ $10)"},
	} {
		if got := speechmaticsCostLevel(tc.cost, botLanguagePT); got != tc.want {
			t.Fatalf("cost %v: got %q, want %q", tc.cost, got, tc.want)
		}
	}
	if got := speechmaticsCostLevel(1, botLanguageEN); got != "level 2 ($1–5)" {
		t.Fatal(got)
	}
}

func TestSpeechmaticsCostIncludesStreamingAndSmallAmounts(t *testing.T) {
	rate, cost, total := 0.8, 0.000002, 1.04
	key := SpeechmaticsKeyUsageResponse{
		Name: "SPEECHMATICS_API_KEY_01", EstimatedCostUSD: &total, RealtimeHours: 0.01 / 3600,
		CostItems: []SpeechmaticsCostItem{{
			Mode: "realtime", Model: "enhanced", UsedHours: 0.01 / 3600,
			RateUSDPerHour: &rate, EstimatedCostUSD: &cost,
		}},
	}
	got := formatSpeechmaticsKeyLine(key, botLanguagePT)
	if want := "**01:** ≈ $1.04 · nível 2 ($1–5)"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if got := formatSpeechmaticsUSD(cost); got != "< $0.01" {
		t.Fatal(got)
	}
}

func TestSpeechmaticsPartialCostsSurviveBatchFailure(t *testing.T) {
	errMessage, rate, cost := "HTTP 401", 0.45, 0.45
	key := SpeechmaticsKeyUsageResponse{
		Name: "key", Error: &errMessage, RealtimeHours: 2,
		CostItems: []SpeechmaticsCostItem{
			{Mode: "realtime", Model: "standard", UsedHours: 1, RateUSDPerHour: &rate, EstimatedCostUSD: &cost},
			{Mode: "realtime", Model: "unknown", UsedHours: 1},
		},
	}
	got := formatSpeechmaticsKeyLine(key, botLanguageEN)
	if want := "**key:** ≈ $0.45 (partial)"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if strings.Contains(got, "level ") {
		t.Fatal("partial cost must not get a spend level")
	}
	key.CostItems = []SpeechmaticsCostItem{{Mode: "realtime", Model: "unknown", UsedHours: 2}}
	got = formatSpeechmaticsKeyLine(key, botLanguagePT)
	if got != "**key:** indisponível" {
		t.Fatalf("unknown prices must not look free: %s", got)
	}
}
