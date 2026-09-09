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


def trello_get_list_cards_paginated(list_id, limit=1000, max_pages=25, **params):
    """Busca todos os cards de uma lista, paginando com o cursor `before` (id do card).

    O cursor `before` do endpoint de cards do Trello não é 100% confiável
    (testado: pode devolver cards repetidos em vez de avançar direito).
    Para não arriscar loop infinito nem perder cards, deduplica por id e só
    para quando uma página não trouxer nenhum card novo (ou o número de
    páginas passar de `max_pages`, como cinto de segurança).
    """
    seen = set()
    out = []
    before = None
    for _ in range(max_pages):
        qs = dict(params)
        qs.setdefault("filter", "all")
        qs["limit"] = limit
        if before:
            qs["before"] = before
        batch = trello_get(f"/lists/{list_id}/cards", **qs)
        if not batch:
            break
        novos = 0
        for card in batch:
            if card["id"] not in seen:
                seen.add(card["id"])
                out.append(card)
                novos += 1
        if len(batch) < limit or novos == 0:
            break
        before = batch[-1]["id"]
    return out


def trello_get_all_cards(board_id, lists, **params):
    """Busca todos os cards do board, lista por lista (paginando cada uma)."""
    out = []
    for lst in lists:
        cards = trello_get_list_cards_paginated(lst["id"], **params)
        out.extend(cards)
    return out


# ---------------------------------------------------------------------------
# Constantes que espelham o front-end (index.html) — mantenha em sincronia.
# ---------------------------------------------------------------------------
STAGE_DEFS = {
    "TEMA": ["Recebimento", "Qualidade 01", "Qualidade 02", "DI", "Revisão", "DG", "Web", "Finalizado"],
    "OBJ":  ["Recebimento", "Qualidade 02", "Revisão", "DG", "Web", "Finalizado"],
    "VIDEO": ["Recebimento", "Roteiro", "Liberação Conteudista", "Gravação", "Edição", "Web", "Finalizado"],
    "BDQ":  ["Recebimento", "Web (publicado)"],
}
# Tester DI e Tester Conteúdo saíram do controle (viram auditoria da
# liderança, não são mais etapa obrigatória rastreada no funil).

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
    "CARDS ESTÚDIO": "Produção Vídeo",
    # >>> PONTO DE ATENÇÃO (favor validar) <<<
    # Não sabemos ao certo o que essa lista representa no fluxo real; assumimos
    # que é uma etapa de vídeo aguardando conteudista. Ajuste se necessário.
    "SEM CONTEUDISTA": "Produção Vídeo",
}

# Algumas listas do board real têm sufixos como " | PRODUÇÃO" (provavelmente de
# uma view/swimlane do Trello). Se o nome exato não bater, tenta de novo sem o sufixo.
LIST_NAME_SUFFIXES_TO_STRIP = (" | PRODUÇÃO",)


def resolve_macro_fase(etapa_upper):
    if etapa_upper in LIST_TO_MACRO:
        return LIST_TO_MACRO[etapa_upper]
    for suf in LIST_NAME_SUFFIXES_TO_STRIP:
        if etapa_upper.endswith(suf):
            base = etapa_upper[: -len(suf)]
            if base in LIST_TO_MACRO:
                return LIST_TO_MACRO[base]
    return "Outro"

# Etapas com "responsável" — nomes exatamente como usados no front-end (STAGE_DEFS),
# usados para popular resp_stages / a aba "Funções".
FUNCAO_RELEVANT_STAGES = {
    "Qualidade 01", "Qualidade 02", "DI", "DG",
    "Revisão", "Roteiro", "Liberação Conteudista", "Gravação", "Edição", "Web",
}

