package app
import (
    "strings"
    "testing"
    "github.com/google/uuid"
)
func TestRenameInput(t *testing.T) {
    id:=uuid.NewString()
    got,label,err:=renameInput(id,"  한국어 모델  ","old",200)
    if err!=nil || got!=id || label!="한국어 모델" { t.Fatal(got,label,err) }
    for _,tc:=range []struct{id,name,expected string}{
        {"bad","valid","old"},{uuid.Nil.String(),"valid","old"},{id,"  ","old"},
        {id,"name\nline","old"},{id,"name\x00line","old"},{id,strings.Repeat("한",201),"old"},{id,"valid",""},
    } { if _,_,err:=renameInput(tc.id,tc.name,tc.expected,200);err==nil { t.Fatal("accepted invalid input",tc) } }
}
