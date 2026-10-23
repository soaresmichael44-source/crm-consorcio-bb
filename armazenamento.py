"""Onde ficam os arquivos (documentos de clientes e fotos das listas).

Com SUPABASE_URL + SUPABASE_SERVICE_KEY: bucket PRIVADO do Supabase Storage
(região São Paulo), acessado só pelo servidor com a chave service_role — o
gerente nunca recebe link direto do Storage, sempre passa pelo portal.
Sem essas variáveis: pastas locais deste Mac (documentos/ e uploads/).
"""
import os
from pathlib import Path
from urllib.parse import quote

import requests

import db

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
BUCKET = os.getenv("SUPABASE_BUCKET", "crm-bb")
NUVEM = bool(SUPABASE_URL and SUPABASE_KEY)


def _cabecalhos(extra=None):
    return {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY, **(extra or {})}


def _url(caminho):
    return f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{quote(caminho)}"


def garantir_bucket():
    """Cria o bucket privado na primeira vez (se já existe, o Supabase responde 400/409 e seguimos)."""
    if not NUVEM:
        return
    r = requests.post(f"{SUPABASE_URL}/storage/v1/bucket", headers=_cabecalhos(),
                      json={"id": BUCKET, "name": BUCKET, "public": False, "file_size_limit": 30 * 1024 * 1024}, timeout=30)
    if r.status_code not in (200, 400, 409):
        r.raise_for_status()


def _pasta_local(area):
    return db.PASTA_DOCS if area == "documentos" else db.BASE / area


def salvar(area, caminho, dados, mime=None):
    """`area` = "documentos" ou "uploads"; `caminho` relativo à área."""
    if NUVEM:
        r = requests.post(_url(f"{area}/{caminho}"), data=dados, timeout=120,
                          headers=_cabecalhos({"Content-Type": mime or "application/octet-stream", "x-upsert": "true"}))
        r.raise_for_status()
        return
    arquivo = _pasta_local(area) / caminho
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_bytes(dados)


def ler(area, caminho):
    if NUVEM:
        r = requests.get(_url(f"{area}/{caminho}"), headers=_cabecalhos(), timeout=120)
        r.raise_for_status()
        return r.content
    return (_pasta_local(area) / caminho).read_bytes()
