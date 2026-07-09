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

        # O PORQUE: bancos criados antes desta versão não têm as colunas
        # abaixo. SQLite não suporta "ADD COLUMN IF NOT EXISTS", então
        # checamos via PRAGMA table_info antes de tentar o ALTER TABLE --
        # migração idempotente, segura de rodar toda vez que o app sobe.
        self._ensure_column("work_logs", "is_impedimento", "INTEGER DEFAULT 0")
        self._ensure_column("work_logs", "is_duvida", "INTEGER DEFAULT 0")

        # O PORQUE: Projeto e Categoria eram listas fixas no código (app.py).
        # Esta tabela guarda as opções criadas manualmente pelo usuário (ex.:
        # "Backoffice", "Cockpit"), somadas às listas base já existentes, sem
        # precisar alterar código para cada novo projeto/categoria.
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
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_def: str):
        existing_cols = [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing_cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
            self.conn.commit()

    def get_custom_options(self, option_type: str) -> list:
        rows = self.conn.execute(
            "SELECT value FROM custom_options WHERE option_type = ? ORDER BY value COLLATE NOCASE",
            (option_type,),
        ).fetchall()
        return [r[0] for r in rows]

    def add_custom_option(self, option_type: str, value: str) -> bool:
        # O PORQUE: retorna False (em vez de deixar estourar exceção) quando o
        # valor já existe (UNIQUE constraint), para a UI mostrar um aviso
        # amigável em vez de travar a tela com um traceback do SQLite.
        value = value.strip()
        if not value:
            return False
        try:
            self.conn.execute(
                "INSERT INTO custom_options (option_type, value) VALUES (?, ?)",
                (option_type, value),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_custom_option(self, option_type: str, value: str):
        self.conn.execute(
            "DELETE FROM custom_options WHERE option_type = ? AND value = ?",
            (option_type, value),
        )
        self.conn.commit()

    def rename_custom_option(self, option_type: str, old_value: str, new_value: str) -> bool:
        # O PORQUE: "editar" um Projeto/Categoria customizado precisa fazer
        # duas coisas: (1) renomear a entrada em custom_options (para o
        # dropdown passar a mostrar o novo nome) e (2) atualizar em cascata
        # os work_logs que já usavam o nome antigo -- senão registros antigos
        # "sumiriam" da visão do novo nome e o filtro do Dashboard ficaria
        # inconsistente. Devolve False (sem levantar exceção) se o novo nome
        # já existir (UNIQUE constraint) ou for igual/vazio, para a UI
        # mostrar um aviso amigável em vez de travar com traceback.
        old_value = (old_value or "").strip()
        new_value = (new_value or "").strip()
        if not new_value or old_value == new_value:
            return False

        column = "project" if option_type == "project" else "category"
        try:
            self.conn.execute(
                "UPDATE custom_options SET value = ? WHERE option_type = ? AND value = ?",
                (new_value, option_type, old_value),
            )
            self.conn.execute(
                f"UPDATE work_logs SET {column} = ? WHERE {column} = ?",
                (new_value, old_value),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False

    def insert_log(self, log_date: str, project: str, category: str, description: str, effort_hours: float,
                    is_impedimento: bool = False, is_duvida: bool = False):
        query = ("INSERT INTO work_logs (log_date, project, category, description, effort_hours, "
                 "is_impedimento, is_duvida) VALUES (?, ?, ?, ?, ?, ?, ?)")
        self.conn.execute(query, (log_date, project, category, description, effort_hours,
                                   int(bool(is_impedimento)), int(bool(is_duvida))))
        self.conn.commit()

    def update_log(self, log_id: int, log_date: str, project: str, category: str, description: str, effort_hours: float,
                    is_impedimento: bool = False, is_duvida: bool = False):
        query = ("UPDATE work_logs SET log_date = ?, project = ?, category = ?, description = ?, effort_hours = ?, "
                 "is_impedimento = ?, is_duvida = ? WHERE id = ?")
        self.conn.execute(query, (log_date, project, category, description, effort_hours,
                                   int(bool(is_impedimento)), int(bool(is_duvida)), log_id))
        self.conn.commit()

    def delete_log(self, log_id: int):
        self.conn.execute("DELETE FROM work_logs WHERE id = ?", (log_id,))
        self.conn.commit()

    def get_all_logs_as_dataframe(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM work_logs ORDER BY log_date DESC", self.conn)
