# تطبيق monkey patch في بداية الملف قبل أي استيرادات أخرى
import eventlet
eventlet.monkey_patch()

import os
import json
import uuid
import time
import logging
import asyncio
import threading
from threading import Lock
from flask import Flask, session, request, render_template, jsonify, redirect
from flask_socketio import SocketIO, emit, join_room, leave_room
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.sessions import StringSession

# تكوين السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# إنشاء التطبيق مع تغيير مسار القوالب إلى المجلد الرئيسي
app = Flask(__name__, template_folder='.')
app.secret_key = os.environ.get("SESSION_SECRET", os.urandom(24))

# إعداد SocketIO مع async_mode='eventlet'
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='eventlet',
    ping_timeout=60, 
    ping_interval=30,
    logger=True,
    engineio_logger=True
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
                            'client': None,
                            'settings': settings,
                            'thread': None,
                            'is_running': False,
                            'stats': {"sent": 0, "errors": 0},
                            'connected': False,
                            'authenticated': False,
                            'awaiting_code': False,
                            'awaiting_password': False,
                            'phone_code_hash': None,
                            'loop': None,
                            'client_thread': None,
                            'last_scheduled_send': 0,
                            'monitoring_active': False,
                            'message_handler': None
                        }
                        session_count += 1
                        logger.info(f"✓ Loaded session for {user_id}")

        except Exception as e:
            logger.error(f"Error loading sessions: {str(e)}")

    logger.info(f"Loaded {session_count} sessions successfully")
    return session_count

# ===========================
# مدير التليجرام المحسن
# ===========================
class TelegramClientManager:
    """مدير عملاء التليجرام المحسن"""

    def __init__(self, user_id):
        self.user_id = user_id
        self.client = None
        self.loop = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.is_ready = threading.Event()

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
            self.client = TelegramClient(StringSession(), int(API_ID), API_HASH, loop=self.loop)

            self.loop.run_until_complete(self._client_main())

        except Exception as e:
            logger.error(f"Client thread error for {self.user_id}: {str(e)}")
            self.is_ready.set()  # تأكد من أن is_ready مضبوط حتى في حالة الخطأ
        finally:
            if self.loop:
                self.loop.close()

    async def _client_main(self):
        """الوظيفة الرئيسية للعميل"""
        try:
            await self.client.connect()
            self.is_ready.set()
            logger.info(f"Client connected for user {self.user_id}")

            while not self.stop_flag.is_set():
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Client main error: {str(e)}")
        finally:
            if self.client:
                await self.client.disconnect()

    def run_coroutine(self, coro):
        """تشغيل coroutine في event loop الخاص بالعميل"""
        if not self.loop or not self.is_ready.is_set():
            raise Exception("Event loop not initialized or client not ready")

        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=30)

    def stop(self):
        """إيقاف العميل"""
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=5)

