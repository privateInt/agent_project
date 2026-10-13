package jobcontrol

import(
 "context"
 "encoding/json"
 "net/http"
 "net/http/httptest"
 "testing"
)
func TestTrackingCancellationPreservesTerminalRuns(t *testing.T){
 for _,state:=range []string{"RUNNING","FINISHED","FAILED","KILLED"}{t.Run(state,func(t *testing.T){
  updates:=0
  server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){
   if r.Method=="GET"{json.NewEncoder(w).Encode(map[string]any{"run":map[string]any{"info":map[string]any{"run_id":"tracking","status":state}}});return}
   updates++;var body map[string]any;if json.NewDecoder(r.Body).Decode(&body)!=nil||body["status"]!="KILLED"||body["run_id"]!="tracking"{t.Error("invalid tracking cancellation",body)}
   w.Write([]byte("{}"))
  }));defer server.Close()
  if err:=CancelTracking(context.Background(),server.URL,"tracking");err!=nil{t.Fatal(err)}
  if (updates==1)!=(state=="RUNNING"){t.Fatal("terminal tracking overwritten",updates)}
 })}
}
