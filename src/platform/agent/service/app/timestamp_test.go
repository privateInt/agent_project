package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"github.com/jackc/pgx/v5/pgtype"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	"testing"
	"time"
)

// Exercise the real database driver's timestamp codec, not an in-memory copy
// of the response. No PostgreSQL server is needed for its wire representation.
func TestDiagnosticResponseSurvivesPostgresTimestampEncoding(t *testing.T) {
	service, ctx := setup()
	created, err := service.CreateSystemCheck(ctx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	for _, format := range []int16{pgtype.BinaryFormatCode, pgtype.TextFormatCode} {
		persisted := proto.Clone(created).(*platformv1.SystemCheck)
		values := []*timestamppb.Timestamp{created.CreatedAt, created.CompletedAt}
		decoded := make([]*timestamppb.Timestamp, len(values))
		mapping := pgtype.NewMap()
		for i, value := range values {
			wire, err := mapping.Encode(pgtype.TimestamptzOID, format, value.AsTime(), nil)
			if err != nil {
				t.Fatal(err)
			}
			var fromDatabase time.Time
			if err := mapping.Scan(pgtype.TimestamptzOID, format, wire, &fromDatabase); err != nil {
				t.Fatal(err)
			}
			decoded[i] = timestamppb.New(fromDatabase)
		}
		persisted.CreatedAt, persisted.CompletedAt = decoded[0], decoded[1]
		if !proto.Equal(created, persisted) {
			t.Fatalf("creation/lookup mismatch after pgx timestamp codec (format %d): created=%s persisted=%s", format, created.CreatedAt.AsTime().Format(time.RFC3339Nano), persisted.CreatedAt.AsTime().Format(time.RFC3339Nano))
		}
		// The REST fields remain exactly equal; no timestamp tolerance is used.
	}
}
