"""
webhook_agent.py — Camada de I/O do agente consultivo (Flask + Twilio).

ISOLADO da produção (webhook.py/main.py intactos). Rota nova /webhook/agent.
Estado das conversas persistido via agent_store (Postgres, ou memória se não
houver DATABASE_URL). Reaproveita só leitura do main.py.

Fluxo assíncrono (o agente com Opus pode passar do timeout de ~15s do Twilio):
  1. Recebe a mensagem do cliente e responde 200 vazio na hora (ack).
  2. Em thread de fundo: roda o agente, envia a resposta via API REST do Twilio
     (dentro da janela de 24h, pois o cliente acabou de escrever), registra o
     lead na planilha e, se o agente escalar, notifica a equipe por email.
"""

import os
import re
import json
import time
import secrets
import threading
from datetime import datetime, timezone, timedelta

from flask import Flask, request, redirect
from dotenv import load_dotenv
import requests
from twilio.rest import Client

from main import (
    conectar_google_sheets,
    formatar_telefone,
    esta_no_horario_comercial,
    descobrir_pais_por_telefone,
    cliente_ja_tem_reserva,
    enviar_mensagem_inicial_com_opcoes,  # EUA: reaproveita o email+SMS da produção
)
import main_agent
import agent_store

load_dotenv()

agent_store.init_db()  # cria a tabela se houver DATABASE_URL (senão, memória)

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")  # "whatsapp:+1..."
# Templates de entrada EXCLUSIVOS do agente (SIDs separados da produção), por idioma
AGENT_TEMPLATE_SID_BR = os.getenv("AGENT_TEMPLATE_SID_BR") or os.getenv("AGENT_TEMPLATE_SID")
AGENT_TEMPLATE_SID_ES = os.getenv("AGENT_TEMPLATE_SID_ES")


def _template_sid(language):
    return AGENT_TEMPLATE_SID_ES if (language or "").lower().startswith("es") else AGENT_TEMPLATE_SID_BR

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Estado das conversas: persistido via agent_store (Postgres, ou memória se sem banco)

# Países hispano-falantes (mesma lógica de idioma da produção)
PAISES_ES = {
    "argentina", "colombia", "colômbia", "mexico", "méxico", "chile", "peru",
    "uruguai", "uruguay", "equador", "ecuador", "paraguai", "paraguay",
    "guatemala", "bolivia", "bolívia", "venezuela",
}

# EUA: mesmo tratamento da produção (email + SMS em inglês, não WhatsApp)
PAISES_USA = {"usa", "united states", "estados unidos", "eua"}

# Token opcional de proteção do dashboard (recomendado configurar no Railway)
DASHBOARD_TOKEN = os.getenv("AGENT_DASHBOARD_TOKEN")

# Link rastreável (jornada WhatsApp → site). O agente manda um link que passa pelo
# nosso redirect /r/<code> (registra o clique + IP) e encaminha ao site com ?ref=<code>.
# TRACK_BASE_URL: base pública do serviço (troque por go.allycar.com se criar o CNAME).
TRACK_BASE_URL = os.getenv("TRACK_BASE_URL", "https://allycar-agent-production.up.railway.app").rstrip("/")
SITE_URL = os.getenv("SITE_URL", "https://allycar.com").rstrip("/")
_ALLYCAR_LINK_RE = re.compile(r'(?:https?://)?(?:www\.)?allycar\.com(?:/[^\s]*)?', re.IGNORECASE)

# Rastreio do link no TEMPLATE do disparo inicial. Fica DESLIGADO até o template
# na Twilio ter a variável {{2}} (o código) aprovada e o go.allycar.com no ar.
# Quando ligado, o disparo manda o código como variável {{2}} (o template deve
# conter "https://go.allycar.com/r/{{2}}"). Ative com TRACK_TEMPLATE_LINK=1.
TRACK_TEMPLATE_LINK = (os.getenv("TRACK_TEMPLATE_LINK", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _nova_conversa_inicial(name, phone, language):
    """Estado inicial da conversa, já com um código de rastreio embutido."""
    return {
        "name": name,
        "phone": (phone or "").replace("whatsapp:", ""),
        "language": language,
        "history": [],
        "ref_code": secrets.token_hex(4),
    }


def _vars_template(name, code):
    """Variáveis do template: {{1}}=nome sempre; {{2}}=código do link só se ativado."""
    if TRACK_TEMPLATE_LINK and code:
        return json.dumps({"1": name, "2": code})
    return json.dumps({"1": name})


def _enviar_template(to, sid, name, code):
    """Envia o template de entrada. Se o rastreio estiver ligado mas o template
    ainda não tiver a variável {{2}} (ex.: aprovação pendente), o envio com {{2}}
    falha — então tentamos de novo SÓ com o nome. Assim o disparo nunca quebra por
    causa da ordem (flag ligada antes do template novo)."""
    try:
        return twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER, to=to, content_sid=sid,
            content_variables=_vars_template(name, code),
        )
    except Exception as e:
        if TRACK_TEMPLATE_LINK and code:
            print(f"⚠️ template com {{2}} falhou ({e}); reenviando só com o nome.")
            return twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER, to=to, content_sid=sid,
                content_variables=json.dumps({"1": name}),
            )
        raise


def _garantir_ref(conversa):
    """Garante um código de rastreio único por conversa (persistido no estado)."""
    code = conversa.get("ref_code")
    if not code:
        code = secrets.token_hex(4)  # 8 caracteres hex, curto e URL-safe
        conversa["ref_code"] = code
    return code


def _injetar_link_rastreavel(texto, conversa):
    """Troca menções a allycar.com pelo link rastreável (redirect que captura o clique)."""
    if not texto or "allycar.com" not in texto.lower():
        return texto
    code = _garantir_ref(conversa)
    return _ALLYCAR_LINK_RE.sub(f"{TRACK_BASE_URL}/r/{code}", texto)


def _dash_ok():
    if not DASHBOARD_TOKEN:
        return True  # sem token configurado → liberado (configure para proteger)
    return request.args.get("token") == DASHBOARD_TOKEN


def _idioma_por_pais(pais):
    return "es" if (pais or "").strip().lower() in PAISES_ES else "pt"


def _bloco_tipo_texto(b):
    """Extrai (tipo, texto) de um bloco, seja objeto do SDK ou dict persistido."""
    if isinstance(b, dict):
        return b.get("type"), b.get("text", "")
    return getattr(b, "type", None), getattr(b, "text", "")


