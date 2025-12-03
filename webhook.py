from flask import Flask, request
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)

# Configurações
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
COMMERCIAL_WHATSAPP = os.getenv('COMMERCIAL_WHATSAPP')  # WhatsApp comercial

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Armazenar estado das conversas (em produção, use banco de dados)
conversations = {}

def notificar_whatsapp_comercial(lead_info):
    """Notifica WhatsApp comercial sobre lead interessado"""
    mensagem = f"""🚨 *NOVO LEAD INTERESSADO!*

👤 Nome: {lead_info['name']}
📱 Telefone: {lead_info['phone']}
🚗 Interesse: {lead_info['category']}
⏰ Horário: {lead_info['timestamp']}

💬 Mensagem do cliente:
"{lead_info['message']}"

👉 Entre em contato agora!"""

    try:
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            body=mensagem,
            to=f'whatsapp:{COMMERCIAL_WHATSAPP}'
        )
        print(f"✅ Notificação enviada para comercial: {message.sid}")
        return True
    except Exception as e:
        print(f"❌ Erro ao notificar comercial: {e}")
        return False


# =====================================
# WEBHOOK - RECEBER RESPOSTAS
# =====================================

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
        msg.body("Olá! Para iniciar, aguarde o envio da nossa mensagem ou digite 'INICIAR'")
        return str(resp)
    
    conversa = conversations[from_number]
    stage = conversa['stage']
    
    # ===== FLUXO DE CONVERSA =====
    
    # Estágio 1: Aguardando categoria
    if stage == 'awaiting_category':
        categoria = processar_escolha_categoria(body)
        
        if categoria == 'consultor':
            conversa['interested'] = True
            conversa['category'] = 'Falar com consultor'
            conversa['stage'] = 'awaiting_message'
            
            msg.body("""Perfeito! 👏

Um consultor entrará em contato em breve.

Por favor, nos conte um pouco sobre o que você procura (modelo, valor, prazo, etc):""")
            
        elif categoria:
            conversa['category'] = categoria
            conversa['stage'] = 'confirming_interest'
            
            msg.body(f"""Ótima escolha! {categoria} 🚗

Temos várias opções disponíveis.

Deseja receber mais informações e falar com nosso consultor?

Digite:
✅ SIM - Quero mais informações
❌ NÃO - Não tenho interesse agora""")
        else:
            msg.body("""Desculpe, não entendi sua resposta. 

Por favor, escolha uma opção:

1️⃣ - Carros Econômicos
2️⃣ - SUVs
3️⃣ - Carros de Luxo
4️⃣ - Utilitários
5️⃣ - Falar com consultor""")
    
    # Estágio 2: Confirmando interesse
    elif stage == 'confirming_interest':
        if body.upper() in ['SIM', 'S', 'YES', 'Y', '✅']:
            conversa['interested'] = True
            conversa['stage'] = 'awaiting_message'
            
            msg.body("""Excelente! 🎉

Por favor, nos conte um pouco sobre o que você procura:
- Modelo preferido
- Valor que pretende investir
- Prazo desejado
- Qualquer outra informação relevante""")
            
        elif body.upper() in ['NÃO', 'NAO', 'N', 'NO', '❌']:
            conversa['interested'] = False
            conversa['stage'] = 'finished'
            
            msg.body("""Tudo bem! Entendo. 😊

Caso mude de ideia, estamos sempre à disposição.

Tenha um ótimo dia! 🚗✨""")
            
            # Remover conversa
            del conversations[from_number]
        else:
            msg.body("""Por favor, responda com:

✅ SIM - Quero mais informações
❌ NÃO - Não tenho interesse agora""")
    
    # Estágio 3: Aguardando mensagem do cliente
    elif stage == 'awaiting_message':
        conversa['message'] = body
        conversa['stage'] = 'finished'
        conversa['timestamp'] = request.form.get('MessageTimestamp', '')
        
        # NOTIFICAR WHATSAPP COMERCIAL
        lead_info = {
            'name': conversa['name'],
            'phone': from_number.replace('whatsapp:', ''),
            'city': conversa['city'],
            'category': conversa.get('category', 'Não especificado'),
            'message': body,
            'timestamp': conversa['timestamp']
        }
        
        notificar_whatsapp_comercial(lead_info)
        
        msg.body("""Obrigado! Recebemos sua mensagem. 📝

Um de nossos consultores entrará em contato em instantes!

Tenha um ótimo dia! 🚗✨""")
        
        # Manter conversa para histórico (em produção, salve no banco)
        conversa['completed'] = True
    
    return str(resp)

def processar_escolha_categoria(body):
    """Processa escolha da categoria via número ou texto"""
    body_upper = body.upper().strip()

    # Mapeamento por número
    map_por_numero = {
        '1': 'Carros Econômicos',
        '2': 'SUVs',
        '3': 'Carros de Luxo',
        '4': 'Utilitários',
        '5': 'consultor'
    }

    if body_upper in map_por_numero:
        return map_por_numero[body_upper]

    # Mapeamento por keywords (para caso de botões no futuro)
    map_por_texto = {
        'ECONOMICO': 'Carros Econômicos',
        'ECONÔMICO': 'Carros Econômicos',
        'SUV': 'SUVs',
        'LUXO': 'Carros de Luxo',
        'UTILITARIO': 'Utilitários',
        'UTILITÁRIO': 'Utilitários',
        'MINIVAN': 'Mini Vans',
        'CONSULTOR': 'consultor',
        'FALAR': 'consultor'
    }

    for key in map_por_texto:
        if key in body_upper:
            return map_por_texto[key]

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
            'interested': False
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
