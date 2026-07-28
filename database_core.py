import os
import sqlite3
import pandas as pd
import sys

# O PORQUE: Definição de credenciais via variáveis de ambiente.
# No Streamlit Cloud, estas serão lidas das 'Secrets'. No Windows local,
# caso não existam, o app tentará usar o sqlite3 local.
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# O PORQUE: chave nova, opcional, também lida via Secrets/variável de
# ambiente (mesmo mecanismo do Turso acima). Por padrão é False -- então
# rodar local sem Turso configurado continua funcionando exatamente como
# antes (fallback silencioso para SQLite). Em produção (Streamlit Cloud),
# defina REQUIRE_TURSO = "true" nos Secrets: aí, se o Turso não estiver
# acessível por qualquer motivo (credencial ausente, token expirado, banco
# fora do ar), o app para de vez em vez de continuar rodando "por engano"
# num banco local efêmero (que se perde no próximo redeploy/sleep).
REQUIRE_TURSO = os.environ.get("REQUIRE_TURSO", "false").strip().lower() in ("1", "true", "yes", "on")


class TursoRequiredError(RuntimeError):
    """Levantado quando REQUIRE_TURSO está ativo mas não foi possível obter
    uma conexão válida com o Turso (credenciais ausentes, driver não
    instalado, ou falha de conexão/autenticação). app.py captura esta
    exceção e interrompe a inicialização do app com uma mensagem clara,
    em vez de deixá-lo continuar sobre um banco local que não é persistente
    em produção."""
    pass


WORK_LOGS_COLUMNS = [
    "id", "log_date", "project", "category", "description",
    "effort_hours", "created_at", "is_impedimento", "is_duvida", "username",
]

class DatabaseConnection:
    def __init__(self, db_name: str = "personal_tracker.db"):
        self.db_name = db_name
        self.using_turso = False  # O PORQUE: app.py usa isso para avisar na
                                   # tela se caiu para o banco local (o que,
                                   # no Streamlit Cloud, é um problema sério:
                                   # disco efêmero, dados "somem" a cada deploy).

    def get_connection(self):
        # Tenta conectar ao Turso se as credenciais estiverem presentes
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            try:
                import libsql
                conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
                # O PORQUE: connect() do libsql não faz nenhuma chamada de
                # rede -- só monta o cliente. Se a URL ou o token estiverem
                # errados/expirados, o erro só aparece na PRIMEIRA consulta
                # real, e o libsql embrulha esse erro (incluindo falhas de
                # autenticação) como ValueError genérico. Sem este teste
                # aqui, esse ValueError estourava sem tratamento lá na
                # frente (dentro de _initialize_database), derrubando o app
                # inteiro com uma mensagem redigida pelo Streamlit Cloud.
                conn.execute("SELECT 1")
                self.using_turso = True
                return conn
            except ImportError:
                msg = "Driver 'libsql' não está instalado no ambiente."
                print(f"AVISO: {msg}", file=sys.stderr)
                if REQUIRE_TURSO:
                    raise TursoRequiredError(msg) from None
            except Exception as e:
                msg = f"Falha ao conectar/autenticar no Turso: {e}"
                print(f"ERRO: {msg}", file=sys.stderr)
                if REQUIRE_TURSO:
                    raise TursoRequiredError(msg) from e
        elif REQUIRE_TURSO:
            # O PORQUE: REQUIRE_TURSO ligado mas nenhuma das duas variáveis
            # (ou nenhuma delas) está configurada -- não faz sentido nem
            # tentar conectar, e muito menos cair pro SQLite local.
            raise TursoRequiredError(
                "TURSO_DATABASE_URL e/ou TURSO_AUTH_TOKEN não estão configurados nos Secrets."
            )

        # Fallback padrão para SQLite local -- só é alcançado quando
        # REQUIRE_TURSO é False (comportamento de desenvolvimento local).
        self.using_turso = False
        return sqlite3.connect(self.db_name, check_same_thread=False)

