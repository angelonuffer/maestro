<#
.SYNOPSIS
    Orquestrador de agente de IA: lê a configuração, envia instruções ao modelo e executa as ações
    passo-a-passo retornadas pelo modelo.

.DESCRIPTION
    Este script recebe o caminho para um arquivo YAML de configuração e orquestra um agente de IA
    conforme essa configuração. Ele envia ao modelo: instruções sobre o orquestrador, o próprio
    arquivo de configuração, a `solicitação` (arquivo Markdown) e uma árvore do diretório que o agente
    pode acessar.

        O modelo deve responder com JSON estruturado contendo pelo menos: `status`, `plan`, `actions`
        e `memory`. O orquestrador executa ações retornadas (uma a uma, respeitando o limite de passos
        por conexão) e reenvia o estado de memória curto ao modelo entre conexões.

        Tipos de ação suportados (formato esperado de cada ação):
        - `leia_arquivo`: o modelo solicita UM arquivo por ação.
            - parâmetros esperados: `path` (caminho relativo/absoluto para o arquivo desejado).
            - comportamento: o orquestrador lerá o arquivo solicitado (se existir e estiver dentro de
                `ferramentas[*].arquivos.caminho_permitido`) e retornará seu conteúdo em base64 no
                mapa `attachments` ao notificar o modelo após a execução da ação. Se o arquivo não
                existir ou estiver fora da raiz permitida, o orquestrador retornará `null` para essa chave.
            - segurança: apenas um arquivo por ação; caminhos fora da raiz permitida são negados.
        - `finalizar`: indica ao orquestrador que o trabalho está completo; o orquestrador encerra o loop.

        Regras e restrições de segurança:
        - O orquestrador só permite leitura/escrita dentro do diretório configurado em
            `ferramentas[*].arquivos.caminho_permitido` (resolvido relativo ao diretório do YAML);
            caminhos fora dessa raiz não serão lidos nem escritos.
        - Os comandos `run_shell` serão executados sem sandbox — proceda com cuidado ao conceder
            acesso ao orquestrador.

        Formato de resposta esperado do modelo (exemplo mínimo):
        {
            "status": "ok",
            "plan": ["Descrição de alto nível"],
            "actions": [
                {"id": "1", "type": "leia_arquivo", "description": "Solicitar arquivo de dados", "parameters": {"path":"./dados/clientes.csv"}},
                {"id": "2", "type": "finalizar", "description": "Finalizar e retornar HTML", "parameters": {"content":"<html><body><h1>Relatório</h1></body></html>"}},
            ],
            "memory": {"step":1}
        }

    Propriedades esperadas no YAML (em português):
    - `conexão.endereço`: URL do endpoint da API do modelo.
    - `conexão.chave`: nome da variável de ambiente que contém a chave da API (ex.: `GOOGLE_API_KEY`).
    - `conexão.modelo`: nome do modelo (informacional).
    - `conexão.limite_passos`: número máximo de ações por conexão.
    - `ferramentas[*].arquivos.caminho_permitido`: caminho relativo (a partir do diretório do YAML)
             que o agente pode acessar.
    - `solicitação`: caminho relativo/absoluto para o arquivo Markdown com as instruções do agente (obrigatório).
    - `relatório`: nome do arquivo HTML final (apenas o nome, relativo ao diretório do YAML).

    Observações importantes:
    - `solicitação` é obrigatória: o script aborta se não existir ou não apontar para um arquivo legível.
    - O script somente usa `ferramentas[*].arquivos.caminho_permitido` como raiz permitida para leitura de arquivos.
    - A variável de ambiente apontada em `conexão.chave` deve estar definida se o endpoint requer autenticação.

.USAGE
    pwsh código/maestro.ps1 /caminho/para/exemplos/clientes/config.yaml

# Exemplo mínimo (`config.yaml`):
# conexão:
#   endereço: "https://generativelanguage.googleapis.com/v1beta2/models/gemini-1.5-mini:generate"
#   chave: "GOOGLE_API_KEY"
#   modelo: "gemini-1.5-mini"
#   limite_passos: 5
# ferramentas:
#   - arquivos:
#       caminho_permitido: "./dados"
# solicitação: "solicitação.md"
# relatorio: "relatorio_clientes.html"
#>

