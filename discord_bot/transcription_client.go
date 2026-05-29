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
	"path/filepath"
	"strings"
	"time"
)

const (
	defaultTranscriptionAPIURL     = "http://localhost:8000"
	defaultTranscriptionAPITimeout = 10 * time.Minute
	defaultSummaryPollTimeout      = 15 * time.Minute
)

type TranscriptionClient struct {
	baseURL    string
	endpoint   string
	httpClient *http.Client
}

type TranscriptionRequest struct {
	SessionID          int64
	AudioPath          string
	DiscordID          string
	Username           string
	DisplayName        string
	ChannelName        string
	RecordingStartedAt time.Time
}

type CreateSessionRequest struct {
	GuildID          string    `json:"guild_id"`
	VoiceChannelID   string    `json:"voice_channel_id"`
	ChannelName      string    `json:"channel_name"`
	SummaryChannelID string    `json:"summary_channel_id,omitempty"`
	StartedAt        time.Time `json:"started_at"`
}

type VoiceSessionResponse struct {
	ID               int64   `json:"id"`
	GuildID          string  `json:"guild_id"`
	VoiceChannelID   string  `json:"voice_channel_id"`
	ChannelName      string  `json:"channel_name"`
	SummaryChannelID *string `json:"summary_channel_id"`
	Status           string  `json:"status"`
	Summary          *string `json:"summary"`
	AgentError       *string `json:"agent_error"`
}

type SessionSummaryResponse struct {
	SessionID  int64   `json:"session_id"`
	Status     string  `json:"status"`
	Summary    *string `json:"summary"`
	AgentError *string `json:"agent_error"`
}

type UserProfileResponse struct {
	DiscordID           string  `json:"discord_id"`
	Username            string  `json:"username"`
	DisplayName         *string `json:"display_name"`
	AnthropologistTitle string  `json:"anthropologist_title"`
	Summary             string  `json:"summary"`
	Interests           string  `json:"interests"`
	CommunicationStyle  string  `json:"communication_style"`
	PersonaNotes        string  `json:"persona_notes"`
	RecentUpdates       string  `json:"recent_updates"`
	ProfileFileURL      *string `json:"google_doc_url"`
}

type TextMessageRequest struct {
	GuildID          string     `json:"guild_id"`
	ChannelID        string     `json:"channel_id"`
	ChannelName      string     `json:"channel_name"`
	DiscordMessageID string     `json:"discord_message_id"`
	DiscordID        string     `json:"discord_id"`
	Username         string     `json:"username"`
	DisplayName      string     `json:"display_name,omitempty"`
	Content          string     `json:"content"`
	Tstamp           time.Time  `json:"tstamp"`
	EditedAt         *time.Time `json:"edited_at,omitempty"`
}

type TextMessageResponse struct {
	Status    string `json:"status"`
	UserID    int64  `json:"user_id"`
	MessageID int64  `json:"message_id"`
}

type TextProfileSyncResponse struct {
	Status          string `json:"status"`
	UpdatedProfiles int64  `json:"updated_profiles"`
	ProcessingMS    int64  `json:"processing_ms"`
}

type ProfilePromptRequest struct {
	Question string `json:"question"`
}

type ProfilePromptResponse struct {
	DiscordID           string  `json:"discord_id"`
	Username            string  `json:"username"`
	DisplayName         *string `json:"display_name"`
	AnthropologistTitle string  `json:"anthropologist_title"`
	Question            string  `json:"question"`
	Answer              string  `json:"answer"`
}

func NewTranscriptionClientFromEnv() *TranscriptionClient {
	baseURL := transcriptionBaseURLFromEnv(os.Getenv("TRANSCRIPTION_API_URL"))
	client := &TranscriptionClient{
		baseURL:  baseURL,
		endpoint: baseURL + "/v1/transcriptions",
		httpClient: &http.Client{
			Timeout: transcriptionTimeoutFromEnv(os.Getenv("TRANSCRIPTION_API_TIMEOUT")),
		},
	}
	log.Printf("cliente API transcricao configurado endpoint=%s timeout=%s", client.endpoint, client.httpClient.Timeout)
	return client
}

func transcriptionBaseURLFromEnv(raw string) string {
	endpoint := strings.TrimSpace(raw)
	if endpoint == "" {
		endpoint = defaultTranscriptionAPIURL
	}

	endpoint = strings.TrimRight(endpoint, "/")
	if strings.HasSuffix(endpoint, "/v1/transcriptions") {
		return strings.TrimSuffix(endpoint, "/v1/transcriptions")
	}
	return endpoint
}

func transcriptionTimeoutFromEnv(raw string) time.Duration {
	value := strings.TrimSpace(raw)
	if value == "" {
		return defaultTranscriptionAPITimeout
	}

	timeout, err := time.ParseDuration(value)
	if err != nil {
		log.Printf("TRANSCRIPTION_API_TIMEOUT invalido (%q), a usar %s", value, defaultTranscriptionAPITimeout)
		return defaultTranscriptionAPITimeout
	}
	return timeout
}

