"""Ponto de entrada na nuvem (Render): CRM e Portal do Gerente no mesmo serviço.

/g/..., /d/... (link do cliente para documentos) e /manifest/... vão para o
Portal do Gerente (gerente.py, acesso por token);
todo o resto vai para o CRM (app.py), que na nuvem exige login sempre.
Rodar com: gunicorn wsgi:app
"""
from werkzeug.middleware.proxy_fix import ProxyFix

import app as crm
import gerente

_ROTAS_GERENTE = ("/g/", "/d/", "/manifest/")


def _despachar(environ, start_response):
    caminho = environ.get("PATH_INFO", "")
    alvo = gerente.app if caminho.startswith(_ROTAS_GERENTE) else crm.app
    return alvo(environ, start_response)


# O Render fica na frente como proxy: IP e https reais vêm nos cabeçalhos X-Forwarded-*.
app = ProxyFix(_despachar, x_for=1, x_proto=1, x_host=1)
