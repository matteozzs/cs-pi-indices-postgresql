# Experimento: impacto de índices no tempo de resposta do PostgreSQL

Equipe 09 · PI Projeto e Pesquisa · Ciência da Computação · UTP 2026/2
Mateus da Silva Zuccoli, Pedro Corrêa Sampaio, Carlos Alberto Bertuol

Este repositório é o artefato reproduzível do artigo. Ele traz o ambiente fixo, a carga da base, as consultas, o script de medição e os resultados.

## 1. O que precisa estar instalado

- Docker Desktop, aberto e rodando (no Windows, com o WSL2 ativo)
- Python 3.10 ou superior (no instalador do Windows, marque "Add Python to PATH")
- Cerca de 6 GB livres em disco

### Recursos do Docker no Windows

Com o backend WSL 2, o Docker Desktop não deixa escolher CPU e memória na tela de configurações. Esses limites pertencem à máquina virtual do WSL 2 e são definidos no arquivo `%UserProfile%\.wslconfig` (documentação da Docker, seção *Settings > Resources > Advanced*). Por padrão, essa máquina virtual usa todas as CPUs do computador e uma parte da memória dele, e cresce conforme a necessidade.

Para o experimento isso não é problema, porque o `docker-compose.yml` limita o próprio contêiner do banco a **2 CPUs e 4 GB**. Esse limite é o ambiente controlado. O script avisa se a máquina virtual tiver menos que isso. Só nesse caso é preciso criar o `.wslconfig` com, por exemplo:

```
[wsl2]
memory=6GB
processors=4
```

e depois rodar `wsl --shutdown` e abrir o Docker Desktop de novo.

## 2. Como rodar

1. Abra o Docker Desktop e espere ele ficar pronto.
2. Dê dois cliques em `rodar_teste_rapido.bat`. Esse teste leva uns 2 minutos e confere se tudo funciona.
3. Preencha o que faltar no arquivo `ambiente_manual.txt`. Hardware, Docker, horário de início e de fim são registrados sozinhos no `ambiente.json`.
4. Feche os outros programas, deixe o notebook na tomada e dê dois cliques em `rodar_experimento_completo.bat`. Leva de 1 a 2 horas. Não use a máquina enquanto roda.
5. Ao terminar, compacte a pasta mais recente dentro de `resultados/` junto com o `ambiente_manual.txt`.

Pelo terminal, o equivalente é:

```
docker compose up -d
python -m pip install -r requirements.txt
python executar_experimento.py --rapido     # teste
python executar_experimento.py              # completo
```

Opções úteis: `--volumes 100000,1000000` (pula os 10 milhões), `--sem-frio` (pula o cache frio, que é a parte mais demorada), `--reps 30`, `--reps-frio 5`.

## 3. Desenho do experimento

| Item | Definição |
|---|---|
| Tipo de pesquisa | Aplicada, quantitativa, explicativa, experimental |
| Variável independente | Presença e tipo de índice (nenhum, B-tree, Hash, BRIN, GIN) |
| Variáveis dependentes | Tempo de execução no servidor (ms), tempo de resposta no cliente (ms), blocos lidos do cache e do disco, uso do índice no plano, tamanho do índice (MB), tempo de criação do índice, tempo de escrita |
| Variáveis controladas | Versão do PostgreSQL (17.6), postgresql.conf único, mesmo contêiner (2 CPUs, 4 GB), mesma máquina, mesma base (semente fixa) |
| Baseline | Mesma consulta sem nenhum índice (Seq Scan) |
| Volumes | 100 mil, 1 milhão e 10 milhões de pedidos (clientes = 10% dos pedidos) |
| Repetições | 5 rodadas de aquecimento descartadas + 30 medidas com cache quente; 5 medidas com cache frio; 15 medidas por operação de escrita |
| Estatísticas | Média, mediana com intervalo de confiança de 95% (bootstrap), P95, desvio padrão, coeficiente de variação, mínimo, máximo, speedup pela mediana, teste de Mann-Whitney contra o baseline |

### Base sintética (`sql/01_carga.sql`)

Gerada com `generate_series` e `setseed(0.42)`, então a mesma base sai em toda execução.

- `clientes(id, nome, email, uf, renda)`
- `pedidos(id, cliente_id, data_pedido, status, valor, tags[])`. A `data_pedido` cresce junto com o id, como num sistema real em que os registros entram em ordem de data. O `status` tem 70% ENTREGUE, 15% ENVIADO, 10% PENDENTE e 5% CANCELADO.

As tabelas são criadas sem chave primária, para que o baseline realmente não tenha nenhum índice.

### Consultas e cenários (`sql/02_consultas.sql`)

