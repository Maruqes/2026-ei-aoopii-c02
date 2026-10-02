package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestEmptyWAVRemovedButUnsubmittedAudioRetained(t *testing.T) {
	for _, empty := range []bool{true, false} {
		path := newUserAudioPath(t.TempDir(), "123")
		writer, err := NewWAVWriter(path, sampleRate, channels, bitsPerSample)
		if err != nil {
			t.Fatal(err)
		}
		if !empty {
			if err := writer.WritePCM([]int16{1, 2}); err != nil {
				t.Fatal(err)
			}
		}
		recording := &userAudioRecording{wav: writer, path: path, user: voiceUserInfo{DiscordID: "123"}, startedAt: time.Now()}
		if err := closeAndTranscribeRecording(recording, voiceUserInfo{}, nil); err != nil {
			t.Fatal(err)
		}
		_, err = os.Stat(path)
		if empty && !os.IsNotExist(err) {
			t.Fatalf("empty WAV retained: %v", err)
		}
		if !empty && err != nil {
			t.Fatalf("recoverable WAV removed: %v", err)
		}
	}
}

func TestYouTubeIncidentalFilesRemovedOnSuccessErrorAndCancellation(t *testing.T) {
	for _, mode := range []string{"success", "error", "cancel"} {
		t.Run(mode, func(t *testing.T) {
			scripts := t.TempDir()
			marker := filepath.Join(t.TempDir(), "workspace")
			script := "#!/bin/sh\nprintf '%s' \"$PWD\" > \"$MUSIC_WORKSPACE_MARKER\"\nprintf accidental > video.mp4\nprintf temporary > \"$TMPDIR/media.part\"\n"
			switch mode {
			case "success":
				script += `printf '%s' '{"title":"test","duration":10,"url":"https://rr1.googlevideo.com/audio"}'` + "\n"
			case "error":
				script += "exit 1\n"
			case "cancel":
				script += "sleep 60\n"
			}
			if err := os.WriteFile(filepath.Join(scripts, "yt-dlp"), []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", scripts+string(os.PathListSeparator)+os.Getenv("PATH"))
			t.Setenv("MUSIC_WORKSPACE_MARKER", marker)
			ctx, cancel := context.WithCancel(testContext(t))
			defer cancel()
			result := make(chan error, 1)
			go func() { _, err := resolveYouTubeMusic(ctx, testMusicURL, "123"); result <- err }()
			if mode == "cancel" {
				waitMusicCondition(t, func() bool { _, err := os.Stat(marker); return err == nil })
				cancel()
			}
			err := <-result
			if mode == "success" && err != nil {
				t.Fatal(err)
			}
			if mode != "success" && err == nil {
				t.Fatal("failure accepted")
			}
			data, err := os.ReadFile(marker)
			if err != nil {
				t.Fatal(err)
			}
			directory := string(data)
			if !strings.HasPrefix(filepath.Base(directory), "discord-youtube-") {
				t.Fatalf("provider was not isolated: %q", directory)
			}
			if _, err := os.Stat(directory); !os.IsNotExist(err) {
				t.Fatalf("workspace retained: %v", err)
			}
		})
	}
}

func TestConverterWorkspaceRemovedAfterProcessReaped(t *testing.T) {
	cmd := musicCommand(testContext(t), "sh", "-c", "printf audio > ffmpeg-cache; printf audio > \"$TMPDIR/output.wav\"")
	cleanup, err := isolateMusicCommand(cmd)
	if err != nil {
		t.Fatal(err)
	}
	directory := cmd.Dir
	if err := cmd.Run(); err != nil {
		cleanup()
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(directory, "ffmpeg-cache")); err != nil {
		cleanup()
		t.Fatal(err)
	}
	cleanup()
	if _, err := os.Stat(directory); !os.IsNotExist(err) {
		t.Fatalf("converter workspace retained: %v", err)
	}
}

func TestForgetFlushesActiveUserAndKeepsOtherUsers(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	targetPath := newUserAudioPath(recordingsDirFromEnv(), "123")
	otherPath := newUserAudioPath(recordingsDirFromEnv(), "1234")
	for _, path := range []string{targetPath, otherPath} {
		if err := os.WriteFile(path, []byte("audio"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	flushed := make(chan struct{})
	allowFlush := make(chan struct{})
	deleted := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("unexpected request %s", r.Method)
		}
		deleted <- struct{}{}
		_ = json.NewEncoder(w).Encode(ForgetUserResponse{Status: "deleted", DiscordID: "123"})
	}))
	defer server.Close()
	client := testAPIClient(server)
	state := &voiceConnectionState{recordingEvents: make(chan recordingControlEvent), recordingDone: make(chan struct{}), transcriptionClient: client}
	registerRecordingState(state)
	defer unregisterRecordingState(state)
	client.submissionMu.Lock()
	other := client.userSubmissionGroupLocked("1234")
	other.pending = 1
	client.submissionMu.Unlock()
	go func() {
		event := <-state.recordingEvents
		close(flushed)
		<-allowFlush
		event.ack <- nil
		close(event.ack)
	}()
	ctx := testContext(t)
	result := make(chan error, 1)
	go func() { _, err := forgetUserWithLocalAudio(ctx, client, "123"); result <- err }()
	<-flushed
	if !isUserCapturePaused("123") || isUserCapturePaused("1234") {
		t.Error("capture gate did not isolate owner")
	}
	select {
	case <-deleted:
		t.Error("API deletion happened before audio flush")
	case <-time.After(20 * time.Millisecond):
	}
	close(allowFlush)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	if isUserCapturePaused("123") {
		t.Fatal("capture remained paused after deletion")
	}
	if _, err := os.Stat(targetPath); !os.IsNotExist(err) {
		t.Fatalf("target audio retained: %v", err)
	}
	if _, err := os.Stat(otherPath); err != nil {
		t.Fatalf("other user's audio removed: %v", err)
	}
}