func (c *TranscriptionClient) QueueTranscription(request TranscriptionRequest) {
	if c == nil {
		log.Printf("cliente API transcricao nil; pedido ignorado user=%s file=%s", request.DiscordID, request.AudioPath)
		return
	}

	request = request.withFallbacks()
	log.Printf(
		"chamada API transcricao enfileirada user=%s username=%s channel=%s file=%s started_at=%s",
		request.DiscordID,
		request.Username,
		request.ChannelName,
		request.AudioPath,
		request.RecordingStartedAt.UTC().Format(time.RFC3339Nano),
	)

	go func() {
		if err := c.SubmitTranscription(context.Background(), request); err != nil {
			log.Printf("erro ao chamar API de transcricao para user=%s file=%s: %v", request.DiscordID, request.AudioPath, err)
			return
		}
		log.Printf("pedido de transcricao aceite pela API para user=%s file=%s", request.DiscordID, request.AudioPath)
	}()
}

func (c *TranscriptionClient) SubmitTranscription(ctx context.Context, request TranscriptionRequest) error {
	if c == nil || c.httpClient == nil {
		return fmt.Errorf("cliente de transcricao nao configurado")
	}

	request = request.withFallbacks()
	started := time.Now()
	audioSize := int64(-1)
	if stat, err := os.Stat(request.AudioPath); err != nil {
		log.Printf("nao foi possivel obter tamanho do audio antes da API user=%s file=%s: %v", request.DiscordID, request.AudioPath, err)
	} else {
		audioSize = stat.Size()
	}

	log.Printf(
		"chamada API transcricao inicio endpoint=%s user=%s username=%s channel=%s file=%s recording_filename=%s bytes=%d",
		c.endpoint,
		request.DiscordID,
		request.Username,
		request.ChannelName,
		request.AudioPath,
		filepath.Base(request.AudioPath),
		audioSize,
	)

	form := url.Values{}
	form.Set("recording_filename", filepath.Base(request.AudioPath))
	form.Set("discord_id", request.DiscordID)
	form.Set("username", request.Username)
	form.Set("channel_name", request.ChannelName)
	form.Set("recording_started_at", request.RecordingStartedAt.UTC().Format(time.RFC3339Nano))
	if request.SessionID > 0 {
		form.Set("session_id", fmt.Sprintf("%d", request.SessionID))
	}
	if request.DisplayName != "" {
		form.Set("display_name", request.DisplayName)
	}

	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return err
	}
	httpRequest.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	response, err := c.httpClient.Do(httpRequest)
	if err != nil {
		log.Printf("chamada API transcricao erro user=%s file=%s elapsed=%s err=%v", request.DiscordID, request.AudioPath, time.Since(started).Round(time.Millisecond), err)
		return err
	}
	defer response.Body.Close()

	if response.StatusCode >= http.StatusOK && response.StatusCode < http.StatusMultipleChoices {
		_, _ = io.Copy(io.Discard, response.Body)
		log.Printf("chamada API transcricao sucesso user=%s file=%s status=%s elapsed=%s", request.DiscordID, request.AudioPath, response.Status, time.Since(started).Round(time.Millisecond))
		return nil
	}

	responseBody, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
	detail := strings.TrimSpace(string(responseBody))
	if detail == "" {
		detail = response.Status
	}
	log.Printf("chamada API transcricao falhou user=%s file=%s status=%s elapsed=%s body=%s", request.DiscordID, request.AudioPath, response.Status, time.Since(started).Round(time.Millisecond), detail)
	return fmt.Errorf("API devolveu %s: %s", response.Status, detail)
}

func (c *TranscriptionClient) CreateSession(ctx context.Context, request CreateSessionRequest) (*VoiceSessionResponse, error) {
	var session VoiceSessionResponse
	if err := c.postJSON(ctx, "/v1/sessions", request, &session); err != nil {
		return nil, err
	}
	return &session, nil
}

func (c *TranscriptionClient) FinishSession(ctx context.Context, sessionID int64) (*VoiceSessionResponse, error) {
	var session VoiceSessionResponse
	if err := c.postJSON(ctx, fmt.Sprintf("/v1/sessions/%d/finish", sessionID), map[string]string{
		"ended_at": time.Now().UTC().Format(time.RFC3339Nano),
	}, &session); err != nil {
		return nil, err
	}
	return &session, nil
}

func (c *TranscriptionClient) GetSessionSummary(ctx context.Context, sessionID int64) (*SessionSummaryResponse, error) {
	var summary SessionSummaryResponse
	if err := c.getJSON(ctx, fmt.Sprintf("/v1/sessions/%d/summary", sessionID), &summary); err != nil {
		return nil, err
	}
	return &summary, nil
}

