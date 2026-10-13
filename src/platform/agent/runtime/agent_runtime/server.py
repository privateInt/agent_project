"""Authenticated Agent runtime: A02 probes and A03 fixed diagnostic Tools."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hmac
import json
import os
import signal
import threading
import uuid
import grpc
from agent_runtime import tools, observability
from google.protobuf import empty_pb2
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from agent.platform.v1 import platform_pb2, platform_pb2_grpc

CONTRACT = "agent.platform.v1/a07"
SERVICE = "agent.platform.v1.RuntimeService"


def read_token():
    path = os.environ.get("AGENT_INTERNAL_TOKEN_FILE")
    if not path:
        raise ValueError("AGENT_INTERNAL_TOKEN_FILE is required")
    try:
        token = Path(path).read_text().strip()
    except OSError:
        raise ValueError("cannot read AGENT_INTERNAL_TOKEN_FILE") from None
    if not 32 <= len(token) <= 256 or any(c.isspace() or c == "\0" for c in token):
        raise ValueError("invalid AGENT_INTERNAL_TOKEN_FILE")
    return token


def valid_id(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.int != 0 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


class Authentication(grpc.ServerInterceptor):
    def __init__(self, token):
        self.token = token

    def intercept_service(self, continuation, details):
        handler = continuation(details)
        if handler is None:
            return None
        metadata = list(details.invocation_metadata or ())
        tokens = [v for k, v in metadata if k == "authorization"]
        ids = [v for k, v in metadata if k == "x-request-id"]
        error = None
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], "Bearer " + self.token):
            error = (grpc.StatusCode.UNAUTHENTICATED, "authentication required")
        elif not details.method.startswith("/grpc.health.v1.Health/"):
            if len(ids) != 1 or not valid_id(ids[0]):
                error = (grpc.StatusCode.INVALID_ARGUMENT, "x-request-id must be a canonical UUID")
        if error is None:
            return handler

        def reject(request, context):
            context.abort(*error)
        factory = grpc.unary_stream_rpc_method_handler if handler.response_streaming else grpc.unary_unary_rpc_method_handler
        return factory(reject, request_deserializer=handler.request_deserializer,
                       response_serializer=handler.response_serializer)


class Runtime(platform_pb2_grpc.RuntimeServiceServicer):
    def __init__(self, token=None):
        self.token = token
        self.instance_id = str(uuid.uuid4())

    def Probe(self, request, context):
        request_id = dict(context.invocation_metadata())["x-request-id"]
        print(json.dumps({"event": "runtime_probe", "request_id": request_id,
                          "instance_id": self.instance_id}), flush=True)
        return platform_pb2.RuntimeProbe(instance_id=self.instance_id,
                                         contract_version=CONTRACT, request_id=request_id)


    def InspectKnowledge(self, request, context):
        from . import knowledge_tools
        if context.time_remaining() is None or context.time_remaining() > 19:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bounded inspection deadline required")
        try:
            result = knowledge_tools.inspect(tools.decode(request.request_json),
                tools.decode(request.knowledge_connection_json),
                tools.decode(request.model_connection_json), context)
            return platform_pb2.KnowledgeInspectionResult(json=json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        except tools.ToolError as exc:
            return platform_pb2.KnowledgeInspectionResult(error_code=exc.code)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return platform_pb2.KnowledgeInspectionResult(error_code="TOOL_RESPONSE_INVALID")

    def InspectAgentModel(self, request, context):
        from . import agents
        if context.time_remaining() is None or context.time_remaining() > 19:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bounded inspection deadline required")
        try:
            result = agents.inspect(tools.decode(request.request_json), tools.decode(request.connection_json), context)
            return platform_pb2.KnowledgeInspectionResult(json=json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        except agents.AgentError as exc:
            return platform_pb2.KnowledgeInspectionResult(error_code=exc.code)
        except (tools.ToolError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return platform_pb2.KnowledgeInspectionResult(error_code="MODEL_RESPONSE_INVALID")

    @observability.rpc('agent')
    def ExecuteAgent(self, request, context):
        from . import agents
        import time
        if not all(valid_id(v) for v in (request.agent_run_id, request.agent_version_id, request.lease_token)):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid execution identity")
        if context.time_remaining() is None or context.time_remaining() > 601:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bounded execution deadline required")
        out = platform_pb2.AgentExecutionResult(agent_run_id=request.agent_run_id,
            agent_version_id=request.agent_version_id, lease_token=request.lease_token, runtime_instance_id=self.instance_id)
        channel = grpc.insecure_channel(os.environ.get("AGENT_SERVICE_ADDRESS", "agent-service:9000"),
            options=[("grpc.max_receive_message_length", 3 << 20), ("grpc.max_send_message_length", 3 << 20)])
        client = platform_pb2_grpc.AgentServiceStub(channel)
        metadata = (("authorization", "Bearer " + (self.token or read_token())),
                    ("x-request-id", dict(context.invocation_metadata())["x-request-id"]))
        def command(op, step, detail):
            payload = platform_pb2.AgentCommand(agent_run_id=request.agent_run_id, lease_token=request.lease_token,
                operation=op, step_id=step, json=json.dumps(detail, ensure_ascii=False, allow_nan=False).encode())
            for attempt in range(3):
                if not context.is_active() or (context.time_remaining() or 0) <= 0:
                    raise agents.AgentError("AGENT_TIMEOUT", step)
                try:
                    reply = client.AgentRuntimeCommand(payload, timeout=min(3, context.time_remaining()), metadata=metadata)
                    return tools.decode(reply.json)
                except grpc.RpcError as exc:
                    if exc.details() == "AGENT_EXECUTION_FENCED":
                        raise agents.AgentError("AGENT_EXECUTION_INTERRUPTED", step) from None
                    if exc.code() not in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                        raise agents.AgentError("AGENT_SEARCH_FAILED" if step == "search" else "AGENT_RESULT_INVALID", step) from None
                    if attempt == 2:
                        raise agents.AgentError("AGENT_EXECUTION_INTERRUPTED", step) from None
                    time.sleep(0.1)
        try:
            snapshot = tools.decode(request.snapshot_json)
            if snapshot.get("version", {}).get("agent_version_id") != request.agent_version_id:
                raise agents.AgentError("AGENT_INPUT_INVALID", "validate")
            result = agents.execute(snapshot, tools.decode(request.input_json), command, context)
            out.result_json = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        except agents.AgentError as exc:
            out.error_code, out.error_stage = exc.code, exc.stage
        except (tools.ToolError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            out.error_code, out.error_stage = "AGENT_RESULT_INVALID", "execute"
        finally:
            channel.close()
        return out

    @observability.rpc('tool')
    def ExecuteTool(self, request, context):
        if not all(valid_id(value) for value in (request.tool_call_id, request.tool_version_id, request.lease_token)):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid execution identity")
        if context.time_remaining() is None or context.time_remaining() > (241 if request.kind == "knowledge_search_v1" else 121):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "bounded execution deadline required")
        response = platform_pb2.ToolExecutionResult(
            tool_call_id=request.tool_call_id, tool_version_id=request.tool_version_id,
            lease_token=request.lease_token, runtime_instance_id=self.instance_id)
        try:
            result = tools.execute(request.kind, tools.decode(request.binding_json),
                                   tools.decode(request.arguments_json),
                                   tools.decode(request.connection_json) if request.connection_json else None, context, request.invocation_key)
            response.result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
        except tools.ToolError as exc:
            response.error_code = exc.code
            if exc.result is not None:
                response.result_json = json.dumps(exc.result, ensure_ascii=False, separators=(",", ":")).encode()
        return response


def make_server(token, address):
    server = grpc.server(ThreadPoolExecutor(max_workers=8),
                         interceptors=[Authentication(token)],
                         options=[("grpc.max_receive_message_length", 3 << 20)])
    runtime = Runtime(token)
    platform_pb2_grpc.add_RuntimeServiceServicer_to_server(runtime, server)
    checks = health.HealthServicer()
    checks.set("", health_pb2.HealthCheckResponse.SERVING)
    checks.set(SERVICE, health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(checks, server)
    port = server.add_insecure_port(address)
    if not port:
        raise RuntimeError("runtime could not bind its address")
    return server, checks, port


def main():
    server, checks, _ = make_server(read_token(), os.environ.get("AGENT_RUNTIME_ADDRESS", "[::]:9001"))
    observability.initialize()
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    server.start()
    print(json.dumps({"event": "runtime_ready", "stage": "A07"}), flush=True)
    stopping.wait()
    checks.enter_graceful_shutdown()
    server.stop(5).wait(6)
    observability.shutdown()


if __name__ == "__main__":
    main()
