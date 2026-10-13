import sys
import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from agent_runtime.server import read_token, SERVICE


def main():
    try:
        with grpc.insecure_channel("127.0.0.1:9001") as channel:
            result = health_pb2_grpc.HealthStub(channel).Check(
                health_pb2.HealthCheckRequest(service=SERVICE), timeout=2,
                metadata=[("authorization", "Bearer " + read_token())])
            return 0 if result.status == health_pb2.HealthCheckResponse.SERVING else 1
    except (grpc.RpcError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
