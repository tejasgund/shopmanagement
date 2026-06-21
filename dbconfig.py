from mysql.connector import pooling

db_pool = pooling.MySQLConnectionPool(
    pool_name="mypool",
    pool_size=10,
    host="172.31.52.221",
    user="admin",
    password="admin",
    database="test_lightbill"
)


cursor=db_pool.cursor()
cursor.execute("show tables")
print(cursor.fetchall())
cursor.close()

def get_connection():
    return db_pool.get_connection()
