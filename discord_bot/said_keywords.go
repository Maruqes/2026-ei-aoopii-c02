package main

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"
	"unicode"

	"github.com/bwmarrin/discordgo"
)

type SaidKeywordContext struct {
	Session   *discordgo.Session
	GuildID   string
	ChannelID string
	DiscordID string
	Text      string
}

type saidKeywordTrigger struct {
	callback func(SaidKeywordContext) error
	words    []string
}

var saidKeywordTriggers []saidKeywordTrigger

// Register at startup, before audio workers start. All words must be said in
// the same recording, in any order. Each trigger runs once per recording.
func triggerSaidKeyworkd(callback func(SaidKeywordContext) error, words ...string) {
	words = spokenWords(strings.Join(words, " "))
	if callback == nil || len(words) == 0 {
		panic("a spoken trigger requires a callback and words")
	}
	saidKeywordTriggers = append(saidKeywordTriggers, saidKeywordTrigger{callback, words})
}

func spokenWords(text string) []string {
	return strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsNumber(r)
	})
}

type saidVoiceMessage struct {
	ID          int64  `json:"id"`
	RecordingID int64  `json:"recording_id"`
	DiscordID   string `json:"discord_id"`
	Content     string `json:"content"`
}

func dispatchSaidKeywords(event SaidKeywordContext, messages []saidVoiceMessage, triggers []saidKeywordTrigger, fired map[string]bool) {
	// Combine Realtime finals from the same speaker/WAV, including words split
	// across finals. Batch recovery keeps the WAV ID, so it cannot fire twice.
	grouped := map[string][]saidVoiceMessage{}
	order := []string{}
	for _, message := range messages {
		id := message.RecordingID
		if id == 0 {
			id = -message.ID
		}
		key := fmt.Sprintf("%d:%s", id, message.DiscordID)
		if _, exists := grouped[key]; !exists {
			order = append(order, key)
		}
		grouped[key] = append(grouped[key], message)
	}
	for _, key := range order {
		parts := []string{}
		for _, message := range grouped[key] {
			parts = append(parts, message.Content)
		}
		event.DiscordID = grouped[key][0].DiscordID
		event.Text = strings.Join(parts, " ")
		words := map[string]bool{}
		for _, word := range spokenWords(event.Text) {
			words[word] = true
		}
		for i, trigger := range triggers {
			identity := fmt.Sprintf("%s:%d", key, i)
			if fired[identity] {
				continue
			}
			matches := true
			for _, word := range trigger.words {
				matches = matches && words[word]
			}
			if !matches {
				continue
			}
			if err := trigger.callback(event); err != nil {
				log.Printf("spoken trigger failed guild=%s words=%v: %v", event.GuildID, trigger.words, err)
				continue
			}
			fired[identity] = true
		}
	}
}

func runSaidKeywordTriggers(ctx context.Context, s *discordgo.Session, guildID string, state *voiceConnectionState) {
	if state.transcriptionClient == nil || state.sessionID <= 0 || len(saidKeywordTriggers) == 0 {
		return
	}
	event := SaidKeywordContext{Session: s, GuildID: guildID, ChannelID: state.summaryChannelID}
	fired := map[string]bool{}
	poll := func() {
		requestCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var messages []saidVoiceMessage
		path := fmt.Sprintf("/v1/sessions/%d/voice-messages", state.sessionID)
		if err := state.transcriptionClient.getJSON(requestCtx, path, &messages); err != nil {
			log.Printf("spoken triggers unavailable session=%d: %v", state.sessionID, err)
			return
		}
		dispatchSaidKeywords(event, messages, saidKeywordTriggers, fired)
	}
	// ponytail: poll the existing session transcript; add an incremental event
	// feed if long calls make full-transcript polling expensive.
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		poll()
		select {
		case <-ctx.Done():
			poll() // Include the last Batch result after session finalization.
			return
		case <-ticker.C:
		}
	}
}

func sayPijama(event SaidKeywordContext) error {
	if event.ChannelID == "" {
		return fmt.Errorf("no writable text channel in guild %s", event.GuildID)
	}
	_, err := safeChannelSend(event.Session, event.ChannelID, "pijama")
	return err
}
