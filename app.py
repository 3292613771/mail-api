from flask import Flask, request, jsonify, send_from_directory, session
import imaplib
import email
import re
import html
import os
import time
import socket
import threading
import tempfile
import base64
from collections import OrderedDict
from contextlib import contextmanager
from email.header import decode_header
from email.utils import parsedate_to_datetime
import hashlib
import json
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '060910')
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_LOCK = threading.RLock()
# 约 20 位使用者的默认配置：8 个邮箱连接，最多 20 个查询请求。
MAIL_CONCURRENCY = max(1, min(16, int(os.environ.get('MAIL_CONCURRENCY', '8'))))
FETCH_SLOTS = threading.BoundedSemaphore(MAIL_CONCURRENCY)
QUERY_SLOTS = threading.BoundedSemaphore(20)
PENDING_FETCHES = {}
QUEUE_TIMEOUT = 5
CACHE_LOCK = threading.Lock()
MAIL_CACHE = OrderedDict()
IN_FLIGHT = set()
CACHE_TTL = 5

# 部署建议：Python >= 3.10；单 worker、多线程（共享缓存和文件锁）。
# gunicorn -w 1 --threads 32 --timeout 45 -b 0.0.0.0:$PORT app:app
# index.html、admin.html、accounts.txt 放在本文件所在目录。


# ========== 删除密码配置 ==========
DELETE_PASSWORD = os.environ.get("DELETE_PASSWORD", "112233")
DELETE_PASSWORD_HASH = hashlib.sha256(DELETE_PASSWORD.encode()).hexdigest()

# ========== 管理后台数据 ==========
EMAIL_STATUS_FILE = "email_status.json"
MAIL_LOG_FILE = "mail_log.json"

def load_json(file):
    with DATA_LOCK:
        try:
            with open(os.path.join(BASE_DIR, file), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}


