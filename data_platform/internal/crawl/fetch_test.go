package crawl
import(
 "context"
 "errors"
 "io"
 "net"
 "net/http"
 "net/netip"
 "net/url"
 "os"
 "strings"
 "testing"
)
func TestURLBoundary(t *testing.T){
 for _,raw:=range []string{"file:///etc/passwd","http://user:pass@example.com/","http://127.0.0.1/","http://[::1]/","http://169.254.169.254/","http://[::ffff:127.0.0.1]/","http://example.com:9000/","https://example.com:80/","http://example.com\\@127.0.0.1/","http://192.0.2.1/","http://[2002:7f00:1::]/","http://[64:ff9b::7f00:1]/"}{t.Run(raw,func(t *testing.T){if _,e:=NormalizeURL(raw);e==nil{t.Fatal("unsafe URL accepted")}})}
 got,e:=NormalizeURL(" https://EXAMPLE.com.:443/a?x=1#part ");if e!=nil||got!="https://example.com/a?x=1"{t.Fatalf("normalization: %s %v",got,e)}
}
type fakeResolver struct{calls int;addresses []netip.Addr}
func(r *fakeResolver)LookupNetIP(context.Context,string,string)([]netip.Addr,error){r.calls++;return r.addresses,nil}
func TestDNSPinningAndMixedAnswers(t *testing.T){
 resolver:=&fakeResolver{addresses:[]netip.Addr{netip.MustParseAddr("93.184.216.34")}}
 dialed:="";dial:=func(_ context.Context,network,address string)(net.Conn,error){dialed=address;return nil,errors.New("test dial")}
 _,_=dialResolved(context.Background(),resolver,dial,"tcp","example.com:443")
 if resolver.calls!=1||dialed!="93.184.216.34:443"{t.Fatal("destination was resolved again",dialed)}
 resolver.addresses=append(resolver.addresses,netip.MustParseAddr("10.0.0.1"));dialed=""
 if _,e:=dialResolved(context.Background(),resolver,dial,"tcp","example.com:443");e==nil||dialed!=""{t.Fatal("mixed DNS answer dialed")}
}
type transport func(*http.Request)(*http.Response,error)
func(f transport)RoundTrip(r *http.Request)(*http.Response,error){return f(r)}
func fakeFetcher(body string,length int64,code int,encoding string)*Fetcher{
 return &Fetcher{Client:&http.Client{Transport:transport(func(r *http.Request)(*http.Response,error){return &http.Response{StatusCode:code,Body:io.NopCloser(strings.NewReader(body)),ContentLength:length,Header:http.Header{"Content-Type":[]string{"text/html; charset=utf-8"},"Content-Encoding":[]string{encoding}},Request:r},nil})}}
}
func TestRawBytesAndCharsetPreserved(t *testing.T){
 body:="<html>한글\x00\n</html>";r,e:=fakeFetcher(body,int64(len(body)),200,"").Fetch(context.Background(),"https://example.com/",Options{});if e!=nil{t.Fatal(e)};defer os.Remove(r.Path)
 got,e:=os.ReadFile(r.Path);if e!=nil||string(got)!=body||r.Size!=int64(len(body))||r.ContentType!="text/html; charset=utf-8"{t.Fatal("original response was altered",r,e)}
}
func TestFailedResponseCannotBecomeAnOriginal(t *testing.T){
 for _,c:=range []struct{name,body,encoding string;length,max int64;code int}{
  {"http-error","error","",5,8,401},{"partial-status","abc","",3,8,206},{"too-large-declared","abc","",999,8,200},{"too-large-stream","abcdefghij","",-1,8,200},{"truncated","abc","",8,8,200},{"encoded","abc","gzip",3,8,200},
 }{t.Run(c.name,func(t *testing.T){r,e:=fakeFetcher(c.body,c.length,c.code,c.encoding).Fetch(context.Background(),"https://example.com/",Options{MaxBytes:c.max});if e==nil||r.Path!=""{t.Fatal("failed response accepted",r,e)}})}
}
func TestRedirectAndProxyPolicy(t *testing.T){
 f:=NewFetcher();tr:=f.Client.Transport.(*http.Transport);if tr.Proxy!=nil{t.Fatal("environment proxy enabled")}
 original,_:=http.NewRequest("GET","https://example.com/",nil)
 for _,target:=range []string{"http://example.com/","https://127.0.0.1/","https://example.com:8443/"}{u,_:=url.Parse(target);if f.Client.CheckRedirect(&http.Request{URL:u},[]*http.Request{original})==nil{t.Fatal("unsafe redirect accepted",target)}}
 target,_:=http.NewRequest("GET","https://www.example.com/",nil);if e:=f.Client.CheckRedirect(target,[]*http.Request{original});e!=nil{t.Fatal(e)}
 if f.Client.CheckRedirect(target,make([]*http.Request,10))==nil{t.Fatal("unbounded redirects")}
}
func TestPublicErrorDoesNotLeakQuery(t *testing.T){
 e:=&url.Error{Op:"Get",URL:"https://example.com/?token=hidden",Err:errors.New("lookup failed")}
 if strings.Contains(PublicError(e),"hidden"){t.Fatal("query credential leaked")}
 if PublicError(&HTTPStatusError{Status:401})!="HTTP response status 401"{t.Fatal("HTTP status lost")}
}
