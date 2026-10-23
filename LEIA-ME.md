# CRM Consórcio BB

Operação **exclusiva** dos clientes que o Banco do Brasil repassa ao representante do Consórcio BB.
Fica separada do CRM Mobie: outro banco de dados, outra porta, outras mensagens.

- Endereço: http://127.0.0.1:8095 (sobe sozinho com o Mac via `com.michaelsoares.crm-bb`)
- Logs: `logs/crm-bb.log`
- Reiniciar: `launchctl kickstart -k gui/$(id -u)/com.michaelsoares.crm-bb`

## Como usar no dia a dia

1. **Importar listas**: arraste, cole (⌘V) ou escolha as fotos/prints das listas. A IA lê cada imagem,
   separa cliente por cliente, marca telefones ilegíveis em vermelho e avisa quem já está cadastrado.
   Você confere e salva.
2. **Fila de follow-up**: a IA já deixa a mensagem de cada cliente escrita. No modo manual, o botão verde
   abre o WhatsApp com o texto pronto; você só aperta enviar. O sistema agenda o próximo contato sozinho.
3. **Cliente respondeu**: cole a resposta na ficha dele em “É resposta do cliente”. Se ele escrever SAIR,
   ele sai da cadência para sempre.
4. **Cadência**: 0, 1, 3, 7, 14, 21 e 30 dias e depois a cada 15 dias, até virar Vendido, Perdido ou SAIR.
   Muda em Configurações.

## Gerentes enviando pelo celular

- Em **Configurações → Gerentes do banco**, crie o acesso do gerente. Sai um link próprio, com QR code
  e botão para mandar pelo WhatsApp.
- O gerente abre o link no celular, toca em **Tirar foto da lista** (pode juntar até 8 fotos) e em
  **Enviar para o CRM**. A IA lê e os contatos entram direto, sem revisão, já na fila de follow-up.
  No Painel aparece o aviso "Novas listas pelo celular".
- Esse app é outro processo (`gerente.py`, porta 8096, serviço `com.michaelsoares.crm-bb-gerente`,
  log em `logs/gerente.log`). Ele só tem a tela de envio: pelo link não dá para ver clientes, fila nem
  configurações. Limite de 20 envios por hora por gerente.
- **Desativar** corta o link na hora. Reativar gera um link novo.
- Para funcionar fora deste Mac, o app 8096 (e só ele) precisa estar publicado na internet. O
  endereço público vai em `URL_PUBLICA_GERENTE` no `.env`. O Mac precisa estar ligado para receber.

## n8n (http://localhost:5678)

- **CRM BB — Cadência diária (24h)**: todo dia às 9h, chama `/api/n8n/ciclo` uma vez e prepara
  (ou envia, no modo robô) todos os follow-ups vencidos até o limite diário. Domingo não envia.
- **CRM BB — Receber resposta do cliente**: webhook `POST /webhook/crm-bb-resposta` com
  `{"telefone": "...", "texto": "..."}`; registra a resposta e trata SAIR.
- Arquivos dos fluxos em `n8n/`. O token fica no `.env` (`CRM_BB_API_TOKEN`).

## Abordagem: o que deixa a operação segura

Você é representante autorizado, então o contato é legítimo. Os riscos estão em **como** ele é feito.

**Identificação.** Toda primeira mensagem diz quem você é: “Michael, da Mobie, representante autorizado
do Consórcio BB”, e de onde veio o contato. Nunca se apresente como funcionário ou gerente do banco.
A IA já faz isso por regra.

**Promessas proibidas.** Nada de prometer contemplação, prazo para ser contemplado, lance garantido ou
rentabilidade. “Sem juros” só junto com “tem taxa de administração”. A IA tem essas travas.

**LGPD.** O banco repassou os dados para essa finalidade, então use-os só para falar de Consórcio BB.
O sistema descarta CPF, RG, agência e conta na leitura, não pede dados sensíveis por mensagem e
respeita o SAIR na hora. Não exporte nem reaproveite essas listas para outros produtos da Mobie.

**WhatsApp.** Listas frias disparadas em volume são a principal causa de bloqueio de número. Por isso:
- comece no **modo manual** (o clique é seu, o ritmo é humano);
- o limite padrão é 40 mensagens por dia, e nada sai à noite nem aos domingos;
- o ideal é ter um **número só para a operação BB**. Se usar o robô atual (`~/claude-whatsapp`),
  quem responder vai cair no atendente da Mobie, que não fala de BB;
- para escala de verdade, use a API oficial do WhatsApp (Meta) com modelos de mensagem aprovados.

**Confirme com o banco.** Antes de ligar o envio automático, pergunte ao seu gestor do Consórcio BB
qual é a regra de contato ativo e de uso da marca para representantes (texto aprovado, se pode
mandar WhatsApp e e-mail, se pode usar o logo). O que o contrato de representação disser vale mais
que este guia.

## E-mail (opcional)

Preencha `BREVO_API_KEY` e `BREVO_REMETENTE_EMAIL` no `.env` (conta grátis em brevo.com, remetente
verificado), reinicie o serviço e marque “Mandar e-mail automático” em Configurações.

## Nuvem (Supabase + Render)

- O mesmo código roda no Mac (SQLite + pastas locais) e na nuvem: com `DATABASE_URL` usa o Postgres
  do Supabase (São Paulo); com `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` guarda documentos e fotos no
  bucket **privado** `crm-bb` do Supabase Storage.
- Na nuvem roda um serviço só (`wsgi.py`, `gunicorn wsgi:app`): `/g/...` é o Portal do Gerente e o
  resto é o CRM, que com `FORCAR_LOGIN=1` sempre pede a senha `CRM_BB_SENHA`.
- `render.yaml` descreve o serviço; segredos ficam só no painel do Render.
- Credenciais da nuvem: preencher `.env.nuvem` (nunca vai para o GitHub, veja `.gitignore`).
- Levar os dados do Mac: `/usr/bin/python3 migrar_para_nuvem.py` (só lê o Mac; recusa se a nuvem
  já tiver clientes).