def save_json(file, data):
    # 原子替换，避免其他请求读到写了一半的 JSON。
    path = os.path.join(BASE_DIR, file)
    with DATA_LOCK:
        fd, tmp = tempfile.mkstemp(dir=BASE_DIR, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def log_mails(email_addr, mails):
    with DATA_LOCK:
        logs = load_json(MAIL_LOG_FILE)
        rows = logs.setdefault('logs', [])
        seen = {(x.get('email'), x.get('mail_id')) for x in rows if x.get('mail_id')}
        stamp = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
        added = False
        for item in mails:
            key = (email_addr, item['mail_id'])
            if key in seen:
                continue
            rows.append(dict(email=email_addr, mail_id=item['mail_id'],
                             sender=item['sender'], subject=item['subject'],
                             code=item.get('code'), time=stamp))
            seen.add(key)
            added = True
        if added:
            logs['logs'] = rows[-1000:]
            save_json(MAIL_LOG_FILE, logs)

def get_beijing_time():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

# ========== 读取账号配置（带缓存，只读一次，静默加载） ==========
_ACCOUNTS_CACHE = None

def load_accounts():
    """加载账号，带缓存，只读一次，静默加载（防止刷屏拖死程序）"""
    global _ACCOUNTS_CACHE
    if _ACCOUNTS_CACHE is not None:
        return _ACCOUNTS_CACHE

    accounts = {}
    try:
        with open(os.path.join(BASE_DIR, "accounts.txt"), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                if "----" in line:
                    parts = line.split("----")
                    if len(parts) == 2:
                        email_addr = parts[0].strip()
                        auth_code = parts[1].strip()
                        if '@' not in email_addr:
                            email_addr = email_addr + "@qq.com"
                        accounts[email_addr] = auth_code
                else:
                    parts = line.split()
                    if len(parts) >= 4:
                        emails = parts[0:3]
                        auth_code = parts[3]
                        for email_addr in emails:
                            if '@' not in email_addr:
                                email_addr = email_addr + "@qq.com"
                            accounts[email_addr] = auth_code
                    elif len(parts) == 2:
                        email_addr = parts[0]
                        auth_code = parts[1]
                        if '@' not in email_addr:
                            email_addr = email_addr + "@qq.com"
                        accounts[email_addr] = auth_code
    except Exception as e:
        print(f"读取账号失败: {e}")

    print(f"[DEBUG] 账号加载完成，共 {len(accounts)} 个邮箱")
    _ACCOUNTS_CACHE = accounts
    return accounts

ACCOUNTS = load_accounts()
print(f"已加载 {len(ACCOUNTS)} 个绑定邮箱")

# ========== 邮件解析函数 ==========
def decode_str(s):
    if not s:
        return ""
    try:
        decoded_parts = []
        for part, charset in decode_header(s):
            if isinstance(part, bytes):
                if charset:
                    decoded_parts.append(part.decode(charset, errors='replace'))
                else:
                    decoded_parts.append(part.decode('utf-8', errors='replace'))
            else:
                decoded_parts.append(str(part))
        return ' '.join(decoded_parts)
    except:
        return str(s)

def clean_html_to_text(html_text):
    if not html_text:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(div|p|tr|td|li|h[1-6])[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()

def get_mail_content(msg):
    content = ""
    
    try:
        all_parts = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() not in ('text/plain', 'text/html') or part.get_content_disposition() == 'attachment':
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        text = payload.decode(charset, errors='replace')
                    except:
                        text = payload.decode('utf-8', errors='replace')
                    if text.strip():
                        all_parts.append((part.get_content_type(), text))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                try:
                    text = payload.decode(charset, errors='replace')
                except:
                    text = payload.decode('utf-8', errors='replace')
                if text.strip():
                    all_parts.append((msg.get_content_type(), text))
        
        for content_type, text in all_parts:
            if content_type == "text/plain":
                content = text.strip()
                break
        
        if not content:
            for content_type, text in all_parts:
                if content_type == "text/html":
                    content = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
                    content = re.sub(r'<[^>]+>', ' ', content)
                    content = html.unescape(content)
                    content = re.sub(r'\s+', ' ', content)
                    content = content.strip()
                    break
        
        if not content:
            return "无法解析邮件内容"
        
        code = None
        match = re.search(r'(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)', content)
        if match:
            code = match.group(1)+match.group(2)+match.group(3)+match.group(4)+match.group(5)+match.group(6)
        if not code:
            match = re.search(r'\b(\d{6})\b', content)
            if match:
                code = match.group(1)
        
        content = content[:1000]
        
        if code:
            return f"验证码：{code}\n\n{content}"
        return content
        
    except Exception as e:
        return f"解析失败"

class BoundedIMAP(imaplib.IMAP4_SSL):
    """限制连接及每次 socket 读写；整个会话共享时间预算。"""
    def __init__(self):
        self.deadline = time.monotonic() + 20
        super().__init__('imap.qq.com', timeout=5)

    def _command(self, name, *args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('邮箱响应超时')
        self.sock.settimeout(min(5, remaining))
        return super()._command(name, *args)

    def read(self, size):
        self._remaining()
        return super().read(size)

    def readline(self):
        self._remaining()
        return super().readline()

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('邮箱响应超时')
        self.sock.settimeout(min(5, remaining))


@contextmanager
def mail_session(email_addr):
    mail = None
    try:
        mail = BoundedIMAP()
        mail.login(email_addr, ACCOUNTS[email_addr])
        yield mail
    finally:
        # 不发 CLOSE（可能清除已标记删除的邮件），断线清理不再等待网络。
        if mail is not None:
            try:
                mail.shutdown()
            except (OSError, imaplib.IMAP4.error):
                pass


def folder_token(folder):
    return base64.urlsafe_b64encode(folder).decode().rstrip('=')


def mail_folders(mail):
    folders = [b'INBOX']
    status, rows = mail.list()
    if status != 'OK':
        return folders
    # 用服务器返回的原始 IMAP 文件夹名称，兼容中文 modified UTF-7。
    for row in rows or []:
        if not isinstance(row, bytes):
            continue
        match = re.match(rb'\((.*?)\)\s+(?:"[^\"]*"|NIL)\s+(.+)$', row)
        if not match:
            continue
        flags, name = match.groups()
        bare = name.strip(b'"').lower()
        if b'\\junk' in flags.lower() or bare in (b'junk', b'spam', b'junk email', b'&V4NXPpCuTvY-', b'&Xn9USpCuTvY-'):
            folders.append(name)
            break
    return folders


def trash_folder(mail):
    """Return the server-identified Trash folder, preserving its IMAP encoding."""
    status, rows = mail.list()
    if status != 'OK':
        return None
    for row in rows or []:
        if not isinstance(row, bytes):
            continue
        match = re.match(rb'\((.*?)\)\s+(?:"[^\"]*"|NIL)\s+(.+)
    mails = []
    with mail_session(email_addr) as mail:
        for folder in mail_folders(mail):
            status, count = mail.select(folder, readonly=True)
            if status != 'OK':
                if folder == b'INBOX':
                    raise imaplib.IMAP4.error('无法读取收件箱')
                continue
            total = int(count[0] or 0)
            if not total:
                continue
            validity = mail.response('UIDVALIDITY')[1][0]
            if not validity:
                raise imaplib.IMAP4.error('缺少 UIDVALIDITY')
            # 每个文件夹只取最后 limit 封；单封最多 128 KiB，避免附件拖垮查询。
            status, rows = mail.fetch(f'{max(1, total-limit+1)}:{total}',
                                      '(UID BODY.PEEK[]<0.131072>)')
            if status != 'OK':
                raise imaplib.IMAP4.error('邮件读取失败')
            for part in rows or []:
                if not isinstance(part, tuple):
                    continue
                uid = re.search(rb'UID (\d+)', part[0])
                if not uid:
                    continue
                msg = email.message_from_bytes(part[1])
                try:
                    dt = parsedate_to_datetime(msg.get('Date', ''))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(timezone(timedelta(hours=8)))
                    timestamp, send_time = dt.timestamp(), dt.strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError, OverflowError):
                    timestamp, send_time = 0, msg.get('Date', '')[:30]
                content = get_mail_content(msg)
                code = re.search(r'验证码：(\d{6})', content)
                mails.append(dict(mail_id=f'{folder_token(folder)}:{validity.decode()}:{uid[1].decode()}',
                                  sender=decode_str(msg.get('From', '未知发件人')),
                                  subject=decode_str(msg.get('Subject', '无主题')),
                                  content=content, code=code[1] if code else None,
                                  time=send_time, _timestamp=timestamp))
    mails.sort(key=lambda item: item['_timestamp'], reverse=True)
    for item in mails:
        item.pop('_timestamp')
    return mails[:limit]


def get_latest_mails(email_addr, limit=10):
    if email_addr not in ACCOUNTS:
        return {'error': '邮箱未绑定'}
    if not QUERY_SLOTS.acquire(blocking=False):
        return {'error': '当前查询人数较多，请稍后重试'}
    try:
        return shared_fetch(email_addr, limit)
    finally:
        QUERY_SLOTS.release()


def shared_fetch(email_addr, limit):
    with CACHE_LOCK:
        cached = MAIL_CACHE.get(email_addr)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        pending = PENDING_FETCHES.get(email_addr)
        owner = pending is None
        if owner:
            if email_addr in IN_FLIGHT:
                return {'error': '正在删除邮件，请稍后重试'}
            pending = {'event': threading.Event(), 'result': {'error': '查询未完成，请重试'}}
            PENDING_FETCHES[email_addr] = pending
            IN_FLIGHT.add(email_addr)
    if not owner:
        # 等待同一次查询结果，不重复连接邮箱；等待时不占用全局锁。
        if not pending['event'].wait(27):
            return {'error': '邮箱响应较慢，请稍后重试'}
        return pending['result']
    acquired = False
    result = {'error': '查询失败，请稍后重试'}
    try:
        acquired = FETCH_SLOTS.acquire(timeout=QUEUE_TIMEOUT)
        if not acquired:
            result = {'error': '查询排队超时，请稍后重试'}
        else:
            result = fetch_mails(email_addr, limit)
            with CACHE_LOCK:
                MAIL_CACHE[email_addr] = (time.monotonic(), result)
                MAIL_CACHE.move_to_end(email_addr)
                while len(MAIL_CACHE) > 128:
                    MAIL_CACHE.popitem(last=False)
        return result
    except (OSError, imaplib.IMAP4.error, ValueError):
        app.logger.warning('邮件查询失败或超时')
        result = {'error': '邮箱连接失败或超时，请稍后重试并检查授权码'}
        return result
    finally:
        if acquired:
            FETCH_SLOTS.release()
        with CACHE_LOCK:
            pending['result'] = result
            PENDING_FETCHES.pop(email_addr, None)
            IN_FLIGHT.discard(email_addr)
            pending['event'].set()


def delete_mail_by_id(email_addr, mail_id):
    if email_addr not in ACCOUNTS:
        return {'error': '邮箱未绑定'}
    try:
        token, validity, uid = mail_id.split(':')
        if not validity.isdigit() or not uid.isdigit():
            raise ValueError()
        folder = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        if b'\r' in folder or b'\n' in folder:
            raise ValueError()
    except (ValueError, TypeError):
        return {'error': '邮件标识已更新，请刷新列表后再删除'}
    with CACHE_LOCK:
        if email_addr in IN_FLIGHT or not FETCH_SLOTS.acquire(blocking=False):
            return {'error': '正在处理邮件，请稍后重试'}
        IN_FLIGHT.add(email_addr)
    try:
        with mail_session(email_addr) as mail:
            if folder not in mail_folders(mail):
                return {'error': '文件夹无效，请刷新列表'}
            status, _ = mail.select(folder)
            current = mail.response('UIDVALIDITY')[1][0]
            if status != 'OK' or current != validity.encode():
                return {'error': '邮箱已变更，请刷新列表'}
            status, rows = mail.uid('search', None, 'UID', uid)
            if status != 'OK' or not rows or uid.encode() not in rows[0].split():
                return {'error': '邮件不存在，请刷新列表'}
            capabilities = current_capabilities(mail)
            # UID EXPUNGE is scoped to one UID; plain EXPUNGE would purge other
            # messages already marked Deleted by another mail client.
            if b'UIDPLUS' in capabilities:
                if mail.uid('store', uid, '+FLAGS.SILENT', r'(\Deleted)')[0] != 'OK':
                    return {'error': '删除标记失败'}
                if mail.uid('expunge', uid)[0] != 'OK':
                    return {'error': '删除未确认，请刷新检查'}
            elif b'MOVE' in capabilities:
                trash = trash_folder(mail)
                if trash is None or trash == folder:
                    return {'error': '邮箱未提供可用的已删除文件夹，未执行删除'}
                if mail.uid('move', uid, trash)[0] != 'OK':
                    return {'error': '移动到已删除文件夹失败，请刷新检查'}
            else:
                return {'error': '邮箱不支持安全的单封删除，未执行删除'}
        return {'success': True, 'message': '邮件已删除'}
    except (OSError, imaplib.IMAP4.error, ValueError):
        return {'error': '删除失败或超时，请刷新检查'}
    finally:
        with CACHE_LOCK:
            MAIL_CACHE.pop(email_addr, None)
            IN_FLIGHT.discard(email_addr)
        FETCH_SLOTS.release()

@app.before_request
def protect_admin():
    if (request.path.startswith('/admin/') and request.path != '/admin/login') or request.path == '/users':
        if not session.get('admin'):
            return jsonify({'error': '请先登录管理后台'}), 401


@app.route('/admin/login', methods=['POST'])
def admin_login():
    import hmac
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get('password', '')).encode(), ADMIN_PASSWORD.encode()):
        return jsonify({'error': '管理员密码错误'}), 401
    session.clear()
    session['admin'] = True
    return jsonify({'success': True})


# ========== API 路由 ==========
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

def query_mailbox(data):
    def failure(message, status):
        return {'success': False, 'error': message, 'mails': [], 'total': 0}, status
    if not isinstance(data, dict):
        return failure('请提供 JSON 对象', 400)
    raw_email = data.get('email')
    if isinstance(raw_email, bool) or not isinstance(raw_email, (str, int)):
        return failure('请提供邮箱号或完整邮箱地址', 400)
    email_addr = str(raw_email).strip()
    if not email_addr or len(email_addr) > 254:
        return failure('请提供有效邮箱号', 400)
    if '@' not in email_addr:
        email_addr += '@qq.com'
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email_addr):
        return failure('邮箱格式不正确', 400)
    raw_limit = data.get('limit', 10)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, str)):
        return failure('limit 必须是 1 到 10 的整数', 400)
    try:
        limit = int(raw_limit)
    except ValueError:
        return failure('limit 必须是 1 到 10 的整数', 400)
    if not 1 <= limit <= 10:
        return failure('limit 必须是 1 到 10 的整数', 400)
    if email_addr not in ACCOUNTS:
        return failure('邮箱未绑定', 404)
    try:
        status = load_json(EMAIL_STATUS_FILE)
    except (OSError, ValueError):
        app.logger.warning('邮箱状态配置读取失败')
        return failure('服务配置暂时不可用，请稍后重试', 503)
    if status.get(email_addr) is False:
        return failure('该邮箱已被禁用，请联系管理员', 403)
    # 始终共享完整的十封缓存；仅在返回时按调用方 limit 截取。
    result = get_latest_mails(email_addr)
    if isinstance(result, dict):
        return failure(result.get('error', '查询失败'), 503)
    result = result[:limit]
    try:
        log_mails(email_addr, result)
    except (OSError, ValueError):
        app.logger.warning('日志写入失败，请检查文件权限或 JSON 内容')
    return {'success': True, 'email': email_addr, 'mails': result, 'total': len(result)}, 200


