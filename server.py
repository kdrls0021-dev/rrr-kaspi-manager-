import os, sqlite3, json, secrets, hmac, re
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from contextlib import closing
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
KASPI_IMPORT = 'https://kaspi.kz/shop/api/products/import'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def kaspi_request(token, card=None, code=None):
    url = KASPI_IMPORT if card is not None else KASPI_IMPORT + '?' + urlencode({'i': code})
    headers = {'Accept': 'application/json', 'X-Auth-Token': token}
    body = None
    if card is not None:
        headers['Content-Type'] = 'text/plain'
        body = json.dumps([card], ensure_ascii=False).encode()
    with build_opener(NoRedirect()).open(Request(url, data=body, headers=headers), timeout=15) as response:
        return json.load(response)


def import_card(data):
    card = {}
    for name in ('sku', 'title', 'brand', 'category', 'description'):
        value = data.get(name, '')
        if not isinstance(value, str) or (name != 'description' and not value.strip()):
            raise ValueError('Толтырыңыз: ' + name)
        card[name] = value.strip()
    if len(card['sku']) > 64:
        raise ValueError('Артикул 64 таңбадан аспауы керек.')
    image = data.get('image', '')
    if not isinstance(image, str) or urlparse(image).scheme != 'https' or not urlparse(image).hostname:
        raise ValueError('Фотоның HTTPS сілтемесін енгізіңіз.')
    attributes = data.get('attributes')
    if not isinstance(attributes, list) or any(
        not isinstance(item, dict) or not isinstance(item.get('code'), str)
        or not item['code'].strip() or 'value' not in item for item in attributes
    ):
        raise ValueError('Сипаттамаларды Kaspi форматында енгізіңіз.')
    card['images'] = [{'url': image}]
    card['attributes'] = attributes
    return card


def open_imports():
    connection = db()
    connection.execute('CREATE TABLE IF NOT EXISTS kaspi_imports (sku TEXT PRIMARY KEY, code TEXT)')
    connection.commit()
    return connection


