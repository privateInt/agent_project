import unittest
import uuid
import grpc
from google.protobuf import empty_pb2
from grpc_health.v1 import health_pb2, health_pb2_grpc
from agent.platform.v1 import platform_pb2_grpc
from agent_runtime.server import CONTRACT, make_server

TOKEN = "c" * 64


class RuntimeContractTest(unittest.TestCase):
    def setUp(self):
        self.server, self.health, port = make_server(TOKEN, "127.0.0.1:0")
        self.server.start()
        self.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.client = platform_pb2_grpc.RuntimeServiceStub(self.channel)

    def tearDown(self):
        self.channel.close()
        self.server.stop(0).wait()

    def metadata(self, request_id=None):
        return [("authorization", "Bearer " + TOKEN),
                ("x-request-id", request_id or str(uuid.uuid4()))]

    def test_probe_round_trip_and_stable_process_identity(self):
        metadata = self.metadata()
        first = self.client.Probe(empty_pb2.Empty(), metadata=metadata, timeout=2)
        second = self.client.Probe(empty_pb2.Empty(), metadata=self.metadata(), timeout=2)
        self.assertEqual(first.request_id, metadata[1][1])
        self.assertEqual(first.contract_version, CONTRACT)
        self.assertEqual(first.instance_id, second.instance_id)
        self.assertEqual(str(uuid.UUID(first.instance_id)), first.instance_id)

    def test_authentication_and_invalid_correlation_are_rejected(self):
        for metadata, code in [([], grpc.StatusCode.UNAUTHENTICATED),
                               (self.metadata("bad"), grpc.StatusCode.INVALID_ARGUMENT)]:
            with self.assertRaises(grpc.RpcError) as caught:
                self.client.Probe(empty_pb2.Empty(), metadata=metadata, timeout=2)
            self.assertEqual(caught.exception.code(), code)

    def test_health_requires_service_token(self):
        stub = health_pb2_grpc.HealthStub(self.channel)
        with self.assertRaises(grpc.RpcError) as caught:
            stub.Check(health_pb2.HealthCheckRequest(), timeout=2)
        self.assertEqual(caught.exception.code(), grpc.StatusCode.UNAUTHENTICATED)
        result = stub.Check(health_pb2.HealthCheckRequest(), metadata=self.metadata(), timeout=2)
        self.assertEqual(result.status, health_pb2.HealthCheckResponse.SERVING)


if __name__ == "__main__":
    unittest.main()
