"""
Utilitário de linha de comando para gerar o hash bcrypt de uma senha, para
colar em [credentials] no secrets.toml -- em vez de guardar a senha em
texto puro.

O PORQUE: com a mudança em app.py (_validar_login agora usa
bcrypt.checkpw), os valores em [credentials] deixaram de ser a senha crua e
passaram a ser o HASH bcrypt dela. Rode este script uma vez por usuário/
senha (ou toda vez que quiser trocar uma senha) e cole o resultado no
secrets.toml -- nunca cole a senha em texto puro lá.

Uso (interativo, não deixa a senha visível no terminal nem no histórico de
comandos do shell):
    python gerar_hash_senha.py

Uso (não interativo, ex.: gerar em lote/script -- CUIDADO: a senha fica
visível no histórico do shell/terminal neste modo):
    python gerar_hash_senha.py "minha_senha_aqui"
"""
import sys
import getpass
import bcrypt


def gerar_hash(senha: str) -> str:
    # O PORQUE: bcrypt.gensalt() usa um custo (rounds) padrão de 12, que é um
    # bom equilíbrio entre segurança e tempo de verificação no login (uns
    # 100-300ms por tentativa). Não precisa mexer nisso.
    hash_bytes = bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt())
    return hash_bytes.decode("utf-8")


def main():
    if len(sys.argv) > 1:
        senha = sys.argv[1]
    else:
        senha = getpass.getpass("Digite a senha (não aparece na tela): ")
        confirmacao = getpass.getpass("Confirme a senha: ")
        if senha != confirmacao:
            print("Erro: as senhas digitadas não coincidem.")
            sys.exit(1)

    if not senha:
        print("Erro: a senha não pode ser vazia.")
        sys.exit(1)

    hash_gerado = gerar_hash(senha)
    print("\nCole esta linha em [credentials] no seu secrets.toml:\n")
    print(f'"seu_usuario" = "{hash_gerado}"')
    print(
        "\n(troque \"seu_usuario\" pelo nome de usuário real -- o hash acima "
        "já está pronto para uso, não precisa editar mais nada nele.)"
    )


if __name__ == "__main__":
    main()
