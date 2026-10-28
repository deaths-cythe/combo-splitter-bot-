from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes, ConversationHandler

from .config import Config
from .splitter import (
    SplitError,
    SplitResult,
    TooManyParts,
    sanitize_filename,
    split_by_lines,
    split_by_size,
    split_into_n_parts,
)

log = logging.getLogger(__name__)

(
    WAIT_FILE,
    CHOOSE_MODE,
    MODE_VALUE,
    OPT_HEADER,
    OPT_ZIP,
    OPT_PREVIEW,
) = range(6)

MODE_LINES = "lines"
MODE_SIZE = "size"
MODE_EQUAL = "equal"

MODE_LABELS = {
    MODE_LINES: "By lines per part",
    MODE_SIZE: "By max file size per part",
    MODE_EQUAL: "Into N equal parts",
}

ALLOWED_EXTS = {".txt", ".csv", ".combo", ".log", ".dat", ".lst", ".text"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    workdir = context.user_data.get("workdir")
    if workdir:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            log.exception("cleanup failed")
    context.user_data.pop("workdir", None)


def _kv(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data


# ---------------------------------------------------------------------------
# entry commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Combo splitter bot.\n\n"
        "/split — start a split job\n"
        "/help  — usage and limits\n"
        "/cancel — abort the current job\n\n"
        "Send me a text/combo file, pick a split mode, get the parts back."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["config"]
    await update.message.reply_text(
        "How to use:\n"
        "1. /split\n"
        f"2. Upload a text file (max {cfg.max_download_mb} MB).\n"
        "3. Pick a split mode.\n"
        "4. Enter the value for that mode.\n"
        "5. Answer the optional questions.\n"
        "6. Receive the parts as documents or a ZIP.\n\n"
        "Modes:\n"
        "• Lines per part — every part gets N lines.\n"
        "• Max size per part — parts stay under N KB, whole lines intact.\n"
        "• N equal parts — line count is divided as evenly as possible.\n\n"
        "Limits:\n"
        f"• Max incoming file: {cfg.max_download_mb} MB\n"
        f"• Max outgoing file: {cfg.max_upload_mb} MB\n"
        f"• Max parts per job: {cfg.max_parts}\n\n"
        "Larger files: run a self-hosted local Bot API server and set "
        "TELEGRAM_API_BASE_URL. See README for instructions.\n\n"
        "Supported extensions: " + ", ".join(sorted(ALLOWED_EXTS)) + "\n"
        "Files are staged in a temp dir and deleted after the job."
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _cleanup(context)
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /split → WAIT_FILE
# ---------------------------------------------------------------------------

async def split_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Config = context.bot_data["config"]
    uid = update.effective_user.id

    # simple per-user rate limit
    rate = context.bot_data.setdefault("rate", {})
    now = time.monotonic()
    if now - rate.get(uid, 0.0) < 2.0:
        await update.message.reply_text("Slow down. Try again in a couple of seconds.")
        return ConversationHandler.END
    rate[uid] = now

    await _cleanup(context)
    context.user_data.clear()

    try:
        workdir = Path(tempfile.mkdtemp(prefix=f"combo_{uid}_", dir=cfg.temp_dir))
    except OSError as exc:
        log.exception("mkdtemp failed")
        await update.message.reply_text(f"Server storage error: {exc}")
        return ConversationHandler.END

    _kv(context)["workdir"] = str(workdir)

    await update.message.reply_text(
        f"Send me a text/combo file. Max size: {cfg.max_download_mb} MB.\n"
        f"Allowed: {', '.join(sorted(ALLOWED_EXTS))}.\n"
        f"/cancel to abort."
    )
    return WAIT_FILE


# ---------------------------------------------------------------------------
# file ingestion
# ---------------------------------------------------------------------------

async def received_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Config = context.bot_data["config"]
    msg = update.message
    doc = msg.document

    if doc is None:
        await msg.reply_text("Send the file as a document. Try again or /cancel.")
        return WAIT_FILE

    size = doc.file_size or 0
    if size == 0:
        await msg.reply_text("Empty file or unknown size. Try again or /cancel.")
        return WAIT_FILE
    if size > cfg.max_download_bytes:
        await msg.reply_text(
            f"File is {size / 1024 / 1024:.1f} MB, over the {cfg.max_download_mb} MB "
            "limit for this bot.\n\n"
            "Options:\n"
            "• Split it locally and send the pieces.\n"
            "• Or run a self-hosted local Bot API server and set "
            "TELEGRAM_API_BASE_URL (see /help)."
        )
        return WAIT_FILE

    name = sanitize_filename(doc.file_name or "file.txt")
    ext = Path(name).suffix.lower()
    if ext and ext not in ALLOWED_EXTS:
        await msg.reply_text(
            f"Unsupported extension: {ext}. Allowed: {', '.join(sorted(ALLOWED_EXTS))}.\n"
            "Try again or /cancel."
        )
        return WAIT_FILE

    workdir = Path(_kv(context)["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / name

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(src))
    except Exception:
        log.exception("download failed")
        await msg.reply_text("Download failed. Try again or /cancel.")
        return WAIT_FILE

    if not src.exists() or src.stat().st_size == 0:
        await msg.reply_text("Download failed or file is empty. Try again or /cancel.")
        return WAIT_FILE

    _kv(context)["file_path"] = str(src)
    _kv(context)["file_name"] = name
    _kv(context)["file_size"] = src.stat().st_size

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(MODE_LABELS[MODE_LINES], callback_data="mode:lines")],
            [InlineKeyboardButton(MODE_LABELS[MODE_SIZE], callback_data="mode:size")],
            [InlineKeyboardButton(MODE_LABELS[MODE_EQUAL], callback_data="mode:equal")],
        ]
    )
    await msg.reply_text(
        f"Got it — {name} ({src.stat().st_size / 1024:.1f} KB).\n"
        "How should I split it?",
        reply_markup=kb,
    )
    return CHOOSE_MODE


# ---------------------------------------------------------------------------
# mode → value
# ---------------------------------------------------------------------------

async def mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    assert q.data is not None
    mode = q.data.split(":", 1)[1]
    if mode not in MODE_LABELS:
        await q.edit_message_text("Unknown mode.")
        return CHOOSE_MODE

    _kv(context)["mode"] = mode
    cfg: Config = context.bot_data["config"]

    if mode == MODE_LINES:
        prompt = "Enter lines per part (1 – 1,000,000)."
    elif mode == MODE_SIZE:
        prompt = f"Enter max size per part in KB (1 – {cfg.max_upload_mb * 1024})."
    else:
        prompt = f"Enter number of parts (2 – {cfg.max_parts})."

    await q.edit_message_text(f"Mode: {MODE_LABELS[mode]}.")
    await context.bot.send_message(q.message.chat_id, prompt)
    return MODE_VALUE


async def value_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Config = context.bot_data["config"]
    mode = _kv(context).get("mode")
    text = (update.message.text or "").strip().replace(",", "").replace("_", "").replace(" ", "")

    try:
        val = int(text)
    except ValueError:
        await update.message.reply_text("Need a whole number. Try again or /cancel.")
        return MODE_VALUE

    if mode == MODE_LINES:
        if not (1 <= val <= 1_000_000):
            await update.message.reply_text("Lines per part must be 1 – 1,000,000.")
            return MODE_VALUE
    elif mode == MODE_SIZE:
        max_kb = cfg.max_upload_mb * 1024
        if not (1 <= val <= max_kb):
            await update.message.reply_text(f"Size must be 1 – {max_kb} KB.")
            return MODE_VALUE
        val = val * 1024  # store bytes
    elif mode == MODE_EQUAL:
        if not (2 <= val <= cfg.max_parts):
            await update.message.reply_text(f"Parts must be 2 – {cfg.max_parts}.")
            return MODE_VALUE
    else:
        await update.message.reply_text("Session lost. Start over with /split.")
        return ConversationHandler.END

    _kv(context)["value"] = val

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data="yn:header:yes"),
                InlineKeyboardButton("No", callback_data="yn:header:no"),
            ]
        ]
    )
    await update.message.reply_text(
        "Preserve the first line as a header on every part?",
        reply_markup=kb,
    )
    return OPT_HEADER


