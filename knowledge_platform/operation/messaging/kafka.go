package messaging

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"knowledge-platform/internal/execution"

	"github.com/twmb/franz-go/pkg/kgo"
)

type Publisher struct {
	client *kgo.Client
	topic  string
}

func Connect(_ context.Context, brokers, topic string) (*Publisher, error) {
	parts := strings.Split(brokers, ",")
	list := make([]string, 0, len(parts))
	for _, part := range parts {
		if value := strings.TrimSpace(part); value != "" {
			list = append(list, value)
		}
	}
	client, err := kgo.NewClient(kgo.SeedBrokers(list...), kgo.AllowAutoTopicCreation())
	if err != nil {
		return nil, err
	}
	// Kafka may be offline at startup. Outbox retains and retries commands.

	return &Publisher{client: client, topic: topic}, nil
}

func (p *Publisher) Close() { p.client.Close() }

// [FLOW-09] Operation -> Kafka boundary.
// Kafka transports the execute-command; PostgreSQL Operation state remains the
// source of truth. Generic resource placement is allowed; credentials are not.
func (p *Publisher) Publish(ctx context.Context, cmd execution.Command) error {
	payload, err := json.Marshal(cmd)
	if err != nil {
		return err
	}
	result := p.client.ProduceSync(ctx, &kgo.Record{
		Topic: p.topic,
		Key:   []byte(cmd.OperationID),
		Value: payload,
	})
	if err := result.FirstErr(); err != nil {
		return fmt.Errorf("publish execution command: %w", err)
	}
	return nil
}
