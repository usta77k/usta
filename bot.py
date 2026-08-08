"""
==============================================================
بوت تيليجرام: مونتاج فيديو مزامَن مع إيقاع الموسيقى (Beat-Sync Editor)
==============================================================
المبدأ:
  1) المستخدم يختار لغة الواجهة (عربي/إنجليزي).
  2) يرسل عدة مقاطع فيديو (Clips).
  3) يختار فلترًا (أبيض وأسود / سينمائي / بدون فلتر) عبر أزرار.
  4) يرسل مقطع صوتي/موسيقى - هذا يُشغّل المعالجة تلقائيًا.
  5) البوت يكتشف "الضربات" (Beats) في الصوت، يقصّ المقاطع بمدد تطابق
     الفواصل بين الضربات، يطبّق الفلتر، يدمجها مع الصوت، ويرسل الفيديو.

أثناء المعالجة يُرسِل البوت تحديثات تقدّم حيّة (تحليل الإيقاع -> تجهيز
المقاطع -> الدمج النهائي) حتى لا يشعر المستخدم أن البوت "معلّق".

المتطلبات: راجع requirements.txt
يتطلب أيضًا تثبيت "ffmpeg" و"libsndfile1" كحزم نظام (apt) على السيرفر.
==============================================================
"""

import os
import time
import logging
import asyncio
import tempfile
import shutil
from typing import List, Dict

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

MAX_CLIPS = 15
MAX_CLIP_SECONDS = 120
MAX_OUTPUT_SECONDS = 120
MIN_SEGMENT_SECONDS = 0.25
OUTPUT_WIDTH = 720
# سرعة تصدير أسرع (تضحّي بقليل من الجودة مقابل وقت أقل بشكل ملحوظ)
FFMPEG_PRESET = "ultrafast"

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
# الترجمة (دعم عربي/إنجليزي)
# --------------------------------------------------------------------------

