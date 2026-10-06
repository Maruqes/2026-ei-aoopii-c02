package main

import (
	"encoding/binary"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/bwmarrin/discordgo"
	"github.com/google/uuid"
	"gopkg.in/hraban/opus.v2"
)

/*
sudo dnf install opus opus-devel
sudo dnf install opus opus-devel opusfile opusfile-devel pkgconf-pkg-config
*/

const (
	sampleRate              = 48000
	channels                = 2
	bitsPerSample           = 16
	maxFrameMs              = 120
	audioReorderWindow      = 60 * time.Millisecond
	realtimeSilenceGrace    = 250 * time.Millisecond
	defaultOpusFrameMs      = 20
	maxConcealedOpusPackets = 6
	defaultOpusFrameSamples = sampleRate * defaultOpusFrameMs / 1000
)

type WAVWriter struct {
	f          *os.File
	dataSize   uint32
	sampleRate uint32
	channels   uint16
	bitDepth   uint16
}

type userAudioRecording struct {
	realtime         *realtimeAudioClient
	idlePadding      int
	lastPacketFrames int
	streamToken      string
	wav              *WAVWriter
	path             string
	startedAt        time.Time
	lastPacketAt     time.Time
	user             voiceUserInfo
	channel          string
	sessionID        int64
	ssrc             uint32
	nextRTPSequence  uint16
	nextRTPTimestamp uint32
	hasRTPSequence   bool
	hasRTPTimestamp  bool
}

type discordOpusDecoder struct {
	decoder          *opus.Decoder
	pcm              []int16
	lastPacketFrames int
}

type rtpPacketPlan struct {
	stale              bool
	missingPackets     int
	timestampGapFrames int
}

func newDiscordOpusDecoder() (*discordOpusDecoder, error) {
	decoder, err := opus.NewDecoder(sampleRate, channels)
	if err != nil {
		return nil, err
	}

	maxSamplesPerChannel := maxFrameMs * sampleRate / 1000
	return &discordOpusDecoder{
		decoder:          decoder,
		pcm:              make([]int16, maxSamplesPerChannel*channels),
		lastPacketFrames: defaultOpusFrameSamples,
	}, nil
}

func (d *discordOpusDecoder) Decode(opusPacket []byte) ([]int16, int, error) {
	if d == nil || d.decoder == nil {
		return nil, 0, fmt.Errorf("decoder Opus do Discord nao configurado")
	}

	frames, err := d.decoder.Decode(opusPacket, d.pcm)
	if err != nil {
		return nil, 0, err
	}
	d.lastPacketFrames = frames
	return d.pcm[:frames*channels], frames, nil
}

func (d *discordOpusDecoder) DecodeFEC(opusPacket []byte, frames int) ([]int16, error) {
	if d == nil || d.decoder == nil {
		return nil, fmt.Errorf("decoder Opus do Discord nao configurado")
	}
	if frames <= 0 {
		return nil, nil
	}

	pcm := make([]int16, frames*channels)
	if err := d.decoder.DecodeFEC(opusPacket, pcm); err != nil {
		return nil, err
	}
	d.lastPacketFrames = frames
	return pcm, nil
}

func (d *discordOpusDecoder) DecodePLC(frames int) ([]int16, error) {
	if d == nil || d.decoder == nil {
		return nil, fmt.Errorf("decoder Opus do Discord nao configurado")
	}
	if frames <= 0 {
		return nil, nil
	}

	pcm := make([]int16, frames*channels)
	if err := d.decoder.DecodePLC(pcm); err != nil {
		return nil, err
	}
	d.lastPacketFrames = frames
	return pcm, nil
}

func (d *discordOpusDecoder) packetFrames() int {
	if d == nil || d.lastPacketFrames <= 0 {
		return defaultOpusFrameSamples
	}
	return d.lastPacketFrames
}

func NewWAVWriter(path string, sampleRate uint32, channels uint16, bitDepth uint16) (*WAVWriter, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}

	w := &WAVWriter{
		f:          f,
		sampleRate: sampleRate,
		channels:   channels,
		bitDepth:   bitDepth,
	}

	// Reservar header WAV inicial
	if err := w.writeHeader(0); err != nil {
		f.Close()
		_ = os.Remove(path)
		return nil, err
	}

	return w, nil
}

