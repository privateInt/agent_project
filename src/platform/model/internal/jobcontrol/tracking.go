package jobcontrol

import (
 "bytes"
 "context"
 "encoding/json"
 "fmt"
 "io"
 "net/http"
 "net/url"
 "strings"
 "time"
)

// Update tracking only AFTER Docker stop confirmation. Finished/failed MLflow
// results stay terminal; an active run left behind by SIGTERM becomes KILLED.
func CancelTracking(ctx context.Context,base,runID string)error{
 if base==""||runID==""{return fmt.Errorf("tracking identity unavailable")}
 client:=&http.Client{Timeout:10*time.Second}
 endpoint:=strings.TrimRight(base,"/")+"/api/2.0/mlflow/runs/"
 req,err:=http.NewRequestWithContext(ctx,"GET",endpoint+"get?run_id="+url.QueryEscape(runID),nil);if err!=nil{return err}
 res,err:=client.Do(req);if err!=nil{return err}
 var current struct{Run struct{Info struct{RunID string `json:"run_id"`;Status string `json:"status"`} `json:"info"`} `json:"run"`}
 err=json.NewDecoder(io.LimitReader(res.Body,1<<20)).Decode(&current);res.Body.Close()
 if err!=nil||res.StatusCode!=200||current.Run.Info.RunID!=runID{return fmt.Errorf("tracking lookup unavailable")}
 switch current.Run.Info.Status{case "FINISHED","FAILED","KILLED":return nil;case "RUNNING","SCHEDULED":default:return fmt.Errorf("unknown tracking state")}
 raw,_:=json.Marshal(map[string]any{"run_id":runID,"status":"KILLED","end_time":time.Now().UnixMilli()})
 req,err=http.NewRequestWithContext(ctx,"POST",endpoint+"update",bytes.NewReader(raw));if err!=nil{return err};req.Header.Set("Content-Type","application/json")
 res,err=client.Do(req);if err!=nil{return err};defer res.Body.Close()
 if res.StatusCode!=200{return fmt.Errorf("tracking update unavailable")};return nil
}
