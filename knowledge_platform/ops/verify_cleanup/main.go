// verify_cleanup is an opt-in read-only observer, never a cleanup executor.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
	collectionv1 "knowledge-platform/gen/data/collection/v1"
	refinementv1 "knowledge-platform/gen/data/refinement/v1"
	"knowledge-platform/internal/chunking"
	dataclient "knowledge-platform/internal/clients/data"
	"knowledge-platform/internal/config"
	"knowledge-platform/internal/searchstore"
)

type expected struct {
	ID        string           `json:"index_version_id"`
	State     string           `json:"reference_state"`
	Count     int              `json:"chunk_count"`
	Documents []chunking.Input `json:"documents"`
}
type request struct {
	Versions []expected `json:"versions"`
}

func validID(s string) bool {
	u, e := uuid.Parse(s)
	return e == nil && u != uuid.Nil && u.String() == s
}
func observe(ctx context.Context, c *searchstore.Client, v expected) error {
	name := "knowledge-v1-" + strings.ReplaceAll(v.ID, "-", "")
	get := func(path string, out any) (int, error) {
		req, e := http.NewRequestWithContext(ctx, http.MethodGet, c.Base+path, nil)
		if e != nil {
			return 0, e
		}
		req.SetBasicAuth(c.User, c.Password)
		res, e := c.HTTP.Do(req)
		if e != nil {
			return 0, fmt.Errorf("OpenSearch observation unavailable")
		}
		defer res.Body.Close()
		if res.StatusCode == 404 {
			return 404, nil
		}
		if res.StatusCode != 200 {
			return res.StatusCode, fmt.Errorf("OpenSearch observation HTTP %d", res.StatusCode)
		}
		raw, e := io.ReadAll(io.LimitReader(res.Body, (2<<20)+1))
		if e != nil || len(raw) > 2<<20 {
			return 0, fmt.Errorf("OpenSearch response invalid")
		}
		return 200, json.Unmarshal(raw, out)
	}
	var mappings map[string]struct {
		Mappings struct {
			Meta struct {
				Owner string `json:"owner"`
				ID    string `json:"index_version_id"`
			} `json:"_meta"`
		} `json:"mappings"`
	}
	code, e := get("/"+name+"/_mapping", &mappings)
	if e != nil {
		return e
	}
	if v.State == "released" {
		if code != 404 {
			return fmt.Errorf("physical index still exists: %s", v.ID)
		}
		return nil
	}
	if code != 200 || len(mappings) != 1 || mappings[name].Mappings.Meta.Owner != "knowledge-platform" || mappings[name].Mappings.Meta.ID != v.ID {
		return fmt.Errorf("physical ownership differs: %s", v.ID)
	}
	var count struct {
		Count  *int `json:"count"`
		Shards struct {
			Total      int `json:"total"`
			Successful int `json:"successful"`
			Failed     int `json:"failed"`
		} `json:"_shards"`
	}
	code, e = get("/"+name+"/_count", &count)
	if e != nil {
		return e
	}
	if code != 200 || count.Count == nil || *count.Count != v.Count || count.Shards.Total < 1 || count.Shards.Successful != count.Shards.Total || count.Shards.Failed != 0 {
		return fmt.Errorf("physical count differs: %s", v.ID)
	}
	return nil
}
func run() error {
	decoder := json.NewDecoder(io.LimitReader(os.Stdin, 65537))
	decoder.DisallowUnknownFields()
	var r request
	if e := decoder.Decode(&r); e != nil {
		return fmt.Errorf("invalid observer input")
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return fmt.Errorf("trailing observer input")
	}
	if len(r.Versions) < 1 || len(r.Versions) > 3 {
		return fmt.Errorf("expected 1..3 versions")
	}
	seen := map[string]bool{}
	documents := map[string]chunking.Input{}
	for _, v := range r.Versions {
		if !validID(v.ID) || seen[v.ID] || (v.State != "held" && v.State != "released") || v.Count < 1 || v.Count > 20000 || len(v.Documents) < 1 || len(v.Documents) > 20 {
			return fmt.Errorf("invalid expected version")
		}
		seen[v.ID] = true
		for _, d := range v.Documents {
			if !validID(d.CanonicalID) || !chunking.ValidDigest(d.CanonicalSHA256) || d.SizeBytes < 1 || d.SizeBytes > chunking.MaxInputBytes {
				return fmt.Errorf("invalid canonical pin")
			}
			if old, ok := documents[d.CanonicalID]; ok && old != d {
				return fmt.Errorf("canonical pins differ")
			}
			documents[d.CanonicalID] = d
		}
	}
	c, e := searchstore.New(os.Getenv("OPENSEARCH_ENDPOINT"), os.Getenv("OPENSEARCH_USERNAME"), os.Getenv("OPENSEARCH_PASSWORD"), os.Getenv("OPENSEARCH_CA_BASE64"))
	if e != nil {
		return fmt.Errorf("OpenSearch settings invalid")
	}
	ref, e := grpc.NewClient(config.Env("DATA_REFINEMENT_GRPC_ADDRESS", "data-refinement:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		return fmt.Errorf("Data refinement settings invalid")
	}
	defer ref.Close()
	col, e := grpc.NewClient(config.Env("DATA_COLLECTION_GRPC_ADDRESS", "data-collection:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		return fmt.Errorf("Data collection settings invalid")
	}
	defer col.Close()
	data := dataclient.Client{Collection: collectionv1.NewCollectionServiceClient(col), Refinement: refinementv1.NewRefinementServiceClient(ref)}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	for _, v := range r.Versions {
		if e = observe(ctx, c, v); e != nil {
			return e
		}
		pins := []*refinementv1.CanonicalPin{}
		for _, d := range v.Documents {
			pins = append(pins, &refinementv1.CanonicalPin{CanonicalId: d.CanonicalID, Sha256: d.CanonicalSHA256})
		}
		sort.Slice(pins, func(i, j int) bool { return pins[i].CanonicalId < pins[j].CanonicalId })
		out, err := data.Refinement.GetKnowledgeReferences(ctx, &refinementv1.KnowledgeReferenceOwner{IndexVersionId: v.ID})
		if err != nil || out.GetIndexVersionId() != v.ID || out.GetState() != v.State || out.GetFingerprint() != chunking.HashJSON(pins) || len(out.GetDocuments()) != len(pins) {
			return fmt.Errorf("Data reference observation differs: %s", v.ID)
		}
		for i, p := range pins {
			if !proto.Equal(p, out.Documents[i]) {
				return fmt.Errorf("Data reference pins differ: %s", v.ID)
			}
		}
	}
	for _, d := range documents {
		if _, e = data.Read(ctx, d); e != nil {
			return fmt.Errorf("canonical/source availability or canonical bytes differ: %s", d.CanonicalID)
		}
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"status": "passed", "versions": len(r.Versions), "canonical_documents": len(documents), "scope": "OpenSearch GET and Data read RPCs only"})
}
func main() {
	if e := run(); e != nil {
		fmt.Fprintln(os.Stderr, "FAIL:", e)
		os.Exit(1)
	}
}