func (c *TranscriptionClient) FinishSessionAndWait(ctx context.Context, sessionID int64) (*SessionSummaryResponse, error) {
	if sessionID <= 0 {
		return nil, nil
	}
	if _, err := c.FinishSession(ctx, sessionID); err != nil {
		return nil, err
	}

	timeout := summaryPollTimeoutFromEnv(os.Getenv("SESSION_SUMMARY_TIMEOUT"))
	deadline := time.Now().Add(timeout)
	for {
		summary, err := c.GetSessionSummary(ctx, sessionID)
		if err != nil {
			return nil, err
		}
		if summary.Status == "agent_done" || summary.Status == "agent_failed" {
			return summary, nil
		}
		if time.Now().After(deadline) {
			return summary, fmt.Errorf("timeout a espera do resumo da sessao %d; estado=%s", sessionID, summary.Status)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(5 * time.Second):
		}
	}
}

func (c *TranscriptionClient) GetUserProfile(ctx context.Context, discordID string) (*UserProfileResponse, error) {
	var profile UserProfileResponse
	if err := c.getJSON(ctx, "/v1/users/"+url.PathEscape(discordID)+"/profile", &profile); err != nil {
		return nil, err
	}
	return &profile, nil
}

func (c *TranscriptionClient) PromptUserProfile(ctx context.Context, discordID string, question string) (*ProfilePromptResponse, error) {
	var response ProfilePromptResponse
	request := ProfilePromptRequest{Question: strings.TrimSpace(question)}
	if err := c.postJSON(ctx, "/v1/users/"+url.PathEscape(discordID)+"/prompt", request, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) QueueTextMessage(request TextMessageRequest) {
	if c == nil {
		log.Printf("cliente API transcricao nil; mensagem de texto ignorada user=%s message=%s", request.DiscordID, request.DiscordMessageID)
		return
	}

	request = request.withFallbacks()
	go func() {
		if err := c.SubmitTextMessage(context.Background(), request); err != nil {
			log.Printf("erro ao guardar mensagem de texto user=%s message=%s: %v", request.DiscordID, request.DiscordMessageID, err)
		}
	}()
}

func (c *TranscriptionClient) SubmitTextMessage(ctx context.Context, request TextMessageRequest) error {
	request = request.withFallbacks()
	var response TextMessageResponse
	return c.postJSON(ctx, "/v1/messages", request, &response)
}

func (c *TranscriptionClient) ForceTextProfileSync(ctx context.Context) (*TextProfileSyncResponse, error) {
	var response TextProfileSyncResponse
	if err := c.postJSON(ctx, "/v1/text-profile-sync", map[string]string{}, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) postJSON(ctx context.Context, path string, body any, target any) error {
	payload, err := json.Marshal(body)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, strings.NewReader(string(payload)))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	return c.doJSON(request, target)
}

func (c *TranscriptionClient) getJSON(ctx context.Context, path string, target any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	return c.doJSON(request, target)
}

func (c *TranscriptionClient) doJSON(request *http.Request, target any) error {
	if c == nil || c.httpClient == nil {
		return fmt.Errorf("cliente de transcricao nao configurado")
	}

	response, err := c.httpClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()

	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return fmt.Errorf("API devolveu %s: %s", response.Status, strings.TrimSpace(string(body)))
	}
	return json.NewDecoder(response.Body).Decode(target)
}

func summaryPollTimeoutFromEnv(raw string) time.Duration {
	value := strings.TrimSpace(raw)
	if value == "" {
		return defaultSummaryPollTimeout
	}
	timeout, err := time.ParseDuration(value)
	if err != nil {
		log.Printf("SESSION_SUMMARY_TIMEOUT invalido (%q), a usar %s", value, defaultSummaryPollTimeout)
		return defaultSummaryPollTimeout
	}
	return timeout
}

func (r TranscriptionRequest) withFallbacks() TranscriptionRequest {
	r.AudioPath = strings.TrimSpace(r.AudioPath)
	r.DiscordID = strings.TrimSpace(r.DiscordID)
	r.Username = strings.TrimSpace(r.Username)
	r.DisplayName = strings.TrimSpace(r.DisplayName)
	r.ChannelName = strings.TrimSpace(r.ChannelName)

	if r.Username == "" {
		r.Username = r.DiscordID
	}
	if r.ChannelName == "" {
		r.ChannelName = "voice"
	}
	if r.RecordingStartedAt.IsZero() {
		r.RecordingStartedAt = time.Now().UTC()
	}

	return r
}

func (r TextMessageRequest) withFallbacks() TextMessageRequest {
	r.GuildID = strings.TrimSpace(r.GuildID)
	r.ChannelID = strings.TrimSpace(r.ChannelID)
	r.ChannelName = strings.TrimSpace(r.ChannelName)
	r.DiscordMessageID = strings.TrimSpace(r.DiscordMessageID)
	r.DiscordID = strings.TrimSpace(r.DiscordID)
	r.Username = strings.TrimSpace(r.Username)
	r.DisplayName = strings.TrimSpace(r.DisplayName)
	r.Content = strings.TrimSpace(r.Content)

	if r.Username == "" {
		r.Username = r.DiscordID
	}
	if r.ChannelName == "" {
		r.ChannelName = r.ChannelID
	}
	if r.Tstamp.IsZero() {
		r.Tstamp = time.Now().UTC()
	}

	return r
}