# Equipe é definida pela ETAPA concluída, não por quem é a pessoa — organização
# real das 4 áreas (2026).
STAGE_TO_EQUIPE = {
    "Qualidade 01": "Desenho Educacional",
    "Qualidade 02": "Desenho Educacional",
    "DI": "Desenho Educacional",
    "Revisão": "Desenho Educacional",
    "DG": "Design Educacional",
    "Roteiro": "Mídia Digital",
    "Liberação Conteudista": "Mídia Digital",
    "Gravação": "Mídia Digital",
    "Edição": "Mídia Digital",
    "Web": "Soluções Educacionais",
}

# Unidade "U1".."U4" (conteúdo) ou "V1".."V4" (vídeo) ou "PE" isolado (plano de ensino).
# "projeto" usa [^_]+ (em vez de [A-Z0-9.]+) porque projetos como "PÓS" têm acento.
CARD_NAME_RE = re.compile(
    r"^(?P<projeto>[^_]+)_(?:[UV](?P<unidade>[1-4])|(?P<unidade_pe>PE))_"
    r"(?P<tipo>[A-Z0-9]+)_(?P<codigo>[A-Z0-9]+)_(?P<disciplina>.+)$"
)

# Card "Tema" tem um tipo por unidade (T1..T4) em vez do literal "TEMA".
TEMA_TIPO_RE = re.compile(r"^T[1-4]$")


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def classify_card_name(nome):
    """Extrai unidade/tipo/codigo/disciplina do nome do card, ou None se fora do padrão."""
    # Alguns cards têm tabs em vez de "_" entre os segmentos (erro de digitação no Trello);
    # normaliza para não perder a classificação por causa disso.
    nome_norm = re.sub(r"[_\t]+", "_", nome.strip())
    m = CARD_NAME_RE.match(nome_norm)
    if not m:
        return None
    groups = m.groupdict()
    if groups.get("unidade_pe"):
        groups["unidade"] = "PE"
    del groups["unidade_pe"]
    return groups


def is_tema_tipo(tipo):
    return tipo == "TEMA" or bool(TEMA_TIPO_RE.match(tipo or ""))


def pipeline_and_stage_def(tipo):
    if is_tema_tipo(tipo):
        return "conteudo", "TEMA"
    if tipo in ("INF", "QUIZ", "POD", "PE", "INT"):
        return "conteudo", "OBJ"
    if tipo in ("CONC", "PEV", "EDUV", "EADUV"):
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


def resolve_equipe(nome_membro, etapa):
    """Equipe é derivada da ETAPA concluída (STAGE_TO_EQUIPE), não da pessoa —
    exceto para externos, que ficam numa equipe própria independente da etapa."""
    if not nome_membro:
        return "Não identificado", "Não mapeado"
    if "(EXTERNO)" in nome_membro.upper():
        return nome_membro, "Externo"
    return nome_membro, STAGE_TO_EQUIPE.get(etapa, "Não mapeado")


# Para TEMA/OBJ, o board tem Custom Fields dedicados de responsável e datas por
# etapa (achado ao comparar com um CSV exportado do Trello) — muito mais
# confiável que inferir pelo histórico de movimentação, que não cobre cards
# fora da janela de retenção de ações do Trello. Vídeo/BDQ continuam usando o
# histórico de movimentação (o mapeamento de campos lá não é tão direto).
# Etapa (STAGE_DEFS) -> (nome do campo de responsável, sufixo dos campos de data
# "DT INÍCIO {sufixo}" / "DT FIM {sufixo}") — nomes exatamente como no Trello.
STAGE_CUSTOM_FIELD = {
    "Qualidade 01": ("QUALIDADE 01", "QLD 01"),
    "Qualidade 02": ("QUALIDADE 02", "QLD 02"),
    "DI": ("DI", "DI"),
    "Revisão": ("REVISOR", "REV"),
    "DG": ("DG", "DG"),
    "Web": ("WEB", "WEB"),
}


def fetch_custom_field_defs(board_id):
    """Busca as definições de Custom Fields do board (id -> nome/tipo/opções)."""
    fields = trello_get(f"/boards/{board_id}/customFields")
    defs = {}
    for f in fields:
        options = {}
        if f.get("type") == "list":
            for opt in f.get("options", []):
                options[opt["id"]] = opt.get("value", {}).get("text")
        defs[f["id"]] = {"name": f["name"], "type": f.get("type"), "options": options}
    return defs