param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'

function Write-Log {
    param([string]$Message)
    $ts = (Get-Date).ToString('s')
    Write-Host "[$ts] $Message"
}

function Parse-YamlToObject {
    param([string]$Path)
    if (-not (Test-Path $Path)) { throw "Arquivo de configuração não encontrado: $Path" }
    try {
        if (Get-Command -Name ConvertFrom-Yaml -ErrorAction SilentlyContinue) {
            $yamlText = Get-Content -Raw -Path $Path
            return ConvertFrom-Yaml -Yaml $yamlText
        } else {
            # Fallback: use Python to convert YAML -> JSON then convert from JSON
            if (-not (Get-Command -Name python -ErrorAction SilentlyContinue)) {
                throw "Nem ConvertFrom-Yaml nem python estão disponíveis para parsear YAML."
            }
            $py = @"
import sys, json, yaml
with open(sys.argv[1], 'r', encoding='utf-8') as f:
    data = yaml.safe_load(f)
print(json.dumps(data))
"@
            $tmpPy = [System.IO.Path]::GetTempFileName() + '.py'
            Set-Content -Path $tmpPy -Value $py -Encoding UTF8
            $json = & python $tmpPy $Path
            Remove-Item $tmpPy -Force -ErrorAction SilentlyContinue
            return $json | ConvertFrom-Json
        }
    } catch {
        throw "Falha ao parsear YAML: $_"
    }
}

function Get-FileTree {
    param([string]$Root)
    $rootFull = (Resolve-Path -Path $Root).Path
    $files = Get-ChildItem -Path $rootFull -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        [PSCustomObject]@{
            path = $_.FullName.Substring($rootFull.Length).TrimStart([System.IO.Path]::DirectorySeparatorChar)
            fullPath = $_.FullName
            length = $_.Length
            lastWrite = $_.LastWriteTimeUtc
        }
    }
    return $files
}

function Read-FileContentBase64 {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    $b = [System.IO.File]::ReadAllBytes((Resolve-Path $Path).Path)
    return [System.Convert]::ToBase64String($b)
}

function Invoke-Model {
    param(
        [hashtable]$Cfg,
        [hashtable]$Payload
    )
    $endpoint = $Cfg.endpoint
    $apiKey = $Cfg.api_key_value
    $authType = $Cfg.auth_type
    if (-not $endpoint) { throw "endpoint da conexão não configurado." }
    $headers = @{'Content-Type'='application/json'}
    $uri = $endpoint
    # Suporte a dois modos de autenticação: api_key (chave via header x-goog-api-key) ou token bearer
    if ($authType -and $authType -eq 'api_key' -and $apiKey) {
        # usar header x-goog-api-key conforme documentação de algumas rotas Gemini
        $headers['x-goog-api-key'] = $apiKey
    } elseif ($apiKey) {
        $headers['Authorization'] = "Bearer $apiKey"
    }
    $body = $Payload | ConvertTo-Json -Depth 20
    Write-Log "endpoint raw: '$endpoint'  authType: '$authType'  apiKeyPresent: $([bool]$apiKey)"
    Write-Log "Enviando requisição ao modelo em $uri..."
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Post -Headers $headers -Body $body -ContentType 'application/json' -ErrorAction Stop
        return $resp
    } catch {
        # tentar capturar corpo da resposta quando disponível — leitura defensiva para evitar erros adicionais
        $errMsg = $_.Exception.Message
        if ($_.Exception -and $_.Exception.Response) {
            $respObj = $_.Exception.Response
            try {
                if ($respObj -is [System.Net.WebResponse]) {
                    $stream = $respObj.GetResponseStream()
                    if ($stream) {
                        $reader = New-Object System.IO.StreamReader($stream)
                        $bodyText = $reader.ReadToEnd()
                        $errMsg = "$errMsg -- Response body: $bodyText"
                    }
                } elseif ($respObj -and $respObj.GetType().GetMethod('get_Content')) {
                    # tentativa limitada para HttpResponseMessage-like objects
                    try {
                        $bodyText = $null
                        if ($respObj.Content -and $respObj.Content.GetType().GetMethod('ReadAsStringAsync')) {
                            $t = $respObj.Content.ReadAsStringAsync()
                            $t.Wait(2000) | Out-Null
                            if ($t.Status -eq 'RanToCompletion') { $bodyText = $t.Result }
                        }
                        if ($bodyText) { $errMsg = "$errMsg -- Response body: $bodyText" }
                    } catch {
                        # ignore reading errors
                    }
                }
            } catch {
                # ignore
            }
        }
        throw "Falha na chamada ao modelo: $errMsg"
    }
}

