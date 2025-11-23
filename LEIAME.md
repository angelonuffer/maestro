# Maestro

O Maestro é um agente de IA configurável que analisa dados locais através de um modelo remoto e gera um relatório.

## Arquivo de configuração

Veja `exemplos/clientes/config.yaml` para a configuração do agente. Principais campos:

- `conexão.chave`: nome da variável de ambiente que deve conter a chave da API (ex.: `GEMINI_API_KEY`).
- `conexão.modelo`: modelo remoto a ser usado (ex.: `gemini-2.5-flash`).
- `ferramentas[0].arquivos.caminho_permitido`: caminho relativo que o agente pode acessar (ex.: `./dados`).
- `solicitação`: arquivo Markdown com instruções para o agente (`solicitação.md`).
- `relatório`: caminho de saída do relatório gerado (`relatório_clientes.html`).

## Exemplo rápido de uso

- Escreva sua configuração e suas instruções.
- Exporte a chave da API ou a variável de credenciais:

```bash
export GEMINI_API_KEY="sua_chave_aqui"
```

- Execute o agente (ou o script de execução da sua implementação) para gerar o relatório em HTML no caminho definido por `relatório`.

```bash
pwsh maestro.ps1 config.yaml
```

## Geração de relatórios

O fluxo básico é:

- O agente analisa as instruções especificadas em `solicitação` e os dados no diretório especificado em `caminho_permitido`.
- Envia solicitações ao modelo remoto configurado em `conexão`.
- Recebe o conteúdo gerado e grava como `relatório` (HTML).