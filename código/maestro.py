#!/usr/bin/env python3
"""
Orquestrador de agente de IA (portado de maestro.ps1).
Uso: python código/maestro.py /caminho/para/exemplos/clientes/config.yaml

Comportamento resumido:
- Lê um arquivo de configuração (YAML ou JSON) com chaves em português.
- Lê o arquivo `solicitação` (Markdown) referenciado pelo config.
- Gera uma árvore de arquivos acessíveis a partir de `ferramentas[*].arquivos.caminho_permitido`.
- Envia payloads ao endpoint do modelo e executa ações retornadas: `leia_arquivo` e `finalizar`.
- Suporta endpoints no formato Google Generative Language (contendo `:generateContent`) e Azure OpenAI.

Observação: para parsear YAML é recomendado instalar `pyyaml` (pip install pyyaml).
"""

from __future__ import annotations
import sys
import os
import json
import base64
import time
import pathlib
import argparse
import datetime
from typing import Any, Dict, Optional, List

try:
    import urllib.request as _urllib_request
    import urllib.error as _urllib_error
except Exception:
    _urllib_request = None
    _urllib_error = None

try:
    import yaml
except Exception:
    yaml = None


ORCHESTRATOR_INSTRUCTIONS = """
                Eu sou o orquestrador. Você deve executar exatamente UM passo por resposta
                para atender à solicitação inicial do usuário descrita na propriedade
                `prompt` enviada no payload. Responda SOMENTE com JSON válido e puro —
                sem Markdown, sem fences de código (```), sem texto explicativo adicional
                e sem campos extras não documentados.

                Formato obrigatório da resposta (campos e tipos esperados):

                - "status": string — valores permitidos: "ok" ou "error".
                - "plan": array — plano de alto nível (pode ficar vazio: []).
                - "action": objeto ou null — Ação única a ser executada pelo orquestrador;
                        quando presente, deve conter os campos mínimos:
                                {"id": <string|int>, "type": <string>, "description": <string>, "parameters": <objeto>}
                - "memory": objeto — estado persistido e incremental. Sempre forneça
                        um objeto (pode ser vazio) que represente o estado atual usado nas
                        próximas chamadas. O orquestrador reenviará esse `memory` a cada passo.

                Uso de `memory`:
                - `memory` deve acumular, de forma estruturada, tudo que será necessário
                    para gerar o relatório final (seções, resumos, índices de anexos,
                    metadados, progresso, mensagens de erro). Não use logs livres para
                    guardar informações obrigatórias.
                - Evite incluir dados binários não codificados; anexe binários como base64
                    nas propriedades `attachments` quando apropriado.

                Ações suportadas (tipos e parâmetros esperados):
                - "leia_arquivo": pede ao orquestrador o conteúdo de UM único arquivo.
                        * parâmetros: {"path": "caminho/relativo/ou/absoluto"}
                        * comportamento do orquestrador: retornará, no próximo envio, um
                            objeto `attachments` com chave igual a `path` e valor sendo o
                            conteúdo do arquivo em base64 ou `null` se não encontrado/permitido.
                        * restrição: o arquivo só será fornecido se estiver dentro da raiz
                            permitida (`ferramentas[*].arquivos.caminho_permitido`);
                            apenas um arquivo por ação.

                - "finalizar": indica que o trabalho terminou e pode conter o HTML final.
                        * parâmetros aceitos (ordem de preferência):
                                - "content" (string) — HTML final.
                                - "html" ou "html_content" (strings) — alternativas.
                                - opcional: "path" — caminho onde o orquestrador deve salvar o HTML.
                        * comportamento: se `content` (ou alternativa) estiver presente, o
                            orquestrador tentará salvar o HTML no `path` fornecido ou no
                            `relatório` do arquivo de configuração; caso contrário, apenas
                            registrará/retornará o conteúdo recebido.

                Propriedades que podem ser encontradas no payload enviado ao modelo
                (ou que o orquestrador irá enviar/reenviar). Descreva-as claramente:
                - `orchestrator`: string — instruções do orquestrador (este texto).
                - `config`: objeto — configuração carregada do arquivo (JSON/YAML);
                        contém chaves como `conexão`, `ferramentas`, `relatório`, etc.
                - `prompt`: string — solicitação inicial do usuário (conteúdo do arquivo
                        `solicitação`). O modelo deve tomar este texto como objetivo primário.
                - `file_tree`: array — árvore de arquivos acessíveis, cada item é um
                        objeto com {"path": <relativo>, "length": <int|null>, "lastWrite": <iso>}.
                - `request`: string — contexto do pedido atual (ex.: instrução inicial,
                        "next_steps", "action_executed").
                - `attachments`: objeto — mapeamento {"path": "base64..." | null}
                        fornecido pelo orquestrador quando arquivos foram lidos.
                - `memory`: objeto — o estado persistente enviado/recebido entre passos.
                - `executed_action`: objeto — quando o orquestrador envia atualização
                        após executar uma ação, inclui a ação que foi executada.
                - `totalSteps`: inteiro — número de ações já executadas até o momento.
                - `max_steps`: inteiro — número máximo remanescente de requisições permitido.
                - `plan`: array — plano atual (se fornecido pelo modelo em respostas
                        anteriores).
                - `file_tree` (completo) e `prompt` podem ser reenviados em updates para
                        dar contexto suficiente ao modelo.

                Formatos especiais:
                - Para endpoints Google Generative Language (URL contendo ":generateContent")
                    o orquestrador pode embalar o JSON em `{ "contents": [{ "parts": [{ "text": "..." }]}] }`.
                - Para endpoints tipo chat (Azure OpenAI), o orquestrador pode converter
                    o payload em `{ "messages": [{ "role": "user", "content": "..." }] }`.

                Regras operacionais e boas práticas:
                - O modelo DEVE retornar UM ÚNICO objeto `action` por resposta. Se não
                    houver nenhuma ação a executar, retorne `"action": null`.
                - Atualize `memory` a cada resposta com o estado incremental completo
                    necessário para que o orquestrador e chamadas futuras possam montar
                    o relatório final sem depender de texto no log.
                - Quando pedir leitura de arquivo, especifique `leia_arquivo` com
                    `parameters.path`; o orquestrador responderá com `attachments[path]`.
                - Use base64 para todo conteúdo de arquivo binário; não inclua binários
                    raw no JSON.
                - Em caso de falha operacional ou erro recuperável, retorne:
                        {"status":"error","plan":[],"action":null,"memory":{"error":"mensagem curta"}}

                Exemplo mínimo de `action` válido:
                {"id":"1","type":"leia_arquivo","description":"Ler CSV de clientes","parameters":{"path":"dados/clientes.csv"}}

                Observação final: seja preciso nos paths, use `memory` como fonte única
                de verdade para progresso e conteúdo acumulado, e retorne apenas o JSON
                solicitado. O orquestrador seguirá estritamente as permissões de
                arquivo e o limite de requisições configurado.
"""


