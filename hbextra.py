#!/usr/bin/env python3
"""HBExtra バックエンド (Flask + SQLite)"""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen, Request, build_opener, HTTPRedirectHandler

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from flask import Flask, jsonify, request, send_from_directory, Response, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import pykakasi as _pk
    _kks = _pk.kakasi()
except ImportError:
    _kks = None

_reading_cache: dict = {}

def _tag_reading(tag: str) -> str:
    """タグのひらがな読みを返す（ローマ字検索用）。pykakasi が無ければ小文字をそのまま返す。"""
    if tag in _reading_cache:
        return _reading_cache[tag]
    if _kks is None:
        reading = tag.lower()
    else:
        reading = ''.join(x.get('hira', '') or x.get('orig', '') for x in _kks.convert(tag)).lower()
    _reading_cache[tag] = reading
    return reading

BASE_DIR  = Path(__file__).parent
DATA_DIR  = Path(os.environ.get('HBEXTRA_DATA_DIR', BASE_DIR))
DB_PATH   = DATA_DIR / 'hbextra.db'
HTML_FILE = 'hbextra.html'

REFRESH_INTERVAL = 10 * 60  # 秒
MAX_IMPORT_ENTRIES = 5000
MAX_IMPORT_MEMBERSHIPS = 20000
MAX_IMPORT_USER_URLS = 10000
MAX_PROXY_BYTES = 1024 * 1024
MAX_FETCH_BYTES = 2 * 1024 * 1024
MAX_TITLE_LEN = 500
MAX_URL_LEN = 2048
MAX_TAG_LEN = 80
MAX_CATEGORY_LEN = 80
MAX_DATE_LEN = 80
MAX_TEXT_LEN = 8000
MAX_COUNT = 10_000_000

DC_NS     = 'http://purl.org/dc/elements/1.1/'
HATENA_NS = 'http://www.hatena.ne.jp/info/xmlns#'
RSS_NS    = 'http://purl.org/rss/1.0/'
RDF_NS    = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
ENTRY_API = 'https://b.hatena.ne.jp/entry/jsonlite/?url='

FEEDS = {
    'hot': {
        '':            'https://b.hatena.ne.jp/hotentry.rss',
        'it':          'https://b.hatena.ne.jp/hotentry/it.rss',
        'social':      'https://b.hatena.ne.jp/hotentry/social.rss',
        'fun':         'https://b.hatena.ne.jp/hotentry/fun.rss',
        'entertainment': 'https://b.hatena.ne.jp/hotentry/entertainment.rss',
        'knowledge':   'https://b.hatena.ne.jp/hotentry/knowledge.rss',
        'life':        'https://b.hatena.ne.jp/hotentry/life.rss',
        'economics':   'https://b.hatena.ne.jp/hotentry/economics.rss',
        'game':        'https://b.hatena.ne.jp/hotentry/game.rss',
        'anime':       'https://b.hatena.ne.jp/hotentry/anime.rss',
    },
    'new': {
        '':            'https://b.hatena.ne.jp/entrylist.rss',
        'it':          'https://b.hatena.ne.jp/entrylist/it.rss',
        'social':      'https://b.hatena.ne.jp/entrylist/social.rss',
        'fun':         'https://b.hatena.ne.jp/entrylist/fun.rss',
        'entertainment': 'https://b.hatena.ne.jp/entrylist/entertainment.rss',
        'knowledge':   'https://b.hatena.ne.jp/entrylist/knowledge.rss',
        'life':        'https://b.hatena.ne.jp/entrylist/life.rss',
        'economics':   'https://b.hatena.ne.jp/entrylist/economics.rss',
        'game':        'https://b.hatena.ne.jp/entrylist/game.rss',
        'anime':       'https://b.hatena.ne.jp/entrylist/anime.rss',
    }
}

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('HBEXTRA_MAX_CONTENT_LENGTH', 2 * 1024 * 1024))

# /hbextra prefix で配信するための WSGI middleware
# - URL は /hbextra/login のように prefix 付きで来る
# - Flask の url_for() は自動的に /hbextra/... を返すようになる
# - prefix 外（ローカル直起動の `/`）は /hbextra/ へ 302 リダイレクトしてユーザー導線を救う
from werkzeug.middleware.dispatcher import DispatcherMiddleware

_HBEXTRA_PREFIX = '/hbextra'

def _root_redirect_app(environ, start_response):
    target = _HBEXTRA_PREFIX + '/'
    start_response('302 Found', [('Location', target), ('Content-Type', 'text/plain; charset=utf-8')])
    return [b'Redirecting to /hbextra/']

app.wsgi_app = DispatcherMiddleware(_root_redirect_app, {_HBEXTRA_PREFIX: app.wsgi_app})

def _chmod_private(path):
    try:
        Path(path).chmod(0o600)
    except OSError:
        pass

def _ensure_data_dir():
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass

def _write_private_text(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    _chmod_private(path)

_ensure_data_dir()

# セッション用の秘密鍵（永続化）
_secret_path = DATA_DIR / '.secret_key'
if _secret_path.exists():
    _chmod_private(_secret_path)
    app.secret_key = _secret_path.read_text().strip()
else:
    app.secret_key = secrets.token_hex(32)
    _write_private_text(_secret_path, app.secret_key)

# CSRF + cookie hardening: HttpOnly で JS から触れないようにし、SameSite=Lax で
# 第三者 origin からの cookie 送信を遮断する（GET の navigation には影響しない）
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('HBEXTRA_COOKIE_SECURE', '').lower() in {'1', 'true', 'yes'},
)

@app.after_request
def add_security_headers(resp):
    # プロキシプレビューのレスポンスには埋め込み制限ヘッダを付けない。
    # CSP sandbox が効くと iframe の中身は opaque origin になり、
    # X-Frame-Options: SAMEORIGIN の同一 origin 判定が必ず失敗して
    # Chrome が "This content is blocked" で描画を止めてしまうため。
    if resp.headers.get('Content-Security-Policy', '').startswith('sandbox'):
        return resp
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    resp.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
    if 'Content-Security-Policy' not in resp.headers:
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'"
        )
    if os.environ.get('HBEXTRA_HSTS', '').lower() in {'1', 'true', 'yes'}:
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return resp

