# Painel da Esteira — vilardani/controleproducao

Painel de produção de conteúdo/vídeo EAD (Afya), publicado como página estática
no GitHub Pages, com os dados atualizados automaticamente a partir do Trello
via GitHub Actions.

Como funciona:

- `index.html` — o painel em si (HTML/CSS/JS, sem build step). Ele busca
  `./data.json` ao carregar e renderiza todas as abas a partir dele.
- `data.json` — o "banco de dados" do painel. Hoje contém dois cards de
  exemplo só para o painel não quebrar antes da primeira execução real.
- `scripts/build_data.py` — script Python que fala com a API do Trello e
  regenera `data.json`.
- `.github/workflows/update-data.yml` — roda `build_data.py` a cada 6 horas
  (e também pode ser disparado manualmente) e commita o `data.json` atualizado.

## Passo a passo para colocar no ar

### 1. Subir estes arquivos para o repositório

Como ainda não tenho como fazer `git push` diretamente neste repositório
(nem esta sessão nem o computador vinculado a ela têm as suas credenciais do
GitHub), o caminho mais simples é pelo próprio site do GitHub:

1. Abra `https://github.com/vilardani/controleproducao`.
2. Clique em **Add file → Upload files**.
3. Arraste a pasta inteira que eu te enviei (com `index.html`, `data.json`,
   `README.md` e as subpastas `scripts/` e `.github/workflows/`) — navegadores
   modernos (Chrome/Edge) preservam a estrutura de pastas no arrasta-e-solta.
4. Confirme o commit direto na branch principal (`main`).

Se preferir usar Git na sua máquina em vez do upload pelo navegador, os
comandos são os de sempre (`git init` / `git remote add origin ...` / `git
add .` / `git commit` / `git push`) — me avise se quiser que eu escreva o
passo a passo exato para o seu caso.

### 2. Criar as credenciais do Trello

1. Acesse https://trello.com/power-ups/admin (ou https://trello.com/app-key)
   logada com a conta que tem acesso ao board "Esteira Produção Disciplinas".
2. Copie a **Key**.
3. Na mesma página, gere um **Token** (permissão de leitura já é suficiente —
   não precisa de escrita, já que só vamos ler o board).
4. Pegue o **ID do board**: abra o board no navegador, adicione `.json` ao
   final da URL (ou use `https://trello.com/b/<shortId>/algum-nome`), o campo
   `id` no JSON retornado é o Board ID. Alternativa mais simples: no board,
   menu **⋯ → Mais → Imprimir e Exportar → Exportar como JSON** e pegue o
   campo `"id"` no topo do arquivo.

### 3. Cadastrar os Secrets no repositório

Em `Settings → Secrets and variables → Actions → New repository secret`,
crie três secrets:

- `TRELLO_KEY`
- `TRELLO_TOKEN`
- `TRELLO_BOARD_ID`

Esses valores nunca aparecem no código nem no painel público — ficam só
disponíveis dentro da execução do workflow.

### 4. Permitir que o Actions escreva no repositório

Em `Settings → Actions → General → Workflow permissions`, marque
**"Read and write permissions"** e salve. Sem isso, o passo final do
workflow (`git push`) falha por falta de permissão.

### 5. Ativar o GitHub Pages

Em `Settings → Pages`:
- **Source**: Deploy from a branch
- **Branch**: `main` / pasta `/ (root)`

Depois de salvar, o GitHub mostra a URL pública (algo como
`https://vilardani.github.io/controleproducao/`).

### 6. Rodar o workflow pela primeira vez

Em `Actions → Atualizar dados do Trello → Run workflow` (botão
"Run workflow" no canto direito). Acompanhe a execução — se dor tudo certo,
`data.json` é atualizado e commitado automaticamente com os dados reais do
board. Depois disso ele roda sozinho a cada 6 horas (ajustável no `cron` do
arquivo `.github/workflows/update-data.yml`).

## Ponto que precisa da sua validação

O painel antigo calculava "Responsável por etapa" (aba Produtividade/Funções)
a partir de um campo próprio do board — não tive acesso a ele para confirmar
exatamente como esse campo é preenchido. `build_data.py` assume, por ora, que
o responsável por uma etapa é **quem moveu o card para fora daquela lista**
no Trello (isso é sempre possível reconstruir puramente pelo histórico de
ações, sem depender de nenhum recurso pago do Trello). Depois da primeira
execução real, dá uma olhada na aba **Produtividade** e me diz se os números
batem com o que você espera. Se o board tiver Custom Fields dedicados para
"Responsável"/"Data" por etapa, eu ajusto o script para ler de lá em vez do
histórico de movimentação — é uma mudança pequena e localizada, só preciso
saber o nome exato desses campos no board.

Da mesma forma, o campo `tipo_conteudista` (interno/externo, usado na aba
Vídeo) ficou como `null` por enquanto — me diga se isso está marcado como
label, campo customizado ou em outro lugar do card, que eu conecto certinho.
