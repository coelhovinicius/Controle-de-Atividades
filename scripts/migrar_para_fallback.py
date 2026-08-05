"""
migrar_para_fallback.py

O QUE FAZ: copia os dados atuais do Turso (work_logs + custom_options)
para o banco de emergência (auto-hospedado na VM Oracle) -- assim ele já
nasce com todo o seu histórico, em vez de vazio, e fica realmente pronto
pra ser usado se o Turso cair de novo.

Só COPIA (leitura no Turso, escrita no fallback) -- nunca apaga nem altera
nada no Turso. Rode de novo sempre que quiser atualizar o fallback com o
que tiver de mais recente (ex.: de tempos em tempos, ou depois de um dia
de uso pesado).

COMO USAR:
    python scripts/migrar_para_fallback.py

Lê as credenciais direto do seu .streamlit/secrets.toml -- não precisa
digitar nada, desde que TURSO_DATABASE_URL/TURSO_AUTH_TOKEN e
FALLBACK_DATABASE_URL/FALLBACK_AUTH_TOKEN já estejam preenchidos lá.
"""
import sys
import tomllib
from pathlib import Path

try:
    import libsql
except ImportError:
    print("ERRO: pacote 'libsql' não encontrado. Rode: pip install -r requirements.txt")
    sys.exit(1)

CAMINHO_SECRETS = Path(".streamlit/secrets.toml")

if not CAMINHO_SECRETS.exists():
    print(f"ERRO: não encontrei {CAMINHO_SECRETS}. Rode este script a partir da raiz do projeto.")
    sys.exit(1)

with open(CAMINHO_SECRETS, "rb") as f:
    secrets = tomllib.load(f)

turso_url = secrets.get("TURSO_DATABASE_URL")
turso_token = secrets.get("TURSO_AUTH_TOKEN")
fallback_url = secrets.get("FALLBACK_DATABASE_URL")
fallback_token = secrets.get("FALLBACK_AUTH_TOKEN")

faltando = [
    nome for nome, valor in [
        ("TURSO_DATABASE_URL", turso_url), ("TURSO_AUTH_TOKEN", turso_token),
        ("FALLBACK_DATABASE_URL", fallback_url), ("FALLBACK_AUTH_TOKEN", fallback_token),
    ] if not valor
]
if faltando:
    print(f"ERRO: faltam estas chaves no secrets.toml: {', '.join(faltando)}")
    sys.exit(1)

print("Conectando no Turso (origem)...")
origem = libsql.connect(database=turso_url, auth_token=turso_token)
origem.execute("SELECT 1")
print("OK.")

print("Conectando no banco de emergência (destino)...")
destino = libsql.connect(database=fallback_url, auth_token=fallback_token)
destino.execute("SELECT 1")
print("OK.")

print("\nCriando estrutura das tabelas no destino (se ainda não existir)...")
destino.execute("""
    CREATE TABLE IF NOT EXISTS work_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        log_date TEXT NOT NULL,
        project TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        effort_hours REAL NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_impedimento INTEGER DEFAULT 0,
        is_duvida INTEGER DEFAULT 0,
        username TEXT
    );
""")
destino.execute("""
    CREATE TABLE IF NOT EXISTS custom_options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        option_type TEXT NOT NULL,
        value TEXT NOT NULL,
        username TEXT
    );
""")
destino.commit()
print("OK.")

# O PORQUE: apaga e recria o CONTEÚDO (não a estrutura) antes de copiar --
# assim rodar o script várias vezes sempre deixa o destino IGUAL ao Turso
# na hora, em vez de acumular duplicata a cada execução.
print("\nLimpando conteúdo anterior do destino (mantém a estrutura)...")
destino.execute("DELETE FROM work_logs")
destino.execute("DELETE FROM custom_options")
destino.commit()

print("\nCopiando work_logs...")
linhas = origem.execute(
    "SELECT id, log_date, project, category, description, effort_hours, "
    "created_at, is_impedimento, is_duvida, username FROM work_logs"
).fetchall()
if linhas:
    destino.executemany(
        "INSERT INTO work_logs (id, log_date, project, category, description, "
        "effort_hours, created_at, is_impedimento, is_duvida, username) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        linhas,
    )
    destino.commit()
print(f"  {len(linhas)} registro(s) de atividade copiado(s).")

print("\nCopiando custom_options...")
linhas_opcoes = origem.execute(
    "SELECT id, option_type, value, username FROM custom_options"
).fetchall()
if linhas_opcoes:
    destino.executemany(
        "INSERT INTO custom_options (id, option_type, value, username) VALUES (?, ?, ?, ?)",
        linhas_opcoes,
    )
    destino.commit()
print(f"  {len(linhas_opcoes)} projeto(s)/categoria(s) customizado(s) copiado(s).")

print("\n=== CONCLUÍDO ===")
print("O banco de emergência agora tem uma cópia completa e atualizada do Turso.")
