<#
    reorganizar_estrutura.ps1 (v2 -- atualizado com os arquivos criados
    desde a primeira versao deste script, que nunca chegou a ser rodada)

    O PORQUE: reorganiza a raiz do PersonalTrackerApp em docs/ e scripts/,
    usando "git mv" (preserva o historico de cada arquivo no Git) sempre que
    o arquivo ja estiver rastreado, e um Move-Item comum para o que nao
    estiver. Roda em modo seguro por padrao -- so MOSTRA o que faria; use
    -Aplicar pra executar de verdade.

    NAO TOCA em: .ignore/ (controle pessoal seu), __pycache__/, venv/,
    venv_linux/, .streamlit/secrets.toml, personal_tracker.db,
    raw_history.txt -- ficam exatamente onde estao.

    Uso:
        # 1) Rode sem parametros primeiro, pra revisar o que vai acontecer:
        .\reorganizar_estrutura.ps1

        # 2) Se estiver tudo certo, rode de novo com -Aplicar:
        .\reorganizar_estrutura.ps1 -Aplicar

    Rode a partir da RAIZ do projeto (onde esta o app.py).
#>

param(
    [switch]$Aplicar
)

# O PORQUE: sem isso, acentos aparecem corrompidos no console -- so
# cosmetico, nao afeta os arquivos movidos/criados.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Test-GitTracked {
    param([string]$Caminho)
    # O PORQUE: "git ls-files" SEM "--error-unmatch" nunca sai com erro --
    # so devolve o caminho (se rastreado) ou nada (se nao estiver). Evita
    # depender de tratamento de erro de comando externo, que se mostrou
    # instavel entre versoes do PowerShell numa tentativa anterior.
    $resultado = git ls-files -- "$Caminho" 2>$null
    return [bool]$resultado
}

function Mover-Item-Seguro {
    param(
        [string]$Origem,
        [string]$Destino
    )
    if (-not (Test-Path $Origem)) {
        Write-Host "  (pular -- nao existe) $Origem" -ForegroundColor DarkGray
        return
    }

    $rastreado = Test-GitTracked -Caminho $Origem

    if ($Aplicar) {
        if ($rastreado) {
            git mv -- "$Origem" "$Destino"
            Write-Host "  git mv  $Origem  ->  $Destino" -ForegroundColor Green
        } else {
            Move-Item -Force -- $Origem $Destino
            Write-Host "  move    $Origem  ->  $Destino  (nao rastreado pelo git)" -ForegroundColor Yellow
        }
    } else {
        $modo = if ($rastreado) { "git mv" } else { "move (nao rastreado)" }
        Write-Host "  [SIMULACAO] $modo :: $Origem  ->  $Destino" -ForegroundColor Cyan
    }
}

function Remover-Item-Seguro {
    param([string]$Caminho)
    if (-not (Test-Path $Caminho)) {
        Write-Host "  (pular -- nao existe) $Caminho" -ForegroundColor DarkGray
        return
    }
    if ($Aplicar) {
        Remove-Item -Recurse -Force -- $Caminho
        Write-Host "  removido: $Caminho" -ForegroundColor Green
    } else {
        Write-Host "  [SIMULACAO] removeria: $Caminho" -ForegroundColor Cyan
    }
}

Write-Host "`n=== 1) Criando pastas docs/ e scripts/ ===" -ForegroundColor Magenta
if ($Aplicar) {
    New-Item -ItemType Directory -Force -Path "docs" | Out-Null
    New-Item -ItemType Directory -Force -Path "scripts" | Out-Null
} else {
    Write-Host "  [SIMULACAO] New-Item docs/, scripts/" -ForegroundColor Cyan
}

Write-Host "`n=== 2) Movendo documentacao para docs/ ===" -ForegroundColor Magenta
Mover-Item-Seguro "Documentacao.md"              "docs/Documentacao.md"
Mover-Item-Seguro "Documentacao_v2.md"           "docs/Documentacao_v2.md"
Mover-Item-Seguro "TURSO_DEPLOY.md"              "docs/TURSO_DEPLOY.md"
# O PORQUE: nomes no disco vieram truncados/diferentes do gerado
# originalmente -- corrigindo pro nome padrao no destino.
Mover-Item-Seguro "changelog_seguranca_e_us.md"  "docs/CHANGELOG_SEGURANCA_E_UX.md"
Mover-Item-Seguro "Guia_Convidado_Admin.md"      "docs/GUIA_CONVIDADO_ADMIN.md"
Mover-Item-Seguro "GUIA_CONVIDADO_ADMIN.md"      "docs/GUIA_CONVIDADO_ADMIN.md"
Mover-Item-Seguro "GUIA_IA_ESFORCO_E_SESSAO.md"  "docs/GUIA_IA_ESFORCO_E_SESSAO.md"

Write-Host "`n=== 3) Movendo scripts utilitarios para scripts/ ===" -ForegroundColor Magenta
Mover-Item-Seguro "backfill_username.py"            "scripts/backfill_username.py"
Mover-Item-Seguro "fix_legacy_descriptions.py"      "scripts/fix_legacy_descriptions.py"
Mover-Item-Seguro "fix_project_reclassification.py" "scripts/fix_project_reclassification.py"
Mover-Item-Seguro "gerar_hash_de_senha.py"          "scripts/gerar_hash_de_senha.py"
Mover-Item-Seguro "import_history.py"               "scripts/import_history.py"
Mover-Item-Seguro "migrate_to_turso.py"             "scripts/migrate_to_turso.py"

Write-Host "`n=== 4) Colocando o modelo de secrets junto do secrets.toml real ===" -ForegroundColor Magenta
Mover-Item-Seguro "secrets.toml.example" ".streamlit/secrets.toml.example"

Write-Host "`n=== 5) Removendo duplicatas e lixo (v2_online/, .zip) ===" -ForegroundColor Magenta
Write-Host "  ATENCAO: confirme antes que nao ha nada unico dentro de v2_online/" -ForegroundColor Yellow
Remover-Item-Seguro "v2_online"
Remover-Item-Seguro "controle_de_atividades.zip"
Remover-Item-Seguro "v2_online_zip.zip"

Write-Host "`n=== 6) O que fica onde esta (sem mudanca) ===" -ForegroundColor Magenta
Write-Host "  app.py, database_core.py, importer_core.py, requirements.txt," -ForegroundColor DarkGray
Write-Host "  README.md, n8n_workflow_estimativa_ia.json, .gitignore -- ficam na raiz." -ForegroundColor DarkGray
Write-Host "  .ignore/, __pycache__/, venv/, venv_linux/, personal_tracker.db," -ForegroundColor DarkGray
Write-Host "  raw_history.txt, .streamlit/secrets.toml -- ficam exatamente onde estao." -ForegroundColor DarkGray

Write-Host "`n=== 7) Status final do git ===" -ForegroundColor Magenta
if ($Aplicar) {
    git status
} else {
    Write-Host "  (rode novamente com -Aplicar para executar de verdade)" -ForegroundColor Cyan
}

Write-Host "`nConcluido. Revise 'git status' e, se estiver tudo certo:" -ForegroundColor Magenta
Write-Host '  git add -A'
Write-Host '  git commit -m "Reorganiza estrutura: docs/, scripts/, remove duplicatas"'