func (w *WAVWriter) writeHeader(dataSize uint32) error {
	byteRate := w.sampleRate * uint32(w.channels) * uint32(w.bitDepth) / 8
	blockAlign := w.channels * w.bitDepth / 8
	chunkSize := 36 + dataSize

	if _, err := w.f.Seek(0, 0); err != nil {
		return err
	}

	// RIFF header
	if _, err := w.f.Write([]byte("RIFF")); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, chunkSize); err != nil {
		return err
	}
	if _, err := w.f.Write([]byte("WAVE")); err != nil {
		return err
	}

	// fmt chunk
	if _, err := w.f.Write([]byte("fmt ")); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, uint32(16)); err != nil { // PCM chunk size
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, uint16(1)); err != nil { // PCM format
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, w.channels); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, w.sampleRate); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, byteRate); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, blockAlign); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, w.bitDepth); err != nil {
		return err
	}

	// data chunk
	if _, err := w.f.Write([]byte("data")); err != nil {
		return err
	}
	if err := binary.Write(w.f, binary.LittleEndian, dataSize); err != nil {
		return err
	}

	return nil
}

func (w *WAVWriter) WritePCM(pcm []int16) error {
	if len(pcm) == 0 {
		return nil
	}
	if err := binary.Write(w.f, binary.LittleEndian, pcm); err != nil {
		return err
	}

	w.dataSize += uint32(len(pcm) * 2)
	return nil
}

func (w *WAVWriter) WriteSilence(frames int) error {
	if frames <= 0 {
		return nil
	}

	const maxSilenceChunkFrames = sampleRate
	zeros := make([]int16, maxSilenceChunkFrames*int(w.channels))
	for frames > 0 {
		chunkFrames := min(frames, maxSilenceChunkFrames)
		if err := w.WritePCM(zeros[:chunkFrames*int(w.channels)]); err != nil {
			return err
		}
		frames -= chunkFrames
	}
	return nil
}

func (w *WAVWriter) FramesWritten() int64 {
	bytesPerFrame := int64(w.channels) * int64(w.bitDepth) / 8
	if bytesPerFrame == 0 {
		return 0
	}
	return int64(w.dataSize) / bytesPerFrame
}

func (w *WAVWriter) Close() error {
	if err := w.writeHeader(w.dataSize); err != nil {
		w.f.Close()
		return err
	}
	return w.f.Close()
}

func newUserAudioPath(outDir string, discordID string) string {
	filename := discordID + "-" + uuid.NewString() + ".wav"
	return filepath.Join(outDir, filename)
}

func closeUserRecordings(recordings map[string]*userAudioRecording, transcriptions *TranscriptionClient) error {
	var firstErr error

	for discordID, recording := range recordings {
		if recording == nil {
			continue
		}

		delete(recordings, discordID)
		if err := closeAndTranscribeRecording(recording, voiceUserInfo{}, transcriptions); err != nil && firstErr == nil {
			firstErr = err
		}
	}

	return firstErr
}

func finishUserRecording(recordings map[string]*userAudioRecording, info voiceUserInfo, transcriptions *TranscriptionClient) error {
	info = info.withFallbacks()
	if info.DiscordID == "" {
		return nil
	}

	recording := recordings[info.DiscordID]
	if recording == nil {
		return nil
	}

	delete(recordings, info.DiscordID)
	return closeAndTranscribeRecording(recording, info, transcriptions)
}