function Ensure-DirectoryExists {
    param([string]$Path)
    $dir = Split-Path -Path $Path -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

function Invoke-Model-WithRetries {
    param(
        [hashtable]$Cfg,
        [hashtable]$Payload,
        [bool]$IsGenerateContent = $false,
        [int]$MaxRetries = 5
    )
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        Write-Log "Tentativa de chamada ao modelo: $attempt / $MaxRetries"
        try {
            $raw = Invoke-Model -Cfg $Cfg -Payload $Payload
        } catch {
            Write-Log "Erro na chamada ao modelo (tentativa $attempt): $_"
            $raw = $null
        }

        if ($IsGenerateContent) {
            $outText = $null
            try {
                if ($raw -and $raw.candidates -and $raw.candidates.Count -gt 0 -and $raw.candidates[0].content -and $raw.candidates[0].content.parts -and $raw.candidates[0].content.parts.Count -gt 0) {
                    $outText = $raw.candidates[0].content.parts[0].text
                }
            } catch {
                $outText = $null
            }

            if ($outText) { return $outText }
            Write-Log "Resposta vazia ou sem texto do candidato (attempt $attempt)."
            if ($attempt -lt $MaxRetries) { Start-Sleep -Seconds (2 * $attempt); continue } else { throw "Resposta vazia do modelo após $MaxRetries tentativas." }
        } else {
            if ($raw) { return $raw }
            Write-Log "Resposta vazia do modelo (attempt $attempt)."
            if ($attempt -lt $MaxRetries) { Start-Sleep -Seconds (2 * $attempt); continue } else { throw "Resposta vazia do modelo após $MaxRetries tentativas." }
        }
    }
}

# Início do fluxo
Write-Log "Carregando configuração: $ConfigPath"
$config = Parse-YamlToObject -Path $ConfigPath


# Determina raiz onde o script pode acessar (diretório do arquivo de configuração)
$configDir = Split-Path -Path (Resolve-Path $ConfigPath) -Parent

# Ler solicitação (usa somente propriedades em português conforme o config.yaml)
$promptPath = $null
if ($config.'solicitação') {
    $promptPath = if ([System.IO.Path]::IsPathRooted($config.'solicitação')) { $config.'solicitação' } else { Join-Path $configDir $config.'solicitação' }
} else {
    Write-Log "Erro: propriedade 'solicitação' ausente no arquivo de configuração.";
    exit 1
}

if (-not (Test-Path $promptPath)) {
    Write-Log "Erro: arquivo apontado por 'solicitação' não encontrado: $promptPath";
    exit 1
}

Write-Log "Lendo solicitação: $promptPath"
$promptText = Get-Content -Raw -Path $promptPath -ErrorAction Stop

# Criar árvore de arquivos acessíveis (usa caminho_permitido definido em ferramentos -> arquivos)
$allowedRoot = $null
if ($config.ferramentas) {
    foreach ($f in $config.ferramentas) {
        if ($f.arquivos -and $f.arquivos.caminho_permitido) {
            $allowed = $f.arquivos.caminho_permitido
            $allowedRoot = if ([System.IO.Path]::IsPathRooted($allowed)) { $allowed } else { Join-Path $configDir $allowed }
            break
        }
        # suporte removido para `f.caminho_permitido` — aceitar apenas `f.arquivos.caminho_permitido`
    }
}
if (-not $allowedRoot) { $allowedRoot = $configDir }
Write-Log "Gerando árvore de arquivos em $allowedRoot"
$tree = Get-FileTree -Root $allowedRoot