# ===========================
# مدير التليجرام الحقيقي
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
                logger.error("API_ID or API_HASH not set")
                return {
                    "status": "error", 
                    "message": "❌ بيانات API غير متوفرة"
                }

            client_manager = self.get_client_manager(user_id)
            client_manager.start_client_thread()

            # الانتظار حتى يكون العميل جاهزاً
            if not client_manager.is_ready.wait(timeout=30):
                return {
                    "status": "error", 
                    "message": "❌ انتهت مهلة الاتصال بالتليجرام"
                }

            is_authorized = False
            try:
                is_authorized = client_manager.run_coroutine(
                    client_manager.client.is_user_authorized()
                )
            except Exception as e:
                logger.error(f"Authorization check error: {str(e)}")
                return {
                    "status": "error", 
                    "message": f"❌ خطأ في التحقق من الصلاحية: {str(e)}"
                }

            if not is_authorized:
                try:
                    sent = client_manager.run_coroutine(
                        client_manager.client.send_code_request(phone_number)
                    )
                    logger.info(f"Verification code sent to {phone_number}")

                    with USERS_LOCK:
                        if user_id in USERS:
                            USERS[user_id]['awaiting_code'] = True
                            USERS[user_id]['phone_code_hash'] = sent.phone_code_hash
                            USERS[user_id]['client_manager'] = client_manager

                    return {
                        "status": "code_required", 
                        "message": "📱 تم إرسال كود التحقق"
                    }

                except Exception as e:
                    logger.error(f"Send code error: {str(e)}")
                    return {
                        "status": "error", 
                        "message": f"❌ فشل إرسال كود التحقق: {str(e)}"
                    }
            else:
                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['client_manager'] = client_manager
                        USERS[user_id]['connected'] = True
                        USERS[user_id]['authenticated'] = True

                return {"status": "success", "message": "✅ تم تسجيل الدخول"}

        except Exception as e:
            logger.error(f"Setup error for {user_id}: {str(e)}")
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
                # استخدام sign_in بدلاً من sign_in
                user = client_manager.run_coroutine(
                    client_manager.client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                )

                with USERS_LOCK:
                    USERS[user_id]['connected'] = True
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['awaiting_code'] = False
                    USERS[user_id]['awaiting_password'] = False

                # حفظ جلسة التسجيل
                session_string = client_manager.run_coroutine(
                    client_manager.client.session.save()
                )
                session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
                with open(session_file, "w") as f:
                    f.write(session_string)

                return {"status": "success", "message": "✅ تم التحقق بنجاح"}

            except SessionPasswordNeededError:
                with USERS_LOCK:
                    USERS[user_id]['awaiting_code'] = False
                    USERS[user_id]['awaiting_password'] = True

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
                # استخدام كلمة المرور للتسجيل
                await_result = client_manager.run_coroutine(
                    client_manager.client.sign_in(password=password)
                )

                with USERS_LOCK:
                    USERS[user_id]['connected'] = True
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['awaiting_password'] = False

                # حفظ جلسة التسجيل
                session_string = client_manager.run_coroutine(
                    client_manager.client.session.save()
                )
                session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
                with open(session_file, "w") as f:
                    f.write(session_string)

                return {"status": "success", "message": "✅ تم التحقق بنجاح"}

            except PasswordHashInvalidError:
                return {"status": "error", "message": "❌ كلمة المرور غير صحيحة"}

        except Exception as e:
            logger.error(f"Password verification error: {str(e)}")
            return {"status": "error", "message": f"❌ خطأ: {str(e)}"}

# إنشاء مدير التليجرام
telegram_manager = TelegramManager()

# ===========================
# نظام المراقبة الفورية واللحظية
# ===========================
def setup_message_handler(user_id):
    """إعداد معالج الرسائل للمراقبة الفورية"""
    try:
        with USERS_LOCK:
            if user_id not in USERS:
                return False
                
            client_manager = USERS[user_id].get('client_manager')
            if not client_manager:
                return False
                
            settings = USERS[user_id]['settings']
            watch_words = settings.get('watch_words', [])
            
            if not watch_words:
                return False
                
            # إضافة معالج الأحداث للرسائل الجديدة
            @client_manager.client.on(events.NewMessage(incoming=True))
            async def handler(event):
                try:
                    message_text = event.message.text or ""
                    sender = await event.get_sender()
                    chat = await event.get_chat()
                    
                    # التحقق من وجود كلمات مراقبة في الرسالة
                    for keyword in watch_words:
                        if keyword.lower() in message_text.lower():
                            # إرسال تنبيه إلى المحادثة الخاصة
                            alert_message = f"🚨 تنبيه مراقبة - كلمة مفتاحية: {keyword}\n\n"
                            alert_message += f"📝 النص: {message_text[:200]}...\n\n"
                            alert_message += f"👤 المرسل: {getattr(sender, 'first_name', '') or getattr(sender, 'username', '') or 'غير معروف'}\n"
                            alert_message += f"💬 الدردشة: {getattr(chat, 'title', '') or getattr(chat, 'username', '') or 'خاص'}\n"
                            alert_message += f"🕐 الوقت: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                            
                            # إرسال التنبيه إلى المحادثة المحفوظة
                            await client_manager.client.send_message('me', alert_message)
                            
                            # إرسال التنبيه إلى الواجهة عبر SocketIO
                            socketio.emit('keyword_alert', {
                                "keyword": keyword,
                                "message": message_text[:200],
                                "sender": getattr(sender, 'first_name', '') or getattr(sender, 'username', '') or 'غير معروف',
                                "chat": getattr(chat, 'title', '') or getattr(chat, 'username', '') or 'خاص',
                                "timestamp": time.strftime('%H:%M:%S')
                            }, to=user_id)
                            
                            break
                            
                except Exception as e:
                    logger.error(f"Error in message handler for {user_id}: {str(e)}")
            
            # حفظ المرجع إلى المعالج
            USERS[user_id]['message_handler'] = handler
            
        return True
        
    except Exception as e:
        logger.error(f"Error setting up message handler for {user_id}: {str(e)}")
        return False

