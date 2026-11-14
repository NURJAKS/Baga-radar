#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Баға Радар — загрузчик реальных цен.

Источник: Бюро национальной статистики (БНС) АСПиР РК, форма Ц-101э (еженедельная),
публикация «Индекс цен и средние цены на социально-значимые продовольственные товары».
Страница:  https://stat.gov.kz/ru/industries/economy/prices/spreadsheets/

Что делает:
  1. находит на странице ссылку на последний недельный файл (xlsx);
  2. сохраняет его в data/szpt-ГГГГ-ММ-ДД.xlsx — архив накапливается неделя за неделей,
     из него потом строится реальная история для прогноза и бэктеста;
  3. парсит лист 5 (средние цены по городам) и листы 1–4 (индексы);
  4. сопоставляет города БНС с регионами приложения, товары БНС — с товарами приложения;
  5. пишет prices-real.json — его подхватывает baga-radar.html.

Запуск:  python3 fetch-prices.py                         # скачать выпуск, пересобрать json
         python3 fetch-prices.py --offline               # пересобрать из архива, без сети
         python3 fetch-prices.py --inject baga-radar-gov.html   # и вшить данные в интерфейс
"""
import json, re, sys, os, urllib.request, datetime, glob

BASE   = 'https://stat.gov.kz'
PAGE   = BASE + '/ru/industries/economy/prices/spreadsheets/'
HERE   = os.path.dirname(os.path.abspath(__file__))
ARCH   = os.path.join(HERE, 'data')
OUT    = os.path.join(HERE, 'prices-real.json')
UA     = 'Mozilla/5.0 (compatible; BagaRadar/1.0; prototype)'
# если страница недоступна — известный рабочий выпуск от 13.11.2026
FALLBACK_ID = 355096

# --- города БНС → регионы приложения (обследуются областные центры) ---
CITY2REG = {
    'Астана':'astana', 'Алматы':'almaty_c', 'Шымкент':'shymkent',
    'Актау':'mangystau', 'Актобе':'aktobe', 'Атырау':'atyrau',
    'Жезказган':'ulytau', 'Кокшетау':'akmola', 'Караганда':'karaganda',
    'Конаев':'almaty_o', 'Костанай':'kostanay', 'Кызылорда':'kyzylorda',
    'Уральск':'wko', 'Усть-Каменогорск':'vko', 'Павлодар':'pavlodar',
    'Петропавловск':'sko', 'Семей':'abay', 'Талдыкорган':'zhetysu',
    'Тараз':'zhambyl', 'Туркестан':'turkestan',
}
NATIONAL = 'По обследованным городам'

# --- перечень СЗПТ (31 позиция), наименования — по форме Ц-101э БНС ---
# (подстрока для поиска в файле, код, единица измерения)
SZPT = [
    ('рис шлифованный',                      'rice',          '₸/кг'),
    ('крупа гречневая',                      'buck',          '₸/кг'),
    ('мука пшеничная первого сорта',         'flour',         '₸/кг'),
    ('хлеб пшеничный из муки первого сорта', 'bread',         '₸/кг'),
    ('рожки',                                'pasta',         '₸/кг'),
    ('говядина с костями',                   'beef_bone',     '₸/кг'),
    ('говядина бескостная',                  'beef',          '₸/кг'),
    ('конина',                               'horse',         '₸/кг'),
    ('баранина',                             'lamb',          '₸/кг'),
    ('куры',                                 'chicken',       '₸/кг'),
    ('мясо кур',                             'chicken_parts', '₸/кг'),
    ('мясной фарш',                          'mince',         '₸/кг'),
    ('рыба свежая',                          'fish',          '₸/кг'),
    ('молоко (пастеризованное',              'milk',          '₸/л'),
    ('сыр твердый',                          'cheese',        '₸/кг'),
    ('творог',                               'curd',          '₸/кг'),
    ('сметана',                              'smetana',       '₸/кг'),
    ('кефир',                                'kefir',         '₸/л'),
    ('яйца',                                 'eggs',          '₸/10 шт'),
    ('масло подсолнечное',                   'oil',           '₸/л'),
    ('масло сливочное',                      'butter',        '₸/кг'),
    ('яблоки',                               'apple',         '₸/кг'),
    ('капуста белокочанная',                 'cabbage',       '₸/кг'),
    ('огурцы',                               'cucumber',      '₸/кг'),
    ('помидоры',                             'tomato',        '₸/кг'),
    ('лук репчатый',                         'onion',         '₸/кг'),
    ('морковь',                              'carrot',        '₸/кг'),
    ('картофель',                            'potato',        '₸/кг'),
    ('сахар-песок',                          'sugar',         '₸/кг'),
    ('соль',                                 'salt',          '₸/кг'),
    ('чай черный',                           'tea',           '₸/кг'),
]
PROD2ID = [(needle, pid) for needle, pid, _ in SZPT]
UNIT = {pid: u for _, pid, u in SZPT}

def log(*a): print(*a, file=sys.stderr)

def get(url, binary=True):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    return data if binary else data.decode('utf-8', 'replace')

def find_latest():
    """Ссылка на последний недельный выпуск СЗПТ + дата публикации."""
    try:
        html = get(PAGE, binary=False)
    except Exception as e:
        log('! страница недоступна (%s), беру известный выпуск %d' % (e, FALLBACK_ID))
        return '%s/api/iblock/element/%d/file/ru/' % (BASE, FALLBACK_ID), None
    # блок публикации: название ... дата ... ссылка на файл
    best = None
    for m in re.finditer(r'социально-значимые продоволь', html, re.I):
        tail = html[m.start():m.start() + 4000]
        d  = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', tail)
        el = re.search(r'/api/iblock/element/(\d+)/file/ru/', tail)
        if not (d and el):
            continue
        day = datetime.date(int(d.group(3)), int(d.group(2)), int(d.group(1)))
        # нужен недельный выпуск по РК, а не «(начало года)» и не месячные
        if best is None or day > best[0]:
            best = (day, int(el.group(1)))
    if not best:
        log('! ссылку на странице не нашёл, беру известный выпуск %d' % FALLBACK_ID)
        return '%s/api/iblock/element/%d/file/ru/' % (BASE, FALLBACK_ID), None
    log('· последний выпуск: %s (element %d)' % (best[0].strftime('%d.%m.%Y'), best[1]))
    return '%s/api/iblock/element/%d/file/ru/' % (BASE, best[1]), best[0]

def download():
    url, day = find_latest()
    raw = get(url)
    if raw[:2] != b'PK':
        raise SystemExit('! пришёл не xlsx (%r…) — проверь ссылку вручную' % raw[:16])
    os.makedirs(ARCH, exist_ok=True)
    name = os.path.join(ARCH, 'szpt-%s.xlsx' % (day or datetime.date.today()).isoformat())
    with open(name, 'wb') as f:
        f.write(raw)
    log('· сохранён %s (%.0f КБ)' % (os.path.basename(name), len(raw) / 1024))
    return name

def pub_date(wb):
    """Дата публикации с обложки файла."""
    for row in wb['Обложка'].iter_rows(max_col=3, values_only=True):
        for c in row:
            if isinstance(c, str) and 'Дата опубликования' in c:
                m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', c)
                if m:
                    return '%s-%s-%s' % (m.group(3), m.group(2), m.group(1))
    return None

def num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip().replace(' ', '').replace('\xa0', '').replace(',', '.')
    if s in ('', '-', '–', '—', 'x', 'х'): return None
    try: return float(s)
    except ValueError: return None

def match_prod(label):
    low = str(label).strip().lower()
    for needle, pid in PROD2ID:
        if low.startswith(needle) or needle in low:
            return pid
    return None

def parse(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {
        'source': 'Бюро национальной статистики АСПиР РК, форма Ц-101э (еженедельная)',
        'sourceUrl': PAGE,
        'published': pub_date(wb),
        'file': os.path.basename(path),
        'prices': {},   # 'regionId|productId' -> ₸
        'national': {},  # productId -> ₸ (по обследованным городам)
        'wow': {},       # 'regionId|productId' -> индекс к прошлой неделе, %
        'products': {},  # productId -> официальное наименование БНС
        'units': {},     # productId -> единица измерения
        'order': [pid for _, pid, _ in SZPT],   # порядок позиций в перечне СЗПТ
        'cities': {},    # regionId -> город, где ведётся наблюдение
    }

    # --- лист 5: средние цены, ₸ ---
    ws = wb['5']
    head = next(ws.iter_rows(min_row=3, max_row=3, values_only=True))
    cols = {}
    for i, c in enumerate(head):
        if not c: continue
        name = str(c).strip()
        if name == NATIONAL:
            cols[i] = '__nat__'
        elif name in CITY2REG:
            cols[i] = CITY2REG[name]
            out['cities'][CITY2REG[name]] = name
    missing = set(CITY2REG) - {str(c).strip() for c in head if c}
    if missing:
        log('! нет колонок для городов: %s' % ', '.join(sorted(missing)))

    found = set()
    for row in ws.iter_rows(min_row=4, values_only=True):
        pid = match_prod(row[0]) if row and row[0] else None
        if not pid or pid in found:
            continue
        found.add(pid)
        out['products'][pid] = re.sub(r'\s+', ' ', str(row[0]).strip())
        out['units'][pid] = UNIT[pid]
        for i, where in cols.items():
            v = num(row[i]) if i < len(row) else None
            if v is None: continue
            if where == '__nat__': out['national'][pid] = v
            else: out['prices']['%s|%s' % (where, pid)] = v

    lost = {p for _, p in PROD2ID} - found
    if lost:
        log('! товары не найдены в файле: %s' % ', '.join(sorted(lost)))

    # --- лист 4: индекс к прошлой неделе (последняя колонка — свежая неделя) ---
    try:
        ws4 = wb['4']
        h4 = next(ws4.iter_rows(min_row=3, max_row=3, values_only=True))
        c4 = {i: CITY2REG[str(c).strip()] for i, c in enumerate(h4)
              if c and str(c).strip() in CITY2REG}
        done = set()
        for row in ws4.iter_rows(min_row=4, values_only=True):
            pid = match_prod(row[0]) if row and row[0] else None
            if not pid or pid in done: continue
            done.add(pid)
            for i, reg in c4.items():
                v = num(row[i]) if i < len(row) else None
                if v is not None:
                    out['wow']['%s|%s' % (reg, pid)] = round(v - 100, 2)
    except Exception as e:
        log('! лист 4 (недельный индекс) не разобран: %s' % e)

    return out

MARK_A = '/*SZPT-DATA-START*/'
MARK_B = '/*SZPT-DATA-END*/'

def inject(data, html_path):
    """Вшить выгрузку в html между маркерами — файл остаётся автономным."""
    html = open(html_path, encoding='utf-8').read()
    a, b = html.find(MARK_A), html.find(MARK_B)
    if a < 0 or b < 0:
        raise SystemExit('! в %s нет маркеров %s … %s' % (html_path, MARK_A, MARK_B))
    block = MARK_A + '\nconst SZPT_DATA = ' + json.dumps(data, ensure_ascii=False) + ';\n'
    open(html_path, 'w', encoding='utf-8').write(html[:a] + block + html[b:])
    log('· данные вшиты в %s' % os.path.basename(html_path))


NB_URL = 'https://nationalbank.kz/rss/rates_all.xml'
NB_SHOW = ['USD', 'EUR', 'RUB', 'CNY']

def fetch_rates():
    """Официальные курсы Нацбанка — снимок на дату сборки.
       В браузере обновляются через relay.py (/api/rates); снимок нужен для
       автономной работы файла, когда сервис не запущен."""
    try:
        xml = get(NB_URL, binary=False)
    except Exception as e:
        log('! курсы НБ РК недоступны (%s)' % e)
        return None
    out, date = [], ''
    for it in re.findall(r'<item>(.*?)</item>', xml, re.S):
        g = lambda t: (re.search(r'<%s>(.*?)</%s>' % (t, t), it, re.S) or [None, ''])[1].strip()
        code = g('title')
        if code not in NB_SHOW:
            continue
        date = date or g('pubDate')
        out.append({'code': code, 'v': float(g('description') or 0),
                    'change': float(g('change') or 0), 'dir': g('index') or ''})
    out.sort(key=lambda x: NB_SHOW.index(x['code']))
    if not out:
        return None
    log('· курсы НБ РК на %s: %s' % (date, ', '.join('%s %.2f' % (r['code'], r['v']) for r in out)))
    return {'date': date, 'rates': out,
            'source': 'Национальный Банк Республики Казахстан',
            'sourceUrl': 'https://nationalbank.kz/ru/exchangerates/ezhednevnye-oficialnye-rynochnye-kursy-valyut'}


def main():
    offline = '--offline' in sys.argv
    if offline:
        files = sorted(glob.glob(os.path.join(ARCH, 'szpt-*.xlsx')))
        if not files: raise SystemExit('! архив пуст, запусти без --offline')
        path = files[-1]
        log('· офлайн: %s' % os.path.basename(path))
    else:
        path = download()

    data = parse(path)
    if not offline:
        data['fx'] = fetch_rates()
    else:
        try: data['fx'] = json.load(open(OUT, encoding='utf-8')).get('fx')
        except Exception: data['fx'] = None
    data['archive'] = [os.path.basename(p) for p in
                       sorted(glob.glob(os.path.join(ARCH, 'szpt-*.xlsx')))]
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    log('· выпуск от %s' % data['published'])
    log('· товаров %d, регионов %d, пар цена-регион %d, недельных индексов %d'
        % (len(data['products']), len(data['cities']),
           len(data['prices']), len(data['wow'])))
    log('· архив: %d файл(ов)' % len(data['archive']))
    log('→ %s' % OUT)

    for i, arg in enumerate(sys.argv):
        if arg == '--inject' and i + 1 < len(sys.argv):
            inject(data, sys.argv[i + 1])

if __name__ == '__main__':
    main()
