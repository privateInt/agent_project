"""Explicit server configuration; credentials are references to private files."""
from pathlib import Path
import urllib.parse

from .contracts import require
from .transport import strict_json, base_url, secret
from .resource_profiles import profile


def load(path):
    path=Path(path)
    require(not path.is_symlink() and path.is_file() and path.stat().st_size<=65536,'INVALID_CONFIG')
    value=strict_json(path.read_bytes())
    keys={'schema','platforms','prometheus_url','langfuse_url','langfuse_file','access_file',
          'state_file','public_url','bind','port','timeout','tls_cert','tls_key'}
    require(isinstance(value,dict) and keys <= set(value) <= keys | {'alertmanager_url','resource_profile'}
            and value['schema']==1,'INVALID_CONFIG')
    profile(value.get('resource_profile'))  # Validate without changing persisted config bytes.
    if 'alertmanager_url' in value: base_url(value['alertmanager_url'])
    require(value['bind'] in ('127.0.0.1','0.0.0.0'),'INVALID_BIND')
    require(type(value['port']) is int and 1024<=value['port']<=65535,'INVALID_PORT')
    require(type(value['timeout']) in (float,int) and 0.1<=value['timeout']<=10,'INVALID_TIMEOUT')
    p=base_url(value['public_url'])
    require((p.port or (443 if p.scheme=='https' else 80))==value['port'],'PUBLIC_PORT_MISMATCH')
    value['public_url']=value['public_url'].rstrip('/')
    require(bool(value['tls_cert'])==bool(value['tls_key'])==(p.scheme=='https'),'TLS_CONFIG_MISMATCH')
    for key in ('prometheus_url','langfuse_url'): base_url(value[key])
    require(isinstance(value['platforms'],dict) and set(value['platforms'])=={'model','data','knowledge','agent'},'INVALID_CONFIG')
    for name,row in value['platforms'].items():
        require(isinstance(row,dict) and set(row)<={'url','token_file','enabled'} and 'url' in row,'INVALID_CONFIG')
        base_url(row['url'])
        require(type(row.get('enabled',True)) is bool,'INVALID_CONFIG')
        require(name!='agent' or row.get('token_file'),'INVALID_CONFIG')
    for key in ('langfuse_file','access_file','state_file','tls_cert','tls_key'):
        if value[key] is not None:
            require(isinstance(value[key],str) and Path(value[key]).is_absolute(),'ABSOLUTE_PATH_REQUIRED')
    access=secret(value['access_file'])
    require(len(access)>=32,'INVALID_ACCESS_KEY')
    return value


def origins(config):
    scheme=urllib.parse.urlsplit(config['public_url']).scheme
    return {config['public_url'],scheme+'://127.0.0.1:'+str(config['port']),scheme+'://localhost:'+str(config['port'])}
