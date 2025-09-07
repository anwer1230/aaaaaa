import os
import json
import uuid
import time
import logging
import asyncio
import threading
from threading import Lock
from flask import Flask, session, request, render_template, jsonify, redirect, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.sessions import StringSession
from telethon.tl.types import Message

# تكوين السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# إنشاء التطبيق
app = Flask(__name__)
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
# قوالب HTML
# ===========================

INDEX_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>مركز سرعة انجاز 📚للخدمات الطلابية والاكاديمية</title>
    <!-- PWA Meta Tags -->
    <meta name="theme-color" content="#007bff">
    <meta name="description" content="مركز سرعة انجاز للخدمات الطلابية والأكاديمية - نظام التليجرام التلقائي">
    <link rel="manifest" href="/static/manifest.json">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="مركز سرعة انجاز">
    <!-- Icons -->
    <link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192x192.png">
    <link rel="apple-touch-icon" href="/static/icon-192x192.png">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        .header-title {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 25px;
            border-radius: 15px;
            margin-bottom: 20px;
            text-align: center;
            box-shadow: 0 8px 32px rgba(31, 38, 135, 0.37);
        }
        .whatsapp-link {
            color: #25D366;
            text-decoration: none;
            font-weight: bold;
            transition: all 0.3s ease;
        }
        .whatsapp-link:hover {
            color: #128C7E;
            transform: scale(1.05);
        }
        .log-container {
            height: 300px;
            overflow-y: auto;
            background: #f8f9fa;
            border: 1px solid #dee2e6;
            border-radius: 5px;
            padding: 10px;
        }
        .console-container {
            height: 250px;
            overflow-y: auto;
            background: #212529;
            border: 1px solid #dee2e6;
            border-radius: 5px;
            padding: 10px;
            color: #28a745;
            font-family: 'Courier New', monospace;
        }
        .log-entry {
            margin-bottom: 5px;
            padding: 5px;
            border-radius: 3px;
        }
        .log-success {
            background: #d1e7dd;
            color: #0f5132;
        }
        .log-error {
            background: #f8d7da;
            color: #842029;
        }
        .log-warning {
            background: #fff3cd;
            color: #664d03;
        }
        .log-info {
            background: #cff4fc;
            color: #055160;
        }
        .log-time {
            font-weight: bold;
            margin-right: 5px;
        }
        .console-line {
            margin-bottom: 2px;
            font-size: 12px;
        }
        .monitoring-status {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1050;
        }
        /* تحسين الشكل للجوال */
        @media (max-width: 768px) {
            .container-fluid {
                padding: 10px;
            }
            .card-body {
                padding: 15px;
            }
            .log-container, .console-container {
                height: 200px;
            }
            .header-title {
                padding: 20px;
            }
            .header-title h2 {
                font-size: 1.5rem;
            }
        }
    </style>
