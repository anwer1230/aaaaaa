
import os
import sys
import psutil
from app import app, socketio, load_all_sessions, alert_queue

def kill_process_on_port(port):
    """إنهاء أي عملية تستخدم المنفذ المحدد"""
    try:
        for proc in psutil.process_iter(['pid', 'name', 'connections']):
            try:
                for conn in proc.connections():
                    if conn.laddr.port == port:
                        print(f"إنهاء العملية {proc.pid} ({proc.name()}) التي تستخدم المنفذ {port}")
                        proc.terminate()
                        proc.wait(timeout=5)
                        return True
            except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                continue
    except Exception as e:
        print(f"خطأ في البحث عن العمليات: {e}")
    return False

if __name__ == '__main__':
    print("🚀 بدء تشغيل نظام مراقبة التليجرام المحسن...")
    
    # التحقق من متغيرات البيئة المطلوبة
    if not os.environ.get('TELEGRAM_API_ID') or not os.environ.get('TELEGRAM_API_HASH'):
        print("⚠️  تحذير: لم يتم إعداد TELEGRAM_API_ID أو TELEGRAM_API_HASH")
        print("   يمكن تشغيل التطبيق ولكن ستحتاج لإضافة هذه المتغيرات لاحقاً")
    
    # إنهاء أي عملية تستخدم المنفذ 5000
    port = 5000
    if kill_process_on_port(port):
        import time
        time.sleep(2)  # انتظار قصير للتأكد من تحرير المنفذ
    
    try:
        # تحميل الجلسات الموجودة
        print("📁 تحميل الجلسات المحفوظة...")
        load_all_sessions()
        
        # تشغيل نظام التنبيهات
        print("🔔 بدء نظام التنبيهات...")
        alert_queue.start()
        
        print(f"🌐 تشغيل الخادم على المنفذ {port}...")
        print(f"🔗 رابط التطبيق: http://0.0.0.0:{port}")
        
        # تشغيل التطبيق
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=port, 
            debug=False,  # إيقاف وضع التطوير لتجنب المشاكل
            use_reloader=False,  # إيقاف إعادة التحميل التلقائي
            allow_unsafe_werkzeug=True
        )
        
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"❌ المنفذ {port} ما زال مُستخدماً. جاري المحاولة على منفذ آخر...")
            # تجربة منافذ أخرى
            for alternative_port in [5001, 5002, 5003, 8000, 8080]:
                try:
                    print(f"🔄 تجربة المنفذ {alternative_port}...")
                    socketio.run(
                        app, 
                        host='0.0.0.0', 
                        port=alternative_port, 
                        debug=False,
                        use_reloader=False,
                        allow_unsafe_werkzeug=True
                    )
                    break
                except OSError:
                    continue
            else:
                print("❌ فشل في العثور على منفذ متاح")
                sys.exit(1)
        else:
            print(f"❌ خطأ في تشغيل الخادم: {e}")
            sys.exit(1)
    
    except KeyboardInterrupt:
        print("\n⏹ تم إيقاف الخادم بواسطة المستخدم")
    
    except Exception as e:
        print(f"❌ خطأ غير متوقع: {e}")
        sys.exit(1)
