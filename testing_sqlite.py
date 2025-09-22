import os
modified=os.path.getmtime(__file__)
try:
    from db import SQLite
    db=SQLite("testing.db")
    with db:
        print(db.create_table("users", {"username": "TEXT PRIMARY KEY", "men": "TEXT", "kissing": "TEXT"}))
        print(db.insert_data("users", {"username": "imdumb", "men": "kissing", "kissing": "hard"}))
        print(db.insert_data("users", {"username": "imevenmoredumb", "men": "kissingg", "kissing": "hardd"}))
        print(db.select_data("users"))
finally:
    import time, sys
    while True:
        time.sleep(1)
        if os.path.getmtime(__file__)>modified: os.execv(sys.executable, ["python", __file__]+sys.argv[1:])