from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from main import conectar_google_sheets
from datetime import datetime
import os
import time
import threading
import http.client
import requests
import braza
# `json` precisa existir no MÓDULO. Antes só havia `import json as _bjson` (BrazaBank)
# e imports locais dentro de algumas funções — e um `import json` dentro de uma função
# torna o nome LOCAL na função inteira, quebrando usos anteriores (UnboundLocalError).
import json   # integração BrazaBank Checkout v2 (PIX + cartão)

load_dotenv()

app = Flask(__name__)

# Configurações
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
COMMERCIAL_WHATSAPP = os.getenv('COMMERCIAL_WHATSAPP')  # WhatsApp comercial
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Armazenar estado das conversas (em produção, use banco de dados)
conversations = {}

# =====================================
# CONFIGURAÇÃO API HQ RENTAL (disponibilidade + preços)
# =====================================
HQ_API_HOST = "https://api-america-miami.caagcrm.com"
HQ_API_AUTH = os.getenv(
    "HQ_API_AUTH",
    "Basic YzQzMlR2elRSbFdxMGlJNldUeEFGM1lvUjBqcjVkV2dxRWJ0NGs2TlFTZzhZbmd0RWg6NXVhQjZTWEdGNU1zTk40RExrd29wVTBuZ2RURVpGeHBNb0l4RnZZRHBveGRjaUgxZnA=",
)
HQ_BRAND_ID = os.getenv("HQ_BRAND_ID", "1")
HQ_PICKUP_LOCATION = os.getenv("HQ_PICKUP_LOCATION", "3")
HQ_DEFAULT_TIME = "10:00"   # horário fixo de retirada/devolução
HQ_CURRENCY = "USD"


def _parse_data(texto):
    """Tenta interpretar uma data digitada pelo cliente. Retorna datetime ou None."""
    texto = texto.strip()
    formatos = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d/%m", "%d-%m"]
    for fmt in formatos:
        try:
            dt = datetime.strptime(texto, fmt)
            # Se o ano não foi informado, assume o próximo ano possível
            if dt.year == 1900:
                hoje = datetime.now()
                dt = dt.replace(year=hoje.year)
                if dt.date() < hoje.date():
                    dt = dt.replace(year=hoje.year + 1)
            return dt
        except ValueError:
            continue
    return None


def _extrair_assentos(features):
    """Extrai o número de assentos a partir da lista de features do veículo."""
    for f in features or []:
        label = (f.get("label") or "").lower()
        if "seat" in label or "assento" in label or "plaza" in label:
            numeros = "".join(c if c.isdigit() else " " for c in label).split()
            if numeros:
                return int(numeros[0])
    return None


def consultar_disponibilidade(pick_up_date, return_date, seats=None, top_n=3):
    """
    Consulta a API HQ por disponibilidade e preços.
    pick_up_date / return_date: strings yyyy-mm-dd
    seats: int (5, 7, 8) para filtrar por categoria, ou None para todos
    Retorna lista de dicts: {label, seats, daily, total, image, quantity}
    """
    payload = {
        "pick_up_date": pick_up_date,
        "return_date": return_date,
        "pick_up_time": HQ_DEFAULT_TIME,
        "return_time": HQ_DEFAULT_TIME,
        "brand_id": HQ_BRAND_ID,
        "pick_up_location": HQ_PICKUP_LOCATION,
        "return_location": HQ_PICKUP_LOCATION,
        "currency": HQ_CURRENCY,
    }
    headers = {"Authorization": HQ_API_AUTH, "Content-Type": "application/json"}

    try:
        r = requests.post(
            f"{HQ_API_HOST}/api-america-miami/car-rental/reservations/dates",
            json=payload, headers=headers, timeout=20,
        )
        if r.status_code != 200:
            print(f"⚠️ HQ disponibilidade HTTP {r.status_code}: {r.text[:200]}")
            return []
        classes = r.json().get("data", {}).get("applicable_classes", [])
    except Exception as e:
        print(f"⚠️ Erro ao consultar disponibilidade: {e}")
        return []

    resultados = []
    for c in classes:
        avail = c.get("availability", {})
        if not avail.get("selectable") or avail.get("quantity", 0) <= 0:
            continue

        vc = c.get("vehicle_class", {})
        n_assentos = _extrair_assentos(vc.get("features"))

        # Filtro por assentos (8 = 8 ou mais)
        if seats is not None and n_assentos is not None:
            if seats >= 8:
                if n_assentos < 8:
                    continue
            elif n_assentos != seats:
                continue

        try:
            preco = c["price"]
            total = float(preco["base_price_with_taxes"]["amount"])
            daily = preco["details"][0]["base_daily_price_with_taxes"]["amount_for_display"]
            total_fmt = preco["base_price_with_taxes"]["amount_for_display"]
        except (KeyError, IndexError, ValueError):
            continue

        resultados.append({
            "label": vc.get("label", "Veículo"),
            "seats": n_assentos,
            "daily": daily,
            "total": total_fmt,
            "total_raw": total,
            "image": vc.get("image", ""),
            "quantity": avail.get("quantity", 0),
        })

    resultados.sort(key=lambda x: x["total_raw"])
    return resultados[:top_n]


def _formatar_resultados(resultados, lang, pickup_str, return_str):
    """Monta a mensagem de WhatsApp com as opções encontradas."""
    if lang == "es":
        cabecalho = f"🔎 Opciones disponibles del *{pickup_str}* al *{return_str}*:\n"
        rotulo_dia = "/día"
        rotulo_total = "Total"
    else:
        cabecalho = f"🔎 Opções disponíveis de *{pickup_str}* a *{return_str}*:\n"
        rotulo_dia = "/dia"
        rotulo_total = "Total"

    linhas = [cabecalho]
    for i, r in enumerate(resultados, 1):
        assentos = f"{r['seats']} " + ("plazas" if lang == "es" else "lugares") if r.get("seats") else ""
        linhas.append(
            f"\n*{i}. {r['label']}* {('· ' + assentos) if assentos else ''}\n"
            f"   💵 {r['daily']}{rotulo_dia}  |  {rotulo_total}: {r['total']}"
        )
    return "\n".join(linhas)

import smtplib
from email.message import EmailMessage
import os

def registrar_lead_qualificado(lead_info):

    # =====================================
    # REGISTRO LEAD QUALIFICADO PLANILHA
    # =====================================
    try:
        sheet = conectar_google_sheets()
        worksheet = sheet.spreadsheet.worksheet("Leads_Qualificados")

        worksheet.append_row([
            lead_info["timestamp"],
            lead_info["name"],
            lead_info["phone"],
            lead_info["category"],
            lead_info["message"]
        ])

        print("✅ Lead qualificado salvo na planilha")

    except Exception as e:
        print(f"⚠️ Erro ao salvar lead qualificado: {e}")
        return False  # aqui sim faz sentido parar, pq não salvou

    # Email NÃO é mais enviado aqui — só no estágio final da conversa
    # (ver enviar_alerta_email, chamado quando a conversa é concluída).
    return True


def enviar_alerta_email(lead_info, canal="WhatsApp"):
    """
    Envia o alerta de lead qualificado por email (Resend).
    Chamado APENAS quando a conversa chega ao estágio final.
    Email é best-effort: falhas são ignoradas.
    """
    try:
        destinatarios = [
            "booking@allycar.com",
            "david@allycar.com",
            "higor@allycar.com",
            "bruno@allycar.com"
        ]

        conteudo = f"""Novo lead qualificado ({canal})

Data/Hora: {lead_info["timestamp"]}
Nome: {lead_info["name"]}
Telefone: {lead_info["phone"]}
Interesse: {lead_info["category"]}

Mensagem:
{lead_info["message"]}
"""

        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                "Content-Type": "application/json"
            },
            json={
                "from": "Allycar <booking@allycar.com>",
                "to": destinatarios,
                "subject": f"🚨 Lead qualificado Allycar: {lead_info['name']}",
                "text": conteudo
            },
            timeout=10
        )

        if response.status_code in (200, 201):
            print("✅ Alerta enviado por email (Resend)")
        else:
            print(f"⚠️ Falha ao enviar email (Resend): {response.status_code} - {response.text}")

    except Exception as e:
        print(f"⚠️ Erro ao enviar alerta por email (ignorado): {e}")


def _finalizar_alerta(conversa, lead_info, canal):
    """
    Envia o alerta por email apenas quando a conversa está concluída,
    garantindo que seja enviado uma única vez por conversa.
    """
    if conversa.get('completed') and not conversa.get('email_sent'):
        info = dict(lead_info)
        info['category'] = conversa.get('category', lead_info.get('category'))

        mensagem = conversa.get('message', lead_info.get('message'))
        historico = conversa.get('historico') or []
        # Só anexa a transcrição se ela agregar contexto além da própria mensagem
        # (evita repetição quando a única resposta do cliente já é a mensagem)
        if historico and not (len(historico) == 1 and historico[0] == mensagem):
            transcricao = "\n".join(f"• {m}" for m in historico)
            mensagem = f"{mensagem}\n\n--- O que o cliente escreveu ---\n{transcricao}"
        info['message'] = mensagem

        enviar_alerta_email(info, canal=canal)
        conversa['email_sent'] = True

# =====================================
# WEBHOOK - RECEBER RESPOSTAS
# =====================================

MESSAGES = {
    "pt": {
        "start_wait": "Olá! Para iniciar, aguarde o envio da nossa mensagem.",
        "consultor_intro": """Perfeito! 👏

Um consultor entrará em contato em breve.""",
        "ask_details": """Excelente! 🎉

Por favor, nos conte um pouco sobre o que você procura:
- Modelo preferido
- Valor que pretende investir
- Prazo desejado
- Qualquer outra informação relevante""",
        "invalid_option": """Desculpe, não entendi sua resposta.

Por favor, escolha uma opção:

1️⃣ Carros com 5 assentos
2️⃣ Carros com 7 assentos
3️⃣ Carros com 8 assentos
4️⃣ Falar direto com nosso consultor""",
        "final_thanks": """Obrigado! Recebemos sua mensagem. 📝

Um de nossos consultores entrará em contato em instantes!

Tenha um ótimo dia! 🚗✨""",
        "ask_pickup_date": """Ótima escolha! 🚗

Para qual *data de retirada* você precisa do carro?
Use o formato DD/MM/AAAA (ex: 15/07/2026):""",
        "ask_return_date": "E qual a *data de devolução*? (ex: 20/07/2026):",
        "invalid_date": "Não consegui entender a data. 😅\nPor favor, use o formato DD/MM/AAAA (ex: 15/07/2026):",
        "invalid_date_range": "A data de devolução precisa ser *depois* da retirada. Por favor, informe a data de devolução novamente:",
        "date_in_past": "Essa data já passou. 😬 Por favor, informe uma data de retirada futura (DD/MM/AAAA):",
        "no_availability": """Não encontramos veículos disponíveis nessa categoria para as datas informadas. 😔

Mas não se preocupe — responda *CONSULTOR* que um de nossos atendentes vai buscar alternativas para você!""",
        "results_footer": """\nGostou de alguma opção? Responda *CONSULTOR* e um de nossos atendentes assume a conversa para finalizar sua reserva! 🚗✨

📍 Atuamos em Orlando e num raio de até 30 milhas.

_Impostos e taxas podem ser aplicados._""",
    },
    "es": {
        "start_wait": "Hola! Para comenzar, espera nuestro mensaje inicial.",
        "consultor_intro": """¡Perfecto! 👏

Un asesor se pondrá en contacto contigo en breve.""",
        "ask_details": """¡Excelente! 🎉

Cuéntanos un poco más sobre lo que estás buscando:
- Modelo preferido
- Presupuesto estimado
- Fechas del alquiler
- Cualquier otra información relevante""",
        "invalid_option": """Lo siento, no entendí tu respuesta.

Por favor, elige una opción:

1️⃣ Autos de 5 plazas
2️⃣ Autos de 7 plazas
3️⃣ Autos de 8 plazas
4️⃣ Hablar directamente con un asesor""",
        "final_thanks": """¡Gracias! Hemos recibido tu mensaje. 📝

Uno de nuestros asesores se pondrá en contacto contigo en breve.

¡Que tengas un excelente día! 🚗✨""",
        "ask_pickup_date": """¡Excelente elección! 🚗

¿Para qué *fecha de recogida* necesitas el auto?
Usa el formato DD/MM/AAAA (ej: 15/07/2026):""",
        "ask_return_date": "¿Y la *fecha de devolución*? (ej: 20/07/2026):",
        "invalid_date": "No pude entender la fecha. 😅\nPor favor, usa el formato DD/MM/AAAA (ej: 15/07/2026):",
        "invalid_date_range": "La fecha de devolución debe ser *posterior* a la de recogida. Por favor, indica la fecha de devolución nuevamente:",
        "date_in_past": "Esa fecha ya pasó. 😬 Por favor, indica una fecha de recogida futura (DD/MM/AAAA):",
        "no_availability": """No encontramos vehículos disponibles en esa categoría para las fechas indicadas. 😔

¡Pero no te preocupes! Responde *ASESOR* y uno de nuestros agentes buscará alternativas para ti.""",
        "results_footer": """\n¿Te gustó alguna opción? Responde *ASESOR* y uno de nuestros agentes continuará la conversación para finalizar tu reserva. 🚗✨

📍 Operamos en Orlando y en un radio de hasta 30 millas.

_Pueden aplicarse impuestos y tasas._""",
    }
}

