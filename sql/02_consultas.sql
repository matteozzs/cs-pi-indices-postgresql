-- Conjunto fixo de consultas (referencia legivel; o executor usa exatamente estes textos)
-- {n_pedidos} e {n_clientes} sao substituidos conforme o volume

-- Q1 filtro por igualdade
SELECT * FROM pedidos WHERE cliente_id = 4242;

-- Q2 filtro por faixa de valores (0,1% do periodo)
SELECT count(*), sum(valor) FROM pedidos
WHERE data_pedido BETWEEN timestamp '2022-06-01' AND timestamp '2022-06-01' + interval '43 hours';

-- Q3 LIKE por prefixo
SELECT * FROM clientes WHERE email LIKE 'cliente4242%';

-- Q4 ordenacao com LIMIT
SELECT * FROM pedidos ORDER BY valor DESC LIMIT 10;

-- Q5 juncao entre tabelas
SELECT c.nome, p.data_pedido, p.valor
FROM clientes c JOIN pedidos p ON p.cliente_id = c.id
WHERE c.id BETWEEN 1000 AND 1050;

-- Q6 agregacao (ultimo 1% do periodo)
SELECT status, count(*), avg(valor) FROM pedidos
WHERE data_pedido >= timestamp '2024-12-13'
GROUP BY status;

-- Q7 filtro pouco seletivo (retorna ~70% da tabela, cenario em que o indice tende a perder)
SELECT count(*), avg(valor) FROM pedidos WHERE status = 'ENTREGUE';

-- Q8 busca em array (GIN)
SELECT count(*) FROM pedidos WHERE tags @> ARRAY['tag07','tag13'];
