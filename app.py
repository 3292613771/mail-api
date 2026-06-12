from flask import Flask, request, jsonify, send_from_directory
import imaplib
import email
import re
import html
import os
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
import datetime
import hashlib

app = Flask(__name__)

# ========== 删除密码配置 ==========
# 在这里设置删除密码（修改成你想要的密码）
DELETE_PASSWORD = "112233"  # 改成你自己的密码
# 存储密码的哈希值（用于验证）
DELETE_PASSWORD_HASH = hashlib.sha256(DELETE_PASSWORD.encode()).hexdigest()

# ========== 读取账号配置 ==========
def load_accounts():
    """读取账号配置，支持两种格式：
    1. 邮箱1 邮箱2 邮箱3 授权码（空格隔开）
    2. 邮箱----授权码（----隔开）
    """
    accounts = {}
    try:
        with open("accounts.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                # 判断格式：如果包含 "----" 就用新格式
                if "----" in line:
                    parts = line.split("----")
                    if len(parts) == 2:
                        email = parts[0].strip()
                        auth_code = parts[1].strip()
                        if '@' not in email:
                            email = email + "@qq.com"
                        accounts[email] = auth_code
                        print(f"加载账号（新格式）: {email}")
                else:
                    # 旧格式：空格隔开，前3个是邮箱，最后一个是授权码
                    parts = line.split()
                    if len(parts) >= 4:
                        emails = parts[0:3]
                        auth_code = parts[3]
                        for email in emails:
                            if '@' not in email:
                                email = email + "@qq.com"
                            accounts[email] = auth_code
                            print(f"加载账号（旧格式）: {email}")
                    elif len(parts) == 2:
                        email = parts[0]
                        auth_code = parts[1]
                        if '@' not in email:
                            email = email + "@qq.com"
                        accounts[email] = auth_code
                        print(f"加载账号（旧格式）: {email}")
    except Exception as e:
        print(f"读取账号失败: {e}")
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
    """获取邮件内容 - 超稳定版"""
    import re
    import html
    
    raw_text = ""
    
    try:
        # 方法1：遍历所有部分，收集所有文字
        if msg.is_multipart():
            for part in msg.walk():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        text = payload.decode(charset, errors='replace')
                    except:
                        text = payload.decode('utf-8', errors='replace')
                    
                    if text:
                        raw_text += text + "\n"
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                try:
                    raw_text = payload.decode(charset, errors='replace')
                except:
                    raw_text = payload.decode('utf-8', errors='replace')
        
        # 如果还是空，暴力提取
        if not raw_text:
            raw_text = str(msg)
        
        # 去掉HTML标签
        clean_text = re.sub(r'<[^>]+>', ' ', raw_text)
        # 解码HTML实体
        clean_text = html.unescape(clean_text)
        # 合并多余空白
        clean_text = re.sub(r'\s+', ' ', clean_text)
        
        # 提取验证码（重点：处理带空格的数字）
        code = None
        
        # 1. 找带空格的6位数字（Flova特色）
        code_match = re.search(r'(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)', clean_text)
        if code_match:
            code = code_match.group(1) + code_match.group(2) + code_match.group(3) + \
                   code_match.group(4) + code_match.group(5) + code_match.group(6)
        
        # 2. 没找到就找连续6位数字
        if not code:
            code_match = re.search(r'\b(\d{6})\b', clean_text)
            if code_match:
                code = code_match.group(1)
        
        # 3. 还没找到就找4-8位数字
        if not code:
            code_match = re.search(r'(\d{4,8})', clean_text)
            if code_match:
                code = code_match.group(1)
        
        # 4. 还是没找到，在原始内容中找
        if not code:
            code_match = re.search(r'(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)\s*(\d)', raw_text)
            if code_match:
                code = code_match.group(1) + code_match.group(2) + code_match.group(3) + \
                       code_match.group(4) + code_match.group(5) + code_match.group(6)
        
        # 返回结果
        if code and code != "000000":
            # 截取验证码附近的内容
            pos = clean_text.find(code)
            if pos >= 0:
                snippet = clean_text[max(0, pos-50):pos+100]
            else:
                snippet = clean_text[:300]
            return f"验证码：{code}\n\n{snippet}"
        else:
            # 没找到有效验证码，返回清理后的内容前500字
            return clean_text[:500] if clean_text else "无法解析邮件内容"
        
    except Exception as e:
        return f"解析失败：{str(e)}"

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
                mail_id_str = mail_id.decode() if isinstance(mail_id, bytes) else str(mail_id)
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
                            send_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        
                        subject = decode_str(msg.get("Subject", "无主题"))
                        sender = decode_str(msg.get("From", "未知发件人"))
                        content = get_mail_content(msg)
                        
                        code_match = re.search(r'验证码[：:]\s*(\d{4,8})', content)
                        code = code_match.group(1) if code_match else None
                        
                        if not code:
                            code_match = re.search(r'(\d{6})', content)
                            if code_match:
                                code = code_match.group(1)
                        
                        mails.append({
                            'mail_id': mail_id_str,
                            'sender': sender,
                            'subject': subject,
                            'content': content,
                            'code': code,
                            'time': send_time
            })
                        break
            except:
                continue
        
        mail.close()
        mail.logout()
        return mails
        
    except Exception as e:
        return {'error': f'连接失败：{str(e)}'}

def delete_mail_by_id(email_addr, mail_id):
    """删除指定邮件"""
    if email_addr not in ACCOUNTS:
        return {'error': f'邮箱 "{email_addr}" 未绑定'}
    
    auth_code = ACCOUNTS[email_addr]
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email_addr, auth_code)
        mail.select("INBOX")
        
        # 标记为删除
        mail.store(mail_id.encode(), '+FLAGS', '\\Deleted')
        # 永久删除
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
    if not data:
        return jsonify({'error': '请提供 JSON 数据'})
    
    email_addr = data.get('email', '').strip()
    
    if not email_addr:
        return jsonify({'error': '请输入邮箱地址'})
    
    if '@' not in email_addr:
        email_addr = email_addr + "@qq.com"
    
    result = get_latest_mails(email_addr)
    
    if isinstance(result, dict) and 'error' in result:
        return jsonify({'error': result['error']})
    
    return jsonify({
        'success': True,
        'email': email_addr,
        'mails': result,
        'total': len(result)
    })

@app.route('/delete', methods=['POST'])
def delete():
    """删除邮件接口（需要密码验证）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供 JSON 数据'})
    
    email_addr = data.get('email', '').strip()
    mail_id = data.get('mail_id', '').strip()
    password = data.get('password', '').strip()
    
    # 验证删除密码
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

# ========== 启动服务 ==========
if __name__ == '__main__':
    print("=" * 60)
    print("邮箱查询系统启动（支持删除邮件 + 删除密码保护）")
    print("=" * 60)
    print(f"已绑定 {len(ACCOUNTS)} 个邮箱")
    print(f"删除密码: {DELETE_PASSWORD}")
    print("访问 http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000)
