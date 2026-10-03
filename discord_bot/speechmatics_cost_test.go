package main

import (
	"strings"
	"testing"
)

func TestSpeechmaticsCostIncludesStreamingAndSmallAmounts(t *testing.T) {
	rate, cost, total := 0.8, 0.000002, 1.04
	key := SpeechmaticsKeyUsageResponse{
		Name: "SPEECHMATICS_API_KEY_01", EstimatedCostUSD: &total, RealtimeHours: 0.01 / 3600,
		CostItems: []SpeechmaticsCostItem{{
			Mode: "realtime", Model: "enhanced", UsedHours: 0.01 / 3600,
			RateUSDPerHour: &rate, EstimatedCostUSD: &cost,
		}},
	}
	got := speechmaticsKeyCost(key, botLanguagePT)
	if want := "≈ $1.04"; got != want {
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
	got := speechmaticsKeyCost(key, botLanguageEN)
	if want := "≈ $0.45 (partial)"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if strings.Contains(got, "level ") {
		t.Fatal("partial cost must not get a spend level")
	}
	key.CostItems = []SpeechmaticsCostItem{{Mode: "realtime", Model: "unknown", UsedHours: 2}}
	got = speechmaticsKeyCost(key, botLanguagePT)
	if got != "indisponível" {
		t.Fatalf("unknown prices must not look free: %s", got)
	}
}
