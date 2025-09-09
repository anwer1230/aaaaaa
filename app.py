import os
import json
import uuid
import time
import logging
import asyncio
import threading
from threading import Lock
from flask import Flask, session, request, render_template, jsonify, redirect, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.sessions import StringSession

# تكوين السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# إنشاء التطبيق مع مسار القوالب في المجلد الرئيسي
app = Flask(__name__, template_folder='.')
app.secret_key = os.environ.get("SESSION_SECRET", os.urandom(24))

# إعداد SocketIO
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='threading',
    ping_timeout=60, 
    ping_interval=30,
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
                            'monitoring_active': False
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
            self.client = TelegramClient(session_file, int(API_ID), API_HASH)

            self.loop.run_until_complete(self._client_main())

        except Exception as e:
            logger.error(f"Client thread error for {self.user_id}: {str(e)}")
        finally:
            if self.loop:
                self.loop.close()

    async def _client_main(self):
        """الوظيفة الرئيسية للعميل"""
        try:
            await self.client.connect()
            self.is_ready.set()

            while not self.stop_flag.is_set():
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Client main error: {str(e)}")
        finally:
            await self.client.disconnect()

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
                return {
                    "status": "error", 
                    "message": "❌ بيانات API غير متوفرة"
                }

            client_manager = self.get_client_manager(user_id)
            client_manager.start_client_thread()

            is_authorized = client_manager.run_coroutine(
                client_manager.client.is_user_authorized()
            )

            if not is_authorized:
                sent = client_manager.run_coroutine(
                    client_manager.client.send_code_request(phone_number)
                )

                with USERS_LOCK:
                    if user_id in USERS:
                        USERS[user_id]['awaiting_code'] = True
                        USERS[user_id]['phone_code_hash'] = sent.phone_code_hash
                        USERS[user_id]['client_manager'] = client_manager

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
                user = client_manager.run_coroutine(
                    client_manager.client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                )

                with USERS_LOCK:
                    USERS[user_id]['connected'] = True
                    USERS[user_id]['authenticated'] = True
                    USERS[user_id]['awaiting_code'] = False
                    USERS[user_id]['awaiting_password'] = False

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
# نظام المراقبة الفورية واللحظية
# ===========================
def monitoring_worker(user_id):
    """مهمة المراقبة الفورية والمستمرة - بدون توقيتات محددة"""
    logger.info(f"Starting instant real-time monitoring worker for user {user_id}")

    consecutive_errors = 0
    max_consecutive_errors = 5

    try:
        # تهيئة متغيرات المراقبة الفورية
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['last_keyword_check'] = 0  # بدء فوري
                USERS[user_id]['monitoring_active'] = True

        # إرسال إشعار بدء المراقبة الفورية
        socketio.emit('log_update', {
            "message": "🚀 بدأت المراقبة الفورية واللحظية - سيتم إرسال التنبيهات فور وصول أي رسالة تحتوي على الكلمات المراقبة"
        }, to=user_id)

        while True:
            with USERS_LOCK:
                if user_id not in USERS or not USERS[user_id]['is_running']:
                    logger.info(f"Stopping instant monitoring for user {user_id}")
                    break

                user_data = USERS[user_id].copy()
                USERS[user_id]['monitoring_active'] = True

            try:
                settings = user_data['settings']
                send_type = settings.get('send_type', 'manual')
                current_time = time.time()

                # المراقبة الفورية للكلمات المفتاحية (بدون انتظار)
                watch_words = settings.get('watch_words', [])
                if watch_words:
                    logger.info(f"Executing INSTANT keyword monitoring for user {user_id}")
                    execute_instant_keyword_monitoring(user_id, settings)

                # تنفيذ الإرسال المجدول إذا كان مطلوب
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

                # إرسال إشارة حياة للمراقبة الفورية
                status_info = {
                    'timestamp': time.strftime('%H:%M:%S'),
                    'status': 'active',
                    'type': 'instant_monitoring',  # نوع جديد للمراقبة الفورية
                    'keywords_active': bool(watch_words),
                    'instant': True  # تأكيد أن المراقبة فورية
                }

                socketio.emit('heartbeat', status_info, to=user_id)

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Instant monitoring cycle error for {user_id}: {str(e)}")

                socketio.emit('log_update', {
                    "message": f"⚠️ خطأ في المراقبة الفورية: {str(e)[:100]}"
                }, to=user_id)

                if consecutive_errors >= max_consecutive_errors:
                    socketio.emit('log_update', {
                        "message": f"❌ تم إيقاف المراقبة بسبب تكرار الأخطاء ({consecutive_errors})"
                    }, to=user_id)
                    break

            # فترة انتظار قصيرة جداً للمراقبة الفورية (2 ثانية فقط)
            time.sleep(2)  # أقصر فترة انتظار للاستجابة الفورية

    except Exception as e:
        logger.error(f"Instant monitoring worker error for {user_id}: {str(e)}")
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

        logger.info(f"Instant monitoring worker ended for user {user_id}")

