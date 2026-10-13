package app

import (
	"context"
	ocrv1 "data-platform/gen/ocr/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (s *Service) ConfigureOCR(client ocrv1.OCRDeploymentServiceClient) { s.ocrClient = client }

func (s *Service) CreateRun(ctx context.Context, r *refinementv1.CreateRunRequest) (*refinementv1.RunAccepted, error) {
	if r.GetOcrDeploymentId() != "" {
		return nil, status.Error(codes.InvalidArgument, "use CreateOCRRun for OCR requests")
	}
	return s.createRun(ctx, r)
}

func (s *Service) CreateOCRRun(ctx context.Context, r *refinementv1.CreateRunRequest) (*refinementv1.RunAccepted, error) {
	if r.GetOcrDeploymentId() == "" {
		return nil, status.Error(codes.InvalidArgument, "OCR deployment is required")
	}
	return s.createRun(ctx, r)
}
