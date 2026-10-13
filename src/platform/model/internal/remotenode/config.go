// Package remotenode defines explicit, operator-approved Docker placement.
// Names are stable physical-node identities, not a load-balancing pool.
package remotenode

import (
 "bytes"
 "encoding/json"
 "fmt"
 "io"
 "os"
 "regexp"
 "strings"
)

type Node struct {
 Name string `json:"name"`
 SSHHost string `json:"ssh_host"`
 Container string `json:"container"`
 Architecture string `json:"architecture"`
 DaemonID string `json:"daemon_id"`
 Enabled bool `json:"enabled"`
}
type Catalog struct { Version int `json:"version"`; Nodes []Node `json:"nodes"` }
var safeName=regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`)
func ValidName(s string)bool{return safeName.MatchString(s)}
func Decode(data []byte,local string)(Catalog,error){
 var c Catalog
 d:=json.NewDecoder(bytes.NewReader(data));d.DisallowUnknownFields()
 if err:=d.Decode(&c);err!=nil{return c,fmt.Errorf("invalid execution node configuration")}
 if d.Decode(new(any))!=io.EOF || c.Version!=1 || len(c.Nodes)>32 {return c,fmt.Errorf("invalid execution node configuration version/count")}
 names,hosts,daemons:=map[string]bool{},map[string]bool{},map[string]bool{}
 for _,n:=range c.Nodes {
  if !ValidName(n.Name)||!ValidName(n.SSHHost)||!ValidName(n.Container)||n.Name==local||names[n.Name]||hosts[n.SSHHost] {return c,fmt.Errorf("invalid or duplicate execution node identity")}
  // First remote release uses the already supported amd64 training image.
  if n.Architecture!="amd64" {return c,fmt.Errorf("remote training currently requires amd64")}
  if len(n.DaemonID)>128 || strings.ContainsAny(n.DaemonID," \n\r\t") || (n.Enabled && n.DaemonID=="") {return c,fmt.Errorf("enabled node requires a pinned Docker daemon ID")}
  if n.DaemonID!="" && daemons[n.DaemonID] {return c,fmt.Errorf("Docker daemon registered under multiple names")}
  names[n.Name]=true;hosts[n.SSHHost]=true;if n.DaemonID!=""{daemons[n.DaemonID]=true}
 }
 return c,nil
}
func Load()(Catalog,error){
 path:=os.Getenv("EXECUTION_NODES_FILE")
 if path=="" {return Catalog{Version:1},nil}
 data,err:=os.ReadFile(path);if err!=nil{return Catalog{},fmt.Errorf("execution node configuration unavailable")}
 if len(data)>64<<10 {return Catalog{},fmt.Errorf("execution node configuration too large")}
 return Decode(data,strings.TrimSpace(os.Getenv("DISPATCHER_NODE_NAME")))
}
func(c Catalog)Find(name string)(Node,bool){for _,n:=range c.Nodes{if n.Name==name{return n,true}};return Node{},false}
func(c Catalog)ValidateTraining(name string,count int32)error{
 if name==""||name==strings.TrimSpace(os.Getenv("DISPATCHER_NODE_NAME")){return nil}
 n,ok:=c.Find(name);if !ok||!n.Enabled{return fmt.Errorf("requested remote node is not enabled")}
 if count!=1 {return fmt.Errorf("remote training currently requires exactly one GPU on the selected node")}
 return nil
}
