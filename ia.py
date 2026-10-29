"""Chamadas ao Gemini: leitura das fotos das listas do BB e redação das mensagens."""
import base64
import json
import os
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

_FALLBACK = dotenv_values(Path.home() / "claude-whatsapp" / ".env")
API_KEY = os.getenv("GEMINI_API_KEY") or _FALLBACK.get("GEMINI_API_KEY", "")
MODELO = os.getenv("GEMINI_MODELO") or "gemini-flash-latest"
MODELO_RESERVA = os.getenv("GEMINI_MODELO_RESERVA") or _FALLBACK.get("MODELO_GEMINI_RESERVA") or "gemini-flash-lite-latest"
MODELO_LEITURA = os.getenv("GEMINI_MODELO_LEITURA") or "gemini-flash-lite-latest"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"


class ErroIA(RuntimeError):
    pass


def _chamar(partes, max_tokens=8192, temperatura=0.2, modelos=None):
    if not API_KEY:
        raise ErroIA("GEMINI_API_KEY não configurada (nem no .env do CRM BB, nem em ~/claude-whatsapp/.env).")
    corpo = {
        "contents": [{"role": "user", "parts": partes}],
        # O flash é um modelo "thinking": precisa de folga no maxOutputTokens,
        # senão o JSON sai cortado (aconteceu no CRM Mobie com 300 tokens).
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": max_tokens,
            "temperature": temperatura,
        },
    }
    # 503/429 de "alta demanda" são frequentes e passageiros: tenta de novo e
    # cai para o modelo reserva antes de desistir.
    for tentativa, modelo in enumerate(modelos or [MODELO, MODELO, MODELO_RESERVA, MODELO_RESERVA]):
        if tentativa:
            time.sleep(2 * tentativa)
        try:
            r = requests.post(URL.format(m=modelo), params={"key": API_KEY}, json=corpo, timeout=120)
        except requests.RequestException as e:
            erro = str(e)
            continue
        if r.status_code == 200:
            break
        erro = f"Gemini respondeu {r.status_code}: {r.text[:200]}"
        if r.status_code not in (429, 500, 502, 503, 504):
            raise ErroIA(erro)
    else:
        raise ErroIA(erro)
    try:
        texto = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(texto, strict=False)  # aceita quebra de linha crua dentro do texto
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise ErroIA(f"Resposta da IA em formato inesperado: {e}")


PROMPT_LEITURA = """Estas imagens são fotos/prints de listas de clientes enviadas pelo Banco do
Brasil a um representante autorizado do Consórcio BB. Extraia TODOS os clientes
que aparecem, um por linha da lista, sem pular nenhum e sem inventar nada.

Devolva JSON: {"clientes": [ {...}, ... ], "observacao_leitura": "..."}
Campos de cada cliente (use "" quando não existir na imagem):
- nome: nome completo como aparece
- telefone: com DDD, só como aparece (não complete dígitos faltando)
- email
- cidade
- produto: um de "auto", "imóvel", "moto", "pesados", "serviços" ou "" se não der para saber
- valor_credito: número em reais, sem símbolo (ex.: 85000), ou ""
- situacao_bb: a situação/status do cliente como está escrito (ex.: "simulou no app",
  "proposta não finalizada", "visitou a página"), ou ""
- observacoes: qualquer anotação da linha que não caiba acima
- extras: objeto com as demais colunas da lista (nome da coluna -> valor), EXCETO
  CPF, RG, conta, agência e senha — esses dados NUNCA devem ser copiados.
- confianca: "alta", "media" ou "baixa" para a leitura daquela linha
Se a mesma pessoa aparecer em duas imagens, liste só uma vez.
Em observacao_leitura diga, em uma frase, se alguma parte estava ilegível ou cortada."""


def ler_listas(imagens):
    """`imagens` = lista de (bytes, mime). Devolve (clientes, observacao)."""
    partes = [{"text": PROMPT_LEITURA}]
    for dados, mime in imagens:
        partes.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(dados).decode()}})
    # Leitura de lista usa o modelo leve primeiro: lê uma página em ~5 s, enquanto o flash
    # com raciocínio passava de 50 s e vivia em "alta demanda" (teste de 29/10/2026).
    res = _chamar(partes, max_tokens=16384, temperatura=0,
                  modelos=[MODELO_LEITURA, MODELO_LEITURA, MODELO, MODELO_LEITURA])
    return res.get("clientes", []), res.get("observacao_leitura", "")