def _variantes_telefone(from_number):
    """
    Gera variações da chave para localizar a conversa apesar de quirks de DDI:
    - Brasil: +55 DD 9XXXXXXXX  <->  +55 DD XXXXXXXX (o nono dígito do celular)
    - México: +52 1 XXXX  <->  +52 XXXX (o '1' de celular)
    - Argentina: +54 9 XXXX  <->  +54 XXXX (o '9' de celular)
    Considera também com e sem o prefixo 'whatsapp:'.
    """
    num = (from_number or "").replace("whatsapp:", "")
    variantes = set()

    def add(n):
        variantes.add(n)
        variantes.add(f"whatsapp:{n}")

    add(num)

    # Brasil (+55): nono dígito do celular fica DEPOIS do DDD
    if num.startswith("+55") and len(num) >= 5:
        ddd = num[3:5]
        local = num[5:]
        if len(local) == 9 and local.startswith("9"):
            add("+55" + ddd + local[1:])    # remove o 9
        elif len(local) == 8:
            add("+55" + ddd + "9" + local)  # adiciona o 9

    # México (+52)
    if num.startswith("+521"):
        add("+52" + num[4:])
    elif num.startswith("+52"):
        add("+521" + num[3:])

    # Argentina (+54)
    if num.startswith("+549"):
        add("+54" + num[4:])
    elif num.startswith("+54"):
        add("+549" + num[3:])

    return variantes


def encontrar_conversa_key(from_number):
    """Retorna a chave da conversa existente (testando variações) ou None."""
    for k in _variantes_telefone(from_number):
        if k in conversations:
            return k
    return None


@app.route('/webhook/whatsapp', methods=['POST'])
def webhook_whatsapp():
    """Recebe mensagens dos clientes via Twilio"""
    
    # Dados da mensagem recebida
    from_number = request.form.get('From')  # whatsapp:+5511999999999
    body = request.form.get('Body', '').strip()
    button_payload = request.form.get('ButtonPayload')

    if button_payload:
        body = button_payload  # Normaliza o valor do botão
    
    print(f"📥 Mensagem recebida de {from_number}: {body}")
    
    # Criar resposta
    resp = MessagingResponse()
    msg = resp.message()

    # Localiza a conversa tratando variações de DDI (México/Argentina) e prefixo
    conversation_key = encontrar_conversa_key(from_number)
    conversa = conversations.get(conversation_key) if conversation_key else None

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Monta o lead com o que temos (funciona mesmo sem conversa registrada)
    lead_info = {
        'name': conversa.get('name', 'Não informado') if conversa else 'Não informado',
        'phone': (from_number or '').replace('whatsapp:', ''),
        'category': conversa.get('category', 'Não especificado') if conversa else 'Primeiro contato',
        'message': body,
        'timestamp': timestamp,
    }

    # Registra TODA mensagem na planilha — inclusive primeiros contatos sem
    # conversa registrada (importante para rastrear leads de números não mapeados)
    try:
        registrar_lead_qualificado(lead_info)
    except Exception as e:
        print(f"⚠️ Falha ao registrar lead na planilha (ignorado): {e}")

    # Sem conversa ativa: já registramos o lead acima; responde e encerra (sem crashar)
    if conversa is None:
        msg.body(
            "Olá! 👋\n"
            "Por favor, aguarde nossa mensagem inicial para continuar.\n\n"
            "Hello! 👋\n"
            "Please wait for our initial message to continue.\n\n"
            "¡Hola! 👋\n"
            "Por favor, espere nuestro mensaje inicial para continuar."
        )
        return str(resp)

    lang = conversa.get("language", "pt")
    texts = MESSAGES.get(lang, MESSAGES["pt"])
    stage = conversa['stage']
    conversa['timestamp'] = timestamp

    # Acumula tudo que o cliente escreve, para o contexto completo no alerta
    if body:
        conversa.setdefault('historico', []).append(body)

    # ===== FLUXO DE CONVERSA =====

    # Resposta via SMS: não roda o fluxo de disponibilidade (somente WhatsApp).
    # Apenas confirma o recebimento e encerra; o lead já foi notificado acima.
    if not from_number.startswith("whatsapp:"):
        msg.body(
            "Thank you for your reply! One of our consultants will contact you shortly. - Allycar"
        )
        conversa['interested'] = True
        conversa['stage'] = 'finished'
        conversa['completed'] = True
        _finalizar_alerta(conversa, lead_info, canal="SMS")
        return str(resp)

    # Atalho global: cliente pede para falar com consultor a qualquer momento.
    # Pedir consultor já qualifica o lead → conversa concluída + alerta por email.
    if body.strip().upper() in ("CONSULTOR", "ASESOR", "ATENDENTE", "AGENTE"):
        conversa['interested'] = True
        conversa['category'] = 'Falar com consultor'
        conversa['message'] = 'Cliente solicitou falar com um consultor.'
        conversa['stage'] = 'finished'
        conversa['completed'] = True
        msg.body(texts["consultor_intro"])
        _finalizar_alerta(conversa, lead_info, canal="WhatsApp")
        return str(resp)

    # Estágio 1: Aguardando categoria
    if stage == 'awaiting_category':
        categoria = processar_escolha_categoria(body)

        if categoria == 'consultor':
            conversa['interested'] = True
            conversa['category'] = 'Falar com consultor'
            conversa['message'] = 'Cliente solicitou falar com um consultor.'
            conversa['stage'] = 'finished'
            conversa['completed'] = True
            msg.body(texts["consultor_intro"])

        elif categoria:
            # categoria = '5', '7' ou '8' (assentos)
            conversa['category'] = categoria
            conversa['seats'] = int(categoria)
            conversa['stage'] = 'awaiting_pickup_date'
            msg.body(texts["ask_pickup_date"])

        else:
            msg.body(texts["invalid_option"])

    # Estágio 2: Aguardando data de retirada
    elif stage == 'awaiting_pickup_date':
        dt = _parse_data(body)
        if not dt:
            msg.body(texts["invalid_date"])
        elif dt.date() < datetime.now().date():
            msg.body(texts["date_in_past"])
        else:
            conversa['pickup_date'] = dt.strftime("%Y-%m-%d")
            conversa['pickup_display'] = dt.strftime("%d/%m/%Y")
            conversa['stage'] = 'awaiting_return_date'
            msg.body(texts["ask_return_date"])

    # Estágio 3: Aguardando data de devolução + consulta de disponibilidade
    elif stage == 'awaiting_return_date':
        dt = _parse_data(body)
        if not dt:
            msg.body(texts["invalid_date"])
        else:
            pickup = datetime.strptime(conversa['pickup_date'], "%Y-%m-%d")
            if dt.date() <= pickup.date():
                msg.body(texts["invalid_date_range"])
            else:
                return_date = dt.strftime("%Y-%m-%d")
                conversa['return_date'] = return_date
                conversa['return_display'] = dt.strftime("%d/%m/%Y")

                resultados = consultar_disponibilidade(
                    conversa['pickup_date'], return_date, seats=conversa.get('seats')
                )

                if resultados:
                    corpo = _formatar_resultados(
                        resultados, lang,
                        conversa['pickup_display'], conversa['return_display'],
                    )
                    corpo += "\n" + texts["results_footer"]
                    msg.body(corpo)
                    carros_str = "\n".join(
                        f"- {r['label']}"
                        + (f" ({r['seats']} lugares)" if r.get('seats') else "")
                        + f": {r['daily']}/dia | total {r['total']}"
                        for r in resultados
                    )
                    conversa['message'] = (
                        f"Consultou disponibilidade: {conversa.get('seats')} assentos, "
                        f"{conversa['pickup_display']} → {conversa['return_display']}. "
                        f"{len(resultados)} opções apresentadas:\n{carros_str}"
                    )
                else:
                    msg.body(texts["no_availability"])
                    conversa['message'] = (
                        f"Consultou disponibilidade SEM resultados: {conversa.get('seats')} assentos, "
                        f"{conversa['pickup_display']} → {conversa['return_display']}."
                    )

                conversa['stage'] = 'finished'
                conversa['completed'] = True

    # Estágio final: aguardando mensagem livre do cliente
    elif stage == 'awaiting_message':
        conversa['message'] = body
        conversa['stage'] = 'finished'
        msg.body(texts["final_thanks"])
        conversa['completed'] = True

    # Envia o alerta por email só quando a conversa foi concluída (uma vez)
    _finalizar_alerta(conversa, lead_info, canal="WhatsApp")

    return str(resp)

def processar_escolha_categoria(body):
    """Processa escolha da categoria via número ou texto"""
    body_upper = body.upper().strip()

    # Mapeamento por número
    map_por_numero = {
        '1': '5',
        '2': '7',
        '3': '8',
        '4': 'consultor'
    }

    if body_upper in map_por_numero:
        return map_por_numero[body_upper]

    return None


@app.route('/sms-webhook', methods=['POST'])
def sms_webhook():
    """
    Recebe respostas via SMS (Twilio). Fluxo simples: registra na planilha,
    avisa o cliente que um consultor entrará em contato e dispara o alerta
    por email. NÃO roda o fluxo de disponibilidade (exclusivo do WhatsApp).
    """
    from_number = request.form.get('From', '')
    body = request.form.get('Body', '').strip()
    print(f"📥 SMS recebido de {from_number}: {body}")

    resp = MessagingResponse()
    msg = resp.message()

    phone_plain = from_number.replace('whatsapp:', '').strip()

    # Reaproveita o estado da conversa se já existir (nome, idioma, dedupe de email)
    conversa = (
        conversations.get(from_number)
        or conversations.get(phone_plain)
        or conversations.get(f"whatsapp:{phone_plain}")
    )
    if conversa is None:
        conversa = conversations.setdefault(
            from_number,
            {'name': 'Não informado', 'stage': 'sms', 'language': 'pt', 'interested': True},
        )

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lead_info = {
        'name': conversa.get('name', 'Não informado'),
        'phone': phone_plain,
        'category': 'Resposta via SMS',
        'message': body,
        'timestamp': timestamp,
    }
    conversa['timestamp'] = timestamp
    conversa['message'] = body

    # Registra TODA mensagem na planilha
    try:
        registrar_lead_qualificado(lead_info)
    except Exception as e:
        print(f"⚠️ Falha ao registrar SMS na planilha (ignorado): {e}")

    # SMS é considerado estágio final → dispara o email (uma vez por conversa)
    conversa['completed'] = True
    _finalizar_alerta(conversa, lead_info, canal="SMS")

    msg.body(
        "Thank you for your reply! One of our consultants will contact you shortly. - Allycar"
    )
    return str(resp)

# =====================================
# ROTAS DE INTEGRAÇÃO
# =====================================

@app.route('/register_conversation', methods=['POST'])
def register_conversation():
    """Registra uma nova conversa (chamado pelo main.py)"""
    try:
        data = request.json
        phone = data.get('phone')
        name = data.get('name')
        
        # Registrar conversa
        conversations[phone] = {
            'name': name,
            'city': 'Não informado',
            'stage': 'awaiting_category',
            'interested': False,
            'language': data.get('language', 'pt') 
        }
        
        print(f"✅ Conversa registrada: {name} ({phone})")
        
        return {'status': 'success', 'message': 'Conversation registered'}, 200
    except Exception as e:
        print(f"❌ Erro ao registrar conversa: {e}")
        return {'status': 'error', 'message': str(e)}, 500


@app.route('/trigger-retargeting', methods=['GET', 'POST'])
def trigger_retargeting():
    """
    Dispara a campanha de retargeting (guia 'Retargeting' da planilha).
    Registra cada lead como conversa em estágio final, de modo que a resposta
    do cliente caia no fluxo existente (agradecimento + alerta por email) e
    seja registrada em Leads_Qualificados como já acontece hoje.
    """
    try:
        import retargeting

        def _registrar_conversa(phone, name, language):
            conversations[phone] = {
                'name': name,
                'city': 'Não informado',
                'stage': 'awaiting_message',
                'category': 'Retargeting - Parcelamento/PIX',
                'interested': True,
                'language': language,
            }

        resumo = retargeting.enviar_campanha_retargeting(
            registrar_conversa=_registrar_conversa
        )
        return {'status': 'success', 'resumo': resumo}, 200
    except Exception as e:
        print(f"❌ Erro na campanha de retargeting: {e}")
        return {'status': 'error', 'message': str(e)}, 500


@app.route('/conversations', methods=['GET'])
def get_conversations():
    """Ver conversas ativas (para debug)"""
    return {
        'active_conversations': len(conversations),
        'conversations': conversations
    }, 200


@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return {'status': 'ok'}, 200


# =====================================
# ENDPOINT - CAPTURA DE LEAD (HOME)
# =====================================

@app.route('/api/leads', methods=['POST', 'OPTIONS'])
def capturar_lead_home():

    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())

    try:
        data  = request.get_json()
        name  = (data.get('name')  or '').strip()
        email = (data.get('email') or '').strip()
        lang  = (data.get('lang')  or 'pt').strip().lower()

        if lang not in ('pt', 'en', 'es'):
            lang = 'pt'

        if not name or not email:
            return {'status': 'error', 'message': 'Nome e email são obrigatórios'}, 400

        print(f"📥 Novo lead da home: {name} <{email}> | lang={lang}")

        subject, html = _lead_coupon_email(name, lang)

        # 1) ENVIA E-MAIL COM CUPOM
        resp = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f"Bearer {os.getenv('RESEND_API_KEY')}",
                'Content-Type': 'application/json'
            },
            json={
                'from': 'Allycar <booking@allycar.com>',
                'reply_to': 'david@allycar.com',
                'to': [email],
                'subject': subject,
                'html': html
            },
            timeout=10
        )

        email_ok = resp.status_code in (200, 201)

        if email_ok:
            print(f"✅ Cupom enviado para {name} <{email}> em [{lang}]")
        else:
            print(f"❌ Erro Resend: {resp.text}")

        # 2) REGISTRA NA PLANILHA (independente do e-mail — best effort)
        try:
            sheet = conectar_google_sheets()
            worksheet = sheet.spreadsheet.worksheet("Leads")
            worksheet.append_row([
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                name,
                email,
                '----',
                lang,
                'Cadastro Site',
            ])
            print(f"✅ Lead registrado na planilha: {name} <{email}>")
        except Exception as e:
            print(f"⚠️ Falha ao registrar na planilha (ignorado): {e}")

        status = 200 if email_ok else 500
        response = app.response_class(
            response=f'{{"status":"{"success" if email_ok else "error"}"}}',
            status=status,
            mimetype='application/json'
        )

    except Exception as e:
        print(f"⚠️ Erro no endpoint /api/leads: {e}")
        response = app.response_class(
            response='{"status":"error"}',
            status=500,
            mimetype='application/json'
        )

    return _cors(response)

