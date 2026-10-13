package jobcontrol

import (
 "bytes"
 "context"
 "encoding/binary"
 "fmt"
 "io"
 "strconv"
 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
)

// Retain only the last max bytes while consuming the stream for integrity.
type tailBuffer struct{data []byte; max int; clipped bool}
func(b *tailBuffer)Write(p []byte)(int,error){
 n:=len(p)
 if len(b.data)+n>b.max{b.clipped=true}
 if n>=b.max {b.data=append(b.data[:0],p[n-b.max:]...)}else{
  drop:=len(b.data)+n-b.max;if drop>0{copy(b.data,b.data[drop:]);b.data=b.data[:len(b.data)-drop]}
  b.data=append(b.data,p...)
 }
 return n,nil
}
func(b *tailBuffer)lines(n int)[]byte{
 data:=b.data;end:=len(data)
 if end>0&&data[end-1]=='\n'{end--}
 for i:=end-1;i>=0;i--{if data[i]=='\n'{n--;if n==0{return data[i+1:]}}}
 return data
}
func readMultiplex(src io.Reader,stdout,stderr io.Writer)error{
 var header [8]byte
 var total int64
 for{
  _,err:=io.ReadFull(src,header[:]);if err==io.EOF{return nil};if err!=nil{return fmt.Errorf("incomplete Docker log frame")}
  if header[1]!=0||header[2]!=0||header[3]!=0||(header[0]!=1&&header[0]!=2){return fmt.Errorf("invalid Docker log frame")}
  size:=int64(binary.BigEndian.Uint32(header[4:]));total+=size
  if total>32<<20{return fmt.Errorf("log window exceeds 32 MiB; reduce tail_lines")}
  out:=stdout;if header[0]==2{out=stderr}
  if _,err=io.CopyN(out,src,size);err!=nil{return fmt.Errorf("incomplete Docker log frame")}
 }
}

// Older local Jobs predate the execution_node label. A controller configured
// with a node name later can read their retained logs, but only after both
// Operation and container have finished. This exception never authorizes stop
// or removal; Cancel continues to use the strict node check in validate.
func validateLogIdentity(c inspection,op *operationv1.Operation,node string)error{
 if err:=validateIdentity(c,op,false);err!=nil{return err}
 actualNode,hasNode:=c.Config.Labels["model-platform.execution_node"]
 if actualNode==node{return nil}
 terminal:=op.GetStatus()=="succeeded"||op.GetStatus()=="failed"||op.GetStatus()=="cancelled"
 if !hasNode&&op.GetResources().GetNodeName()==""&&c.Config.Labels["model-platform.requested_node"]==""&&
  terminal&&c.State.Status=="exited"&&!c.State.Running&&!c.State.Paused&&!c.State.Restarting{return nil}
 return fmt.Errorf("training log container node mismatch")
}

func(r *Runtime)Logs(ctx context.Context,op *operationv1.Operation,lines,max int)(*trainingv1.TrainingLogs,error){
 out:=&trainingv1.TrainingLogs{OperationId:op.GetOperationId(),TrainingRunId:op.GetResourceId(),Source:"none",Availability:"not_started",TailLines:int32(lines),MaxBytes:int32(max)}
 if op.GetResources().GetNodeName()!=""&&op.GetResources().GetNodeName()!=r.Node{return nil,fmt.Errorf("remote logs are not supported")}
 if !uuidID.MatchString(op.GetOperationId())||op.GetResourceType()!="training_run"||op.GetExecutionType()!="training.run"||op.GetDispatchProtocol()!=1{return nil,fmt.Errorf("unsupported training execution identity")}
 if op.GetDispatchToken()==""{return out,nil}
 if !uuidID.MatchString(op.GetDispatchToken()){return nil,fmt.Errorf("invalid dispatch identity")}
 name:="model-op-"+op.GetOperationId()+"-"+op.GetDispatchToken()
 c,found,err:=r.inspect(ctx,name);if err!=nil{return nil,err}
 if found{
  if err:=validateLogIdentity(c,op,r.Node);err!=nil{return nil,err}
  res,err:=r.request(ctx,"GET","/containers/"+c.ID+"/logs?stdout=1&stderr=1&timestamps=1&tail="+strconv.Itoa(lines));if err!=nil{return nil,fmt.Errorf("Docker logs unavailable")}
  defer res.Body.Close()
  if res.StatusCode==200{
   stdout,stderr:=&tailBuffer{max:max},&tailBuffer{max:max}
   if err:=readMultiplex(res.Body,stdout,stderr);err!=nil{return nil,err}
   out.Source,out.Availability="docker","available";out.Stdout,out.Stderr=textUTF8(stdout.lines(lines)),textUTF8(stderr.lines(lines));out.Truncated=stdout.clipped||stderr.clipped
   return out,nil
  }
  if res.StatusCode!=404{return nil,fmt.Errorf("Docker logs unavailable")}
  // GC may remove a container between inspection and log retrieval.
 }
 if r.Archive!=nil{
  stored,err:=r.Archive.Read(ctx,op,lines,max);if err!=nil{return nil,err};if stored!=nil{return stored,nil}
 }
 out.Availability="not_retained"
 if op.GetExecutionStartedAt()==nil{out.Availability="not_started"}
 return out,nil
}

// Verify archive metadata/manifest using byte equality of digests, never expose
// internal execution credentials in the public response.
func equalDigest(a,b string)bool{return bytes.Equal([]byte(a),[]byte(b))}
