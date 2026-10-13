// Package crawl collects a bounded HTTP response; it does not parse or clean it.
package crawl

import (
 "context"
 "fmt"
 "net"
 "net/netip"
 "net/url"
 "strings"
 "time"
)

// Credentials belong in a future explicit credential contract, not URL userinfo.
func NormalizeURL(raw string)(string,error){
 raw=strings.TrimSpace(raw)
 if len(raw)==0||len(raw)>4096||strings.ContainsAny(raw,"\x00\r\n\t\\"){return "",fmt.Errorf("invalid URL")}
 u,e:=url.Parse(raw);if e!=nil||u.Opaque!=""||u.User!=nil||u.Hostname()==""||(u.Scheme!="http"&&u.Scheme!="https"){return "",fmt.Errorf("absolute HTTP(S) URL without credentials required")}
 host:=strings.ToLower(u.Hostname());if strings.HasSuffix(host,"."){host=strings.TrimSuffix(host,".")}
 if host==""||strings.Contains(host,"%")||strings.ContainsAny(host," <>\"'"){return "",fmt.Errorf("invalid URL host")}
 for _,c:=range host {if c>127{return "",fmt.Errorf("use an ASCII or punycode hostname")}}
 port:=u.Port();expected:="80";if u.Scheme=="https" {expected="443"}
 if port!=""&&port!=expected{return "",fmt.Errorf("only HTTP port 80 and HTTPS port 443 are supported")}
 if a,e:=netip.ParseAddr(host);e==nil {if !PublicIP(a){return "",fmt.Errorf("non-public URL address is not permitted")};host=a.String();if a.Is6(){host="["+host+"]"}}
 if strings.Contains(host,":")&&!strings.HasPrefix(host,"["){return "",fmt.Errorf("invalid URL host")}
 u.Host=host;u.Fragment="";u.RawFragment="";if u.Path=="" {u.Path="/"}
 return u.String(),nil
}

var forbidden=[]netip.Prefix{
 netip.MustParsePrefix("0.0.0.0/8"),netip.MustParsePrefix("10.0.0.0/8"),netip.MustParsePrefix("100.64.0.0/10"),
 netip.MustParsePrefix("127.0.0.0/8"),netip.MustParsePrefix("169.254.0.0/16"),netip.MustParsePrefix("172.16.0.0/12"),
 netip.MustParsePrefix("192.0.0.0/24"),netip.MustParsePrefix("192.0.2.0/24"),netip.MustParsePrefix("192.168.0.0/16"),
 netip.MustParsePrefix("198.18.0.0/15"),netip.MustParsePrefix("198.51.100.0/24"),netip.MustParsePrefix("203.0.113.0/24"),
 netip.MustParsePrefix("224.0.0.0/3"),netip.MustParsePrefix("2001:db8::/32"),netip.MustParsePrefix("2001::/23"),
 netip.MustParsePrefix("2002::/16"),netip.MustParsePrefix("64:ff9b::/96"),netip.MustParsePrefix("64:ff9b:1::/48"),
}
func PublicIP(a netip.Addr)bool{
 a=a.Unmap();if !a.IsValid()||!a.IsGlobalUnicast()||a.IsPrivate()||a.IsLoopback()||a.IsLinkLocalUnicast(){return false}
 if a.Is6()&&!netip.MustParsePrefix("2000::/3").Contains(a){return false}
 for _,p:=range forbidden {if p.Contains(a){return false}};return true
}

type Resolver interface { LookupNetIP(context.Context,string,string)([]netip.Addr,error) }
// Resolve and connect to the SAME checked address. TLS keeps the original host.
// No proxy, second DNS lookup, URL-controlled port or private redirect is used.
func publicDial(ctx context.Context,network,address string)(net.Conn,error){
 return dialResolved(ctx,net.DefaultResolver,(&net.Dialer{Timeout:10*time.Second}).DialContext,network,address)
}
func dialResolved(ctx context.Context,resolver Resolver,dial func(context.Context,string,string)(net.Conn,error),network,address string)(net.Conn,error){
 host,port,e:=net.SplitHostPort(address);if e!=nil{return nil,fmt.Errorf("invalid destination")}
 if port!="80"&&port!="443" {return nil,fmt.Errorf("destination port not permitted")}
 addresses,e:=resolver.LookupNetIP(ctx,"ip",host);if e!=nil||len(addresses)==0{return nil,fmt.Errorf("DNS lookup failed")}
 for _,a:=range addresses {if !PublicIP(a){return nil,fmt.Errorf("DNS returned a non-public address")}}
 var last error
 for _,a:=range addresses {c,e:=dial(ctx,network,net.JoinHostPort(a.String(),port));if e==nil{return c,nil};last=e}
 return nil,fmt.Errorf("destination connection failed: %w",last)
}
