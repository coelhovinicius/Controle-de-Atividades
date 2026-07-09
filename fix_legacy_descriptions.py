"""
Script de correção pontual (rodar UMA VEZ) para limpar o "- " residual no
início das descrições já importadas antes da correção do parser.

O PORQUE: o bug estava no parser (import_history.py / importer_core.py),
não nos dados em si. Em vez de apagar e reimportar tudo pela tela de
Sincronização (o que geraria novos IDs e perderia o created_at original),
este script faz um UPDATE cirúrgico apenas nas linhas afetadas.

Uso:
    python fix_legacy_descriptions.py [caminho_do_banco]

Por padrão usa personal_tracker.db no diretório atual. É seguro rodar mais
de uma vez: na segunda execução ele simplesmente não encontra mais nada
para corrigir.
"""
import re
import sqlite3
import sys

_BULLET_RE = re.compile(r'^-+\s*')


def main(db_path: str = "personal_tracker.db"):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    rows = cur.execute("SELECT id, description FROM work_logs WHERE description LIKE '-%'").fetchall()

    if not rows:
        print("Nenhuma descrição com '-' residual encontrada. Nada para corrigir.")
        return

    updates = []
    for row_id, desc in rows:
        clean = _BULLET_RE.sub("", desc).strip()
        if clean != desc:
            updates.append((clean, row_id))

    if not updates:
        print("Nenhuma descrição precisava de correção.")
        return

    cur.executemany("UPDATE work_logs SET description = ? WHERE id = ?", updates)
    conn.commit()
    print(f"{len(updates)} descrição(ões) corrigida(s) em '{db_path}'.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "personal_tracker.db"
    main(path)