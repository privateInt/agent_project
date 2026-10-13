package gpuadmission

import (
 "context"
 "fmt"
 "strings"
)

type CancelledError struct{}
func(*CancelledError)Error()string{return "operation launch was cancelled"}
type CreationUnconfirmedError struct{}
func(*CreationUnconfirmedError)Error()string{return "unobserved creation intent; cancellation cannot yet confirm stop"}

// CancelOperation serializes with ALL local GPU creators. Its durable tombstone
// blocks a dispatcher paused before admission, even after controller restart.
// Unobserved creation intents remain uncertain; there is deliberately no TTL.
func(g Guard)CancelOperation(ctx context.Context,id string,stop func(context.Context,Container)error)error{
 if !safeName.MatchString(id)||g.Dir==""||g.Inventory==nil||stop==nil{return fmt.Errorf("cancellation guard unavailable")}
 return g.locked(ctx,func(s *State)error{
  if s.CancelledOperations==nil{s.CancelledOperations=map[string]bool{}}
  s.CancelledOperations[id]=true
  if err:=g.save(s);err!=nil{return err}
  items,err:=g.Inventory(ctx);if err!=nil{return err}
  prefix:="model-op-"+id
  belongs:=func(c Container)bool{return c.Config.Labels["model-platform.operation_id"]==id || strings.TrimPrefix(c.Name,"/")==prefix || strings.HasPrefix(strings.TrimPrefix(c.Name,"/"),prefix+"-")}
  observed:=map[string]bool{}
  for _,c:=range items {
   if !belongs(c){continue}
   name:=strings.TrimPrefix(c.Name,"/");observed[name]=true
   if claim,ok:=s.Claims[name];ok{claim.Seen=true;s.Claims[name]=claim}
  }
  if err:=g.save(s);err!=nil{return err} // survive a crash after removing a never-started container
  for _,c:=range items {
   if !belongs(c){continue}
   // Callback validates identity even for exited containers; never stop a
   // same-name foreign container or delete completed logs.
   if err:=stop(ctx,c);err!=nil{return err}
  }
  for name,claim:=range s.Claims {
   if claim.OperationID==id && !claim.Seen && !observed[name] {return &CreationUnconfirmedError{}}
  }
  again,err:=g.Inventory(ctx);if err!=nil{return err}
  for _,c:=range again {if belongs(c) && occupied(c){return fmt.Errorf("container stop is unconfirmed")}}
  for name,claim:=range s.Claims{if claim.OperationID==id{delete(s.Claims,name)}}
  return g.save(s)
 })
}