def execute_instant_keyword_monitoring(user_id, settings):
    """تنفيذ مراقبة الكلمات المفتاحية الفورية واللحظية - بدون توقيتات"""
    watch_words = settings.get('watch_words', [])
    groups = settings.get('groups', [])

    if not watch_words:
        return

    if not groups:
        return

    try:
        with USERS_LOCK:
            client_manager = USERS[user_id].get('client_manager')
            last_check_time = USERS[user_id].get('last_keyword_check', 0)

        if not client_manager:
            return

        current_time = time.time()
        detected_keywords = 0
        checked_groups = 0

        # فحص كل مجموعة للكلمات المفتاحية بشكل فوري
        for group in groups:  # فحص جميع المجموعات بدون حد
            try:
                # تنظيف اسم المجموعة
                clean_group = group.strip()
                if not clean_group.startswith('@') and not clean_group.startswith('https://'):
                    clean_group = '@' + clean_group

                entity_obj = client_manager.run_coroutine(
                    client_manager.client.get_entity(clean_group)
                )

                # جلب آخر 50 رسالة للمراقبة الفورية
                messages = client_manager.run_coroutine(
                    client_manager.client.get_messages(entity_obj, limit=50)
                )

                checked_groups += 1

                for msg in messages:
                    if msg.text and msg.date:
                        msg_time = msg.date.timestamp()

                        # فحص الرسائل الجديدة فقط (أحدث من آخر فحص)
                        if msg_time > last_check_time:
                            msg_lower = msg.text.lower()

                            for keyword in watch_words:
                                keyword_lower = keyword.lower().strip()
                                if keyword_lower and keyword_lower in msg_lower:
                                    detected_keywords += 1

                                    # الحصول على معلومات المرسل
                                    sender_name = "غير معروف"
                                    try:
                                        if msg.sender:
                                            sender_name = getattr(msg.sender, 'first_name', '') or getattr(msg.sender, 'username', '') or str(msg.sender.id)
                                    except:
                                        pass

                                    # إشعار فوري ومفصل
                                    alert_data = {
                                        "keyword": keyword,
                                        "group": clean_group,
                                        "message": msg.text[:200] + "..." if len(msg.text) > 200 else msg.text,
                                        "timestamp": time.strftime('%H:%M:%S'),
                                        "sender": sender_name,
                                        "message_time": time.strftime('%H:%M:%S', time.localtime(msg_time)),
                                        "message_id": msg.id
                                    }

                                    socketio.emit('keyword_alert', alert_data, to=user_id)

                                    socketio.emit('log_update', {
                                        "message": f"🚨 تنبيه فوري: '{keyword}' في {clean_group} من {sender_name}"
                                    }, to=user_id)

                                    # إرسال تنبيه للرسائل المحفوظة فوراً
                                    try:
                                        notification_msg = f"""🚨 تنبيه فوري - مراقبة الكلمات المفتاحية

📝 الكلمة المراقبة: {keyword}
📊 المجموعة: {clean_group}
👤 المرسل: {sender_name}
🕐 وقت الرسالة: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(msg_time))}
🔗 معرف الرسالة: {msg.id}

💬 نص الرسالة:
{msg.text[:500]}{'...' if len(msg.text) > 500 else ''}

---
تنبيه فوري من نظام المراقبة اللحظية"""

                                        client_manager.run_coroutine(
                                            client_manager.client.send_message('me', notification_msg)
                                        )

                                        logger.info(f"Sent INSTANT keyword alert to saved messages for user {user_id}")

                                    except Exception as save_error:
                                        logger.error(f"Failed to send instant alert to saved messages: {str(save_error)}")

                # بدون تأخير بين المجموعات للمراقبة الفورية

            except Exception as group_error:
                logger.error(f"Instant keyword monitoring error for {group}: {str(group_error)}")

        # تحديث وقت آخر فحص
        with USERS_LOCK:
            if user_id in USERS:
                USERS[user_id]['last_keyword_check'] = current_time

        # إحصائيات مبسطة للمراقبة الفورية
        if detected_keywords > 0:
            socketio.emit('log_update', {
                "message": f"🎯 عُثر على {detected_keywords} تطابق فوري في {checked_groups} مجموعة"
            }, to=user_id)

        logger.info(f"Instant keyword monitoring completed for {user_id}: {detected_keywords} matches found instantly")

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Instant keyword monitoring error for {user_id}: {error_msg}")

