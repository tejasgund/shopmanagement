from log import get_logger


#database connection
try:
    logger = get_logger("Database Connection")
    logger.info("Connectiong Database")
    from dbconfig import get_connection
    logger.info("Successfully connected to Database")

except Exception as e:
    logger.error("%s",e)