# Função de logging: escreve mensagens com timestamp UTC em stderr ou
# em um arquivo quando `LOG_FILE_PATH` for configurado.
LOG_FILE_PATH: Optional[str] = None
def write_log(*args, sep=' ', end='\n'):
    """Escreve uma linha de log com timestamp UTC.

    Se `LOG_FILE_PATH` estiver definido, tenta escrever em append nesse
    arquivo (criando diretórios se necessário). Em caso de falha, cai
    back para `stderr`. Não lança exceções.
    """
    try:
        ts = datetime.datetime.utcnow().isoformat()
        msg = sep.join(str(a) for a in args)
        line = f"{ts} {msg}{end}"
        if LOG_FILE_PATH:
            try:
                log_dir = os.path.dirname(LOG_FILE_PATH)
                if log_dir and not os.path.exists(log_dir):
                    os.makedirs(log_dir, exist_ok=True)
                with open(LOG_FILE_PATH, 'a', encoding='utf-8') as fh:
                    fh.write(line)
                return
            except Exception:
                # fallback para stderr
                pass
        try:
            sys.stderr.write(line)
        except Exception:
            pass
    except Exception:
        try:
            sys.stderr.write("logging failure\n")
        except Exception:
            pass

# Flag para controlar exibição das mensagens trocadas com o modelo
SHOW_MESSAGES = False
SHOW_MEMORY = False


