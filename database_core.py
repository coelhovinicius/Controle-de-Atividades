import os
import sqlite3
import pandas as pd

# O PORQUE: por padrão, o app roda contra um arquivo SQLite local
# (personal_tracker.db). Isso não funciona no Streamlit Community Cloud,
# porque o armazenamento local de lá NÃO é garantido entre "sonos"/deploys
# (a própria documentação do Streamlit avisa que os arquivos locais podem
# ser apagados a qualquer momento). Por isso, o banco "de verdade" passa a
# viver no Turso (banco compatível com SQLite, hospedado na nuvem, com
# camada gratuita). Se as variáveis TURSO_DATABASE_URL e TURSO_AUTH_TOKEN
# existirem (via .streamlit/secrets.toml local, ou via "Secrets" no painel
# do Community Cloud -- o Streamlit expõe as chaves de nível raiz do
# secrets.toml também como variável de ambiente), conecta no Turso. Caso
# contrário, cai para o arquivo SQLite local -- útil para rodar/testar 100%
# offline, sem precisar de conta no Turso.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# O PORQUE: usado como fallback caso o cursor devolvido pela conexão não
# exponha `.description` (esperado tanto em sqlite3 quanto no driver do
# Turso, mas mantemos uma rede de segurança para não quebrar o app caso um
# dos dois se comporte de forma inesperada).
WORK_LOGS_COLUMNS = [
    "id", "log_date", "project", "category", "description",
    "effort_hours", "created_at", "is_impedimento", "is_duvida",
]


class DatabaseConnection:
    def __init__(self, db_name: str = "personal_tracker.db"):
        self.db_name = db_name

    def get_connection(self):
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            # O PORQUE: import feito aqui dentro (e não no topo do arquivo)
            # para o app continuar funcionando 100% localmente mesmo em uma
            # máquina sem o pacote `libsql` instalado, contanto que as
            # variáveis do Turso não estejam configuradas.
            import libsql
            return libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
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
        # Checamos o TEXTO da mensagem de erro (padrão herdado do SQLite,
        # "UNIQUE constraint failed...") em vez do tipo exato da exceção,
        # porque o driver do Turso (libsql) pode levantar uma classe de
        # exceção diferente de sqlite3.IntegrityError para a mesma violação
        # -- assim o comportamento fica igual nos dois bancos.
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
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                return False
            raise

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
        except Exception as e:
            try:
                self.conn.rollback()
            except Exception:
                pass
            if "UNIQUE" in str(e).upper():
                return False
            raise

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
        # O PORQUE: pd.read_sql_query() detecta internamente se `con` é uma
        # sqlite3.Connection para decidir como ler os resultados; um driver
        # diferente (como o do Turso) pode não ser reconhecido do mesmo jeito.
        # Montar o DataFrame manualmente a partir do cursor (fetchall +
        # description) funciona igual nos dois bancos.
        cursor = self.conn.execute("SELECT * FROM work_logs ORDER BY log_date DESC")
        rows = cursor.fetchall()
        try:
            columns = [d[0] for d in cursor.description]
        except Exception:
            columns = WORK_LOGS_COLUMNS
        return pd.DataFrame(rows, columns=columns)

