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
        Instruções para o orquestrador (maestro.ps1 / maestro.py).

        Responda SOMENTE com JSON válido e puro — sem Markdown, sem fences de
        código (```), sem texto explicativo adicional. O JSON de resposta deve
        conter exatamente os campos a seguir (tipos esperados indicados):

        - "status": string — valores permitidos: "ok" ou "error".
        - "plan": array — plano de alto nível (pode ficar vazio: []).
        - "actions": array de objetos — cada ação deve ter pelo menos:
                {"id": <string|int>, "type": <string>, "description": <string>, "parameters": <objeto>}.
        - "memory": objeto — estado persistido que será devolvido ao orquestrador
            e enviado na próxima chamada.

        Tipos de ação suportados e comportamento esperado:
        - "leia_arquivo": solicita ao orquestrador o conteúdo de um arquivo.
                * parâmetros esperados: {"path": "caminho/relativo/ou/absoluto"}.
                * O orquestrador só fornecerá arquivos que estejam dentro do caminho
                    permitido definido em `ferramentas[*].arquivos.caminho_permitido`.
                * Somente um arquivo por ação será retornado. O conteúdo será enviado
                    ao orquestrador em base64, no objeto `attachments` com a chave igual
                    ao valor de `path` solicitado. Se o arquivo não existir ou estiver
                    fora da raiz permitida, o valor do attachment será `null`.

        - "finalizar": encerra a execução e pode fornecer o HTML final.
                * parâmetros aceitos (ordem de preferência):
                        - "content" (string) — conteúdo HTML final.
                        - "html" (string) ou "html_content" (string) — alternativas.
                        - opcional: "path" — caminho onde o orquestrador deve salvar o HTML.
                * Se nenhum "path" for fornecido, o orquestrador tentará salvar em
                    `relatório` definido no arquivo de configuração. Se não houver destino
                    válido, o HTML será recebido mas não será salvo.

        Regras gerais e restrições:
        - Sempre retorne um objeto "memory" atualizado com o estado necessário
            para a próxima conexão; o orquestrador reenviará essa memória.
        - O orquestrador executa ações sequencialmente, respeitando
            `conexão.limite_passos` (truncará ações se o modelo retornar mais do que
            o limite). Planeje suas ações de forma concisa.
        - Para chamadas que usam Google Generative Language (endpoints contendo
            ":generateContent") o payload pode ser enviado/recebido no formato de
            candidatos; verifique que o JSON retornado siga o esquema acima.
        - Não inclua dados binários fora do base64; use base64 quando precisar
            transmitir conteúdo de arquivos.
        - As respostas devem ser UTF-8 e conter apenas o JSON solicitado — nada
            além (sem logs, sem explicações, sem comentários).

        Campos de ação recomendados (exemplo):
        {"id": "1", "type": "leia_arquivo", "description": "Ler o CSV de
         clientes", "parameters": {"path": "dados/clientes.csv"}}

        Exceções e erros:
        - Em caso de erro operacional, retorne {"status": "error", "plan": [],
            "actions": [], "memory": {"error": "mensagem curta"}}.

        Observação final: seja preciso no uso de paths e parâmetros. O orquestrador
        segue estritamente as permissões de acesso a arquivos e salvará o HTML
        final automaticamente quando receber uma ação "finalizar" com
        `parameters.content` (ou similares).
"""


# Removida a função de logging. Substituímos por um no-op para manter chamadas existentes
# sem produzir saída. Isso simplifica a remoção de logs sem alterar demais o fluxo.
write_log = lambda *args, **kwargs: None


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
                'path': rel.replace(os.path.sep, '/'),
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
    return http_post_json(endpoint, headers, body)


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
            return {'status': 'ok', 'plan': [], 'actions': [], 'memory': {'raw_text': r}}
    if isinstance(r, dict):
        if 'memory' not in r:
            r['memory'] = {}
        return r
    # caso inesperado, tente converter para dict via JSON
    try:
        return json.loads(json.dumps(r))
    except Exception:
        return {'status': 'ok', 'plan': [], 'actions': [], 'memory': {}}


def print_action_descriptions(actions: Optional[List[Dict[str, Any]]]) -> None:
    """Imprime na saída as descrições das ações retornadas pelo modelo.

    Formato simples e legível com id, type e description.
    """
    if not actions:
        return
    for a in actions:
        aid = a.get('id')
        typ = a.get('type')
        desc = a.get('description')
        print(f"-- {aid}:{typ}: {desc}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config_path', help='Caminho para o arquivo de configuração (YAML/JSON)')
    args = parser.parse_args()
    config_path = args.config_path

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
    ferramentas = config.get('ferramentas')
    if ferramentas and isinstance(ferramentas, list):
        for f in ferramentas:
            arquivos = f.get('arquivos') if isinstance(f, dict) else None
            if arquivos and 'caminho_permitido' in arquivos:
                allowed = arquivos['caminho_permitido']
                allowed_root = allowed if os.path.isabs(allowed) else os.path.join(config_dir, allowed)
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
        'request': 'Por favor responda com objeto JSON estruturado contendo: status("ok"/"error"), plan:[...], actions:[{id,type,description,parameters}], memory:{}'
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
            write_log('== ENVIANDO (initial generateContent) payload -> model ==')
            write_log(json.dumps(gen_payload, ensure_ascii=False))
            raw_resp = invoke_model(conn_obj, gen_payload)
            write_log('== RESPOSTA (raw) DO MODELO ==')
            write_log(json.dumps(raw_resp, ensure_ascii=False) if isinstance(raw_resp, dict) else str(raw_resp))
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
                            resp = {'status': 'ok', 'plan': [], 'actions': [], 'memory': {'raw_text': out_text}}
                    else:
                        resp = {'status': 'ok', 'plan': [], 'actions': [], 'memory': {'raw_text': out_text}}
            else:
                resp = raw_resp
        else:
            write_log('== ENVIANDO (initial) payload -> model ==')
            write_log(json.dumps(initial_payload, ensure_ascii=False))
            resp = invoke_model(conn_obj, initial_payload)
            write_log('== RESPOSTA (raw) DO MODELO ==')
            write_log(json.dumps(resp, ensure_ascii=False) if isinstance(resp, dict) else str(resp))
    except Exception as e:
        write_log(f"Falha ao chamar o modelo: {e}")
        sys.exit(1)

    def _ensure_structured(x):
        return ensure_structured_response(x)

    response = _ensure_structured(resp)
    memory = response.get('memory', {})
    # Exibir descrições das ações retornadas inicialmente pelo modelo
    print_action_descriptions(response.get('actions'))

    done = False
    total_steps = 0
    limite = 1
    try:
        if 'conexão' in config and 'limite_passos' in config['conexão']:
            limite = int(config['conexão']['limite_passos'])
    except Exception:
        limite = 1

    write_log(f"Iniciando execução de passos (limite por conexão = {limite}).")

    while not done:
        actions = response.get('actions') or []
        if len(actions) > limite:
            write_log(f"Modelo retornou mais ações ({len(actions)}) do que o limite permitido ({limite}). Irei truncar as ações à quantidade do limite.")
            actions = actions[:limite]

        for action in actions:
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
                fpath = path_param if os.path.isabs(path_param) else os.path.join(config_dir, path_param)
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

                write_log('== ENVIANDO (update) payload -> model ==')
                write_log(json.dumps(send_payload, ensure_ascii=False) if isinstance(send_payload, dict) else str(send_payload))
                raw_or_text = invoke_model_with_retries(conn_obj, send_payload, is_generate_content=is_generate_content, max_retries=5)
                response = _ensure_structured(raw_or_text)
                memory = response.get('memory', {})
                # Mostrar ações retornadas após update
                print_action_descriptions(response.get('actions'))
            except Exception as e:
                write_log(f"Falha ao notificar modelo após executar ação: {e}")
                response = {'status': 'error', 'plan': [], 'actions': [], 'memory': memory}

        # if no actions, ask next steps
        if not actions or len(actions) == 0:
            write_log('Solicitando próximos passos ao modelo (enviando memória).')
            ask_payload = {'request': 'next_steps', 'memory': memory, 'max_steps': limite}
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
                write_log('== ENVIANDO (ask) payload -> model ==')
                write_log(json.dumps(send_ask, ensure_ascii=False))
                raw_or_text2 = invoke_model_with_retries(conn_obj, send_ask, is_generate_content=is_generate_content, max_retries=5)
                response = _ensure_structured(raw_or_text2)
                memory = response.get('memory', {})
                # Mostrar ações retornadas após ask
                print_action_descriptions(response.get('actions'))
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
