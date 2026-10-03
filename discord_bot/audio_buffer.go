package main

import (
	"sort"
	"time"

	"github.com/bwmarrin/discordgo"
)

type queuedDiscordAudio struct {
	packet     *discordgo.Packet
	receivedAt time.Time
	discordID  string
}

// Hold a short, bounded window per speaker before decoding stateful Opus.
// A full queue is flushed rather than discarding speech or growing indefinitely.
type discordAudioBuffer map[uint32][]queuedDiscordAudio

func (b discordAudioBuffer) add(packet *discordgo.Packet, receivedAt time.Time, discordID string) {
	queue := b[packet.SSRC]
	for _, queued := range queue {
		if queued.packet.Sequence == packet.Sequence {
			return
		}
	}
	b[packet.SSRC] = append(queue, queuedDiscordAudio{packet, receivedAt, discordID})
}

func (b discordAudioBuffer) ready(now time.Time, all bool) []queuedDiscordAudio {
	var ready []queuedDiscordAudio
	for ssrc, queue := range b {
		oldest := queue[0].receivedAt
		if !all && len(queue) < 32 && now.Sub(oldest) < audioReorderWindow {
			continue
		}
		// Signed subtraction also orders sequence numbers across uint16 wrap.
		sort.SliceStable(queue, func(i, j int) bool {
			return int16(queue[i].packet.Sequence-queue[j].packet.Sequence) < 0
		})
		ready = append(ready, queue...)
		delete(b, ssrc)
	}
	return ready
}
