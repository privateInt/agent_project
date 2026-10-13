package app

import (
 "context"
 "errors"
 "testing"
 "model-platform/internal/remotenode"
)
func TestNodeRejectionRequiresConfirmedFailureReport(t *testing.T){
 for _,lost:=range []bool{false,true}{d,o,_,r:=fixture("dispatching",true);r.err=&remotenode.RejectedError{Message:"node disabled"};if lost{o.errorFail=errors.New("reply lost")}
  err:=d.Handle(context.Background(),testPayload);if o.fails!=1||(err!=nil)!=lost{t.Fatal("rejection acknowledgement mismatch",err)}
 }
}
