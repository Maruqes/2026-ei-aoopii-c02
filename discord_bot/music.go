package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/bwmarrin/discordgo"
	"gopkg.in/hraban/opus.v2"
)

const musicQueueLimit = 20

var youtubeVideoID = regexp.MustCompile(`^[A-Za-z0-9_-]{11}$`)

var (
	ErrMusicInvalidURL     = errors.New("provide a HTTPS YouTube video link")
	ErrMusicQueueFull      = errors.New("music queue full (20 tracks)")
	ErrMusicUnavailable    = errors.New("YouTube audio unavailable")
	ErrMusicDuration       = errors.New("choose a video lasting between 1 second and 1 hour")
	ErrMusicLive           = errors.New("live streams and playlists are not supported")
	ErrMusicClosed         = errors.New("music player closed")
	ErrMusicNothingPlaying = errors.New("nothing is playing")
)

// MusicTrack contains public metadata only; signed CDN URLs never reach Discord.
type MusicTrack struct {
	Title           string
	URL             string
	Requester       string
	DurationSeconds float64
	streamURL       string
	resolvedAt      time.Time
}

type MusicSnapshot struct {
	Current *MusicTrack
	Queue   []MusicTrack
	Paused  bool
}

type MusicPlayer struct {
	mu          sync.Mutex
	vc          *discordgo.VoiceConnection
	ctx         context.Context
	cancel      context.CancelFunc
	done        chan struct{}
	changed     chan struct{}
	queue       []MusicTrack
	current     *MusicTrack
	trackStop   context.CancelFunc
	paused      bool
	closed      bool
	resolving   int
	voicing     int
	generation  uint64
	lookupCtx   context.Context
	lookupStop  context.CancelFunc
	onError     func(MusicTrack, error)
	resolve     func(context.Context, string, string) (MusicTrack, error)
	play        func(context.Context, MusicTrack) error
	deps        func() error
	output      chan struct{}
	resumeMusic bool // Protected by the output token.
	voiceStatus func(bool) error
}

func NewMusicPlayer(vc *discordgo.VoiceConnection, onError ...func(MusicTrack, error)) *MusicPlayer {
	p := newMusicPlayer(vc)
	if len(onError) > 0 {
		p.onError = onError[0]
	}
	go p.run()
	return p
}

// Keeping construction separate lets offline tests use deterministic audio sources.
func newMusicPlayer(vc *discordgo.VoiceConnection) *MusicPlayer {
	ctx, cancel := context.WithCancel(context.Background())
	p := &MusicPlayer{vc: vc, ctx: ctx, cancel: cancel, done: make(chan struct{}), changed: make(chan struct{}), output: make(chan struct{}, 1)}
	p.output <- struct{}{}
	p.voiceStatus = func(speaking bool) error {
		if p.vc == nil {
			return errors.New("Discord voice connection is unavailable")
		}
		return p.vc.Speaking(speaking)
	}
	p.lookupCtx, p.lookupStop = context.WithCancel(ctx)
	p.resolve, p.play = resolveYouTubeMusic, p.stream
	p.deps = func() error {
		for _, name := range []string{"yt-dlp", "ffmpeg", "node"} {
			if _, err := exec.LookPath(name); err != nil {
				return fmt.Errorf("%w: dependency %s is missing", ErrMusicUnavailable, name)
			}
		}
		return nil
	}
	return p
}

func canonicalYouTubeURL(raw string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" {
		return "", ErrMusicInvalidURL
	}
	host := strings.ToLower(u.Hostname())
	var id string
	switch host {
	case "youtu.be", "www.youtu.be":
		id = strings.TrimPrefix(u.Path, "/")
	case "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com":
		if u.Path == "/watch" {
			id = u.Query().Get("v")
		} else {
			parts := strings.Split(strings.Trim(u.Path, "/"), "/")
			if len(parts) == 2 && (parts[0] == "shorts" || parts[0] == "live") {
				id = parts[1]
			}
		}
	}
	if !youtubeVideoID.MatchString(id) {
		return "", ErrMusicInvalidURL
	}
	return "https://www.youtube.com/watch?v=" + id, nil
}

// musicCommand cancels the entire process group, including yt-dlp's JS runtime.
func musicCommand(ctx context.Context, name string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return os.ErrProcessDone
		}
		if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil {
			if errors.Is(err, syscall.ESRCH) {
				return os.ErrProcessDone
			}
			return err
		}
		return nil
	}
	cmd.WaitDelay = 2 * time.Second
	return cmd
}

