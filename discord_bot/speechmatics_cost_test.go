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
		Name: "key", EstimatedCostUSD: &total, RealtimeHours: 0.01 / 3600,
		CostItems: []SpeechmaticsCostItem{{
			Mode: "realtime", Model: "enhanced", UsedHours: 0.01 / 3600,
			RateUSDPerHour: &rate, EstimatedCostUSD: &cost,
		}},
	}
	got := formatSpeechmaticsKeyLine(key, botLanguagePT)
	for _, want := range []string{"Streaming (local) / enhanced", "$0.80/h", "< $0.0001", "$1.04 USD", "nível 2"} {
		if !strings.Contains(got, want) {
			t.Fatalf("missing %q in %q", want, got)
		}
	}
	if got := formatSpeechmaticsDuration(318.0 / 3600); got != "0h 05m 18s" {
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
	for _, want := range []string{"Batch unavailable: HTTP 401", "unknown model", "cost unavailable", "Partial estimate: ≈ $0.4500 USD; total unavailable"} {
		if !strings.Contains(got, want) {
			t.Fatalf("missing %q in %q", want, got)
		}
	}
	if strings.Contains(got, "level ") {
		t.Fatal("partial cost must not get a spend level")
	}
	key.CostItems = []SpeechmaticsCostItem{{Mode: "realtime", Model: "unknown", UsedHours: 2}}
	got = formatSpeechmaticsKeyLine(key, botLanguagePT)
	if !strings.Contains(got, "Custo estimado indisponível") || strings.Contains(got, "$0") {
		t.Fatalf("unknown prices must not look free: %s", got)
	}
}
