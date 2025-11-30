import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
import zipfile


def archive_old_logs(log_dir: str) -> None:
    """
    Compress log files from previous months into a monthly zip archive.
    """
    log_path = Path(log_dir)
    archive_dir = log_path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.utcnow().strftime("%Y-%m")

    candidates = list(log_path.glob("bot-*.log")) + list(log_path.glob("bot.log.*"))
    for log_file in candidates:
        parts = log_file.stem.split("-")
        file_month = None
        if len(parts) >= 3 and parts[1].isdigit():
            file_month = "-".join(parts[1:3])  # YYYY-MM
        elif log_file.name.startswith("bot.log.") and len(parts) >= 2:
            # format bot.log.YYYY-MM-DD
            date_part = parts[-1]
            if len(date_part) >= 7:
                file_month = date_part[:7]
        if not file_month:
            continue
        if file_month >= today:
            continue
        archive_path = archive_dir / f"{file_month}.zip"
        with zipfile.ZipFile(archive_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(log_file, arcname=log_file.name)
        log_file.unlink(missing_ok=True)


def setup_logging(log_dir: str, level: int = logging.INFO, name: str = "bot") -> logging.Logger:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    base_log = log_path / f"{name}.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        base_log,
        when="midnight",
        interval=1,
        backupCount=14,
        utc=True,
    )
    file_handler.suffix = "%Y-%m-%d"

    def _namer(file_name: str) -> str:
        # Convert <base>.log.YYYY-MM-DD to <base>-YYYY-MM-DD.log
        p = Path(file_name)
        renamed = p.name.replace(".log.", "-") + ".log"
        return str(p.with_name(renamed))

    file_handler.namer = _namer
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    archive_old_logs(log_dir)
    return logger