PROMPT_MENSAGEM = """Você escreve mensagens de {canal} para {consultor}, da {empresa}, que é
REPRESENTANTE AUTORIZADO do Consórcio BB (Banco do Brasil). O cliente abaixo veio de
uma lista que o próprio Banco do Brasil repassou ao representante — ele demonstrou
interesse em consórcio pelos canais do banco. O assunto é EXCLUSIVAMENTE Consórcio BB.

DADOS DO CLIENTE
{dados}

HISTÓRICO RECENTE (mais recente por último)
{historico}

ESTE É O CONTATO Nº {tentativa} DA CADÊNCIA. Objetivo desta etapa: {objetivo}

REGRAS OBRIGATÓRIAS (compliance):
- No 1º contato, apresente-se: "{consultor}, da {empresa}, representante autorizado do
  Consórcio BB" e diga de onde veio o contato (interesse demonstrado no Banco do Brasil).
  Nunca diga que é funcionário ou gerente do banco.
- Nunca prometa contemplação, prazo de contemplação, valor de lance ou rentabilidade.
- Não diga "sem juros" sem completar que existe taxa de administração.
- Não invente valores, parcelas, prazos, grupos ou promoções que não estão nos dados.
- Nunca peça CPF, senha, dados bancários ou código por mensagem.
- Termine com uma pergunta simples que convide resposta.
- Inclua no final, em linha separada, exatamente: "{optout}"
- Tom: pessoal, educado, consultivo; nada de caixa alta, emoji em excesso ou urgência falsa.
{regras_canal}

Devolva JSON: {{"assunto": "...", "texto": "..."}} (assunto vazio se for WhatsApp)."""

OBJETIVOS = [
    "apresentação e confirmar se o interesse em consórcio continua",
    "retomar de forma leve e oferecer tirar dúvidas sobre como funciona o consórcio",
    "trazer um benefício concreto do consórcio ligado ao produto de interesse",
    "propor uma simulação personalizada, sem compromisso",
    "contar como funciona lance e sorteio, de forma educativa",
    "perguntar se o momento mudou ou se prefere ser contatado mais para frente",
    "mensagem curta de lembrete, deixando a porta aberta",
]


def gerar_mensagem(cliente, historico, tentativa, cfg, canal="whatsapp"):
    dados = {k: cliente[k] for k in ("nome", "cidade", "produto", "valor_credito", "situacao_bb", "observacoes") if cliente[k]}
    if cliente["extras"]:
        dados["outras_informacoes"] = json.loads(cliente["extras"])
    hist = "\n".join(f"[{h['criado_em']}] {h['canal']}/{h['direcao']}: {h['texto'][:300]}" for h in historico) or "(nenhum contato ainda)"
    optout = ("Se não quiser mais receber mensagens sobre isso, é só responder SAIR."
              if canal == "whatsapp" else
              "Se não quiser mais receber estes e-mails, basta responder com SAIR.")
    regras = ("- WhatsApp: no máximo 4 frases curtas, pode ter 1 emoji discreto."
              if canal == "whatsapp" else
              "- E-mail: assunto até 60 caracteres, corpo de 3 a 6 frases, assinatura com nome e empresa.")
    prompt = PROMPT_MENSAGEM.format(
        canal="WhatsApp" if canal == "whatsapp" else "e-mail",
        consultor=cfg["nome_consultor"], empresa=cfg["nome_empresa"],
        dados=json.dumps(dados, ensure_ascii=False, indent=1),
        historico=hist, tentativa=tentativa,
        objetivo=OBJETIVOS[min(tentativa - 1, len(OBJETIVOS) - 1)],
        optout=optout, regras_canal=regras,
    )
    res = _chamar([{"text": prompt}], max_tokens=4096, temperatura=0.7)
    texto = (res.get("texto") or "").strip()
    if optout not in texto:
        texto = f"{texto}\n\n{optout}"
    return (res.get("assunto") or "").strip(), texto
