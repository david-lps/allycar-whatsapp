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

import smtplib
from email.message import EmailMessage
import os

def registrar_lead_qualificado(lead_info):

    try:
        sheet = conectar_google_sheets()
        
        # Abre a aba correta
        worksheet = sheet.spreadsheet.worksheet("Leads_Qualificados")

        worksheet.append_row([
            lead_info["timestamp"],
            lead_info["name"],
            lead_info["phone"],
            lead_info["category"],
            lead_info["message"]
        ])

        print("✅ Lead qualificado salvo na planilha")
        return True

    except Exception as e:
        print(f"⚠️ Erro ao salvar lead qualificado: {e}")
        return False

# =====================================
# WEBHOOK - RECEBER RESPOSTAS
# =====================================

MESSAGES = {
    "pt": {
        "start_wait": "Olá! Para iniciar, aguarde o envio da nossa mensagem.",
        "consultor_intro": """Perfeito! 👏

Um consultor entrará em contato em breve.

Por favor, nos conte um pouco sobre o que você procura (modelo, valor, prazo, etc):""",
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

Tenha um ótimo dia! 🚗✨"""
    },
    "es": {
        "start_wait": "Hola! Para comenzar, espera nuestro mensaje inicial.",
        "consultor_intro": """¡Perfecto! 👏

Un asesor se pondrá en contacto contigo en breve.

Cuéntanos un poco sobre lo que estás buscando (modelo, presupuesto, fechas, etc.):""",
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

¡Que tengas un excelente día! 🚗✨"""
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
    
    # Verificar se existe conversa ativa
    if from_number not in conversations:
        msg.body(
            "Olá! 👋\n"
            "Por favor, aguarde nossa mensagem inicial para continuar.\n\n"
            "Hello! 👋\n"
            "Please wait for our initial message to continue.\n\n"
            "¡Hola! 👋\n"
            "Por favor, espere nuestro mensaje inicial para continuar."
        )
        return str(resp)
    
    conversa = conversations[from_number]
    lang = conversa.get("language", "pt")
    texts = MESSAGES[lang]
    stage = conversa['stage']
    
    # ===== FLUXO DE CONVERSA =====
    
    # Estágio 1: Aguardando categoria
    if stage == 'awaiting_category':
        categoria = processar_escolha_categoria(body)
        
        if categoria == 'consultor':
            conversa['interested'] = True
            conversa['category'] = 'Falar com consultor'
            conversa['stage'] = 'awaiting_message'         
            msg.body(texts["consultor_intro"])
            
        elif categoria:
            conversa['category'] = categoria
            conversa['stage'] = 'awaiting_message'
            msg.body(texts["ask_details"])
            
        else:
            msg.body(texts["invalid_option"])
        
    # Estágio 3: Aguardando mensagem do cliente
    elif stage == 'awaiting_message':
        conversa['message'] = body
        conversa['stage'] = 'finished'
        conversa['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # REGISTRAR LEAD QUALIFICADO
        lead_info = {
            'name': conversa['name'],
            'phone': from_number.replace('whatsapp:', ''),
            'category': conversa.get('category', 'Não especificado'),
            'message': body,
            'timestamp': conversa['timestamp']
        }

        try:
            registrar_lead_qualificado(lead_info)
        except Exception as e:
            print(f"⚠️ Falha ao notificar lead (ignorado): {e}")

        msg.body(texts["final_thanks"])
        
        # Manter conversa para histórico (em produção, salve no banco)
        conversa['completed'] = True
    
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

@app.route('/trigger-send', methods=['GET', 'POST'])
def trigger_send():
    """Disparar envio de mensagens manualmente"""
    try:
        from main import processar_leads
        processar_leads()
        return {'status': 'success', 'message': 'Envio iniciado!'}, 200
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500


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
