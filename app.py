import subprocess
import tempfile
import os
import re
import json
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ------------------------------------------------------------
# 1. FUNÇÕES AUXILIARES DE EXECUÇÃO
# ------------------------------------------------------------
def criar_arquivo_temp(codigo: str):
    # Prefixo para fácil identificação
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="esbmc_codigo_usuario_", suffix=".py")
    tmp.write(codigo.encode("utf-8"))
    tmp.close()
    return tmp.name

def rodar_cmd_esbmc(path_arquivo: str, flags: list, timeout_sec=None):
    # Passa todas as flags diretamente (ESBMC v7.11+ suporta --strict-types nativamente)
    cmd = ["esbmc", path_arquivo] + flags
    try:
        # timeout=None espera o tempo que for necessário
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        return result.stdout + result.stderr, " ".join(cmd)
    except subprocess.TimeoutExpired:
        return "TIMEOUT CRÍTICO", " ".join(cmd)
    except Exception as e:
        return str(e), "Erro"

# ------------------------------------------------------------
# 2. PARSE LÓGICO (Lista Branca + Deduplicação)
# ------------------------------------------------------------
def parse_contraexemplo_detalhado(saida_esbmc: str, codigo_original: str, arquivo_analisado: str):
    # Se deu sucesso, não há contraexemplo
    if "VERIFICATION SUCCESSFUL" in saida_esbmc:
        return [], {}

    passos_brutos = []
    variaveis_memoria = {}
    
    regex_state = re.compile(r"State \d+ (?:file (.*?) line (\d+)|function (.*?) thread|thread \d+)")
    regex_assign = re.compile(r"^\s*([\w:$.\[\]]+)\s*=\s*(.+)$")
    regex_violation = re.compile(r"Violated property|ERROR: Exception thrown")
    regex_clean_var = re.compile(r"^([a-zA-Z_]\w*)(?:\$.+)?$")

    linhas_log = saida_esbmc.split("\n")
    nome_arquivo_base = os.path.basename(arquivo_analisado)
    linhas_cod = codigo_original.split("\n")
    total_linhas = len(linhas_cod)

    # --- 1. ALLOWLIST (Variáveis do Usuário) ---
    vars_usuario = set()
    rgx_attr = re.compile(r"^\s*([a-zA-Z_]\w*)(?:\s*:\s*[\w\[\]]*)?\s*=")
    rgx_def = re.compile(r"def\s+[a-zA-Z_]\w*\s*\(([^)]*)\)")
    rgx_for = re.compile(r"for\s+([a-zA-Z_]\w*)\s+in")

    for l in linhas_cod:
        m = rgx_attr.search(l)
        if m: vars_usuario.add(m.group(1))
        m_for = rgx_for.search(l)
        if m_for: vars_usuario.add(m_for.group(1))
        m_def = rgx_def.search(l)
        if m_def:
            args_str = m_def.group(1)
            args = [a.split(':')[0].strip() for a in args_str.split(',') if a.strip()]
            for arg in args:
                if arg and arg != 'self': vars_usuario.add(arg)

    # --- MAPAS ---
    mapa_vars_linhas = {} 
    for i, l in enumerate(linhas_cod):
        m = rgx_attr.search(l)
        if m: 
            v = m.group(1)
            if v not in mapa_vars_linhas: mapa_vars_linhas[v] = i + 1
        m_for = rgx_for.search(l)
        if m_for:
            v = m_for.group(1)
            if v not in mapa_vars_linhas: mapa_vars_linhas[v] = i + 1

    mapa_args_func = {} 
    for l in linhas_cod:
        m = rgx_def.search(l)
        if m:
            func = l.split('def')[1].split('(')[0].strip()
            args_str = m.group(1)
            args = [a.split(':')[0].strip() for a in args_str.split(',') if a.strip()]
            for arg in args:
                if arg and arg != 'self': mapa_args_func[arg] = func

    mapa_chamadas = {} 
    rgx_call = re.compile(r"\b([a-zA-Z_]\w*)\s*\(")
    for i, l in enumerate(linhas_cod):
        if l.strip().startswith("def "): continue
        matches = rgx_call.finditer(l)
        for m in matches:
            f = m.group(1)
            if f not in mapa_chamadas: mapa_chamadas[f] = []
            mapa_chamadas[f].append(i + 1)

    indice_chamadas = {f: 0 for f in mapa_chamadas}
    args_vistos_chamada_atual = {f: set() for f in mapa_chamadas}
    chamadas_injetadas = set()

    linha_atual = -1
    exibir_passo = False
    erro_na_linha = False
    
    # Controle de múltiplos contraexemplos (Pega apenas o primeiro)
    contador_contraexemplos = 0 

    def salvar():
        if 1 <= linha_atual <= total_linhas:
            # Deduplicação
            if passos_brutos:
                prev = passos_brutos[-1]
                if prev["linha_atual"] == linha_atual and not prev["erro"]:
                    passos_brutos[-1] = {
                        "codigo": codigo_original,
                        "linha_atual": linha_atual,
                        "variaveis": variaveis_memoria.copy(),
                        "erro": erro_na_linha
                    }
                    return

            passos_brutos.append({
                "codigo": codigo_original,
                "linha_atual": linha_atual,
                "variaveis": variaveis_memoria.copy(),
                "erro": erro_na_linha
            })

    for ln in linhas_log:
        ln = ln.strip()

        if "[Counterexample]" in ln:
            contador_contraexemplos += 1
            if contador_contraexemplos > 1: break
            continue

        if regex_violation.search(ln):
            match_l = re.search(r"line (\d+)", ln)
            if match_l:
                nova_linha_erro = int(match_l.group(1))
                if linha_atual != -1 and linha_atual != nova_linha_erro and exibir_passo:
                    salvar()
                linha_atual = nova_linha_erro
                erro_na_linha = True
                exibir_passo = True
            continue

        match_st = regex_state.search(ln)
        if match_st:
            f_name = match_st.group(1)
            l_num = match_st.group(2)
            if f_name and l_num:
                nova_linha = int(l_num)
                eh_user = (os.path.basename(f_name.strip()) == nome_arquivo_base)
                if eh_user:
                    if nova_linha != linha_atual:
                        if linha_atual != -1 and exibir_passo: salvar()
                        linha_atual = nova_linha
                        exibir_passo = True
                        erro_na_linha = False
            continue

        if "=" in ln and "State" not in ln:
            if "==" in ln: continue 

            m_as = regex_assign.match(ln)
            if m_as:
                raw_name = m_as.group(1).strip()
                valor = m_as.group(2).strip().split('(')[0].strip()
                valor = valor.lstrip("= ")
                if valor.replace(" ", "") == "{}": valor = "None"

                clean_match = regex_clean_var.match(raw_name)
                nome = clean_match.group(1) if clean_match else raw_name

                if nome not in vars_usuario: continue

                variaveis_memoria[nome] = valor
                passo_injetado = False

                # Injeção de Chamada
                if nome in mapa_args_func:
                    func_dona = mapa_args_func[nome]
                    if func_dona in mapa_chamadas and func_dona in indice_chamadas:
                        if nome in args_vistos_chamada_atual[func_dona]:
                            indice_chamadas[func_dona] += 1
                            args_vistos_chamada_atual[func_dona] = set()
                        args_vistos_chamada_atual[func_dona].add(nome)
                        
                        idx = indice_chamadas[func_dona]
                        lista = mapa_chamadas[func_dona]
                        if idx < len(lista):
                            l_call = lista[idx]
                            call_id = (func_dona, idx)
                            if l_call != linha_atual and call_id not in chamadas_injetadas:
                                if linha_atual != -1 and exibir_passo: salvar()
                                linha_atual = l_call
                                exibir_passo = True
                                erro_na_linha = False
                                salvar()
                                passo_injetado = True
                                chamadas_injetadas.add(call_id)

                # Inferência
                if not passo_injetado and nome in mapa_vars_linhas:
                    l_mapa = mapa_vars_linhas[nome]
                    deve_inferir = (not exibir_passo) or (l_mapa > linha_atual) or (nome in mapa_args_func)
                    if deve_inferir and l_mapa != linha_atual:
                        if linha_atual != -1 and exibir_passo: salvar()
                        linha_atual = l_mapa
                        exibir_passo = True
                        erro_na_linha = False

    if linha_atual != -1 and exibir_passo:
        salvar()

    return passos_brutos, variaveis_memoria.copy()

