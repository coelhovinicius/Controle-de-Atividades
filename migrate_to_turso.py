"""
Script de migração para copiar todos os dados do banco local (personal_tracker.db) para o Turso.
Refatorado para Batch Processing e Idempotência.
"""
import os
import sqlite3
import sys

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
        is_duvida INTEGER DEFAULT 0
    );
    """
]


def _copy_table(local_conn, remote_conn, table_name: str) -> int:
    local_conn.row_factory = sqlite3.Row
    cursor = local_conn.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()

    if not rows:
        return 0

    columns = rows[0].keys()
    cols_str = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))

    # O PORQUE: 'INSERT OR IGNORE' garante a idempotencia do ETL. 
    # Registros ja consolidados no banco remoto na execucao anterior cancelada serao pulados sem disparar exceptions de Constraint (Primary Key).
    insert_sql = f"INSERT OR IGNORE INTO {table_name} ({cols_str}) VALUES ({placeholders})"

    # O PORQUE: Substituicao de laco 'for' iterativo por 'executemany'. 
    # Mitiga o bloqueio de I/O de rede (N+1 queries). O driver serializa a carga util e executa o dispatch em batch (1 RTT), reduzindo o tempo de execucao de minutos para milissegundos.
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
            "TURSO_AUTH_TOKEN antes de rodar este script."
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
    tables = ["work_logs"]
    for table in tables:
        _copy_table(local_conn, remote_conn, table)

    print("Migração concluída.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "personal_tracker.db"
    main(path)