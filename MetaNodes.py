from log import get_logger
try:
    from dbconfig import get_connection
except Exception as e:
    logger = get_logger("Database Connection")
    logger.error("%s",e)