def build_custom_values(card, field_defs):
    """Resolve os Custom Field Items de um card para {nome_do_campo: valor}."""
    values = {}
    for item in card.get("customFieldItems") or []:
        fdef = field_defs.get(item.get("idCustomField"))
        if not fdef:
            continue
        val = item.get("value") or {}
        if fdef["type"] == "list":
            resolved = fdef["options"].get(item.get("idValue"))
        elif "text" in val:
            resolved = val["text"]
        elif "date" in val:
            resolved = val["date"]
        elif "number" in val:
            resolved = val["number"]
        elif "checked" in val:
            resolved = val["checked"]
        else:
            resolved = None
        if resolved not in (None, ""):
            values[fdef["name"]] = resolved
    return values


def card_created_at(card_id):
    """Data de criação do card, decodificada do próprio ObjectId (sempre disponível,
    ao contrário do histórico de ações, que tem janela de retenção limitada)."""
    return datetime.fromtimestamp(int(card_id[:8], 16), tz=timezone.utc).isoformat()


def main():
    now = datetime.now(timezone.utc)

    print("Buscando listas do board...", file=sys.stderr)
    lists = trello_get(f"/boards/{BOARD_ID}/lists", fields="name,closed", filter="all")
    list_name_by_id = {l["id"]: l["name"] for l in lists}

    print("Buscando membros do board...", file=sys.stderr)
    members = trello_get(f"/boards/{BOARD_ID}/members", fields="fullName,username")
    member_name_by_id = {m["id"]: m.get("fullName") or m.get("username") for m in members}

    print("Buscando Custom Fields do board...", file=sys.stderr)
    custom_field_defs = fetch_custom_field_defs(BOARD_ID)

    print("Buscando cards (ativos + arquivados)...", file=sys.stderr)
    cards_raw = trello_get_all_cards(
        BOARD_ID, lists,
        fields="name,idList,due,dateLastActivity,shortUrl,closed,idMembers,labels",
        customFieldItems="true",
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
        macro_fase = resolve_macro_fase(etapa_atual.upper())

        moves = moves_by_card.get(c["id"], [])
        stages = {}
        resp_stages = []
        custom = build_custom_values(c, custom_field_defs)

        if stage_key in ("TEMA", "OBJ"):
            # Conteúdo (Tema/Objetos): lê direto dos Custom Fields de
            # responsável e data por etapa — mais preciso que inferir pelo
            # histórico de movimentação, e não depende da janela de retenção
            # de ações do Trello.
            labels = STAGE_DEFS[stage_key]
            for lbl in labels:
                stages.setdefault(lbl, {"ini": None, "fim": None})
            for lbl, (pessoa_field, date_suffix) in STAGE_CUSTOM_FIELD.items():
                if lbl not in labels:
                    continue
                ini = custom.get(f"DT INÍCIO {date_suffix}")
                fim = custom.get(f"DT FIM {date_suffix}")
                if ini:
                    stages[lbl]["ini"] = ini
                if fim:
                    stages[lbl]["fim"] = fim
                pessoa_nome = custom.get(pessoa_field)
                if pessoa_nome and fim:
                    pessoa, equipe = resolve_equipe(pessoa_nome, lbl)
                    resp_stages.append({"pessoa": pessoa, "equipe": equipe, "etapa": lbl, "data": fim})
            criado_em = card_created_at(c["id"])
            stages["Recebimento"] = {"ini": criado_em, "fim": criado_em}
            if "Finalizado" in labels and macro_fase == "Concluído" and c.get("dateLastActivity"):
                stages["Finalizado"] = {"ini": c["dateLastActivity"], "fim": c["dateLastActivity"]}
        elif stage_key:
            # Vídeo/BDQ: mapeamento de Custom Field por etapa não é tão direto
            # (o board tem campos como "LIBERAÇÃO GRAVAÇÃO" sem correspondência
            # clara com STAGE_DEFS.VIDEO) — continua pelo histórico de
            # movimentação entre listas.
            labels = STAGE_DEFS[stage_key]
            # Listas do Trello vêm em CAIXA ALTA; STAGE_DEFS usa Title Case para exibição.
            # Casa por nome normalizado (maiúsculo) e devolve o rótulo "bonito" canônico.
            label_by_upper = {lbl.upper(): lbl for lbl in labels}

            def canon_label(lista_raw):
                return label_by_upper.get((lista_raw or "").strip().upper())

            moves_canon = []
            for mv in moves:
                canon = canon_label(mv["lista"])
                if canon:
                    moves_canon.append({**mv, "lista": canon})

            # ini de uma etapa = quando o card entrou na lista correspondente (1ª ocorrência).
            entradas = {}
            for mv in moves_canon:
                if mv["lista"] not in entradas:
                    entradas[mv["lista"]] = mv["data"]
            # fim de uma etapa = data de entrada na PRÓXIMA lista relevante que ele efetivamente visitou.
            visited_in_order = moves_canon
            for i, mv in enumerate(visited_in_order):
                stages.setdefault(mv["lista"], {"ini": entradas.get(mv["lista"]), "fim": None})
                if i + 1 < len(visited_in_order):
                    stages[mv["lista"]]["fim"] = visited_in_order[i + 1]["data"]
                autor_nome = member_name_by_id.get(mv["autor_id"])
                if mv["lista"] in FUNCAO_RELEVANT_STAGES and i + 1 < len(visited_in_order):
                    pessoa, equipe = resolve_equipe(autor_nome, mv["lista"])
                    resp_stages.append({
                        "pessoa": pessoa, "equipe": equipe,
                        "etapa": mv["lista"], "data": visited_in_order[i + 1]["data"],
                    })
            for lbl in labels:
                stages.setdefault(lbl, {"ini": None, "fim": None})
            stages["Recebimento"] = {"ini": card_created_at(c["id"]), "fim": card_created_at(c["id"])}
            if "Finalizado" in labels and macro_fase == "Concluído" and c.get("dateLastActivity"):
                stages["Finalizado"] = {"ini": c["dateLastActivity"], "fim": c["dateLastActivity"]}

        # Arquivado sem nenhuma movimentação registrada (nenhum Custom Field nem
        # histórico de lista além da entrada) = card sem produção real, nunca
        # chegou a começar. "Recebimento" e "Finalizado" não contam: são
        # sintéticos (data de criação do card / card caiu numa lista de
        # conclusão), não vêm de um campo complementar de fato preenchido.
        # Trata como template_teste para sair de toda análise.
        if stage_key and arquivado and not any(
            v.get("fim") for lbl, v in stages.items() if lbl not in ("Recebimento", "Finalizado")
        ):
            template_teste = True

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
            "projeto": projeto,
            "unidade_num": unidade_num,
            "unidade_label": ({"1": "Unidade 1", "2": "Unidade 2", "3": "Unidade 3", "4": "Unidade 4"}.get(unidade_num, "Plano de Ensino") if tipo else None),
            "tipo": tipo,
            "pipeline": pipeline,
            "macro_fase": macro_fase,
            "etapa_atual": etapa_atual.upper(),
            "arquivado": arquivado,
            "template_teste": template_teste,
            "dias_parado": None if arquivado else dias_parado,
            "data_conclusao": data_conclusao,
            "stages": stages,
            "resp_stages": resp_stages,
            "tipo_conteudista": custom.get("TIPO CONTEUDISTA"),
            "qtd_caracteres": custom.get("QTD CARACTERES"),
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
        tema_cards = [c for c in d["cards"] if is_tema_tipo(c["tipo"])]
        unidades_concluidas = sum(
            1 for c in tema_cards
            if c["stages"].get("Finalizado", {}).get("fim")
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
