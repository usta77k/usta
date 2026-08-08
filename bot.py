"""
==============================================================
بوت تيليجرام: حماية الدردشة من المحتوى الإباحي (Content Guard)
==============================================================
المبدأ:
  - يعمل داخل مجموعات تيليجرام (Groups/Supergroups).
  - يجب أن يكون البوت مشرفًا (Admin) بصلاحيتي:
      1) حذف الرسائل (Delete Messages)
      2) حظر الأعضاء (Ban Users)
  - عند إرسال أي صورة/فيديو/ملصق/GIF، يحلّله البوت عبر نموذج NudeNet
    لكشف المحتوى الإباحي (Nudity Detection).
  - إن كان المحتوى مخالفًا:
      * تُحذف الرسالة فورًا.
      * يُرسَل تحذير للمستخدم مع عدّاد المخالفات (1/3, 2/3, 3/3).
      * عند تجاوز 3 مخالفات -> يُحظر المستخدم تلقائيًا من المجموعة.
  - عدد المخالفات لكل مستخدم يُحفظ في ملف JSON محلي (يبقى بعد إعادة
    تشغيل البوت طالما لم يُعد نشر المشروع من الصفر على Railway، لأن
    التخزين المحلي هناك غير دائم بين عمليات إعادة النشر - راجع الملاحظة
    في نهاية الملف لحل دائم عبر Railway Volume).

المتطلبات: راجع requirements.txt
يتطلب أيضًا تثبيت "ffmpeg" و"libgl1" وما شابه كحزم نظام (apt) على السيرفر.
==============================================================
"""

import os
import json
import logging
import tempfile
import asyncio
from typing import List, Optional

import cv2
import numpy as np
from nudenet import NudeDetector

from telegram import Update, ChatPermissions
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------
# الإعدادات العامة
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8594065413:AAEL8kt5KGJnODIjkFVE7UpGTnsz_Br0BFY")

# مسار ملف حفظ عدّادات المخالفات (يدوم بين رسائل نفس التشغيلة)
VIOLATIONS_FILE = os.path.join(os.path.dirname(__file__), "data", "violations.json")

# عدد المخالفات المسموح بها قبل الحظر (بعد تجاوز هذا الرقم يُحظر المستخدم)
MAX_WARNINGS_BEFORE_BAN = 3

# حد ثقة الكشف لاعتبار المحتوى إباحيًا (0.0 - 1.0). ارفعه لتقليل الإنذارات
# الكاذبة، أو اخفضه لزيادة الحساسية.
NSFW_CONFIDENCE_THRESHOLD = 0.55

# التصنيفات التي يعتبرها NudeNet "إباحية صريحة" ويجب حظرها
EXPLICIT_LABELS = {
    "EXPOSED_ANUS",
    "EXPOSED_BUTTOCKS",
    "EXPOSED_BREAST_F",
    "EXPOSED_GENITALIA_F",
    "EXPOSED_GENITALIA_M",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
}

# عدد الإطارات التي تُفحص من كل فيديو/GIF (كافية للكشف السريع بدون إبطاء)
VIDEO_SAMPLE_FRAMES = 3

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

logger.info("جاري تحميل نموذج كشف المحتوى الإباحي (NudeNet) ...")
detector = NudeDetector()
logger.info("تم تحميل النموذج بنجاح.")


# --------------------------------------------------------------------------
# تخزين عدّادات المخالفات
# --------------------------------------------------------------------------