class LogRepository:
    def __init__(self, db_connection: DatabaseConnection):
        self.db_connection = db_connection
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

    def insert_logs_bulk(self, username: str, rows: list) -> int:
        # O PORQUE: insert_log() acima dá commit a CADA chamada -- ótimo pra
        # 1 registro manual, mas péssimo pra importar centenas/milhares de
        # uma vez (ex.: tela de Sincronização) contra um banco REMOTO
        # (Turso): cada commit vira uma ida-e-volta de rede, então 2.600
        # linhas = 2.600 round-trips em série (minutos de espera). Este
        # método monta todas as linhas e faz um ÚNICO executemany + commit
        # -- 1 round-trip (ou pertinho disso) pra tudo, independente de ter
        # 10 ou 10.000 linhas. Cada item de `rows` é um dict com as chaves:
        # log_date, project, category, description, effort_hours,
        # is_impedimento (opcional), is_duvida (opcional).
        if not rows:
            return 0
        query = ("INSERT INTO work_logs (username, log_date, project, category, description, effort_hours, "
                 "is_impedimento, is_duvida) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
        tuples_to_insert = [
            (
                username, r["log_date"], r["project"], r["category"], r["description"], r["effort_hours"],
                int(bool(r.get("is_impedimento", False))), int(bool(r.get("is_duvida", False))),
            )
            for r in rows
        ]
        self.conn.executemany(query, tuples_to_insert)
        self.conn.commit()
        return len(tuples_to_insert)

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

    def delete_logs_bulk(self, username: str, log_ids: list) -> int:
        # O PORQUE: mesmo raciocínio de insert_logs_bulk() -- 1 DELETE com
        # "IN (...)" e 1 commit, em vez de 1 commit por id apagado.
        if not log_ids:
            return 0
        placeholders = ", ".join(["?"] * len(log_ids))
        query = f"DELETE FROM work_logs WHERE username = ? AND id IN ({placeholders})"
        self.conn.execute(query, (username, *log_ids))
        self.conn.commit()
        return len(log_ids)

    def update_logs_bulk(self, username: str, updates: list) -> int:
        # O PORQUE: usado pela reestimativa de esforço por IA (recalcula
        # registros JÁ salvos, diferente de insert_logs_bulk que só lida com
        # linhas novas ainda não gravadas). Mesma lógica de 1 executemany +
        # 1 commit em vez de 1 commit por linha -- crítico contra o Turso
        # (cada commit é uma ida-e-volta de rede). Cada item de `updates` é
        # um dict com as chaves: id, effort_hours, project, category.
        if not updates:
            return 0
        query = "UPDATE work_logs SET effort_hours = ?, project = ?, category = ? WHERE id = ? AND username = ?"
        tuples_to_update = [
            (u["effort_hours"], u["project"], u["category"], u["id"], username)
            for u in updates
        ]
        self.conn.executemany(query, tuples_to_update)
        self.conn.commit()
        return len(tuples_to_update)

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

    def get_logs_as_dataframe_by_range(self, username: str, start_date: str, end_date: str) -> pd.DataFrame:
        # O PORQUE: mesma coisa que get_all_logs_as_dataframe, mas filtrando
        # o período já na consulta SQL (WHERE log_date BETWEEN ...) -- usado
        # pela aba Registro de Atividades, para não precisar trazer o
        # histórico inteiro pela rede a cada abertura da tela (importante
        # contra o Turso, onde cada consulta é uma ida-e-volta de rede;
        # trazer só o período pedido é bem mais leve que trazer tudo e
        # filtrar depois em pandas). start_date/end_date no formato
        # "YYYY-MM-DD" (mesmo formato salvo em log_date).
        cursor = self.conn.execute(
            "SELECT * FROM work_logs WHERE username = ? AND log_date >= ? AND log_date <= ? ORDER BY log_date DESC",
            (username, start_date, end_date),
        )
        rows = cursor.fetchall()
        try:
            columns = [d[0] for d in cursor.description]
        except Exception:
            columns = WORK_LOGS_COLUMNS
        return pd.DataFrame(rows, columns=columns)
