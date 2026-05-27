package main

import (
	"log"
	"strings"
	"time"

	"github.com/bwmarrin/discordgo"
)

func OnMessageCreate(s *discordgo.Session, m *discordgo.MessageCreate) {
	request, ok := textMessageRequestFromDiscordMessage(s, m)
	if !ok {
		return
	}

	client := botAPIClient
	if client == nil {
		client = NewTranscriptionClientFromEnv()
	}
	client.QueueTextMessage(request)
}

func textMessageRequestFromDiscordMessage(s *discordgo.Session, m *discordgo.MessageCreate) (TextMessageRequest, bool) {
	if !shouldCaptureTextMessage(m) {
		return TextMessageRequest{}, false
	}

	author := m.Author
	displayName := ""
	if m.Member != nil {
		displayName = strings.TrimSpace(m.Member.Nick)
		if displayName == "" && m.Member.User != nil {
			displayName = strings.TrimSpace(m.Member.User.GlobalName)
		}
	}
	if displayName == "" && author != nil {
		displayName = strings.TrimSpace(author.GlobalName)
	}

	channelName := resolveTextChannelName(s, m.ChannelID)
	editedAt := discordgoTimestampPtr(m.EditedTimestamp)

	return TextMessageRequest{
		GuildID:          m.GuildID,
		ChannelID:        m.ChannelID,
		ChannelName:      channelName,
		DiscordMessageID: m.ID,
		DiscordID:        author.ID,
		Username:         author.Username,
		DisplayName:      displayName,
		Content:          m.Content,
		Tstamp:           time.Time(m.Timestamp),
		EditedAt:         editedAt,
	}, true
}

func shouldCaptureTextMessage(m *discordgo.MessageCreate) bool {
	if m == nil || m.Message == nil {
		return false
	}
	if strings.TrimSpace(m.GuildID) == "" {
		return false
	}
	if strings.TrimSpace(m.ID) == "" || strings.TrimSpace(m.ChannelID) == "" {
		return false
	}
	if strings.TrimSpace(m.Content) == "" {
		return false
	}
	if strings.TrimSpace(m.WebhookID) != "" {
		return false
	}
	if m.Author == nil || strings.TrimSpace(m.Author.ID) == "" {
		return false
	}
	if m.Author.Bot {
		return false
	}
	return true
}

func resolveTextChannelName(s *discordgo.Session, channelID string) string {
	channelID = strings.TrimSpace(channelID)
	if channelID == "" {
		return "text"
	}

	if s != nil && s.State != nil {
		if channel, err := s.State.Channel(channelID); err == nil && strings.TrimSpace(channel.Name) != "" {
			return channel.Name
		}
	}

	if s != nil {
		channel, err := s.Channel(channelID)
		if err != nil {
			log.Printf("erro ao obter nome do canal de texto=%s: %v", channelID, err)
		} else if strings.TrimSpace(channel.Name) != "" {
			return channel.Name
		}
	}

	return channelID
}

func discordgoTimestampPtr(value *time.Time) *time.Time {
	if value == nil {
		return nil
	}
	tstamp := *value
	return &tstamp
}