</head>
<body>
    <div class="container-fluid mt-4">
        <!-- مساحة للتنبيهات -->
        <div id="alertContainer"></div>
        
        <!-- مؤشر حالة المراقبة -->
        <div class="monitoring-status">
            <span id="monitoringIndicator" class="badge bg-secondary">
                <i class="fas fa-circle"></i> غير نشط
            </span>
        </div>

        <!-- العنوان الرئيسي الجديد -->
        <div class="header-title">
            <h2 class="mb-3">
                <i class="fas fa-graduation-cap me-2"></i>
                📚 تصميم وتنفيذ مركز سرعة انجاز
            </h2>
            <h4 class="mb-3">للخدمات الطلابية والأكاديمية</h4>
            <p class="mb-2">
                <i class="fab fa-whatsapp me-2"></i>
                واتساب: 
                <a href="https://wa.me/+966510349663" class="whatsapp-link" target="_blank">
                    +966510349663
                </a>
            </p>
            <div class="mt-3">
                <small id="connectionStatus" class="badge bg-secondary">
                    {% if connection_status == 'connected' %}
                        <i class="fas fa-circle text-success"></i> متصل
                    {% else %}
                        <i class="fas fa-circle text-danger"></i> غير متصل
                    {% endif %}
                </small>
            </div>
        </div>

        <div class="row">
            <!-- لوحة تسجيل الدخول -->
            <div class="col-lg-6 mb-4">
                <div class="card">
                    <div class="card-header bg-primary text-white text-center">
                        <h2 class="mb-1">
                            <i class="fas fa-telegram-plane me-2"></i>
                            تصميم وتنفيذ مركز سرعة انجاز 📚
                        </h2>
                        <h5 class="mb-2">للخدمات الطلابية والاكاديمية</h5>
                        <p class="mb-2">
                            <a href="https://wa.me/+966510349663" target="_blank" class="text-white text-decoration-none">
                                <i class="fab fa-whatsapp me-1"></i>
                                +966510349663
                            </a>
                        </p>
                        <small id="connectionStatus" class="badge bg-secondary">
                            {% if connection_status == 'connected' %}
                                <i class="fas fa-circle text-success"></i> متصل
                            {% else %}
                                <i class="fas fa-circle text-danger"></i> غير متصل
                            {% endif %}
                        </small>
                    </div>
                    <div class="card-body">
                        <form id="loginForm">
                            <div class="mb-3">
                                <label for="phone" class="form-label">رقم الهاتف</label>
                                <input type="tel" class="form-control" id="phone" 
                                       placeholder="+966xxxxxxxxx" 
                                       value="{{ settings.phone or '' }}"
                                       required>
                                <div class="form-text">أدخل رقم الهاتف مع رمز الدولة</div>
                            </div>
                            
                            <div class="mb-3" id="passwordDiv" style="display: none;">
                                <label for="password" class="form-label">كلمة مرور التليجرام (اختيارية)</label>
                                <input type="password" class="form-control" id="password" 
                                       placeholder="كلمة المرور للتحقق الثنائي">
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-2">
                                    <button type="submit" class="btn btn-primary w-100" id="loginBtn">
                                        <i class="fas fa-sign-in-alt me-2"></i>
                                        تسجيل الدخول
                                    </button>
                                </div>
                                <div class="col-md-3 mb-2">
                                    <button type="button" class="btn btn-warning w-100" id="resetBtn" style="display: none;">
                                        <i class="fas fa-redo me-1"></i>
                                        إعادة تعيين
                                    </button>
                                </div>
                                <div class="col-md-3 mb-2">
                                    <button type="button" class="btn btn-danger w-100" id="logoutBtn" style="display: none;">
                                        <i class="fas fa-sign-out-alt me-1"></i>
                                        خروج
                                    </button>
                                </div>
                            </div>
                        </form>

                        <!-- نموذج التحقق من الكود -->
                        <div id="codeVerification" style="display: none;">
                            <hr>
                            <h6>التحقق من الهوية</h6>
                            <div class="mb-3">
                                <label for="verificationCode" class="form-label">كود التحقق</label>
                                <input type="text" class="form-control" id="verificationCode" 
                                       placeholder="أدخل الكود المرسل إلى هاتفك">
                            </div>
                            
                            <div class="mb-3" id="passwordVerificationDiv" style="display: none;">
                                <label for="passwordVerification" class="form-label">كلمة مرور التليجرام</label>
                                <input type="password" class="form-control" id="passwordVerification" 
                                       placeholder="أدخل كلمة مرور حسابك">
                            </div>
                            
                            <button type="button" class="btn btn-success" id="verifyBtn">
                                <i class="fas fa-check me-2"></i>
                                تأكيد
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- لوحة الإعدادات -->
            <div class="col-lg-6 mb-4">
                <div class="card">
                    <div class="card-header bg-success text-white">
                        <h5 class="mb-0">
                            <i class="fas fa-cog me-2"></i>
                            إعدادات الإرسال
                        </h5>
                    </div>
                    <div class="card-body">
                        <form id="settingsForm">
                            <div class="mb-3">
                                <label for="message" class="form-label">الرسالة</label>
                                <textarea class="form-control" id="message" rows="4" 
                                          placeholder="اكتب الرسالة التي تريد إرسالها">{{ settings.message or '' }}</textarea>
                            </div>
                            
                            <div class="mb-3">
                                <label for="groups" class="form-label">المجموعات</label>
                                <textarea class="form-control" id="groups" rows="4" 
                                          placeholder="@group1&#10;@group2&#10;@group3">{% if settings.groups %}{{ '\\n'.join(settings.groups) }}{% endif %}</textarea>
                                <div class="form-text">أدخل كل مجموعة في سطر منفصل</div>
                            </div>
                            
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label for="sendType" class="form-label">نوع الإرسال</label>
                                    <select class="form-select" id="sendType">
                                        <option value="keyword_monitoring" {{ 'selected' if settings.send_type == 'keyword_monitoring' else '' }}>مراقبة فورية</option>
                                        <option value="scheduled" {{ 'selected' if settings.send_type == 'scheduled' else '' }}>مجدول</option>
                                    </select>
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label for="intervalSeconds" class="form-label">الفترة الزمنية (ثانية)</label>
                                    <input type="number" class="form-control" id="intervalSeconds" 
                                           value="{{ settings.interval_seconds or 3600 }}" min="60">
                                    <div class="form-text">للإرسال المجدول فقط</div>
                                </div>
                            </div>
                            
                            <div class="mb-3">
                                <label for="watchWords" class="form-label">الكلمات المراقبة</label>
                                <textarea class="form-control" id="watchWords" rows="3" 
                                          placeholder="كلمة1&#10;كلمة2&#10;كلمة3">{% if settings.watch_words %}{{ '\\n'.join(settings.watch_words) }}{% endif %}</textarea>
                                <div class="form-text">أدخل كل كلمة في سطر منفصل (للمراقبة التلقائية)</div>
                            </div>
                            
                            <div class="mb-3">
                                <div class="form-check form-switch">
                                    <input class="form-check-input" type="checkbox" id="autoReconnect"
                                           {{ 'checked' if settings.auto_reconnect else '' }}>
                                    <label class="form-check-label" for="autoReconnect">
                                        إعادة الاتصال التلقائي
                                    </label>
                                </div>
                            </div>
                            
                            <button type="submit" class="btn btn-success">
                                <i class="fas fa-save me-2"></i>
                                حفظ الإعدادات
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </div>

        <!-- لوحة التحكم -->
        <div class="row">
            <div class="col-lg-8 mb-4">
                <div class="card">
                    <div class="card-header bg-info text-white">
                        <h5 class="mb-0">
                            <i class="fas fa-play-circle me-2"></i>
                            التحكم في النظام
                        </h5>
                    </div>
                    <div class="card-body">
                        <div class="row">
                            <div class="col-md-3 mb-2">
                                <button class="btn btn-success w-100" id="startBtn">
                                    <i class="fas fa-play me-2"></i>
                                    بدء المراقبة الفورية
                                </button>
                            </div>
                            <div class="col-md-3 mb-2">
                                <button class="btn btn-danger w-100" id="stopBtn" style="display: none;">
                                    <i class="fas fa-stop me-2"></i>
                                    إيقاف المراقبة
                                </button>
                            </div>
                            <div class="col-md-3 mb-2">
                                <button class="btn btn-warning w-100" id="sendNowBtn">
                                    <i class="fas fa-paper-plane me-2"></i>
                                    إرسال فوري
                                </button>
                            </div>
                            <div class="col-md-3 mb-2">
                                <button class="btn btn-info w-100" id="autoSendBtn">
                                    <i class="fas fa-robot me-2"></i>
                                    إرسال تلقائي
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- لوحة الإحصائيات -->
            <div class="col-lg-4 mb-4">
                <div class="card">
                    <div class="card-header bg-secondary text-white">
                        <h5 class="mb-0">
                            <i class="fas fa-chart-bar me-2"></i>
                            الإحصائيات
                        </h5>
                    </div>
                    <div class="card-body">
                        <div class="row text-center">
                            <div class="col-6">
                                <div class="border-end">
                                    <h3 id="sentCount" class="text-success">0</h3>
                                    <small>تم الإرسال</small>
                                </div>
                            </div>
                            <div class="col-6">
                                <h3 id="errorCount" class="text-danger">0</h3>
                                <small>الأخطاء</small>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- سجل النشاط -->
        <div class="row">
            <div class="col-12">
                <div class="card">
                    <div class="card-header bg-dark text-white d-flex justify-content-between">
                        <h5 class="mb-0">
                            <i class="fas fa-list-alt me-2"></i>
                            سجل النشاط
                        </h5>
                        <button class="btn btn-sm btn-outline-light" id="clearLogsBtn">
                            <i class="fas fa-trash me-1"></i>
                            مسح السجل
                        </button>
                    </div>
                    <div class="card-body">
                        <div id="logContainer" class="log-container">
                            <div class="text-muted">لا توجد رسائل حتى الآن...</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- وحدة التحكم -->
        <div class="row mt-4">
            <div class="col-12">
                <div class="card">
                    <div class="card-header bg-warning text-dark d-flex justify-content-between">
                        <h5 class="mb-0">
                            <i class="fas fa-terminal me-2"></i>
                            وحدة التحكم
                        </h5>
                        <button class="btn btn-sm btn-outline-dark" id="clearConsoleBtn">
                            <i class="fas fa-broom me-1"></i>
                            مسح الوحدة
                        </button>
                    </div>
                    <div class="card-body">
                        <div id="consoleContainer" class="console-container">
                            <div class="text-muted">Console ready...</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- معلومات التطبيق المحمول -->
        <div class="row mt-4">
            <div class="col-12">
                <div class="card">
                    <div class="card-header bg-info text-white">
                        <h5 class="mb-0">
                            <i class="fas fa-mobile-alt me-2"></i>
                            تحميل التطبيق المحمول
                        </h5>
                    </div>
                    <div class="card-body text-center">
                        <p>يمكنك إضافة هذا النظام إلى شاشتك الرئيسية كتطبيق</p>
                        <div class="row">
                            <div class="col-md-6 mb-2">
                                <button class="btn btn-success w-100" id="installBtn" style="display: none;">
                                    <i class="fas fa-download me-2"></i>
                                    تثبيت التطبيق
                                </button>
                            </div>
                            <div class="col-md-6 mb-2">
                                <small class="text-muted">
                                    للأندرويد: استخدم Chrome وانقر "إضافة إلى الشاشة الرئيسية"<br>
                                    لآيفون: استخدم Safari وانقر مشاركة ← إضافة إلى الشاشة الرئيسية
                                </small>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Toast Notifications -->
    <div class="toast-container position-fixed bottom-0 end-0 p-3">
        <div id="toast" class="toast align-items-center text-white bg-primary" role="alert" aria-live="assertive" aria-atomic="true">
            <div class="d-flex">
                <div class="toast-body" id="toastBody">
                </div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdn.socket.io/4.5.0/socket.io.min.js"></script>
    <script>
        // PWA Install
        let deferredPrompt;
        const installBtn = document.getElementById('installBtn');
        
        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredPrompt = e;
            installBtn.style.display = 'block';
        });
        
        installBtn.addEventListener('click', async () => {
            if (deferredPrompt) {
                deferredPrompt.prompt();
                const { outcome } = await deferredPrompt.userChoice;
                if (outcome === 'accepted') {
                    console.log('App installed');
                }
                deferredPrompt = null;
                installBtn.style.display = 'none';
            }
        });
        
        // Register Service Worker
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/static/sw.js')
                .then((registration) => {
                    console.log('SW registered: ', registration);
                })
                .catch((registrationError) => {
                    console.log('SW registration failed: ', registrationError);
                });
            });
        }
        
        // Socket.IO and App Logic
        const socket = io();
        const user_id = '{{ session.get("user_id", "") }}';
        
        if (user_id) {
            socket.emit('join', {room: user_id});
        }
        
        // إدارة حالة الاتصال
        socket.on('connection_status', (data) => {
            const statusBadge = document.getElementById('connectionStatus');
            if (data.status === 'connected') {
                statusBadge.innerHTML = '<i class="fas fa-circle text-success"></i> متصل';
                statusBadge.classList.remove('bg-secondary');
                statusBadge.classList.add('bg-success');
            } else {
                statusBadge.innerHTML = '<i class="fas fa-circle text-danger"></i> غير متصل';
                statusBadge.classList.remove('bg-success');
                statusBadge.classList.add('bg-secondary');
            }
        });
        
        // تحديث الإحصائيات
        socket.on('stats_update', (data) => {
            document.getElementById('sentCount').textContent = data.sent;
            document.getElementById('errorCount').textContent = data.errors;
        });
        
        // تحديث سجل النشاط
        socket.on('log_update', (data) => {
            const logContainer = document.getElementById('logContainer');
            const logEntry = document.createElement('div');
            logEntry.className = 'log-entry log-info';
            
            const timestamp = new Date().toLocaleTimeString();
            logEntry.innerHTML = `<span class="log-time">[${timestamp}]</span> ${data.message}`;
            
            // تحديد نوع السجل وإضافة التنسيق المناسب
            if (data.message.includes('❌') || data.message.includes('فشل') || data.message.includes('خطأ')) {
                logEntry.className = 'log-entry log-error';
            } else if (data.message.includes('⚠️') || data.message.includes('تحذير')) {
                logEntry.className = 'log-entry log-warning';
            } else if (data.message.includes('✅') || data.message.includes('نجح')) {
                logEntry.className = 'log-entry log-success';
            }
            
            logContainer.appendChild(logEntry);
            logContainer.scrollTop = logContainer.scrollHeight;
            
            // إذا كان السجل يحتوي على كلمة "تنبيه"، عرض إشعار toast
            if (data.message.includes('تنبيه')) {
                showToast(data.message);
            }
        });
        
        // تنبيهات الكلمات المفتاحية
        socket.on('keyword_alert', (data) => {
            const alertContainer = document.getElementById('alertContainer');
            const alertDiv = document.createElement('div');
            alertDiv.className = 'alert alert-warning alert-dismissible fade show';
            alertDiv.innerHTML = `
                <strong>🚨 تنبيه فوري!</strong> 
                تم اكتشاف الكلمة "${data.keyword}" في ${data.group}
                <br><small>المرسل: ${data.sender} | الوقت: ${data.timestamp}</small>
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            `;
            alertContainer.appendChild(alertDiv);
            
            // إضافة إلى سجل النشاط أيضاً
            const logContainer = document.getElementById('logContainer');
            const logEntry = document.createElement('div');
            logEntry.className = 'log-entry log-warning';
            
            const timestamp = new Date().toLocaleTimeString();
            logEntry.innerHTML = `<span class="log-time">[${timestamp}]</span> 🚨 تنبيه فوري: "${data.keyword}" في ${data.group} من ${data.sender}`;
            
            logContainer.appendChild(logEntry);
            logContainer.scrollTop = logContainer.scrollHeight;
            
            // عرض إشعار toast
            showToast(`🚨 تنبيه فوري: "${data.keyword}" في ${data.group}`);
        });
        
        // تحديث حالة المراقبة
        socket.on('heartbeat', (data) => {
            const indicator = document.getElementById('monitoringIndicator');
            if (data.status === 'active') {
                indicator.innerHTML = '<i class="fas fa-circle text-success"></i> نشط';
                indicator.classList.remove('bg-secondary');
                indicator.classList.add('bg-success');
                
                // إظهار زر الإيقاف وإخفاء زر البدء
                document.getElementById('startBtn').style.display = 'none';
                document.getElementById('stopBtn').style.display = 'block';
            } else {
                indicator.innerHTML = '<i class="fas fa-circle text-danger"></i> غير نشط';
                indicator.classList.remove('bg-success');
                indicator.classList.add('bg-secondary');
                
                // إظهار زر البدء وإخفاء زر الإيقاف
                document.getElementById('startBtn').style.display = 'block';
                document.getElementById('stopBtn').style.display = 'none';
            }
        });
        
        // دالة لعرض إشعارات Toast
        function showToast(message) {
            const toastBody = document.getElementById('toastBody');
            toastBody.textContent = message;
            
            const toast = new bootstrap.Toast(document.getElementById('toast'));
            toast.show();
        }
        
        // إدارة النماذج والأزرار
        document.getElementById('loginForm').addEventListener('submit', function(e) {
            e.preventDefault();
            const phone = document.getElementById('phone').value;
            const password = document.getElementById('password').value;
            
            fetch('/api/save_login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({phone, password})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    if (data.code_required) {
                        document.getElementById('codeVerification').style.display = 'block';
                    } else {
                        showToast('✅ تم تسجيل الدخول بنجاح');
                        document.getElementById('resetBtn').style.display = 'block';
                        document.getElementById('logoutBtn').style.display = 'block';
                    }
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء تسجيل الدخول');
            });
        });
        
        document.getElementById('verifyBtn').addEventListener('click', function() {
            const code = document.getElementById('verificationCode').value;
            const password = document.getElementById('passwordVerification').value;
            
            fetch('/api/verify_code', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({code, password})
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('✅ تم التحقق بنجاح');
                    document.getElementById('codeVerification').style.display = 'none';
                    document.getElementById('resetBtn').style.display = 'block';
                    document.getElementById('logoutBtn').style.display = 'block';
                } else if (data.password_required) {
                    document.getElementById('passwordVerificationDiv').style.display = 'block';
                    showToast('🔒 يرجى إدخال كلمة مرور التحقق بخطوتين');
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء التحقق');
            });
        });
        
        document.getElementById('settingsForm').addEventListener('submit', function(e) {
            e.preventDefault();
            const message = document.getElementById('message').value;
            const groups = document.getElementById('groups').value.split('\\n').filter(g => g.trim());
            const intervalSeconds = document.getElementById('intervalSeconds').value;
            const watchWords = document.getElementById('watchWords').value.split('\\n').filter(w => w.trim());
            const sendType = document.getElementById('sendType').value;
            const autoReconnect = document.getElementById('autoReconnect').checked;
            
            fetch('/api/save_settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    message, 
                    groups: groups.join('\\n'), 
                    intervalSeconds, 
                    watchWords: watchWords.join('\\n'), 
                    sendType, 
                    autoReconnect
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('✅ تم حفظ الإعدادات بنجاح');
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء حفظ الإعدادات');
            });
        });
        
        document.getElementById('startBtn').addEventListener('click', function() {
            fetch('/api/start_monitoring', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('🚀 بدأت المراقبة');
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء بدء المراقبة');
            });
        });
        
        document.getElementById('stopBtn').addEventListener('click', function() {
            fetch('/api/stop_monitoring', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('⏹ توقفت المراقبة');
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء إيقاف المراقبة');
            });
        });
        
        document.getElementById('sendNowBtn').addEventListener('click', function() {
            fetch('/api/send_now', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('🚀 بدأ الإرسال الفوري');
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء الإرسال الفوري');
            });
        });
        
        document.getElementById('resetBtn').addEventListener('click', function() {
            fetch('/api/reset_login', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('🔄 تم إعادة تعيين الجلسة');
                    document.getElementById('resetBtn').style.display = 'none';
                    document.getElementById('logoutBtn').style.display = 'none';
                    document.getElementById('codeVerification').style.display = 'none';
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء إعادة التعيين');
            });
        });
        
        document.getElementById('logoutBtn').addEventListener('click', function() {
            fetch('/api/logout', {
                method: 'POST'
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showToast('✅ تم تسجيل الخروج');
                    document.getElementById('resetBtn').style.display = 'none';
                    document.getElementById('logoutBtn').style.display = 'none';
                } else {
                    showToast('❌ ' + data.message);
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showToast('❌ حدث خطأ أثناء تسجيل الخروج');
            });
        });
        
        document.getElementById('clearLogsBtn').addEventListener('click', function() {
            document.getElementById('logContainer').innerHTML = '<div class="text-muted">لا توجد رسائل حتى الآن...</div>';
        });
        
        document.getElementById('clearConsoleBtn').addEventListener('click', function() {
            document.getElementById('consoleContainer').innerHTML = '<div class="text-muted">Console ready...</div>';
        });
        
        // تحميل الإحصائيات الأولية
        fetch('/api/get_stats')
        .then(response => response.json())
        .then(data => {
            document.getElementById('sentCount').textContent = data.sent;
            document.getElementById('errorCount').textContent = data.errors;
        });
        
        // التحقق من حالة تسجيل الدخول
        fetch('/api/get_login_status')
        .then(response => response.json())
        .then(data => {
            if (data.logged_in) {
                document.getElementById('resetBtn').style.display = 'block';
                document.getElementById('logoutBtn').style.display = 'block';
            }
            if (data.is_running) {
                document.getElementById('startBtn').style.display = 'none';
                document.getElementById('stopBtn').style.display = 'block';
            }
        });
    </script>