// Isolate incidental downloader/converter files. Call cleanup only after Wait has
// reaped the process group, so cancellation cannot leave a writer behind.
func isolateMusicCommand(cmd *exec.Cmd) (func(), error) {
	directory, err := os.MkdirTemp("", "discord-youtube-")
	if err != nil {
		return nil, fmt.Errorf("create music workspace: %w", err)
	}
	cmd.Dir = directory
	env := os.Environ()
	for _, key := range []string{"TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME"} {
		filtered := env[:0]
		for _, entry := range env {
			if !strings.HasPrefix(entry, key+"=") {
				filtered = append(filtered, entry)
			}
		}
		env = append(filtered, key+"="+filepath.Join(directory, "temporary"))
	}
	if err := os.Mkdir(filepath.Join(directory, "temporary"), 0700); err != nil {
		_ = os.RemoveAll(directory)
		return nil, err
	}
	cmd.Env = env
	return func() {
		if err := os.RemoveAll(directory); err != nil {
			log.Printf("Could not remove music workspace %s: %v", directory, err)
		}
	}, nil
}

// yt-dlp metadata can be large; bound retained output without blocking its pipes.
type musicOutput struct{ bytes.Buffer }

func (b *musicOutput) Write(data []byte) (int, error) {
	n := len(data)
	if remaining := 8*1024*1024 - b.Len(); remaining > 0 {
		if len(data) > remaining {
			data = data[:remaining]
		}
		_, _ = b.Buffer.Write(data)
	}
	return n, nil
}

func resolveYouTubeMusic(parent context.Context, canonical, requester string) (MusicTrack, error) {
	ctx, cancel := context.WithTimeout(parent, 30*time.Second)
	defer cancel()
	cmd := musicCommand(ctx, "yt-dlp", "--ignore-config", "--no-plugin-dirs", "--js-runtimes", "node",
		"--no-playlist", "--no-cache-dir", "--no-progress", "--socket-timeout", "15", "--retries", "1",
		"--dump-single-json", "--skip-download", "-f", "bestaudio[protocol=https]", "--", canonical)
	cleanup, err := isolateMusicCommand(cmd)
	if err != nil {
		return MusicTrack{}, err
	}
	defer cleanup()
	var output musicOutput
	cmd.Stdout = &output
	// Provider stderr includes signed URLs and arbitrary remote text; never publish it.
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return MusicTrack{}, fmt.Errorf("YouTube lookup interrupted: %w", ctx.Err())
		}
		return MusicTrack{}, ErrMusicUnavailable
	}
	return parseYouTubeMusic(output.Bytes(), canonical, requester)
}

func parseYouTubeMusic(data []byte, canonical, requester string) (MusicTrack, error) {
	var info struct {
		Title      string  `json:"title"`
		URL        string  `json:"url"`
		Duration   float64 `json:"duration"`
		IsLive     bool    `json:"is_live"`
		LiveStatus string  `json:"live_status"`
		Type       string  `json:"_type"`
	}
	if err := json.Unmarshal(data, &info); err != nil {
		return MusicTrack{}, fmt.Errorf("%w: invalid metadata", ErrMusicUnavailable)
	}
	if info.IsLive || info.LiveStatus == "is_live" || info.LiveStatus == "is_upcoming" || info.Type == "playlist" {
		return MusicTrack{}, ErrMusicLive
	}
	if math.IsNaN(info.Duration) || math.IsInf(info.Duration, 0) || info.Duration < 1 || info.Duration > 3600 {
		return MusicTrack{}, ErrMusicDuration
	}
	u, err := url.Parse(info.URL)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" ||
		!(u.Hostname() == "googlevideo.com" || strings.HasSuffix(u.Hostname(), ".googlevideo.com")) {
		return MusicTrack{}, fmt.Errorf("%w: unsupported audio source", ErrMusicUnavailable)
	}
	title := strings.TrimSpace(info.Title)
	if title == "" {
		return MusicTrack{}, ErrMusicUnavailable
	}
	if chars := []rune(title); len(chars) > 200 {
		title = string(chars[:200])
	}
	return MusicTrack{Title: title, URL: canonical, Requester: requester, DurationSeconds: info.Duration,
		streamURL: info.URL, resolvedAt: time.Now()}, nil
}

func (p *MusicPlayer) notifyLocked() {
	close(p.changed)
	p.changed = make(chan struct{})
}

