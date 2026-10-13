package creation_test

import (
 "encoding/json"
 "fmt"
 "os"
 "path/filepath"
 "testing"
 "model-platform/internal/remotenode"
)
func TestRemotePlacementPersistsAndReplaySurvivesDisable(t *testing.T){
 f:=newFixture(t);path:=filepath.Join(t.TempDir(),"nodes.json");t.Setenv("EXECUTION_NODES_FILE",path);t.Setenv("DISPATCHER_NODE_NAME","local")
 node:=remotenode.Node{Name:"worker",SSHHost:"worker-ssh",Container:"model-node-agent",Architecture:"amd64",DaemonID:"test-daemon",Enabled:true}
 save:=func(){data,_:=json.Marshal(remotenode.Catalog{Version:1,Nodes:[]remotenode.Node{node}});if err:=os.WriteFile(path,data,0600);err!=nil{t.Fatal(err)}};save()
 body:=fmt.Sprintf(`{"source_model_version_id":%q,"resources":{"node_name":"worker","gpu_ids":[0]}}`,f.versionID)
 code,first:=f.request(t,"POST","/api/training-runs",body,"remote-create");if code!=202{t.Fatal(code,first)}
 for _,path:=range []string{"/api/training-runs/"+first["training_run_id"].(string),"/api/operations/"+first["operation_id"].(string)}{
  code,view:=f.request(t,"GET",path,"");if code!=200||view["resources"].(map[string]any)["node_name"]!="worker"{t.Fatal("placement lost",view)}
 }
 node.Enabled=false;save()
 code,replay:=f.request(t,"POST","/api/training-runs",body,"remote-create");if code!=202||replay["training_run_id"]!=first["training_run_id"]{t.Fatal("disable changed existing request replay",code,replay)}
 code,_=f.request(t,"POST","/api/training-runs",body,"remote-new");if code!=400{t.Fatal("disabled node accepted",code)}
 if count(t,f.training,`SELECT count(*) FROM training_runs`)!=1{t.Fatal("rejected placement created work")}
}
func TestUnknownRemoteNodeDoesNotCreateWork(t *testing.T){
 f:=newFixture(t);t.Setenv("EXECUTION_NODES_FILE","");t.Setenv("DISPATCHER_NODE_NAME","")
 body:=fmt.Sprintf(`{"source_model_version_id":%q,"resources":{"node_name":"missing","gpu_ids":[0]}}`,f.versionID)
 if code,res:=f.request(t,"POST","/api/training-runs",body);code!=400{t.Fatal(code,res)}
 if count(t,f.training,`SELECT count(*) FROM training_runs`)!=0||count(t,f.ops,`SELECT count(*) FROM operations`)!=0{t.Fatal("unknown placement created durable work")}
}
