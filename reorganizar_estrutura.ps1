<#
    reorganizar_estrutura.ps1

    O PORQUE: reorganiza a raiz do PersonalTrackerApp em docs/ e scripts/,
    usando "git mv" (preserva o histórico de cada arquivo no Git) sempre que
    o arquivo já estiver rastreado, e um Move-Item comum para o que não
    estiver (ex.: v2_online/, os .zip). Roda em modo seguro por padrão --
    só MOSTRA o que faria; use -Aplicar pra executar de verdade.

    Uso:
        # 1) Rode sem parâmetros primeiro, pra revisar o que vai acontecer:
        .\reorganizar_estrutura.ps1

        # 2) Se estiver tudo certo, rode de novo com -Aplicar:
        .\reorganizar_estrutura.ps1 -Aplicar

    Rode a partir da RAIZ do projeto (onde está o app.py).
#>

param(
    [switch]$Aplicar
)

# O PORQUE: sem isso, acentos (ex.: "documentação") aparecem corrompidos no
# console como "documentaÃ§Ã£o" -- é só a codificação de saída do terminal,
# não afeta em nada os arquivos movidos/criados.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ErrorActionPreference = "Stop"

# O PORQUE: no PowerShell 7.3+, por padrão, um comando externo (nativo) que
# escreve em stderr E sai com código != 0 é promovido a um erro FATAL,
# respeitando $ErrorActionPreference -- mesmo quando esse "erro" é só o jeito
# normal de um programa reportar uma condição esperada (como o
# "git ls-files --error-unmatch" abaixo, que existe justamente pra
# perguntar "esse arquivo está rastreado?" e responde "não" saindo com
# código 1). Sem esta linha, isso derrubava o script inteiro na primeira
# vez que encontrava um arquivo ainda não commitado. Aqui restauramos o
# comportamento clássico: comandos externos nunca viram exceção sozinhos,
# e continuamos checando $LASTEXITCODE manualmente (como já fazíamos).
$PSNativeCommandUseErrorActionPreference = $false

function Test-GitTracked {
    param([string]$Caminho)
    # O PORQUE: "git ls-files --error-unmatch" (usado antes aqui) sai com
    # código != 0 quando o arquivo NÃO está rastreado -- e isso, dependendo
    # da versão/config do PowerShell, pode virar um erro fatal mesmo com
    # $PSNativeCommandUseErrorActionPreference = $false (como você viu na
    # prática). "git ls-files" SEM essa flag nunca sai com erro: só
    # devolve o caminho (se rastreado) ou nada (se não estiver) -- e a
    # gente decide com base no que voltou, sem depender de nenhum
    # comportamento de tratamento de erro do PowerShell.
    $resultado = git ls-files -- "$Caminho" 2>$null
    return [bool]$resultado
}

function Mover-Item-Seguro {
    param(
        [string]$Origem,
        [string]$Destino
    )
    if (-not (Test-Path $Origem)) {
        Write-Host "  (pular -- não existe) $Origem" -ForegroundColor DarkGray
        return
    }

    $rastreado = Test-GitTracked -Caminho $Origem

    if ($Aplicar) {
        if ($rastreado) {
            git mv -- "$Origem" "$Destino"
            Write-Host "  git mv  $Origem  ->  $Destino" -ForegroundColor Green
        } else {
            Move-Item -Force -- $Origem $Destino
            Write-Host "  move    $Origem  ->  $Destino  (não rastreado pelo git)" -ForegroundColor Yellow
        }
    } else {
        $modo = if ($rastreado) { "git mv" } else { "move (não rastreado)" }
        Write-Host "  [SIMULACAO] $modo :: $Origem  ->  $Destino" -ForegroundColor Cyan
    }
}

function Remover-Item-Seguro {
    param([string]$Caminho)
    if (-not (Test-Path $Caminho)) {
        Write-Host "  (pular -- não existe) $Caminho" -ForegroundColor DarkGray
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

Write-Host "`n=== 2) Movendo documentação para docs/ ===" -ForegroundColor Magenta
Mover-Item-Seguro "Documentacao.md"              "docs/Documentacao.md"
Mover-Item-Seguro "Documentacao_v2.md"           "docs/Documentacao_v2.md"
Mover-Item-Seguro "TURSO_DEPLOY.md"              "docs/TURSO_DEPLOY.md"
# O PORQUE: o nome no disco veio truncado/diferente do gerado
# originalmente -- corrigindo pra CHANGELOG_SEGURANCA_E_UX.md no destino.
Mover-Item-Seguro "changelog_seguranca_e_us.md"  "docs/CHANGELOG_SEGURANCA_E_UX.md"

Write-Host "`n=== 3) Movendo scripts utilitários para scripts/ ===" -ForegroundColor Magenta
Mover-Item-Seguro "backfill_username.py"            "scripts/backfill_username.py"
Mover-Item-Seguro "fix_legacy_descriptions.py"      "scripts/fix_legacy_descriptions.py"
Mover-Item-Seguro "fix_project_reclassification.py" "scripts/fix_project_reclassification.py"
Mover-Item-Seguro "gerar_hash_de_senha.py"          "scripts/gerar_hash_de_senha.py"
Mover-Item-Seguro "import_history.py"               "scripts/import_history.py"
Mover-Item-Seguro "migrate_to_turso.py"             "scripts/migrate_to_turso.py"

Write-Host "`n=== 4) Removendo duplicatas e lixo (v2_online/, .zip) ===" -ForegroundColor Magenta
Write-Host "  ATENÇÃO: confirme antes que não há nada único dentro de v2_online/" -ForegroundColor Yellow
Remover-Item-Seguro "v2_online"
Remover-Item-Seguro "controle_de_atividades.zip"
Remover-Item-Seguro "v2_online_zip.zip"

Write-Host "`n=== 5) Verificando duplicidade de venv ===" -ForegroundColor Magenta
if ((Test-Path "venv") -and (Test-Path "venv_linux")) {
    Write-Host "  Encontrei venv/ E venv_linux/ juntos. Isso é esperado se você" -ForegroundColor Yellow
    Write-Host "  desenvolve em Windows E Linux/WSL -- mantenha os dois, ambos" -ForegroundColor Yellow
    Write-Host "  já cobertos pelo .gitignore novo. Se só usa um dos dois," -ForegroundColor Yellow
    Write-Host "  apague manualmente o que sobrou (não removo automaticamente" -ForegroundColor Yellow
    Write-Host "  por segurança)." -ForegroundColor Yellow
}

Write-Host "`n=== 6) Status final do git ===" -ForegroundColor Magenta
if ($Aplicar) {
    git status
} else {
    Write-Host "  (rode novamente com -Aplicar para executar de verdade)" -ForegroundColor Cyan
}

Write-Host "`nConcluído. Revise 'git status' e, se estiver tudo certo:" -ForegroundColor Magenta
Write-Host '  git add -A'
Write-Host '  git commit -m "Reorganiza estrutura: docs/, scripts/, remove duplicatas"'
