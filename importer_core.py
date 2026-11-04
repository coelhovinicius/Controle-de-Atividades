import re
import os
import io
import difflib
import unicodedata
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

# O PORQUE: marcadores de lista que aparecem no texto do Diário -- "-" do
# item principal e "○"/"•"/"▪" dos subitens que o OneNote gera ao colar.
_LINE_BULLET_RE = re.compile(r'^[-–•○◦▪*]+\s*')

# O PORQUE: linhas que o parser ignora (sobras de cópia do OneNote/Azure).
_SKIP_PREFIXES = ("---", "AZURE BOARDS", "BACKLOG")


def match_date_header(line: str):
    """Devolve a data (dd/mm/aaaa) se a linha (já sem espaços nas pontas)
    for um cabeçalho de dia, senão None. Mesma regra para todos os leitores
    de texto: uma data numa linha curta (<= 30 caracteres) abre um novo dia
    -- uma data no meio de uma descrição longa não conta."""
    date_match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', line)
    if date_match and len(line) <= 30:
        return date_match.group(1)
    return None


# O PORQUE: separa uma linha de tarefa em marcador/indentação, texto e
# pontuação final, para trocar só o texto e manter a linha como foi escrita.
_PARTES_DA_LINHA_RE = re.compile(r'^(\s*[-–•○◦▪*]*\s*)(.*?)([;.]*\s*)$')

# O PORQUE: abaixo disso, duas linhas são tarefas diferentes, e não a mesma
# tarefa reescrita. Tratar como "alterada" manteria as horas e o projeto da
# tarefa antiga numa tarefa que não tem nada a ver com ela.
_SIMILARIDADE_MINIMA_DE_ALTERACAO = 0.5


