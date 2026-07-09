import re
import os
import io
import pandas as pd
from datetime import datetime

# O PORQUE: Isolar as constantes de classificacao no motor de importacao previne poluicao da interface grafica e centraliza a manutencao heuristica.
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
# É heurístico (baseado em palavras-chave na descrição) -- pode errar em
# casos ambíguos. Ajuste essas listas livremente conforme o vocabulário real
# do seu dia a dia. Registros criados manualmente na tela não dependem
# dessas listas: têm checkbox próprio no formulário.
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


def _infer_is_impedimento(text: str) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in IMPEDIMENT_KEYWORDS)


def _infer_is_duvida(text: str) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in QUESTION_KEYWORDS)

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

    def _infer_is_impedimento(self, text: str) -> bool:
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in IMPEDIMENT_KEYWORDS)

    def _infer_is_duvida(self, text: str) -> bool:
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in QUESTION_KEYWORDS)

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

        df_parsed = pd.DataFrame(parsed_records, columns=["log_date", "project", "category", "description", "effort_hours", "is_impedimento", "is_duvida"])
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
                "effort_hours": 1.0,
                "is_impedimento": self._infer_is_impedimento(task_clean),
                "is_duvida": self._infer_is_duvida(task_clean),
            })

    # O PORQUE: além do formato de log em .txt (parse_text), agora aceitamos
    # também um .csv já estruturado nas colunas do banco. Trabalha a partir
    # de bytes em memória (igual parse_text recebe texto em memória), para
    # servir tanto o upload da tela de Sincronização quanto qualquer uso via
    # script. Tenta primeiro o padrão pt-BR (separador ';' e decimal ',')
    # e, se as colunas esperadas não baterem, cai para o padrão US
    # (separador ',' e decimal '.') antes de desistir.
    REQUIRED_CSV_COLUMNS = ["log_date", "project", "category", "description", "effort_hours"]

    # O PORQUE: is_impedimento/is_duvida são OPCIONAIS no CSV -- se a planilha
    # já tiver essas colunas (ex.: exportada por este próprio app), respeita o
    # valor explícito de cada linha; se não tiver, calcula pela mesma
    # heurística de keywords usada no .txt, para o resultado ser consistente
    # entre os dois formatos de upload.
    OPTIONAL_FLAG_COLUMNS = ["is_impedimento", "is_duvida"]

    @staticmethod
    def _parse_bool_series(series: pd.Series) -> pd.Series:
        # O PORQUE: aceita as variações mais comuns de "verdadeiro" que podem
        # aparecer num CSV editado manualmente (1/0, true/false, sim/não),
        # sem depender de um único formato rígido.
        truthy = {"1", "true", "verdadeiro", "sim", "yes"}

        def parse_one(value):
            if pd.isna(value):
                return False
            return str(value).strip().lower() in truthy

        return series.apply(parse_one)

    @staticmethod
    def _parse_log_date_series(series: pd.Series) -> pd.Series:
        # O PORQUE: pd.to_datetime(..., dayfirst=True) parece a solução obvia
        # para aceitar "06/07/2026" (dd/mm/aaaa, padrao pt-BR) sem trocar dia e
        # mes -- mas na pratica o dayfirst do pandas tambem reinterpreta datas
        # ISO ja inequivocas (ex.: "2026-07-06" virava "2026-06-07"). Por isso
        # aqui tentamos formatos EXPLICITOS, um de cada vez, sem heuristica de
        # adivinhacao: primeiro ISO (YYYY-MM-DD), depois pt-BR (DD/MM/YYYY).
        def parse_one(value):
            text = str(value).strip()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                try:
                    return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            return None

        return series.apply(parse_one)

    def parse_csv(self, raw_bytes: bytes) -> pd.DataFrame:
        output_columns = self.REQUIRED_CSV_COLUMNS + self.OPTIONAL_FLAG_COLUMNS
        for sep, decimal in ((';', ','), (',', '.')):
            try:
                df_raw = pd.read_csv(io.BytesIO(raw_bytes), sep=sep, decimal=decimal)
            except Exception:
                continue

            if all(col in df_raw.columns for col in self.REQUIRED_CSV_COLUMNS):
                df = df_raw[self.REQUIRED_CSV_COLUMNS].copy()
                try:
                    df["log_date"] = self._parse_log_date_series(df["log_date"])
                    df["effort_hours"] = df["effort_hours"].astype(float)
                except Exception:
                    continue
                if df["log_date"].isna().any():
                    # Datas fora dos formatos aceitos (ISO ou dd/mm/aaaa) --
                    # tenta a proxima combinacao de separador/decimal antes de desistir.
                    continue
                df["project"] = df["project"].astype(str).str.strip()
                df["category"] = df["category"].astype(str).str.strip()
                df["description"] = df["description"].astype(str).apply(
                    lambda t: _BULLET_RE.sub("", t.strip()).strip()
                )

                if "is_impedimento" in df_raw.columns:
                    df["is_impedimento"] = self._parse_bool_series(df_raw["is_impedimento"])
                else:
                    df["is_impedimento"] = df["description"].apply(self._infer_is_impedimento)

                if "is_duvida" in df_raw.columns:
                    df["is_duvida"] = self._parse_bool_series(df_raw["is_duvida"])
                else:
                    df["is_duvida"] = df["description"].apply(self._infer_is_duvida)

                return df[output_columns]

        return pd.DataFrame(columns=output_columns)