from twilio.rest import Client
from dotenv import load_dotenv
import os

load_dotenv()

print("="*50)
print("🔍 DIAGNÓSTICO TWILIO")
print("="*50)

account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
from_number = os.getenv('TWILIO_WHATSAPP_NUMBER')

print(f"\n📋 Configurações:")
print(f"   Account SID: {account_sid}")
print(f"   From Number: {from_number}")

client = Client(account_sid, auth_token)

print(f"\n💰 Conta:")
try:
    account = client.api.accounts(account_sid).fetch()
    print(f"   Status: {account.status}")
    print(f"   Tipo: {account.type}")
except Exception as e:
    print(f"   ❌ Erro: {e}")

print(f"\n📨 Últimas 3 mensagens:")
try:
    messages = client.messages.list(limit=3)
    
    for msg in messages:
        print(f"\n   ------------------")
        print(f"   De: {msg.from_}")
        print(f"   Para: {msg.to}")
        print(f"   Status: {msg.status}")
        if msg.error_code:
            print(f"   ❌ ERRO {msg.error_code}: {msg.error_message}")
        else:
            print(f"   ✅ Sem erro")
        print(f"   Data: {msg.date_created}")
except Exception as e:
    print(f"   ❌ Erro: {e}")

print("\n" + "="*50)
