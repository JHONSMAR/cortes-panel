# -*- coding: utf-8 -*-
"""
cortar.py - gera cortes automaticos de lives do YouTube.

O que faz:
  1. baixa o VOD (ou usa um arquivo local)
  2. extrai o audio e transcreve com faster-whisper
  3. acha os picos (grito, risada, chat pegando fogo) por energia de audio
  4. corta os melhores trechos e exporta em 9:16 e/ou 16:9
  5. queima legenda grande no estilo Shorts/TikTok

Exemplos:
  python cortar.py "https://youtu.be/XXXX" --clips 10
  python cortar.py "https://youtu.be/XXXX" --formato both --modo blur
  python cortar.py E:\\CORTES\\vods\\live.mp4 --clips 5 --sem-legenda
"""

import argparse
import os
import re
import subprocess
import sys
import textwrap
import wave

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = r"E:\CORTES"

# O ffmpeg fica fora do PATH em processos iniciados em background.
# Resolve o caminho na marra e injeta no PATH deste processo.
_FFMPEG_DIR = r"C:\Users\mailo\tools\ffmpeg\bin"
if os.path.isdir(_FFMPEG_DIR):
    os.environ["PATH"] = _FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
FFMPEG = os.path.join(_FFMPEG_DIR, "ffmpeg.exe") if os.path.isdir(_FFMPEG_DIR) else "ffmpeg"
FFPROBE = os.path.join(_FFMPEG_DIR, "ffprobe.exe") if os.path.isdir(_FFMPEG_DIR) else "ffprobe"

# DLLs de CUDA/cuDNN instaladas via pip (nvidia-*-cu12). Sem isso o
# ctranslate2 nao acha cublas64_12.dll e o fallback pra CPU e 4x mais lento.
def _registrar_cuda():
    import glob
    import site
    bases = list(site.getsitepackages()) + [site.getusersitepackages()]
    for base in bases:
        for d in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except (AttributeError, OSError):
                pass


_registrar_cuda()

VODS = os.path.join(BASE, "vods")
AUDIO = os.path.join(BASE, "audio")
CLIPS = os.path.join(BASE, "clips")
TEMP = os.path.join(BASE, "temp")

# controlado por --crf / --preset na linha de comando
CRF = 20
PRESET = "veryfast"


# ------------------------------------------------------------------ utilidades

