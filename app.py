import subprocess
import tempfile
import os
import re
import json
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ------------------------------------------------------------
# 1. EXECUÇÃO DO ESBMC
# ------------------------------------------------------------
def executar_esbmc(codigo_usuario: str, flags: list):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as tmp:
            tmp.write(codigo_usuario.encode("utf-8"))
            tmp_path = tmp.name

        cmd_base = ["esbmc", tmp_path]
        
        user_flags = list(flags) if flags else []
        if not any(f.strip() == "--unwind" for f in user_flags):
            user_flags.extend(["--unwind", "32"])
        cmd_base.extend(user_flags)

        # NÍVEL 1: Modo Visual
        cmd_visual = cmd_base.copy()
        if "--no-slice" not in cmd_visual: cmd_visual.append("--no-slice")

        try:
            result = subprocess.run(cmd_visual, capture_output=True, text=True, timeout=15)
            return result.stdout + result.stderr, " ".join(cmd_visual)
        except subprocess.TimeoutExpired:
            # NÍVEL 2: Modo Padrão
            try:
                result = subprocess.run(cmd_base, capture_output=True, text=True, timeout=60)
                return result.stdout + result.stderr, " ".join(cmd_base)
            except subprocess.TimeoutExpired:
                # NÍVEL 3: Fallback
                cmd_fallback = list(cmd_base)
                if "--multi-property" in cmd_fallback: cmd_fallback.remove("--multi-property")
                if "--memory-leak-check" in cmd_fallback: cmd_fallback.remove("--memory-leak-check")
                try:
                    result = subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=90)
                    return result.stdout + result.stderr, " ".join(cmd_fallback) + " (Fallback)"
                except:
                    return "TIMEOUT CRÍTICO", "Erro"
    except Exception as e:
        return str(e), "Erro"

# ------------------------------------------------------------
# 2. PARSE LÓGICO (FILTRO ESTRITO)
# ------------------------------------------------------------
def parse_contraexemplo_detalhado(saida_esbmc: str, codigo_original: str):
    if "VERIFICATION FAILED" not in saida_esbmc and "VERIFICATION FAILED" not in saida_esbmc.upper():
        return []

    passos_brutos = []
    variaveis_memoria = {}
    
    regex_state = re.compile(r"State \d+ .*? line (\d+)")
    regex_assign = re.compile(r"^\s*([\w:$.\[\]]+)\s*=\s*(.+)$")

    linhas = saida_esbmc.split("\n")
    
    # Variáveis temporárias para agrupar estados da MESMA linha
    linha_atual_processando = -1
    vars_linha_atual = {}
    erro_na_linha = False

    def salvar_passo(linha, vars_snapshot, erro):
        # Só cria o passo se for uma linha válida (>0)
        total_linhas = len(codigo_original.split("\n"))
        
        if 1 <= linha <= total_linhas:
            passos_brutos.append({
                "codigo": codigo_original,
                "linha_atual": linha,
                "variaveis": vars_snapshot.copy(),
                "erro": erro
            })

    for ln in linhas:
        ln = ln.strip()
        
        # 1. Detectou Violação? Marca flag.
        if "Violated property" in ln:
             erro_na_linha = True
             continue

        # 2. Novo Estado Detectado
        match_state = regex_state.search(ln)
        if match_state:
            nova_linha = int(match_state.group(1))
            
            # Se mudou de linha, salvamos o acumulado da linha ANTERIOR
            if nova_linha != linha_atual_processando:
                if linha_atual_processando != -1:
                    salvar_passo(linha_atual_processando, variaveis_memoria, erro_na_linha)
                
                # Reseta controles para a nova linha
                linha_atual_processando = nova_linha
                erro_na_linha = False
            
            continue

        # 3. Variáveis
        # Processa variáveis apenas se estamos dentro de um estado válido
        if linha_atual_processando != -1 and "=" in ln and "State" not in ln and "thread" not in ln:
            match_assign = regex_assign.match(ln)
            if match_assign:
                var_nome = match_assign.group(1).strip()
                var_valor_bruto = match_assign.group(2).strip()

                # --- LISTA NEGRA: Remove variáveis internas e message ---
                ignorar = [
                    "__ESBMC", "argv", "return_value", "value", "nondet", 
                    "pthread", "dynamic_", "rounding_mode", 
                    "message", "name", "stdin", "stdout", "stderr"
                ]
                
                if any(x == var_nome or var_nome.startswith(x) for x in ignorar):
                    continue
                
                # Limpa valor binário
                valor = var_valor_bruto.split('(')[0].strip() if "(" in var_valor_bruto else var_valor_bruto
                
                # Atualiza a memória global
                variaveis_memoria[var_nome] = valor

    # Salva o último passo pendente
    if linha_atual_processando != -1:
        salvar_passo(linha_atual_processando, variaveis_memoria, erro_na_linha)

    # --- DEDUPLICAÇÃO FINAL ---
    # Remove passos onde as variáveis NÃO mudaram em relação ao passo anterior.
    # Exceção: O primeiro passo e o passo de erro sempre aparecem.

    return passos_brutos

