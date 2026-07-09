import re
import os
from datetime import datetime
from database_core import DatabaseConnection, LogRepository

# O PORQUE: O uso de constantes para mapeamento reduz a complexidade ciclomática e garante consistência de chaves com o frontend (Streamlit).
# O PORQUE: Backoffice e Cockpit eram absorvidos pelo projeto "360" (mesma
# keyword list). Passaram a ser projetos próprios a pedido do usuário -- cada
# um com sua própria entrada e keyword, sem afetar "360" (que continua
# cobrindo humor, pesquisa de clima e pdi).
# O PORQUE (ampliação): análise do histórico completo (raw_history.txt)
# mostrou ~49% dos registros caindo em "Outros" por falta de keyword --
# nomes de projeto recorrentes (Novos Planos, Gameficação, Automação de
# Cobrança, RH Summit, IPED, BI CPFL, Mini App, Viva+) não tinham entrada
# própria, e itens de GMUD/CAB (ex.: "Mudança (#425) ... Suporte
# Refuturiza") não batiam com nenhuma keyword de Sustentacao. Ajuste as
# listas abaixo livremente conforme novos projetos surgirem.
PROJECT_KEYWORDS = {
    "Passaporte": ["passaporte", "checkout", "kiddle", "vendedor", "cesta de serviços", "vale presente"],
    "360": ["360", "humor", "pesquisa de clima", "pdi", "migração legado", "migracao legado", "legado"],
    "Backoffice": ["backoffice"],
    "Cockpit": ["cockpit"],
    "Job Boards": ["job boards"],
    "Vagas": ["vagas"],
    "Motor RCE": ["motor rce", "motor mais todos"],
    "Price Up": ["price up"],
    "Novos Planos": ["novos planos"],
    "Gameficacao": ["gameficação", "gameficacao"],
    "Automacao de Cobranca": [
        "automação de cobrança", "automacao de cobranca", "link de conciliação",
        "link de conciliacao", "link de inadimplência", "link de inadimplencia",
    ],
    "Automacao QA": [
        "documentação qa", "documentacao qa", "quality-assurance-docs",
    ],
    "RH Summit": ["rh summit"],
    "IPED": ["iped"],
    "BI CPFL": ["cpfl"],
    "Mini App": ["mini app"],
    "Viva+": ["viva+", "viva mais"],
    "Sustentacao": [
        "sustentação", "sustentacao", "glpi", "war room", "bug", "chamado",
        "suporte refuturiza", "mudança (#", "mudanca (#", "gmud",
    ],
}

# O PORQUE (ampliação): "Análises/Especificações/Visão/Requisitos" era o
# maior bloco de descrições sem categoria (caindo em "Outros"). Inserida
# ANTES de "Documentacao"/"Estudos"/"Desenvolvimento de Testes" mas DEPOIS
# de "Resolucao/Testes de BUG/Problema" e "Execucao de Testes" -- assim, se a
# descrição também citar "bug"/"teste" explicitamente, essas categorias mais
# específicas continuam vencendo. "Apoio a Equipe" fica por último (menor
# prioridade), só pega o que mais nada capturou.
CATEGORY_KEYWORDS = {
    "Reuniao": ["reunião", "reuniao", "daily", "weekly", "alinhamento", "cab", "checkpoint", "apresentação", "workshop"],
    "Resolucao/Testes de BUG/Problema": ["bug", "problema", "chamado", "glpi", "war room", "falha", "erro", "incidente"],
    "Execucao de Testes": ["testes", "teste", "validação", "homologação", "hml", "prod", "postman", "exploração", "exploracao", "verificação", "verificacao"],
    "Analise de Requisitos/Especificacao": [
        "análise", "analise", "análises", "analises", "especificação", "especificacao",
        "especificações", "especificacoes", "requisito", "requisitos", "levantamento",
        "visão", "visao",
    ],
    "Documentacao": ["documentação", "documentacao", "manual", "cenários", "casos de teste", "pop", "relatório"],
    "Estudos/Certificacao": ["treinamento", "curso", "estudo", "certificado", "palestra", "python", "appium"],
    "Desenvolvimento de Testes": ["automação", "automacao", "criação de casos", "elaboração de testes"],
    "Apoio a Equipe": [
        "apoio à equipe", "apoio a equipe", "acompanhamento e reports",
        "acompanhamento de automações", "acompanhamento de automacoes",
        "passagem de conhecimento",
    ],
}

# O PORQUE: mesma lógica de PROJECT_KEYWORDS/CATEGORY_KEYWORDS, agora para
# marcar automaticamente registros importados como Impedimento e/ou Dúvida.
# Heurístico -- ajuste livremente conforme o vocabulário real do seu dia a
# dia. Registros manuais têm checkbox próprio, independente destas listas.
# O PORQUE (ampliação): achamos no histórico casos reais de impedimento que
# não batiam com a lista anterior -- "Loops - aguardando autorização para
# visualização", "Aguardando tratativas feitas por Marcos Souza / Wander" e
# "Testes Vale-Presente - Aguardando ajustes com Daniel Calistrato".
IMPEDIMENT_KEYWORDS = [
    "impedimento", "bloqueio", "bloqueado", "bloqueada", "travado", "travada",
    "aguardando liberação", "aguardando liberacao", "aguardando acesso",
    "sem acesso", "pendente de aprovação", "pendente de aprovacao",
    "aguardo retorno", "aguardando retorno",
    "aguardando autorização", "aguardando autorizacao",
    "aguardando ajustes", "aguardando tratativas",
]
QUESTION_KEYWORDS = [
    "dúvida", "duvida", "questionamento", "a confirmar", "validar com",
    "confirmar com", "perguntar para", "pendente de definição",
    "pendente de definicao", "alinhar com",
]

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

    def _infer_is_impedimento(self, text: str) -> bool:
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in IMPEDIMENT_KEYWORDS)

    def _infer_is_duvida(self, text: str) -> bool:
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in QUESTION_KEYWORDS)

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
            is_impedimento = self._infer_is_impedimento(task_clean)
            is_duvida = self._infer_is_duvida(task_clean)

            # O PORQUE: Esforço flat de 1.0 hora por registro inserido via batch, permitindo ajuste fino manual posterior via UI ou query SQL, caso necessário.
            self.repo.insert_log(
                log_date=date_iso,
                project=project,
                category=category,
                description=task_clean,
                effort_hours=1.0,
                is_impedimento=is_impedimento,
                is_duvida=is_duvida,
            )

if __name__ == "__main__":
    importer = HistoryImporter("raw_history.txt")
    importer.execute_import()