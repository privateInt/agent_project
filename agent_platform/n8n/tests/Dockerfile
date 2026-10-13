# Tests travel with the image; no host bind mounts or production n8n state.
FROM n8nio/n8n:2.40.5@sha256:9f693fd5565539efd5e75ad168526c8041a6af516d9e50bc4d9cb1c9c5031523
USER root
WORKDIR /checks
COPY tests/run.cjs tests/contract.cjs /checks/tests/
COPY workflows/agent-question.json /checks/workflows/
# COPY --chmod also affects newly created directories on current BuildKit.
# Set directory search permissions separately from read-only file permissions.
RUN node -e 'const fs=require("node:fs"); for(const p of ["/checks","/checks/tests","/checks/workflows"]) fs.chmodSync(p,0o555); for(const p of ["/checks/tests/run.cjs","/checks/tests/contract.cjs","/checks/workflows/agent-question.json"]) fs.chmodSync(p,0o444);'
USER node
RUN node -e 'const fs=require("node:fs"); if(process.getuid()===0) throw Error("Checks must use a non-root user"); for(const p of ["/checks","/checks/tests","/checks/workflows"]) fs.accessSync(p,fs.constants.R_OK|fs.constants.X_OK); for(const p of ["/checks/tests/run.cjs","/checks/tests/contract.cjs","/checks/workflows/agent-question.json"]) fs.accessSync(p,fs.constants.R_OK); console.log("PASS: non-root check inputs readable");' \
    && node --check /checks/tests/run.cjs \
    && node /checks/tests/contract.cjs
ENTRYPOINT ["node", "/checks/tests/run.cjs"]
