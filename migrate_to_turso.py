"""
Script de migração (rodar UMA VEZ) para copiar todos os dados do banco local
(personal_tracker.db) para o banco no Turso.

O PORQUE: ao migrar o app para o Streamlit Community Cloud, o arquivo SQLite
local deixa de ser confiável como armazenamento definitivo (disco efêmero).
Este script copia, uma única vez, todo o histórico que já existe no seu
personal_tracker.db para o banco no Turso -- que passa a ser a fonte de
dados de verdade a partir de então (veja docs/TURSO_DEPLOY.md).

Uso:
    $env:TURSO_DATABASE_URL = "libsql://seu-banco.turso.io"
    $env:TURSO_AUTH_TOKEN = "seu-token-aqui"
    python migrate_to_turso.py [caminho_do_banco_local]

Por padrão usa personal_tracker.db no diretório atual como origem. Seguro
rodar mais de uma vez: 'INSERT OR IGNORE' pula registros já copiados numa
execução anterior, sem duplicar nem estourar erro de PRIMARY KEY.
"""
import os
import sqlite3
import sys

# O PORQUE: schema alinhado com o que LogRepository._initialize_database()
# (em database_core.py) cria hoje -- incluindo a coluna "username" em
# AMBAS as tabelas (necessária desde que o app passou a ter login por
# usuário) e a tabela "custom_options" (projetos/categorias
# personalizados). Uma versão anterior deste script só criava/copiava
# "work_logs" e sem "username" -- isso fazia uma migração para um banco
# Turso NOVO/vazio perder os custom_options e falhar de forma confusa ao
# tentar inserir uma coluna "username" que a tabela remota ainda não tinha.
SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS work_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_date DATE NOT NULL,
        project TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        effort_hours REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_impedimento INTEGER DEFAULT 0,
        is_duvida INTEGER DEFAULT 0,
        username TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS custom_options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        option_type TEXT NOT NULL CHECK(option_type IN ('project', 'category')),
        value TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        username TEXT,
        UNIQUE(option_type, value)
    );
    """,
]

# O PORQUE: as duas tabelas que existem hoje no app -- se um dia surgir uma
# terceira (ex.: alguma tabela de auditoria), é só adicionar o nome aqui.
TABLES_TO_COPY = ["work_logs", "custom_options"]


def _copy_table(local_conn: sqlite3.Connection, remote_conn, table_name: str) -> int:
    # O PORQUE: introspectar as colunas via cursor.description (em vez de
    # sqlite3.Row/.keys()) funciona igual tanto pro sqlite3 local quanto
    # pro driver do Turso (libsql), caso este script um dia precise ler de
    # um lado que não seja sqlite3 puro.
    cursor = local_conn.execute(f"SELECT * FROM {table_name}")
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()

    if not rows:
        print(f"  '{table_name}': nenhum registro para copiar.")
        return 0

    col_list = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))

    # O PORQUE: 'INSERT OR IGNORE' garante a idempotência do ETL --
    # registros já copiados numa execução anterior (mesmo PRIMARY KEY) são
    # pulados sem disparar exceção de constraint.
    insert_sql = f"INSERT OR IGNORE INTO {table_name} ({col_list}) VALUES ({placeholders})"

    # O PORQUE: executemany em vez de um loop com execute() um a um --
    # evita N round-trips de rede pro Turso (um round-trip só, com a carga
    # inteira), o que é a diferença entre minutos e milissegundos num banco
    # remoto com centenas/milhares de linhas.
    tuples_to_insert = [tuple(row) for row in rows]
    remote_conn.executemany(insert_sql, tuples_to_insert)
    remote_conn.commit()

    print(f"  '{table_name}': {len(rows)} registro(s) verificado(s)/copiado(s) em batch.")
    return len(rows)


def main(local_db_path: str = "personal_tracker.db"):
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")

    if not turso_url or not turso_token:
        print(
            "Erro: defina as variáveis de ambiente TURSO_DATABASE_URL e "
            "TURSO_AUTH_TOKEN antes de rodar este script (veja docs/TURSO_DEPLOY.md)."
        )
        sys.exit(1)

    if not os.path.exists(local_db_path):
        print(f"Erro: arquivo '{local_db_path}' não encontrado.")
        sys.exit(1)

    import libsql

    print(f"Lendo dados locais de '{local_db_path}'...")
    local_conn = sqlite3.connect(local_db_path)

    print("Conectando ao Turso...")
    remote_conn = libsql.connect(database=turso_url, auth_token=turso_token)

    print("Garantindo que as tabelas existem no Turso...")
    for stmt in SCHEMA_STATEMENTS:
        remote_conn.execute(stmt)
    remote_conn.commit()

    print("Copiando dados (Batch Mode)...")
    total = 0
    for table_name in TABLES_TO_COPY:
        total += _copy_table(local_conn, remote_conn, table_name)

    print(f"\nMigração concluída! {total} registro(s) no total copiado(s) para o Turso.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "personal_tracker.db"
    main(path)