def monitoring_worker(user_id):
    """مهمة المراقبة الفورية والمستمرة"""
    logger.info(f"Starting instant real-time monitoring for user {user_id}")

    try:
        # تهيئة متغيرات المراقبة الفورية
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['monitoring_active'] = True
                settings = USERS[user_id]['settings'].copy()
                client_manager = USERS[user_id].get('client_manager')

        if not client_manager:
            logger.error(f"No client manager for user {user_id}")
            return

        # إعداد معالج الرسائل للمراقبة الفورية
        if not setup_message_handler(user_id):
            logger.error(f"Failed to setup message handler for user {user_id}")
            return

        # إرسال إشعار بدء المراقبة الفورية
        socketio.emit('log_update', {
            "message": "🚀 بدأت المراقبة الفورية - سيتم إرسال التنبيهات فور وصول أي رسالة تحتوي على الكلمات المراقبة"
        }, to=user_id)

        # البقاء في الحلقة للمراقبة المستمرة
        while True:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id]['is_running']:
                    logger.info(f"Stopping monitoring for user {user_id}")
                    break

            # إرسال إشارة حياة للمراقبة
            status_info = {
                'timestamp': time.strftime('%H:%M:%S'),
                'status': 'active',
                'type': 'instant_monitoring'
            }

            socketio.emit('heartbeat', status_info, to=user_id)
            
            # انتظار قصير جداً لتجنب استهلاك CPU عالي
            time.sleep(1)

    except Exception as e:
        logger.error(f"Monitoring worker error for {user_id}: {str(e)}")
        socketio.emit('log_update', {
            "message": f"❌ خطأ في المراقبة: {str(e)}"
        }, to=user_id)
    finally:
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['is_running'] = False
                USERS[user_id]['monitoring_active'] = False
                USERS[user_id]['thread'] = None

        socketio.emit('log_update', {
            "message": "⏹ تم إيقاف نظام المراقبة الفورية"
        }, to=user_id)

        socketio.emit('heartbeat', {
            'timestamp': time.strftime('%H:%M:%S'),
            'status': 'stopped'
        }, to=user_id)

        logger.info(f"Monitoring worker ended for user {user_id}")

# ===========================
# أحداث Socket.IO
# ===========================
@socketio.on('connect')
def handle_connect():
    if 'user_id' in session:
        user_id = session['user_id']
        join_room(user_id)
        logger.info(f"User {user_id} connected via socket")

        with USERS_LOCK:
            if user_id in USERS:
                connected = USERS[user_id].get('connected', False)
                emit('connection_status', {
                    "status": "connected" if connected else "disconnected"
                })

        # إرسال رسالة ترحيب إلى وحدة التحكم
        emit('console_log', {
            "message": f"[{time.strftime('%H:%M:%S')}] INFO: Socket connected"
        })
        
        # إرسال رسالة ترحيب إلى سجل النشاط
        emit('log_update', {
            "message": f"[{time.strftime('%H:%M:%S')}] تم الاتصال بالخادم"
        })

