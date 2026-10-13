package app

import "testing"

func TestTransitionPolicy(t *testing.T) {
 for _, terminal := range []string{"succeeded", "failed", "cancelled"} {
  for _, target := range []string{"running", "phase", "succeeded", "failed"} {
   allowed, noop := transitionPolicy(terminal, target)
   want := terminal == target
   if allowed != want || noop != want { t.Fatalf("terminal %s -> %s: %v %v", terminal, target, allowed, noop) }
  }
 }
 for _, tc := range []struct{ from, to string; allowed, noop bool }{
  {"pending","running",false,false}, {"dispatching","running",true,false},
  {"running","running",true,true}, {"running","succeeded",true,false},
  {"dispatching","succeeded",true,false}, {"pending","failed",true,false},
  {"retry_wait","running",false,false}, {"running","phase",true,false},
  {"pending","succeeded",false,false}, {"unknown","failed",false,false},
 } {
  a,n := transitionPolicy(tc.from,tc.to)
  if a != tc.allowed || n != tc.noop { t.Fatalf("%+v got %v %v",tc,a,n) }
 }
}
