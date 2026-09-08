#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_data.py
Gera data.json a partir do quadro Trello "Esteira Produção Disciplinas",
no formato exato que o painel (index.html) espera em `DATA`:
  { meta, cards[], disciplinas[], disciplinas_lista[] }

Credenciais via variáveis de ambiente (definidas como Secrets no GitHub Actions):
  TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID

Como funciona (visão geral):
  1. Busca as listas do board (para saber o nome de cada lista/etapa).
  2. Busca todos os cards do board (ativos e arquivados) com campos básicos.
  3. Busca o histórico de movimentação entre listas (ações "updateCard" com
     mudança de lista) para reconstruir, por card, quando ele ENTROU em cada
     lista (stages[label].ini) e quando SAIU dela (stages[label].fim), além
     de quem foi o responsável por cada etapa concluída (resp_stages) — aqui
     assumimos que "responsável pela etapa" = quem moveu o card para fora
     daquela lista (idMemberCreator da ação de saída).

  >>> PONTO DE ATENÇÃO (favor validar) <<<
  Não temos acesso ao board real, então essa é a hipótese mais comum para
  reconstruir "responsável por etapa" a partir do Trello puro (sem Power-Up
  pago). Se o quadro da Danielle já usa Custom Fields dedicados (ex.: um
  campo "Responsável" e um campo "Data" por etapa), o resultado desse script
  vai divergir do painel antigo — nesse caso, troque a função
  `build_resp_stages_from_actions` por uma leitura dos Custom Fields
  (`customFieldItems=true` na chamada de cards) e ajuste ao formato do board.
  Rode uma vez, compare "Responsável" no data.json com o que você já conhece
  do time, e ajuste esta função se necessário.

Classificação por nome do card (padrão observado):
  {PROJETO}_{UNIDADE}_{TIPO}_{CODIGO}_{DISCIPLINA}
  ex.: GRD2026.2_U4_BDQ_AFYA262125_COMPOSIÇÃO NUTRICIONAL...
  Cards fora desse padrão caem em pipeline "extras".
