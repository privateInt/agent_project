// Package trainingparallel verifies observed DDP workers, not just requested GPUs.
package trainingparallel

import (
 "encoding/json"
 "fmt"
 "os"
 "path/filepath"
)

type Rank struct {
 Rank int `json:"rank"`
 LocalRank int `json:"local_rank"`
 WorldSize int `json:"world_size"`
 TrainingRunID string `json:"training_run_id"`
 DeviceUUID string `json:"device_uuid"`
 Initialized bool `json:"initialized"`
 DDPWrapped bool `json:"ddp_wrapped"`
 Completed bool `json:"completed"`
 GlobalStep int64 `json:"global_step"`
}
type Result struct {
 Strategy string `json:"strategy"`
 WorldSize int `json:"world_size"`
 EffectiveBatchSize int64 `json:"effective_batch_size"`
 Ranks []Rank `json:"ranks,omitempty"`
}

func Validate(r Result,runID string,count int,steps,batch int64)error {
 if count<2 || r.Strategy!="ddp" || r.WorldSize!=count || r.EffectiveBatchSize!=batch || len(r.Ranks)!=count {return fmt.Errorf("distributed result does not match requested parallelism")}
 seen:=map[int]bool{};devices:=map[string]bool{}
 for _,rank:=range r.Ranks {
  if rank.Rank<0 || rank.Rank>=count || rank.LocalRank!=rank.Rank || seen[rank.Rank] || rank.WorldSize!=count || rank.TrainingRunID!=runID || !rank.Initialized || !rank.DDPWrapped || !rank.Completed || rank.GlobalStep!=steps || rank.DeviceUUID=="" || devices[rank.DeviceUUID] {
   return fmt.Errorf("missing, duplicate or inconsistent DDP worker evidence")
  }
  seen[rank.Rank]=true;devices[rank.DeviceUUID]=true
 }
 return nil
}
func Read(dir,runID string,count int,steps,batch int64)(Result,error) {
 out:=Result{Strategy:"ddp",WorldSize:count,EffectiveBatchSize:batch}
 for rank:=0;rank<count;rank++ {
  data,err:=os.ReadFile(filepath.Join(dir,fmt.Sprintf("rank-%d.json",rank)));if err!=nil{return out,err}
  var r Rank;if err:=json.Unmarshal(data,&r);err!=nil{return out,err}
  if r.Rank!=rank{return out,fmt.Errorf("DDP rank filename/content mismatch")};out.Ranks=append(out.Ranks,r)
 }
 return out,Validate(out,runID,count,steps,batch)
}