def log(msg):
    print(msg, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def ffmpeg():
    return FFMPEG


def ffprobe_dims(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def ffprobe_dur(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def slug(texto, limite=40):
    texto = re.sub(r"[^\w\s-]", "", texto, flags=re.UNICODE).strip()
    texto = re.sub(r"\s+", "-", texto)
    return texto[:limite] or "corte"


def hhmmss(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    return f"{h:02d}h{m:02d}m{s:02d}s"


# ------------------------------------------------------------------- download

def baixar(url, qual=1080, codec="avc", nome=None, cookies_file=None):
    import yt_dlp

    os.makedirs(VODS, exist_ok=True)
    log(f"[1/5] Baixando: {url}")

    modelo_nome = (nome if nome else "%(title).80s") + ".%(ext)s"


    # protocol^=http exclui os streams m3u8 "Untested", que costumam ter
    # bitrate inflado (18 GB em vez de 11 GB no mesmo 1080p60).
    if codec == "vp9":
        fmt = (f"bv*[height<={qual}][protocol^=http][vcodec^=vp9]"
               f"+ba[protocol^=http]/"
               f"bv*[height<={qual}][protocol^=http]+ba[protocol^=http]/b")
        container = "mkv"
    else:
        fmt = (f"bv*[height<={qual}][protocol^=http][vcodec^=avc1]"
               f"+ba[protocol^=http]/"
               f"bv*[height<={qual}][protocol^=http]+ba[protocol^=http]/b")
        container = "mp4"

    def hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "?").strip()
            vel = d.get("_speed_str", "?").strip()
            eta = d.get("_eta_str", "?").strip()
            print(f"\r      {pct}  {vel}  ETA {eta}   ", end="", flush=True)
        elif d.get("status") == "finished":
            print()

    opcoes_base = {
        "format": fmt,
        "outtmpl": os.path.join(VODS, modelo_nome),
        "merge_output_format": container,
        "ffmpeg_location": _FFMPEG_DIR,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 5,
    }
    if cookies_file:
        opcoes_base["cookiefile"] = cookies_file

    def _tentar(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = info["requested_downloads"][0]["filepath"]
        return path, info

    def _browser_cookie_paths():
        env = os.environ
        local = env.get("LOCALAPPDATA", "")
        roaming = env.get("APPDATA", "")
        return {
            "edge": [os.path.join(local, "Microsoft", "Edge", "User Data", "Default", "Network", "Cookies"),
                     os.path.join(local, "Microsoft", "Edge", "User Data", "Default", "Cookies")],
            "chrome": [os.path.join(local, "Google", "Chrome", "User Data", "Default", "Network", "Cookies"),
                       os.path.join(local, "Google", "Chrome", "User Data", "Default", "Cookies")],
            "chromium": [os.path.join(local, "Chromium", "User Data", "Default", "Network", "Cookies")],
            "firefox": [os.path.join(roaming, "Mozilla", "Firefox", "Profiles")],
            "brave": [os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data", "Default", "Network", "Cookies")],
            "opera": [os.path.join(roaming, "Opera Software", "Opera Stable", "Network", "Cookies")],
            "vivaldi": [os.path.join(local, "Vivaldi", "User Data", "Default", "Network", "Cookies")],
        }

    def _browser_presente(browser, paths):
        for p in paths.get(browser, []):
            if os.path.exists(p):
                return True
        return False

    try:
        path, info = _tentar(opcoes_base)
    except Exception as e:
        msg = str(e).lower()
        precisa_cookies = any(s in msg for s in (
            "sign in to confirm", "not a bot", "confirm you",
            "use --cookies", "cookies-from-browser", "authentication required",
        ))
        if not precisa_cookies:
            raise

        # 1) Fallback sem cookies: alguns clients evitam o bloqueio anti-bot.
        for clientes in (["mweb"], ["ios", "web"], ["android", "web"]):
            try:
                log(f"      YouTube bloqueou: tentando client alternativo ({','.join(clientes)})...")
                opts = dict(opcoes_base)
                ea = dict(opts.get("extractor_args") or {})
                yt = dict(ea.get("youtube") or {})
                yt["player_client"] = list(clientes)
                ea["youtube"] = yt
                opts["extractor_args"] = ea
                path, info = _tentar(opts)
                log(f"      ok com client alternativo ({','.join(clientes)})")
                break
            except Exception:
                continue
        else:
            # 2) Fallback com cookies do navegador (apenas navegadores detectados).
            erros_browser = []
            paths = _browser_cookie_paths()
            browsers = [b for b in ("edge", "chrome", "chromium", "firefox", "brave", "opera", "vivaldi")
                        if _browser_presente(b, paths)]
            if cookies_file and os.path.exists(cookies_file):
                browsers = []  # cookiefile já foi tentado; evita repetir ruído

            ok = False
            for browser in browsers:
                try:
                    log(f"      YouTube bloqueou: tentando cookies do {browser}...")
                    opts = dict(opcoes_base)
                    opts.pop("cookiefile", None)
                    opts["cookiesfrombrowser"] = (browser,)
                    path, info = _tentar(opts)
                    log(f"      ok com cookies do {browser}")
                    ok = True
                    break
                except Exception as eb:
                    erros_browser.append(f"{browser}: {str(eb).splitlines()[0][:160]}")
                    continue

            if not ok:
                if not browsers and not cookies_file:
                    dica_nav = "  - Nenhum navegador suportado com cookies detectado automaticamente."
                else:
                    dica_nav = "\n".join(f"  - {x}" for x in erros_browser[-6:]) if erros_browser else "  - Falha ao ler cookies do navegador."

                raise RuntimeError(
                    "O YouTube exigiu verificação anti-bot e o download não pôde ser autenticado automaticamente.\n"
                    "Como resolver:\n"
                    "  1) Exporte os cookies do YouTube para um arquivo cookies.txt e informe no campo 'cookies' do painel.\n"
                    "     Guia: https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp\n"
                    "  2) Se for usar cookies do navegador, feche o navegador completamente e tente novamente.\n"
                    "  3) Atualize o yt-dlp: pip install -U yt-dlp\n\n"
                    f"Tentativas automáticas:\n{dica_nav}\n\n"
                    f"Erro original: {e}"
                ) from e

    log(f"      salvo em: {path}")
    return path, info


# ----------------------------------------------------------------------- audio

def extrair_audio(src, wav_path):
    log("[2/5] Extraindo audio")
    run([FFMPEG, "-y", "-v", "error", "-i", src,
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav_path])


def ler_wav(wav_path):
    import numpy as np
    with wave.open(wav_path, "rb") as w:
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    y = np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0
    return y, sr


# ------------------------------------------------------- deteccao de highlights

def detectar_picos(y, sr, hop=0.5, janela_base=60.0):
    """Energia RMS por janela, comparada com a mediana local (volume da live varia)."""
    import numpy as np

    win = int(sr * hop)
    n = len(y) // win
    if n < 10:
        return np.array([]), np.array([])

    blocos = y[: n * win].reshape(n, win)
    env = np.sqrt((blocos ** 2).mean(axis=1) + 1e-9)

    db = 20.0 * np.log10(env + 1e-9)

    # mediana local para normalizar o volume da live inteira
    k = int(janela_base / hop) // 2
    padded = np.pad(db, k, mode="edge")
    from numpy.lib.stride_tricks import sliding_window_view
    base = np.median(sliding_window_view(padded, 2 * k + 1), axis=1)

    score = db - base
    tempos = np.arange(n) * hop
    return tempos, score


def escolher_candidatos(tempos, score, minimo_db=4.0, raio=25.0, top=60):
    """Nao-max suppression: pega os picos mais fortes e descarta vizinhos."""
    import numpy as np

    if len(score) == 0:
        return []

    ordem = np.argsort(score)[::-1]
    escolhidos = []
    for idx in ordem:
        if score[idx] < minimo_db:
            break
        t = tempos[idx]
        if all(abs(t - e) > raio for e in escolhidos):
            escolhidos.append(t)
        if len(escolhidos) >= top:
            break
    return sorted(escolhidos)


# ----------------------------------------------------------------- transcricao

def transcrever(wav_path, modelo="small", idioma="pt", dispositivo="auto",
                bloco=900.0, cache=None):
    log(f"[3/5] Transcrevendo (modelo {modelo}, isso e a parte demorada)")
    from faster_whisper import WhisperModel

    if dispositivo == "auto":
        candidatos = [("cuda", "float16"), ("cpu", "int8")]
    elif dispositivo == "cuda":
        candidatos = [("cuda", "float16")]
    else:
        candidatos = [("cpu", "int8")]

    m = None
    for dev, ct in candidatos:
        try:
            log(f"      tentando {dev} ({ct})...")
            m = WhisperModel(modelo, device=dev, compute_type=ct)
            log(f"      ok: {dev}")
            break
        except Exception as e:
            log(f"      falhou em {dev}: {str(e)[:120]}")
            m = None
    if m is None:
        raise RuntimeError("nao consegui carregar o modelo em cuda nem em cpu")

    # Le o wav direto e passa array float32 pro Whisper.
    # Evita o decode_audio/PyAV, que quebra com av.open(metadata_errors=...)
    # dependendo da versao da lib instalada.
    y, sr = ler_wav(wav_path)
    if sr != 16000:
        raise RuntimeError(f"wav precisa estar em 16kHz, veio {sr}Hz")

    # Transcreve em blocos. O VAD do faster-whisper aloca um STFT do audio
    # inteiro; em 7h isso pede ~4,7 GB e estoura a memoria. Fatiar resolve
    # e ainda permite salvar progresso parcial.
    import json

    total = len(y) / sr
    blocos = max(1, int(total // bloco) + (1 if total % bloco else 0))

    parcial = (cache + ".parcial") if cache else None
    progresso = (cache + ".progresso") if cache else None

    resultado = []
    feito_ate = 0.0
    if parcial and os.path.exists(parcial) and os.path.exists(progresso):
        with open(parcial, encoding="utf-8") as f:
            resultado = json.load(f)
        with open(progresso) as f:
            feito_ate = float(f.read().strip() or 0)
        log(f"      retomando de {hhmmss(feito_ate)} ({len(resultado)} segmentos)")

    for i in range(blocos):
        ini = i * bloco
        fim = min(ini + bloco, total)
        if fim <= feito_ate:
            continue

        parte = y[int(ini * sr): int(fim * sr)]
        segmentos, info = m.transcribe(
            parte,
            language=idioma,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
        )

        for s in segmentos:
            palavras = [{"t0": w.start + ini, "t1": w.end + ini, "p": w.word}
                        for w in (s.words or [])]
            resultado.append({"start": s.start + ini, "end": s.end + ini,
                              "texto": s.text, "palavras": palavras})

        feito_ate = fim
        if parcial:
            with open(parcial, "w", encoding="utf-8") as f:
                json.dump(resultado, f, ensure_ascii=False)
            with open(progresso, "w") as f:
                f.write(str(feito_ate))
        print(f"\r      {hhmmss(fim)} de {hhmmss(total)}  "
              f"({100 * fim / total:.0f}%)   ", end="", flush=True)

    print()
    if cache:
        if parcial and os.path.exists(parcial):
            os.replace(parcial, cache)
        if progresso and os.path.exists(progresso):
            os.remove(progresso)
    return resultado


# ------------------------------------------------------------- montagem cortes

GATILHOS = [
    "nao acredito", "olha isso", "meu deus", "que isso", "nao e possivel",
    "para", "gritando", "caralho", "puta", "mds", "nossa", "velho",
    "ta maluco", "impossivel", "chocante", "pelamor",
]


def montar_cortes(candidatos, segmentos, minimo=18.0, maximo=55.0, n=10,
                  alvo=None, dur_total=None):
    """Converte picos de audio em janelas.

    Se `alvo` for definido, gera cortes com essa duracao (ex.: 60s exatos),
    tentando alinhar o inicio perto de uma fala e posicionar o pico dentro
    da janela. Caso contrario usa o comportamento antigo (minimo/maximo).
    """
    if not segmentos:
        return []

    if dur_total is None:
        dur_total = max((s["end"] for s in segmentos), default=0.0)
    if alvo is not None:
        alvo = float(alvo)

    cortes = []
    for t in candidatos:
        # segmento de fala mais proximo do pico
        alvo_seg = min(segmentos, key=lambda s: 0 if s["start"] <= t <= s["end"]
                       else min(abs(s["start"] - t), abs(s["end"] - t)))

        if alvo is not None:
            # ancora o pico por volta de 35% dentro da janela
            inicio = max(0.0, t - alvo * 0.35)
            # tenta comecar numa fronteira de fala proxima
            melhor_inicio = inicio
            melhor_d = 1e9
            for s in segmentos:
                cand = s["start"] - 0.25
                if inicio - 8.0 <= cand <= inicio + 6.0:
                    d = abs(cand - inicio)
                    if d < melhor_d:
                        melhor_d = d
                        melhor_inicio = cand
            inicio = max(0.0, melhor_inicio)

            fim = inicio + alvo
            if dur_total > 0 and fim > dur_total:
                fim = dur_total
                inicio = max(0.0, fim - alvo)
            # se o video for menor que o alvo, usa o que houver
            if dur_total > 0 and dur_total < alvo:
                inicio, fim = 0.0, dur_total

            dur = fim - inicio
            if dur < alvo * 0.9 and (dur_total <= 0 or dur_total >= alvo * 0.9):
                continue
        else:
            inicio = max(0.0, alvo_seg["start"] - 0.5)
            fim = inicio
            for s in segmentos:
                if s["end"] > inicio and s["end"] - inicio <= maximo:
                    fim = s["end"] + 0.35
                elif s["end"] > inicio:
                    break

            dur = fim - inicio
            if dur < minimo:
                continue

        texto = " ".join(s["texto"] for s in segmentos
                         if s["end"] > inicio and s["start"] < fim).lower()
        bonus = sum(2.0 for g in GATILHOS if g in texto)
        densidade = sum(1 for s in segmentos if s["end"] > inicio and s["start"] < fim)
        # nota levemente favorece pico bem posicionado no corte
        pos = 0.0
        if dur > 0:
            pos = 1.0 - abs(((t - inicio) / dur) - 0.35)
        nota = bonus + densidade * 0.15 + max(0.0, pos) * 0.5

        cortes.append({"start": round(inicio, 2), "end": round(fim, 2),
                       "nota": nota, "texto": texto.strip()})

    # remove sobreposicao, mantendo os de maior nota
    cortes.sort(key=lambda c: c["nota"], reverse=True)
    finais = []
    for c in cortes:
        if all(c["end"] <= f["start"] + 0.05 or c["start"] >= f["end"] - 0.05 for f in finais):
            finais.append(c)
        if len(finais) >= n:
            break
    finais.sort(key=lambda c: c["start"])
    return finais


# ------------------------------------------------------------------- legendas

def ass_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


CABECALHO_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONTE},{FS},&H00FFFFFF,&H000000FF,&H00222222,&H80000000,-1,0,0,0,100,100,0,0,1,{BORDA},2,2,{ML},{MR},{MV},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def gerar_ass(palavras, inicio, fim, path, W, H, fonte, fs):
    """Agrupa palavras de 2 em 2 (ou 3) e escreve um .ass com o trecho do corte."""
    linhas = []
    grupo = []

    def flush():
        if not grupo:
            return
        txt = " ".join(w["p"] for w in grupo).strip()
        txt = re.sub(r"[{}]", "", txt).replace(",", "").upper()
        t0 = grupo[0]["t0"] - inicio
        t1 = grupo[-1]["t1"] - inicio
        if t1 - t0 < 0.25:
            t1 = t0 + 0.25
        if t0 < 0 and t1 > 0:
            t0 = 0
        if t0 < 0:
            grupo.clear()
            return
        linhas.append(f"Dialogue: 0,{ass_time(t0)},{ass_time(t1)},Cap,,0,0,0,,{txt}")
        grupo.clear()

    for w in palavras:
        if w["t1"] <= inicio or w["t0"] >= fim:
            continue
        grupo.append(w)
        dur = grupo[-1]["t1"] - grupo[0]["t0"]
        if len(grupo) >= 3 or dur > 2.2:
            flush()
    flush()

    mv = int(H * 0.27) if H > W else int(H * 0.10)
    ml = int(W * 0.06)
    with open(path, "w", encoding="utf-8") as f:
        f.write(CABECALHO_ASS.format(
            W=W, H=H, FONTE=fonte, FS=fs,
            BORDA=max(3, fs // 12), ML=ml, MR=ml, MV=mv,
        ))
        f.write("\n".join(linhas) + "\n")


# -------------------------------------------------------------------- render

FORMATOS = {
    "916": (1080, 1920),
    "169": (1920, 1080),
    "45": (1080, 1350),
    "11": (1080, 1080),
}


def _frames_gray(src, w, h, n=16):
    """n frames espalhados pelo video, em cinza, como array numpy."""
    import numpy as np
    total = ffprobe_dur(src)
    if total <= 0:
        return None
    out = []
    for i in range(n):
        t = total * (i + 0.5) / n
        r = subprocess.run(
            [ffmpeg(), "-v", "error", "-ss", f"{t:.2f}", "-i", src,
             "-frames:v", "1", "-vf", f"scale={w}:{h}",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True)
        if len(r.stdout) == w * h:
            out.append(np.frombuffer(r.stdout, np.uint8).reshape(h, w).astype(np.float32))
    return np.stack(out) if out else None


def _caixa(a, k):
    """media numa janela k x k (borda replicada)."""
    p = k // 2
    ap = __import__("numpy").pad(a, p, mode="edge")
    s = __import__("numpy").zeros_like(a)
    for dy in range(k):
        for dx in range(k):
            s = s + ap[dy:dy + a.shape[0], dx:dx + a.shape[1]]
    return s / (k * k)


def _morfo(m, k, dilatar=True):
    import numpy as np
    p = k // 2
    mp = np.pad(m, p, constant_values=False)
    out = np.zeros_like(m)
    for dy in range(k):
        for dx in range(k):
            if dilatar:
                out |= mp[dy:dy + m.shape[0], dx:dx + m.shape[1]]
    if dilatar:
        return out
    return ~_morfo(~m, k, True)


def _blocos(m):
    """componentes conexos (4-vizinhos) de uma mascara booleana."""
    import numpy as np
    h, w = m.shape
    visto = np.zeros((h, w), bool)
    achados = []
    for y0 in range(h):
        for x0 in range(w):
            if not m[y0, x0] or visto[y0, x0]:
                continue
            pilha = [(y0, x0)]
            visto[y0, x0] = True
            pts = 0
            minx = maxx = x0
            miny = maxy = y0
            while pilha:
                cy, cx = pilha.pop()
                pts += 1
                if cx < minx: minx = cx
                if cx > maxx: maxx = cx
                if cy < miny: miny = cy
                if cy > maxy: maxy = cy
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < h and 0 <= nx < w and m[ny, nx] and not visto[ny, nx]:
                        visto[ny, nx] = True
                        pilha.append((ny, nx))
            achados.append((pts, minx, miny, maxx + 1, maxy + 1))
    return achados


def detectar_cam(src, n=16, w=240, h=135):
    """Procura a webcam no video.

    A cam fica parada (fundo do quarto) enquanto o jogo muda o tempo todo,
    entao ela aparece como uma area de variacao temporal bem menor que a
    vizinhanca. Retorna {x,y,w,h} em fracao do video, ou None.
    """
    import numpy as np
    S = _frames_gray(src, w, h, n)
    if S is None or len(S) < 5:
        return None
    M = np.median(np.abs(S - np.median(S, 0)), 0)
    M = M / (M.max() or 1.0)
    fundo = _caixa(M, 31)
    R = M / (fundo + 1e-3)

    melhor = None
    for lim in (0.45, 0.55, 0.65):
        mask = R < lim
        mask = _morfo(_morfo(mask, 3), 5)
        for pts, x0, y0, x1, y1 in _blocos(mask):
            bw, bh = x1 - x0, y1 - y0
            if bw < 12 or bh < 12:
                continue
            area = pts / float(w * h)
            fill = pts / float(bw * bh)
            ar = bw / float(bh)
            if not (0.03 <= area <= 0.32) or fill < 0.7 or not (0.45 <= ar <= 2.6):
                continue
            if bw > 0.5 * w or bh > 0.6 * h:
                continue
            # cam costuma encostar num canto e ocupar ~12% do frame
            canto = min(x0, y0, w - x1, h - y1) / float(min(w, h))
            prior = pow(2.718, -(((area - 0.12) / 0.12) ** 2))
            nota = (fill ** 2) * prior * (1.0 + 0.6 * max(0.0, 1.0 - canto * 12))
            if melhor is None or nota > melhor[0]:
                melhor = (nota, x0, y0, x1, y1)

    if not melhor:
        return None
    _, x0, y0, x1, y1 = melhor
    # tira 1% de folga pra nao pegar borda do jogo junto
    fx, fy = 0.01 * w, 0.01 * h
    x0, y0 = x0 + fx, y0 + fy
    x1, y1 = x1 - fx, y1 - fy
    return {"x": round(max(0.0, x0 / w), 4), "y": round(max(0.0, y0 / h), 4),
            "w": round(max(0.02, (x1 - x0) / w), 4),
            "h": round(max(0.02, (y1 - y0) / h), 4)}


def filtro_video(sw, sh, W, H, modo):
    R = W / H
    if sw / sh > R:
        cw = int(round(sh * R)); ch = sh
    else:
        cw = sw; ch = int(round(sw / R))
    cw -= cw % 2
    ch -= ch % 2
    x = (sw - cw) // 2
    y = (sh - ch) // 2

    if abs((cw / ch) - R) < 0.02 and cw == sw:
        return f"[0:v]scale={W}:{H}:flags=lanczos,setsar=1"

    if modo == "crop":
        return f"[0:v]crop={cw}:{ch}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1"

    if modo == "blur":
        bw, bh = W // 4, H // 4
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh},gblur=sigma=8,scale={W}:{H}[bgb];"
            f"[fg]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )

    return (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


# --- cam em cima + marca d'agua -------------------------------------------

FONTES_MARCA = [
    os.path.join(BASE, "marca.ttf"),
    os.path.join(BASE, "fonts", "marca.ttf"),
    r"C:\Windows\Fonts\ariblk.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def fonte_marca():
    for f in FONTES_MARCA:
        if os.path.exists(f):
            return f
    return None


def _esc_path_ffmpeg(p):
    """Caminho pra usar dentro de filtro do ffmpeg: barra normal e ':' escapado."""
    return p.replace("\\", "/").replace(":", "\\:")


def filtro_marca(W, H, texto="VICEMSHORTS", tam=None, opac=0.85, margem=None):
    """drawtext da marca d'agua, canto inferior direito."""
    if not texto:
        return ""
    tam = tam or max(26, int(W * 0.042))
    margem = margem or max(18, int(W * 0.045))
    fonte = fonte_marca()
    prefixo = f"fontfile='{_esc_path_ffmpeg(fonte)}':" if fonte else ""
    # drawtext come ':' e '%' como sintaxe
    txt = (texto.replace("\\", "\\\\").replace(":", "\\:")
                .replace("%", "\\%").replace("'", "\u2019"))
    return (f",drawtext={prefixo}text={txt}:expansion=none:fontsize={tam}"
            f":fontcolor=white@{opac}"
            f":borderw={max(2, int(tam * 0.09))}:bordercolor=black@0.55"
            f":x=w-tw-{margem}:y=h-th-{margem}")


def filtro_cam(sw, sh, W, H, cam, topo=0.45, marca=None):
    """Layout de shorts: cam ocupando a faixa de cima, jogo embaixo.

    cam = {x,y,w,h} em fracao do video original. topo = fracao da altura
    final reservada pra cam. marca = dict com o texto da marca d'agua.
    """
    cx = int(round(float(cam.get("x", 0.0)) * sw))
    cy = int(round(float(cam.get("y", 0.0)) * sh))
    cw = int(round(float(cam.get("w", 0.2)) * sw))
    ch = int(round(float(cam.get("h", 0.3)) * sh))
    cw = max(2, min(cw - cw % 2, sw))
    ch = max(2, min(ch - ch % 2, sh))
    cx = max(0, min(cx, sw - cw))
    cy = max(0, min(cy, sh - ch))

    topo = max(0.15, min(0.75, float(topo)))
    HC = int(round(H * topo))
    HC -= HC % 2
    HG = H - HC

    # recorte do jogo: centro, no formato da area de baixo
    R = W / HG
    if sw / sh > R:
        gw = int(round(sh * R)); gh = sh
    else:
        gw = sw; gh = int(round(sw / R))
    gw -= gw % 2
    gh -= gh % 2
    gx = (sw - gw) // 2
    gy = (sh - gh) // 2

    # a cam preenche a faixa de cima (corta sobra em cima/baixo)
    f = (f"[0:v]split=2[cam][jogo];"
         f"[cam]crop={cw}:{ch}:{cx}:{cy},"
         f"scale={W}:{HC}:force_original_aspect_ratio=increase:flags=lanczos,"
         f"crop={W}:{HC},setsar=1[camb];"
         f"[jogo]crop={gw}:{gh}:{gx}:{gy},"
         f"scale={W}:{HG}:flags=lanczos,setsar=1[jogob];"
         f"[camb][jogob]vstack=inputs=2")
    if marca:
        f += filtro_marca(W, H, marca.get("texto", "VICEMSHORTS"),
                          marca.get("tam"), marca.get("opac", 0.85))
    return f


def renderizar(src, dims, corte, fmt, modo, ass_nome, out_path, temp_dir,
               sem_legenda=False, crf=None, preset=None, tamanho=None,
               cam=None, topo=0.45, marca=None):
    W, H = tamanho if tamanho else FORMATOS[fmt]
    if modo == "cam" and cam:
        filtro = filtro_cam(dims[0], dims[1], W, H, cam, topo, marca)
    else:
        filtro = filtro_video(dims[0], dims[1], W, H, modo)
        if marca:
            filtro += filtro_marca(W, H, marca.get("texto", "VICEMSHORTS"),
                                   marca.get("tam"), marca.get("opac", 0.85))
    if not sem_legenda and ass_nome:
        filtro += f",ass={ass_nome}"
    filtro += "[vout]"

    dur = corte["end"] - corte["start"]
    cmd = [
        ffmpeg(), "-y", "-v", "error", "-stats",
        "-ss", f"{corte['start']:.3f}",
        "-i", src,
        "-t", f"{dur:.3f}",
        "-filter_complex", filtro,
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", preset or PRESET,
        "-crf", str(crf if crf is not None else CRF),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.2",
        "-g", "120", "-keyint_min", "60",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        out_path,
    ]
    subprocess.run(cmd, check=True, cwd=temp_dir)


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Corta highlights de lives do YouTube automaticamente.")
    ap.add_argument("fonte", help="URL do YouTube ou caminho de um video local")
    ap.add_argument("--clips", type=int, default=10, help="quantos clips gerar (padrao 10)")
    ap.add_argument("--formato", default="both",
                    choices=["916", "169", "45", "11", "both"],
                    help="916, 169, 45, 11 ou both (padrao both = 916 + 169)")
    ap.add_argument("--modo", default="crop", choices=["crop", "blur", "pad"],
                    help="crop = corta o centro | blur = fundo desfocado | pad = barras")
    ap.add_argument("--modelo", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--idioma", default="pt")
    ap.add_argument("--dispositivo", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--min", dest="minimo", type=float, default=18.0)
    ap.add_argument("--max", dest="maximo", type=float, default=55.0)
    ap.add_argument("--duracao", type=float, default=60.0,
                    help="duracao fixa de cada corte em segundos (0 = usa --min/--max)")
    ap.add_argument("--qualidade", type=int, default=1080, help="altura maxima do download")
    ap.add_argument("--codec", default="avc", choices=["avc", "vp9"],
                    help="avc = mp4 compativel (~11GB/7h) | vp9 = webm menor (~6.5GB/7h)")
    ap.add_argument("--crf", type=int, default=20,
                    help="qualidade do encode final: 18 = top, 20 = otimo, 23 = bom")
    ap.add_argument("--preset", default="veryfast",
                    choices=["ultrafast", "veryfast", "faster", "fast",
                             "medium", "slow"], help="mais lento = arquivo menor")
    ap.add_argument("--sem-legenda", action="store_true")
    ap.add_argument("--fonte-legenda", default="Arial Black")
    ap.add_argument("--tamanho-fonte", type=int, default=0,
                    help="0 = automatico conforme o formato")
    args = ap.parse_args()

    for d in (VODS, AUDIO, CLIPS, TEMP):
        os.makedirs(d, exist_ok=True)

    # 1. fonte
    if re.match(r"^https?://", args.fonte):
        src, info = baixar(args.fonte, args.qualidade, args.codec)
        titulo = (info.get("title") or "live")[:60]
    else:
        src = args.fonte
        titulo = os.path.splitext(os.path.basename(src))[0]

    if not os.path.exists(src):
        sys.exit(f"arquivo nao encontrado: {src}")

    dims = ffprobe_dims(src)
    total = ffprobe_dur(src)
    log(f"      video: {dims[0]}x{dims[1]}  duracao {hhmmss(total)}")

    # 2. audio
    wav = os.path.join(AUDIO, os.path.splitext(os.path.basename(src))[0] + ".wav")
    if not os.path.exists(wav):
        extrair_audio(src, wav)

    # 3. transcrever
    seg_path = wav + ".json"
    if os.path.exists(seg_path):
        import json
        with open(seg_path, encoding="utf-8") as f:
            segmentos = json.load(f)
        log("[3/5] Transcricao em cache, reaproveitando")
    else:
        segmentos = transcrever(wav, args.modelo, args.idioma, args.dispositivo,
                                cache=seg_path)
        import json
        with open(seg_path, "w", encoding="utf-8") as f:
            json.dump(segmentos, f, ensure_ascii=False)

    # 4. escolher trechos
    log("[4/5] Procurando os picos")
    y, sr = ler_wav(wav)
    tempos, score = detectar_picos(y, sr)
    candidatos = escolher_candidatos(tempos, score)
    log(f"      {len(candidatos)} picos candidatos")
    cortes = montar_cortes(candidatos, segmentos, args.minimo, args.maximo, args.clips,
                           alvo=args.duracao if args.duracao > 0 else None,
                           dur_total=total)
    if not cortes:
        sys.exit("nenhum trecho bom encontrado. tente outro video ou ajuste os parametros.")

    log(f"      {len(cortes)} trechos selecionados")

    # 5. render
    log("[5/5] Renderizando")
    formatos = ["916", "169"] if args.formato == "both" else [args.formato]
    pasta = os.path.join(CLIPS, slug(titulo, 50))
    os.makedirs(pasta, exist_ok=True)

    for i, c in enumerate(cortes, 1):
        base = f"{i:02d}_{hhmmss(c['start']).replace(':', '-')}"
        for fmt in formatos:
            W, H = FORMATOS[fmt]
            fs = args.tamanho_fonte or (76 if fmt in ("916", "45", "11") else 52)

            ass_nome = None
            if not args.sem_legenda:
                ass_nome = f"cap_{base}_{fmt}.ass"
                palavras = []
                for s in segmentos:
                    if s["end"] > c["start"] and s["start"] < c["end"]:
                        palavras.extend(s["palavras"])
                gerar_ass(palavras, c["start"], c["end"],
                          os.path.join(TEMP, ass_nome), W, H,
                          args.fonte_legenda, fs)

            out = os.path.join(pasta, f"{base}_{fmt}.mp4")
            print(f"\r      [{i}/{len(cortes)}] {fmt} -> {os.path.basename(out)}   ",
                  end="", flush=True)
            renderizar(src, dims, c, fmt, args.modo, ass_nome, out, TEMP,
                       args.sem_legenda)
            if ass_nome:
                try:
                    os.remove(os.path.join(TEMP, ass_nome))
                except OSError:
                    pass
    print()

    log(f"\nPronto. {len(cortes) * len(formatos)} arquivos em:\n  {pasta}")
    for i, c in enumerate(cortes, 1):
        log(f"  {i:02d}. {hhmmss(c['start'])} -> {hhmmss(c['end'])}  ({c['end']-c['start']:.0f}s)")
        if c["texto"]:
            log("      " + textwrap.shorten(c["texto"], 110))


if __name__ == "__main__":
    main()