# ---------------------------------------------------------------------------
# yes/no steps
# ---------------------------------------------------------------------------

async def yesno(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    assert q.data is not None
    parts = q.data.split(":")
    if len(parts) != 3 or parts[0] != "yn":
        await q.edit_message_text("Bad input.")
        return ConversationHandler.END
    field, answer = parts[1], parts[2] == "yes"
    chat_id = q.message.chat_id

    if field == "header":
        _kv(context)["preserve_header"] = answer
        await q.edit_message_text(f"Header preservation: {'on' if answer else 'off'}.")
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes", callback_data="yn:zip:yes"),
                    InlineKeyboardButton("No", callback_data="yn:zip:no"),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id, "Package outputs as a single ZIP?", reply_markup=kb
        )
        return OPT_ZIP

    if field == "zip":
        _kv(context)["zip"] = answer
        await q.edit_message_text(f"ZIP output: {'on' if answer else 'off'}.")
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes", callback_data="yn:preview:yes"),
                    InlineKeyboardButton("No", callback_data="yn:preview:no"),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id, "Show a short preview before processing?", reply_markup=kb
        )
        return OPT_PREVIEW

    if field == "preview":
        _kv(context)["preview"] = answer
        await q.edit_message_text(f"Preview: {'on' if answer else 'off'}.")
        return await _run_job(update, context)

    await q.edit_message_text("Unknown step.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# processing
# ---------------------------------------------------------------------------

def _do_split(
    src: Path,
    out_dir: Path,
    base_name: str,
    mode: str,
    value: int,
    preserve_header: bool,
    max_parts: int,
    progress,
) -> SplitResult:
    if mode == MODE_LINES:
        return split_by_lines(
            src, out_dir, base_name, value,
            preserve_header=preserve_header,
            max_parts=max_parts, progress=progress,
        )
    if mode == MODE_SIZE:
        return split_by_size(
            src, out_dir, base_name, value,
            preserve_header=preserve_header,
            max_parts=max_parts, progress=progress,
        )
    if mode == MODE_EQUAL:
        return split_into_n_parts(
            src, out_dir, base_name, value,
            preserve_header=preserve_header,
            max_parts=max_parts, progress=progress,
        )
    raise SplitError(f"unknown mode: {mode}")


def _build_preview(src: Path, n_lines: int, max_chars: int = 3000) -> str:
    lines: list[str] = []
    used = 0
    with src.open("rb") as f:
        for i, raw in enumerate(f):
            if i >= n_lines:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            lines.append(text)
            used += len(text) + 1
            if used >= max_chars:
                break
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars] + "…"
    return f"Preview (first {len(lines)} line(s)):\n\n{body}"


