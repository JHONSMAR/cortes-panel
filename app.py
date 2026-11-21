# -*- coding: utf-8 -*-
"""
app.py - painel web para cortar lives.

  python app.py        -> abre http://127.0.0.1:8765 no navegador
"""

import json
import os
import re
import subprocess
import sys
import shutil
import threading
import time
import traceback
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cortar as N  # noqa: E402
from flask import Flask, Response, jsonify, request, send_from_directory  # noqa: E402

app = Flask(__name__)

BASE = N.BASE
PREV = os.path.join(BASE, "preview")
THUMB = os.path.join(BASE, "thumbs")
SAIDA = os.path.join(BASE, "clips")
for _d in (PREV, THUMB, SAIDA):
    os.makedirs(_d, exist_ok=True)

ESTADO = {"rodando": False, "etapa": "", "pct": 0, "msg": "", "erro": None}
PROJETO = {"src": None, "dims": None, "titulo": "", "dur": 0.0,
           "segmentos": [], "candidatos": [], "chave": ""}
LOCK = threading.Lock()
FILA = {"ativa": False, "atual": None, "itens": []}
FILA_LOCK = threading.Lock()
FILA_EVENT = threading.Event()

# ---- config da cam (layout de shorts) + marca d'agua
CAM_ARQ = os.path.join(BASE, "cam.json")
CAM_PADRAO = {"x": 0.018, "y": 0.011, "w": 0.168, "h": 0.333, "topo": 0.45,
              "marca": True, "texto": "SEU CANAL"}


def _ler_cam():
    d = dict(CAM_PADRAO)
    try:
        with open(CAM_ARQ, encoding="utf-8") as f:
            d.update(json.load(f) or {})
    except (OSError, ValueError):
        pass
    return d


CAM = _ler_cam()


def _salvar_cam():
    try:
        with open(CAM_ARQ, "w", encoding="utf-8") as f:
            json.dump(CAM, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _marca():
    """Dict de marca d'agua pro render, ou None se desligada."""
    if not CAM.get("marca", True):
        return None
    return {"texto": (CAM.get("texto") or "SEU CANAL").strip(),
            "opac": float(CAM.get("opac", 0.85))}


def _chave(src):
    """Identidade do video: caminho + tamanho + data. Evita que o cache de
    thumb/preview de um video seja servido para outro."""
    import hashlib
    st = os.stat(src)
    bruto = f"{src}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")
    return hashlib.md5(bruto).hexdigest()[:10]


ESTADO_ARQ = os.path.join(BASE, "estado.json")
PRESETS_ARQ = os.path.join(BASE, "presets.json")