def _eventos_por_linha(raw_text: str) -> list:
    # O PORQUE: regra única de leitura linha a linha. O Diário usa o resultado
    # para gerar registros, e o Caderno para achar em qual linha do texto está
    # cada tarefa. Com duas cópias, um registro poderia deixar de ser
    # encontrado na própria anotação que o gerou.
    #
    # Devolve, em ordem, {"dia": "dd/mm/aaaa"} para cada cabeçalho de data e
    # {"tarefa", "linha", "linha_prefixo", "prefixo"} para cada tarefa.
    # "linha" é None quando a tarefa veio junto com a data, na mesma linha.
    eventos = []
    prefix = None
    prefix_line = None
    prefix_used = False
    em_anotacoes = False

    def close_prefix():
        nonlocal prefix, prefix_line, prefix_used
        if prefix and not prefix_used:
            eventos.append({"tarefa": prefix, "linha": prefix_line, "linha_prefixo": None, "prefixo": None})
        prefix, prefix_line, prefix_used = None, None, False

    for indice, raw_line in enumerate(raw_text.splitlines()):
        line = raw_line.strip()
        if not line:
            close_prefix()
            continue
        if line.startswith("---"):
            close_prefix()
            em_anotacoes = True
            continue
        if line.startswith(_SKIP_PREFIXES):
            continue

        raw_date = match_date_header(line)
        if raw_date:
            close_prefix()
            em_anotacoes = False
            eventos.append({"dia": raw_date})
            inline_text = line.replace(raw_date, "").strip(" :-.")
            if inline_text:
                eventos.append({"tarefa": inline_text, "linha": None, "linha_prefixo": None, "prefixo": None})
            continue

        if em_anotacoes:
            continue

        is_top_level = line.startswith(("-", "–"))
        text = _LINE_BULLET_RE.sub("", line).strip().rstrip(";.").strip()
        if not text:
            continue

        if text.endswith(":"):
            close_prefix()
            prefix = text.rstrip(":").strip()
            prefix_line = indice
            continue

        if prefix and not is_top_level:
            eventos.append({"tarefa": f"{prefix}: {text}", "linha": indice, "linha_prefixo": prefix_line, "prefixo": prefix})
            prefix_used = True
            continue

        close_prefix()
        eventos.append({"tarefa": text, "linha": indice, "linha_prefixo": None, "prefixo": None})

    close_prefix()
    return eventos


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
            if not line or line.startswith(_SKIP_PREFIXES):
                continue

            raw_date = match_date_header(line)

            if raw_date:
                self._flush_buffer(current_date_iso, current_task_buffer, parsed_records)
                current_task_buffer.clear()

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

    def parse_text_por_linha(self, raw_text: str) -> pd.DataFrame:
        # O PORQUE: leitor usado pelo Diário. Diferente de parse_text() (que
        # só fecha uma tarefa quando a linha termina em ";" ou "." e cola o
        # resto na tarefa anterior), aqui CADA LINHA é uma tarefa -- quem
        # digita direto no app não precisa lembrar do ";". parse_text() fica
        # intacto de propósito: a Sincronização compara descrições exatas
        # com o banco, e mudar o jeito de ler mudaria essas descrições.
        #
        # Linha terminada em ":" vira prefixo dos subitens logo abaixo
        # (formato que o OneNote gera ao colar):
        #     - Testes Vale Presente homolog:
        #         ○ CDT;
        #         ○ Não-CDT;
        # vira "Testes Vale Presente homolog: CDT" e "...: Não-CDT". O
        # prefixo vale até a próxima linha em branco, o próximo item "-" ou
        # a próxima data; se não vier nenhum subitem, o cabeçalho vira uma
        # tarefa sozinho.
        #
        # Uma linha "---" encerra as tarefas do dia: o que vem depois, até a
        # próxima data, são outras anotações (links, impedimentos em aberto).
        # Elas continuam no Caderno, via split_text_by_date(), mas não viram
        # registro.
        parsed_records = []
        current_date_iso = None
        tasks = []

        for evento in _eventos_por_linha(raw_text):
            if "dia" in evento:
                self._flush_buffer(current_date_iso, tasks, parsed_records)
                tasks.clear()
                current_date_iso = self._parse_date(evento["dia"])
            elif current_date_iso:
                tasks.append(evento["tarefa"])

        self._flush_buffer(current_date_iso, tasks, parsed_records)

        return pd.DataFrame(parsed_records, columns=["log_date", "project", "category", "description", "effort_hours", "is_impedimento", "is_duvida"])

    def registros_de_descricoes(self, date_iso: str, descricoes: list) -> pd.DataFrame:
        # O PORQUE: linhas novas escritas no Caderno já pertencem a um dia
        # conhecido -- recebem a mesma classificação por palavra-chave do
        # Diário, sem precisar remontar um texto com cabeçalho de data.
        parsed_records = []
        self._flush_buffer(date_iso, descricoes, parsed_records)
        return pd.DataFrame(parsed_records, columns=["log_date", "project", "category", "description", "effort_hours", "is_impedimento", "is_duvida"])

    @staticmethod
    def chave_tarefa(texto) -> str:
        # O PORQUE: compara tarefas ignorando acentos, maiúsculas e a
        # pontuação final -- "- Reunião X;" na anotação e "reuniao x" no
        # registro são a mesma tarefa.
        nfkd = unicodedata.normalize("NFKD", str(texto))
        sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
        return sem_acento.strip().rstrip(";.").strip().lower()

    @staticmethod
    def tarefas_do_texto(texto: str) -> list:
        """Descrições das tarefas do texto de um dia, com a regra do Diário:
        o que vem depois de uma linha "---" não é tarefa."""
        return [e["tarefa"] for e in _eventos_por_linha(texto) if "tarefa" in e]

    @staticmethod
    def tem_texto_alem_das_tarefas(texto: str) -> bool:
        """True se houver alguma linha que não vire tarefa: anotações depois
        de "---", linhas ignoradas pelo leitor ou um cabeçalho de data."""
        eventos = [e for e in _eventos_por_linha(texto) if "tarefa" in e]
        linhas_de_tarefa = {e["linha"] for e in eventos} | {e["linha_prefixo"] for e in eventos}
        return any(linha.strip() and i not in linhas_de_tarefa for i, linha in enumerate(texto.splitlines()))

    @staticmethod
    def comparar_tarefas(texto_antigo: str, texto_novo: str) -> tuple:
        """Compara duas versões do texto de um dia. Devolve (alteradas,
        removidas, incluidas), onde alteradas é uma lista de pares (antes,
        depois). Linhas iguais nas duas versões não aparecem em nenhuma lista.

        O PORQUE: compara o texto com ele mesmo, e não com os registros do
        dia. Um registro pode ter sido escrito de outro jeito (lançado à mão
        num dia com histórico importado), e comparar com o banco proporia
        excluí-lo sem a pessoa ter mexido nele. Assim, só o que foi mudado
        nesta edição vira proposta."""
        antigas = HistoryParser.tarefas_do_texto(texto_antigo)
        novas = HistoryParser.tarefas_do_texto(texto_novo)
        chave = HistoryParser.chave_tarefa

        def similaridade(a, b):
            return difflib.SequenceMatcher(None, chave(a), chave(b)).ratio()

        alteradas, removidas, incluidas = [], [], []
        for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, antigas, novas, autojunk=False).get_opcodes():
            if op == "equal":
                continue
            livres = novas[b1:b2]
            for antiga in antigas[a1:a2]:
                parecida = max(livres, key=lambda nova: similaridade(antiga, nova), default=None)
                if parecida is not None and similaridade(antiga, parecida) >= _SIMILARIDADE_MINIMA_DE_ALTERACAO:
                    alteradas.append((antiga, parecida))
                    livres.remove(parecida)
                else:
                    removidas.append(antiga)
            incluidas.extend(livres)

        # O PORQUE: linha só mudada de lugar aparece como removida + incluída.
        # Sem isto, trocar duas linhas de ordem excluiria um registro (com as
        # horas dele) para criar outro igual.
        for antiga in list(removidas):
            mesma = next((nova for nova in incluidas if chave(nova) == chave(antiga)), None)
            if mesma is not None:
                removidas.remove(antiga)
                incluidas.remove(mesma)
                if mesma != antiga:
                    alteradas.append((antiga, mesma))

        return alteradas, removidas, incluidas

    @staticmethod
    def trocar_tarefa_no_texto(texto: str, descricao_antiga: str, descricao_nova: str = None) -> tuple:
        """Troca a linha da tarefa no texto de um dia, ou apaga a linha se
        descricao_nova for None. Mantém marcador, indentação e pontuação
        final, e não mexe no que vem depois de "---". Devolve (texto, achou)."""
        chave = HistoryParser.chave_tarefa
        eventos = [e for e in _eventos_por_linha(texto) if "tarefa" in e]
        alvo = next(
            (e for e in eventos if e["linha"] is not None and chave(e["tarefa"]) == chave(descricao_antiga)),
            None,
        )
        if alvo is None:
            return texto, False

        linhas = texto.splitlines()
        trecho_novo = descricao_nova
        if descricao_nova is not None and alvo["prefixo"]:
            # O PORQUE: num subitem ("Testes:" + "○ CDT"), a linha guarda só
            # "CDT". Se o texto novo não começar mais com o prefixo, a tarefa
            # não pertence mais ao grupo: sai dele e vai para uma linha própria.
            if chave(descricao_nova).startswith(chave(alvo["prefixo"]) + ":"):
                trecho_novo = descricao_nova.strip()[len(alvo["prefixo"]):].lstrip(": ")
            else:
                trecho_novo = None

        if trecho_novo is not None:
            partes = _PARTES_DA_LINHA_RE.match(linhas[alvo["linha"]])
            linhas[alvo["linha"]] = f"{partes.group(1)}{trecho_novo}{partes.group(3)}"
            return "\n".join(linhas), True

        indices_a_apagar = [alvo["linha"]]
        # O PORQUE: sem o último subitem, o cabeçalho "Testes:" viraria uma
        # tarefa sozinho na próxima leitura -- uma tarefa que nunca existiu.
        if alvo["prefixo"] and sum(e["linha_prefixo"] == alvo["linha_prefixo"] for e in eventos) == 1:
            indices_a_apagar.append(alvo["linha_prefixo"])
        for indice in sorted(indices_a_apagar, reverse=True):
            del linhas[indice]

        texto_novo = "\n".join(linhas)
        if descricao_nova is not None:
            texto_novo = HistoryParser.acrescentar_tarefa_no_texto(texto_novo, descricao_nova)
        return texto_novo, True

    @staticmethod
    def acrescentar_tarefa_no_texto(texto: str, descricao: str) -> str:
        """Acrescenta a tarefa depois das outras tarefas do dia, antes de uma
        eventual linha "---". Se ela já estiver no texto, nada muda."""
        chave = HistoryParser.chave_tarefa
        if chave(descricao) in {chave(t) for t in HistoryParser.tarefas_do_texto(texto)}:
            return texto
        linhas = texto.splitlines()
        posicao = next((i for i, linha in enumerate(linhas) if linha.strip().startswith("---")), len(linhas))
        while posicao > 0 and not linhas[posicao - 1].strip():
            posicao -= 1
        linhas.insert(posicao, f"- {descricao}")
        return "\n".join(linhas)

    @staticmethod
    def split_text_by_date(raw_text: str) -> dict:
        """Separa o texto em {"YYYY-MM-DD": texto do dia}, preservando as
        linhas como foram escritas (é o que vai para o caderno do Diário).
        Usa a mesma regra de cabeçalho de data dos leitores acima; texto
        antes da primeira data é ignorado. Se a mesma data aparecer duas
        vezes, os trechos são juntados."""
        days = {}
        current_date_iso = None
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            raw_date = match_date_header(line) if line else None
            if raw_date:
                try:
                    current_date_iso = datetime.strptime(raw_date, "%d/%m/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    current_date_iso = None
                    continue
                days.setdefault(current_date_iso, [])
                inline_text = line.replace(raw_date, "").strip(" :-.")
                if inline_text:
                    days[current_date_iso].append(inline_text)
                continue
            if current_date_iso:
                days[current_date_iso].append(raw_line.rstrip())

        return {d: "\n".join(lines).strip("\n") for d, lines in days.items() if "\n".join(lines).strip()}

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