# ─── DB ──────────────────────────────────────────────────────────────

_db_lock = threading.Lock()
_refresh_rest_lock = threading.Lock()
_rate_lock = threading.Lock()
_rate_buckets = {}

@contextmanager
def db_conn():
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _chmod_private(DB_PATH)
            _chmod_private(str(DB_PATH) + '-wal')
            _chmod_private(str(DB_PATH) + '-shm')
            conn.close()

def init_db():
    with db_conn() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS entries (
            url          TEXT PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT '',
            date         TEXT NOT NULL DEFAULT '',
            count        INTEGER NOT NULL DEFAULT 0,
            cats         TEXT NOT NULL DEFAULT '[]',
            tags         TEXT NOT NULL DEFAULT '[]',
            tags_loaded  INTEGER NOT NULL DEFAULT 0,
            first_seen   TEXT NOT NULL DEFAULT (datetime('now')),
            starred      INTEGER NOT NULL DEFAULT 0,
            dismissed    INTEGER NOT NULL DEFAULT 0
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS memberships (
            url  TEXT NOT NULL,
            mode TEXT NOT NULL,
            cat  TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (url, mode, cat)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_mem_mode_cat ON memberships(mode, cat)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_first_seen ON entries(first_seen)')
        # ── ユーザー関連テーブル ──
        db.execute('''CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS user_stars (
            user_id INTEGER NOT NULL,
            url     TEXT NOT NULL,
            PRIMARY KEY (user_id, url)
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS user_dismissed (
            user_id INTEGER NOT NULL,
            url     TEXT NOT NULL,
            PRIMARY KEY (user_id, url)
        )''')

# ─── Helpers (validation, JSON, HTTP) ────────────────────────────────

class _NoRedirectHandler(HTTPRedirectHandler):
    """SSRF 対策: redirect を一切追従しない（短縮 URL → 内部 IP の経路を塞ぐ）"""
    def http_error_301(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, f'redirect blocked: {msg}', headers, fp)
    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301

_no_redirect_opener = build_opener(_NoRedirectHandler())

def _is_internal_ip(value):
    ip = ipaddress.ip_address(value)
    return (ip.is_loopback or ip.is_private or ip.is_link_local or
            ip.is_multicast or ip.is_reserved or ip.is_unspecified)

def _validate_external_url(raw_url):
    if not raw_url or not isinstance(raw_url, str):
        raise ValueError('missing url')
    normalized = raw_url.strip()
    if len(normalized) > MAX_URL_LEN:
        raise ValueError('url too long')
    if re.search(r'[\x00-\x20<>"\'`]', normalized):
        raise ValueError('url contains unsafe characters')
    parsed = urlparse(normalized)
    if parsed.scheme not in {'http', 'https'}:
        raise ValueError('unsupported scheme')
    host = parsed.hostname
    if not host:
        raise ValueError('missing hostname')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as ex:
        raise ValueError('host resolution failed') from ex
    for info in infos:
        if _is_internal_ip(info[4][0]):
            raise ValueError('internal address is not allowed')
    return normalized

def _validate_response_peer(resp):
    sock = getattr(getattr(getattr(resp, 'fp', None), 'raw', None), '_sock', None)
    if sock is None:
        return
    try:
        host = sock.getpeername()[0]
    except OSError:
        return
    if _is_internal_ip(host):
        raise ValueError('internal address is not allowed')

def _read_limited(resp, max_bytes):
    chunks = []
    total = 0
    while True:
        chunk = resp.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError('response too large')
    return b''.join(chunks)

def _charset_from_content_type(content_type):
    match = re.search(r'charset=["\']?([^;"\'\s]+)', content_type or '', re.I)
    return match.group(1).strip() if match else ''

def _charset_from_html(body):
    head = body[:4096].decode('ascii', errors='ignore')
    match = re.search(r'<meta[^>]+charset=["\']?\s*([^"\'\s/>;]+)', head, re.I)
    if match:
        return match.group(1).strip()
    match = re.search(
        r'<meta[^>]+http-equiv=["\']?content-type["\']?[^>]+content=["\'][^"\']*charset=([^"\'\s;]+)',
        head,
        re.I,
    )
    return match.group(1).strip() if match else ''

def _decode_response_body(body, content_type):
    candidates = [
        _charset_from_content_type(content_type),
        _charset_from_html(body),
        'utf-8',
        'cp932',
        'shift_jis',
        'euc_jp',
    ]
    tried = set()
    for charset in candidates:
        charset = (charset or '').lower()
        if not charset or charset in tried:
            continue
        tried.add(charset)
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass
    return body.decode('utf-8', errors='replace')

def _bounded_str(value, field_name, max_len, *, allow_empty=True):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError(f'{field_name} must be a string')
    value = value.strip()
    if not allow_empty and not value:
        raise ValueError(f'{field_name} is required')
    if len(value) > max_len:
        raise ValueError(f'{field_name} is too long')
    return value

def _bounded_int(value, field_name, *, min_value=0, max_value=MAX_COUNT):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{field_name} must be an integer')
    if value < min_value or value > max_value:
        raise ValueError(f'{field_name} out of range')
    return value

def _normalize_json_array(value, field_name):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as ex:
            raise ValueError(f'{field_name} is not valid JSON') from ex
    if not isinstance(value, list):
        raise ValueError(f'{field_name} must be a list')
    return value

def _normalize_import_tags(raw_tags):
    tags = _normalize_json_array(raw_tags, 'tags')
    if len(tags) > 500:
        raise ValueError('too many tags')
    normalized = []
    for item in tags:
        if not isinstance(item, dict):
            raise ValueError('tag must be an object')
        tag = _bounded_str(item.get('tag'), 'tag', MAX_TAG_LEN, allow_empty=False)
        count = _bounded_int(item.get('count', 0), 'tag count')
        normalized.append({'tag': tag, 'count': count})
    return normalized

def _normalize_import_categories(raw_cats):
    cats = _normalize_json_array(raw_cats, 'cats')
    if len(cats) > 50:
        raise ValueError('too many categories')
    return [_bounded_str(cat, 'category', MAX_CATEGORY_LEN, allow_empty=False) for cat in cats]

def _normalize_import_entry(entry):
    if not isinstance(entry, dict):
        raise ValueError('entry must be an object')
    url = _validate_external_url(entry.get('url', ''))
    title = _bounded_str(entry.get('title', ''), 'title', MAX_TITLE_LEN)
    date = _bounded_str(entry.get('date', ''), 'date', MAX_DATE_LEN)
    count = _bounded_int(entry.get('count', 0), 'count')
    cats = _normalize_import_categories(entry.get('cats', []))
    tags = _normalize_import_tags(entry.get('tags', []))
    tags_loaded = _bounded_int(entry.get('tags_loaded', 0), 'tags_loaded', min_value=0, max_value=2)
    first_seen = _bounded_str(entry.get('first_seen', datetime.now().isoformat()), 'first_seen', MAX_DATE_LEN)
    return {
        'url': url,
        'title': title,
        'date': date,
        'count': count,
        'cats': cats,
        'tags': tags,
        'tags_loaded': tags_loaded,
        'first_seen': first_seen,
        'starred': bool(entry.get('starred', 0)),
        'dismissed': bool(entry.get('dismissed', 0)),
    }

def _normalize_import_membership(item, valid_urls):
    if not isinstance(item, dict):
        raise ValueError('membership must be an object')
    url = _bounded_str(item.get('url', ''), 'membership url', MAX_URL_LEN, allow_empty=False)
    if url not in valid_urls:
        raise ValueError('membership url is not in entries')
    mode = _bounded_str(item.get('mode', 'new'), 'mode', 20, allow_empty=False)
    if mode not in FEEDS:
        raise ValueError('invalid mode')
    cat = _bounded_str(item.get('cat', ''), 'cat', MAX_CATEGORY_LEN)
    if cat and cat not in FEEDS[mode]:
        raise ValueError('invalid cat')
    return {'url': url, 'mode': mode, 'cat': cat}

def _normalize_url_list(values, field_name, valid_urls):
    values = values or []
    if not isinstance(values, list):
        raise ValueError(f'{field_name} must be a list')
    if len(values) > MAX_IMPORT_USER_URLS:
        raise ValueError(f'too many {field_name}')
    normalized = []
    for url in values:
        url = _bounded_str(url, field_name, MAX_URL_LEN, allow_empty=False)
        if url in valid_urls:
            normalized.append(url)
    return normalized

def _safe_json_array(raw):
    """一行壊れただけで /api/entries や /api/tags が 500 にならないよう、不正 JSON は [] に潰す。"""
    try:
        value = json.loads(raw or '[]')
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []

def _parse_int_arg(name, default, *, min_value=None, max_value=None):
    raw = request.args.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'invalid {name}')
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value

def _rate_limit(bucket, key, limit, window_seconds):
    now = time.time()
    ident = (bucket, key)
    with _rate_lock:
        hits = [t for t in _rate_buckets.get(ident, []) if now - t < window_seconds]
        if len(hits) >= limit:
            _rate_buckets[ident] = hits
            return False
        hits.append(now)
        _rate_buckets[ident] = hits
        return True

def rate_limited(bucket, limit, window_seconds=60):
    def wrapper(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            key = request.headers.get('X-Forwarded-For', request.remote_addr or 'local').split(',')[0].strip()
            if not _rate_limit(bucket, key, limit, window_seconds):
                return jsonify({'error': 'rate limit exceeded'}), 429
            return f(*args, **kwargs)
        return decorated
    return wrapper

# ─── Auth ─────────────────────────────────────────────────────────────

_LEGACY_HASH_RE = re.compile(r'[0-9a-f]{32}:[0-9a-f]{64}')

def hash_password(pw):
    return generate_password_hash(pw, method='pbkdf2:sha256')

def _is_legacy_hash(stored):
    """ffdf6c1 以前の 'salt_hex:sha256_hex' 形式を判定。新形式は werkzeug の prefix で始まる。"""
    if not stored:
        return False
    return bool(_LEGACY_HASH_RE.fullmatch(stored))

def verify_password(pw, stored):
    if _is_legacy_hash(stored):
        salt, h = stored.split(':', 1)
        return hashlib.sha256((salt + pw).encode()).hexdigest() == h
    try:
        return check_password_hash(stored, pw)
    except (ValueError, TypeError):
        return False

def get_current_user_id():
    return session.get('user_id')

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_current_user_id():
            if request.path.startswith('/api/'):
                return jsonify({'error': 'login required'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def _issue_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(16)
        session['csrf_token'] = token
    return token

def csrf_protected(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-CSRF-Token', '')
        expected = session.get('csrf_token', '')
        if not token or not expected or not secrets.compare_digest(token, expected):
            return jsonify({'error': 'csrf token invalid'}), 403
        return f(*args, **kwargs)
    return decorated

def migrate_legacy_data(user_id):
    """既存のstarred/dismissedデータを新テーブルに移行（初回ユーザー登録時）"""
    with db_conn() as db:
        rows = db.execute('SELECT url FROM entries WHERE starred=1').fetchall()
        for r in rows:
            db.execute('INSERT OR IGNORE INTO user_stars (user_id, url) VALUES (?, ?)',
                       (user_id, r['url']))
        rows = db.execute('SELECT url FROM entries WHERE dismissed=1').fetchall()
        for r in rows:
            db.execute('INSERT OR IGNORE INTO user_dismissed (user_id, url) VALUES (?, ?)',
                       (user_id, r['url']))

# ─── RSS parsing ──────────────────────────────────────────────────────

def fetch_url(url, timeout=10, follow_redirects=True, max_bytes=MAX_FETCH_BYTES):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 hbextra/2.0'})
    opener = urlopen if follow_redirects else _no_redirect_opener.open
    with opener(req, timeout=timeout) as r:
        _validate_response_peer(r)
        body = _read_limited(r, max_bytes)
        return _decode_response_body(body, r.headers.get('Content-Type', ''))

def parse_rss(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException):
        return []
    # RSS 1.0 (RDF) uses namespace-qualified elements
    items = root.findall(f'{{{RSS_NS}}}item')
    # Fallback: RSS 2.0 or no namespace
    if not items:
        items = root.findall('.//item')
    entries = []
    def _find(item, ns_tag, plain_tag):
        el = item.find(ns_tag)
        return el if el is not None else item.find(plain_tag)

    for item in items:
        # URL: prefer rdf:about attribute, then <link> element
        url = item.get(f'{{{RDF_NS}}}about', '')
        if not url:
            link_el = _find(item, f'{{{RSS_NS}}}link', 'link')
            url = (link_el.text or '').strip() if link_el is not None else ''
        title_el = _find(item, f'{{{RSS_NS}}}title', 'title')
        title = (title_el.text or '').strip() if title_el is not None else ''
        if not url or not title:
            continue
        count_el = item.find(f'{{{HATENA_NS}}}bookmarkcount')
        count = int(count_el.text or '0') if count_el is not None and count_el.text else 0
        date_el = item.find(f'{{{DC_NS}}}date')
        if date_el is None:
            date_el = item.find('pubDate')
        date = (date_el.text or '').strip() if date_el is not None else ''
        cats = [el.text.strip() for el in item.findall(f'{{{DC_NS}}}subject') if el.text]
        entries.append({'url': url, 'title': title, 'count': count, 'date': date, 'cats': cats})
    return entries

def _safe_tag_count(item):
    if not isinstance(item, dict):
        return 0
    count = item.get('count', 0)
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0

# ─── Feed refresh ─────────────────────────────────────────────────────

last_refresh_at = 0.0

def refresh_feed(mode, cat):
    if mode not in FEEDS:
        return 0
    feed_url = FEEDS[mode].get(cat, FEEDS[mode][''])
    try:
        xml = fetch_url(feed_url, timeout=12)
        entries = parse_rss(xml)
        if not entries:
            return 0
        with db_conn() as db:
            for e in entries:
                db.execute('''
                    INSERT INTO entries (url, title, date, count, cats)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        title = excluded.title,
                        count = excluded.count,
                        date  = excluded.date,
                        cats  = excluded.cats
                ''', (e['url'], e['title'], e['date'], e['count'],
                      json.dumps(e['cats'], ensure_ascii=False)))
                db.execute('''
                    INSERT OR IGNORE INTO memberships (url, mode, cat) VALUES (?, ?, ?)
                ''', (e['url'], mode, cat))
        return len(entries)
    except Exception as ex:
        print(f'[feed] {mode}/{cat or "all"}: {ex}')
        return 0

def refresh_all():
    global last_refresh_at
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] refreshing all feeds…')
    total = 0
    for mode in FEEDS:
        for cat in FEEDS[mode]:
            n = refresh_feed(mode, cat)
            total += n
            time.sleep(0.3)
    last_refresh_at = time.time()
    print(f'[refresh] done — {total} entries')

# ─── Tag loading ──────────────────────────────────────────────────────

def load_one_tag(url):
    try:
        text = fetch_url(ENTRY_API + url, timeout=10)
        data = json.loads(text)
        cnt = {}
        for bm in data.get('bookmarks', []):
            for tag in bm.get('tags', []):
                cnt[tag] = cnt.get(tag, 0) + 1
        tags = [{'tag': t, 'count': c}
                for t, c in sorted(cnt.items(), key=lambda x: -x[1])]
        with db_conn() as db:
            db.execute('UPDATE entries SET tags=?, tags_loaded=1 WHERE url=?',
                       (json.dumps(tags, ensure_ascii=False), url))
    except Exception:
        # Mark as attempted (2) so we don't retry endlessly
        try:
            with db_conn() as db:
                db.execute('UPDATE entries SET tags_loaded=2 WHERE url=?', (url,))
        except Exception as db_ex:
            print(f'[tags] failed to mark tag load failure url={url}: {db_ex}')

def tag_loader_bg():
    """Background: load tags for entries without them."""
    while True:
        try:
            with db_conn() as db:
                row = db.execute(
                    'SELECT url FROM entries WHERE tags_loaded=0 LIMIT 1'
                ).fetchone()
            if row:
                load_one_tag(row['url'])
                time.sleep(0.5)
            else:
                time.sleep(30)
        except Exception as ex:
            print(f'[tags] {ex}')
            time.sleep(5)

def refresh_scheduler():
    refresh_all()
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh_all()

# ─── API ──────────────────────────────────────────────────────────────

LOGIN_PAGE = '''<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HBExtra - ログイン</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #f0f2f5; display: flex;
         align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { background: #fff; padding: 32px; border-radius: 12px;
          box-shadow: 0 2px 16px rgba(0,0,0,.1); width: 320px; }
  h1 { font-size: 22px; margin: 0 0 20px; text-align: center; color: #008fde; }
  input { width: 100%; padding: 10px; margin: 6px 0; border: 1px solid #ddd;
          border-radius: 6px; font-size: 14px; box-sizing: border-box; }
  input:focus { outline: none; border-color: #008fde; }
  .btn { width: 100%; padding: 10px; border: none; border-radius: 6px;
         font-size: 14px; cursor: pointer; margin: 6px 0; }
  .btn-primary { background: #008fde; color: #fff; }
  .btn-secondary { background: #eee; color: #333; }
  .btn:hover { opacity: .85; }
  .error { color: #c00; font-size: 13px; text-align: center; margin: 8px 0; }
  .toggle { text-align: center; font-size: 13px; color: #666; margin-top: 12px; }
  .toggle a { color: #008fde; cursor: pointer; text-decoration: none; }
</style></head><body>
<div class="card">
  <h1>HBExtra</h1>
  <div id="error" class="error"></div>
  <form id="form">
    <input id="username" name="username" placeholder="ユーザー名" required autocomplete="username">
    <input id="password" name="password" type="password" placeholder="パスワード" required autocomplete="current-password">
    <button type="submit" class="btn btn-primary" id="submit-btn">ログイン</button>
  </form>
  <div class="toggle"><a id="toggle-link" onclick="toggleMode()">アカウントを作成する</a></div>
</div>
<script>
let isRegister = false;
function toggleMode() {
  isRegister = !isRegister;
  document.getElementById('submit-btn').textContent = isRegister ? '登録' : 'ログイン';
  document.getElementById('toggle-link').textContent = isRegister ? 'ログインに戻る' : 'アカウントを作成する';
  document.getElementById('error').textContent = '';
}
document.getElementById('form').onsubmit = async e => {
  e.preventDefault();
  const body = { username: document.getElementById('username').value,
                 password: document.getElementById('password').value };
  const url = isRegister ? '/hbextra/api/register' : '/hbextra/api/login';
  const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const data = await r.json();
  if (data.ok) { window.location.href = '/hbextra/'; }
  else { document.getElementById('error').textContent = data.error || 'エラーが発生しました'; }
};
</script></body></html>'''

@app.route('/login')
def login_page():
    if get_current_user_id():
        return redirect(url_for('index'))
    return LOGIN_PAGE

@app.route('/api/login', methods=['POST'])
@rate_limited('login', 20)
def api_login():
    d = request.json or {}
    username = d.get('username', '').strip()
    password = d.get('password', '')
    if not username or not password:
        return jsonify({'ok': False, 'error': 'ユーザー名とパスワードを入力してください'})
    with db_conn() as db:
        user = db.execute('SELECT id, password_hash FROM users WHERE username=?', (username,)).fetchone()
    if not user or not verify_password(password, user['password_hash']):
        return jsonify({'ok': False, 'error': 'ユーザー名またはパスワードが違います'})
    if _is_legacy_hash(user['password_hash']):
        with db_conn() as db:
            db.execute('UPDATE users SET password_hash=? WHERE id=?',
                       (hash_password(password), user['id']))
    session['user_id'] = user['id']
    session['username'] = username
    _issue_csrf_token()
    return jsonify({'ok': True})

@app.route('/api/register', methods=['POST'])
@rate_limited('register', 10)
def api_register():
    d = request.json or {}
    username = d.get('username', '').strip()
    password = d.get('password', '')
    if not username or not password:
        return jsonify({'ok': False, 'error': 'ユーザー名とパスワードを入力してください'})
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'パスワードは8文字以上にしてください'})
    with db_conn() as db:
        existing = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if existing:
            return jsonify({'ok': False, 'error': 'このユーザー名は既に使われています'})
        is_first = db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
        if not is_first:
            expected_token = os.environ.get('HBEXTRA_REGISTRATION_TOKEN', '')
            provided_token = d.get('registration_token') or request.headers.get('X-Registration-Token', '')
            if not expected_token or not secrets.compare_digest(str(provided_token), expected_token):
                return jsonify({'ok': False, 'error': 'ユーザー登録は管理者の招待が必要です'}), 403
        db.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                   (username, hash_password(password)))
        user = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if is_first:
        migrate_legacy_data(user['id'])
    session['user_id'] = user['id']
    session['username'] = username
    _issue_csrf_token()
    return jsonify({'ok': True})

@app.route('/api/me')
def api_me():
    uid = get_current_user_id()
    if not uid:
        return jsonify({'ok': False})
    return jsonify({
        'ok': True,
        'user_id': uid,
        'username': session.get('username', ''),
        'csrf_token': _issue_csrf_token(),
    })

@app.route('/logout', methods=['POST'])
@login_required
@csrf_protected
def logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/')
@login_required
def index():
    return send_from_directory(str(BASE_DIR), HTML_FILE)

@app.route('/api/entries')
@login_required
def api_entries():
    uid        = get_current_user_id()
    mode       = request.args.get('mode', 'new')
    cat        = request.args.get('cat', '')
    try:
        page     = _parse_int_arg('page', 0, min_value=0)
        per_page = _parse_int_arg('per_page', 100, min_value=1, max_value=500)
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    search     = request.args.get('search', '').strip()
    tag_filter = request.args.get('tag', '').strip()
    star_only      = request.args.get('star_only',      'false') == 'true'
    dismissed_only = request.args.get('dismissed_only', 'false') == 'true'

    if cat == '':
        mem_cond = 'EXISTS (SELECT 1 FROM memberships m WHERE m.url=e.url AND m.mode=?)'
        params = [mode]
    else:
        mem_cond = 'EXISTS (SELECT 1 FROM memberships m WHERE m.url=e.url AND m.mode=? AND m.cat=?)'
        params = [mode, cat]

    dismissed_cond = 'EXISTS (SELECT 1 FROM user_dismissed ud WHERE ud.user_id=? AND ud.url=e.url)'
    not_dismissed_cond = 'NOT ' + dismissed_cond

    if dismissed_only:
        where = [dismissed_cond, mem_cond]
        params = [uid] + params
    else:
        where = [not_dismissed_cond, mem_cond]
        params = [uid] + params

    if star_only and not dismissed_only:
        where.append('EXISTS (SELECT 1 FROM user_stars us WHERE us.user_id=? AND us.url=e.url)')
        params.append(uid)
    if search:
        where.append('(e.title LIKE ? OR e.cats LIKE ? OR e.tags LIKE ?)')
        s = f'%{search}%'
        params += [s, s, s]
    if tag_filter:
        where.append('e.tags LIKE ?')
        params.append(f'%"{tag_filter}"%')

    sql_where = ' AND '.join(where)
    base_sql = f'FROM entries e WHERE {sql_where}'

    with db_conn() as db:
        total = db.execute(
            f'SELECT COUNT(*) {base_sql}', params
        ).fetchone()[0]
        rows = db.execute(
            # SQL fragments are fixed server-side; all user values use parameters.
            f'SELECT e.url,e.title,e.date,e.count,e.cats,e.tags,e.tags_loaded,'  # nosec
            f'EXISTS(SELECT 1 FROM user_stars us WHERE us.user_id=? AND us.url=e.url) as starred '
            f'{base_sql} '
            f'ORDER BY e.first_seen DESC LIMIT ? OFFSET ?',
            [uid] + params + [per_page, page * per_page]
        ).fetchall()
        all_tag_rows = db.execute(
            f'SELECT e.tags, e.date {base_sql}', params
        ).fetchall()

    cnt       = {}
    last_date = {}
    for row in all_tag_rows:
        d = row[1] or ''
        for t in _safe_json_array(row[0]):
            tag = t.get('tag', '') if isinstance(t, dict) else ''
            if tag:
                cnt[tag] = cnt.get(tag, 0) + _safe_tag_count(t)
                if d > last_date.get(tag, ''):
                    last_date[tag] = d
    top_tags_all = [{'tag': t, 'count': c, 'last': last_date.get(t, ''), 'reading': _tag_reading(t)}
                    for t, c in sorted(cnt.items(), key=lambda x: -x[1]) if c >= 2][:1000]

    entries = [{
        'url':        r['url'],
        'title':      r['title'],
        'date':       r['date'],
        'count':      r['count'] if isinstance(r['count'], int) and not isinstance(r['count'], bool) else 0,
        'cats':       _safe_json_array(r['cats']),
        'tags':       _safe_json_array(r['tags']),
        'tagsLoaded': r['tags_loaded'] >= 1,
        'starred':    bool(r['starred']),
    } for r in rows]

    return jsonify({'entries': entries, 'total': total, 'page': page,
                    'top_tags_all': top_tags_all})

@app.route('/api/status')
@login_required
def api_status():
    uid = get_current_user_id()
    with db_conn() as db:
        total   = db.execute(
            'SELECT COUNT(*) FROM entries e WHERE NOT EXISTS '
            '(SELECT 1 FROM user_dismissed ud WHERE ud.user_id=? AND ud.url=e.url)', (uid,)
        ).fetchone()[0]
        starred = db.execute('SELECT COUNT(*) FROM user_stars WHERE user_id=?', (uid,)).fetchone()[0]
        no_tags = db.execute(
            'SELECT COUNT(*) FROM entries e WHERE e.tags_loaded=0 AND NOT EXISTS '
            '(SELECT 1 FROM user_dismissed ud WHERE ud.user_id=? AND ud.url=e.url)', (uid,)
        ).fetchone()[0]
    return jsonify({
        'total':           total,
        'starred':         starred,
        'no_tags':         no_tags,
        'last_refresh_at': int(last_refresh_at * 1000),
        'now':             int(time.time() * 1000),
    })

@app.route('/api/refresh', methods=['POST'])
@login_required
@csrf_protected
@rate_limited('refresh', 3)
def api_refresh():
    """現在のフィードを同期的に更新し、他はバックグラウンドで更新する。"""
    global last_refresh_at
    data = request.json or {}
    mode = data.get('mode', 'new')
    cat  = data.get('cat', '')
    if mode not in FEEDS:
        return jsonify({'error': 'invalid mode'}), 400
    if cat and cat not in FEEDS[mode]:
        return jsonify({'error': 'invalid cat'}), 400

    # 現在表示中のフィードを先に更新（即時反映）
    refresh_feed(mode, cat)
    last_refresh_at = time.time()

    # 残りをバックグラウンドで更新
    def refresh_rest():
        global last_refresh_at
        try:
            for m in FEEDS:
                for c in FEEDS[m]:
                    if m == mode and c == cat:
                        continue
                    refresh_feed(m, c)
                    time.sleep(0.3)
            last_refresh_at = time.time()
        finally:
            _refresh_rest_lock.release()

    if _refresh_rest_lock.acquire(blocking=False):
        threading.Thread(target=refresh_rest, daemon=True).start()
    return jsonify({'ok': True})

class _TextExtractor(HTMLParser):
    """HTML から本文テキストを抽出するシンプルなパーサー"""
    SKIP_TAGS = {'script','style','noscript','nav','footer','header','aside','form','button'}
    BLOCK_TAGS = {'p','div','li','h1','h2','h3','h4','h5','h6','br','tr','article','section'}

    def __init__(self):
        super().__init__()
        self._buf, self._depth, self._skip_depth = [], [], None

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if self._skip_depth is None and t in self.SKIP_TAGS:
            self._skip_depth = len(self._depth)
        self._depth.append(t)
        if t in self.BLOCK_TAGS:
            self._buf.append('\n')

    def handle_endtag(self, tag):
        t = tag.lower()
        if self._depth and self._depth[-1] == t:
            self._depth.pop()
        if self._skip_depth is not None and len(self._depth) <= self._skip_depth:
            self._skip_depth = None
        if t in self.BLOCK_TAGS:
            self._buf.append('\n')

    def handle_data(self, data):
        if self._skip_depth is None:
            self._buf.append(data)

    def get_text(self):
        lines = [l.strip() for l in ''.join(self._buf).splitlines()]
        return '\n'.join(l for l in lines if l)

def _sandboxed_proxy_response(body, content_type):
    resp = Response(body, content_type=content_type)
    resp.headers['Content-Security-Policy'] = (
        'sandbox allow-scripts allow-forms allow-popups; '
        "default-src 'self' data: blob: https: http:; "
        "script-src 'unsafe-inline' 'unsafe-eval' https: http:; "
        "style-src 'unsafe-inline' https: http:; "
        'img-src data: https: http:; '
        'connect-src https: http:; '
        'form-action https: http:'
    )
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    return resp

@app.route('/api/preview')
@login_required
@rate_limited('preview', 60)
def api_preview():
    raw_url = request.args.get('url', '').strip()
    try:
        url = _validate_external_url(raw_url)
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    try:
        html = fetch_url(url, timeout=12, follow_redirects=False)
        title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        title = title_m.group(1).strip() if title_m else ''
        # HTMLエンティティを簡易デコード
        title = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), title)
        title = title.replace('&amp;','&').replace('&lt;','<').replace('&gt;','>').replace('&quot;','"')
        extractor = _TextExtractor()
        extractor.feed(html)
        text = extractor.get_text()
        return jsonify({'title': title, 'text': text[:8000]})
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500

