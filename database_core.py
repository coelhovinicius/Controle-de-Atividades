import sqlite3
import pandas as pd

class DatabaseConnection:
    def __init__(self, db_name: str = "personal_tracker.db"):
        self.db_name = db_name

    def get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_name, check_same_thread=False)

class LogRepository:
    def __init__(self, db_connection: DatabaseConnection):
        self.conn = db_connection.get_connection()
        self._initialize_database()

    def _initialize_database(self):
        query = """
        CREATE TABLE IF NOT EXISTS work_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date DATE NOT NULL,
            project TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            effort_hours REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        self.conn.execute(query)
        self.conn.commit()

    def insert_log(self, log_date: str, project: str, category: str, description: str, effort_hours: float):
        query = "INSERT INTO work_logs (log_date, project, category, description, effort_hours) VALUES (?, ?, ?, ?, ?)"
        self.conn.execute(query, (log_date, project, category, description, effort_hours))
        self.conn.commit()

    def update_log(self, log_id: int, log_date: str, project: str, category: str, description: str, effort_hours: float):
        query = "UPDATE work_logs SET log_date = ?, project = ?, category = ?, description = ?, effort_hours = ? WHERE id = ?"
        self.conn.execute(query, (log_date, project, category, description, effort_hours, log_id))
        self.conn.commit()

    def delete_log(self, log_id: int):
        self.conn.execute("DELETE FROM work_logs WHERE id = ?", (log_id,))
        self.conn.commit()

    def get_all_logs_as_dataframe(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM work_logs ORDER BY log_date DESC", self.conn)