PANEL = r'''<!doctype html>
<html lang="kk"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RRR Brand · Kaspi Manager</title>
<style>
*{box-sizing:border-box}body{background:#f4f5f0;color:#182821;font:16px system-ui;margin:auto;max-width:650px;padding:24px 16px}
h1{font-size:32px;margin:6px 0}small{color:#53685d}section{background:white;padding:18px;border:1px solid #d9e3dc;border-radius:14px;margin:18px 0}
h2{font-size:20px;margin:0 0 12px}label{display:grid;gap:5px;margin:12px 0}input,textarea,button{font:inherit;border:1px solid #9aaea2;border-radius:8px;padding:10px;width:100%}button{background:#245b42;color:white;cursor:pointer;margin:5px 0}li{overflow-wrap:anywhere;margin:12px 0}
</style>
<small>RRR BRAND</small><h1>Kaspi Manager</h1>
<p>Карточка дайындау және Kaspi-ге жіберу</p>
<section><h2>Кіру</h2><form id="login">
<label>Әкімші кілті<input id="key" type="password" required autocomplete="current-password"></label>
<button>Кіру</button></form></section>
<div id="workspace" hidden>
<section><h2>Карточка дайындау</h2><form id="product">
<label>Артикул<input name="sku" required maxlength="64"></label>
<label>Атауы<input name="title" required maxlength="200"></label>
<label>Фото сілтемесі<input name="image" type="url" maxlength="1000"></label>
<label>Сипаттамасы<textarea name="description" maxlength="1500"></textarea></label>
<button>Карточканы сақтау</button></form></section>
<section><h2>Kaspi-ге жіберу</h2><form id="kaspi">
<label>Артикул<input name="sku" required maxlength="64"></label>
<label>Атауы<input name="title" required></label>
<label>Бренд<input name="brand" required></label>
<label>Kaspi санатының коды<input name="category" required></label>
<label>Фото сілтемесі<input name="image" type="url" required></label>
<label>Сипаттамасы<textarea name="description"></textarea></label>
<label>Kaspi сипаттамалары (JSON)<textarea name="attributes" required placeholder='[{"code":"...","value":"..."}]'></textarea></label>
<button type="submit">Kaspi-ге жіберу</button>
<button type="button" id="check">Жүктеу мәртебесін тексеру</button>
</form><p id="kaspi-message" role="status"></p></section>
<section><h2>Сақталған карточкалар</h2><ul id="tasks"></ul></section>
</div><p id="message" role="status"></p>
<script>
const login = document.querySelector('#login');
const product = document.querySelector('#product');
const message = document.querySelector('#message');
const workspace = document.querySelector('#workspace');
let key = '';

async function api(path, body) {
    const options = { headers: { Authorization: 'Bearer ' + key } };
    if (body) {
        options.method = 'POST';
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    if (response.status === 401) {
        workspace.hidden = true;
        login.hidden = false;
        key = '';
        throw new Error('Әкімші кілтін тексеріңіз.');
    }
    if (!response.ok && path.startsWith('/kaspi/')) {
        const result = await response.json();
        throw new Error(result.error);
    }
    if (!response.ok) throw new Error('Сервер сұрауды орындай алмады.');
    return response.json();
}

async function refresh() {
    const {tasks} = await api('/tasks');
    const list = document.querySelector('#tasks');
    list.replaceChildren();
    for (const task of tasks) {
        const row = document.createElement('li');
        const p = task.payload;
        row.textContent = [p.sku, p.title, task.kind, task.status].filter(Boolean).join(' · ');
        if (task.kind === 'prepare_card') {
            const fill = document.createElement('button');
            fill.textContent = 'Жіберу формасына көшіру';
            fill.addEventListener('click', () => {
                const form = document.querySelector('#kaspi');
                form.reset();
                for (const name of ['sku', 'title', 'image', 'description']) {
                    form.elements[name].value = p[name] || '';
                }
                form.scrollIntoView({block: 'start'});
            });
            row.append(fill);
        }
        list.append(row);
    }
    if (!tasks.length) {
        const row = document.createElement('li');
        row.textContent = 'Тапсырма әзірге жоқ.';
        list.append(row);
    }
}

login.addEventListener('submit', async event => {
    event.preventDefault();
    const input = document.querySelector('#key');
    key = input.value;
    try {
        await refresh();
        input.value = '';
        login.hidden = true;
        workspace.hidden = false;
        message.textContent = '';
    } catch (error) { message.textContent = error.message; }
});

product.addEventListener('submit', async event => {
    event.preventDefault();
    const button = product.querySelector('button');
    button.disabled = true;
    try {
        const payload = Object.fromEntries(new FormData(product));
        payload.sku = payload.sku.trim();
        payload.title = payload.title.trim();
        if (!payload.sku || !payload.title) throw new Error('Артикул мен атауын жазыңыз.');
        await api('/tasks', {kind: 'prepare_card', payload});
        product.reset();
        message.textContent = 'Карточка тапсырмаларға қосылды.';
        await refresh();
    } catch (error) { message.textContent = error.message; }
    finally { button.disabled = false; }
});
const kaspiForm = document.querySelector('#kaspi');
const kaspiMessage = document.querySelector('#kaspi-message');

async function runKaspi(path, payload, button) {
    button.disabled = true;
    try {
        const result = await api(path, payload);
        kaspiMessage.textContent = [result.status, result.code, result.description].filter(Boolean).join(' · ');
    } catch (error) { kaspiMessage.textContent = error.message; }
    finally { button.disabled = false; }
}

kaspiForm.addEventListener('submit', event => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(kaspiForm));
    try { payload.attributes = JSON.parse(payload.attributes); }
    catch { kaspiMessage.textContent = 'Сипаттамалардың JSON форматын тексеріңіз.'; return; }
    if (confirm('Осы карточканы Kaspi-ге жібересіз бе?')) {
        runKaspi('/kaspi/send', payload, event.submitter);
    }
});
document.querySelector('#check').addEventListener('click', event => {
    runKaspi('/kaspi/status', {sku: kaspiForm.elements.sku.value}, event.currentTarget);
});
</script></html>'''

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
  if path=='/':
   raw=PANEL.encode()
   self.send_response(200)
   self.send_header('Content-Type','text/html; charset=utf-8')
   self.send_header('Cache-Control','no-store')
   self.send_header('Content-Length',str(len(raw)))
   self.end_headers()
   self.wfile.write(raw)
   return
  if path=='/health':return self.send(200,{'status':'ok','mode':'kaspi-import' if os.getenv('KASPI_TOKEN') else 'offline-demo','kaspi_connected':False})
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
 def handle_import(self, path, data):
  token = os.getenv('KASPI_TOKEN')
  if not token:
   return self.send(409, {'error': 'Railway-де KASPI_TOKEN қосыңыз.'})
  if path == '/kaspi/status':
   sku = data.get('sku')
   if not isinstance(sku, str) or not sku.strip():
    return self.send(400, {'error': 'Артикулды енгізіңіз.'})
   with closing(open_imports()) as connection:
    row = connection.execute('SELECT code FROM kaspi_imports WHERE sku=?', (sku.strip(),)).fetchone()
   if not row or not row['code']:
    return self.send(409, {'error': 'Жүктеу коды жоқ. Kaspi кабинетін тексеріңіз.'})
   return self.read_import_status(token, row['code'])
  try:
   card = import_card(data)
  except ValueError as error:
   return self.send(400, {'error': str(error)})
  with closing(open_imports()) as connection:
   try:
    with connection:
     connection.execute('INSERT INTO kaspi_imports (sku) VALUES (?)', (card['sku'],))
   except sqlite3.IntegrityError:
    return self.send(409, {'error': 'Бұл артикул жіберілген немесе нәтижесін тексеру керек.'})
   try:
    result = kaspi_request(token, card=card)
    code = result.get('code') if isinstance(result, dict) else None
    if not isinstance(code, str) or not code.strip():
     raise ValueError('Missing import code')
   except HTTPError as error:
    if error.code in (400, 401, 403, 422):
     with connection:
      connection.execute('DELETE FROM kaspi_imports WHERE sku=?', (card['sku'],))
     return self.send(502, {'error': f'Kaspi сұрауды қабылдамады (HTTP {error.code}).'})
    return self.send(502, {'error': 'Жіберу нәтижесі белгісіз. Kaspi кабинетін тексеріңіз.'})
   except (OSError, ValueError):
    return self.send(502, {'error': 'Жіберу нәтижесі белгісіз. Kaspi кабинетін тексеріңіз.'})
   with connection:
    connection.execute('UPDATE kaspi_imports SET code=? WHERE sku=?', (code, card['sku']))
  return self.send(201, {'code': code, 'status': result.get('status', 'UPLOADED')})

 def read_import_status(self, token, code):
  try:
   result = kaspi_request(token, code=code)
   if not isinstance(result, dict) or not result.get('status'):
    raise ValueError('Missing status')
  except (OSError, ValueError):
   return self.send(502, {'error': 'Kaspi жүктеу мәртебесін қайтармады.'})
  return self.send(200, {'code': code, 'status': result['status'], 'description': result.get('description')})

 def do_POST(self):
  if not self.auth():return self.send(401,{'error':'unauthorized'})
  try:data=self.body()
  except (ValueError,json.JSONDecodeError):return self.send(400,{'error':'invalid_json'})
  path=urlparse(self.path).path
  if path in ('/kaspi/send', '/kaspi/status'):
   return self.handle_import(path, data)
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
