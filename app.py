import os
import sys
import json
import uuid
import time
import logging
import asyncio
import threading
import queue
import psutil
from threading import Lock
from flask import Flask, session, request, render_template, jsonify, redirect
from flask_socketio import SocketIO, emit, join_room, leave_room
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.sessions import StringSession

# تكوين السجلات المحسن
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('telegram_monitoring.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# إنشاء التطبيق
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", os.urandom(24))

# إعداد SocketIO
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=20, 
    ping_interval=10,
    logger=False, 
    engineio_logger=False
)

# إعدادات النظام
SESSIONS_DIR = "sessions"
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

USERS = {}
USERS_LOCK = Lock()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# بيانات Telegram API
API_ID = os.environ.get('TELEGRAM_API_ID')
API_HASH = os.environ.get('TELEGRAM_API_HASH')

if not API_ID or not API_HASH:
    logger.error("❌ يجب إضافة TELEGRAM_API_ID و TELEGRAM_API_HASH في متغيرات البيئة")

# =========================== 
# نظام Queue للتنبيهات المحسن
# ===========================
class AlertQueue:
    """نظام queue متقدم لإدارة التنبيهات"""
    
    def __init__(self):
        self.queue = queue.Queue()
        self.running = False
        self.thread = None
    
    def start(self):
        """بدء معالج التنبيهات"""
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._process_alerts, daemon=True)
            self.thread.start()
            logger.info("Alert queue processor started")
    
    def stop(self):
        """إيقاف معالج التنبيهات"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
    
    def add_alert(self, user_id, alert_data):
        """إضافة تنبيه جديد للقائمة"""
        try:
            self.queue.put({
                'user_id': user_id,
                'alert_data': alert_data,
                'timestamp': time.time()
            }, timeout=1)
        except queue.Full:
            logger.warning(f"Alert queue full for user {user_id}")
    
    def _process_alerts(self):
        """معالجة التنبيهات بشكل مستمر"""
        while self.running:
            try:
                alert = self.queue.get(timeout=1)
                self._send_alert(alert)
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing alert: {str(e)}")
    
    def _send_alert(self, alert):
        """إرسال التنبيه للمستخدم"""
        user_id = alert['user_id']
        alert_data = alert['alert_data']
        
        try:
            # إرسال للواجهة
            socketio.emit('keyword_alert', alert_data, to=user_id)
            socketio.emit('log_update', {
                "message": f"🚨 تنبيه فوري: '{alert_data['keyword']}' في {alert_data['group']}"
            }, to=user_id)
            
            # إرسال للرسائل المحفوظة
            self._send_to_saved_messages(user_id, alert_data)
            
        except Exception as e:
            logger.error(f"Failed to send alert for user {user_id}: {str(e)}")
    
    def _send_to_saved_messages(self, user_id, alert_data):
        """إرسال التنبيه للرسائل المحفوظة"""
        try:
            with USERS_LOCK:
                if user_id in USERS:
                    client_manager = USERS[user_id].get('client_manager')
                    if client_manager and client_manager.client:
                        notification_msg = f"""🚨 تنبيه فوري - مراقبة شاملة للحساب

📝 الكلمة المراقبة: {alert_data['keyword']}
📊 المصدر: {alert_data['group']}
👤 المرسل: {alert_data.get('sender', 'غير معروف')}
🕐 وقت الرسالة: {alert_data.get('message_time', '')}
🔗 معرف الرسالة: {alert_data.get('message_id', '')}

💬 نص الرسالة:
{alert_data.get('message', '')[:500]}{'...' if len(alert_data.get('message', '')) > 500 else ''}

