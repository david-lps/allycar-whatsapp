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
import requests

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
            "higor@allycar.com"
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
        info['message'] = conversa.get('message', lead_info.get('message'))
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

    # Normaliza chave para funcionar com WhatsApp e SMS
    conversation_key = from_number

    if conversation_key not in conversations and not from_number.startswith("whatsapp:"):
        whatsapp_key = f"whatsapp:{from_number}"
        if whatsapp_key in conversations:
            conversation_key = whatsapp_key

    conversa = conversations[conversation_key]
    lang = conversa.get("language", "pt")
    texts = MESSAGES.get(lang, MESSAGES["pt"])
    stage = conversa['stage']
    conversa['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lead_info = {
        'name': conversa['name'],
        'phone': from_number.replace('whatsapp:', ''),
        'category': conversa.get('category', 'Não especificado'),
        'message': body,
        'timestamp': conversa['timestamp']
    }

    # Registra TODA mensagem na planilha (o email é enviado só no final da conversa)
    try:
        registrar_lead_qualificado(lead_info)
    except Exception as e:
        print(f"⚠️ Falha ao registrar lead na planilha (ignorado): {e}")
        
    # Verificar se existe conversa ativa
    if conversation_key not in conversations:
        msg.body(
            "Olá! 👋\n"
            "Por favor, aguarde nossa mensagem inicial para continuar.\n\n"
            "Hello! 👋\n"
            "Please wait for our initial message to continue.\n\n"
            "¡Hola! 👋\n"
            "Por favor, espere nuestro mensaje inicial para continuar."
        )
        return str(resp)
    
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
                    conversa['message'] = (
                        f"Consultou disponibilidade: {conversa.get('seats')} assentos, "
                        f"{conversa['pickup_display']} → {conversa['return_display']}. "
                        f"{len(resultados)} opções apresentadas."
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
        response = app.make_default_options_response()
        response.headers['Access-Control-Allow-Origin'] = 'https://www.allycar.com'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

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

    response.headers['Access-Control-Allow-Origin'] = 'https://www.allycar.com'
    return response

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

# ── Cabeçalhos CORS reutilizáveis ──────────────────────────────────────────
def _cors(response):
    response.headers['Access-Control-Allow-Origin']  = ALLOWED_ORIGIN
    response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# ── 1. Criar contato + retornar contact_id ────────────────────────────────
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
            'pick_up_location': data.get('pick_up_location', '2'),
            'return_location':  data.get('return_location', '2'),
            'brand_id':         data.get('brand_id', '1'),
            'vehicle_class_id': data.get('vehicle_class_id', '15'),
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
        import json
        print(f'[create-contact] erro: {e}')
        response = app.response_class(
            response=json.dumps({'success': False, 'message': str(e)}),
            status=500,
            mimetype='application/json'
        )
        return _cors(response)

# ── 2. Criar reserva ───────────────────────────────────────────────────────
@app.route('/api/hq/create-reservation', methods=['POST', 'OPTIONS'])
def hq_create_reservation():
    """Proxy: cria reserva na HQ Rental evitando CORS no browser."""

    if request.method == 'OPTIONS':
        return _cors(app.make_default_options_response())

    try:
        data = request.get_json()

        params = {
            'pick_up_date':                   data.get('pick_up_date'),
            'return_date':                    data.get('return_date'),
            'pick_up_time':                   data.get('pick_up_time'),
            'return_time':                    data.get('return_time'),
            'brand_id':                       data.get('brand_id', 1),
            'pick_up_location':               data.get('pick_up_location', 2),
            'return_location':                data.get('return_location', 2),
            'vehicle_class_id':               data.get('vehicle_class_id', 15),
            'customer_id':                    data.get('customer_id'),
            'customer_first_name':            data.get('customer_first_name'),
            'customer_last_name':             data.get('customer_last_name'),
            'customer_email':                 data.get('customer_email'),
            'customer_birthdate':             data.get('customer_birthdate'),
            'customer_driver_license_number': data.get('customer_driver_license_number'),
            'additional_charges[]':           '',
        }

        # Remove chaves com valor None para não poluir a URL
        params = {k: v for k, v in params.items() if v is not None}

        resp = requests.post(
            f'{HQ_API_BASE}/car-rental/reservations/confirm',
            headers={'Authorization': HQ_API_TOKEN},
            params=params,
            timeout=15
        )

        # ================================
        # ENVIO DE EMAIL (SOMENTE SUCESSO)
        # ================================
        if resp.status_code in (200, 201):
            try:
                destinatarios = [
                    "higor@allycar.com",
                    "david@allycar.com"
                ]

                conteudo = f"""
Nova reserva criada com sucesso 🚗

Cliente:
Nome: {data.get('customer_first_name')} {data.get('customer_last_name')}
Email: {data.get('customer_email')}

Reserva:
Pick-up: {data.get('pick_up_date')} às {data.get('pick_up_time')}
Return: {data.get('return_date')} às {data.get('return_time')}

Local:
Pick-up location ID: {data.get('pick_up_location')}
Return location ID: {data.get('return_location')}

Veículo:
Class ID: {data.get('vehicle_class_id')}

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
                        "subject": f"🚗 Nova reserva TACOMA: {data.get('customer_first_name')} {data.get('customer_last_name')}",
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
HQ_VANS_LOCATION_ID         = os.getenv("HQ_VANS_LOCATION_ID", HQ_PICKUP_LOCATION)
HQ_STRIPE_PAYMENT_METHOD_ID = os.getenv("HQ_STRIPE_PAYMENT_METHOD_ID", "4")   # gateway Stripe da brand AllyCar

# Origens permitidas para o fluxo de reserva (inclui localhost p/ dev).
TRANSFER_ALLOWED_ORIGINS = {
    "https://www.allycar.com",
    "https://allycar.com",
    "http://localhost:4321",
    "http://localhost:4323",
}

def _cors_transfer(response):
    origin = request.headers.get("Origin", "")
    response.headers["Access-Control-Allow-Origin"] = origin if origin in TRANSFER_ALLOWED_ORIGINS else ALLOWED_ORIGIN
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

def _json_resp(payload, status=200):
    import json
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
# EXECUÇÃO
# =====================================

if __name__ == '__main__':
    print("🚀 Servidor webhook iniciado!")
    print("📱 Endpoint: http://localhost:5000/webhook/whatsapp")
    print("🧪 Teste: http://localhost:5000/test/send")
    print("\n⚠️  Lembre-se de configurar o webhook no Twilio Console!")
    PORT = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=PORT, debug=False)