TEXTS: Dict[str, Dict[str, str]] = {
    "choose_lang": {
        "ar": "🌐 اختر لغة الواجهة:",
        "en": "🌐 Choose interface language:",
    },
    "lang_set": {
        "ar": "✅ تم ضبط اللغة على العربية.",
        "en": "✅ Language set to English.",
    },
    "welcome": {
        "ar": (
            "👋 أهلًا بك في بوت مونتاج الفيديو المزامَن مع الإيقاع!\n\n"
            "طريقة الاستخدام:\n"
            "1️⃣ أرسل مقاطع الفيديو (حتى {max_clips} مقطعًا، كل مقطع "
            "يُقتطع تلقائيًا لأقصى {max_clip}s).\n"
            "2️⃣ استخدم /filter لاختيار الفلتر.\n"
            "3️⃣ أرسل ملف صوت/موسيقى — هذا يبدأ المعالجة تلقائيًا.\n\n"
            "⏱ مدة الفيديو النهائي محدودة بـ {max_out}s.\n"
            "🌐 لتغيير اللغة في أي وقت: /language\n\n"
            "🤖 تم تطوير البوت بواسطة @usta77k"
        ),
        "en": (
            "👋 Welcome to the Beat-Sync Video Editor!\n\n"
            "How to use:\n"
            "1️⃣ Send your video clips (up to {max_clips}, {max_clip}s max each).\n"
            "2️⃣ Use /filter to choose a visual style.\n"
            "3️⃣ Send an audio/music file — this starts auto-editing.\n\n"
            "⏱ Final video length is limited to {max_out}s.\n"
            "🌐 Change language anytime: /language\n\n"
            "🤖 Bot developed by @usta77k"
        ),
    },
    "choose_filter": {"ar": "🎨 اختر أسلوب الفلتر:", "en": "🎨 Choose a filter style:"},
    "filter_set": {"ar": "🎨 تم ضبط الفلتر على: {name}", "en": "🎨 Filter set to: {name}"},
    "max_clips_reached": {
        "ar": "⚠️ وصلت للحد الأقصى ({max}) مقطعًا. أرسل ملف صوت الآن للمعالجة.",
        "en": "⚠️ Maximum of {max} clips reached. Send an audio file to render now.",
    },
    "saving_clip": {"ar": "⏳ جاري حفظ المقطع...", "en": "⏳ Saving clip..."},
    "clip_saved": {
        "ar": (
            "✅ تم حفظ المقطع {count}/{max}.\n"
            "أرسل مقاطع إضافية، اختر فلترًا عبر /filter، أو أرسل ملف صوت "
            "لبدء المعالجة."
        ),
        "en": (
            "✅ Clip {count}/{max} saved.\n"
            "Send more clips, choose a filter with /filter, or send an "
            "audio file to render your video."
        ),
    },
    "clip_error": {"ar": "❌ تعذّر قراءة هذا المقطع: {err}", "en": "❌ Could not read this clip: {err}"},
    "need_clip_first": {
        "ar": "⚠️ الرجاء إرسال مقطع فيديو واحد على الأقل قبل إرسال الصوت.",
        "en": "⚠️ Please send at least one video clip before sending audio.",
    },
    "ffmpeg_missing": {
        "ar": "❌ ffmpeg غير مثبّت على السيرفر.",
        "en": "❌ ffmpeg is not installed on the server.",
    },
    "stage_analyzing": {
        "ar": "🎵 جاري تحليل إيقاع الصوت...",
        "en": "🎵 Analyzing audio beat...",
    },
    "stage_preparing": {
        "ar": "🎬 جاري تجهيز المقاطع وتطبيق الفلتر ({done}/{total})...",
        "en": "🎬 Preparing clips & applying filter ({done}/{total})...",
    },
    "stage_rendering": {
        "ar": "📦 جاري دمج الفيديو النهائي (قد يستغرق دقيقة أو أكثر)...",
        "en": "📦 Rendering final video (may take a minute or more)...",
    },
    "render_done": {
        "ar": "✅ *الفيديو جاهز!*\n\n🎨 الفلتر: {filter}\n\n🤖 تم تطوير البوت بواسطة @usta77k",
        "en": "✅ *Beat-Synced Video Ready!*\n\n🎨 Filter: {filter}\n\n🤖 Bot developed by @usta77k",
    },
    "render_timeout": {
        "ar": "❌ استغرقت المعالجة وقتًا طويلاً جدًا وتم إلغاؤها. جرّب مقاطع أقل/أقصر.",
        "en": "❌ Rendering took too long and was cancelled. Try fewer/shorter clips.",
    },
    "render_error": {"ar": "❌ خطأ غير متوقع: {err}", "en": "❌ Unexpected error: {err}"},
    "session_cleared": {
        "ar": "🗑️ تم مسح الجلسة. أرسل مقاطع جديدة للبدء من جديد.",
        "en": "🗑️ Session cleared. Send new clips to start again.",
    },
    "send_video_or_audio": {
        "ar": "الرجاء إرسال مقطع فيديو، أو ملف صوت لبدء المعالجة.",
        "en": "Please send a video clip, or an audio file to render.",
    },
}

FILTER_LABELS = {
    "ar": {FILTER_NONE: "🚫 بدون فلتر", FILTER_BW: "⚫⚪ أبيض وأسود", FILTER_CINEMATIC: "🎬 سينمائي"},
    "en": {FILTER_NONE: "🚫 No Filter", FILTER_BW: "⚫⚪ Black & White", FILTER_CINEMATIC: "🎬 Cinematic"},
}


def t(key: str, lang: str, **kwargs) -> str:
    template = TEXTS[key].get(lang, TEXTS[key]["en"])
    return template.format(**kwargs) if kwargs else template


# --------------------------------------------------------------------------
# إدارة جلسة المستخدم
# --------------------------------------------------------------------------

def get_session(context: ContextTypes.DEFAULT_TYPE) -> Dict:
    if "clips" not in context.user_data:
        context.user_data["clips"] = []
        context.user_data["filter"] = FILTER_NONE
        context.user_data["tmp_dir"] = tempfile.mkdtemp(prefix="beatbot_")
    context.user_data.setdefault("lang", None)  # None = لم تُختر بعد
    return context.user_data


def reset_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    tmp_dir = context.user_data.get("tmp_dir")
    lang = context.user_data.get("lang")
    if tmp_dir and os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    context.user_data.clear()
    context.user_data["lang"] = lang  # نحافظ على تفضيل اللغة بعد إعادة الضبط