func closeAndTranscribeRecording(recording *userAudioRecording, info voiceUserInfo, transcriptions *TranscriptionClient) error {
	if recording == nil || recording.wav == nil {
		return nil
	}

	user := mergeVoiceUserInfo(info, recording.user)
	if user.DiscordID == "" {
		user = recording.user.withFallbacks()
	}

	log.Printf(
		"a finalizar WAV user=%s username=%s channel=%s file=%s data_bytes=%d started_at=%s",
		user.DiscordID,
		user.Username,
		recording.channel,
		recording.path,
		recording.wav.dataSize,
		recording.startedAt.UTC().Format(time.RFC3339Nano),
	)

	clearLive := func() {
		if transcriptions != nil {
			transcriptions.submissionMu.Lock()
			delete(transcriptions.liveAudio, recording.path+".request.json")
			transcriptions.submissionMu.Unlock()
		}
	}
	if err := recording.wav.Close(); err != nil {
		clearLive()
		if recording.realtime != nil {
			recording.realtime.close()
		}
		log.Printf("erro ao fechar WAV de user=%s: %v", user.DiscordID, err)
		if recording.wav.dataSize == 0 {
			_ = os.Remove(recording.path)
		}
		return err
	}

	if recording.wav.dataSize == 0 {
		clearLive()
		if recording.realtime != nil {
			recording.realtime.close()
		}
		_ = os.Remove(recording.path + ".request.json")
		log.Printf("WAV sem áudio para user=%s file=%s; transcrição ignorada", user.DiscordID, recording.path)
		return os.Remove(recording.path)
	}

	log.Printf(
		"WAV finalizado; a chamar API transcrição user=%s username=%s channel=%s file=%s data_bytes=%d elapsed_recording=%s",
		user.DiscordID,
		user.Username,
		recording.channel,
		recording.path,
		recording.wav.dataSize,
		time.Since(recording.startedAt).Round(time.Second),
	)
	if transcriptions == nil {
		log.Printf("cliente de transcricao nil; WAV ignorado user=%s file=%s", user.DiscordID, recording.path)
		return nil
	}
	if recording.realtime != nil {
		recording.realtime.close()
	}
	transcriptions.QueueTranscription(TranscriptionRequest{
		Realtime:           recording.realtime,
		SafetyWAV:          recording.realtime != nil,
		SessionID:          recording.sessionID,
		AudioPath:          recording.path,
		DiscordID:          user.DiscordID,
		Username:           user.Username,
		DisplayName:        user.DisplayName,
		ChannelName:        recording.channel,
		RecordingStartedAt: recording.startedAt,
	})

	return nil
}

