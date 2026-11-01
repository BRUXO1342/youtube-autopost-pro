import os, sqlite3, uuid, threading, time, json, hashlib, secrets, re, logging
from pathlib import Path
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, session, flash, abort, url_for, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
app=Flask(__name__)
app.secret_key=os.getenv('SECRET_KEY','')
if len(app.secret_key)<32: raise RuntimeError('Configure SECRET_KEY com pelo menos 32 caracteres no .env')
app.config.update(MAX_CONTENT_LENGTH=int(os.getenv('MAX_UPLOAD_MB','2048'))*1024**2, SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.getenv('BASE_URL','').startswith('https://'))
BASE=os.getenv('BASE_URL','http://localhost:5000').rstrip('/')
DB=Path(os.getenv('DB_PATH',str(ROOT/'autopost.sqlite3'))).resolve()
UPLOAD=Path(os.getenv('UPLOAD_DIR',str(ROOT/'uploads'))).resolve(); UPLOAD.mkdir(parents=True,exist_ok=True)
fernet=Fernet(os.environ['TOKEN_KEY'].encode())
SCOPES=['https://www.googleapis.com/auth/youtube.upload','https://www.googleapis.com/auth/youtube.readonly']
lock=threading.Lock()
def now():return datetime.now(timezone.utc).isoformat()
def conn():
 c=sqlite3.connect(DB,timeout=30);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