def get_lang(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("lang") or "en"


# --------------------------------------------------------------------------
# منطق كشف الإيقاع وبناء المونتاج
# --------------------------------------------------------------------------

def detect_beat_segment_durations(audio_path: str, total_duration: float) -> List[float]:
    y, sr = librosa.load(audio_path, sr=None, mono=True, duration=total_duration)
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()

    if not beat_times or beat_times[0] > 0.05:
        beat_times.insert(0, 0.0)
    if beat_times[-1] < total_duration - 0.05:
        beat_times.append(total_duration)

    durations = [
        beat_times[i + 1] - beat_times[i]
        for i in range(len(beat_times) - 1)
        if beat_times[i + 1] - beat_times[i] >= MIN_SEGMENT_SECONDS
    ]
    if not durations:
        durations = [1.0] * max(1, int(total_duration))
    return durations


def _apply_filter(clip: VideoFileClip, filter_name: str) -> VideoFileClip:
    if filter_name == FILTER_BW:
        return blackwhite(clip)
    if filter_name == FILTER_CINEMATIC:
        def cinematic_frame(frame: np.ndarray) -> np.ndarray:
            f = frame.astype(np.float32)
            f[:, :, 0] *= 0.92
            f[:, :, 2] *= 1.08
            f = (f - 128) * 1.15 + 128
            return np.clip(f, 0, 255).astype(np.uint8)
        return clip.fl_image(cinematic_frame)
    return clip


def build_beat_synced_video(
    clip_paths: List[str],
    audio_path: str,
    filter_name: str,
    output_path: str,
    stage_cb,
) -> None:
    """stage_cb(stage_key, done, total) تُستدعى دوريًا لتحديث المستخدم بالتقدّم."""
    stage_cb("stage_analyzing", 0, 1)
    audio_clip = AudioFileClip(audio_path)
    total_duration = min(audio_clip.duration, MAX_OUTPUT_SECONDS)
    segment_durations = detect_beat_segment_durations(audio_path, total_duration)

    source_clips = [VideoFileClip(p) for p in clip_paths]
    cursors = [0.0] * len(source_clips)

    final_segments = []
    elapsed = 0.0
    clip_idx = 0
    total_segments = len(segment_durations)

    for seg_num, seg_dur in enumerate(segment_durations, start=1):
        if elapsed >= total_duration:
            break
        stage_cb("stage_preparing", seg_num, total_segments)

        seg_dur = min(seg_dur, total_duration - elapsed)
        src = source_clips[clip_idx % len(source_clips)]
        start = cursors[clip_idx % len(source_clips)]

        if start + seg_dur > src.duration:
            start = 0.0

        end = min(start + seg_dur, src.duration)
        sub = src.subclip(start, end).resize(width=OUTPUT_WIDTH)
        sub = _apply_filter(sub, filter_name)

        final_segments.append(sub)
        cursors[clip_idx % len(source_clips)] = end
        elapsed += (end - start)
        clip_idx += 1

    stage_cb("stage_rendering", 0, 1)
    final_video = concatenate_videoclips(final_segments, method="compose")
    final_audio = audio_clip.subclip(0, final_video.duration)
    final_video = final_video.set_audio(final_audio)

    final_video.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac",
        fps=30,
        preset=FFMPEG_PRESET,
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

def build_lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇸🇦 العربية", callback_data="lang:ar"),
        InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
    ]])


def build_filter_keyboard(lang: str, selected: str) -> InlineKeyboardMarkup:
    rows = []
    for key, label in FILTER_LABELS[lang].items():
        text = f"✅ {label}" if key == selected else label
        rows.append([InlineKeyboardButton(text, callback_data=f"filter:{key}")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_session(context)
    await update.message.reply_text(
        t("choose_lang", "ar") + " / " + t("choose_lang", "en"),
        reply_markup=build_lang_keyboard(),
    )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        t("choose_lang", "ar") + " / " + t("choose_lang", "en"),
        reply_markup=build_lang_keyboard(),
    )


async def lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    get_session(context)
    context.user_data["lang"] = lang
    await query.edit_message_text(t("lang_set", lang))
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=t("welcome", lang, max_clips=MAX_CLIPS, max_clip=MAX_CLIP_SECONDS, max_out=MAX_OUTPUT_SECONDS),
    )


