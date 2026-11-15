import os, sqlite3, json, secrets, hmac, re
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
DB=os.getenv('RRR_DB','/data/tasks.db' if Path('/data').exists() else 'tasks.db')
TOKEN=os.getenv('RRR_ADMIN_TOKEN')
if not TOKEN or len(TOKEN)<24: raise SystemExit('Set RRR_ADMIN_TOKEN (at least 24 random characters)')
KINDS={'prepare_card','review_card','review_stale_products','request_republish','request_delete'}
DANGEROUS={'request_republish','request_delete'}
def now():return datetime.now(timezone.utc).isoformat()
def db():
 c=sqlite3.connect(DB,timeout=10);c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL')
 c.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,action TEXT NOT NULL,at TEXT NOT NULL)');c.commit();return c
class Handler(BaseHTTPRequestHandler):
 def send(self,code,obj):
  raw=json.dumps(obj,ensure_ascii=False).encode();self.send_response(code)
  for k,v in [('Content-Type','application/json; charset=utf-8'),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Content-Length',str(len(raw)))]:self.send_header(k,v)
  self.end_headers();self.wfile.write(raw)
 def auth(self):return hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+TOKEN)
 def body(self):
  n=int(self.headers.get('Content-Length','0'))
  if n<1 or n>32768:raise ValueError('Invalid payload length')
  d=json.loads(self.rfile.read(n));
  if not isinstance(d,dict):raise ValueError('Object required')
  return d
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/health':return self.send(200,{'status':'ok','mode':'offline-demo','kaspi_connected':False})
  if not self.auth():return self.send(401,{'error':'unauthorized'})
  c=db()
  if path=='/tasks':
   rows=[dict(r) for r in c.execute('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100')]
   for r in rows:
    r['payload']=json.loads(r['payload']);r['result']=json.loads(r['result']) if r['result'] else None
   c.close();return self.send(200,{'tasks':rows})
  if path=='/audit':
   rows=[dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')];c.close();return self.send(200,{'events':rows})
  c.close();return self.send(404,{'error':'not_found'})
 def do_POST(self):
  if not self.auth():return self.send(401,{'error':'unauthorized'})
  try:data=self.body()
  except (ValueError,json.JSONDecodeError):return self.send(400,{'error':'invalid_json'})
  path=urlparse(self.path).path
  if path=='/tasks':
   kind=data.get('kind');payload=data.get('payload')
   if kind not in KINDS or not isinstance(payload,dict):return self.send(400,{'error':'invalid_task'})
   if len(json.dumps(payload))>16000:return self.send(400,{'error':'payload_too_large'})
   tid=secrets.token_hex(12);t=now();status='awaiting_approval' if kind in DANGEROUS else 'queued'
   c=db();c.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',(tid,kind,json.dumps(payload,ensure_ascii=False),status,None,t,t));c.execute('INSERT INTO audit(task_id,action,at) VALUES (?,?,?)',(tid,'created:'+status,t));c.commit();c.close();return self.send(201,{'id':tid,'status':status})
  m=re.fullmatch(r'/tasks/([a-f0-9]{24})/(decision|process)',path)
  if not m:return self.send(404,{'error':'not_found'})
  tid,action=m.groups();c=db();r=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
  if not r:c.close();return self.send(404,{'error':'task_not_found'})
  if action=='decision':
   if r['status']!='awaiting_approval' or data.get('decision') not in ('approve','reject'):
    c.close();return self.send(409,{'error':'invalid_decision_or_status'})
   status='approved_pending_integration' if data['decision']=='approve' else 'rejected';result=None
  else:
   if r['status']!='queued':c.close();return self.send(409,{'error':'not_queued'})
   payload=json.loads(r['payload']);kind=r['kind']
   if kind=='review_stale_products':
    items=payload.get('products',[])
    if not isinstance(items,list) or len(items)>500:c.close();return self.send(400,{'error':'products_must_be_list_max_500'})
    flagged=[]
    for p in items:
     if not isinstance(p,dict):continue
     try:
      if int(p.get('days_without_sales',0))>=30 and int(p.get('reviews',0))==0:flagged.append({'sku':str(p.get('sku',''))[:100],'reason':'30+ days no sales and zero reviews'})
     except (ValueError,TypeError):continue
    result={'flagged':flagged,'source':'user_supplied_data','automatic_deletion':False}
   elif kind in ('prepare_card','review_card'):
    result={'summary':'Карточка қолмен тексеруге дайын','fields_received':sorted(payload.keys()),'ai_generated':False}
   else: c.close();return self.send(409,{'error':'integration_required'})
   status='completed_demo'
  t=now();c.execute('UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?',(status,json.dumps(result,ensure_ascii=False) if result is not None else None,t,tid));c.execute('INSERT INTO audit(task_id,action,at) VALUES (?,?,?)',(tid,action+':'+status,t));c.commit();c.close();return self.send(200,{'id':tid,'status':status,'result':result,'kaspi_action_executed':False})
if __name__=='__main__':
 db().close();ThreadingHTTPServer((os.getenv('HOST','127.0.0.1'),int(os.getenv('PORT','8080'))),Handler).serve_forever()