func ListenAndWriteOpusToWAV(
	vc *discordgo.VoiceConnection,
	outDir string,
	sessionID int64,
	ssrcUsers *SSRCUserMap,
	recordingEvents <-chan recordingControlEvent,
	transcriptions *TranscriptionClient,
	lookupUserInfo func(string) voiceUserInfo,
	currentChannelName func() string,
	stopSignals ...<-chan struct{},
) error {
	if outDir == "" {
		outDir = "."
	}
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return err
	}

	log.Printf("a gravar áudio para a pasta %s", outDir)

	decoders := make(map[uint32]*discordOpusDecoder)
	userRecordings := make(map[string]*userAudioRecording)
	identifiedUsers := make(map[uint32]string)
	unknownSSRCs := make(map[uint32]bool)
	defer closeUserRecordings(userRecordings, transcriptions)
	var stopSignal <-chan struct{}
	if len(stopSignals) > 0 {
		stopSignal = stopSignals[0]
	}
	var streaming *streamingController
	voiceMu.Lock()
	for _, state := range voiceConnections {
		if state.sessionID == sessionID && state.vc == vc {
			streaming = state.streaming
			break
		}
	}
	voiceMu.Unlock()
	identifyPacket := func(packet *discordgo.Packet) string {
		var discordID string
		if ssrcUsers != nil {
			if user, ok := ssrcUsers.GetBySSRC(packet.SSRC); ok {
				discordID = user.DiscordID
				if identifiedUsers[packet.SSRC] != user.DiscordID {
					identifiedUsers[packet.SSRC] = user.DiscordID
					log.Printf("a receber áudio de user=%s ssrc=%d", user.DiscordID, user.SSRC)
				}
			} else {
				if syncSSRCUserMapFromVoiceConnection(vc, ssrcUsers) {
					if user, ok := ssrcUsers.GetBySSRC(packet.SSRC); ok {
						discordID = user.DiscordID
						if identifiedUsers[packet.SSRC] != user.DiscordID {
							identifiedUsers[packet.SSRC] = user.DiscordID
							log.Printf("SSRC associado pelo voice websocket user=%s ssrc=%d", user.DiscordID, user.SSRC)
						}
					}
				}
				if discordID == "" && !unknownSSRCs[packet.SSRC] {
					unknownSSRCs[packet.SSRC] = true
					log.Printf("a receber áudio de ssrc=%d sem user associado", packet.SSRC)
				}
			}
		}

		return discordID
	}
	processPacket := func(packet *discordgo.Packet, discordID string, packetAt time.Time) error {
		// Ownership was resolved on receipt, before a member can leave or lose
		// their SSRC mapping. Privacy/suspension gates still apply at flush.
		if isUserCapturePaused(discordID) || streaming.suspended() {
			return nil
		}

		dec := decoders[packet.SSRC]
		if dec == nil {
			newDecoder, err := newDiscordOpusDecoder()
			if err != nil {
				return err
			}
			dec = newDecoder
			decoders[packet.SSRC] = dec
		}

		recording := userRecordings[discordID]
		plan := rtpPacketPlan{}
		if recording != nil {
			plan = recording.planRTPPacket(packet.SSRC, packet.Sequence, packet.Timestamp)
			if plan.stale {
				// Discard before rotation so a late duplicate cannot become a new clip.
				return nil
			}
		}
		if (recording != nil && recording.realtime != nil && recording.realtime.failed.Load()) || shouldRotateRecording(recording, packetAt, packet.SSRC) || shouldRotateForRTPGap(plan) || (recording != nil && recording.streamToken != streaming.grant(discordID).Token) {
			delete(userRecordings, discordID)
			if err := closeAndTranscribeRecording(recording, voiceUserInfo{}, transcriptions); err != nil {
				return err
			}
			recording = nil
			plan = rtpPacketPlan{}
		}

		recoveredPCM := []int16(nil)
		silenceFrames := plan.timestampGapFrames
		if recording != nil && plan.missingPackets > 0 && plan.timestampGapFrames > 0 {
			var recoverErr error
			recoveredPCM, silenceFrames, recoverErr = recoverMissingOpusAudio(
				dec,
				packet.Opus,
				plan.missingPackets,
				plan.timestampGapFrames,
			)
			if recoverErr != nil {
				log.Printf(
					"erro a recuperar perda RTP user=%s ssrc=%d sequence=%d missing=%d gap_frames=%d: %v",
					discordID,
					packet.SSRC,
					packet.Sequence,
					plan.missingPackets,
					plan.timestampGapFrames,
					recoverErr,
				)
				recoveredPCM = nil
				silenceFrames = plan.timestampGapFrames
			} else if len(recoveredPCM) > 0 {
				log.Printf(
					"perda RTP recuperada user=%s ssrc=%d sequence=%d missing=%d recovered_frames=%d silence_frames=%d",
					discordID,
					packet.SSRC,
					packet.Sequence,
					plan.missingPackets,
					len(recoveredPCM)/channels,
					silenceFrames,
				)
			}
		}

		pcm, frames, err := dec.Decode(packet.Opus)
		if err != nil {
			log.Printf("erro a descodificar opus (ssrc=%d): %v", packet.SSRC, err)
			return nil
		}

		if recording == nil {
			outPath := newUserAudioPath(outDir, discordID)
			newWriter, err := NewWAVWriter(outPath, sampleRate, channels, bitsPerSample)
			if err != nil {
				return err
			}
			recording = &userAudioRecording{
				wav:       newWriter,
				path:      outPath,
				startedAt: packetAt,
				user:      getRecordingUserInfo(discordID, lookupUserInfo),
				channel:   getCurrentChannelName(currentChannelName, vc.ChannelID),
				sessionID: sessionID,
			}
			if grant := streaming.grant(discordID); grant.Token != "" && transcriptions != nil {
				request := TranscriptionRequest{SessionID: sessionID, AudioPath: outPath, DiscordID: discordID,
					Username: recording.user.Username, DisplayName: recording.user.DisplayName,
					ChannelName: recording.channel, RecordingStartedAt: packetAt, SafetyWAV: true}
				// Persist before opening Realtime: a crash still leaves a recoverable WAV and owner.
				if err := persistTranscription(request); err == nil {
					_ = persistSessionFinish(sessionID, currentBotLanguage().apiValue())
					transcriptions.submissionMu.Lock()
					if transcriptions.liveAudio == nil {
						transcriptions.liveAudio = map[string]bool{}
					}
					transcriptions.liveAudio[transcriptionOutboxPath(request)] = true
					transcriptions.submissionMu.Unlock()
					recording.streamToken = grant.Token
					recording.realtime = newRealtimeAudioClient(transcriptions, withStreamingController(request, streaming), grant, streaming.state.assistant)
				} else {
					log.Printf("Realtime outbox failed user=%s; using Batch", discordID)
				}
			}
			userRecordings[discordID] = recording
			log.Printf("a gravar user=%s para %s", discordID, outPath)
		}

		if err := recording.writeRTPPacket(
			packet.SSRC,
			packet.Sequence,
			packet.Timestamp,
			silenceFrames,
			recoveredPCM,
			pcm,
			frames,
		); err != nil {
			return err
		}
		recording.lastPacketAt = maxPacketArrival(recording.lastPacketAt, packetAt)
		return nil
	}
	buffer := discordAudioBuffer{}
	flush := func(now time.Time, all bool) error {
		for _, queued := range buffer.ready(now, all) {
			if err := processPacket(queued.packet, queued.discordID, queued.receivedAt); err != nil {
				return err
			}
		}
		return nil
	}
	reorderTicker := time.NewTicker(10 * time.Millisecond)
	defer reorderTicker.Stop()
	idleTicker := time.NewTicker(250 * time.Millisecond)
	defer idleTicker.Stop()
	for {
		select {
		case <-stopSignal:
			if err := flush(time.Now(), true); err != nil {
				return err
			}
			return closeUserRecordings(userRecordings, transcriptions)
		case tick := <-reorderTicker.C:
			if err := flush(tick, false); err != nil {
				return err
			}
		case tick := <-idleTicker.C:
			for id, recording := range userRecordings {
				if err := recording.padRealtimeSilence(tick, buffer); err != nil {
					return err
				}
				if !recording.lastPacketAt.IsZero() && tick.Sub(recording.lastPacketAt) >= recordingIdleTimeout() || recording.streamToken != streaming.grant(id).Token || (recording.realtime != nil && recording.realtime.failed.Load()) || streaming.suspended() {
					delete(userRecordings, id)
					if err := closeAndTranscribeRecording(recording, voiceUserInfo{}, transcriptions); err != nil {
						log.Printf("Could not flush idle recording: %v", err)
					}
				}
			}
		case event, ok := <-recordingEvents:
			if !ok {
				recordingEvents = nil
				continue
			}
			if err := flush(time.Now(), true); err != nil {
				return err
			}
			if event.finishAll {
				log.Printf("evento recebido: finalizar todas as gravações ativas count=%d stop=%v", len(userRecordings), event.stopListening)
				if err := closeUserRecordings(userRecordings, transcriptions); err != nil {
					return err
				}
				if event.ack != nil {
					event.ack <- nil
					close(event.ack)
				}
				if event.stopListening {
					return nil
				}
				continue
			}
			log.Printf("evento recebido: finalizar gravação user=%s", event.user.DiscordID)
			err := finishUserRecording(userRecordings, event.user, transcriptions)
			if event.ack != nil {
				event.ack <- err
				close(event.ack)
			}
			if err != nil {
				return err
			}
			continue

		case packet, ok := <-vc.OpusRecv:
			if !ok {
				log.Println("OpusRecv fechado")
				return flush(time.Now(), true)
			}
			if packet == nil || len(packet.Opus) == 0 {
				continue
			}

			discordID := identifyPacket(packet)
			if discordID == "" || discordID == vc.UserID || isUserCapturePaused(discordID) || streaming.suspended() || !streaming.participantPresent(discordID) {
				continue
			}
			buffer.add(packet, time.Now().UTC(), discordID)
			if err := flush(time.Now(), false); err != nil {
				return err
			}
		}
	}
}