def _period_cutoff(period):
    """期間文字列 → (from_date, to_date) ISO文字列のタプル（Noneは制限なし）"""
    now   = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'today':
        return today.isoformat(), None
    elif period == 'yesterday':
        return (today - timedelta(days=1)).isoformat(), today.isoformat()
    elif period == '7d':
        return (now - timedelta(days=7)).isoformat(), None
    elif period == '30d':
        return (now - timedelta(days=30)).isoformat(), None
    elif period == '90d':
        return (now - timedelta(days=90)).isoformat(), None
    elif period == '1y':
        return (now - timedelta(days=365)).isoformat(), None
    return None, None  # 'all'

@app.route('/api/tags')
@login_required
def api_tags():
    uid    = get_current_user_id()
    mode   = request.args.get('mode', 'new')
    cat    = request.args.get('cat', '')
    period = request.args.get('period', 'all')
    if cat == '':
        mem_cond = 'EXISTS (SELECT 1 FROM memberships m WHERE m.url=e.url AND m.mode=?)'
        params = [uid, mode]
    else:
        mem_cond = 'EXISTS (SELECT 1 FROM memberships m WHERE m.url=e.url AND m.mode=? AND m.cat=?)'
        params = [uid, mode, cat]
    # SQL fragments are fixed server-side; all user values use parameters.
    sql = f'FROM entries e WHERE NOT EXISTS (SELECT 1 FROM user_dismissed ud WHERE ud.user_id=? AND ud.url=e.url) AND {mem_cond}'  # nosec B608
    from_d, to_d = _period_cutoff(period)
    if from_d:
        sql += ' AND e.date >= ?'; params.append(from_d)
    if to_d:
        sql += ' AND e.date < ?';  params.append(to_d)
    with db_conn() as db:
        rows = db.execute(f'SELECT e.tags, e.date {sql}', params).fetchall()
    cnt = {}; last_date = {}
    for row in rows:
        d = row[1] or ''
        for t in _safe_json_array(row[0]):
            tag = t.get('tag', '') if isinstance(t, dict) else ''
            if tag:
                cnt[tag] = cnt.get(tag, 0) + _safe_tag_count(t)
                if d > last_date.get(tag, ''):
                    last_date[tag] = d
    tags = [{'tag': t, 'count': c, 'last': last_date.get(t, ''), 'reading': _tag_reading(t)}
            for t, c in sorted(cnt.items(), key=lambda x: -x[1]) if c >= 2][:1000]
    return jsonify({'tags': tags})