def _lead_coupon_email(name: str, lang: str):
    """Retorna (subject, html) do e-mail do cupom no idioma correto."""

    FOOTER = f"""
        <br>
        <div style="background-color:#006354;padding:20px;text-align:center;border-radius:8px;">
            <img src="https://allycar.com/assets/allycar.png" alt="Allycar" style="max-width:180px;display:block;margin:0 auto 15px;">
            <p style="margin:5px 0;color:#fff;font-size:16px;font-weight:bold;">Allycar Team</p>
            <p style="margin:5px 0;color:#fff;font-size:14px;">Premium Car Rental | Orlando, FL</p>
            <p style="margin:5px 0;color:#fff;font-size:13px;">📞 +1 (407) 712-0270 | 📧 booking@allycar.com</p>
            <p style="margin:5px 0;"><a href="https://www.allycar.com" style="color:#fff;font-size:13px;text-decoration:none;">🌐 www.allycar.com</a></p>
        </div>
    """

    COUPON_BLOCK = """
        <div style="background-color:#f9f5e8;border:2px dashed #c9a84c;border-radius:10px;padding:24px;text-align:center;margin:28px 0;">
            <p style="margin:0 0 6px;font-size:14px;color:#888;letter-spacing:0.05em;text-transform:uppercase;">{label}</p>
            <p style="margin:0 0 8px;font-size:36px;font-weight:bold;letter-spacing:4px;color:#0a1628;">MYFIRSTBOOKING</p>
            <p style="margin:0;font-size:15px;color:#c9a84c;font-weight:bold;">{discount}</p>
        </div>
    """

    if lang == 'en':
        subject = 'Allycar | Your 5% discount coupon 🎉'
        coupon  = COUPON_BLOCK.format(
            label='Your exclusive coupon',
            discount='5% off your first booking'
        )
        html = f"""
        <div style="font-family:Arial,sans-serif;color:#333;max-width:600px;">
            <p>Hi, {name}!</p>
            <p>Great to have you here! Here is your welcome coupon:</p>
            {coupon}
            <p>How to use it:</p>
            <ol>
                <li>Go to <a href="https://www.allycar.com" style="color:#c9a84c;">www.allycar.com</a></li>
                <li>Choose your vehicle and travel dates</li>
                <li>Enter the code <strong>MYFIRSTBOOKING</strong> in the coupon field</li>
                <li>The 5% discount will be applied automatically</li>
            </ol>
            <p>Any questions? Just reply to this email — our team is ready to help!</p>
            <p>See you soon in Orlando! 🌴</p>
            {FOOTER}
        </div>"""

    elif lang == 'es':
        subject = 'Allycar | Tu cupón de 5% de descuento 🎉'
        coupon  = COUPON_BLOCK.format(
            label='Tu cupón exclusivo',
            discount='5% de descuento en tu primera reserva'
        )
        html = f"""
        <div style="font-family:Arial,sans-serif;color:#333;max-width:600px;">
            <p>¡Hola, {name}!</p>
            <p>¡Qué bueno tenerte aquí! Aquí está tu cupón de bienvenida:</p>
            {coupon}
            <p>Cómo usarlo:</p>
            <ol>
                <li>Entra en <a href="https://www.allycar.com" style="color:#c9a84c;">www.allycar.com</a></li>
                <li>Elige tu vehículo y las fechas de tu viaje</li>
                <li>Ingresa el código <strong>MYFIRSTBOOKING</strong> en el campo de cupón</li>
                <li>El 5% de descuento se aplicará automáticamente</li>
            </ol>
            <p>¿Alguna duda? Solo responde este correo — ¡nuestro equipo está listo para ayudarte!</p>
            <p>¡Hasta pronto en Orlando! 🌴</p>
            {FOOTER}
        </div>"""

    else:  # pt (default)
        subject = 'Allycar | Seu cupom de 5% de desconto 🎉'
        coupon  = COUPON_BLOCK.format(
            label='Seu cupom exclusivo',
            discount='5% de desconto na sua primeira reserva'
        )
        html = f"""
        <div style="font-family:Arial,sans-serif;color:#333;max-width:600px;">
            <p>Olá, {name}!</p>
            <p>Que ótimo ter você aqui! Aqui está o seu cupom de boas-vindas:</p>
            {coupon}
            <p>Para usar o cupom, basta:</p>
            <ol>
                <li>Acesse <a href="https://www.allycar.com" style="color:#c9a84c;">www.allycar.com</a></li>
                <li>Escolha o veículo e as datas da sua viagem</li>
                <li>Digite o código <strong>MYFIRSTBOOKING</strong> no campo de cupom</li>
                <li>O desconto de 5% será aplicado automaticamente</li>
            </ol>
            <p>Qualquer dúvida, é só responder este e-mail — nossa equipe está pronta para te ajudar!</p>
            <p>Até breve em Orlando! 🌴</p>
            {FOOTER}
        </div>"""

    return subject, html

@app.route('/trigger-send', methods=['GET', 'POST'])
def trigger_send():
    """Disparar envio de mensagens manualmente"""
    try:
        from main import processar_leads
        processar_leads()
        return {'status': 'success', 'message': 'Envio iniciado!'}, 200
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500


## ======================================================
## Criacao direta de cliente e reserva pelas APIs da HQ
## ======================================================

HQ_BASE    = 'api-america-miami.caagcrm.com'
HQ_PATH    = '/api-america-miami'
HQ_API_BASE  = 'https://api-america-miami.caagcrm.com/api-america-miami'
HQ_API_TOKEN = 'Basic YzQzMlR2elRSbFdxMGlJNldUeEFGM1lvUjBqcjVkV2dxRWJ0NGs2TlFTZzhZbmd0RWg6NXVhQjZTWEdGNU1zTk40RExrd29wVTBuZ2RURVpGeHBNb0l4RnZZRHBveGRjaUgxZnA='
ALLOWED_ORIGIN = 'https://www.allycar.com'

# Origens permitidas para os proxies HQ (www + apex + dev).
# Antes o CORS era fixo em www.allycar.com; quem abria o site SEM "www"
# (ex.: https://allycar.com) tomava "Failed to fetch" no browser.
HQ_ALLOWED_ORIGINS = {
    'https://www.allycar.com',
    'https://allycar.com',
    'http://localhost:4321',
    'http://localhost:4323',
}

# ── Cabeçalhos CORS reutilizáveis ──────────────────────────────────────────
def _cors(response):
    origin = request.headers.get('Origin', '')
    response.headers['Access-Control-Allow-Origin']  = origin if origin in HQ_ALLOWED_ORIGINS else ALLOWED_ORIGIN
    response.headers['Vary'] = 'Origin'
    response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# ── 1. Criar contato + retornar contact_id ────────────────────────────────
# =====================================
# SUNNY STORAGE — landing de UM veículo só (allycar.com/sunnystorage)
# O veículo, a brand e o local são FORÇADOS aqui no servidor. O navegador
# manda esses campos, mas eles são IGNORADOS: sem isso, qualquer um com o
# DevTools trocaria o vehicle_class_id e reservaria um Escalade pelo preço
# da van. Mesma proteção que já é usada nas vans do transfer.
# =====================================
SUNNY_VEHICLE_CLASS_ID = os.getenv("SUNNY_VEHICLE_CLASS_ID", "20")  # Ford Transit 250 Cargo Van (key 0039, $159/dia)
SUNNY_BRAND_ID         = os.getenv("SUNNY_BRAND_ID", "1")
SUNNY_LOCATION_ID      = os.getenv("SUNNY_LOCATION_ID", "5")

def _sunny_forced(data, rota):
    """Devolve (brand_id, location_id, vehicle_class_id) forçados e loga tentativa de troca."""
    enviado = str(data.get('vehicle_class_id') or '')
    if enviado and enviado != str(SUNNY_VEHICLE_CLASS_ID):
        print(f"⚠️ [{rota}] vehicle_class_id do cliente ({enviado}) IGNORADO — forçando {SUNNY_VEHICLE_CLASS_ID}")
    return SUNNY_BRAND_ID, SUNNY_LOCATION_ID, SUNNY_VEHICLE_CLASS_ID


@app.route('/api/hq/create-contact', methods=['POST', 'OPTIONS'])
def hq_create_contact():
    """Proxy: cria contato via /car-rental/reservations/customer (multipart)."""
 
    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())
 
    try:
        import http.client, json
        from urllib.parse import urlencode
 
        data = request.get_json()
        print(f"[create-contact] payload recebido: {data}")

        _s_brand, _s_loc, _s_class = _sunny_forced(data, 'create-contact')

        # Campos confirmados via curl --form (multipart/form-data)
        # field_254 = DL Number (campo customizado da conta)
        fields = {
            'contact_entity': 'person',
            'first_name':     data.get('first_name', ''),
            'last_name':      data.get('last_name', ''),
            'email':          data.get('email', ''),
            'phone_number':   data.get('phone_number', ''),
            'birthdate':      data.get('birthdate', ''),
            'field_254':      data.get('license_number', ''),
            'pick_up_date':   data.get('pick_up_date', ''),
            'return_date':    data.get('return_date', ''),
            'pick_up_location': _s_loc,
            'return_location':  _s_loc,
            'brand_id':         _s_brand,
            'vehicle_class_id': _s_class,
        }
 
        # Monta multipart manualmente
        boundary = 'HQBoundary1234567890'
        body_parts = []
        for key, value in fields.items():
            if value:
                body_parts.append(
                    f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f'{value}'
                )
        body = '\r\n'.join(body_parts) + f'\r\n--{boundary}--'
        body_bytes = body.encode('utf-8')
 
        print(f"[create-contact] enviando para HQ...")
 
        conn = http.client.HTTPSConnection(HQ_BASE)
        conn.request(
            'POST',
            f'{HQ_PATH}/car-rental/reservations/customer',
            body_bytes,
            {
                'Authorization': HQ_API_TOKEN,
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Content-Length': str(len(body_bytes)),
            }
        )
        res = conn.getresponse()
        resp_text = res.read().decode('utf-8')
        resp_status = res.status
 
        print(f"[create-contact] resposta HQ: {resp_status} | {resp_text[:500]}")
 
        try:
            resp_json = json.loads(resp_text)
        except Exception:
            resp_json = {'error': resp_text}
 
        # Normaliza retorno para { contact: { id: X } }
        contact_id = (
            resp_json.get('data', {}).get('contact_id') or
            resp_json.get('contact', {}).get('id') if resp_json.get('contact') else None
        )
 
        if contact_id:
            normalized = {'contact': {'id': contact_id}, 'original': resp_json}
        else:
            normalized = resp_json
 
        response = app.response_class(
            response=json.dumps(normalized),
            status=resp_status,
            mimetype='application/json'
        )
        return _cors(response)
 
    except Exception as e:
        print(f'[create-contact] erro: {e}')
        response = app.response_class(
            response=json.dumps({'success': False, 'message': str(e)}),
            status=500,
            mimetype='application/json'
        )
        return _cors(response)

# ── 2. Criar reserva ───────────────────────────────────────────────────────
@app.route('/api/hq/_diag', methods=['GET'])
def hq_diag():
    """Diagnóstico: mostra a config de pagamento em uso SEM criar reserva.
    Serve pra saber se o deploy do Railway já pegou a versão nova."""
    return _cors(app.response_class(
        response=json.dumps({
            'build':               'gateway-fix-2',
            'payment_method_id':   HQ_STRIPE_PAYMENT_METHOD_ID,
            'payment_gateway_id':  HQ_PAYMENT_GATEWAY_ID,
            'sunny_vehicle_class': SUNNY_VEHICLE_CLASS_ID,
        }),
        status=200, mimetype='application/json'))


