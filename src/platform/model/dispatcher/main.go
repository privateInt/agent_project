package main

import (
	"context"
	"fmt"
 "time"
	"log"
	"os"
	"strings"

	dispatchapp "model-platform/dispatcher/app"
	"model-platform/dispatcher/executor"
	"model-platform/dispatcher/registry"
 "model-platform/internal/remotenode"

	"github.com/twmb/franz-go/pkg/kgo"
)

func main() {
 if len(os.Args)>1 {
  var err error
  switch os.Args[1] {
  case "--node-request": if len(os.Args)!=2 {err=fmt.Errorf("unexpected node arguments")} else {err=nodeRequest()}
  case "--probe-node","--inspect-node-job": err=nodeCLI(os.Args[1:])
  default: err=recoverGPUWait(os.Args[1:])
  }
  if err!=nil{log.Fatal(err)};return
 }
 if err := run(); err != nil { log.Fatal(err) }
}

func run() error {
	ctx := context.Background()
	brokers := split(required("KAFKA_BROKERS"))
	topic := env("KAFKA_COMMAND_TOPIC", "ops.operations.requested")
	group := env("KAFKA_CONSUMER_GROUP", "model-platform-dispatchers")
	ops, err := dispatchapp.ConnectOperation(env("OPERATION_GRPC_ADDRESS", "operation:9000"))
	if err != nil {
		return err
	}
	defer ops.Close()
	consumer, err := kgo.NewClient(kgo.SeedBrokers(brokers...), kgo.ConsumeTopics(topic), kgo.ConsumerGroup(group), kgo.DisableAutoCommit(), kgo.BlockRebalanceOnPoll(), kgo.AllowAutoTopicCreation())
	if err != nil {
		return err
	}
	defer func() { consumer.AllowRebalance(); consumer.Close() }()
	pingCtx, cancelPing := context.WithTimeout(ctx, 5*time.Second)
 defer cancelPing()
 if err := consumer.Ping(pingCtx); err != nil {
		return err
	}
	reg := registry.New()
	runner := executor.NewDocker(env("JOB_NETWORK", "ops-infra"), env("DISPATCHER_NODE_NAME", ""))
	nodes,err:=remotenode.Load();if err!=nil{return err}
 router:=executor.Router{Local:runner,LocalName:runner.NodeName,Nodes:nodes,Remote:remotenode.NewClient()}
 delivery := dispatchapp.Delivery{Operations: ops, Profiles: reg, Runner: router}
 log.Printf("common dispatcher ready topic=%s group=%s", topic, group)
	// [FLOW-10] Kafka -> Common Dispatcher boundary. One shared dispatcher
	// handles Management, Training, and Deployment commands.
	for {
		fetches := consumer.PollRecords(ctx, 1)
        if fetches.IsClientClosed() { return nil }
        // A fetch error can accompany records. Stop and replay, never advance
        // the local consumer position past records returned with an error.
        if errs := fetches.Errors(); len(errs)>0 { return fmt.Errorf("kafka fetch failed: %v", errs) }
        for _, record := range fetches.Records() {
            if err := delivery.Handle(ctx, record.Value); err != nil {
                return fmt.Errorf("record retained topic=%s partition=%d offset=%d: %w",record.Topic,record.Partition,record.Offset,err)
            }
            if err := dispatchapp.Commit(ctx, func(commitCtx context.Context) error { return consumer.CommitRecords(commitCtx,record) }); err != nil {
                return fmt.Errorf("stop before next record topic=%s partition=%d offset=%d: %w",record.Topic,record.Partition,record.Offset,err)
            }
            log.Printf("record handled topic=%s partition=%d offset=%d",record.Topic,record.Partition,record.Offset)
        }
        consumer.AllowRebalance()
    }
}

func split(value string) []string {
	parts := strings.Split(value, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if v := strings.TrimSpace(part); v != "" {
			out = append(out, v)
		}
	}
	return out
}
func required(name string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	log.Fatalf("%s is required", name)
	return ""
}
func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
