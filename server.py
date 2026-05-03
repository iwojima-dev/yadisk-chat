"""
YaDisk Chat v6.0
================
Модель: у каждого свой Диск, беседы в отдельных файлах.

Структура на Диске:
  disk:/YaChatV2/
  ├── chats/
  └── attachments/
      ├── contact-abc123.jsonl   <- публичный (только ваши слова Марии)
      └── contact-def456.jsonl   <- публичный (только ваши слова Алексею)

Запуск:  python server_v6_0.py
Открыть: http://127.0.0.1:8765

OAuth:
  1. Зарегистрируйте приложение на https://oauth.yandex.ru/
     Доступы: cloud_api:disk.read  +  cloud_api:disk.write
     Callback URI: http://127.0.0.1:8765/auth/callback
  2. ClientID вводится один раз в UI
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json, urllib.parse, urllib.request, urllib.error
import time, datetime, random, string, traceback as _tb

HOST, PORT = '127.0.0.1', 8765
DISK       = 'https://cloud-api.yandex.net/v1/disk'
YANDEX_AUTH = 'https://oauth.yandex.ru/authorize'

# ── In-memory state ──────────────────────────────────────────
S = {
    'token': '', 'username': '', 'root': 'disk:/YaChatV2',
    'client_id': '',
    '_pending_username': '', '_pending_root': '', '_pending_client_id': '',
    # contacts: [{id, name, peer_url, chat_path, chat_url, unread, seen:set}]
    'contacts': [],
    'active_id': '',        # id активного контакта
    'messages':  [],        # сообщения активной беседы
    'my_card_url': '',      # chat_url активного контакта (передаю собеседнику)
}

# ── Low-level helpers ────────────────────────────────────────
def q(s):        return urllib.parse.quote(s, safe='')
def now_ms():    return int(time.time() * 1000)
def ts_iso():    return datetime.datetime.utcnow().isoformat() + 'Z'
def rand_id():   return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

def yd(token, method, url, data=None, ctype=None):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header('Authorization', f'OAuth {token}')
    if data is not None:
        req.data = data.encode('utf-8') if isinstance(data, str) else data
        if ctype:
            req.add_header('Content-Type', ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def disk_exists(token, path):
    """True если ресурс существует на Диске."""
    s, _ = yd(token, 'GET', f'{DISK}/resources?path={q(path)}')
    return s == 200

def ensure_dir(token, path):
    """Создаёт папку только если её нет. Возвращает ('created'|'existed', ok).
    409 = уже существует — не ошибка."""
    if disk_exists(token, path):
        return 'existed', True
    s, b = yd(token, 'PUT', f'{DISK}/resources?path={q(path)}')
    if s == 409:
        return 'existed', True   # гонка или уже есть
    if s == 201:
        return 'created', True
    raise Exception(f'mkdir {path}: HTTP {s}: {b.decode("utf-8","ignore")[:80]}')

def upload(token, path, text, _retries=4, _delay=1.2):
    """Загружает текст по пути. Повторяет при 409/423/503 до _retries раз."""
    last_exc = None
    for attempt in range(_retries):
        if attempt > 0:
            time.sleep(_delay * attempt)
        # Шаг 1: получить upload-URL
        s, b = yd(token, 'GET',
                  f'{DISK}/resources/upload?path={q(path)}&overwrite=true')
        if s in (409, 423, 503):
            last_exc = Exception(f'upload-url:{s} (attempt {attempt+1})')
            continue
        if s >= 400:
            raise Exception(f'upload-url:{s}:{b.decode("utf-8","ignore")[:80]}')
        href = json.loads(b)['href']
        # Шаг 2: PUT данные
        s2, b2 = yd(None, 'PUT', href, data=text,
                    ctype='text/plain; charset=utf-8')
        if s2 in (409, 423, 503):
            last_exc = Exception(f'upload-put:{s2} (attempt {attempt+1})')
            continue
        if s2 >= 400:
            raise Exception(f'upload-put:{s2}:{b2.decode("utf-8","ignore")[:80]}')
        return  # успех
    raise last_exc or Exception('upload: все попытки исчерпаны')
def upload_binary(token, path, data_bytes, mime='application/octet-stream'):
    """Загружает бинарный файл. data_bytes — bytes. Возвращает public_url."""
    last_exc = None
    for attempt in range(4):
        if attempt > 0:
            time.sleep(1.2 * attempt)
        s, b = yd(token, 'GET',
                  f'{DISK}/resources/upload?path={q(path)}&overwrite=true')
        if s in (409, 423, 503):
            last_exc = Exception(f'upload-url:{s} attempt {attempt+1}')
            continue
        if s >= 400:
            raise Exception(f'upload-url:{s}:{b.decode("utf-8","ignore")[:80]}')
        href = json.loads(b)['href']
        s2, b2 = yd(None, 'PUT', href, data=data_bytes, ctype=mime)
        if s2 in (409, 423, 503):
            last_exc = Exception(f'upload-put:{s2} attempt {attempt+1}')
            continue
        if s2 >= 400:
            raise Exception(f'upload-put:{s2}:{b2.decode("utf-8","ignore")[:80]}')
        return publish(token, path)
    raise last_exc or Exception('upload_binary: все попытки исчерпаны')

def get_preview_url(token, path, size='L'):
    """Возвращает прямую preview-ссылку для картинки."""
    s, b = yd(token, 'GET',
              f'{DISK}/resources?path={q(path)}&preview_size={size}&preview_crop=false')
    if s != 200:
        raise Exception(f'preview meta:{s}')
    data = json.loads(b)
    url = data.get('preview', '')
    if not url:
        raise Exception('no preview field')
    return url

def get_download_link(token, path):
    """Возвращает одноразовую прямую ссылку для скачивания файла."""
    s, b = yd(token, 'GET', f'{DISK}/resources/download?path={q(path)}')
    if s >= 400:
        raise Exception(f'dl-link:{s}')
    return json.loads(b)['href']


def download_auth(token, path):
    s, b = yd(token, 'GET', f'{DISK}/resources/download?path={q(path)}')
    if s >= 400:
        raise Exception(f'dl-url:{s}')
    href = json.loads(b)['href']
    s2, body = yd(None, 'GET', href)
    if s2 >= 400:
        raise Exception(f'dl:{s2}')
    return body.decode('utf-8')

def download_public(pub_url):
    s, b = yd(None, 'GET',
              f'{DISK}/public/resources/download?public_key={q(pub_url)}')
    if s >= 400:
        raise Exception(f'pub-dl-url:{s}:{b.decode("utf-8","ignore")[:120]}')
    href = json.loads(b)['href']
    s2, body = yd(None, 'GET', href)
    if s2 >= 400:
        raise Exception(f'pub-dl:{s2}')
    return body.decode('utf-8')

def publish(token, path):
    yd(token, 'PUT', f'{DISK}/resources/publish?path={q(path)}')
    s, b = yd(token, 'GET', f'{DISK}/resources?path={q(path)}')
    if s >= 400:
        raise Exception(f'meta:{s}')
    return json.loads(b).get('public_url', '')

def parse_jsonl(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        try:    out.append(json.loads(line))
        except: pass
    return out

# ── contacts: только память + восстановление из chats/ ───────
def restore_contacts():
    """Читает contacts/ на Диске и восстанавливает контакты.
    Структура: contacts/contact-<id>/inbox.jsonl
    Имя и peer_url берутся из meta-записи в inbox.jsonl.
    """
    root         = S['root']
    contacts_dir = root + '/contacts'
    s, b = yd(S['token'], 'GET',
              f'{DISK}/resources?path={q(contacts_dir)}&limit=100')
    if s != 200:
        return
    folders = json.loads(b).get('_embedded', {}).get('items', [])
    for folder in folders:
        if folder.get('type') != 'dir':
            continue
        folder_path = folder['path']
        inbox_path  = folder_path + '/inbox.jsonl'
        try:
            txt  = download_auth(S['token'], inbox_path)
            recs = parse_jsonl(txt)
        except:
            continue
        name     = 'Unknown'
        peer_url = ''
        cid      = ''
        chat_url = ''
        for r in recs:
            if r.get('kind') == 'announce' and r.get('from'):
                name = r['from']
            if r.get('kind') == 'meta':
                peer_url     = r.get('peer_url', '')
                cid          = r.get('contact_id', '')
                contact_name = r.get('contact_name', '')
                if contact_name:
                    name = contact_name
        if not peer_url or not cid:
            continue
        if any(c['id'] == cid for c in S['contacts']):
            continue
        # Восстанавливаем public_url
        s2, b2 = yd(S['token'], 'GET',
                    f'{DISK}/resources?path={q(inbox_path)}')
        if s2 == 200:
            chat_url = json.loads(b2).get('public_url', '')
        if not chat_url:
            try: chat_url = publish(S['token'], inbox_path)
            except: pass
        S['contacts'].append({
            'id':        cid,
            'name':      name,
            'peer_url':  peer_url,
            'chat_path': inbox_path,
            'chat_url':  chat_url,
            'unread':    0,
            'seen':      set(),
        })

# ── Chat-file helpers ────────────────────────────────────────
def chat_path_for(cid):
    """Путь к МОЕМУ файлу для контакта cid (я пишу, собеседник читает)."""""
    return S['root'] + f'/contacts/contact-{cid}/inbox.jsonl'

def contact_dir(cid):
    return S['root'] + f'/contacts/contact-{cid}'

def attachments_dir(cid):
    return S['root'] + f'/contacts/contact-{cid}/attachments'

def append_chat(cid, record):
    """Дописывает запись в чат-файл контакта."""
    path = chat_path_for(cid)
    try:    existing = download_auth(S['token'], path)
    except: existing = ''
    upload(S['token'], path,
           existing + json.dumps(record, ensure_ascii=False) + '\n')

def load_peer_chat(c):
    """Читает inbox.jsonl собеседника (peer_url — его файл, он пишет, я читаю).
    Возвращает (name, new_messages_list)."""
    txt  = download_public(c['peer_url'])
    recs = parse_jsonl(txt)
    name = c.get('name', 'Unknown')
    for r in recs:
        if r.get('kind') == 'announce' and r.get('from'):
            name = r['from']; break
    new = []
    for r in recs:
        kind = r.get('kind')
        # пропускаем служебные записи
        if kind in ('announce', 'meta'): continue
        if not (r.get('id') and r.get('from') and r.get('ts')): continue
        if r['id'] in c['seen']: continue
        c['seen'].add(r['id'])
        if kind == 'text' or kind is None:
            pl   = r.get('payload', {})
            txt2 = pl.get('text','') if pl.get('enc_stub') == 'plain' else '[зашифровано]'
            new.append({'id': r['id'], 'from': r['from'], 'ts': r['ts'],
                        'kind': 'text', 'text': txt2, 'payload': pl, 'own': False})
        elif kind in ('image', 'file'):
            new.append({'id': r['id'], 'from': r['from'], 'ts': r['ts'],
                        'kind': kind,
                        'name': r.get('name', 'file'),
                        'size': r.get('size', 0),
                        'path': r.get('path', ''),
                        'own': False})
    return name, new

def find_contact(cid):
    for c in S['contacts']:
        if c['id'] == cid: return c
    return None

def active_contact():
    return find_contact(S['active_id']) if S['active_id'] else None

def contacts_for_ui():
    return [{'id': c['id'], 'name': c['name'],
             'peer_url': c['peer_url'], 'chat_url': c['chat_url'],
             'unread': c.get('unread', 0)}
            for c in S['contacts']]

# ── HTTP Handler ─────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a): pass

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, url):
        self.send_response(302)
        self.send_header('Location', url)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in [('Access-Control-Allow-Origin', '*'),
                     ('Access-Control-Allow-Headers', 'Content-Type'),
                     ('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')]:
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        qs     = urllib.parse.parse_qs(parsed.query)

        if path == '/':
            return self.send_html(HTML_MAIN)

        if path == '/auth/start':
            cid   = qs.get('client_id', [''])[0].strip()
            uname = qs.get('username',  [''])[0].strip()
            root  = qs.get('root', ['disk:/YaChatV2'])[0].strip().rstrip('/')
            if not cid:
                return self.send_html('<p>client_id not specified</p>')
            S['_pending_client_id'] = cid
            S['_pending_username']  = uname
            S['_pending_root']      = root
            cb  = 'http://127.0.0.1:8765/auth/callback'
            url = (f'{YANDEX_AUTH}?response_type=token'
                   f'&client_id={q(cid)}'
                   f'&redirect_uri={q(cb)}'
                   f'&force_confirm=no')
            return self.redirect(url)

        if path == '/auth/callback':
            return self.send_html(HTML_CALLBACK)

        if path == '/api/state':
            return self.send_json(200, {
                'ok':          True,
                'authed':      bool(S['token']),
                'username':    S['username'],
                'root':        S['root'],
                'contacts':    contacts_for_ui(),
                'active_id':   S['active_id'],
                'active_name': next((c['name'] for c in S['contacts']
                                     if c['id'] == S['active_id']), ''),
                'messages':    S['messages'],
                'my_card_url': S['my_card_url'],
            })

        if path == '/api/proxy-image':
            if not S['token']:
                self.send_response(401); self.end_headers(); return
            fpath = qs.get('path', [''])[0].strip()
            if not fpath:
                self.send_response(400); self.end_headers(); return
            try:
                prev_url = get_preview_url(S['token'], fpath)
                req2 = urllib.request.Request(
                    prev_url,
                    headers={'Authorization': f'OAuth {S["token"]}'}
                )
                with urllib.request.urlopen(req2, timeout=30) as resp:
                    img_data = resp.read()
                    ctype = resp.headers.get('Content-Type', 'image/jpeg')
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(img_data)))
                self.end_headers()
                self.wfile.write(img_data)
            except Exception as e:
                self.send_response(500); self.end_headers()
            return

        self.send_response(404); self.end_headers()

    def do_POST(self):
        n   = int(self.headers.get('Content-Length', 0) or 0)
        ct  = self.headers.get('Content-Type', '')
        if n > 0:
            raw = self.rfile.read(n)
        elif 'multipart' in ct:
            # fetch() с FormData иногда не шлёт Content-Length (chunked)
            # читаем до закрытия соединения с таймаутом
            chunks = []
            try:
                self.rfile._sock.settimeout(5)
            except Exception:
                pass
            try:
                while True:
                    chunk = self.rfile.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except Exception:
                pass
            raw = b''.join(chunks)
        else:
            raw = b'{}'

        # ── /api/upload — multipart/form-data ────────────
        if self.path == '/api/upload':
            try:
                if not S['token']:
                    return self.send_json(401, {'ok': False, 'error': 'not authorized'})
                c = active_contact()
                if not c:
                    return self.send_json(400, {'ok': False, 'error': 'no active contact'})

                # Парсим multipart вручную — без cgi (deprecated в 3.13)
                import mimetypes, re as _re
                ct = self.headers.get('Content-Type', '')
                bm = _re.search(r'boundary=([^;\s]+)', ct)
                if not bm:
                    return self.send_json(400, {'ok': False, 'error': 'no boundary in Content-Type'})
                boundary = ('--' + bm.group(1)).encode()

                filename   = 'file'
                file_bytes = b''
                mime_type  = 'application/octet-stream'

                # Разбиваем по boundary
                parts = raw.split(boundary)
                for part in parts[1:]:
                    if part.strip() in (b'', b'--', b'--\r\n'):
                        continue
                    # Отделяем заголовки от тела
                    if b'\r\n\r\n' in part:
                        hdr_raw, body_part = part.split(b'\r\n\r\n', 1)
                    elif b'\n\n' in part:
                        hdr_raw, body_part = part.split(b'\n\n', 1)
                    else:
                        continue
                    hdr_text = hdr_raw.decode('utf-8', errors='replace')
                    # Только поле file
                    if 'name="file"' not in hdr_text:
                        continue
                    # filename
                    fm = _re.search(r'filename="([^"]+)"', hdr_text)
                    if fm:
                        filename = fm.group(1)
                    # Content-Type из заголовка части
                    cm = _re.search(r'Content-Type:\s*([^\r\n]+)', hdr_text)
                    if cm:
                        mime_type = cm.group(1).strip()
                    # Тело — убираем финальный \r\n перед boundary
                    file_bytes = body_part.rstrip(b'\r\n')
                    break

                if not file_bytes:
                    return self.send_json(400, {'ok': False, 'error': 'no file in request'})

                if mime_type == 'application/octet-stream':
                    guessed = mimetypes.guess_type(filename)[0]
                    if guessed:
                        mime_type = guessed

                kind = 'image' if mime_type.startswith('image/') else 'file'
                ext  = filename.rsplit('.', 1)[-1] if '.' in filename else 'bin'

                # Путь в папке текущего контакта
                att_path = attachments_dir(c['id']) + f'/{rand_id()}.{ext}'
                pub_url  = upload_binary(S['token'], att_path, file_bytes, mime_type)

                record = {
                    'id':      'msg-' + rand_id(),
                    'kind':    kind,
                    'from':    S['username'],
                    'to':      c['name'],
                    'ts':      now_ms(),
                    'ts_iso':  ts_iso(),
                    'name':    filename,
                    'size':    len(file_bytes),
                    'path':    att_path,
                    'pub_url': pub_url,
                }
                append_chat(c['id'], record)
                msg = {
                    'id':   record['id'],
                    'from': S['username'],
                    'ts':   record['ts'],
                    'kind': kind,
                    'name': filename,
                    'size': len(file_bytes),
                    'path': att_path,
                    'own':  True,
                }
                c['seen'].add(record['id'])
                S['messages'].append(msg)
                return self.send_json(200, {'ok': True, 'message': msg})
            except Exception as e:
                tb = _tb.format_exc()
                print(f'UPLOAD ERROR: {e}\n{tb}', flush=True)
                return self.send_json(500, {'ok': False, 'error': str(e),
                                            'trace': tb[-1200:]})

        try:
            d = json.loads(raw.decode('utf-8'))
            p = self.path

            # ── /api/token (от OAuth callback) ───────────
            if p == '/api/token':
                token = d.get('access_token', '').strip()
                if not token:
                    return self.send_json(400, {'ok': False, 'error': 'no token'})
                S['token']     = token
                S['username']  = S['_pending_username']
                S['root']      = S['_pending_root'] or 'disk:/YaChatV2'
                S['client_id'] = S['_pending_client_id']
                return self.send_json(200, {'ok': True,
                                            'username': S['username'],
                                            'root':     S['root']})

            # ── /api/token_manual ────────────────────────
            if p == '/api/token_manual':
                token = d.get('token', '').strip()
                uname = d.get('username', '').strip()
                root  = d.get('root', 'disk:/YaChatV2').strip().rstrip('/')
                if not token or not uname:
                    return self.send_json(400, {'ok': False,
                                                'error': 'token and username required'})
                S['token'] = token; S['username'] = uname; S['root'] = root
                return self.send_json(200, {'ok': True, 'username': uname})

            # ── /api/setup ───────────────────────────────
            if p == '/api/setup':
                if not S['token']:
                    return self.send_json(401, {'ok': False, 'error': 'not authorized'})
                root         = S['root']
                contacts_dir = root + '/contacts'
                log = []

                # Корневая папка
                st_root, _ = ensure_dir(S['token'], root)
                log.append(f'root: {st_root}')

                # contacts/ — папка для всех бесед
                st_cnt, _ = ensure_dir(S['token'], contacts_dir)
                log.append(f'contacts/: {st_cnt}')

                # Восстанавливаем контакты из contacts/*/inbox.jsonl
                S['contacts'] = []
                restore_contacts()
                log.append(f'contacts restored: {len(S["contacts"])}')

                # my_card_url — chat_url активного контакта (если есть)
                if not S['my_card_url'] and S['contacts']:
                    first = S['contacts'][0]
                    S['my_card_url'] = first.get('chat_url', '')
                    if S['active_id'] == '':
                        S['active_id'] = first['id']

                return self.send_json(200, {
                    'ok':           True,
                    'log':          log,
                    'contacts':     contacts_for_ui(),
                    'my_card_url':  S['my_card_url'],
                    'setup_done':   True,
                })

            # ── /api/contacts/new ────────────────────────
            # Шаг 1: создаём папку и inbox.jsonl, возвращаем ссылку
            # peer_url пока неизвестен — пользователь ещё не ввёл
            if p == '/api/contacts/new':
                cid = rand_id()
                ensure_dir(S['token'], contact_dir(cid))
                ensure_dir(S['token'], attachments_dir(cid))
                chat_path = chat_path_for(cid)
                ann = {'id': 'announce-'+rand_id(), 'kind': 'announce',
                       'from': S['username'], 'ts': now_ms(), 'ts_iso': ts_iso()}
                upload(S['token'], chat_path,
                       json.dumps(ann, ensure_ascii=False) + '\n')
                chat_url = publish(S['token'], chat_path)
                return self.send_json(200, {
                    'ok':       True,
                    'cid':      cid,
                    'chat_url': chat_url,
                })

            # ── /api/contacts/add ────────────────────────
            # Шаг 2: пользователь ввёл peer_url и имя
            # cid уже создан на шаге 1 (/api/contacts/new)
            if p == '/api/contacts/add':
                cid      = d.get('cid', '').strip()
                peer_url = d.get('peer_url', '').strip()
                name_in  = d.get('name', '').strip()
                if not peer_url or not cid:
                    return self.send_json(400, {'ok': False, 'error': 'cid and peer_url required'})

                # Дубликат по peer_url?
                for c in S['contacts']:
                    if c['peer_url'] == peer_url:
                        S['active_id']   = c['id']
                        S['my_card_url'] = c.get('chat_url', '')
                        return self.send_json(200, {'ok': True,
                            'contact': {'id': c['id'], 'name': c['name'],
                                        'peer_url': c['peer_url'], 'chat_url': c['chat_url'],
                                        'unread': c.get('unread', 0)},
                            'my_card_url': c.get('chat_url',''), 'duplicate': True})

                # Читаем inbox собеседника — берём имя из announce если не задано
                try:
                    txt  = download_public(peer_url)
                    recs = parse_jsonl(txt)
                except Exception as e:
                    return self.send_json(400, {'ok': False, 'error': str(e)})

                name = name_in or 'Unknown'
                if not name_in:
                    for r in recs:
                        if r.get('kind') == 'announce' and r.get('from'):
                            name = r['from']; break
                    if name == 'Unknown':
                        for r in recs:
                            if r.get('from'): name = r['from']; break

                # Дописываем meta-запись в уже созданный inbox.jsonl
                chat_path = chat_path_for(cid)
                try:
                    existing = download_auth(S['token'], chat_path)
                except:
                    existing = ''
                meta = {'id': 'meta-'+rand_id(), 'kind': 'meta',
                        'contact_id': cid, 'peer_url': peer_url,
                        'contact_name': name,
                        'ts': now_ms(), 'ts_iso': ts_iso()}
                if existing and not existing.endswith('\n'):
                    existing += '\n'
                upload(S['token'], chat_path,
                       existing + json.dumps(meta, ensure_ascii=False) + '\n')

                # chat_url уже опубликован на шаге 1 — берём из ресурса
                s2, b2 = yd(S['token'], 'GET',
                            f'{DISK}/resources?path={q(chat_path)}')
                chat_url = json.loads(b2).get('public_url', '') if s2 == 200 else ''
                if not chat_url:
                    chat_url = publish(S['token'], chat_path)

                contact = {
                    'id':        cid,
                    'name':      name,
                    'peer_url':  peer_url,
                    'chat_path': chat_path,
                    'chat_url':  chat_url,
                    'unread':    0,
                    'seen':      set(),
                }
                S['contacts'].append(contact)
                S['active_id']   = cid
                S['my_card_url'] = chat_url
                S['messages']    = []

                # Читаем уже существующие сообщения собеседника
                _, new_msgs = load_peer_chat(contact)
                S['messages'].extend(new_msgs)
                S['messages'].sort(key=lambda x: x['ts'])

                return self.send_json(200, {
                    'ok': True,
                    'contact': {
                        'id':       cid,
                        'name':     name,
                        'peer_url': peer_url,
                        'chat_url': chat_url,
                        'unread':   0,
                    },
                    'messages':    S['messages'],
                    'loaded':      len(new_msgs),
                    'my_card_url': chat_url,
                })

            # ── /api/contacts/select ─────────────────────
            if p == '/api/contacts/select':
                cid = d.get('id', '').strip()
                c   = find_contact(cid)
                if not c:
                    return self.send_json(404, {'ok': False, 'error': 'contact not found'})
                S['active_id'] = cid
                c['unread']    = 0
                S['my_card_url'] = c.get('chat_url', '')
                # Загружаем свежие сообщения собеседника
                try:
                    _, new_msgs = load_peer_chat(c)
                except:
                    new_msgs = []
                # Собираем full history: свои (own) + чужие из seen
                # own messages — читаем из своего чат-файла
                own_msgs = []
                try:
                    own_txt = download_auth(S['token'], c['chat_path'])
                    for r in parse_jsonl(own_txt):
                        kind = r.get('kind')
                        if not (r.get('id') and r.get('ts')): continue
                        if kind == 'text' or kind is None:
                            pl   = r.get('payload', {})
                            txt2 = pl.get('text','') if pl.get('enc_stub')=='plain' else '[зашифровано]'
                            own_msgs.append({'id': r['id'], 'from': r['from'],
                                             'ts': r['ts'], 'kind': 'text',
                                             'text': txt2, 'payload': pl, 'own': True})
                        elif kind in ('image', 'file'):
                            own_msgs.append({'id': r['id'], 'from': r['from'],
                                             'ts': r['ts'], 'kind': kind,
                                             'name': r.get('name','file'),
                                             'size': r.get('size', 0),
                                             'path': r.get('path',''),
                                             'own': True})
                except:
                    pass
                all_msgs = own_msgs + [m for m in S['messages']
                                       if not m.get('own')]
                # Добавим только что загруженные
                seen_ids = {m['id'] for m in all_msgs}
                for m in new_msgs:
                    if m['id'] not in seen_ids:
                        all_msgs.append(m)
                all_msgs.sort(key=lambda x: x['ts'])
                S['messages'] = all_msgs
                return self.send_json(200, {
                    'ok':          True,
                    'contact':     {'id': c['id'], 'name': c['name'],
                                    'peer_url': c['peer_url'], 'chat_url': c['chat_url'],
                                    'unread': 0},
                    'messages':    S['messages'],
                    'my_card_url': c.get('chat_url', ''),
                })

            # ── /api/contacts/delete ─────────────────────
            if p == '/api/contacts/delete':
                cid    = d.get('id', '').strip()
                before = len(S['contacts'])
                S['contacts'] = [c for c in S['contacts'] if c['id'] != cid]
                if S['active_id'] == cid:
                    S['active_id'] = S['contacts'][0]['id'] if S['contacts'] else ''
                    S['messages']  = []
                return self.send_json(200, {'ok': True,
                                            'removed': before - len(S['contacts'])})

            # ── /api/send ────────────────────────────────
            if p == '/api/send':
                text       = d.get('text', '').strip()
                payload_in = d.get('payload', None)   # зашифрованный payload от JS
                c = active_contact()
                if not c:
                    return self.send_json(400, {'ok': False, 'error': 'no active contact'})
                if payload_in:
                    # JS зашифровал — храним как есть, не знаем содержимое
                    record_payload = payload_in
                    display_text   = '[зашифровано]'
                elif text:
                    record_payload = {'enc_stub': 'plain', 'text': text}
                    display_text   = text
                else:
                    return self.send_json(400, {'ok': False, 'error': 'empty'})
                record = {
                    'id':      'msg-' + rand_id(),
                    'kind':    'text',
                    'from':    S['username'],
                    'ts':      now_ms(),
                    'ts_iso':  ts_iso(),
                    'payload': record_payload,
                }
                append_chat(c['id'], record)
                msg = {'id': record['id'], 'from': S['username'],
                       'ts': record['ts'],  'text': display_text,
                       'payload': record_payload, 'own': True, 'kind': 'text'}
                c['seen'].add(record['id'])
                S['messages'].append(msg)
                return self.send_json(200, {'ok': True, 'message': msg})

            # /api/upload обрабатывается выше (multipart, до JSON-парсинга)

            # ── /api/preview ──────────────────────────────
            if p == '/api/preview':
                if not S['token']:
                    return self.send_json(401, {'ok': False, 'error': 'not authorized'})
                path = d.get('path', '').strip()
                if not path:
                    return self.send_json(400, {'ok': False, 'error': 'path required'})
                try:
                    url = get_preview_url(S['token'], path)
                    return self.send_json(200, {'ok': True, 'url': url})
                except Exception as e:
                    return self.send_json(500, {'ok': False, 'error': str(e)})

            # ── /api/download-link ────────────────────────
            if p == '/api/download-link':
                if not S['token']:
                    return self.send_json(401, {'ok': False, 'error': 'not authorized'})
                path = d.get('path', '').strip()
                if not path:
                    return self.send_json(400, {'ok': False, 'error': 'path required'})
                try:
                    url = get_download_link(S['token'], path)
                    return self.send_json(200, {'ok': True, 'url': url})
                except Exception as e:
                    return self.send_json(500, {'ok': False, 'error': str(e)})

                        # ── /api/poll ────────────────────────────────
            if p == '/api/poll':
                c = active_contact()
                if not c:
                    return self.send_json(200, {'ok': True, 'new': [],
                                                'contacts': contacts_for_ui()})
                try:
                    name, new_msgs = load_peer_chat(c)
                    if name != c['name']:
                        c['name'] = name
                    for m in new_msgs:
                        S['messages'].append(m)
                    if new_msgs:
                        S['messages'].sort(key=lambda x: x['ts'])
                    # Фоновый подсчёт unread для неактивных контактов
                    for other in S['contacts']:
                        if other['id'] == S['active_id']: continue
                        try:
                            _, bg = load_peer_chat(other)
                            other['unread'] = other.get('unread', 0) + len(bg)
                        except:
                            pass
                    return self.send_json(200, {
                        'ok':       True,
                        'new':      new_msgs,
                        'contacts': contacts_for_ui(),
                    })
                except Exception as e:
                    return self.send_json(200, {'ok': True, 'new': [],
                                                'warn': str(e),
                                                'contacts': contacts_for_ui()})

            # ── /api/state ───────────────────────────────
            if p == '/api/state':
                c = active_contact()
                return self.send_json(200, {
                    'ok':         True,
                    'authed':     bool(S['token']),
                    'setup_done': bool(S['token']
                                       and disk_exists(S['token'],
                                           S['root'] + '/chats')
                                       if S['token'] else False),
                    'username':   S['username'],
                    'root':       S['root'],
                    'contacts':    contacts_for_ui(),
                    'active_id':   S['active_id'],
                    'active_name': c['name'] if c else '',
                    'messages':    S['messages'],
                    'my_card_url': S['my_card_url'],
                })

            return self.send_json(404, {'ok': False, 'error': 'not found'})

        except Exception as e:
            import traceback
            return self.send_json(500, {'ok': False, 'error': str(e),
                                        'trace': traceback.format_exc()[-400:]})


# ════════════════════════════════════════════════════════════
#  HTML pages
# ════════════════════════════════════════════════════════════

HTML_CALLBACK = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<title>YaDisk Chat — OAuth</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font:16px/1.5 Inter,sans-serif;background:#171614;color:#cdccca;
     min-height:100dvh;display:grid;place-items:center}
.card{background:#1c1b19;border:1px solid #393836;border-radius:16px;
      padding:36px 40px;text-align:center;max-width:380px;width:90%}
h1{font-size:20px;margin-bottom:8px}
p{color:#797876;font-size:14px;margin-top:6px}
.dot{width:10px;height:10px;border-radius:50%;background:#4f98a3;
     display:inline-block;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
.err{color:#d163a7;font-size:12px;margin-top:12px;word-break:break-all}
</style></head><body>
<div class="card">
  <h1><span class="dot"></span>&nbsp;Авторизация</h1>
  <p id="msg">Читаем токен, возвращаемся в приложение...</p>
  <div class="err" id="err"></div>
</div>
<script>
(function(){
  // ── Шаг 1: спасаем hash немедленно, до любых редиректов ──
  const rawHash = window.location.hash;
  if(rawHash.includes('access_token')){
    sessionStorage.setItem('yx_cb_hash', rawHash);
  }

  // ── Шаг 2: берём hash — свежий или из sessionStorage ──────
  const savedHash = sessionStorage.getItem('yx_cb_hash') || '';
  const hash = rawHash.includes('access_token') ? rawHash : savedHash;
  sessionStorage.removeItem('yx_cb_hash');

  const p = new URLSearchParams(hash.slice(1));
  const t = p.get('access_token');

  const msgEl = document.getElementById('msg');
  const errEl = document.getElementById('err');

  if(!t){
    msgEl.textContent = 'Токен не найден.';
    errEl.textContent = 'hash был: '+(hash||'(пусто)');
    return;
  }

  // ── Шаг 3: retry — отправляем токен на сервер (до 3 попыток) ──
  function sendToken(attempt){
    fetch('/api/token',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({access_token:t})})
    .then(r=>r.json()).then(j=>{
      if(j.ok){
        msgEl.textContent = 'Успешно! Переходим...';
        window.location.href = '/?authed=1';
      } else {
        errEl.textContent = 'Ошибка сервера: '+(j.error||'?');
      }
    }).catch(e=>{
      if(attempt < 3){
        msgEl.textContent = 'Повторная попытка ('+(attempt+1)+'/3)...';
        setTimeout(()=>sendToken(attempt+1), 800);
      } else {
        errEl.textContent = 'Нет связи с сервером: '+e;
      }
    });
  }
  sendToken(1);
})();
</script></body></html>"""


