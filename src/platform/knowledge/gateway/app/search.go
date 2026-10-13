package app

import (
	"io"
	"net/http"

	indexingv1 "knowledge-platform/gen/indexing/v1"
	"knowledge-platform/internal/retrieval"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (s *Server) search(w http.ResponseWriter, r *http.Request) {
	if r.URL.RawQuery != "" || !retrieval.ValidID(r.PathValue("id")) {
		fail(w, status.Error(codes.InvalidArgument, "SEARCH_INVALID_REQUEST: index UUID and no query parameters required")); return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, retrieval.MaxRequestBytes))
	if err != nil { fail(w, status.Error(codes.ResourceExhausted, "SEARCH_REQUEST_TOO_LARGE: request exceeds 64 KiB")); return }
	if _, err = retrieval.Parse(raw); err != nil { fail(w, status.Error(codes.InvalidArgument, err.Error())); return }
	out, err := s.Indexes.Search(r.Context(), &indexingv1.SearchRequest{IndexId: r.PathValue("id"), QueryJson: raw})
	if err != nil { fail(w, err); return }
	if out == nil || len(out.Json) == 0 || len(out.Json) > retrieval.MaxResponseBytes {
		fail(w, status.Error(codes.DataLoss, "SEARCH_RESPONSE_INVALID: missing or excessive response")); return
	}
	writeRaw(w, http.StatusOK, out.Json)
}