@app.route('/api/proxy')
@login_required
@rate_limited('proxy', 60)
def api_proxy():
    """ページを sandboxed preview としてプロキシ配信する。"""
    raw_url = request.args.get('url', '').strip()
    try:
        url = _validate_external_url(raw_url)
    except ValueError as ex:
        return jsonify({'error': str(ex)}), 400
    try:
        req = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,*/*;q=0.9',
            'Accept-Language': 'ja,en;q=0.9',
        })
        with _no_redirect_opener.open(req, timeout=15) as resp:
            _validate_response_peer(resp)
            ct = resp.headers.get('Content-Type', 'text/html')
            body = _read_limited(resp, MAX_PROXY_BYTES)
    except Exception as ex:
        # SSRF 文脈で urllib 例外文字列が内部 IP / ポート / ホスト名を含む可能性があるため、
        # ユーザーには詳細を返さずサーバーログにのみ残す
        print(f'[proxy] fetch failed url={url}: {ex}')
        err_html = f'<h2>読み込みエラー</h2><p><a href="{escape(url)}" target="_blank">元のページを開く →</a></p>'
        return _sandboxed_proxy_response(err_html.encode('utf-8'), 'text/html; charset=utf-8')

    if 'html' in ct.lower():
        text = _decode_response_body(body, ct)
        # <base> タグで相対URLを元サイト基準に解決
        base_tag = f'<base href="{url}" target="_blank">'
        text = re.sub(r'(<head[^>]*>)', r'\1' + base_tag, text, count=1, flags=re.I)
        bridge_script = '''<script>
(function() {
  window.addEventListener('message', function(event) {
    var data = event.data || {};
    if (data.type !== 'hbextra-preview-scroll') return;
    var direction = data.direction === -1 ? -1 : 1;
    window.scrollBy({ top: window.innerHeight * 0.9 * direction, behavior: 'smooth' });
  });
  window.addEventListener('keydown', function(event) {
    var key = event.key === 'Escape' ? 'Escape' : String(event.key || '').toLowerCase();
    var target = event.target;
    var isTyping = target && target.closest &&
      target.closest('input, textarea, select, button, [contenteditable]');
    if (event.key === ' ' || event.key === 'Spacebar') {
      if (!isTyping) {
        event.preventDefault();
        event.stopPropagation();
        var direction = event.shiftKey ? -1 : 1;
        window.scrollBy({ top: window.innerHeight * 0.9 * direction, behavior: 'smooth' });
      }
      return;
    }
    if (!['Escape', 'v', 'j', 'k'].includes(key)) return;
    if (!isTyping) {
      event.preventDefault();
      event.stopPropagation();
      parent.postMessage({ type: 'hbextra-preview-key', key: key }, '*');
    }
  }, true);
})();
</script>'''
        if re.search(r'</body\s*>', text, re.I):
            text = re.sub(r'</body\s*>', bridge_script + r'\g<0>', text, count=1, flags=re.I)
        else:
            text += bridge_script
        body = text.encode('utf-8')
        ct = 'text/html; charset=utf-8'

    return _sandboxed_proxy_response(body, ct)

@app.route('/api/star', methods=['POST'])
@login_required
@csrf_protected
def api_star():
    uid = get_current_user_id()
    d   = request.json or {}
    url = d.get('url', '')
    starred = d.get('starred', False)
    with db_conn() as db:
        if starred:
            db.execute('INSERT OR IGNORE INTO user_stars (user_id, url) VALUES (?, ?)', (uid, url))
        else:
            db.execute('DELETE FROM user_stars WHERE user_id=? AND url=?', (uid, url))
    return jsonify({'ok': True})

@app.route('/api/dismiss', methods=['POST'])
@login_required
@csrf_protected
def api_dismiss():
    uid = get_current_user_id()
    d   = request.json or {}
    url = d.get('url', '')
    with db_conn() as db:
        db.execute('INSERT OR IGNORE INTO user_dismissed (user_id, url) VALUES (?, ?)', (uid, url))
    return jsonify({'ok': True})

@app.route('/api/undismiss', methods=['POST'])
@login_required
@csrf_protected
def api_undismiss():
    uid = get_current_user_id()
    d   = request.json or {}
    url = d.get('url', '')
    with db_conn() as db:
        db.execute('DELETE FROM user_dismissed WHERE user_id=? AND url=?', (uid, url))
    return jsonify({'ok': True})

@app.route('/api/export')
@login_required
def api_export():
    uid = get_current_user_id()
    with db_conn() as db:
        entries = [dict(r) for r in db.execute('SELECT * FROM entries').fetchall()]
        mems    = [dict(r) for r in db.execute('SELECT * FROM memberships').fetchall()]
        stars   = [r['url'] for r in db.execute('SELECT url FROM user_stars WHERE user_id=?', (uid,)).fetchall()]
        dismissed = [r['url'] for r in db.execute('SELECT url FROM user_dismissed WHERE user_id=?', (uid,)).fetchall()]
    data = {
        'version':     5,
        'exportedAt':  datetime.now(timezone.utc).isoformat(),
        'username':    session.get('username', ''),
        'entries':     entries,
        'memberships': mems,
        'user_stars':  stars,
        'user_dismissed': dismissed,
    }
    filename = f'hbextra-{datetime.now().strftime("%Y-%m-%d")}.json'
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route('/api/import', methods=['POST'])
@login_required
@csrf_protected
@rate_limited('import', 5)
def api_import():
    uid  = get_current_user_id()
    data = request.json
    if not data or 'entries' not in data or not isinstance(data['entries'], list):
        return jsonify({'ok': False, 'error': 'invalid format'}), 400
    # まず全エントリを検証してから DB に書く（中途半端な書き込みを避ける）
    try:
        if len(data['entries']) > MAX_IMPORT_ENTRIES:
            raise ValueError('too many entries')
        normalized = [_normalize_import_entry(e) for e in data['entries']]
        valid_urls = {e['url'] for e in normalized}
        memberships_raw = data.get('memberships', [])
        if not isinstance(memberships_raw, list):
            raise ValueError('memberships must be a list')
        if len(memberships_raw) > MAX_IMPORT_MEMBERSHIPS:
            raise ValueError('too many memberships')
        memberships = [_normalize_import_membership(m, valid_urls) for m in memberships_raw]
        user_stars = _normalize_url_list(data.get('user_stars', []), 'user_stars', valid_urls)
        user_dismissed = _normalize_url_list(data.get('user_dismissed', []), 'user_dismissed', valid_urls)
    except ValueError as ex:
        return jsonify({'ok': False, 'error': str(ex)}), 400

    count = 0
    with db_conn() as db:
        for e in normalized:
            # 共有 entries は import で上書きしない（他ユーザーのデータを退行させないため）
            db.execute('''
                INSERT INTO entries
                (url,title,date,count,cats,tags,tags_loaded,first_seen,starred,dismissed)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO NOTHING
            ''', (
                e['url'], e['title'], e['date'], e['count'],
                json.dumps(e['cats'], ensure_ascii=False),
                json.dumps(e['tags'], ensure_ascii=False),
                e['tags_loaded'],
                e['first_seen'],
                0, 0
            ))
            count += 1
        for m in memberships:
            db.execute('INSERT OR IGNORE INTO memberships (url,mode,cat) VALUES (?,?,?)',
                       (m['url'], m['mode'], m['cat']))
        # ユーザー固有のスター/非表示をインポート
        for url in user_stars:
            db.execute('INSERT OR IGNORE INTO user_stars (user_id, url) VALUES (?, ?)', (uid, url))
        for url in user_dismissed:
            db.execute('INSERT OR IGNORE INTO user_dismissed (user_id, url) VALUES (?, ?)', (uid, url))
        # v4以前の形式にも対応（entries内のstarred/dismissed）
        if data.get('version', 0) < 5:
            for e in normalized:
                if e['starred']:
                    db.execute('INSERT OR IGNORE INTO user_stars (user_id, url) VALUES (?, ?)', (uid, e['url']))
                if e['dismissed']:
                    db.execute('INSERT OR IGNORE INTO user_dismissed (user_id, url) VALUES (?, ?)', (uid, e['url']))
    return jsonify({'ok': True, 'imported': count})

# ─── Start ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    threading.Thread(target=refresh_scheduler, daemon=True).start()
    threading.Thread(target=tag_loader_bg,     daemon=True).start()
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
        lan_part = f'  (LAN if HBEXTRA_HOST=0.0.0.0: http://{local_ip}:8000)'
    except OSError:
        lan_part = ''
    host = os.environ.get('HBEXTRA_HOST', '127.0.0.1')
    print(f'HBExtra → http://{host}:8000{lan_part}')
    app.run(host=host, port=8000, debug=False, threaded=True)