HTML_MAIN = r"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>YaDisk Chat v6</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ── tokens ─────────────────────────────────────────────── */
:root,[data-theme="light"]{
  --bg:#f7f6f2;--s1:#f9f8f5;--s2:#f3f0ec;--s3:#edeae5;
  --bd:#d4d1ca;--dv:#dcd9d5;
  --tx:#28251d;--mu:#7a7974;--fa:#bab9b4;
  --pr:#01696f;--pr2:#0c4e54;--ps:#cedcd8;
  --own-bg:#014d52;--own-tx:#f0fbfb;
  --oth-bg:#f0eeea;--oth-tx:#28251d;
  --ok:#437a22;--er:#a12c7b;--wa:#964219;
  --sh1:0 1px 3px rgba(40,37,29,.07);
  --sh2:0 4px 14px rgba(40,37,29,.09);
}
[data-theme="dark"]{
  --bg:#171614;--s1:#1c1b19;--s2:#1f1e1c;--s3:#252421;
  --bd:#393836;--dv:#2a2927;
  --tx:#cdccca;--mu:#797876;--fa:#5a5957;
  --pr:#4f98a3;--pr2:#3a7f8a;--ps:#1e3537;
  --own-bg:#1f4447;--own-tx:#d8f4f7;
  --oth-bg:#252321;--oth-tx:#cdccca;
  --ok:#6daa45;--er:#d163a7;--wa:#bb653b;
  --sh1:0 1px 3px rgba(0,0,0,.22);
  --sh2:0 4px 14px rgba(0,0,0,.32);
}
/* ── reset ──────────────────────────────────────────────── */
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;-webkit-font-smoothing:antialiased}
body{font:16px/1.5 Inter,sans-serif;background:var(--bg);color:var(--tx);overflow:hidden}
button,input,textarea{font:inherit;color:inherit}
button{cursor:pointer;background:none;border:none}
input,textarea{outline:none}
a{color:var(--pr)}

