"""Bounded Chromium DOM capture. stdin/stdout are the ONLY network bridge.

The Linux filter is installed BEFORE the Playwright driver starts and inherited
by all descendants. Only AF_UNIX sockets are available. Failure is fatal.
No route.continue_(), route.fetch(), browser HTTP API or cookie forwarding.
"""
import base64
import ctypes
import errno
import json
import os
import socket
import sys

MAX_FRAME = 48 << 20
STAGE = "configuration"


def isolate_network():
    class Arg(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Arg)]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise RuntimeError("network isolation unavailable")
    ctx = lib.seccomp_init(0x7fff0000)  # ALLOW, with explicit prohibitions below
    if not ctx:
        raise RuntimeError("network isolation unavailable")
    try:
        for name in (b"socket", b"socketpair"):
            nr = lib.seccomp_syscall_resolve_name(name)
            # SCMP_CMP_NE: deny every family except UNIX, including IPv4/6,
            # packet, netlink, vsock and future protocol families.
            arg = Arg(0, 1, socket.AF_UNIX, 0)
            if nr < 0 or lib.seccomp_rule_add_array(ctx, 0x50000 | errno.EPERM, nr, 1, ctypes.byref(arg)) != 0:
                raise RuntimeError("network isolation unavailable")
        # Do not allow io_uring to bypass syscall socket restrictions.
        for name in (b"io_uring_setup", b"io_uring_enter", b"io_uring_register"):
            nr = lib.seccomp_syscall_resolve_name(name)
            if nr >= 0 and lib.seccomp_rule_add_array(ctx, 0x50000 | errno.EPERM, nr, 0, None) != 0:
                raise RuntimeError("network isolation unavailable")
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError("network isolation unavailable")
    finally:
        lib.seccomp_release(ctx)
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            connection = socket.socket(family, socket.SOCK_STREAM)
        except OSError as exc:
            if exc.errno == errno.EPERM:
                continue
            raise RuntimeError("network isolation verification failed") from None
        connection.close()
        raise RuntimeError("network isolation verification failed")


def read():
    line = sys.stdin.buffer.readline(MAX_FRAME + 1)
    if not line or len(line) > MAX_FRAME:
        raise RuntimeError("invalid bridge frame")
    return json.loads(line)


def emit(value):
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise RuntimeError("bridge frame too large")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main():
    global STAGE
    config = read()
    STAGE = "network_isolation"
    isolate_network()
    STAGE = "browser_start"
    # Import after isolation: the driver and Chromium inherit the filter.
    from playwright.sync_api import sync_playwright
    failed = []
    blocked = 0
    main_served = False
    callbacks = 0
    with sync_playwright() as playwright:
        # Docker CPU Job (uid 65532, no capabilities, read-only root) is the
        # process boundary. Do not request privileged Chromium user namespaces.
        browser = playwright.chromium.launch(headless=True, chromium_sandbox=False,
            args=["--disable-dev-shm-usage", "--disable-background-networking",
                  "--disable-quic", "--disable-extensions", "--disable-sync"])
        context = browser.new_context(service_workers="block", accept_downloads=False,
            viewport={"width": 1280, "height": 720}, locale="en-US", timezone_id="UTC")
        context.set_default_timeout(config["timeout_ms"])
        context.set_default_navigation_timeout(config["timeout_ms"])
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("pageerror", lambda error: failed.append("javascript_error"))
        page.on("crash", lambda: failed.append("browser_crash"))

        def websocket(route):
            nonlocal blocked
            blocked += 1
            route.close()
        context.route_web_socket("**/*", websocket)

        def route_request(route):
            nonlocal main_served, blocked, callbacks
            callbacks += 1
            request = route.request
            if callbacks > 1024:
                failed.append("request_limit")
                route.abort()
                return
            # Ignore frames, popups, worker loads, methods with side effects and
            # repeated top-level navigation. Cookies/credentials never reach Go.
            try:
                frame = request.frame
            except Exception:
                frame = None
            if frame != page.main_frame or request.method != "GET":
                blocked += 1
                route.abort()
                return
            if request.is_navigation_request():
                if main_served or request.url != config["url"]:
                    failed.append("navigation_changed")
                    route.abort()
                    return
                main_served = True
                route.fulfill(status=config["status"], headers=config["headers"],
                              body=base64.b64decode(config["body"], validate=True))
                return
            if request.resource_type not in ("script", "stylesheet", "image", "font", "xhr", "fetch"):
                blocked += 1
                route.abort()
                return
            emit({"type": "fetch", "url": request.url, "method": request.method})
            response = read()
            if response.get("blocked"):
                blocked += 1
                route.abort()
            elif response.get("error"):
                failed.append("resource_failed")
                route.abort()
            else:
                route.fulfill(status=response["status"], headers=response["headers"],
                              body=base64.b64decode(response["body"], validate=True))

        context.route("**/*", route_request)
        try:
            STAGE = "navigation"
            page.goto(config["url"], wait_until="load")
            STAGE = "render_wait"
            page.wait_for_timeout(config["wait_ms"])
            # Fragment-only navigation is immaterial; pushState URL changes are
            # rejected because the retained HTTP response would no longer match.
            if page.url.split("#", 1)[0] != config["url"] or not main_served or failed or blocked > 1024:
                raise RuntimeError("rendering did not complete")
            # page.content() serializes the live DOM, it is NOT response bytes.
            STAGE = "dom_capture"
            content = page.content().encode("utf-8")
            if len(content) > config["max_bytes"]:
                raise RuntimeError("DOM exceeds max_bytes")
            emit({"type": "result", "body": base64.b64encode(content).decode("ascii"),
                  "url": config["url"], "blocked": blocked})
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Browser diagnostics can embed source URLs, query values or page text.
        # Keep raw stderr out of Operation and ordinary retained Job logs.
        emit({"type": "error", "code": "BROWSER_RENDER_FAILED", "stage": STAGE})
        sys.exit(1)
