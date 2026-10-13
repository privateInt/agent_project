package app

import (
 "context"
 "testing"
 refinementv1 "data-platform/gen/refinement/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

// A malformed filter must fail before accessing the database. Otherwise an
// invalid source selector can accidentally broaden the canonical candidate set.
func TestListRunsRejectsMalformedSourceFilter(t *testing.T) {
 service:=&Service{}
 _,err:=service.ListRuns(context.Background(),&refinementv1.ListRunsRequest{SourceDocumentId:"not-a-source-id"})
 if status.Code(err)!=codes.InvalidArgument {t.Fatalf("invalid source filter accepted: %v",err)}
}
