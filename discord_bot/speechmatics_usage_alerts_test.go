package main

import (
	"strings"
	"testing"
)

func TestSpeechmaticsUsageAlertStateAlertsAtTenPercentSteps(t *testing.T) {
	state := newSpeechmaticsUsageAlertState()
	key := speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY", 9)

	if got := state.collect([]SpeechmaticsKeyUsageResponse{key}, botLanguagePT); len(got) != 0 {
		t.Fatalf("alerts below 10%% = %v, want none", got)
	}

	key = speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY", 10)
	alerts := state.collect([]SpeechmaticsKeyUsageResponse{key}, botLanguagePT)
	if len(alerts) != 1 || !strings.Contains(alerts[0], "**10%**") {
		t.Fatalf("alerts at 10%% = %v, want one current-percent alert", alerts)
	}

	key = speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY", 19.9)
	if got := state.collect([]SpeechmaticsKeyUsageResponse{key}, botLanguagePT); len(got) != 0 {
		t.Fatalf("alerts inside same bucket = %v, want none", got)
	}

	key = speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY", 20.4)
	alerts = state.collect([]SpeechmaticsKeyUsageResponse{key}, botLanguagePT)
	if len(alerts) != 1 || !strings.Contains(alerts[0], "**20.4%**") {
		t.Fatalf("alerts at next bucket = %v, want current 20.4%% alert", alerts)
	}
}

func TestSpeechmaticsUsageAlertStateResetsWhenUsageDrops(t *testing.T) {
	state := newSpeechmaticsUsageAlertState()

	key := speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY_01", 80)
	if got := state.collect([]SpeechmaticsKeyUsageResponse{key}, botLanguagePT); len(got) != 1 {
		t.Fatalf("initial alerts = %v, want one", got)
	}

	key = speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY_01", 3)
	if got := state.collect([]SpeechmaticsKeyUsageResponse{key}, botLanguagePT); len(got) != 0 {
		t.Fatalf("reset alerts = %v, want none", got)
	}

	key = speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY_01", 10)
	alerts := state.collect([]SpeechmaticsKeyUsageResponse{key}, botLanguagePT)
	if len(alerts) != 1 || !strings.Contains(alerts[0], "**10%**") {
		t.Fatalf("alerts after reset = %v, want one 10%% alert", alerts)
	}
}

func TestSpeechmaticsUsageAlertLineUsesRequestedLanguage(t *testing.T) {
	key := speechmaticsUsageAlertTestKey("SPEECHMATICS_API_KEY_02", 31.2)

	got := speechmaticsUsageAlertLine(key, botLanguageEN)

	if !strings.Contains(got, "Speechmatics warning") || !strings.Contains(got, "31.2%") || !strings.Contains(got, "jobs") {
		t.Fatalf("english alert = %q, want English text with current percent", got)
	}
}

func speechmaticsUsageAlertTestKey(name string, percent float64) SpeechmaticsKeyUsageResponse {
	usedHours := percent / 2
	jobs := int(percent)
	return SpeechmaticsKeyUsageResponse{
		Name:        name,
		UsedHours:   &usedHours,
		LimitHours:  50,
		PercentUsed: &percent,
		JobCount:    &jobs,
	}
}
