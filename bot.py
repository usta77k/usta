"""
==============================================================
بوت تيليجرام: مونتاج فيديو مزامَن مع إيقاع الموسيقى (Beat-Sync Editor)
==============================================================
المبدأ:
  1) المستخدم يرسل عدة مقاطع فيديو (Clips).
  2) يختار فلترًا (أبيض وأسود / سينمائي / بدون فلتر) عبر أزرار.
  3) يرسل مقطع صوتي/موسيقى - هذا يُشغّل المعالجة تلقائيًا.
  4) البوت يكتشف "الضربات" (Beats) في الصوت باستخدام librosa، يقصّ
     المقاطع بمدد تطابق الفواصل بين الضربات، يطبّق الفلتر، يدمجها
     مع الصوت الأصلي، ويرسل الفيديو النهائي.

القيود (لحماية أداء السيرفر):
  - كل مقطع مُدخل يُقتطع تلقائيًا لأقصى MAX_CLIP_SECONDS ثانية.
  - الفيديو النهائي محدود بأقصى MAX_OUTPUT_SECONDS ثانية (مدة الصوت).
  - عدد المقاطع محدود بـ MAX_CLIPS لكل جلسة.

المتطلبات: راجع requirements.txt
يتطلب أيضًا تثبيت "ffmpeg" و"libsndfile1" كحزم نظام (apt) على السيرفر.

التشغيل:
  1) ضع التوكن في متغير البيئة TELEGRAM_BOT_TOKEN (أو داخل الكود أدناه)
  2) شغّل: python bot.py
==============================================================
"""

import os
import io
import logging
import asyncio
import tempfile
import shutil
from typing import List, Dict, Optional

import numpy as np
import librosa
from moviepy.editor import (
    VideoFileClip, concatenate_videoclips, AudioFileClip,
)
from moviepy.video.fx.all import blackwhite

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------
# الإعدادات العامة
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8594065413:AAEL8kt5KGJnODIjkFVE7UpGTnsz_Br0BFY")

MAX_CLIPS = 15                 # أقصى عدد مقاطع فيديو لكل جلسة
MAX_CLIP_SECONDS = 120         # أقصى مدة يُقتطع لها كل مقطع مُدخل (دقيقتان)
MAX_OUTPUT_SECONDS = 120       # أقصى مدة للفيديو النهائي (يحدّها طول الصوت)
MIN_SEGMENT_SECONDS = 0.25     # أقصر "قصّة" مسموحة بين ضربتين متقاربتين جدًا
OUTPUT_WIDTH = 720             # عرض الفيديو النهائي (يحافظ على الأداء والحجم)

FILTER_NONE = "none"
FILTER_BW = "bw"
FILTER_CINEMATIC = "cinematic"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
if not FFMPEG_AVAILABLE:
    logger.warning("لم يتم العثور على ffmpeg! معالجة الفيديو لن تعمل.")


# --------------------------------------------------------------------------
# أدوات مساعدة: تخزين الجلسة لكل مستخدم
# --------------------------------------------------------------------------

def get_session(context: ContextTypes.DEFAULT_TYPE) -> Dict:
    """يهيّئ ويعيد بيانات جلسة المستخدم الحالية (مقاطع + فلتر + مجلد مؤقت)."""
    if "clips" not in context.user_data:
        context.user_data["clips"] = []            # قائمة مسارات ملفات محلية
        context.user_data["filter"] = FILTER_NONE
        context.user_data["tmp_dir"] = tempfile.mkdtemp(prefix="beatbot_")
    return context.user_data