def init():
 with conn() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,is_admin INTEGER DEFAULT 0);CREATE TABLE IF NOT EXISTS invites(code_hash TEXT PRIMARY KEY,created_by TEXT,used_by TEXT);CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,yt_id TEXT NOT NULL,name TEXT NOT NULL,token TEXT NOT NULL,status TEXT DEFAULT 'verificando',detail TEXT DEFAULT '',checked_at TEXT,UNIQUE(user_id,yt_id),FOREIGN KEY(user_id) REFERENCES users(id));CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,channel_id TEXT NOT NULL,file_path TEXT NOT NULL,original_name TEXT,title TEXT NOT NULL,description TEXT,tags TEXT,privacy TEXT,scheduled_at TEXT,state TEXT DEFAULT 'pendente',yt_video_id TEXT,error TEXT,created_at TEXT NOT NULL,FOREIGN KEY(channel_id) REFERENCES channels(id));''')
init()
def csrf():
 if 'csrf' not in session:session['csrf']=secrets.token_urlsafe(32)
 return session['csrf']
app.jinja_env.globals['csrf']=csrf
@app.before_request
def protect():
 if request.method=='POST' and not secrets.compare_digest(request.form.get('csrf',''),csrf()):abort(400,'CSRF inválido')
def login_required(f):
 @wraps(f)
 def inner(*a,**kw):
  if not session.get('uid'):return redirect(url_for('login'))
  return f(*a,**kw)
 return inner
def current_user():
 with conn() as c:return c.execute('SELECT * FROM users WHERE id=?',(session.get('uid'),)).fetchone()
def own_channel(cid):
 with conn() as c:return c.execute('SELECT * FROM channels WHERE id=? AND user_id=?',(cid,session['uid'])).fetchone()
def flow(state=None):
 config={'web':{'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token','redirect_uris':[BASE+'/oauth/callback']}}
 return Flow.from_client_config(config,scopes=SCOPES,state=state,redirect_uri=BASE+'/oauth/callback')
def creds(channel):
 data=json.loads(fernet.decrypt(channel['token'].encode()))
 cred=Credentials.from_authorized_user_info(data,SCOPES)
 if not cred.valid and cred.refresh_token:
  cred.refresh(Request())
  with conn() as c:c.execute('UPDATE channels SET token=? WHERE id=?',(fernet.encrypt(cred.to_json().encode()).decode(),channel['id']))
 return cred
@app.get('/register')
def register():
 with conn() as c:count=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
 return render_template('register.html',first=count==0)
@app.post('/register')
def register_post():
 email=request.form.get('email','').strip().lower();password=request.form.get('password','');invite=request.form.get('invite','').strip()
 if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(password)<10:flash('E-mail válido e senha de pelo menos 10 caracteres.');return redirect('/register')
 with conn() as c:
  first=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0
  if first:
   admin_email=os.getenv('ADMIN_EMAIL','').lower().strip()
   if not admin_email or email!=admin_email:flash('Primeiro cadastro deve usar ADMIN_EMAIL configurado.');return redirect('/register')
  else:
   invitation=c.execute('SELECT * FROM invites WHERE code_hash=? AND used_by IS NULL',(hashlib.sha256(invite.encode()).hexdigest(),)).fetchone()
   if not invitation:flash('Convite inválido ou já utilizado.');return redirect('/register')
  uid=uuid.uuid4().hex
  try:
   c.execute('INSERT INTO users VALUES(?,?,?,?)',(uid,email,generate_password_hash(password),int(first)))
   if not first:c.execute('UPDATE invites SET used_by=? WHERE code_hash=?',(uid,hashlib.sha256(invite.encode()).hexdigest()))
  except sqlite3.IntegrityError:flash('E-mail já cadastrado.');return redirect('/register')
 session.clear();session['uid']=uid;return redirect('/')
@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  with conn() as c:u=c.execute('SELECT * FROM users WHERE email=?',(request.form.get('email','').lower().strip(),)).fetchone()
  if u and check_password_hash(u['password'],request.form.get('password','')):
   session.clear();session['uid']=u['id'];return redirect('/')
  flash('Credenciais inválidas.')
 return render_template('login.html')
@app.post('/logout')
def logout():session.clear();return redirect('/login')
@app.get('/')
@login_required
def home():
 with conn() as c:
  channels=[dict(x) for x in c.execute('SELECT * FROM channels WHERE user_id=? ORDER BY name',(session['uid'],))]
  jobs=[dict(x) for x in c.execute('SELECT jobs.*,channels.name channel_name FROM jobs JOIN channels ON channels.id=jobs.channel_id WHERE jobs.user_id=? ORDER BY jobs.created_at DESC LIMIT 100',(session['uid'],))]
 return render_template('home.html',channels=channels,jobs=jobs,user=current_user())
@app.post('/invite')
@login_required
def invite():
 if not current_user()['is_admin']:abort(403)
 code=secrets.token_urlsafe(18)
 with conn() as c:c.execute('INSERT INTO invites VALUES(?,?,NULL)',(hashlib.sha256(code.encode()).hexdigest(),session['uid']))
 flash('Convite de uso único: '+code+' — copie agora; não será mostrado novamente.')
 return redirect('/')
@app.get('/oauth/start')
@login_required
def oauth_start():
 try:
  f=flow();url,state=f.authorization_url(access_type='offline',prompt='consent',include_granted_scopes='true')
  session['oauth_state']=state;return redirect(url)
 except Exception as e:flash('Configure credenciais Google: '+str(e));return redirect('/')
@app.get('/oauth/callback')
@login_required
def oauth_callback():
 if not session.get('oauth_state') or request.args.get('state')!=session.pop('oauth_state'):abort(400,'OAuth state inválido')
 try:
  f=flow();f.fetch_token(authorization_response=request.url)
  cr=f.credentials;api=build('youtube','v3',credentials=cr,cache_discovery=False)
  result=api.channels().list(part='snippet',mine=True).execute()
  items=result.get('items',[])
  if not items:raise ValueError('Nenhum canal YouTube encontrado na conta selecionada')
  item=items[0];ytid=item['id'];name=item['snippet']['title']
  with conn() as c:
   old=c.execute('SELECT * FROM channels WHERE user_id=? AND yt_id=?',(session['uid'],ytid)).fetchone()
   if old and not cr.refresh_token:
    old_cr=json.loads(fernet.decrypt(old['token'].encode()));cr.refresh_token=old_cr.get('refresh_token')
   if not cr.refresh_token:raise ValueError('Google não retornou token de atualização; revogue o acesso anterior e reconecte')
   token=fernet.encrypt(cr.to_json().encode()).decode()
   c.execute('INSERT INTO channels(id,user_id,yt_id,name,token,status,checked_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,yt_id) DO UPDATE SET name=excluded.name,token=excluded.token,status=excluded.status,checked_at=excluded.checked_at',(uuid.uuid4().hex,session['uid'],ytid,name,token,'online',now()))
  flash('Canal conectado: '+name)
 except Exception as e:flash('Falha na conexão: '+str(e))
 return redirect('/')
@app.post('/upload')
@login_required
def upload():
 cid=request.form.get('channel_id','');channel=own_channel(cid)
 if not channel:abort(403)
 files=request.files.getlist('videos');files=[f for f in files if f.filename]
 if not files or len(files)>20:flash('Escolha de 1 a 20 vídeos.');return redirect('/')
 title=request.form.get('title','').strip();description=request.form.get('description','')[:4900]
 privacy=request.form.get('privacy','private')
 if privacy not in ('private','unlisted','public'):abort(400)
 when=request.form.get('scheduled_at','').strip()
 if when:
  try:
   dt=datetime.fromisoformat(when.replace('Z','+00:00'))
   if not dt.tzinfo or dt.astimezone(timezone.utc)<=datetime.now(timezone.utc):raise ValueError()
   when=dt.astimezone(timezone.utc).isoformat()
  except Exception:flash('Agendamento inválido: informe data futura com fuso.');return redirect('/')
  if privacy!='private':flash('Agendamento do YouTube exige privacidade privada.');return redirect('/')
 tags=','.join([t.strip() for t in request.form.get('tags','').split(',') if t.strip()])[:400]
 for f in files:
  ext=Path(f.filename).suffix.lower()
  if ext not in ('.mp4','.mov','.webm','.mkv','.avi'):flash('Arquivo ignorado: formato não permitido.');continue
  original=secure_filename(f.filename)[:180];generated=Path(original).stem.replace('_',' ').replace('-',' ').title()[:90]
  final_title=(title or generated or 'Novo vídeo')[:100]
  path=UPLOAD/(uuid.uuid4().hex+ext)
  f.save(path)
  with conn() as c:c.execute('INSERT INTO jobs(id,user_id,channel_id,file_path,original_name,title,description,tags,privacy,scheduled_at,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,session['uid'],cid,str(path),original,final_title,description,tags,privacy,when or None,'pendente',now()))
 flash('Vídeos adicionados à fila.');return redirect('/')
@app.post('/jobs/<jid>/cancel')
@login_required
def cancel(jid):
 with conn() as c:c.execute("UPDATE jobs SET state='cancelado' WHERE id=? AND user_id=? AND state='pendente'",(jid,session['uid']))
 return redirect('/')
@app.get('/status')
@login_required
def status():
 with conn() as c:rows=c.execute('SELECT id,name,status,detail,checked_at FROM channels WHERE user_id=?',(session['uid'],)).fetchall()
 return jsonify([dict(r) for r in rows])
def check_channel(ch):
 try:
  api=build('youtube','v3',credentials=creds(ch),cache_discovery=False)
  result=api.channels().list(part='id',mine=True).execute()
  state='online' if any(x['id']==ch['yt_id'] for x in result.get('items',[])) else 'offline'
  detail='' if state=='online' else 'Canal não acessível com esta autorização; verifique no YouTube Studio.'
 except Exception as e:
  state='offline';detail=str(e)[:350]
  if isinstance(e,HttpError) and e.resp.status in (429,500,502,503):state='verificando'
 with conn() as c:c.execute('UPDATE channels SET status=?,detail=?,checked_at=? WHERE id=?',(state,detail,now(),ch['id']))
def process_job(job,ch):
 try:
  if ch['status']!='online':raise RuntimeError('Canal não está online; upload pausado.')
  api=build('youtube','v3',credentials=creds(ch),cache_discovery=False)
  body={'snippet':{'title':job['title'],'description':job['description'] or '', 'tags':(job['tags'] or '').split(',') if job['tags'] else []},'status':{'privacyStatus':job['privacy'],'selfDeclaredMadeForKids':False}}
  if job['scheduled_at']:body['status']['publishAt']=datetime.fromisoformat(job['scheduled_at']).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
  media=MediaFileUpload(job['file_path'],resumable=True,chunksize=8*1024*1024)
  req=api.videos().insert(part='snippet,status',body=body,media_body=media)
  result=None
  while result is None:_,result=req.next_chunk()
  with conn() as c:c.execute("UPDATE jobs SET state='enviado',yt_video_id=?,error=NULL WHERE id=?",(result['id'],job['id']))
  Path(job['file_path']).unlink(missing_ok=True)
 except Exception as e:
  with conn() as c:c.execute("UPDATE jobs SET state='erro',error=? WHERE id=?",(str(e)[:500],job['id']))
def worker():
 last_check=0
 while True:
  try:
   if time.time()-last_check>=300:
    with conn() as c:chs=c.execute('SELECT * FROM channels').fetchall()
    for ch in chs:check_channel(ch)
    last_check=time.time()
   with conn() as c:
    job=c.execute("SELECT * FROM jobs WHERE state='pendente' ORDER BY created_at LIMIT 1").fetchone()
    if job:
     ch=c.execute('SELECT * FROM channels WHERE id=?',(job['channel_id'],)).fetchone()
     if ch and ch['status']=='online':c.execute("UPDATE jobs SET state='enviando' WHERE id=?",(job['id'],))
     else:job=None
   if job:process_job(job,ch)
  except Exception:logging.exception('Worker error')
  time.sleep(int(os.getenv('CHECK_INTERVAL_SECONDS','60')))
if os.getenv('WORKER_ENABLED','1')=='1':
 with conn() as c:c.execute("UPDATE jobs SET state='erro',error='Upload interrompido; verifique antes de tentar novamente' WHERE state='enviando'")
 threading.Thread(target=worker,daemon=True).start()
if __name__=='__main__':app.run(host='127.0.0.1',port=int(os.getenv('PORT','5000')),debug=False)
