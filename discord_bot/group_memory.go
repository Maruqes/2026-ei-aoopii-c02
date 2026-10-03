package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/bwmarrin/discordgo"
)

type groupReaction struct {
	ID        int64  `json:"id"`
	GuildID   string `json:"guild_id"`
	ChannelID string `json:"channel_id"`
	Text      string `json:"text"`
	Speak     bool   `json:"speak"`
	GIFQuery  string `json:"gif_query"`
}

func runGroupReactions(ctx context.Context, s *discordgo.Session, client *TranscriptionClient) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			requestCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
			var reactions []groupReaction
			err := client.getJSON(requestCtx, "/v1/memory/reactions", &reactions)
			cancel()
			if err != nil {
				log.Printf("group reactions unavailable: %v", err)
				continue
			}
			for _, reaction := range reactions {
				if ctx.Err() != nil {
					return
				}
				deliverGroupReaction(ctx, s, client, reaction)
			}
		}
	}
}

// Caller holds a.mu. Only speak when the whole monitored call has been quiet.
func (a *assistantController) proactiveReady(now time.Time, speak bool) bool {
	if !a.configured || !a.settings.Enabled || a.stopped || a.request != nil || a.proactiveCancel != nil || !a.canRespond() {
		return false
	}
	if !speak || !a.voiceEnabled {
		return true
	}
	_, uncovered, ready := a.state.streaming.coverageSnapshot()
	if !ready || len(uncovered) > 0 || len(a.streams) == 0 {
		return false
	}
	if a.state.music != nil && a.state.music.IsBusy() {
		return false
	}
	for user, stream := range a.streams {
		if !a.eligible(user) {
			continue
		}
		_, _, lastSpeech := stream.audio.progress()
		if stream.failed {
			return false
		}
		// A bulk reaction needs a quiet call, not a final for every PCM noise peak.
		if recognizedEnd := stream.audio.startedAt.Add(time.Duration(stream.wordThrough * float64(time.Second))); stream.wordThrough > 0 && recognizedEnd.After(lastSpeech) {
			lastSpeech = recognizedEnd
		}
		if stream.lastSpeechAt.After(lastSpeech) {
			lastSpeech = stream.lastSpeechAt
		}
		if !lastSpeech.IsZero() && now.Sub(lastSpeech) < a.silence {
			return false
		}
	}
	return true
}

