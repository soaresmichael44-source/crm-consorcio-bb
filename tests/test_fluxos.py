"""Testes dos fluxos críticos do CRM BB, num banco SQLite temporário (nunca toca no banco real).

Rodar:  python3 -m unittest discover -s tests -v
"""
import os
import secrets
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_PASTA = tempfile.mkdtemp(prefix="crm-bb-teste-")
# Valores definidos antes de importar o app: o .env não sobrescreve variáveis que já existem.
os.environ.update({
    "CRM_BB_DB": str(Path(_PASTA) / "teste.db"),
    "DATABASE_URL": "",          # força SQLite
    "CRM_BB_DEMO": "0",
    "FORCAR_LOGIN": "1",         # como na nuvem: ninguém entra sem login
    "SECRET_KEY": "teste",
    "SUPABASE_URL": "",          # arquivos na pasta local
    "GEMINI_API_KEY": "",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as crm  # noqa: E402
import db  # noqa: E402
import gerente as portal  # noqa: E402
import sdr  # noqa: E402

_seq = iter(range(10_000_000, 99_999_999, 7919))


def telefone():
    return f"55319{next(_seq)}"


def novo_rep(conn, nome="Rep"):
    return conn.execute("INSERT INTO representantes (nome, criado_em) VALUES (?, ?)",
                        (f"{nome} {secrets.token_hex(2)}", db.agora())).lastrowid


def novo_gerente(conn, rep_id=None, nome="Gerente"):
    return conn.execute("INSERT INTO gerentes (nome, agencia, token, criado_em, representante_id) VALUES (?, ?, ?, ?, ?)",
                        (f"{nome} {secrets.token_hex(2)}", "0001", secrets.token_urlsafe(16), db.agora(), rep_id)).lastrowid


def lista(conn, gerente_id, *clientes):
    """Importa uma lista como se o gerente tivesse mandado. Devolve (lote_id, novos, atualizados)."""
    return db.importar_lote(conn, list(clientes), "teste", 1, gerente_id=gerente_id)


def cliente_por_tel(conn, tel):
    return conn.execute("SELECT * FROM clientes WHERE telefone = ?", (tel,)).fetchone()


class Base(unittest.TestCase):
    def setUp(self):
        self.c = crm.app.test_client()

    def como_admin(self):
        with self.c.session_transaction() as s:
            s.clear()
            s["logado"] = True

    def como_rep(self, rep_id):
        with self.c.session_transaction() as s:
            s.clear()
            s["rep_id"] = rep_id


class Importacao(Base):
    def test_lista_do_gerente_entra_na_fila_ligada_ao_gerente(self):
        with db.conectar() as conn:
            gid = novo_gerente(conn, novo_rep(conn))
            tel = telefone()
            lote, novos, atualizados = lista(conn, gid, {"nome": "ana souza", "telefone": tel, "valor_credito": "R$ 80.000,00"})
            c = cliente_por_tel(conn, tel)
            self.assertEqual((novos, atualizados), (1, 0))
            self.assertEqual(c["lote_id"], lote)
            self.assertEqual(c["nome"], "Ana Souza")
            self.assertEqual(c["valor_credito"], 80000)
            self.assertEqual(c["etapa"], "novo")
            self.assertIsNotNone(c["proximo_contato"])  # já entra vencido para o follow-up
            self.assertIn(c["id"], [x["id"] for x in sdr.fila(conn)])
            self.assertEqual(conn.execute("SELECT gerente_id, visto FROM lotes WHERE id = ?", (lote,)).fetchone()["visto"], 0)

    def test_campos_ausentes_nao_sao_inventados(self):
        with db.conectar() as conn:
            gid = novo_gerente(conn)
            lista(conn, gid, {"nome": "", "telefone": ""})
            c = conn.execute("SELECT * FROM clientes ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(c["nome"], "Sem nome")
            self.assertFalse(c["telefone"])  # vazio, nunca um número inventado
            self.assertIsNone(c["valor_credito"])

    def test_telefone_em_formatos_diferentes_e_o_mesmo_cliente(self):
        with db.conectar() as conn:
            gid = novo_gerente(conn)
            tel = telefone()
            lista(conn, gid, {"nome": "Bruno", "telefone": tel})
            _, novos, atualizados = lista(conn, gid, {"nome": "Bruno", "telefone": f"({tel[2:4]}) {tel[4:9]}-{tel[9:]}"})
            self.assertEqual((novos, atualizados), (0, 1))


class Duplicidade(Base):
    def test_mesmo_gerente_reenviando_nao_gera_aviso(self):
        with db.conectar() as conn:
            gid = novo_gerente(conn, novo_rep(conn))
            tel = telefone()
            lista(conn, gid, {"nome": "Carla", "telefone": tel})
            lista(conn, gid, {"nome": "Carla", "telefone": tel, "situacao_bb": "fez simulação"})
            c = cliente_por_tel(conn, tel)
            self.assertIsNone(c["alerta_duplicidade"])
            self.assertEqual(c["situacao_bb"], "fez simulação")  # dado novo da lista é aproveitado

    def test_cliente_de_outro_representante_continua_com_quem_trouxe_primeiro(self):
        with db.conectar() as conn:
            rep_a, rep_b = novo_rep(conn, "Ana"), novo_rep(conn, "Beto")
            ga, gb = novo_gerente(conn, rep_a), novo_gerente(conn, rep_b)
            tel = telefone()
            lote_a, _, _ = lista(conn, ga, {"nome": "Daniel", "telefone": tel})
            lista(conn, gb, {"nome": "Daniel", "telefone": tel})
            c = cliente_por_tel(conn, tel)
            self.assertEqual(c["lote_id"], lote_a)
            self.assertIn("Continua com", c["alerta_duplicidade"])
            self.assertTrue(conn.execute("SELECT 1 FROM interacoes WHERE cliente_id = ? AND texto LIKE 'Possível duplicidade%'",
                                         (c["id"],)).fetchone())

    def test_aviso_aparece_no_painel_e_some_depois_de_conferido(self):
        with db.conectar() as conn:
            tel = telefone()
            lista(conn, novo_gerente(conn, novo_rep(conn)), {"nome": "Elaine Repetida", "telefone": tel})
            lista(conn, novo_gerente(conn, novo_rep(conn)), {"nome": "Elaine Repetida", "telefone": tel})
            cid = cliente_por_tel(conn, tel)["id"]
        self.como_admin()
        self.assertIn("Elaine Repetida", self.c.get("/").get_data(as_text=True))
        self.assertEqual(self.c.post(f"/api/cliente/{cid}/duplicidade-vista", json={}).status_code, 200)
        with db.conectar() as conn:
            self.assertIsNone(conn.execute("SELECT alerta_duplicidade FROM clientes WHERE id = ?", (cid,)).fetchone()[0])

    def test_representante_nao_limpa_aviso_de_duplicidade(self):
        with db.conectar() as conn:
            rep = novo_rep(conn)
        self.como_rep(rep)
        self.assertEqual(self.c.post("/api/cliente/1/duplicidade-vista", json={}).status_code, 403)


class Permissoes(Base):
    def setUp(self):
        super().setUp()
        with db.conectar() as conn:
            self.rep_a, self.rep_b = novo_rep(conn, "Ana"), novo_rep(conn, "Beto")
            self.ger_a, self.ger_b = novo_gerente(conn, self.rep_a), novo_gerente(conn, self.rep_b)
            lista(conn, self.ger_a, {"nome": "Cliente Da Ana", "telefone": telefone()})
            lista(conn, self.ger_b, {"nome": "Cliente Do Beto", "telefone": telefone()})
            self.cli_a = conn.execute("SELECT id FROM clientes WHERE nome = 'Cliente Da Ana' ORDER BY id DESC").fetchone()["id"]
            self.cli_b = conn.execute("SELECT id FROM clientes WHERE nome = 'Cliente Do Beto' ORDER BY id DESC").fetchone()["id"]

    def test_sem_login_nao_entra(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.post("/api/ciclo", json={}).status_code, 401)

    def test_representante_ve_so_a_carteira_dele(self):
        self.como_rep(self.rep_a)
        self.assertEqual(self.c.get(f"/cliente/{self.cli_a}").status_code, 200)
        self.assertEqual(self.c.get(f"/cliente/{self.cli_b}").status_code, 404)
        fila = self.c.get("/fila").get_data(as_text=True)
        self.assertIn("Cliente Da Ana", fila)
        self.assertNotIn("Cliente Do Beto", fila)
        self.assertNotIn("Cliente Do Beto", self.c.get("/negociacoes").get_data(as_text=True))

    def test_representante_nao_entra_nas_telas_da_operacao(self):
        self.como_rep(self.rep_a)
        for url in ("/rede", "/importar", "/config", f"/rede/representante/{self.rep_b}"):
            self.assertEqual(self.c.get(url).status_code, 302, url)
        self.assertEqual(self.c.post("/api/ciclo", json={}).status_code, 403)

    def test_gerente_cadastrado_pelo_representante_fica_com_ele(self):
        self.como_rep(self.rep_a)
        nome = f"Gerente Novo {secrets.token_hex(3)}"
        self.c.post("/gerentes/novo", data={"nome": nome, "representante_id": str(self.rep_b)})
        with db.conectar() as conn:
            self.assertEqual(conn.execute("SELECT representante_id FROM gerentes WHERE nome = ?", (nome,)).fetchone()[0], self.rep_a)

    def test_representante_nao_desativa_gerente_de_outro(self):
        self.como_rep(self.rep_a)
        self.assertEqual(self.c.post(f"/gerentes/{self.ger_b}/ativo").status_code, 404)
        with db.conectar() as conn:
            self.assertEqual(conn.execute("SELECT ativo FROM gerentes WHERE id = ?", (self.ger_b,)).fetchone()[0], 1)

    def test_ver_como_e_voltar(self):
        self.como_admin()
        self.c.post(f"/rede/representante/{self.rep_a}/ver-como")
        self.assertEqual(self.c.get(f"/cliente/{self.cli_b}").status_code, 404)
        self.c.get("/voltar-operacao")
        self.assertEqual(self.c.get(f"/cliente/{self.cli_b}").status_code, 200)

    def test_portal_do_gerente_abre_so_com_o_link_certo_e_sem_mensagens(self):
        p = portal.app.test_client()
        with db.conectar() as conn:
            token = conn.execute("SELECT token FROM gerentes WHERE id = ?", (self.ger_a,)).fetchone()["token"]
            db.registrar(conn, self.cli_a, "whatsapp", "saida", "TEXTO-SECRETO-DA-CONVERSA")
        self.assertEqual(p.get("/g/token-que-nao-existe").status_code, 404)
        r = p.get(f"/g/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("TEXTO-SECRETO-DA-CONVERSA", r.get_data(as_text=True))


class RepasseEFollowUp(Base):
    def test_resposta_passa_para_o_representante_e_sai_da_fila_central(self):
        with db.conectar() as conn:
            rep = novo_rep(conn)
            tel = telefone()
            lista(conn, novo_gerente(conn, rep), {"nome": "Fábio", "telefone": tel})
            cid = cliente_por_tel(conn, tel)["id"]
            self.assertIn(cid, [x["id"] for x in sdr.fila(conn)])
            sdr.registrar_resposta(conn, tel, "Tenho interesse sim")
            c = cliente_por_tel(conn, tel)
            self.assertEqual(c["etapa"], "respondeu")
            self.assertIsNotNone(c["repasse_em"])
            self.assertEqual(c["repasse_visto"], 0)
            self.assertNotIn(cid, [x["id"] for x in sdr.fila(conn)])
        self.como_rep(rep)
        self.assertIn("Fábio", self.c.get("/atender").get_data(as_text=True))
        self.c.get(f"/cliente/{cid}")  # abrir a ficha tira o selo NOVO
        with db.conectar() as conn:
            self.assertEqual(cliente_por_tel(conn, tel)["repasse_visto"], 1)

    def test_cliente_da_operacao_direta_nao_e_repassado(self):
        with db.conectar() as conn:
            tel = telefone()
            lista(conn, novo_gerente(conn, None), {"nome": "Gisele", "telefone": tel})
            sdr.registrar_resposta(conn, tel, "quero saber mais")
            self.assertIsNone(cliente_por_tel(conn, tel)["repasse_em"])

    def test_pedido_para_sair_para_a_cadencia(self):
        with db.conectar() as conn:
            tel = telefone()
            lista(conn, novo_gerente(conn, novo_rep(conn)), {"nome": "Hugo", "telefone": tel})
            sdr.registrar_resposta(conn, tel, "Parar")
            c = cliente_por_tel(conn, tel)
            self.assertEqual(c["etapa"], "optout")
            self.assertIsNone(c["proximo_contato"])
            self.assertIsNone(c["repasse_em"])
            self.assertNotIn(c["id"], [x["id"] for x in sdr.fila(conn)])

    def test_cadencia_segue_os_dias_configurados(self):
        cfg = {"cadencia_dias": "0,1,3,7", "intervalo_final": "15"}
        hoje = datetime.now().date()
        dias = [(datetime.strptime(sdr.proximo_contato(cfg, n), "%Y-%m-%d %H:%M:%S").date() - hoje).days for n in (1, 2, 3, 4, 9)]
        self.assertEqual(dias, [1, 2, 4, 15, 15])

    def test_mensagem_sai_no_nome_de_quem_faz_o_follow_up(self):
        with db.conectar() as conn:
            rep = novo_rep(conn, "Carina")
            nome_rep = conn.execute("SELECT nome FROM representantes WHERE id = ?", (rep,)).fetchone()["nome"]
            tel = telefone()
            lista(conn, novo_gerente(conn, rep), {"nome": "Ivo", "telefone": tel})
            cid = cliente_por_tel(conn, tel)["id"]
            cfg = db.config(conn)
            self.assertEqual(db.cfg_do_cliente(conn, cid, cfg)["nome_consultor"], cfg["nome_consultor"])  # central
            conn.execute("UPDATE representantes SET faz_followup = 1 WHERE id = ?", (rep,))
            self.assertEqual(db.cfg_do_cliente(conn, cid, cfg)["nome_consultor"], nome_rep)

    def test_representante_que_faz_follow_up_trabalha_a_fila_so_dele(self):
        with db.conectar() as conn:
            rep, outro = novo_rep(conn, "Carina"), novo_rep(conn, "Outro")
            conn.execute("UPDATE representantes SET faz_followup = 1 WHERE id = ?", (rep,))
            lista(conn, novo_gerente(conn, rep), {"nome": "Joana Da Carina", "telefone": telefone()})
            lista(conn, novo_gerente(conn, outro), {"nome": "Kleber De Outro", "telefone": telefone()})
            cid_outro = conn.execute("SELECT id FROM clientes WHERE nome = 'Kleber De Outro'").fetchone()["id"]
        self.como_rep(rep)
        html = self.c.get("/fila").get_data(as_text=True)
        self.assertIn("Joana Da Carina", html)
        self.assertNotIn("Kleber De Outro", html)
        self.assertIn("Abrir no WhatsApp", html)  # tela de trabalho, não só acompanhamento
        self.assertEqual(self.c.post(f"/api/cliente/{cid_outro}/enviado", json={"texto": "oi"}).status_code, 404)
        self.assertEqual(self.c.post(f"/api/cliente/{cid_outro}/enviar-auto", json={"texto": "oi"}).status_code, 403)

    def test_envio_automatico_vem_desligado(self):
        with db.conectar() as conn:
            self.assertEqual(db.config(conn)["envio_automatico"], "0")


if __name__ == "__main__":
    unittest.main()