def _transcricao(conversa):
    """Monta o texto legível da conversa (só falas do cliente e do agente)."""
    linhas = []
    for m in conversa.get("history", []):
        role = m.get("role")
        content = m.get("content")
        if role == "user" and isinstance(content, str):
            linhas.append(f"Cliente: {content}")
        elif role == "assistant" and isinstance(content, list):
            txt = "".join(
                t for (typ, t) in (_bloco_tipo_texto(b) for b in content) if typ == "text"
            ).strip()
            if txt:
                linhas.append(f"Agente: {txt}")
        elif role == "assistant" and isinstance(content, str) and content.strip():
            linhas.append(f"Agente: {content.strip()}")
    return "\n".join(linhas)


def _resumo_lead(st):
    """Extrai do histórico as datas, o carro e os preços apresentados ao cliente."""
    datas = {}
    modelo_aceito = st.get("modelo_interesse")
    veiculos = None
    for m in st.get("history", []):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b.get("name") == "consultar_disponibilidade_precos":
                    if inp.get("data_retirada"):
                        datas = {"dr": inp.get("data_retirada"), "dv": inp.get("data_devolucao")}
                elif b.get("name") == "acionar_consultor_pagamento":
                    modelo_aceito = inp.get("modelo") or modelo_aceito
                    if inp.get("data_retirada"):
                        datas = {"dr": inp.get("data_retirada"), "dv": inp.get("data_devolucao")}
            elif b.get("type") == "tool_result":
                cont = b.get("content")
                try:
                    data = json.loads(cont) if isinstance(cont, str) else cont
                    if isinstance(data, dict) and data.get("veiculos"):
                        veiculos = data["veiculos"][:3]
                except Exception:
                    pass
    partes = []
    if datas.get("dr") and datas.get("dv"):
        partes.append(f"📅 {datas['dr']} → {datas['dv']}")
    if modelo_aceito:
        partes.append(f"🚗 {modelo_aceito}")
    if veiculos:
        vs = "; ".join(f"{v.get('modelo')} {v.get('total', '')}".strip() for v in veiculos)
        partes.append(f"💵 {vs} (s/ impostos)")
    return " | ".join(partes) if partes else "—"


# ------- classificação das conversas (para as estatísticas) -------
def _tem_ferramenta(st, nome):
    for m in st.get("history", []):
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == nome:
                    return True
    return False


def _texto_cliente(st):
    return " ".join(
        m.get("content") for m in st.get("history", [])
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ).lower()


def _texto_agente(st):
    partes = []
    for m in st.get("history", []):
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                typ, txt = _bloco_tipo_texto(b)
                if typ == "text":
                    partes.append(txt)
    return " ".join(partes).lower()


def _classificar(st):
    """Folha ÚNICA da conversa (base da árvore/funil). Os ramos de preço
    (Solicitou reserva / Reclamou de preço / Não teve continuidade) somam 'Viram preço'."""
    manual = st.get("situacao_manual")
    if manual:  # ajuste manual sobrepõe a classificação automática
        return manual
    reservou = bool(st.get("reservou")) or bool(st.get("intencao"))
    escalar = bool(st.get("escalar"))
    tc = _texto_cliente(st)
    ta = _texto_agente(st)
    fora = bool(st.get("fora_area")) or any(k in ta for k in [
        "cobertura", "não atendemos", "nao atendemos", "no atendemos",
        "no podemos atender", "fora de orlando", "fuera de orlando",
    ])
    reclamou = any(k in tc for k in [
        "caro", "mais barato", "más barato", "mas barato", "menos de",
        "desconto", "descuento", "presupuesto", "orçamento", "orcamento",
        "muito alto", "muy caro", "no tengo tanto",
    ])
    viu = _tem_ferramenta(st, "consultar_disponibilidade_precos")

    if not tc.strip():
        return "Sem interação"
    if reservou:
        return "Solicitou reserva"       # ramo: Viram preço
    if reclamou:
        return "Reclamou de preço"       # ramo: Viram preço
    if viu:
        return "Não teve continuidade"   # ramo: Viram preço
    if fora:
        return "Fora de Orlando"
    if escalar:
        return "Solicitou consultor"
    return "Em conversa"


def _tem_resposta_cliente(st):
    """True se o cliente já enviou ao menos uma mensagem (role 'user')."""
    return any(m.get("role") == "user" for m in st.get("history", []))


