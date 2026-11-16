#!/usr/bin/env python3
"""
Experimento: impacto de indices no tempo de resposta do PostgreSQL
Equipe 09 - PI Projeto e Pesquisa - UTP 2026/2

Fluxo por volume de dados:
  1. carga da base sintetica sem nenhum indice + VACUUM ANALYZE
  2. leitura: cada consulta roda no baseline (sem indice) e em cada cenario de indice,
     com cache quente (aquecimento + N rodadas) e cache frio (reinicio do banco antes de cada rodada)
  3. escrita: lote de INSERT, UPDATE e DELETE sem indices e com todos os indices
Todos os dados brutos sao gravados em CSV a cada medicao (nada se perde se parar no meio).

Uso rapido (teste de 2 minutos):   python executar_experimento.py --rapido
Uso completo:                      python executar_experimento.py
"""
import argparse
import csv
import datetime as dt
import json
import os
import platform
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

try:
    import psycopg
except ImportError:
    sys.exit("Falta a biblioteca psycopg. Rode:  pip install \"psycopg[binary]\"")

BASE = Path(__file__).resolve().parent

# ---------------------------------------------------------------- consultas
CONSULTAS = {
    "Q1_igualdade": "SELECT * FROM pedidos WHERE cliente_id = 4242",
    "Q2_faixa": ("SELECT count(*), sum(valor) FROM pedidos WHERE data_pedido BETWEEN "
                 "timestamp '2022-06-01' AND timestamp '2022-06-01' + interval '43 hours'"),
    "Q3_like_prefixo": "SELECT * FROM clientes WHERE email LIKE 'cliente4242%'",
    "Q4_ordenacao_limit": "SELECT * FROM pedidos ORDER BY valor DESC LIMIT 10",
    "Q5_juncao": ("SELECT c.nome, p.data_pedido, p.valor FROM clientes c "
                  "JOIN pedidos p ON p.cliente_id = c.id WHERE c.id BETWEEN 1000 AND 1050"),
    "Q6_agregacao": ("SELECT status, count(*), avg(valor) FROM pedidos "
                     "WHERE data_pedido >= timestamp '2024-12-13' GROUP BY status"),
    "Q7_baixa_seletividade": "SELECT count(*), avg(valor) FROM pedidos WHERE status = 'ENTREGUE'",
    "Q8_array_gin": "SELECT count(*) FROM pedidos WHERE tags @> ARRAY['tag07','tag13']",
}

# cenario -> {consulta: [(nome_indice, tipo, DDL)]}
IDX = {
    "bt_ped_cliente": ("btree", "CREATE INDEX bt_ped_cliente ON pedidos USING btree (cliente_id)"),
    "bt_ped_data": ("btree", "CREATE INDEX bt_ped_data ON pedidos USING btree (data_pedido)"),
    "bt_cli_email": ("btree", "CREATE INDEX bt_cli_email ON clientes USING btree (email text_pattern_ops)"),
    "bt_ped_valor": ("btree", "CREATE INDEX bt_ped_valor ON pedidos USING btree (valor)"),
    "bt_cli_id": ("btree", "CREATE INDEX bt_cli_id ON clientes USING btree (id)"),
    "bt_ped_status": ("btree", "CREATE INDEX bt_ped_status ON pedidos USING btree (status)"),
    "hs_ped_cliente": ("hash", "CREATE INDEX hs_ped_cliente ON pedidos USING hash (cliente_id)"),
    "br_ped_data": ("brin", "CREATE INDEX br_ped_data ON pedidos USING brin (data_pedido)"),
    "gin_ped_tags": ("gin", "CREATE INDEX gin_ped_tags ON pedidos USING gin (tags)"),
}