@app.route('/check', methods=['POST'])
def check():
    payload, status = query_mailbox(request.get_json(silent=True))
    # 保留原网页业务错误的 HTTP 200 行为，非法参数仍返回 400。
    return jsonify(payload), 400 if status == 400 else 200


@app.route('/api/mails', methods=['GET', 'POST'])
def api_mails():
    data = request.args.to_dict() if request.method == 'GET' else request.get_json(silent=True)
    payload, status = query_mailbox(data)
    response = jsonify(payload)
    response.status_code = status
    if status == 503:
        response.headers['Retry-After'] = '5'
    return response


@app.after_request
def private_response(response):
    if request.path in ('/check', '/api/mails', '/delete') or request.path.startswith('/admin'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/delete', methods=['POST'])
def delete():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请提供 JSON 对象'}), 400
    if not data:
        return jsonify({'error': '请提供 JSON 数据'})
    
    email_addr = str(data.get('email') or '').strip()
    mail_id = str(data.get('mail_id') or '').strip()
    password = str(data.get('password') or '').strip()
    
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    if password_hash != DELETE_PASSWORD_HASH:
        return jsonify({'error': '删除密码错误，无法删除'})
    
    if not email_addr:
        return jsonify({'error': '请提供邮箱地址'})
    
    if not mail_id:
        return jsonify({'error': '请提供邮件ID'})
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    result = delete_mail_by_id(email_addr, mail_id)
    
    if 'error' in result:
        return jsonify({'error': result['error']})
    
    return jsonify({'success': True, 'message': '邮件已删除'})

@app.route('/users', methods=['GET'])
def list_users():
    return jsonify({
        'total': len(ACCOUNTS),
        'users': list(ACCOUNTS.keys())
    })

@app.route('/health', methods=['GET'])
def health():
    return 'ok'

# ========== 管理后台路由 ==========
@app.route('/admin')
def admin():
    return send_from_directory(BASE_DIR, 'admin.html')

@app.route('/admin/emails')
def admin_emails():
    status = load_json(EMAIL_STATUS_FILE)
    emails = list(ACCOUNTS.keys())
    return jsonify({
        "emails": emails,
        "status": {email: status.get(email, True) for email in emails}
    })

@app.route('/admin/toggle', methods=['POST'])
def admin_toggle():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请提供 JSON 对象'}), 400
    email_addr = str(data.get('email') or '').strip()
    enabled = data.get('enabled', True)
    if not isinstance(enabled, bool):
        return jsonify({'error': 'enabled 必须为布尔值'}), 400
    
    if email_addr not in ACCOUNTS:
        return jsonify({'error': '邮箱不存在'})
    
    with DATA_LOCK:
        status = load_json(EMAIL_STATUS_FILE)
        status[email_addr] = enabled
        save_json(EMAIL_STATUS_FILE, status)
    return jsonify({'success': True})

@app.route('/admin/logs')
def admin_logs():
    logs = load_json(MAIL_LOG_FILE)
    log_list = logs.get("logs", [])
    log_list.sort(key=lambda x: x.get('time', ''), reverse=True)
    return jsonify({"logs": log_list})

@app.route('/admin/add', methods=['POST'])
def admin_add():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请提供 JSON 对象'}), 400
    email_addr = str(data.get('email') or '').strip()
    auth = str(data.get('auth') or '').strip()
    
    if not email_addr or not auth:
        return jsonify({'error': '请提供邮箱和授权码'})
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    if any(c in email_addr + auth for c in '\r\n'):
        return jsonify({'error': '邮箱和授权码不能包含换行'})
    try:
        with DATA_LOCK:
            with open(os.path.join(BASE_DIR, "accounts.txt"), "a", encoding="utf-8") as f:
                f.write(f"\n{email_addr} {auth}")
            ACCOUNTS[email_addr] = auth
        with CACHE_LOCK:
            MAIL_CACHE.pop(email_addr, None)
    except OSError:
        return jsonify({'error': '账号保存失败，请检查文件权限'})
    
    return jsonify({'success': True})

# ========== 启动服务 ==========
if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print("邮箱查询系统启动")
    print("=" * 60)
    print(f"[DEBUG] 已绑定 {len(ACCOUNTS)} 个邮箱")
    print(f"[DEBUG] 监听端口: {PORT}")
    print("=" * 60)
    app.run(host='0.0.0.0', port=PORT, debug=False), row)
        if match and b'\\trash' in match.group(1).lower().split():
            return match.group(2)
    return None


