package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/bwmarrin/discordgo"
)

const speechmaticsUsageAlertStep = 10

var speechmaticsUsageAlerts = newSpeechmaticsUsageAlertState()

type speechmaticsUsageAlertState struct {
	mu      sync.Mutex
	buckets map[string]int
}

func newSpeechmaticsUsageAlertState() *speechmaticsUsageAlertState {
	return &speechmaticsUsageAlertState{
		buckets: make(map[string]int),
	}
}

func checkAndSendSpeechmaticsUsageAlerts(
	s *discordgo.Session,
	channelID string,
	client *TranscriptionClient,
	lang botLanguage,
) {
	if s == nil || client == nil || strings.TrimSpace(channelID) == "" {
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	keys, err := client.GetSpeechmaticsKeys(ctx)
	if err != nil {
		log.Printf("erro ao consultar uso Speechmatics para alertas: %v", err)
		return
	}
	if keys.Provider != "speechmatics" {
		return
	}

	alerts := speechmaticsUsageAlerts.collect(keys.Keys, lang)
	if len(alerts) == 0 {
		return
	}

	message := strings.Join(alerts, "\n")
	if len(alerts) > 1 {
		message = textForLanguage(lang, "**Avisos Speechmatics**", "**Speechmatics warnings**") + "\n" + message
	}
	if err := sendLongChannelMessage(s, channelID, message); err != nil {
		log.Printf("erro ao enviar alerta de uso Speechmatics no canal %s: %v", channelID, err)
	}
}

func (state *speechmaticsUsageAlertState) collect(keys []SpeechmaticsKeyUsageResponse, lang botLanguage) []string {
	if state == nil {
		return nil
	}

	alerts := []string{}

	state.mu.Lock()
	defer state.mu.Unlock()

	for _, key := range keys {
		bucket := speechmaticsUsageAlertBucket(key.PercentUsed)
		lastBucket := state.buckets[key.Name]
		if bucket < lastBucket {
			state.buckets[key.Name] = bucket
			continue
		}
		if bucket == 0 || bucket <= lastBucket {
			continue
		}

		state.buckets[key.Name] = bucket
		alerts = append(alerts, speechmaticsUsageAlertLine(key, lang))
	}

	return alerts
}

func speechmaticsUsageAlertBucket(percent *float64) int {
	if percent == nil || *percent < speechmaticsUsageAlertStep {
		return 0
	}
	return int(math.Floor(*percent/float64(speechmaticsUsageAlertStep))) * speechmaticsUsageAlertStep
}

func speechmaticsUsageAlertLine(key SpeechmaticsKeyUsageResponse, lang botLanguage) string {
	percent := "?"
	if key.PercentUsed != nil {
		percent = formatPercent(*key.PercentUsed)
	}

	used := "?"
	if key.UsedHours != nil {
		used = formatAPIHoursMinutes(*key.UsedHours)
	}

	limit := formatAPIHoursMinutes(key.LimitHours)
	jobs := "?"
	if key.JobCount != nil {
		jobs = strconv.Itoa(*key.JobCount)
	}

	return fmt.Sprintf(
		textForLanguage(
			lang,
			"Aviso Speechmatics: a chave **%s** esta em **%s** de uso (%s/%s, %s tarefas).",
			"Speechmatics warning: key **%s** is at **%s** usage (%s/%s, %s jobs).",
		),
		key.Name,
		percent,
		used,
		limit,
		jobs,
	)
}