@socketio.on('disconnect')
def handle_disconnect():
    if 'user_id' in session:
        user_id = session['user_id']
        leave_room(user_id)
        logger.info(f"User {user_id} disconnected from socket")
        
        # إرسال رسالة انفصال إلى سجل النشاط
        emit('log_update', {
            "message": f"[{time.strftime('%H:%M:%S')}] تم قطع الاتصال بالخادم"
        }, to=user_id)

# ===========================
# المسارات الأساسية
# ===========================
@app.route("/")
def index():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
        session.permanent = True

    user_id = session['user_id']
    settings = load_settings(user_id)
    connection_status = "disconnected"

    with USERS_LOCK:
        if user_id in USERS:
            connected = USERS[user_id].get('connected', False)
            connection_status = "connected" if connected else "disconnected"

    # إضافة عنوان التطبيق
    app_title = "مركز سرعة انجاز"
    whatsapp_link = "https://wa.me/+966510349663"

    return render_template('index.html', 
                         settings=settings, 
                         connection_status=connection_status,
                         app_title=app_title,
                         whatsapp_link=whatsapp_link)

@app.route("/admin")
def admin():
    if not session.get('is_admin'):
        return redirect('/admin_login')

    with USERS_LOCK:
        users_data = {}
        for user_id, data in USERS.items():
            users_data[user_id] = {
                'settings': data['settings'],
                'is_running': data['is_running'],
                'stats': data['stats'],
                'connected': data.get('connected', False)
            }

    return render_template('admin.html', users=users_data)

@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template('admin_login.html')

    if request.form.get('password') == ADMIN_PASSWORD:
        session['is_admin'] = True
        return redirect('/admin')

    return render_template('admin_login.html', error="كلمة المرور غير صحيحة")

@app.route('/static/<path:filename>')
def static_files(filename):
    """خدمة الملفات الثابتة بما في ذلك manifest وأيقونات PWA"""
    return app.send_static_file(filename)

# ===========================
# API Routes
# ===========================
@app.route("/api/save_login", methods=["POST"])
def api_save_login():
    user_id = session['user_id']
    data = request.json

    logger.info(f"Received login request from user {user_id}: {data}")

    if not data or not data.get('phone'):
        logger.warning("No phone number provided")
        return jsonify({
            "success": False, 
            "message": "❌ يرجى إدخال رقم الهاتف"
        })

    phone_number = data.get('phone')
    password = data.get('password', '')

    # تنظيف رقم الهاتف (إزالة أي مسافات أو أحرف غير رقمية)
    phone_number = ''.join(filter(str.isdigit, phone_number))

    settings = {
        'phone': phone_number,
        'password': password,
        'login_time': time.time()
    }

    if not save_settings(user_id, settings):
        logger.error("Failed to save settings")
        return jsonify({
            "success": False, 
            "message": "❌ فشل في حفظ البيانات"
        })

    try:
        socketio.emit('log_update', {
            "message": "🔄 بدء عملية تسجيل الدخول..."
        }, to=user_id)

        with USERS_LOCK:
            USERS[user_id] = {
                'client': None,
                'settings': settings,
                'thread': None,
                'is_running': False,
                'stats': {"sent": 0, "errors": 0},
                'connected': False,
                'authenticated': False,
                'awaiting_code': False,
                'awaiting_password': False,
                'phone_code_hash': None,
                'client_manager': None,
                'last_scheduled_send': 0,
                'monitoring_active': False,
                'message_handler': None
            }

        result = telegram_manager.setup_client(user_id, phone_number)
        logger.info(f"Login result for {user_id}: {result}")

        if result["status"] == "success":
            socketio.emit('log_update', {
                "message": "✅ تم تسجيل الدخول بنجاح"
            }, to=user_id)

            socketio.emit('connection_status', {
                "status": "connected"
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "✅ تم تسجيل الدخول"
            })

        elif result["status"] == "code_required":
            socketio.emit('log_update', {
                "message": "📱 تم إرسال كود التحقق"
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "📱 تم إرسال كود التحقق", 
                "code_required": True
            })

        else:
            error_message = result.get('message', 'خطأ غير معروف')
            logger.error(f"Login error: {error_message}")
            socketio.emit('log_update', {
                "message": f"❌ {error_message}"
            }, to=user_id)

            return jsonify({
                "success": False, 
                "message": f"❌ {error_message}"
            })

    except Exception as e:
        logger.error(f"Login error for user {user_id}: {str(e)}")
        socketio.emit('log_update', {
            "message": f"❌ خطأ: {str(e)}"
        }, to=user_id)

        return jsonify({
            "success": False, 
            "message": f"❌ خطأ: {str(e)}"
        })

