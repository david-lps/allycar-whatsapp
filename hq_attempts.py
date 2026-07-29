"""
hq_attempts.py — Cruza a jornada do lead com as tentativas de reserva da HQ.

A HQ registra cada tentativa de reserva em /car-rental/reservation-attempts
(inclui last_step, booker_ip, datas, vehicle_class_id, cidade...). Como essas
tentativas quase nunca têm telefone/email (só no último passo), o elo com o
lead do WhatsApp é o IP do clique (capturado no redirect /r/<code>).

Este módulo busca as tentativas (cache curto) e o mapa de vehicle-classes
(id -> nome, cache longo), monta um índice por IP e resolve o "melhor" attempt
(passo mais avançado) para um conjunto de IPs.
"""

import time
import requests

# Reaproveita a config da HQ já definida no agente (host, auth, brand)
from main_agent import HQ_API_HOST, HQ_API_AUTH, HQ_BRAND_ID

_ATTEMPTS_TTL = 300      # 5 min — evita bater na HQ a cada abertura do painel
_CLASSES_TTL = 3600      # 1 h
_ATTEMPTS_LIMIT = 1000   # tentativas mais recentes (cobre semanas de leads)

_cache = {
    "attempts": None, "attempts_ts": 0.0, "idx": None,
    "classes": None, "classes_ts": 0.0,
}

# Rótulo de cada passo do funil (last_step da HQ)
STEP_LABEL = {
    1: "buscou datas",
    2: "viu a frota e os preços",
    3: "escolheu o veículo",
    4: "preencheu os dados",
    5: "foi ao pagamento",
}


def _get(path, params):
    r = requests.get(
        f"{HQ_API_HOST}/api-america-miami/{path}",
        params=params,
        headers={"Authorization": HQ_API_AUTH, "Content-Type": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _classes():
    """Mapa vehicle_class_id -> nome (cache de 1h)."""
    now = time.time()
    if _cache["classes"] is not None and (now - _cache["classes_ts"]) < _CLASSES_TTL:
        return _cache["classes"]
    mapa = _cache["classes"] or {}
    try:
        d = _get("fleets/vehicle-classes", {"limit": 200})
        mapa = {}
        for c in d.get("fleets_vehicle_classes", []):
            if c.get("id") is not None:
                mapa[c["id"]] = c.get("name") or c.get("label") or f"Classe {c['id']}"
        _cache["classes"] = mapa
        _cache["classes_ts"] = now
    except Exception as e:
        print(f"⚠️ hq_attempts: falha ao carregar vehicle-classes: {e}")
    return mapa


def _attempts():
    """Tentativas de reserva recentes + índice por IP (cache de 5 min)."""
    now = time.time()
    if _cache["attempts"] is not None and (now - _cache["attempts_ts"]) < _ATTEMPTS_TTL:
        return _cache["attempts"], _cache["idx"]
    lista = _cache["attempts"] or []
    try:
        d = _get("car-rental/reservation-attempts",
                 {"brand_id": HQ_BRAND_ID, "limit": _ATTEMPTS_LIMIT})
        lista = d if isinstance(d, list) else d.get("data", [])
        idx = {}
        for a in lista:
            ip = (a.get("booker_ip") or "").strip()
            if ip:
                idx.setdefault(ip, []).append(a)
        _cache["attempts"] = lista
        _cache["idx"] = idx
        _cache["attempts_ts"] = now
    except Exception as e:
        print(f"⚠️ hq_attempts: falha ao carregar reservation-attempts: {e}")
    return _cache["attempts"] or [], _cache["idx"] or {}


def funil_global():
    """Funil de conversão de TODOS os visitantes do site (reservation-attempts).
    Retorna contagens CUMULATIVAS por passo (quem chegou >= step N)."""
    lista, _ = _attempts()
    def reached(n):
        return sum(1 for a in lista if (a.get("last_step") or 0) >= n)
    datas = sorted(a.get("created_at", "") for a in lista if a.get("created_at"))
    return {
        "step2": reached(2),
        "step3": reached(3),
        "step4": reached(4),
        "step5": reached(5),
        "periodo_ini": datas[0] if datas else "",
        "periodo_fim": datas[-1] if datas else "",
        "total_amostra": len(lista),
    }


def match_por_ips(ips):
    """Dado um conjunto de IPs (do clique), retorna o 'melhor' attempt
    (passo mais avançado; desempate pelo mais recente) ou None."""
    ips = {(i or "").strip() for i in (ips or []) if (i or "").strip()}
    if not ips:
        return None
    _, idx = _attempts()
    candidatos = []
    for ip in ips:
        candidatos.extend(idx.get(ip, []))
    if not candidatos:
        return None
    classes = _classes()
    melhor = max(candidatos, key=lambda a: ((a.get("last_step") or 0), a.get("created_at") or ""))
    step = melhor.get("last_step") or 0
    vc = melhor.get("vehicle_class_id")
    return {
        "step": step,
        "step_label": STEP_LABEL.get(step, f"step {step}"),
        "vehicle_class_id": vc,
        "veiculo": (classes.get(vc) if vc else "") or "",
        "pick_up_date": melhor.get("pick_up_date") or "",
        "return_date": melhor.get("return_date") or "",
        "created_at": melhor.get("created_at") or "",
        "cidade": melhor.get("city") or "",
        "pais": (melhor.get("country") or "").upper(),
        "ip": melhor.get("booker_ip") or "",
        "n_tentativas": len(candidatos),
    }
