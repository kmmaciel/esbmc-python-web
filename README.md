🔍 ESBMC-Python Web Checker

Este projeto é uma interface web para verificar a segurança e correção de programas Python utilizando o ESBMC (Efficient SMT-Based Context-Bounded Model Checker).

A ferramenta permite escrever código Python, configurar flags de verificação e visualizar contraexemplos passo a passo, além de gerar automaticamente testes de regressão (pytest) para reproduzir as falhas encontradas.

✨ Funcionalidades

Verificação Formal: Detecta divisão por zero, acesso fora dos limites de lista, overflows e vazamentos de memória.

Strict Type Checking: Suporte nativo para detecção de erros de tipagem estática (com a flag --strict-types).

Contraexemplo Visual: Debugger interativo que mostra a execução passo a passo até a falha, exibindo valores de variáveis.

Geração de Testes: Cria automaticamente um arquivo pytest com os valores exatos que causaram a falha.

Propriedades Customizadas: Suporte para assert, __ESBMC_assume e __ESBMC_cover (alcançabilidade).

🚀 Pré-requisitos

Este projeto foi desenvolvido para rodar em ambiente Linux (ou WSL no Windows), pois depende da compilação do ESBMC a partir do código-fonte.

1. Instalar Dependências do Sistema

sudo apt-get update
sudo apt-get install -y build-essential cmake ninja-build python3 python3-dev \
    python3-pip clang libclang-dev llvm-dev libgmp-dev flex bison gperf \
    git curl unzip wget libz3-dev libboost-all-dev libxml2-dev


2. Instalar Dependências Python

Recomenda-se usar um ambiente virtual (venv):

python3 -m venv venv
source venv/bin/activate
pip install flask pytest


🛠️ Instalação do ESBMC (Essencial)

Para utilizar funcionalidades recentes como --strict-types e suporte completo a Python, é necessário compilar o ESBMC a partir da branch main.

Crie um script chamado install_esbmc.sh na raiz do projeto:

#!/bin/bash
set -e
cd ~
echo "🛠️  Compilando ESBMC (Git Main)..."

# Remove versões antigas
sudo rm -f /usr/bin/esbmc
rm -rf esbmc_build

# Clona e Compila
git clone --depth 1 [https://github.com/esbmc/esbmc.git](https://github.com/esbmc/esbmc.git) esbmc_build
cd esbmc_build
mkdir build && cd build
cmake .. -GNinja -DCMAKE_BUILD_TYPE=Release -DENABLE_Regression=OFF \
      -DBUILD_TESTING=OFF -DENABLE_PYTHON_FRONTEND=ON \
      -DENABLE_Z3=ON -DENABLE_BOOLECTOR=ON \
      -DClang_DIR=$(find /usr/lib -name "ClangConfig.cmake" | head -n 1 | xargs dirname)
ninja esbmc

# Instala
sudo mv src/esbmc/esbmc /usr/bin/esbmc
echo "✅ ESBMC Instalado com sucesso!"
esbmc --version


Dê permissão e execute:

chmod +x install_esbmc.sh
./install_esbmc.sh


▶️ Como Rodar

Certifique-se de que o arquivo app.py e a pasta templates/index.html estão no lugar correto.

Inicie o servidor Flask:

python3 app.py


Acesse no navegador: http://localhost:5000

📖 Exemplos de Uso

1. Divisão por Zero (Básico)

def divisao(a, b):
    return a / b

# ESBMC vai encontrar um caso onde b=0
x = nondet_int()
y = nondet_int()
divisao(x, y)


2. Tipagem Estrita (Marque a flag --strict-types)

def soma(a: int, b: int) -> int:
    return a + b

# Isso gera um erro de tipo, pois "10" é string
soma(5, "10")


3. Alcançabilidade (Cover)

Verifica se é possível chegar a um determinado estado.

x = nondet_int()
if x > 100:
    # Se aparecer "Falha Encontrada", significa que esta linha é alcançável (Sucesso do teste)
    __ESBMC_cover(x > 100)


🧩 Estrutura do Projeto

app.py: Backend Flask. Gerencia a execução do binário esbmc, faz o parsing dos logs (regex) e gera o código pytest.

templates/index.html: Frontend. Interface para escrita de código e visualização dos contraexemplos.

install_esbmc.sh: Script auxiliar para compilar o verificador.

⚠️ Resolução de Problemas

Erro "No solver backends built": Significa que o ESBMC foi compilado sem Z3. Rode o script de instalação novamente.

Erro "unrecognised option '--strict-types'": Sua versão do ESBMC é antiga. Use o script de instalação para atualizar para a versão Nightly/Main.

Timeout: Para códigos muito complexos ou com loops grandes, aumente o --unwind ou simplifique o código. A interface web aguarda indefinidamente, mas o navegador pode desconectar.

📄 Licença

Este projeto é uma interface para o ESBMC. Consulte a licença do ESBMC para detalhes sobre o uso do verificador.
