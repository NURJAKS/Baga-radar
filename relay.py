#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Баға Радар — локальный сервис.

Делает три вещи:
  1. раздаёт файлы проекта (интерфейс открывается по http://localhost:8787/);
  2. отдаёт официальные курсы валют Национального Банка РК — браузер не может
     обратиться к nationalbank.kz напрямую (нет заголовков CORS), поэтому запрос
     идёт через этот сервис, ответ кэшируется на 30 минут;
  3. принимает обращения граждан с фотографией ценника и передаёт их в Telegram.

Токен бота в коде не хранится. Создайте файл .env рядом со скриптом:
    TG_TOKEN=123456:AA...        # токен бота от @BotFather
    TG_CHAT_ID=-1001234567890    # чат или канал, куда поступают обращения
Файл .env в репозиторий не попадает (внесён в .gitignore).

Запуск:   python3 relay.py                 # порт 8787
          python3 relay.py --port 9000
          python3 relay.py --chat-id       # показать chat_id тех, кто писал боту
          python3 relay.py --chat-id --wait  # дождаться сообщения боту и записать
                                             # chat_id в .env (разовая настройка)
"""
import datetime, json, os, re, ssl, sys, threading, time, base64, urllib.request, urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
NB_URL = 'https://nationalbank.kz/rss/rates_all.xml'
NB_DAY = 'https://nationalbank.kz/rss/get_rates.cfm?fdate=%s'   # курсы на указанную дату
HIST = os.path.join(HERE, 'data', 'rates-history.json')
HIST_DAYS = 38       # глубина истории: 30 дней для месячной динамики плюс запас,
                     # чтобы сравнение попадало на дату с опубликованным курсом
SHOW = ['USD', 'EUR', 'RUB', 'CNY']          # курсы, выводимые в интерфейсе
COMPLAINTS = os.path.join(HERE, 'data', 'complaints.jsonl')
MAX_PHOTO = 12 * 1024 * 1024                  # предел размера фотографии, байт


# ------------------------------------------------------------------ настройки
def env():
    """Чтение .env; переменные окружения имеют приоритет."""
    cfg = {}
    path = os.path.join(HERE, '.env')
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ('TG_TOKEN', 'TG_CHAT_ID'):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def tg(method, payload=None, files=None):
    """Вызов Bot API. Токен берётся из .env и в ответах не повторяется."""
    cfg = env()
    token = cfg.get('TG_TOKEN')
    if not token:
        raise RuntimeError('не задан TG_TOKEN в .env')
    url = 'https://api.telegram.org/bot%s/%s' % (token, method)
    if files:
        boundary = '----bagaradar%d' % int(time.time() * 1000)
        body = b''
        for k, v in (payload or {}).items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                     % (boundary, k, v)).encode('utf-8')
        for k, (fname, data, ctype) in files.items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                     'Content-Type: %s\r\n\r\n' % (boundary, k, fname, ctype)).encode('utf-8')
            body += data + b'\r\n'
        body += ('--%s--\r\n' % boundary).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers={
            'Content-Type': 'multipart/form-data; boundary=%s' % boundary})
    else:
        req = urllib.request.Request(url, data=json.dumps(payload or {}).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:300]
        # описание ошибки может содержать адрес вызова вместе с токеном — убираем
        raise RuntimeError('Telegram API %s: %s' % (e.code, re.sub(r'bot\d+:[\w-]+', 'bot***', detail)))


# ------------------------------------------------------------------ курсы НБ РК
_rates = {'t': 0, 'v': None}
_hist_lock = threading.Lock()

def _parse(xml):
    """Разбор ленты курсов: код валюты → значение и дневное изменение."""
    out, date = {}, ''
    for it in re.findall(r'<item>(.*?)</item>', xml, re.S):
        g = lambda tag: (re.search(r'<%s>(.*?)</%s>' % (tag, tag), it, re.S) or [None, ''])[1].strip()
        code = g('title')
        if code not in SHOW:
            continue
        date = date or g('pubDate')
        out[code] = {'v': float(g('description') or 0),
                     'change': float(g('change') or 0), 'dir': g('index') or ''}
    return date, out


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'BagaRadar/1.0'})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return r.read().decode('utf-8', 'replace')


def history(days=HIST_DAYS):
    """Курсы за последние дни. Опубликованные курсы не пересматриваются,
       поэтому однажды полученная дата хранится в data/rates-history.json
       и повторно не запрашивается."""
    with _hist_lock:
        store = {}
        if os.path.exists(HIST):
            try: store = json.load(open(HIST, encoding='utf-8'))
            except Exception: store = {}
        today = datetime.date.today()
        need = [(today - datetime.timedelta(days=i)).strftime('%d.%m.%Y') for i in range(days)]
        added = 0
        for d in need:
            if d in store:
                continue
            try:
                _, vals = _parse(_get(NB_DAY % d))
                if vals:
                    store[d] = {k: v['v'] for k, v in vals.items()}
                    added += 1
            except Exception:
                continue
        if added:
            os.makedirs(os.path.dirname(HIST), exist_ok=True)
            json.dump(store, open(HIST, 'w', encoding='utf-8'), ensure_ascii=False)
        return store


def _ago(store, code, days):
    """Курс, действовавший N дней назад (ближайшая доступная дата назад)."""
    d = datetime.date.today() - datetime.timedelta(days=days)
    for back in range(0, 6):
        k = (d - datetime.timedelta(days=back)).strftime('%d.%m.%Y')
        if k in store and code in store[k]:
            return store[k][code]
    return None


def rates():
    """Текущие курсы с динамикой за сутки, неделю и месяц. Кэш 30 минут."""
    if _rates['v'] and time.time() - _rates['t'] < 1800:
        return _rates['v']
    date, cur = _parse(_get(NB_URL))
    store = history()
    out = []
    for code in SHOW:
        if code not in cur:
            continue
        r = dict(cur[code]); r['code'] = code
        for days, key in ((7, 'w'), (30, 'm')):
            prev = _ago(store, code, days)
            r[key] = None if not prev else {'abs': r['v'] - prev, 'pct': (r['v'] / prev - 1) * 100}
        # ряд для графика: последние 14 дат по возрастанию
        ser = []
        for i in range(13, -1, -1):
            k = (datetime.date.today() - datetime.timedelta(days=i)).strftime('%d.%m.%Y')
            if k in store and code in store[k]:
                ser.append(round(store[k][code], 4))
        if len(ser) >= 2:
            r['series'] = ser
        out.append(r)
    val = {'date': date, 'rates': out, 'days': len(store),
           'source': 'Национальный Банк Республики Казахстан',
           'sourceUrl': 'https://nationalbank.kz/ru/exchangerates/ezhednevnye-oficialnye-rynochnye-kursy-valyut'}
    _rates['v'], _rates['t'] = val, time.time()
    return val


# ------------------------------------------------------------------ обращения
def complaint(body):
    """Регистрация обращения и передача его в Telegram."""
    cfg = env()
    chat = cfg.get('TG_CHAT_ID')
    rec = {
        'ts': time.strftime('%d.%m.%Y %H:%M'),
        'region': str(body.get('region', ''))[:120],
        'product': str(body.get('product', ''))[:160],
        'point': str(body.get('point', ''))[:160],
        'price': str(body.get('price', ''))[:32],
        'unit': str(body.get('unit', ''))[:16],
        'contact': str(body.get('contact', ''))[:120],
        'note': str(body.get('note', ''))[:600],
        'photo': bool(body.get('photo'))
    }
    text = ('Обращение по цене на СЗПТ\n'
            'Дата: %(ts)s\nРегион: %(region)s\nТовар: %(product)s\n'
            'Торговая точка: %(point)s\nЦена на ценнике: %(price)s %(unit)s\n' % rec)
    if rec['note']:    text += 'Пояснение: %s\n' % rec['note']
    if rec['contact']: text += 'Обратная связь: %s\n' % rec['contact']

    sent, err = False, None
    if not chat:
        err = 'не задан TG_CHAT_ID в .env'
    else:
        try:
            photo = body.get('photo')
            if photo:
                raw = base64.b64decode(re.sub(r'^data:[^,]+,', '', photo))
                if len(raw) > MAX_PHOTO:
                    raise RuntimeError('фотография больше %d МБ' % (MAX_PHOTO // 1048576))
                tg('sendPhoto', {'chat_id': chat, 'caption': text[:1024]},
                   {'photo': ('cennik.jpg', raw, 'image/jpeg')})
            else:
                tg('sendMessage', {'chat_id': chat, 'text': text})
            sent = True
        except Exception as e:
            err = str(e)

    rec['sent'] = sent
    if err: rec['error'] = err
    os.makedirs(os.path.dirname(COMPLAINTS), exist_ok=True)
    with open(COMPLAINTS, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    return rec


def complaints(limit=200):
    if not os.path.exists(COMPLAINTS):
        return []
    rows = [json.loads(l) for l in open(COMPLAINTS, encoding='utf-8') if l.strip()]
    return rows[-limit:][::-1]


# ------------------------------------------------------------------ сервер
class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        if '/api/' in (args[0] if args else ''):
            sys.stderr.write('  %s\n' % (fmt % args))

    def cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def js(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.cors(); self.end_headers(); self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()

    def do_GET(self):
        p = self.path.split('?')[0]
        if p == '/api/rates':
            try: self.js(rates())
            except Exception as e: self.js({'error': str(e)}, 502)
            return
        if p == '/api/status':
            cfg = env()
            self.js({'token': bool(cfg.get('TG_TOKEN')), 'chat': bool(cfg.get('TG_CHAT_ID')),
                     'complaints': len(complaints(10000))})
            return
        if p == '/api/complaints':
            self.js({'items': complaints()}); return
        if p == '/api/chat-id':
            try:
                upd = tg('getUpdates', {'limit': 20})
                seen = {}
                for u in upd.get('result', []):
                    m = u.get('message') or u.get('channel_post') or {}
                    c = m.get('chat') or {}
                    if c.get('id'):
                        seen[c['id']] = c.get('title') or ' '.join(
                            filter(None, [c.get('first_name'), c.get('last_name'), c.get('username')]))
                self.js({'chats': [{'id': k, 'name': v} for k, v in seen.items()]})
            except Exception as e:
                self.js({'error': str(e)}, 502)
            return
        if p == '/':
            self.send_response(302)
            self.send_header('Location', '/baga-radar-gov.html')
            self.end_headers(); return
        super().do_GET()

    def end_headers(self):
        # без кэша: правки в файлах должны быть видны сразу по F5
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        super().end_headers()

    def do_POST(self):
        if self.path.split('?')[0] != '/api/complaint':
            self.js({'error': 'неизвестный адрес'}, 404); return
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_PHOTO + 2 * 1024 * 1024:
            self.js({'error': 'слишком большой запрос'}, 413); return
        try:
            body = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            self.js({'error': 'некорректный запрос'}, 400); return
        try:
            self.js(complaint(body))
        except Exception as e:
            self.js({'error': str(e)}, 500)


def chats():
    """Кто писал боту: chat_id и наименование."""
    upd = tg('getUpdates', {'limit': 50})
    out, seen = [], set()
    for u in upd.get('result', []):
        m = u.get('message') or u.get('channel_post') or {}
        c = m.get('chat') or {}
        if c.get('id') and c['id'] not in seen:
            seen.add(c['id'])
            out.append((c['id'], c.get('title') or ' '.join(
                filter(None, [c.get('first_name'), c.get('last_name')])) or c.get('username') or ''))
    return out


def set_chat_id(chat_id):
    """Записать TG_CHAT_ID в .env, сохранив остальные строки."""
    path = os.path.join(HERE, '.env')
    lines, done = [], False
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            if line.strip().startswith('TG_CHAT_ID='):
                lines.append('TG_CHAT_ID=%s\n' % chat_id); done = True
            else:
                lines.append(line)
    if not done:
        lines.append('TG_CHAT_ID=%s\n' % chat_id)
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    os.chmod(path, 0o600)


def main():
    port = 8787
    if '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])
    if '--chat-id' in sys.argv:
        wait = '--wait' in sys.argv
        deadline = time.time() + (900 if wait else 0)
        while True:
            try:
                found = chats()
            except Exception as e:
                print('Ошибка:', e); return
            if found:
                for cid, name in found:
                    print('chat_id=%s  %s' % (cid, name))
                if wait:
                    set_chat_id(found[0][0])
                    print('\nЗаписано в .env: TG_CHAT_ID=%s' % found[0][0])
                    print('Передача обращений в Telegram настроена.')
                return
            if not wait:
                print('Боту ещё никто не писал. Откройте бота в Telegram, нажмите «Старт»\n'
                      'или напишите любое сообщение, затем повторите команду.')
                return
            if time.time() > deadline:
                print('Сообщение боту так и не поступило.'); return
            time.sleep(4)
    cfg = env()
    print('Баға Радар — локальный сервис')
    print('  интерфейс     http://localhost:%d/' % port)
    print('  курсы валют   /api/rates     — Национальный Банк РК, кэш 30 мин')
    print('  обращения     /api/complaint — %s' % (
        'передача в Telegram настроена' if cfg.get('TG_TOKEN') and cfg.get('TG_CHAT_ID')
        else 'НЕ настроена: заполните .env (TG_TOKEN, TG_CHAT_ID)'))
    threading.Thread(target=lambda: (history(), None), daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()


if __name__ == '__main__':
    main()
