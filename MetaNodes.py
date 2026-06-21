from dbconfig import get_connection
cursor = get_connection().cursor()
cursor.execute("show databases")
print(cursor.fetchall())
