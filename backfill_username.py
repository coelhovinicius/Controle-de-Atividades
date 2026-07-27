"""
Script de migração pontual (rodar UMA VEZ, tanto local quanto no Turso) para
atribuir os registros já existentes -- gravados ANTES da tela de login por
usuário existir -- ao usuário "dono" original.

O PORQUE: work_logs e custom_options ganharam uma coluna "username" para que
cada pessoa logada só veja/edite os próprios dados. Registros gravados antes
dessa mudança ficaram com username = NULL. Como toda consulta do app agora
filtra "WHERE username = ?", sem esta migração esses registros antigos
ficariam invisíveis para todo mundo (inclusive para o dono original).

Uso (rode a partir da RAIZ do projeto, não de dentro de scripts/):
    python scripts/backfill_username.py <usuario_dono> [caminho_do_banco_local]

Exemplos:
    # Banco local (SQLite):
    python scripts/backfill_username.py coelhovinicius

    # Banco no Turso (produção) -- defina as variáveis de ambiente antes:
    $env:TURSO_DATABASE_URL = "libsql://qa-tracker-seu-usuario.turso.io"
    $env:TURSO_AUTH_TOKEN = "eyJ..."
    python scripts/backfill_username.py coelhovinicius

É seguro rodar mais de uma vez: o WHERE username IS NULL não encontra mais
nada depois da primeira execução bem-sucedida.
"""
import sys
import os

# O PORQUE: este script agora mora em scripts/, mas database_core.py
# continua na raiz do projeto. O Python só olha a pasta do próprio script
# (não a pasta de onde você chamou "python") para resolver imports -- sem
# esta linha, "from database_core import ..." abaixo daria
# ModuleNotFoundError. Isto adiciona a pasta pai (a raiz do projeto) ao
# caminho de busca de módulos.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database_core import DatabaseConnection, LogRepository


def main(owner_username: str, db_path: str = "personal_tracker.db"):
    if not owner_username or not owner_username.strip():
        print("Erro: informe o nome de usuário dono destes registros.")
        sys.exit(1)
    owner_username = owner_username.strip()

    db_conn = DatabaseConnection(db_path)
    # O PORQUE: instanciar o LogRepository garante que as colunas "username"
    # (em work_logs e custom_options) já existem antes de tentarmos usá-las
    # -- ele roda a mesma checagem/criação de coluna que o app faz ao subir.
    repo = LogRepository(db_conn)
    conn = repo.conn

    cur = conn.execute("SELECT COUNT(*) FROM work_logs WHERE username IS NULL OR username = ''")
    pending_logs = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM custom_options WHERE username IS NULL OR username = ''")
    pending_options = cur.fetchone()[0]

    if pending_logs == 0 and pending_options == 0:
        print("Nada para migrar -- todos os registros já têm um usuário atribuído.")
        return

    if pending_logs:
        conn.execute(
            "UPDATE work_logs SET username = ? WHERE username IS NULL OR username = ''",
            (owner_username,),
        )
        conn.commit()
        print(f"{pending_logs} registro(s) de work_logs atribuído(s) a '{owner_username}'.")

    if pending_options:
        conn.execute(
            "UPDATE custom_options SET username = ? WHERE username IS NULL OR username = ''",
            (owner_username,),
        )
        conn.commit()
        print(f"{pending_options} opção(ões) customizada(s) atribuída(s) a '{owner_username}'.")

    print("Migração concluída.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/backfill_username.py <usuario_dono> [caminho_do_banco_local]")
        sys.exit(1)
    owner = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "personal_tracker.db"
    main(owner, path)
