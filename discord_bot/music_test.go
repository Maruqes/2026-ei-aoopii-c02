package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"os"
	"os/exec"
	"sync"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
	"gopkg.in/hraban/opus.v2"
)

const testMusicURL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

func TestMusicYouTubeURLCanonicalization(t *testing.T) {
	for _, raw := range []string{
		testMusicURL + "&list=PLsomething&t=45",
		"https://youtu.be/dQw4w9WgXcQ?si=tracking",
		"https://www.youtube.com/shorts/dQw4w9WgXcQ",
		"https://m.youtube.com/live/dQw4w9WgXcQ",
		"https://music.youtube.com/watch?v=dQw4w9WgXcQ",
	} {
		got, err := canonicalYouTubeURL(raw)
		if err != nil || got != testMusicURL {
			t.Fatalf("%q canonicalized to %q: %v", raw, got, err)
		}
	}
	for _, raw := range []string{
		"http://youtu.be/dQw4w9WgXcQ", "file:///etc/passwd", "--exec=touch",
		"https://youtube.com.evil.test/watch?v=dQw4w9WgXcQ", "https://youtube.com@localhost/watch?v=dQw4w9WgXcQ",
		"https://youtu.be:443/dQw4w9WgXcQ", "https://youtube.com/playlist?list=abc",
		"https://youtube.com/watch?v=short", "https://youtu.be/dQw4w9WgXcQ/extra",
	} {
		if _, err := canonicalYouTubeURL(raw); !errors.Is(err, ErrMusicInvalidURL) {
			t.Fatalf("unsafe URL accepted: %q (%v)", raw, err)
		}
	}
}

func TestMusicMetadataRejectsLiveOversizedAndUnsafeStreams(t *testing.T) {
	good := `{"title":"A song","url":"https://rr1---sn.googlevideo.com/videoplayback?token=secret","duration":180}`
	track, err := parseYouTubeMusic([]byte(good), testMusicURL, "123")
	if err != nil || track.Title != "A song" || track.Requester != "123" || track.DurationSeconds != 180 {
		t.Fatalf("valid metadata rejected: %#v, %v", track, err)
	}
	cases := []struct {
		metadata string
		want     error
	}{
		{`{"title":"live","duration":10,"is_live":true}`, ErrMusicLive},
		{`{"title":"future","duration":10,"live_status":"is_upcoming"}`, ErrMusicLive},
		{`{"title":"playlist","duration":10,"_type":"playlist"}`, ErrMusicLive},
		{`{"title":"long","duration":3601}`, ErrMusicDuration},
		{`{"title":"unknown"}`, ErrMusicDuration},
		{`{"title":"bad","duration":10,"url":"http://googlevideo.com/audio"}`, ErrMusicUnavailable},
		{`{"title":"bad","duration":10,"url":"https://googlevideo.com.evil.test/audio"}`, ErrMusicUnavailable},
		{`{"title":"bad","duration":10,"url":"https://localhost/audio"}`, ErrMusicUnavailable},
		{`{"title":"bad","duration":10,"url":"file:///etc/passwd"}`, ErrMusicUnavailable},
		{`{"title":"bad","duration":10,"url":"https://googlevideo.com:1234/audio"}`, ErrMusicUnavailable},
		{`{"title":"bad","duration":10,"url":"https://user@googlevideo.com/audio"}`, ErrMusicUnavailable},
		{`not json`, ErrMusicUnavailable},
	}
	for _, tc := range cases {
		if _, err := parseYouTubeMusic([]byte(tc.metadata), testMusicURL, "123"); !errors.Is(err, tc.want) {
			t.Fatalf("metadata %s: wanted %v, got %v", tc.metadata, tc.want, err)
		}
	}
}

func offlineMusicPlayer(t *testing.T, play func(context.Context, MusicTrack) error) *MusicPlayer {
	t.Helper()
	p := newMusicPlayer(nil)
	p.deps = func() error { return nil }
	p.resolve = func(ctx context.Context, url, requester string) (MusicTrack, error) {
		return MusicTrack{URL: url, Title: requester, Requester: requester, DurationSeconds: 10}, ctx.Err()
	}
	p.play = play
	go p.run()
	t.Cleanup(p.Close)
	return p
}

