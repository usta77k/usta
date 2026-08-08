"""
==============================================================
بوت تيليجرام: مونتاج فيديو مزامَن مع إيقاع الموسيقى (Beat-Sync Editor)
==============================================================
تدفق الاستخدام الجديد:
  1) المستخدم يختار لغة الواجهة.
  2) يرسل ملف الصوت/الموسيقى أولًا -> البوت يحلل مدته وإيقاعه بسرعة
     (فك ترميز مباشر عبر ffmpeg->WAV ثم librosa، بدل المسار البطيء
     المعرّض للتعليق).
  3) البوت يخبره بمدة الصوت ويطلب إرسال لقطات فيديو (بدون حد أقصى
     لعددها) حتى تُغطّي مجموع مددها مدة الصوت.
  4) بمجرد اكتمال التغطية، تبدأ المعالجة والإرسال تلقائيًا فورًا.

المتطلبات: راجع requirements.txt
يتطلب أيضًا تثبيت "ffmpeg" و"libsndfile1" كحزم نظام (apt) على السيرفر.
==============================================================
"""

import os
import time
import logging
import asyncio
import subprocess
import tempfile
import shutil
from typing import List, Dict, Tuple

import numpy as np
import soundfile as sf
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

MAX_CLIP_SECONDS = 120
MAX_OUTPUT_SECONDS = 120
MIN_SEGMENT_SECONDS = 0.25
OUTPUT_WIDTH = 640                 # مخفّض قليلاً لتحسين السرعة والذاكرة
FFMPEG_PRESET = "ultrafast"
AUDIO_ANALYSIS_TIMEOUT = 40        # ثانية - لو تجاوزها فك ترميز الصوت نلغي
AUDIO_DECODE_SR = 22050            # معدل عيّنات مخفّض = تحليل أسرع بكثير
RENDER_TIMEOUT = 280

FILTER_NONE = "none"
FILTER_BW = "bw"
FILTER_CINEMATIC = "cinematic"

STATE_WAITING_AUDIO = "waiting_audio"
STATE_WAITING_CLIPS = "waiting_clips"
STATE_RENDERING = "rendering"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
if not FFMPEG_AVAILABLE:
    logger.warning("لم يتم العثور على ffmpeg! معالجة الفيديو لن تعمل.")


# --------------------------------------------------------------------------
# الترجمة
# --------------------------------------------------------------------------

