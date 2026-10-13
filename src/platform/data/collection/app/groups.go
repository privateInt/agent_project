package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/idempotency"
	"data-platform/internal/listing"
	"data-platform/internal/sourcefile"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"time"
)

const groupSchema = `
CREATE TABLE IF NOT EXISTS source_groups (
 group_id UUID PRIMARY KEY, name TEXT NOT NULL UNIQUE CHECK(char_length(name) BETWEEN 1 AND 200),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 version BIGINT NOT NULL DEFAULT 1 CHECK(version > 0)
);
CREATE INDEX IF NOT EXISTS source_groups_browse_idx ON source_groups(created_at DESC,group_id DESC);
CREATE TABLE IF NOT EXISTS source_group_members (
 group_id UUID NOT NULL REFERENCES source_groups(group_id),
 document_id UUID NOT NULL REFERENCES source_documents(document_id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(group_id,document_id)
);
CREATE INDEX IF NOT EXISTS source_group_members_document_idx ON source_group_members(document_id,group_id);
`

const groupSelect = `SELECT group_id::text,name,created_at,updated_at,version FROM source_groups`

func scanGroup(row pgx.Row) (*collectionv1.SourceGroup, error) {
	g := &collectionv1.SourceGroup{}
	var created, updated time.Time
	if e := row.Scan(&g.GroupId, &g.Name, &created, &updated, &g.Version); e != nil {
		return nil, e
	}
	g.CreatedAt = timestamppb.New(created)
	g.UpdatedAt = timestamppb.New(updated)
	return g, nil
}
func groupName(value string) (string, error) {
	name, e := sourcefile.Name(value)
	if e != nil {
		return "", status.Error(codes.InvalidArgument, e.Error())
	}
	return name, nil
}
func (s *Service) CreateGroup(ctx context.Context, r *collectionv1.CreateGroupRequest) (*collectionv1.SourceGroup, error) {
	name, e := groupName(r.GetName())
	if e != nil {
		return nil, e
	}
	if r.GetIdempotencyKey() == "" {
		return nil, status.Error(codes.InvalidArgument, "Idempotency-Key is required")
	}
	if e = idempotency.ValidateKey(r.GetIdempotencyKey()); e != nil {
		return nil, e
	}
	fingerprint, e := idempotency.Fingerprint(struct{ Version, Name string }{"source-group-create-v1", name}, nil)
	if e != nil {
		return nil, e
	}
	out := &collectionv1.SourceGroup{}
	e = idempotency.Execute(ctx, s.pool, "collection.group.create", r.IdempotencyKey, fingerprint, out, func(ctx context.Context, tx pgx.Tx) error {
		g, e := scanGroup(tx.QueryRow(ctx, `INSERT INTO source_groups(group_id,name) VALUES($1,$2) RETURNING group_id::text,name,created_at,updated_at,version`, uuid.NewString(), name))
		if e != nil {
			return e
		}
		out.GroupId = g.GroupId
		out.Name = g.Name
		out.CreatedAt = g.CreatedAt
		out.UpdatedAt = g.UpdatedAt
		out.Version = g.Version
		return nil
	})
	return out, e
}
func (s *Service) GetGroup(ctx context.Context, r *collectionv1.GetGroupRequest) (*collectionv1.SourceGroup, error) {
	if e := validID(r.GetGroupId()); e != nil {
		return nil, e
	}
	g, e := scanGroup(s.pool.QueryRow(ctx, groupSelect+` WHERE group_id=$1`, r.GroupId))
	return g, readError(e)
}
func (s *Service) RenameGroup(ctx context.Context, r *collectionv1.RenameGroupRequest) (*collectionv1.SourceGroup, error) {
	if e := validID(r.GetGroupId()); e != nil {
		return nil, e
	}
	name, e := groupName(r.GetName())
	if e != nil {
		return nil, e
	}
	if r.GetExpectedVersion() < 1 {
		return nil, status.Error(codes.InvalidArgument, "expected_version must be positive")
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer idempotency.Rollback(tx)
	g, e := scanGroup(tx.QueryRow(ctx, groupSelect+` WHERE group_id=$1 FOR UPDATE`, r.GroupId))
	if e != nil {
		return nil, readError(e)
	}
	// A lost successful reply can be retried. A stale rename cannot undo a later change.
	if g.Version != r.ExpectedVersion && !(g.Version == r.ExpectedVersion+1 && g.Name == name) {
		return nil, status.Error(codes.AlreadyExists, "group changed; refresh before renaming")
	}
	if g.Name != name {
		g, e = scanGroup(tx.QueryRow(ctx, `UPDATE source_groups SET name=$2,version=version+1,updated_at=clock_timestamp() WHERE group_id=$1 RETURNING group_id::text,name,created_at,updated_at,version`, g.GroupId, name))
		if e != nil {
			return nil, idempotency.DBError(e)
		}
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	return g, nil
}
func (s *Service) ListGroups(ctx context.Context, r *collectionv1.ListGroupsRequest) (*collectionv1.ListGroupsResponse, error) {
	q, e := listing.Text(r.GetQ())
	if e != nil {
		return nil, e
	}
	page, e := listing.New(r.GetLimit(), r.GetCursor(), "source-groups", q)
	if e != nil {
		return nil, e
	}
	query := listing.Query{}
	query.Contains(q, "name")
	rows, e := s.pool.Query(ctx, groupSelect+query.Finish(page, "created_at", "group_id"), query.Args...)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer rows.Close()
	out := &collectionv1.ListGroupsResponse{}
	for rows.Next() {
		if len(out.Items) == page.Limit {
			last := out.Items[len(out.Items)-1]
			out.NextCursor = page.Next(last.CreatedAt.AsTime(), last.GroupId)
			break
		}
		g, e := scanGroup(rows)
		if e != nil {
			return nil, idempotency.DBError(e)
		}
		out.Items = append(out.Items, g)
	}
	return out, idempotency.DBError(rows.Err())
}
func (s *Service) SetGroupDocument(ctx context.Context, r *collectionv1.SetGroupDocumentRequest) (*collectionv1.GroupMembership, error) {
	for _, v := range []string{r.GetGroupId(), r.GetDocumentId()} {
		if e := validID(v); e != nil {
			return nil, e
		}
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer idempotency.Rollback(tx)
	if _, e = scanGroup(tx.QueryRow(ctx, groupSelect+` WHERE group_id=$1`, r.GroupId)); e != nil {
		return nil, readError(e)
	}
	var deleted, purged *time.Time
	var purgeState string
	// Serialize with catalog deletion/purge. Membership never writes to source_documents.
	e = tx.QueryRow(ctx, `SELECT deleted_at,purge_state,purged_at FROM source_documents WHERE document_id=$1 FOR UPDATE`, r.DocumentId).Scan(&deleted, &purgeState, &purged)
	if e != nil {
		return nil, readError(e)
	}
	if r.Present {
		if deleted != nil || purged != nil || purgeState != "" {
			return nil, status.Error(codes.FailedPrecondition, "only available source documents can be added to a group")
		}
		_, e = tx.Exec(ctx, `INSERT INTO source_group_members(group_id,document_id) VALUES($1,$2) ON CONFLICT DO NOTHING`, r.GroupId, r.DocumentId)
	} else {
		_, e = tx.Exec(ctx, `DELETE FROM source_group_members WHERE group_id=$1 AND document_id=$2`, r.GroupId, r.DocumentId)
	}
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	return &collectionv1.GroupMembership{GroupId: r.GroupId, DocumentId: r.DocumentId, Present: r.Present}, nil
}

// AssignDocumentGroup creates/reuses an exact normalized name and adds membership
// in one transaction. Invalid/deleted sources cannot leave an orphan new group.
func (s *Service) AssignDocumentGroup(ctx context.Context, r *collectionv1.AssignDocumentGroupRequest) (*collectionv1.DocumentGroupAssignment, error) {
	if e := validID(r.GetDocumentId()); e != nil {
		return nil, e
	}
	name, e := groupName(r.GetName())
	if e != nil {
		return nil, e
	}
	if r.GetIdempotencyKey() == "" {
		return nil, status.Error(codes.InvalidArgument, "Idempotency-Key is required")
	}
	if e = idempotency.ValidateKey(r.GetIdempotencyKey()); e != nil {
		return nil, e
	}
	hash, e := idempotency.Fingerprint(struct{ Version, DocumentID, Name string }{"document-group-assign-v1", r.DocumentId, name}, nil)
	if e != nil {
		return nil, e
	}
	out := &collectionv1.DocumentGroupAssignment{}
	e = idempotency.Execute(ctx, s.pool, "collection.group.assign", r.IdempotencyKey, hash, out, func(ctx context.Context, tx pgx.Tx) error {
		var deleted, purged *time.Time
		var purge string
		if e := tx.QueryRow(ctx, `SELECT deleted_at,purge_state,purged_at FROM source_documents WHERE document_id=$1 FOR UPDATE`, r.DocumentId).Scan(&deleted, &purge, &purged); e != nil {
			return readError(e)
		}
		if deleted != nil || purged != nil || purge != "" {
			return status.Error(codes.FailedPrecondition, "only available source documents can be added to a group")
		}
		// The no-op conflict update locks and returns the existing row without changing
		// its public version or timestamps, including concurrent same-name creation.
		g, e := scanGroup(tx.QueryRow(ctx, `INSERT INTO source_groups(group_id,name) VALUES($1,$2)
   ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name
   RETURNING group_id::text,name,created_at,updated_at,version`, uuid.NewString(), name))
		if e != nil {
			return e
		}
		if _, e = tx.Exec(ctx, `INSERT INTO source_group_members(group_id,document_id) VALUES($1,$2) ON CONFLICT DO NOTHING`, g.GroupId, r.DocumentId); e != nil {
			return e
		}
		out.DocumentId = r.DocumentId
		out.Group = g
		return nil
	})
	return out, e
}

// One metadata query for a source page; never fetches or rewrites artifacts.
func (s *Service) attachDocumentGroups(ctx context.Context, docs []*collectionv1.SourceDocument) error {
	if len(docs) == 0 {
		return nil
	}
	ids := make([]string, 0, len(docs))
	byID := map[string]*collectionv1.SourceDocument{}
	for _, d := range docs {
		ids = append(ids, d.DocumentId)
		byID[d.DocumentId] = d
		d.Groups = nil
	}
	rows, e := s.pool.Query(ctx, `SELECT m.document_id::text,g.group_id::text,g.name,g.created_at,g.updated_at,g.version
 FROM source_group_members m JOIN source_groups g ON g.group_id=m.group_id
 WHERE m.document_id=ANY($1::uuid[]) ORDER BY m.document_id,g.name,g.group_id`, ids)
	if e != nil {
		return idempotency.DBError(e)
	}
	defer rows.Close()
	for rows.Next() {
		var document string
		var created, updated time.Time
		g := &collectionv1.SourceGroup{}
		if e = rows.Scan(&document, &g.GroupId, &g.Name, &created, &updated, &g.Version); e != nil {
			return idempotency.DBError(e)
		}
		g.CreatedAt = timestamppb.New(created)
		g.UpdatedAt = timestamppb.New(updated)
		byID[document].Groups = append(byID[document].Groups, g)
	}
	return idempotency.DBError(rows.Err())
}
