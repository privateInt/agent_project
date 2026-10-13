// GC archives terminal Job stdout/stderr and, in apply mode, collects their
// stopped containers after retention. It never changes an Operation or Job.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	operationv1 "model-platform/gen/operation/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type settings struct {
	policy                 Policy
	interval, itemTimeout  time.Duration
	maxBytes               int64
	batchSize              int
	useSSL                 bool
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func loadSettings() (settings, error) {
	c := settings{policy: Policy{Mode: env("GC_MODE", "dry-run"), Node: os.Getenv("DISPATCHER_NODE_NAME"), Network: env("JOB_NETWORK", "ops-infra")}}
	if c.policy.Mode != "dry-run" && c.policy.Mode != "archive" && c.policy.Mode != "apply" {
		return c, fmt.Errorf("GC_MODE must be dry-run, archive or apply")
	}
	for _, field := range []struct {
		key, fallback string
		target *time.Duration
	}{
		{"GC_SUCCESS_RETENTION", "24h", &c.policy.SuccessKeep},
		{"GC_FAILURE_RETENTION", "168h", &c.policy.FailureKeep},
		{"GC_INTERVAL", "5m", &c.interval},
		{"GC_ITEM_TIMEOUT", "2m", &c.itemTimeout},
	} {
		value, err := time.ParseDuration(env(field.key, field.fallback))
		if err != nil || value < time.Second {
			return c, fmt.Errorf("%s must be a duration of at least 1s", field.key)
		}
		*field.target = value
	}
	var err error
	c.maxBytes, err = strconv.ParseInt(env("GC_MAX_LOG_BYTES", "134217728"), 10, 64)
	if err != nil || c.maxBytes < 1 || c.maxBytes > 1<<30 {
		return c, fmt.Errorf("GC_MAX_LOG_BYTES must be 1..1073741824")
	}
	c.batchSize, err = strconv.Atoi(env("GC_BATCH_SIZE", "20"))
	if err != nil || c.batchSize < 1 || c.batchSize > 1000 {
		return c, fmt.Errorf("GC_BATCH_SIZE must be 1..1000")
	}
	c.useSSL, err = strconv.ParseBool(env("MINIO_USE_SSL", "false"))
	if err != nil {
		return c, fmt.Errorf("invalid MINIO_USE_SSL")
	}
	return c, nil
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	once := flag.Bool("once", false, "process one bounded batch and exit")
	only := flag.String("container-id", "", "inspect this exact full container ID; requires --once")
	flag.Parse()
	if flag.NArg() != 0 || (*only != "" && (!*once || !containerRE.MatchString(*only))) {
		return fmt.Errorf("use --once --container-id with a full 64-character container ID")
	}
	c, err := loadSettings()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	conn, err := grpc.NewClient(env("OPERATION_GRPC_ADDRESS", "operation:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return err
	}
	defer conn.Close()
	client := operationv1.NewOperationServiceClient(conn)
	w := &worker{policy: c.policy, docker: dockerCLI{}, maxBytes: c.maxBytes, now: time.Now,
		get: func(ctx context.Context, id string) (*operationv1.Operation, error) {
			return client.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: id})
		},
	}
	if c.policy.Mode != "dry-run" {
		storeCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		store, storeErr := connectStore(storeCtx, c.useSSL)
		cancel()
		if storeErr != nil {
			return fmt.Errorf("GC archive storage initialization failed: %w", storeErr)
		}
		w.store = store
	}
	log.Printf("job GC ready mode=%s success_retention=%s failure_retention=%s", c.policy.Mode, c.policy.SuccessKeep, c.policy.FailureKeep)
	output := json.NewEncoder(os.Stdout)
	for ctx.Err() == nil {
		listCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		var ids []string
		if *only != "" {
			ids = []string{*only}
		} else {
			ids, err = w.docker.List(listCtx)
		}
		cancel()
		if err != nil {
			if *once {
				return err
			}
			log.Printf("GC list failed: %v", err)
		} else {
			batch := w.batch(ids, c.batchSize)
			failures := 0
			for _, id := range batch {
				itemCtx, cancel := context.WithTimeout(ctx, c.itemTimeout)
				result, itemErr := w.process(itemCtx, id)
				cancel()
				if itemErr != nil {
					result.Action, result.Note = "error_retained", itemErr.Error()
					failures++
				}
				if err := output.Encode(result); err != nil {
					return err
				}
				if ctx.Err() != nil {
					return ctx.Err()
				}
			}
			log.Printf("GC cycle listed=%d checked=%d errors=%d", len(ids), len(batch), failures)
			if *once {
				if failures > 0 {
					return fmt.Errorf("GC cycle had %d errors; affected containers retained", failures)
				}
				return nil
			}
		}
		select {
		case <-ctx.Done(): return nil
		case <-time.After(c.interval):
		}
	}
	return nil
}
