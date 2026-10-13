package gpuadmission

import (
 "context"
 "fmt"
 "strings"
)

// WithUnstartedProof holds the SAME lock as all creators until the caller fences
// the expired token in Operation. It never deletes a claim or stops a container.
// Absence of a container alone cannot establish absence of uncertain creation.
func(g Guard)WithUnstartedProof(ctx context.Context,operationID string,action func()error)error{
 if !safeName.MatchString(operationID)||g.Dir==""||g.Inventory==nil{return fmt.Errorf("GPU recovery identity/volume unavailable")}
 return g.locked(ctx,func(s *State)error{
  for _,claim:=range s.Claims {if claim.OperationID==operationID{return fmt.Errorf("GPU creation intent exists; manual inspection required")}}
  items,err:=g.Inventory(ctx);if err!=nil{return err}
  prefix:="model-op-"+operationID
  for _,c:=range items {
   name:=strings.TrimPrefix(c.Name,"/")
   if c.Config.Labels["model-platform.operation_id"]==operationID || name==prefix || strings.HasPrefix(name,prefix+"-"){return fmt.Errorf("Job container exists; refusing unstarted recovery")}
  }
  return action()
 })
}