@app.route('/api/hq/create-reservation', methods=['POST', 'OPTIONS'])
def hq_create_reservation():
    """Proxy: cria reserva na HQ Rental evitando CORS no browser."""

    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())

    try:
        data = request.get_json()

        _s_brand, _s_loc, _s_class = _sunny_forced(data, 'create-reservation')

        params = {
            'pick_up_date':                   data.get('pick_up_date'),
            'return_date':                    data.get('return_date'),
            'pick_up_time':                   data.get('pick_up_time'),
            'return_time':                    data.get('return_time'),
            'brand_id':                       _s_brand,
            'pick_up_location':               _s_loc,
            'return_location':                _s_loc,
            'vehicle_class_id':               _s_class,
            'customer_id':                    data.get('customer_id'),
            'customer_first_name':            data.get('customer_first_name'),
            'customer_last_name':             data.get('customer_last_name'),
            'customer_email':                 data.get('customer_email'),
            'customer_birthdate':             data.get('customer_birthdate'),
            'customer_driver_license_number': data.get('customer_driver_license_number'),
            'additional_charges[]':           '',
            # Self-service: pedimos à HQ o link de pagamento (Stripe) junto da reserva.
            # A reserva nasce aguardando pagamento; a própria HQ cancela se não for paga.
            'return_payment_link':            'true',
        }
        if HQ_STRIPE_PAYMENT_METHOD_ID:
            params['payment_method_id'] = HQ_STRIPE_PAYMENT_METHOD_ID
        if HQ_PAYMENT_GATEWAY_ID:
            # sem isto a HQ cai no gateway BNPL e o cliente não vê cartão
            params['gateway_id']         = HQ_PAYMENT_GATEWAY_ID
            params['payment_gateway_id'] = HQ_PAYMENT_GATEWAY_ID

        # Remove chaves com valor None para não poluir a URL
        params = {k: v for k, v in params.items() if v is not None}

        resp = requests.post(
            f'{HQ_API_BASE}/car-rental/reservations/confirm',
            headers={'Authorization': HQ_API_TOKEN},
            params=params,
            timeout=15
        )

        # A HQ devolve o link do Stripe em data.transaction.payment_link
        # (mesmos fallbacks usados em /api/transfer/confirm).
        try:
            _j = resp.json()
        except Exception:
            _j = None
        payment_link = None
        if isinstance(_j, dict):
            _cd = _j.get('data') or {}
            if isinstance(_cd, dict):
                payment_link = (
                    (_cd.get('transaction') or {}).get('payment_link')
                    or _cd.get('payment_link')
                    or ((_cd.get('payment') or {}).get('link'))
                )
            payment_link = payment_link or _j.get('payment_link')

        if resp.status_code in (200, 201):
            if payment_link:
                print(f'[create-reservation] payment_link obtido: {payment_link}')
            else:
                print('⚠️ [create-reservation] reserva criada SEM payment_link — '
                      'cliente cai na tela de fallback e o pagamento fica manual')
                # Diagnóstico: sem o corpo inteiro não dá pra saber ONDE a HQ pôs o link
                # (ou por que não pôs). Logamos as chaves e o corpo cru.
                if isinstance(_j, dict):
                    _dd = _j.get('data') or {}
                    print(f'   ↳ chaves de data: {list(_dd.keys()) if isinstance(_dd, dict) else type(_dd).__name__}')
                    if isinstance(_dd, dict) and isinstance(_dd.get('transaction'), dict):
                        print(f'   ↳ chaves de data.transaction: {list(_dd["transaction"].keys())}')
                print(f'   ↳ corpo cru da HQ: {resp.text[:2000]}')

        # ================================
        # ENVIO DE EMAIL (SOMENTE SUCESSO)
        # ================================
        if resp.status_code in (200, 201):
            try:
                destinatarios = [
                    "higor@allycar.com",
                    "david@allycar.com"
                ]

                _pgto = (
                    f"Link de pagamento gerado — cliente redirecionado ao Stripe:\n{payment_link}\n\n"
                    "A reserva fica AGUARDANDO PAGAMENTO. Se o cliente não pagar,\n"
                    "a própria HQ cancela. Nada a fazer manualmente."
                    if payment_link else
                    "⚠️ ATENÇÃO: a HQ NÃO devolveu link de pagamento.\n"
                    "O cliente viu a tela de fallback — cobrança precisa ser feita MANUALMENTE."
                )

                conteudo = f"""
Nova reserva criada 🚐 (self-service Sunny Storage)

{_pgto}

Cliente:
Nome: {data.get('customer_first_name')} {data.get('customer_last_name')}
Email: {data.get('customer_email')}

Reserva:
Pick-up: {data.get('pick_up_date')} às {data.get('pick_up_time')}
Return: {data.get('return_date')} às {data.get('return_time')}

Local:
Pick-up location ID: {_s_loc}
Return location ID: {_s_loc}

Veículo:
Class ID: {_s_class} (Ford Transit 250 Cargo Van)

Documento:
CNH: {data.get('customer_driver_license_number')}
Nascimento: {data.get('customer_birthdate')}
"""

                email_resp = requests.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "from": "Allycar <booking@allycar.com>",
                        "to": destinatarios,
                        "subject": (
                            f"{'💳' if payment_link else '⚠️'} Reserva VAN Sunny Storage: "
                            f"{data.get('customer_first_name')} {data.get('customer_last_name')}"
                            f"{'' if payment_link else ' — SEM LINK DE PAGAMENTO'}"
                        ),
                        "text": conteudo
                    },
                    timeout=10
                )

                if email_resp.status_code in (200, 201):
                    print("✅ Email de reserva enviado")
                else:
                    print(f"⚠️ Falha ao enviar email: {email_resp.status_code} - {email_resp.text}")

            except Exception as e:
                print(f"⚠️ Erro ao enviar email (ignorado): {e}")

        # Devolve o corpo original da HQ + payment_link no topo, para o site
        # redirecionar o cliente ao Stripe. Se o corpo não for JSON, passa cru
        # (o front já sabe lidar com os campos de erro da HQ).
        if isinstance(_j, dict):
            _out = dict(_j)
            _out['payment_link'] = payment_link
            response = app.response_class(
                response=json.dumps(_out),
                status=resp.status_code,
                mimetype='application/json'
            )
        else:
            response = app.response_class(
                response=resp.text,
                status=resp.status_code,
                mimetype='application/json'
            )
        return _cors(response)

    except Exception as e:
        print(f'❌ Erro proxy create-reservation: {e}')
        response = app.response_class(
            response=f'{{"success":false,"message":"{str(e)}"}}',
            status=500,
            mimetype='application/json'
        )
        return _cors(response)

# =====================================
# TRANSFER — RESERVA DAS VANS (chauffeur) VIA HQ RENTAL
# As vans ficam numa vehicle class própria na HQ. Estes endpoints
# FORÇAM essa classe no servidor: o site de transfer nunca consegue
# reservar outra categoria. Pagamento sai pelo Stripe configurado na HQ
# (o /confirm devolve o payment_link).
# =====================================
HQ_VANS_VEHICLE_CLASS_ID    = os.getenv("HQ_VANS_VEHICLE_CLASS_ID", "19")   # Mercedes Sprinter 2500
HQ_VANS_BRAND_ID            = os.getenv("HQ_VANS_BRAND_ID", HQ_BRAND_ID)
# Location 7 ("Office - For chauffeur") tem regra de imposto própria e NÃO
# aplica Sales Tax — é onde as reservas do transfer são registradas. O ponto de
# retirada escolhido pelo cliente é gravado em pick_up_location_custom.
HQ_VANS_LOCATION_ID         = os.getenv("HQ_VANS_LOCATION_ID", "7")
# ⚠️ Este campo do /reservations/confirm pede o ID do MÉTODO de pagamento,
# NÃO o ID do gateway. Ficou "4" por muito tempo por causa dessa confusão:
#   método  4 = "Klarna"      -> gateway 2 "Allycar (Buy Now Pay Later)"  (geração ANTIGA)
#   método 11 = "Credit Card" -> gateway 4 "Allycar (Online) new"         (geração NOVA)
# A brand AllyCar só autoriza os gateways 4/5/6 (supported_gateways), então o
# método 4 caía num gateway não autorizado: a HQ criava a reserva (200) e
# ignorava o pagamento — sem transação e sem payment_link.
# Nome da env var mudou de propósito: se sobrou HQ_STRIPE_PAYMENT_METHOD_ID=4
# no Railway, ela não envenena mais o valor.
#
# 🚨 O método TEM que pertencer ao gateway. Havia (e pode haver ainda) uma env var
# HQ_PAYMENT_METHOD_ID=12 no Railway — 12 é "Affirm", do gateway 5 — e ela vencia o
# código, jogando o checkout inteiro pro BNPL. Por isso o método não é mais lido cru
# da env: ele é derivado do gateway, e uma env divergente é ignorada com aviso.
_CARD_METHOD_BY_GATEWAY = {"4": "11"}   # gateway 4 "Allycar (Online) new" -> method 11 "Credit Card"
_env_method = os.getenv("HQ_PAYMENT_METHOD_ID")

# ⚠️ SÓ o método NÃO basta. Sem o gateway explícito a HQ escolhe um default —
# e o default dela é o gateway 5 ("Buy Now Pay Later new"), o que faz o checkout
# abrir só com Affirm/Klarna/Afterpay e NENHUMA opção de cartão.
# Comprovado: reserva 361 (sem gateway) -> gw 5 / Affirm;
#             reserva 362 (com gateway) -> gw 4 / Credit Card.
# Mandamos os dois nomes porque a HQ não documenta qual reconhece e ignorar
# parâmetro desconhecido é inofensivo (foi o que ela fez com payment_method_id).
HQ_PAYMENT_GATEWAY_ID = os.getenv("HQ_PAYMENT_GATEWAY_ID", "4")   # "Allycar (Online) new"

_expected_method = _CARD_METHOD_BY_GATEWAY.get(HQ_PAYMENT_GATEWAY_ID)
if _expected_method and _env_method and _env_method != _expected_method:
    print(f"⚠️ [boot] HQ_PAYMENT_METHOD_ID={_env_method} NÃO pertence ao gateway "
          f"{HQ_PAYMENT_GATEWAY_ID} (cartão = {_expected_method}). Env var IGNORADA — "
          f"apague-a no Railway pra evitar confusão.")
HQ_STRIPE_PAYMENT_METHOD_ID = _expected_method or _env_method or "11"
print(f"[boot] pagamento HQ -> gateway={HQ_PAYMENT_GATEWAY_ID} method={HQ_STRIPE_PAYMENT_METHOD_ID} "
      f"(esperado: gateway 4 / method 11 = Credit Card)")

# Origens permitidas para o fluxo de reserva (inclui localhost p/ dev).
TRANSFER_ALLOWED_ORIGINS = {
    "https://www.allycar.com",
    "https://allycar.com",
    "http://localhost:4321",
    "http://localhost:4322",
    "http://localhost:4323",
    "http://localhost:4324",
    "http://localhost:4325",
}

def _cors_transfer(response):
    origin = request.headers.get("Origin", "")
    response.headers["Access-Control-Allow-Origin"] = origin if origin in TRANSFER_ALLOWED_ORIGINS else ALLOWED_ORIGIN
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

def _json_resp(payload, status=200):
    return _cors_transfer(app.response_class(
        response=json.dumps(payload), status=status, mimetype="application/json"))

def _fmt_usd_hq(value):
    """Formata float no estilo da HQ: 1599.63 -> '$1.599,63' (ponto milhar, vírgula decimal)."""
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"${s}"


@app.route('/api/transfer/availability', methods=['POST', 'OPTIONS'])
def transfer_availability():
    """Preço (por hora/diária) da van via `additional-charges`.

    IMPORTANTE: usamos `additional-charges` (e não `reservations/dates`) porque
    a van fica com `available_on_website=False` na HQ — assim ela NÃO aparece no
    widget do site normal (mesma brand), mas continua reservável direto pela
    classe (HQ_VANS_VEHICLE_CLASS_ID). `additional-charges` devolve o preço por
    hora ($250) ou diária ($1.500, até 8h) já calculado pela HQ.
    """
    if request.method == 'OPTIONS':
        return _cors_transfer(app.make_default_options_response())
    try:
        data = request.get_json(force=True) or {}
        params = {
            "pick_up_date":     data.get("pick_up_date"),
            "return_date":      data.get("return_date"),
            "pick_up_time":     data.get("pick_up_time", HQ_DEFAULT_TIME),
            "return_time":      data.get("return_time", HQ_DEFAULT_TIME),
            "brand_id":         HQ_VANS_BRAND_ID,
            "pick_up_location": HQ_VANS_LOCATION_ID,
            "return_location":  HQ_VANS_LOCATION_ID,
            "vehicle_class_id": HQ_VANS_VEHICLE_CLASS_ID,
        }
        r = requests.get(
            f"{HQ_API_BASE}/car-rental/reservations/additional-charges",
            headers={"Authorization": HQ_API_TOKEN},
            params=params, timeout=20,
        )
        if r.status_code != 200:
            return _json_resp({"available": False, "error": f"HQ {r.status_code}", "detail": r.text[:300]}, 502)

        d        = r.json().get("data", {}) or {}
        svc      = d.get("selected_vehicle_class") or {}
        price    = svc.get("price", {}) or {}
        base     = price.get("base_price", {}) or {}
        # Total que o cliente realmente paga = base + cobranças obrigatórias + impostos.
        grand    = price.get("total_price_with_mandatory_charges_and_taxes", {}) or {}
        fallback = price.get("base_price_with_taxes", {}) or {}
        det      = (price.get("details") or [{}])[0]

        if not svc or not base.get("amount_for_display"):
            return _json_resp({"available": False, "reason": "no_price"}, 200)

        # "Taxes & fees" = total - base (sempre fecha com o total; engloba o
        # Florida Sales Tax 6.5% + cobranças obrigatórias). Mostrar uma linha de
        # imposto "exata" não fecharia, pois a HQ taxa base+cobranças.
        total_disp = grand.get("amount_for_display") or fallback.get("amount_for_display")
        total_raw  = grand.get("amount") or fallback.get("amount")
        fees_disp = None
        try:
            fees_raw = round(float(total_raw) - float(base.get("amount") or 0), 2)
            if fees_raw > 0:
                fees_disp = _fmt_usd_hq(fees_raw)
        except (TypeError, ValueError):
            pass

        return _json_resp({
            "available":        True,
            "vehicle_class_id": svc.get("vehicle_class_id"),
            "label":            "Mercedes-Benz Sprinter",
            "hours":            det.get("hours"),
            "days":             det.get("days"),
            "price_base":       base.get("amount_for_display"),
            "price_fees":       fees_disp,
            "price_total":      total_disp,
            "price_total_raw":  total_raw,
        }, 200)
    except Exception as e:
        print(f"[transfer/availability] erro: {e}")
        return _json_resp({"available": False, "error": str(e)}, 500)