func waitMusicCondition(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !condition() {
		if time.Now().After(deadline) {
			t.Fatal("music state did not settle")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestMusicQueuePauseSkipAndSnapshot(t *testing.T) {
	started := make(chan string, 3)
	p := offlineMusicPlayer(t, func(ctx context.Context, track MusicTrack) error {
		started <- track.Requester
		<-ctx.Done()
		return ctx.Err()
	})
	if _, position, err := p.Enqueue(testMusicURL, "first"); err != nil || position != 0 {
		t.Fatalf("initial enqueue: %d %v", position, err)
	}
	if first := <-started; first != "first" {
		t.Fatal(first)
	}
	if _, position, err := p.Enqueue(testMusicURL, "second"); err != nil || position != 1 {
		t.Fatalf("queued enqueue: %d %v", position, err)
	}
	paused, err := p.PauseToggle()
	if !paused || err != nil || !p.Snapshot().Paused {
		t.Fatal("pause was not persisted", err)
	}
	snapshot := p.Snapshot()
	snapshot.Current.Title, snapshot.Queue[0].Title = "changed", "changed"
	if fresh := p.Snapshot(); fresh.Current.Title != "first" || fresh.Queue[0].Title != "second" {
		t.Fatal("snapshot aliases the internal queue")
	}
	if !p.Skip() {
		t.Fatal("skip failed")
	}
	if second := <-started; second != "second" {
		t.Fatal(second)
	}
	if p.Snapshot().Paused {
		t.Fatal("skip carried pause into next track")
	}
	p.Clear()
	waitMusicCondition(t, func() bool { return !p.IsBusy() })
	if _, err := p.PauseToggle(); !errors.Is(err, ErrMusicNothingPlaying) {
		t.Fatal("empty pause should fail", err)
	}
	p.Close()
	p.Close()
	if _, _, err := p.Enqueue(testMusicURL, "closed"); !errors.Is(err, ErrMusicClosed) {
		t.Fatal("closed player accepted track", err)
	}
}

func TestMusicQueueBoundedAndConcurrentAdmissions(t *testing.T) {
	started := make(chan struct{})
	p := offlineMusicPlayer(t, func(ctx context.Context, _ MusicTrack) error {
		close(started)
		<-ctx.Done()
		return ctx.Err()
	})
	if _, _, err := p.Enqueue(testMusicURL, "first"); err != nil {
		t.Fatal(err)
	}
	<-started
	var wg sync.WaitGroup
	var mu sync.Mutex
	accepted := 0
	for i := 0; i < 40; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _, err := p.Enqueue(testMusicURL, "queued")
			if err == nil {
				mu.Lock()
				accepted++
				mu.Unlock()
			} else if !errors.Is(err, ErrMusicQueueFull) {
				t.Errorf("unexpected admission error: %v", err)
			}
		}()
	}
	wg.Wait()
	if accepted != musicQueueLimit || len(p.Snapshot().Queue) != musicQueueLimit {
		t.Fatalf("queue exceeded limit or lost capacity: %d accepted", accepted)
	}
}

func TestMusicClearCancelsPendingLookup(t *testing.T) {
	p := newMusicPlayer(nil)
	p.deps = func() error { return nil }
	started := make(chan struct{})
	p.resolve = func(ctx context.Context, _, _ string) (MusicTrack, error) {
		close(started)
		<-ctx.Done()
		return MusicTrack{}, ctx.Err()
	}
	go p.run()
	defer p.Close()
	result := make(chan error, 1)
	go func() { _, _, err := p.Enqueue(testMusicURL, "first"); result <- err }()
	<-started
	if !p.IsBusy() {
		t.Fatal("metadata lookup should count as busy")
	}
	p.Clear()
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("cleared lookup added a track")
		}
	case <-time.After(time.Second):
		t.Fatal("clear did not cancel pending lookup")
	}
	if p.IsBusy() || p.Snapshot().Current != nil || len(p.Snapshot().Queue) != 0 {
		t.Fatal("cleared lookup resurrected playback")
	}
}