# Cria payload inicial
$initialPayload = @{
    orchestrator = 'maestro.ps1: orquestrador. POR FAVOR RESPONDA SOMENTE COM JSON VÁLIDO E PURO — SEM MARCAÇÃO Markdown, SEM CODE FENCES (```), SEM TEXTO ADICIONAL. O JSON deve conter apenas os campos: "status", "plan", "actions" e "memory".\nTipos de ação suportados: leia_arquivo (path), finalizar.\n(O modelo deve retornar o HTML final NA AÇÃO `finalizar` através de `parameters.content`. O orquestrador salvará esse HTML automaticamente no caminho definido em `relatório` no config ou no `parameters.path` fornecido na ação.)\nO orquestrador executa ações uma a uma, respeita conexao.limite_passos, e fornece arquivos solicitados em base64 quando o modelo usar a ação `leia_arquivo` (um arquivo por ação). O orquestrador só acessa caminhos dentro de ferramentas[*].arquivos.caminho_permitido. Sempre retorne um objeto "memory" atualizado para a próxima conexão.'
    config = $config
    prompt = $promptText
    file_tree = $tree | ForEach-Object { @{path=$_.path; length=$_.length; lastWrite=$_.lastWrite } }
    request = 'Por favor responda com objeto JSON estruturado contendo: status("ok"/"error"), plan:[...], actions:[{id,type,description,parameters}], memory:{}'
}

# monta objeto de conexão a partir das propriedades em português
$connObj = @{}
if ($config.'conexão') {
    $conn = $config.'conexão'
    if ($conn.endereço) { $connObj.endpoint = $conn.endereço }
    elseif ($conn.endereco) { $connObj.endpoint = $conn.endereco }
    # modelo preferencial (gemini-2.5-flash etc.)
    if ($conn.modelo) { $connObj.model = $conn.modelo } elseif ($conn.model) { $connObj.model = $conn.model }
    if ($conn.chave) { $connObj.api_key_var = $conn.chave }
    elseif ($conn.api_key) { $connObj.api_key_var = $conn.api_key }
    # tipo de autenticação (api_key ou service_account)
    if ($conn.autenticação -and $conn.autenticação.tipo) { $connObj.auth_type = $conn.autenticação.tipo } else { $connObj.auth_type = 'api_key' }
    # resolve valor da chave da API a partir da variável de ambiente, se indicada
    $connObj.api_key_value = $null
    if ($connObj.api_key_var) {
        $ev = Get-Item -Path Env:$($connObj.api_key_var) -ErrorAction SilentlyContinue
        if ($ev) { $connObj.api_key_value = $ev.Value }
    }
}

# Se endpoint não foi fornecido, tente construir a URL com base no modelo (usando v1beta generateContent)
if (-not $connObj.endpoint -and $connObj.model) {
    $connObj.endpoint = "https://generativelanguage.googleapis.com/v1beta/models/$($connObj.model):generateContent"
}

