
#!/bin/bash

# إنشاء مجلد الجلسات
mkdir -p sessions

# تشغيل التطبيق
if [ "$ENVIRONMENT" = "production" ]; then
    gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:${PORT:-5000} app:app
else
    python main.py
fi