def execute_keyword_monitoring(user_id, settings):
    """وظيفة للتوافق مع النظام القديم - تستدعي المراقبة الفورية"""
    execute_instant_keyword_monitoring(user_id, settings)

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

        emit('console_log', {
            "message": f"[{time.strftime('%H:%M:%S')}] INFO: Socket connected"
        })

@socketio.on('disconnect')
def handle_disconnect():
    if 'user_id' in session:
        user_id = session['user_id']
        leave_room(user_id)
        logger.info(f"User {user_id} disconnected from socket")

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
    app_title = "مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية"
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
    
    # إذا كان الطلب يحتوي على JSON (من AJAX)
    if request.is_json:
        data = request.get_json()
        password = data.get('password')
    else:
        password = request.form.get('password')

    if password == ADMIN_PASSWORD:
        session['is_admin'] = True
        if request.is_json:
            return jsonify({"success": True, "message": "تم تسجيل الدخول بنجاح"})
        else:
            return redirect('/admin')
    else:
        if request.is_json:
            return jsonify({"success": False, "message": "كلمة المرور غير صحيحة"})
        else:
            return render_template('admin_login.html', error="كلمة المرور غير صحيحة")

@app.route('/static/<path:filename>')
def static_files(filename):
    """خدمة الملفات الثابتة بما في ذلك manifest وأيقونات PWA"""
    return send_from_directory('static', filename)

# ===========================
# API Routes
# ===========================
@app.route("/api/save_login", methods=["POST"])
def api_save_login():
    user_id = session['user_id']
    data = request.json

    if not data or not data.get('phone'):
        return jsonify({
            "success": False, 
            "message": "❌ يرجى إدخال رقم الهاتف"
        })

    settings = {
        'phone': data.get('phone'),
        'password': data.get('password', ''),
        'login_time': time.time()
    }

    if not save_settings(user_id, settings):
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
                'monitoring_active': False
            }

        result = telegram_manager.setup_client(user_id, settings['phone'])

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
        "message": "🚀 بدء تشغيل نظام المراقبة المحسن..."
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
            "message": "🚀 بدأت المراقبة المحسنة"
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
    logger.info("🚀 Starting enhanced Telegram automation system...")
    socketio.run(
        app, 
        host="0.0.0.0", 
        port=5000, 
        debug=False,
        use_reloader=False
                            )