| Consulta | O que testa | Cenários |
|---|---|---|
| Q1 | Filtro por igualdade (`cliente_id = 4242`) | sem índice, B-tree, Hash |
| Q2 | Faixa de datas (0,1% do período) | sem índice, B-tree, BRIN |
| Q3 | LIKE por prefixo no e-mail | sem índice, B-tree (`text_pattern_ops`) |
| Q4 | ORDER BY valor DESC LIMIT 10 | sem índice, B-tree |
| Q5 | Junção clientes × pedidos | sem índice, B-tree nas duas chaves |
| Q6 | Agregação por status no último 1% do período | sem índice, B-tree, BRIN |
| Q7 | Filtro pouco seletivo (70% da tabela) | sem índice, B-tree, B-tree forçado (`enable_seqscan = off`) |
| Q8 | Busca em array (`tags @> ...`) | sem índice, GIN |

Cada cenário cria só o índice que está sendo testado, roda `ANALYZE`, mede e apaga o índice antes do próximo. Assim um índice não interfere na medição de outro.

### Duas formas de medir o tempo

- **Servidor**: `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)`. Mede só o trabalho do PostgreSQL e traz o plano e os blocos lidos. O `TIMING OFF` desliga a cronometragem de cada etapa do plano, que encarece consultas curtas, e mantém o tempo total.
- **Cliente**: o script executa a consulta de verdade, recebe todas as linhas e mede com o relógio do Python. Esse é o tempo de resposta que a aplicação sente, com conexão e transferência incluídas.

Depois de criar cada índice o script roda `ANALYZE` e `CHECKPOINT`, para que a gravação pendente da criação não caia no meio da medição.

### Cache quente e cache frio

- **Quente**: a consulta roda 5 vezes sem medir, depois 30 vezes medindo.
- **Frio**: antes de cada rodada o contêiner é parado (esvazia o `shared_buffers`) e o cache de páginas do sistema operacional é limpo com `drop_caches`, num contêiner auxiliar com `--privileged`. A coluna `cache_so_limpo` indica se essa limpeza funcionou.

### Custo de escrita

Para cada volume, um lote de 10.000 linhas de INSERT, UPDATE e DELETE é executado 15 vezes em duas configurações: sem índice e com os 9 índices criados. Cada operação roda dentro de `BEGIN ... ROLLBACK`, então a base não muda. A coluna `tempo_escrita_ms` desconta o tempo gasto para localizar as linhas e mostra só o custo de gravar, incluindo a manutenção dos índices.

### Configuração do banco (`config/postgresql.conf`)

`autovacuum`, `jit` e paralelismo ficam desligados para reduzir a variação entre rodadas. O `VACUUM ANALYZE` é feito pelo script depois da carga. Os parâmetros de custo do planejador ficam no padrão do PostgreSQL.

## 4. Arquivos gerados em `resultados/<data_hora>/`

| Arquivo | Conteúdo |
|---|---|
| `ambiente.json` | Versão do PostgreSQL, configuração aplicada, hardware (CPU, RAM, discos), versão e limites do Docker, início, fim e duração |
| `tabelas.csv` | Linhas, tamanho e tempo de carga de cada tabela |
| `indices.csv` | Tamanho (bytes e MB) e tempo de criação de cada índice |
| `medicoes_leitura.csv` | Uma linha por execução: tempo, plano, uso do índice, blocos do cache e do disco |
| `medicoes_escrita.csv` | Uma linha por execução de INSERT, UPDATE e DELETE |
| `pg_stat_statements.csv` | Estatísticas acumuladas pelo PostgreSQL, para conferência |
| `planos/` | Plano completo em JSON da primeira execução de cada caso |
| `resumo_leitura.csv` | Estatísticas por caso: mediana com IC95%, P95, desvio, CV, speedup, p-valor, tempo no cliente, tamanho do índice |
| `resumo_escrita.csv` | Mediana com IC95%, P95, sobrecusto (%) e p-valor da escrita com índices |
| `resumo.md` | Tabelas legíveis com os principais números |

Para refazer o resumo a partir dos dados brutos: `python analisar.py resultados/<pasta>`

## 5. Limitações conhecidas

- O `EXPLAIN ANALYZE` mede com precisão de 0,001 ms. Em consultas muito rápidas (abaixo de 0,05 ms), a comparação entre tipos de índice fica no limite da resolução. Para esses casos, vale olhar também o tempo no cliente.
- No Docker Desktop, a limpeza do cache do sistema operacional atua na máquina virtual do Docker, não no Windows.
- A base é sintética e a distribuição dos valores é uniforme. Bases reais podem ter distribuições mais concentradas.