def current_capabilities(mail):
    """IMAP servers may advertise UIDPLUS/MOVE only after authentication."""
    status, rows = mail.capability()
    if status != 'OK':
        raise imaplib.IMAP4.error('无法获取邮箱功能列表')
    return set(b' '.join(row for row in rows or [] if isinstance(row, bytes)).upper().split())


def fetch_mails(email_addr, limit):
    mails = []
    with mail_session(email_addr) as mail:
        for folder in mail_folders(mail):
            status, count = mail.select(folder, readonly=True)
            if status != 'OK':
                if folder == b'INBOX':
                    raise imaplib.IMAP4.error('无法读取收件箱')
                continue
            total = int(count[0] or 0)
            if not total:
                continue
            validity = mail.response('UIDVALIDITY')[1][0]
            if not validity:
                raise imaplib.IMAP4.error('缺少 UIDVALIDITY')
            # 每个文件夹只取最后 limit 封；单封最多 128 KiB，避免附件拖垮查询。
            status, rows = mail.fetch(f'{max(1, total-limit+1)}:{total}',
                                      '(UID BODY.PEEK[]<0.131072>)')
            if status != 'OK':
                raise imaplib.IMAP4.error('邮件读取失败')
            for part in rows or []:
                if not isinstance(part, tuple):
                    continue
                uid = re.search(rb'UID (\d+)', part[0])
                if not uid:
                    continue
                msg = email.message_from_bytes(part[1])
                try:
                    dt = parsedate_to_datetime(msg.get('Date', ''))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(timezone(timedelta(hours=8)))
                    timestamp, send_time = dt.timestamp(), dt.strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError, OverflowError):
                    timestamp, send_time = 0, msg.get('Date', '')[:30]
                content = get_mail_content(msg)
                code = re.search(r'验证码：(\d{6})', content)
                mails.append(dict(mail_id=f'{folder_token(folder)}:{validity.decode()}:{uid[1].decode()}',
                                  sender=decode_str(msg.get('From', '未知发件人')),
                                  subject=decode_str(msg.get('Subject', '无主题')),
                                  content=content, code=code[1] if code else None,
                                  time=send_time, _timestamp=timestamp))
    mails.sort(key=lambda item: item['_timestamp'], reverse=True)
    for item in mails:
        item.pop('_timestamp')
    return mails[:limit]


