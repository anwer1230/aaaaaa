
# نشر نظام التليجرام التلقائي على Render

## المتطلبات الأساسية

### 1. الحصول على بيانات Telegram API
- اذهب إلى https://my.telegram.org/apps
- سجل الدخول بحسابك
- أنشئ تطبيق جديد واحصل على:
  - `API_ID` (رقم)
  - `API_HASH` (نص)

### 2. إعداد الحساب على Render
- أنشئ حساب على https://render.com
- اربط حسابك بـ GitHub

## خطوات النشر

### 1. رفع الكود إلى GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/yourusername/telegram-automation.git
git push -u origin main
```

### 2. إنشاء Web Service على Render
1. اذهب إلى لوحة تحكم Render
2. انقر على "New +"
3. اختر "Web Service"
4. اربط مستودع GitHub الخاص بك
5. استخدم الإعدادات التالية:

**إعدادات أساسية:**
- **Name:** telegram-automation
- **Environment:** Python 3
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app`

**متغيرات البيئة (Environment Variables):**
- `TELEGRAM_API_ID`: أدخل رقم API ID الخاص بك
- `TELEGRAM_API_HASH`: أدخل API Hash الخاص بك  
- `SESSION_SECRET`: أدخل مفتاح تشفير عشوائي (32 حرف)
- `ADMIN_PASSWORD`: أدخل كلمة مرور للوحة الإدارة

### 3. نشر التطبيق
- انقر على "Create Web Service"
- انتظر حتى يكتمل النشر (5-10 دقائق)
- سيعطيك Render رابط للتطبيق مثل: `https://your-app-name.onrender.com`

## الاستخدام

### 1. الوصول للنظام
- اذهب إلى رابط تطبيقك
- أدخل رقم هاتفك مع رمز الدولة
- أدخل كود التحقق الذي سيصلك

### 2. إعداد الرسائل
- أدخل الرسالة المراد إرسالها
- أدخل المجموعات (كل مجموعة في سطر منفصل)
- اختر نوع الإرسال (يدوي/مجدول/مراقبة كلمات)

### 3. بدء النظام
- انقر على "بدء المراقبة"
- راقب السجل للتأكد من عمل النظام

## إعدادات متقدمة

### استخدام Docker
```bash
docker build -t telegram-automation .
docker run -p 5000:5000 \
  -e TELEGRAM_API_ID=your_api_id \
  -e TELEGRAM_API_HASH=your_api_hash \
  -e SESSION_SECRET=your_secret \
  -e ADMIN_PASSWORD=your_password \
  telegram-automation
```

### النشر باستخدام render.yaml
ضع ملف `render.yaml` في جذر المشروع:

```yaml
services:
  - type: web
    name: telegram-automation
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app
    plan: free
```

## استكشاف الأخطاء

### مشاكل شائعة:
1. **خطأ API**: تأكد من صحة `API_ID` و `API_HASH`
2. **خطأ الاتصال**: تأكد من استقرار الإنترنت
3. **رسائل محظورة**: بعض المجموعات قد تكون خاصة أو محظورة

### فحص السجلات:
- في لوحة تحكم Render، اذهب إلى "Logs"
- راقب رسائل الخطأ والتحقق من حالة التطبيق

## الأمان

### نصائح مهمة:
- لا تشارك بيانات API مع أحد
- استخدم كلمة مرور قوية للوحة الإدارة
- فعّل المصادقة الثنائية لحساب Telegram
- راقب نشاط التطبيق بانتظام

## الدعم الفني

### في حالة وجود مشاكل:
1. تحقق من السجلات في Render
2. تأكد من صحة متغيرات البيئة
3. تأكد من صحة أذونات Telegram
4. تحقق من حالة الشبكة والاتصال

### تحسين الأداء:
- استخدم Render's Paid Plan للحصول على موارد أكثر
- راقب استخدام الذاكرة والمعالج
- قلل من عدد المجموعات المراقبة إذا لزم الأمر

## التحديثات

### لتحديث التطبيق:
1. ادفع التغييرات إلى GitHub
2. Render سيعيد النشر تلقائياً
3. راقب عملية النشر في لوحة التحكم

### النسخ الاحتياطي:
- بيانات الجلسات محفوظة في مجلد `sessions`
- يمكن تحميل النسخة الاحتياطية من Render
- احتفظ بنسخة من بيانات API في مكان آمن
