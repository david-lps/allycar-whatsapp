import gspread
from oauth2client.service_account import ServiceAccountCredentials
from twilio.rest import Client
from datetime import datetime
import pytz
import time
import os
import http.client
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Importar configurações do arquivo config.py
from config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_NUMBER,
    SPREADSHEET_NAME,
    GOOGLE_CREDENTIALS,
    BUSINESS_HOURS,
    TWILIO_WHATSAPP_TEMPLATE_SID_BR,
    TWILIO_WHATSAPP_TEMPLATE_SID_LATAM,
    EMAIL_USER, 
    EMAIL_PASSWORD, 
    EMAIL_TO
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
        
        creds = ServiceAccountCredentials.from_json_keyfile_dict(GOOGLE_CREDENTIALS, scope)

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

def enviar_mensagem_inicial_com_opcoes(telefone, nome, pais, email_cliente=None):
    try:
        pais_norm = (pais or "").lower()

        # ===============================
        # 🇺🇸 USA → EMAIL
        # ===============================
        if pais_norm in ["usa", "united states", "estados unidos", "eua"]:
            if not email_cliente:
                print(f"⚠️ USA sem email para {nome}, pulando envio")
                return False, "USA sem email"

            try:
                msg = EmailMessage()
                msg["Subject"] = "Allycar | Sua locação em Orlando"
                msg["From"] = EMAIL_USER
                msg["To"] = email_cliente

                msg.set_content(f"""
Olá {nome},

Aqui é da Allycar 🚗🇺🇸

Recebemos seu interesse em alugar um veículo em Orlando.
Em breve um consultor entrará em contato com você.

Obrigado!
""")

                with smtplib.SMTP("smtp.gmail.com", 587) as server:
                    server.starttls()
                    server.login(EMAIL_USER, EMAIL_PASSWORD)
                    server.send_message(msg)

                print(f"✅ Email enviado para {nome} ({email_cliente})")
                return True, "email"

            except Exception as e:
                print(f"⚠️ Falha ao enviar email (não bloqueante): {e}")
                return False, str(e)

        # ===============================
        # 🇧🇷 BRASIL → TEMPLATE BR
        # ===============================
        if pais_norm in ["brazil", "brasil"]:
            template_sid = TWILIO_WHATSAPP_TEMPLATE_SID_BR

        # ===============================
        # 🇦🇷 🇨🇴 AR / CO → TEMPLATE LATAM
        # ===============================
        elif pais_norm in ["argentina", "colombia"]:
            template_sid = TWILIO_WHATSAPP_TEMPLATE_SID_LATAM

        # ===============================
        # OUTROS → NÃO ENVIA
        # ===============================
        else:
            print(f"⏭️ País não tratado ({pais}) para {nome}")
            return False, "pais_nao_tratado"

        # ===============================
        # ENVIO WHATSAPP
        # ===============================
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=telefone,
            content_sid=template_sid,
            content_variables=json.dumps({
                "1": nome
            })
        )

        print(f"✅ WhatsApp enviado para {nome} ({pais}): {message.sid}")

        # registra conversa no webhook (não bloqueante)
        try:
            import requests, os
            webhook_url = os.getenv(
                "WEBHOOK_URL",
                "https://allycar-whatsapp-production.up.railway.app"
            )
            requests.post(
                f"{webhook_url}/register_conversation",
                json={"phone": telefone, "name": nome},
                timeout=2
            )
        except Exception as e:
            print(f"⚠️ Falha ao registrar conversa: {e}")

        return True, message.sid

    except Exception as e:
        print(f"❌ Erro geral no envio para {nome}: {e}")
        return False, str(e)


