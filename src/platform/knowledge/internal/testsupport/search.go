package testsupport

import (
	"encoding/json"
	"strings"
	"unicode/utf8"

	embeddingv1 "knowledge-platform/gen/model/embedding/v1"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/indexing"
)

// Synthetic metadata only; no model weights, vectors or external test documents.
func SearchVersion() indexing.Version {
	deployment := "33333333-3333-4333-8333-333333333333"
	return indexing.Version{
		SchemaVersion: indexing.SchemaVersion, ID: "11111111-1111-4111-8111-111111111111", IndexID: "22222222-2222-4222-8222-222222222222",
		State: "ready", Count: 2, Next: 2, ReferenceState: "held",
		CanonicalIDs: []string{"44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555"},
		Spec: chunking.Spec{Chunker: chunking.Selection{PolicyHash: strings.Repeat("9",64)}, Documents: []chunking.Input{
			{CanonicalID: "44444444-4444-4444-8444-444444444444", SourceDocumentID: "66666666-6666-4666-8666-666666666666"},
			{CanonicalID: "55555555-5555-4555-8555-555555555555", SourceDocumentID: "77777777-7777-4777-8777-777777777777"},
		}},
		Profile: &embeddingv1.EmbeddingProfile{
			DeploymentId: deployment, ModelVersionId: "88888888-8888-4888-8888-888888888888", ServedModelName: "deployment-"+deployment,
			ProfileId: "snowflake-ko-v1", RepoId: "dragonkue/snowflake-arctic-embed-l-v2.0-ko", Capability: "embedding", Status: "ready",
			EmbeddingSpaceId: "es_"+strings.Repeat("a",64), SourceManifestSha256: strings.Repeat("b",64), TokenizerSha256: strings.Repeat("c",64),
			RuntimeImageId: "sha256:"+strings.Repeat("d",64), RuntimeContainerId: strings.Repeat("e",64),
			QueryPrefix: "query: ", Pooling: "cls", Normalization: "l2", Dimension: 1024, MaxInputTokens: 2048, MaxBatchSize: 16, MaxInputBytes: 131072, Truncation: "reject",
		},
	}
}
func SearchChunk(v indexing.Version, ordinal int) chunking.Chunk {
	input := v.Spec.Documents[ordinal]
	text := "자료의 환경 영향"
	c := chunking.Chunk{
		SourceDocumentID: input.SourceDocumentID, CanonicalID: input.CanonicalID,
		Text: text, ContentSHA256: chunking.Digest([]byte(text)), Kind: "text", Ordinal: ordinal,
		Spans: []chunking.Span{{BlockID: "b1", Start: 0, End: utf8.RuneCountInString(text), ChunkStart: 0, ChunkEnd: utf8.RuneCountInString(text), Metadata: json.RawMessage(`{"type":"paragraph"}`)}},
		Quality: json.RawMessage(`{"state":"needs_ocr","issues":["image excluded"]}`),
	}
	c.ID = chunking.Digest([]byte(input.CanonicalID))
	return c
}