def _load_violations() -> dict:
    if os.path.exists(VIOLATIONS_FILE):
        try:
            with open(VIOLATIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            logger.warning("تعذّر قراءة ملف المخالفات، سيبدأ من جديد.")
    return {}


def _save_violations(data: dict) -> None:
    os.makedirs(os.path.dirname(VIOLATIONS_FILE), exist_ok=True)
    with open(VIOLATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_violations = _load_violations()


def get_violation_count(chat_id: int, user_id: int) -> int:
    return _violations.get(f"{chat_id}:{user_id}", 0)


def increment_violation(chat_id: int, user_id: int) -> int:
    key = f"{chat_id}:{user_id}"
    _violations[key] = _violations.get(key, 0) + 1
    _save_violations(_violations)
    return _violations[key]


def reset_violations(chat_id: int, user_id: int) -> None:
    key = f"{chat_id}:{user_id}"
    if key in _violations:
        del _violations[key]
        _save_violations(_violations)


# --------------------------------------------------------------------------
# منطق الكشف (Core Detection Logic)
# --------------------------------------------------------------------------

def _is_frame_explicit(frame_bgr: np.ndarray, tmp_dir: str) -> bool:
    """يحفظ الإطار مؤقتًا كصورة ويشغّل عليه NudeNet، يعيد True إن كان إباحيًا."""
    tmp_path = os.path.join(tmp_dir, "frame.jpg")
    cv2.imwrite(tmp_path, frame_bgr)
    try:
        results = detector.detect(tmp_path)
    except Exception:  # noqa: BLE001
        logger.exception("خطأ أثناء تشغيل نموذج الكشف")
        return False
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    for item in results:
        label = item.get("class", "")
        score = item.get("score", 0.0)
        if label in EXPLICIT_LABELS and score >= NSFW_CONFIDENCE_THRESHOLD:
            return True
    return False


def is_image_explicit(image_path: str, tmp_dir: str) -> bool:
    frame = cv2.imread(image_path)
    if frame is None:
        return False
    return _is_frame_explicit(frame, tmp_dir)


def is_video_explicit(video_path: str, tmp_dir: str) -> bool:
    """يفحص عدة إطارات موزّعة من الفيديو/GIF ويعيد True إن وُجد أي إطار مخالف."""
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    indices = sorted(set(
        int(total_frames * i / (VIDEO_SAMPLE_FRAMES + 1))
        for i in range(1, VIDEO_SAMPLE_FRAMES + 1)
    ))

    explicit_found = False
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        if _is_frame_explicit(frame, tmp_dir):
            explicit_found = True
            break

    cap.release()
    return explicit_found


# --------------------------------------------------------------------------
# معالج الوسائط الموحّد
# --------------------------------------------------------------------------

async def moderate_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return  # البوت يعمل فقط داخل المجموعات

    user = message.from_user
    chat_id = message.chat_id

    media_type: Optional[str] = None
    tg_file_obj = None

    if message.photo:
        media_type = "image"
        tg_file_obj = message.photo[-1]
    elif message.video:
        media_type = "video"
        tg_file_obj = message.video
    elif message.animation:
        media_type = "video"
        tg_file_obj = message.animation
    elif message.sticker:
        sticker = message.sticker
        if sticker.is_animated:
            return  # ملصقات Lottie المتجهة (.tgs) لا يمكن تحليلها كصورة
        media_type = "video" if sticker.is_video else "image"
        tg_file_obj = sticker
    else:
        return  # نوع رسالة لا يهمّنا (نص، إلخ)

    tmp_dir = tempfile.mkdtemp(prefix="guard_")
    try:
        tg_file = await tg_file_obj.get_file()
        ext = ".mp4" if media_type == "video" else ".jpg"
        local_path = os.path.join(tmp_dir, f"media{ext}")
        await tg_file.download_to_drive(local_path)

        loop = asyncio.get_running_loop()
        if media_type == "video":
            is_explicit = await loop.run_in_executor(None, is_video_explicit, local_path, tmp_dir)
        else:
            is_explicit = await loop.run_in_executor(None, is_image_explicit, local_path, tmp_dir)

        if is_explicit:
            await handle_violation(update, context)

    except Exception:  # noqa: BLE001
        logger.exception("خطأ أثناء فحص الوسائط")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_violation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = message.from_user
    chat_id = message.chat_id

    # حذف الرسالة المخالفة أولًا
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception:  # noqa: BLE001
        logger.warning("تعذّر حذف الرسالة - تأكد أن البوت مشرف بصلاحية حذف الرسائل.")

    count = increment_violation(chat_id, user.id)

    if count > MAX_WARNINGS_BEFORE_BAN:
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚫 تم حظر {user.mention_html()} نهائيًا من المجموعة "
                    f"لتكرار إرسال محتوى مخالف أكثر من {MAX_WARNINGS_BEFORE_BAN} مرات."
                ),
                parse_mode="HTML",
            )
            reset_violations(chat_id, user.id)
        except Exception:  # noqa: BLE001
            logger.warning("تعذّر حظر المستخدم - تأكد أن البوت مشرف بصلاحية حظر الأعضاء.")
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"⚠️ تحذير لـ {user.mention_html()}: تم رصد وحذف محتوى مخالف "
            f"(إباحي).\nعدد المخالفات: {count}/{MAX_WARNINGS_BEFORE_BAN}\n"
            f"عند تجاوز {MAX_WARNINGS_BEFORE_BAN} مخالفات سيتم حظرك تلقائيًا."
        ),
        parse_mode="HTML",
    )


# --------------------------------------------------------------------------
# أوامر عامة
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🛡️ بوت حماية الدردشة من المحتوى الإباحي\n\n"
        "أضفني كمشرف في مجموعتك بصلاحيتي:\n"
        "• حذف الرسائل (Delete Messages)\n"
        "• حظر الأعضاء (Ban Users)\n\n"
        "بعدها سأراقب الصور/الفيديوهات/الملصقات/GIF تلقائيًا وأحذف أي "
        f"محتوى إباحي، مع تحذير المستخدم، وحظره بعد {MAX_WARNINGS_BEFORE_BAN} مخالفات.\n\n"
        "أوامر المشرفين:\n"
        "/reset - إعادة تصفير مخالفات مستخدم (بالرد على رسالته)\n\n"
        "🤖 تم تطوير البوت بواسطة @usta77k"
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    member = await context.bot.get_chat_member(message.chat_id, message.from_user.id)
    if member.status not in ("administrator", "creator"):
        await message.reply_text("⚠️ هذا الأمر للمشرفين فقط.")
        return

    if not message.reply_to_message:
        await message.reply_text("↩️ الرجاء استخدام هذا الأمر بالرد على رسالة المستخدم المطلوب.")
        return

    target = message.reply_to_message.from_user
    reset_violations(message.chat_id, target.id)
    await message.reply_text(f"✅ تم تصفير مخالفات {target.mention_html()}.", parse_mode="HTML")


# --------------------------------------------------------------------------
# نقطة التشغيل الرئيسية
# --------------------------------------------------------------------------

def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("الرجاء ضبط توكن البوت في BOT_TOKEN أو متغير البيئة TELEGRAM_BOT_TOKEN.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Sticker.ALL,
            moderate_media,
        )
    )

    logger.info("البوت يعمل الآن ... اضغط Ctrl+C للإيقاف.")
    app.run_polling()


if __name__ == "__main__":
    main()