func TestForgetBackendFailureKeepsLocalRecoveryAndResumesCapture(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	path := newUserAudioPath(recordingsDirFromEnv(), "123")
	if err := os.WriteFile(path, []byte("audio"), 0600); err != nil {
		t.Fatal(err)
	}
	request := TranscriptionRequest{AudioPath: path, DiscordID: "123"}
	if err := persistTranscription(request); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Error(w, "cannot delete", http.StatusBadRequest) }))
	defer server.Close()
	_, err := forgetUserWithLocalAudio(testContext(t), testAPIClient(server), "123")
	if err == nil {
		t.Fatal("deletion failure ignored")
	}
	if isUserCapturePaused("123") {
		t.Fatal("capture remained paused after failure")
	}
	for _, file := range []string{path, transcriptionOutboxPath(request)} {
		if _, err := os.Stat(file); err != nil {
			t.Fatalf("recovery file lost: %v", err)
		}
	}
}

func TestUserSubmissionWaitHonorsCancellation(t *testing.T) {
	client := &TranscriptionClient{}
	client.submissionMu.Lock()
	group := client.userSubmissionGroupLocked("123")
	group.pending = 1
	client.submissionMu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := client.waitForUserSubmissions(ctx, "123"); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestLocalForgetProtectsForeignReceiptAndUnmanagedFiles(t *testing.T) {
	t.Setenv("RECORDINGS_DIR", t.TempDir())
	foreign := newUserAudioPath(recordingsDirFromEnv(), "123")
	unmanaged := filepath.Join(recordingsDirFromEnv(), "123-manual.wav")
	for _, path := range []string{foreign, unmanaged, foreign + ".speechmatics.json"} {
		if err := os.WriteFile(path, []byte("preserve"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	request := TranscriptionRequest{AudioPath: foreign, DiscordID: "999"}
	if err := persistTranscription(request); err != nil {
		t.Fatal(err)
	}
	if err := removeUserLocalRecordings("123", nil); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{foreign, unmanaged, foreign + ".speechmatics.json", foreign + ".request.json"} {
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("unowned file removed: %s: %v", path, err)
		}
	}
}