func deliverGroupReaction(ctx context.Context, s *discordgo.Session, client *TranscriptionClient, reaction groupReaction) {
	state := getVoiceConnection(reaction.GuildID)
	var assistant *assistantController
	if state != nil {
		assistant = state.assistant
	}
	channel := reaction.ChannelID
	if assistant != nil {
		assistant.publishMu.Lock()
		assistant.mu.Lock()
		ready := assistant.proactiveReady(time.Now(), false)
		if ready && reaction.Speak && assistant.voiceEnabled {
			_, uncovered, monitored := assistant.state.streaming.coverageSnapshot()
			if monitored && len(uncovered) == 0 && len(assistant.streams) > 0 {
				ready = assistant.proactiveReady(time.Now(), true)
			}
		}
		channel = assistant.destination()
		assistant.mu.Unlock()
		if !ready {
			assistant.publishMu.Unlock()
			return
		}
	} else {
		if s == nil || s.State == nil || s.State.User == nil {
			return
		}
		ch, err := s.State.Channel(channel)
		if err != nil || ch.GuildID != reaction.GuildID || ch.Type != discordgo.ChannelTypeGuildText {
			return
		}
		permissions, err := s.State.UserChannelPermissions(s.State.User.ID, channel)
		if err != nil || permissions&(discordgo.PermissionViewChannel|discordgo.PermissionSendMessages) != (discordgo.PermissionViewChannel|discordgo.PermissionSendMessages) {
			return
		}
	}
	unlock := func() {
		if assistant != nil {
			assistant.publishMu.Unlock()
		}
	}
	requestCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	var claimed struct {
		Status string `json:"status"`
	}
	err := client.postJSON(requestCtx, fmt.Sprintf("/v1/memory/reactions/%d/claim", reaction.ID), map[string]string{}, &claimed)
	cancel()
	if err != nil {
		unlock()
		return
	}
	// ponytail: claim before sending gives at-most-once attempts; never replay ambiguous Discord sends.
	content := reaction.Text
	if reaction.GIFQuery != "" && os.Getenv("GIPHY_API_KEY") != "" {
		gifCtx, gifCancel := context.WithTimeout(ctx, 5*time.Second)
		gif, gifErr := searchMemeGIF(gifCtx, http.DefaultClient, "https://api.giphy.com/v1/gifs/search", os.Getenv("GIPHY_API_KEY"), reaction.GIFQuery)
		gifCancel()
		if gifErr == nil && gif != "" {
			content += "\n\n" + gif
		}
	}
	if assistant != nil {
		assistant.mu.Lock()
		ready := assistant.proactiveReady(time.Now(), false)
		assistant.mu.Unlock()
		if ready {
			err = assistant.send(channel, content)
		} else {
			err = fmt.Errorf("reaction superseded by a direct request")
		}
	} else {
		sendCtx, sendCancel := context.WithTimeout(ctx, 10*time.Second)
		_, err = s.ChannelMessageSendComplex(channel, &discordgo.MessageSend{Content: content, AllowedMentions: noMentions()}, discordgo.WithContext(sendCtx))
		sendCancel()
	}
	var voiceCtx context.Context
	var voiceCancel context.CancelFunc
	if assistant != nil && err == nil && reaction.Speak && assistant.voiceEnabled {
		assistant.mu.Lock()
		if assistant.proactiveReady(time.Now(), true) {
			voiceCtx, voiceCancel = context.WithTimeout(ctx, 2*time.Minute)
			assistant.proactiveCancel = voiceCancel
			assistant.proactiveStartedAt = time.Now()
		}
		assistant.mu.Unlock()
	}
	unlock()
	status := "sent"
	if err != nil {
		status = "failed"
		log.Printf("group reaction send failed bulk=%d: %v", reaction.ID, err)
	}
	resultCtx, resultCancel := context.WithTimeout(ctx, 5*time.Second)
	_ = client.postJSON(resultCtx, fmt.Sprintf("/v1/memory/reactions/%d/result", reaction.ID), map[string]string{"status": status}, nil)
	resultCancel()
	if voiceCtx != nil {
		voiceErr := assistant.speak(voiceCtx, reaction.Text)
		voiceCancel()
		assistant.mu.Lock()
		assistant.proactiveCancel = nil
		assistant.mu.Unlock()
		if voiceErr != nil {
			log.Printf("group reaction voice stopped bulk=%d: %v", reaction.ID, voiceErr)
		}
	}
}

func searchMemeGIF(ctx context.Context, client *http.Client, endpoint, key, query string) (string, error) {
	values := url.Values{"api_key": {key}, "q": {strings.TrimSpace(query) + " meme"}, "limit": {"3"}, "rating": {"pg-13"}}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"?"+values.Encode(), nil)
	if err != nil {
		return "", fmt.Errorf("invalid GIF request")
	}
	response, err := client.Do(request)
	if err != nil {
		return "", fmt.Errorf("GIF provider unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("GIF provider HTTP %d", response.StatusCode)
	}
	var data struct {
		Data []struct {
			URL string `json:"url"`
		} `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&data); err != nil {
		return "", fmt.Errorf("invalid GIF response")
	}
	for _, gif := range data.Data {
		parsed, err := url.Parse(gif.URL)
		if err == nil && parsed.Scheme == "https" && parsed.Hostname() == "giphy.com" && parsed.User == nil {
			return gif.URL, nil
		}
	}
	return "", nil
}
