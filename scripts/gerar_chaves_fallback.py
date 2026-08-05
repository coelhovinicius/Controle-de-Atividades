"""
gerar_chaves_fallback.py

O QUE FAZ: gera o par de chaves (pública/privada) e o token de acesso
necessários para o banco auto-hospedado (libsql-server) que vai rodar na
sua VM Oracle -- o "Turso caseiro" que serve de plano B.

COMO USAR:
    python scripts/gerar_chaves_fallback.py

Roda no seu PC (não precisa estar na VM). Guarda a SAÍDA em lugar seguro --
principalmente a "CHAVE PRIVADA": se precisar gerar outro token depois
(revogar o antigo, por exemplo), é ela que assina um novo.
"""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from pathlib import Path
import jwt
import time

private_key = ed25519.Ed25519PrivateKey.generate()
public_key = private_key.public_key()

private_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

public_pem = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

# "a": "rw" = permissão de leitura E escrita (é o formato que o próprio
# sqld/libsql-server espera dentro do token).
token = jwt.encode({"a": "rw", "iat": int(time.time())}, private_pem, algorithm="EdDSA")

print("=" * 70)
print("CHAVE PÚBLICA -- salva automaticamente em jwt_public.pem, na pasta")
print("de onde você rodou este script (não precisa copiar/colar nada)")
print("=" * 70)
print(public_pem)

with open("jwt_public.pem", "w", encoding="utf-8") as f:
    f.write(public_pem)
print(f"✅ Arquivo jwt_public.pem criado em: {Path('jwt_public.pem').resolve()}")

print("=" * 70)
print("TOKEN -- copie e guarde. Vai virar o FALLBACK_AUTH_TOKEN no")
print("secrets.toml (NÃO precisa ir pra VM, só pro app)")
print("=" * 70)
print(token)

print("=" * 70)
print("CHAVE PRIVADA -- guarde em lugar seguro (ex.: gerenciador de senhas).")
print("NÃO vai pra VM nem pro secrets.toml -- só serve se precisar gerar")
print("outro token no futuro.")
print("=" * 70)
print(private_pem)