@app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    user_id = session['user_id']
    data = request.json

    if not data:
        return jsonify({
            "success": False, 
            "message": "❌ لم يتم إرسال البيانات"
        })

    code = data.get('code')
    password = data.get('password')

    if not code and not password:
        return jsonify({
            "success": False, 
            "message": "❌ يرجى إدخال الكود أو كلمة المرور"
        })

    try:
        if code:
            result = telegram_manager.verify_code(user_id, code)
        else:
            result = telegram_manager.verify_password(user_id, password)

        if result["status"] == "success":
            socketio.emit('log_update', {
                "message": "✅ تم التحقق بنجاح"
            }, to=user_id)

            socketio.emit('connection_status', {
                "status": "connected"
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "✅ تم التحقق بنجاح"
            })

        elif result["status"] == "password_required":
            return jsonify({
                "success": True, 
                "message": result["message"], 
                "password_required": True
            })

        else:
            error_message = result.get('message', 'فشل التحقق')
            socketio.emit('log_update', {
                "message": f"❌ {error_message}" 
            }, to=user_id)

            return jsonify({
                "success": False, 
                "message": f"❌ {error_message}"
            })

    except Exception as e:
        socketio.emit('log_update', {
            "message": f"❌ خطأ في التحقق: {str(e)}"
        }, to=user_id)

        return jsonify({
            "success": False, 
            "message": f"❌ خطأ: {str(e)}"
        })

@app.route("/api/save_settings", methods=["POST"])
def api_save_settings():
    user_id = session['user_id']
    data = request.json

    if not data:
        return jsonify({
            "success": False, 
            "message": "❌ لم يتم إرسال البيانات"
        })

    current_settings = load_settings(user_id)
    current_settings.update({
        'message': data.get('message', ''),
        'groups': [g.strip() for g in data.get('groups', '').split('\n') if g.strip()],
        'interval_seconds': int(data.get('interval_seconds', 3600)),
        'watch_words': [w.strip() for w in data.get('watch_words', '').split('\n') if w.strip()],
        'send_type': data.get('send_type', 'manual'),
        'max_retries': int(data.get('max_retries', 5)),
        'auto_reconnect': data.get('auto_reconnect', False)
    })

    if save_settings(user_id, current_settings):
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['settings'] = current_settings

        socketio.emit('log_update', {
            "message": "✅ تم حفظ الإعدادات بنجاح"
        }, to=user_id)

        return jsonify({
            "success": True, 
            "message": "✅ تم حفظ الإعدادات"
        })
    else:
        return jsonify({
            "success": False, 
            "message": "❌ فشل في حفظ الإعدادات"
        })