def get_latest_mails(email_addr, limit=10):
    if email_addr not in ACCOUNTS:
        return {'error': '邮箱未绑定'}
    if not QUERY_SLOTS.acquire(blocking=False):
        return {'error': '当前查询人数较多，请稍后重试'}
    try:
        return shared_fetch(email_addr, limit)
    finally:
        QUERY_SLOTS.release()


def shared_fetch(email_addr, limit):
    with CACHE_LOCK:
        cached = MAIL_CACHE.get(email_addr)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        pending = PENDING_FETCHES.get(email_addr)
        owner = pending is None
        if owner:
            if email_addr in IN_FLIGHT:
                return {'error': '正在删除邮件，请稍后重试'}
            pending = {'event': threading.Event(), 'result': {'error': '查询未完成，请重试'}}
            PENDING_FETCHES[email_addr] = pending
            IN_FLIGHT.add(email_addr)
    if not owner:
        # 等待同一次查询结果，不重复连接邮箱；等待时不占用全局锁。
        if not pending['event'].wait(27):
            return {'error': '邮箱响应较慢，请稍后重试'}
        return pending['result']
    acquired = False
    result = {'error': '查询失败，请稍后重试'}
    try:
        acquired = FETCH_SLOTS.acquire(timeout=QUEUE_TIMEOUT)
        if not acquired:
            result = {'error': '查询排队超时，请稍后重试'}
        else:
            result = fetch_mails(email_addr, limit)
            with CACHE_LOCK:
                MAIL_CACHE[email_addr] = (time.monotonic(), result)
                MAIL_CACHE.move_to_end(email_addr)
                while len(MAIL_CACHE) > 128:
                    MAIL_CACHE.popitem(last=False)
        return result
    except (OSError, imaplib.IMAP4.error, ValueError):
        app.logger.warning('邮件查询失败或超时')
        result = {'error': '邮箱连接失败或超时，请稍后重试并检查授权码'}
        return result
    finally:
        if acquired:
            FETCH_SLOTS.release()
        with CACHE_LOCK:
            pending['result'] = result
            PENDING_FETCHES.pop(email_addr, None)
            IN_FLIGHT.discard(email_addr)
            pending['event'].set()