TEXTS: Dict[str, Dict[str, str]] = {
    "choose_lang": {"ar": "🌐 اختر لغة الواجهة:", "en": "🌐 Choose interface language:"},
    "lang_set": {"ar": "✅ تم ضبط اللغة على العربية.", "en": "✅ Language set to English."},
    "welcome": {
        "ar": (
            "👋 أهلًا بك في بوت مونتاج الفيديو المزامَن مع الإيقاع!\n\n"
            "🎵 أرسل ملف الصوت/الموسيقى أولًا لنبدأ.\n"
            "🎨 يمكنك اختيار الفلتر في أي وقت عبر /filter\n"
            "🌐 لتغيير اللغة: /language | لإلغاء الجلسة: /cancel\n\n"
            "🤖 تم تطوير البوت بواسطة @usta77k"
        ),
        "en": (
            "👋 Welcome to the Beat-Sync Video Editor!\n\n"
            "🎵 Send your audio/music file first to begin.\n"
            "🎨 Choose a filter anytime with /filter\n"
            "🌐 Change language: /language | Cancel session: /cancel\n\n"
            "🤖 Bot developed by @usta77k"
        ),
    },
    "choose_filter": {"ar": "🎨 اختر أسلوب الفلتر:", "en": "🎨 Choose a filter style:"},
    "filter_set": {"ar": "🎨 تم ضبط الفلتر على: {name}", "en": "🎨 Filter set to: {name}"},
    "analyzing_audio": {"ar": "🎵 جاري تحليل الصوت بسرعة...", "en": "🎵 Quickly analyzing audio..."},
    "audio_ready": {
        "ar": (
            "✅ تم تحليل الصوت! المدة: {dur:.0f} ثانية.\n\n"
            "📹 الآن أرسل لقطات الفيديو (بدون حد لعددها) — سأجمعها تلقائيًا "
            "حتى تكفي مدة الصوت، وحينها سأبدأ المعالجة والإرسال فورًا.\n\n"
            "التغطية الحالية: 0/{dur:.0f} ثانية"
        ),
        "en": (
            "✅ Audio analyzed! Duration: {dur:.0f}s.\n\n"
            "📹 Now send video clips (no limit on count) — I'll collect "
            "them until their total covers the audio, then auto-render "
            "and send immediately.\n\n"
            "Coverage: 0/{dur:.0f}s"
        ),
    },
    "audio_error": {
        "ar": "❌ تعذّر تحليل الصوت (قد يكون تنسيقًا غير مدعوم أو تجاوز الوقت المسموح): {err}",
        "en": "❌ Could not analyze the audio (unsupported format or timeout): {err}",
    },
    "send_audio_first": {
        "ar": "🎵 الرجاء إرسال ملف الصوت/الموسيقى أولًا لبدء الجلسة.",
        "en": "🎵 Please send an audio/music file first to start the session.",
    },
    "saving_clip": {"ar": "⏳ جاري حفظ المقطع...", "en": "⏳ Saving clip..."},
    "clip_error": {"ar": "❌ تعذّر قراءة هذا المقطع: {err}", "en": "❌ Could not read this clip: {err}"},
    "clip_progress": {
        "ar": "✅ تم الحفظ. التغطية: {covered:.0f}/{needed:.0f} ثانية.",
        "en": "✅ Saved. Coverage: {covered:.0f}/{needed:.0f}s.",
    },
    "already_rendering": {
        "ar": "⏳ جاري إنشاء الفيديو النهائي، الرجاء الانتظار...",
        "en": "⏳ Final video is already being rendered, please wait...",
    },
    "ffmpeg_missing": {"ar": "❌ ffmpeg غير مثبّت على السيرفر.", "en": "❌ ffmpeg is not installed on the server."},
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
        "ar": "❌ استغرقت المعالجة وقتًا طويلاً جدًا وتم إلغاؤها. جرّب لقطات أقل/أقصر أو صوتًا أقصر.",
        "en": "❌ Rendering took too long and was cancelled. Try fewer/shorter clips or shorter audio.",
    },
    "render_error": {"ar": "❌ خطأ غير متوقع: {err}", "en": "❌ Unexpected error: {err}"},
    "session_cleared": {"ar": "🗑️ تم مسح الجلسة. أرسل صوتًا للبدء من جديد.", "en": "🗑️ Session cleared. Send audio to start again."},
    "send_video_only": {
        "ar": "الرجاء إرسال مقطع فيديو (المرحلة الحالية: إرسال اللقطات).",
        "en": "Please send a video clip (current stage: sending clips).",
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
    if "state" not in context.user_data:
        context.user_data["state"] = STATE_WAITING_AUDIO
        context.user_data["filter"] = FILTER_NONE
        context.user_data["clips"] = []          # [(path, duration), ...]
        context.user_data["covered"] = 0.0
        context.user_data["tmp_dir"] = tempfile.mkdtemp(prefix="beatbot_")
    context.user_data.setdefault("lang", None)
    return context.user_data


def reset_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    tmp_dir = context.user_data.get("tmp_dir")
    lang = context.user_data.get("lang")
    if tmp_dir and os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    context.user_data.clear()
    context.user_data["lang"] = lang


def get_lang(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("lang") or "en"


# --------------------------------------------------------------------------
# فك ترميز صوت سريع وموثوق (بديل عن librosa.load المعرّض للتعليق)
# --------------------------------------------------------------------------

def decode_audio_fast(input_path: str, wav_out_path: str) -> Tuple[np.ndarray, int, float]:
    """
    يحوّل أي ملف صوتي إلى WAV أحادي 22.05kHz عبر استدعاء ffmpeg مباشر
    (مع مهلة صريحة تمنع التعليق الأبدي)، ثم يقرأه بمكتبة soundfile
    السريعة والموثوقة بدل مسار librosa.load البطيء/المعرّض للتعليق.
    """
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ac", "1", "-ar", str(AUDIO_DECODE_SR),
        wav_out_path,
    ]
    subprocess.run(
        cmd, check=True, capture_output=True, timeout=AUDIO_ANALYSIS_TIMEOUT,
    )
    y, sr = sf.read(wav_out_path, dtype="float32", always_2d=False)
    duration = len(y) / sr
    return y, sr, duration


def detect_beat_segment_durations(y: np.ndarray, sr: int, total_duration: float) -> List[float]:
    tempo, beat_frames = librosa.beat.beat_track(
        y=y, sr=sr, hop_length=1024, units="frames"
    )
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=1024).tolist()

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


def analyze_audio(input_path: str, tmp_dir: str) -> Tuple[float, List[float]]:
    """يُشغَّل داخل executor: يعيد (مدة الصوت الفعلية، قائمة مدد القصّات)."""
    wav_path = os.path.join(tmp_dir, "audio_analysis.wav")
    y, sr, full_duration = decode_audio_fast(input_path, wav_path)
    total_duration = min(full_duration, MAX_OUTPUT_SECONDS)
    if total_duration < full_duration:
        y = y[: int(total_duration * sr)]
    segments = detect_beat_segment_durations(y, sr, total_duration)
    os.remove(wav_path)
    return total_duration, segments