def _fmt_ts(iso, offset_horas=-3):
    """Formata um ISO (UTC) para 'dd/mm HH:MM' no fuso do Brasil (UTC-3)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=offset_horas))).strftime("%d/%m %H:%M")
    except Exception:
        return ""


def _janela_info(st):
    """Janela de 24h do WhatsApp a partir da última mensagem do cliente.

    Estados possíveis:
      - aberta       : cliente respondeu há menos de 24h (recado livre liberado).
      - fechada      : cliente respondeu, mas já passou das 24h.
      - sem_resposta : cliente NUNCA respondeu (só recebeu o disparo). A janela
                       nunca abriu — só dá para reabrir com um template.
      - desconhecida : respondeu antes de existir o registro de horário; não
                       sabemos se ainda está aberta, então deixamos tentar e o
                       WhatsApp valida no envio."""
    ts = st.get("ultima_msg_cliente")
    if not ts:
        if not _tem_resposta_cliente(st):
            return {"estado": "sem_resposta", "aberta": False, "restante_min": 0,
                    "label": "cliente ainda não respondeu — só via template"}
        return {"estado": "desconhecida", "aberta": False, "restante_min": 0,
                "label": "janela desconhecida (conversa anterior ao registro)"}
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return {"estado": "desconhecida", "aberta": False, "restante_min": 0,
                "label": "janela desconhecida"}
    seg = (timedelta(hours=24) - (datetime.now(timezone.utc) - dt)).total_seconds()
    if seg <= 0:
        return {"estado": "fechada", "aberta": False, "restante_min": 0,
                "label": "fechada (+24h)"}
    m = int(seg // 60)
    return {"estado": "aberta", "aberta": True, "restante_min": m,
            "label": f"aberta · faltam {m // 60}h{m % 60:02d}m"}


# Filtros do painel: cada card leva a um conjunto de folhas (None = todos)
_RAMO_PRECO = {"Não teve continuidade", "Reclamou de preço", "Solicitou reserva"}
FILTROS = {
    "todos": None,
    "sem_interacao": {"Sem interação"},
    "conversa_iniciada": {"Em conversa", "Fora de Orlando", "Solicitou consultor"} | _RAMO_PRECO,
    "em_conversa": {"Em conversa"},
    "fora": {"Fora de Orlando"},
    "consultor": {"Solicitou consultor"},
    "viram_preco": set(_RAMO_PRECO),
    "nao_continuidade": {"Não teve continuidade"},
    "reclamou": {"Reclamou de preço"},
    "reserva": {"Solicitou reserva"},
}
FILTRO_LABEL = {
    "todos": "Todas as conversas", "sem_interacao": "Sem interação",
    "conversa_iniciada": "Conversa iniciada", "em_conversa": "Em conversa",
    "fora": "Fora de Orlando", "consultor": "Solicitou consultor",
    "viram_preco": "Viram preço", "nao_continuidade": "Não teve continuidade",
    "reclamou": "Reclamou de preço", "reserva": "Solicitou reserva",
}
# Categorias válidas para ajuste manual da situação
SITUACOES_VALIDAS = {
    "Sem interação", "Em conversa", "Fora de Orlando", "Solicitou consultor",
    "Não teve continuidade", "Reclamou de preço", "Solicitou reserva",
}


# ------- normalização de telefone (mesma lógica da produção, isolada) -------
def _variantes_telefone(from_number):
    num = (from_number or "").replace("whatsapp:", "")
    variantes = set()

    def add(n):
        variantes.add(n)
        variantes.add(f"whatsapp:{n}")

    add(num)
    if num.startswith("+55") and len(num) >= 5:
        ddd, local = num[3:5], num[5:]
        if len(local) == 9 and local.startswith("9"):
            add("+55" + ddd + local[1:])
        elif len(local) == 8:
            add("+55" + ddd + "9" + local)
    if num.startswith("+521"):
        add("+52" + num[4:])
    elif num.startswith("+52"):
        add("+521" + num[3:])
    if num.startswith("+549"):
        add("+54" + num[4:])
    elif num.startswith("+54"):
        add("+549" + num[3:])
    return variantes


def _encontrar_conversa(from_number):
    """Procura a conversa (testando variações de telefone) no store. (key, conversa) ou (None, None)."""
    for k in _variantes_telefone(from_number):
        c = agent_store.carregar(k)
        if c is not None:
            return k, c
    return None, None


# ------- efeitos: registro na planilha e alerta de escalonamento -------
def _registrar_planilha(lead_info):
    try:
        sheet = conectar_google_sheets()
        ws = sheet.spreadsheet.worksheet("Leads_Qualificados")
        ws.append_row([
            lead_info["timestamp"], lead_info["name"], lead_info["phone"],
            lead_info["category"], lead_info["message"],
        ])
    except Exception as e:
        print(f"⚠️ Falha ao registrar lead (agente): {e}")


def _enviar_email_conversa(lead_info, conversa):
    """Envia à equipe o alerta com a CONVERSA COMPLETA (uma vez por conversa)."""
    if conversa.get("email_enviado"):
        return
    try:
        reservou = bool(conversa.get("reservou"))
        assunto = (
            "🔥 Agente Allycar: cliente PRONTO para reservar (finalizar pagamento)"
            if reservou else
            "🧑‍💼 Agente Allycar: cliente pede atendimento humano"
        )
        conteudo = f"""Lead do agente (WhatsApp)

Data/Hora: {lead_info['timestamp']}
Nome: {lead_info['name']}
Telefone: {lead_info['phone']}
Situação: {lead_info['category']}
Motivo: {conversa.get('motivo_escalonamento', '')}