@app.route('/api/transfer/customer', methods=['POST', 'OPTIONS'])
def transfer_customer():
    """Cria o contato na HQ (classe = vans). License/nascimento opcionais."""
    if request.method == 'OPTIONS':
        return _cors_transfer(app.make_default_options_response())
    import http.client, json
    try:
        data = request.get_json(force=True) or {}
        fields = {
            'contact_entity':   'person',
            'first_name':       data.get('first_name', ''),
            'last_name':        data.get('last_name', ''),
            'email':            data.get('email', ''),
            'phone_number':     data.get('phone_number', ''),
            'birthdate':        data.get('birthdate', ''),
            'pick_up_date':     data.get('pick_up_date', ''),
            'return_date':      data.get('return_date', ''),
            'pick_up_location': HQ_VANS_LOCATION_ID,
            'return_location':  HQ_VANS_LOCATION_ID,
            'brand_id':         HQ_VANS_BRAND_ID,
            'vehicle_class_id': HQ_VANS_VEHICLE_CLASS_ID,
        }
        # A HQ exige DL Number (field_254) para criar o contato. No transfer o
        # cliente NÃO dirige (motorista é da Allycar), então mandamos um
        # placeholder quando não vier do formulário.
        fields['field_254'] = data.get('license_number') or 'CHAUFFEUR-SERVICE'

        boundary = 'HQBoundary1234567890'
        body_parts = []
        for key, value in fields.items():
            if value:
                body_parts.append(
                    f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                    f'{value}'
                )
        body_bytes = ('\r\n'.join(body_parts) + f'\r\n--{boundary}--').encode('utf-8')

        conn = http.client.HTTPSConnection(HQ_BASE)
        conn.request('POST', f'{HQ_PATH}/car-rental/reservations/customer', body_bytes, {
            'Authorization':  HQ_API_TOKEN,
            'Content-Type':   f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(len(body_bytes)),
        })
        res = conn.getresponse()
        resp_text = res.read().decode('utf-8')
        try:
            resp_json = json.loads(resp_text)
        except Exception:
            resp_json = {'error': resp_text}

        # A HQ devolve o id do cliente em data.customer.id (e também em
        # data.reservation.customer_id). NÃO existe data.contact_id aqui.
        _data = resp_json.get('data', {}) or {}
        contact_id = (
            (_data.get('customer') or {}).get('id')
            or (_data.get('reservation') or {}).get('customer_id')
            or _data.get('contact_id')
        )
        out = {'contact': {'id': contact_id}, 'original': resp_json} if contact_id else resp_json
        return _cors_transfer(app.response_class(
            response=json.dumps(out), status=res.status, mimetype='application/json'))
    except Exception as e:
        print(f'[transfer/customer] erro: {e}')
        return _json_resp({'success': False, 'message': str(e)}, 500)


@app.route('/api/transfer/confirm', methods=['POST', 'OPTIONS'])
def transfer_confirm():
    """Confirma a reserva da van e devolve o payment_link (Stripe via HQ)."""
    if request.method == 'OPTIONS':
        return _cors_transfer(app.make_default_options_response())
    try:
        data = request.get_json(force=True) or {}
        params = {
            'pick_up_date':       data.get('pick_up_date'),
            'return_date':        data.get('return_date'),
            'pick_up_time':       data.get('pick_up_time'),
            'return_time':        data.get('return_time'),
            'brand_id':           HQ_VANS_BRAND_ID,
            'pick_up_location':   HQ_VANS_LOCATION_ID,
            'return_location':    HQ_VANS_LOCATION_ID,
            'vehicle_class_id':   HQ_VANS_VEHICLE_CLASS_ID,
            'customer_id':        data.get('customer_id'),
            'customer_first_name': data.get('customer_first_name'),
            'customer_last_name':  data.get('customer_last_name'),
            'customer_email':      data.get('customer_email'),
            'customer_birthdate':  data.get('customer_birthdate'),
            'additional_charges[]': '',
            'return_payment_link': 'true',
        }
        if HQ_STRIPE_PAYMENT_METHOD_ID:
            params['payment_method_id'] = HQ_STRIPE_PAYMENT_METHOD_ID
        if HQ_PAYMENT_GATEWAY_ID:
            # sem isto a HQ cai no gateway BNPL e o cliente não vê cartão
            params['gateway_id']         = HQ_PAYMENT_GATEWAY_ID
            params['payment_gateway_id'] = HQ_PAYMENT_GATEWAY_ID
        # DL Number obrigatório na HQ; placeholder pois o cliente não dirige.
        params['customer_driver_license_number'] = data.get('customer_driver_license_number') or 'CHAUFFEUR-SERVICE'
        params = {k: v for k, v in params.items() if v is not None}

        resp = requests.post(
            f'{HQ_API_BASE}/car-rental/reservations/confirm',
            headers={'Authorization': HQ_API_TOKEN},
            params=params,
            timeout=20,
        )
        try:
            j = resp.json()
        except Exception:
            j = {'raw': resp.text}
        # A HQ devolve o link do Stripe em data.transaction.payment_link.
        _cd = j.get('data', {}) or {}
        payment_link = (
            (_cd.get('transaction') or {}).get('payment_link')
            or _cd.get('payment_link')
            or j.get('payment_link')
            or ((_cd.get('payment') or {}).get('link'))
        )

        if resp.status_code in (200, 201):
            try:
                requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                             "Content-Type": "application/json"},
                    json={
                        "from": "Allycar <booking@allycar.com>",
                        "to": ["higor@allycar.com", "david@allycar.com"],
                        "subject": f"🚐 Nova reserva VAN/transfer: {data.get('customer_first_name')} {data.get('customer_last_name')}",
                        "text": (
                            f"Pick-up: {data.get('pick_up_date')} {data.get('pick_up_time')}\n"
                            f"Return: {data.get('return_date')} {data.get('return_time')}\n"
                            f"Endereço de retirada: {data.get('pickup_address') or '—'}\n"
                            f"Cliente: {data.get('customer_first_name')} {data.get('customer_last_name')} "
                            f"- {data.get('customer_email')}\n"
                            f"Payment link: {payment_link}"
                        ),
                    },
                    timeout=10,
                )
            except Exception as e:
                print(f"[transfer/confirm] email ignorado: {e}")

        return _json_resp(
            {'status': resp.status_code, 'payment_link': payment_link, 'reservation': j},
            resp.status_code or 200,
        )
    except Exception as e:
        print(f'[transfer/confirm] erro: {e}')
        return _json_resp({'success': False, 'message': str(e)}, 500)


# =====================================
# BRAZABANK — PIX + CARTÃO (Checkout v2, fluxo COM documento)
# Chamado pelo booking-confirmation.html. A lógica de API está em braza.py;
# aqui só orquestramos + CORS. Nada roda até as env vars BRAZA_* existirem.
# =====================================
import json as _bjson


def _braza_json(payload, status=200):
    return _cors(app.response_class(
        response=_bjson.dumps(payload), status=status, mimetype='application/json'))


def _braza_fail(e, status=502):
    """Erro de chamada à Braza -> JSON limpo p/ o front (sem stack)."""
    resp = getattr(e, 'response', None)
    if resp is not None:
        try:
            body = resp.json()
        except Exception:
            body = {'error': (resp.text or '')[:300]}
        return _braza_json({'ok': False, 'braza': body}, resp.status_code)
    print(f'[braza] erro: {e}')
    return _braza_json({'ok': False, 'error': str(e)}, status)


@app.route('/api/braza/quote', methods=['POST', 'OPTIONS'])
def braza_quote():
    """Cota US$->BRL e valida o CPF. Front usa p/ montar PIX e parcelas."""
    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())
    try:
        data = request.get_json() or {}
        amount_usd = data.get('amount_usd') or data.get('amount')
        order_ref  = data.get('order_ref') or data.get('orderRef') or data.get('external_id')
        cpf        = (data.get('cpf') or '').strip()
        if not amount_usd or not order_ref:
            return _braza_json({'ok': False, 'error': 'amount_usd e order_ref são obrigatórios'}, 400)
        quote = braza.create_quote(amount_usd, order_ref)
        client_status = braza.validate_client(cpf) if cpf else None
        return _braza_json({'ok': True, 'quote': quote, 'client': client_status})
    except Exception as e:
        return _braza_fail(e)


@app.route('/api/braza/client/complete', methods=['POST', 'OPTIONS'])
def braza_client_complete():
    """Completa cadastro pendente do cliente (endereço via CEP + contato)."""
    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())
    try:
        data = request.get_json() or {}
        cpf = (data.get('cpf') or '').strip()
        cep = (data.get('cep') or '').strip()
        if not cpf or not cep:
            return _braza_json({'ok': False, 'error': 'cpf e cep são obrigatórios'}, 400)
        addr = braza.lookup_cep(cep)
        info = {
            'cep':          addr.get('cep', cep),
            'state':        addr.get('uf') or data.get('state', ''),
            'city':         addr.get('localidade') or data.get('city', ''),
            'code':         addr.get('ibge') or data.get('code', ''),
            'neighborhood': addr.get('bairro') or data.get('neighborhood', ''),
            # prefere o que o cliente informou (veio da HQ); CEP é o fallback
            'address':      data.get('address') or addr.get('logradouro', ''),
            'number':       data.get('number', ''),
            'complement':   data.get('complement', ''),
            'phone':        data.get('phone', ''),
            'email':        data.get('email', ''),
        }
        return _braza_json({'ok': True, 'client': braza.update_client(cpf, info)})
    except Exception as e:
        return _braza_fail(e)


@app.route('/api/braza/pix', methods=['POST', 'OPTIONS'])
def braza_pix():
    """Gera o PIX (QR + copia-e-cola) para uma cotação já criada."""
    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())
    try:
        data = request.get_json() or {}
        cod_quote    = data.get('cod_quote') or data.get('codQuote')
        cod_customer = data.get('cod_customer') or data.get('codCustomer')
        if not cod_quote or not cod_customer:
            return _braza_json({'ok': False, 'error': 'cod_quote e cod_customer são obrigatórios'}, 400)
        pix = braza.create_pix(cod_quote, cod_customer)
        # Vigia no SERVIDOR: avisa a equipe mesmo se o cliente fechar a página
        try:
            pix_id = pix.get('id') or pix.get('invoiceIdPix')
            if pix_id:
                threading.Thread(
                    target=_watch_pix_payment,
                    args=(pix_id, cod_quote),
                    daemon=True,
                ).start()
                print(f'[braza/pix] vigia de PIX iniciado (pix={pix_id} cod_quote={cod_quote})')
        except Exception as e:
            print(f'[braza/pix] não iniciou o vigia: {e}')
        return _braza_json({'ok': True, 'pix': pix})
    except Exception as e:
        return _braza_fail(e)


@app.route('/api/braza/cc-session', methods=['POST', 'OPTIONS'])
def braza_cc_session():
    """Cria a pré-sessão de cartão (fluxo advanced) e devolve a URL hospedada.
    A URL de pagamento é montada a partir do cod_quote (não de uuid)."""
    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())
    try:
        data = request.get_json() or {}
        cod_quote    = data.get('cod_quote') or data.get('codQuote')
        cod_customer = data.get('cod_customer') or data.get('codCustomer')
        installments = int(data.get('installments') or 1)
        brl_quantity = data.get('brl_quantity') or data.get('brlQuantity')
        if not cod_quote or not cod_customer or not brl_quantity:
            return _braza_json({'ok': False, 'error': 'cod_quote, cod_customer e brl_quantity são obrigatórios'}, 400)
        session = braza.create_cc_session(cod_quote, cod_customer, installments)
        cc_uuid = session.get('uuid') if isinstance(session, dict) else None
        url = braza.cc_payment_url(cc_uuid, brl_quantity, installments)
        # B2: inicia o vigia p/ disparar o e-mail quando o cartão for aprovado
        try:
            if cc_uuid:
                threading.Thread(
                    target=_watch_cc_payment,
                    args=(cc_uuid, cod_quote),
                    daemon=True,
                ).start()
        except Exception as e:
            print(f'[braza/cc-session] não iniciou o vigia: {e}')
        return _braza_json({'ok': True, 'session': session, 'payment_url': url})
    except Exception as e:
        return _braza_fail(e)


@app.route('/api/braza/status', methods=['GET', 'OPTIONS'])
def braza_status():
    """Status do pagamento: ?pix_id=... (PIX) ou ?cc_uuid=... (cartão)."""
    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())
    try:
        pix_id  = request.args.get('pix_id')
        cc_uuid = request.args.get('cc_uuid')
        if pix_id:
            return _braza_json({'ok': True, 'status': braza.pix_status(pix_id)})
        if cc_uuid:
            return _braza_json({'ok': True, 'status': braza.cc_status(cc_uuid)})
        return _braza_json({'ok': False, 'error': 'informe pix_id ou cc_uuid'}, 400)
    except Exception as e:
        return _braza_fail(e)


# Reconciliação na HQ. Precisa de 2 valores da conta HQ (ainda a confirmar):
HQ_PAYMENT_ITEM_TYPE = os.getenv('HQ_PAYMENT_ITEM_TYPE', 'car_rental.reservations')  # confirmado no step 6
HQ_PAYMENT_METHOD_ID = os.getenv('HQ_PAYMENT_METHOD_ID')   # id do método PIX/Braza na HQ (candidato: 12)


def _hq_register_payment(order_ref, amount, label):
    """Dá baixa do pagamento na reserva do HQ. Sem os env vars, faz no-op seguro."""
    if not (HQ_PAYMENT_ITEM_TYPE and HQ_PAYMENT_METHOD_ID and order_ref):
        print(f'[braza/webhook] HQ_PAYMENT_* não configurado — baixa MANUAL necessária p/ {order_ref}')
        return {'skipped': True}
    params = {
        'item_type':         HQ_PAYMENT_ITEM_TYPE,
        'item_id':           order_ref,
        'payment_method_id': HQ_PAYMENT_METHOD_ID,
        'amount':            amount,
        'label':             label,
        'description':       f'BrazaBank {label} - reserva {order_ref}',
    }
    resp = requests.post(
        f'{HQ_API_BASE}/payment-gateways/payment-transactions/',
        headers={'Authorization': HQ_API_TOKEN}, params=params, timeout=15)
    print(f'[braza/webhook] HQ payment-transactions: {resp.status_code} | {resp.text[:300]}')
    return {'status': resp.status_code}


