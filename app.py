from flask import Flask, request, jsonify, send_from_directory
import imaplib
import email
import re
import html
import os
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
import hashlib
import json
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# ========== 删除密码配置 ==========
DELETE_PASSWORD = "112233"
DELETE_PASSWORD_HASH = hashlib.sha256(DELETE_PASSWORD.encode()).hexdigest()

# ========== 管理后台数据 ==========
EMAIL_STATUS_FILE = "email_status.json"
MAIL_LOG_FILE = "mail_log.json"

def load_json(file):
    try:
        with open(file, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_json(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_beijing_time():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

def log_mail(email, sender, subject, content, code):
    logs = load_json(MAIL_LOG_FILE)
    if "logs" not in logs:
        logs["logs"] = []
    
    beijing_time = get_beijing_time()
    time_str = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
    
    logs["logs"].append({
        "email": email,
        "sender": sender,
        "subject": subject,
        "code": code,
        "time": time_str
    })
    if len(logs["logs"]) > 1000:
        logs["logs"] = logs["logs"][-1000:]
    save_json(MAIL_LOG_FILE, logs)

# ========== 读取账号配置（带缓存，只读一次，静默加载） ==========
_ACCOUNTS_CACHE = None

def load_accounts():
    """加载账号，带缓存，只读一次，静默加载（防止刷屏拖死程序）"""
    global _ACCOUNTS_CACHE
    if _ACCOUNTS_CACHE is not None:
        return _ACCOUNTS_CACHE

    accounts = {}
    try:
        with open("accounts.txt", "r", encoding="utf-8") as f:
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

def get_latest_mails(email_addr, limit=10):
    if email_addr not in ACCOUNTS:
        return {'error': f'邮箱 "{email_addr}" 未绑定'}
    
    auth_code = ACCOUNTS[email_addr]
    mail = None
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email_addr, auth_code)
        
        all_mail_ids = []
        folder_info = []
        
        # 读取收件箱
        try:
            mail.select("INBOX")
            status, data = mail.search(None, "ALL")
            if data[0]:
                for mid in data[0].split():
                    all_mail_ids.append(mid)
                    folder_info.append("INBOX")
        except Exception as e:
            print(f"读取收件箱失败: {e}")
        
        # 读取垃圾箱
        spam_folders = ["垃圾箱", "广告邮件", "[Gmail]/Spam", "Spam", "Junk", "Junk Email"]
        for folder in spam_folders:
            try:
                mail.select(folder)
                status, data = mail.search(None, "ALL")
                if data[0]:
                    for mid in data[0].split():
                        all_mail_ids.append(mid)
                        folder_info.append(folder)
                break
            except:
                continue
        
        if not all_mail_ids:
            return []
        
        seen = set()
        unique_ids = []
        unique_folders = []
        for mid, folder in zip(all_mail_ids, folder_info):
            mid_str = mid.decode() if isinstance(mid, bytes) else str(mid)
            if mid_str not in seen:
                seen.add(mid_str)
                unique_ids.append(mid)
                unique_folders.append(folder)
        
        sorted_pairs = sorted(zip(unique_ids, unique_folders), key=lambda x: int(x[0]))
        latest_pairs = sorted_pairs[-limit:]
        
        mails = []
        
        for mail_id, folder in reversed(latest_pairs):
            try:
                mail_id_str = mail_id.decode() if isinstance(mail_id, bytes) else str(mail_id)
                
                mail.select(folder)
                _, msg_data = mail.fetch(mail_id, "(RFC822)")
                
                for part in msg_data:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])
                        
                        date_str = msg.get("Date", "")
                        send_time = ""
                        try:
                            if date_str:
                                dt = parsedate_to_datetime(date_str)
                                send_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except:
                            send_time = date_str[:30]
                        subject = decode_str(msg.get("Subject", "无主题"))
                        sender = decode_str(msg.get("From", "未知发件人"))
                        content = get_mail_content(msg)
                        
                        mails.append({
                            'mail_id': mail_id_str,
                            'sender': sender,
                            'subject': subject,
                            'content': content,
                            'time': send_time
                        })
                        break
            except Exception as e:
                print(f"读取单封邮件失败 (ID:{mail_id_str}, Folder:{folder}): {e}")
                continue
        
        return mails
        
    except Exception as e:
        return {'error': f'连接失败：{str(e)}'}
    
    finally:
        if mail:
            try:
                mail.close()
            except:
                pass
            try:
                mail.logout()
            except:
                pass

def delete_mail_by_id(email_addr, mail_id):
    if email_addr not in ACCOUNTS:
        return {'error': f'邮箱 "{email_addr}" 未绑定'}
    
    auth_code = ACCOUNTS[email_addr]
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email_addr, auth_code)
        mail.select("INBOX")
        mail.store(mail_id.encode(), '+FLAGS', '\\Deleted')
        mail.expunge()
        mail.close()
        mail.logout()
        return {'success': True, 'message': '邮件已删除'}
    except Exception as e:
        return {'error': f'删除失败：{str(e)}'}

# ========== API 路由 ==========
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/check', methods=['POST'])
def check():
    data = request.get_json()
    email_addr = data.get('email', '').strip()
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    status = load_json(EMAIL_STATUS_FILE)
    if status.get(email_addr) == False:
        return jsonify({'error': '该邮箱已被禁用，请联系管理员'})
    
    if email_addr not in ACCOUNTS:
        return jsonify({'error': '邮箱未绑定'})
    
    result = get_latest_mails(email_addr)
    
    if isinstance(result, list) and result:
        for mail in result:
            log_mail(email_addr, mail.get('sender'), mail.get('subject'), 
                    mail.get('content'), mail.get('code'))
    
    return jsonify({'success': True, 'mails': result, 'total': len(result) if isinstance(result, list) else 0})

@app.route('/delete', methods=['POST'])
def delete():
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供 JSON 数据'})
    
    email_addr = data.get('email', '').strip()
    mail_id = data.get('mail_id', '').strip()
    password = data.get('password', '').strip()
    
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
    return send_from_directory('.', 'admin.html')

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
    data = request.get_json()
    email_addr = data.get('email')
    enabled = data.get('enabled', True)
    
    if email_addr not in ACCOUNTS:
        return jsonify({'error': '邮箱不存在'})
    
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
    data = request.get_json()
    email_addr = data.get('email')
    auth = data.get('auth')
    
    if not email_addr or not auth:
        return jsonify({'error': '请提供邮箱和授权码'})
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    ACCOUNTS[email_addr] = auth
    
    try:
        with open("accounts.txt", "a", encoding="utf-8") as f:
            f.write(f"\n{email_addr} {auth}")
    except:
        pass
    
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
