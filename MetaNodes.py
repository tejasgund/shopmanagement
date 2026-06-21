from log import get_logger
logger = get_logger("test")
logger.info("test")
connection = get_connection()
cursor = connection.cursor()""

from dbconfig import get_connection