_braza_notified = set()   # cod_quotes já avisados — evita e-mail duplicado


def _notify_team_payment(order_ref, status, amount_usd, method):
    """Avisa a equipe por e-mail (Resend) sobre a mudança de pagamento.
    amount_usd = sale.amount, que é o valor em USD (não BRL)."""
    status = (status or '').upper()

    if status == 'PAID':
        subject = f'✅ Pagamento CONFIRMADO ({method}) - reserva {order_ref} - AÇÃO em até 2h'
        body = (
            'Pagamento CONFIRMADO via BrazaBank.\n\n'
            f'Reserva HQ: {order_ref}\n'
            f'Status: {status}\n'
            f'Metodo: {method}\n'
            f'Valor USD: {amount_usd}\n\n'
            'Próximos passos (DEVE SER FEITO EM ATÉ 2 HORAS):\n\n'
            f'Acessar a reserva {order_ref} no sistema HQ, entrar no "step 6 - Payments" e inserir '
            'manualmente um pagamento no botão "Add Offline Payment" com o valor acima.\n\n'
            'Logo em seguida verificar se ficou algum Outstanding Balance, uma vez que para pagamentos '
            'como PIX oferecemos descontos por fora.\n\n'
            'Em caso da necessidade de ajustar o Balance, adicionar manualmente o desconto correspondente '
            'na seção "Discounts", logo abaixo, através do botão "Add Discount".'
        )
    else:
        subject = f'Pagamento {status} ({method}) - reserva {order_ref}'
        body = (
            f'Pagamento {status} via BrazaBank.\n\n'
            f'Reserva HQ: {order_ref}\nStatus: {status}\nMetodo: {method}\nValor USD: {amount_usd}\n\n'
            'Verifique a reserva na HQ, se necessário.'
        )

    try:
        requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f"Bearer {os.getenv('RESEND_API_KEY')}",
                     'Content-Type': 'application/json'},
            json={'from': 'Allycar <booking@allycar.com>',
                  'to': ['higor@allycar.com', 'bruno@allycar.com', 'david@allycar.com'],
                  'subject': subject,
                  'text': body},
            timeout=10)
    except Exception as e:
        print(f'[braza] e-mail ignorado: {e}')


def _watch_cc_payment(cc_uuid, cod_quote, expires_in=0):
    """
    B2 — 'vigia' do pagamento por cartão. Como no cartão o cliente sai da nossa
    página, o servidor consulta o status ele mesmo até aprovar e dispara o e-mail.
    Consulta cc_status(cc_uuid) (uuid da sessão); usa cod_quote p/ get_sale e dedup.
    Best-effort (thread não sobrevive a restart; a Braza não tem webhook — polling
    é o método oficial). Compartilha _braza_notified p/ não duplicar com o /confirm.
    """
    window = min((int(expires_in) if expires_in else 1800) + 60, 40 * 60)  # cap 40 min
    deadline = time.time() + window
    while time.time() < deadline:
        try:
            st = braza.cc_status(cc_uuid)
            if st.get('isApproved') is True:
                if cod_quote in _braza_notified:
                    return
                _braza_notified.add(cod_quote)
                try:
                    sale = braza.get_sale(cod_quote)
                except Exception:
                    sale = {}
                order_ref = sale.get('identifier') or cod_quote
                amount    = sale.get('amount')
                method    = sale.get('paymentMethod') or 'cartao'
                _notify_team_payment(order_ref, 'PAID', amount, method)
                print(f'[braza/cc-watch] aprovado e notificado (uuid={cc_uuid} cod_quote={cod_quote})')
                return
        except Exception as e:
            print(f'[braza/cc-watch] erro consultando status: {e}')
        time.sleep(7)
    print(f'[braza/cc-watch] encerrado sem aprovação (cod_quote={cod_quote})')


def _watch_pix_payment(pix_id, cod_quote, expires_in=0):
    """
    Vigia do PIX no SERVIDOR (espelha o do cartão). No PIX o cliente costuma pagar
    no app do banco e FECHAR a página — então não dá pra depender do polling da
    pagar-braza.html. Aqui o servidor consulta o status ele mesmo até PAID e dispara
    o e-mail. Best-effort (a thread não sobrevive a restart do serviço). Compartilha
    _braza_notified com o /confirm p/ não duplicar e-mail.
    """
    window = min((int(expires_in) if expires_in else 1800) + 60, 45 * 60)  # cap 45 min
    deadline = time.time() + window
    while time.time() < deadline:
        try:
            estado = str(braza.pix_status(pix_id).get('status', '')).upper()
            if estado == 'PAID':
                key = cod_quote or pix_id
                if key in _braza_notified:
                    return
                _braza_notified.add(key)
                try:
                    sale = braza.get_sale(cod_quote) if cod_quote else {}
                except Exception:
                    sale = {}
                order_ref = sale.get('identifier') or cod_quote or pix_id
                _notify_team_payment(order_ref, 'PAID', sale.get('amount'),
                                     sale.get('paymentMethod') or 'pix')
                print(f'[braza/pix-watch] pago e notificado (pix={pix_id} cod_quote={cod_quote})')
                return
            if estado in ('EXPIRED', 'REFUNDED', 'CANCELED', 'CANCELLED'):
                print(f'[braza/pix-watch] encerrado por status {estado} (pix={pix_id})')
                return
        except Exception as e:
            print(f'[braza/pix-watch] erro consultando status: {e}')
        time.sleep(10)
    print(f'[braza/pix-watch] janela encerrada sem pagamento (pix={pix_id})')


@app.route('/api/braza/confirm', methods=['POST', 'OPTIONS'])
def braza_confirm():
    """
    Interino (enquanto o webhook da Braza não está ativo): o front chama isto
    quando detecta o PIX pago. RE-VERIFICA o pagamento no servidor (não confia
    no cliente) e, se realmente pago, avisa a equipe por e-mail. A baixa na HQ
    segue MANUAL por enquanto.
    """
    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())
    try:
        data = request.get_json() or {}
        cod_quote = data.get('cod_quote') or data.get('codQuote')
        pix_id    = data.get('pix_id')
        if not cod_quote and not pix_id:
            return _braza_json({'ok': False, 'error': 'cod_quote ou pix_id obrigatório'}, 400)

        paid = False
        if pix_id:
            st = braza.pix_status(pix_id)
            paid = str(st.get('status', '')).upper() == 'PAID'
            cod_quote = cod_quote or st.get('codQuote')
        if cod_quote:
            sale = braza.get_sale(cod_quote)
            if sale.get('statusLabel') == 'success' or str(sale.get('statusName', '')).lower() in ('recebido', 'processado'):
                paid = True

        if not paid:
            return _braza_json({'ok': True, 'paid': False})

        key = cod_quote or pix_id
        if key in _braza_notified:
            return _braza_json({'ok': True, 'paid': True, 'already': True})
        _braza_notified.add(key)

        info = braza.get_sale(cod_quote) if cod_quote else {}
        _notify_team_payment(info.get('identifier'), 'PAID', info.get('amount'), info.get('paymentMethod', 'pix'))
        return _braza_json({'ok': True, 'paid': True, 'notified': True})
    except Exception as e:
        return _braza_fail(e)


@app.route('/api/braza/webhook', methods=['POST'])
def braza_webhook():
    """
    Notificação da Braza { codQuote, status }. Valida Basic token
    (env BRAZA_WEBHOOK_TOKEN), descobre a reserva via get_sale.identifier,
    avisa a equipe e registra o pagamento na HQ. Responder 2XX sempre p/
    a Braza não reenviar em loop; erros ficam no log.
    """
    expected = os.getenv('BRAZA_WEBHOOK_TOKEN')
    if expected and request.headers.get('Authorization', '') != f'Basic {expected}':
        return app.response_class(response='{"ok":false}', status=401, mimetype='application/json')
    try:
        data = request.get_json(force=True) or {}
        cod_quote = data.get('codQuote')
        status    = (data.get('status') or '').upper()
        print(f'[braza/webhook] codQuote={cod_quote} status={status}')
        if cod_quote and status in ('PAID', 'REFUNDED', 'EXPIRED'):
            sale = braza.get_sale(cod_quote)
            order_ref  = sale.get('identifier')
            amount_usd = sale.get('amount')   # sale.amount é o valor em USD
            method     = sale.get('paymentMethod', 'braza')
            if cod_quote not in _braza_notified:                          # dedup c/ o /confirm
                _braza_notified.add(cod_quote)
                _notify_team_payment(order_ref, status, amount_usd, method)
            if status == 'PAID':
                _hq_register_payment(order_ref, amount_usd, method)
    except Exception as e:
        print(f'[braza/webhook] erro: {e}')
    return app.response_class(response='{"ok":true}', status=200, mimetype='application/json')


# =========================================================================
# TRANSFER — PACOTE MULTI-SEGMENTO COM PAGAMENTO ÚNICO
#
# O cliente compra vários blocos avulsos (ex.: dia 11 = 2h, dia 13 = 3h,
# dia 14 = 1h). A HQ modela reserva como bloco contínuo, então cada bloco
# vira UMA reserva — mas o cliente paga UMA vez.
#
# Estratégia "autorizar primeiro" (authorize-first):
#   1. /pkg/quote     → precifica (sem efeito colateral)
#   2. /pkg/checkout  → cria Stripe Checkout com capture_method=manual.
#                       Nada é escrito na HQ ainda.
#   3. webhook Stripe → cliente autorizou (dinheiro ainda NÃO capturado):
#                       cria as N reservas, dá baixa em cada uma e SÓ ENTÃO
#                       captura. Se algum bloco falhar, captura apenas o que
#                       existe e libera o resto — nunca há estorno a fazer.
#
# Fatos da HQ comprovados em produção (ver DESIGN-multi-segmento.md):
#   - Qualquer bloco de até 8h é cobrado como 1 diária ($1.500). O preço é
#     ajustado com manual_discount, aplicado PRÉ-imposto:
#         total_hq = (1500 - desconto + 2) * 1.065
#     (o "+2" é uma taxa obrigatória oculta que não aparece na API)
#   - Baixa de pagamento exige RÓTULOS LITERAIS, não ids numéricos.
#     Valores numéricos são aceitos (200 OK) e NÃO liquidam nada.
#   - Só `referral` e `pick_up_location_custom` gravam texto livre.
#   - `created_at` da HQ é hora de Nova York carimbada como "Z".
# =========================================================================

import hashlib

try:
    import stripe as _stripe
except ImportError:                                    # nunca derruba o bot
    _stripe = None

STRIPE_SECRET_KEY     = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')
TRANSFER_SITE_URL     = os.getenv('TRANSFER_SITE_URL', 'https://www.allycar.com/transfer')

# --- Regra de preço (espelha src/lib/pricing.ts do site) ---
PKG_HOURLY_RATE   = 250
PKG_DAY_RATE      = 1500
PKG_DAILY_FROM_H  = 6      # 6h ou mais = diária
PKG_MAX_HOURS     = 8
PKG_MAX_BLOCKS    = 10
PKG_OPEN_HOUR     = 6
PKG_CLOSE_HOUR    = 23

# --- HQ ---
# Referência do que foi medido em produção (NÃO usado no cálculo: o preço da HQ
# é sempre lido ao vivo em _hq_quote_block, para sobreviver a mudanças de
# imposto/tarifa sem alterar código):
#   taxa obrigatória oculta = $2,00/dia   |   Sales Tax = 6,5%
#   1h é cobrada por hora; 2h ou mais viram 1 diária
HQ_PAY_METHOD_LABEL = os.getenv('HQ_OFFLINE_PAY_METHOD', 'Bank Transfer')