def delete_mail_by_id(email_addr, mail_id):
    if email_addr not in ACCOUNTS:
        return {'error': '邮箱未绑定'}
    try:
        token, validity, uid = mail_id.split(':')
        if not validity.isdigit() or not uid.isdigit():
            raise ValueError()
        folder = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
        if b'\r' in folder or b'\n' in folder:
            raise ValueError()
    except (ValueError, TypeError):
        return {'error': '邮件标识已更新，请刷新列表后再删除'}
    with CACHE_LOCK:
        if email_addr in IN_FLIGHT or not FETCH_SLOTS.acquire(blocking=False):
            return {'error': '正在处理邮件，请稍后重试'}
        IN_FLIGHT.add(email_addr)
    try:
        with mail_session(email_addr) as mail:
            if folder not in mail_folders(mail):
                return {'error': '文件夹无效，请刷新列表'}
            status, _ = mail.select(folder)
            current = mail.response('UIDVALIDITY')[1][0]
            if status != 'OK' or current != validity.encode():
                return {'error': '邮箱已变更，请刷新列表'}
            # UID EXPUNGE 只删除指定邮件，不清空其他客户端标记删除的邮件。
            if b'UIDPLUS' not in mail.capabilities:
                return {'error': '邮箱不支持安全的单封删除，未执行删除'}
            status, rows = mail.uid('search', None, 'UID', uid)
            if status != 'OK' or not rows or uid.encode() not in rows[0].split():
                return {'error': '邮件不存在，请刷新列表'}
            if mail.uid('store', uid, '+FLAGS.SILENT', r'(\Deleted)')[0] != 'OK':
                return {'error': '删除标记失败'}
            if mail.uid('expunge', uid)[0] != 'OK':
                return {'error': '删除未确认，请刷新检查'}
        return {'success': True, 'message': '邮件已删除'}
    except (OSError, imaplib.IMAP4.error, ValueError):
        return {'error': '删除失败或超时，请刷新检查'}
    finally:
        with CACHE_LOCK:
            MAIL_CACHE.pop(email_addr, None)
            IN_FLIGHT.discard(email_addr)
        FETCH_SLOTS.release()