# --------------------------------------------------------------------------
# بناء المونتاج النهائي
# --------------------------------------------------------------------------

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
    segment_durations: List[float],
    total_duration: float,
    audio_wav_path: str,
    filter_name: str,
    output_path: str,
    stage_cb,
) -> None:
    audio_clip = AudioFileClip(audio_wav_path)
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
        output_path, codec="libx264", audio_codec="aac",
        fps=30, preset=FFMPEG_PRESET, threads=2, logger=None,
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
    await context.bot.send_message(chat_id=query.message.chat_id, text=t("welcome", lang))


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


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    lang = get_lang(context)

    if not FFMPEG_AVAILABLE:
        await update.message.reply_text(t("ffmpeg_missing", lang))
        return
    if session["state"] == STATE_RENDERING:
        await update.message.reply_text(t("already_rendering", lang))
        return

    media = update.message.audio or update.message.voice or update.message.document
    status = await update.message.reply_text(t("analyzing_audio", lang))

    tg_file = await media.get_file()
    audio_input_path = os.path.join(session["tmp_dir"], "audio_original")
    await tg_file.download_to_drive(audio_input_path)

    loop = asyncio.get_running_loop()
    try:
        total_duration, segments = await asyncio.wait_for(
            loop.run_in_executor(None, analyze_audio, audio_input_path, session["tmp_dir"]),
            timeout=AUDIO_ANALYSIS_TIMEOUT + 10,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطأ أثناء تحليل الصوت")
        await status.edit_text(t("audio_error", lang, err=exc))
        return

    # نحوّل الصوت أيضًا لملف WAV نهائي جاهز للدمج لاحقًا (بدل إعادة فك الترميز)
    final_audio_wav = os.path.join(session["tmp_dir"], "audio_final.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_input_path, "-ac", "2", "-ar", "44100", final_audio_wav],
        check=True, capture_output=True, timeout=AUDIO_ANALYSIS_TIMEOUT,
    )

    session["audio_wav"] = final_audio_wav
    session["audio_duration"] = total_duration
    session["segments"] = segments
    session["clips"] = []
    session["covered"] = 0.0
    session["state"] = STATE_WAITING_CLIPS

    await status.edit_text(t("audio_ready", lang, dur=total_duration))


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    lang = get_lang(context)

    if session["state"] == STATE_WAITING_AUDIO:
        await update.message.reply_text(t("send_audio_first", lang))
        return
    if session["state"] == STATE_RENDERING:
        await update.message.reply_text(t("already_rendering", lang))
        return

    media = update.message.video or update.message.document
    status = await update.message.reply_text(t("saving_clip", lang))

    tg_file = await media.get_file()
    local_path = os.path.join(session["tmp_dir"], f"clip_{len(session['clips'])}.mp4")
    await tg_file.download_to_drive(local_path)

    loop = asyncio.get_running_loop()
    try:
        def read_and_trim():
            with VideoFileClip(local_path) as c:
                dur = min(c.duration, MAX_CLIP_SECONDS)
                if c.duration > MAX_CLIP_SECONDS:
                    trimmed_path = local_path.replace(".mp4", "_trim.mp4")
                    c.subclip(0, MAX_CLIP_SECONDS).write_videofile(
                        trimmed_path, codec="libx264", audio_codec="aac", logger=None,
                    )
                    os.remove(local_path)
                    return trimmed_path, dur
            return local_path, dur

        final_path, duration = await loop.run_in_executor(None, read_and_trim)
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطأ أثناء معالجة المقطع")
        await status.edit_text(t("clip_error", lang, err=exc))
        return

    session["clips"].append(final_path)
    session["covered"] += duration

    needed = session["audio_duration"]
    await status.edit_text(t("clip_progress", lang, covered=session["covered"], needed=needed))

    if session["covered"] >= needed:
        await render_final_video(update, context)


async def render_final_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_session(context)
    lang = get_lang(context)
    session["state"] = STATE_RENDERING

    status = await update.message.reply_text(t("stage_rendering", lang))
    output_path = os.path.join(session["tmp_dir"], "final_output.mp4")

    loop = asyncio.get_running_loop()
    last_edit_time = {"t": 0.0}

    def stage_cb(stage_key: str, done: int, total: int) -> None:
        now = time.time()
        if now - last_edit_time["t"] < 2.0 and stage_key == "stage_preparing":
            return
        last_edit_time["t"] = now
        text = t(stage_key, lang, done=done, total=total) if stage_key == "stage_preparing" else t(stage_key, lang)
        asyncio.run_coroutine_threadsafe(status.edit_text(text), loop)

    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None, build_beat_synced_video,
                session["clips"], session["segments"], session["audio_duration"],
                session["audio_wav"], session["filter"], output_path, stage_cb,
            ),
            timeout=RENDER_TIMEOUT,
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
    session = get_session(context)
    lang = get_lang(context)
    if session["state"] == STATE_WAITING_CLIPS:
        await update.message.reply_text(t("send_video_only", lang))
    else:
        await update.message.reply_text(t("send_audio_first", lang))


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
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
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
