// Package listing implements bounded, filter-bound keyset pagination.
// Cursors are navigation data, never authorization or execution grants.
package listing

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type cursor struct {
	Version   int       `json:"v"`
	Scope     string    `json:"scope"`
	CreatedAt time.Time `json:"created_at"`
	ID        string    `json:"id"`
}

type Page struct {
	Limit int
	scope string
	after *cursor
}

func New(limit int32, token, collection string, filters ...string) (Page, error) {
	if limit == 0 {
		limit = 20
	}
	if limit < 1 || limit > 100 {
		return Page{}, status.Error(codes.InvalidArgument, "limit must be 1..100")
	}
	data, _ := json.Marshal(append([]string{collection}, filters...))
	hash := sha256.Sum256(data)
	page := Page{Limit: int(limit), scope: hex.EncodeToString(hash[:])}
	if token == "" {
		return page, nil
	}
	invalid := status.Error(codes.InvalidArgument, "invalid cursor or changed filters; restart from the first page")
	if len(token) > 2048 {
		return Page{}, invalid
	}
	raw, err := base64.RawURLEncoding.Strict().DecodeString(token)
	if err != nil {
		return Page{}, invalid
	}
	var c cursor
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&c) != nil || decoder.Decode(new(any)) != io.EOF {
		return Page{}, invalid
	}
	id, err := uuid.Parse(c.ID)
	if err != nil || id.String() != c.ID || c.Version != 1 || c.Scope != page.scope || c.CreatedAt.IsZero() || c.CreatedAt.Year() < 1 || c.CreatedAt.Year() > 9999 {
		return Page{}, invalid
	}
	page.after = &c
	return page, nil
}

func (p Page) Next(created time.Time, id string) string {
	raw, _ := json.Marshal(cursor{Version: 1, Scope: p.scope, CreatedAt: created.UTC(), ID: id})
	return base64.RawURLEncoding.EncodeToString(raw)
}

// Query identifiers/expressions must be constants owned by the calling service.
// All user-supplied values are parameters; no field or sort expression comes
// from REST or gRPC input.
type Query struct {
	conditions []string
	Args       []any
}

func (q *Query) Param(value any) string {
	q.Args = append(q.Args, value)
	return fmt.Sprintf("$%d", len(q.Args))
}

// Where accepts only service-owned SQL; bind user values through Param.
func (q *Query) Where(expression string) { q.conditions = append(q.conditions, expression) }
func (q *Query) IsNull(column string)    { q.conditions = append(q.conditions, column+" IS NULL") }
func (q *Query) Equal(column, value string) {
	if value != "" {
		q.conditions = append(q.conditions, column+"="+q.Param(value))
	}
}
func (q *Query) Contains(value string, columns ...string) {
	if value == "" {
		return
	}
	parameter := q.Param(Pattern(value))
	terms := make([]string, 0, len(columns))
	for _, column := range columns {
		terms = append(terms, column+" ILIKE "+parameter+` ESCAPE '\'`)
	}
	q.conditions = append(q.conditions, "("+strings.Join(terms, " OR ")+")")
}
func (q *Query) Finish(p Page, createdColumn, idColumn string) string {
	if p.after != nil {
		q.conditions = append(q.conditions, "("+createdColumn+","+idColumn+") < ("+q.Param(p.after.CreatedAt)+"::timestamptz,"+q.Param(p.after.ID)+"::uuid)")
	}
	where := ""
	if len(q.conditions) > 0 {
		where = " WHERE " + strings.Join(q.conditions, " AND ")
	}
	return where + " ORDER BY " + createdColumn + " DESC," + idColumn + " DESC LIMIT " + q.Param(p.Limit+1)
}

func Text(value string) (string, error) {
	value = strings.TrimSpace(value)
	if !utf8.ValidString(value) || utf8.RuneCountInString(value) > 200 || strings.ContainsRune(value, 0) {
		return "", status.Error(codes.InvalidArgument, "q must be valid text of at most 200 characters")
	}
	return value, nil
}
func Pattern(value string) string {
	return "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(value) + "%"
}
func Enum(value, field string, choices ...string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	if value == "" {
		return "", nil
	}
	for _, choice := range choices {
		if value == choice {
			return value, nil
		}
	}
	return "", status.Errorf(codes.InvalidArgument, "invalid %s", field)
}
func UUID(value, field string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", nil
	}
	parsed, err := uuid.Parse(value)
	if err != nil {
		return "", status.Errorf(codes.InvalidArgument, "invalid %s", field)
	}
	return parsed.String(), nil
}