def cliente_ja_tem_reserva(telefone):
    """
    Consulta a API do HQ para verificar se o telefone já possui reserva
    Retorna True se encontrar reserva, False se não
    """

    conn = http.client.HTTPSConnection("api.caagcrm.com")

    headers = {
        'Authorization': 'Basic YzQzMlR2elRSbFdxMGlJNldUeEFGM1lvUjBqcjVkV2dxRWJ0NGs2TlFTZzhZbmd0RWg6NXVhQjZTWEdGNU1zTk40RExrd29wVTBuZ2RURVpGeHBNb0l4RnZZRHBveGRjaUgxZnA='
    }

    conn.request(
        "GET",
        "/api/car-rental/reservations?filter-from-mine-dashboard=null&filters=null",
        headers=headers
    )

    res = conn.getresponse()
    print("Status HTTP:", res.status)
    
    data = res.read().decode("utf-8")
    print("Resposta bruta da API:", data)

    try:
        reservas = json.loads(data)
    except:
        print("⚠️ Erro ao interpretar resposta do HQ")
        return False

    telefone_limpo = telefone.replace("whatsapp:", "").replace("+", "")

    for reserva in reservas.get("data", []):
        telefone_reserva = str(reserva.get("phone", "")).replace("+", "")
        if telefone_limpo in telefone_reserva:
            print(f"⛔ Reserva encontrada para {telefone}")
            return True

    print(f"✅ Nenhuma reserva encontrada para {telefone}")
    return False

def processar_leads():
    """Processa leads da planilha e envia mensagens com opções interativas"""
    print("🚀 Iniciando processamento de leads...\n")
    
    # Conecta à planilha
    sheet = conectar_google_sheets()
    leads = sheet.get_all_records()
    
    enviados = 0
    erros = 0
    pulados = 0
    
    for idx, lead in enumerate(leads, start=2):  # Começa em 2 (linha 1 é cabeçalho)
        nome = lead.get('NOME', '')
        telefone = lead.get('TELEFONE', '')
        pais = lead.get('PAIS', 'Brazil')
        status = lead.get('STATUS', '')
        email_cliente = lead.get('EMAIL', '')
        
        # Debug
        print(f"\n📋 Processando linha {idx}: {nome}")

        # Pula se já foi enviado
        if status == 'Sent':
            print(f"⏭️  Pulando {nome} - já enviado")
            pulados += 1
            continue

        pais_normalizado = pais.strip().lower()
        
        # Verifica horário comercial
        if not esta_no_horario_comercial(pais):
            print(f"⏰ Pulando {nome} - fora do horário comercial de {pais}")
            pulados += 1
            continue
        
        # Valida dados
        if not nome or not telefone:
            print(f"⚠️  Pulando linha {idx} - dados incompletos")
            sheet.update_cell(idx, 6, 'Error - Incomplete data')
            erros += 1
            continue

        # Formata telefone
        telefone_formatado = formatar_telefone(telefone)
        
        # Verificar se já existe reserva no HQ
        if cliente_ja_tem_reserva(telefone_formatado):
            print(f"⏭️ Pulando {nome} - cliente já possui reserva")
            sheet.update_cell(idx, 5, 'Skipped - Already has reservation')
            pulados += 1
            continue
        
        # Envia mensagem INICIAL com OPÇÕES
        print(f"📤 Enviando mensagem para {nome} ({telefone_formatado})...")
        sucesso, resultado = enviar_mensagem_inicial_com_opcoes(
            telefone_formatado, 
            nome,
            pais_normalizado,
            email_cliente
        )
        
        if sucesso:
            # Atualiza planilha
            sheet.update_cell(idx, 7, 'Sent')
            sheet.update_cell(idx, 8, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            enviados += 1
            print(f"✅ Sucesso! O cliente vai receber opções interativas")
            print(f"   As respostas serão processadas pelo webhook")
        else:
            sheet.update_cell(idx, 7, f'Error: {resultado[:50]}')
            erros += 1
        
        # Delay entre mensagens (respeitar limites Twilio)
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
    print("   1. Os clientes vão responder")
    print("   2. O webhook vai capturar as respostas automaticamente")
    print("   3. Leads interessados vão gerar notificação para o WhatsApp comercial")
    print("   4. Acompanhe os logs do webhook em tempo real!")
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
║  interativas para leads de locação de veículos               ║
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