func (p *MusicPlayer) Enqueue(rawURL, requester string) (MusicTrack, int, error) {
	canonical, err := canonicalYouTubeURL(rawURL)
	if err != nil {
		return MusicTrack{}, 0, err
	}
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return MusicTrack{}, 0, ErrMusicClosed
	}
	if len(p.queue)+p.resolving >= musicQueueLimit {
		p.mu.Unlock()
		return MusicTrack{}, 0, ErrMusicQueueFull
	}
	p.resolving++
	lookupCtx, generation := p.lookupCtx, p.generation
	p.mu.Unlock()
	defer func() { p.mu.Lock(); p.resolving--; p.mu.Unlock() }()
	if err := p.deps(); err != nil {
		return MusicTrack{}, 0, err
	}
	track, err := p.resolve(lookupCtx, canonical, requester)
	if err != nil {
		return MusicTrack{}, 0, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed || generation != p.generation {
		return MusicTrack{}, 0, ErrMusicClosed
	}
	position := len(p.queue)
	if p.current != nil {
		position++
	}
	p.queue = append(p.queue, track)
	p.notifyLocked()
	return track, position, nil
}

func (p *MusicPlayer) Snapshot() MusicSnapshot {
	p.mu.Lock()
	defer p.mu.Unlock()
	snapshot := MusicSnapshot{Queue: append([]MusicTrack(nil), p.queue...), Paused: p.paused}
	if p.current != nil {
		track := *p.current
		snapshot.Current = &track
	}
	return snapshot
}

func (p *MusicPlayer) IsBusy() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.current != nil || len(p.queue) > 0 || p.resolving > 0 || p.voicing > 0
}

func (p *MusicPlayer) PauseToggle() (bool, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.current == nil || p.closed {
		return false, ErrMusicNothingPlaying
	}
	p.paused = !p.paused
	p.notifyLocked()
	return p.paused, nil
}

func (p *MusicPlayer) Skip() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.trackStop == nil {
		return false
	}
	p.trackStop()
	p.paused = false
	p.notifyLocked()
	return true
}

func (p *MusicPlayer) Clear() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.generation++
	p.lookupStop()
	p.lookupCtx, p.lookupStop = context.WithCancel(p.ctx)
	p.queue = nil
	p.paused = false
	if p.trackStop != nil {
		p.trackStop()
	}
	p.notifyLocked()
}

func (p *MusicPlayer) Close() {
	p.mu.Lock()
	p.closed = true
	p.queue = nil
	p.cancel()
	p.notifyLocked()
	p.mu.Unlock()
	<-p.done
}

func (p *MusicPlayer) run() {
	defer close(p.done)
	for {
		p.mu.Lock()
		if p.closed {
			p.mu.Unlock()
			return
		}
		if len(p.queue) == 0 {
			changed := p.changed
			p.mu.Unlock()
			select {
			case <-p.ctx.Done():
				return
			case <-changed:
				continue
			}
		}
		track := p.queue[0]
		p.queue = p.queue[1:]
		ctx, cancel := context.WithCancel(p.ctx)
		p.current, p.trackStop = &track, cancel
		p.mu.Unlock()
		err := p.play(ctx, track)
		interrupted := ctx.Err() != nil
		cancel()
		p.mu.Lock()
		p.current, p.trackStop, p.paused = nil, nil, false
		p.mu.Unlock()
		if err != nil && !interrupted && p.onError != nil {
			p.onError(track, err)
		}
	}
}