/* ── layout ─────────────────────────────────────────────── */
.app{display:grid;grid-template-columns:320px 1fr;height:100dvh}
.side{background:var(--s1);border-right:1px solid var(--bd);
      display:flex;flex-direction:column;overflow-y:auto;scrollbar-width:thin}
.main{display:grid;grid-template-rows:56px 1fr auto 28px;min-width:0;max-height:100dvh}

/* ── sidebar sections ────────────────────────────────────── */
.brand{padding:13px 15px;border-bottom:1px solid var(--bd);
       display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.brand-title{font-size:15px;font-weight:700;letter-spacing:-.02em}
.brand-sub{font-size:10.5px;color:var(--mu);margin-top:1px}
.sec{padding:13px 15px;border-bottom:1px solid var(--bd);
     display:flex;flex-direction:column;gap:8px;flex-shrink:0}
.sec-hd{display:flex;justify-content:space-between;align-items:center}
.sec-label{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;
            font-weight:600;color:var(--mu)}
.badge{font-size:10.5px;font-family:"JetBrains Mono",monospace;
       background:var(--s2);border:1px solid var(--bd);
       border-radius:999px;padding:2px 8px;color:var(--mu)}

/* ── inputs / buttons ────────────────────────────────────── */
.inp{background:var(--bg);border:1px solid var(--bd);border-radius:9px;
     padding:8px 11px;width:100%;font-size:13.5px;transition:.15s}
.inp:focus{border-color:var(--pr)}
.inp::placeholder{color:var(--fa)}
.mono{font-family:"JetBrains Mono",monospace;font-size:12px}
.row{display:flex;gap:6px}
.btn{padding:8px 12px;border-radius:9px;font-weight:600;font-size:13px;
     background:var(--s2);border:1px solid var(--bd);transition:.15s;white-space:nowrap}
.btn:hover:not(:disabled){background:var(--s3)}
.btn:disabled{opacity:.4;cursor:default}
.btn.primary{background:var(--pr);color:#fff;border-color:transparent}
.btn.primary:hover:not(:disabled){background:var(--pr2)}
.btn.ya{background:#fc3f1d;color:#fff;border-color:transparent;
        display:flex;align-items:center;justify-content:center;gap:7px;
        width:100%;padding:10px 12px}
.btn.ya:hover:not(:disabled){background:#e83412}
.btn.sm{padding:5px 10px;font-size:12px}
.btn.danger{background:var(--er);color:#fff;border-color:transparent}
.hint{font-size:11.5px;color:var(--mu);line-height:1.65}

/* ── auth badge ──────────────────────────────────────────── */
.auth-row{display:flex;align-items:center;gap:9px;background:var(--bg);
          border:1px solid var(--bd);border-radius:9px;padding:9px 11px}
.av{width:30px;height:30px;border-radius:50%;background:var(--ps);
    display:grid;place-items:center;font-weight:700;font-size:13px;
    color:var(--pr);flex-shrink:0}
.av.big{width:34px;height:34px;font-size:14px}
.auth-name{font-weight:600;font-size:13px}
.auth-root{font-size:10.5px;color:var(--mu);font-family:"JetBrains Mono",monospace}

/* ── link box ────────────────────────────────────────────── */
.link-box{background:var(--bg);border:1px solid var(--bd);border-radius:9px;
          padding:9px 11px;display:flex;gap:8px;align-items:flex-start}
.link-val{flex:1;font-family:"JetBrains Mono",monospace;font-size:10px;
          color:var(--pr);word-break:break-all;line-height:1.5}
.copy-btn{flex-shrink:0;padding:4px 8px;border-radius:7px;background:var(--ps);
          color:var(--pr);font-size:11px;font-weight:600;transition:.15s}
.copy-btn:hover{background:var(--pr);color:#fff}

/* ── status rows ─────────────────────────────────────────── */
.st-row{display:flex;justify-content:space-between;align-items:center;
        font-size:12px;gap:6px}
.ok{color:var(--ok)}.er{color:var(--er)}.wa{color:var(--wa)}

/* ── details/summary ─────────────────────────────────────── */
details{background:var(--bg);border:1px solid var(--bd);border-radius:9px}
summary{padding:8px 11px;font-size:12px;color:var(--mu);cursor:pointer;
        list-style:none;display:flex;align-items:center;gap:5px}
summary::-webkit-details-marker{display:none}
summary::before{content:"&#9654;";font-size:8px;transition:.15s}
details[open] summary::before{content:"&#9660;"}
.det-body{padding:0 11px 11px;display:flex;flex-direction:column;gap:8px}

/* ── contact list ────────────────────────────────────────── */
.contact-list{display:flex;flex-direction:column;gap:4px}
.c-empty{font-size:12px;color:var(--fa);text-align:center;padding:8px 0}
.c-item{display:flex;align-items:center;gap:9px;padding:8px 10px;
        border-radius:9px;background:var(--bg);border:1px solid var(--bd);
        cursor:pointer;transition:.15s;min-width:0;position:relative}
.c-item:hover{background:var(--s2)}
.c-item.active{background:var(--ps);border-color:var(--pr)}
.c-av{width:30px;height:30px;border-radius:50%;background:var(--s2);
      border:1px solid var(--bd);display:grid;place-items:center;
      font-weight:700;font-size:12px;color:var(--pr);flex-shrink:0}
.c-item.active .c-av{background:var(--pr);color:#fff;border-color:transparent}
.c-info{flex:1;min-width:0}
.c-name{font-size:13px;font-weight:600;white-space:nowrap;
        overflow:hidden;text-overflow:ellipsis}
.c-sub{font-size:10px;color:var(--fa);font-family:"JetBrains Mono",monospace;
       white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.c-unread{min-width:18px;height:18px;border-radius:999px;background:var(--pr);
          color:#fff;font-size:10px;font-weight:700;display:grid;
          place-items:center;padding:0 5px;flex-shrink:0}
.c-del{flex-shrink:0;width:22px;height:22px;border-radius:5px;
       display:grid;place-items:center;color:var(--fa);
       font-size:13px;transition:.15s}
.c-del:hover{background:var(--er);color:#fff}
.add-contact-btn{display:flex;align-items:center;gap:7px;padding:8px 10px;
  border-radius:9px;border:1px dashed var(--bd);color:var(--mu);
  font-size:13px;cursor:pointer;transition:.15s;width:100%;background:none}
.add-contact-btn:hover{background:var(--s2);border-color:var(--pr);color:var(--pr)}
.add-form{display:flex;flex-direction:column;gap:7px;
          background:var(--s2);border:1px solid var(--bd);
          border-radius:9px;padding:10px}

/* ── chat header ─────────────────────────────────────────── */
.chat-head{padding:0 16px;background:var(--s1);border-bottom:1px solid var(--bd);
           display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.chat-head-left{display:flex;align-items:center;gap:10px;min-width:0}
.chat-head h2{font-size:14.5px;font-weight:700;white-space:nowrap;
              overflow:hidden;text-overflow:ellipsis}
.chat-head p{font-size:11px;color:var(--mu);margin-top:1px;
             white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill{padding:3px 9px;border-radius:999px;background:var(--s2);
      border:1px solid var(--bd);font-size:10.5px;
      font-family:"JetBrains Mono",monospace;flex-shrink:0}

/* ── messages ────────────────────────────────────────────── */
.messages{overflow-y:auto;padding:14px 16px;
          display:flex;flex-direction:column;gap:8px;scrollbar-width:thin}
.empty-chat{flex:1;display:flex;flex-direction:column;align-items:center;
            justify-content:center;gap:10px;color:var(--fa);text-align:center;padding:24px}
.empty-chat svg{opacity:.18}
.empty-chat p{font-size:13px;max-width:32ch;line-height:1.6;color:var(--mu)}

.msg{max-width:75%;display:flex;flex-direction:column;gap:3px;
     animation:msgIn .14s ease-out}
@keyframes msgIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.msg.own{align-self:flex-end;align-items:flex-end}
.msg.other{align-self:flex-start;align-items:flex-start}
.msg-from{font-size:10px;color:var(--pr);font-weight:500;margin-bottom:1px}
.bubble{padding:8px 11px;border-radius:14px;word-break:break-word;
        white-space:pre-wrap;font-size:13.5px;line-height:1.45;box-shadow:var(--sh1)}
.own .bubble{background:var(--own-bg);color:var(--own-tx);border-bottom-right-radius:3px}
.other .bubble{background:var(--oth-bg);color:var(--oth-tx);
               border:1px solid var(--bd);border-bottom-left-radius:3px}
.msg-time{font-size:9px;color:var(--fa);font-family:"JetBrains Mono",monospace}
.day-sep{display:flex;align-items:center;gap:8px;color:var(--fa);
         font-size:10px;font-family:"JetBrains Mono",monospace;margin:3px 0}
.day-sep::before,.day-sep::after{content:"";flex:1;height:1px;background:var(--dv)}

/* ── compose ─────────────────────────────────────────────── */
.compose{padding:10px 14px;background:var(--s1);border-top:1px solid var(--bd);
         display:flex;flex-direction:column;gap:6px;flex-shrink:0}
.compose-row{display:flex;gap:8px;align-items:flex-end}
.compose textarea{flex:1;min-height:42px;max-height:120px;resize:none;
  background:var(--bg);border:1px solid var(--bd);border-radius:10px;
  padding:9px 12px;font-size:13.5px;line-height:1.4;
  scrollbar-width:thin;transition:.15s}
.compose textarea:focus{border-color:var(--pr)}
.send-btn{width:40px;height:40px;border-radius:10px;background:var(--pr);
          color:#fff;display:grid;place-items:center;flex-shrink:0;transition:.15s}
.send-btn:hover:not(:disabled){background:var(--pr2)}
.send-btn:disabled{opacity:.35;cursor:default}
.compose-foot{font-size:10.5px;color:var(--fa);display:flex;justify-content:space-between}
.enc-tag{color:var(--wa);font-family:"JetBrains Mono",monospace;font-size:10px}

/* ── status bar ──────────────────────────────────────────── */
.statusbar{padding:5px 14px;background:var(--bg);border-top:1px solid var(--dv);
           display:flex;align-items:center;gap:7px;
           font-size:10.5px;font-family:"JetBrains Mono",monospace;flex-shrink:0}
.dot{width:6px;height:6px;border-radius:50%;flex-shrink:0;transition:.3s}
.dot.on{background:var(--ok);animation:blink 2.5s infinite}
.dot.warn{background:var(--wa)}
.dot.off{background:var(--fa)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
.statusbar .stxt{color:var(--mu)}

/* ── media messages ──────────────────────────────────────── */
.bubble-media{padding:5px;border-radius:14px;overflow:hidden;
              box-shadow:var(--sh1);max-width:260px;cursor:pointer}
.own .bubble-media{background:var(--own-bg);border-bottom-right-radius:3px}
.other .bubble-media{background:var(--oth-bg);border:1px solid var(--bd);
                     border-bottom-left-radius:3px}
.media-img{display:block;width:100%;max-width:260px;height:auto;
           border-radius:10px;background:var(--s2);min-height:80px;
           object-fit:cover;transition:.2s}
.media-img.loading{filter:blur(4px) brightness(.7)}
.media-placeholder{width:240px;height:160px;background:var(--s2);
                   border-radius:10px;display:grid;place-items:center;
                   color:var(--fa);font-size:12px;font-family:"JetBrains Mono",monospace}
.media-caption{font-size:10px;color:var(--fa);padding:3px 5px 2px;
               font-family:"JetBrains Mono",monospace;white-space:nowrap;
               overflow:hidden;text-overflow:ellipsis}
.file-bubble{display:flex;align-items:center;gap:9px;padding:9px 12px;
             border-radius:14px;box-shadow:var(--sh1);min-width:180px;max-width:260px}
.own .file-bubble{background:var(--own-bg);color:var(--own-tx);border-bottom-right-radius:3px}
.other .file-bubble{background:var(--oth-bg);color:var(--oth-tx);
                    border:1px solid var(--bd);border-bottom-left-radius:3px}
.file-icon{width:32px;height:32px;border-radius:8px;background:var(--ps);
           display:grid;place-items:center;flex-shrink:0;color:var(--pr);font-size:17px}
.file-info{flex:1;min-width:0}
.file-name{font-size:12.5px;font-weight:600;white-space:nowrap;
           overflow:hidden;text-overflow:ellipsis}
.file-size{font-size:10px;color:var(--fa);font-family:"JetBrains Mono",monospace;margin-top:1px}
.file-dl{flex-shrink:0;font-size:18px;cursor:pointer;padding:2px 4px;
         border-radius:6px;transition:.15s}
.file-dl:hover{background:var(--pr);color:#fff}

/* ── attach button ───────────────────────────────────────── */
.attach-btn{width:40px;height:40px;border-radius:10px;background:var(--s2);
            border:1px solid var(--bd);color:var(--mu);display:grid;
            place-items:center;flex-shrink:0;transition:.15s}
.attach-btn:hover{background:var(--s3);color:var(--pr);border-color:var(--pr)}
.upload-progress{font-size:11px;color:var(--wa);font-family:"JetBrains Mono",monospace;
                 display:none;align-items:center;gap:6px}
.upload-progress.active{display:flex}
.prog-bar{flex:1;height:3px;background:var(--s3);border-radius:2px;overflow:hidden}
.prog-fill{height:100%;background:var(--pr);transition:width .3s;width:0%}

/* ── setup-status-row ────────────────────────────────────── */
.setup-log{font-size:11px;color:var(--mu);font-family:"JetBrains Mono",monospace;
           background:var(--s2);border-radius:7px;padding:6px 9px;line-height:1.7}

@media(max-width:820px){
  .app{grid-template-columns:1fr}
  .side{position:fixed;z-index:10;width:300px;height:100%;
        transform:translateX(-100%);transition:.2s}
  .side.open{transform:none}
}
</style>
</head>
<body>
<div class="app">

<!-- ══ SIDEBAR ══════════════════════════════════════════════ -->
<aside class="side" id="side">

  <!-- Brand -->
  <div class="brand">
    <div>
      <div class="brand-title">YaDisk Chat <span style="color:var(--pr)">v6</span></div>
      <div class="brand-sub">у каждого хранится только своё</div>
    </div>
    <button id="themeBtn" title="Тема" style="font-size:17px;padding:3px 5px">&#9680;</button>
  </div>

  <!-- Шаг 1: Авторизация -->
  <div class="sec" id="s_auth">
    <div class="sec-hd">
      <span class="sec-label">Авторизация</span>
      <span class="badge">шаг 1</span>
    </div>
    <div id="authForm">
      <div style="display:flex;flex-direction:column;gap:7px">
        <input id="usernameIn" class="inp" placeholder="Ваше имя (отображаемое)">
        <input id="rootIn"     class="inp mono" placeholder="disk:/YaChatV2">
        <input id="clientIdIn" class="inp mono" placeholder="ClientID с oauth.yandex.ru">
        <button class="btn ya" id="oauthBtn">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
            <path d="M13.604 21v-8.4h2.4L16.404 9h-2.8V7.2c0-.9.24-1.512 1.536-1.512l1.64-.002V2.096A21.94 21.94 0 0 0 14.54 2c-2.388 0-4.02 1.458-4.02 4.134V9H8.1v3.6h2.42V21h3.084z"/>
          </svg>
          Войти через Яндекс
        </button>
        <details>
          <summary>Ввести токен вручную</summary>
          <div class="det-body">
            <input id="tokenManual" class="inp mono" placeholder="OAuth token...">
            <button class="btn primary sm" id="tokenManualBtn">Применить</button>
          </div>
        </details>
        <div class="hint">
          Нужен <b>ClientID</b> приложения на
          <a href="https://oauth.yandex.ru/" target="_blank">oauth.yandex.ru</a><br>
          Доступы: <span class="mono">disk.read</span> + <span class="mono">disk.write</span><br>
          Callback: <span class="mono">http://127.0.0.1:8765/auth/callback</span>
        </div>
      </div>
    </div>
    <div id="authBadge" style="display:none">
      <div class="auth-row">
        <div class="av" id="authAv">?</div>
        <div>
          <div class="auth-name" id="authName">—</div>
          <div class="auth-root" id="authRoot">—</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Шаг 2: Настройка Диска -->
  <div class="sec" id="s_setup" style="display:none">
    <div class="sec-hd">
      <span class="sec-label">Диск</span>
      <span class="badge">шаг 2</span>
    </div>
    <button class="btn primary" id="setupBtn">Проверить / создать структуру &rarr;</button>
    <div class="hint">
      Проверяет папки <span class="mono">YaChatV2/</span> и <span class="mono">contacts/</span>.<br>
      Если есть — читает контакты. Если нет — создаёт.
    </div>
    <div id="setupLog" class="setup-log" style="display:none"></div>
  </div>

  <!-- Шаг 3: Добавить контакт (всегда видимо после setup) -->
  <div class="sec" id="s_contacts" style="display:none">
    <div class="sec-hd">
      <span class="sec-label">Контакты</span>
    </div>
    <div class="contact-list" id="contactList">
      <div class="c-empty" id="cEmpty">Контактов пока нет</div>
    </div>
    <!-- Форма добавления — всегда открыта если нет контактов -->
    <div class="add-form" id="addForm" style="display:none">
      <div class="hint" style="margin-bottom:6px">
        <b>Шаг 1:</b> Ваша ссылка уже создана — скопируйте из блока «Моя ссылка» и передайте собеседнику.<br>
        <b>Шаг 2:</b> Получите ссылку собеседника и введите ниже:
      </div>
      <input id="contactNameIn" class="inp" placeholder="Имя контакта (необязательно)" style="margin-bottom:4px">
      <input id="peerUrlIn" class="inp mono" placeholder="Ссылка собеседника https://disk.yandex.ru/d/...">
      <div class="row">
        <button class="btn primary sm" id="addPeerBtn">Добавить</button>
        <button class="add-contact-btn" id="addContactToggle" style="padding:5px 10px;font-size:12px;width:auto">
          Скрыть
        </button>
      </div>
    </div>
    <button class="add-contact-btn" id="addContactShow" style="display:flex">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
      </svg>
      Добавить контакт
    </button>
  </div>

  <!-- Моя визитка -->
  <div class="sec" id="s_mylinks" style="display:none">
    <div class="sec-hd">
      <span class="sec-label">📤 Моя ссылка для собеседников</span>
    </div>
    <div class="hint">
      Это <b>ваша ссылка</b> для текущего контакта. Передайте её собеседнику — он вставит в «Добавить контакт». Получите его ссылку и введите ниже.
    </div>
    <div id="myCardLinkBox" style="display:flex;flex-direction:column;gap:6px">
      <div class="c-empty">Загрузка…</div>
    </div>
  </div>

  <!-- Состояние -->
  <div class="sec" id="s_status" style="display:none">
    <span class="sec-label">Состояние</span>
    <div class="st-row"><span>Авторизация</span><strong id="stAuth" class="er">нет</strong></div>
    <div class="st-row"><span>Структура Диска</span><strong id="stDisk" class="er">нет</strong></div>
    <div class="st-row"><span>Контактов</span><strong id="stCnt">0</strong></div>
    <div class="st-row"><span>Шифрование</span><strong id="encStatus" class="wa">🔓 plain</strong></div>
    <div class="st-row"><span>Поллинг</span><strong id="stPoll" class="er">off</strong></div>
  </div>

  <!-- Принцип -->
  <div class="sec" style="margin-top:auto">
    <span class="sec-label">Принцип</span>
    <div class="hint" style="line-height:1.9">
      &#128194; <span class="mono">contacts/contact-XYZ/inbox.jsonl</span><br>
      &nbsp;&nbsp;&nbsp;&#8592; только ваши слова конкретному<br>
      &#128274; каждый видит только своё<br>
      &#128200; беседа = два файла + UI
    </div>
  </div>

</aside>

<!-- ══ MAIN ═════════════════════════════════════════════════ -->
<main class="main">
  <!-- Chat header -->
  <div class="chat-head">
    <div class="chat-head-left">
      <div class="av big" id="chatAv" style="display:none"></div>
      <div>
        <h2 id="chatTitle">Войдите через Яндекс</h2>
        <p id="chatSub">Настройте подключение в боковой панели</p>
      </div>
    </div>
    <div class="pill" id="pollPill">poll: off</div>
  </div>

  <!-- Messages -->
  <div class="messages" id="msgBox">
    <div class="empty-chat" id="emptyChat">
      <svg width="42" height="42" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="1.1">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <p>Выберите контакт или добавьте нового собеседника</p>
    </div>
  </div>

  <!-- Compose -->
  <div class="compose">
    <div class="compose-row">
      <button class="attach-btn" id="attachBtn" disabled title="Прикрепить файл">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
        </svg>
      </button>
      <input type="file" id="fileInput" style="display:none" accept="*/*">
      <textarea id="msgIn" placeholder="Сообщение..." disabled></textarea>
      <button class="send-btn" id="sendBtn" disabled>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2.2"
             stroke-linecap="round" stroke-linejoin="round">
          <line x1="22" y1="2" x2="11" y2="13"/>
          <polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </svg>
      </button>
    </div>
    <div class="upload-progress" id="uploadProgress">
      <span id="uploadLabel">Загрузка…</span>
      <div class="prog-bar"><div class="prog-fill" id="progFill"></div></div>
    </div>
    <div class="compose-foot">
      
      <span>Enter — отправить &nbsp;·&nbsp; Shift+Enter — перенос</span>
    </div>
  </div>

  <!-- Status bar -->
  <div class="statusbar">
    <div class="dot off" id="dot"></div>
    <span class="stxt" id="stBar">не подключено</span>
  </div>
</main>

</div><!-- .app -->

<script>
/* ── utils ────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
function esc(x){ return String(x).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function pad(n){ return String(n).padStart(2,'0'); }
function fmtT(ts){ const d=new Date(ts); return pad(d.getHours())+':'+pad(d.getMinutes()); }
function fmtD(ts){ return new Date(ts).toLocaleDateString('ru-RU',{day:'numeric',month:'long'}); }

function toast(msg, dur=2700){
  let t=$('_toast');
  if(!t){
    t=document.createElement('div'); t.id='_toast';
    t.style.cssText='position:fixed;bottom:36px;left:50%;'
      +'transform:translateX(-50%) translateY(6px);'
      +'background:var(--s1);border:1px solid var(--bd);border-radius:999px;'
      +'padding:7px 18px;font-size:13px;box-shadow:var(--sh2);z-index:99;'
      +'transition:opacity .18s,transform .18s;opacity:0;pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent=msg;
  t.style.opacity='1'; t.style.transform='translateX(-50%) translateY(0)';
  clearTimeout(t._t);
  t._t=setTimeout(()=>{
    t.style.opacity='0'; t.style.transform='translateX(-50%) translateY(6px)';
  }, dur);
}

// ── scratch sound (Web Audio API, без файлов) ─────────────
const _ac=(()=>{try{return new(window.AudioContext||window.webkitAudioContext)()}catch(e){return null}})();
function playRecv(){
  if(!_ac) return;
  try{
    const buf=_ac.createBuffer(1,_ac.sampleRate*0.18,_ac.sampleRate);
    const d=buf.getChannelData(0);
    for(let i=0;i<d.length;i++){
      const t=i/_ac.sampleRate;
      // скребущий шум: белый шум × убывающая огибающая × лёгкая модуляция
      d[i]=(Math.random()*2-1)*Math.exp(-t*28)*(1+0.4*Math.sin(2*Math.PI*180*t));
    }
    const src=_ac.createBufferSource();
    src.buffer=buf;
    const g=_ac.createGain(); g.gain.value=0.22;
    src.connect(g); g.connect(_ac.destination);
    src.start();
  }catch(e){}
}

async function api(path, data={}){
  const r = await fetch(path,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data)});
  const j = await r.json();
  if(!j.ok && j.error) throw new Error(j.error);
  return j;
}

/* ── state ────────────────────────────────────────────────── */
let username='', contacts=[], activeId='', rendered=new Set(), lastDay='';

/* ── E2E Encryption (AES-GCM + PBKDF2) ─────────────────────── */
const cryptoKeys = {};

async function deriveKey(password) {
  const enc = new TextEncoder();
  const raw = await crypto.subtle.importKey(
    'raw', enc.encode(password), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name:'PBKDF2', salt:enc.encode('YaChatV2-e2e-v1'),
      iterations:120000, hash:'SHA-256' },
    raw, { name:'AES-GCM', length:256 },
    false, ['encrypt','decrypt']);
}

async function encryptText(key, plaintext) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt(
    {name:'AES-GCM',iv}, key, new TextEncoder().encode(plaintext));
  const b64 = u8 => btoa(String.fromCharCode(...u8));
  return {v:1, alg:'AES-GCM', iv:b64(iv), ct:b64(new Uint8Array(ct))};
}

async function decryptPayload(key, payload) {
  if (!payload) return null;
  if (payload.enc_stub==='plain') return payload.text ?? '';
  if (payload.v!==1 || payload.alg!=='AES-GCM') return null;
  try {
    const fb = s => Uint8Array.from(atob(s), c=>c.charCodeAt(0));
    const dec = await crypto.subtle.decrypt(
      {name:'AES-GCM', iv:fb(payload.iv)}, key, fb(payload.ct));
    return new TextDecoder().decode(dec);
  } catch { return null; }
}

async function processMessages(msgs, cid) {
  const entry = cryptoKeys[cid || activeId];
  const key   = entry ? entry.key : null;
  window._lastMessages = msgs;
  const out = [];
  for (const m of msgs) {
    const r = Object.assign({}, m);
    if (r.kind === 'text' && r.payload) {
      if (r.payload.enc_stub === 'plain') {
        r.text = r.payload.text || '';
        r.encBadge = '';
      } else if (key) {
        const dec = await decryptPayload(key, r.payload);
        if (dec !== null) { r.text = dec;                  r.encBadge = '🔒'; }
        else              { r.text = '🔓 [неверный ключ]'; r.encBadge = '🔓'; }
      } else {
        r.text = '🔒 [зашифровано]'; r.encBadge = '🔒';
      }
    }
    out.push(r);
  }
  return out;
}

function updateEncBadge(cid) {
  const locked = !!cryptoKeys[cid];
  // Все кнопки с data-enc-cid
  document.querySelectorAll('[data-enc-cid="'+cid+'"]')
    .forEach(btn => btn.textContent = locked ? '🔒' : '🔓');
  // chatSub
  if (cid === activeId) {
    if ($('chatSub'))
      $('chatSub').textContent = 'поллинг каждые 8с · '
        + (locked ? 'AES-256-GCM 🔒' : 'plain 🔓');
    if ($('encStatus')) {
      $('encStatus').textContent = locked ? '🔒 AES-256-GCM' : '🔓 plain';
      $('encStatus').className   = locked ? 'ok' : 'wa';
    }
  }
}

function showKeyDialog(cid, contactName) {
  const existing = cryptoKeys[cid];
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);'
    + 'z-index:9999;display:flex;align-items:center;justify-content:center;';
  const box = document.createElement('div');
  box.style.cssText = 'background:var(--bg);border:1px solid var(--border);'
    + 'border-radius:10px;padding:20px;width:320px;'
    + 'display:flex;flex-direction:column;gap:10px;';
  box.innerHTML =
    '<div style="font-weight:600;font-size:14px">🔒 Ключ шифрования</div>'+
    '<div style="font-size:12px;color:var(--muted)">Собеседник: <b>'+esc(contactName)+'</b><br>'+
    'Оба участника должны ввести <b>одинаковый</b> пароль.</div>'+
    '<input id="keyPassIn" type="password" class="inp" '
      + 'placeholder="Общий пароль…" autocomplete="new-password">'+
    (existing
      ? '<div style="font-size:11px;color:var(--muted)">Ключ установлен — введите новый или оставьте пустым для удаления</div>'
      : '') +
    '<div style="display:flex;gap:8px">'+
      '<button id="keyOkBtn" class="btn primary sm">Применить</button>'+
      (existing ? '<button id="keyDelBtn" class="btn sm" style="color:var(--err,#c33)">Сбросить</button>' : '')+
      '<button id="keyCancelBtn" class="btn sm">Отмена</button>'+
    '</div>';
  modal.appendChild(box);
  document.body.appendChild(modal);
  const inp = box.querySelector('#keyPassIn');
  inp.focus();
  const close = () => modal.remove();
  box.querySelector('#keyCancelBtn').onclick = close;
  modal.addEventListener('click', e => { if (e.target===modal) close(); });
  if (existing) {
    box.querySelector('#keyDelBtn').onclick = () => {
      delete cryptoKeys[cid]; updateEncBadge(cid);
      toast('Ключ удалён — переписка открыта'); close();
    };
  }
  box.querySelector('#keyOkBtn').onclick = async () => {
    const pw = inp.value.trim();
    if (!pw) {
      delete cryptoKeys[cid]; updateEncBadge(cid);
      toast('Ключ удалён'); close(); return;
    }
    toast('Генерирую ключ…');
    try {
      cryptoKeys[cid] = { key: await deriveKey(pw) };
      updateEncBadge(cid);
      toast('🔒 Ключ установлен — следующие сообщения зашифрованы');
      if (cid === activeId) {
        const msgs = await processMessages(window._lastMessages||[], cid);
        renderAll(msgs);
      }
      close();
    } catch(e) { toast('Ошибка: '+e.message); }
  };
  inp.addEventListener('keydown', e => {
    if (e.key==='Enter') box.querySelector('#keyOkBtn').click();
  });
}

let pollTimer=null;

/* ── OAuth ────────────────────────────────────────────────── */
$('oauthBtn').onclick = ()=>{
  const cid   = $('clientIdIn').value.trim();
  const uname = $('usernameIn').value.trim();
  const root  = $('rootIn').value.trim() || 'disk:/YaChatV2';
  if(!cid)   { toast('Введите ClientID'); return; }
  if(!uname) { toast('Введите имя');      return; }
  window.location.href = '/auth/start?client_id='+encodeURIComponent(cid)
    +'&username='+encodeURIComponent(uname)
    +'&root='+encodeURIComponent(root);
};

$('tokenManualBtn').onclick = async ()=>{
  const token = $('tokenManual').value.trim();
  const uname = $('usernameIn').value.trim();
  const root  = $('rootIn').value.trim() || 'disk:/YaChatV2';
  if(!token||!uname){ toast('Нужны токен и имя'); return; }
  try{
    await api('/api/token_manual',{token,username:uname,root});
    onAuthed(uname,root);
  }catch(e){ toast('Ошибка: '+e.message); }
};

function onAuthed(uname, root){
  username = uname;
  $('authForm').style.display  = 'none';
  $('authBadge').style.display = 'block';
  $('authAv').textContent      = uname[0].toUpperCase();
  $('authName').textContent    = uname;
  $('authRoot').textContent    = root;
  $('s_setup').style.display   = 'flex';
  $('s_status').style.display  = 'flex';
  $('stAuth').textContent = '✓ '+uname; $('stAuth').className='ok';
  $('chatTitle').textContent = 'Привет, '+uname;
  $('chatSub').textContent   = 'Нажмите «Проверить / создать структуру»';
  $('dot').className = 'dot warn';
  $('stBar').textContent = 'авторизован · '+uname;
}

/* ── Setup disk ───────────────────────────────────────────── */
$('setupBtn').onclick = async ()=>{
  $('setupBtn').disabled=true; $('setupBtn').textContent='Проверяю...';
  try{
    const j = await api('/api/setup');
    const log = $('setupLog');
    log.innerHTML = j.log.map(l=>'• '+esc(l)).join('<br>');
    log.style.display='block';
    $('s_setup').style.display     = 'none';
    $('s_contacts').style.display  = 'flex';
    $('s_mylinks').style.display   = 'none';   // покажем только когда будет ссылка
    $('addContactShow').style.display = 'flex'; // кнопка сразу видна
    $('stDisk').textContent='✓ OK'; $('stDisk').className='ok';
    $('stCnt').textContent = j.contacts.length;
    $('dot').className='dot on';
    $('stBar').textContent = 'Диск ОК · '+username;
    if(j.my_card_url) showMyCard(j.my_card_url);
    if(j.contacts && j.contacts.length){
      contacts = j.contacts;
      renderContacts();
      $('addForm').style.display='none';
      $('addContactShow').style.display='flex';
      $('chatSub').textContent = 'Выберите контакт слева';
      toast('Загружено контактов: '+j.contacts.length);
      startPoll();
    } else {
      $('addForm').style.display='none';
      $('addContactShow').style.display='flex';
      $('chatSub').textContent = 'Добавьте первый контакт — нажмите кнопку ниже';
      toast('Структура готова. Нажмите «Добавить контакт» — получите вашу ссылку.');
    }
  }catch(e){
    toast('Ошибка: '+e.message);
    $('setupBtn').disabled=false;
    $('setupBtn').textContent='Проверить / создать структуру →';
    return;
  }
};

/* ── Contacts UI ──────────────────────────────────────────── */
let pendingCid = '';

// Шаг 1: нажали "Добавить контакт" → создаём папку, получаем ссылку
$('addContactShow').onclick = async ()=>{
  $('addContactShow').disabled = true;
  $('addContactShow').textContent = '...';
  try {
    const j = await api('/api/contacts/new', {});
    if(!j.ok){ toast('Ошибка: '+(j.error||'?')); return; }
    pendingCid = j.cid;
    showMyCard(j.chat_url);
    $('addForm').style.display = 'flex';
    $('addContactShow').style.display = 'none';
    if($('contactNameIn')) $('contactNameIn').value = '';
    $('peerUrlIn').value = '';
    $('peerUrlIn').focus();
    toast('Ссылка готова — скопируйте и передайте собеседнику, затем введите его ссылку');
  } catch(e){ toast('Ошибка: '+e.message); }
  $('addContactShow').disabled = false;
  $('addContactShow').innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg> Добавить контакт';
};

// Скрыть форму добавления
$('addContactToggle').onclick = ()=>{
  $('addForm').style.display = 'none';
  if(contacts.length) $('addContactShow').style.display = 'flex';
  pendingCid = '';
};

// Шаг 2: пользователь ввёл ссылку собеседника → сохраняем контакт
$('addPeerBtn').onclick = async ()=>{
  const url = $('peerUrlIn').value.trim();
  if(!url){ toast('Вставьте ссылку собеседника'); return; }
  if(!pendingCid){ toast('Сначала нажмите «Добавить контакт»'); return; }
  $('addPeerBtn').disabled = true; $('addPeerBtn').textContent = '...';
  try {
    const nameVal = ($('contactNameIn') ? $('contactNameIn').value.trim() : '');
    const j = await api('/api/contacts/add', {cid: pendingCid, peer_url: url, name: nameVal});
    if(!j.ok){ toast('Ошибка: '+(j.error||'?')); return; }
    pendingCid = '';
    if(j.duplicate){
      toast('Уже в списке — переключаю на '+j.contact.name);
    } else {
      contacts.push(j.contact);
      toast('Добавлен: '+j.contact.name+(j.loaded?' · сообщений: '+j.loaded:''));
    }
    activeId = j.contact.id;
    if(j.my_card_url) showMyCard(j.my_card_url, j.contact ? j.contact.name : '');
    $('peerUrlIn').value = '';
    if($('contactNameIn')) $('contactNameIn').value = '';
    $('addForm').style.display = 'none';
    $('addContactShow').style.display = 'flex';
    renderContacts();
    updateChatHeader(j.contact);
    processMessages(j.messages||[], activeId).then(msgs => renderAll(msgs));
    $('stCnt').textContent = contacts.length;
    startPoll();
    enableCompose(true);
  } catch(e){ toast('Ошибка: '+e.message); }
  $('addPeerBtn').disabled = false; $('addPeerBtn').textContent = 'Добавить';
};

function renderContacts(){
  const list=$('contactList');
  list.querySelectorAll('.c-item').forEach(n=>n.remove());
  const empty=$('cEmpty');
  if(!contacts.length){ empty.style.display='block'; return; }
  empty.style.display='none';
  contacts.forEach(c=>{
    const el=document.createElement('div');
    el.className='c-item'+(c.id===activeId?' active':'');
    el.dataset.id=c.id;
    const unread = c.unread||0;
    el.innerHTML=
      `<div class="c-av">${esc(c.name[0]||'?').toUpperCase()}</div>`+
      `<div class="c-info">`+
        `<div class="c-name">${esc(c.name)}</div>`+
        `<div class="c-sub">${esc(c.peer_url.slice(0,38)+'...')}</div>`+
      `</div>`+
      `<button class="c-enc" data-enc-cid="${esc(c.id)}" title="Шифрование"
        style="background:none;border:none;cursor:pointer;font-size:13px;padding:2px 4px;opacity:.6"
        >${cryptoKeys[c.id]?'🔒':'🔓'}</button>`+
      (unread?`<div class="c-unread">${unread}</div>`:'')+
      `<button class="c-del" data-del="${esc(c.id)}" title="Удалить">&#10005;</button>`;
    el.addEventListener('click', e=>{
      if(e.target.closest('.c-del')) return;
      selectContact(c.id);
    });
    el.querySelector('.c-del').addEventListener('click', e=>{
      e.stopPropagation();
      deleteContact(c.id, c.name);
    });
    el.querySelector('.c-enc').addEventListener('click', e=>{
      e.stopPropagation();
      showKeyDialog(c.id, c.name);
    });
    list.appendChild(el);
  });
}

async function selectContact(cid){
  try{
    const j = await api('/api/contacts/select',{id:cid});
    activeId = cid;
    // сбрасываем unread у выбранного
    const ci = contacts.find(c=>c.id===cid);
    if(ci) ci.unread=0;
    renderContacts();
    updateChatHeader(j.contact);
    processMessages(j.messages||[], cid).then(msgs => renderAll(msgs));
    enableCompose(true);
    startPoll();
    if(j.my_card_url) showMyCard(j.my_card_url, j.contact.name);
    updateEncBadge(cid);
    toast('Беседа с '+j.contact.name+(j.loaded?' · новых: '+j.loaded:''));
  }catch(e){ toast('Ошибка: '+e.message); }
}

async function deleteContact(cid, cname){
  if(!confirm('Удалить контакт «'+cname+'»?')) return;
  try{
    await api('/api/contacts/delete',{id:cid});
    contacts = contacts.filter(c=>c.id!==cid);
    if(activeId===cid){
      activeId='';
      $('chatTitle').textContent='Выберите контакт';
      $('chatSub').textContent='';
      $('chatAv').style.display='none';
      enableCompose(false);
      clearMessages();
      if(pollTimer){ clearTimeout(pollTimer); pollTimer=null; }
      $('pollPill').textContent='poll: off';
      $('stPoll').textContent='off'; $('stPoll').className='er';
    }
    renderContacts();
    updateMyLinks();
    $('stCnt').textContent=contacts.length;
    toast('Удалён: '+cname);
  }catch(e){ toast('Ошибка: '+e.message); }
}

function updateChatHeader(c){
  $('chatTitle').textContent = 'Беседа с '+c.name;
  const av=$('chatAv');
  av.textContent=c.name[0].toUpperCase();
  av.style.display='grid';
  // Кнопка шифрования в шапке
  let encBtn = $('chatEncBtn');
  if(!encBtn){
    encBtn = document.createElement('button');
    encBtn.id = 'chatEncBtn';
    encBtn.title = 'Управление шифрованием';
    encBtn.style.cssText = 'background:none;border:none;cursor:pointer;'
      + 'font-size:17px;padding:2px 5px;border-radius:6px;'
      + 'opacity:.65;transition:opacity .15s;margin-left:4px';
    encBtn.onmouseover = ()=>encBtn.style.opacity='1';
    encBtn.onmouseout  = ()=>encBtn.style.opacity='.65';
    const title = $('chatTitle');
    if(title && title.parentNode) title.parentNode.appendChild(encBtn);
  }
  encBtn.dataset.encCid = c.id;
  encBtn.textContent = cryptoKeys[c.id] ? '🔒' : '🔓';
  encBtn.onclick = () => showKeyDialog(c.id, c.name||'?');
  updateEncBadge(c.id);
}

function updateMyLinks(){}  // no-op, заменена на showMyCard

function showMyCard(url, forName){
  const sec=$('s_mylinks');
  const box=$('myCardLinkBox');
  if(sec) sec.style.display='flex';
  if(!url){
    box.innerHTML='<div class="c-empty">Ссылка не получена</div>';
    return;
  }
  const safeUrl = url.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  const label = forName
    ? '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">Ссылка для: <b>'+esc(forName)+'</b></div>'
    : '';
  box.innerHTML=
    label+
    '<div class="link-box">'+
      '<div class="link-val">'+safeUrl+'</div>'+
      '<button class="copy-btn" data-url="'+safeUrl+'">Копир.</button>'+
    '</div>';
  box.querySelector('.copy-btn').onclick=function(){
    navigator.clipboard.writeText(this.dataset.url)
      .then(()=>toast('\u2713 Ссылка скопирована!'))
      .catch(()=>toast(this.dataset.url,5000));
  };
}

/* ── Send ─────────────────────────────────────────────────── */
async function doSend(){
  const text=$('msgIn').value.trim();
  if(!text) return;
  $('sendBtn').disabled=true;
  try{
    const entry = cryptoKeys[activeId];
    let sendData, displayText = text;
    if (entry && entry.key) {
      sendData = { payload: await encryptText(entry.key, text) };
    } else {
      sendData = { text };
    }
    const j = await api('/api/send', sendData);
    // Всегда показываем оригинальный текст для своих сообщений
    const m = Object.assign({}, j.message, {text, encBadge: entry ? '🔒' : ''});
    addMsg(m);
    $('msgIn').value=''; $('msgIn').style.height='auto';
  }catch(e){ toast('Ошибка: '+e.message); }
  $('sendBtn').disabled=false; $('msgIn').focus();
}
$('sendBtn').onclick=doSend;
$('msgIn').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); doSend(); }
});
$('msgIn').addEventListener('input',function(){
  this.style.height='auto';
  this.style.height=Math.min(this.scrollHeight,120)+'px';
});

/* ── Poll ─────────────────────────────────────────────────── */
async function poll(){
  try{
    const j=await api('/api/poll');
    if(j.new && j.new.length){
      processMessages(j.new, activeId).then(msgs => msgs.forEach(addMsg));
    }
    // обновляем счётчики unread
    if(j.contacts){
      j.contacts.forEach(jc=>{
        const ci=contacts.find(c=>c.id===jc.id);
        if(ci){ ci.unread=jc.unread; }
      });
      renderContacts();
      $('stCnt').textContent=contacts.length;
    }
    const t=new Date().toLocaleTimeString('ru-RU');
    $('pollPill').textContent='poll: '+t;
    $('stPoll').textContent='✓ '+t; $('stPoll').className='ok';
    $('stBar').textContent='поллинг · '+t;
    $('dot').className='dot on';
    if(j.warn) $('stBar').textContent='⚠ '+j.warn;
  }catch(e){ $('stBar').textContent='poll err: '+e.message; }
  pollTimer=setTimeout(poll,8000);
}
function startPoll(){ if(pollTimer) clearTimeout(pollTimer); poll(); }

/* ── Render messages ──────────────────────────────────────── */
function clearMessages(){
  $('msgBox').querySelectorAll('.msg,.day-sep').forEach(n=>n.remove());
  rendered.clear(); lastDay='';
  $('emptyChat').style.display='flex';
}
function renderAll(msgs){
  clearMessages();
  if(!msgs||!msgs.length) return;
  $('emptyChat').style.display='none';
  [...msgs].sort((a,b)=>a.ts-b.ts).forEach(m=>{
    const d=fmtD(m.ts);
    if(d!==lastDay){
      lastDay=d;
      const sep=document.createElement('div');
      sep.className='day-sep'; sep.textContent=d;
      $('msgBox').appendChild(sep);
    }
    if(!rendered.has(m.id)){ rendered.add(m.id); $('msgBox').appendChild(buildMsg(m)); }
  });
  $('msgBox').scrollTop=$('msgBox').scrollHeight;
}
function addMsg(m){
  if(rendered.has(m.id)) return;
  rendered.add(m.id);
  if(!m.own && m.from !== username) playRecv();
  $('emptyChat').style.display='none';
  const d=fmtD(m.ts);
  if(d!==lastDay){
    lastDay=d;
    const sep=document.createElement('div');
    sep.className='day-sep'; sep.textContent=d;
    $('msgBox').appendChild(sep);
  }
  $('msgBox').appendChild(buildMsg(m));
  $('msgBox').scrollTop=$('msgBox').scrollHeight;
}
function formatSize(b){
  if(b<1024)return b+'B';
  if(b<1048576)return (b/1024).toFixed(1)+'KB';
  return (b/1048576).toFixed(1)+'MB';
}

// Lazy-load observer для картинок
const imgObserver=new IntersectionObserver(entries=>{
  entries.forEach(entry=>{
    if(!entry.isIntersecting)return;
    const ph=entry.target;
    const path=ph.dataset.path;
    if(!path){imgObserver.unobserve(ph);return;}
    imgObserver.unobserve(ph);
    const wrap=ph.closest('.bubble-media');
    if(!wrap)return;
    const caption=wrap.querySelector('.media-caption');
    const img=document.createElement('img');
    img.className='media-img loading';
    img.alt=ph.dataset.name||'';
    img.style.cssText='max-width:260px;display:block';
    img.onload=()=>img.classList.remove('loading');
    img.onerror=()=>{
      img.remove();
      ph.textContent='❌ не загрузить';
      wrap.insertBefore(ph,caption);
    };
    wrap.insertBefore(img,caption);
    ph.remove();
    img.src='/api/proxy-image?path='+encodeURIComponent(path);
  });
},{rootMargin:'200px'});

function buildMsg(m){
  const own=m.own||(m.from===username);
  const el=document.createElement('div');
  el.className='msg '+(own?'own':'other');
  el.dataset.id=m.id;
  const fromH=!own?`<div class="msg-from">${esc(m.from)}</div>`:'';
  const timeH=`<div class="msg-time">${fmtT(m.ts)}</div>`;

  if(m.kind==='image'){
    el.innerHTML=fromH+
      `<div class="bubble-media">
        <div class="media-placeholder" data-path="${esc(m.path||'')}" data-name="${esc(m.name||'')}">
          <span>⏳ загрузка…</span>
        </div>
        <div class="media-caption">${esc(m.name||'')}</div>
      </div>`+timeH;
    const ph=el.querySelector('.media-placeholder');
    if(ph)imgObserver.observe(ph);

  }else if(m.kind==='file'){
    el.innerHTML=fromH+
      `<div class="file-bubble">
        <div class="file-icon">📄</div>
        <div class="file-info">
          <div class="file-name">${esc(m.name||'файл')}</div>
          <div class="file-size">${formatSize(m.size||0)}</div>
        </div>
        <span class="file-dl" data-path="${esc(m.path||'')}">⬇</span>
      </div>`+timeH;
    el.querySelector('.file-dl').onclick=function(){
      const p=this.dataset.path;
      if(!p)return;
      api('/api/download-link',{path:p}).then(j=>{
        const a=document.createElement('a');
        a.href=j.url;a.target='_blank';a.download=m.name||'file';
        document.body.appendChild(a);a.click();setTimeout(()=>a.remove(),100);
      }).catch(e=>toast('Ошибка: '+e.message));
    };

  }else{
    const badge = m.encBadge
      ? `<span style="font-size:10px;opacity:.5;margin-left:4px;vertical-align:middle">${m.encBadge}</span>`
      : '';
    el.innerHTML=fromH+
      `<div class="bubble">${esc(m.text||'')}${badge}</div>`+timeH;
  }
  return el;
}

/* ── Misc ─────────────────────────────────────────────────── */
function enableCompose(on){
  $('msgIn').disabled=!on;
  $('sendBtn').disabled=!on;
  $('attachBtn').disabled=!on;
}

// ── Attach / Upload ────────────────────────────────────────
$('attachBtn').onclick=()=>$('fileInput').click();

$('fileInput').onchange=async function(){
  const file=this.files[0];
  if(!file)return;
  this.value='';
  const prog=$('uploadProgress');
  const fill=$('progFill');
  const lbl=$('uploadLabel');
  prog.classList.add('active');
  lbl.textContent='Загрузка: '+file.name;
  fill.style.width='5%';
  try{
    const fd = new FormData();
    fd.append('file', file, file.name);
    // XHR вместо fetch — гарантирует Content-Length и даёт прогресс
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload');
      xhr.upload.onprogress = e => {
        if(e.lengthComputable)
          fill.style.width = Math.round(10 + e.loaded/e.total*80) + '%';
      };
      xhr.onload = () => {
        fill.style.width = '100%';
        try {
          const j = JSON.parse(xhr.responseText);
          if(!j.ok) reject(new Error(j.error || 'upload error'));
          else { addMsg(j.message); toast('✓ '+file.name+' отправлен'); resolve(); }
        } catch(e) { reject(new Error('bad response')); }
      };
      xhr.onerror = () => reject(new Error('network error'));
      xhr.send(fd);
    });
  }catch(e){
    toast('Ошибка загрузки: '+e.message);
  }finally{
    setTimeout(()=>{prog.classList.remove('active');fill.style.width='0%';},600);
  }
};

$('themeBtn').onclick=()=>{
  const r=document.documentElement;
  r.setAttribute('data-theme', r.getAttribute('data-theme')==='dark'?'light':'dark');
};

/* ── Init: restore state after OAuth redirect ─────────────── */
(async()=>{
  $('rootIn').value='disk:/YaChatV2';
  try{
    const j=await api('/api/state');
    if(j.authed && j.username){
      onAuthed(j.username, j.root);
      if(j.contacts && j.contacts.length){
        contacts=j.contacts;
        $('s_contacts').style.display='flex';
        $('stDisk').textContent='✓ OK'; $('stDisk').className='ok';
        $('stCnt').textContent=contacts.length;
        renderContacts();
        if(j.my_card_url) showMyCard(j.my_card_url);
        else $('addContactShow').style.display='flex';
        $('s_setup').style.display='flex';
        if(j.active_id){
          activeId=j.active_id;
          renderContacts();
          if(j.active_name){
            updateChatHeader({name:j.active_name, id:j.active_id});
            enableCompose(true);
          }
          if(j.messages&&j.messages.length){
            processMessages(j.messages, j.active_id).then(msgs => {
              renderAll(msgs);
              if(j.active_id) updateEncBadge(j.active_id);
            });
          }
          startPoll();
        }
      }
    }
    if(new URLSearchParams(window.location.search).get('authed')==='1'){
      history.replaceState(null,'','/');
      toast('Авторизация успешна! Нажмите «Проверить / создать структуру»');
    }
  }catch(e){}
})();
</script>
</body></html>"""


if __name__ == '__main__':
    print(f'YaDisk Chat v6  →  http://{HOST}:{PORT}')
    print('Ctrl+C для остановки.')
    try:
        HTTPServer((HOST, PORT), H).serve_forever()
    except KeyboardInterrupt:
        print('Остановлен.')
