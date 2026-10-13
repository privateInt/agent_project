package main

import (
    "bytes"
    "context"
    "crypto/sha256"
    "encoding/hex"
    "errors"
    "io"
    "net/http"
    "strings"
    "testing"
    "time"

    operationv1 "model-platform/gen/operation/v1"
    "google.golang.org/grpc"
)

type transferTransport func(*http.Request)(*http.Response,error)
func(f transferTransport) RoundTrip(r *http.Request)(*http.Response,error){return f(r)}
type memoryTransferStore struct{ fail bool; payload []byte }
func(s *memoryTransferStore)Put(_ context.Context,_ string,r io.Reader,_ int64,_ string)error{
    data,err:=io.ReadAll(r);if err!=nil{return err};s.payload=data
    if s.fail{return errors.New("synthetic MinIO refusal")};return nil
}

func TestTransferProgressCountsOnlyStoredFiles(t *testing.T){
    for _,c:=range []struct{name string; length int64; code int; uploadFailure bool}{
        {"known",7,200,false},{"unknown",-1,200,false},{"unauthorized",0,401,false},{"upload_failure",7,200,true},
    } {t.Run(c.name,func(t *testing.T){
        old:=http.DefaultClient;t.Cleanup(func(){http.DefaultClient=old})
        t.Setenv("TMPDIR",t.TempDir())
        http.DefaultClient=&http.Client{Transport:transferTransport(func(r *http.Request)(*http.Response,error){
            if r.Header.Get("Authorization")!="Bearer test-only-token"{t.Fatal("token did not reach download")}
            return &http.Response{StatusCode:c.code,ContentLength:c.length,Body:io.NopCloser(strings.NewReader("weights")),Header:make(http.Header),Request:r},nil
        })}
        store:=&memoryTransferStore{fail:c.uploadFailure}
        var samples []*operationv1.TransferProgress
        siblings:=[]struct{Filename string `json:"rfilename"`}{{"model.safetensors"}}
        files,total,err:=transferFiles(context.Background(),store,"example/repo","commit",siblings,"prefix/","test-only-token",func(p *operationv1.TransferProgress){samples=append(samples,p)})
        last:=samples[len(samples)-1]
        if c.code==401||c.uploadFailure {
            if err==nil||last.GetFilesCompleted()!=0||last.GetStage()=="stored"{t.Fatal("failed transfer counted as saved",err,last)}
            if c.code==401&&!strings.Contains(err.Error(),"status 401"){t.Fatal(err)}
        }else{
            hash:=sha256.Sum256([]byte("weights"))
            if err!=nil||total!=7||len(files)!=1||files[0].SHA256!=hex.EncodeToString(hash[:])||!bytes.Equal(store.payload,[]byte("weights"))||last.GetFilesCompleted()!=1||last.GetStage()!="stored"{t.Fatal("content or counters changed",err,last)}
        }
        for i,p:=range samples{
            if p.GetSequence()!=int64(i+1){t.Fatal("sample sequence not increasing")}
            if p.GetFilesTotal()!=1||p.GetCurrentFile()!="model.safetensors"{t.Fatal("wrong file progress")}
        }
        if c.length==-1 && samples[1].GetBytesTotal()!=0 {t.Fatal("unknown length became an estimate")}
    })}
}

type unavailableProgressClient struct{operationv1.OperationServiceClient;calls int}
func(c *unavailableProgressClient)UpdatePhase(context.Context,*operationv1.UpdateOperationRequest,...grpc.CallOption)(*operationv1.Operation,error){c.calls++;return nil,errors.New("offline")}
func TestTransferMeterThrottlesAndReportFailureBacksOff(t *testing.T){
    var samples []*operationv1.TransferProgress
    m:=&transferMeter{report:func(p *operationv1.TransferProgress){samples=append(samples,p)}}
    m.emit(true)
    if _,err:=m.Write([]byte("abc"));err!=nil{t.Fatal(err)}
    if len(samples)!=1||samples[0].GetBytesCompleted()!=0{t.Fatal("sample was mutated or throttling failed")}
    m.last=time.Now().Add(-4*time.Second)
    m.Write([]byte("de"))
    if len(samples)!=2||samples[1].GetBytesCompleted()!=5{t.Fatal("written bytes not measured")}
    unavailable:=&unavailableProgressClient{}
    report:=progressReport(context.Background(),unavailable,"test")
    report(samples[0]);report(samples[1])
    if unavailable.calls!=1{t.Fatal("offline progress RPC was retried without backoff")}
}