func (p *MusicPlayer) waitUnpaused(ctx context.Context) error {
	for {
		p.mu.Lock()
		paused, changed := p.paused, p.changed
		p.mu.Unlock()
		if !paused {
			return ctx.Err()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}

func (p *MusicPlayer) stream(ctx context.Context, track MusicTrack) error {
	if p.vc == nil || p.vc.OpusSend == nil {
		return errors.New("Discord voice connection is unavailable")
	}
	// Signed URLs can expire while waiting in the queue.
	if time.Since(track.resolvedAt) > 5*time.Minute {
		fresh, err := p.resolve(ctx, track.URL, track.Requester)
		if err != nil {
			return err
		}
		track = fresh
	}
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppAudio)
	if err != nil {
		return fmt.Errorf("create music encoder: %w", err)
	}
	if err := encoder.SetBitrate(96000); err != nil {
		return fmt.Errorf("configure music encoder: %w", err)
	}
	streamCtx, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	cmd := musicCommand(streamCtx, "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
		"-rw_timeout", "15000000", "-protocol_whitelist", "https,tls,tcp", "-i", track.streamURL,
		"-vn", "-t", "3600", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1")
	cleanup, err := isolateMusicCommand(cmd)
	if err != nil {
		return err
	}
	defer cleanup()
	cmd.Stderr = io.Discard
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return errors.New("could not open music stream")
	}
	if err := cmd.Start(); err != nil {
		return errors.New("could not start ffmpeg")
	}
	defer func() { stop(nil); _ = stdout.Close(); _ = cmd.Wait() }()
	if err := p.musicSpeaking(ctx, true); err != nil {
		return errors.New("could not activate Discord voice playback")
	}
	defer p.musicSpeaking(p.ctx, false)
	if err := p.sendPCM(streamCtx, stop, stdout, encoder); err != nil {
		return err
	}
	if err := cmd.Wait(); err != nil {
		return ErrMusicUnavailable
	}
	return nil
}

func (p *MusicPlayer) sendPCM(ctx context.Context, stop context.CancelCauseFunc, input io.Reader, encoder *opus.Encoder) error {
	return p.sendAudioPCM(ctx, stop, input, encoder, true)
}

func (p *MusicPlayer) sendAudioPCM(ctx context.Context, stop context.CancelCauseFunc, input io.Reader, encoder *opus.Encoder, music bool) error {
	pcmBytes := make([]byte, defaultOpusFrameSamples*channels*2)
	pcm := make([]int16, defaultOpusFrameSamples*channels)
	encoded := make([]byte, 4000)
	readTimeout := 30 * time.Second
	for {
		if music {
			if err := p.waitUnpaused(ctx); err != nil {
				return err
			}
		}
		if ctx.Err() != nil {
			return context.Cause(ctx)
		}
		watchdog := time.AfterFunc(readTimeout, func() { stop(errors.New("music stream stopped delivering audio")) })
		n, readErr := io.ReadFull(input, pcmBytes)
		watchdog.Stop()
		if ctx.Err() != nil {
			return context.Cause(ctx)
		}
		if n == 0 && readErr == io.EOF {
			return nil
		}
		if readErr != nil && readErr != io.ErrUnexpectedEOF {
			return errors.New("could not read music audio")
		}
		clear(pcmBytes[n:])
		for i := range pcm {
			pcm[i] = int16(binary.LittleEndian.Uint16(pcmBytes[2*i:]))
		}
		size, err := encoder.Encode(pcm, encoded)
		if err != nil {
			return errors.New("could not encode music audio")
		}
		packet := append([]byte(nil), encoded[:size]...)
		if music {
			if err := p.acquireMusicOutput(ctx); err != nil {
				return err
			}
			if p.resumeMusic {
				if err := p.voiceStatus(true); err != nil {
					p.releaseOutput()
					return err
				}
				p.resumeMusic = false
			}
		}
		err = p.sendAudioPacket(ctx, packet)
		if music {
			p.releaseOutput()
		}
		if err != nil {
			return err
		}
		if readErr == io.ErrUnexpectedEOF {
			return nil
		}
		readTimeout = 15 * time.Second
	}
}

func (p *MusicPlayer) acquireOutput(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-p.output:
		if ctx.Err() != nil {
			p.releaseOutput()
			return ctx.Err()
		}
		return nil
	}
}

func (p *MusicPlayer) releaseOutput() { p.output <- struct{}{} }

func (p *MusicPlayer) acquireMusicOutput(ctx context.Context) error {
	for {
		if err := p.waitUnpaused(ctx); err != nil {
			return err
		}
		if err := p.acquireOutput(ctx); err != nil {
			return err
		}
		p.mu.Lock()
		paused := p.paused
		p.mu.Unlock()
		if !paused {
			return nil
		}
		p.releaseOutput()
	}
}

func (p *MusicPlayer) musicSpeaking(ctx context.Context, speaking bool) error {
	if err := p.acquireOutput(ctx); err != nil {
		return err
	}
	defer p.releaseOutput()
	p.resumeMusic = false
	return p.voiceStatus(speaking)
}

func (p *MusicPlayer) sendAudioPacket(ctx context.Context, packet []byte) error {
	sendTimer := time.NewTimer(15 * time.Second)
	defer sendTimer.Stop()
	select {
	case <-ctx.Done():
		return context.Cause(ctx)
	case <-sendTimer.C:
		return errors.New("Discord voice connection stopped accepting audio")
	case p.vc.OpusSend <- packet:
	}
	// Pace at 20ms, including the music frame before handing output to speech.
	frameTimer := time.NewTimer(20 * time.Millisecond)
	defer frameTimer.Stop()
	select {
	case <-ctx.Done():
		return context.Cause(ctx)
	case <-frameTimer.C:
		return nil
	}
}
