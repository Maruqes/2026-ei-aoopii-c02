package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	defaultTranscriptionAPIURL = "http://localhost:8000"
	defaultSummaryPollInterval = 5 * time.Second
)

type TranscriptionClient struct {
	baseURL         string
	endpoint        string
	httpClient      *http.Client
	chatgptAdminKey string
	submissionMu    sync.Mutex
	submissions     map[int64]*sessionSubmissions
	inflight        map[string]bool
	userSubmissions map[string]*sessionSubmissions
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

type LLMModelsResponse struct {
	Provider     string   `json:"provider"`
	CurrentModel string   `json:"current_model"`
	Models       []string `json:"models"`
}

type SelectLLMModelRequest struct {
	Model string `json:"model"`
}

type SelectLLMModelResponse struct {
	Provider     string `json:"provider"`
	Model        string `json:"model"`
	TestResponse string `json:"test_response"`
}

type ProfilePromptRequest struct {
	Question string `json:"question"`
	Language string `json:"language,omitempty"`
}

type ProfilePromptResponse struct {
	DiscordID           string  `json:"discord_id"`
	Username            string  `json:"username"`
	DisplayName         *string `json:"display_name"`
	AnthropologistTitle string  `json:"anthropologist_title"`
	Question            string  `json:"question"`
	Answer              string  `json:"answer"`
}

type HealthResponse struct {
	SessionsPending        int        `json:"sessions_pending"`
	SessionsFailed         int        `json:"sessions_failed"`
	VoiceProfilesPending   int        `json:"voice_profiles_pending"`
	VoiceProfilesFailed    int        `json:"voice_profiles_failed"`
	LLMProvider            string     `json:"llm_provider"`
	LLMModel               string     `json:"llm_model"`
	Status                 string     `json:"status"`
	Database               string     `json:"database"`
	RecordingsTranscribing int        `json:"recordings_transcribing"`
	RecordingsFailed       int        `json:"recordings_failed"`
	RecordingsCompleted    int        `json:"recordings_completed"`
	LastRecordingStatus    *string    `json:"last_recording_status"`
	LastRecordingFilename  *string    `json:"last_recording_filename"`
	LastRecordingAt        *time.Time `json:"last_recording_at"`
}

type SpeechmaticsKeyUsageResponse struct {
	Name            string   `json:"name"`
	ReportedHours   *float64 `json:"reported_hours"`
	LocalTodayHours float64  `json:"local_today_hours"`
	UsedHours       *float64 `json:"used_hours"`
	LimitHours      float64  `json:"limit_hours"`
	PercentUsed     *float64 `json:"percent_used"`
	JobCount        *int     `json:"job_count"`
	Since           *string  `json:"since"`
	Until           *string  `json:"until"`
	Error           *string  `json:"error"`
}

type SpeechmaticsKeysResponse struct {
	Provider    string                         `json:"provider"`
	UsageNote   string                         `json:"usage_note"`
	LimitHours  float64                        `json:"limit_hours"`
	SelectedKey *string                        `json:"selected_key"`
	Keys        []SpeechmaticsKeyUsageResponse `json:"keys"`
}

type ForgetUserResponse struct {
	Status          string `json:"status"`
	DiscordID       string `json:"discord_id"`
	MessagesDeleted int    `json:"messages_deleted"`
	LoreFileDeleted bool   `json:"lore_file_deleted"`
}

type GuildOracleRequest struct {
	Question string `json:"question"`
	Language string `json:"language,omitempty"`
}

type GuildOracleResponse struct {
	GuildID  string `json:"guild_id"`
	Question string `json:"question"`
	Answer   string `json:"answer"`
}

type GuessResponse struct {
	Quote              string   `json:"quote"`
	Options            []string `json:"options"`
	CorrectDiscordID   string   `json:"correct_discord_id"`
	CorrectDisplayName string   `json:"correct_display_name"`
	SessionID          *int64   `json:"session_id"`
	ChannelName        *string  `json:"channel_name"`
}

type SessionRecapResponse struct {
	SessionID   int64      `json:"session_id"`
	GuildID     string     `json:"guild_id"`
	ChannelName string     `json:"channel_name"`
	StartedAt   time.Time  `json:"started_at"`
	EndedAt     *time.Time `json:"ended_at"`
	Status      string     `json:"status"`
	RecapSource string     `json:"recap_source"`
	Recap       string     `json:"recap"`
	AgentError  *string    `json:"agent_error"`
}

func NewTranscriptionClientFromEnv() *TranscriptionClient {
	baseURL := transcriptionBaseURLFromEnv(os.Getenv("TRANSCRIPTION_API_URL"))
	client := &TranscriptionClient{
		baseURL:         baseURL,
		endpoint:        baseURL + "/v1/transcriptions",
		httpClient:      &http.Client{Timeout: apiRequestTimeoutFromEnv(os.Getenv("API_REQUEST_TIMEOUT"))},
		chatgptAdminKey: strings.TrimSpace(os.Getenv("CHATGPT_ADMIN_PASSWORD")),
	}
	log.Printf("cliente API transcricao configurado endpoint=%s", client.endpoint)
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

	path := transcriptionOutboxPath(request)
	c.submissionMu.Lock()
	if c.inflight == nil {
		c.inflight = make(map[string]bool)
	}
	if c.inflight[path] {
		c.submissionMu.Unlock()
		return
	}
	c.inflight[path] = true
	group := c.submissionGroupLocked(request.SessionID)
	group.pending++
	group.notify()
	userGroup := c.userSubmissionGroupLocked(request.DiscordID)
	userGroup.pending++
	userGroup.notify()
	c.submissionMu.Unlock()
	if err := persistTranscription(request); err != nil {
		log.Printf("Could not persist transcription outbox file=%s: %v", request.AudioPath, err)
	}
	if request.SessionID > 0 {
		if err := persistSessionFinish(request.SessionID, currentBotLanguage().apiValue()); err != nil {
			log.Printf("Could not persist session recovery marker id=%d: %v", request.SessionID, err)
		}
	}
	go func() {
		var submissionErr error
		defer func() {
			c.submissionMu.Lock()
			defer c.submissionMu.Unlock()
			delete(c.inflight, path)
			userGroup.pending--
			userGroup.notify()
			group.pending--
			if submissionErr != nil {
				group.errors[path] = submissionErr
			} else {
				delete(group.errors, path)
			}
			group.notify()
		}()
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		defer cancel()
		err := retryAPI(ctx, func() error { return c.SubmitTranscription(ctx, request) })
		if err != nil {
			submissionErr = err
			log.Printf("Transcription not accepted; retained in outbox file=%s: %v", request.AudioPath, err)
			return
		}
		_ = os.Remove(transcriptionOutboxPath(request))
		log.Printf("Transcription accepted file=%s", request.AudioPath)
	}()
}

func (c *TranscriptionClient) SubmitTranscription(ctx context.Context, request TranscriptionRequest) error {
	if c == nil || c.httpClient == nil {
		return fmt.Errorf("cliente de transcricao nao configurado")
	}

	request = request.withFallbacks()
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
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
	return &APIError{StatusCode: response.StatusCode, Detail: detail}
}

func (c *TranscriptionClient) CreateSession(ctx context.Context, request CreateSessionRequest) (*VoiceSessionResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	var session VoiceSessionResponse
	if err := c.postJSON(ctx, "/v1/sessions", request, &session); err != nil {
		return nil, err
	}
	return &session, nil
}

func (c *TranscriptionClient) FinishSession(ctx context.Context, sessionID int64, language string) (*VoiceSessionResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	var session VoiceSessionResponse
	request := map[string]string{
		"ended_at": time.Now().UTC().Format(time.RFC3339Nano),
	}
	if strings.TrimSpace(language) != "" {
		request["language"] = strings.TrimSpace(language)
	}
	if err := c.postJSON(ctx, fmt.Sprintf("/v1/sessions/%d/finish", sessionID), request, &session); err != nil {
		return nil, err
	}
	_ = os.Remove(sessionFinishPath(sessionID))
	c.submissionMu.Lock()
	if group := c.submissions[sessionID]; group != nil && group.pending == 0 && len(group.errors) == 0 {
		delete(c.submissions, sessionID)
	}
	c.submissionMu.Unlock()
	return &session, nil
}

func (c *TranscriptionClient) GetSessionSummary(ctx context.Context, sessionID int64) (*SessionSummaryResponse, error) {
	var summary SessionSummaryResponse
	if err := c.getJSON(ctx, fmt.Sprintf("/v1/sessions/%d/summary", sessionID), &summary); err != nil {
		return nil, err
	}
	return &summary, nil
}

func (c *TranscriptionClient) FinishSessionAndWait(ctx context.Context, sessionID int64, language string) (*SessionSummaryResponse, error) {
	if sessionID <= 0 {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(ctx, summaryWaitTimeoutFromEnv(os.Getenv("SESSION_SUMMARY_TIMEOUT")))
	defer cancel()
	if err := persistSessionFinish(sessionID, language); err != nil {
		log.Printf("Could not persist session recovery marker id=%d: %v", sessionID, err)
	}
	if err := c.waitForSubmissions(ctx, sessionID); err != nil {
		return nil, err
	}
	if err := retryAPI(ctx, func() error { _, err := c.FinishSession(ctx, sessionID, language); return err }); err != nil {
		return nil, err
	}

	interval := summaryPollIntervalFromEnv(os.Getenv("SESSION_SUMMARY_POLL_INTERVAL"))
	for {
		summary, err := c.GetSessionSummary(ctx, sessionID)
		if err != nil && !retryableAPIError(err) {
			return nil, err
		}
		if err == nil && (summary.Status == "agent_done" || summary.Status == "agent_failed") {
			return summary, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(interval):
		}
	}
}

func (c *TranscriptionClient) GetHealth(ctx context.Context) (*HealthResponse, error) {
	var response HealthResponse
	if err := c.getJSON(ctx, "/health", &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) GetSpeechmaticsKeys(ctx context.Context) (*SpeechmaticsKeysResponse, error) {
	var response SpeechmaticsKeysResponse
	if err := c.getJSON(ctx, "/v1/speechmatics/keys", &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) GetGuildRecap(ctx context.Context, guildID string, sessionID int64) (*SessionRecapResponse, error) {
	path := "/v1/guilds/" + url.PathEscape(guildID) + "/recap"
	if sessionID > 0 {
		path += "?session_id=" + url.QueryEscape(fmt.Sprintf("%d", sessionID))
	}
	var response SessionRecapResponse
	if err := c.getJSON(ctx, path, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) GetGuildGuess(ctx context.Context, guildID string) (*GuessResponse, error) {
	var response GuessResponse
	path := "/v1/guilds/" + url.PathEscape(guildID) + "/guess"
	if err := c.getJSON(ctx, path, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) AskGuildOracle(ctx context.Context, guildID string, question string, language string) (*GuildOracleResponse, error) {
	var response GuildOracleResponse
	request := GuildOracleRequest{Question: strings.TrimSpace(question), Language: strings.TrimSpace(language)}
	path := "/v1/guilds/" + url.PathEscape(guildID) + "/oracle"
	if err := c.postJSON(ctx, path, request, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) ForgetUser(ctx context.Context, discordID string) (*ForgetUserResponse, error) {
	var response ForgetUserResponse
	if err := c.deleteJSON(ctx, "/v1/users/"+url.PathEscape(discordID), &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) GetUserProfile(ctx context.Context, discordID string) (*UserProfileResponse, error) {
	var profile UserProfileResponse
	if err := c.getJSON(ctx, "/v1/users/"+url.PathEscape(discordID)+"/profile", &profile); err != nil {
		return nil, err
	}
	return &profile, nil
}

func (c *TranscriptionClient) PromptUserProfile(ctx context.Context, discordID string, question string, language string) (*ProfilePromptResponse, error) {
	var response ProfilePromptResponse
	request := ProfilePromptRequest{Question: strings.TrimSpace(question), Language: strings.TrimSpace(language)}
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
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := retryAPI(ctx, func() error { return c.SubmitTextMessage(ctx, request) }); err != nil {
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

func (c *TranscriptionClient) GetLLMModels(ctx context.Context) (*LLMModelsResponse, error) {
	var response LLMModelsResponse
	if err := c.getJSON(ctx, "/v1/models", &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *TranscriptionClient) SelectLLMModel(ctx context.Context, model string) (*SelectLLMModelResponse, error) {
	var response SelectLLMModelResponse
	request := SelectLLMModelRequest{Model: strings.TrimSpace(model)}
	if err := c.postJSON(ctx, "/v1/models/current", request, &response); err != nil {
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
	if path == "/v1/models/current" && c.chatgptAdminKey != "" {
		request.Header.Set("X-ChatGPT-Admin-Key", c.chatgptAdminKey)
	}
	return c.doJSON(request, target)
}

func (c *TranscriptionClient) getJSON(ctx context.Context, path string, target any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	return c.doJSON(request, target)
}

func (c *TranscriptionClient) deleteJSON(ctx context.Context, path string, target any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodDelete, c.baseURL+path, nil)
	if err != nil {
		return err
	}
	return c.doJSON(request, target)
}

func (c *TranscriptionClient) doJSON(request *http.Request, target any) error {
	if c == nil || c.httpClient == nil {
		return fmt.Errorf("cliente de transcricao nao configurado")
	}

	timeout := apiRequestTimeoutFromEnv(os.Getenv("API_REQUEST_TIMEOUT"))
	if request.Method == http.MethodGet && timeout > 30*time.Second {
		timeout = 30 * time.Second
	}
	ctx, cancel := context.WithTimeout(request.Context(), timeout)
	defer cancel()
	request = request.Clone(ctx)
	response, err := c.httpClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()

	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return &APIError{StatusCode: response.StatusCode, Detail: strings.TrimSpace(string(body))}
	}
	return json.NewDecoder(io.LimitReader(response.Body, 4<<20)).Decode(target)
}

func summaryPollIntervalFromEnv(raw string) time.Duration {
	value := strings.TrimSpace(raw)
	if value == "" {
		return defaultSummaryPollInterval
	}
	interval, err := time.ParseDuration(value)
	if err != nil || interval <= 0 {
		log.Printf("SESSION_SUMMARY_POLL_INTERVAL invalido (%q), a usar %s", value, defaultSummaryPollInterval)
		return defaultSummaryPollInterval
	}
	return interval
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

type APIError struct {
	StatusCode int
	Detail     string
}

func (e *APIError) Error() string { return fmt.Sprintf("API HTTP %d: %s", e.StatusCode, e.Detail) }
func retryableAPIError(err error) bool {
	var api *APIError
	if errors.As(err, &api) {
		return api.StatusCode == 408 || api.StatusCode == 429 || api.StatusCode >= 500
	}
	var netErr net.Error
	return errors.As(err, &netErr)
}
func retryAPI(ctx context.Context, call func() error) error {
	var err error
	for attempt := 0; attempt < 3; attempt++ {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err = call(); err == nil || !retryableAPIError(err) {
			return err
		}
		if attempt == 2 {
			break
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(1<<attempt) * time.Second):
		}
	}
	return err
}
func apiRequestTimeoutFromEnv(raw string) time.Duration {
	if duration, err := time.ParseDuration(strings.TrimSpace(raw)); err == nil && duration > 0 && duration <= 15*time.Minute {
		return duration
	}
	return 5 * time.Minute
}

func summaryWaitTimeoutFromEnv(raw string) time.Duration {
	if duration, err := time.ParseDuration(strings.TrimSpace(raw)); err == nil && duration > 0 {
		return duration
	}
	return 30 * time.Minute
}

// A change channel makes waiting cancellable without a goroutine parked on a WaitGroup.
// Tracking failures per file lets a successful replay clear only its own failure.
type sessionSubmissions struct {
	pending int
	errors  map[string]error
	changed chan struct{}
}

func (group *sessionSubmissions) notify() {
	close(group.changed)
	group.changed = make(chan struct{})
}

func (c *TranscriptionClient) submissionGroupLocked(sessionID int64) *sessionSubmissions {
	if c.submissions == nil {
		c.submissions = make(map[int64]*sessionSubmissions)
	}
	group := c.submissions[sessionID]
	if group == nil {
		group = &sessionSubmissions{errors: make(map[string]error), changed: make(chan struct{})}
		c.submissions[sessionID] = group
	}
	return group
}

func (c *TranscriptionClient) waitForSubmissions(ctx context.Context, sessionID int64) error {
	for {
		c.submissionMu.Lock()
		group := c.submissionGroupLocked(sessionID)
		if group.pending == 0 {
			var failures []error
			for _, err := range group.errors {
				failures = append(failures, err)
			}
			c.submissionMu.Unlock()
			if len(failures) > 0 {
				return fmt.Errorf("recording submission failed: %w", errors.Join(failures...))
			}
			return nil
		}
		changed := group.changed
		c.submissionMu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}
func transcriptionOutboxPath(request TranscriptionRequest) string {
	return request.AudioPath + ".request.json"
}
func persistTranscription(request TranscriptionRequest) error {
	return persistJSONAtomically(transcriptionOutboxPath(request), request)
}

func persistJSONAtomically(path string, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".outbox-*.tmp")
	if err != nil {
		return err
	}
	temp := file.Name()
	defer os.Remove(temp)
	if _, err = file.Write(payload); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err := os.Rename(temp, path); err != nil {
		return err
	}
	// Persist the directory entry too, so an acknowledged write survives a crash.
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

type sessionFinishRequest struct {
	SessionID int64  `json:"session_id"`
	Language  string `json:"language"`
}

func sessionFinishPath(sessionID int64) string {
	return filepath.Join(recordingsDirFromEnv(), fmt.Sprintf("session-%d.finish.json", sessionID))
}

func persistSessionFinish(sessionID int64, language string) error {
	if err := os.MkdirAll(recordingsDirFromEnv(), 0755); err != nil {
		return err
	}
	return persistJSONAtomically(sessionFinishPath(sessionID), sessionFinishRequest{sessionID, language})
}

func (c *TranscriptionClient) ReplayTranscriptions() {
	paths, err := filepath.Glob(filepath.Join(recordingsDirFromEnv(), "*.request.json"))
	if err != nil {
		return
	}
	sessions := make(map[int64]string)
	finishPaths, _ := filepath.Glob(filepath.Join(recordingsDirFromEnv(), "session-*.finish.json"))
	for _, path := range finishPaths {
		payload, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var request sessionFinishRequest
		if err := json.Unmarshal(payload, &request); err == nil && request.SessionID > 0 {
			sessions[request.SessionID] = request.Language
		}
	}
	for _, path := range paths {
		payload, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var request TranscriptionRequest
		if err := json.Unmarshal(payload, &request); err != nil {
			log.Printf("Invalid outbox file=%s", path)
			continue
		}
		c.QueueTranscription(request)
		if request.SessionID > 0 {
			if _, exists := sessions[request.SessionID]; !exists {
				sessions[request.SessionID] = currentBotLanguage().apiValue()
			}
		}
	}
	// These are recordings from a previous bot process; finalize after their admission.
	for sessionID, language := range sessions {
		go func(id int64, lang string) {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
			defer cancel()
			if err := c.waitForSubmissions(ctx, id); err == nil {
				err := retryAPI(ctx, func() error { _, err := c.FinishSession(ctx, id, lang); return err })
				if err != nil {
					log.Printf("Could not finalize recovered session id=%d: %v", id, err)
				}
			}
		}(sessionID, language)
	}
}

type TextDigestResponse struct {
	Summary      string `json:"summary"`
	MessageCount int    `json:"message_count"`
	Limited      bool   `json:"limited"`
	Hours        int    `json:"hours"`
}

func (c *TranscriptionClient) GetTextDigest(ctx context.Context, guildID, channelID string, hours int, language string) (*TextDigestResponse, error) {
	var result TextDigestResponse
	body := map[string]any{"hours": hours, "channel_id": channelID, "language": language}
	err := c.postJSON(ctx, "/v1/guilds/"+url.PathEscape(guildID)+"/digest", body, &result)
	return &result, err
}
func (c *TranscriptionClient) RetrySession(ctx context.Context, guildID string, sessionID int64) error {
	// Recover bot-to-API admission failures too; these files do not yet exist
	// in the backend's recording queue and cannot be retried there.
	paths, err := filepath.Glob(filepath.Join(recordingsDirFromEnv(), "*.request.json"))
	if err != nil {
		return err
	}
	for _, path := range paths {
		payload, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var request TranscriptionRequest
		if err := json.Unmarshal(payload, &request); err == nil && request.SessionID == sessionID {
			c.QueueTranscription(request)
		}
	}
	if err := c.waitForSubmissions(ctx, sessionID); err != nil {
		return err
	}
	var result SessionSummaryResponse
	return c.postJSON(ctx, fmt.Sprintf("/v1/guilds/%s/sessions/%d/retry", url.PathEscape(guildID), sessionID), map[string]string{}, &result)
}
