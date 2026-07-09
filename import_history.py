import re
import os
from datetime import datetime
from database_core import DatabaseConnection, LogRepository

# O PORQUE: O uso de constantes para mapeamento reduz a complexidade ciclomática e garante consistência de chaves com o frontend (Streamlit).
PROJECT_KEYWORDS = {
    "Passaporte": ["passaporte", "checkout", "kiddle", "vendedor", "cesta de serviços", "vale presente"],
    "360": ["360", "cockpit", "backoffice", "humor", "pesquisa de clima", "pdi"],
    "Job Boards": ["job boards"],
    "Vagas": ["vagas"],
    "Motor RCE": ["motor rce", "motor mais todos"],
    "Price Up": ["price up"],
    "Sustentacao": ["sustentação", "sustentacao", "glpi", "war room", "bug", "chamado"]
}

CATEGORY_KEYWORDS = {
    "Reuniao": ["reunião", "reuniao", "daily", "weekly", "alinhamento", "cab", "checkpoint", "apresentação", "workshop"],
    "Resolucao de BUG/Problema": ["bug", "problema", "chamado", "glpi", "war room", "falha", "erro", "incidente"],
    "Execucao de Testes": ["testes", "teste", "validação", "homologação", "hml", "prod", "postman"],
    "Documentacao": ["documentação", "documentacao", "manual", "cenários", "casos de teste", "pop", "relatório"],
    "Estudos/Certificacao": ["treinamento", "curso", "estudo", "certificado", "palestra", "python", "appium"],
    "Desenvolvimento de Testes": ["automação", "automacao", "criação de casos", "elaboração de testes"]
}

class HistoryImporter:
    def __init__(self, raw_file_path: str):
        self.raw_file_path = raw_file_path
        db_conn = DatabaseConnection()
        self.repo = LogRepository(db_conn)

    def _parse_date(self, date_str: str) -> str:
        # O PORQUE: A padronizacao ISO 8601 (YYYY-MM-DD) é mandatória para ordenação e agrupamento cronológico correto no SQLite e Pandas.
        try:
            parsed_date = datetime.strptime(date_str, "%d/%m/%Y")
            return parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            return None

    def _infer_project(self, text: str) -> str:
        text_lower = text.lower()
        for project, keywords in PROJECT_KEYWORDS.items():
            if any(keyword in text_lower for keyword in keywords):
                return project
        # O PORQUE: Fallback padrão para atividades operacionais que não citam o projeto explicitamente, mantendo a integridade dos gráficos.
        return "Outros"

    def _infer_category(self, text: str) -> str:
        text_lower = text.lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(keyword in text_lower for keyword in keywords):
                return category
        return "Outros"

    def execute_import(self):
        if not os.path.exists(self.raw_file_path):
            print(f"Erro: Arquivo {self.raw_file_path} não encontrado.")
            return

        with open(self.raw_file_path, 'r', encoding='utf-8') as file:
            lines = file.readlines()

        current_date_iso = None
        current_task_buffer = []
        import_count = 0

        # O PORQUE: A varredura O(N) linha a linha previne estouro de memória (Memory Leak) em históricos de texto massivos.
        for line in lines:
            line = line.strip()
            if not line or line.startswith("---") or line.startswith("AZURE BOARDS") or line.startswith("BACKLOG"):
                continue

            # Busca por padrão de data (ex: 06/05/2025: ou 19/06/2025 - Feriado)
            date_match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', line)
            
            if date_match and len(line) <= 30: 
                # Processa o buffer pendente da data anterior antes de avançar
                self._flush_buffer(current_date_iso, current_task_buffer)
                import_count += len(current_task_buffer)
                current_task_buffer.clear()
                
                raw_date = date_match.group(1)
                current_date_iso = self._parse_date(raw_date)
                
                # Captura anotações na mesma linha da data (ex: Feriado, Day off)
                inline_text = line.replace(raw_date, "").strip(" :-.")
                if inline_text:
                    current_task_buffer.append(inline_text)
                continue

            if current_date_iso:
                # O PORQUE: O delimitador ';' é o padrão predominante no seu log para quebra de contexto operacional.
                if line.endswith(';') or line.endswith('.'):
                    current_task_buffer.append(line.rstrip(';.'))
                else:
                    # Anexa linhas quebradas ao último item do buffer (ex: links do Azure que quebraram de linha)
                    if current_task_buffer:
                        current_task_buffer[-1] += f" {line}"
                    else:
                        current_task_buffer.append(line)

        # Processa o resíduo do buffer final
        self._flush_buffer(current_date_iso, current_task_buffer)
        import_count += len(current_task_buffer)

        print(f"Carga ETL finalizada. {import_count} registros processados e inseridos no banco de dados com Governanca aplicada.")

    def _flush_buffer(self, date_iso: str, tasks: list):
        if not date_iso or not tasks:
            return

        for task_desc in tasks:
            task_clean = task_desc.strip()
            if not task_clean:
                continue

            project = self._infer_project(task_clean)
            category = self._infer_category(task_clean)
            
            # O PORQUE: Esforço flat de 1.0 hora por registro inserido via batch, permitindo ajuste fino manual posterior via UI ou query SQL, caso necessário.
            self.repo.insert_log(
                log_date=date_iso,
                project=project,
                category=category,
                description=task_clean,
                effort_hours=1.0
            )

if __name__ == "__main__":
    importer = HistoryImporter("raw_history.txt")
    importer.execute_import()