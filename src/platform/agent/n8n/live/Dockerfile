FROM n8nio/n8n:2.40.5@sha256:9f693fd5565539efd5e75ad168526c8041a6af516d9e50bc4d9cb1c9c5031523
USER root
WORKDIR /checks
COPY live/run.cjs /checks/live/
COPY workflows/agent-question.json /checks/workflows/
RUN node -e 'const fs=require("node:fs"); for(const p of ["/checks","/checks/live","/checks/workflows"]) fs.chmodSync(p,0o555); for(const p of ["/checks/live/run.cjs","/checks/workflows/agent-question.json"]) fs.chmodSync(p,0o444);'
USER node
RUN node -e 'const fs=require("node:fs"); if(process.getuid()===0) throw Error("non-root required"); for(const p of ["/checks","/checks/live","/checks/workflows"]) fs.accessSync(p,fs.constants.R_OK|fs.constants.X_OK); fs.accessSync("/checks/live/run.cjs",fs.constants.R_OK); fs.accessSync("/checks/workflows/agent-question.json",fs.constants.R_OK);' \
    && node --check /checks/live/run.cjs
ENTRYPOINT ["node", "/checks/live/run.cjs"]
