"""Casamento conservador de recursos recriáveis, independente do ID antigo.

IDs do snapshot são apenas chaves para remapear referências depois do casamento.
Ambiguidade nunca autoriza remoção: o chamador recebe os itens sem par.
"""
from collections import Counter


def parear(desejados, atuais, assinatura, compatibilidade, pontuacao, *, minimo=1):
    """Pares um-a-um: assinatura exata, depois melhor candidato único e recíproco."""
    restantes_d = set(range(len(desejados)))
    restantes_a = set(range(len(atuais)))
    pares = {}
    for i in range(len(desejados)):
        candidatos = [j for j in restantes_a if compatibilidade(desejados[i], atuais[j])
                      and assinatura(desejados[i]) == assinatura(atuais[j])]
        iguais = [k for k in restantes_d if assinatura(desejados[k]) == assinatura(desejados[i])]
        if len(candidatos) == len(iguais) == 1:
            j = candidatos[0]
            pares[i] = j
            restantes_d.remove(i)
            restantes_a.remove(j)
    while True:
        opcoes = {}
        for i in restantes_d:
            classificados = sorted(((pontuacao(desejados[i], atuais[j]), j) for j in restantes_a
                                   if compatibilidade(desejados[i], atuais[j])), reverse=True)
            if classificados and classificados[0][0] >= minimo and (len(classificados) == 1 or classificados[0][0] > classificados[1][0]):
                opcoes[i] = classificados[0]
        seguros = [(i, j) for i, (_, j) in opcoes.items()
                   if sum(other_j == j for _, other_j in opcoes.values()) == 1]
        if not seguros:
            break
        for i, j in seguros:
            pares[i] = j
            restantes_d.remove(i)
            restantes_a.remove(j)
    return pares, restantes_d, restantes_a


def assinatura_mensagem(registro):
    """Campos persistentes após replay por webhook; sem IDs, URL efêmera ou timestamp."""
    import json
    return (registro.get('conteudo', ''),
            json.dumps(registro.get('embeds') or [], sort_keys=True, ensure_ascii=False),
            tuple((a.get('nome'), a.get('tamanho')) for a in registro.get('anexos') or []))


def atribuir_mensagens(registros, atuais, assinatura_backup, assinatura_atual):
    """Multiconjunto, preserva duplicatas legítimas e retorna índices ausentes/sobrando."""
    por_assinatura = {}
    for j, atual in enumerate(atuais):
        por_assinatura.setdefault(assinatura_atual(atual), []).append(j)
    usados = set()
    ausentes = []
    for i, registro in enumerate(registros):
        fila = por_assinatura.get(assinatura_backup(registro), [])
        if fila:
            usados.add(fila.pop(0))
        else:
            ausentes.append(i)
    return ausentes, set(range(len(atuais))) - usados