--- Conversa completa ---
{_transcricao(conversa)}
"""
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                     "Content-Type": "application/json"},
            json={
                "from": "Allycar <booking@allycar.com>",
                "to": ["david@allycar.com", "bruno@allycar.com", "higor@allycar.com"],
                "subject": assunto,
                "text": conteudo,
            },
            timeout=10,
        )
        conversa["email_enviado"] = True
    except Exception as e:
        print(f"⚠️ Falha ao enviar email da conversa (agente): {e}")


# ------- processamento assíncrono (thread de fundo) -------
def _processar(from_number, body):
    try:
        key, conversa = _encontrar_conversa(from_number)
        if conversa is None:
            key = from_number
            conversa = {"name": "Cliente", "phone": from_number.replace("whatsapp:", ""), "history": []}

        # Marca a hora da última mensagem DO CLIENTE (abre/renova a janela de 24h)
        conversa["ultima_msg_cliente"] = datetime.now(timezone.utc).isoformat()

        # Modo humano: um consultor assumiu — o agente NÃO responde automaticamente.
        if conversa.get("humano"):
            conversa.setdefault("history", []).append({"role": "user", "content": body})
            agent_store.salvar(key, conversa)
            _registrar_planilha({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "name": conversa.get("name", "Cliente"),
                "phone": from_number.replace("whatsapp:", ""),
                "category": "Atendimento humano",
                "message": body,
            })
            print(f"🧑 Modo humano — mensagem de {from_number} registrada (agente não respondeu).")
            return

        resultado = main_agent.responder_agente(conversa, body)
        texto = resultado["texto"]

        # Link rastreável: troca allycar.com pelo redirect /r/<code> (registra clique + IP).
        # Só afeta o texto ENVIADO — o histórico do agente mantém "allycar.com".
        texto_envio = _injetar_link_rastreavel(texto, conversa)

        # Envia a resposta do agente ao cliente (janela de 24h — cliente acabou de escrever)
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER, to=from_number, body=texto_envio
        )
        print(f"🤖 Agente respondeu {from_number}: {texto[:80]}")

        lead_info = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": conversa.get("name", "Cliente"),
            "phone": from_number.replace("whatsapp:", ""),
            "category": "Reservou" if resultado["reservou"] else ("Escalar humano" if resultado["escalar"] else "Agente - em conversa"),
            "message": body,
        }
        _registrar_planilha(lead_info)

        # Email com a conversa completa quando o cliente reserva ou pede humano
        if resultado["reservou"] or resultado["escalar"]:
            _enviar_email_conversa(lead_info, conversa)

        agent_store.salvar(key, conversa)  # persiste o estado após o turno

    except Exception as e:
        print(f"❌ Erro no processamento do agente: {e}")


# ------- rotas -------
@app.route("/webhook/agent", methods=["POST"])
def webhook_agent():
    """Recebe mensagens do cliente e responde de forma assíncrona via agente."""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    button_payload = request.form.get("ButtonPayload")
    if button_payload:
        body = button_payload

    print(f"📥 [agente] mensagem de {from_number}: {body}")

    if from_number and body:
        threading.Thread(target=_processar, args=(from_number, body), daemon=True).start()

    # Ack imediato (a resposta real vai pela API REST, na thread de fundo)
    return ("", 204)


@app.route("/r/<code>", methods=["GET"])
def redirect_rastreavel(code):
    """Link rastreável: registra o clique (IP, device, hora) e encaminha ao site."""
    try:
        key, conversa = agent_store.buscar_por_ref(code)
        if conversa is not None:
            ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or request.remote_addr or "")
            clique = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ip": ip,
                "ua": request.headers.get("User-Agent", "")[:300],
            }
            conversa.setdefault("site_cliques", []).append(clique)
            conversa["site_clicou"] = True
            conversa["site_ultimo_clique"] = clique["ts"]
            conversa["site_ip"] = ip
            agent_store.salvar(key, conversa)
            print(f"🔗 Clique rastreado: {key} ip={ip}")
    except Exception as e:
        print(f"⚠️ Falha ao registrar clique {code}: {e}")
    # Encaminha ao site levando o ref adiante (pra correlação futura com a HQ)
    return redirect(f"{SITE_URL}/?ref={code}", code=302)


@app.route("/agent/enviar-inicial", methods=["GET", "POST"])
def enviar_inicial():
    """
    Envia o template de entrada do agente para um número e registra a conversa.
    Aceita POST JSON {phone, name, language?} OU GET por querystring
    (?phone=+55...&name=David&language=pt) — para testar direto pelo navegador.
    """
    data = request.get_json(force=True, silent=True) or {}
    phone = (data.get("phone") or request.args.get("phone") or "").strip()
    name = (data.get("name") or request.args.get("name") or "Cliente").strip()
    language = (data.get("language") or request.args.get("language") or "pt").strip()
    if not phone:
        return {"error": "phone obrigatório"}, 400
    sid = _template_sid(language)
    if not sid:
        return {"error": f"template do agente não configurado para o idioma '{language}'"}, 400

    to = formatar_telefone(phone)  # whatsapp:+...
    conversa = _nova_conversa_inicial(name, phone, language)
    try:
        msg = _enviar_template(to, sid, name, conversa["ref_code"])
    except Exception as e:
        return {"error": str(e)}, 500

    agent_store.salvar(to, conversa)
    return {"status": "enviado", "sid": msg.sid, "to": to, "ref": conversa["ref_code"]}, 200


def _disparar_leads_agente():
    """
    Lê os leads da planilha e dispara o template do agente — com a MESMA
    verificação da produção: horário comercial por país, dados válidos e
    checagem de reserva ativa na HQ (pula quem já é cliente com reserva).
    Registra a conversa (com nome/idioma) para as respostas caírem no agente.
    """
    print("🚀 [agente] iniciando disparo de leads...")
    sheet = conectar_google_sheets()
    leads = sheet.get_all_records()
    headers = sheet.row_values(1)
    col_status = headers.index("STATUS") + 1

    enviados = erros = pulados = 0
    for idx, lead in enumerate(leads, start=2):  # linha 1 = cabeçalho
        nome = str(lead.get("NOME", "")).strip()
        telefone = str(lead.get("TELEFONE", "")).strip()
        status = str(lead.get("STATUS", "")).strip()

        if status == "Sent":  # só pula quem já foi enviado (mesma regra da produção)
            pulados += 1
            continue
        if not nome or not telefone:
            sheet.update_cell(idx, col_status, "Error - dados incompletos")
            erros += 1
            continue

        pais = descobrir_pais_por_telefone(telefone)
        if not esta_no_horario_comercial(pais):
            print(f"⏰ Fora do horário de {pais} — pulando {nome}")
            pulados += 1
            continue

        telefone_fmt = formatar_telefone(telefone)  # whatsapp:+...
        try:
            if cliente_ja_tem_reserva(telefone_fmt):
                sheet.update_cell(idx, col_status, "Skipped - já tem reserva")
                pulados += 1
                continue
        except Exception as e:
            print(f"⚠️ Falha ao checar reserva HQ de {nome}: {e}")

        # EUA → email + SMS em inglês (mesma função da produção, sem WhatsApp/agente)
        if (pais or "").strip().lower() in PAISES_USA:
            email_cliente = str(lead.get("EMAIL", "")).strip()
            try:
                sucesso, resultado = enviar_mensagem_inicial_com_opcoes(
                    telefone_fmt, nome, pais, email_cliente
                )
                if sucesso:
                    sheet.update_cell(idx, col_status, "Sent")
                    enviados += 1
                    print(f"✅ [agente/EUA] email+SMS para {nome}")
                else:
                    sheet.update_cell(idx, col_status, f"Error: {str(resultado)[:80]}")
                    erros += 1
            except Exception as e:
                sheet.update_cell(idx, col_status, f"Error: {str(e)[:80]}")
                erros += 1
            time.sleep(2)
            continue

        # Brasil / LATAM → WhatsApp com o agente
        language = _idioma_por_pais(pais)
        sid = _template_sid(language)
        if not sid:
            sheet.update_cell(idx, col_status, f"Error - sem template {language}")
            erros += 1
            continue

        conversa = _nova_conversa_inicial(nome, telefone_fmt, language)
        try:
            _enviar_template(telefone_fmt, sid, nome, conversa["ref_code"])
            agent_store.salvar(telefone_fmt, conversa)
            sheet.update_cell(idx, col_status, "Sent")
            enviados += 1
            print(f"✅ [agente] enviado para {nome} ({telefone_fmt}) [{language}]")
        except Exception as e:
            sheet.update_cell(idx, col_status, f"Error: {str(e)[:80]}")
            erros += 1
        time.sleep(2)  # respeita limites do Twilio

    resumo = {"enviados": enviados, "erros": erros, "pulados": pulados}
    print(f"🏁 [agente] disparo concluído: {resumo}")
    return resumo


@app.route("/agent/trigger-send", methods=["GET", "POST"])
def agent_trigger_send():
    """Dispara os leads da planilha pelo agente (para o cron chamar)."""
    try:
        return {"status": "success", "resumo": _disparar_leads_agente()}, 200
    except Exception as e:
        print(f"❌ Erro no disparo do agente: {e}")
        return {"status": "error", "message": str(e)}, 500


@app.route("/agent/health", methods=["GET"])
def health():
    return {
        "status": "ok",
        "conversas": agent_store.contar(),
        "persistencia": "postgres" if agent_store.ATIVO else "memoria",
        "modelo": main_agent.MODELO,
        "template_br": bool(AGENT_TEMPLATE_SID_BR),
        "template_es": bool(AGENT_TEMPLATE_SID_ES),
    }, 200


# ------- teste conversacional pelo navegador (sem Twilio/WhatsApp) -------
@app.route("/agent/chat", methods=["POST"])
def agent_chat():
    """Roda o agente de forma síncrona para testes. Body JSON: {session, message}."""
    data = request.get_json(force=True, silent=True) or {}
    session = (data.get("session") or "teste-web").strip()
    message = (data.get("message") or "").strip()
    if not message:
        return {"error": "message vazio"}, 400
    conversa = agent_store.carregar(session) or {"name": "Cliente", "phone": session, "history": []}
    resultado = main_agent.responder_agente(conversa, message)
    agent_store.salvar(session, conversa)
    return {
        "reply": resultado["texto"],
        "reservou": resultado["reservou"],
        "escalar": resultado["escalar"],
    }, 200


@app.route("/agent/chat/reset", methods=["POST"])
def agent_chat_reset():
    data = request.get_json(force=True, silent=True) or {}
    session = (data.get("session") or "teste-web").strip()
    agent_store.deletar(session)
    return {"status": "reset", "session": session}, 200


@app.route("/agent", methods=["GET"])
def agent_ui():
    """Página simples de chat para testar o agente."""
    return _CHAT_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# ------- dashboard dos leads (lê direto do Postgres) -------
@app.route("/agent/leads/data", methods=["GET"])
def agent_leads_data():
    if not _dash_ok():
        return {"error": "não autorizado"}, 401
    filtro = (request.args.get("filtro") or "").strip()
    leafs = FILTROS.get(filtro)  # None = todos
    itens = []
    for row in agent_store.listar():
        st = row.get("state") or {}
        situacao = _classificar(st)  # mesma classificação-folha do funil
        if leafs is not None and situacao not in leafs:
            continue
        jan = _janela_info(st)
        itens.append({
            "key": row.get("key"),
            "nome": st.get("name", "Cliente"),
            "telefone": st.get("phone", ""),
            "idioma": st.get("language", ""),
            "situacao": situacao,
            "situacao_manual": st.get("situacao_manual") or "",
            "resumo": _resumo_lead(st),
            "motivo": st.get("motivo_escalonamento", ""),
            "atualizado": row.get("updated_at"),
            "transcricao": _transcricao(st),
            "janela_aberta": jan["aberta"],
            "janela_estado": jan["estado"],
            "janela_label": jan["label"],
            "humano": bool(st.get("humano")),
            "pode_enviar": bool((row.get("key") or "").startswith("whatsapp:")),
            "site_clicou": bool(st.get("site_clicou")),
            "site_ultimo_clique": _fmt_ts(st.get("site_ultimo_clique")),
            "site_ip": st.get("site_ip") or "",
            "site_cliques": len(st.get("site_cliques") or []),
        })
    return {
        "total": len(itens),
        "filtro": filtro,
        "filtro_label": FILTRO_LABEL.get(filtro, ""),
        "leads": itens,
    }, 200


@app.route("/agent/leads/situacao", methods=["POST"])
def agent_leads_situacao():
    """Ajuste manual da situação (sobrepõe a classificação automática)."""
    if not _dash_ok():
        return {"error": "não autorizado"}, 401
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("key") or "").strip()
    sit = (data.get("situacao") or "").strip()
    if not key:
        return {"error": "key obrigatório"}, 400
    if sit and sit not in SITUACOES_VALIDAS:
        return {"error": "situação inválida"}, 400
    conversa = agent_store.carregar(key)
    if conversa is None:
        return {"error": "conversa não encontrada"}, 404
    if sit:
        conversa["situacao_manual"] = sit
    else:
        conversa.pop("situacao_manual", None)  # limpar → volta ao automático
    agent_store.salvar(key, conversa)
    return {"status": "ok", "key": key, "situacao": sit or "(automático)"}, 200


@app.route("/agent/leads/enviar", methods=["POST"])
def agent_leads_enviar():
    """Envia um recado manual do consultor ao cliente (dentro da janela de 24h)."""
    if not _dash_ok():
        return {"error": "não autorizado"}, 401
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("key") or "").strip()
    msg = (data.get("mensagem") or "").strip()
    if not key or not msg:
        return {"error": "key e mensagem obrigatórios"}, 400
    if not key.startswith("whatsapp:"):
        return {"error": "esta conversa não é um WhatsApp real (ex: teste web)"}, 400
    conversa = agent_store.carregar(key)
    if conversa is None:
        return {"error": "conversa não encontrada"}, 404
    jan = _janela_info(conversa)
    if jan["estado"] == "sem_resposta":
        return {"error": "Cliente ainda não respondeu — a janela de 24h nunca abriu. Só é possível reabrir com um template (ex.: o disparo de retargeting)."}, 400
    if jan["estado"] == "fechada":
        return {"error": "Janela de 24h fechada — o WhatsApp não permite mensagem livre agora. O cliente precisa escrever primeiro (ou use um template)."}, 400
    try:
        twilio_client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, to=key, body=msg)
    except Exception as e:
        txt = str(e)
        if "63016" in txt or "outside" in txt.lower() or "window" in txt.lower():
            return {"error": "O WhatsApp recusou: fora da janela de 24h. O cliente precisa escrever primeiro (ou use um template)."}, 400
        return {"error": f"Falha ao enviar: {e}"}, 500
    # registra no histórico e assume modo humano (agente para de responder)
    conversa.setdefault("history", []).append(
        {"role": "assistant", "content": [{"type": "text", "text": f"[Consultor] {msg}"}]}
    )
    conversa["humano"] = True
    agent_store.salvar(key, conversa)
    return {"status": "enviado", "key": key}, 200


@app.route("/agent/leads/modo", methods=["POST"])
def agent_leads_modo():
    """Alterna entre atendimento humano e agente para uma conversa."""
    if not _dash_ok():
        return {"error": "não autorizado"}, 401
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("key") or "").strip()
    humano = bool(data.get("humano"))
    if not key:
        return {"error": "key obrigatório"}, 400
    conversa = agent_store.carregar(key)
    if conversa is None:
        return {"error": "conversa não encontrada"}, 404
    if humano:
        conversa["humano"] = True
    else:
        conversa.pop("humano", None)  # devolve ao agente
    agent_store.salvar(key, conversa)
    return {"status": "ok", "humano": humano}, 200


@app.route("/agent/leads/delete", methods=["POST"])
def agent_leads_delete():
    if not _dash_ok():
        return {"error": "não autorizado"}, 401
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return {"error": "key obrigatório"}, 400
    agent_store.deletar(key)
    return {"status": "deletado", "key": key}, 200


@app.route("/agent/leads", methods=["GET"])
def agent_leads_ui():
    if not _dash_ok():
        return "Não autorizado. Adicione ?token=SEU_TOKEN à URL.", 401
    return _LEADS_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/agent/stats/data", methods=["GET"])
def agent_stats_data():
    if not _dash_ok():
        return {"error": "não autorizado"}, 401
    from collections import Counter
    cats = Counter()
    total = 0
    for row in agent_store.listar(limit=5000):
        st = row.get("state") or {}
        cats[_classificar(st)] += 1
        total += 1

    sem = cats.get("Sem interação", 0)
    reserva = cats.get("Solicitou reserva", 0)
    reclamou = cats.get("Reclamou de preço", 0)
    nao_cont = cats.get("Não teve continuidade", 0)
    fora = cats.get("Fora de Orlando", 0)
    consultor = cats.get("Solicitou consultor", 0)
    em_conversa = cats.get("Em conversa", 0)

    conversa_iniciada = total - sem
    viram_preco = reserva + reclamou + nao_cont

    return {
        "total": total,
        "conversa_iniciada": conversa_iniciada,
        "viram_preco": viram_preco,
        "camada2": [
            {"nome": "Sem interação", "valor": sem},
            {"nome": "Conversa iniciada", "valor": conversa_iniciada},
        ],
        "camada3": [
            {"nome": "Em conversa", "valor": em_conversa},
            {"nome": "Fora de Orlando", "valor": fora},
            {"nome": "Solicitou consultor", "valor": consultor},
            {"nome": "Viram preço", "valor": viram_preco},
        ],
        "camada4": [
            {"nome": "Não teve continuidade", "valor": nao_cont},
            {"nome": "Reclamou de preço", "valor": reclamou},
            {"nome": "Solicitou reserva", "valor": reserva},
        ],
    }, 200


@app.route("/agent/stats", methods=["GET"])
def agent_stats_ui():
    if not _dash_ok():
        return "Não autorizado. Adicione ?token=SEU_TOKEN à URL.", 401
    return _STATS_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


_STATS_HTML = """<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Allycar — Estatísticas do Agente</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b141a;color:#e9edef;margin:0}
  header{background:#202c33;padding:14px 18px;font-weight:600;display:flex;justify-content:space-between;align-items:center}
  header a{color:#8fd0ff;text-decoration:none;font-weight:600}
  .wrap{max-width:1100px;margin:0 auto;padding:18px}
  .layer{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;margin:6px 0}
  .block{background:#111b21;border:1px solid #22303a;border-radius:12px;padding:14px 16px;flex:1;min-width:150px;max-width:250px;text-align:center}
  .block.big{max-width:340px;background:#0e2233}
  a.block{text-decoration:none;color:inherit;cursor:pointer;transition:border-color .15s, background .15s}
  a.block:hover{border-color:#3f88c5;background:#152634}
  .bn{color:#cbd5db;font-size:13px;font-weight:600}
  .bv{font-size:30px;font-weight:700;margin:4px 0}
  .bp{color:#8696a0;font-size:12px;line-height:1.3}
  .conn{text-align:center;color:#3a4c57;margin:2px 0;font-size:16px}
  .lvl{text-align:center;color:#8fd0ff;font-size:12px;margin:14px 0 6px}
</style></head><body>
<header><span>📈 Allycar — Estatísticas do Agente</span>
  <span><a id="leadsLink" href="#">← Leads</a>&nbsp;&nbsp;<a href="#" onclick="load();return false">Atualizar</a></span></header>
<div class="wrap">
  <div class="layer"><a id="totalCard" class="block big" href="#" style="border-top:3px solid #2f6fed">
    <div class="bn">Total de Leads</div><div class="bv" id="total">–</div><div class="bp">100%</div></a></div>
  <div class="conn">▼</div>
  <div class="layer" id="c2"></div>
  <div class="lvl">▼ dos que iniciaram conversa (<b id="n3">–</b>)</div>
  <div class="layer" id="c3"></div>
  <div class="lvl">▼ dos que viram preço (<b id="n4">–</b>)</div>
  <div class="layer" id="c4"></div>
</div>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const q=token?('?token='+encodeURIComponent(token)):'';
document.getElementById('leadsLink').href='/agent/leads'+q;
const CORES={'Sem interação':'#5b6b78','Conversa iniciada':'#3f88c5','Em conversa':'#4b7ea3',
  'Fora de Orlando':'#8a4fd0','Solicitou consultor':'#c2740c','Viram preço':'#0f9488',
  'Não teve continuidade':'#5b6b78','Reclamou de preço':'#b0742a','Solicitou reserva':'#12a150'};
let d={total:0};
const FILT={'Total de Leads':'todos','Sem interação':'sem_interacao','Conversa iniciada':'conversa_iniciada',
  'Em conversa':'em_conversa','Fora de Orlando':'fora','Solicitou consultor':'consultor','Viram preço':'viram_preco',
  'Não teve continuidade':'nao_continuidade','Reclamou de preço':'reclamou','Solicitou reserva':'reserva'};
function lurl(nome){const p=new URLSearchParams();if(token)p.set('token',token);p.set('filtro',FILT[nome]||'todos');return '/agent/leads?'+p.toString();}
function pct(a,b){return b>0?Math.round(a*100/b)+'%':'0%';}
function blk(o,prev,prevLbl){
  const cor=CORES[o.nome]||'#4b7ea3';
  const p1=pct(o.valor,prev)+' '+prevLbl;
  const p2=(prev===d.total)?'':(' · '+pct(o.valor,d.total)+' do total');
  return `<a class="block" href="${lurl(o.nome)}" style="border-top:3px solid ${cor}"><div class="bn">${o.nome}</div><div class="bv">${o.valor}</div><div class="bp">${p1}${p2}</div></a>`;
}
async function load(){
  const r=await fetch('/agent/stats/data'+q);
  if(!r.ok){document.body.innerHTML='<p style="padding:20px">Não autorizado — adicione ?token= na URL.</p>';return;}
  d=await r.json();
  document.getElementById('totalCard').href=lurl('Total de Leads');
  document.getElementById('total').textContent=d.total;
  document.getElementById('n3').textContent=d.conversa_iniciada;
  document.getElementById('n4').textContent=d.viram_preco;
  document.getElementById('c2').innerHTML=d.camada2.map(o=>blk(o,d.total,'do total')).join('');
  document.getElementById('c3').innerHTML=d.camada3.map(o=>blk(o,d.conversa_iniciada,'de quem iniciou')).join('');
  document.getElementById('c4').innerHTML=d.camada4.map(o=>blk(o,d.viram_preco,'de quem viu preço')).join('');
}
load();
</script></body></html>"""


_LEADS_HTML = """<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Allycar — Leads do Agente</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b141a;color:#e9edef;margin:0}
  header{background:#202c33;padding:14px 18px;font-weight:600;display:flex;justify-content:space-between;align-items:center}
  .wrap{max-width:1700px;width:96%;margin:0 auto;padding:16px}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:10px;border-bottom:1px solid #22303a;font-size:14px;vertical-align:top}
  th{color:#8696a0;font-weight:600}
  .tag{padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap;display:inline-block}
  .t-res{background:#0b6b3a;color:#d7ffe8}
  .t-rec{background:#7a4a12;color:#ffe8c7}
  .t-nc{background:#2b3640;color:#cdd6db}
  .t-con{background:#6b4a12;color:#ffe8c7}
  .t-fora{background:#432a63;color:#e5d3ff}
  .t-em{background:#284152;color:#cfe4f5}
  .t-sem{background:#2a3138;color:#96a3ab}
  button{background:#2a3942;color:#e9edef;border:none;border-radius:6px;padding:5px 10px;cursor:pointer}
  pre{white-space:pre-wrap;background:#111b21;padding:10px;border-radius:8px;margin:8px 0 0;font-size:13px;line-height:1.4}
  .conv{background:#111b21;padding:10px;border-radius:8px;margin:8px 0 0;font-size:13px;line-height:1.5}
  .conv>div{white-space:pre-wrap;word-break:break-word}
  .c-def{color:#c8d0d6}
  .c-cli{color:#7ec8ff}
  .c-cli b{color:#b9e2ff}
  .c-age{color:#86e0b0}
  .c-age b{color:#b6f0d0}
  .c-con{color:#ffd479}
  .c-con b{color:#ffe6ad}
  .c-mot{color:#8696a0;margin-top:8px}
</style></head><body>
<header><span>📊 Allycar — Leads do Agente</span><span><a id="statsLink" href="#" style="color:#8fd0ff;text-decoration:none;font-weight:600;margin-right:14px">📈 Estatísticas</a><button onclick="load()">Atualizar</button></span></header>
<div class="wrap"><div id="info" style="color:#8696a0;margin-bottom:8px"></div>
<table><thead><tr><th>Nome</th><th>Telefone</th><th>Idioma</th><th>Situação</th><th>Resumo (datas · carro · preços)</th><th>Atualizado</th><th></th></tr></thead>
<tbody id="rows"></tbody></table></div>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const filtro=new URLSearchParams(location.search).get('filtro')||'';
function qs(f){const p=new URLSearchParams();if(token)p.set('token',token);if(f)p.set('filtro',f);const s=p.toString();return s?('?'+s):'';}
document.getElementById('statsLink').href='/agent/stats'+qs('');
function tag(s){const m={'Solicitou reserva':'t-res','Reclamou de preço':'t-rec','Não teve continuidade':'t-nc','Solicitou consultor':'t-con','Fora de Orlando':'t-fora','Em conversa':'t-em','Sem interação':'t-sem'};return `<span class="tag ${m[s]||'t-em'}">${s}</span>`;}
function fmtConversa(txt){
  const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  let cur='c-def';  // fala atual (linhas de continuação herdam a cor)
  return txt.split('\\n').map(line=>{
    if(line.startsWith('Cliente:')) cur='c-cli';
    else if(line.startsWith('Agente: [Consultor]')) cur='c-con';
    else if(line.startsWith('Agente:')) cur='c-age';
    else if(line.startsWith('[Motivo')) return `<div class="c-mot">${esc(line)}</div>`;
    const idx=line.indexOf(':');
    let html=esc(line);
    if(idx>0 && (line.startsWith('Cliente:')||line.startsWith('Agente:')))
      html='<b>'+esc(line.slice(0,idx+1))+'</b>'+esc(line.slice(idx+1));
    return `<div class="${cur}">${html||'&nbsp;'}</div>`;
  }).join('');
}
async function load(){
  const r=await fetch('/agent/leads/data'+qs(filtro));
  if(!r.ok){document.getElementById('info').textContent='Não autorizado — adicione ?token= na URL.';return;}
  const j=await r.json();
  if(j.filtro && j.filtro_label){
    document.getElementById('info').innerHTML='Filtrando: <b>'+j.filtro_label+'</b> · '+j.total+' conversa(s) &nbsp; <a href="/agent/leads'+qs('')+'" style="color:#8fd0ff">✕ limpar filtro</a>';
  }else{
    document.getElementById('info').textContent=j.total+' conversa(s)';
  }
  const tb=document.getElementById('rows');tb.innerHTML='';
  j.leads.forEach((l,i)=>{
    const modo=l.humano?'<span title="atendimento manual" style="color:#ffd479">🧑 Humano</span>':'<span title="agente automático" style="color:#8fd0ff">🤖 Agente</span>';
    const est=l.janela_estado||(l.janela_aberta?'aberta':'fechada');
    const jan= est==='aberta'
      ?`<span style="color:#7ee0a8">🟢 ${l.janela_label}</span>`
      : est==='desconhecida'
      ?`<span style="color:#ffd479">🟡 ${l.janela_label}</span>`
      : est==='sem_resposta'
      ?`<span style="color:#9fb0bb">⚪ ${l.janela_label}</span>`
      :`<span style="color:#e0a0a0">🔴 ${l.janela_label}</span>`;
    const siteBadge = l.site_clicou
      ? `<span title="clicou no link do site${l.site_ip?(' · IP '+l.site_ip):''}" style="color:#8fd0ff">🔗 site ${l.site_ultimo_clique||''}${l.site_cliques>1?(' ('+l.site_cliques+'×)'):''}</span>`
      : `<span style="color:#5b6b75">🔗 sem clique</span>`;
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${l.nome||''}</td><td>${l.telefone||''}</td><td>${(l.idioma||'').toUpperCase()}</td>
      <td style="white-space:nowrap">${tag(l.situacao)}${l.situacao_manual?' <span title="ajustado manualmente" style="color:#8fd0ff">✎</span>':''}<br>${selectSit(l.key,l.situacao_manual)}</td>
      <td style="font-size:12px;max-width:230px">${(l.resumo||'—')}</td><td style="white-space:nowrap">${l.atualizado||''}<br><span style="font-size:11px">${modo}</span><br><span style="font-size:11px">${siteBadge}</span></td>
      <td style="white-space:nowrap"><button onclick="document.getElementById('t${i}').style.display=document.getElementById('t${i}').style.display==='block'?'none':'block'">ver conversa</button>
      <button style="background:#5a1f1f;color:#ffb4b4;margin-left:6px" onclick='del(${JSON.stringify(l.key||"")})'>excluir</button></td>`;
    tb.appendChild(tr);
    const tr2=document.createElement('tr');
    const key=l.key||'';
    let painel='';
    if(l.pode_enviar){
      const btnModo=l.humano
        ?`<button style="background:#2a3942" onclick='modo(${JSON.stringify(key)},false)'>↩︎ devolver ao agente</button>`
        :`<button style="background:#3a2f14;color:#ffd479" onclick='modo(${JSON.stringify(key)},true)'>🧑 assumir manualmente</button>`;
      const notaDesc = est==='desconhecida'
        ?`<div style="color:#ffd479;font-size:11px;margin-bottom:4px">Sem registro da última resposta do cliente (conversa anterior a esta função). Pode tentar enviar — se estiver fora das 24h, o WhatsApp recusa e avisamos aqui.</div>`
        :'';
      let caixa;
      if(est==='sem_resposta'){
        caixa=`<div style="margin-top:8px;color:#9fb0bb;font-size:12px">O cliente ainda não respondeu ao disparo — a janela de 24h nunca abriu. Não dá para mandar recado livre; só é possível reengajar com um <b>template</b> (ex.: o disparo de retargeting).</div>`;
      }else if(est==='fechada'){
        caixa=`<div style="margin-top:8px;color:#e0a0a0;font-size:12px">Janela de 24h fechada — o WhatsApp só permite mensagem livre depois que o cliente escrever de novo (ou via template).</div>`;
      }else{
        caixa=`${notaDesc}<div style="display:flex;gap:8px;margin-top:4px">
             <input id="m${i}" placeholder="Escreva um recado ao cliente…" style="flex:1;padding:9px;border-radius:8px;border:none;background:#2a3942;color:#e9edef">
             <button style="background:#00a884;color:#fff" onclick='enviar(${JSON.stringify(key)},${i})'>Enviar</button>
           </div>`;
      }
      painel=`<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">
                <span style="font-size:12px">Janela: ${jan}</span>${btnModo}</div>${caixa}<div id="s${i}" style="font-size:12px;margin-top:6px"></div>`;
    }
    const jornada = l.site_clicou
      ? `<div style="background:#0e1a20;border:1px solid #22303a;border-radius:8px;padding:8px 10px;margin:8px 0 0;font-size:12px">
           <b style="color:#8fd0ff">🔗 Jornada no site</b> · clicou ${l.site_cliques}× · último clique: ${l.site_ultimo_clique||'—'}${l.site_ip?(' · IP '+l.site_ip):''}
           <div style="color:#8696a0;margin-top:3px">Próximo passo: cruzar este IP com as tentativas de reserva da HQ para ver o step alcançado / se fechou.</div>
         </div>`
      : `<div style="color:#5b6b75;font-size:12px;margin:8px 0 0">🔗 Sem clique registrado no link do site.</div>`;
    const conv=(l.transcricao||'(sem mensagens)')+(l.motivo?('\\n\\n[Motivo: '+l.motivo+']'):'');
    tr2.innerHTML=`<td colspan="7"><div id="t${i}" style="display:none">${painel}${jornada}
      <div class="conv">${fmtConversa(conv)}</div></div></td>`;
    tb.appendChild(tr2);
  });
}
async function enviar(key,i){
  const inp=document.getElementById('m'+i);const st=document.getElementById('s'+i);
  const msg=(inp.value||'').trim();if(!msg){return;}
  st.style.color='#8696a0';st.textContent='Enviando…';
  const r=await fetch('/agent/leads/enviar'+qs(''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,mensagem:msg})});
  const j=await r.json().catch(()=>({}));
  if(r.ok){inp.value='';st.style.color='#7ee0a8';st.textContent='✓ enviado';setTimeout(load,600);}
  else{st.style.color='#e0a0a0';st.textContent='✕ '+(j.error||'falha ao enviar');}
}
async function modo(key,humano){
  await fetch('/agent/leads/modo'+qs(''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,humano})});
  load();
}
const SITS=['Sem interação','Em conversa','Fora de Orlando','Solicitou consultor','Não teve continuidade','Reclamou de preço','Solicitou reserva'];
function selectSit(key,manual){
  const opts=['<option value="">↻ automático</option>'].concat(
    SITS.map(s=>`<option value="${s}"${s===manual?' selected':''}>${s}</option>`));
  return `<select onchange='reclass(${JSON.stringify(key||"")},this.value)' style="margin-top:5px;font-size:11px;background:#2a3942;color:#e9edef;border:1px solid #33434d;border-radius:5px;padding:2px">${opts.join('')}</select>`;
}
async function reclass(key,sit){
  if(!key)return;
  await fetch('/agent/leads/situacao'+qs(''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,situacao:sit})});
  load();
}
async function del(key){
  if(!key||!confirm('Excluir a conversa de '+key+' ?'))return;
  await fetch('/agent/leads/delete'+qs(''),
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});
  load();
}
load();
</script></body></html>"""


_CHAT_HTML = """<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Allycar — Teste do Consultor</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b141a;color:#e9edef;margin:0}
  header{background:#202c33;padding:14px 18px;font-weight:600}
  #chat{max-width:640px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:10px;min-height:70vh}
  .msg{max-width:80%;padding:9px 12px;border-radius:10px;white-space:pre-wrap;line-height:1.35}
  .user{align-self:flex-end;background:#005c4b}
  .bot{align-self:flex-start;background:#202c33}
  .meta{align-self:center;font-size:12px;color:#8696a0}
  form{max-width:640px;margin:0 auto;display:flex;gap:8px;padding:12px 16px;position:sticky;bottom:0;background:#0b141a}
  input{flex:1;padding:11px;border-radius:8px;border:none;background:#2a3942;color:#e9edef;font-size:15px}
  button{padding:11px 16px;border:none;border-radius:8px;background:#00a884;color:#fff;font-weight:600;cursor:pointer}
</style></head><body>
<header>🚗 Allycar — Consultor (teste)&nbsp; <button onclick="reset()" style="float:right;background:#3b4a54">Reiniciar</button></header>
<div id="chat"></div>
<form onsubmit="return send(event)">
  <input id="inp" placeholder="Escreva como um cliente…" autocomplete="off" autofocus>
  <button>Enviar</button>
</form>
<script>
const chat=document.getElementById('chat'), inp=document.getElementById('inp');
const session='teste-web-'+Math.floor(Date.now()/86400000);
function add(t,cls){const d=document.createElement('div');d.className='msg '+cls;d.textContent=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;}
async function send(e){e.preventDefault();const m=inp.value.trim();if(!m)return false;add(m,'user');inp.value='';
  const w=add('…','meta');
  try{const r=await fetch('/agent/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session,message:m})});
    const j=await r.json();w.remove();add(j.reply||('erro: '+(j.error||'')),'bot');
    if(j.reservou)add('✅ lead marcou RESERVOU (consultor acionado)','meta');
    else if(j.escalar)add('🧑‍💼 escalado para humano','meta');
  }catch(err){w.remove();add('erro de rede','meta');}
  return false;}
async function reset(){await fetch('/agent/chat/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session})});chat.innerHTML='';}
</script></body></html>"""


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    print("🤖 webhook_agent (consultor) iniciado.")
    app.run(host="0.0.0.0", port=port)