"""

import os
import re
import sys
import json
import time
import unicodedata
from datetime import datetime, timezone
from collections import defaultdict

import requests

TRELLO_KEY = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN", "")
BOARD_ID = os.environ.get("TRELLO_BOARD_ID", "")
API = "https://api.trello.com/1"

if not (TRELLO_KEY and TRELLO_TOKEN and BOARD_ID):
    sys.exit("Faltam variáveis de ambiente: TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID")

AUTH = {"key": TRELLO_KEY, "token": TRELLO_TOKEN}
SESSION = requests.Session()


def trello_get(path, **params):
    """GET simples com retry leve para 429 (rate limit)."""
    qs = dict(AUTH)
    qs.update(params)
    for attempt in range(6):
        r = SESSION.get(f"{API}{path}", params=qs, timeout=30)
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def trello_get_paginated_actions(board_id, filter_types, limit=1000):
    """Pagina /boards/{id}/actions usando o cursor `before` (id da última ação)."""
    out = []
    before = None
    while True:
        params = {"filter": filter_types, "limit": limit, "fields": "all"}
        if before:
            params["before"] = before
        batch = trello_get(f"/boards/{board_id}/actions", **params)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < limit:
            break
        before = batch[-1]["id"]
    return out


# ---------------------------------------------------------------------------
# Constantes que espelham o front-end (index.html) — mantenha em sincronia.
# ---------------------------------------------------------------------------
STAGE_DEFS = {
    "TEMA": ["Recebimento", "Qualidade 01", "Qualidade 02", "DI", "Tester DI", "Revisão", "DG", "Web", "Tester Conteúdo"],
    "OBJ":  ["Recebimento", "Qualidade 02", "Revisão", "DG", "Web", "Tester Conteúdo"],
    "VIDEO": ["Recebimento", "Roteiro", "Liberação Conteudista", "Gravação", "Edição", "Web", "Tester Conteúdo"],
    "BDQ":  ["Recebimento", "Web (publicado)"],
}

# Lista (nome em maiúsculas, como aparece no Trello) -> macro-fase.
# Ajuste aqui se os nomes das listas do board real forem diferentes.
LIST_TO_MACRO = {
    "CARDS CONTEÚDO": "Intake",
    "AGUARDANDO": "Intake",
    "QUALIDADE 01": "Produção Conteúdo",
    "QUALIDADE 02": "Produção Conteúdo",
    "DI": "Produção Conteúdo",
    "TESTER DI": "Produção Conteúdo",
    "REVISÃO": "Produção Conteúdo",
    "DG": "Produção Conteúdo",
    "AJUSTE PROFESSOR": "Produção Conteúdo",
    "ROTEIRO": "Produção Vídeo",
    "LIBERAÇÃO CONTEUDISTA": "Produção Vídeo",
    "ACIONADO INTERNO": "Produção Vídeo",
    "ACIONADO EXTERNO": "Produção Vídeo",
    "LIBERADO EXTERNO": "Produção Vídeo",
    "GRAVAÇÃO": "Produção Vídeo",
    "EDIÇÃO": "Produção Vídeo",
    "WEB": "Publicação",
    "TESTER CONTEÚDO": "Publicação",
    "TESTER": "Publicação",
    "CAPACITADOS": "Concluído",
    "FINALIZADOS": "Concluído",
    "ARQUIVADOS": "Concluído",
}

# Etapas com "responsável" — nomes exatamente como usados no front-end (STAGE_DEFS),
# usados para popular resp_stages / a aba "Funções".
FUNCAO_RELEVANT_STAGES = {
    "Qualidade 01", "Qualidade 02", "Tester DI", "Tester Conteúdo", "DI", "DG",
    "Revisão", "Roteiro Revisão", "Liberação Conteudista", "Gravação", "Edição", "Web",
}

# Roster conhecido — ajuste/complete conforme a composição real das equipes.
TEAM_ROSTER = {
    "Desenho Educacional": ["Giselly", "Amanda", "Roberta", "Solange", "Anderson"],
    "Soluções Educacionais": ["Thiago", "Vinicius"],
    "Design e Mídia Digital": ["Olivia", "Andressa", "Cristiane", "David", "Fabio", "Gustavo", "Lucas", "Matheus"],
}
PESSOA_TO_EQUIPE = {}
for equipe, pessoas in TEAM_ROSTER.items():
    for p in pessoas:
        PESSOA_TO_EQUIPE[p] = equipe

CARD_NAME_RE = re.compile(
    r"^(?P<projeto>[A-Z0-9.]+)_U(?P<unidade>[1-4]|PE)_(?P<tipo>[A-Z]+)_(?P<codigo>[A-Z0-9]+)_(?P<disciplina>.+)$"
)


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def classify_card_name(nome):
    """Extrai unidade/tipo/codigo/disciplina do nome do card, ou None se fora do padrão."""
    m = CARD_NAME_RE.match(nome.strip())
    if not m:
        return None
    return m.groupdict()


def pipeline_and_stage_def(tipo):
    if tipo == "TEMA":
        return "conteudo", "TEMA"
    if tipo in ("INF", "QUIZ", "POD", "PE"):
        return "conteudo", "OBJ"
    if tipo in ("CONC", "PEV", "EDUV"):
        return "video", "VIDEO"
    if tipo == "BDQ":
        return "bdq", "BDQ"
    return "extras", None


def grupo_extra_de(nome, projeto):
    upper = nome.upper()
    if projeto and projeto.startswith("SOI"):
        return projeto
    if "DMD" in upper:
        return "DMD (documentos internos)"
    if "TESTE" in upper or "MODELO" in upper:
        return "Teste/Modelo"
    return "Avulso / fora do padrão"


def resolve_pessoa_equipe(nome_membro):
    if not nome_membro:
        return "Não identificado", "Não mapeado"
    if "(EXTERNO)" in nome_membro.upper():
        return nome_membro, "Externo"
    primeiro_nome = nome_membro.strip().split()[0]
    equipe = PESSOA_TO_EQUIPE.get(primeiro_nome, "Não mapeado")
    return nome_membro, equipe


def main():
    now = datetime.now(timezone.utc)

    print("Buscando listas do board...", file=sys.stderr)
    lists = trello_get(f"/boards/{BOARD_ID}/lists", fields="name,closed")
    list_name_by_id = {l["id"]: l["name"] for l in lists}

    print("Buscando membros do board...", file=sys.stderr)
    members = trello_get(f"/boards/{BOARD_ID}/members", fields="fullName,username")
    member_name_by_id = {m["id"]: m.get("fullName") or m.get("username") for m in members}

    print("Buscando cards (ativos + arquivados)...", file=sys.stderr)
    cards_raw = trello_get(
        f"/boards/{BOARD_ID}/cards/all",
        fields="name,idList,due,dateLastActivity,shortUrl,closed,idMembers,labels",
        limit=1000,
    )
    print(f"  {len(cards_raw)} cards.", file=sys.stderr)

    print("Buscando histórico de movimentação entre listas (pode levar alguns minutos)...", file=sys.stderr)
    move_actions = trello_get_paginated_actions(BOARD_ID, "updateCard:idList")
    print(f"  {len(move_actions)} eventos de movimentação.", file=sys.stderr)

    # Reconstrói, por card, a sequência cronológica de (data, lista_destino, autor).
    moves_by_card = defaultdict(list)
    for act in move_actions:
        data = act.get("data", {})
        card = data.get("card", {})
        card_id = card.get("id")
        list_after = data.get("listAfter", {}).get("name")
        if not card_id or not list_after:
            continue
        moves_by_card[card_id].append({
            "data": act["date"],
            "lista": list_after,
            "autor_id": act.get("idMemberCreator"),
        })
    for card_id in moves_by_card:
        moves_by_card[card_id].sort(key=lambda e: e["data"])

    cards_out = []
    disciplinas_map = {}  # codigo -> {nome, cards: []}

    for c in cards_raw:
        nome = c["name"]
        etapa_atual = list_name_by_id.get(c["idList"], "?")
        arquivado = bool(c.get("closed"))
        info = classify_card_name(nome)

        parsed = info or {}
        tipo = parsed.get("tipo")
        unidade_num = parsed.get("unidade") if parsed.get("unidade") != "PE" else None
        codigo = parsed.get("codigo")
        disciplina = parsed.get("disciplina")
        projeto = parsed.get("projeto")

        pipeline, stage_key = pipeline_and_stage_def(tipo) if tipo else ("extras", None)
        template_teste = "TESTE" in nome.upper() or "MODELO" in nome.upper()

        moves = moves_by_card.get(c["id"], [])
        stages = {}
        resp_stages = []
        if stage_key:
            labels = STAGE_DEFS[stage_key]
            label_set = set(labels)
            # ini de uma etapa = quando o card entrou na lista correspondente (1ª ocorrência).
            entradas = {}
            for mv in moves:
                if mv["lista"] in label_set and mv["lista"] not in entradas:
                    entradas[mv["lista"]] = mv["data"]
            # fim de uma etapa = data de entrada na PRÓXIMA lista relevante que ele efetivamente visitou.
            visited_in_order = [mv for mv in moves if mv["lista"] in label_set]
            for i, mv in enumerate(visited_in_order):
                stages.setdefault(mv["lista"], {"ini": entradas.get(mv["lista"]), "fim": None})
                if i + 1 < len(visited_in_order):
                    stages[mv["lista"]]["fim"] = visited_in_order[i + 1]["data"]
                autor_nome = member_name_by_id.get(mv["autor_id"])
                if mv["lista"] in FUNCAO_RELEVANT_STAGES and i + 1 < len(visited_in_order):
                    pessoa, equipe = resolve_pessoa_equipe(autor_nome)
                    resp_stages.append({
                        "pessoa": pessoa, "equipe": equipe,
                        "etapa": mv["lista"], "data": visited_in_order[i + 1]["data"],
                    })
            for lbl in labels:
                stages.setdefault(lbl, {"ini": None, "fim": None})
            # Recebimento = entrada do card no board (sem lista anterior) -> data de criação.
            if not stages["Recebimento"]["ini"] and moves:
                stages["Recebimento"]["ini"] = moves[0]["data"]
                stages["Recebimento"]["fim"] = stages["Recebimento"]["ini"]

        dias_parado = round((now - datetime.fromisoformat(c["dateLastActivity"].replace("Z", "+00:00"))).total_seconds() / 86400, 1) if c.get("dateLastActivity") else None

        last_label = STAGE_DEFS[stage_key][-1] if stage_key else None
        data_conclusao = stages.get(last_label, {}).get("fim") if last_label else None

        membros_nomes = [member_name_by_id.get(mid) for mid in c.get("idMembers", [])]
        membros_nomes = [m for m in membros_nomes if m]

        card_obj = {
            "id": c["id"],
            "nome": nome,
            "url": c.get("shortUrl"),
            "codigo": codigo,
            "disciplina": disciplina,
            "unidade_num": unidade_num,
            "unidade_label": ({"1": "Unidade 1", "2": "Unidade 2", "3": "Unidade 3", "4": "Unidade 4"}.get(unidade_num, "Plano de Ensino") if tipo else None),
            "tipo": tipo,
            "pipeline": pipeline,
            "macro_fase": LIST_TO_MACRO.get(etapa_atual.upper(), "Outro"),
            "etapa_atual": etapa_atual.upper(),
            "arquivado": arquivado,
            "template_teste": template_teste,
            "dias_parado": None if arquivado else dias_parado,
            "data_conclusao": data_conclusao,
            "stages": stages,
            "resp_stages": resp_stages,
            "tipo_conteudista": None,  # TODO: derive from Trello label/custom field once known
            "grupo_extra": grupo_extra_de(nome, projeto) if pipeline == "extras" else None,
            "membros": ", ".join(membros_nomes),
        }
        cards_out.append(card_obj)

        if codigo and disciplina:
            d = disciplinas_map.setdefault(codigo, {"codigo": codigo, "nome": disciplina, "cards": []})
            d["cards"].append(card_obj)

    # -------- disciplinas / disciplinas_lista --------
    disciplinas_lista = sorted(
        [{"codigo": k, "nome": v["nome"]} for k, v in disciplinas_map.items()],
        key=lambda d: d["codigo"],
    )
    disciplinas = []
    for codigo, d in disciplinas_map.items():
        tema_cards = [c for c in d["cards"] if c["tipo"] == "TEMA"]
        unidades_concluidas = sum(
            1 for c in tema_cards
            if c["stages"].get("Tester Conteúdo", {}).get("fim")
        )
        disciplinas.append({
            "codigo": codigo,
            "nome": d["nome"],
            "unidades_tema": len(tema_cards),
            "unidades_concluidas": unidades_concluidas,
            "pendentes_conteudo": len(tema_cards) - unidades_concluidas,
        })

    meta = {
        "gerado_em": now.isoformat(),
        "snapshot_ate": now.isoformat(),
        "total_cards": len(cards_out),
        "total_cards_validos": sum(1 for c in cards_out if not c["template_teste"]),
        "total_disciplinas": len(disciplinas_map),
        "total_ativos": sum(1 for c in cards_out if not c["arquivado"] and not c["template_teste"]),
        "total_arquivados": sum(1 for c in cards_out if c["arquivado"]),
    }

    data = {
        "meta": meta,
        "cards": cards_out,
        "disciplinas": disciplinas,
        "disciplinas_lista": disciplinas_lista,
    }

    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"OK — {out_path} atualizado com {len(cards_out)} cards.", file=sys.stderr)


if __name__ == "__main__":
    main()
