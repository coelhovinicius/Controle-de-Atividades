import os
import sqlite3
import pandas as pd
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# O PORQUE: datetime.now() sozinho pega o horário do SISTEMA rodando o
# servidor -- no Streamlit Community Cloud, isso é UTC (Greenwich), não o
# horário de Brasília. Use SEMPRE agora_br() daqui pra frente.
FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")


def agora_br() -> datetime:
    return datetime.now(FUSO_BRASILIA)


def _comparar_com_agora_br(timestamp_iso: str) -> bool:
    """True se o timestamp ISO já passou de agora. Trata tanto timestamps
    antigos (sem fuso -- assume que já era horário de Brasília) quanto
    novos (já com fuso embutido), sem quebrar a comparação."""
    dt = datetime.fromisoformat(timestamp_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_BRASILIA)
    return agora_br() > dt

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

class _ConexaoComRetentativa:
    """
    Envelopa a conexão de verdade (libsql ou sqlite3) e reconecta
    automaticamente se uma operação falhar por causa de uma conexão
    velha/inválida -- ex.: a sessão HTTP com o Turso expirar depois de
    muito tempo sem uso real. Isso PODE acontecer mesmo com o app mantido
    "acordado" por um script externo: o Streamlit não dormir não garante
    que a conexão com o Turso continua válida pra sempre -- são dois
    relógios diferentes. Sem isso, esse tipo de falha só se resolvia com
    um reboot manual do app (limpando o cache do Streamlit, que é onde a
    conexão antiga ficava presa).

    Só tenta reconectar quando o erro PARECE ser de conexão (bate com uma
    das palavras-chave abaixo) -- um erro de SQL genuíno (ex.: coluna que
    não existe) continuaria dando erro numa conexão nova também, então
    tentar de novo só atrasaria a mensagem de erro real sem resolver nada;
    nesses casos, deixa o erro original subir normalmente.
    """
    _PALAVRAS_CHAVE_CONEXAO_VELHA = (
        "hrana", "stream", "connection", "closed", "timeout",
        "broken pipe", "network", "reset by peer",
    )

    def __init__(self, fabrica_conexao):
        self._fabrica_conexao = fabrica_conexao
        self._conn = fabrica_conexao()

    def _com_retentativa(self, nome_metodo, *args, **kwargs):
        try:
            return getattr(self._conn, nome_metodo)(*args, **kwargs)
        except Exception as e:
            mensagem = str(e).lower()
            parece_conexao_velha = any(p in mensagem for p in self._PALAVRAS_CHAVE_CONEXAO_VELHA)
            if not parece_conexao_velha:
                raise
            print(f"AVISO: conexão parecia velha/inválida ({e}) -- reconectando e tentando de novo.", file=sys.stderr)
            self._conn = self._fabrica_conexao()
            return getattr(self._conn, nome_metodo)(*args, **kwargs)

    def execute(self, *args, **kwargs):
        return self._com_retentativa("execute", *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._com_retentativa("executemany", *args, **kwargs)

    def commit(self, *args, **kwargs):
        return self._com_retentativa("commit", *args, **kwargs)

    def rollback(self, *args, **kwargs):
        return self._com_retentativa("rollback", *args, **kwargs)


class DatabaseConnection:
    def __init__(self, db_name: str = "personal_tracker.db"):
        self.db_name = db_name
        self.using_turso = False  # O PORQUE: app.py usa isso para avisar na
                                   # tela se caiu para o banco local (o que,
                                   # no Streamlit Cloud, é um problema sério:
                                   # disco efêmero, dados "somem" a cada deploy).

    def get_connection(self):
        def _criar_conexao_turso():
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
            return conn

        # Tenta conectar ao Turso se as credenciais estiverem presentes
        if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
            try:
                # O PORQUE: _ConexaoComRetentativa (não a conexão crua) --
                # ela já chama _criar_conexao_turso() aqui dentro pra testar
                # a conexão (mesmo comportamento de antes), mas também
                # guarda essa função pra poder reconectar sozinha depois,
                # se algum dia essa conexão específica ficar velha/inválida
                # em uso normal (não só na hora de abrir).
                conexao = _ConexaoComRetentativa(_criar_conexao_turso)
                self.using_turso = True
                return conexao
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
        # Não precisa do envelope de retentativa: é um arquivo local, não
        # uma conexão de rede que possa "expirar" com o tempo.
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

        # O PORQUE: tabela nova para o fluxo de "solicitação de acesso de
        # convidado" -- quem não tem usuário/senha preenche nome, e-mail e
        # justificativa; o admin aprova/rejeita/exclui pela área
        # administrativa. access_token só é preenchido quando aprovado (é o
        # que vira o link de acesso -- ?g=<token> -- que o admin copia e
        # manda pra pessoa). status controla quem pode entrar: só
        # 'approved' com token válido consegue. expires_at é opcional
        # (NULL = sem prazo, até o admin revogar manualmente) -- o admin
        # escolhe/ajusta esse prazo a qualquer momento pela área
        # administrativa.
        query_access_requests = """
        CREATE TABLE IF NOT EXISTS access_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            justification TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
            access_token TEXT,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP,
            expires_at TIMESTAMP
        );
        """
        self.conn.execute(query_access_requests)
        self._ensure_column("access_requests", "expires_at", "TIMESTAMP")
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

    # ==========================================
    # SOLICITAÇÕES DE ACESSO DE CONVIDADO
    # ==========================================
    ACCESS_REQUESTS_COLUMNS = [
        "id", "name", "email", "justification", "status", "access_token", "requested_at", "decided_at", "expires_at",
    ]

    def count_active_access_requests(self) -> int:
        # O PORQUE: "ativa" = pending OU approved -- é o que conta pro limite
        # de 5. Uma rejeitada ou excluída libera vaga na hora.
        row = self.conn.execute(
            "SELECT COUNT(*) FROM access_requests WHERE status IN ('pending', 'approved')"
        ).fetchone()
        return int(row[0]) if row else 0

    def get_active_access_request_by_email(self, email: str):
        # O PORQUE: usado pra checar duplicidade -- só bloqueia um e-mail
        # novo se já existir uma solicitação ATIVA (pending/approved) com
        # esse e-mail. Uma pessoa cuja solicitação foi rejeitada pode
        # solicitar de novo (ex.: com uma justificativa melhor).
        row = self.conn.execute(
            "SELECT id FROM access_requests WHERE email = ? AND status IN ('pending', 'approved')",
            (email.strip().lower(),),
        ).fetchone()
        return row[0] if row else None

    def create_access_request(self, name: str, email: str, justification: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO access_requests (name, email, justification, status) VALUES (?, ?, ?, 'pending')",
            (name.strip(), email.strip().lower(), justification.strip()),
        )
        self.conn.commit()
        return cursor.lastrowid

    def list_access_requests(self) -> pd.DataFrame:
        cursor = self.conn.execute(
            "SELECT * FROM access_requests ORDER BY requested_at DESC"
        )
        rows = cursor.fetchall()
        try:
            columns = [d[0] for d in cursor.description]
        except Exception:
            columns = self.ACCESS_REQUESTS_COLUMNS
        return pd.DataFrame(rows, columns=columns)

    def approve_access_request(self, request_id: int, dias_validade: int = 0) -> str:
        # O PORQUE: gera o token só na hora da aprovação (não antes) -- um
        # pedido pendente não tem nenhum link válido ainda, então não tem
        # como "vazar" acesso de algo que nunca foi aprovado.
        # dias_validade: 0 (ou negativo) = sem expiração (até revogar
        # manualmente); um número positivo define daqui a quantos dias o
        # link para de funcionar sozinho.
        import secrets as _secrets
        token = _secrets.token_urlsafe(24)
        expires_at = (agora_br() + timedelta(days=dias_validade)).isoformat() if dias_validade > 0 else None
        self.conn.execute(
            "UPDATE access_requests SET status = 'approved', access_token = ?, decided_at = CURRENT_TIMESTAMP, expires_at = ? WHERE id = ?",
            (token, expires_at, request_id),
        )
        self.conn.commit()
        return token

    def update_access_request_expiry(self, request_id: int, dias_validade: int = 0):
        # O PORQUE: permite ajustar a validade de um acesso JÁ aprovado, a
        # qualquer momento -- sem precisar revogar e aprovar de novo (o que
        # trocaria o token, invalidando um link já compartilhado).
        # dias_validade: 0 (ou negativo) = remove a expiração (passa a
        # valer até ser revogado manualmente).
        expires_at = (agora_br() + timedelta(days=dias_validade)).isoformat() if dias_validade > 0 else None
        self.conn.execute(
            "UPDATE access_requests SET expires_at = ? WHERE id = ?",
            (expires_at, request_id),
        )
        self.conn.commit()

    def reject_access_request(self, request_id: int):
        # O PORQUE: também serve para REVOGAR um acesso já aprovado -- muda
        # o status pra 'rejected', o que invalida o token na hora (ver
        # get_access_request_by_token, que só aceita status='approved').
        self.conn.execute(
            "UPDATE access_requests SET status = 'rejected', access_token = NULL, decided_at = CURRENT_TIMESTAMP WHERE id = ?",
            (request_id,),
        )
        self.conn.commit()

    def delete_access_request(self, request_id: int):
        self.conn.execute("DELETE FROM access_requests WHERE id = ?", (request_id,))
        self.conn.commit()

    def get_access_request_by_token(self, token: str):
        # O PORQUE: checagem ao vivo no banco (não um token autoverificável
        # tipo o de sessão do admin) -- é isso que permite revogar o acesso
        # de um convidado instantaneamente (rejeitar/excluir o pedido), sem
        # depender de lista de tokens revogados em memória. A expiração por
        # tempo (quando definida) é checada aqui também, em Python -- mais
        # simples e mais confiável entre SQLite/Turso do que comparar
        # timestamps direto no SQL.
        row = self.conn.execute(
            "SELECT id, name, email, status, expires_at FROM access_requests WHERE access_token = ? AND status = 'approved'",
            (token,),
        ).fetchone()
        if not row:
            return None
        expires_at_str = row[4]
        if expires_at_str:
            try:
                if _comparar_com_agora_br(str(expires_at_str)):
                    return None
            except Exception:
                # O PORQUE: um valor de data mal formado não deve travar o
                # acesso de quem já estava aprovado -- trata como "sem
                # expiração" nesse caso raro, em vez de derrubar o convidado
                # por um dado corrompido.
                pass
        return {"id": row[0], "name": row[1], "email": row[2], "status": row[3]}
