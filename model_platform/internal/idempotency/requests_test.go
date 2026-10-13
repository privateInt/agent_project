package idempotency

import (
	"context"
	"fmt"
	"strings"
	"testing"

	managementv1 "model-platform/gen/management/v1"
	"model-platform/internal/testsupport"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestKeyAndCredentialFingerprint(t *testing.T) {
	for _, key := range []string{"", "replay-123", "a.b:c_1", strings.Repeat("a",128)} { if err:=ValidateKey(key); err!=nil { t.Fatal(err) } }
	for _, key := range []string{" ","a b","a,b","한글",strings.Repeat("a",129)} { if ValidateKey(key)==nil { t.Fatal("invalid key accepted") } }
	one,_ := Fingerprint(map[string]string{"token":"test-secret-a"},[]byte("server-key"))
	two,_ := Fingerprint(map[string]string{"token":"test-secret-b"},[]byte("server-key"))
	plain,_ := Fingerprint(map[string]string{"token":"test-secret-a"},nil)
	if one==two || one==plain || strings.Contains(one,"test-secret") {
		t.Fatal("credential fingerprint not protected")
	}
}

func TestIdempotencyRollsBackReservation(t *testing.T) {
	pool := testsupport.Postgres(t); ctx:=context.Background()
	if _,err:=pool.Exec(ctx,Schema+`CREATE TABLE fixture (id INTEGER PRIMARY KEY);`); err!=nil {
		t.Fatal(err)
	}
	fail := func(ctx context.Context, tx pgx.Tx) error { if _,err:=tx.Exec(ctx,`INSERT INTO fixture VALUES(1)`);err!=nil{return err}; return fmt.Errorf("forced failure after domain write") }
	if err:=Execute(ctx,pool,"test","key","hash",&managementv1.Model{},fail); err==nil {
		t.Fatal("failure hidden")
	}
	var count int
	for _, table := range []string{"fixture","api_requests"} { if err:=pool.QueryRow(ctx,"SELECT count(*) FROM "+table).Scan(&count);err!=nil || count!=0 {t.Fatalf("partial commit: %s %d %v",table,count,err)} }
	response:=&managementv1.Model{}
	err:=Execute(ctx,pool,"test","key","hash",response,func(ctx context.Context,tx pgx.Tx)error{response.ModelId="winner";_,err:=tx.Exec(ctx,`INSERT INTO fixture VALUES(1)`);return err})
	if err!=nil {t.Fatal(err)}
	again:=&managementv1.Model{}
	err=Execute(ctx,pool,"test","key","hash",again,func(context.Context,pgx.Tx)error{t.Fatal("replay executed mutation");return nil})
	if err!=nil || again.ModelId!="winner" {t.Fatal("replay failed",err)}
	if err=Execute(ctx,pool,"test","key","changed",&managementv1.Model{},fail);status.Code(err)!=codes.AlreadyExists {t.Fatalf("conflict: %v",err)}
}
