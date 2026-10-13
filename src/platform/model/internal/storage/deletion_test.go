package storage

import (
	"context"
	"fmt"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPurgeModelRemovesAllObjectVersionsAndRejectsStorageErrors(t *testing.T) {
	id := uuid.NewString()
	prefix := "models/" + id + "/"
	objects := map[string]string{"v1": prefix + "weights", "v2": prefix + "weights", "v3": prefix + "_manifest.json"}
	fail := true
	deletes := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "GET":
			if r.URL.Query().Get("prefix") != prefix {
				t.Errorf("unscoped prefix %s", r.URL.Query().Get("prefix"))
			}
			w.Header().Set("Content-Type", "application/xml")
			fmt.Fprint(w, `<ListVersionsResult><Name>model-artifacts</Name><IsTruncated>false</IsTruncated>`)
			for version, key := range objects {
				fmt.Fprintf(w, `<Version><Key>%s</Key><VersionId>%s</VersionId><IsLatest>true</IsLatest><LastModified>2026-09-16T00:00:00Z</LastModified><Size>1</Size></Version>`, key, version)
			}
			fmt.Fprint(w, `</ListVersionsResult>`)
		case "DELETE":
			deletes++
			v := r.URL.Query().Get("versionId")
			if _, ok := objects[v]; !ok {
				t.Errorf("wrong version %q", v)
			}
			if fail {
				w.WriteHeader(403)
				fmt.Fprint(w, `<Error><Code>AccessDenied</Code><Message>locked</Message></Error>`)
				return
			}
			delete(objects, v)
			w.WriteHeader(204)
		default:
			t.Errorf("unexpected method %s", r.Method)
			w.WriteHeader(500)
		}
	}))
	defer server.Close()
	client, e := minio.New(strings.TrimPrefix(server.URL, "http://"), &minio.Options{Region: "us-east-1"})
	if e != nil {
		t.Fatal(e)
	}
	store := &MinIO{client: client, bucket: "model-artifacts"}
	if e = store.PurgeModel(context.Background(), "../all"); e == nil || deletes != 0 {
		t.Fatal("invalid scope accepted", e)
	}
	if e = store.PurgeModel(context.Background(), id); e == nil || len(objects) != 3 {
		t.Fatal("object lock ignored", e)
	}
	fail = false
	if e = store.PurgeModel(context.Background(), id); e != nil || len(objects) != 0 {
		t.Fatal("versions retained", e)
	}
	previous := deletes
	if e = store.PurgeModel(context.Background(), id); e != nil || deletes != previous {
		t.Fatal("retry not idempotent", e)
	}
}