</body>
</html>
'''

ADMIN_LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تسجيل دخول الإدارة</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: 'Arial', sans-serif;
        }
        .login-card {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        .card-header {
            background: linear-gradient(135deg, #ff6b6b 0%, #ee5a24 100%);
            border-radius: 15px 15px 0 0 !important;
            border: none;
        }
        .form-control:focus {
            box-shadow: 0 0 0 0.25rem rgba(255, 107, 107, 0.25);
            border-color: #ff6b6b;
        }
        .btn-danger {
            background: linear-gradient(135deg, #ff6b6b 0%, #ee5a24 100%);
            border: none;
            transition: transform 0.2s ease;
        }
        .btn-danger:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(238, 90, 36, 0.4);
        }
        .error-alert {
            background: rgba(220, 53, 69, 0.1);
            border: 1px solid rgba(220, 53, 69, 0.2);
            border-radius: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-md-6 col-lg-4">
                <div class="card login-card">
                    <div class="card-header text-center text-white py-4">
                        <i class="fas fa-shield-alt fa-3x mb-3"></i>
                        <h4 class="mb-0">لوحة الإدارة</h4>
                        <p class="mb-0 opacity-75">تسجيل دخول المدير</p>
                    </div>
                    <div class="card-body p-4">
                        {% if error %}
                        <div class="alert error-alert text-danger mb-4">
                            <i class="fas fa-exclamation-triangle me-2"></i>
                            {{ error }}
                        </div>
                        {% endif %}
                        <form method="POST">
                            <div class="mb-4">
                                <label for="password" class="form-label">
                                    <i class="fas fa-lock me-2"></i>
                                    كلمة المرور
                                </label>
                                <input type="password" 
                                       class="form-control form-control-lg" 
                                       id="password" 
                                       name="password" 
                                       placeholder="أدخل كلمة مرور الإدارة"
                                       required 
                                       autofocus>
                            </div>
                            <button type="submit" class="btn btn-danger btn-lg w-100 mb-3">
                                <i class="fas fa-sign-in-alt me-2"></i>
                                دخول لوحة الإدارة
                            </button>
                        </form>
                        <div class="text-center">
                            <a href="/" class="text-muted text-decoration-none">
                                <i class="fas fa-arrow-right me-1"></i>
                                العودة للصفحة الرئيسية
                            </a>
                        </div>
                    </div>
                    <div class="card-footer text-center text-muted small py-3">
                        <i class="fas fa-lock me-1"></i>
                        منطقة محمية - المدير فقط
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
'''

ADMIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>لوحة الإدارة - نظام التليجرام</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        .log-container {
            height: 300px;
            overflow-y: auto;
            background: #f8f9fa;
            border: 1px solid #dee2e6;
            border-radius: 5px;
            padding: 10px;
        }
    </style>
</head>
<body>
    <div class="container-fluid">
        <nav class="navbar navbar-expand-lg navbar-dark bg-danger mb-4">
            <div class="container">
                <a class="navbar-brand" href="#">
                    <i class="fas fa-user-shield me-2"></i>
                    لوحة الإدارة
                </a>
                <div class="navbar-nav ms-auto">
                    <a class="nav-link" href="/">
                        <i class="fas fa-home me-1"></i>
                        الصفحة الرئيسية
                    </a>
                </div>
            </div>
        </nav>

        <div class="container">
            <!-- إحصائيات عامة -->
            <div class="row mb-4">
                <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card bg-primary text-white">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="flex-grow-1">
                                    <h4 id="totalUsers">{{ users|length }}</h4>
                                    <small>إجمالي المستخدمين</small>
                                </div>
                                <div class="ms-3">
                                    <i class="fas fa-users fa-2x"></i>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card bg-success text-white">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="flex-grow-1">
                                    <h4 id="activeUsers">
                                        {% set active_count = users.values() | selectattr('is_running') | list | length %}
                                        {{ active_count }}
                                    </h4>
                                    <small>المستخدمين النشطين</small>
                                </div>
                                <div class="ms-3">
                                    <i class="fas fa-play-circle fa-2x"></i>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card bg-info text-white">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="flex-grow-1">
                                    <h4 id="totalSent">
                                        {% set total_sent = users.values() | sum(attribute='stats.sent') %}
                                        {{ total_sent }}
                                    </h4>
                                    <small>إجمالي المرسل</small>
                                </div>
                                <div class="ms-3">
                                    <i class="fas fa-paper-plane fa-2x"></i>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card bg-warning text-white">
                        <div class="card-body">
                            <div class="d-flex align-items-center">
                                <div class="flex-grow-1">
                                    <h4 id="totalErrors">
                                        {% set total_errors = users.values() | sum(attribute='stats.errors') %}
                                        {{ total_errors }}
                                    </h4>
                                    <small>إجمالي الأخطاء</small>
                                </div>
                                <div class="ms-3">
                                    <i class="fas fa-exclamation-triangle fa-2x"></i>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- جدول المستخدمين -->
            <div class="card">
                <div class="card-header bg-dark text-white d-flex justify-content-between align-items-center">
                    <h5 class="mb-0">
                        <i class="fas fa-table me-2"></i>
                        إدارة المستخدمين
                    </h5>
                    <button class="btn btn-outline-light btn-sm" id="refreshBtn">
                        <i class="fas fa-sync me-1"></i>
                        تحديث
                    </button>
                </div>
                <div class="card-body">
                    <div class="table-responsive">
                        <table class="table table-striped table-hover">
                            <thead class="table-dark">
                                <tr>
                                    <th>ID المستخدم</th>
                                    <th>رقم الهاتف</th>
                                    <th>الحالة</th>
                                    <th>الإحصائيات</th>
                                    <th>نوع الإرسال</th>
                                    <th>المجموعات</th>
                                    <th>الإجراءات</th>
                                </tr>
                            </thead>
                            <tbody id="usersTableBody">
                                {% for user_id, user_data in users.items() %}
                                <tr data-user-id="{{ user_id }}">
                                    <td>
                                        <code>{{ user_id[:8] }}...</code>
                                    </td>
                                    <td>
                                        <s
