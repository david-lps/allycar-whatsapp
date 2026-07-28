"""
agent_store.py — Persistência das conversas do agente (Postgres).

Guarda o estado das conversas (nome, telefone, idioma, histórico e flags) numa
tabela Postgres, para sobreviver a reinícios do serviço. Se DATABASE_URL não
estiver definido, cai automaticamente para um dicionário em memória (mesmo
comportamento de antes) — assim o app funciona com ou sem banco.

O histórico da conversa contém blocos do SDK Anthropic (objetos). Para gravar,
serializamos cada bloco em dict limpo (text / tool_use / tool_result) e
descartamos blocos de 'thinking' (raciocínio interno, não necessário entre
turnos). Ao recarregar, o histórico volta como dicts — aceitos pela API.
"""

import os
import json

DATABASE_URL = os.getenv("DATABASE_URL")
ATIVO = bool(DATABASE_URL)

_mem = {}  # fallback em memória quando não há DATABASE_URL


def _conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Cria a tabela se necessário. Seguro chamar sempre no boot."""
    if not ATIVO:
        print("ℹ️ agent_store: sem DATABASE_URL — usando memória (sem persistência).")
        return
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS agent_conversas ("
                "  key text PRIMARY KEY,"
                "  state jsonb NOT NULL,"
                "  updated_at timestamptz NOT NULL DEFAULT now()"
                ")"
            )
        print("✅ agent_store: tabela pronta (Postgres).")
    except Exception as e:
        print(f"⚠️ agent_store: falha ao inicializar o banco: {e}")
    finally:
        conn.close()


def _bloco_limpo(b):
    """Converte um bloco (dict ou objeto do SDK) em dict mínimo aceito pela API."""
    if isinstance(b, dict):
        if b.get("type") in ("thinking", "redacted_thinking"):
            return None
        return b
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": getattr(b, "text", "")}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return None  # thinking / desconhecido → descarta


def _serializar(conversa):
    """Estado JSON-safe da conversa (com histórico limpo)."""
    hist = []
    for m in conversa.get("history", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            hist.append({"role": role, "content": content})
            continue
        blocos = [d for d in (_bloco_limpo(b) for b in content) if d]
        if blocos:
            hist.append({"role": role, "content": blocos})
    estado = {k: v for k, v in conversa.items() if k != "history"}
    estado["history"] = hist
    return estado


def salvar(key, conversa):
    estado = _serializar(conversa)
    if not ATIVO:
        _mem[key] = estado
        return
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_conversas (key, state, updated_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                (key, json.dumps(estado, ensure_ascii=False)),
            )
    except Exception as e:
        print(f"⚠️ agent_store: falha ao salvar {key}: {e}")
        _mem[key] = estado  # não perde o turno
    finally:
        conn.close()


def carregar(key):
    if not ATIVO:
        return _mem.get(key)
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM agent_conversas WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None  # jsonb → dict
    except Exception as e:
        print(f"⚠️ agent_store: falha ao carregar {key}: {e}")
        return _mem.get(key)
    finally:
        conn.close()


def buscar_por_ref(code):
    """Encontra a conversa cujo ref_code == code. Retorna (key, conversa) ou (None, None)."""
    if not code:
        return None, None
    if not ATIVO:
        for k, v in _mem.items():
            if v.get("ref_code") == code:
                return k, v
        return None, None
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key, state FROM agent_conversas WHERE state->>'ref_code' = %s LIMIT 1",
                (code,),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else (None, None)
    except Exception as e:
        print(f"⚠️ agent_store: falha ao buscar ref {code}: {e}")
        return None, None
    finally:
        conn.close()


def deletar(key):
    if not ATIVO:
        _mem.pop(key, None)
        return
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agent_conversas WHERE key = %s", (key,))
    except Exception as e:
        print(f"⚠️ agent_store: falha ao deletar {key}: {e}")
    finally:
        conn.close()


def contar():
    if not ATIVO:
        return len(_mem)
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM agent_conversas")
            return cur.fetchone()[0]
    except Exception:
        return len(_mem)
    finally:
        conn.close()


def listar(limit=300):
    """Lista as conversas (mais recentes primeiro) para o dashboard."""
    if not ATIVO:
        return [{"key": k, "state": v, "updated_at": None} for k, v in list(_mem.items())[:limit]]
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key, state, updated_at FROM agent_conversas "
                "ORDER BY updated_at DESC LIMIT %s", (limit,)
            )
            return [
                {"key": r[0], "state": r[1],
                 "updated_at": r[2].strftime("%Y-%m-%d %H:%M") if r[2] else None}
                for r in cur.fetchall()
            ]
    except Exception as e:
        print(f"⚠️ agent_store: falha ao listar: {e}")
        return []
    finally:
        conn.close()