// Feed DTX silence while keeping the safety WAV and upstream clock identical.
func (recording *userAudioRecording) padRealtimeSilence(now time.Time, pending discordAudioBuffer) error {
	if recording.realtime == nil || recording.lastPacketAt.IsZero() || len(pending[recording.ssrc]) > 0 {
		return nil
	}
	select {
	case <-recording.realtime.abort:
		return nil
	case <-recording.realtime.done:
		return nil
	default:
	}
	// Silence is speculative until the next RTP timestamp arrives. Leave room
	// for network jitter as well as reordering before advancing the live clock.
	expected := int((now.Sub(recording.lastPacketAt) - realtimeSilenceGrace).Seconds() * sampleRate)
	padding := max(0, expected-recording.idlePadding-recording.lastPacketFrames)
	if padding == 0 {
		return nil
	}
	if err := recording.wav.WriteSilence(padding); err != nil {
		return err
	}
	recording.realtime.silence(padding)
	recording.idlePadding += padding
	return nil
}

func (recording *userAudioRecording) planRTPPacket(
	ssrc uint32,
	sequence uint16,
	timestamp uint32,
) rtpPacketPlan {
	if recording == nil ||
		recording.ssrc != ssrc ||
		!recording.hasRTPSequence ||
		!recording.hasRTPTimestamp {
		return rtpPacketPlan{}
	}

	sequenceDelta := int16(sequence - recording.nextRTPSequence)
	if sequenceDelta < 0 {
		return rtpPacketPlan{stale: true}
	}

	timestampDelta := int32(timestamp - recording.nextRTPTimestamp)
	if timestampDelta < 0 {
		return rtpPacketPlan{stale: true}
	}

	return rtpPacketPlan{
		missingPackets:     int(sequenceDelta),
		timestampGapFrames: int(timestampDelta),
	}
}

