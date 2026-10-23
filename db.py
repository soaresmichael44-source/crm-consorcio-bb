"""Banco do CRM BB — operação exclusiva de clientes do Consórcio BB.

Com DATABASE_URL (nuvem, Postgres do Supabase) usa Postgres; sem ela, o SQLite
deste Mac. O resto do sistema escreve SQL no dialeto do SQLite ("?" como
parâmetro) e a classe _ConexaoPg traduz o pouco que muda.
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
# CRM_BB_DB aponta para outro arquivo (ex.: a cópia de demonstração, sem clientes reais).
DB_PATH = Path(os.getenv("CRM_BB_DB") or BASE / "crm_bb.db")
DATABASE_URL = os.getenv("DATABASE_URL", "")
PG = bool(DATABASE_URL)

ETAPAS = [
    ("novo", "Novo (ainda não contatado)"),
    ("contatado", "Contatado, sem resposta"),
    ("respondeu", "Respondeu"),
    ("negociando", "Negociando / simulação"),
    ("documentacao", "Documentação recebida"),
    ("vendido", "Vendido"),
    ("perdido", "Perdido"),
    ("optout", "Pediu para não receber"),
]
ETAPAS_DICT = dict(ETAPAS)
# Etapas em que a cadência automática de follow-up para de vez.
ETAPAS_FINAIS = {"vendido", "perdido", "optout"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em TEXT NOT NULL,
    descricao TEXT,
    qtd_imagens INTEGER DEFAULT 0,
    qtd_clientes INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    telefone TEXT,
    email TEXT,
    cidade TEXT,
    produto TEXT,              -- auto, imóvel, moto, serviços, pesados...
    valor_credito REAL,
    situacao_bb TEXT,          -- situação como veio na lista do banco (ex.: "fez simulação")
    observacoes TEXT,
    extras TEXT,               -- JSON com qualquer outra coluna lida da lista
    etapa TEXT NOT NULL DEFAULT 'novo',
    lote_id INTEGER REFERENCES lotes(id),
    tentativas INTEGER NOT NULL DEFAULT 0,
    ultimo_contato TEXT,
    proximo_contato TEXT,      -- quando a cadência manda o próximo follow-up
    pausado INTEGER NOT NULL DEFAULT 0,
    rascunho TEXT,             -- próxima mensagem já redigida pela IA, aguardando envio
    rascunho_assunto TEXT,
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clientes_tel ON clientes(telefone);
CREATE INDEX IF NOT EXISTS idx_clientes_prox ON clientes(proximo_contato);
CREATE TABLE IF NOT EXISTS interacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    criado_em TEXT NOT NULL,
    canal TEXT NOT NULL,       -- whatsapp, email, nota, sistema
    direcao TEXT NOT NULL,     -- saida, entrada, interna
    status TEXT,               -- enviado, aberto_manual, falhou, bloqueado
    assunto TEXT,
    texto TEXT
);
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
-- Gerentes do banco (Portal do Gerente, link com token).
CREATE TABLE IF NOT EXISTS gerentes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    agencia TEXT,
    token TEXT NOT NULL UNIQUE,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL,
    ultimo_envio TEXT
);
-- Documentos de clientes novos que o consultor leva ao banco: chegam no Portal do Gerente.
CREATE TABLE IF NOT EXISTS pacotes_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gerente_id INTEGER NOT NULL REFERENCES gerentes(id),
    cliente_id INTEGER REFERENCES clientes(id),
    cliente_nome TEXT NOT NULL,
    produto TEXT,
    observacao TEXT,
    criado_em TEXT NOT NULL,
    visto_em TEXT,             -- gerente abriu a aba Documentos depois do envio
    recebido_em TEXT           -- gerente marcou "Recebi, vou dar andamento"
);
CREATE TABLE IF NOT EXISTS documentos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pacote_id INTEGER NOT NULL REFERENCES pacotes_docs(id) ON DELETE CASCADE,
    nome TEXT NOT NULL,        -- nome original do arquivo, como aparece para o gerente
    caminho TEXT NOT NULL,     -- relativo a documentos/
    mime TEXT,
    tamanho INTEGER,
    baixado_em TEXT
);
"""

