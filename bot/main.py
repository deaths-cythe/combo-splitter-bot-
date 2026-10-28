from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

from .config import Config
from .handlers import build_conversation, cmd_cancel, cmd_help, cmd_start


def main() -> None:
    load_dotenv()
    cfg = Config.from_env()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("combo_bot")

    Path(cfg.temp_dir).mkdir(parents=True, exist_ok=True)

    builder = Application.builder().token(cfg.bot_token)
    if cfg.api_base_url:
        base = cfg.api_base_url.rstrip("/")
        builder = (
            builder.base_url(f"{base}/bot")
            .base_file_url(f"{base}/file/bot")
        )
        log.info("Using custom Bot API base URL: %s", base)

    app = builder.build()
    app.bot_data["config"] = cfg

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(build_conversation())

    log.info(
        "Bot starting. download limit %d MB, upload limit %d MB, max parts %d",
        cfg.max_download_mb, cfg.max_upload_mb, cfg.max_parts,
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()