# (consulta, cenario, [indices], settings extras)
CASOS = [(q, "sem_indice", [], {}) for q in CONSULTAS] + [
    ("Q1_igualdade", "btree", ["bt_ped_cliente"], {}),
    ("Q1_igualdade", "hash", ["hs_ped_cliente"], {}),
    ("Q2_faixa", "btree", ["bt_ped_data"], {}),
    ("Q2_faixa", "brin", ["br_ped_data"], {}),
    ("Q3_like_prefixo", "btree", ["bt_cli_email"], {}),
    ("Q4_ordenacao_limit", "btree", ["bt_ped_valor"], {}),
    ("Q5_juncao", "btree", ["bt_cli_id", "bt_ped_cliente"], {}),
    ("Q6_agregacao", "btree", ["bt_ped_data"], {}),
    ("Q6_agregacao", "brin", ["br_ped_data"], {}),
    ("Q7_baixa_seletividade", "btree", ["bt_ped_status"], {}),
    # mesmo indice, mas obrigando o planejador a usa-lo: mostra o custo de usar indice no caso errado
    ("Q7_baixa_seletividade", "btree_forcado", ["bt_ped_status"], {"enable_seqscan": "off"}),
    ("Q8_array_gin", "gin", ["gin_ped_tags"], {}),
]

ESCRITA = {
    "INSERT": ("INSERT INTO pedidos SELECT g, 1 + (g % {n_clientes}), timestamp '2025-01-01' + g * interval '1 second', "
               "'PENDENTE', 100.00, ARRAY['tag01','tag02','tag03'] "
               "FROM generate_series({n_pedidos} + 1, {n_pedidos} + {lote}) g"),
    "UPDATE": "UPDATE pedidos SET valor = valor + 1, status = 'ENVIADO' WHERE id BETWEEN 1000 AND 1000 + {lote} - 1",
    "DELETE": "DELETE FROM pedidos WHERE id BETWEEN 1000 AND 1000 + {lote} - 1",
}

INDICE_NODES = {"Index Scan", "Index Only Scan", "Bitmap Index Scan"}


# ---------------------------------------------------------------- utilitarios
def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