async def _run_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cfg: Config = context.bot_data["config"]
    q = update.callback_query
    assert q is not None
    chat_id = q.message.chat_id

    data = _kv(context)
    src = Path(data["file_path"])
    base_name = data["file_name"]
    mode = data["mode"]
    value = data["value"]
    preserve_header = data.get("preserve_header", False)
    want_zip = data.get("zip", False)
    want_preview = data.get("preview", False)

    if want_preview:
        try:
            preview = _build_preview(src, cfg.default_preview_lines)
        except Exception:
            log.exception("preview failed")
            preview = "(preview failed)"
        await context.bot.send_message(chat_id, preview)

    status: Message = await context.bot.send_message(chat_id, "Starting…")
    last_edit = [time.monotonic()]

    async def report(done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and (now - last_edit[0]) < 2.0:
            return
        last_edit[0] = now
        pct = 100 if total <= 0 else min(100, int(done * 100 / total))
        try:
            await status.edit_text(f"Processing… {pct}%")
        except Exception:
            pass

    loop = asyncio.get_running_loop()

    def sync_progress(done: int, total: int) -> None:
        try:
            asyncio.run_coroutine_threadsafe(report(done, total), loop)
        except RuntimeError:
            pass

    out_dir = Path(data["workdir"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        result: SplitResult = await loop.run_in_executor(
            None,
            lambda: _do_split(
                src, out_dir, base_name, mode, value,
                preserve_header, cfg.max_parts, sync_progress,
            ),
        )
    except TooManyParts as exc:
        await status.edit_text(
            f"Stopped: {exc}.\n"
            "Reduce the number of parts or raise MAX_PARTS."
        )
        await _cleanup(context)
        data.clear()
        return ConversationHandler.END
    except SplitError as exc:
        await status.edit_text(f"Split failed: {exc}")
        await _cleanup(context)
        data.clear()
        return ConversationHandler.END
    except Exception:
        log.exception("split crashed")
        await status.edit_text("Split failed. Check the bot log.")
        await _cleanup(context)
        data.clear()
        return ConversationHandler.END

    if not result.parts:
        await status.edit_text("Nothing to split — the file has no content lines.")
        await _cleanup(context)
        data.clear()
        return ConversationHandler.END

    oversized = [p for p in result.parts if p.stat().st_size > cfg.max_upload_bytes]
    if oversized:
        worst = max(oversized, key=lambda p: p.stat().st_size)
        await status.edit_text(
            f"{len(oversized)} part(s) exceed the {cfg.max_upload_mb} MB upload limit "
            f"(largest: {worst.stat().st_size / 1024 / 1024:.1f} MB).\n"
            "Reduce the part size and try again, or run a local Bot API server."
        )
        await _cleanup(context)
        data.clear()
        return ConversationHandler.END

    await status.edit_text(f"Processing… 100%. Sending {len(result.parts)} part(s).")

    try:
        if want_zip:
            zip_name = Path(base_name).stem + "_parts.zip"
            zip_path = out_dir / zip_name
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in result.parts:
                    zf.write(p, arcname=p.name)
            if zip_path.stat().st_size > cfg.max_upload_bytes:
                await status.edit_text(
                    f"ZIP is {zip_path.stat().st_size / 1024 / 1024:.1f} MB, over the "
                    f"{cfg.max_upload_mb} MB upload limit.\n"
                    "Try smaller parts or a local Bot API server."
                )
                return ConversationHandler.END
            with zip_path.open("rb") as f:
                await context.bot.send_document(
                    chat_id, document=f, filename=zip_name
                )
        else:
            for p in result.parts:
                with p.open("rb") as f:
                    await context.bot.send_document(
                        chat_id, document=f, filename=p.name
                    )
    except Exception:
        log.exception("send failed")
        await status.edit_text("Sending failed mid-way. Check the bot log.")
    finally:
        stats = (
            "Done.\n\n"
            f"Mode: {MODE_LABELS.get(mode, result.mode)}\n"
            f"Total lines: {result.total_lines}\n"
            f"Parts: {len(result.parts)}\n"
            f"Bytes in: {result.total_bytes_in:,}\n"
            f"Bytes out: {result.total_bytes_out:,}"
        )
        try:
            await context.bot.send_message(chat_id, stats)
        except Exception:
            log.exception("stats send failed")
        await _cleanup(context)
        data.clear()

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# conversation wiring
# ---------------------------------------------------------------------------

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters


def build_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("split", split_start)],
        states={
            WAIT_FILE: [MessageHandler(filters.ALL & ~filters.COMMAND, received_file)],
            CHOOSE_MODE: [CallbackQueryHandler(mode_chosen, pattern=r"^mode:")],
            MODE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, value_received)
            ],
            OPT_HEADER: [CallbackQueryHandler(yesno, pattern=r"^yn:header:")],
            OPT_ZIP: [CallbackQueryHandler(yesno, pattern=r"^yn:zip:")],
            OPT_PREVIEW: [CallbackQueryHandler(yesno, pattern=r"^yn:preview:")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
        conversation_timeout=600,
    )