@app.route("/api/start_monitoring", methods=["POST"])
def api_start_monitoring():
    user_id = session['user_id']

    with USERS_LOCK:
        if user_id not in USERS:
            return jsonify({
                "success": False, 
                "message": "❌ لم يتم إعداد الحساب"
            })

        if not USERS[user_id].get('authenticated'):
            return jsonify({
                "success": False, 
                "message": "❌ يجب تسجيل الدخول أولاً"
            })

        if USERS[user_id]['is_running']:
            return jsonify({
                "success": False, 
                "message": "✅ النظام يعمل بالفعل"
            })

        USERS[user_id]['is_running'] = True

    socketio.emit('log_update', {
        "message": "🚀 بدء تشغيل نظام المراقبة الفورية..."
    }, to=user_id)

    try:
        monitoring_thread = threading.Thread(
            target=monitoring_worker, 
            args=(user_id,), 
            daemon=True
        )
        monitoring_thread.start()

        with USERS_LOCK:
            USERS[user_id]['thread'] = monitoring_thread

        return jsonify({
            "success": True, 
            "message": "🚀 بدأت المراقبة الفورية"
        })

    except Exception as e:
        logger.error(f"Failed to start monitoring for {user_id}: {str(e)}")

        with USERS_LOCK:
            USERS[user_id]['is_running'] = False

        return jsonify({
            "success": False, 
            "message": f"❌ فشل في بدء المراقبة: {str(e)}"
        })

@app.route("/api/stop_monitoring", methods=["POST"])
def api_stop_monitoring():
    user_id = session['user_id']

    with USERS_LOCK:
        if user_id in USERS and USERS[user_id]['is_running']:
            USERS[user_id]['is_running'] = False
            socketio.emit('log_update', {
                "message": "⏹ إيقاف نظام المراقبة..."
            }, to=user_id)

            return jsonify({
                "success": True, 
                "message": "⏹ تم إيقاف المراقبة"
            })

    return jsonify({
        "success": False, 
        "message": "❌ النظام غير مشغل"
    })

@app.route("/api/send_now", methods=["POST"])
def api_send_now():
    user_id = session['user_id']

    with USERS_LOCK:
        if user_id not in USERS:
            return jsonify({
                "success": False, 
                "message": "❌ لم يتم إعداد الحساب"
            })

        if not USERS[user_id].get('authenticated'):
            return jsonify({
                "success": False, 
                "message": "❌ يجب تسجيل الدخول أولاً"
            })

        settings = USERS[user_id]['settings']

    groups = settings.get('groups', [])
    message = settings.get('message', '')

    if not groups or not message:
        return jsonify({
            "success": False, 
            "message": "❌ يرجى تحديد المجموعات والرسالة"
        })

    socketio.emit('log_update', {
        "message": f"🚀 بدء الإرسال الفوري إلى {len(groups)} مجموعة"
    }, to=user_id)

    def send_messages():
        try:
            successful = 0
            failed = 0

            for i, group in enumerate(groups, 1):
                try:
                    result = telegram_manager.send_message_async(user_id, group, message)

                    socketio.emit('log_update', {
                        "message": f"✅ [{i}/{len(groups)}] نجح إلى: {group}"
                    }, to=user_id)

                    successful += 1
                    with USERS_LOCK:
                        if user_id in USERS:
                            USERS[user_id]['stats']['sent'] += 1

                    socketio.emit('stats_update', USERS[user_id]['stats'], to=user_id)

                    if i < len(groups):
                        time.sleep(3)

                except Exception as e:
                    error_msg = str(e)
                    if "banned" in error_msg.lower():
                        error_type = "محظور"
                    elif "private" in error_msg.lower():
                        error_type = "خاص/محدود"
                    elif "can't write" in error_msg.lower():
                        error_type = "غير مسموح"
                    else:
                        error_type = "خطأ"

                    logger.error(f"Send error to {group}: {error_msg}")
                    socketio.emit('log_update', {
                        "message": f"❌ [{i}/{len(groups)}] فشل إلى {group}: {error_type}"
                    }, to=user_id)

                    failed += 1
                    with USERS_LOCK:
                        if user_id in USERS:
                            USERS[user_id]['stats']['errors'] += 1

                    socketio.emit('stats_update', USERS[user_id]['stats'], to=user_id)

            # ملخص نهائي
            socketio.emit('log_update', {
                "message": f"📊 انتهى الإرسال الفوري: ✅ {successful} نجح | ❌ {failed} فشل"
            }, to=user_id)

        except Exception as e:
            logger.error(f"Send thread error: {str(e)}")

    threading.Thread(target=send_messages, daemon=True).start()

    return jsonify({
        "success": True, 
        "message": f"🚀 بدأ إرسال {len(groups)} رسالة"
    })

