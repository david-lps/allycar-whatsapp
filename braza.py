"""
Integração BrazaBank Checkout v2 — PIX + Cartão (fluxo COM documento).

Este módulo concentra TODAS as chamadas à API da Braza. Os endpoints Flask
(em webhook.py) só orquestram estas funções. Credenciais e ambiente vêm de
variáveis de ambiente no Railway — NUNCA hardcode usuário/senha aqui.

Env vars esperadas:
  BRAZA_ENV       = 'sandbox' (default) ou 'prod'
  BRAZA_USERNAME  = usuário da conta Braza
  BRAZA_PASSWORD  = senha da conta Braza

Fluxo (com documento):
  login -> quote(USDBRL) -> validate_client(CPF) [-> update_client se pendente]
        -> create_pix / create_cc_session -> status/webhook
"""

import os
import time
import requests

# ── Ambiente e URLs base ────────────────────────────────────────────────────
BRAZA_ENV = os.getenv("BRAZA_ENV", "sandbox").strip().lower()
_P = "" if BRAZA_ENV == "prod" else "sandbox-"

URL_AUTH   = f"https://{_P}authentication.brazacheckout.com.br"
URL_RATES  = f"https://{_P}rates.brazacheckout.com.br"
URL_PIX    = f"https://{_P}pix.brazacheckout.com.br"
URL_CC     = f"https://{_P}cc.brazacheckout.com.br"
URL_CLIENT = f"https://{_P}client.brazacheckout.com.br"
URL_ADDR   = f"https://{_P}address.brazacheckout.com.br"
URL_SALES  = f"https://{_P}sales.brazacheckout.com.br"
URL_APP    = f"https://{_P}app.brazacheckout.com.br"
URL_PAYMENT = f"https://{_P}payment.brazacheckout.com.br"   # fluxo advanced de cartão (guia oficial)

BRAZA_USERNAME = os.getenv("BRAZA_USERNAME")
BRAZA_PASSWORD = os.getenv("BRAZA_PASSWORD")

_TIMEOUT = 20

# ── Token com cache (login devolve ttl ~3600s) ──────────────────────────────
_token_cache = {"access": None, "refresh": None, "exp": 0.0}


def _get_token():
    """Retorna um accessToken válido, reaproveitando enquanto não expira."""
    now = time.time()
    if _token_cache["access"] and now < _token_cache["exp"] - 60:
        return _token_cache["access"]

    if not BRAZA_USERNAME or not BRAZA_PASSWORD:
        raise RuntimeError("BRAZA_USERNAME/BRAZA_PASSWORD não configurados no ambiente")

    r = requests.post(
        f"{URL_AUTH}/auth/login",
        json={"username": BRAZA_USERNAME, "password": BRAZA_PASSWORD},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["access"] = data["accessToken"]
    _token_cache["refresh"] = data.get("refreshToken")
    _token_cache["exp"] = now + int(data.get("ttl", 3600))
    return _token_cache["access"]


def _headers(extra=None):
    h = {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


# ── 1. Cotação (converte US$ -> BRL, devolve pix.id e credit_card.id) ────────
def create_quote(amount_usd, external_id, currency="USDBRL"):
    """
    amount_usd: valor da reserva em dólar (ex.: 155 ou 155.00).
    external_id: código da reserva (orderRef do booking-confirmation) — a Braza
                 guarda como identifier, o que nos deixa reconciliar depois.
    Retorna dict com chaves 'pix' e 'credit_card' (esta última com installments).
    """
    r = requests.post(
        f"{URL_RATES}/v1/quotes",
        headers=_headers(),
        json={"amount": amount_usd, "currency": currency, "externalId": str(external_id)},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ── 2. Cliente (fluxo COM documento) ────────────────────────────────────────
def validate_client(cpf):
    """
    Valida o CPF. Retorna { clientId, enabled, pendent?[], code? }.
    Se 'enabled' for False, os campos em 'pendent' precisam ser completados
    com update_client (ex.: phone, email, address...).
    Enviar o CPF mascarado: 999.999.999-99
    """
    r = requests.get(
        f"{URL_CLIENT}/v1",
        headers={"Authorization": f"Bearer {_get_token()}", "Accept": "application/json",
                 "X-CLIENT-CPF": cpf},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def lookup_cep(cep):
    """Consulta endereço por CEP (retorna logradouro, bairro, localidade, uf...)."""
    r = requests.get(
        f"{URL_ADDR}/v1/cep/{cep}",
        headers={"Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def update_client(cpf, info):
    """
    Completa o cadastro do cliente pendente.
    info: dict com os campos pedidos em 'pendent' (cep, state, city, code,
          neighborhood, address, number, complement, phone, email).
    Retorna { clientId, enabled }.
    """
    r = requests.patch(
        f"{URL_CLIENT}/v1",
        headers={"Authorization": f"Bearer {_get_token()}", "Content-Type": "application/json",
                 "Accept": "application/json", "X-CLIENT-CPF": cpf},
        json=info,
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ── 3a. PIX ─────────────────────────────────────────────────────────────────
def create_pix(cod_quote, cod_customer):
    """
    Gera o PIX. Retorna qrcode (copia-e-cola), qrCodeImage (data:image base64),
    id (invoiceIdPix), key, expirationDate, status, quantityBRL...
    """
    r = requests.post(
        f"{URL_PIX}/v1/pix",
        headers=_headers(),
        json={"codQuote": cod_quote, "codCustomer": cod_customer, "numberOfInstallments": 1},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def pix_status(pix_id):
    """Status do PIX: CREATED, PENDING, PAID, EXPIRED, REFUNDED."""
    r = requests.get(
        f"{URL_PIX}/v1/pix/{pix_id}/status",
        headers={"Authorization": f"Bearer {_get_token()}", "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ── 3b. Cartão de crédito (fluxo 'advanced' do guia oficial) ─────────────────
def create_cc_presession(cod_quote, cod_customer, installments):
    """
    Cria a PRÉ-SESSÃO de cartão (fluxo 'advanced'). Retorna { message, expiresIn }.
    NÃO retorna uuid — a URL de pagamento é montada a partir do próprio cod_quote
    (ver cc_payment_url). Só seguir se o 'pendent' do cliente NÃO contiver 'serpro'.
    Mínimo de operação: 25 USD.
    """
    r = requests.post(
        f"{URL_CC}/v1/credit-card/pre-session",
        headers=_headers(),
        json={"codCustomer": cod_customer, "codQuote": cod_quote,
              "flowType": "advanced", "numberOfInstallments": installments},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def cc_payment_url(cod_quote, brl_quantity, installments):
    """URL hospedada da Braza onde o cliente digita o cartão (fluxo advanced)."""
    return (f"{URL_PAYMENT}/payment/cc-installments/{cod_quote}"
            f"?brlQuantity={brl_quantity}&installments={installments}&flowType=advanced")


def cc_status(cod_quote):
    """Status do pagamento por cartão. Pago quando isApproved == true."""
    r = requests.get(
        f"{URL_CC}/v1/credit-card/status/{cod_quote}",
        headers={"Authorization": f"Bearer {_get_token()}", "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ── 4. Venda por codQuote (p/ reconciliação: identifier == orderRef) ────────
def get_sale(cod_quote):
    """
    Detalhe da venda pelo id da cotação. Traz 'identifier' (=externalId que
    enviamos = orderRef da reserva) e o statusLabel/statusName. Usado no webhook
    para descobrir qual reserva do HQ dar baixa.
    """
    r = requests.get(
        f"{URL_SALES}/v3/{cod_quote}",
        headers={"Authorization": f"Bearer {_get_token()}", "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()
