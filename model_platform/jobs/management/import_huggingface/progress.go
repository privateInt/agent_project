package main

import (
    "context"
    "log"
    "time"

    operationv1 "model-platform/gen/operation/v1"
    "google.golang.org/protobuf/proto"
)

// This observer never changes file bytes or retries a transfer. Only a bounded,
// best-effort status RPC is performed; a failed report backs off for 30 seconds.
func progressReport(ctx context.Context, ops operationv1.OperationServiceClient, id string) func(*operationv1.TransferProgress) {
    var blockedUntil time.Time
    return func(p *operationv1.TransferProgress) {
        if time.Now().Before(blockedUntil) { return }
        sampleCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
        defer cancel()
        _, err := ops.UpdatePhase(sampleCtx, &operationv1.UpdateOperationRequest{OperationId:id, Phase:"transferring_files", Progress:p})
        if err != nil {
            blockedUntil = time.Now().Add(30*time.Second)
            log.Printf("transfer progress unavailable operation=%s (file transfer continues)", id)
        }
    }
}

type transferMeter struct {
    sample operationv1.TransferProgress
    report func(*operationv1.TransferProgress)
    last time.Time
}

func (m *transferMeter) emit(force bool) {
    if m == nil || m.report == nil || (!force && time.Since(m.last) < 3*time.Second) { return }
    m.last = time.Now()
    m.sample.Sequence++
    m.report(proto.Clone(&m.sample).(*operationv1.TransferProgress))
}
func (m *transferMeter) Write(p []byte) (int, error) {
    if m != nil {
        m.sample.BytesCompleted += int64(len(p))
        // An inconsistent/absent HTTP length cannot be a percentage denominator.
        if m.sample.BytesTotal > 0 && m.sample.BytesCompleted > m.sample.BytesTotal { m.sample.BytesTotal = 0 }
        m.emit(false)
    }
    return len(p), nil
}
