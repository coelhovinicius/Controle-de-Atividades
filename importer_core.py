import re
import os
import pandas as pd
from datetime import datetime

# O PORQUE: Isolar as constantes de classificacao no motor de importacao previne poluicao da interface grafica e centraliza a manutencao heuristica.
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

# O PORQUE: bullet "- " no início do item (ex.: "- Integração") não é removido
# por .strip(), pois strip() só descarta espaços/pontuação nas pontas, e "-"
# não estava nesse conjunto. Isso fazia toda descrição ser gravada com o
# traço sobrando. Esse regex resolve isso na origem, tanto para leitura de
# arquivo quanto para upload.
_BULLET_RE = re.compile(r'^-+\s*')


class HistoryParser:
    def __init__(self, file_path: str = "raw_history.txt"):
        self.file_path = file_path

    def _parse_date(self, date_str: str) -> str:
        try:
            return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    def _infer_project(self, text: str) -> str:
        text_lower = text.lower()
        for project, keywords in PROJECT_KEYWORDS.items():
            if any(keyword in text_lower for keyword in keywords):
                return project
        return "Outros"

    def _infer_category(self, text: str) -> str:
        text_lower = text.lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(keyword in text_lower for keyword in keywords):
                return category
        return "Outros"

    def extract_as_dataframe(self) -> pd.DataFrame:
        # O PORQUE: mantido para compatibilidade com quem ainda usa o script
        # de linha de comando (import_history.py), lendo direto do disco.
        if not os.path.exists(self.file_path):
            return pd.DataFrame()

        with open(self.file_path, 'r', encoding='utf-8') as file:
            raw_text = file.read()

        return self.parse_text(raw_text)

    def parse_text(self, raw_text: str) -> pd.DataFrame:
        # O PORQUE: extraído de extract_as_dataframe() para que a tela de
        # Sincronização possa processar o conteúdo de um arquivo enviado por
        # upload (em memória), sem depender de um caminho no disco/root.
        parsed_records = []
        current_date_iso = None
        current_task_buffer = []

        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("---") or line.startswith("AZURE BOARDS") or line.startswith("BACKLOG"):
                continue

            date_match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', line)

            if date_match and len(line) <= 30:
                self._flush_buffer(current_date_iso, current_task_buffer, parsed_records)
                current_task_buffer.clear()

                raw_date = date_match.group(1)
                current_date_iso = self._parse_date(raw_date)

                inline_text = line.replace(raw_date, "").strip(" :-.")
                if inline_text:
                    current_task_buffer.append(inline_text)
                continue

            if current_date_iso:
                if line.endswith(';') or line.endswith('.'):
                    current_task_buffer.append(line.rstrip(';.'))
                else:
                    if current_task_buffer:
                        current_task_buffer[-1] += f" {line}"
                    else:
                        current_task_buffer.append(line)

        self._flush_buffer(current_date_iso, current_task_buffer, parsed_records)

        df_parsed = pd.DataFrame(parsed_records, columns=["log_date", "project", "category", "description", "effort_hours"])
        return df_parsed

    def _flush_buffer(self, date_iso: str, tasks: list, record_list: list):
        if not date_iso or not tasks:
            return

        for task_desc in tasks:
            # O PORQUE: primeiro remove espaços/pontuação nas pontas, depois
            # remove o(s) traço(s) de bullet que sobrarem no início.
            task_clean = task_desc.strip()
            task_clean = _BULLET_RE.sub("", task_clean).strip()
            if not task_clean:
                continue

            project = self._infer_project(task_clean)
            category = self._infer_category(task_clean)
            
            # O PORQUE: Formato estruturado compatível com a schema do SQLite.
            record_list.append({
                "log_date": date_iso,
                "project": project,
                "category": category,
                "description": task_clean,
                "effort_hours": 1.0
            })