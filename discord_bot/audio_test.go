package main

import (
	"testing"

	"github.com/bwmarrin/discordgo"
)

func TestSelectSummaryTextChannelPrefersSystemChannel(t *testing.T) {
	channels := []*discordgo.Channel{
		{ID: "first-text", Type: discordgo.ChannelTypeGuildText, Position: 0},
		{ID: "system", Type: discordgo.ChannelTypeGuildText, Position: 3},
	}

	channel := selectSummaryTextChannel(channels, "system", nil)

	if channel == nil || channel.ID != "system" {
		t.Fatalf("expected system text channel, got %#v", channel)
	}
}

func TestSelectSummaryTextChannelFallsBackToFirstSendableTextChannel(t *testing.T) {
	channels := []*discordgo.Channel{
		{ID: "voice", Type: discordgo.ChannelTypeGuildVoice, Position: 0},
		{ID: "blocked-system", Type: discordgo.ChannelTypeGuildText, Position: 1},
		{ID: "later-text", Type: discordgo.ChannelTypeGuildText, Position: 5},
		{ID: "first-text", Type: discordgo.ChannelTypeGuildText, Position: 2},
	}

	channel := selectSummaryTextChannel(channels, "blocked-system", func(channel *discordgo.Channel) bool {
		return channel.ID != "blocked-system"
	})

	if channel == nil || channel.ID != "first-text" {
		t.Fatalf("expected first sendable text channel, got %#v", channel)
	}
}