@app.before_request
def protect_admin():
    if (request.path.startswith('/admin/') and request.path != '/admin/login') or request.path == '/users':
        if not session.get('admin'):
            return jsonify({'error': '请先登录管理后台'}), 401


@app.route('/admin/login', methods=['POST'])
def admin_login():
    import hmac
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not hmac.compare_digest(str(data.get('password', '')).encode(), ADMIN_PASSWORD.encode()):
        return jsonify({'error': '管理员密码错误'}), 401
    session.clear()
    session['admin'] = True
    return jsonify({'success': True})


# ========== API 路由 ==========
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

def query_mailbox(data):
    def failure(message, status):
        return {'success': False, 'error': message, 'mails': [], 'total': 0}, status
    if not isinstance(data, dict):
        return failure('请提供 JSON 对象', 400)
    raw_email = data.get('email')
    if isinstance(raw_email, bool) or not isinstance(raw_email, (str, int)):
        return failure('请提供邮箱号或完整邮箱地址', 400)
    email_addr = str(raw_email).strip()
    if not email_addr or len(email_addr) > 254:
        return failure('请提供有效邮箱号', 400)
    if '@' not in email_addr:
        email_addr += '@qq.com'
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email_addr):
        return failure('邮箱格式不正确', 400)
    raw_limit = data.get('limit', 10)
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, str)):
        return failure('limit 必须是 1 到 10 的整数', 400)
    try:
        limit = int(raw_limit)
    except ValueError:
        return failure('limit 必须是 1 到 10 的整数', 400)
    if not 1 <= limit <= 10:
        return failure('limit 必须是 1 到 10 的整数', 400)
    if email_addr not in ACCOUNTS:
        return failure('邮箱未绑定', 404)
    try:
        status = load_json(EMAIL_STATUS_FILE)
    except (OSError, ValueError):
        app.logger.warning('邮箱状态配置读取失败')
        return failure('服务配置暂时不可用，请稍后重试', 503)
    if status.get(email_addr) is False:
        return failure('该邮箱已被禁用，请联系管理员', 403)
    # 始终共享完整的十封缓存；仅在返回时按调用方 limit 截取。
    result = get_latest_mails(email_addr)
    if isinstance(result, dict):
        return failure(result.get('error', '查询失败'), 503)
    result = result[:limit]
    try:
        log_mails(email_addr, result)
    except (OSError, ValueError):
        app.logger.warning('日志写入失败，请检查文件权限或 JSON 内容')
    return {'success': True, 'email': email_addr, 'mails': result, 'total': len(result)}, 200


