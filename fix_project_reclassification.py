"""
Script de correção pontual (rodar UMA VEZ) para reclassificar registros que
foram importados como "Job Boards", "Passaporte" ou "360" antes da separação
dos projetos "Vagas"/"Price Up"/"Backoffice"/"Cockpit" nas keywords do parser
(import_history.py / importer_core.py).

O PORQUE: antes dessas mudanças, a keyword "vagas" apontava para o projeto
"Job Boards", "price up" apontava para "Passaporte", e "backoffice"/"cockpit"
apontavam para "360" (embora todos já existissem ou passassem a existir como
projetos próprios). Este script corrige apenas os registros históricos que
foram classificados pela regra antiga, sem tocar em mais nada — é seguro
rodar mais de uma vez: depois de corrigido, o WHERE não encontra mais nada
(o project já não é mais o antigo para essas linhas).

Uso:
    python fix_project_reclassification.py [caminho_do_banco]

Por padrão usa personal_tracker.db no diretório atual.
"""
import sqlite3
import sys

# O PORQUE: cada tupla é (projeto_antigo, projeto_novo, padrao_LIKE) —
# mesma logica de keyword que agora vive em PROJECT_KEYWORDS nos parsers,
# aplicada apenas retroativamente aos dados ja gravados.
RECLASSIFICATIONS = [
    ("Job Boards", "Vagas", "%vagas%"),
    ("Passaporte", "Price Up", "%price up%"),
    ("360", "Backoffice", "%backoffice%"),
    ("360", "Cockpit", "%cockpit%"),
]


def main(db_path: str = "personal_tracker.db"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    total = 0
    for old_project, new_project, like_pattern in RECLASSIFICATIONS:
        rows = cur.execute(
            "SELECT id FROM work_logs WHERE project = ? AND LOWER(description) LIKE ?",
            (old_project, like_pattern),
        ).fetchall()

        if not rows:
            continue

        cur.execute(
            "UPDATE work_logs SET project = ? WHERE project = ? AND LOWER(description) LIKE ?",
            (new_project, old_project, like_pattern),
        )
        conn.commit()
        print(f"{len(rows)} registro(s) reclassificado(s) de '{old_project}' para '{new_project}'.")
        total += len(rows)

    if total == 0:
        print("Nenhum registro para reclassificar. Nada a fazer.")
    else:
        print(f"Total: {total} registro(s) atualizado(s) em '{db_path}'.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "personal_tracker.db"
    main(path)