func TestMusicPCMEncodesPacedOpusAndCancelsBlockedSend(t *testing.T) {
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppAudio)
	if err != nil {
		t.Fatal(err)
	}
	frames := 3
	pcmBytes := make([]byte, defaultOpusFrameSamples*channels*2*frames)
	for i := 0; i < len(pcmBytes)/2; i++ {
		binary.LittleEndian.PutUint16(pcmBytes[2*i:], uint16(int16((i%100)*30)))
	}
	packets := make(chan []byte, 3)
	p := newMusicPlayer(&discordgo.VoiceConnection{OpusSend: packets})
	ctx, cancel := context.WithCancelCause(context.Background())
	defer cancel(nil)
	start := time.Now()
	if err := p.sendPCM(ctx, cancel, bytes.NewReader(pcmBytes), encoder); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) < 55*time.Millisecond || len(packets) != 3 {
		t.Fatal("music producer did not pace frames")
	}
	decoder, err := opus.NewDecoder(sampleRate, channels)
	if err != nil {
		t.Fatal(err)
	}
	decoded := make([]int16, defaultOpusFrameSamples*channels)
	for len(packets) > 0 {
		count, err := decoder.Decode(<-packets, decoded)
		if err != nil || count != defaultOpusFrameSamples {
			t.Fatalf("invalid Discord Opus frame: %d %v", count, err)
		}
	}
	p.vc.OpusSend = make(chan []byte)
	blocked, stop := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer stop()
	child, stopChild := context.WithCancelCause(blocked)
	defer stopChild(nil)
	if err := p.sendPCM(child, stopChild, bytes.NewReader(pcmBytes), encoder); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("blocked Discord send ignored cancellation: %v", err)
	}
	p.cancel()
}

func TestMusicProcessCancellation(t *testing.T) {
	if os.Getenv("MUSIC_PROCESS_TEST_CHILD") == "1" {
		time.Sleep(30 * time.Second)
		os.Exit(0)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	cmd := musicCommand(ctx, os.Args[0], "-test.run=^TestMusicProcessCancellation$")
	cmd.Env = append(os.Environ(), "MUSIC_PROCESS_TEST_CHILD=1")
	start := time.Now()
	if err := cmd.Run(); err == nil || ctx.Err() == nil {
		t.Fatalf("subprocess did not cancel: %v", err)
	}
	if time.Since(start) > 2*time.Second || cmd.ProcessState == nil {
		t.Fatal("subprocess was not reaped promptly")
	}
}

func TestMusicPausedPCMDoesNotSendUntilResumed(t *testing.T) {
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppAudio)
	if err != nil {
		t.Fatal(err)
	}
	packets := make(chan []byte, 1)
	p := newMusicPlayer(&discordgo.VoiceConnection{OpusSend: packets})
	defer p.cancel()
	p.current = &MusicTrack{Title: "paused"}
	p.paused = true
	ctx, cancel := context.WithCancelCause(context.Background())
	defer cancel(nil)
	finished := make(chan error, 1)
	go func() {
		finished <- p.sendPCM(ctx, cancel, bytes.NewReader(make([]byte, defaultOpusFrameSamples*channels*2)), encoder)
	}()
	select {
	case <-packets:
		t.Fatal("paused playback sent audio")
	case <-time.After(40 * time.Millisecond):
	}
	paused, err := p.PauseToggle()
	if err != nil || paused {
		t.Fatal("could not resume playback", err)
	}
	select {
	case <-packets:
	case <-time.After(time.Second):
		t.Fatal("resuming did not deliver audio")
	}
	if err := <-finished; err != nil {
		t.Fatal(err)
	}
}

func TestMusicFFmpegPCMToDiscordOpusOffline(t *testing.T) {
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		t.Skip("ffmpeg is not installed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	cmd := musicCommand(ctx, "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
		"-i", "sine=frequency=440:sample_rate=48000:duration=0.06", "-ac", "2", "-f", "s16le", "pipe:1")
	output, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { cancel(); _ = output.Close(); _ = cmd.Wait() }()
	packets := make(chan []byte, 4)
	p := newMusicPlayer(&discordgo.VoiceConnection{OpusSend: packets})
	defer p.cancel()
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppAudio)
	if err != nil {
		t.Fatal(err)
	}
	streamCtx, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	if err := p.sendPCM(streamCtx, stop, output, encoder); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Wait(); err != nil {
		t.Fatal(err)
	}
	if len(packets) != 3 {
		t.Fatalf("expected three 20ms frames, received %d", len(packets))
	}
	decoder, err := opus.NewDecoder(sampleRate, channels)
	if err != nil {
		t.Fatal(err)
	}
	decoded := make([]int16, defaultOpusFrameSamples*channels)
	hasSignal := false
	for len(packets) > 0 {
		if frames, err := decoder.Decode(<-packets, decoded); err != nil || frames != defaultOpusFrameSamples {
			t.Fatalf("ffmpeg generated an invalid Discord frame: %d %v", frames, err)
		}
		for _, sample := range decoded {
			hasSignal = hasSignal || sample != 0
		}
	}
	if !hasSignal {
		t.Fatal("ffmpeg audio was replaced by silence")
	}
}
