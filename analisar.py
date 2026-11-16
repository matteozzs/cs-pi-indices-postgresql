#!/usr/bin/env python3
"""
Gera o resumo estatistico a partir dos CSVs brutos de uma pasta de resultados.
Uso:  python analisar.py resultados/<pasta>
Saidas: resumo_leitura.csv, resumo_escrita.csv, resumo.md
"""
import csv
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def ler(arq):
    if not arq.exists():
        return []
    with open(arq, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def p95(valores):
    v = sorted(valores)
    if len(v) == 1:
        return v[0]
    pos = 0.95 * (len(v) - 1)  # interpolacao linear (mesmo metodo do Excel PERCENTIL.INC)
    i = math.floor(pos)
    return v[i] + (v[min(i + 1, len(v) - 1)] - v[i]) * (pos - i)


def ic95_mediana(valores, n_boot=2000, semente=42):
    """Intervalo de confianca de 95% da mediana por bootstrap (semente fixa = resultado reprodutivel)."""
    if len(valores) < 5:
        return "", ""
    rng = random.Random(semente)
    meds = sorted(statistics.median(rng.choices(valores, k=len(valores))) for _ in range(n_boot))
    return round(meds[int(0.025 * n_boot)], 3), round(meds[int(0.975 * n_boot) - 1], 3)


def mann_whitney_p(a, b):
    """Teste de Mann-Whitney (bilateral, aproximacao normal com correcao de empates).
    Nao exige distribuicao normal, adequado para tempos de execucao."""
    if len(a) < 5 or len(b) < 5:
        return ""
    todos = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    postos, i = [0.0] * len(todos), 0
    empates = 0.0
    while i < len(todos):
        j = i
        while j + 1 < len(todos) and todos[j + 1][0] == todos[i][0]:
            j += 1
        for k in range(i, j + 1):
            postos[k] = (i + j) / 2 + 1
        t = j - i + 1
        empates += t ** 3 - t
        i = j + 1
    n1, n2 = len(a), len(b)
    r1 = sum(pk for pk, (_, g) in zip(postos, todos) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - empates / (n * (n - 1)))
    if var <= 0:
        return 1.0
    z = (u - n1 * n2 / 2) / math.sqrt(var)
    return float(f"{math.erfc(abs(z) / math.sqrt(2)):.3g}")


def estat(valores):
    lo, hi = ic95_mediana(valores)
    media = statistics.fmean(valores)
    dp = statistics.stdev(valores) if len(valores) > 1 else 0.0
    return {
        "ic95_mediana_inf": lo,
        "ic95_mediana_sup": hi,
        "cv_pct": round(dp / media * 100, 1) if media else "",
        "n": len(valores),
        "media_ms": round(statistics.fmean(valores), 3),
        "mediana_ms": round(statistics.median(valores), 3),
        "p95_ms": round(p95(valores), 3),
        "desvio_padrao_ms": round(statistics.stdev(valores), 3) if len(valores) > 1 else 0.0,
        "min_ms": round(min(valores), 3),
        "max_ms": round(max(valores), 3),
    }


def resumir(pasta):
    pasta = Path(pasta)
    leitura = ler(pasta / "medicoes_leitura.csv")
    escrita = ler(pasta / "medicoes_escrita.csv")
    indices = ler(pasta / "indices.csv")

    # ---------------- leitura
    grupos = defaultdict(list)
    extra = defaultdict(lambda: {"usou": [], "hit": [], "read": [], "nos": set(), "linhas": set(), "cli": []})
    for r in leitura:
        k = (int(r["volume"]), r["consulta"], r["cenario"], r["cache"])
        grupos[k].append(float(r["tempo_execucao_ms"]))
        e = extra[k]
        e["usou"].append(int(r["usou_indice"]))
        if r.get("tempo_cliente_ms"):
            e["cli"].append(float(r["tempo_cliente_ms"]))
        e["hit"].append(float(r["shared_hit_blocks"] or 0))
        e["read"].append(float(r["shared_read_blocks"] or 0))
        e["nos"].add(r["no_raiz"])
        e["linhas"].add(r["linhas_retornadas"])

    tam_idx = defaultdict(float)
    for r in indices:
        if r["consulta"] != "ESCRITA":
            tam_idx[(int(r["volume"]), r["consulta"], r["cenario"])] += float(r["tamanho_mb"])

    linhas_res = []
    for k in sorted(grupos):
        vol, q, cen, cache = k
        s = estat(grupos[k])
        base = grupos.get((vol, q, "sem_indice", cache))
        if base:
            mb = statistics.median(base)
            s["speedup_mediana"] = round(mb / s["mediana_ms"], 2) if s["mediana_ms"] else ""
            s["variacao_pct"] = round((s["mediana_ms"] - mb) / mb * 100, 1) if mb else ""
            if cen != "sem_indice":
                s["p_valor_vs_baseline"] = mann_whitney_p(grupos[k], base)
        e = extra[k]
        if e["cli"]:
            s["mediana_cliente_ms"] = round(statistics.median(e["cli"]), 3)
            s["p95_cliente_ms"] = round(p95(e["cli"]), 3)
            bcli = extra.get((vol, q, "sem_indice", cache), {}).get("cli") if (vol, q, "sem_indice", cache) in extra else None
            if bcli:
                s["speedup_cliente"] = round(statistics.median(bcli) / s["mediana_cliente_ms"], 2)
        linhas_res.append({
            "volume": vol, "consulta": q, "cenario": cen, "cache": cache, **s,
            "pct_rodadas_usou_indice": round(100 * sum(e["usou"]) / len(e["usou"]), 1),
            "mediana_blocos_cache": statistics.median(e["hit"]),
            "mediana_blocos_disco": statistics.median(e["read"]),
            "tamanho_indices_mb": round(tam_idx.get((vol, q, cen), 0.0), 3),
            "no_raiz_plano": ";".join(sorted(e["nos"])),
        })

    campos = ["volume", "consulta", "cenario", "cache", "n", "media_ms", "mediana_ms", "ic95_mediana_inf",
              "ic95_mediana_sup", "p95_ms", "desvio_padrao_ms", "cv_pct", "min_ms", "max_ms", "speedup_mediana",
              "variacao_pct", "p_valor_vs_baseline", "mediana_cliente_ms", "p95_cliente_ms", "speedup_cliente",
              "pct_rodadas_usou_indice",
              "mediana_blocos_cache", "mediana_blocos_disco", "tamanho_indices_mb", "no_raiz_plano"]
    with open(pasta / "resumo_leitura.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(linhas_res)

    # ---------------- escrita
    ge = defaultdict(list)
    for r in escrita:
        ge[(int(r["volume"]), r["operacao"], r["configuracao"])].append(float(r["tempo_escrita_ms"]))
    linhas_esc = []
    for (vol, op, cfg) in sorted(ge):
        s = estat(ge[(vol, op, cfg)])
        base = ge.get((vol, op, "sem_indice"))
        if base and cfg != "sem_indice":
            mb = statistics.median(base)
            s["sobrecusto_pct"] = round((s["mediana_ms"] - mb) / mb * 100, 1) if mb else ""
            s["fator_mais_lento"] = round(s["mediana_ms"] / mb, 2) if mb else ""
            s["p_valor_vs_sem_indice"] = mann_whitney_p(ge[(vol, op, cfg)], base)
        linhas_esc.append({"volume": vol, "operacao": op, "configuracao": cfg, **s})
    campos_e = ["volume", "operacao", "configuracao", "n", "media_ms", "mediana_ms", "ic95_mediana_inf",
                "ic95_mediana_sup", "p95_ms", "desvio_padrao_ms", "cv_pct", "min_ms", "max_ms", "sobrecusto_pct",
                "fator_mais_lento", "p_valor_vs_sem_indice"]
    with open(pasta / "resumo_escrita.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos_e, extrasaction="ignore")
        w.writeheader()
        w.writerows(linhas_esc)

    # ---------------- resumo legivel
    md = ["# Resumo do experimento", "", f"Pasta: `{pasta.name}`", "",
          "## Leitura (mediana em ms, cache quente)", "",
          "| Volume | Consulta | Cenário | Mediana (IC95%) | P95 | Speedup | p-valor | Cliente (ms) | Usou índice | Tam. índice (MB) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in linhas_res:
        if r["cache"] == "quente":
            md.append(f"| {r['volume']:,} | {r['consulta']} | {r['cenario']} | {r['mediana_ms']} "
                      f"({r['ic95_mediana_inf']} a {r['ic95_mediana_sup']}) | {r['p95_ms']} | "
                      f"{r.get('speedup_mediana', '')} | {r.get('p_valor_vs_baseline', '')} | "
                      f"{r.get('mediana_cliente_ms', '')} | {r['pct_rodadas_usou_indice']}% | "
                      f"{r['tamanho_indices_mb']} |".replace(",", "."))
    md += ["", "## Escrita (mediana do tempo de escrita em ms)", "",
           "| Volume | Operação | Configuração | Mediana | P95 | Sobrecusto |", "|---|---|---|---|---|---|"]
    for r in linhas_esc:
        md.append(f"| {r['volume']:,} | {r['operacao']} | {r['configuracao']} | {r['mediana_ms']} | {r['p95_ms']} | "
                  f"{str(r.get('sobrecusto_pct', '')) + '%' if r.get('sobrecusto_pct', '') != '' else ''} |"
                  .replace(",", "."))
    (pasta / "resumo.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Resumo gerado em {pasta}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Uso: python analisar.py resultados/<pasta>")
    resumir(sys.argv[1])
