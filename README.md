# Cortes

Painel web local para **encontrar, ajustar e exportar automaticamente os melhores momentos de lives** (YouTube ou arquivo local) em clipes verticais e horizontais — com detecção por picos de áudio, transcrição por IA e renderização via FFmpeg.

> Funciona localmente em `http://127.0.0.1:8765`. YouTube pode bloquear downloads por anti-bot/IP; o app traz fallbacks e fluxo local.

---

## Destaques

- **Análise automática de highlights**: detecta picos de energia no áudio (gritos, risadas, momentos quentes) e sugere trechos candidatos.
- **Transcrição com IA**: usa [faster-whisper](https://github.com/SYSTRAN/faster-whisper) para gerar transcrição/legendas (com cache por vídeo), com escolha de idioma (PT-BR, EN, FR, ES, Auto).
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
2. **Download (opcional)**: baixa o VOD com `yt-dlp` (seleção de qualidade/codec).
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
├─ app.py                  # Backend Flask + UI web (HTML/CSS/JS embutido)
├─ cortar.py               # Download, áudio, transcrição, detecção, legendas, render, WARP/bgutil
├─ Abrir Cortes.vbs        # Launcher Windows (segundo plano + abre navegador)
├─ README.md
├─ requirements.txt
├─ LICENSE
├─ .gitignore
└─ (runtime/local, ignorados)
   ├─ vods/ audio/ clips/ preview/ thumbs/ temp/
   ├─ tools/               # ferramentas locais opcionais (ex.: bgutil provider)
   └─ *.log / estado.json / historico.json / cam.json
```

## Configuração e dados locais

- **Config de câmera/layout**: `cam.json` (posição/tamanho da cam, marca d'água, texto) — gerado/atualizado pelo painel.
- **Estado do último projeto**: `estado.json`
- **Histórico de downloads**: `historico.json`
- **Cache de transcrição**: `audio/<arquivo>.wav.json`
- **Cache de prévias/thumbs**: pastas `preview/` e `thumbs/`

> Esses arquivos/pastas são específicos do ambiente local e, por padrão, ficam fora do versionamento para manter o repositório limpo.

## API local (resumo)

- `GET /api/status` — progresso/estado
- `POST /api/analisar` — inicia análise
- `GET /api/candidatos` — candidatos do projeto
- `POST /api/ajustar` / `POST /api/exportar` — edição/exportação
- `POST /api/baixar` — download/rebaixar VOD (inclui modo só-áudio)
- `GET /api/audio-tracks` — faixas de áudio do vídeo atual
- `GET/POST /api/presets`, `DELETE /api/presets/<nome>` — presets locais
- `GET/POST /api/fila`, `POST /api/fila/limpar` — fila sequencial
- `GET /api/network-status` — status WARP + bgutil
- `POST /api/warp/connect` / `POST /api/warp/disconnect` — controle opcional do Cloudflare WARP
- `POST /api/sair` — encerra o painel
- `GET /api/arquivos` + endpoints de limpeza — gerenciamento de disco
- `GET/POST /api/cam`, `POST /api/detectar-cam` — layout de cam

> API local para uso do próprio painel.

## YouTube / anti-bot / IP

- **Bloqueio "Sign in to confirm you're not a bot"**: normalmente é verificação de PO Token/reputação de IP/sessão do YouTube, não bug do app. O Cortes usa yt-dlp com Node/EJS e client fallback.
- **Cloudflare WARP (opcional)**: se `warp-cli` estiver instalado, o painel mostra um botão **WARP ON/OFF** e a API expõe `/api/network-status`, `/api/warp/connect`, `/api/warp/disconnect`. Conectar o WARP troca o IP de saída e pode destravar downloads; **não é garantido**.
- **bgutil PO Token (opcional, avançado)**: o Cortes detecta automaticamente `bgutil-ytdlp-pot-provider` no mesmo Python e um provider HTTP em `BGUTIL_POT_PROVIDER_URL` (default `http://127.0.0.1:4416`). Pode ajudar, mas **não garante** bypass se o IP estiver flagged. `BGUTIL_POT_DISABLE=1` desabilita.
- **Fallback garantido**: baixe o vídeo por outro método e cole o **caminho do arquivo local** no campo de fonte. A análise, detecção, legendas e exportação funcionam normalmente com arquivo local.
- **Sem cookies no app**: o fluxo/UI de cookies foi removido por decisão do projeto.

## Notas de execução

- **Sem janelas de CMD**: FFmpeg/FFprobe/WARP são chamados sem abrir janelas de console no Windows.
- **Idioma da transcrição**: PT-BR, EN, FR, ES, Auto.
- **Áudio**: download só-áudio (MP3/M4A/Opus/WAV + qualidade), escolha de faixa de áudio no export, modo de áudio do clipe (original, normalizar, ganho dB, sem áudio).
- **Presets**: salve/carregue presets de análise/exportação no painel (`presets.json` local).
- **Fila**: enfileire análises/exportações para rodar em sequência (`/api/fila`).
- **Retomar projeto**: o último projeto com candidatos é salvo em `estado.json` e pode ser reaberto sem reanalisar.
- **Legendas separadas**: ao exportar, o Cortes também gera `.srt` e `.ass` por trecho (além do MP4 com legenda queimada, se habilitada).

## Troubleshooting

- **yt-dlp desatualizado**: `python -m pip install -U yt-dlp`.
- **FFmpeg não encontrado**: verifique `ffmpeg`/`ffprobe` no PATH ou ajuste `_FFMPEG_DIR` em `cortar.py`.
- **Transcrição lenta na CPU**: use modelo menor (`tiny`/`base`) ou configure CUDA/cuDNN compatíveis.
- **Sem candidatos encontrados**: ajuste a sensibilidade ou parâmetros de detecção/corte.
- **Arquivos grandes**: use a aba/modal **Arquivos** para limpar VODs/clipes/previews.

## Roadmap (ideias)

- [ ] Separar frontend (templates/static) para facilitar manutenção
- [ ] Configuração centralizada (`.env` / arquivo de config)
- [ ] Testes automatizados para partes do pipeline
- [ ] Suporte multiplataforma mais completo (Linux/macOS)
- [ ] Empacotamento/distribuição simplificada

## Licença

MIT. Veja `LICENSE`.

## Aviso

Use este projeto apenas com conteúdo que você tem direito de baixar, processar e publicar, respeitando os Termos de Serviço das plataformas e direitos autorais aplicáveis.
