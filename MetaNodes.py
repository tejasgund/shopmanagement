from dbconfig import get_connection
from log import get_logger
logger = get_logger("test")
logger.info("test")
cursor = get_connection().cursor()
cursor.execute("show databases")
print(cursor.fetchall())