@app.route('/check', methods=['POST'])
def check():
    payload, status = query_mailbox(request.get_json(silent=True))
    # 保留原网页业务错误的 HTTP 200 行为，非法参数仍返回 400。
    return jsonify(payload), 400 if status == 400 else 200


@app.route('/api/mails', methods=['GET', 'POST'])
def api_mails():
    data = request.args.to_dict() if request.method == 'GET' else request.get_json(silent=True)
    payload, status = query_mailbox(data)
    response = jsonify(payload)
    response.status_code = status
    if status == 503:
        response.headers['Retry-After'] = '5'
    return response


@app.after_request
def private_response(response):
    if request.path in ('/check', '/api/mails', '/delete') or request.path.startswith('/admin'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/delete', methods=['POST'])
def delete():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请提供 JSON 对象'}), 400
    if not data:
        return jsonify({'error': '请提供 JSON 数据'})
    
    email_addr = str(data.get('email') or '').strip()
    mail_id = str(data.get('mail_id') or '').strip()
    password = str(data.get('password') or '').strip()
    
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    if password_hash != DELETE_PASSWORD_HASH:
        return jsonify({'error': '删除密码错误，无法删除'})
    
    if not email_addr:
        return jsonify({'error': '请提供邮箱地址'})
    
    if not mail_id:
        return jsonify({'error': '请提供邮件ID'})
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    result = delete_mail_by_id(email_addr, mail_id)
    
    if 'error' in result:
        return jsonify({'error': result['error']})
    
    return jsonify({'success': True, 'message': '邮件已删除'})

@app.route('/users', methods=['GET'])
def list_users():
    return jsonify({
        'total': len(ACCOUNTS),
        'users': list(ACCOUNTS.keys())
    })

@app.route('/health', methods=['GET'])
def health():
    return 'ok'

# ========== 管理后台路由 ==========
@app.route('/admin')
def admin():
    return send_from_directory(BASE_DIR, 'admin.html')

@app.route('/admin/emails')
def admin_emails():
    status = load_json(EMAIL_STATUS_FILE)
    emails = list(ACCOUNTS.keys())
    return jsonify({
        "emails": emails,
        "status": {email: status.get(email, True) for email in emails}
    })

@app.route('/admin/toggle', methods=['POST'])
def admin_toggle():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请提供 JSON 对象'}), 400
    email_addr = str(data.get('email') or '').strip()
    enabled = data.get('enabled', True)
    if not isinstance(enabled, bool):
        return jsonify({'error': 'enabled 必须为布尔值'}), 400
    
    if email_addr not in ACCOUNTS:
        return jsonify({'error': '邮箱不存在'})
    
    with DATA_LOCK:
        status = load_json(EMAIL_STATUS_FILE)
        status[email_addr] = enabled
        save_json(EMAIL_STATUS_FILE, status)
    return jsonify({'success': True})

@app.route('/admin/logs')
def admin_logs():
    logs = load_json(MAIL_LOG_FILE)
    log_list = logs.get("logs", [])
    log_list.sort(key=lambda x: x.get('time', ''), reverse=True)
    return jsonify({"logs": log_list})

@app.route('/admin/add', methods=['POST'])
def admin_add():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请提供 JSON 对象'}), 400
    email_addr = str(data.get('email') or '').strip()
    auth = str(data.get('auth') or '').strip()
    
    if not email_addr or not auth:
        return jsonify({'error': '请提供邮箱和授权码'})
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    if any(c in email_addr + auth for c in '\r\n'):
        return jsonify({'error': '邮箱和授权码不能包含换行'})
    try:
        with DATA_LOCK:
            with open(os.path.join(BASE_DIR, "accounts.txt"), "a", encoding="utf-8") as f:
                f.write(f"\n{email_addr} {auth}")
            ACCOUNTS[email_addr] = auth
        with CACHE_LOCK:
            MAIL_CACHE.pop(email_addr, None)
    except OSError:
        return jsonify({'error': '账号保存失败，请检查文件权限'})
    
    return jsonify({'success': True})

# ========== 启动服务 ==========
if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print("邮箱查询系统启动")
    print("=" * 60)
    print(f"[DEBUG] 已绑定 {len(ACCOUNTS)} 个邮箱")
    print(f"[DEBUG] 监听端口: {PORT}")
    print("=" * 60)
    app.run(host='0.0.0.0', port=PORT, debug=False)