func recoverMissingOpusAudio(
	decoder *discordOpusDecoder,
	nextOpusPacket []byte,
	missingPackets int,
	timestampGapFrames int,
) ([]int16, int, error) {
	if decoder == nil || missingPackets <= 0 || timestampGapFrames <= 0 {
		return nil, max(0, timestampGapFrames), nil
	}

	packetFrames := decoder.packetFrames()
	if packetFrames <= 0 {
		packetFrames = defaultOpusFrameSamples
	}

	recoverablePackets := min(missingPackets, timestampGapFrames/packetFrames)
	if recoverablePackets <= 0 || recoverablePackets > maxConcealedOpusPackets {
		return nil, timestampGapFrames, nil
	}

	recovered := make([]int16, 0, recoverablePackets*packetFrames*channels)
	for packetIndex := 0; packetIndex < recoverablePackets; packetIndex++ {
		var (
			pcm []int16
			err error
		)
		if packetIndex == recoverablePackets-1 {
			pcm, err = decoder.DecodeFEC(nextOpusPacket, packetFrames)
		} else {
			pcm, err = decoder.DecodePLC(packetFrames)
		}
		if err != nil {
			return nil, timestampGapFrames, err
		}
		recovered = append(recovered, pcm...)
	}

	recoveredFrames := len(recovered) / channels
	return recovered, max(0, timestampGapFrames-recoveredFrames), nil
}