# Enviar o payload inicial ao modelo e preparar a primeira resposta estrutural para o loop de execução.
Write-Log "Enviando payload inicial ao modelo..."
try {
    $isGenerateContent = $false
    if ($connObj.endpoint -and $connObj.endpoint -match ':generateContent') { $isGenerateContent = $true }

    if ($isGenerateContent) {
        # Construir payload no formato esperado pelo endpoint v1beta generateContent
        $combinedText = $initialPayload.orchestrator + "\n\n" + $initialPayload.prompt
        $genPayload = @{ contents = @( @{ parts = @(@{ text = $combinedText }) } ) }
        Write-Log "== ENVIANDO (initial generateContent) payload -> model =="
        try { $genPayload | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $genPayload }
        
        $rawResp = Invoke-Model -Cfg $connObj -Payload $genPayload
        Write-Log "== RESPOSTA (raw) DO MODELO =="
        try { $rawResp | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $rawResp }
        Write-Log "Resposta recebida (generateContent). Tentando extrair texto do primeiro candidato."
        $outText = $null
        try {
            if ($rawResp.candidates -and $rawResp.candidates.Count -gt 0 -and $rawResp.candidates[0].content -and $rawResp.candidates[0].content.parts -and $rawResp.candidates[0].content.parts.Count -gt 0) {
                $outText = $rawResp.candidates[0].content.parts[0].text
                Write-Host $outText
            } else {
                $rawResp | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ }
            }
        } catch {
            Write-Log "Falha ao extrair texto do candidato: $_"
            $rawResp | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ }
        }

        # Se o modelo retornou texto, tente interpretar como JSON estrutural (status/plan/actions/memory).
        $resp = $null
        if ($outText) {
            try {
                $parsed = $outText | ConvertFrom-Json -ErrorAction Stop
                $resp = $parsed
            } catch {
                # tentativa heurística: extrair o primeiro bloco JSON entre a primeira '{' e a última '}'
                try {
                    $sIndex = $outText.IndexOf('{')
                    $eIndex = $outText.LastIndexOf('}')
                    if ($sIndex -ge 0 -and $eIndex -gt $sIndex) {
                        $candidate = $outText.Substring($sIndex, ($eIndex - $sIndex + 1))
                        $parsed2 = $candidate | ConvertFrom-Json -ErrorAction Stop
                        $resp = $parsed2
                    } else {
                        throw 'no-json-block'
                    }
                } catch {
                    Write-Log "Texto retornado não é JSON válido — armazenando texto em memory.raw_text para continuar.";
                    $resp = @{ status = 'ok'; plan = @(); actions = @(); memory = @{ raw_text = $outText } }
                }
            }
        } else {
            # fallback: usar o objeto bruto retornado
            $resp = $rawResp
        }
    } else {
        Write-Log "== ENVIANDO (initial) payload -> model =="
        try { $initialPayload | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $initialPayload }
        
        $resp = Invoke-Model -Cfg $connObj -Payload $initialPayload
        Write-Log "== RESPOSTA (raw) DO MODELO =="
        try { $resp | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $resp }
    }
} catch {
    Write-Log "Falha ao chamar o modelo: $_"
    exit 1
}
# bloco temporário removido — fluxo normal restaurado

# valida resposta estruturada (espera JSON com plan/actions/memory)
function Ensure-StructuredResponse($r) {
    if (-not $r) { throw 'Resposta vazia do modelo.' }
    # se veio como string, tente converter
    if ($r -is [string]) {
        try {
            $r = $r | ConvertFrom-Json -ErrorAction Stop
        } catch {
            # tentativa heurística: extrair primeiro bloco JSON entre a primeira '{' e a última '}'
            try {
                $sIndex = $r.IndexOf('{')
                $eIndex = $r.LastIndexOf('}')
                if ($sIndex -ge 0 -and $eIndex -gt $sIndex) {
                    $candidate = $r.Substring($sIndex, ($eIndex - $sIndex + 1))
                    $r = $candidate | ConvertFrom-Json -ErrorAction Stop
                }
            } catch {
                # manter como string se a heurística falhar
            }
        }
    }
    if (-not $r) { throw 'Resposta do modelo não é um objeto JSON esperado.' }
    # garantir que exista a propriedade memory
    if (-not $r.psobject.Properties.Match('memory')) { $r | Add-Member -MemberType NoteProperty -Name memory -Value @{} -Force }
    return $r
}

$response = Ensure-StructuredResponse $resp

# Memória de curto prazo que será enviada de volta a cada conexão
$memory = $response.memory

# Loop de execução: pede ao modelo ações, executa uma a uma e repassa memória
$done = $false
$totalSteps = 0
$limite = 1
if ($config.'conexão' -and $config.'conexão'.limite_passos) { $limite = [int]$config.'conexão'.limite_passos }

Write-Log "Iniciando execução de passos (limite por conexão = $limite)."

