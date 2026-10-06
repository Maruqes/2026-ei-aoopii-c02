package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/bwmarrin/discordgo"
	"gopkg.in/hraban/opus.v2"
)

func TestRealtimeRecoveredLossConsumesAlreadySentSilence(t *testing.T) {
	for _, tc := range []struct{ silence, recovered, padding int }{
		{0, 960, 480}, {0, 960, 960}, {960, 960, 480},
		{960, 960, 1200}, {960, 960, 1920},
	} {
		t.Run(fmt.Sprintf("silence=%d/recovered=%d/padding=%d", tc.silence, tc.recovered, tc.padding), func(t *testing.T) {
			writer, err := NewWAVWriter(filepath.Join(t.TempDir(), "loss.wav"), sampleRate, channels, bitsPerSample)
			if err != nil {
				t.Fatal(err)
			}
			defer writer.Close()
			rt := &realtimeAudioClient{queue: make(chan []byte, 10), done: make(chan struct{}), abort: make(chan struct{})}
			r := &userAudioRecording{wav: writer, realtime: rt}
			pcm := make([]int16, 960*channels)
			for i := range pcm {
				pcm[i] = 1000
			}
			if err := r.writeRTPPacket(10, 1, 0, 0, nil, pcm, 960); err != nil {
				t.Fatal(err)
			}
			if err := writer.WriteSilence(tc.padding); err != nil {
				t.Fatal(err)
			}
			rt.silence(tc.padding)
			r.idlePadding = tc.padding
			recovered := make([]int16, tc.recovered*channels)
			for i := range recovered {
				recovered[i] = 500
			}
			if err := r.writeRTPPacket(10, 3, uint32(960+tc.silence+tc.recovered), tc.silence, recovered, pcm, 960); err != nil {
				t.Fatal(err)
			}
			select {
			case <-rt.abort:
				t.Fatal("concealed loss incorrectly aborted Realtime")
			default:
			}
			wantFrames := 1920 + tc.silence + tc.recovered
			var sent []byte
			for len(rt.queue) > 0 {
				sent = append(sent, <-rt.queue...)
			}
			if writer.FramesWritten() != int64(wantFrames) || len(sent)/2 != wantFrames || r.idlePadding != 0 {
				t.Fatalf("clocks diverged: WAV=%d live=%d want=%d", writer.FramesWritten(), len(sent)/2, wantFrames)
			}
			data, err := os.ReadFile(writer.f.Name())
			if err != nil {
				t.Fatal(err)
			}
			for frame := 0; frame < wantFrames; frame++ {
				want := int16(1000)
				if frame >= 960 && frame < 960+tc.silence {
					want = 0
				} else if frame >= 960+tc.silence && frame < wantFrames-960 {
					want = 500
				}
				for channel := 0; channel < channels; channel++ {
					at := 44 + (frame*channels+channel)*2
					if got := int16(binary.LittleEndian.Uint16(data[at:])); got != want {
						t.Fatalf("WAV lost recovery at frame %d: got=%d want=%d", frame, got, want)
					}
				}
				liveWant := want
				if frame >= 960 && frame < 960+tc.padding {
					liveWant = 0 // Already sent speculative silence cannot be retracted.
				}
				if got := int16(binary.LittleEndian.Uint16(sent[frame*2:])); got != liveWant {
					t.Fatalf("live audio repeated/skipped at frame %d: got=%d want=%d", frame, got, liveWant)
				}
			}
		})
	}
}

func TestRealtimeSilenceWaitsForBufferedAudioAndJitter(t *testing.T) {
	writer, err := NewWAVWriter(filepath.Join(t.TempDir(), "pending.wav"), sampleRate, channels, bitsPerSample)
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	rt := &realtimeAudioClient{queue: make(chan []byte, 10), done: make(chan struct{}), abort: make(chan struct{})}
	now := time.Now()
	r := &userAudioRecording{wav: writer, realtime: rt, lastPacketAt: now}
	if err := r.writeRTPPacket(10, 1, 0, 0, nil, make([]int16, 960*channels), 960); err != nil {
		t.Fatal(err)
	}
	if err := r.padRealtimeSilence(now.Add(200*time.Millisecond), nil); err != nil {
		t.Fatal(err)
	}
	if writer.FramesWritten() != 960 {
		t.Fatal("ordinary network jitter was padded as silence")
	}
	b := discordAudioBuffer{}
	b.add(&discordgo.Packet{SSRC: 10, Sequence: 2}, now, "speaker")
	if err := r.padRealtimeSilence(now.Add(time.Second), b); err != nil {
		t.Fatal(err)
	}
	if writer.FramesWritten() != 960 {
		t.Fatal("buffered speech was padded as silence")
	}
	delete(b, 10)
	b.add(&discordgo.Packet{SSRC: 11}, now, "other")
	if err := r.padRealtimeSilence(now.Add(time.Second), b); err != nil {
		t.Fatal(err)
	}
	if writer.FramesWritten() != 36000 {
		t.Fatal("another speaker prevented actual DTX silence")
	}
}

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
	if err := r.padRealtimeSilence(r.lastPacketAt.Add(realtimeSilenceGrace+60*time.Millisecond), nil); err != nil {
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
	if err := r.padRealtimeSilence(r.lastPacketAt.Add(time.Second), nil); err != nil || writer.FramesWritten() != 4*defaultOpusFrameSamples {
		t.Fatal("failed Realtime continued to pad Batch audio")
	}
}
