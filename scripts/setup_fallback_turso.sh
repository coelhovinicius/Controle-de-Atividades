#!/bin/bash
# setup_fallback_turso.sh
#
# O QUE FAZ: instala o Docker (se não tiver) e sobe o libsql-server (o
# motor open-source por trás do Turso) na sua VM Oracle, como um "Turso
# caseiro" -- serve de plano B pra quando o Turso hospedado cair.
#
# COMO USAR:
#   1) Copie o arquivo jwt_public.pem (gerado por gerar_chaves_fallback.py)
#      pra essa mesma pasta, ANTES de rodar este script.
#   2) SSH na VM, cole este script inteiro num arquivo (ex.: nano setup_fallback_turso.sh),
#      dê permissão de execução e rode:
#        chmod +x setup_fallback_turso.sh
#        ./setup_fallback_turso.sh
#
# Sua VM é ARM (Ampere A1) -- este script já usa a imagem certa pra essa
# arquitetura (a "-arm"). Se um dia trocar de VM pra uma x86, é só tirar o
# "-arm" do nome da imagem mais abaixo.

set -e  # para na primeira falha, em vez de continuar e mascarar erro

echo "=== 1) Instalando Docker (pula se já tiver) ==="
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    echo "Docker instalado. Pode ser necessário desconectar e reconectar o SSH"
    echo "para o grupo 'docker' valer -- se o próximo passo der erro de permissão,"
    echo "faça isso e rode o script de novo."
else
    echo "Docker já está instalado, pulando."
fi

echo ""
echo "=== 2) Conferindo se jwt_public.pem está na pasta ==="
if [ ! -f "jwt_public.pem" ]; then
    echo "ERRO: não encontrei jwt_public.pem nesta pasta."
    echo "Gere com 'python gerar_chaves_fallback.py' no seu PC, copie o"
    echo "conteúdo da CHAVE PÚBLICA para um arquivo chamado jwt_public.pem"
    echo "aqui na VM (na mesma pasta deste script), e rode de novo."
    exit 1
fi
echo "OK, encontrado."

echo ""
echo "=== 3) Criando pasta de dados persistente ==="
mkdir -p "$HOME/turso-fallback/data"
cp jwt_public.pem "$HOME/turso-fallback/jwt_public.pem"

echo ""
echo "=== 4) Subindo o container (porta 8080) ==="
# O PORQUE do SQLD_HTTP_AUTH: essa imagem exige um valor de verdade aqui
# (não aceita vazio, nem deixar de definir) mesmo não sendo o método de
# autenticação que a gente usa (usamos SQLD_AUTH_JWT_KEY_FILE) -- gera um
# valor descartável só pra satisfazer essa exigência; nunca é usado de
# verdade, já que o app só se autentica via JWT.
DUMMY_BASIC_AUTH="basic:$(echo -n "unused:$(openssl rand -hex 16)" | base64 -w0)"

sudo docker rm -f turso-fallback 2>/dev/null || true
sudo docker run -d \
    --name turso-fallback \
    --restart unless-stopped \
    -p 8080:8080 \
    -v "$HOME/turso-fallback/data:/var/lib/sqld" \
    -v "$HOME/turso-fallback/jwt_public.pem:/etc/sqld/jwt_public.pem:ro" \
    -e SQLD_NODE=primary \
    -e SQLD_HTTP_AUTH="$DUMMY_BASIC_AUTH" \
    -e SQLD_AUTH_JWT_KEY_FILE=/etc/sqld/jwt_public.pem \
    ghcr.io/tursodatabase/libsql-server:latest-arm

echo ""
echo "=== 5) Aguardando o container subir... ==="
sleep 5
sudo docker ps --filter "name=turso-fallback"

echo ""
echo "=== PRONTO ==="
echo "Se a linha acima mostrar 'turso-fallback' com status 'Up', está no ar."
echo ""
echo "PRÓXIMO PASSO (fora da VM, no Console da Oracle Cloud):"
echo "  Libere a porta 8080 -- veja instruções separadas, é uma tela do"
echo "  navegador, não dá pra fazer por aqui."
echo ""
echo "Depois de liberar a porta, teste de fora da VM (no seu PC) com:"
echo "  curl http://<IP_PUBLICO_DA_VM>:8080"
echo "Se responder algo (mesmo um erro de autenticação), a porta está aberta."
