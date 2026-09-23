"""Authenticated loopback transport through the restricted SSH channel. No retries."""
import io,json,os,sys,urllib.request
from pathlib import Path

def enabled():return bool(os.environ.get('ARB_CLOUD_GATEWAY_URL'))
def request(path,payload=None,timeout=8):
 base=os.environ['ARB_CLOUD_GATEWAY_URL'].rstrip('/')
 token=Path(os.environ['ARB_CLOUD_TOKEN_FILE']).read_text().strip()
 req=urllib.request.Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
 return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=timeout)
def call(path,payload=None,timeout=8):
 with request(path,payload,timeout) as r:return json.load(r)
def depth(req,timeout=5):
 if not enabled():return urllib.request.urlopen(req,timeout=timeout)
 return io.BytesIO(json.dumps(call('/depth',{'url':req.full_url},timeout)).encode())
def main():
 # Retain the existing gateway NDJSON contract and subprocess watchdogs.
 # Unknown outcomes are propagated; neither this shim nor HTTP retries writes.
 try:
  payload=json.load(sys.stdin)
  with request('/gateway',payload,timeout=65) as r:
   for line in r:
    sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
 except Exception as exc:
  print(json.dumps({'ok':False,'error':{'code':'CLOUD_TRANSPORT_UNCERTAIN','message':'云端通道未确认结果：'+type(exc).__name__}}),flush=True)
  sys.exit(1)
if __name__=='__main__':main()