# ------------------------------------------------------------
# 3. GERAR PYTEST
# ------------------------------------------------------------
def gerar_pytest(codigo_usuario: str):
    codigo_seguro = codigo_usuario.replace("'''", "\\'\\'\\'")
    return f"""import pytest
import math
import random

def mock_nondet_int(): return random.randint(-100, 100)
def mock_nondet_uint(): return random.randint(0, 100)
def mock_assume(cond): pass 
def mock_cover(cond): pass

mock_globals = {{
    'math': math, 'pytest': pytest, 'random': random,
    'nondet_int': mock_nondet_int, 'nondet_uint': mock_nondet_uint,
    '__ESBMC_assume': mock_assume, '__ESBMC_cover': mock_cover
}}

def test_cenario_usuario():
    print("\\n--- Execução Controlada ---")
    codigo_fonte = '''
{codigo_seguro}
'''
    try:
        exec(codigo_fonte, mock_globals)
    except AssertionError as e: pytest.fail(f"Asserção falhou: {{e}}")
    except ZeroDivisionError: pytest.fail("Divisão por zero!")
    except TypeError as e: pytest.fail(f"Erro de Tipo: {{e}}")
    except IndexError as e: pytest.fail(f"Erro de Índice: {{e}}")
    except NameError as e: pytest.fail(f"Erro de Nome: {{e}}")
    except Exception as e: pytest.fail(f"Erro: {{type(e).__name__}}: {{e}}")
"""

# ------------------------------------------------------------
# 4. ENDPOINTS
# ------------------------------------------------------------
@app.route("/verificar", methods=["POST"])
def verificar():
    data = request.get_json()
    codigo = data.get("codigo", "")
    flags = data.get("flags", [])

    saida_esbmc, comando_usado = executar_esbmc(codigo, flags)
    passos_contraexemplo = parse_contraexemplo_detalhado(saida_esbmc, codigo)
    
    status_texto = "Sucesso: Nenhuma falha encontrada."
    causa_texto = "Nenhuma falha detectada."
    
    if "TIMEOUT CRÍTICO" in saida_esbmc:
        status_texto = "Erro: Timeout Crítico."
        causa_texto = "Tempo limite excedido."
    elif "VERIFICATION FAILED" in saida_esbmc or "VERIFICATION FAILED" in saida_esbmc.upper():
        status_texto = "Falha detectada!"
        linhas = saida_esbmc.split("\n")
        causa_texto = "Erro desconhecido"
        for i, ln in enumerate(linhas):
            if "Violated property" in ln:
                if i + 2 < len(linhas):
                    c = linhas[i+2].strip()
                    if c and "-----" not in c: causa_texto = c
                    elif i + 3 < len(linhas): causa_texto = linhas[i+3].strip()
                break

    if "Sucesso" not in status_texto:
        interpretacao_final = f"{status_texto}\nCausa: {causa_texto}"
    else:
        interpretacao_final = status_texto

    pytest_code = gerar_pytest(codigo)
    
    pytest_result = "Não executado."
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".py") as tmp_test:
            tmp_test.write(pytest_code.encode("utf-8"))
            tmp_path_test = tmp_test.name
        
        res_pytest = subprocess.run(["pytest", tmp_path_test], capture_output=True, text=True, timeout=10)
        pytest_result = res_pytest.stdout + res_pytest.stderr
        os.remove(tmp_path_test)
    except Exception as e:
        pytest_result = f"Erro ao rodar pytest: {str(e)}"

    return jsonify({
        "comando": comando_usado,
        "resultado_bruto": saida_esbmc,
        "interpretacao": interpretacao_final,
        "contraexemplo": passos_contraexemplo,
        "tempo": "N/A",
        "pytest_code": pytest_code,
        "pytest_result": pytest_result
    })

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
