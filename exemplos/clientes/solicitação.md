# Solicitação para o Agente

Analise os seguintes pontos:

- Resumo dos clientes presentes em `./dados/clientes.csv`.
- Principais métricas: número total de clientes, soma do `valor_contrato`, média por cliente, clientes ativos vs inativos.
- Observações relevantes encontradas em `./dados/observações.txt`.
- Recomendações e pontos de atenção.

Formato de saída esperado:

- Capa: título "Relatório Consolidado de Clientes", data de geração e sumário executivo.
- Metodologia: fontes consultadas (arquivos em `./dados`).
- Tabela de clientes: inclua colunas `id`, `nome`, `email`, `valor_contrato`, `data_inicio`, `status`.
- Análise agregada: totais e médias, top 5 clientes por `valor_contrato`.
- Observações: texto extraído de `observações.txt` e interpretação.
- Recomendações finais.