# Cortes

Painel web local para **encontrar, ajustar e exportar automaticamente os melhores momentos de lives** (YouTube ou arquivo local) em clipes verticais e horizontais — com detecção por picos de áudio, transcrição por IA e renderização via FFmpeg.

> Pensado para criadores que querem transformar VODs longos em Shorts/Reels/TikTok sem editar tudo manualmente.

---

## Destaques

- **Análise automática de highlights**: detecta picos de energia no áudio (gritos, risadas, momentos quentes) e sugere trechos candidatos.
- **Transcrição com IA**: usa [faster-whisper](https://github.com/SYSTRAN/faster-whisper) para gerar transcrição/legendas (com cache por vídeo).
- **Editor de cortes no navegador**: ajuste início/fim, duração alvo, nudges finos, prévia por trecho e salvamento individual ou em lote.
- **Exportação multi-formato**: `9:16`, `16:9`, `4:5`, `1:1` em um único fluxo.
- **Modos de enquadramento**: fundo desfocado, corte central, barras pretas e **layout "cam em cima"** para shorts (com posicionamento arrastável, detecção automática e marca d'água).
- **Legendas queimadas**: geração de ASS e renderização direto no vídeo.
- **Gerenciamento de arquivos**: visualização de uso em disco, reaproveitamento de vídeos já baixados, limpeza de sobras e exclusão segura de artefatos.
- **Painel local**: roda em `http://127.0.0.1:8765`, sem dependência de serviços externos além do download opcional via YouTube.

## Capturas

> Adicione aqui screenshots/GIFs do painel para o README público (ex.: `docs/screenshot-home.png`, `docs/screenshot-editor.png`, `docs/screenshot-export.png`).

## Como funciona (pipeline)

1. **Fonte**: informa um link do YouTube ou caminho local.
2. **Download (opcional)**: baixa o VOD com `yt-dlp` (suporte a cookies, seleção de qualidade/codec).
3. **Áudio**: extrai áudio WAV 16 kHz mono via FFmpeg.
4. **Transcrição**: gera segmentos/palavras com `faster-whisper` (cache em `.wav.json`).
5. **Detecção de picos**: calcula energia RMS por janela, normaliza por mediana local e seleciona candidatos.
6. **Montagem de cortes**: ajusta janelas de corte com base na transcrição e duração alvo.
7. **Edição**: usuário revisa/ajusta no painel (prévia, duração, nudge, seleção).
8. **Exportação**: renderiza clipes finais com FFmpeg (filtros de enquadramento, legenda, marca d'água, CRF/preset).

## Requisitos

### Sistema
- **Windows** (testado; usa `explorer.exe` e caminhos padrão do projeto)
- **Python 3.10+** (recomendado 3.11/3.12)
- **FFmpeg + FFprobe** disponíveis no sistema (ou configurados no código)
- **GPU NVIDIA + CUDA/cuDNN** (opcional, recomendado para transcrição mais rápida com `faster-whisper`)

### Python (principais dependências)
- `flask`
- `yt-dlp`
- `faster-whisper`
- `numpy`

> Observação: `faster-whisper` pode baixar modelos na primeira execução. Para aceleração em GPU, instale os pacotes CUDA/cuDNN compatíveis com seu ambiente.

## Instalação

```bash
# 1) Clone o repositório
git clone https://github.com/JHONSMAR/cortes-panel.git
cd cortes-panel

# 2) (Recomendado) crie um ambiente virtual
python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# 3) Instale dependências Python
pip install -r requirements.txt
```

### FFmpeg
Certifique-se de que `ffmpeg` e `ffprobe` estejam acessíveis. O projeto também suporta configuração de caminho fixo para FFmpeg (ver `cortar.py`).

## Uso

### Painel web (recomendado)

```bash
python app.py
```

- Abre automaticamente em: `http://127.0.0.1:8765`
- Para iniciar sem abrir navegador:

```bash
python app.py --no-browser
```

No Windows, você também pode usar o launcher `Abrir Cortes.vbs` (inicia o servidor em segundo plano, sem janela de console, e abre o painel no navegador). Se já estiver rodando, ele apenas abre a página.

### CLI (pipeline direto)

O módulo `cortar.py` também pode ser usado via linha de comando para gerar cortes sem o painel:

```bash
# A partir de URL do YouTube
python cortar.py "https://www.youtube.com/watch?v=..." --clips 10 --formato both --modo blur

# A partir de arquivo local
python cortar.py "E:\CORTES\vods\minha-live.mp4" --clips 5 --sem-legenda
```

> Execute `python cortar.py --help` para ver todas as opções disponíveis (modelo, idioma, qualidade, codec, duração, CRF, preset, etc.).

## Estrutura do projeto

```text
.
├─ app.py                  # Backend Flask + UI web (HTML/CSS/JS embutido) e orquestração do pipeline
├─ cortar.py               # Núcleo de download, áudio, transcrição, detecção, legendas e renderização
├─ Abrir Cortes.vbs        # Launcher do painel no Windows (inicia em segundo plano e abre o navegador)
├─ CANAL_VICEMSHORTS.md    # Notas/checklist específicas de canal (exemplo)
├─ requirements.txt        # Dependências Python
├─ .gitignore
└─ (gerados em runtime, ignorados no git)
   ├─ vods/                # Vídeos baixados/originais
   ├─ audio/               # Áudio extraído e transcrições (.wav/.json)
   ├─ clips/               # Clipes exportados
   ├─ preview/             # Prévias geradas pelo painel
   ├─ thumbs/              # Thumbnails dos candidatos
   └─ temp/                # Arquivos temporários
```

## Configuração e dados locais

- **Config de câmera/layout**: `cam.json` (posição/tamanho da cam, marca d'água, texto) — gerado/atualizado pelo painel.
- **Estado do último projeto**: `estado.json`
- **Histórico de downloads**: `historico.json`
- **Cache de transcrição**: `audio/<arquivo>.wav.json`
- **Cache de prévias/thumbs**: pastas `preview/` e `thumbs/`

> Esses arquivos/pastas são específicos do ambiente local e, por padrão, ficam fora do versionamento para manter o repositório limpo.

## API (painel)

O frontend se comunica com o backend Flask via endpoints locais (exemplos):

- `GET /api/status` — status/progresso da tarefa atual
- `POST /api/analisar` — inicia análise de uma fonte
- `GET /api/candidatos` — lista candidatos do projeto atual
- `POST /api/ajustar` — ajusta tempo/duração de um corte
- `POST /api/exportar` — renderiza/exporta clipes selecionados
- `POST /api/baixar` — baixa/rebaixa VOD
- `GET /api/arquivos` — inventário de arquivos e uso de disco
- `POST /api/apagar-item` / `POST /api/apagar-vod` / `POST /api/limpar-tudo` — limpeza/gerenciamento
- `GET/POST /api/cam`, `POST /api/detectar-cam` — layout de câmera para shorts

> A API foi projetada para uso local pelo próprio painel.

## Dicas e troubleshooting

- **YouTube pedindo login/captcha**: forneça um `cookies.txt` no campo opcional do painel (ou via CLI, se suportado) para autenticar o download.
- **Transcrição lenta na CPU**: use modelo menor (`tiny`/`base`) ou configure CUDA para aceleração.
- **FFmpeg não encontrado**: verifique instalação/PATH e a configuração de diretório do FFmpeg em `cortar.py`.
- **Sem candidatos encontrados**: ajuste a sensibilidade no painel ou os parâmetros de detecção/corte.
- **Arquivos grandes**: VODs, áudios e clipes ocupam bastante espaço; use a aba/modal de **Arquivos** para gerenciar e limpar.

## Roadmap (ideias)

- [ ] Separar frontend (templates/static) para facilitar manutenção
- [ ] Configuração centralizada (`.env` / arquivo de config)
- [ ] Testes automatizados para partes do pipeline
- [ ] Suporte multiplataforma mais completo (Linux/macOS)
- [ ] Empacotamento/distribuição simplificada

## Licença

Defina a licença do projeto antes de torná-lo público (ex.: MIT). Veja o arquivo `LICENSE` se presente.

## Aviso

Use este projeto apenas com conteúdo que você tem direito de baixar, processar e publicar, respeitando os Termos de Serviço das plataformas e direitos autorais aplicáveis.
