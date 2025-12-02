import gspread
from oauth2client.service_account import ServiceAccountCredentials
from twilio.rest import Client
from datetime import datetime
import pytz
import time

# Importar configurações do arquivo config.py
from config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_NUMBER,
    SPREADSHEET_NAME,
    CREDENTIALS_FILE,
    BUSINESS_HOURS
)

# =====================================
# FUNÇÕES
# =====================================

def conectar_google_sheets():
    """Conecta ao Google Sheets"""
    
    print("🔗 Tentando conectar ao Google Sheets...")
    
    try:
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        
        print(f"📄 Carregando credenciais de: {CREDENTIALS_FILE}")
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
        
        print("🔐 Autorizando cliente...")
        client = gspread.authorize(creds)
        
        print(f"📊 Abrindo planilha: {SPREADSHEET_NAME}")
        sheet = client.open(SPREADSHEET_NAME).sheet1
        
        print("✅ Conectado ao Google Sheets com sucesso!")
        return sheet
        
    except FileNotFoundError:
        print(f"❌ ERRO: Arquivo '{CREDENTIALS_FILE}' não encontrado!")
        print("   Verifique se o arquivo credentials.json está na pasta correta")
        raise
        
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ ERRO: Planilha '{SPREADSHEET_NAME}' não encontrada!")
        print("   Verifique:")
        print("   1. Se o nome da planilha está correto no .env")
        print("   2. Se você compartilhou a planilha com o email da Service Account")
        raise
        
    except gspread.exceptions.APIError as e:
        print(f"❌ ERRO na API do Google: {e}")
        print("   Verifique se você ativou as APIs no Google Cloud Console")
        raise
        
    except Exception as e:
        print(f"❌ ERRO inesperado: {type(e).__name__}")
        print(f"   Detalhes: {str(e)}")
        raise

def esta_no_horario_comercial(pais):
    """Verifica se está no horário comercial do país"""
    if pais not in BUSINESS_HOURS:
        pais = 'Brazil'  # Default
    
    config = BUSINESS_HOURS[pais]
    tz = pytz.timezone(config['timezone'])
    hora_atual = datetime.now(tz).hour
    
    return config['start'] <= hora_atual < config['end']

def formatar_telefone(telefone):
    """Formata telefone para padrão WhatsApp"""
    # Remove espaços e caracteres especiais
    telefone = ''.join(filter(str.isdigit, str(telefone)))
    
    # Adiciona + se não tiver
    if not telefone.startswith('+'):
        telefone = '+' + telefone
    
    return f'whatsapp:{telefone}'

def enviar_mensagem_inicial_com_opcoes(telefone, nome, cidade):
    """Envia mensagem inicial com opções interativas - NOVA VERSÃO"""
    
    mensagem = f"""Olá *{nome}*! 👋

Sou da *Allycar* e temos ofertas especiais de veículos em {cidade}! 🚗

✨ *Qual categoria te interessa?*

1️⃣ - Carros Econômicos
2️⃣ - SUVs
3️⃣ - Carros de Luxo
4️⃣ - Utilitários
5️⃣ - Falar com consultor

*Responda com o número da opção!*"""
    
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            body=mensagem,
            to=telefone
        )
        
        print(f"✅ Mensagem com opções enviada para {nome}: {message.sid}")
        
        # REGISTRAR CONVERSA NO WEBHOOK
        try:
            import requests
            response = requests.post('http://localhost:5000/register_conversation', 
                json={
                    'phone': telefone,
                    'name': nome,
                    'city': cidade
                },
                timeout=2
            )
            if response.status_code == 200:
                print(f"✅ Conversa registrada no webhook para {nome}")
            else:
                print(f"⚠️  Aviso: Não foi possível registrar conversa no webhook")
        except Exception as e:
            print(f"⚠️  Aviso: Webhook pode não estar rodando - {e}")
            print(f"   As respostas do cliente não serão processadas!")
        
        return True, message.sid
        
    except Exception as e:
        print(f"❌ Erro ao enviar para {nome}: {str(e)}")
        return False, str(e)

