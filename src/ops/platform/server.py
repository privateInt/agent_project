"""O03 single-process Ops API and same-origin console. No business write routes."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException

from .contracts import RESOURCES, ReadFailure, identifier, require, resource
from .service import ReadService
from .settings import load, origins
from .transport import secret, strict_json

WEB=Path(__file__).parent/'web'
COOKIE='ops_session'
LABELS={'model':'모델','data':'데이터','knowledge':'지식','agent':'Agent'}
RESOURCE_LABELS={'operations':'작업','model-versions':'모델 버전','training-runs':'학습 실행',
    'deployments':'모델 배포','crawl-runs':'수집 실행','refinement-runs':'정제 실행',
    'chunking-runs':'청킹 실행','indexes':'인덱스','index-versions':'인덱스 버전',
    'agent-runs':'Agent 실행','tool-calls':'도구 호출'}


def error(code,status=400):
    return JSONResponse({'status':'error','data':None,'error':{'code':code}},status_code=status)


class Sessions:
    def __init__(self,key):
        self.key=key;self.sessions={};self.attempts={}

    def check(self,value):
        if not isinstance(value,str) or len(value)>128:return False
        return self.sessions.get(hashlib.sha256(value.encode()).hexdigest(),0)>time.monotonic()

    def limited(self,host):
        t=time.monotonic()
        self.attempts={k:v for k,v in self.attempts.items() if v[1]>t}
        n,until=self.attempts.get(host,(0,t+60))
        if n>=10 or (host not in self.attempts and len(self.attempts)>=512):return True
        self.attempts[host]=(n+1,until)
        return False

    def login(self,password):
        if not isinstance(password,str) or len(password)>512 or not hmac.compare_digest(password.encode(),self.key.encode()):return None
        now=time.monotonic();self.sessions={k:v for k,v in self.sessions.items() if v>now}
        if len(self.sessions)>=256:return None
        token=secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(token.encode()).hexdigest()]=now+8*3600
        return token

    def logout(self,value):
        if isinstance(value,str):self.sessions.pop(hashlib.sha256(value.encode()).hexdigest(),None)


def create_app(config, service_factory=ReadService):
    auth=Sessions(secret(config['access_file']))
    allowed_origins=origins(config)
    hosts={urllib.parse.urlsplit(x).netloc.lower() for x in allowed_origins}
    active=0
    @asynccontextmanager
    async def lifespan(app):
        app.state.reads=service_factory(config)
        yield
        app.state.reads.close()
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.auth=auth

    @app.middleware('http')
    async def boundary(request,call_next):
        nonlocal active
        path=request.url.path
        response=None;counted=False
        try:
            if request.headers.get('host','').lower() not in hosts:
                response=error('HOST_NOT_ALLOWED',400)
            elif len(request.scope.get('query_string',b''))>4096:
                response=error('REQUEST_TOO_LARGE',414)
            elif request.method!='GET' and not (path=='/api/session' and request.method in ('POST','DELETE')):
                response=error('READ_ONLY_API',405)
            elif request.method in ('POST','DELETE') and request.headers.get('origin') not in allowed_origins:
                response=error('ORIGIN_NOT_ALLOWED',403)
            elif path.startswith('/api/') and path!='/api/session':
                bearer=request.headers.get('authorization','')
                token=bearer[7:] if bearer.startswith('Bearer ') and len(bearer)<=520 else ''
                if not ((token and hmac.compare_digest(token.encode(),auth.key.encode())) or auth.check(request.cookies.get(COOKIE))):
                    response=error('LOGIN_REQUIRED',401)
                elif active>=4:
                    response=error('OPS_BUSY',503)
                else: active+=1;counted=True
            if response is None:response=await call_next(request)
        except Exception:
            # Never include raw inputs, upstream bodies, paths or credentials in API errors.
            response=error('OPS_INTERNAL_ERROR',500)
        finally:
            if counted:active-=1
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            'Permissions-Policy':'camera=(), microphone=(), geolocation=()'})
        return response

    @app.exception_handler(ReadFailure)
    async def contract_error(request,exc):return error(exc.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):return error('INVALID_ARGUMENT')

    @app.exception_handler(HTTPException)
    async def http_error(request,exc):return error('NOT_FOUND' if exc.status_code==404 else 'INVALID_REQUEST',exc.status_code)

    @app.get('/healthz')
    def health():return {'status':'ok','platform':'ops','scope':'web_process_only'}

    @app.get('/')
    def index():return FileResponse(WEB/'index.html',media_type='text/html')

    @app.get('/app.js')
    def javascript():return FileResponse(WEB/'app.js',media_type='text/javascript')

    @app.get('/style.css')
    def stylesheet():return FileResponse(WEB/'style.css',media_type='text/css')

    @app.get('/api/session')
    def session(request:Request):return {'authenticated':auth.check(request.cookies.get(COOKIE))}

    @app.post('/api/session')
    async def login(request:Request):
        if auth.limited(request.client.host if request.client else 'unknown'):return error('LOGIN_RATE_LIMITED',429)
        if request.headers.get('content-type','').split(';')[0]!='application/json':return error('INVALID_ARGUMENT')
        async def body():
            data=bytearray()
            async for part in request.stream():
                data.extend(part)
                require(len(data)<=2048,'REQUEST_TOO_LARGE')
            return bytes(data)
        try:raw=strict_json(await asyncio.wait_for(body(),3))
        except asyncio.TimeoutError:return error('REQUEST_TIMEOUT',408)
        require(isinstance(raw,dict) and set(raw)=={'password'},'INVALID_ARGUMENT')
        token=auth.login(raw['password'])
        if not token:return error('LOGIN_FAILED',401)
        response=JSONResponse({'authenticated':True})
        response.set_cookie(COOKIE,token,max_age=8*3600,httponly=True,
                            secure=config['public_url'].startswith('https:'),samesite='strict',path='/')
        return response

    @app.delete('/api/session')
    def logout(request:Request):
        auth.logout(request.cookies.get(COOKIE));response=JSONResponse({'authenticated':False})
        response.delete_cookie(COOKIE,path='/',httponly=True,samesite='strict',secure=config['public_url'].startswith('https:'))
        return response

    def query(request,allowed):
        keys=list(request.query_params.keys())
        require(set(keys)<=set(allowed) and len(list(request.query_params.multi_items()))==len(keys),'INVALID_ARGUMENT')

    @app.get('/api/catalog')
    def catalog():
        return {'platforms':[{'id':name,'label':LABELS[name],'resources':[
            {'id':key,'label':RESOURCE_LABELS[key],'listable':spec.listable,'parent_required':spec.parent}
            for key,spec in resources.items()]} for name,resources in RESOURCES.items()],
            'global_counts_supported':False,'alerting':'not_implemented'}

    @app.get('/api/overview')
    def overview(request:Request):
        query(request,());return app.state.reads.overview()

    @app.get('/api/resources')
    def resources(request:Request):
        query(request,());return app.state.reads.resources()

    @app.get('/api/alerts')
    def alerts(request:Request):
        query(request,());return app.state.reads.alerts()

    @app.get('/api/platforms/{platform}/{name}')
    def page(platform:str,name:str,request:Request):
        resource(platform,name);query(request,('limit','cursor','parent_id'))
        raw=request.query_params.get('limit','20')
        require(raw.isascii() and raw.isdecimal() and len(raw)<=3,'INVALID_LIMIT')
        limit=int(raw);require(1<=limit<=100,'INVALID_LIMIT')
        parent=request.query_params.get('parent_id')
        if parent:identifier(parent)
        return app.state.reads.page(platform,name,limit,request.query_params.get('cursor',''),parent)

    @app.get('/api/platforms/{platform}/{name}/{ident}')
    def detail(platform:str,name:str,ident:str,request:Request):
        resource(platform,name);identifier(ident);query(request,())
        return app.state.reads.detail(platform,name,ident)

    @app.get('/api/agent-runs/{ident}/trace')
    def trace(ident:str,request:Request):
        identifier(ident);query(request,());return app.state.reads.trace(ident)

    return app


def main():
    import uvicorn
    config=load(os.environ.get('OPS_CONFIG','/run/ops/config.json'))
    uvicorn.run(create_app(config),host=config['bind'],port=config['port'],workers=1,
        proxy_headers=False,server_header=False,access_log=False,limit_concurrency=32,ws='none',http='h11',
        timeout_keep_alive=5,timeout_graceful_shutdown=15,
        ssl_certfile=config['tls_cert'],ssl_keyfile=config['tls_key'])


if __name__=='__main__':main()
