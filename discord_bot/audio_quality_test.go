package main

import (
	"bytes"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
	"gopkg.in/hraban/opus.v2"
)

func TestDiscordAudioBufferWindowWrapDuplicatesAndBound(t *testing.T) {
	now := time.Now()
	b := discordAudioBuffer{}
	for _, seq := range []uint16{65535, 1, 0, 65534, 0} {
		b.add(&discordgo.Packet{SSRC: 1, Sequence: seq}, now, "speaker")
	}
	b.add(&discordgo.Packet{SSRC: 2, Sequence: 80}, now.Add(audioReorderWindow), "other")
	if len(b.ready(now.Add(audioReorderWindow-time.Nanosecond), false)) != 0 {
		t.Fatal("packets released before reorder window")
	}
	ready := b.ready(now.Add(audioReorderWindow), false)
	for i, want := range []uint16{65534, 65535, 0, 1} {
		if len(ready) != 4 || ready[i].packet.Sequence != want {
			t.Fatalf("incorrect wrap/dedup order: %+v", ready)
		}
	}
	if len(b.ready(now, true)) != 1 || len(b) != 0 {
		t.Fatal("shutdown failed to drain other speaker")
	}
	for seq := range uint16(32) {
		b.add(&discordgo.Packet{SSRC: 1, Sequence: seq}, now, "speaker")
	}
	if len(b.ready(now, false)) != 32 {
		t.Fatal("full queue was not drained")
	}
}

func TestDiscordReorderedAudioMatchesOrderedRecording(t *testing.T) {
	encoder, err := opus.NewEncoder(sampleRate, channels, opus.AppVoIP)
	if err != nil {
		t.Fatal(err)
	}
	encoded := make([][]byte, 5)
	for frame := range encoded {
		pcm := make([]int16, defaultOpusFrameSamples*channels)
		for i := 0; i < len(pcm); i += channels {
			value := int16(8000 * math.Sin(2*math.Pi*440*float64(frame*defaultOpusFrameSamples+i/channels)/sampleRate))
			pcm[i], pcm[i+1] = value, value
		}
		data := make([]byte, 4000)
		n, err := encoder.Encode(pcm, data)
		if err != nil {
			t.Fatal(err)
		}
		encoded[frame] = data[:n]
	}
	capture := func(order []int) []byte {
		t.Helper()
		dir := t.TempDir()
		packets := make(chan *discordgo.Packet, len(order))
		for _, i := range order {
			packets <- &discordgo.Packet{SSRC: 10, Sequence: uint16(i), Timestamp: uint32(i * defaultOpusFrameSamples), Opus: encoded[i]}
		}
		close(packets)
		users := NewSSRCUserMap()
		users.Set(10, "speaker")
		if err := ListenAndWriteOpusToWAV(&discordgo.VoiceConnection{OpusRecv: packets}, dir, 1, users, nil, nil, nil, nil); err != nil {
			t.Fatal(err)
		}
		files, _ := filepath.Glob(filepath.Join(dir, "*.wav"))
		if len(files) != 1 {
			t.Fatalf("expected one WAV, got %d", len(files))
		}
		data, err := os.ReadFile(files[0])
		if err != nil {
			t.Fatal(err)
		}
		return data
	}
	ordered := capture([]int{0, 1, 2, 3, 4})
	reordered := capture([]int{1, 0, 3, 2, 2, 4})
	if len(ordered) != 44+5*defaultOpusFrameSamples*channels*2 || !bytes.Equal(ordered, reordered) {
		t.Fatal("reordering or duplicates changed decoded PCM or WAV duration")
	}
	withLoss := capture([]int{0, 2, 3, 4})
	if len(withLoss) != len(ordered) {
		t.Fatal("Opus loss recovery changed recording duration")
	}
}

func TestRealtimeLateSpeechPreservedForBatchWithoutStretching(t *testing.T) {
	writer, err := NewWAVWriter(filepath.Join(t.TempDir(), "late.wav"), sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	rt := &realtimeAudioClient{queue: make(chan []byte, 10), done: make(chan struct{}), abort: make(chan struct{})}
	r := &userAudioRecording{wav: writer, realtime: rt, lastPacketAt: time.Now()}
	pcm := make([]int16, defaultOpusFrameSamples*channels)
	for i := range pcm {
		pcm[i] = 1000
	}
	if err := r.writeRTPPacket(10, 1, 0, 0, nil, pcm, defaultOpusFrameSamples); err != nil {
		t.Fatal(err)
	}
	// 40 ms beyond the first packet have already been sent as silence.
	if err := r.padRealtimeSilence(r.lastPacketAt.Add(120 * time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	for seq := uint16(2); seq <= 4; seq++ {
		if err := r.writeRTPPacket(10, seq, uint32(seq-1)*defaultOpusFrameSamples, 0, nil, pcm, defaultOpusFrameSamples); err != nil {
			t.Fatal(err)
		}
	}
	if writer.FramesWritten() != 4*defaultOpusFrameSamples || r.idlePadding != 0 {
		t.Fatalf("late packets stretched WAV: frames=%d padding=%d", writer.FramesWritten(), r.idlePadding)
	}
	select {
	case <-rt.abort:
	default:
		t.Fatal("late speech did not trigger Batch fallback")
	}
	data, err := os.ReadFile(writer.f.Name())
	if err != nil {
		t.Fatal(err)
	}
	for i := 44; i < len(data); i += 2 {
		if data[i] != 0xe8 || data[i+1] != 0x03 {
			t.Fatal("late speech was replaced with silence")
		}
	}
	if err := r.padRealtimeSilence(r.lastPacketAt.Add(time.Second)); err != nil || writer.FramesWritten() != 4*defaultOpusFrameSamples {
		t.Fatal("failed Realtime continued to pad Batch audio")
	}
}