def processar_leads():
    """Processa leads da planilha e envia mensagens com opções interativas"""
    print("🚀 Iniciando processamento de leads...\n")
    print("⚠️  IMPORTANTE: Certifique-se que o servidor webhook está rodando!")
    print("   Execute 'python webhook.py' em outro terminal\n")
    
    # Conecta à planilha
    sheet = conectar_google_sheets()
    leads = sheet.get_all_records()
    
    enviados = 0
    erros = 0
    pulados = 0
    
    for idx, lead in enumerate(leads, start=2):  # Começa em 2 (linha 1 é cabeçalho)
        nome = lead.get('Name', '')
        telefone = lead.get('Phone', '')
        cidade = lead.get('City', '')
        pais = lead.get('Country', 'Brazil')
        status = lead.get('Status', '')
        
        # Debug
        print(f"\n📋 Processando linha {idx}: {nome}")

        # Pula se já foi enviado
        if status == 'Sent':
            print(f"⏭️  Pulando {nome} - já enviado")
            pulados += 1
            continue
        
        # Verifica horário comercial
        if not esta_no_horario_comercial(pais):
            print(f"⏰ Pulando {nome} - fora do horário comercial de {pais}")
            pulados += 1
            continue
        
        # Valida dados
        if not nome or not telefone:
            print(f"⚠️  Pulando linha {idx} - dados incompletos")
            sheet.update_cell(idx, 5, 'Error - Incomplete data')
            erros += 1
            continue
        
        # Formata telefone
        telefone_formatado = formatar_telefone(telefone)
        
        # Envia mensagem INICIAL com OPÇÕES
        print(f"📤 Enviando mensagem para {nome} ({telefone_formatado})...")
        sucesso, resultado = enviar_mensagem_inicial_com_opcoes(
            telefone_formatado, 
            nome, 
            cidade
        )
        
        if sucesso:
            # Atualiza planilha
            sheet.update_cell(idx, 5, 'Sent')
            sheet.update_cell(idx, 6, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            enviados += 1
            print(f"✅ Sucesso! O cliente vai receber opções interativas")
            print(f"   As respostas serão processadas pelo webhook")
        else:
            sheet.update_cell(idx, 5, f'Error: {resultado[:50]}')
            erros += 1
        
        # Delay entre mensagens (respeitar limites Twilio)
        print(f"⏳ Aguardando 2 segundos antes da próxima mensagem...")
        time.sleep(2)
    
    # Relatório final
    print("\n" + "="*60)
    print("📊 RELATÓRIO FINAL")
    print("="*60)
    print(f"✅ Mensagens enviadas: {enviados}")
    print(f"❌ Erros: {erros}")
    print(f"⏭️  Pulados: {pulados}")
    print(f"📝 Total processado: {len(leads)}")
    print("="*60)
    print("\n💡 PRÓXIMOS PASSOS:")
    print("   1. Os clientes vão responder escolhendo uma opção (1-5)")
    print("   2. O webhook vai capturar as respostas automaticamente")
    print("   3. Leads interessados vão gerar notificação para o WhatsApp comercial")
    print("   4. Acompanhe os logs do webhook em tempo real!")
    print("\n🔍 Para ver conversas ativas:")
    print("   curl http://localhost:5000/conversations")
    print("="*60)

# =====================================
# EXECUÇÃO
# =====================================

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║              🚗 SISTEMA ALLYCAR - WHATSAPP BOT 🚗             ║
║                                                              ║
║  Sistema de envio automatizado de mensagens com opções       ║
║  interativas para leads de locação de veículos              ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    try:
        processar_leads()
    except KeyboardInterrupt:
        print("\n\n⚠️  Processo interrompido pelo usuário")
    except Exception as e:
        print(f"\n\n❌ Erro fatal: {str(e)}")
        import traceback
        print("\n🔍 Detalhes do erro:")
        traceback.print_exc()