# ------------------------------------------------------------
# 3. GERAR PYTEST
# ------------------------------------------------------------
def gerar_pytest(codigo_usuario: str, vars_erro: dict):
    codigo_seguro = codigo_usuario.replace("'''", "\\'\\'\\'")
    return f"""import pytest
import math
import random

VALORES_FALHA = {json.dumps(vars_erro, indent=4)}

def mock_input(prompt=""): return "0"
def mock_assume(cond): 
    if not cond: pytest.skip("Assume ignorado")
def mock_cover(cond): pass

mock_globals = {{
    'math': math, 'pytest': pytest, 'random': random, 'input': mock_input,
    'nondet_int': lambda: random.randint(-100,100),
    'nondet_uint': lambda: random.randint(0,100),
    'nondet_float': lambda: random.uniform(-100,100),
    'nondet_bool': lambda: random.choice([True, False]),
    '__ESBMC_assume': mock_assume, '__ESBMC_cover': mock_cover
}}

def test_reproducao():
    print(f"\\nContexto Falha ESBMC: {{VALORES_FALHA}}")
    try:
        exec('''{codigo_seguro}''', mock_globals)
    except AssertionError as e: pytest.fail(f"Asserção: {{e}}")
    except ZeroDivisionError: pytest.fail("Divisão por zero!")
    except Exception as e: pytest.fail(f"Erro: {{type(e).__name__}}: {{e}}")
"""

