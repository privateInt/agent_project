package trainingparallel

import (
 "encoding/json"
 "os"
 "path/filepath"
 "testing"
)
func valid()Result{return Result{Strategy:"ddp",WorldSize:2,EffectiveBatchSize:8,Ranks:[]Rank{
 {Rank:0,LocalRank:0,WorldSize:2,TrainingRunID:"run",DeviceUUID:"GPU-a",Initialized:true,DDPWrapped:true,Completed:true,GlobalStep:3},
 {Rank:1,LocalRank:1,WorldSize:2,TrainingRunID:"run",DeviceUUID:"GPU-b",Initialized:true,DDPWrapped:true,Completed:true,GlobalStep:3},
}}}
func TestObservedDDPWorkersRequired(t *testing.T){
 if err:=Validate(valid(),"run",2,3,8);err!=nil{t.Fatal(err)}
 for name,mutate:=range map[string]func(*Result){
  "missing-rank":func(r *Result){r.Ranks=r.Ranks[:1]},
  "one-gpu-fallback":func(r *Result){r.WorldSize=1},
  "duplicate-device":func(r *Result){r.Ranks[1].DeviceUUID="GPU-a"},
  "duplicate-rank":func(r *Result){r.Ranks[1].Rank=0},
  "unwrapped":func(r *Result){r.Ranks[1].DDPWrapped=false},
  "unfinished":func(r *Result){r.Ranks[1].Completed=false},
  "stale-run":func(r *Result){r.Ranks[1].TrainingRunID="old"},
  "wrong-steps":func(r *Result){r.Ranks[1].GlobalStep=2},
  "batch":func(r *Result){r.EffectiveBatchSize=4},
 }{t.Run(name,func(t *testing.T){r:=valid();mutate(&r);if Validate(r,"run",2,3,8)==nil{t.Fatal("bad evidence accepted")}})}
}
func TestReadNeverAcceptsMissingRankFile(t *testing.T){
 dir:=t.TempDir();r:=valid();data,_:=json.Marshal(r.Ranks[0])
 if err:=os.WriteFile(filepath.Join(dir,"rank-0.json"),data,0600);err!=nil{t.Fatal(err)}
 if _,err:=Read(dir,"run",2,3,8);err==nil{t.Fatal("missing rank accepted")}
 data,_=json.Marshal(r.Ranks[1]);if err:=os.WriteFile(filepath.Join(dir,"rank-1.json"),data,0600);err!=nil{t.Fatal(err)}
 if _,err:=Read(dir,"run",2,3,8);err!=nil{t.Fatal(err)}
}