# Colunas adicionadas depois que o banco já estava em uso (ALTER idempotente).
MIGRACOES = [
    ("lotes", "gerente_id", "INTEGER REFERENCES gerentes(id)"),
    ("lotes", "visto", "INTEGER NOT NULL DEFAULT 1"),
    ("gerentes", "ultima_visita", "TEXT"),
    # CPF só entra quando digitado pelo gerente no cadastro manual (a leitura de listas continua ignorando CPF).
    ("clientes", "cpf", "TEXT"),
    # Link público do cliente para mandar documentos direto ao gerente (separado do token do gerente).
    ("gerentes", "token_docs", "TEXT"),
    ("pacotes_docs", "origem", "TEXT"),            # consultor | cliente
    ("pacotes_docs", "cliente_telefone", "TEXT"),
    ("pacotes_docs", "visto_consultor", "INTEGER NOT NULL DEFAULT 0"),  # aviso no Painel do CRM
]

CONFIG_PADRAO = {
    "nome_consultor": "Michael",
    "nome_empresa": "Mobie",
    # manual = o sistema gera a mensagem e o botão abre o WhatsApp para você
    # apertar "enviar"; ponte = envia sozinho pelo robô (~/claude-whatsapp).
    "modo_whatsapp": "manual",
    # Trava geral: com "0" nada sai sozinho (nem WhatsApp pelo robô, nem e-mail);
    # o sistema só deixa as mensagens prontas na fila. Ligar só quando a
    # abordagem estiver alinhada e o número da operação BB estiver pronto.
    "envio_automatico": "0",
    "email_ativo": "0",
    "max_envios_dia": "40",
    # Cadência em dias depois do 1º contato. Depois da última, repete a cada
    # `intervalo_final` dias até o cliente comprar, sair ou pedir para parar.
    "cadencia_dias": "0,1,3,7,14,21,30",
    "intervalo_final": "15",
    "hora_inicio": "9",
    "hora_fim": "19",
}


def agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class _Linha(dict):
    """Linha do Postgres que também aceita índice (r[0]), como o sqlite3.Row."""
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


def _fabrica_linha(cursor):
    nomes = [c.name for c in cursor.description] if cursor.description else []
    return lambda valores: _Linha(zip(nomes, valores))


class _CursorPg:
    def __init__(self, cur, lastrowid=None):
        self._cur, self.lastrowid = cur, lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())


_RE_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.I)


class _ConexaoPg:
    """Imita o pedaço da API do sqlite3 que o sistema usa (execute, commit, with)."""
    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.getconn()

    def execute(self, sql, params=()):
        sql = sql.replace("%", "%%").replace("?", "%s").replace(" LIKE ", " ILIKE ")
        m = _RE_INSERT.match(sql)
        devolve_id = bool(m) and m.group(1).lower() != "config" and "RETURNING" not in sql.upper()
        if devolve_id:
            sql += " RETURNING id"
        cur = self._conn.cursor(row_factory=_fabrica_linha)
        cur.execute(sql, params)
        return _CursorPg(cur, cur.fetchone()["id"] if devolve_id else None)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def __enter__(self):
        return self

    def __exit__(self, tipo, *_):
        try:
            self._conn.rollback() if tipo else self._conn.commit()
        except Exception:
            if not tipo:
                raise  # falha ao gravar: o erro tem de aparecer
        finally:
            self._pool.putconn(self._conn)  # o pool descarta sozinho conexão quebrada


_pool = None
_pool_lock = threading.Lock()


def _pool_pg():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool
            # prepare_threshold=None: o pooler do Supabase não guarda prepared statements entre clientes.
            # check: testa a conexão antes de entregar (o pooler do Supabase derruba conexões paradas);
            # max_idle: descarta conexões ociosas antes que o servidor as feche.
            _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=int(os.getenv("PG_POOL_MAX", "5")),
                                   kwargs={"prepare_threshold": None}, check=ConnectionPool.check_connection,
                                   max_idle=120, open=True)
        return _pool


def conectar():
    if PG:
        return _ConexaoPg(_pool_pg())
    # Dois processos usam o banco (app.py e gerente.py): espera o lock em vez de falhar.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _schema_pg():
    return (SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            .replace(" REAL", " DOUBLE PRECISION"))


def iniciar():
    with conectar() as conn:
        if PG:
            for comando in _schema_pg().split(";"):
                linhas = [l for l in comando.splitlines() if not l.strip().startswith("--")]
                if "".join(linhas).strip():
                    conn.execute("\n".join(linhas))
            for tabela, coluna, tipo in MIGRACOES:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN IF NOT EXISTS {coluna} {tipo}")
        else:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            for tabela, coluna, tipo in MIGRACOES:
                existentes = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
                if coluna not in existentes:
                    try:
                        conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
                    except sqlite3.OperationalError as e:
                        # app.py e gerente.py sobem juntos: o outro processo pode ter criado antes.
                        if "duplicate column" not in str(e):
                            raise
        for k, v in CONFIG_PADRAO.items():
            conn.execute("INSERT INTO config (chave, valor) VALUES (?, ?) ON CONFLICT (chave) DO NOTHING", (k, v))