def _sv(obj, key, default=None):
    """Lê um campo de dict OU de objeto do Stripe.

    Na stripe-python 15 o StripeObject NÃO é dict: não tem .get e dict(obj)
    levanta KeyError. Só [] e getattr funcionam."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except Exception:
        return getattr(obj, key, default)


def _pi_log(pi):
    """Metadata do PaymentIntent como dict simples (não dá para converter direto)."""
    m = _sv(pi, 'metadata')
    if isinstance(m, dict):
        return dict(m)
    out = {}
    chaves = (['v', 'ord', 'state', 'cid', 'cap']
              + [f'r{i}' for i in range(PKG_MAX_BLOCKS)]
              + [f'e{i}' for i in range(PKG_MAX_BLOCKS)])
    for k in chaves:
        val = _sv(m, k)
        if val is not None:
            out[k] = val
    return out


def _pkg_price_block(hours):
    """Preço de um bloco: por hora até 5h, diária de 6h em diante."""
    h = max(1, min(int(hours), PKG_MAX_HOURS))
    return float(PKG_DAY_RATE) if h >= PKG_DAILY_FROM_H else float(h * PKG_HOURLY_RATE)


def _pkg_validate(segments):
    """Valida e normaliza os blocos. Devolve (lista_normalizada, erro)."""
    if not isinstance(segments, list) or not segments:
        return None, 'Informe pelo menos um bloco de serviço.'
    if len(segments) > PKG_MAX_BLOCKS:
        return None, f'Máximo de {PKG_MAX_BLOCKS} blocos por pacote.'
    out = []
    for s in segments:
        try:
            date  = str(s.get('date'))
            start = str(s.get('start'))
            hours = int(s.get('hours'))
        except (AttributeError, TypeError, ValueError):
            return None, 'Bloco inválido.'
        try:
            datetime.strptime(date, '%Y-%m-%d')
            sh = int(start.split(':')[0])
        except (ValueError, IndexError):
            return None, 'Data ou horário inválido.'
        if not (1 <= hours <= PKG_MAX_HOURS):
            return None, f'Cada bloco deve ter de 1 a {PKG_MAX_HOURS} horas.'
        if not (PKG_OPEN_HOUR <= sh <= PKG_CLOSE_HOUR):
            return None, 'Horário de início fora da janela de atendimento.'
        if sh + hours > 24:
            return None, 'O bloco não pode passar da meia-noite.'
        out.append({'date': date, 'start': f'{sh:02d}:00', 'hours': hours,
                    'end': f'{sh + hours:02d}:00', 'amount': _pkg_price_block(hours)})
    # sem sobreposição no mesmo dia
    by_day = {}
    for i, s in enumerate(out):
        for j, other in by_day.get(s['date'], []):
            a1, a2 = int(s['start'][:2]), int(s['end'][:2])
            b1, b2 = other
            if a1 < b2 and b1 < a2:
                return None, f'Os blocos {j + 1} e {i + 1} se sobrepõem no mesmo dia.'
        by_day.setdefault(s['date'], []).append((i, (int(s['start'][:2]), int(s['end'][:2]))))
    return out, None


def _pkg_quote(segments):
    segs, err = _pkg_validate(segments)
    if err:
        return None, err
    total = round(sum(s['amount'] for s in segs), 2)
    return {'segments': segs, 'total': total, 'tax': 0.0}, None


def _hq_quote_block(seg):
    """Preço que a HQ cobraria pelo bloco, SEM desconto.

    Nada é presumido: a HQ cobra 1h por hora e 2h+ como diária, tem uma taxa
    obrigatória oculta e aplica imposto por cima. Lemos tudo dela."""
    params = {
        'pick_up_date': seg['date'], 'return_date': seg['date'],
        'pick_up_time': seg['start'], 'return_time': seg['end'],
        'brand_id': HQ_VANS_BRAND_ID,
        'pick_up_location': HQ_VANS_LOCATION_ID, 'return_location': HQ_VANS_LOCATION_ID,
        'vehicle_class_id': HQ_VANS_VEHICLE_CLASS_ID,
    }
    r = requests.get(f'{HQ_API_BASE}/car-rental/reservations/additional-charges',
                     headers={'Authorization': HQ_API_TOKEN}, params=params, timeout=25)
    price = ((r.json().get('data') or {}).get('selected_vehicle_class') or {}).get('price') or {}
    base = float((price.get('base_price') or {}).get('amount') or 0)
    with_tax = float((price.get('base_price_with_taxes') or {}).get('amount') or 0)
    total = float((price.get('total_price_with_mandatory_charges_and_taxes') or {}).get('amount') or 0)
    if base <= 0 or total <= 0:
        raise RuntimeError('HQ não devolveu preço para o bloco (van indisponível?)')
    # multiplicador de imposto lido ao vivo — vira 1.0 sozinho quando a
    # location isenta entrar no ar, sem precisar mexer no código
    return total, (with_tax / base if base else 1.0)


def _hq_discount_for(hq_total, tax_mult, target_total):
    """Desconto (pré-imposto) que faz a HQ fechar exatamente no nosso preço."""
    return round((float(hq_total) - float(target_total)) / (tax_mult or 1.0), 2)


def _pkg_ord(email, segs, total):
    """Identificador determinístico do pedido: mesmo conteúdo = mesmo pedido.
       Sem nonce do navegador, para que F5 / segunda aba não gerem 2 cobranças."""
    canon = '|'.join(f"{s['date']}T{s['start']}x{s['hours']}" for s in segs)
    raw = f"{(email or '').strip().lower()}|{canon}|{int(round(total * 100))}"
    return 'TR-' + hashlib.sha256(raw.encode()).hexdigest()[:12].upper()


def _hq_create_contact(cust, first_date):
    """Cria (ou recria) o contato na HQ. field_254 é obrigatório lá."""
    fields = {
        'contact_entity': 'person',
        'first_name':   cust.get('first_name', ''),
        'last_name':    cust.get('last_name', ''),
        'email':        cust.get('email', ''),
        'phone_number': cust.get('phone_number', ''),
        'field_254':    'CHAUFFEUR-SERVICE',
        'pick_up_date': first_date, 'return_date': first_date,
        'pick_up_location': HQ_VANS_LOCATION_ID, 'return_location': HQ_VANS_LOCATION_ID,
        'brand_id': HQ_VANS_BRAND_ID, 'vehicle_class_id': HQ_VANS_VEHICLE_CLASS_ID,
    }
    boundary = 'HQPkgBoundary1234567890'
    body = '\r\n'.join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}'
        for k, v in fields.items() if v
    ) + f'\r\n--{boundary}--'
    bb = body.encode('utf-8')
    conn = http.client.HTTPSConnection(HQ_BASE, timeout=20)
    conn.request('POST', f'{HQ_PATH}/car-rental/reservations/customer', bb, {
        'Authorization': HQ_API_TOKEN,
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'Content-Length': str(len(bb)),
    })
    data = json.loads(conn.getresponse().read().decode('utf-8')).get('data', {}) or {}
    return (data.get('customer') or {}).get('id') or (data.get('reservation') or {}).get('customer_id')


def _hq_create_block(seg, cust, customer_id, ord_token, index, pickup_address):
    """Cria a reserva de um bloco com o preço exato do pacote. Devolve o id."""
    hq_total, tax_mult = _hq_quote_block(seg)
    desconto = _hq_discount_for(hq_total, tax_mult, seg['amount'])
    params = {
        'pick_up_date': seg['date'], 'return_date': seg['date'],
        'pick_up_time': seg['start'], 'return_time': seg['end'],
        'brand_id': HQ_VANS_BRAND_ID,
        'pick_up_location': HQ_VANS_LOCATION_ID, 'return_location': HQ_VANS_LOCATION_ID,
        'vehicle_class_id': HQ_VANS_VEHICLE_CLASS_ID,
        'customer_id': customer_id,
        'customer_first_name': cust.get('first_name', ''),
        'customer_last_name':  cust.get('last_name', ''),
        'customer_email':      cust.get('email', ''),
        'customer_driver_license_number': 'CHAUFFEUR-SERVICE',
        'additional_charges[]': '',
        'manual_discount': str(desconto),
        'manual_discount_is_percentage': '0',
        # únicos campos de texto livre que a HQ realmente grava
        'referral': f'{ord_token}#{index}',
        'pick_up_location_custom': (pickup_address or '')[:250],
        # comentário: é o que a equipe vê de imediato na reserva. A location da
        # reserva é sempre a isenta ("For chauffeur"), então o ponto real de
        # retirada precisa estar visível aqui.
        'comments': (f'TRANSFER · Retirada: {pickup_address or "a combinar"} · '
                     f'{seg["start"]}–{seg["end"]} ({seg["hours"]}h) · '
                     f'USD {seg["amount"]:.2f} · pedido {ord_token} bloco {index + 1}')[:500],
    }
    r = requests.post(f'{HQ_API_BASE}/car-rental/reservations/confirm',
                      headers={'Authorization': HQ_API_TOKEN}, params=params, timeout=25)
    j = r.json()
    if not j.get('success'):
        raise RuntimeError(f"HQ confirm falhou: {json.dumps(j.get('errors'))[:200]}")
    rsv = (j.get('data') or {}).get('reservation', {}) or {}
    # confere que a HQ gravou o valor que vamos cobrar — nunca confiar no cálculo
    gravado = float((rsv.get('total_price') or {}).get('amount') or 0)
    if abs(gravado - float(seg['amount'])) > 0.02:
        _hq_cancel(rsv.get('id'))
        raise RuntimeError(f"preço divergente: HQ gravou {gravado}, esperado {seg['amount']}")
    return rsv.get('id')


def _hq_settle(reservation_id, amount, reference):
    """Dá baixa REAL do pagamento e CONFERE que o saldo baixou.

    A HQ aceita ids numéricos e devolve 200 sem liquidar nada — por isso
    usamos os rótulos literais e relemos a reserva para confirmar."""
    fields = {
        'field_31': HQ_PAY_METHOD_LABEL,   # método (rótulo literal)
        'field_32': datetime.now().strftime('%Y-%m-%d'),
        'field_34': f'{float(amount):.2f}',
        'field_37': 'Approved',            # status (rótulo literal)
        'field_29': 'payment',             # pagamento (não autorização)
        'field_35': (reference or '')[:80],
    }
    boundary = 'HQPayBoundary1234567890'
    body = '\r\n'.join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}'
        for k, v in fields.items() if v
    ) + f'\r\n--{boundary}--'
    bb = body.encode('utf-8')
    conn = http.client.HTTPSConnection(HQ_BASE, timeout=20)
    conn.request('POST', f'{HQ_PATH}/car-rental/reservations/{reservation_id}/payments', bb, {
        'Authorization': HQ_API_TOKEN,
        'Content-Type': f'multipart/form-data; boundary={boundary}',
        'Content-Length': str(len(bb)),
    })
    conn.getresponse().read()
    # confere de verdade — o status 200 não é prova de nada
    chk = requests.get(f'{HQ_API_BASE}/car-rental/reservations/{reservation_id}',
                       headers={'Authorization': HQ_API_TOKEN}, timeout=20)
    rsv = (chk.json().get('data') or {}).get('reservation', {}) or {}
    paid = float(rsv.get('total_paid') or 0)
    if paid <= 0:
        print(f'[pkg] ATENÇÃO: reserva {reservation_id} não liquidou (total_paid={paid})')
    return paid


def _hq_cancel(reservation_id):
    try:
        requests.post(f'{HQ_API_BASE}/car-rental/reservations/{reservation_id}/cancelled',
                      headers={'Authorization': HQ_API_TOKEN}, timeout=20)
    except Exception as e:
        print(f'[pkg] falha ao cancelar {reservation_id}: {e}')


@app.route('/api/transfer/pkg/locations', methods=['GET', 'OPTIONS'])
def pkg_locations():
    """Pontos de retirada — as locations da HQ marcadas como 'Show on website'.

    A reserva em si é sempre registrada em HQ_VANS_LOCATION_ID (a location
    isenta de imposto); isto aqui é só onde o motorista busca o cliente."""
    if request.method == 'OPTIONS':
        return _cors_transfer(app.make_default_options_response())
    try:
        r = requests.get(f'{HQ_API_BASE}/fleets/locations',
                         headers={'Authorization': HQ_API_TOKEN}, timeout=20)
        out = []
        for l in (r.json().get('fleets_locations') or []):
            if (str(l.get('show_on_website')).lower() == 'true'
                    and str(l.get('active')).lower() == 'true'
                    and str(l.get('pick_up_allowed')).lower() == 'true'):
                out.append({'id': str(l.get('id')),
                            'label': l.get('label_for_website_translated') or l.get('name'),
                            'order': l.get('order') or 99})
        out.sort(key=lambda x: (int(x['order'] or 99), x['label']))
        for o in out:
            o.pop('order', None)
        return _json_resp({'ok': True, 'locations': out}, 200)
    except Exception as e:
        print(f'[pkg/locations] erro: {e}')
        return _json_resp({'ok': False, 'locations': []}, 502)


@app.route('/api/transfer/pkg/quote', methods=['POST', 'OPTIONS'])
def pkg_quote():
    """Precifica o roteiro. Não cria nada em lugar nenhum."""
    if request.method == 'OPTIONS':
        return _cors_transfer(app.make_default_options_response())
    data = request.get_json(force=True, silent=True) or {}
    q, err = _pkg_quote(data.get('segments'))
    if err:
        return _json_resp({'ok': False, 'message': err}, 400)
    return _json_resp({'ok': True, **q}, 200)


@app.route('/api/transfer/pkg/_diag', methods=['GET'])
def pkg_diag():
    """Diagnóstico da integração Stripe (sem expor segredos)."""
    info = {
        'hq_vans_class': HQ_VANS_VEHICLE_CLASS_ID,
        'hq_vans_brand': HQ_VANS_BRAND_ID,
        'hq_vans_location': HQ_VANS_LOCATION_ID,
        'hq_pay_method_label': HQ_PAY_METHOD_LABEL,
        'stripe_lib': bool(_stripe),
        'stripe_version': getattr(_stripe, 'VERSION', None) if _stripe else None,
        'key_set': bool(STRIPE_SECRET_KEY),
        'key_prefix': (STRIPE_SECRET_KEY[:7] + '…') if STRIPE_SECRET_KEY else None,
        'webhook_secret_set': bool(STRIPE_WEBHOOK_SECRET),
        'site_url': TRANSFER_SITE_URL,
    }
    if _stripe and STRIPE_SECRET_KEY:
        _stripe.api_key = STRIPE_SECRET_KEY
        try:
            acct = _stripe.Account.retrieve()
            info['account'] = _sv(acct, 'id')
            info['charges_enabled'] = _sv(acct, 'charges_enabled')
        except Exception as e:
            info['account_error'] = f'{type(e).__name__}: {e}'[:300]
        try:
            s = _stripe.checkout.Session.create(
                mode='payment', payment_method_types=['card'],
                payment_intent_data={'capture_method': 'manual'},
                line_items=[{'quantity': 1, 'price_data': {
                    'currency': 'usd', 'unit_amount': 100,
                    'product_data': {'name': 'diag'}}}],
                success_url='https://www.allycar.com/transfer/confirmation',
                cancel_url='https://www.allycar.com/transfer/book',
            )
            info['session_ok'] = bool(_sv(s, 'url'))
            try:
                _stripe.checkout.Session.expire(_sv(s, 'id'))   # não deixa sessão órfã
                info['session_expired'] = True
            except Exception:
                pass
        except Exception as e:
            info['session_error'] = f'{type(e).__name__}: {e}'[:400]
        # endpoints de webhook configurados na conta
        try:
            eps = []
            for ep in _stripe.WebhookEndpoint.list(limit=10).data:
                eps.append({'url': _sv(ep, 'url'), 'status': _sv(ep, 'status'),
                            'events': list(_sv(ep, 'enabled_events') or [])[:6]})
            info['webhook_endpoints'] = eps
        except Exception as e:
            info['endpoints_error'] = f'{type(e).__name__}: {e}'[:200]
        # últimos eventos de checkout e se foram entregues
        try:
            evs = []
            for ev in _stripe.Event.list(limit=5, type='checkout.session.completed').data:
                evs.append({'id': _sv(ev, 'id'), 'created': _sv(ev, 'created'),
                            'pending_webhooks': _sv(ev, 'pending_webhooks')})
            info['recent_checkout_events'] = evs
        except Exception as e:
            info['events_error'] = f'{type(e).__name__}: {e}'[:200]
    return _json_resp(info, 200)


@app.route('/api/transfer/pkg/checkout', methods=['POST', 'OPTIONS'])
def pkg_checkout():
    """Recalcula o preço no servidor e devolve o Checkout do Stripe.
       capture_method=manual: o cliente autoriza, mas nada é cobrado ainda."""
    if request.method == 'OPTIONS':
        return _cors_transfer(app.make_default_options_response())
    if not (_stripe and STRIPE_SECRET_KEY):
        return _json_resp({'ok': False, 'message': 'Pagamento indisponível no momento.'}, 503)

    data = request.get_json(force=True, silent=True) or {}
    cust = data.get('customer') or {}
    if not all(cust.get(k) for k in ('first_name', 'last_name', 'email', 'phone_number')):
        return _json_resp({'ok': False, 'message': 'Preencha nome, e-mail e telefone.'}, 400)

    # o preço vem SEMPRE daqui — o navegador nunca é fonte de verdade
    q, err = _pkg_quote(data.get('segments'))
    if err:
        return _json_resp({'ok': False, 'message': err}, 400)

    segs, total = q['segments'], q['total']
    ord_token = _pkg_ord(cust['email'], segs, total)
    _stripe.api_key = STRIPE_SECRET_KEY

    try:
        # mesmo pedido reaberto (F5, 2ª aba) reaproveita a sessão em vez de cobrar de novo
        for s in _stripe.checkout.Session.list(limit=100).data:
            if _sv(s, 'client_reference_id') == ord_token:
                if _sv(s, 'status') == 'open':
                    return _json_resp({'ok': True, 'ord': ord_token,
                                       'checkout_url': _sv(s, 'url')}, 200)
                if _sv(s, 'status') == 'complete':
                    return _json_resp({'ok': False, 'already': True, 'ord': ord_token,
                                       'message': 'Este pacote já foi pago.'}, 409)
                break

        meta = {'v': '1', 'ord': ord_token, 'n': str(len(segs)),
                'tot': str(int(round(total * 100))),
                'fn': cust['first_name'][:60], 'ln': cust['last_name'][:60],
                'em': cust['email'][:80], 'ph': cust['phone_number'][:30],
                'addr': (data.get('pickup_address') or '')[:200]}
        for i, s in enumerate(segs):
            meta[f's{i}'] = f"{s['date']}|{s['start']}|{s['hours']}|{int(round(s['amount'] * 100))}"

        session = _stripe.checkout.Session.create(
            mode='payment',
            payment_method_types=['card'],           # captura manual é só cartão
            client_reference_id=ord_token,
            customer_email=cust['email'],
            payment_intent_data={'capture_method': 'manual',
                                 'metadata': {'v': '1', 'ord': ord_token}},
            line_items=[{
                'quantity': 1,
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': int(round(s['amount'] * 100)),
                    'product_data': {'name': f"Chauffeur — {s['date']} {s['start']}–{s['end']} ({s['hours']}h)"},
                },
            } for s in segs],
            metadata=meta,
            success_url=f'{TRANSFER_SITE_URL}/confirmation?ord={ord_token}',
            cancel_url=f'{TRANSFER_SITE_URL}/book?canceled={ord_token}',
            idempotency_key=f'sess:{ord_token}',
        )
        return _json_resp({'ok': True, 'ord': ord_token, 'total': total,
                           'checkout_url': _sv(session, 'url')}, 200)
    except Exception as e:
        print(f'[pkg/checkout] erro: {e}')
        return _json_resp({'ok': False, 'message': 'Não foi possível iniciar o pagamento.'}, 502)


def _pkg_fulfil(session):
    """Cria as reservas, dá baixa e só então captura o dinheiro.

    Idempotente: os ids já criados ficam gravados na metadata do PaymentIntent,
    então uma reentrega do webhook continua de onde parou."""
    _stripe.api_key = STRIPE_SECRET_KEY
    meta = _sv(session, 'metadata') or {}          # dict puro (webhook parseia o JSON cru)
    ord_token = _sv(meta, 'ord')
    pi_id = _sv(session, 'payment_intent')
    if not isinstance(pi_id, str):
        pi_id = _sv(pi_id, 'id')
    pi = _stripe.PaymentIntent.retrieve(pi_id)
    log = _pi_log(pi)

    if log.get('state') in ('captured', 'voided'):
        print(f'[pkg] {ord_token} já finalizado ({log["state"]})')
        return

    n = int(_sv(meta, 'n') or 0)
    cust = {'first_name': _sv(meta, 'fn', ''), 'last_name': _sv(meta, 'ln', ''),
            'email': _sv(meta, 'em', ''), 'phone_number': _sv(meta, 'ph', '')}
    segs = []
    for i in range(n):
        raw = _sv(meta, f's{i}')
        if not raw:
            continue
        d, st, h, cents = raw.split('|')
        segs.append({'date': d, 'start': st, 'hours': int(h),
                     'end': f'{int(st[:2]) + int(h):02d}:00', 'amount': int(cents) / 100.0})

    customer_id = log.get('cid')
    if not customer_id and segs:
        customer_id = _hq_create_contact(cust, segs[0]['date'])
        log['cid'] = str(customer_id)
        _stripe.PaymentIntent.modify(pi_id, metadata=log)

    captured_cents = 0
    for i, seg in enumerate(segs):
        if log.get(f'r{i}'):                      # já criado numa entrega anterior
            captured_cents += int(round(seg['amount'] * 100))
            continue
        try:
            rid = _hq_create_block(seg, cust, customer_id, ord_token, i, meta.get('addr'))
            log[f'r{i}'] = str(rid)
            _stripe.PaymentIntent.modify(pi_id, metadata=log)   # grava antes de seguir
            _hq_settle(rid, seg['amount'], pi_id)
            captured_cents += int(round(seg['amount'] * 100))
        except Exception as e:
            print(f'[pkg] {ord_token} bloco {i} falhou: {type(e).__name__}: {e}')
            log[f'r{i}'] = 'FAIL'
            log[f'e{i}'] = f'{type(e).__name__}: {e}'[:480]   # visível no /pkg/order
            _stripe.PaymentIntent.modify(pi_id, metadata=log)

    if captured_cents <= 0:
        _stripe.PaymentIntent.cancel(pi_id)        # nada criado → libera tudo
        log['state'] = 'voided'
        _stripe.PaymentIntent.modify(pi_id, metadata=log)
        print(f'[pkg] {ord_token}: nenhum bloco criado, autorização liberada')
        return

    # captura só o que existe de fato; o resto da autorização é liberado
    _stripe.PaymentIntent.capture(pi_id, amount_to_capture=captured_cents)
    log['state'] = 'captured'
    log['cap'] = str(captured_cents)
    _stripe.PaymentIntent.modify(pi_id, metadata=log)
    print(f'[pkg] {ord_token}: capturado {captured_cents / 100:.2f} USD')

    falhas = [i for i in range(len(segs)) if log.get(f'r{i}') == 'FAIL']
    try:
        requests.post('https://api.resend.com/emails',
                      headers={'Authorization': f"Bearer {os.getenv('RESEND_API_KEY')}",
                               'Content-Type': 'application/json'},
                      json={'from': 'Allycar <booking@allycar.com>',
                            'to': ['higor@allycar.com', 'david@allycar.com'],
                            'subject': f'🚐 Pacote transfer {ord_token} — {len(segs) - len(falhas)}/{len(segs)} blocos',
                            'text': (f'Pedido: {ord_token}\nCliente: {cust["first_name"]} {cust["last_name"]}'
                                     f' ({cust["email"]} / {cust["phone_number"]})\n'
                                     f'Capturado: USD {captured_cents / 100:.2f}\nStripe: {pi_id}\n'
                                     f'Retirada: {meta.get("addr") or "—"}\n\n'
                                     + '\n'.join(
                                         f'  {s["date"]} {s["start"]}–{s["end"]} ({s["hours"]}h) '
                                         f'USD {s["amount"]:.2f} → reserva {log.get(f"r{i}")}'
                                         for i, s in enumerate(segs))
                                     + (f'\n\n⚠️ BLOCOS COM FALHA: {falhas} — verificar na HQ' if falhas else ''))},
                      timeout=10)
    except Exception as e:
        print(f'[pkg] e-mail ignorado: {e}')


@app.route('/api/transfer/pkg/order/<ord_token>', methods=['GET', 'OPTIONS'])
def pkg_order(ord_token):
    """Estado do pedido — e rede de segurança do webhook.

    Se o cliente autorizou mas o webhook não chegou (falha, timeout, endpoint
    fora do ar), esta rota completa o processo. É idempotente: se já foi
    processado, só informa o estado. A página de confirmação chama isto."""
    if request.method == 'OPTIONS':
        return _cors_transfer(app.make_default_options_response())
    if not (_stripe and STRIPE_SECRET_KEY):
        return _json_resp({'ok': False, 'message': 'indisponível'}, 503)
    _stripe.api_key = STRIPE_SECRET_KEY
    try:
        alvo = None
        for s in _stripe.checkout.Session.list(limit=100).data:
            if _sv(s, 'client_reference_id') == ord_token:
                alvo = s
                break
        if alvo is None:
            return _json_resp({'ok': False, 'message': 'Pedido não encontrado.'}, 404)

        pi_id = _sv(alvo, 'payment_intent')
        if not isinstance(pi_id, str):
            pi_id = _sv(pi_id, 'id')
        estado = 'aguardando pagamento'
        if pi_id:
            pi = _stripe.PaymentIntent.retrieve(pi_id)
            log = _pi_log(pi)
            situacao = _sv(pi, 'status')
            # autorizado mas ainda não processado → completa agora
            if situacao == 'requires_capture' and log.get('state') not in ('captured', 'voided'):
                _pkg_fulfil(alvo)          # _pkg_fulfil usa _sv, aceita os dois formatos
                pi = _stripe.PaymentIntent.retrieve(pi_id)
                log = _pi_log(pi)
                situacao = _sv(pi, 'status')
            estado = log.get('state') or situacao
            reservas = [log[f'r{i}'] for i in range(PKG_MAX_BLOCKS) if log.get(f'r{i}')]
            erros = [log[f'e{i}'] for i in range(PKG_MAX_BLOCKS) if log.get(f'e{i}')]
            return _json_resp({'ok': True, 'ord': ord_token, 'state': estado,
                               'stripe_status': situacao, 'reservations': reservas,
                               'errors': erros}, 200)
        return _json_resp({'ok': True, 'ord': ord_token, 'state': estado}, 200)
    except Exception as e:
        print(f'[pkg/order] erro: {e}')
        return _json_resp({'ok': False, 'message': str(e)[:200]}, 500)


@app.route('/api/transfer/stripe/webhook', methods=['POST'])
def pkg_stripe_webhook():
    """Único lugar que escreve na HQ. Sem CORS — quem chama é o Stripe."""
    if not (_stripe and STRIPE_SECRET_KEY):
        return app.response_class(response='{"ok":true}', status=200, mimetype='application/json')
    payload = request.get_data()
    try:
        if STRIPE_WEBHOOK_SECRET:
            # a lib valida a assinatura; depois usamos o JSON cru, porque o
            # objeto que ela devolve não é dict e quebra qualquer .get()
            _stripe.Webhook.construct_event(
                payload, request.headers.get('Stripe-Signature', ''), STRIPE_WEBHOOK_SECRET)
        else:
            print('[pkg/webhook] STRIPE_WEBHOOK_SECRET ausente — assinatura NÃO verificada')
        event = json.loads(payload)
    except Exception as e:
        print(f'[pkg/webhook] assinatura inválida: {e}')
        return app.response_class(response='{"ok":false}', status=400, mimetype='application/json')

    obj = _sv(_sv(event, 'data'), 'object') or {}
    # ignora tudo que não é nosso (a HQ usa a mesma conta Stripe p/ locação)
    if (_sv(obj, 'metadata') or {}).get('v') != '1':
        return app.response_class(response='{"ok":true}', status=200, mimetype='application/json')

    if _sv(event, 'type') == 'checkout.session.completed':
        try:
            _pkg_fulfil(obj)
        except Exception as e:
            print(f'[pkg/webhook] erro ao processar: {e}')
            # 500 mantém a fila de retentativas do Stripe engajada
            return app.response_class(response='{"ok":false}', status=500, mimetype='application/json')
    return app.response_class(response='{"ok":true}', status=200, mimetype='application/json')


# =====================================
# EXECUÇÃO
# =====================================

if __name__ == '__main__':
    print("🚀 Servidor webhook iniciado!")
    print("📱 Endpoint: http://localhost:5000/webhook/whatsapp")
    print("🧪 Teste: http://localhost:5000/test/send")
    print("\n⚠️  Lembre-se de configurar o webhook no Twilio Console!")
    PORT = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=PORT, debug=False)
