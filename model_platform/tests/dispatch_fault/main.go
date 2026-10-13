// Test-only driver. The production dispatcher/operation/reconciler binaries
// remain unchanged. When named docker, this pauses immediately before launch.
package main

import (
    "context"
    "encoding/json"
    "errors"
    "flag"
    "fmt"
    "os"
    "os/exec"
    "path/filepath"
    "regexp"
    "time"

    "github.com/google/uuid"
    "github.com/jackc/pgx/v5"
    commonv1 "model-platform/gen/common/v1"
    deploymentv1 "model-platform/gen/deployment/v1"
    "google.golang.org/grpc"
    "google.golang.org/grpc/credentials/insecure"
    healthv1 "google.golang.org/grpc/health/grpc_health_v1"
)

type launchEvidence struct {
    OperationID string `json:"operation_id"`
    ResourceID string `json:"resource_id"`
    DispatchToken string `json:"dispatch_token"`
    ExecutionType string `json:"execution_type"`
}

func launchFromArgs(args []string) (launchEvidence,error) {
    var e launchEvidence
    if len(args)==0 || args[0]!="run" { return e,fmt.Errorf("only a deployment docker run can be paused") }
    env:=map[string]string{}
    for i:=1;i<len(args);i++ {
        if args[i]=="--gpus" { return e,fmt.Errorf("GPU launch denied by CPU test shim") }
        if args[i]=="-e" && i+1<len(args) {
            i++
            for j,c:=range args[i] { if c=='=' {env[args[i][:j]]=args[i][j+1:];break} }
        }
    }
    e=launchEvidence{env["OPERATION_ID"],env["RESOURCE_ID"],env["DISPATCH_TOKEN"],env["EXECUTION_TYPE"]}
    for _,id:=range []string{e.OperationID,e.ResourceID,e.DispatchToken} {if _,err:=uuid.Parse(id);err!=nil{return e,fmt.Errorf("invalid launch identity")}}
    if e.ExecutionType!="deployment.apply" || env["RESOURCE_TYPE"]!="deployment" || env["REQUESTED_GPU_COUNT"]!="0" || env["REQUESTED_GPU_IDS"]!="" {
        return e,fmt.Errorf("only CPU demo deployment is allowed")
    }
    return e,nil
}

func dockerShim()error {
    args:=os.Args[1:]
    // Image inspection is read-only and uses the real CLI from the control image.
    if len(args)>=2 && args[0]=="image" && args[1]=="inspect" {
        cmd:=exec.Command("/usr/local/bin/docker",args...)
        cmd.Stdout=os.Stdout;cmd.Stderr=os.Stderr
        return cmd.Run()
    }
    if _,err:=uuid.Parse(os.Getenv("FAULT_RUN_ID"));err!=nil{return fmt.Errorf("test run identity is required")}
    evidence,err:=launchFromArgs(args);if err!=nil{return err}
    data,err:=json.Marshal(evidence);if err!=nil{return err}
    // The host waits for the atomic rename, checks the DB claim, then kills
    // the isolated dispatcher container. No docker run is ever forwarded.
    if err=os.WriteFile("/fault-evidence/claim.tmp",data,0644);err!=nil{return err}
    if err=os.Rename("/fault-evidence/claim.tmp","/fault-evidence/claim.json");err!=nil{return err}
    for {time.Sleep(time.Hour)}
}

func checkDatabaseScope(ctx context.Context,schema string)error {
    if !regexp.MustCompile(`^dispatch_smoke_[0-9a-f]{32}$`).MatchString(schema){return fmt.Errorf("invalid isolated schema")}
    cfg,err:=pgx.ParseConfig(os.Getenv("POSTGRES_URL"));if err!=nil{return fmt.Errorf("invalid test PostgreSQL configuration")}
    if cfg.RuntimeParams["search_path"]!=schema{return fmt.Errorf("test search_path must contain only the isolated schema")}
    for _,database:=range []string{"model_operations","model_deployment"} {
        c:=cfg.Copy();c.Database=database
        conn,err:=pgx.ConnectConfig(ctx,c);if err!=nil{return fmt.Errorf("connect isolated %s: %w",database,err)}
        var current,path string
        err=conn.QueryRow(ctx,`SELECT current_schema(),current_setting('search_path')`).Scan(&current,&path)
        conn.Close(ctx)
        if err!=nil{return fmt.Errorf("check isolated schema: %w",err)}
        if current!=schema||path!=schema{return fmt.Errorf("refusing public/fallback schema in %s",database)}
    }
    fmt.Println(`{"database_scope_verified":true}`)
    return nil
}

func run()error {
    if filepath.Base(os.Args[0])=="docker"{return dockerShim()}
    mode:=flag.String("mode","health","health, deploy, or db-scope")
    address:=flag.String("address","","gRPC endpoint")
    model:=flag.String("model-version-id","","ready snapshot UUID")
    schema:=flag.String("schema","","isolated schema")
    flag.Parse()
    ctx,cancel:=context.WithTimeout(context.Background(),35*time.Second);defer cancel()
    if *mode=="db-scope"{return checkDatabaseScope(ctx,*schema)}
    if *address==""{return fmt.Errorf("address is required")}
    conn,err:=grpc.NewClient(*address,grpc.WithTransportCredentials(insecure.NewCredentials()));if err!=nil{return err};defer conn.Close()
    if *mode=="health" {
        for ctx.Err()==nil {
            call,stop:=context.WithTimeout(ctx,2*time.Second)
            r,e:=healthv1.NewHealthClient(conn).Check(call,&healthv1.HealthCheckRequest{});stop()
            if e==nil && r.GetStatus()==healthv1.HealthCheckResponse_SERVING{fmt.Println(`{"serving":true}`);return nil}
            select {case <-ctx.Done():return ctx.Err();case <-time.After(time.Second):}
        }
        return ctx.Err()
    }
    if *mode!="deploy"{return fmt.Errorf("unknown mode")}
    if _,err=uuid.Parse(*model);err!=nil{return fmt.Errorf("valid model-version-id is required")}
    // Exactly one mutating request. An uncertain response is never retried.
    r,err:=deploymentv1.NewDeploymentServiceClient(conn).DeployModel(ctx,&deploymentv1.DeployModelRequest{ModelVersionId:*model,Resources:&commonv1.ExecutionResources{GpuCount:0}})
    if err!=nil{return err}
    return json.NewEncoder(os.Stdout).Encode(map[string]string{"operation_id":r.GetOperationId(),"deployment_id":r.GetDeploymentId()})
}
func main(){if err:=run();err!=nil{var exit *exec.ExitError;if errors.As(err,&exit){os.Exit(exit.ExitCode())};fmt.Fprintln(os.Stderr,err);os.Exit(1)}}
