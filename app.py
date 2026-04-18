from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import imaplib
import email
import re
import html
import os
import time
from email.header import decode_header
from email.utils import parsedate_to_datetime
import datetime

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# ========== 读取账号配置 ==========
def load_accounts():
    """读取 accounts.txt，支持一行3个邮箱+1个授权码"""
    accounts = {}
    try:
        with open("accounts.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split()
                    if len(parts) >= 4:
                        # 前3个是邮箱，最后一个是授权码
                        emails = parts[0:3]
                        auth_code = parts[3]
                        for email in emails:
                            if '@' not in email:
                                email = email + "@qq.com"
                            accounts[email] = auth_code
                    elif len(parts) == 2:
                        # 兼容 邮箱+授权码 格式
                        email = parts[0]
                        if '@' not in email:
                            email = email + "@qq.com"
                        accounts[email] = parts[1]
    except Exception as e:
        print(f"读取账号失败: {e}")
    return accounts

ACCOUNTS = load_accounts()
print(f"已加载 {len(ACCOUNTS)} 个绑定邮箱")

# ========== 邮件解析函数 ==========
def decode_str(s):
    """解码邮件标题、发件人"""
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
    """将HTML转换为纯文本"""
    if not html_text:
        return ""
    # 去除 style 和 script 标签
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # 把常见块级标签换成换行
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(div|p|tr|td|li|h[1-6])[^>]*>', '\n', text, flags=re.IGNORECASE)
    # 去掉所有其他HTML标签
    text = re.sub(r'<[^>]+>', ' ', text)
    # 解码HTML实体
    text = html.unescape(text)
    # 清理多余空白和换行
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()

def get_mail_content(msg):
    """获取邮件纯文本内容（完美处理HTML）"""
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
                            if content.strip():
                                break
                        except:
                            content = payload.decode('utf-8', errors='replace')
                            if content.strip():
                                break
                elif content_type == "text/html" and not content:
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            html_text = payload.decode(charset, errors='replace')
                            content = clean_html_to_text(html_text)
                        except:
                            html_text = payload.decode('utf-8', errors='replace')
                            content = clean_html_to_text(html_text)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                content_type = msg.get_content_type()
                charset = msg.get_content_charset() or 'utf-8'
                try:
                    text = payload.decode(charset, errors='replace')
                except:
                    text = payload.decode('utf-8', errors='replace')
                
                if content_type == "text/html":
                    content = clean_html_to_text(text)
                else:
                    content = text
    except Exception as e:
        content = f"解析失败: {e}"
    
    # 最后清理：合并多余换行和空格
    if content:
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = content[:2000]  # 限制长度，避免内容过多
    
    return content.strip() or "无法解析邮件内容"

def get_latest_mails(email_addr, limit=10):
    """获取最新N封邮件"""
    # 检查是否已绑定
    if email_addr not in ACCOUNTS:
        return {'error': f'邮箱 "{email_addr}" 未绑定，请联系管理员添加'}
    
    auth_code = ACCOUNTS[email_addr]
    
    try:
        mail = imaplib.IMAP4_SSL("imap.qq.com")
        mail.login(email_addr, auth_code)
        mail.select("INBOX")
        
        # 获取所有邮件ID
        status, data = mail.search(None, "ALL")
        mail_ids = data[0].split() if data[0] else []
        
        if not mail_ids:
            return []
        
        # 取最新的 limit 封
        latest_ids = mail_ids[-limit:]
        mails = []
        
        for mail_id in reversed(latest_ids):
            try:
                _, msg_data = mail.fetch(mail_id, "(RFC822)")
                for part in msg_data:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])
                        
                        # 获取时间
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
                        
                        # 提取验证码
                        code_match = re.search(r'验证码[：:]\s*(\d{4,8})', content)
                        code = code_match.group(1) if code_match else None
                        
                        # 如果没有验证码，尝试其他格式
                        if not code:
                            code_match = re.search(r'(\d{6})', content)
                            if code_match:
                                code = code_match.group(1)
                        
          mails.append({
                            'sender': sender,
                            'subject': subject,
                            'content': content,
                            'code': code,
                            'time': send_time
                        })
                        break
            except Exception as e:
                print(f"读取邮件失败: {e}")
                continue
        
        mail.close()
        mail.logout()
        return mails
        
    except imaplib.IMAP4.error as e:
        return {'error': f'登录失败：授权码错误或邮箱未开启IMAP'}
    except Exception as e:
        return {'error': f'连接失败：{str(e)}'}

# ========== API 路由 ==========
@app.route('/')
def index():
    """返回网页界面"""
    return send_from_directory('.', 'index.html')

@app.route('/check', methods=['POST'])
def check():
    """检查邮箱，接收 JSON: {"email": "xxx@qq.com"}"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供 JSON 数据'})
    
    email_addr = data.get('email', '').strip()
    
    if not email_addr:
        return jsonify({'error': '请输入邮箱地址'})
    
    # 补全邮箱格式
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

@app.route('/users', methods=['GET'])
def list_users():
    """列出所有已绑定的邮箱（用于调试）"""
    return jsonify({
        'total': len(ACCOUNTS),
        'users': list(ACCOUNTS.keys())
    })

@app.route('/health', methods=['GET'])
def health():
    """健康检查接口"""
    return jsonify({
        'status': 'ok',
        'bind_count': len(ACCOUNTS),
        'time': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

# ========== 启动服务 ==========
if __name__ == '__main__':
    print("=" * 60)
    print("📬 邮箱查询系统启动")
    print("=" * 60)
    print(f"✅ 已绑定 {len(ACCOUNTS)} 个邮箱")
    print(f"📧 绑定的邮箱列表:")
    for email in list(ACCOUNTS.keys())[:10]:
        print(f"   - {email}")
    if len(ACCOUNTS) > 10:
        print(f"   ... 共 {len(ACCOUNTS)} 个")
    print("\n🌐 访问地址:")
    print("   本地: http://127.0.0.1:5000")
    print("   局域网: http://你的IP:5000")
    print("\n💡 按 Ctrl+C 停止服务")
    print("=" * 60)
    app.run(host='0.0.0.0', port=5000, debug=False)