class CSVSaida:
    def __init__(self, caminho, campos):
        novo = not caminho.exists()
        self.f = open(caminho, "a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=campos)
        if novo:
            self.w.writeheader()

    def gravar(self, linha):
        self.w.writerow(linha)
        self.f.flush()


def percorrer_plano(no, tipos, indices):
    tipos.append(no.get("Node Type"))
    if no.get("Index Name"):
        indices.add(no["Index Name"])
    for filho in no.get("Plans", []):
        percorrer_plano(filho, tipos, indices)


class Banco:
    def __init__(self, args):
        self.args = args
        self.conn = None

    def dsn(self):
        a = self.args
        return f"host={a.host} port={a.porta} dbname={a.banco} user={a.usuario} password={a.senha}"

    def conectar(self, espera=90):
        fim = time.time() + espera
        while True:
            try:
                self.conn = psycopg.connect(self.dsn(), autocommit=True)
                self.conn.execute("SELECT 1")
                return
            except Exception as e:
                if time.time() > fim:
                    raise RuntimeError(f"Nao consegui conectar no PostgreSQL: {e}")
                time.sleep(1)

    def fechar(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def exec(self, sql, params=None):
        return self.conn.execute(sql, params)

    def um(self, sql):
        return self.conn.execute(sql).fetchone()

    def explain(self, sql, settings=None):
        """Plano real com tempo do servidor. TIMING OFF evita o custo de cronometrar cada no do plano."""
        for k, v in (settings or {}).items():
            self.exec(f"SET {k} = {v}")
        try:
            res = self.um(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) {sql}")[0][0]
        finally:
            for k in (settings or {}):
                self.exec(f"RESET {k}")
        return res

    def tempo_cliente(self, sql, settings=None):
        """Executa a consulta de verdade e mede no cliente, incluindo o envio das linhas (tempo de resposta)."""
        for k, v in (settings or {}).items():
            self.exec(f"SET {k} = {v}")
        try:
            cur = self.conn.cursor()
            t0 = time.perf_counter()
            cur.execute(sql)
            cur.fetchall()
            return (time.perf_counter() - t0) * 1000
        finally:
            for k in (settings or {}):
                self.exec(f"RESET {k}")


def rodar_cmd(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def reiniciar_frio(banco, args):
    """Esvazia shared_buffers (reinicio) e tenta esvaziar o cache do sistema operacional."""
    banco.fechar()
    ok, saida = rodar_cmd(args.cmd_parar)
    if not ok:
        raise RuntimeError(f"Falha ao parar o banco: {saida}")
    cache_ok = False
    if args.cmd_limpar_cache:
        cache_ok, _ = rodar_cmd(args.cmd_limpar_cache)
    ok, saida = rodar_cmd(args.cmd_iniciar)
    if not ok:
        raise RuntimeError(f"Falha ao iniciar o banco: {saida}")
    banco.conectar()
    return cache_ok


# ---------------------------------------------------------------- fases
def coletar_ambiente(banco, args, pasta):
    params = ["server_version", "shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size",
              "random_page_cost", "seq_page_cost", "autovacuum", "jit", "max_parallel_workers_per_gather",
              "track_io_timing", "shared_preload_libraries"]
    cfg = {}
    for p in params:
        cfg[p] = banco.um(f"SHOW {p}")[0]
    info = {
        "data_hora_inicio": dt.datetime.now().isoformat(timespec="seconds"),
        "postgres_version": banco.um("SELECT version()")[0],
        "configuracao": cfg,
        "host": {
            "sistema": platform.platform(),
            "processador": platform.processor(),
            "cpus_logicas": os.cpu_count(),
            "python": sys.version.split()[0],
            "psycopg": psycopg.__version__,
        },
        "parametros_execucao": {k: (" ".join(v) if isinstance(v, list) else v) for k, v in vars(args).items()
                                if k != "senha"},
        "observacao": "Preencha em ambiente_manual.txt o modelo da CPU, RAM e tipo de disco da maquina.",
    }
    info["hardware"] = coletar_hardware()
    log("Fazendo a varredura de processos da maquina...")
    info["processos_inicio"] = varrer_processos()
    avisar_processos(info["processos_inicio"])
    info["docker"] = coletar_docker(args)
    (pasta / "ambiente.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")


def tentar(cmd):
    try:
        ok, saida = rodar_cmd(cmd)
        return saida if ok else f"erro: {saida[:200]}"
    except FileNotFoundError:
        return "comando nao encontrado"


def coletar_hardware():
    if platform.system() != "Windows":
        return {"cpu": tentar(["sh", "-c", "grep -m1 'model name' /proc/cpuinfo | cut -d: -f2"]),
                "ram": tentar(["sh", "-c", "grep MemTotal /proc/meminfo"])}
    ps = ("$c=Get-CimInstance Win32_Processor; $s=Get-CimInstance Win32_ComputerSystem; "
          "$o=Get-CimInstance Win32_OperatingSystem; "
          "$d=Get-PhysicalDisk | ForEach-Object { $_.FriendlyName + ' | ' + $_.MediaType + ' | ' + $_.BusType + ' | ' "
          "+ [math]::Round($_.Size/1GB) + ' GB' }; "
          "$m=Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [string][math]::Round($_.Capacity/1GB) + ' GB @ ' "
          "+ $_.Speed + ' MHz' }; "
          "[pscustomobject]@{cpu=$c.Name; nucleos=$c.NumberOfCores; threads=$c.NumberOfLogicalProcessors; "
          "ram_gb=[math]::Round($s.TotalPhysicalMemory/1GB,1); pentes_ram=@($m); discos=@($d); "
          "sistema=$o.Caption + ' ' + $o.Version} | ConvertTo-Json")
    saida = tentar(["powershell", "-NoProfile", "-Command", ps])
    try:
        return json.loads(saida)
    except Exception:
        return {"bruto": saida}


def varrer_processos():
    """Fotografia da maquina antes do teste: carga da CPU, memoria livre e processos que mais consomem."""
    if platform.system() != "Windows":
        return {"observacao": "varredura implementada apenas para Windows"}
    ps = ("$cpu=(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average; "
          "$os=Get-CimInstance Win32_OperatingSystem; "
          "$a=Get-Process | Where-Object { $_.Id -ne 0 }; "
          "Start-Sleep -Seconds 3; "
          "$b=Get-Process | Where-Object { $_.Id -ne 0 }; "
          "$t=@{}; foreach($p in $a){ $t[$p.Id]=$p.CPU }; "
          "$lista=foreach($p in $b){ $d=0; if($t.ContainsKey($p.Id) -and $p.CPU -ne $null -and $t[$p.Id] -ne $null)"
          "{ $d=$p.CPU-$t[$p.Id] }; [pscustomobject]@{nome=$p.ProcessName; pid=$p.Id; "
          "cpu_pct=[math]::Round($d/3/[Environment]::ProcessorCount*100,1); memoria_mb=[math]::Round($p.WorkingSet64/1MB)} }; "
          "[pscustomobject]@{carga_cpu_pct=$cpu; "
          "memoria_livre_gb=[math]::Round($os.FreePhysicalMemory/1MB,1); "
          "memoria_total_gb=[math]::Round($os.TotalVisibleMemorySize/1MB,1); "
          "qtd_processos=@($b).Count; "
          "top_cpu=@($lista | Sort-Object cpu_pct -Descending | Select-Object -First 10); "
          "top_memoria=@($lista | Sort-Object memoria_mb -Descending | Select-Object -First 15)} "
          "| ConvertTo-Json -Depth 4")
    saida = tentar(["powershell", "-NoProfile", "-Command", ps])
    try:
        return json.loads(saida)
    except Exception:
        return {"bruto": saida[:2000]}


SUSPEITOS = ["chrome", "msedge", "firefox", "opera", "brave", "discord", "steam", "epicgameslauncher", "spotify",
             "teams", "ms-teams", "slack", "zoom", "obs64", "code", "idea64", "onedrive", "googledrivefs", "dropbox",
             "claude", "whatsapp", "telegram", "battle.net", "riotclientservices", "leagueclient", "msmpeng"]


def avisar_processos(v):
    if "top_memoria" not in v:
        return
    log(f"Varredura: CPU {v.get('carga_cpu_pct')}% em uso, {v.get('memoria_livre_gb')} GB livres de "
        f"{v.get('memoria_total_gb')} GB, {v.get('qtd_processos')} processos")
    nomes = {p["nome"].lower() for p in v.get("top_memoria", []) + v.get("top_cpu", [])}
    abertos = sorted(n for n in nomes if n in SUSPEITOS)
    if abertos:
        log(f"AVISO: programas que podem atrapalhar a medicao estao abertos: {', '.join(abertos)}")
    pesados = [p for p in v.get("top_cpu", []) if (p.get("cpu_pct") or 0) >= 5 and p["nome"].lower() not in
               ("vmmem", "vmmemwsl", "com.docker.backend", "python", "powershell")]
    for p in pesados:
        log(f"AVISO: {p['nome']} esta usando {p['cpu_pct']}% de CPU")


def coletar_docker(args):
    d = {
        "versao": tentar(["docker", "version", "--format", "cliente {{.Client.Version}} / servidor {{.Server.Version}}"]),
        "vm": tentar(["docker", "info", "--format", "{{.OperatingSystem}} | {{.NCPU}} CPUs | {{.MemTotal}} bytes"]),
        "limites_conteiner": tentar(["docker", "inspect", args.container, "--format",
                                     "NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}} "
                                     "Image={{.Config.Image}}"]),
    }
    try:
        cpus, mem = rodar_cmd(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"])[1].split()
        if int(cpus) < 2 or int(mem) < 4.5 * 1024 ** 3:
            log(f"AVISO: a VM do Docker tem {cpus} CPUs e {int(mem) / 1024 ** 3:.1f} GB. O conteiner pede 2 CPUs e 4 GB. "
                "Aumente no .wslconfig (veja o README).")
    except Exception:
        pass
    return d


def carregar(banco, n_pedidos, saida_tab):
    n_clientes = max(n_pedidos // 10, 100)
    sql = (BASE / "sql" / "01_carga.sql").read_text(encoding="utf-8")
    sql = sql.replace("{n_pedidos}", str(n_pedidos)).replace("{n_clientes}", str(n_clientes))
    log(f"Carregando base: {n_pedidos:,} pedidos e {n_clientes:,} clientes".replace(",", "."))
    t0 = time.perf_counter()
    banco.exec(sql)
    banco.exec("VACUUM (ANALYZE) clientes")
    banco.exec("VACUUM (ANALYZE) pedidos")
    banco.exec("CHECKPOINT")
    t_carga = (time.perf_counter() - t0) * 1000
    for tab in ("clientes", "pedidos"):
        linhas = banco.um(f"SELECT count(*) FROM {tab}")[0]
        tam = banco.um(f"SELECT pg_relation_size('{tab}'), pg_total_relation_size('{tab}')")
        saida_tab.gravar({"volume": n_pedidos, "tabela": tab, "linhas": linhas, "tamanho_bytes": tam[0],
                          "tamanho_total_bytes": tam[1], "tempo_carga_ms": round(t_carga, 1)})
    return n_clientes


def criar_indices(banco, nomes, volume, cenario, consulta, saida_idx):
    for nome in nomes:
        tipo, ddl = IDX[nome]
        t0 = time.perf_counter()
        banco.exec(ddl)
        t = (time.perf_counter() - t0) * 1000
        tam = banco.um(f"SELECT pg_relation_size('{nome}')")[0]
        saida_idx.gravar({"volume": volume, "consulta": consulta, "cenario": cenario, "indice": nome, "tipo": tipo,
                          "tamanho_bytes": tam, "tamanho_mb": round(tam / 1048576, 3),
                          "tempo_criacao_ms": round(t, 1)})
    if nomes:
        banco.exec("ANALYZE clientes")
        banco.exec("ANALYZE pedidos")
    banco.exec("CHECKPOINT")


def remover_indices(banco, nomes):
    for nome in nomes:
        banco.exec(f"DROP INDEX IF EXISTS {nome}")


def medir(banco, volume, consulta, cenario, settings, cache, rodada, saida, pasta_planos, cache_os_limpo="",
          com_cliente=False):
    t_cli = round(banco.tempo_cliente(CONSULTAS[consulta], settings), 3) if com_cliente else ""
    plano = banco.explain(CONSULTAS[consulta], settings)
    raiz = plano["Plan"]
    tipos, indices = [], set()
    percorrer_plano(raiz, tipos, indices)
    saida.gravar({
        "data_hora": dt.datetime.now().isoformat(timespec="milliseconds"),
        "volume": volume, "consulta": consulta, "cenario": cenario, "cache": cache, "rodada": rodada,
        "tempo_execucao_ms": plano.get("Execution Time"), "tempo_planejamento_ms": plano.get("Planning Time"),
        "tempo_cliente_ms": t_cli,
        "no_raiz": raiz.get("Node Type"), "nos_plano": ">".join(tipos),
        "usou_indice": int(any(t in INDICE_NODES for t in tipos)), "indices_usados": ";".join(sorted(indices)),
        "linhas_retornadas": raiz.get("Actual Rows"),
        "shared_hit_blocks": raiz.get("Shared Hit Blocks"), "shared_read_blocks": raiz.get("Shared Read Blocks"),
        "io_read_time_ms": raiz.get("I/O Read Time", raiz.get("Shared I/O Read Time")),
        "cache_so_limpo": cache_os_limpo,
    })
    if rodada == 1:
        arq = pasta_planos / f"{volume}_{consulta}_{cenario}_{cache}.json"
        arq.write_text(json.dumps(plano, indent=2), encoding="utf-8")


def fase_leitura(banco, args, volume, saidas, pasta_planos):
    for i, (consulta, cenario, indices, settings) in enumerate(CASOS, 1):
        log(f"  [{i}/{len(CASOS)}] {consulta} | {cenario}")
        criar_indices(banco, indices, volume, cenario, consulta, saidas["idx"])
        try:
            # cache quente: aquecimento descartado + rodadas medidas
            for _ in range(args.aquecimento):
                banco.explain(CONSULTAS[consulta], settings)
                banco.tempo_cliente(CONSULTAS[consulta], settings)
            for r in range(1, args.reps + 1):
                medir(banco, volume, consulta, cenario, settings, "quente", r, saidas["leitura"], pasta_planos,
                      com_cliente=True)
            # cache frio: banco reiniciado antes de cada rodada
            if not args.sem_frio:
                for r in range(1, args.reps_frio + 1):
                    limpo = reiniciar_frio(banco, args)
                    medir(banco, volume, consulta, cenario, settings, "frio", r, saidas["leitura"], pasta_planos,
                          cache_os_limpo=int(limpo))
        finally:
            remover_indices(banco, indices)


def fase_escrita(banco, args, volume, n_clientes, saida):
    todos = list(IDX)
    for config, indices in (("sem_indice", []), ("todos_indices", todos)):
        log(f"  escrita | {config}")
        criar_indices(banco, indices, volume, config, "ESCRITA", saida["idx"])
        try:
            for op, modelo in ESCRITA.items():
                sql = modelo.format(n_pedidos=volume, n_clientes=n_clientes, lote=args.lote)
                for r in range(0, args.reps_escrita + 1):  # rodada 0 = aquecimento, descartada
                    banco.exec("BEGIN")
                    try:
                        plano = banco.um(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")[0][0]
                    finally:
                        banco.exec("ROLLBACK")
                    if r == 0:
                        continue
                    raiz = plano["Plan"]  # ModifyTable
                    filhos = sum(f.get("Actual Total Time", 0) for f in raiz.get("Plans", []))
                    saida["escrita"].gravar({
                        "volume": volume, "configuracao": config, "operacao": op, "rodada": r, "lote": args.lote,
                        "qtd_indices": len(indices),
                        "tempo_execucao_ms": plano.get("Execution Time"),
                        "tempo_escrita_ms": round(raiz.get("Actual Total Time", 0) - filhos, 3),
                        "tempo_localizar_linhas_ms": round(filhos, 3),
                        "shared_hit_blocks": raiz.get("Shared Hit Blocks"),
                        "shared_read_blocks": raiz.get("Shared Read Blocks"),
                        "shared_dirtied_blocks": raiz.get("Shared Dirtied Blocks"),
                    })
                banco.exec("VACUUM pedidos")
        finally:
            remover_indices(banco, indices)
            banco.exec("VACUUM (ANALYZE) pedidos")


def salvar_pg_stat_statements(banco, volume, pasta):
    try:
        linhas = banco.exec(
            "SELECT %s AS volume, calls, round(total_exec_time::numeric,3) total_ms, round(mean_exec_time::numeric,3) media_ms, "
            "round(stddev_exec_time::numeric,3) desvio_ms, round(min_exec_time::numeric,3) min_ms, "
            "round(max_exec_time::numeric,3) max_ms, rows, shared_blks_hit, shared_blks_read, "
            "regexp_replace(query, '\\s+', ' ', 'g') AS query "
            "FROM pg_stat_statements WHERE query ILIKE ANY (ARRAY['%%pedidos%%','%%clientes%%']) "
            "AND query NOT ILIKE '%%pg_stat_statements%%' ORDER BY total_exec_time DESC", (volume,)).fetchall()
        arq = pasta / "pg_stat_statements.csv"
        novo = not arq.exists()
        with open(arq, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if novo:
                w.writerow(["volume", "calls", "total_ms", "media_ms", "desvio_ms", "min_ms", "max_ms", "rows",
                            "shared_blks_hit", "shared_blks_read", "query"])
            w.writerows(linhas)
    except Exception as e:
        log(f"  aviso: pg_stat_statements nao coletado ({e})")


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Experimento de indices no PostgreSQL - Equipe 09")
    p.add_argument("--volumes", default="100000,1000000,10000000", help="quantidade de pedidos, separada por virgula")
    p.add_argument("--reps", type=int, default=30, help="rodadas medidas com cache quente")
    p.add_argument("--aquecimento", type=int, default=5, help="rodadas descartadas antes das medidas")
    p.add_argument("--reps-frio", type=int, default=5, help="rodadas com cache frio (cada uma reinicia o banco)")
    p.add_argument("--sem-frio", action="store_true", help="pula as medicoes de cache frio")
    p.add_argument("--reps-escrita", type=int, default=15)
    p.add_argument("--lote", type=int, default=10000, help="linhas por operacao de escrita")
    p.add_argument("--sem-escrita", action="store_true")
    p.add_argument("--rapido", action="store_true", help="teste curto: 10 mil linhas, poucas rodadas")
    p.add_argument("--host", default="localhost")
    p.add_argument("--porta", default="55432")
    p.add_argument("--banco", default="experimento")
    p.add_argument("--usuario", default="pesquisa")
    p.add_argument("--senha", default="pesquisa")
    p.add_argument("--container", default="pi_equipe09_pg")
    p.add_argument("--cmd-parar", default=None, help="comando para parar o banco (padrao: docker stop)")
    p.add_argument("--cmd-iniciar", default=None, help="comando para iniciar o banco (padrao: docker start)")
    p.add_argument("--cmd-limpar-cache", default=None,
                   help="comando para limpar o cache do SO; 'nenhum' desativa")
    args = p.parse_args()

    if args.rapido:
        args.volumes, args.reps, args.reps_frio, args.reps_escrita, args.lote, args.aquecimento = \
            "10000", 5, 2, 3, 1000, 1
    args.cmd_parar = shlex.split(args.cmd_parar) if args.cmd_parar else ["docker", "stop", "-t", "30", args.container]
    args.cmd_iniciar = shlex.split(args.cmd_iniciar) if args.cmd_iniciar else ["docker", "start", args.container]
    if args.cmd_limpar_cache == "nenhum":
        args.cmd_limpar_cache = None
    elif args.cmd_limpar_cache:
        args.cmd_limpar_cache = shlex.split(args.cmd_limpar_cache)
    else:
        args.cmd_limpar_cache = ["docker", "run", "--rm", "--privileged", "alpine:3.20", "sh", "-c",
                                 "sync; echo 3 > /proc/sys/vm/drop_caches"]
    volumes = [int(v) for v in args.volumes.split(",")]

    nome = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ("_rapido" if args.rapido else "")
    pasta = BASE / "resultados" / nome
    pasta_planos = pasta / "planos"
    pasta_planos.mkdir(parents=True)

    saidas = {
        "leitura": CSVSaida(pasta / "medicoes_leitura.csv",
                            ["data_hora", "volume", "consulta", "cenario", "cache", "rodada", "tempo_execucao_ms",
                             "tempo_planejamento_ms", "tempo_cliente_ms", "no_raiz", "nos_plano", "usou_indice", "indices_usados",
                             "linhas_retornadas", "shared_hit_blocks", "shared_read_blocks", "io_read_time_ms",
                             "cache_so_limpo"]),
        "idx": CSVSaida(pasta / "indices.csv",
                        ["volume", "consulta", "cenario", "indice", "tipo", "tamanho_bytes", "tamanho_mb",
                         "tempo_criacao_ms"]),
        "tab": CSVSaida(pasta / "tabelas.csv",
                        ["volume", "tabela", "linhas", "tamanho_bytes", "tamanho_total_bytes", "tempo_carga_ms"]),
        "escrita": CSVSaida(pasta / "medicoes_escrita.csv",
                            ["volume", "configuracao", "operacao", "rodada", "lote", "qtd_indices",
                             "tempo_execucao_ms", "tempo_escrita_ms", "tempo_localizar_linhas_ms",
                             "shared_hit_blocks", "shared_read_blocks", "shared_dirtied_blocks"]),
    }

    banco = Banco(args)
    banco.conectar()
    banco.exec("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    coletar_ambiente(banco, args, pasta)
    log(f"Resultados em: {pasta}")
    inicio = time.time()

    for volume in volumes:
        log(f"===== VOLUME {volume:,} pedidos =====".replace(",", "."))
        n_clientes = carregar(banco, volume, saidas["tab"])
        banco.exec("SELECT pg_stat_statements_reset()")
        fase_leitura(banco, args, volume, saidas, pasta_planos)
        if not args.sem_escrita:
            fase_escrita(banco, args, volume, n_clientes, saidas)
        salvar_pg_stat_statements(banco, volume, pasta)
        log(f"Volume concluido. Tempo total ate aqui: {(time.time() - inicio) / 60:.1f} min")

    banco.fechar()
    amb = json.loads((pasta / "ambiente.json").read_text(encoding="utf-8"))
    amb["data_hora_fim"] = dt.datetime.now().isoformat(timespec="seconds")
    amb["processos_fim"] = varrer_processos()
    amb["duracao_min"] = round((time.time() - inicio) / 60, 1)
    (pasta / "ambiente.json").write_text(json.dumps(amb, indent=2, ensure_ascii=False), encoding="utf-8")
    log("Gerando resumo estatistico...")
    import analisar
    analisar.resumir(pasta)
    log(f"FIM. Envie a pasta resultados/{nome} (compactada) para a analise.")


if __name__ == "__main__":
    main()