--- تنبيه فوري من المراقبة الشاملة اللحظية لكامل الحساب"""
                        
                        # تشغيل في event loop الخاص بالعميل
                        if hasattr(client_manager, 'run_coroutine'):
                            client_manager.run_coroutine(
                                client_manager.client.send_message('me', notification_msg)
                            )
                        
                        logger.info(f"Alert sent to saved messages for user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send to saved messages: {str(e)}")

# إنشاء نظام التنبيهات العالمي
alert_queue = AlertQueue()

# =========================== 
# إدارة الجلسات والإعدادات
# ===========================
def save_settings(user_id, settings):
    """حفظ إعدادات المستخدم"""
    try:
        path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving settings for {user_id}: {str(e)}")
        return False

def load_settings(user_id):
    """تحميل إعدادات المستخدم"""
    try:
        path = os.path.join(SESSIONS_DIR, f"{user_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"Error loading settings for {user_id}: {str(e)}")
        return {}

def load_all_sessions():
    """تحميل جميع الجلسات الموجودة"""
    logger.info("Loading existing sessions...")
    session_count = 0
    
    with USERS_LOCK:
        try:
            for filename in os.listdir(SESSIONS_DIR):
                if filename.endswith('.json'):
                    user_id = filename.split('.')[0]
                    settings = load_settings(user_id)
                    
                    if settings and 'phone' in settings:
                        USERS[user_id] = {
                            'client_manager': None,
                            'settings': settings,
                            'thread': None,
                            'is_running': False,
                            'stats': {"sent": 0, "errors": 0},
                            'connected': False,
                            'authenticated': False,
                            'awaiting_code': False,
                            'awaiting_password': False,
                            'phone_code_hash': None,
                            'monitoring_active': False,
                            'event_handlers_registered': False
                        }
                        session_count += 1
                        logger.info(f"✓ Loaded session for {user_id}")
        
        except Exception as e:
            logger.error(f"Error loading sessions: {str(e)}")
    
    logger.info(f"Loaded {session_count} sessions successfully")
    return session_count

# =========================== 
# مدير التليجرام المحسن مع Event Handlers
# ===========================
class TelegramClientManager:
    """مدير عملاء التليجرام المحسن مع Event Handlers"""
    
    def __init__(self, user_id):
        self.user_id = user_id
        self.client = None
        self.loop = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.is_ready = threading.Event()
        self.event_handlers_registered = False
        self.monitored_keywords = []
        self.monitored_groups = []
    
    def start_client_thread(self):
        """بدء thread منفصل للعميل"""
        if self.thread and self.thread.is_alive():
            return
        
        self.stop_flag.clear()
        self.is_ready.clear()
        self.thread = threading.Thread(target=self._run_client_loop, daemon=True)
        self.thread.start()
        
        # انتظار حتى يصبح العميل جاهزاً
        if not self.is_ready.wait(timeout=30):
            raise Exception("Client initialization timeout")
    
    def _run_client_loop(self):
        """تشغيل event loop للعميل"""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            
            session_file = os.path.join(SESSIONS_DIR, f"{self.user_id}_session.session")
            if API_ID and API_HASH:
                self.client = TelegramClient(session_file, int(API_ID), API_HASH)
            else:
                logger.error("API_ID or API_HASH not set")
                return
            
            self.loop.run_until_complete(self._client_main())
        
        except Exception as e:
            logger.error(f"Client thread error for {self.user_id}: {str(e)}")
        finally:
            if self.loop:
                self.loop.close()
    
    async def _client_main(self):
        """الوظيفة الرئيسية للعميل"""
        try:
            if self.client:
                await self.client.connect()
                self.is_ready.set()
                
                # تسجيل event handlers
                await self._register_event_handlers()
                
                # الحفاظ على الاتصال
                while not self.stop_flag.is_set():
                    await asyncio.sleep(1)
        
        except Exception as e:
            logger.error(f"Client main error: {str(e)}")
        finally:
            if self.client:
                await self.client.disconnect()
    
    async def _register_event_handlers(self):
        """تسجيل event handlers للرسائل الجديدة"""
        try:
            if self.event_handlers_registered or not self.client:
                return
            
            @self.client.on(events.NewMessage)
            async def new_message_handler(event):
                await self._handle_new_message(event)
            
            self.event_handlers_registered = True
            logger.info(f"Event handlers registered for user {self.user_id}")
            
        except Exception as e:
            logger.error(f"Failed to register event handlers: {str(e)}")
    
    async def _handle_new_message(self, event):
        """معالجة الرسائل الجديدة الواردة - مراقبة شاملة لكامل الحساب"""
        try:
            message = event.message
            if not message.text:
                return
            
            # الحصول على معلومات المحادثة
            chat = await event.get_chat()
            chat_username = getattr(chat, 'username', None)
            chat_title = getattr(chat, 'title', None)
            
            # تحديد معرف المجموعة/المحادثة
            group_identifier = None
            if chat_username:
                group_identifier = f"@{chat_username}"
            elif chat_title:
                group_identifier = chat_title
            elif hasattr(chat, 'first_name'):
                # محادثة شخصية
                group_identifier = f"محادثة مع {chat.first_name}"
            else:
                group_identifier = f"محادثة {chat.id}"
            
            # ⚠️ إزالة فحص المجموعات المحددة - مراقبة شاملة لكل شيء
            # مراقبة كامل المجموعات والمحادثات بدون استثناء
            
            # فحص الكلمات المفتاحية في كل رسالة
            if self.monitored_keywords:  # إذا كان هناك كلمات مراقبة
                message_lower = message.text.lower()
                for keyword in self.monitored_keywords:
                    keyword_lower = keyword.lower().strip()
                    if keyword_lower and keyword_lower in message_lower:
                        await self._trigger_keyword_alert(message, keyword, group_identifier, event)
            else:
                # إذا لم تكن هناك كلمات محددة، راقب كل الرسائل
                await self._trigger_keyword_alert(message, "رسالة جديدة", group_identifier, event)
        
        except Exception as e:
            logger.error(f"Error handling new message: {str(e)}")
    
    async def _trigger_keyword_alert(self, message, keyword, group_identifier, event):
        """تشغيل تنبيه الكلمة المفتاحية"""
        try:
            # الحصول على معلومات المرسل
            sender_name = "غير معروف"
            try:
                sender = await event.get_sender()
                if sender:
                    sender_name = getattr(sender, 'first_name', '') or getattr(sender, 'username', '') or str(sender.id)
            except:
                pass
            
            # إنشاء بيانات التنبيه
            alert_data = {
                "keyword": keyword,
                "group": group_identifier,
                "message": message.text[:200] + "..." if len(message.text) > 200 else message.text,
                "timestamp": time.strftime('%H:%M:%S'),
                "sender": sender_name,
                "message_time": time.strftime('%H:%M:%S', time.localtime(message.date.timestamp())),
                "message_id": message.id,
                "full_message": message.text
            }
            
            # إضافة التنبيه للقائمة
            alert_queue.add_alert(self.user_id, alert_data)
            
            logger.info(f"Keyword alert triggered for user {self.user_id}: '{keyword}' in {group_identifier}")
            
        except Exception as e:
            logger.error(f"Error triggering keyword alert: {str(e)}")
    
    def update_monitoring_settings(self, keywords, groups):
        """تحديث إعدادات المراقبة - فقط الكلمات المفتاحية (المجموعات للإرسال فقط)"""
        self.monitored_keywords = [k.strip() for k in keywords if k.strip()]
        # ⚠️ لا نحفظ مجموعات المراقبة - نراقب كل شيء
        # نحفظ مجموعات الإرسال منفصلة في الإعدادات العادية
        
        logger.info(f"Updated monitoring settings for {self.user_id}: {len(self.monitored_keywords)} keywords - مراقبة شاملة لكامل الحساب")
    
    def run_coroutine(self, coro):
        """تشغيل coroutine في event loop الخاص بالعميل"""
        if not self.loop:
            raise Exception("Event loop not initialized")
        
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=30)
    
    def stop(self):
        """إيقاف العميل"""
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=5)

# =========================== 
# مدير التليجرام الرئيسي
# ===========================
class TelegramManager:
    """مدير عملاء التليجرام"""
    
    def __init__(self):
        self.client_managers = {}
    
    def get_client_manager(self, user_id):
        """الحصول على مدير العميل للمستخدم"""
        if user_id not in self.client_managers:
            self.client_managers[user_id] = TelegramClientManager(user_id)
        return self.client_managers[user_id]
    
    def setup_client(self, user_id, phone_number):
        """إعداد عميل التليجرام"""
        try:
            if not API_ID or not API_HASH:
                socketio.emit('log_update', {
                    "message": "❌ لم يتم إعداد بيانات Telegram API"
                }, to=user_id)
                return {
                    "status": "error", 
                    "message": "❌ بيانات API غير متوفرة - يرجى إضافة TELEGRAM_API_ID و TELEGRAM_API_HASH في الأسرار"
                }
            
            socketio.emit('log_update', {
                "message": "🔄 جاري إعداد العميل..."
            }, to=user_id)
            
            client_manager = self.get_client_manager(user_id)
            client_manager.start_client_thread()
            
            socketio.emit('log_update', {
                "message": "📡 فحص حالة التصريح..."
            }, to=user_id)
            
            is_authorized = client_manager.run_coroutine(
                client_manager.client.is_user_authorized()
            )
            
            if not is_authorized:
                socketio.emit('log_update', {
                    "message": f"📱 إرسال كود التحقق إلى: {phone_number}"
                }, to=user_id)
                
                sent = client_manager.run_coroutine(
                    client_manager.client.send_code_request(phone_number)
                )
                
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['awaiting_code'] = True
                        USERS[user_id]['phone_code_hash'] = sent.phone_code_hash
                        USERS[user_id]['client_manager'] = client_manager
                        USERS[user_id]['connected'] = True
                
                # إرسال إشعار تحديث حالة تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": False,
                    "connected": True,
                    "awaiting_code": True,
                    "awaiting_password": False,
                    "is_running": False
                }, to=user_id)
                
                socketio.emit('log_update', {
                    "message": "✅ تم إرسال كود التحقق - تحقق من رسائل تيليجرام"
                }, to=user_id)
                
                return {
                    "status": "code_required", 
                    "message": "📱 تم إرسال كود التحقق"
                }
            else:
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['client_manager'] = client_manager
                        USERS[user_id]['connected'] = True
                        USERS[user_id]['authenticated'] = True
                        USERS[user_id]['awaiting_code'] = False
                        USERS[user_id]['awaiting_password'] = False
                
                # إرسال إشعار نجح تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": True,
                    "connected": True,
                    "awaiting_code": False,
                    "awaiting_password": False,
                    "is_running": False
                }, to=user_id)
                
                socketio.emit('connection_status', {
                    "status": "connected"
                }, to=user_id)
                
                return {"status": "success", "message": "✅ تم تسجيل الدخول"}
        
        except Exception as e:
            logger.error(f"Setup error for {user_id}: {str(e)}")
            socketio.emit('log_update', {
                "message": f"❌ خطأ في الإعداد: {str(e)}"
            }, to=user_id)
            return {"status": "error", "message": f"❌ خطأ: {str(e)}"}
    
    def verify_code(self, user_id, code):
        """التحقق من كود التحقق"""
        try:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id].get('awaiting_code'):
                    return {"status": "error", "message": "❌ لم يتم طلب كود التحقق"}
                
                client_manager = USERS[user_id].get('client_manager')
                phone_code_hash = USERS[user_id].get('phone_code_hash')
                phone = USERS[user_id]['settings']['phone']
            
            if not client_manager or not phone_code_hash:
                return {"status": "error", "message": "❌ بيانات الجلسة مفقودة"}
            
            try:
                user = client_manager.run_coroutine(
                    client_manager.client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                )
                
                with USERS_LOCK:
                    USERS[user_id]['connected'] = True
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['awaiting_code'] = False
                    USERS[user_id]['awaiting_password'] = False
                
                # إرسال تحديث حالة تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": True,
                    "connected": True,
                    "awaiting_code": False,
                    "awaiting_password": False,
                    "is_running": False
                }, to=user_id)
                
                socketio.emit('connection_status', {
                    "status": "connected"
                }, to=user_id)
                
                return {"status": "success", "message": "✅ تم التحقق بنجاح"}
            
            except SessionPasswordNeededError:
                with USERS_LOCK:
                    USERS[user_id]['awaiting_code'] = False
                    USERS[user_id]['awaiting_password'] = True
                
                # إرسال تحديث حالة تسجيل الدخول
                socketio.emit('login_status', {
                    "logged_in": False,
                    "connected": True,
                    "awaiting_code": False,
                    "awaiting_password": True,
                    "is_running": False
                }, to=user_id)
                
                return {
                    "status": "password_required", 
                    "message": "🔒 يرجى إدخال كلمة مرور التحقق بخطوتين"
                }
        
        except PhoneCodeInvalidError:
            return {"status": "error", "message": "❌ كود التحقق غير صحيح"}
        except PhoneCodeExpiredError:
            return {"status": "error", "message": "❌ انتهت صلاحية كود التحقق"}
        except Exception as e:
            logger.error(f"Code verification error: {str(e)}")
            return {"status": "error", "message": f"❌ خطأ: {str(e)}"}
    
    def verify_password(self, user_id, password):
        """التحقق من كلمة المرور"""
        try:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id].get('awaiting_password'):
                    return {"status": "error", "message": "❌ لم يتم طلب كلمة المرور"}
                
                client_manager = USERS[user_id].get('client_manager')
            
            if not client_manager:
                return {"status": "error", "message": "❌ بيانات الجلسة مفقودة"}
            
            try:
                await_result = client_manager.run_coroutine(
                    client_manager.client.sign_in(password=password)
                )
                
                with USERS_LOCK:
                    USERS[user_id]['connected'] = True
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['awaiting_password'] = False
                
                return {"status": "success", "message": "✅ تم التحقق بنجاح"}
            
            except PasswordHashInvalidError:
                return {"status": "error", "message": "❌ كلمة المرور غير صحيحة"}
        
        except Exception as e:
            logger.error(f"Password verification error: {str(e)}")
            return {"status": "error", "message": f"❌ خطأ: {str(e)}"}
    
    def send_message_async(self, user_id, entity, message):
        """إرسال رسالة"""
        try:
            with USERS_LOCK:
                if user_id not in USERS:
                    raise Exception("المستخدم غير موجود")
                
                client_manager = USERS[user_id].get('client_manager')
            
            if not client_manager:
                raise Exception("العميل غير متصل")
            
            is_authorized = client_manager.run_coroutine(
                client_manager.client.is_user_authorized()
            )
            
            if not is_authorized:
                raise Exception("العميل غير مصرح")
            
            try:
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )
            except:
                if not entity.startswith('@') and not entity.startswith('https://'):
                    entity = '@' + entity
                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(entity)
                )
            
            result = client_manager.run_coroutine(
                client_manager.client.send_message(entity_obj, message)
            )
            
            return {"success": True, "message_id": result.id}
        
        except Exception as e:
            logger.error(f"Send message error: {str(e)}")
            raise Exception(str(e))

# إنشاء مدير التليجرام
telegram_manager = TelegramManager()

# =========================== 
# نظام المراقبة المحسن مع Event Handlers
# ===========================
def monitoring_worker(user_id):
    """مهمة المراقبة المحسنة مع Event Handlers"""
    logger.info(f"Starting enhanced monitoring worker with event handlers for user {user_id}")
    
    try:
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['monitoring_active'] = True
                client_manager = USERS[user_id].get('client_manager')
                settings = USERS[user_id]['settings']
        
        if not client_manager:
            logger.error(f"No client manager for user {user_id}")
            return
        
        # تحديث إعدادات المراقبة في العميل
        watch_words = settings.get('watch_words', [])
        send_groups = settings.get('groups', [])  # مجموعات الإرسال فقط
        
        if hasattr(client_manager, 'update_monitoring_settings'):
            client_manager.update_monitoring_settings(watch_words, send_groups)
        
        # إرسال إشعار بدء المراقبة
        if watch_words:
            socketio.emit('log_update', {
                "message": f"🚀 بدأت المراقبة الشاملة الفورية - {len(watch_words)} كلمة مراقبة في كامل الحساب | الإرسال لـ {len(send_groups)} مجموعة"
            }, to=user_id)
        else:
            socketio.emit('log_update', {
                "message": f"🚀 بدأت المراقبة الشاملة لكامل الرسائل في الحساب | الإرسال لـ {len(send_groups)} مجموعة"
            }, to=user_id)
        
        # الحفاظ على المراقبة نشطة
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while True:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id]['is_running']:
                    logger.info(f"Stopping monitoring for user {user_id}")
                    break
                
                user_data = USERS[user_id].copy()
                USERS[user_id]['monitoring_active'] = True
            
            try:
                # تنفيذ الإرسال المجدول إذا كان مطلوب
                settings = user_data['settings']
                send_type = settings.get('send_type', 'manual')
                current_time = time.time()
                
                if send_type == 'scheduled':
                    interval_seconds = int(settings.get('interval_seconds', 3600))
                    last_send = user_data.get('last_scheduled_send', 0)
                    
                    if current_time - last_send >= interval_seconds:
                        logger.info(f"Executing scheduled send for user {user_id}")
                        execute_scheduled_messages(user_id, settings)
                        
                        with USERS_LOCK:
                            if user_id in USERS:
                                USERS[user_id]['last_scheduled_send'] = current_time
                
                consecutive_errors = 0
                
                # إرسال إشارة حياة
                status_info = {
                    'timestamp': time.strftime('%H:%M:%S'),
                    'status': 'active',
                    'type': 'event_driven_monitoring',
                    'keywords_active': bool(watch_words),
                    'event_handlers': True
                }
                
                socketio.emit('heartbeat', status_info, to=user_id)
            
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Monitoring cycle error for {user_id}: {str(e)}")
                
                socketio.emit('log_update', {
                    "message": f"⚠️ خطأ في المراقبة: {str(e)[:100]}"
                }, to=user_id)
                
                if consecutive_errors >= max_consecutive_errors:
                    socketio.emit('log_update', {
                        "message": f"❌ تم إيقاف المراقبة بسبب تكرار الأخطاء ({consecutive_errors})"
                    }, to=user_id)
                    break
            
            # فترة انتظار مناسبة
            time.sleep(10)
    
    except Exception as e:
        logger.error(f"Monitoring worker error for {user_id}: {str(e)}")
    finally:
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['is_running'] = False
                USERS[user_id]['monitoring_active'] = False
                USERS[user_id]['thread'] = None
        
        socketio.emit('log_update', {
            "message": "⏹ تم إيقاف نظام المراقبة المحسن"
        }, to=user_id)
        
        socketio.emit('heartbeat', {
            'timestamp': time.strftime('%H:%M:%S'),
            'status': 'stopped'
        }, to=user_id)
        
        logger.info(f"Enhanced monitoring worker ended for user {user_id}")

def execute_scheduled_messages(user_id, settings):
    """تنفيذ الإرسال المجدول"""
    groups = settings.get('groups', [])
    message = settings.get('message', '')
    
    if not groups or not message:
        return
    
    try:
        socketio.emit('log_update', {
            "message": f"📅 تنفيذ الإرسال المجدول إلى {len(groups)} مجموعة"
        }, to=user_id)
        
        successful = 0
        failed = 0
        
        for i, group in enumerate(groups, 1):
            try:
                result = telegram_manager.send_message_async(user_id, group, message)
                
                socketio.emit('log_update', {
                    "message": f"✅ [{i}/{len(groups)}] إرسال مجدول نجح إلى: {group}"
                }, to=user_id)
                
                successful += 1
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['stats']['sent'] += 1
                
                if i < len(groups):
                    time.sleep(3)
            
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Scheduled send error to {group}: {error_msg}")
                
                socketio.emit('log_update', {
                    "message": f"❌ [{i}/{len(groups)}] إرسال مجدول فشل إلى {group}"
                }, to=user_id)
                
                failed += 1
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['stats']['errors'] += 1
        
        socketio.emit('log_update', {
            "message": f"📊 انتهى الإرسال المجدول: ✅ {successful} نجح | ❌ {failed} فشل"
        }, to=user_id)
        
    except Exception as e:
        logger.error(f"Scheduled messages error: {str(e)}")

# =========================== 
# أحداث Socket.IO
# ===========================
@socketio.on('connect')
def handle_connect():
    if 'user_id' in session:
        user_id = session['user_id']
        join_room(user_id)
        logger.info(f"User {user_id} connected via socket")
        
        # إرسال حالة الاتصال فوراً
        with USERS_LOCK:
            if user_id in USERS:
                connected = USERS[user_id].get('connected', False)
                authenticated = USERS[user_id].get('authenticated', False)
                awaiting_code = USERS[user_id].get('awaiting_code', False)
                awaiting_password = USERS[user_id].get('awaiting_password', False)
                is_running = USERS[user_id].get('is_running', False)
                
                emit('connection_status', {
                    "status": "connected" if connected else "disconnected"
                })
                
                emit('login_status', {
                    "logged_in": authenticated,
                    "connected": connected,
                    "awaiting_code": awaiting_code,
                    "awaiting_password": awaiting_password,
                    "is_running": is_running
                })
        
        emit('console_log', {
            "message": f"[{time.strftime('%H:%M:%S')}] INFO: Socket connected"
        })
        
        # إرسال رسالة ترحيب
        emit('log_update', {
            "message": f"🔄 تم الاتصال بالخادم - {time.strftime('%H:%M:%S')}"
        })

@socketio.on('disconnect')
def handle_disconnect():
    if 'user_id' in session:
        user_id = session['user_id']
        leave_room(user_id)
        logger.info(f"User {user_id} disconnected from socket")

# =========================== 
# قوالب HTML مدمجة
# ===========================
INDEX_HTML = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        body {
            background-color: #f8f9fa;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        .navbar-brand {
            font-weight: bold;
            font-size: 1.5rem;
        }
        .card {
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            margin-bottom: 20px;
        }
        .card-header {
            background-color: #0d6efd;
            color: white;
            border-radius: 10px 10px 0 0 !important;
        }
        .btn-primary {
            background-color: #0d6efd;
            border-color: #0d6efd;
        }
        .connection-status {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 5px;
        }
        .connected {
            background-color: #28a745;
        }
        .disconnected {
            background-color: #dc3545;
        }
        .log-container {
            height: 300px;
            overflow-y: auto;
            background-color: #1a1a1a;
            color: #00ff00;
            padding: 10px;
            border-radius: 5px;
            font-family: monospace;
        }
        .alert-item {
            border-left: 4px solid #dc3545;
            padding: 10px;
            margin-bottom: 10px;
            background-color: #f8d7da;
            border-radius: 5px;
        }
        .form-control, .form-select {
            border-radius: 5px;
        }
        .tab-content {
            padding: 20px;
            border: 1px solid #dee2e6;
            border-top: none;
            border-radius: 0 0 10px 10px;
        }
        .nav-tabs .nav-link.active {
            background-color: #0d6efd;
            color: white;
            border-radius: 5px 5px 0 0;
        }
        .stats-box {
            text-align: center;
            padding: 15px;
            border-radius: 10px;
            background-color: #e9ecef;
            margin-bottom: 15px;
        }
        .stats-number {
            font-size: 24px;
            font-weight: bold;
            color: #0d6efd;
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-primary">
        <div class="container">
            <a class="navbar-brand" href="#">
                <i class="fas fa-graduation-cap me-2"></i>
                مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية
            </a>
            <div class="d-flex">
                <a href="https://wa.me/+966510349663" class="btn btn-light me-2">
                    <i class="fab fa-whatsapp me-1"></i> واتساب
                </a>
                <a href="/admin" class="btn btn-outline-light">
                    <i class="fas fa-cog me-1"></i> الإدارة
                </a>
            </div>
        </div>
    </nav>

    <div class="container mt-4">
        <div class="row">
            <div class="col-md-12">
                <div class="card">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <h5 class="mb-0"><i class="fas fa-sliders-h me-2"></i>لوحة التحكم الرئيسية</h5>
                        <div id="connectionStatus">
                            <span class="connection-status disconnected"></span>
                            <span id="statusText">غير متصل</span>
                        </div>
                    </div>
                    <div class="card-body">
                        <ul class="nav nav-tabs" id="myTab" role="tablist">
                            <li class="nav-item" role="presentation">
                                <button class="nav-link active" id="login-tab" data-bs-toggle="tab" data-bs-target="#login" type="button" role="tab">تسجيل الدخول</button>
                            </li>
                            <li class="nav-item" role="presentation">
                                <button class="nav-link" id="settings-tab" data-bs-toggle="tab" data-bs-target="#settings" type="button" role="tab">الإعدادات</button>
                            </li>
                            <li class="nav-item" role="presentation">
                                <button class="nav-link" id="monitor-tab" data-bs-toggle="tab" data-bs-target="#monitor" type="button" role="tab">المراقبة</button>
                            </li>
                            <li class="nav-item" role="presentation">
                                <button class="nav-link" id="logs-tab" data-bs-toggle="tab" data-bs-target="#logs" type="button" role="tab">السجلات</button>
                            </li>
                        </ul>
                        
                        <div class="tab-content" id="myTabContent">
                            <!-- تسجيل الدخول -->
                            <div class="tab-pane fade show active" id="login" role="tabpanel">
                                <div class="row">
                                    <div class="col-md-6">
                                        <div class="card">
                                            <div class="card-header bg-info text-white">
                                                <i class="fas fa-sign-in-alt me-2"></i>تسجيل الدخول إلى Telegram
                                            </div>
                                            <div class="card-body">
                                                <div id="loginForm">
                                                    <div class="mb-3">
                                                        <label for="phone" class="form-label">رقم الهاتف</label>
                                                        <input type="text" class="form-control" id="phone" placeholder="+966XXXXXXXXX">
                                                    </div>
                                                    <div class="mb-3">
                                                        <label for="password" class="form-label">كلمة المرور (اختياري)</label>
                                                        <input type="password" class="form-control" id="password" placeholder="كلمة مرور التحقق بخطوتين">
                                                    </div>
                                                    <button id="loginBtn" class="btn btn-primary w-100">
                                                        <i class="fas fa-paper-plane me-2"></i>إرسال كود التحقق
                                                    </button>
                                                </div>
                                                
                                                <div id="codeForm" style="display: none;">
                                                    <div class="mb-3">
                                                        <label for="code" class="form-label">كود التحقق</label>
                                                        <input type="text" class="form-control" id="code" placeholder="أدخل الكود المرسل إليك">
                                                    </div>
                                                    <button id="verifyCodeBtn" class="btn btn-success w-100">
                                                        <i class="fas fa-check-circle me-2"></i>تحقق من الكود
                                                    </button>
                                                </div>
                                                
                                                <div id="passwordForm" style="display: none;">
                                                    <div class="mb-3">
                                                        <label for="twoFactorPassword" class="form-label">كلمة مرور التحقق بخطوتين</label>
                                                        <input type="password" class="form-control" id="twoFactorPassword" placeholder="أدخل كلمة المرور">
                                                    </div>
                                                    <button id="verifyPasswordBtn" class="btn btn-success w-100">
                                                        <i class="fas fa-check-circle me-2"></i>تحقق من كلمة المرور
                                                    </button>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                    
                                    <div class="col-md-6">
                                        <div class="card">
                                            <div class="card-header bg-info text-white">
                                                <i class="fas fa-info-circle me-2"></i>حالة الاتصال
                                            </div>
                                            <div class="card-body">
                                                <div id="loginStatus">
                                                    <p class="text-center">لم يتم تسجيل الدخول بعد</p>
                                                </div>
                                                <div class="d-grid gap-2">
                                                    <button id="resetLoginBtn" class="btn btn-warning">
                                                        <i class="fas fa-sync-alt me-2"></i>إعادة تعيين الجلسة
                                                    </button>
                                                    <button id="logoutBtn" class="btn btn-danger">
                                                        <i class="fas fa-sign-out-alt me-2"></i>تسجيل الخروج
                                                    </button>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            
                            <!-- الإعدادات -->
                            <div class="tab-pane fade" id="settings" role="tabpanel">
                                <div class="row">
                                    <div class="col-md-6">
                                        <div class="card">
                                            <div class="card-header bg-success text-white">
                                                <i class="fas fa-cog me-2"></i>إعدادات الرسائل
                                            </div>
                                            <div class="card-body">
                                                <div class="mb-3">
                                                    <label for="message" class="form-label">نص الرسالة</label>
                                                    <textarea class="form-control" id="message" rows="5" placeholder="اكتب نص الرسالة هنا..."></textarea>
                                                </div>
                                                <div class="mb-3">
                                                    <label for="groups" class="form-label">المجموعات/القنوات (كل مجموعة في سطر)</label>
                                                    <textarea class="form-control" id="groups" rows="5" placeholder="@group1&#10;@group2&#10;https://t.me/group3"></textarea>
                                                </div>
                                                <div class="mb-3">
                                                    <label for="watchWords" class="form-label">الكلمات المراقبة (كل كلمة في سطر)</label>
                                                    <textarea class="form-control" id="watchWords" rows="3" placeholder="كلمة1&#10;كلمة2&#10;كلمة3"></textarea>
                                                    <div class="form-text">اتركها فارغة لمراقبة كل الرسائل</div>
                                                </div>
                                                <button id="saveSettingsBtn" class="btn btn-success w-100">
                                                    <i class="fas fa-save me-2"></i>حفظ الإعدادات
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                    
                                    <div class="col-md-6">
                                        <div class="card">
                                            <div class="card-header bg-success text-white">
                                                <i class="fas fa-clock me-2"></i>إعدادات الإرسال
                                            </div>
                                            <div class="card-body">
                                                <div class="mb-3">
                                                    <label for="sendType" class="form-label">نوع الإرسال</label>
                                                    <select class="form-select" id="sendType">
                                                        <option value="manual">يدوي</option>
                                                        <option value="scheduled">مجدول</option>
                                                    </select>
                                                </div>
                                                <div id="scheduledSettings">
                                                    <div class="mb-3">
                                                        <label for="interval" class="form-label">الفترة الزمنية (ثانية)</label>
                                                        <input type="number" class="form-control" id="interval" value="3600" min="60">
                                                    </div>
                                                </div>
                                                <div class="mb-3">
                                                    <label for="maxRetries" class="form-label">أقصى عدد من المحاولات</label>
                                                    <input type="number" class="form-control" id="maxRetries" value="5" min="1">
                                                </div>
                                                <div class="form-check mb-3">
                                                    <input class="form-check-input" type="checkbox" id="autoReconnect">
                                                    <label class="form-check-label" for="autoReconnect">إعادة الاتصال التلقائي</label>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            
                            <!-- المراقبة -->
                            <div class="tab-pane fade" id="monitor" role="tabpanel">
                                <div class="row">
                                    <div class="col-md-8">
                                        <div class="card">
                                            <div class="card-header bg-warning text-dark">
                                                <i class="fas fa-play-circle me-2"></i>نظام المراقبة
                                            </div>
                                            <div class="card-body">
                                                <div class="row mb-4">
                                                    <div class="col-md-4">
                                                        <div class="stats-box">
                                                            <div class="stats-number" id="sentCount">0</div>
                                                            <div>الرسائل المرسلة</div>
                                                        </div>
                                                    </div>
                                                    <div class="col-md-4">
                                                        <div class="stats-box">
                                                            <div class="stats-number" id="errorsCount">0</d
