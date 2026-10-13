package main
import(
 "context"
 "fmt"
 "log"
 "strings"
 "time"
 "data-platform/dispatcher/app"
 "data-platform/dispatcher/executor"
 "data-platform/dispatcher/registry"
 "data-platform/internal/config"
 "github.com/twmb/franz-go/pkg/kgo"
)
func main(){if err:=run();err!=nil{log.Fatal(err)}}
func run()error{
 ctx:=context.Background()
 topic:=config.Env("KAFKA_COMMAND_TOPIC","data.operations.requested")
 group:=config.Env("KAFKA_CONSUMER_GROUP","data-platform-dispatchers")
 if topic!="data.operations.requested"||group!="data-platform-dispatchers"{return fmt.Errorf("Data Platform requires its own fixed topic and consumer group")}
 ops,err:=app.ConnectOperation(config.Env("OPERATION_GRPC_ADDRESS","data-operation:9000"));if err!=nil{return err};defer ops.Close()
 consumer,err:=kgo.NewClient(kgo.SeedBrokers(strings.Split(config.Required("KAFKA_BROKERS"),",")...),kgo.ConsumeTopics(topic),kgo.ConsumerGroup(group),kgo.DisableAutoCommit(),kgo.BlockRebalanceOnPoll(),kgo.AllowAutoTopicCreation());if err!=nil{return err}
 defer func(){consumer.AllowRebalance();consumer.Close()}()
 ping,cancel:=context.WithTimeout(ctx,10*time.Second);err=consumer.Ping(ping);cancel();if err!=nil{return err}
 delivery:=app.Delivery{Operations:ops,Profiles:registry.Registry{},Runner:executor.New(config.Env("JOB_NETWORK","ops-infra"))}
 log.Printf("data dispatcher ready topic=%s group=%s (CPU only)",topic,group)
 for{
  fetched:=consumer.PollRecords(ctx,1);if fetched.IsClientClosed(){return nil}
  if len(fetched.Errors())>0{return fmt.Errorf("Kafka fetch failed; offsets retained")}
  for _,record:=range fetched.Records(){
   if err:=delivery.Handle(ctx,record.Value);err!=nil{return fmt.Errorf("record retained partition=%d offset=%d: %w",record.Partition,record.Offset,err)}
   if err:=app.Commit(ctx,func(c context.Context)error{return consumer.CommitRecords(c,record)});err!=nil{return err}
   log.Printf("record handled partition=%d offset=%d",record.Partition,record.Offset)
  }
  consumer.AllowRebalance()
 }
}