async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    lang = get_lang(context)
    await update.message.reply_text(
        t("choose_filter", lang),
        reply_markup=build_filter_keyboard(lang, session["filter"]),
    )


async def filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    filter_name = query.data.split(":", 1)[1]
    session = get_session(context)
    lang = get_lang(context)
    session["filter"] = filter_name
    await query.edit_message_text(
        t("filter_set", lang, name=FILTER_LABELS[lang][filter_name]),
        reply_markup=build_filter_keyboard(lang, filter_name),
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    lang = get_lang(context)

    if len(session["clips"]) >= MAX_CLIPS:
        await update.message.reply_text(t("max_clips_reached", lang, max=MAX_CLIPS))
        return

    media = update.message.video or update.message.document
    status = await update.message.reply_text(t("saving_clip", lang))

    tg_file = await media.get_file()
    local_path = os.path.join(session["tmp_dir"], f"clip_{len(session['clips'])}.mp4")
    await tg_file.download_to_drive(local_path)

    try:
        loop = asyncio.get_running_loop()

        def trim_if_needed():
            with VideoFileClip(local_path) as c:
                if c.duration > MAX_CLIP_SECONDS:
                    trimmed_path = local_path.replace(".mp4", "_trim.mp4")
                    c.subclip(0, MAX_CLIP_SECONDS).write_videofile(
                        trimmed_path, codec="libx264", audio_codec="aac", logger=None,
                    )
                    os.remove(local_path)
                    return trimmed_path
            return local_path

        final_path = await loop.run_in_executor(None, trim_if_needed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطأ أثناء معالجة المقطع")
        await status.edit_text(t("clip_error", lang, err=exc))
        return

    session["clips"].append(final_path)
    await status.edit_text(t("clip_saved", lang, count=len(session["clips"]), max=MAX_CLIPS))


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    lang = get_lang(context)

    if not session["clips"]:
        await update.message.reply_text(t("need_clip_first", lang))
        return
    if not FFMPEG_AVAILABLE:
        await update.message.reply_text(t("ffmpeg_missing", lang))
        return

    media = update.message.audio or update.message.voice or update.message.document
    status = await update.message.reply_text(t("stage_analyzing", lang))

    tg_file = await media.get_file()
    audio_path = os.path.join(session["tmp_dir"], "audio_input")
    await tg_file.download_to_drive(audio_path)

    output_path = os.path.join(session["tmp_dir"], "final_output.mp4")

    loop = asyncio.get_running_loop()
    last_edit_time = {"t": 0.0}

    def stage_cb(stage_key: str, done: int, total: int) -> None:
        now = time.time()
        # لا نُحدّث الرسالة أكثر من مرة كل ثانيتين حتى لا نتجاوز حدود تيليجرام
        if now - last_edit_time["t"] < 2.0 and stage_key == "stage_preparing":
            return
        last_edit_time["t"] = now
        if stage_key == "stage_preparing":
            text = t(stage_key, lang, done=done, total=total)
        else:
            text = t(stage_key, lang)
        asyncio.run_coroutine_threadsafe(status.edit_text(text), loop)

    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                build_beat_synced_video,
                session["clips"], audio_path, session["filter"], output_path, stage_cb,
            ),
            timeout=280,
        )

        caption = t("render_done", lang, filter=FILTER_LABELS[lang][session["filter"]])
        with open(output_path, "rb") as f:
            await update.message.reply_video(video=f, caption=caption, parse_mode="Markdown")
        await status.delete()

    except asyncio.TimeoutError:
        await status.edit_text(t("render_timeout", lang))
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطأ أثناء المعالجة النهائية")
        await status.edit_text(t("render_error", lang, err=exc))
    finally:
        reset_session(context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(context)
    reset_session(context)
    await update.message.reply_text(t("session_cleared", lang))


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(context)
    await update.message.reply_text(t("send_video_or_audio", lang))


# --------------------------------------------------------------------------
# نقطة التشغيل الرئيسية
# --------------------------------------------------------------------------

def main() -> None:
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("الرجاء ضبط توكن البوت في BOT_TOKEN أو متغير البيئة TELEGRAM_BOT_TOKEN.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CommandHandler("filter", filter_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(lang_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(filter_callback, pattern=r"^filter:"))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
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
