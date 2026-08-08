import logging
import sqlite3
import sys
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes

# محاولة استيراد instagrapi مع معالجة خطأ PIL
try:
    from instagrapi import Client
except ImportError as e:
    if "PIL" in str(e):
        print("⚠️ جاري تثبيت Pillow...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow>=10.0.0"])
        from instagrapi import Client
    else:
        raise

# ========== إعدادات ==========
TOKEN = "8594065413:AAEL8kt5KGJnODIjkFVE7UpGTnsz_Br0BFY"

# ========== مراحل المحادثة ==========
USERNAME, PASSWORD, TARGET = range(3)

# ========== قاعدة البيانات ==========
def init_db():
    conn = sqlite3.connect("accounts.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        insta_username TEXT UNIQUE,
        insta_password TEXT,
        target_username TEXT,
        status TEXT DEFAULT 'active',
        follow_count INTEGER DEFAULT 0,
        date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def save_account(insta_user, insta_pass, target_user):
    conn = sqlite3.connect("accounts.db")
    c = conn.cursor()
    try:
        c.execute("INSERT INTO accounts (insta_username, insta_password, target_username) VALUES (?, ?, ?)",
                  (insta_user, insta_pass, target_user))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

# ========== دوال إنستغرام ==========
def insta_login_and_follow(insta_user, insta_pass, target_user):
    cl = Client()
    try:
        cl.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        cl.login(insta_user, insta_pass)
        user_id = cl.user_id_from_username(target_user)
        cl.user_follow(user_id)
        return True, "✅ تم متابعة الحساب بنجاح"
    except Exception as e:
        return False, f"❌ فشل: {str(e)}"

# ========== أوامر البوت ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 مرحباً بك في بوت زيادة المتابعين 🔥\n\n"
        "⚠️ استخدم حسابات ثانوية فقط\n\n"
        "الرجاء إدخال **يوزر إنستغرام** (حساب ثانوي غير مهم):"
    )
    return USERNAME

async def get_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['insta_user'] = update.message.text.strip()
    await update.message.reply_text("📌 الآن أدخل **كلمة المرور** لهذا الحساب:")
    return PASSWORD

async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['insta_pass'] = update.message.text.strip()
    await update.message.reply_text("🎯 أدخل **يوزر الحساب المستهدف** (الذي تريد زيادة متابعيه):")
    return TARGET

async def get_target_and_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.text.strip()
    insta_user = context.user_data['insta_user']
    insta_pass = context.user_data['insta_pass']

    await update.message.reply_text("⏳ جاري تسجيل الدخول ومتابعة الحساب المستهدف...")

    success, msg = insta_login_and_follow(insta_user, insta_pass, target)

    if success:
        saved = save_account(insta_user, insta_pass, target)
        if saved:
            await update.message.reply_text(
                f"✅ {msg}\n\n"
                f"📁 تم حفظ الحساب في قاعدة البوت.\n"
                f"👤 الحساب: {insta_user}\n"
                f"🎯 المستهدف: {target}"
            )
        else:
            await update.message.reply_text("⚠️ الحساب مسجل مسبقاً، لكن المتابعة تمت بنجاح.")
    else:
        await update.message.reply_text(f"❌ {msg}\n\nيرجى التأكد من صحة البيانات وإعادة المحاولة.")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم إلغاء العملية.")
    return ConversationHandler.END

# ========== تشغيل البوت ==========
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_target_and_execute)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(conv_handler)
    print("🤖 البوت يعمل على Railway...")
    app.run_polling()

if __name__ == "__main__":
    main()