def reset_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يمسح ملفات الجلسة المؤقتة ويعيد الحالة للبداية."""
    tmp_dir = context.user_data.get("tmp_dir")
    if tmp_dir and os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    context.user_data.clear()


# --------------------------------------------------------------------------
# منطق كشف الإيقاع وبناء المونتاج (Core Logic)
# --------------------------------------------------------------------------

def detect_beat_segment_durations(audio_path: str, total_duration: float) -> List[float]:
    """
    يحلل ملف الصوت ويعيد قائمة بمدد "القصّات" (بالثواني) بين كل ضربتين
    متتاليتين، مقصوصة على أقصى مدة الفيديو الناتج.
    """
    y, sr = librosa.load(audio_path, sr=None, mono=True, duration=total_duration)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()

    # نضمن وجود بداية عند 0 ونهاية عند مدة الصوت
    if not beat_times or beat_times[0] > 0.05:
        beat_times.insert(0, 0.0)
    if beat_times[-1] < total_duration - 0.05:
        beat_times.append(total_duration)

    durations = []
    for i in range(len(beat_times) - 1):
        seg = beat_times[i + 1] - beat_times[i]
        if seg >= MIN_SEGMENT_SECONDS:
            durations.append(seg)

    if not durations:
        # صوت بدون إيقاع واضح -> قصّات ثابتة كل ثانية واحدة
        n = max(1, int(total_duration))
        durations = [1.0] * n

    return durations


def _apply_filter(clip: VideoFileClip, filter_name: str) -> VideoFileClip:
    if filter_name == FILTER_BW:
        return blackwhite(clip)
    if filter_name == FILTER_CINEMATIC:
        def cinematic_frame(frame: np.ndarray) -> np.ndarray:
            # تدرج ألوان "سينمائي" بسيط: رفع تباين + ميل دافئ/بارد خفيف
            f = frame.astype(np.float32)
            f[:, :, 0] *= 0.92   # تقليل الأحمر قليلاً في الظلال
            f[:, :, 2] *= 1.08   # رفع الأزرق قليلاً (Teal في الظلال)
            f = (f - 128) * 1.15 + 128  # زيادة التباين
            return np.clip(f, 0, 255).astype(np.uint8)
        return clip.fl_image(cinematic_frame)
    return clip


def build_beat_synced_video(
    clip_paths: List[str],
    audio_path: str,
    filter_name: str,
    output_path: str,
) -> None:
    """يبني الفيديو النهائي المزامَن مع الإيقاع ويحفظه في output_path."""
    audio_clip = AudioFileClip(audio_path)
    total_duration = min(audio_clip.duration, MAX_OUTPUT_SECONDS)

    segment_durations = detect_beat_segment_durations(audio_path, total_duration)

    source_clips = [VideoFileClip(p) for p in clip_paths]
    cursors = [0.0] * len(source_clips)  # موضع القراءة الحالي داخل كل مقطع مصدر

    final_segments = []
    elapsed = 0.0
    clip_idx = 0

    for seg_dur in segment_durations:
        if elapsed >= total_duration:
            break
        seg_dur = min(seg_dur, total_duration - elapsed)

        src = source_clips[clip_idx % len(source_clips)]
        start = cursors[clip_idx % len(source_clips)]

        # إن تجاوزنا نهاية المقطع المصدر، نعيد اللف من البداية
        if start + seg_dur > src.duration:
            start = 0.0

        end = min(start + seg_dur, src.duration)
        sub = src.subclip(start, end)
        sub = sub.resize(width=OUTPUT_WIDTH)
        sub = _apply_filter(sub, filter_name)

        final_segments.append(sub)
        cursors[clip_idx % len(source_clips)] = end
        elapsed += (end - start)
        clip_idx += 1

    final_video = concatenate_videoclips(final_segments, method="compose")
    final_audio = audio_clip.subclip(0, final_video.duration)
    final_video = final_video.set_audio(final_audio)

    final_video.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac",
        fps=30,
        preset="veryfast",
        threads=2,
        logger=None,
    )

    for c in source_clips:
        c.close()
    audio_clip.close()
    final_video.close()


# --------------------------------------------------------------------------
# معالجات تيليجرام
# --------------------------------------------------------------------------

FILTER_LABELS = {
    FILTER_NONE: "🚫 No Filter",
    FILTER_BW: "⚫⚪ Black & White",
    FILTER_CINEMATIC: "🎬 Cinematic",
}


def build_filter_keyboard(selected: str) -> InlineKeyboardMarkup:
    rows = []
    for key, label in FILTER_LABELS.items():
        text = f"✅ {label}" if key == selected else label
        rows.append([InlineKeyboardButton(text, callback_data=f"filter:{key}")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_session(context)
    await update.message.reply_text(
        "👋 Welcome to the Beat-Sync Video Editor!\n\n"
        "How to use:\n"
        f"1️⃣ Send me your video clips (up to {MAX_CLIPS} clips, "
        f"{MAX_CLIP_SECONDS}s max each).\n"
        "2️⃣ Use /filter to choose a visual style.\n"
        "3️⃣ Send an audio/music file — this starts the auto-editing.\n\n"
        f"⏱ Final video length is limited to {MAX_OUTPUT_SECONDS}s.\n\n"
        "🤖 Bot developed by @usta77k"
    )


async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    await update.message.reply_text(
        "🎨 Choose a filter style:",
        reply_markup=build_filter_keyboard(session["filter"]),
    )


async def filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    filter_name = query.data.split(":", 1)[1]
    session = get_session(context)
    session["filter"] = filter_name
    await query.edit_message_text(
        f"🎨 Filter set to: {FILTER_LABELS[filter_name]}",
        reply_markup=build_filter_keyboard(filter_name),
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    if len(session["clips"]) >= MAX_CLIPS:
        await update.message.reply_text(
            f"⚠️ Maximum of {MAX_CLIPS} clips reached. Send an audio file to render now."
        )
        return

    media = update.message.video or update.message.document
    status = await update.message.reply_text("⏳ Saving clip...")

    tg_file = await media.get_file()
    local_path = os.path.join(session["tmp_dir"], f"clip_{len(session['clips'])}.mp4")
    await tg_file.download_to_drive(local_path)

    # نقتطع أي مقطع أطول من الحد المسموح فورًا لحماية الأداء لاحقًا
    try:
        loop = asyncio.get_running_loop()

        def trim_if_needed():
            with VideoFileClip(local_path) as c:
                if c.duration > MAX_CLIP_SECONDS:
                    trimmed_path = local_path.replace(".mp4", "_trim.mp4")
                    c.subclip(0, MAX_CLIP_SECONDS).write_videofile(
                        trimmed_path, codec="libx264", audio_codec="aac",
                        logger=None,
                    )
                    os.remove(local_path)
                    return trimmed_path
            return local_path

        final_path = await loop.run_in_executor(None, trim_if_needed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطأ أثناء معالجة المقطع")
        await status.edit_text(f"❌ Could not read this clip: {exc}")
        return

    session["clips"].append(final_path)
    await status.edit_text(
        f"✅ Clip {len(session['clips'])}/{MAX_CLIPS} saved.\n"
        "Send more clips, choose a filter with /filter, or send an audio "
        "file to render your video."
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)

    if not session["clips"]:
        await update.message.reply_text(
            "⚠️ Please send at least one video clip before sending audio."
        )
        return

    if not FFMPEG_AVAILABLE:
        await update.message.reply_text("❌ ffmpeg is not installed on the server.")
        return

    media = update.message.audio or update.message.voice or update.message.document
    status = await update.message.reply_text("⏳ Analyzing beat & rendering video...")

    tg_file = await media.get_file()
    audio_path = os.path.join(session["tmp_dir"], "audio_input")
    await tg_file.download_to_drive(audio_path)

    output_path = os.path.join(session["tmp_dir"], "final_output.mp4")

    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                build_beat_synced_video,
                session["clips"], audio_path, session["filter"], output_path,
            ),
            timeout=280,
        )

        caption = (
            "✅ *Beat-Synced Video Ready!*\n\n"
            f"🎨 Filter: {FILTER_LABELS[session['filter']]}\n\n"
            "🤖 Bot developed by @usta77k"
        )
        with open(output_path, "rb") as f:
            await update.message.reply_video(video=f, caption=caption, parse_mode="Markdown")

        await status.delete()

    except asyncio.TimeoutError:
        await status.edit_text("❌ Rendering took too long and was cancelled. Try fewer/shorter clips.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطأ أثناء المعالجة النهائية")
        await status.edit_text(f"❌ Unexpected error: {exc}")
    finally:
        reset_session(context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_session(context)
    await update.message.reply_text("🗑️ Session cleared. Send new clips to start again.")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Please send a video clip, or an audio file to render.")


# --------------------------------------------------------------------------
# نقطة التشغيل الرئيسية
# --------------------------------------------------------------------------

def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("الرجاء ضبط توكن البوت في BOT_TOKEN أو متغير البيئة TELEGRAM_BOT_TOKEN.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("filter", filter_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(filter_callback, pattern=r"^filter:"))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(
        MessageHandler(filters.AUDIO | filters.VOICE, handle_audio)
    )
    app.add_handler(
        MessageHandler(
            ~filters.VIDEO & ~filters.AUDIO & ~filters.VOICE & ~filters.COMMAND,
            handle_other,
        )
    )

    logger.info("البوت يعمل الآن ... اضغط Ctrl+C للإيقاف.")
    app.run_polling()


if __name__ == "__main__":
    main()