def _ler_ultimo():
    try:
        with open(ESTADO_ARQ, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _salvar_ultimo(src, projeto=None):
    try:
        data = {"src": src, "quando": time.time()}
        if projeto and projeto.get("src") and os.path.abspath(projeto.get("src")) == os.path.abspath(src):
            data["projeto"] = {
                "src": projeto.get("src"),
                "dims": projeto.get("dims"),
                "titulo": projeto.get("titulo"),
                "dur": projeto.get("dur"),
                "chave": projeto.get("chave"),
                "segmentos": projeto.get("segmentos", []),
                "candidatos": projeto.get("candidatos", []),
            }
        with open(ESTADO_ARQ, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _ler_presets():
    try:
        with open(PRESETS_ARQ, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _salvar_presets(presets):
    with open(PRESETS_ARQ, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def _protegidos():
    """Videos que nao podem ser apagados sem confirmacao extra."""
    p = set()
    if PROJETO.get("src"):
        p.add(os.path.abspath(PROJETO["src"]))
    ultimo = _ler_ultimo().get("src")
    if ultimo:
        p.add(os.path.abspath(ultimo))
    return p


LOG_ARQ = os.path.join(BASE, "cortes.log")


def _log(msg):
    texto = str(msg).strip()
    ESTADO["msg"] = texto
    linha = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {texto}"
    print(linha, flush=True)
    try:
        with open(LOG_ARQ, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


N.log = _log


def _set(pct=None, etapa=None, msg=None, **extra):
    with LOCK:
        if pct is not None:
            ESTADO["pct"] = pct
        if etapa is not None:
            ESTADO["etapa"] = etapa
        if msg is not None:
            ESTADO["msg"] = msg
        ESTADO.update(extra)
        # persiste projeto quando hÃÂ¡ candidatos (permite reabrir sem reanalisar)
        try:
            src = PROJETO.get("src")
            if src and PROJETO.get("candidatos") and not ESTADO.get("rodando"):
                _salvar_ultimo(src, PROJETO)
        except Exception:
            pass


# --------------------------------------------------------------- pipeline

def _job_analisar(fonte, opcoes):
    try:
        _set(rodando=True, erro=None, pct=2, etapa="fonte", msg="preparando...")
        with LOCK:
            PROJETO.update({"src": None, "dims": None, "titulo": "", "dur": 0.0,
                            "segmentos": [], "candidatos": [], "chave": ""})

        if re.match(r"^https?://", fonte):
            _set(5, "download", "baixando video do YouTube...")
            src, info = N.baixar(fonte, opcoes["qualidade"], opcoes["codec"])
            titulo = info.get("title") or "live"
            _add_hist(fonte, titulo, src)
        else:
            src = fonte
            titulo = os.path.splitext(os.path.basename(src))[0]

        if not os.path.exists(src):
            raise FileNotFoundError(f"arquivo nao encontrado: {src}")

        dims = N.ffprobe_dims(src)
        dur = N.ffprobe_dur(src)
        chave = _chave(src)
        _set(15, "audio", f"video {dims[0]}x{dims[1]} - {N.hhmmss(dur)}")

        wav = os.path.join(N.AUDIO, os.path.splitext(os.path.basename(src))[0] + ".wav")
        if not os.path.exists(wav):
            N.extrair_audio(src, wav)

        seg_path = wav + ".json"
        if os.path.exists(seg_path):
            with open(seg_path, encoding="utf-8") as f:
                segmentos = json.load(f)
            _set(60, "transcricao", f"transcricao em cache ({len(segmentos)} trechos)")
        else:
            _set(20, "transcricao", "transcrevendo (parte demorada)...")
            parar = threading.Event()
            prog = seg_path + ".progresso"

            def _vigiar():
                while not parar.wait(2.0):
                    try:
                        with open(prog) as f:
                            s = float(f.read().strip() or 0)
                    except Exception:
                        continue
                    if dur > 0 and s > 0 and ESTADO.get("etapa") == "transcricao":
                        _set(20 + min(55, s / dur * 55), "transcricao",
                             f"{N.hhmmss(s)} de {N.hhmmss(dur)}")

            threading.Thread(target=_vigiar, daemon=True).start()
            try:
                segmentos = N.transcrever(wav, opcoes["modelo"], opcoes.get("idioma", "pt"), "auto",
                                          cache=seg_path)
            finally:
                parar.set()

        _set(85, "picos", "procurando os melhores momentos...")
        y, sr = N.ler_wav(wav)
        tempos, score = N.detectar_picos(y, sr)
        del y
        picos = N.escolher_candidatos(tempos, score, minimo_db=opcoes["limiar"])
        alvo = opcoes.get("duracao", 60)
        alvo = None if not alvo or alvo <= 0 else float(alvo)
        cortes = N.montar_cortes(picos, segmentos, 18.0, 55.0, opcoes["n"],
                                 alvo=alvo, dur_total=dur)

        if not cortes:
            raise RuntimeError("nenhum trecho encontrado. baixe a sensibilidade.")

        candidatos = []
        for i, c in enumerate(cortes):
            candidatos.append({
                "id": i,
                "inicio": round(c["start"], 1),
                "fim": round(c["end"], 1),
                "dur": round(c["end"] - c["start"], 1),
                "texto": (c.get("texto") or "").strip(),
                "score": round(float(c.get("nota") or 0), 1),
            })

        with LOCK:
            PROJETO.update({
                "src": src, "dims": dims, "titulo": titulo, "dur": dur,
                "segmentos": segmentos, "candidatos": candidatos,
                "chave": chave,
            })
            ESTADO.update(rodando=False, pct=100, etapa="pronto",
                          msg=f"{len(candidatos)} trechos encontrados")
        _salvar_ultimo(src)
    except Exception:
        with LOCK:
            ESTADO.update(rodando=False, etapa="erro",
                          erro=traceback.format_exc()[-800:],
                          msg="falhou")
        traceback.print_exc()


def _ffprobe_dur(p):
    return float(subprocess.run(
        [N.FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", p], capture_output=True, text=True, **N._subprocess_kw()).stdout.strip() or 0)


def _gerar_thumb(idx):
    c = PROJETO["candidatos"][idx]
    d = os.path.join(THUMB, PROJETO["chave"])
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, f"{idx}.jpg")
    if os.path.exists(out):
        return out
    meio = (c["inicio"] + c["fim"]) / 2
    subprocess.run([N.FFMPEG, "-y", "-v", "error", "-ss", str(meio),
                    "-i", PROJETO["src"], "-frames:v", "1",
                    "-vf", "scale=560:-2", "-q:v", "4", out], check=False, **N._subprocess_kw())
    return out


def _gerar_preview(idx, modo):
    c = PROJETO["candidatos"][idx]
    d = os.path.join(PREV, PROJETO["chave"])
    os.makedirs(d, exist_ok=True)
    extra = ""
    if modo == "cam":
        import hashlib
        extra = "_" + hashlib.md5(
            json.dumps([CAM, PROJETO["dims"]], sort_keys=True).encode()).hexdigest()[:8]
    out = os.path.join(d, f"{idx}_{modo}{extra}.mp4")
    if os.path.exists(out) and _ffprobe_dur(out) > 1:
        return out
    corte = {"start": c["inicio"], "end": c["fim"]}
    N.renderizar(PROJETO["src"], PROJETO["dims"], corte, "916", modo,
                 None, out, N.TEMP, sem_legenda=True,
                 crf=30, preset="ultrafast", tamanho=(432, 768),
                 cam=CAM, topo=CAM.get("topo", 0.45),
                 marca=_marca() if modo == "cam" else None)
    return out


# ------------------------------------------------------------------ rotas

@app.route("/api/status")
def api_status():
    with LOCK:
        return jsonify(dict(ESTADO, titulo=PROJETO["titulo"],
                            dur=PROJETO["dur"],
                            n=len(PROJETO["candidatos"])))


@app.route("/api/network-status")
def api_network_status():
    return jsonify({
        "warp": N.warp_status(),
        "bgutil": N._bgutil_pot_status(),
    })


@app.route("/api/warp/connect", methods=["POST"])
def api_warp_connect():
    if ESTADO["rodando"]:
        return jsonify({"erro": "jÃÂ¡ tem coisa rodando, espera terminar"}), 409
    r = N.warp_connect()
    status = 200 if r.get("ok") else 500
    return jsonify(r), status


@app.route("/api/warp/disconnect", methods=["POST"])
def api_warp_disconnect():
    if ESTADO["rodando"]:
        return jsonify({"erro": "jÃÂ¡ tem coisa rodando, espera terminar"}), 409
    r = N.warp_disconnect()
    status = 200 if r.get("ok") else 500
    return jsonify(r), status


@app.route("/api/analisar", methods=["POST"])
def api_analisar():
    d = request.get_json(force=True)
    if ESTADO["rodando"]:
        return jsonify({"erro": "ja existe uma tarefa rodando"}), 409

    fonte = (d.get("fonte") or "").strip().strip('"')
    if not fonte:
        return jsonify({"erro": "informe o link ou o caminho do arquivo"}), 400

    sens = float(d.get("sensibilidade", 5))
    duracao = d.get("duracao", 60)
    try:
        duracao = float(duracao)
    except (TypeError, ValueError):
        duracao = 60.0
    if duracao > 0:
        duracao = max(15.0, min(180.0, duracao))
    opcoes = {
        "n": max(1, min(40, int(d.get("n", 12)))),
        "modelo": d.get("modelo", "small"),
        "idioma": d.get("idioma", "pt") or "pt",
        "qualidade": int(d.get("qualidade", 1080)),
        "codec": d.get("codec", "avc"),
        "limiar": round(max(1.5, 7.5 - sens * 0.55), 2),
        "duracao": duracao,
    }
    threading.Thread(target=_job_analisar, args=(fonte, opcoes), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/candidatos")
def api_candidatos():
    return jsonify({"titulo": PROJETO["titulo"],
                    "chave": PROJETO["chave"],
                    "dur": PROJETO["dur"],
                    "itens": PROJETO["candidatos"]})


@app.route("/thumb/<chave>/<int:idx>")
def thumb(chave, idx):
    if chave != PROJETO["chave"] or not (0 <= idx < len(PROJETO["candidatos"])):
        return "", 404
    p = _gerar_thumb(idx)
    if not os.path.exists(p):
        return "", 404
    return send_from_directory(os.path.dirname(p), os.path.basename(p),
                               conditional=True)


@app.route("/preview/<chave>/<int:idx>")
def preview(chave, idx):
    if chave != PROJETO["chave"] or not (0 <= idx < len(PROJETO["candidatos"])):
        return "", 404
    modo = request.args.get("modo", "blur")
    if modo not in ("blur", "crop", "pad", "cam"):
        modo = "blur"
    p = _gerar_preview(idx, modo)
    return send_from_directory(os.path.dirname(p), os.path.basename(p),
                               conditional=True)


# --------------------------------------------------------- cam (shorts)

def _tempo_amostra():
    if PROJETO["candidatos"]:
        c = PROJETO["candidatos"][0]
        return (c["inicio"] + c["fim"]) / 2
    return max(1.0, PROJETO["dur"] * 0.5)


@app.route("/api/cam", methods=["GET", "POST"])
def api_cam():
    if request.method == "POST":
        d = request.get_json(force=True) or {}
        with LOCK:
            for k in ("x", "y", "w", "h"):
                if k in d:
                    CAM[k] = round(max(0.0, min(1.0, float(d[k]))), 4)
            if "topo" in d:
                CAM["topo"] = round(max(0.2, min(0.65, float(d["topo"]))), 3)
            if "marca" in d:
                CAM["marca"] = bool(d["marca"])
            if "texto" in d:
                CAM["texto"] = str(d["texto"])[:24]
            CAM["w"] = max(0.02, min(CAM["w"], 1.0 - CAM["x"]))
            CAM["h"] = max(0.02, min(CAM["h"], 1.0 - CAM["y"]))
        _salvar_cam()
        return jsonify(CAM)
    return jsonify(CAM)


@app.route("/api/detectar-cam", methods=["POST"])
def api_detectar_cam():
    if not PROJETO["src"] or not os.path.exists(PROJETO["src"]):
        return jsonify({"erro": "analise um video primeiro"}), 400
    try:
        r = N.detectar_cam(PROJETO["src"])
    except Exception:
        traceback.print_exc()
        r = None
    if not r:
        return jsonify({"erro": "nao consegui achar a cam nesse video"}), 422
    with LOCK:
        CAM.update(r)
    _salvar_cam()
    return jsonify(CAM)


def _limpar_stills():
    for nome in (f"fonte_{PROJETO['chave']}.jpg", f"layout_{PROJETO['chave']}.jpg"):
        try:
            os.remove(os.path.join(PREV, nome))
        except OSError:
            pass


@app.route("/fonteframe")
def fonteframe():
    """Frame do video original pra posicionar a caixa da cam."""
    if not PROJETO["src"]:
        return "", 404
    out = os.path.join(PREV, f"fonte_{PROJETO['chave']}.jpg")
    if not os.path.exists(out):
        os.makedirs(PREV, exist_ok=True)
        subprocess.run([N.ffmpeg(), "-y", "-v", "error",
                        "-ss", f"{_tempo_amostra():.2f}", "-i", PROJETO["src"],
                        "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", out],
                       check=False, **N._subprocess_kw())
    if not os.path.exists(out):
        return "", 404
    return send_from_directory(PREV, os.path.basename(out), conditional=True)


@app.route("/camlayout")
def camlayout():
    """Still do resultado final (cam em cima + marca) pra conferir."""
    if not PROJETO["src"]:
        return "", 404
    import hashlib
    tag = hashlib.md5(json.dumps(CAM, sort_keys=True).encode()).hexdigest()[:8]
    out = os.path.join(PREV, f"layout_{PROJETO['chave']}_{tag}.jpg")
    if not os.path.exists(out):
        os.makedirs(PREV, exist_ok=True)
        filtro = N.filtro_cam(PROJETO["dims"][0], PROJETO["dims"][1], 270, 480,
                              CAM, CAM.get("topo", 0.45), _marca()) + "[v]"
        subprocess.run([N.ffmpeg(), "-y", "-v", "error",
                        "-ss", f"{_tempo_amostra():.2f}", "-i", PROJETO["src"],
                        "-frames:v", "1", "-filter_complex", filtro,
                        "-map", "[v]", "-q:v", "3", out], check=False, **N._subprocess_kw())
    if not os.path.exists(out):
        return "", 404
    return send_from_directory(PREV, os.path.basename(out), conditional=True)


@app.route("/api/ajustar", methods=["POST"])
def api_ajustar():
    dados = request.get_json(force=True)
    try:
        idx = int(dados["id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "dados invalidos"}), 400
    if not (0 <= idx < len(PROJETO["candidatos"])):
        return jsonify({"erro": "trecho inexistente"}), 404
    c = PROJETO["candidatos"][idx]
    dur_total = float(PROJETO.get("dur") or 0.0)

    if "inicio" in dados and "fim" in dados:
        try:
            ini = float(dados["inicio"])
            fim = float(dados["fim"])
        except (TypeError, ValueError):
            return jsonify({"erro": "dados invalidos"}), 400
    elif "inicio" in dados and "duracao" in dados:
        try:
            ini = float(dados["inicio"])
            alvo = float(dados["duracao"])
        except (TypeError, ValueError):
            return jsonify({"erro": "dados invalidos"}), 400
        fim = ini + alvo
    elif "centro" in dados and "duracao" in dados:
        try:
            centro = float(dados["centro"])
            alvo = float(dados["duracao"])
        except (TypeError, ValueError):
            return jsonify({"erro": "dados invalidos"}), 400
        ini = centro - alvo / 2.0
        fim = centro + alvo / 2.0
    elif "duracao" in dados:
        try:
            alvo = float(dados["duracao"])
        except (TypeError, ValueError):
            return jsonify({"erro": "dados invalidos"}), 400
        centro = (float(c.get("inicio", 0)) + float(c.get("fim", 0))) / 2.0
        ini = centro - alvo / 2.0
        fim = centro + alvo / 2.0
    else:
        return jsonify({"erro": "dados invalidos"}), 400

    dur = fim - ini
    if dur < 5 or dur > 180:
        return jsonify({"erro": "duracao precisa ficar entre 5s e 180s"}), 400
    if ini < 0:
        ini, fim = 0.0, dur
    if dur_total > 0 and fim > dur_total:
        fim = dur_total
        ini = max(0.0, fim - dur)

    c.update(inicio=round(ini, 1), fim=round(fim, 1), dur=round(fim - ini, 1))
    pasta_prev = os.path.join(PREV, PROJETO["chave"])
    if os.path.isdir(pasta_prev):
        for f in os.listdir(pasta_prev):
            if f.startswith(f"{idx}_"):
                try:
                    os.remove(os.path.join(pasta_prev, f))
                except OSError:
                    pass
    return jsonify({"ok": True})


def _job_exportar(itens, fmtos, modo, crf, legenda, fs,
                   audio_mode="original", audio_track=0, audio_gain_db=0.0):
    try:
        _set(rodando=True, erro=None, pct=0, etapa="export",
             msg="renderizando clipes...")
        pasta = os.path.join(SAIDA, N.slug(PROJETO["titulo"], 50))
        os.makedirs(pasta, exist_ok=True)
        # marca de origem: permite apagar os clipes junto com o video
        try:
            with open(os.path.join(pasta, ".projeto"), "w", encoding="utf-8") as f:
                f.write(PROJETO.get("chave") or "")
        except OSError:
            pass
        total = len(itens) * len(fmtos)
        feito = 0

        for k, idx in enumerate(itens, 1):
            c = PROJETO["candidatos"][idx]
            corte = {"start": c["inicio"], "end": c["fim"]}
            base = f"{idx + 1:02d}_{N.hhmmss(c['inicio']).replace(':', '-')}"
            for fmt in fmtos:
                W, H = N.FORMATOS[fmt]
                ass_nome = None
                palavras = []
                for s in PROJETO["segmentos"]:
                    if s["end"] > c["inicio"] and s["start"] < c["fim"]:
                        palavras.extend(s["palavras"])
                if legenda:
                    tamanho = fs or (76 if fmt in ("916", "45", "11") else 52)
                    ass_nome = f"cap_{idx}_{fmt}.ass"
                    N.gerar_ass(palavras, c["inicio"], c["fim"],
                                os.path.join(N.TEMP, ass_nome), W, H,
                                "Arial Black", tamanho)
                # exporta legendas separadas (SRT/ASS) quando houver texto
                if palavras and fmt == fmtos[0]:
                    base_leg = os.path.join(pasta, f"{base}")
                    try:
                        N.gerar_srt(palavras, c["inicio"], c["fim"], base_leg + ".srt")
                        N.gerar_ass(palavras, c["inicio"], c["fim"], base_leg + ".ass", W, H, "Arial Black", fs or 52)
                    except Exception:
                        pass
                out = os.path.join(pasta, f"{base}_{fmt}.mp4")
                N.renderizar(PROJETO["src"], PROJETO["dims"], corte, fmt, modo,
                             ass_nome, out, N.TEMP, sem_legenda=not legenda,
                             crf=crf, preset="slow",
                             cam=CAM, topo=CAM.get("topo", 0.45),
                             marca=_marca() if modo == "cam" else None,
                             audio_mode=audio_mode, audio_track=audio_track,
                             audio_gain_db=audio_gain_db)
                if ass_nome:
                    try:
                        os.remove(os.path.join(N.TEMP, ass_nome))
                    except OSError:
                        pass
                feito += 1
                _set(int(feito / total * 100), "export",
                     f"{feito}/{total} arquivos")

        with LOCK:
            ESTADO.update(rodando=False, pct=100, etapa="pronto",
                          msg=f"salvo em {pasta}")
    except Exception:
        with LOCK:
            ESTADO.update(rodando=False, etapa="erro", msg="falhou",
                          erro=traceback.format_exc()[-800:])
        traceback.print_exc()


@app.route("/api/exportar", methods=["POST"])
def api_exportar():
    d = request.get_json(force=True)
    if ESTADO["rodando"]:
        return jsonify({"erro": "ja existe uma tarefa rodando"}), 409
    if not PROJETO["candidatos"]:
        return jsonify({"erro": "analise um video primeiro"}), 400

    total_cands = len(PROJETO["candidatos"])
    itens = sorted({int(i) for i in d.get("itens", []) if 0 <= int(i) < total_cands})
    if not itens:
        return jsonify({"erro": "selecione ao menos um trecho"}), 400
    fmtos = [f for f in d.get("formatos", []) if f in N.FORMATOS]
    if not fmtos:
        return jsonify({"erro": "escolha ao menos um formato"}), 400

    audio_mode = (d.get("audio_mode") or "original").lower()
    if audio_mode not in ("original", "normalize", "gain", "mute"):
        audio_mode = "original"
    try:
        audio_track = max(0, int(d.get("audio_track", 0)))
    except Exception:
        audio_track = 0
    try:
        audio_gain_db = float(d.get("audio_gain_db", 0))
    except Exception:
        audio_gain_db = 0.0
    audio_gain_db = max(-30.0, min(30.0, audio_gain_db))

    threading.Thread(target=_job_exportar,
                     args=(itens, fmtos, d.get("modo", "blur"),
                           int(d.get("crf", 18)),
                           bool(d.get("legenda", True)),
                           int(d.get("tamanho_fonte", 0)),
                           audio_mode, audio_track, audio_gain_db),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/audio-tracks")
def api_audio_tracks():
    src = (request.args.get("src") or "").strip()
    if src and not os.path.isabs(src):
        src = os.path.abspath(os.path.join(BASE, src))
    if not src:
        src = PROJETO.get("src") or ""
    if not src or not os.path.isfile(src):
        return jsonify({"tracks": [], "erro": "arquivo nÃÂ£o encontrado"})
    try:
        return jsonify({"tracks": N.ffprobe_audio_tracks(src)})
    except Exception as e:
        return jsonify({"tracks": [], "erro": str(e)}), 500


@app.route("/api/presets", methods=["GET", "POST"])
def api_presets():
    if request.method == "GET":
        return jsonify({"presets": _ler_presets()})
    d = request.get_json(force=True)
    nome = (d.get("nome") or "").strip()[:60]
    if not nome:
        return jsonify({"erro": "nome do preset obrigatÃÂ³rio"}), 400
    cfg = d.get("config") or {}
    if not isinstance(cfg, dict):
        return jsonify({"erro": "config invÃÂ¡lida"}), 400
    presets = _ler_presets()
    presets[nome] = cfg
    _salvar_presets(presets)
    return jsonify({"ok": True, "presets": presets})


@app.route("/api/presets/<nome>", methods=["DELETE"])
def api_presets_delete(nome):
    nome = (nome or "").strip()
    presets = _ler_presets()
    if nome in presets:
        del presets[nome]
        _salvar_presets(presets)
    return jsonify({"ok": True, "presets": presets})


def _fila_snapshot():
    with FILA_LOCK:
        return json.loads(json.dumps(FILA))


def _fila_worker():
    while True:
        FILA_EVENT.wait()
        while True:
            with FILA_LOCK:
                item = None
                for it in FILA["itens"]:
                    if it["status"] == "pendente":
                        item = it
                        it["status"] = "rodando"
                        it["msg"] = "iniciando..."
                        FILA["atual"] = it["id"]
                        break
                if not item:
                    FILA["ativa"] = False
                    FILA["atual"] = None
                    FILA_EVENT.clear()
                    break
            try:
                _fila_executar(item)
                with FILA_LOCK:
                    item["status"] = "concluido"
                    item["msg"] = "concluÃÂ­do"
            except Exception as e:
                with FILA_LOCK:
                    item["status"] = "erro"
                    item["msg"] = str(e)[-300:]


def _aguardar_idle(timeout=60*60):
    ini = time.time()
    while time.time() - ini < timeout:
        if not ESTADO.get("rodando"):
            return True
        time.sleep(1)
    return not ESTADO.get("rodando")


def _fila_executar(item):
    tipo = item.get("tipo")
    cfg = item.get("config") or {}
    if tipo == "baixar":
        url = (cfg.get("url") or "").strip()
        if not url:
            raise RuntimeError("URL vazia")
        src, info = N.baixar(url, int(cfg.get("qualidade", 1080)), cfg.get("codec", "avc"),
                             audio_only=bool(cfg.get("audio_only", False)),
                             audio_format=cfg.get("audio_format", "mp3"),
                             audio_quality=cfg.get("audio_quality", "192"))
        titulo = info.get("title") if isinstance(info, dict) else ""
        _add_hist(url, titulo, src)
        item["msg"] = f"baixado: {os.path.basename(src)[:50]}"
        return

    if tipo == "analisar":
        fonte = (cfg.get("fonte") or "").strip()
        if not fonte:
            raise RuntimeError("fonte vazia")
        op = {
            "n": max(1, min(40, int(cfg.get("n", 12)))),
            "modelo": cfg.get("modelo", "small"),
            "idioma": cfg.get("idioma", "pt") or "pt",
            "qualidade": int(cfg.get("qualidade", 1080)),
            "codec": cfg.get("codec", "avc"),
            "limiar": round(max(1.5, 7.5 - float(cfg.get("sensibilidade", 5)) * 0.55), 2),
            "duracao": float(cfg.get("duracao", 60) or 0),
        }
        while ESTADO.get("rodando"):
            time.sleep(1)
        _job_analisar(fonte, op)
        _aguardar_idle()
        if ESTADO.get("erro"):
            raise RuntimeError(ESTADO.get("erro")[-300:])
        item["msg"] = f"{len(PROJETO.get('candidatos', []))} trechos"
        return

    if tipo == "exportar":
        while ESTADO.get("rodando"):
            time.sleep(1)
        if not PROJETO.get("candidatos"):
            raise RuntimeError("sem candidatos para exportar")
        total_cands = len(PROJETO["candidatos"])
        itens = sorted({int(i) for i in cfg.get("itens", list(range(total_cands))) if 0 <= int(i) < total_cands})
        fmtos = [f for f in cfg.get("formatos", ["916"]) if f in N.FORMATOS]
        if not itens or not fmtos:
            raise RuntimeError("exportaÃÂ§ÃÂ£o sem itens/formatos")
        _job_exportar(itens, fmtos, cfg.get("modo", "blur"), int(cfg.get("crf", 18)),
                      bool(cfg.get("legenda", True)), int(cfg.get("tamanho_fonte", 0)),
                      (cfg.get("audio_mode") or "original"), int(cfg.get("audio_track", 0)),
                      float(cfg.get("audio_gain_db", 0)))
        _aguardar_idle()
        if ESTADO.get("erro"):
            raise RuntimeError(ESTADO.get("erro")[-300:])
        item["msg"] = "exportado"
        return

    raise RuntimeError(f"tipo desconhecido: {tipo}")


@app.route("/api/fila", methods=["GET", "POST"])
def api_fila():
    if request.method == "GET":
        return jsonify(_fila_snapshot())
    d = request.get_json(force=True)
    itens = d.get("itens") or []
    if not isinstance(itens, list) or not itens:
        return jsonify({"erro": "envie itens da fila"}), 400
    novos = []
    with FILA_LOCK:
        base = len(FILA["itens"])
        for i, it in enumerate(itens):
            tipo = (it.get("tipo") or "").strip()
            if tipo not in ("baixar", "analisar", "exportar"):
                continue
            novos.append({
                "id": f"f{int(time.time()*1000)}_{base+i}",
                "tipo": tipo,
                "config": it.get("config") or {},
                "status": "pendente",
                "msg": "na fila",
            })
        FILA["itens"].extend(novos)
        FILA["ativa"] = True
    if novos:
        FILA_EVENT.set()
        if not any(t.name == "fila-worker" for t in threading.enumerate()):
            threading.Thread(target=_fila_worker, daemon=True, name="fila-worker").start()
    return jsonify(_fila_snapshot())


@app.route("/api/fila/limpar", methods=["POST"])
def api_fila_limpar():
    with FILA_LOCK:
        FILA["itens"] = [i for i in FILA["itens"] if i["status"] == "rodando"]
        if not FILA["itens"]:
            FILA["ativa"] = False
            FILA["atual"] = None
            FILA_EVENT.clear()
    return jsonify(_fila_snapshot())


HIST = os.path.join(BASE, "historico.json")


def _ler_hist():
    try:
        with open(HIST, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _add_hist(url, titulo, arquivo):
    h = [x for x in _ler_hist()
         if os.path.basename(x.get("arquivo", "")) != os.path.basename(arquivo)]
    h.insert(0, {"url": url, "titulo": titulo or "",
                 "arquivo": os.path.basename(arquivo), "quando": time.time()})
    try:
        with open(HIST, "w", encoding="utf-8") as f:
            json.dump(h[:60], f, ensure_ascii=False, indent=1)
    except OSError:
        pass


@app.route("/api/baixar", methods=["POST"])
def api_baixar():
    if ESTADO["rodando"]:
        return jsonify({"erro": "jÃÂ¡ tem coisa rodando, espera terminar"}), 409
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"erro": "faltou o link"}), 400
    qual = int(d.get("qualidade") or 1080)
    codec = d.get("codec") or "avc"
    audio_only = bool(d.get("audio_only", False))
    audio_format = (d.get("audio_format") or "mp3").lower()
    if audio_format not in ("mp3", "m4a", "opus", "wav", "best"):
        audio_format = "mp3"
    audio_quality = str(d.get("audio_quality") or "192")

    def job():
        try:
            _set(rodando=True, erro=None, pct=2, etapa="download",
                 msg=("baixando ÃÂ¡udio " if audio_only else "baixando ") + url[-24:])
            src, info = N.baixar(url, qual, codec,
                                 audio_only=audio_only,
                                 audio_format=audio_format,
                                 audio_quality=audio_quality)
            titulo = info.get("title") if isinstance(info, dict) else ""
            _add_hist(url, titulo, src)
            _set(rodando=False, pct=100, etapa="pronto",
                 msg="baixado: " + os.path.basename(src)[:60])
        except Exception as e:
            msg = str(e)
            if ("anti-bot/PO token" in msg or "Sign in to confirm" in msg) and "caminho do arquivo local" not in msg:
                msg = (
                    f"{msg}\n"
                    "Dica: baixe o vÃÂ­deo por outro mÃÂ©todo e cole o caminho do arquivo local no campo de fonte."
                )
            _set(rodando=False, pct=0, etapa="erro", erro=f"falha no download: {msg}")

    threading.Thread(target=job, daemon=True).start()
    return jsonify({"ok": True})


def _videos():
    itens = []
    if not os.path.isdir(N.VODS):
        return itens
    for f in sorted(os.listdir(N.VODS)):
        p = os.path.join(N.VODS, f)
        if os.path.isfile(p) and f.lower().endswith((".mp4", ".mkv", ".webm")):
            itens.append((f, p))
    return itens


def _clipes_por_chave():
    mapa = {}
    if not os.path.isdir(SAIDA):
        return mapa
    for sub in os.listdir(SAIDA):
        pasta = os.path.join(SAIDA, sub)
        marca = os.path.join(pasta, ".projeto")
        if os.path.isfile(marca):
            try:
                with open(marca, encoding="utf-8") as f:
                    k = f.read().strip()
            except OSError:
                continue
            if k:
                mapa.setdefault(k, []).append(sub)
    return mapa


def _item(id_, tipo, rotulo, caminho):
    existe = os.path.exists(caminho)
    return {"id": id_, "tipo": tipo, "rotulo": rotulo,
            "bytes": _tamanho(caminho) if existe else 0, "existe": existe}


@app.route("/api/arquivos")
def api_arquivos():
    grupos = []
    chaves = {}
    audio_ref = set()
    clipes = _clipes_por_chave()
    todas_pastas_clipe = {s for v in clipes.values() for s in v}

    for nome, p in _videos():
        try:
            k = _chave(p)
        except OSError:
            k = None
        if k:
            chaves[k] = nome
        raiz = os.path.splitext(nome)[0]

        itens = [_item("vod|" + nome, "video", "vÃÂ­deo", p)]
        for tipo, arq, rot in (("audio", raiz + ".wav", "ÃÂ¡udio"),
                               ("transcricao", raiz + ".wav.json", "transcriÃÂ§ÃÂ£o")):
            audio_ref.add(arq)
            itens.append(_item(f"{tipo}|{arq}", tipo, rot,
                               os.path.join(N.AUDIO, arq)))
        if k:
            itens.append(_item("preview|" + k, "preview", "previews",
                               os.path.join(PREV, k)))
            itens.append(_item("thumb|" + k, "thumb", "thumbnails",
                               os.path.join(THUMB, k)))
            for sub in clipes.get(k, []):
                itens.append(_item("clipes|" + sub, "clipes", "clipes exportados",
                                   os.path.join(SAIDA, sub)))

        itens = [i for i in itens if i["existe"]]
        grupos.append({"titulo": nome, "chave": k, "tamanho": p,
                       "bytes": sum(i["bytes"] for i in itens), "itens": itens})

    # sobras que nao pertencem a nenhum video existente
    orf = []
    if os.path.isdir(N.AUDIO):
        for f in sorted(os.listdir(N.AUDIO)):
            p = os.path.join(N.AUDIO, f)
            if os.path.isfile(p) and f not in audio_ref:
                orf.append(_item("orfao|audio|" + f, "audio", f, p))
    for base, tag, rot in ((PREV, "preview", "previews"), (THUMB, "thumb", "thumbnails")):
        if os.path.isdir(base):
            for f in sorted(os.listdir(base)):
                p = os.path.join(base, f)
                if os.path.isdir(p) and f not in chaves:
                    orf.append(_item(f"orfao|{tag}|{f}", tag, rot, p))
    if os.path.isdir(SAIDA):
        for sub in sorted(os.listdir(SAIDA)):
            p = os.path.join(SAIDA, sub)
            if os.path.isdir(p) and sub not in todas_pastas_clipe:
                orf.append(_item("orfao|clipes|" + sub, "clipes", "clipes exportados", p))

    if orf:
        grupos.append({"titulo": "sobras sem vÃÂ­deo", "chave": None, "tamanho": "",
                       "bytes": sum(i["bytes"] for i in orf), "itens": orf})

    uso = shutil.disk_usage(BASE)
    faltando = []
    for h in _ler_hist():
        alvo = os.path.join(N.VODS, os.path.basename(h.get("arquivo", "")))
        faltando.append({"url": h.get("url", ""),
                         "titulo": h.get("titulo") or h.get("arquivo", ""),
                         "existe": os.path.exists(alvo)})

    return jsonify({"grupos": grupos,
                    "historico": faltando,
                    "total_bytes": sum(g["bytes"] for g in grupos),
                    "livre_bytes": uso.free,
                    "disco_bytes": uso.total})


TIPO_PASTA = {"vod": N.VODS, "audio": N.AUDIO, "transcricao": N.AUDIO,
              "preview": PREV, "thumb": THUMB, "clipes": SAIDA}


@app.route("/api/apagar-item", methods=["POST"])
def api_apagar_item():
    d = request.get_json(force=True)
    ident = (d.get("id") or "").strip()
    if not d.get("confirmar"):
        return jsonify({"erro": "confirmacao necessaria"}), 400

    partes = ident.split("|")
    if len(partes) == 3 and partes[0] == "orfao":
        tipo, nome = partes[1], partes[2]
    elif len(partes) == 2:
        tipo, nome = partes
    else:
        return jsonify({"erro": "item invalido"}), 400

    base = TIPO_PASTA.get(tipo)
    if not base:
        return jsonify({"erro": "tipo invalido"}), 400

    nome = os.path.basename(nome)
    if nome in ("", ".", ".."):
        return jsonify({"erro": "nome invalido"}), 400
    alvo = os.path.join(base, nome)
    if os.path.abspath(os.path.dirname(alvo)) != os.path.abspath(base):
        return jsonify({"erro": "caminho invalido"}), 400
    if not os.path.exists(alvo):
        return jsonify({"erro": "nao existe mais"}), 404
    if tipo == "vod" and os.path.abspath(alvo) in _protegidos() and not d.get("forcar"):
        return jsonify({"erro": "protegido",
                        "detalhe": "video do ultimo projeto analisado"}), 409

    apagados = []
    livre = _remover(alvo, apagados)
    if livre == 0 and not apagados:
        return jsonify({"erro": "não consegui apagar (arquivo em uso?)"}), 500

    if tipo == "vod" and PROJETO.get("src") and \
            os.path.abspath(PROJETO["src"]) == os.path.abspath(alvo):
        with LOCK:
            PROJETO.update({"src": None, "candidatos": [], "segmentos": [],
                            "chave": "", "titulo": "", "dur": 0.0})

    return jsonify({"ok": True, "apagados": apagados,
                    "liberado_gb": round(livre / 1e9, 3)})


@app.route("/api/vods")
def api_vods():
    itens = []
    prot = _protegidos()
    for f in sorted(os.listdir(N.VODS)):
        p = os.path.join(N.VODS, f)
        if os.path.isfile(p) and f.lower().endswith((".mp4", ".mkv", ".webm")):
            itens.append({"nome": f, "caminho": p,
                          "gb": round(os.path.getsize(p) / 1e9, 2),
                          "protegido": os.path.abspath(p) in prot})
    return jsonify(itens)


def _tamanho(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for raiz, _dirs, arqs in os.walk(p):
        for a in arqs:
            try:
                total += os.path.getsize(os.path.join(raiz, a))
            except OSError:
                pass
    return total


def _remover(p, apagados):
    """Remove arquivo ou pasta e registra no relatorio. Retorna bytes liberados."""
    if not os.path.exists(p):
        return 0
    livre = _tamanho(p)

    if os.path.isdir(p):
        import shutil
        shutil.rmtree(p, ignore_errors=True)
        if os.path.exists(p):
            for raiz, _d, arqs in os.walk(p):
                for a in arqs:
                    try:
                        os.chmod(os.path.join(raiz, a), 0o666)
                    except OSError:
                        pass
            shutil.rmtree(p, ignore_errors=True)
        if os.path.exists(p):
            return 0          # falhou: nao mente no relatorio
        apagados.append(os.path.basename(p) + "/")
    else:
        try:
            os.remove(p)
        except OSError:
            return 0
        apagados.append(os.path.basename(p))
    return livre


@app.route("/api/apagar-vod", methods=["POST"])
def api_apagar_vod():
    d = request.get_json(force=True)
    nome = os.path.basename((d.get("nome") or "").strip())
    if not nome:
        return jsonify({"erro": "nome nao informado"}), 400

    p = os.path.join(N.VODS, nome)
    if os.path.abspath(os.path.dirname(p)) != os.path.abspath(N.VODS):
        return jsonify({"erro": "caminho invalido"}), 400
    if not os.path.exists(p):
        return jsonify({"erro": "arquivo nao existe"}), 404

    if not d.get("confirmar"):
        return jsonify({"erro": "confirmacao necessaria"}), 400

    if os.path.abspath(p) in _protegidos() and not d.get("forcar"):
        return jsonify({"erro": "protegido",
                        "detalhe": "este e o video do ultimo projeto analisado"}), 409

    raiz = os.path.splitext(nome)[0]

    # chave tem que ser calculada ANTES de apagar o arquivo
    try:
        chave = _chave(p)
    except OSError:
        chave = None

    alvos = [p]
    for sufixo in (".wav", ".wav.json", ".wav.parcial", ".wav.progresso"):
        alvos.append(os.path.join(N.AUDIO, raiz + sufixo))
    if chave:
        alvos.append(os.path.join(PREV, chave))
        alvos.append(os.path.join(THUMB, chave))

    apagados, liberado = [], 0
    for alvo in alvos:
        liberado += _remover(alvo, apagados)

    # pastas de clipes exportados que apontam para este video
    if chave and os.path.isdir(SAIDA):
        for sub in os.listdir(SAIDA):
            pasta = os.path.join(SAIDA, sub)
            marca = os.path.join(pasta, ".projeto")
            if os.path.isfile(marca):
                igual = False
                try:
                    with open(marca, encoding="utf-8") as f:
                        igual = f.read().strip() == chave
                    # arquivo precisa estar FECHADO antes do rmtree, senao
                    # o Windows recusa apagar a pasta
                except OSError:
                    pass
                if igual:
                    liberado += _remover(pasta, apagados)

    if chave and PROJETO.get("chave") == chave:
        with LOCK:
            PROJETO.update({"src": None, "candidatos": [], "segmentos": [],
                            "chave": "", "titulo": "", "dur": 0.0})

    return jsonify({"ok": True, "apagados": apagados,
                    "liberado_gb": round(liberado / 1e9, 2)})


@app.route("/api/limpar-tudo", methods=["POST"])
def api_limpar_tudo():
    d = request.get_json(force=True)
    if (d.get("texto") or "").strip().upper() != "APAGAR":
        return jsonify({"erro": "digite APAGAR para confirmar"}), 400
    if ESTADO["rodando"]:
        return jsonify({"erro": "tem tarefa rodando. espere terminar."}), 409

    apagados, liberado = [], 0
    for pasta in (N.VODS, N.AUDIO, PREV, THUMB, SAIDA, N.TEMP):
        if not os.path.isdir(pasta):
            continue
        for item in os.listdir(pasta):
            liberado += _remover(os.path.join(pasta, item), apagados)
    liberado += _remover(ESTADO_ARQ, apagados)

    with LOCK:
        PROJETO.update({"src": None, "dims": None, "titulo": "", "dur": 0.0,
                        "segmentos": [], "candidatos": [], "chave": ""})
        ESTADO.update(rodando=False, pct=0, etapa="", erro=None,
                      msg=f"{len(apagados)} itens apagados")

    return jsonify({"ok": True, "itens": len(apagados),
                    "liberado_gb": round(liberado / 1e9, 2)})


@app.route("/api/abrir-pasta", methods=["POST"])
def api_abrir_pasta():
    d = request.get_json(force=True)
    p = d.get("pasta") or SAIDA
    if os.path.isdir(p):
        subprocess.Popen(["explorer.exe", p], **N._subprocess_kw())
        return jsonify({"ok": True})
    return jsonify({"erro": "pasta nao existe"}), 404


@app.route("/api/sair", methods=["POST"])
def api_sair():
    if ESTADO.get("rodando"):
        return jsonify({"erro": "Tem tarefa rodando. Espere terminar ou use com cuidado."}), 409

    def _bye():
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True, "msg": "Encerrando Cortes..."})


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Painel local para analisar lives, encontrar highlights e exportar clipes em formatos verticais e horizontais.">
<meta name="theme-color" content="#0a0c11">
<title>Cortes - Painel de Clipes</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop stop-color='%236c5cff'/%3E%3Cstop offset='1' stop-color='%2322d3a6'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='%23111420'/%3E%3Cpath d='M20 18l26 14-26 14z' fill='url(%23g)'/%3E%3C/svg%3E">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');
  :root{
    --bg:#07090f; --bg2:#0d111a; --card:#121826cc; --card-solid:#121826; --card2:#182033; --card3:#1e2940;
    --line:#ffffff14; --line2:#ffffff24; --txt:#f5f7fb; --dim:#a7b1c7; --dim2:#748099;
    --acc:#7c5cff; --acc2:#36d3b0; --warn:#ffd36a; --danger:#ff6b81;
    --shadow:0 20px 60px #00000055; --radius:18px;
  }
  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{margin:0;color:var(--txt);font:14px/1.5 "Manrope",system-ui,-apple-system,sans-serif;
    background:
      radial-gradient(1200px 500px at 10% -10%, #7c5cff22, transparent 60%),
      radial-gradient(900px 500px at 100% 0%, #36d3b018, transparent 55%),
      linear-gradient(180deg, #0a0d14 0%, #07090f 100%);
    min-height:100vh}
  body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.35;
    background-image:linear-gradient(#ffffff05 1px,transparent 1px),linear-gradient(90deg,#ffffff05 1px,transparent 1px);
    background-size:28px 28px;mask-image:radial-gradient(circle at center,black 35%,transparent 85%)}
  .wrap{max-width:1380px;margin:0 auto;padding:28px 20px 64px;position:relative}
  .top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px;padding:18px 20px;
    background:linear-gradient(180deg,#ffffff0d,transparent),var(--card);border:1px solid var(--line);border-radius:22px;backdrop-filter:blur(14px);box-shadow:var(--shadow)}
  .brand{display:flex;gap:14px;align-items:center}
  .logo{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:14px;
    background:linear-gradient(135deg,var(--acc),#5a9bff 55%,var(--acc2));box-shadow:0 10px 30px #7c5cff55}
  .brand h1{margin:0;font-size:28px;line-height:1;letter-spacing:-.03em;font-weight:800}
  .sub{margin-top:6px;color:var(--dim);font-size:13px;max-width:760px}
  .topacts{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
  .chip{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;background:#ffffff08;border:1px solid var(--line);color:var(--dim);font-size:12px;font-weight:600}
  .chip i{width:8px;height:8px;border-radius:50%;background:#748099;box-shadow:0 0 0 4px #74809922}
  .chip.on{color:#dffbf2;border-color:#36d3b044;background:#36d3b012}
  .chip.on i{background:var(--acc2);box-shadow:0 0 0 4px #36d3b022;animation:pulse 1.6s infinite}
  @keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.5);opacity:.4}}

  .card{background:linear-gradient(180deg,#ffffff09,transparent 30%),var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:20px;margin-bottom:16px;backdrop-filter:blur(14px);box-shadow:0 8px 30px #00000035}
  .chead{display:flex;align-items:center;gap:10px;margin-bottom:16px}
  .num{width:26px;height:26px;border-radius:9px;display:grid;place-items:center;font-size:12px;font-weight:800;color:#ddd6ff;
    background:#7c5cff20;border:1px solid #7c5cff44}
  .chead h2{margin:0;font-size:16px;letter-spacing:-.01em;font-weight:750}
  .count{color:var(--dim2);font-size:12px;margin-left:2px}
  .row{display:flex;gap:12px;flex-wrap:wrap}.mt{margin-top:12px}
  .f{display:flex;flex-direction:column;gap:6px;min-width:150px;flex:1}
  .f.grow{flex:2;min-width:260px}
  label{font-size:11px;font-weight:700;color:#c4ccdb;letter-spacing:.04em;text-transform:uppercase}
  input[type=text],input[type=number],select{width:100%;background:#0b1220;border:1px solid var(--line2);color:var(--txt);
    border-radius:12px;padding:11px 12px;font:inherit;outline:none;transition:.18s}
  input[type=text]:focus,input[type=number]:focus,select:focus{border-color:#7c5cff99;box-shadow:0 0 0 4px #7c5cff1f}
  input[type=range]{width:100%;accent-color:var(--acc)}
  .chk{display:flex;align-items:center;gap:8px;color:var(--dim);font-size:13px;font-weight:600;text-transform:none;letter-spacing:0}
  .chk input{width:16px;height:16px;accent-color:var(--acc)}

  button{appearance:none;border:1px solid transparent;background:linear-gradient(135deg,var(--acc),#697eff);color:#fff;
    padding:10px 14px;border-radius:12px;font:600 13px/1 Manrope,sans-serif;cursor:pointer;transition:.18s;white-space:nowrap}
  button:hover{transform:translateY(-1px);filter:brightness(1.06);box-shadow:0 8px 24px #7c5cff44}
  button:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
  button.ghost{background:#ffffff06;color:#e6ebf5;border-color:var(--line2)}
  button.ghost:hover{background:#ffffff10;box-shadow:none}
  button.sm{padding:7px 10px;border-radius:10px;font-size:12px}
  button.danger{background:#ff6b8118;color:#ff9aaa;border-color:#ff6b8144}
  button.danger:hover{background:#ff6b8128;box-shadow:none}

  .progwrap{margin-top:14px;padding:14px;border-radius:14px;background:#0b1220;border:1px solid var(--line)}
  .bar{height:8px;background:#ffffff10;border-radius:999px;overflow:hidden}
  .bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),#59a0ff,var(--acc2));box-shadow:0 0 18px #7c5cff88;transition:width .25s}
  .st{display:flex;justify-content:space-between;margin-top:10px;font-size:12px;color:var(--dim);font-weight:600}
  .logmini{margin-top:8px;display:grid;gap:4px;font:11px/1.35 "JetBrains Mono",monospace;color:#93a0b8;max-height:74px;overflow:auto}
  .err{display:none;margin-top:10px;padding:10px 12px;border-radius:10px;background:#ff6b8114;border:1px solid #ff6b8144;color:#ffb3bf;font-size:12px;white-space:pre-wrap}

  .toolbar2{display:flex;justify-content:space-between;align-items:center;margin:-4px 0 12px;padding:10px 12px;border-radius:12px;background:#ffffff05;border:1px solid var(--line);color:var(--dim);font-size:12px}
  #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
  .clip{background:#0d1422;border:1px solid var(--line);border-radius:16px;overflow:hidden;transition:.2s}
  .clip:hover{border-color:#7c5cff55;transform:translateY(-2px);box-shadow:0 14px 30px #00000045}
  .clip.sel{border-color:#7c5cffaa;box-shadow:0 0 0 1px #7c5cff55,0 14px 30px #7c5cff18}
  .thumb{position:relative;aspect-ratio:16/9;background:#05080f;cursor:pointer}
  .thumb img,.thumb video{width:100%;height:100%;object-fit:cover;display:block}
  .badge{position:absolute;top:10px;left:10px;padding:4px 8px;border-radius:8px;font:700 11px/1 "JetBrains Mono",monospace;background:#00000088;border:1px solid #ffffff22;backdrop-filter:blur(6px)}
  .badge.r{left:auto;right:10px;color:#c7ffe9}
  .badge.b{top:auto;bottom:10px;left:10px;color:var(--warn)}
  .play{position:absolute;inset:auto 10px 10px auto;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:#00000088;border:1px solid #ffffff25;font-size:12px}
  .body{padding:14px}
  .meta{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:8px}
  .time{font:600 12px/1 "JetBrains Mono",monospace;color:#d6def0}
  .durbadge{font-size:11px;padding:4px 8px;border-radius:999px;background:#ffffff08;color:var(--dim);border:1px solid var(--line)}
  .durbadge.ok{color:#9ff0d8;background:#36d3b014;border-color:#36d3b044}
  .txt{font-size:13px;color:#c2cada;min-height:38px;margin-bottom:10px}
  .editor{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .nudge{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
  .nudge button{padding:6px 8px;font-size:11px;border-radius:8px;background:#ffffff08;color:#dce3f3;border:1px solid var(--line)}
  .nudge button.acc{background:#7c5cff20;color:#ddd6ff;border-color:#7c5cff55}
  .foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
  .acts{display:flex;gap:6px}
  .empty{padding:28px;text-align:center;color:var(--dim);background:#ffffff03;border:1px dashed var(--line2);border-radius:14px}

  .pills{display:flex;flex-wrap:wrap;gap:8px}
  .pill{padding:8px 12px;border-radius:999px;background:#ffffff06;border:1px solid var(--line2);color:var(--dim);font-weight:700;font-size:12px;cursor:pointer;user-select:none}
  .pill.on{color:#efeaff;background:#7c5cff22;border-color:#7c5cff88}
  .expbar{display:flex;align-items:center;gap:10px;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
  .tot{font-size:13px;color:var(--dim);font-weight:600}
  .grow{flex:1}
  .preset-row{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-top:14px;padding:12px;border-radius:14px;background:#0b1220;border:1px solid var(--line)}
  .fila-box{margin-top:12px;padding:12px;border-radius:14px;background:#0b1220;border:1px solid var(--line)}
  .fila-head{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px}
  .fila-list{display:grid;gap:6px;max-height:170px;overflow:auto}
  .fila-item{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:10px;background:#ffffff04;font-size:12px;color:var(--dim)}
  .fila-item b{font-size:10px;padding:3px 6px;border-radius:6px;background:#ffffff10;color:#dfe7f7;text-transform:uppercase}

  .camwrap{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:14px;margin-top:12px}
  .camsrc{position:relative;border-radius:14px;overflow:hidden;border:1px solid var(--line);background:#000}
  .camsrc img{width:100%;display:block}
  #cambox{position:absolute;border:2px solid #7c5cff;box-shadow:0 0 0 9999px #00000066;cursor:move}
  #cambox i{position:absolute;width:12px;height:12px;background:#7c5cff;border-radius:3px}
  #cambox i[data-h="nw"]{top:-6px;left:-6px;cursor:nwse-resize} #cambox i[data-h="ne"]{top:-6px;right:-6px;cursor:nesw-resize}
  #cambox i[data-h="sw"]{bottom:-6px;left:-6px;cursor:nesw-resize} #cambox i[data-h="se"]{bottom:-6px;right:-6px;cursor:nwse-resize}
  .camres{border-radius:14px;overflow:hidden;border:1px solid var(--line);background:#0b1220}
  .camres img{width:100%;display:block;aspect-ratio:9/16;object-fit:cover}
  .camres .cap{padding:8px;font-size:12px;color:var(--dim);text-align:center}
  .camctl{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:10px;padding:10px;border-radius:12px;background:#ffffff04;border:1px solid var(--line)}

  .modal{position:fixed;inset:0;background:#05070dcc;backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;padding:20px;z-index:100}
  .modal.on{display:flex}
  .mbox{width:min(860px,100%);max-height:min(82vh,860px);overflow:auto;padding:20px;border-radius:20px;background:#111827;border:1px solid var(--line2);box-shadow:var(--shadow)}
  .mbox h3{margin:0 0 4px;font-size:22px;letter-spacing:-.02em}
  .hint{color:var(--dim);font-size:13px;margin-bottom:12px}
  .vfav{display:flex;gap:8px;margin-bottom:10px}
  .disk{display:flex;justify-content:space-between;gap:10px;padding:10px 12px;margin:10px 0;border-radius:12px;background:#ffffff05;color:var(--dim);font-size:12px}
  .vgroup{margin-bottom:10px;border:1px solid var(--line);border-radius:14px;overflow:hidden}
  .vhead{display:flex;justify-content:space-between;gap:10px;padding:10px 12px;background:#ffffff06;font-size:13px}
  .vhead b{font-size:13px}
  .vitem{display:flex;align-items:center;gap:10px;padding:9px 12px;border-top:1px solid var(--line);font-size:12px}
  .vitem .rot{flex:1;color:#d9e1f0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vitem .sz{color:var(--dim2);font-family:"JetBrains Mono",monospace;font-size:11px}
  .vfalta{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:12px;color:var(--dim)}

  #toasts{position:fixed;right:18px;bottom:18px;display:grid;gap:8px;z-index:200}
  .toast{padding:10px 12px;border-radius:12px;background:#182033;color:#eef3ff;border:1px solid var(--line2);box-shadow:var(--shadow);font-size:13px}
  .toast.ok{border-color:#36d3b055}.toast.erro{border-color:#ff6b8155;color:#ffd8de}
  .conf{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:#00000099;z-index:210;padding:20px}
  .conf.on{display:flex}
  .confbox{width:min(480px,100%);padding:18px;border-radius:16px;background:#121826;border:1px solid var(--line2);box-shadow:var(--shadow)}
  .confbox p{margin:0 0 14px;color:#dce4f4}
  .confbox .acts{display:flex;justify-content:flex-end;gap:8px}

  @media (max-width:980px){.camwrap{grid-template-columns:1fr}.camres{max-width:280px}}
  @media (max-width:720px){
    .wrap{padding:14px 12px 48px}.top{padding:16px}.brand h1{font-size:22px}
    .topacts{justify-content:flex-start}.card{padding:14px;border-radius:16px}
    #grid{grid-template-columns:1fr}.f{min-width:120px}.expbar{flex-wrap:wrap}
    .mbox{padding:14px}#toasts{left:12px;right:12px}
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand">
      <div class="logo" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 5.5v13l10-6.5z"/></svg></div>
      <div>
        <h1>Cortes</h1>
        <div class="sub">Encontre os melhores momentos da live, ajuste os cortes com precisÃÂ£o e exporte clipes prontos para Shorts, Reels e YouTube.</div>
      </div>
    </div>
    <div class="topacts">
      <span class="chip" id="chip"><i></i><span id="chiptxt">parado</span></span>
      <button class="ghost" onclick="abrirBiblioteca()">Biblioteca</button>
      <button class="ghost" onclick="listarVods()" title="Gerenciar vÃÂ­deos, ÃÂ¡udios e clipes no disco">Arquivos</button>
    </div>
  </header>

  <section class="card">
    <div class="chead"><span class="num">1</span><h2>Fonte</h2></div>
    <div class="row">
      <div class="f grow">
        <label>Link do YouTube ou arquivo local</label>
        <input type="text" id="fonte" placeholder="https://www.youtube.com/live/...">
      </div>
      <button id="btnAnalisar" onclick="analisar()">Analisar</button>
      <button class="ghost" id="btnWarp" onclick="toggleWarp()" title="Cloudflare WARP (troca IP de saÃÂ­da)">WARP: ...</button>
    </div>
    <div class="row mt">
      <div class="f"><label>Trechos</label><input type="number" id="n" value="12" min="1" max="40"></div>
      <div class="f"><label>DuraÃÂ§ÃÂ£o</label>
        <select id="duracao">
          <option value="60" selected>60s (exato)</option>
          <option value="45">45s</option>
          <option value="30">30s</option>
          <option value="90">90s</option>
          <option value="0">automÃÂ¡tica</option>
        </select></div>
      <div class="f"><label>Modelo</label>
        <select id="modelo"><option value="tiny">tiny</option>
        <option value="base">base</option><option value="small" selected>small</option>
        <option value="medium">medium</option></select></div>
      <div class="f"><label>Idioma</label>
        <select id="idioma">
          <option value="pt" selected>PortuguÃÂªs (BR)</option>
          <option value="en">English</option>
          <option value="fr">FranÃÂ§ais</option>
          <option value="es">EspaÃÂ±ol</option>
          <option value="auto">Auto</option>
        </select></div>
      <div class="f"><label>Qualidade</label>
        <select id="qualidade"><option>720</option><option selected>1080</option><option>1440</option></select></div>
      <div class="f"><label>Codec</label>
        <select id="codec"><option value="avc" selected>H.264</option><option value="vp9">VP9</option></select></div>
      <div class="f grow"><label>Sensibilidade <span id="sensv" style="color:var(--acc)">5</span></label>
        <input type="range" id="sens" min="1" max="10" value="5"
               oninput="document.getElementById('sensv').textContent=this.value"></div>
    </div>
    <div class="row mt">
      <div class="f">
        <label>&nbsp;</label>
        <label class="chk" style="height:41px"><input type="checkbox" id="audioOnly" onchange="toggleAudioOnly()"> sÃÂ³ ÃÂ¡udio</label>
      </div>
      <div class="f"><label>Formato ÃÂ¡udio</label>
        <select id="audioFormat">
          <option value="mp3" selected>MP3</option>
          <option value="m4a">M4A</option>
          <option value="opus">Opus</option>
          <option value="wav">WAV</option>
        </select></div>
      <div class="f"><label>Qualidade ÃÂ¡udio</label>
        <select id="audioQuality">
          <option value="128">128 kbps</option>
          <option value="192" selected>192 kbps</option>
          <option value="256">256 kbps</option>
          <option value="320">320 kbps</option>
        </select></div>
      <div class="f grow"><label>Download direto</label>
        <button class="ghost" onclick="baixarFonte()" style="height:41px">Baixar fonte</button>
      </div>
    </div>
    <div class="progwrap">
      <div class="bar"><i id="prog"></i></div>
      <div class="st"><span id="status">parado</span><span id="pct"></span></div>
      <div class="logmini" id="logmini"></div>
      <div class="err" id="erro"></div>
      <div class="logmini" id="netstatus" style="margin-top:8px;display:none"></div>
    </div>
  </section>

  <section class="card">
    <div class="chead"><span class="num">2</span><h2>Trechos</h2><span class="count" id="cont"></span>
      <span class="grow"></span>
      <button class="ghost sm" onclick="aplicarDuracaoTodos(60)">todos em 60s</button>
      <button class="ghost sm" onclick="marcarTodos(true)">todos</button>
      <button class="ghost sm" onclick="marcarTodos(false)">nenhum</button>
    </div>
    <div class="toolbar2">
      <span id="selresumo" class="tot"></span>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="hint">Edite inÃÂ­cio/fim ou use os botÃÂµes de ajuste, depois salve. A prÃÂ©via recarrega apÃÂ³s salvar.</span>
        <button class="ghost sm" onclick="salvarTodosAjustes()">salvar todos</button>
      </div>
    </div>
    <div id="grid"><div class="empty"><strong style="display:block;color:var(--txt);font-size:14px;margin-bottom:6px">Nenhum trecho analisado ainda</strong>Cole um link do YouTube ou caminho local, ajuste os parÃÂ¢metros e clique em <b>Analisar</b> para gerar candidatos automaticamente.</div></div>
  </section>

  <section class="card">
    <div class="chead"><span class="num">3</span><h2>Exportar</h2></div>
    <div class="row">
      <div class="f grow">
        <label>Formatos</label>
        <div class="pills" id="fmtos">
          <div class="pill on" data-f="916">9:16</div>
          <div class="pill on" data-f="169">16:9</div>
          <div class="pill" data-f="45">4:5</div>
          <div class="pill" data-f="11">1:1</div>
        </div>
      </div>
      <div class="f"><label>Enquadramento</label>
        <select id="modo" onchange="modoMudou()"><option value="blur" selected>fundo desfocado</option>
        <option value="cam">cam em cima (shorts)</option>
        <option value="crop">cortar centro</option><option value="pad">barras pretas</option></select></div>
      <div class="f"><label>Qualidade</label>
        <select id="crf"><option value="18" selected>top (18)</option>
        <option value="20">otima (20)</option><option value="23">boa (23)</option></select></div>
      <div class="f" style="min-width:auto">
        <label>&nbsp;</label>
        <label class="chk" style="height:41px"><input type="checkbox" id="legenda" checked> legenda</label>
      </div>
    </div>
    <div class="row mt">
      <div class="f"><label>Áudio do clipe</label>
        <select id="audioMode" onchange="toggleAudioMode()">
          <option value="original" selected>Original</option>
          <option value="normalize">Normalizar</option>
          <option value="gain">Ajustar volume</option>
          <option value="mute">Sem ÃÂ¡udio</option>
        </select></div>
      <div class="f" id="audioGainWrap" style="display:none"><label>Ganho (dB) <span id="audioGainV" style="color:var(--acc)">0</span></label>
        <input type="range" id="audioGainDb" min="-18" max="18" value="0" oninput="document.getElementById('audioGainV').textContent=this.value"></div>
      <div class="f grow"><label>Faixa de ÃÂ¡udio</label>
        <select id="audioTrack"><option value="0">Faixa 1 (padrÃÂ£o)</option></select></div>
    </div>
    <div id="campanel" style="display:none">
      <div class="camwrap">
        <div class="camsrc" id="camsrc">
          <img id="camimg" alt="">
          <div id="cambox">
            <i data-h="nw"></i><i data-h="ne"></i><i data-h="sw"></i><i data-h="se"></i>
          </div>
        </div>
        <div class="camres">
          <img id="camout" alt="">
          <div class="cap">como vai sair</div>
        </div>
      </div>
      <div class="camctl">
        <button class="ghost sm" onclick="detectarCam()">detectar automÃÂ¡tico</button>
        <label class="chk"><input type="checkbox" id="camMarca" checked
          onchange="camSalvar()"> marca d'ÃÂ¡gua</label>
        <input type="text" id="camTexto" value="VICEMSHORTS" onchange="camSalvar()">
        <label class="chk">cam = <b id="camTopoV" style="color:var(--acc)">45</b>%</label>
        <input type="range" id="camTopo" min="25" max="65" value="45"
               oninput="camTopoMudou(this.value)">
        <span id="camInfo" style="color:var(--dim2)"></span>
      </div>
    </div>
    <div class="preset-row">
      <div class="f" style="min-width:220px"><label>Preset</label><select id="presetSelect" onchange="aplicarPresetSelecionado()"><option value="">- selecionar -</option></select></div>
      <button class="ghost sm" onclick="salvarPreset()">Salvar preset</button>
      <button class="ghost sm" onclick="excluirPreset()">Excluir preset</button>
    </div>
    <div class="expbar">
      <span class="tot" id="selinfo"></span>
      <span class="grow"></span>
      <button class="ghost" onclick="adicionarFilaExportar()">+ fila exportaÃÂ§ÃÂ£o</button>
      <button class="ghost" onclick="abrirPasta()">Abrir pasta</button>
      <button class="ghost danger" onclick="sairApp()">Sair</button>
      <button id="btnExport" onclick="exportar()" disabled>Exportar</button>
    </div>
    <div class="fila-box">
      <div class="fila-head"><div><b style="font-size:12px;letter-spacing:.8px;text-transform:uppercase;color:var(--dim)">Fila</b> <span id="filaStatus" style="font-size:12px;color:var(--dim2)"></span></div><div style="display:flex;gap:8px"><button class="ghost sm" onclick="adicionarFilaAnalise()">+ fila anÃÂ¡lise</button><button class="ghost sm" onclick="limparFila()">Limpar</button></div></div>
      <div class="fila-list" id="filaList"><div class="fila-item">Fila vazia</div></div>
    </div>
  </section>
</div>

  <div class="modal" id="modal">
    <div class="mbox">
      <h3>Arquivos no disco</h3>
      <div class="hint">Cada item ÃÂ© apagado por si. Apagar o vÃÂ­deo nÃÂ£o apaga o ÃÂ¡udio, os clipes ou a transcriÃÂ§ÃÂ£o Ã¢â¬â sÃÂ³ o que vocÃÂª mandar.</div>
      <div class="vfav">
        <input id="reurl" placeholder="link do YouTube pra baixar de novo">
        <button class="ghost sm" onclick="baixarDeNovo()">baixar</button>
      </div>
      <div id="vfalta"></div>
      <div class="disk">
        <span id="dtotal">-</span>
        <span id="dlivre">-</span>
      </div>
      <div id="mlist"></div>
      <div style="margin-top:16px;text-align:right">
        <button class="ghost" onclick="fecharModal()">fechar</button>
      </div>
    </div>
  </div>

  <div class="modal" id="modalBib">
    <div class="mbox" style="width:min(980px,100%)">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px">
        <div>
          <h3 style="margin:0">Biblioteca</h3>
          <div class="hint" style="margin:6px 0 0">VODs baixados e clipes/shorts jÃÂ¡ exportados. Reabra ou abra a pasta direto.</div>
        </div>
        <button class="ghost sm" onclick="fecharBiblioteca()">fechar</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div>
          <div style="font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px">VODs baixados</div>
          <div id="bibVods"></div>
        </div>
        <div>
          <div style="font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px">Clipes / Shorts feitos</div>
          <div id="bibClips"></div>
        </div>
      </div>
    </div>
  </div>

<div id="toasts"></div>

<div class="conf" id="conf">
  <div class="confbox">
    <p id="confmsg"></p>
    <div class="acts">
      <button class="ghost" id="confno">cancelar</button>
      <button class="danger" id="confok">apagar</button>
    </div>
  </div>
</div>

<script>
let cands = [];
let chave = '';
let selecionados = new Set();
let timer = null;

function fmtT(s){
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = Math.floor(s%60);
  return (h?h+"h":"") + String(m).padStart(2,"0") + "m" + String(x).padStart(2,"0") + "s";
}

function toast(msg, tipo){
  const t = document.createElement("div");
  t.className = "toast " + (tipo || "");
  t.textContent = msg;
  const box = document.getElementById("toasts");
  box.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .25s"; }, 3600);
  setTimeout(() => t.remove(), 3900);
}

function toggleAudioOnly(){
  const on = document.getElementById("audioOnly").checked;
  document.getElementById("audioFormat").disabled = !on;
  document.getElementById("audioQuality").disabled = !on;
}

function toggleAudioMode(){
  const mode = document.getElementById("audioMode").value;
  document.getElementById("audioGainWrap").style.display = mode === "gain" ? "block" : "none";
}

async function carregarFaixasAudio(){
  try{
    const r = await fetch("/api/audio-tracks");
    const j = await r.json();
    const sel = document.getElementById("audioTrack");
    const tracks = j.tracks || [];
    sel.innerHTML = "";
    if(!tracks.length){
      sel.innerHTML = '<option value="0">Faixa 1 (padrÃÂ£o)</option>';
      return;
    }
    tracks.forEach((t, idx) => {
      const label = `Faixa ${idx+1}` + (t.lang ? ` [${t.lang}]` : "") + (t.title ? ` - ${t.title}` : "") + (t.codec ? ` (${t.codec})` : "");
      const opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = label;
      sel.appendChild(opt);
    });
  }catch(e){}
}

async function baixarFonte(){
  const fonte = document.getElementById("fonte").value.trim();
  if(!fonte){ toast("Cole um link ou caminho", "erro"); return; }
  if(!/^https?:\/\//i.test(fonte)){ toast("Para baixar direto, use um link http/https", "erro"); return; }
  const body = {
    url: fonte,
    qualidade: +document.getElementById("qualidade").value,
    codec: document.getElementById("codec").value,
    audio_only: document.getElementById("audioOnly").checked,
    audio_format: document.getElementById("audioFormat").value,
    audio_quality: document.getElementById("audioQuality").value,
  };
  document.getElementById("erro").style.display = "none";
  const r = await fetch("/api/baixar", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); return; }
  toast("Download iniciado - acompanhe a barra", "ok");
  iniciarPolling();
}

function confirmar(msg){
  return new Promise(res => {
    const ov = document.getElementById("conf");
    document.getElementById("confmsg").textContent = msg;
    ov.classList.add("on");
    const ok = document.getElementById("confok");
    const no = document.getElementById("confno");
    const fim = v => { ov.classList.remove("on"); ok.onclick = null; no.onclick = null; res(v); };
    ok.onclick = () => fim(true);
    no.onclick = () => fim(false);
  });
}

async function analisar(){
  const fonte = document.getElementById("fonte").value.trim();
  if(!fonte){ toast("Cole um link ou caminho", "erro"); return; }
  const body = {
    fonte,
    n: +document.getElementById("n").value,
    duracao: +document.getElementById("duracao").value,
    modelo: document.getElementById("modelo").value,
    idioma: document.getElementById("idioma").value,
    qualidade: +document.getElementById("qualidade").value,
    codec: document.getElementById("codec").value,
    sensibilidade: +document.getElementById("sens").value,
  };
  document.getElementById("btnAnalisar").disabled = true;
  document.getElementById("erro").style.display = "none";
  const r = await fetch("/api/analisar", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); document.getElementById("btnAnalisar").disabled = false; return; }
  iniciarPolling();
}

let warpBusy = false;
async function atualizarNetwork(){
  try{
    const r = await fetch("/api/network-status");
    const j = await r.json();
    const w = j.warp || {};
    const b = j.bgutil || {};
    const btn = document.getElementById("btnWarp");
    if(btn){
      if(!w.installed) { btn.textContent = "WARP: off"; btn.disabled = true; btn.title = "Cloudflare WARP não instalado"; }
      else { btn.disabled = warpBusy; btn.textContent = w.connected ? "WARP: ON" : "WARP: OFF"; btn.title = "Clique para conectar/desconectar Cloudflare WARP (troca IP)"; }
    }
    const ns = document.getElementById("netstatus");
    if(ns){
      const parts = [];
      parts.push("WARP: " + (w.installed ? (w.connected ? "conectado" : "desconectado") : "não instalado"));
      parts.push("bgutil: " + (b.plugin ? (b.provider ? "ativo" : "plugin sem provider") : "não instalado"));
      ns.style.display = "block";
      ns.innerHTML = parts.map(p => "<div>" + p + "</div>").join("");
    }
  }catch(e){}
}

toggleAudioOnly();
toggleAudioMode();

async function toggleWarp(){
  if(warpBusy) return;
  warpBusy = true;
  const btn = document.getElementById("btnWarp");
  if(btn) btn.disabled = true;
  try{
    const st = await (await fetch("/api/network-status")).json();
    const connected = !!(st.warp && st.warp.connected);
    const rota = connected ? "/api/warp/disconnect" : "/api/warp/connect";
    if(btn) btn.textContent = connected ? "WARP..." : "WARP...";
    const r = await fetch(rota, {method:"POST"});
    const j = await r.json();
    if(j.erro || j.ok === false) toast(j.erro || "falha no WARP", "erro");
    else toast(connected ? "WARP desconectado" : "WARP conectado", "ok");
  }catch(e){ toast("falha no WARP", "erro"); }
  warpBusy = false;
  atualizarNetwork();
}

const logLinhas = [];
function pushLog(txt){
  if(!txt) return;
  if(logLinhas[logLinhas.length-1] === txt) return;
  logLinhas.push(txt);
  if(logLinhas.length > 5) logLinhas.shift();
  document.getElementById("logmini").innerHTML = logLinhas
    .map(l => "<div>" + l.replace(/&/g,"&amp;").replace(/</g,"&lt;") + "</div>").join("");
}

function iniciarPolling(){
  if(timer) clearInterval(timer);
  timer = setInterval(async () => {
    const s = await (await fetch("/api/status")).json();
    document.getElementById("prog").style.width = (s.pct||0) + "%";
    document.getElementById("pct").textContent = s.rodando ? (s.pct||0) + "%" : "";
    const linha = s.etapa + (s.msg ? " - " + s.msg : "");
    document.getElementById("status").textContent = linha;
    pushLog(linha);
    document.getElementById("chip").classList.toggle("on", !!s.rodando);
    document.getElementById("chiptxt").textContent =
      s.rodando ? s.etapa : (s.erro ? "erro" : "parado");
    if(s.erro){
      const e = document.getElementById("erro");
      e.style.display = "block"; e.textContent = s.erro;
    }
    if(!s.rodando){
      clearInterval(timer); timer = null;
      document.getElementById("btnAnalisar").disabled = false;
      document.getElementById("btnExport").disabled = selecionados.size === 0;
      if(s.etapa === "pronto" && s.n) carregarCandidatos();
    }
  }, 900);
}

let durVideo = 0;

const PRESETS_EMBUTIDOS = {
  "Shorts Viral (9:16)": {n:12, duracao:60, modelo:"small", idioma:"pt", qualidade:1080, codec:"avc", sensibilidade:6, formatos:["916"], modo:"cam", crf:18, legenda:true, audio_mode:"normalize", audio_track:0, audio_gain_db:0},
  "Reels/TikTok (9:16)": {n:10, duracao:45, modelo:"small", idioma:"pt", qualidade:1080, codec:"avc", sensibilidade:5, formatos:["916"], modo:"blur", crf:18, legenda:true, audio_mode:"normalize", audio_track:0, audio_gain_db:0},
  "YouTube (16:9)": {n:8, duracao:90, modelo:"small", idioma:"pt", qualidade:1080, codec:"avc", sensibilidade:4, formatos:["169"], modo:"crop", crf:20, legenda:false, audio_mode:"original", audio_track:0, audio_gain_db:0},
  "Quadrado (1:1)": {n:10, duracao:45, modelo:"small", idioma:"pt", qualidade:1080, codec:"avc", sensibilidade:5, formatos:["11"], modo:"blur", crf:20, legenda:true, audio_mode:"normalize", audio_track:0, audio_gain_db:0},
  "SÃÂ³ ÃÂ¡udio (MP3)": {n:1, duracao:0, modelo:"small", idioma:"pt", qualidade:1080, codec:"avc", sensibilidade:5, formatos:["916"], modo:"blur", crf:23, legenda:false, audio_mode:"original", audio_track:0, audio_gain_db:0, _download_audio_only:true, _audio_format:"mp3", _audio_quality:"192"}
};

function configAtual(){
  return {
    n:+document.getElementById('n').value, duracao:+document.getElementById('duracao').value,
    modelo:document.getElementById('modelo').value, idioma:document.getElementById('idioma').value,
    qualidade:+document.getElementById('qualidade').value, codec:document.getElementById('codec').value,
    sensibilidade:+document.getElementById('sens').value, formatos:[...document.querySelectorAll('#fmtos .pill.on')].map(p=>p.dataset.f),
    modo:document.getElementById('modo').value, crf:+document.getElementById('crf').value, legenda:document.getElementById('legenda').checked,
    audio_mode:document.getElementById('audioMode').value, audio_track:+document.getElementById('audioTrack').value||0, audio_gain_db:+document.getElementById('audioGainDb').value||0
  };
}

async function carregarPresets(){
  try{
    const j = await (await fetch('/api/presets')).json();
    const sel = document.getElementById('presetSelect'); const cur = sel.value;
    sel.innerHTML = '<option value="">- selecionar -</option>';
    const builtins = PRESETS_EMBUTIDOS;
    const user = j.presets || {};
    const og1 = document.createElement('optgroup'); og1.label='Embutidos';
    Object.keys(builtins).forEach(nome=>{ const o=document.createElement('option'); o.value='builtin:'+nome; o.textContent=nome; og1.appendChild(o); });
    sel.appendChild(og1);
    const ukeys = Object.keys(user).sort();
    if(ukeys.length){ const og2=document.createElement('optgroup'); og2.label='Meus presets'; ukeys.forEach(nome=>{ const o=document.createElement('option'); o.value='user:'+nome; o.textContent=nome; og2.appendChild(o); }); sel.appendChild(og2); }
    if(cur) sel.value = cur;
    window.__presets = user;
  }catch(e){}
}

function aplicarPresetSelecionado(){
  const val = document.getElementById('presetSelect').value; if(!val) return;
  let p = null;
  if(val.startsWith('builtin:')) p = PRESETS_EMBUTIDOS[val.slice(8)];
  else if(val.startsWith('user:')) p = (window.__presets||{})[val.slice(5)];
  else p = (window.__presets||{})[val] || PRESETS_EMBUTIDOS[val];
  if(!p) return;
  const set = (id,v)=>{const el=document.getElementById(id); if(el!=null && v!=null) el.value=v;};
  set('n',p.n); set('duracao',p.duracao); set('modelo',p.modelo); set('idioma',p.idioma); set('qualidade',p.qualidade); set('codec',p.codec); set('sens',p.sensibilidade);
  if(p.sensibilidade!=null) document.getElementById('sensv').textContent=p.sensibilidade;
  if(Array.isArray(p.formatos)){ document.querySelectorAll('#fmtos .pill').forEach(el=>el.classList.toggle('on', p.formatos.includes(el.dataset.f))); }
  set('modo',p.modo); set('crf',p.crf); if(p.legenda!=null) document.getElementById('legenda').checked=!!p.legenda;
  set('audioMode',p.audio_mode); set('audioGainDb',p.audio_gain_db); if(p.audio_gain_db!=null) document.getElementById('audioGainV').textContent=p.audio_gain_db;
  if(p._download_audio_only){ document.getElementById('audioOnly').checked=true; if(p._audio_format) document.getElementById('audioFormat').value=p._audio_format; if(p._audio_quality) document.getElementById('audioQuality').value=p._audio_quality; toggleAudioOnly(); }
  toggleAudioMode(); modoMudou(); atualizarSel();
}

async function salvarPreset(){
  const nome = prompt('Nome do preset:'); if(!nome) return;
  const r = await fetch('/api/presets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nome,config:configAtual()})});
  const j = await r.json(); if(j.erro){toast(j.erro,'erro');return;} window.__presets=j.presets||{}; await carregarPresets(); document.getElementById('presetSelect').value=nome; toast('Preset salvo','ok');
}
async function excluirPreset(){
  const val = document.getElementById('presetSelect').value; if(!val){toast('Selecione um preset','erro');return;}
  if(val.startsWith('builtin:')){ toast('Presets embutidos nÃÂ£o podem ser excluÃÂ­dos','erro'); return; }
  const nome = val.startsWith('user:') ? val.slice(5) : val;
  if(!await confirmar(`Excluir preset "${nome}"?`)) return;
  const r = await fetch('/api/presets/'+encodeURIComponent(nome),{method:'DELETE'}); const j=await r.json(); window.__presets=j.presets||{}; await carregarPresets(); toast('Preset excluÃÂ­do','ok');
}

async function atualizarFila(){
  try{
    const j = await (await fetch('/api/fila')).json();
    const list = document.getElementById('filaList');
    const itens = j.itens || [];
    document.getElementById('filaStatus').textContent = j.ativa ? ` • ativa (${itens.filter(i=>i.status!=='concluido').length} pendente/rodando)` : '';
    if(!itens.length){ list.innerHTML='<div class="fila-item">Fila vazia</div>'; return; }
    list.innerHTML = itens.slice(-20).map(i=>`<div class="fila-item"><b>${i.status}</b><span>${i.tipo}</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(i.msg||'').replace(/</g,'&lt;')}</span></div>`).join('');
  }catch(e){}
}

async function adicionarFilaAnalise(){
  const fonte = document.getElementById('fonte').value.trim(); if(!fonte){toast('Cole fonte para enfileirar','erro');return;}
  const cfg = {...configAtual(), fonte};
  const r = await fetch('/api/fila',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({itens:[{tipo:'analisar',config:cfg}]})});
  const j = await r.json(); if(j.erro){toast(j.erro,'erro');return;} toast('AnÃÂ¡lise adicionada ÃÂ  fila','ok'); atualizarFila();
}
async function adicionarFilaExportar(){
  const ids=[...selecionados]; if(!ids.length){toast('Selecione trechos','erro');return;}
  const cfg = {...configAtual(), itens:ids};
  const r = await fetch('/api/fila',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({itens:[{tipo:'exportar',config:cfg}]})});
  const j = await r.json(); if(j.erro){toast(j.erro,'erro');return;} toast('ExportaÃÂ§ÃÂ£o adicionada ÃÂ  fila','ok'); atualizarFila();
}
async function limparFila(){ await fetch('/api/fila/limpar',{method:'POST'}); atualizarFila(); }

async function carregarCandidatos(){
  const d = await (await fetch("/api/candidatos")).json();
  cands = d.itens || [];
  chave = d.chave || '';
  durVideo = d.dur || 0;
  selecionados = new Set(cands.map(c => c.id));
  document.getElementById("cont").textContent = d.titulo || "";
  carregarFaixasAudio();
  render();
  if(document.getElementById("modo").value === "cam") camInit();
}

function card(id){ return document.querySelector(`.clip[data-id="${id}"]`); }

function render(){
  const g = document.getElementById("grid");
  if(!cands.length){ g.innerHTML = '<div class="empty">Nada analisado ainda.</div>'; atualizarSel(); return; }
  g.innerHTML = "";
  cands.forEach(c => {
    const ok60 = Math.abs((+c.dur || 0) - 60) < 0.6;
    const el = document.createElement("div");
    el.className = "clip" + (selecionados.has(c.id) ? " sel" : "");
    el.dataset.id = c.id;
    el.innerHTML = `
      <div class="thumb" onclick="ver(${c.id}, this)">
        <img src="/thumb/${chave}/${c.id}" loading="lazy">
        <div class="badge">${fmtT(c.inicio)} - ${fmtT(c.fim)}</div>
        ${c.score ? `<div class="badge r">${Math.round(c.score)}</div>` : ""}
        <div class="badge b" data-role="dur">${(+c.dur).toFixed(1)}s</div>
        <div class="play">&#9654;</div>
      </div>
      <div class="body">
        <div class="meta">
          <span class="time" data-role="time">${fmtT(c.inicio)} - ${fmtT(c.fim)}</span>
          <span class="durbadge ${ok60?'ok':''}" data-role="badge">${(+c.dur).toFixed(1)}s</span>
        </div>
        <div class="txt" title="${(c.texto||"").slice(0,400).replace(/"/g,"&quot;")}">${(c.texto||"").slice(0,180)}</div>
        <div class="editor">
          <div><label>InÃÂ­cio (s)</label><input type="number" step="0.1" value="${c.inicio}" id="i${c.id}" onchange="syncDur(${c.id},'i')"></div>
          <div><label>Fim (s)</label><input type="number" step="0.1" value="${c.fim}" id="f${c.id}" onchange="syncDur(${c.id},'f')"></div>
          <div style="grid-column:1/-1"><label>DuraÃÂ§ÃÂ£o alvo (s)</label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="number" step="1" value="${Math.round(c.dur||60)}" id="d${c.id}" onchange="syncDur(${c.id},'d')" style="max-width:110px">
              <button class="ghost sm" onclick="aplicarDuracao(${c.id},60)">60s exato</button>
              <button class="ghost sm" onclick="centralizar(${c.id})">centralizar</button>
            </div>
          </div>
        </div>
        <div class="nudge">
          <button class="ghost sm" onclick="nudge(${c.id},-5)">-5s</button>
          <button class="ghost sm" onclick="nudge(${c.id},-1)">-1s</button>
          <button class="ghost sm" onclick="nudge(${c.id},-0.5)">-0.5s</button>
          <button class="ghost sm" onclick="nudge(${c.id},0.5)">+0.5s</button>
          <button class="ghost sm" onclick="nudge(${c.id},1)">+1s</button>
          <button class="ghost sm" onclick="nudge(${c.id},5)">+5s</button>
        </div>
        <div class="foot">
          <label class="chk"><input type="checkbox" ${selecionados.has(c.id)?"checked":""}
            onchange="marcar(${c.id}, this.checked)"> selecionar</label>
          <div class="acts">
            <button class="ghost sm" onclick="ver(${c.id}, card(${c.id}).querySelector('.thumb'))">prÃÂ©via</button>
            <button class="sm acc" onclick="ajustar(${c.id})">salvar corte</button>
          </div>
        </div>
      </div>`;
    g.appendChild(el);
  });
  atualizarSel();
}

function syncDur(id, origem){
  const ii = document.getElementById("i"+id), fi = document.getElementById("f"+id), di = document.getElementById("d"+id);
  let ini = +ii.value, fim = +fi.value, dur = +di.value;
  if(origem === 'i' || origem === 'f'){
    if(fim <= ini) fim = ini + (dur > 0 ? dur : 60);
    dur = fim - ini;
  } else {
    const centro = (ini + fim) / 2;
    if(!(dur > 0)) dur = 60;
    ini = centro - dur/2; fim = centro + dur/2;
  }
  if(ini < 0){ ini = 0; fim = dur; }
  if(durVideo > 0 && fim > durVideo){ fim = durVideo; ini = Math.max(0, fim - dur); }
  ii.value = (+ini).toFixed(1); fi.value = (+fim).toFixed(1); di.value = Math.max(1, Math.round(fim-ini));
  atualizarCardInfo(id, +ii.value, +fi.value);
}

function atualizarCardInfo(id, ini, fim){
  const el = card(id); if(!el) return;
  const dur = +(fim-ini).toFixed(1);
  const ok60 = Math.abs(dur - 60) < 0.6;
  const b = el.querySelector('[data-role="dur"]'); if(b) b.textContent = dur.toFixed(1)+"s";
  const t = el.querySelector('[data-role="time"]'); if(t) t.textContent = fmtT(ini)+" - "+fmtT(fim);
  const bg = el.querySelector('[data-role="badge"]'); if(bg){ bg.textContent = dur.toFixed(1)+"s"; bg.classList.toggle("ok", ok60); }
  const top = el.querySelector('.badge'); if(top) top.textContent = fmtT(ini)+" - "+fmtT(fim);
  const c = cands.find(x=>x.id===id); if(c){ c.inicio = ini; c.fim = fim; c.dur = dur; }
}

function nudge(id, delta){
  const ii = document.getElementById("i"+id), fi = document.getElementById("f"+id);
  let ini = +ii.value + delta, fim = +fi.value + delta, dur = fim - ini;
  if(ini < 0){ ini = 0; fim = dur; }
  if(durVideo > 0 && fim > durVideo){ fim = durVideo; ini = Math.max(0, fim - dur); }
  ii.value = (+ini).toFixed(1); fi.value = (+fim).toFixed(1);
  atualizarCardInfo(id, +ii.value, +fi.value);
}

function centralizar(id){
  const ii = document.getElementById("i"+id), fi = document.getElementById("f"+id), di = document.getElementById("d"+id);
  let dur = +di.value || (+fi.value - +ii.value) || 60;
  const c = cands.find(x=>x.id===id);
  const centro = c ? (c.inicio + c.fim)/2 : (+ii.value + +fi.value)/2;
  let ini = centro - dur/2, fim = centro + dur/2;
  if(ini < 0){ ini = 0; fim = dur; }
  if(durVideo > 0 && fim > durVideo){ fim = durVideo; ini = Math.max(0, fim - dur); }
  ii.value = (+ini).toFixed(1); fi.value = (+fim).toFixed(1); di.value = Math.max(1, Math.round(fim-ini));
  atualizarCardInfo(id, +ii.value, +fi.value);
}

function aplicarDuracao(id, alvo){
  const di = document.getElementById("d"+id);
  di.value = alvo;
  syncDur(id, 'd');
}

async function salvarTodosAjustes(){
  if(!cands.length) return;
  let ok = 0;
  for(const c of cands){
    const ii = document.getElementById("i"+c.id), fi = document.getElementById("f"+c.id);
    if(!ii || !fi) continue;
    const inicio = +ii.value, fim = +fi.value;
    const r = await fetch("/api/ajustar", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({id:c.id, inicio, fim})});
    const j = await r.json();
    if(!j.erro){
      c.inicio = +inicio.toFixed(1); c.fim = +fim.toFixed(1); c.dur = +(fim-inicio).toFixed(1);
      atualizarCardInfo(c.id, c.inicio, c.fim);
      ok++;
    }
  }
  toast(`Salvos ${ok} cortes`, ok ? "ok" : "erro");
}

async function aplicarDuracaoTodos(alvo){
  if(!cands.length) return;
  for(const c of cands){ aplicarDuracao(c.id, alvo); }
  await salvarTodosAjustes();
  toast(`Todos ajustados e salvos em ~${alvo}s`, "ok");
}

function ver(id, box){
  const el = (typeof box === 'string') ? document.querySelector(box) : box;
  const alvo = el || card(id)?.querySelector('.thumb');
  if(!alvo) return;
  const modo = document.getElementById("modo").value;
  alvo.innerHTML = `<video controls autoplay src="/preview/${chave}/${id}?modo=${modo}&t=${Date.now()}"></video>`;
}

function marcarTodos(v){
  selecionados = v ? new Set(cands.map(c => c.id)) : new Set();
  render();
}

function marcar(id, on){
  on ? selecionados.add(id) : selecionados.delete(id);
  const el = card(id);
  if(el) el.classList.toggle("sel", on);
  atualizarSel();
}

function atualizarSel(){
  const fmtos = document.querySelectorAll("#fmtos .pill.on").length;
  const total = selecionados.size * fmtos;
  const txt = selecionados.size + " de " + cands.length + " selecionados" +
    (total ? " - " + total + " arquivo" + (total>1?"s":"") : "");
  document.getElementById("selinfo").textContent = txt;
  const sr = document.getElementById("selresumo"); if(sr) sr.textContent = txt;
  const b = document.getElementById("btnExport");
  b.disabled = selecionados.size === 0;
  b.textContent = selecionados.size ? "Exportar " + selecionados.size : "Exportar";
}

async function ajustar(id){
  const inicio = +document.getElementById("i"+id).value;
  const fim = +document.getElementById("f"+id).value;
  const duracao = +(fim - inicio).toFixed(1);
  const r = await fetch("/api/ajustar", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id, inicio, fim})});
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); return; }
  const c = cands.find(x=>x.id===id);
  if(c){ c.inicio = +inicio.toFixed(1); c.fim = +fim.toFixed(1); c.dur = duracao; }
  atualizarCardInfo(id, c.inicio, c.fim);
  toast(`Corte salvo (${duracao}s)`);
}

async function exportar(){
  const fmtos = [...document.querySelectorAll("#fmtos .pill.on")].map(p=>p.dataset.f);
  if(!fmtos.length){ toast("Escolha ao menos um formato", "erro"); return; }
  const body = {
    itens: [...selecionados],
    formatos: fmtos,
    modo: document.getElementById("modo").value,
    crf: +document.getElementById("crf").value,
    legenda: document.getElementById("legenda").checked,
    audio_mode: document.getElementById("audioMode").value,
    audio_track: +document.getElementById("audioTrack").value || 0,
    audio_gain_db: +document.getElementById("audioGainDb").value || 0
  };
  document.getElementById("btnExport").disabled = true;
  const r = await fetch("/api/exportar", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); document.getElementById("btnExport").disabled = false; return; }
  iniciarPolling();
}

async function abrirPasta(){
  const s = await (await fetch("/api/status")).json();
  await fetch("/api/abrir-pasta", {method:"POST", headers:{"Content-Type":"application/json"}, body: "{}"});
}

async function sairApp(){
  if(!await confirmar("Encerrar o Cortes? O painel vai fechar.")) return;
  try{
    const r = await fetch("/api/sair", {method:"POST"});
    const j = await r.json();
    if(j.erro){ toast(j.erro, "erro"); return; }
    toast("Encerrando...", "ok");
    document.body.innerHTML = '<div style="min-height:100vh;display:grid;place-items:center;background:#0c0e14;color:#f2f3f7;font-family:Inter,system-ui,sans-serif"><div style="text-align:center"><h2 style="margin:0 0 8px">Cortes encerrado</h2><p style="margin:0;color:#9ca3b4">Pode fechar esta aba.</p></div></div>';
  }catch(e){
    toast("Encerrando...", "ok");
  }
}

async function listarVods(){
  document.getElementById("modal").classList.add("on");
  renderArquivos();
}

function tam(b){
  if(b >= 1e9) return (b/1e9).toFixed(2) + " GB";
  if(b >= 1e6) return (b/1e6).toFixed(0) + " MB";
  if(b >= 1e3) return (b/1e3).toFixed(0) + " KB";
  return b + " B";
}

async function renderArquivos(){
  const box = document.getElementById("mlist");
  box.innerHTML = '<div class="empty">lendo disco...</div>';
  const d = await (await fetch("/api/arquivos")).json();
  box.innerHTML = "";

  document.getElementById("dtotal").textContent =
    "EspaÃÂ§o usado pelos arquivos: " + tam(d.total_bytes);
  document.getElementById("dlivre").textContent =
    "EspaÃÂ§o livre no disco: " + tam(d.livre_bytes);

  renderFaltando(d.historico || []);

  if(!d.grupos.length){
    box.innerHTML = '<div class="empty">Nada no disco.</div>';
    return;
  }

  d.grupos.forEach(g => {
    const el = document.createElement("div");
    el.className = "vgroup";

    const head = document.createElement("div");
    head.className = "vhead";
    const b = document.createElement("b");
    b.textContent = g.titulo;
    if(g.tamanho) b.title = g.tamanho;
    const t = document.createElement("span");
    t.textContent = tam(g.bytes);
    head.append(b, t);
    el.appendChild(head);

    g.itens.forEach(it => {
      const row = document.createElement("div");
      row.className = "vitem";

      const rot = document.createElement("div");
      rot.className = "rot";
      rot.textContent = it.rotulo;

      const sz = document.createElement("div");
      sz.className = "sz";
      sz.textContent = tam(it.bytes);

      row.append(rot, sz);

      if(it.tipo === "video"){
        const usar = document.createElement("button");
        usar.className = "ghost sm";
        usar.textContent = "usar";
        usar.onclick = () => {
          document.getElementById("fonte").value = g.tamanho;
          fecharModal();
        };
        row.appendChild(usar);
      }

      const del = document.createElement("button");
      del.className = "danger sm";
      del.textContent = "apagar";
      del.onclick = () => apagarItem(it, del);
      row.appendChild(del);

      el.appendChild(row);
    });

    box.appendChild(el);
  });
}

function renderFaltando(lista){
  const box = document.getElementById("vfalta");
  box.innerHTML = "";
  const falta = (lista || []).filter(x => !x.existe && x.url);
  falta.forEach(x => {
    const row = document.createElement("div");
    row.className = "vfalta";

    const t = document.createElement("span");
    t.textContent = "apagado: " + x.titulo;
    t.title = x.titulo;
    t.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";

    const b = document.createElement("button");
    b.className = "ghost sm";
    b.textContent = "baixar de novo";
    b.onclick = () => baixarUrl(x.url, b);

    row.append(t, b);
    box.appendChild(row);
  });
}

async function baixarUrl(url, btn){
  if(btn) btn.disabled = true;
  document.getElementById("erro").style.display = "none";
  const body = {
    url, qualidade:1440, codec:"vp9",
    audio_only: document.getElementById("audioOnly")?.checked || false,
    audio_format: document.getElementById("audioFormat")?.value || "mp3",
    audio_quality: document.getElementById("audioQuality")?.value || "192",
  };
  const r = await fetch("/api/baixar", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)
  });
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); if(btn) btn.disabled = false; return; }
  toast("baixando - acompanhe a barra", "ok");
  fecharModal();
  iniciarPolling();
}

async function baixarDeNovo(){
  const inp = document.getElementById("reurl");
  const url = inp.value.trim();
  if(!url){ toast("cola o link primeiro", "erro"); return; }
  inp.value = "";
  await baixarUrl(url, null);
}

async function apagarItem(it, btn){
  if(!await confirmar("Apagar " + it.rotulo + " (" + tam(it.bytes) + ")?\n\nSó esse item, o resto fica.")) return;
  btn.disabled = true;

  const enviar = async (body) => (await fetch("/api/apagar-item", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)
  })).json();

  let j = await enviar({id: it.id, confirmar: true});

  if(j.erro === "protegido"){
    if(!await confirmar("Este é o vídeo do último projeto analisado.\n\nApagar mesmo assim?")){
      btn.disabled = false;
      return;
    }
    j = await enviar({id: it.id, confirmar: true, forcar: true});
  }

  if(j.erro){ toast(j.erro, "erro"); btn.disabled = false; return; }
  renderArquivos();
}

function fecharModal(){
  document.getElementById("modal").classList.remove("on");
}

function abrirBiblioteca(){
  document.getElementById("modalBib").classList.add("on");
  renderBiblioteca();
}
function fecharBiblioteca(){
  document.getElementById("modalBib").classList.remove("on");
}

async function renderBiblioteca(){
  const vbox = document.getElementById("bibVods");
  const cbox = document.getElementById("bibClips");
  vbox.innerHTML = '<div class="empty">lendo...</div>';
  cbox.innerHTML = '<div class="empty">lendo...</div>';
  try{
    const [arq, vods] = await Promise.all([
      fetch('/api/arquivos').then(r=>r.json()),
      fetch('/api/vods').then(r=>r.json()),
    ]);
    vbox.innerHTML = '';
    (vods||[]).forEach(v=>{
      const row = document.createElement('div'); row.className='vitem';
      const rot = document.createElement('div'); rot.className='rot'; rot.textContent = v.nome;
      const sz = document.createElement('div'); sz.className='sz'; sz.textContent = v.gb + ' GB';
      const usar = document.createElement('button'); usar.className='ghost sm'; usar.textContent='usar';
      usar.onclick = ()=>{ document.getElementById('fonte').value = v.caminho; fecharBiblioteca(); toast('VOD carregado no campo fonte','ok'); };
      row.append(rot,sz,usar); vbox.appendChild(row);
    });
    if(!vbox.children.length) vbox.innerHTML='<div class="empty">Nenhum VOD baixado.</div>';

    cbox.innerHTML = '';
    (arq.grupos||[]).filter(g=>g.itens && g.itens.some(i=>i.tipo==='clipes')).forEach(g=>{
      const clipItem = g.itens.find(i=>i.tipo==='clipes');
      const row = document.createElement('div'); row.className='vitem';
      const rot = document.createElement('div'); rot.className='rot'; rot.textContent = g.titulo;
      const sz = document.createElement('div'); sz.className='sz'; sz.textContent = tam(g.bytes);
      const abrir = document.createElement('button'); abrir.className='ghost sm'; abrir.textContent='abrir pasta';
      abrir.onclick = async ()=>{ if(!clipItem) return; await fetch('/api/abrir-pasta',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pasta: clipItem.caminho})}); };
      row.append(rot,sz,abrir); cbox.appendChild(row);
    });
    if(!cbox.children.length) cbox.innerHTML='<div class="empty">Nenhum clipe exportado ainda.</div>';
  }catch(e){
    vbox.innerHTML='<div class="empty">Erro ao carregar biblioteca.</div>';
    cbox.innerHTML='';
  }
}

document.querySelectorAll("#fmtos .pill").forEach(p=>{
  p.onclick = () => { p.classList.toggle("on"); atualizarSel(); };
});

// ---------------------------------------------------- cam em cima (shorts)
let CAMCFG = null;

function modoMudou(){
  const m = document.getElementById("modo").value;
  document.getElementById("campanel").style.display = m === "cam" ? "block" : "none";
  if(m === "cam") camInit();
}

async function camInit(){
  try {
    CAMCFG = await (await fetch("/api/cam")).json();
  } catch(e){ return; }
  camMostrar();
  document.getElementById("camimg").src = "/fonteframe?t=" + Date.now();
  camOut();
}

function camMostrar(){
  if(!CAMCFG) return;
  const b = document.getElementById("cambox");
  b.style.left = (CAMCFG.x*100) + "%";
  b.style.top = (CAMCFG.y*100) + "%";
  b.style.width = (CAMCFG.w*100) + "%";
  b.style.height = (CAMCFG.h*100) + "%";
  document.getElementById("camMarca").checked = !!CAMCFG.marca;
  document.getElementById("camTexto").value = CAMCFG.texto || "VICEMSHORTS";
  const pct = Math.round((CAMCFG.topo || 0.45) * 100);
  document.getElementById("camTopo").value = pct;
  document.getElementById("camTopoV").textContent = pct;
  document.getElementById("camInfo").textContent =
    "x " + Math.round(CAMCFG.x*100) + "% ÃÂ· y " + Math.round(CAMCFG.y*100) +
    "% ÃÂ· w " + Math.round(CAMCFG.w*100) + "% ÃÂ· h " + Math.round(CAMCFG.h*100) + "%";
}

function camOut(){
  document.getElementById("camout").src = "/camlayout?t=" + Date.now();
}

async function camSalvar(){
  if(!CAMCFG) return;
  CAMCFG.marca = document.getElementById("camMarca").checked;
  CAMCFG.texto = document.getElementById("camTexto").value.trim() || "VICEMSHORTS";
  CAMCFG.topo = +document.getElementById("camTopo").value / 100;
  const r = await fetch("/api/cam", {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify(CAMCFG)});
  CAMCFG = await r.json();
  camMostrar();
  camOut();
}

let camTopoTimer = null;
function camTopoMudou(v){
  document.getElementById("camTopoV").textContent = v;
  if(CAMCFG) CAMCFG.topo = +v / 100;
  clearTimeout(camTopoTimer);
  camTopoTimer = setTimeout(camSalvar, 450);
}

async function detectarCam(){
  const btns = document.querySelectorAll("#campanel button");
  const btn = btns[0];
  btn.disabled = true; btn.textContent = "procurando...";
  try {
    const r = await fetch("/api/detectar-cam", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: "{}"});
    const j = await r.json();
    if(j.erro){ toast(j.erro, "erro"); return; }
    CAMCFG = j;
    camMostrar(); camOut();
    toast("cam encontrada - ajuste arrastando se precisar");
  } finally {
    btn.disabled = false; btn.textContent = "detectar automÃÂ¡tico";
  }
}

(function camArrastar(){
  const box = document.getElementById("cambox");
  const src = document.getElementById("camsrc");
  let modo = null, ini = null, base = null;

  box.addEventListener("pointerdown", e => {
    if(!CAMCFG) return;
    modo = e.target.dataset.h ? e.target.dataset.h : "move";
    const r = src.getBoundingClientRect();
    ini = {mx: e.clientX, my: e.clientY, w: r.width, h: r.height};
    base = {x: CAMCFG.x, y: CAMCFG.y, w: CAMCFG.w, h: CAMCFG.h};
    try { box.setPointerCapture(e.pointerId); } catch(err){}
    e.preventDefault();
  });

  box.addEventListener("pointermove", e => {
    if(!modo) return;
    const dx = (e.clientX - ini.mx) / ini.w;
    const dy = (e.clientY - ini.my) / ini.h;
    let x = base.x, y = base.y, w = base.w, h = base.h;
    if(modo === "move"){
      x += dx; y += dy;
    } else {
      if(modo.includes("w")){ x += dx; w -= dx; }
      if(modo.includes("e")){ w += dx; }
      if(modo.includes("n")){ y += dy; h -= dy; }
      if(modo.includes("s")){ h += dy; }
    }
    x = Math.max(0, Math.min(1 - 0.02, x));
    y = Math.max(0, Math.min(1 - 0.02, y));
    w = Math.max(0.03, Math.min(w, 1 - x));
    h = Math.max(0.03, Math.min(h, 1 - y));
    CAMCFG = Object.assign({}, CAMCFG, {x, y, w, h});
    camMostrar();
  });

  box.addEventListener("pointerup", () => {
    if(!modo) return;
    modo = null;
    camSalvar();
  });
})();

(async () => {
  try {
    atualizarNetwork(); carregarPresets(); atualizarFila();
    setInterval(atualizarNetwork, 15000); setInterval(atualizarFila, 5000);
    const st = await (await fetch("/api/status")).json();
    if(st && st.rodando){
      document.getElementById("prog").style.width = (st.pct||0) + "%";
      document.getElementById("pct").textContent = (st.pct||0) + "%";
      iniciarPolling();
    }
    if(st && st.n) await carregarCandidatos();
  } catch(e) {}
})();
</script>
</body>
</html>
"""


def _restaurar_ultimo():
    """Restaura ultimo video/projeto para continuar sem reanalisar."""
    try:
        data = _ler_ultimo() or {}
        src = data.get("src")
        if not src or not os.path.exists(src):
            return
        projeto = data.get("projeto") or {}
        with LOCK:
            if projeto.get("src") and os.path.abspath(projeto.get("src")) == os.path.abspath(src) and projeto.get("candidatos"):
                PROJETO.update({
                    "src": src,
                    "dims": projeto.get("dims") or N.ffprobe_dims(src),
                    "titulo": projeto.get("titulo") or os.path.splitext(os.path.basename(src))[0],
                    "dur": float(projeto.get("dur") or N.ffprobe_dur(src)),
                    "segmentos": projeto.get("segmentos") or [],
                    "candidatos": projeto.get("candidatos") or [],
                    "chave": projeto.get("chave") or _chave(src),
                })
            else:
                PROJETO.update(src=src, dims=N.ffprobe_dims(src),
                               titulo=os.path.splitext(os.path.basename(src))[0],
                               dur=N.ffprobe_dur(src), chave=_chave(src),
                               segmentos=[], candidatos=[])
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    _restaurar_ultimo()
    url = "http://127.0.0.1:8765"
    print(f"\n  Painel rodando em {url}\n  Ctrl+C para parar\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