def _print_prefixed(prefix: str, text: Any) -> None:
    """Imprime `text` (pode ser multi-linha) prefixando cada linha com `prefix`."""
    if text is None:
        return
    s = str(text)
    # Garantir que mesmo string vazia resulte em uma linha impressa
    lines = s.splitlines() or [s]
    for line in lines:
        print(f"{prefix} {line}")


def print_sent(text: Any) -> None:
    _print_prefixed('>>', text)


def print_received(text: Any) -> None:
    _print_prefixed('<<', text)


def parse_config(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Arquivo de configuração não encontrado: {path}")
    text = open(path, 'r', encoding='utf-8').read()
    ext = os.path.splitext(path)[1].lower()
    # If it's JSON or looks like JSON, parse as JSON
    if ext == '.json':
        return json.loads(text)
    try:
        return json.loads(text)
    except Exception:
        pass
    if yaml:
        return yaml.safe_load(text)
    raise RuntimeError("PyYAML não está instalado e o arquivo não é JSON; instale com: pip install pyyaml")


def get_file_tree(root: str) -> List[Dict[str, Any]]:
    root_full = os.path.abspath(root)
    out = []
    for dirpath, _, filenames in os.walk(root_full):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_full)
            try:
                stat = os.stat(full)
                length = stat.st_size
                last_write = datetime.datetime.utcfromtimestamp(stat.st_mtime).isoformat()
            except Exception:
                length = None
                last_write = None
            out.append({
                'path': rel,
                'fullPath': full,
                'length': length,
                'lastWrite': last_write,
            })
    return out


