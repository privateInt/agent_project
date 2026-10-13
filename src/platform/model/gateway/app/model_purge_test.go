package app

import (
	"context"
	"github.com/gofiber/fiber/v3"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/timestamppb"
	"model-platform/gateway/grpcclient"
	managementv1 "model-platform/gen/management/v1"
	"net/http/httptest"
	"strings"
	"testing"
)

type permanentModelClient struct {
	managementv1.ManagementServiceClient
	calls int
	bad   bool
}

func (f *permanentModelClient) PurgeModel(_ context.Context, r *managementv1.GetModelRequest, _ ...grpc.CallOption) (*managementv1.Model, error) {
	f.calls++
	v := &managementv1.Model{ModelId: r.ModelId, PurgeState: "purged", PurgedAt: timestamppb.Now()}
	if f.bad {
		v.PurgeState = "purging"
	}
	return v, nil
}
func TestModelPurgeRESTRejectsScopeAndIncompleteReceipt(t *testing.T) {
	f := &permanentModelClient{}
	app := fiber.New()
	New(&grpcclient.Clients{Management: f}).Register(app)
	path := "/api/models/" + uuid.NewString() + "/purge"
	for _, body := range []string{"{}", "true"} {
		r, e := app.Test(httptest.NewRequest("POST", path, strings.NewReader(body)))
		if e != nil {
			t.Fatal(e)
		}
		r.Body.Close()
		if r.StatusCode != 400 {
			t.Fatal(r.StatusCode)
		}
	}
	if f.calls != 0 {
		t.Fatal("invalid scope forwarded")
	}
	r, e := app.Test(httptest.NewRequest("POST", path, nil))
	if e != nil {
		t.Fatal(e)
	}
	r.Body.Close()
	if r.StatusCode != 200 {
		t.Fatal(r.StatusCode)
	}
	f.bad = true
	r, e = app.Test(httptest.NewRequest("POST", path, nil))
	if e != nil {
		t.Fatal(e)
	}
	r.Body.Close()
	if r.StatusCode == 200 {
		t.Fatal("pending deletion reported complete")
	}
}