def config(conn):
    return {r["chave"]: r["valor"] for r in conn.execute("SELECT chave, valor FROM config")}


def salvar_config(conn, dados):
    for k in CONFIG_PADRAO:
        if k in dados:
            conn.execute("INSERT INTO config (chave, valor) VALUES (?, ?) "
                         "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor", (k, str(dados[k]).strip()))


def normalizar_telefone(tel):
    """Devolve só dígitos no formato 55DDDNUMERO, ou '' se não der para usar."""
    d = re.sub(r"\D", "", str(tel or ""))
    if not d:
        return ""
    d = d.lstrip("0")
    if len(d) in (10, 11):
        d = "55" + d
    if len(d) not in (12, 13) or not d.startswith("55"):
        return ""
    return d


def telefone_formatado(d):
    if not d or len(d) < 12:
        return d or ""
    ddd, num = d[2:4], d[4:]
    return f"({ddd}) {num[:-4]}-{num[-4:]}"


def normalizar_cpf(cpf):
    """Só dígitos, se for um CPF válido (dígitos verificadores); senão ''."""
    d = re.sub(r"\D", "", str(cpf or ""))
    if len(d) != 11 or d == d[0] * 11:
        return ""
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != int(d[n]):
            return ""
    return d


def cpf_formatado(d):
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if d and len(d) == 11 else (d or "")


def valor_em_reais(v):
    """Aceita 80000, "80000.00", "80.000", "R$ 80.000,00"."""
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d.,]", "", str(v or ""))
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    elif t.count(".") == 1 and len(t.split(".")[1]) <= 2:
        pass  # ponto decimal: "80000.00"
    else:
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def registrar(conn, cliente_id, canal, direcao, texto, status=None, assunto=None):
    conn.execute(
        "INSERT INTO interacoes (cliente_id, criado_em, canal, direcao, status, assunto, texto) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cliente_id, agora(), canal, direcao, status, assunto, texto),
    )


def inserir_cliente(conn, c, lote_id=None):
    """Insere ou atualiza (mesmo telefone = mesmo cliente). Devolve (id, novo?)."""
    tel = normalizar_telefone(c.get("telefone"))
    extras = c.get("extras") or {}
    valor = valor_em_reais(c.get("valor_credito"))
    campos = {
        "nome": (c.get("nome") or "").strip().title() or "Sem nome",
        "telefone": tel,
        "email": (c.get("email") or "").strip().lower(),
        "cidade": (c.get("cidade") or "").strip(),
        "produto": (c.get("produto") or "").strip(),
        "valor_credito": valor,
        "situacao_bb": (c.get("situacao_bb") or "").strip(),
        "observacoes": (c.get("observacoes") or "").strip(),
        "extras": json.dumps(extras, ensure_ascii=False) if extras else None,
        "cpf": normalizar_cpf(c.get("cpf")) or None,
    }
    existente = None
    if tel:
        existente = conn.execute("SELECT id FROM clientes WHERE telefone = ?", (tel,)).fetchone()
    if not existente and campos["cpf"]:
        existente = conn.execute("SELECT id FROM clientes WHERE cpf = ?", (campos["cpf"],)).fetchone()
    if existente:
        # Lista nova do banco pode trazer situação atualizada: só preenche o que veio.
        sets = {k: v for k, v in campos.items() if v not in (None, "")}
        sets["atualizado_em"] = agora()
        conn.execute(
            f"UPDATE clientes SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), existente["id"]),
        )
        registrar(conn, existente["id"], "sistema", "interna", "Cliente apareceu novamente numa lista do BB; dados atualizados.")
        return existente["id"], False
    ts = agora()
    cur = conn.execute(
        "INSERT INTO clientes (nome, telefone, email, cidade, produto, valor_credito, situacao_bb, "
        "observacoes, extras, cpf, lote_id, proximo_contato, criado_em, atualizado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*campos.values(), lote_id, ts, ts, ts),
    )
    registrar(conn, cur.lastrowid, "sistema", "interna", "Cliente cadastrado a partir de lista do BB.")
    return cur.lastrowid, True