@app.route("/api/get_stats", methods=["GET"])
def api_get_stats():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"sent": 0, "errors": 0})

    with USERS_LOCK:
        if user_id in USERS:
            return jsonify(USERS[user_id]['stats'])

    return jsonify({"sent": 0, "errors": 0})

@app.route("/api/get_login_status", methods=["GET"])
def api_get_login_status():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"logged_in": False, "connected": False})

    with USERS_LOCK:
        if user_id in USERS:
            return jsonify({
                "logged_in": USERS[user_id].get('authenticated', False), 
                "connected": USERS[user_id].get('connected', False),
                "is_running": USERS[user_id].get('is_running', False)
            })

    return jsonify({"logged_in": False, "connected": False, "is_running": False})

@app.route("/api/reset_login", methods=["POST"])
def api_reset_login():
    user_id = session['user_id']

    with USERS_LOCK:
        if user_id in USERS:
            if USERS[user_id]['is_running']:
                USERS[user_id]['is_running'] = False

            client_manager = USERS[user_id].get('client_manager')
            if client_manager:
                client_manager.stop()

            del USERS[user_id]

    session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
    if os.path.exists(session_file):
        try:
            os.remove(session_file)
        except Exception as e:
            logger.error(f"Failed to remove session file: {str(e)}")

    socketio.emit('log_update', {
        "message": "🔄 إعادة تعيين جلسة تسجيل الدخول"
    }, to=user_id)

    socketio.emit('connection_status', {
        "status": "disconnected"
    }, to=user_id)

    return jsonify({
        "success": True, 
        "message": "✅ تم إعادة التعيين"
    })

@app.route("/api/logout", methods=["POST"])
def api_logout():
    user_id = session['user_id']

    with USERS_LOCK:
        if user_id in USERS:
            if USERS[user_id]['is_running']:
                USERS[user_id]['is_running'] = False

            client_manager = USERS[user_id].get('client_manager')
            if client_manager:
                try:
                    client_manager.run_coroutine(client_manager.client.log_out())
                except:
                    pass
                client_manager.stop()

            del USERS[user_id]

    session_file = os.path.join(SESSIONS_DIR, f"{user_id}_session.session")
    settings_file = os.path.join(SESSIONS_DIR, f"{user_id}.json")

    for file_path in [session_file, settings_file]:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to remove file {file_path}: {str(e)}")

    return jsonify({
        "success": True, 
        "message": "✅ تم تسجيل الخروج"
    })

# ===========================
# Admin API
# ===========================
@app.route("/api/admin/get_users", methods=["GET"])
def api_admin_get_users():
    if not session.get('is_admin'):
        return jsonify({"success": False, "message": "غير مصرح"})

    with USERS_LOCK:
        users_data = {}
        for user_id, data in USERS.items():
            users_data[user_id] = {
                'settings': data['settings'],
                'is_running': data['is_running'],
                'stats': data['stats'],
                'connected': data.get('connected', False),
                'authenticated': data.get('authenticated', False)
            }

    return jsonify({"success": True, "users": users_data})

@app.route("/api/admin/stop_user/<user_id>", methods=["POST"])
def api_admin_stop_user(user_id):
    if not session.get('is_admin'):
        return jsonify({"success": False, "message": "غير مصرح"})

    with USERS_LOCK:
        if user_id in USERS:
            USERS[user_id]['is_running'] = False
            return jsonify({
                "success": True, 
                "message": f"تم إيقاف المستخدم {user_id}"
            })

    return jsonify({"success": False, "message": "المستخدم غير موجود"})

# تحميل الجلسات عند بدء التطبيق
load_all_sessions()

if __name__ == "__main__":
    with app.app_context():
        logger.info("🚀 Starting enhanced Telegram automation system...")
        socketio.run(
            app, 
            host="0.0.0.0", 
            port=5000, 
            debug=True,
            use_reloader=False
        )