func (recording *userAudioRecording) writeRTPPacket(
	ssrc uint32,
	sequence uint16,
	timestamp uint32,
	silenceFrames int,
	recoveredPCM []int16,
	pcm []int16,
	frames int,
) error {
	if recording == nil || recording.wav == nil || frames <= 0 {
		return nil
	}
	samples := frames * int(recording.wav.channels)
	if samples > len(pcm) {
		return fmt.Errorf("WAV PCM incompleto: frames=%d channels=%d samples=%d", frames, recording.wav.channels, len(pcm))
	}

	realtimeSilenceFrames := silenceFrames
	realtimeRecoveredPCM := recoveredPCM
	// Padding can cover a lost packet reconstructed by FEC/PLC. Replace that
	// speculative silence in the WAV, but send only the unsent part of the gap
	// upstream. Only overlap with the received speech requires Batch fallback.
	if recording.idlePadding > silenceFrames {
		padding := recording.idlePadding
		dataSize := recording.wav.dataSize - uint32(recording.idlePadding*int(recording.wav.channels)*2)
		if err := recording.wav.f.Truncate(44 + int64(dataSize)); err != nil {
			return err
		}
		if _, err := recording.wav.f.Seek(44+int64(dataSize), 0); err != nil {
			return err
		}
		recording.wav.dataSize = dataSize
		recording.idlePadding = 0
		recoveredFrames := len(recoveredPCM) / int(recording.wav.channels)
		if padding > silenceFrames+recoveredFrames {
			if recording.realtime != nil {
				recording.realtime.abortOnce.Do(func() { close(recording.realtime.abort) })
			}
			log.Printf("Late RTP overlaps Realtime silence; full WAV retained for Batch user=%s ssrc=%d sequence=%d padding_frames=%d gap_frames=%d recovered_frames=%d", recording.user.DiscordID, ssrc, sequence, padding, silenceFrames+recoveredFrames, recoveredFrames)
		} else {
			realtimeSilenceFrames = 0
			realtimeRecoveredPCM = recoveredPCM[(padding-silenceFrames)*int(recording.wav.channels):]
		}
	} else {
		silenceFrames -= recording.idlePadding
		realtimeSilenceFrames = silenceFrames
		recording.idlePadding = 0
	}

	if err := recording.wav.WriteSilence(silenceFrames); err != nil {
		return err
	}
	if err := recording.wav.WritePCM(recoveredPCM); err != nil {
		return err
	}
	if err := recording.wav.WritePCM(pcm[:samples]); err != nil {
		return err
	}

	if recording.realtime != nil {
		recording.realtime.silence(realtimeSilenceFrames)
		recording.realtime.enqueue(realtimeRecoveredPCM)
		recording.realtime.enqueue(pcm[:samples])
	}
	recording.lastPacketFrames = frames
	recording.ssrc = ssrc
	recording.nextRTPSequence = sequence + 1
	recording.nextRTPTimestamp = timestamp + uint32(frames)
	recording.hasRTPSequence = true
	recording.hasRTPTimestamp = true
	return nil
}

func maxPacketArrival(previous, current time.Time) time.Time {
	if previous.After(current) {
		return previous
	}
	return current
}

func getRecordingUserInfo(discordID string, lookupUserInfo func(string) voiceUserInfo) voiceUserInfo {
	if lookupUserInfo == nil {
		return voiceUserInfo{DiscordID: discordID}.withFallbacks()
	}

	info := lookupUserInfo(discordID)
	if info.DiscordID == "" {
		info.DiscordID = discordID
	}
	return info.withFallbacks()
}

func getCurrentChannelName(currentChannelName func() string, fallback string) string {
	if currentChannelName != nil {
		if channelName := currentChannelName(); channelName != "" {
			return channelName
		}
	}
	if fallback != "" {
		return fallback
	}
	return "voice"
}

func recordingIdleTimeout() time.Duration {
	return recordingDurationFromEnv("RECORDING_IDLE_SECONDS", 10*time.Second)
}
func recordingDurationFromEnv(name string, fallback time.Duration) time.Duration {
	seconds, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil || seconds < 2 || seconds > 3600 {
		return fallback
	}
	return time.Duration(seconds) * time.Second
}
func shouldRotateRecording(recording *userAudioRecording, packetAt time.Time, ssrc uint32) bool {
	if recording == nil || recording.wav == nil {
		return false
	}
	if recording.hasRTPSequence && recording.ssrc != ssrc {
		return true
	}
	if !recording.lastPacketAt.IsZero() && packetAt.Sub(recording.lastPacketAt) >= recordingIdleTimeout() {
		return true
	}
	maximum := recordingDurationFromEnv("RECORDING_MAX_SECONDS", 5*time.Minute)
	return recording.wav.FramesWritten() >= int64(maximum.Seconds()*sampleRate)
}

// RTP timestamps are untrusted. A discontinuity must not allocate hours of
// silence or produce a giant upload, even if packets arrived close together.
func shouldRotateForRTPGap(plan rtpPacketPlan) bool {
	return plan.timestampGapFrames >= int(recordingIdleTimeout().Seconds()*sampleRate)
}
