package main

import (
	"encoding/binary"
	"fmt"
	"log"
	"os"
	"path/filepath"
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
	wav              *WAVWriter
	path             string
	startedAt        time.Time
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

	if err := recording.wav.Close(); err != nil {
		log.Printf("erro ao fechar WAV de user=%s: %v", user.DiscordID, err)
		return err
	}

	if recording.wav.dataSize == 0 {
		log.Printf("WAV sem áudio para user=%s file=%s; transcrição ignorada", user.DiscordID, recording.path)
		return nil
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
	transcriptions.QueueTranscription(TranscriptionRequest{
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

	for {
		select {
		case event, ok := <-recordingEvents:
			if !ok {
				recordingEvents = nil
				continue
			}
			if event.finishAll {
				log.Printf("evento recebido: finalizar todas as gravações ativas count=%d stop=%v", len(userRecordings), event.stopListening)
				if err := closeUserRecordings(userRecordings, transcriptions); err != nil {
					return err
				}
				if event.stopListening {
					return nil
				}
				continue
			}
			log.Printf("evento recebido: finalizar gravação user=%s", event.user.DiscordID)
			if err := finishUserRecording(userRecordings, event.user, transcriptions); err != nil {
				return err
			}
			continue

		case packet, ok := <-vc.OpusRecv:
			if !ok {
				log.Println("OpusRecv fechado")
				return nil
			}
			if packet == nil || len(packet.Opus) == 0 {
				continue
			}

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

			if discordID == "" {
				continue
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

			packetAt := time.Now().UTC()
			recording := userRecordings[discordID]
			plan := rtpPacketPlan{}
			if recording != nil {
				plan = recording.planRTPPacket(packet.SSRC, packet.Sequence, packet.Timestamp)
				if plan.stale {
					log.Printf(
						"pacote RTP atrasado/duplicado ignorado user=%s ssrc=%d sequence=%d expected=%d",
						discordID,
						packet.SSRC,
						packet.Sequence,
						recording.nextRTPSequence,
					)
					continue
				}
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
				continue
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
		}
	}
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

	if err := recording.wav.WriteSilence(silenceFrames); err != nil {
		return err
	}
	if err := recording.wav.WritePCM(recoveredPCM); err != nil {
		return err
	}
	if err := recording.wav.WritePCM(pcm[:samples]); err != nil {
		return err
	}

	recording.ssrc = ssrc
	recording.nextRTPSequence = sequence + 1
	recording.nextRTPTimestamp = timestamp + uint32(frames)
	recording.hasRTPSequence = true
	recording.hasRTPTimestamp = true
	return nil
}

func (recording *userAudioRecording) padToElapsed(endAt time.Time) error {
	if recording == nil || recording.wav == nil || recording.startedAt.IsZero() || !endAt.After(recording.startedAt) {
		return nil
	}

	elapsedFrames := endAt.Sub(recording.startedAt).Nanoseconds() * sampleRate / int64(time.Second)
	missingFrames := elapsedFrames - recording.wav.FramesWritten()
	if missingFrames <= 0 {
		return nil
	}
	return recording.wav.WriteSilence(int(missingFrames))
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