def importar_lote(conn, clientes, descricao="", qtd_imagens=0, gerente_id=None):
    """Grava um lote de clientes lidos das listas. Devolve (lote_id, novos, atualizados).
    Lote vindo de gerente fica `visto = 0` até o consultor abrir o painel."""
    cur = conn.execute(
        "INSERT INTO lotes (criado_em, descricao, qtd_imagens, gerente_id, visto) VALUES (?, ?, ?, ?, ?)",
        (agora(), descricao, qtd_imagens, gerente_id, 0 if gerente_id else 1),
    )
    lote_id = cur.lastrowid
    novos = atualizados = 0
    for c in clientes:
        _, novo = inserir_cliente(conn, c, lote_id)
        novos += novo
        atualizados += not novo
    conn.execute("UPDATE lotes SET qtd_clientes = ? WHERE id = ?", (novos + atualizados, lote_id))
    return lote_id, novos, atualizados


PASTA_DOCS = BASE / "documentos"
EXTENSOES_DOCS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".doc", ".docx"}


def salvar_pacote_docs(conn, gerente_id, cliente_nome, arquivos, produto="", observacao="", cliente_id=None,
                       origem="consultor", cliente_telefone=None):
    """`arquivos` = lista de (nome_original, bytes, mime). Grava em documentos/<pacote>/ e devolve o id."""
    cur = conn.execute(
        "INSERT INTO pacotes_docs (gerente_id, cliente_id, cliente_nome, produto, observacao, criado_em, origem, cliente_telefone) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (gerente_id, cliente_id, cliente_nome.strip(), produto.strip(), observacao.strip(), agora(), origem, cliente_telefone),
    )
    pacote_id = cur.lastrowid
    import armazenamento  # aqui dentro: armazenamento importa db
    for i, (nome, dados, mime) in enumerate(arquivos, 1):
        # Nome guardado é nosso (nunca o do upload); o original só aparece na tela.
        ext = Path(nome).suffix.lower()
        arquivo = f"{i:02d}{ext}"
        armazenamento.salvar("documentos", f"{pacote_id}/{arquivo}", dados, mime)
        conn.execute(
            "INSERT INTO documentos (pacote_id, nome, caminho, mime, tamanho) VALUES (?, ?, ?, ?, ?)",
            (pacote_id, Path(nome).name[:120], f"{pacote_id}/{arquivo}", mime, len(dados)),
        )
    if cliente_id:
        registrar(conn, cliente_id, "sistema", "interna", f"{len(arquivos)} documento(s) enviados ao gerente pelo portal.")
    return pacote_id


def token_docs_do_gerente(conn, gerente_id):
    """Token do link que o consultor manda para o cliente; criado na primeira vez que é pedido."""
    import secrets
    g = conn.execute("SELECT token_docs FROM gerentes WHERE id = ?", (gerente_id,)).fetchone()
    if g and g["token_docs"]:
        return g["token_docs"]
    tok = secrets.token_urlsafe(18)
    conn.execute("UPDATE gerentes SET token_docs = ? WHERE id = ?", (tok, gerente_id))
    return tok


def cliente_enviou_documentos(conn, gerente_id, nome, telefone, qtd_arquivos):
    """Cliente mandou documentos pelo link: acha a ficha pelo telefone (ou cria, ligada ao gerente)
    e coloca na etapa "Documentação recebida". Devolve o id do cliente."""
    c = conn.execute("SELECT id, etapa FROM clientes WHERE telefone = ?", (telefone,)).fetchone()
    if not c:
        lote_id, _, _ = importar_lote(
            conn, [{"nome": nome, "telefone": telefone, "situacao_bb": "Enviou a documentação pelo link"}],
            "Documentos enviados pelo cliente", 0, gerente_id=gerente_id)
        # Não é lista de indicação: não deve aparecer no aviso "Novas listas pelo celular".
        conn.execute("UPDATE lotes SET visto = 1 WHERE id = ?", (lote_id,))
        c = conn.execute("SELECT id, etapa FROM clientes WHERE telefone = ?", (telefone,)).fetchone()
    if c["etapa"] != "vendido":
        conn.execute("UPDATE clientes SET etapa = 'documentacao', atualizado_em = ? WHERE id = ?", (agora(), c["id"]))
    registrar(conn, c["id"], "documentos", "entrada",
              f"Enviou {qtd_arquivos} documento(s) pelo link: ficha em \"Documentação recebida\".")
    return c["id"]
