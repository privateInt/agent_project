package outbox
import("testing";"time")
func TestRetryDelay(t *testing.T) {
 for _,tc:=range []struct{attempt int;seconds int64}{{1,1},{2,2},{6,32},{7,60},{100,60}} {
  if got:=retryDelay(tc.attempt);got!=time.Duration(tc.seconds)*time.Second {t.Fatalf("attempt=%d delay=%v",tc.attempt,got)}
 }
}
