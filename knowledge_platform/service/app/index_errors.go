package app
import("google.golang.org/grpc/codes";"google.golang.org/grpc/status")
func invalid(s string)error{return status.Error(codes.InvalidArgument,s)}
func precondition(s string)error{return status.Error(codes.FailedPrecondition,s)}
func unavailable(s string)error{return status.Error(codes.Unavailable,s)}
func missing(s string)error{return status.Error(codes.NotFound,s)}
func corrupt(s string)error{return status.Error(codes.DataLoss,s)}
