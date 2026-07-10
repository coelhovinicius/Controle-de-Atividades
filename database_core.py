import os
import sqlite3
import pandas as pd
import sys

# O PORQUE: Definição de credenciais via variáveis de ambiente.
# No Streamlit Cloud, estas serão lidas das 'Secrets'. No Windows local,
# caso não existam, o app tentará usar o sqlite3 local.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

WORK_LOGS_COLUMNS = [
    "id", "log_date", "project", "category", "description",
    "effort_hours", "created_at", "is_impedimento", "is_duvida", "username",
]

class DatabaseConnection:
    def __init__(self, db_name: str = "personal_tracker.db"):
        self.db_name = db_name

    def get_connection(self):
        # Tenta conectar ao Turso se as credenciais estiverem presentes
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            try:
                import libsql
                return libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
            except ImportError:
                print("AVISO: Driver 'libsql' não instalado. Ignorando Turso e usando banco local.", file=sys.stderr)
            except Exception as e:
                print(f"ERRO: Falha ao conectar ao Turso: {e}. Usando banco local.", file=sys.stderr)
        
        # Fallback padrão para SQLite local
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

        self._ensure_column("work_logs", "is_impedimento", "INTEGER DEFAULT 0")
        self._ensure_column("work_logs", "is_duvida", "INTEGER DEFAULT 0")
        self._ensure_column("work_logs", "username", "TEXT")

        query_options = """
        CREATE TABLE IF NOT EXISTS custom_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            option_type TEXT NOT NULL CHECK(option_type IN ('project', 'category')),
            value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(option_type, value)
        );
        """
        self.conn.execute(query_options)
        self._ensure_column("custom_options", "username", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_def: str):
        existing_cols = [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing_cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
            self.conn.commit()

    def get_custom_options(self, option_type: str, username: str) -> list:
        rows = self.conn.execute(
            "SELECT value FROM custom_options WHERE option_type = ? AND username = ? ORDER BY value COLLATE NOCASE",
            (option_type, username),
        ).fetchall()
        return [r[0] for r in rows]

    def add_custom_option(self, option_type: str, username: str, value: str) -> bool:
        value = value.strip()
        if not value:
            return False
        try:
            self.conn.execute(
                "INSERT INTO custom_options (option_type, username, value) VALUES (?, ?, ?)",
                (option_type, username, value),
            )
            self.conn.commit()
            return True
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                return False
            raise

    def delete_custom_option(self, option_type: str, username: str, value: str):
        self.conn.execute(
            "DELETE FROM custom_options WHERE option_type = ? AND username = ? AND value = ?",
            (option_type, username, value),
        )
        self.conn.commit()

    def rename_custom_option(self, option_type: str, username: str, old_value: str, new_value: str) -> bool:
        old_value = (old_value or "").strip()
        new_value = (new_value or "").strip()
        if not new_value or old_value == new_value:
            return False

        column = "project" if option_type == "project" else "category"
        try:
            self.conn.execute(
                "UPDATE custom_options SET value = ? WHERE option_type = ? AND username = ? AND value = ?",
                (new_value, option_type, username, old_value),
            )
            self.conn.execute(
                f"UPDATE work_logs SET {column} = ? WHERE {column} = ? AND username = ?",
                (new_value, old_value, username),
            )
            self.conn.commit()
            return True
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            if "UNIQUE" in str(e).upper():
                return False
            raise

    def insert_log(self, username: str, log_date: str, project: str, category: str, description: str, effort_hours: float,
                    is_impedimento: bool = False, is_duvida: bool = False):
        query = ("INSERT INTO work_logs (username, log_date, project, category, description, effort_hours, "
                 "is_impedimento, is_duvida) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
        self.conn.execute(query, (username, log_date, project, category, description, effort_hours,
                                   int(bool(is_impedimento)), int(bool(is_duvida))))
        self.conn.commit()

    def update_log(self, log_id: int, username: str, log_date: str, project: str, category: str, description: str, effort_hours: float,
                    is_impedimento: bool = False, is_duvida: bool = False):
        # O PORQUE: o "AND username = ?" no WHERE não é só um filtro -- é uma
        # segunda camada de proteção contra um usuário editar/apagar um log
        # de outra pessoa (defesa em profundidade), mesmo que algum bug na
        # tela permitisse mandar um log_id de outro usuário por engano.
        query = ("UPDATE work_logs SET log_date = ?, project = ?, category = ?, description = ?, effort_hours = ?, "
                 "is_impedimento = ?, is_duvida = ? WHERE id = ? AND username = ?")
        self.conn.execute(query, (log_date, project, category, description, effort_hours,
                                   int(bool(is_impedimento)), int(bool(is_duvida)), log_id, username))
        self.conn.commit()

    def delete_log(self, log_id: int, username: str):
        self.conn.execute("DELETE FROM work_logs WHERE id = ? AND username = ?", (log_id, username))
        self.conn.commit()

    def get_all_logs_as_dataframe(self, username: str) -> pd.DataFrame:
        cursor = self.conn.execute(
            "SELECT * FROM work_logs WHERE username = ? ORDER BY log_date DESC", (username,)
        )
        rows = cursor.fetchall()
        try:
            columns = [d[0] for d in cursor.description]
        except Exception:
            columns = WORK_LOGS_COLUMNS
        return pd.DataFrame(rows, columns=columns)