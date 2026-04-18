from flask import Flask, request, jsonify, send_from_directory
import imaplib
import email
import re
import html
import os

app = Flask(__name__)

# 读取账号（支持一行3个邮箱+1个授权码）
def load_accounts():
    accounts = {}
    try:
        with open("accounts.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split()
                    if len(parts) >= 4:
                        emails = parts[0:3]
                        auth_code = parts[3]
                        for email in emails:
                            if '@' not in email:
                                email = email + "@qq.com"
                            accounts[email] = auth_code
    except Exception as e:
        print(f"读取账号失败: {e}")
    return accounts

ACCOUNTS = load_accounts()

def decode_str(s):
    if not s:
        return ""
    try:
        from email.header import decode_header
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

def clean_html(html_text):
    if not html_text:
        return ""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL)
    text = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_mail_content(msg):
    content = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                charset = part.get_content_charset() or 'utf-8'
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            content = payload.decode(charset, errors='replace')
                            break
                        except:
                            content = payload.decode('utf-8', errors='replace')
                            break
                elif content_type == "text/html" and not content:
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            html_text = payload.decode(charset, errors='replace')
                            content = clean_html(html_text)
                        except:
                            html_text = payload.decode('utf-8', errors='replace')
                            content = clean_html(html_text)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                content = payload.decode('utf-8', errors='replace')
    except:
        content = "解析失败"
    return content.strip()

def get_latest_mails(email_addr, limit=10):
    if email_addr not in ACCOUNTS:
        return {'error': f'邮箱 "{email_addr}" 未绑定'}
    
    auth_code = ACCOUNTS[email_addr]
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email_addr, auth_code)
        mail.select("INBOX")
        
        status, data = mail.search(None, "ALL")
        mail_ids = data[0].split() if data[0] else []
        
        if not mail_ids:
            return []
        
        latest_ids = mail_ids[-limit:]
        mails = []
        
        for mail_id in reversed(latest_ids):
            try:
                _, msg_data = mail.fetch(mail_id, "(RFC822)")
                for part in msg_data:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])
                        
                        subject = decode_str(msg.get("Subject", "无主题"))
                        sender = decode_str(msg.get("From", "未知发件人"))
                        content = get_mail_content(msg)
                        
                        code_match = re.search(r'验证码[：:]\s*(\d{4,8})', content)
                        code = code_match.group(1) if code_match else None
                        
                        mails.append({
                            'sender': sender,
                            'subject': subject,
                            'content': content,
                            'code': code
                        })
                        break
            except:
                continue
        
        mail.close()
        mail.logout()
        return mails
        
    except Exception as e:
        return {'error': str(e)}

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/check', methods=['POST'])
def check():
    data = request.get_json()
    email_addr = data.get('email', '').strip()
    
    if not email_addr:
        return jsonify({'error': '请输入邮箱地址'})
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    result = get_latest_mails(email_addr)
    
    if isinstance(result, dict) and 'error' in result:
        return jsonify({'error': result['error']})
    
    return jsonify({'success': True, 'email': email_addr, 'mails': result})

@app.route('/users')
def list_users():
    return jsonify({'users': list(ACCOUNTS.keys())})

if __name__ == '__main__':
    print("=" * 50)
    print("邮箱查询系统")
    print("=" * 50)
    print(f"\n已绑定 {len(ACCOUNTS)} 个邮箱")
    print("\n打开浏览器访问: http://127.0.0.1:5000")
    print("\n按 Ctrl+C 停止服务")
    app.run(host='0.0.0.0', port=5000)