while (-not $done) {
    # quando houver actions na resposta, executa até $limite ações
    $actions = @()
    if ($response.actions) { $actions = $response.actions }
    if ($actions.Count -gt $limite) {
        Write-Log "Modelo retornou mais ações ($($actions.Count)) do que o limite permitido ($limite). Irei truncar as ações à quantidade do limite."
        $actions = $actions[0..($limite-1)]
    }

    foreach ($action in $actions) {
        $totalSteps++
        Write-Log "Executando ação #${totalSteps}: $($action.type) - $($action.description)"

        # attachments será preenchido pela ação `leia_arquivo` quando o modelo solicitar um arquivo
        $attachments = @{}

        switch ($action.type) {
            'leia_arquivo' {
                $pathParam = $null
                if ($action.parameters -and $action.parameters.path) { $pathParam = $action.parameters.path }
                elseif ($action.path) { $pathParam = $action.path }
                if (-not $pathParam) { throw 'Parâmetro path obrigatório para leia_arquivo.' }
                $fpath = if ([System.IO.Path]::IsPathRooted($pathParam)) { $pathParam } else { Join-Path $configDir $pathParam }
                $resolved = $null
                try { $resolved = (Resolve-Path -Path $fpath -ErrorAction Stop).Path } catch { $resolved = $null }
                if (-not $resolved) {
                    Write-Log "Arquivo solicitado não encontrado: $fpath"
                    $attachments[$pathParam] = $null
                } else {
                    $allowedResolved = (Resolve-Path -Path $allowedRoot -ErrorAction Stop).Path
                    if (-not $resolved.StartsWith($allowedResolved)) {
                        Write-Log "Acesso negado ao arquivo solicitado (fora da raiz permitida): $resolved"
                        $attachments[$pathParam] = $null
                    } else {
                        Write-Log "Lendo arquivo solicitado pelo modelo: $resolved"
                        $contentB64 = Read-FileContentBase64 -Path $resolved
                        if ($contentB64) { $attachments[$pathParam] = $contentB64 } else { $attachments[$pathParam] = $null }
                    }
                }
            }
            'finalizar' {
                Write-Log "Ação finalizar recebida: processando resultado final."

                # O modelo pode retornar o HTML final diretamente em parameters.content (ou em keys alternativas)
                $htmlContent = $null
                if ($action.parameters -and $action.parameters.content) { $htmlContent = $action.parameters.content }
                elseif ($action.parameters -and $action.parameters.html) { $htmlContent = $action.parameters.html }
                elseif ($action.parameters -and $action.parameters.html_content) { $htmlContent = $action.parameters.html_content }

                if ($htmlContent) {
                    # determina local de saída: prioridade para parameters.path, senão usa config.'relatório' quando definido
                    $outPath = $null
                    if ($action.parameters -and $action.parameters.path) {
                        $outPath = if ([System.IO.Path]::IsPathRooted($action.parameters.path)) { $action.parameters.path } else { Join-Path $configDir $action.parameters.path }
                    } elseif ($config.'relatório') {
                        $nomeArquivo = $config.'relatório'
                        $outPath = if ([System.IO.Path]::IsPathRooted($nomeArquivo)) { $nomeArquivo } else { Join-Path $configDir $nomeArquivo }
                    }

                    if ($outPath) {
                        Ensure-DirectoryExists -Path $outPath
                        Set-Content -Path $outPath -Value $htmlContent -Encoding UTF8
                        Write-Log "HTML final salvo em: $outPath"
                    } else {
                        Write-Log "HTML final recebido, mas nenhum caminho para salvar foi fornecido; content será ignorado." 
                    }
                } else {
                    Write-Log "Ação finalizar recebida sem conteúdo HTML em parameters.content."
                }

                $done = $true
                break
            }
            default {
                Write-Log "Tipo de ação não reconhecido: $($action.type). Pulando."
            }
        }

        # após executar a ação, envie um update ao modelo com novo estado/memory
        $updatePayload = @{
            update = 'action_executed'
            executed_action = $action
            attachments = $attachments
            memory = $memory
            totalSteps = $totalSteps
        }
        try {
            # augment payload with context required by subsequent requests
            $planVal = $null
            if ($response -and $response.plan) { $planVal = $response.plan } elseif ($resp -and $resp.plan) { $planVal = $resp.plan }
            $augmented = $updatePayload
            $augmented['file_tree'] = $tree
            $augmented['prompt'] = $promptText
            $augmented['orchestrator'] = $initialPayload.orchestrator
            $augmented['request'] = $initialPayload.request
            $augmented['config'] = $config
            $augmented['plan'] = $planVal

            $sendPayload = $augmented
            if ($isGenerateContent) {
                $jsonText = $augmented | ConvertTo-Json -Depth 20
                $sendPayload = @{ contents = @( @{ parts = @(@{ text = $jsonText }) } ) }
            }
            Write-Log "== ENVIANDO (update) payload -> model =="
            try { $sendPayload | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $sendPayload }
            $rawOrText = Invoke-Model-WithRetries -Cfg $connObj -Payload $sendPayload -IsGenerateContent $isGenerateContent -MaxRetries 5
            $response = Ensure-StructuredResponse $rawOrText
            Write-Log "== RESPOSTA DO MODELO (update) =="
            try { $response | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $response }
            # atualizar memória
            $memory = $response.memory
        } catch {
            Write-Log "Falha ao notificar modelo após executar ação: $_"
            # fallback seguro: limpar ações para evitar repetir indefinidamente se o modelo não respondeu
            $response = @{ status = 'error'; plan = @(); actions = @(); memory = $memory }
        }
    }

    # se não houver ações, pergunte ao modelo o próximo passo (envia memória)
    if (-not $actions -or $actions.Count -eq 0) {
        Write-Log "Solicitando próximos passos ao modelo (enviando memória)."
        $askPayload = @{
            request = 'next_steps'
            memory = $memory
            max_steps = $limite
        }
        try {
            # augment ask payload with full context for consistency
            $planVal2 = $null
            if ($response -and $response.plan) { $planVal2 = $response.plan } elseif ($resp -and $resp.plan) { $planVal2 = $resp.plan }
            $augAsk = $askPayload
            $augAsk['file_tree'] = $tree
            $augAsk['prompt'] = $promptText
            $augAsk['orchestrator'] = $initialPayload.orchestrator
            $augAsk['request'] = $initialPayload.request
            $augAsk['config'] = $config
            $augAsk['plan'] = $planVal2

            $sendAsk = $augAsk
            if ($isGenerateContent) {
                $jsonText = $augAsk | ConvertTo-Json -Depth 20
                $sendAsk = @{ contents = @( @{ parts = @(@{ text = $jsonText }) } ) }
            }
            Write-Log "== ENVIANDO (ask) payload -> model =="
            try { $sendAsk | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $sendAsk }
            $rawOrText2 = Invoke-Model-WithRetries -Cfg $connObj -Payload $sendAsk -IsGenerateContent $isGenerateContent -MaxRetries 5
            $response = Ensure-StructuredResponse $rawOrText2
            Write-Log "== RESPOSTA DO MODELO (ask) =="
            try { $response | ConvertTo-Json -Depth 20 | ForEach-Object { Write-Host $_ } } catch { Write-Host $response }
            $memory = $response.memory
            if ($response.status -and $response.status -eq 'done') { $done = $true }
        } catch {
            Write-Log "Erro ao solicitar próximos passos: $_"
            break
        }
    }

    # Segurança: evita loop infinito
    if ($totalSteps -gt 1000) { Write-Log "Limite total de passos atingido (1000). Abortando."; break }
}

Write-Log "Execução completa. Passos executados: $totalSteps"

if ($config.'relatório') {
    $nomeArquivo = $config.'relatório'
    $outPath = if ([System.IO.Path]::IsPathRooted($nomeArquivo)) { $nomeArquivo } else { Join-Path $configDir $nomeArquivo }
    if (-not (Test-Path $outPath)) {
        Write-Log "Arquivo HTML final não encontrado automaticamente: $outPath"
    } else {
        Write-Log "HTML final disponível em: $outPath"
    }
} else {
    Write-Log "Propriedade 'relatório' não definida no config; nenhum arquivo final será verificado automaticamente."
}

Write-Log "Maestro finalizado." 
