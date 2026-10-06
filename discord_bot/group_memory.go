package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strings"
	"sync"
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

// ponytail: recent GIFs live in memory; persist them if repeat avoidance must survive bot restarts.
var recentReactionGIFs = struct {
	sync.Mutex
	byGuild map[string][]string
}{byGuild: make(map[string][]string)}

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
	if !a.configured || !a.settings.Enabled || a.stopped || a.conversation != nil || a.request != nil || a.proactiveCancel != nil || !a.canRespond() {
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
	reaction.Speak = reaction.Speak && strings.TrimSpace(reaction.Text) != ""
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
	if reaction.GIFQuery != "" && s != nil && s.State != nil && s.State.User != nil {
		permissions, permissionErr := s.State.UserChannelPermissions(s.State.User.ID, channel)
		if permissionErr == nil && permissions&discordgo.PermissionEmbedLinks == 0 {
			log.Printf("group reaction GIF preview unavailable bulk=%d channel=%s: missing Embed Links permission", reaction.ID, channel)
		}
	}
	requestCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	var claimed struct {
		Status string `json:"status"`
	}
	err := client.postJSON(requestCtx, fmt.Sprintf("/v1/memory/reactions/%d/claim", reaction.ID), map[string]string{}, &claimed)
	cancel()
	if err != nil {
		log.Printf("group reaction claim failed bulk=%d guild=%s: %v", reaction.ID, reaction.GuildID, err)
		unlock()
		return
	}
	// ponytail: claim before sending gives at-most-once attempts; never replay ambiguous Discord sends.
	content := groupReactionContent(ctx, http.DefaultClient, "https://api.giphy.com/v1/gifs/search", os.Getenv("GIPHY_API_KEY"), reaction)
	if content == "" {
		err = fmt.Errorf("reaction has no deliverable text or GIF")
	} else if assistant != nil {
		assistant.mu.Lock()
		ready := assistant.proactiveReady(time.Now(), false)
		assistant.mu.Unlock()
		if ready {
			err = assistant.send(ctx, channel, content)
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
	if assistant != nil && err == nil && reaction.Speak && strings.TrimSpace(reaction.Text) != "" && assistant.voiceEnabled {
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
	if resultErr := client.postJSON(resultCtx, fmt.Sprintf("/v1/memory/reactions/%d/result", reaction.ID), map[string]string{"status": status}, nil); resultErr != nil {
		log.Printf("group reaction result failed bulk=%d status=%s: %v", reaction.ID, status, resultErr)
	}
	resultCancel()
	if voiceCtx != nil {
		voiceErr := assistant.speak(voiceCtx, reaction.Text, nil)
		voiceCancel()
		assistant.mu.Lock()
		assistant.proactiveCancel = nil
		assistant.mu.Unlock()
		if voiceErr != nil {
			log.Printf("group reaction voice stopped bulk=%d: %v", reaction.ID, voiceErr)
		}
	}
}

func groupReactionContent(ctx context.Context, client *http.Client, endpoint, key string, reaction groupReaction) string {
	content := strings.TrimSpace(reaction.Text)
	if strings.TrimSpace(reaction.GIFQuery) == "" {
		log.Printf("group reaction GIF omitted bulk=%d: no visual reaction proposed", reaction.ID)
		return content
	}
	if strings.TrimSpace(key) == "" {
		log.Printf("group reaction GIF omitted bulk=%d: GIPHY_API_KEY not configured", reaction.ID)
		return content
	}
	gifCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	gif, err := searchMemeGIF(gifCtx, client, endpoint, key, reaction.GIFQuery, reaction.GuildID)
	if err != nil {
		log.Printf("group reaction GIF search failed bulk=%d guild=%s: %v", reaction.ID, reaction.GuildID, err)
	} else if gif == "" {
		log.Printf("group reaction GIF omitted bulk=%d: no fresh playable results", reaction.ID)
	} else {
		log.Printf("group reaction GIF selected bulk=%d guild=%s", reaction.ID, reaction.GuildID)
		if content != "" {
			content += "\n\n"
		}
		content += gif
	}
	return content
}

type reactionGIF struct {
	ID     string `json:"id"`
	Images struct {
		Downsized struct {
			URL string `json:"url"`
		} `json:"downsized"`
		Original struct {
			URL string `json:"url"`
		} `json:"original"`
		FixedWidth struct {
			URL string `json:"url"`
		} `json:"fixed_width"`
	} `json:"images"`
}

// Use an animated rendition, not the provider's HTML page or a still/MP4 preview.
func (gif reactionGIF) mediaURL() string {
	for _, value := range []string{gif.Images.Downsized.URL, gif.Images.Original.URL, gif.Images.FixedWidth.URL} {
		parsed, err := url.Parse(value)
		if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.Port() != "" || !strings.HasSuffix(parsed.Path, ".gif") {
			continue
		}
		switch parsed.Hostname() {
		case "media.giphy.com", "media0.giphy.com", "media1.giphy.com", "media2.giphy.com", "media3.giphy.com", "media4.giphy.com", "i.giphy.com":
			return value
		}
	}
	return ""
}

func searchMemeGIF(ctx context.Context, client *http.Client, endpoint, key, query, guildID string) (string, error) {
	query, key = strings.TrimSpace(query), strings.TrimSpace(key)
	if query == "" || key == "" {
		return "", fmt.Errorf("GIF search requires a query and API key")
	}
	// A second page keeps a recurring visual theme usable after page one is exhausted.
	for _, offset := range []string{"0", "12"} {
		gifs, err := fetchReactionGIFs(ctx, client, endpoint, key, query, offset)
		if err != nil {
			return "", err
		}
		if gif := selectReactionGIF(guildID, gifs); gif != "" {
			return gif, nil
		}
		if len(gifs) < 12 {
			break
		}
	}
	return "", nil
}

func fetchReactionGIFs(ctx context.Context, client *http.Client, endpoint, key, query, offset string) ([]reactionGIF, error) {
	values := url.Values{"api_key": {key}, "q": {query}, "limit": {"12"}, "offset": {offset}, "rating": {"pg-13"}, "lang": {"en"}}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"?"+values.Encode(), nil)
	if err != nil {
		return nil, fmt.Errorf("invalid GIF request")
	}
	response, err := client.Do(request)
	if err != nil {
		// Do not wrap transport errors: their URL can contain the API key.
		if ctx.Err() != nil {
			return nil, fmt.Errorf("GIF search interrupted: %v", ctx.Err())
		}
		return nil, fmt.Errorf("GIF provider unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GIF provider HTTP %d", response.StatusCode)
	}
	var data struct {
		Data []reactionGIF `json:"data"`
		Meta struct {
			Status int `json:"status"`
		} `json:"meta"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&data); err != nil {
		return nil, fmt.Errorf("invalid GIF response")
	}
	if data.Meta.Status != 0 && data.Meta.Status != http.StatusOK {
		return nil, fmt.Errorf("GIF provider status %d", data.Meta.Status)
	}
	return data.Data, nil
}

func selectReactionGIF(guildID string, gifs []reactionGIF) string {
	recentReactionGIFs.Lock()
	defer recentReactionGIFs.Unlock()
	recent := recentReactionGIFs.byGuild[guildID]
	type candidate struct{ id, url string }
	candidates := make([]candidate, 0, 5)
	for _, gif := range gifs {
		media := gif.mediaURL()
		if media == "" || gif.ID == "" || slices.Contains(recent, gif.ID) {
			continue
		}
		candidates = append(candidates, candidate{gif.ID, media})
		// Preserve search relevance while varying the best available matches.
		if len(candidates) == 5 {
			break
		}
	}
	if len(candidates) == 0 {
		return ""
	}
	gif := candidates[rand.IntN(len(candidates))]
	recent = append(recent, gif.id)
	if len(recent) > 20 {
		recent = recent[len(recent)-20:]
	}
	recentReactionGIFs.byGuild[guildID] = recent
	return gif.url
}