# ------------------------------------------------------------
# 4. ROTA PRINCIPAL
# ------------------------------------------------------------
@app.route("/verificar", methods=["POST"])
def verificar():
    data = request.get_json()
    codigo = data.get("codigo", "")
    flags_usuario = data.get("flags", [])

    path_tmp = criar_arquivo_temp(codigo)
    
    # 1. VERIFICAÇÃO PRINCIPAL
    saida_usuario, cmd_usuario_display = rodar_cmd_esbmc(path_tmp, flags_usuario, timeout_sec=None)
    
    # Análise de Resultados
    is_success = "VERIFICATION SUCCESSFUL" in saida_usuario
    has_type_warning = "Type checking warning" in saida_usuario
    
    is_failed = "VERIFICATION FAILED" in saida_usuario or "FAILED:" in saida_usuario
    is_timeout = "TIMEOUT CRÍTICO" in saida_usuario
    is_cmd_error = "ERROR: unrecognised option" in saida_usuario
    
    modo_estrito = "--strict-types" in flags_usuario

    passos_contraexemplo = []
    vars_erro = {}

    # 2. SHADOW RUN
    should_debug = (not is_success) and not is_timeout and not is_cmd_error
    
    if should_debug:
        flags_debug = list(flags_usuario)
        if "--no-slice" not in flags_debug: flags_debug.append("--no-slice")
        if not any("--unwind" in f for f in flags_debug): flags_debug.extend(["--unwind", "32"])
        
        saida_debug, _ = rodar_cmd_esbmc(path_tmp, flags_debug, timeout_sec=None)
        passos_contraexemplo, vars_erro = parse_contraexemplo_detalhado(saida_debug, codigo, path_tmp)

    if os.path.exists(path_tmp): os.remove(path_tmp)

    # 3. STATUS
    status_label = "Sucesso: Código Seguro"
    status_class = "font-bold text-xl text-green-600"
    causa_texto = "Nenhuma falha."

    if is_timeout:
        status_label = "⚠️ Erro: Timeout"
        status_class = "font-bold text-xl text-orange-600"
        causa_texto = "Tempo excedido."
    elif is_cmd_error:
        status_label = "⚠️ Erro de Configuração"
        status_class = "font-bold text-xl text-orange-600"
        match_err = re.search(r"ERROR: (.*)", saida_usuario)
        causa_texto = match_err.group(1) if match_err else "Opção inválida detectada."
    
    elif is_success:
        status_label = "✅ Sucesso / Código Seguro"
        status_class = "font-bold text-xl text-green-600"
        causa_texto = "Nenhuma falha."
    
    else:
        # Falha Real (crash, assert, ou strict type error nativo)
        if modo_estrito and has_type_warning:
            status_label = "❌ Falha Encontrada (Strict Mode)"
            status_class = "font-bold text-xl text-red-600"
            match_warn = re.search(r"error: (.*?) \[", saida_usuario)
            causa_texto = match_warn.group(1) if match_warn else "Type checking warning"
        
        elif has_type_warning and not is_failed:
             # Warning de tipo sem flag strict = Alerta
             status_label = "⚠️ Alerta de Tipagem"
             status_class = "font-bold text-xl text-yellow-600"
             match_warn = re.search(r"error: (.*?) \[", saida_usuario)
             causa_texto = match_warn.group(1) if match_warn else "Type checking warning"
        
        else:
            status_label = "❌ Falha Encontrada"
            status_class = "font-bold text-xl text-red-600"
            causa_texto = "Erro desconhecido"
            
            linhas = saida_usuario.split("\n")
            melhor_causa = ""
            prioridade_causa = 0

            for i, ln in enumerate(linhas):
                if "Violated property" in ln:
                    causa_temp = ""
                    for j in range(1, 4):
                        if i+j < len(linhas):
                            t = linhas[i+j].strip()
                            if t and "-----" not in t and "file" not in t:
                                if t == "assertion": continue
                                t = re.sub(r"\$\w+", "", t)
                                causa_temp = t
                                break
                    if causa_temp:
                        prio_temp = 1
                        if any(c in causa_temp for c in ["division by zero", "overflow", "bounds", "NaN"]): prio_temp = 2
                        if prio_temp > prioridade_causa:
                            prioridade_causa = prio_temp
                            melhor_causa = causa_temp
                            if prio_temp == 2: break 

                elif "Missing return" in ln and prioridade_causa < 2:
                    melhor_causa = "Missing return statement"
                    prioridade_causa = 2
                elif "ERROR: Exception thrown" in ln and prioridade_causa < 2:
                    melhor_causa = ln
                    prioridade_causa = 2

            if melhor_causa:
                if any(op in melhor_causa for op in ["==", "!=", "<", ">", "+", "-", "*", "/"]) and prioridade_causa == 1:
                    causa_texto = f"assert {melhor_causa}"
                else:
                    causa_texto = melhor_causa

    interpretacao_final = f"{status_label}\nCausa: {causa_texto}"

    pytest_code = gerar_pytest(codigo, vars_erro)
    
    pytest_result = "N/A"
    try:
        t_py = tempfile.NamedTemporaryFile(delete=False, suffix=".py")
        t_py.write(pytest_code.encode("utf-8"))
        t_py_path = t_py.name
        t_py.close()
        res_py = subprocess.run(["pytest", t_py_path], capture_output=True, text=True, timeout=10)
        pytest_result = res_py.stdout + res_py.stderr
        os.remove(t_py_path)
    except Exception as e:
        pytest_result = str(e)

    return jsonify({
        "comando": cmd_usuario_display,
        "resultado_bruto": saida_usuario,
        "interpretacao": interpretacao_final,
        "status_label": status_label,
        "status_class": status_class,
        "causa": causa_texto,
        "contraexemplo": passos_contraexemplo,
        "pytest_code": pytest_code,
        "pytest_result": pytest_result
    })

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
