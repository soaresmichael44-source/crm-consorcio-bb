"""Copia o CRM BB deste Mac (crm_bb.db + documentos/ + uploads/) para a nuvem (Supabase).

Uso:  /usr/bin/python3 migrar_para_nuvem.py            (lê as credenciais de .env.nuvem)
      /usr/bin/python3 migrar_para_nuvem.py --destino postgresql://...   (teste em outro banco)

Só lê o banco local — nada aqui muda os dados do Mac. Recusa rodar se a nuvem já
tiver clientes, para nunca duplicar (use --forcar só se souber o que está fazendo).
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
TABELAS = ["gerentes", "lotes", "clientes", "interacoes", "config", "pacotes_docs", "documentos"]  # ordem das chaves estrangeiras

arg = argparse.ArgumentParser()
arg.add_argument("--destino", help="URL do Postgres de destino (padrão: SUPABASE_DB_URL do .env.nuvem)")
arg.add_argument("--forcar", action="store_true")
arg.add_argument("--sem-arquivos", action="store_true", help="copia só o banco, sem documentos/fotos")
op = arg.parse_args()

nuvem = dotenv_values(BASE / ".env.nuvem")
destino = op.destino or nuvem.get("SUPABASE_DB_URL")
if not destino:
    sys.exit("Falta SUPABASE_DB_URL no .env.nuvem.")
os.environ["DATABASE_URL"] = destino
if not op.destino and not op.sem_arquivos:
    os.environ["SUPABASE_URL"] = nuvem.get("SUPABASE_URL", "")
    os.environ["SUPABASE_SERVICE_KEY"] = nuvem.get("SUPABASE_SERVICE_KEY", "")
sys.path.insert(0, str(BASE))

import armazenamento  # noqa: E402  (depois das variáveis: os módulos leem o ambiente ao importar)
import db  # noqa: E402

origem = sqlite3.connect(f"file:{BASE / 'crm_bb.db'}?mode=ro", uri=True)
origem.row_factory = sqlite3.Row

db.iniciar()
with db.conectar() as conn:
    ja = conn.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
    if ja and not op.forcar:
        sys.exit(f"A nuvem já tem {ja} clientes. Nada foi copiado (use --forcar para copiar mesmo assim).")
    for tabela in TABELAS:
        linhas = origem.execute(f"SELECT * FROM {tabela}").fetchall()
        if not linhas:
            print(f"{tabela:13s} 0")
            continue
        cols = linhas[0].keys()
        conflito = " ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor" if tabela == "config" else ""
        sql = f"INSERT INTO {tabela} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}){conflito}"
        for r in linhas:
            conn.execute(sql, tuple(r))
        if tabela != "config":
            # ids vieram prontos do Mac: o contador do Postgres precisa continuar depois do maior.
            conn.execute(f"SELECT setval(pg_get_serial_sequence('{tabela}', 'id'), (SELECT MAX(id) FROM {tabela}))")
        print(f"{tabela:13s} {len(linhas)}")

if armazenamento.NUVEM and not op.sem_arquivos:
    armazenamento.garantir_bucket()
    enviados = 0
    for area, pasta in (("documentos", BASE / "documentos"), ("uploads", BASE / "uploads")):
        for arq in sorted(pasta.rglob("*")) if pasta.exists() else []:
            if arq.is_file():
                armazenamento.salvar(area, arq.relative_to(pasta).as_posix(), arq.read_bytes())
                enviados += 1
    print(f"arquivos      {enviados} enviados para o bucket privado '{armazenamento.BUCKET}'")
print("Pronto.")
