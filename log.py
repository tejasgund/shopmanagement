import os
import zipfile
from logging.handlers import TimedRotatingFileHandler
import logging


class ZipTimedRotatingFileHandler(TimedRotatingFileHandler):
    def doRollover(self):
        super().doRollover()

        log_dir = os.path.dirname(self.baseFilename)

        for filename in os.listdir(log_dir):
            if filename.endswith(".log") and filename != os.path.basename(self.baseFilename):
                log_path = os.path.join(log_dir, filename)
                zip_path = log_path + ".zip"

                if not os.path.exists(zip_path):
                    try:
                        with zipfile.ZipFile(
                            zip_path,
                            "w",
                            zipfile.ZIP_DEFLATED
                        ) as zf:
                            zf.write(log_path, arcname=filename)

                        os.remove(log_path)
                    except Exception:
                        pass


def get_logger(name="app"):
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    log_file = os.path.join(log_dir, "app.log")

    handler = ZipTimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=12,
        encoding="utf-8"
    )

    handler.suffix = "%Y-%m-%d"

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    logger.addHandler(handler)
    logger.addHandler(console)

    return logger