-- Carga da base sintetica (sem nenhum indice, nem chave primaria)
-- Parametros substituidos pelo executor: {n_pedidos} e {n_clientes}
-- setseed garante que a mesma base seja gerada em toda execucao

DROP TABLE IF EXISTS pedidos;
DROP TABLE IF EXISTS clientes;

SELECT setseed(0.42);

CREATE TABLE clientes (
    id      bigint  NOT NULL,
    nome    text    NOT NULL,
    email   text    NOT NULL,
    uf      char(2) NOT NULL,
    renda   numeric(10,2) NOT NULL
);

INSERT INTO clientes
SELECT g,
       'Cliente ' || g,
       'cliente' || g || '@exemplo.com',
       (ARRAY['PR','SP','SC','RS','RJ','MG','BA','GO'])[1 + floor(random()*8)::int],
       round((1500 + random()*18500)::numeric, 2)
FROM generate_series(1, {n_clientes}) AS g;

CREATE TABLE pedidos (
    id           bigint    NOT NULL,
    cliente_id   bigint    NOT NULL,
    data_pedido  timestamp NOT NULL,
    status       text      NOT NULL,
    valor        numeric(10,2) NOT NULL,
    tags         text[]    NOT NULL
);

-- data_pedido cresce junto com o id (ordem fisica de insercao), como num sistema real.
-- status: 70% ENTREGUE, 15% ENVIADO, 10% PENDENTE, 5% CANCELADO
INSERT INTO pedidos
SELECT g,
       1 + floor(random() * {n_clientes})::bigint,
       timestamp '2020-01-01' + (g::double precision / {n_pedidos}) * interval '5 years',
       CASE WHEN r < 0.70 THEN 'ENTREGUE'
            WHEN r < 0.85 THEN 'ENVIADO'
            WHEN r < 0.95 THEN 'PENDENTE'
            ELSE 'CANCELADO' END,
       round((5 + random()*995)::numeric, 2),
       ARRAY['tag' || lpad((1 + floor(random()*50))::int::text, 2, '0'),
             'tag' || lpad((1 + floor(random()*50))::int::text, 2, '0'),
             'tag' || lpad((1 + floor(random()*50))::int::text, 2, '0')]
FROM (SELECT g, random() AS r FROM generate_series(1, {n_pedidos}) AS g) s;