def read_file_base64(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        b = f.read()
    return base64.b64encode(b).decode('ascii')


def ensure_directory_exists(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def http_post_json(url: str, headers: Dict[str, str], body_obj: Any, timeout: int = 60) -> Any:
    # body_obj already JSON-serializable
    body = json.dumps(body_obj).encode('utf-8')
    req = _urllib_request.Request(url, data=body, headers=headers or {}, method='POST')
    try:
        with _urllib_request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            # try to decode JSON
            try:
                return json.loads(data.decode('utf-8'))
            except Exception:
                # if not JSON, return raw decoded text
                try:
                    return data.decode('utf-8')
                except Exception:
                    return data
    except _urllib_error.HTTPError as he:
        body = None
        try:
            body = he.read().decode('utf-8')
        except Exception:
            body = str(he)
        raise RuntimeError(f"HTTPError {he.code}: {body}")
    except Exception as e:
        raise


def invoke_model(cfg: Dict[str, Any], payload: Any) -> Any:
    endpoint = cfg.get('endpoint') or cfg.get('endpoint'.replace('-', '_'))
    api_key = cfg.get('api_key_value')
    auth_type = cfg.get('auth_type')
    api_key_header = cfg.get('api_key_header')
    if not endpoint:
        raise RuntimeError('endpoint da conexão não configurado.')
    headers = {'Content-Type': 'application/json'}
    if auth_type and auth_type == 'api_key' and api_key:
        header_name = api_key_header if api_key_header else 'x-goog-api-key'
        headers[header_name] = api_key
    elif api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    platform = cfg.get('platform')
    # Azure OpenAI expects a chat-like body for some endpoints
    if platform == 'Azure OpenAI':
        # try to extract text content
        message_text = None
        if isinstance(payload, dict) and 'contents' in payload:
            try:
                message_text = payload['contents'][0]['parts'][0]['text']
            except Exception:
                message_text = json.dumps(payload)
        else:
            try:
                message_text = json.dumps(payload)
            except Exception:
                message_text = str(payload)
        model_name = cfg.get('model') or (payload.get('model') if isinstance(payload, dict) else None) or 'gpt-35-turbo'
        azure_body = {'model': model_name, 'messages': [{'role': 'user', 'content': message_text}]}
        body = azure_body
    else:
        body = payload

    write_log(f"endpoint raw: '{endpoint}'  authType: '{auth_type}'  apiKeyPresent: {bool(api_key)}  plataforma: {platform}")
    write_log(f"Enviando requisição ao modelo em {endpoint}...")
    # Se habilitado, exibe a mensagem enviada ao modelo (prefixo >>)
    try:
        if SHOW_MESSAGES:
            try:
                # tentar serializar o body de forma legível
                print_sent(json.dumps(body, ensure_ascii=False))
            except Exception:
                print_sent(body)
    except Exception:
        pass

    resp = http_post_json(endpoint, headers, body)

    # Se habilitado, exibe a mensagem recebida do modelo (prefixo <<)
    try:
        if SHOW_MESSAGES:
            try:
                if isinstance(resp, dict):
                    print_received(json.dumps(resp, ensure_ascii=False))
                else:
                    print_received(resp)
            except Exception:
                print_received(resp)
    except Exception:
        pass

    return resp


def invoke_model_with_retries(cfg: Dict[str, Any], payload: Any, is_generate_content: bool = False, max_retries: int = 5) -> Any:
    for attempt in range(1, max_retries + 1):
        write_log(f"Tentativa de chamada ao modelo: {attempt} / {max_retries}")
        try:
            raw = invoke_model(cfg, payload)
        except Exception as e:
            write_log(f"Erro na chamada ao modelo (tentativa {attempt}): {e}")
            raw = None
        if is_generate_content:
            out_text = None
            try:
                if raw and isinstance(raw, dict) and 'candidates' in raw and len(raw['candidates']) > 0:
                    out_text = raw['candidates'][0]['content']['parts'][0]['text']
            except Exception:
                out_text = None
            if out_text:
                return out_text
            write_log(f"Resposta vazia ou sem texto do candidato (attempt {attempt}).")
            if attempt < max_retries:
                time.sleep(2 * attempt)
                continue
            else:
                raise RuntimeError(f"Resposta vazia do modelo após {max_retries} tentativas.")
        else:
            if raw is not None:
                return raw
            write_log(f"Resposta vazia do modelo (attempt {attempt}).")
            if attempt < max_retries:
                time.sleep(2 * attempt)
                continue
            else:
                raise RuntimeError(f"Resposta vazia do modelo após {max_retries} tentativas.")


def ensure_structured_response(r: Any) -> Dict[str, Any]:
    if r is None:
        raise RuntimeError('Resposta vazia do modelo.')
    if isinstance(r, str):
        # tenta parsear JSON direto
        try:
            return json.loads(r)
        except Exception:
            # heurística: extrair bloco entre primeira '{' e última '}'
            s_index = r.find('{')
            e_index = r.rfind('}')
            if s_index >= 0 and e_index > s_index:
                candidate = r[s_index:e_index + 1]
                try:
                    return json.loads(candidate)
                except Exception:
                    pass
            # fallback: retornar string em memory.raw_text
            return {'status': 'ok', 'plan': [], 'action': None, 'memory': {'raw_text': r}}
    # Se for um dicionário, tentar detectar formatos conhecidos de resposta.
    if isinstance(r, dict):
        # Azure OpenAI: olhar em choices[0].message.content
        try:
            if 'choices' in r and isinstance(r['choices'], list) and len(r['choices']) > 0:
                choice0 = r['choices'][0]
                content = None
                if isinstance(choice0, dict):
                    msg = choice0.get('message')
                    if isinstance(msg, dict) and 'content' in msg:
                        content = msg.get('content')
                    elif 'text' in choice0:
                        content = choice0.get('text')
                # Se encontramos conteúdo textual, tentar parsear o JSON interno
                if content is not None:
                    # content pode ser string ou objeto; normalizar para string
                    content_text = content if isinstance(content, str) else (content.get('content') if isinstance(content, dict) and 'content' in content else json.dumps(content))
                    if isinstance(content_text, str):
                        try:
                            parsed = json.loads(content_text)
                            if isinstance(parsed, dict):
                                if 'memory' not in parsed:
                                    parsed['memory'] = {}
                                return parsed
                            # se o JSON parseado não é dict, encapsular
                            return {'status': 'ok', 'plan': [], 'action': None, 'memory': {'raw_parsed': parsed}}
                        except Exception:
                            # heurística: extrair bloco JSON dentro do texto
                            s_index = content_text.find('{')
                            e_index = content_text.rfind('}')
                            if s_index >= 0 and e_index > s_index:
                                candidate = content_text[s_index:e_index + 1]
                                try:
                                    parsed2 = json.loads(candidate)
                                    if isinstance(parsed2, dict):
                                        if 'memory' not in parsed2:
                                            parsed2['memory'] = {}
                                        return parsed2
                                except Exception:
                                    pass
                            return {'status': 'ok', 'plan': [], 'action': None, 'memory': {'raw_text': content_text}}
        except Exception:
            # falha ao inspecionar formato choices — continuar com fallback abaixo
            pass

        # fallback: garantir que exista 'memory' e retornar o dict original
        if 'memory' not in r:
            r['memory'] = {}
        return r
    # caso inesperado, tente converter para dict via JSON
    try:
        return json.loads(json.dumps(r))
    except Exception:
        return {'status': 'ok', 'plan': [], 'action': None, 'memory': {}}


def print_action_descriptions(action: Optional[Dict[str, Any]]) -> None:
    """Imprime na saída a descrição de uma única ação (`action`)."""
    if not action or not isinstance(action, dict):
        return
    aid = action.get('id')
    typ = action.get('type')
    desc = action.get('description')
    print(f"-- {aid}:{typ}: {desc}")


def print_memory(mem: Any) -> None:
    """Imprime a memória com prefixo `MEM` e formatação legível (JSON)."""
    if mem is None:
        return
    try:
        pretty = json.dumps(mem, ensure_ascii=False, indent=2)
    except Exception:
        pretty = str(mem)
    _print_prefixed('MEM', pretty)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config_path', help='Caminho para o arquivo de configuração (YAML/JSON)')
    parser.add_argument('--show-messages', '-m', action='store_true', help='Mostrar mensagens enviadas/recebidas do modelo (prefixo >> e <<)')
    parser.add_argument('--show-memory', '-M', action='store_true', help='Mostrar a memória retornada a cada passo (prefixo MEM)')
    parser.add_argument('--log-file', '-l', help='Caminho para arquivo de log; se fornecido, logs serão salvos nele (append).')
    args = parser.parse_args()
    config_path = args.config_path
    global SHOW_MESSAGES
    global SHOW_MEMORY
    global LOG_FILE_PATH
    SHOW_MESSAGES = bool(getattr(args, 'show_messages', False))
    SHOW_MEMORY = bool(getattr(args, 'show_memory', False))
    # definir caminho do arquivo de log se fornecido
    if getattr(args, 'log_file', None):
        LOG_FILE_PATH = args.log_file

    write_log(f"Carregando configuração: {config_path}")
    config = parse_config(config_path)

    config_dir = os.path.dirname(os.path.abspath(config_path))

    # solicitação
    if 'solicitação' not in config:
        write_log("Erro: propriedade 'solicitação' ausente no arquivo de configuração.")
        sys.exit(1)
    prompt_path = config['solicitação']
    prompt_path = prompt_path if os.path.isabs(prompt_path) else os.path.join(config_dir, prompt_path)
    if not os.path.exists(prompt_path):
        write_log(f"Erro: arquivo apontado por 'solicitação' não encontrado: {prompt_path}")
        sys.exit(1)
    write_log(f"Lendo solicitação: {prompt_path}")
    with open(prompt_path, 'r', encoding='utf-8') as f:
        prompt_text = f.read()

    # allowed root
    allowed_root = None
    # flag indicando se o caminho_permitido originalmente era absoluto
    allowed_root_from_absolute = False
    ferramentas = config.get('ferramentas')
    if ferramentas and isinstance(ferramentas, list):
        for f in ferramentas:
            arquivos = f.get('arquivos') if isinstance(f, dict) else None
            if arquivos and 'caminho_permitido' in arquivos:
                allowed = arquivos['caminho_permitido']
                if os.path.isabs(allowed):
                    allowed_root = allowed
                    allowed_root_from_absolute = True
                else:
                    allowed_root = os.path.join(config_dir, allowed)
                break
    if not allowed_root:
        allowed_root = config_dir
    write_log(f"Gerando árvore de arquivos em {allowed_root}")
    tree = get_file_tree(allowed_root)

    orchestrator_text = ORCHESTRATOR_INSTRUCTIONS

    initial_payload = {
        'orchestrator': orchestrator_text,
        'config': config,
        'prompt': prompt_text,
        'file_tree': [{'path': e['path'], 'length': e['length'], 'lastWrite': e['lastWrite']} for e in tree],
        'request': 'Por favor responda com objeto JSON estruturado contendo: status("ok"/"error"), plan:[...], action:{id,type,description,parameters}, memory:{}'
    }

    # monta objeto de conexão a partir das propriedades em português
    conn_obj = {}
    if 'conexão' not in config:
        raise RuntimeError("Propriedade 'conexão' ausente no arquivo de configuração; 'conexão.autenticação' é obrigatória.")
    conn = config['conexão']
    if 'endereço' in conn:
        conn_obj['endpoint'] = conn['endereço']
    elif 'endereco' in conn:
        conn_obj['endpoint'] = conn['endereco']
    if 'modelo' in conn:
        conn_obj['model'] = conn['modelo']
    elif 'model' in conn:
        conn_obj['model'] = conn['model']
    if 'chave' in conn:
        conn_obj['api_key_var'] = conn['chave']
    elif 'api_key' in conn:
        conn_obj['api_key_var'] = conn['api_key']
    if 'autenticação' not in conn:
        raise RuntimeError("Propriedade 'conexão.autenticação' ausente: obrigatória e deve ser uma string com o nome do header.")
    if not isinstance(conn['autenticação'], str):
        raise RuntimeError("Propriedade 'conexão.autenticação' inválida: deve ser uma string com o nome do header.")
    conn_obj['api_key_header'] = conn['autenticação']
    conn_obj['auth_type'] = 'api_key'

    conn_obj['api_key_value'] = None
    if 'api_key_var' in conn_obj and conn_obj['api_key_var']:
        ev = os.environ.get(conn_obj['api_key_var'])
        if ev:
            conn_obj['api_key_value'] = ev

    if 'plataforma' not in conn:
        raise RuntimeError("Propriedade 'conexão.plataforma' ausente no arquivo de configuração; use 'Google Gemini' ou 'Azure OpenAI'.")
    if not isinstance(conn['plataforma'], str):
        raise RuntimeError("Propriedade 'conexão.plataforma' inválida: deve ser uma string com o valor 'Google Gemini' ou 'Azure OpenAI'.")
    if conn['plataforma'] not in ('Google Gemini', 'Azure OpenAI'):
        raise RuntimeError("Propriedade 'conexão.plataforma' inválida: valor esperado 'Google Gemini' ou 'Azure OpenAI'.")
    conn_obj['platform'] = conn['plataforma']

    # construir endpoint padrão se precisa
    if not conn_obj.get('endpoint') and conn_obj.get('model') and conn_obj.get('platform') == 'Google Gemini':
        conn_obj['endpoint'] = f"https://generativelanguage.googleapis.com/v1beta/models/{conn_obj['model']}:generateContent"

    write_log('Enviando payload inicial ao modelo...')
    is_generate_content = bool(conn_obj.get('endpoint') and ':generateContent' in conn_obj.get('endpoint'))

    try:
        if is_generate_content:
            combined_text = initial_payload['orchestrator'] + "\n\n" + initial_payload['prompt']
            gen_payload = {'contents': [{'parts': [{'text': combined_text}]}]}
            raw_resp = invoke_model(conn_obj, gen_payload)
            out_text = None
            try:
                if raw_resp and isinstance(raw_resp, dict) and 'candidates' in raw_resp and len(raw_resp['candidates']) > 0:
                    out_text = raw_resp['candidates'][0]['content']['parts'][0]['text']
                else:
                    write_log(str(raw_resp))
            except Exception as e:
                write_log(f"Falha ao extrair texto do candidato: {e}")
                write_log(str(raw_resp))
            resp = None
            if out_text:
                try:
                    parsed = json.loads(out_text)
                    resp = parsed
                except Exception:
                    # heurística
                    s_idx = out_text.find('{')
                    e_idx = out_text.rfind('}')
                    if s_idx >= 0 and e_idx > s_idx:
                        candidate = out_text[s_idx:e_idx+1]
                        try:
                            parsed2 = json.loads(candidate)
                            resp = parsed2
                        except Exception:
                            write_log('Texto retornado não é JSON válido — armazenando texto em memory.raw_text para continuar.')
                            resp = {'status': 'ok', 'plan': [], 'action': None, 'memory': {'raw_text': out_text}}
                    else:
                        resp = {'status': 'ok', 'plan': [], 'action': None, 'memory': {'raw_text': out_text}}
            else:
                resp = raw_resp
        else:
            resp = invoke_model(conn_obj, initial_payload)
    except Exception as e:
        write_log(f"Falha ao chamar o modelo: {e}")
        sys.exit(1)

    def _ensure_structured(x):
        return ensure_structured_response(x)

    response = _ensure_structured(resp)
    memory = response.get('memory', {})
    # Exibir descrição da ação retornada inicialmente pelo modelo
    print_action_descriptions(response.get('action'))
    if SHOW_MEMORY:
        print_memory(memory)

    done = False
    total_steps = 0
    # O limite agora é um limite de requisições feitas ao modelo (max_requests).
    # Se o limite for alcançado, o orquestrador NÃO enviará novas requisições,
    # mesmo que não tenha recebido uma ação 'finalizar'.
    try:
        if 'conexão' in config and 'limite_passos' in config['conexão']:
            max_requests = int(config['conexão']['limite_passos'])
        else:
            max_requests = 1
    except Exception:
        max_requests = 1

    # Contador de requisições já realizadas (a chamada inicial já foi feita)
    requests_made = 1

    write_log(f"Iniciando execução (limite de requisições = {max_requests}). Requisições já feitas: {requests_made}.")

    while not done:
        # Extrair a ação única da resposta: 'action'
        action = response.get('action') if isinstance(response, dict) else None

        if action:
            total_steps += 1
            write_log(f"Executando ação #{total_steps}: {action.get('type')} - {action.get('description')}")
            attachments: Dict[str, Optional[str]] = {}

            typ = action.get('type')
            if typ == 'leia_arquivo':
                path_param = None
                if action.get('parameters') and action['parameters'].get('path'):
                    path_param = action['parameters']['path']
                elif action.get('path'):
                    path_param = action['path']
                if not path_param:
                    raise RuntimeError('Parâmetro path obrigatório para leia_arquivo.')
                # Se o parâmetro for absoluto, use diretamente.
                # Caso contrário, quando a configuração `caminho_permitido` foi
                # fornecida como um caminho absoluto, use-a como raiz para
                # reconstruir o caminho; caso contrário, use `config_dir`.
                if os.path.isabs(path_param):
                    fpath = path_param
                else:
                    base_for_relative = allowed_root if allowed_root_from_absolute else config_dir
                    fpath = os.path.join(base_for_relative, path_param)
                resolved = os.path.abspath(fpath) if os.path.exists(fpath) else None
                if not resolved:
                    write_log(f"Arquivo solicitado não encontrado: {fpath}")
                    attachments[path_param] = None
                else:
                    allowed_resolved = os.path.abspath(allowed_root)
                    if not resolved.startswith(allowed_resolved):
                        write_log(f"Acesso negado ao arquivo solicitado (fora da raiz permitida): {resolved}")
                        attachments[path_param] = None
                    else:
                        write_log(f"Lendo arquivo solicitado pelo modelo: {resolved}")
                        content_b64 = read_file_base64(resolved)
                        attachments[path_param] = content_b64

            elif typ == 'finalizar':
                write_log('Ação finalizar recebida: processando resultado final.')
                html_content = None
                params = action.get('parameters') or {}
                if params.get('content'):
                    html_content = params.get('content')
                elif params.get('html'):
                    html_content = params.get('html')
                elif params.get('html_content'):
                    html_content = params.get('html_content')
                if html_content:
                    out_path = None
                    if params.get('path'):
                        out_path = params.get('path') if os.path.isabs(params.get('path')) else os.path.join(config_dir, params.get('path'))
                    elif config.get('relatório'):
                        nome = config.get('relatório')
                        out_path = nome if os.path.isabs(nome) else os.path.join(config_dir, nome)
                    if out_path:
                        ensure_directory_exists(out_path)
                        with open(out_path, 'w', encoding='utf-8') as fw:
                            fw.write(html_content)
                        write_log(f"HTML final salvo em: {out_path}")
                    else:
                        write_log('HTML final recebido, mas nenhum caminho para salvar foi fornecido; content será ignorado.')
                else:
                    write_log('Ação finalizar recebida sem conteúdo HTML em parameters.content.')
                done = True
                break
            else:
                write_log(f"Tipo de ação não reconhecido: {typ}. Pulando.")

            update_payload = {
                'update': 'action_executed',
                'executed_action': action,
                'attachments': attachments,
                'memory': memory,
                'totalSteps': total_steps,
            }
            try:
                plan_val = response.get('plan') if response else None
                augmented = update_payload
                augmented['file_tree'] = tree
                augmented['prompt'] = prompt_text
                augmented['orchestrator'] = initial_payload['orchestrator']
                augmented['request'] = initial_payload['request']
                augmented['config'] = config
                augmented['plan'] = plan_val

                send_payload = augmented
                if is_generate_content:
                    json_text = json.dumps(augmented, ensure_ascii=False)
                    send_payload = {'contents': [{'parts': [{'text': json_text}]}]}

                # Antes de enviar o update, verificar se ainda podemos fazer requisições
                if requests_made >= max_requests:
                    write_log('Limite de requisições atingido; não enviarei atualização ao modelo.')
                    done = True
                    break

                raw_or_text = invoke_model_with_retries(conn_obj, send_payload, is_generate_content=is_generate_content, max_retries=5)
                requests_made += 1
                response = _ensure_structured(raw_or_text)
                memory = response.get('memory', {})
                # Mostrar ação retornada após update
                print_action_descriptions(response.get('action'))
                if SHOW_MEMORY:
                    print_memory(memory)
            except Exception as e:
                write_log(f"Falha ao notificar modelo após executar ação: {e}")
                response = {'status': 'error', 'plan': [], 'action': None, 'memory': memory}
        # Se não houve ação nesta resposta, solicitar próximos passos (se ainda pudermos requisitar)
        else:
            write_log('Nenhuma ação recebida nesta resposta.')

        if not action:
            # Antes de enviar a requisição para pedir próximos passos, verificar limite
            if requests_made >= max_requests:
                write_log('Limite de requisições atingido; não solicitarei próximos passos.')
                break

            write_log('Solicitando próximos passos ao modelo (enviando memória).')
            ask_payload = {'request': 'next_steps', 'memory': memory, 'max_steps': max(0, max_requests - requests_made)}
            try:
                plan_val2 = response.get('plan') if response else None
                aug_ask = ask_payload
                aug_ask['file_tree'] = tree
                aug_ask['prompt'] = prompt_text
                aug_ask['orchestrator'] = initial_payload['orchestrator']
                aug_ask['request'] = initial_payload['request']
                aug_ask['config'] = config
                aug_ask['plan'] = plan_val2

                send_ask = aug_ask
                if is_generate_content:
                    json_text = json.dumps(aug_ask, ensure_ascii=False)
                    send_ask = {'contents': [{'parts': [{'text': json_text}]}]}
                raw_or_text2 = invoke_model_with_retries(conn_obj, send_ask, is_generate_content=is_generate_content, max_retries=5)
                requests_made += 1
                response = _ensure_structured(raw_or_text2)
                memory = response.get('memory', {})
                # Mostrar ação retornada após ask
                print_action_descriptions(response.get('action'))
                if SHOW_MEMORY:
                    print_memory(memory)
                if response.get('status') == 'done':
                    done = True
            except Exception as e:
                write_log(f"Erro ao solicitar próximos passos: {e}")
                break

        if total_steps > 1000:
            write_log('Limite total de passos atingido (1000). Abortando.')
            break

    write_log(f"Execução completa. Passos executados: {total_steps}")

    if 'relatório' in config:
        nome = config['relatório']
        out_path = nome if os.path.isabs(nome) else os.path.join(config_dir, nome)
        if not os.path.exists(out_path):
            write_log(f"Arquivo HTML final não encontrado automaticamente: {out_path}")
        else:
            write_log(f"HTML final disponível em: {out_path}")
    else:
        write_log("Propriedade 'relatório' não definida no config; nenhum arquivo final será verificado automaticamente.")

    write_log('Maestro finalizado.')


if __name__ == '__main__':
    main()
