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

# ---- config da cam (layout de shorts) + marca d'agua
CAM_ARQ = os.path.join(BASE, "cam.json")
CAM_PADRAO = {"x": 0.018, "y": 0.011, "w": 0.168, "h": 0.333, "topo": 0.45,
              "marca": True, "texto": "VICEMSHORTS"}


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
    return {"texto": (CAM.get("texto") or "VICEMSHORTS").strip(),
            "opac": float(CAM.get("opac", 0.85))}


def _chave(src):
    """Identidade do video: caminho + tamanho + data. Evita que o cache de
    thumb/preview de um video seja servido para outro."""
    import hashlib
    st = os.stat(src)
    bruto = f"{src}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")
    return hashlib.md5(bruto).hexdigest()[:10]


ESTADO_ARQ = os.path.join(BASE, "estado.json")


def _ler_ultimo():
    try:
        with open(ESTADO_ARQ, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _salvar_ultimo(src):
    try:
        with open(ESTADO_ARQ, "w", encoding="utf-8") as f:
            json.dump({"src": src, "quando": time.time()}, f)
    except Exception:
        pass


def _protegidos():
    """Videos que nao podem ser apagados sem confirmacao extra."""
    p = set()
    if PROJETO.get("src"):
        p.add(os.path.abspath(PROJETO["src"]))
    ultimo = _ler_ultimo().get("src")
    if ultimo:
        p.add(os.path.abspath(ultimo))
    return p


def _log(msg):
    ESTADO["msg"] = str(msg).strip()
    print(msg, flush=True)


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


# --------------------------------------------------------------- pipeline

def _job_analisar(fonte, opcoes):
    try:
        _set(rodando=True, erro=None, pct=2, etapa="fonte", msg="preparando...")
        with LOCK:
            PROJETO.update({"src": None, "dims": None, "titulo": "", "dur": 0.0,
                            "segmentos": [], "candidatos": [], "chave": ""})

        cookies_file = opcoes.get("cookies_file")
        if re.match(r"^https?://", fonte):
            _set(5, "download", "baixando video do YouTube...")
            src, info = N.baixar(fonte, opcoes["qualidade"], opcoes["codec"],
                                 cookies_file=cookies_file)
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
                segmentos = N.transcrever(wav, opcoes["modelo"], "pt", "auto",
                                          cache=seg_path)
            finally:
                parar.set()

        _set(85, "picos", "procurando os melhores momentos...")
        y, sr = N.ler_wav(wav)
        tempos, score = N.detectar_picos(y, sr)
        del y
        picos = N.escolher_candidatos(tempos, score, minimo_db=opcoes["limiar"])
        cortes = N.montar_cortes(picos, segmentos, 18.0, 55.0, opcoes["n"])

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
         "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip() or 0)


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
                    "-vf", "scale=560:-2", "-q:v", "4", out], check=False)
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


@app.route("/api/analisar", methods=["POST"])
def api_analisar():
    d = request.get_json(force=True)
    if ESTADO["rodando"]:
        return jsonify({"erro": "ja existe uma tarefa rodando"}), 409

    fonte = (d.get("fonte") or "").strip().strip('"')
    if not fonte:
        return jsonify({"erro": "informe o link ou o caminho do arquivo"}), 400

    sens = float(d.get("sensibilidade", 5))
    opcoes = {
        "n": max(1, min(40, int(d.get("n", 12)))),
        "modelo": d.get("modelo", "small"),
        "qualidade": int(d.get("qualidade", 1080)),
        "codec": d.get("codec", "avc"),
        "limiar": round(max(1.5, 7.5 - sens * 0.55), 2),
        "cookies_file": d.get("cookies") or None,
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
                       check=False)
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
                        "-map", "[v]", "-q:v", "3", out], check=False)
    if not os.path.exists(out):
        return "", 404
    return send_from_directory(PREV, os.path.basename(out), conditional=True)


@app.route("/api/ajustar", methods=["POST"])
def api_ajustar():
    dados = request.get_json(force=True)
    try:
        idx = int(dados["id"])
        ini = float(dados["inicio"])
        fim = float(dados["fim"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"erro": "dados invalidos"}), 400
    if not (0 <= idx < len(PROJETO["candidatos"])):
        return jsonify({"erro": "trecho inexistente"}), 404
    if fim - ini < 5 or fim - ini > 120:
        return jsonify({"erro": "duracao precisa ficar entre 5s e 120s"}), 400
    PROJETO["candidatos"][idx].update(inicio=round(ini, 1), fim=round(fim, 1),
                                      dur=round(fim - ini, 1))
    pasta_prev = os.path.join(PREV, PROJETO["chave"])
    if os.path.isdir(pasta_prev):
        for f in os.listdir(pasta_prev):
            if f.startswith(f"{idx}_"):
                try:
                    os.remove(os.path.join(pasta_prev, f))
                except OSError:
                    pass
    return jsonify({"ok": True})


def _job_exportar(itens, fmtos, modo, crf, legenda, fs):
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
                if legenda:
                    tamanho = fs or (76 if fmt in ("916", "45", "11") else 52)
                    palavras = []
                    for s in PROJETO["segmentos"]:
                        if s["end"] > c["inicio"] and s["start"] < c["fim"]:
                            palavras.extend(s["palavras"])
                    ass_nome = f"cap_{idx}_{fmt}.ass"
                    N.gerar_ass(palavras, c["inicio"], c["fim"],
                                os.path.join(N.TEMP, ass_nome), W, H,
                                "Arial Black", tamanho)
                out = os.path.join(pasta, f"{base}_{fmt}.mp4")
                N.renderizar(PROJETO["src"], PROJETO["dims"], corte, fmt, modo,
                             ass_nome, out, N.TEMP, sem_legenda=not legenda,
                             crf=crf, preset="slow",
                             cam=CAM, topo=CAM.get("topo", 0.45),
                             marca=_marca() if modo == "cam" else None)
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

    threading.Thread(target=_job_exportar,
                     args=(itens, fmtos, d.get("modo", "blur"),
                           int(d.get("crf", 18)),
                           bool(d.get("legenda", True)),
                           int(d.get("tamanho_fonte", 0))),
                     daemon=True).start()
    return jsonify({"ok": True})


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
        return jsonify({"erro": "já tem coisa rodando, espera terminar"}), 409
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    if not url:
        return jsonify({"erro": "faltou o link"}), 400
    qual = int(d.get("qualidade") or 1080)
    codec = d.get("codec") or "avc"
    cookies_file = d.get("cookies") or None

    def job():
        try:
            _set(rodando=True, erro=None, pct=2, etapa="download",
                 msg="baixando " + url[-24:])
            src, info = N.baixar(url, qual, codec, cookies_file=cookies_file)
            titulo = info.get("title") if isinstance(info, dict) else ""
            _add_hist(url, titulo, src)
            _set(rodando=False, pct=100, etapa="pronto",
                 msg="baixado: " + os.path.basename(src)[:60])
        except Exception as e:
            _set(rodando=False, pct=0, etapa="erro", erro=f"falha no download: {e}")

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

        itens = [_item("vod|" + nome, "video", "vídeo", p)]
        for tipo, arq, rot in (("audio", raiz + ".wav", "áudio"),
                               ("transcricao", raiz + ".wav.json", "transcrição")):
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
        grupos.append({"titulo": "sobras sem vídeo", "chave": None, "tamanho": "",
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
        return jsonify({"erro": "nao consegui apagar (arquivo em uso?)"}), 500

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
        subprocess.Popen(["explorer.exe", p])
        return jsonify({"ok": True})
    return jsonify({"erro": "pasta nao existe"}), 404


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cortes</title>
<style>
  :root{
    --bg:#0a0c11; --card:#111420; --card2:#171b26; --card3:#1f2432;
    --line:#232939; --line2:#2f3648;
    --txt:#e9ecf4; --dim:#868da4; --dim2:#5f6779;
    --acc:#6c5cff; --acc2:#22d3a6; --danger:#ff5c78;
  }
  *{box-sizing:border-box}
  ::selection{background:var(--acc);color:#fff}
  body{margin:0;color:var(--txt);padding:34px 28px 70px;
       background:radial-gradient(1000px 520px at 12% -12%, #191d33 0%, transparent 62%), var(--bg);
       font:14.5px/1.55 "Inter","Segoe UI",system-ui,-apple-system,sans-serif;
       -webkit-font-smoothing:antialiased}
  .wrap{max-width:1240px;margin:0 auto}

  .top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;
       padding-bottom:20px;margin-bottom:26px;border-bottom:1px solid var(--line)}
  h1{font-size:20px;margin:0;letter-spacing:-.4px;font-weight:650}
  .sub{color:var(--dim);font-size:13px;margin-top:5px;max-width:640px}
  .topacts{display:flex;gap:9px;align-items:center;flex-shrink:0}

  .chip{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);
        background:var(--card2);border:1px solid var(--line);border-radius:99px;
        padding:6px 13px;white-space:nowrap}
  .chip i{width:6px;height:6px;border-radius:50%;background:var(--dim2);flex-shrink:0}
  .chip.on{color:var(--acc2);border-color:#1c4a3d;background:#0d2620}
  .chip.on i{background:var(--acc2);animation:pulse 1.5s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}

  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
        padding:22px;margin-bottom:16px;box-shadow:0 1px 0 #ffffff07 inset}
  .chead{display:flex;align-items:center;gap:10px;margin-bottom:18px}
  .num{width:22px;height:22px;border-radius:50%;background:#6c5cff1f;color:var(--acc);
       font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .card h2{font-size:12.5px;text-transform:uppercase;letter-spacing:1.1px;margin:0;font-weight:600}
  .count{color:var(--dim);font-size:12px;font-weight:400;letter-spacing:0;text-transform:none}

  label{display:block;font-size:11px;color:var(--dim);margin-bottom:7px;
        text-transform:uppercase;letter-spacing:.6px;font-weight:600}
  input,select,button{font:inherit;color:var(--txt)}
  input[type=text],input[type=number],select{
    background:var(--card2);border:1px solid var(--line);border-radius:10px;
    padding:10px 13px;width:100%;transition:border-color .15s,background .15s,box-shadow .15s}
  input[type=text]:hover,input[type=number]:hover,select:hover{border-color:var(--line2)}
  input:focus,select:focus{outline:none;border-color:var(--acc);background:#161a27;
    box-shadow:0 0 0 3px #6c5cff22}
  input::placeholder{color:var(--dim2)}
  input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:4px;
    background:var(--line2);border-radius:99px;outline:none;margin:13px 0 4px}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;
    border-radius:50%;background:var(--acc);border:2px solid #0a0c11;cursor:pointer;
    box-shadow:0 0 0 3px #6c5cff30}

  button{background:var(--acc);border:1px solid transparent;border-radius:10px;
         padding:10px 18px;font-weight:600;font-size:13.5px;cursor:pointer;
         transition:filter .15s,transform .1s,background .15s;white-space:nowrap}
  button:hover{filter:brightness(1.12)}
  button:active{transform:translateY(1px)}
  button:disabled{opacity:.38;cursor:not-allowed;transform:none;filter:none}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--dim)}
  button.ghost:hover{background:var(--card2);border-color:var(--line2);color:var(--txt)}
  button.danger{background:#2a141c;border:1px solid #6a2333;color:#ff8fa3}
  button.danger:hover{background:#371820;border-color:var(--danger)}
  button.sm{padding:6px 11px;font-size:12px;border-radius:8px}

  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
  .row.mt{margin-top:14px}
  .f{flex:1;min-width:130px}
  .f.grow{flex:3;min-width:280px}
  .grow{flex:1}

  .progwrap{margin-top:20px;padding-top:16px;border-top:1px solid var(--line)}
  .bar{height:5px;background:var(--card3);border-radius:99px;overflow:hidden}
  .bar>i{display:block;height:100%;width:0;border-radius:99px;
         background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .5s ease}
  .st{display:flex;justify-content:space-between;gap:10px;font-size:12px;color:var(--dim);
      margin-top:9px;font-variant-numeric:tabular-nums}
  #pct{color:var(--acc2);font-weight:600}
  .logmini{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:11px;
           line-height:1.7;color:var(--dim2);margin-top:10px;padding-left:10px;
           border-left:2px solid var(--line);max-height:90px;overflow:hidden}
  .logmini div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .logmini div:last-child{color:var(--dim)}

  #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(272px,1fr));gap:14px}
  .clip{background:var(--card2);border:1px solid var(--line);border-radius:13px;overflow:hidden;
        transition:border-color .15s,transform .15s,box-shadow .15s}
  .clip:hover{border-color:var(--line2);transform:translateY(-2px);box-shadow:0 10px 26px #0007}
  .clip.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc),0 8px 22px #6c5cff26}
  .thumb{position:relative;background:#05060a;aspect-ratio:16/9;cursor:pointer;overflow:hidden}
  .thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .thumb video{width:100%;height:100%;object-fit:contain;display:block;background:#000}
  .badge{position:absolute;top:9px;left:9px;background:#000000b3;backdrop-filter:blur(8px);
         padding:3px 8px;border-radius:6px;font-size:11px;font-weight:600;
         font-variant-numeric:tabular-nums;border:1px solid #ffffff14}
  .badge.r{left:auto;right:9px;color:var(--acc2)}
  .play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
        font-size:30px;color:#fff;text-shadow:0 2px 14px #000;pointer-events:none;
        opacity:0;transition:opacity .15s;background:#00000040}
  .thumb:hover .play{opacity:1}
  .body{padding:13px}
  .txt{font-size:12.5px;color:var(--dim);height:55px;overflow:hidden;margin-bottom:12px;line-height:1.45}
  .mini{display:flex;gap:6px;align-items:center}
  .mini input{width:66px;padding:6px 8px;font-size:12px;text-align:center;
              font-variant-numeric:tabular-nums}
  .mini span{color:var(--dim2);font-size:12px}
  .foot{display:flex;justify-content:space-between;align-items:center;margin-top:11px;
        padding-top:11px;border-top:1px solid var(--line)}
  .chk{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);cursor:pointer;
       text-transform:none;letter-spacing:0;font-weight:400;margin:0}
  input[type=checkbox]{width:16px;height:16px;accent-color:var(--acc);cursor:pointer;margin:0}

  .pills{display:flex;gap:8px;flex-wrap:wrap}
  .pill{background:var(--card2);border:1px solid var(--line);border-radius:99px;
        padding:7px 15px;font-size:12.5px;cursor:pointer;user-select:none;transition:.15s;color:var(--dim)}
  .pill:hover{border-color:var(--line2);color:var(--txt)}
  .pill.on{background:#6c5cff1f;border-color:var(--acc);color:#c3baff;font-weight:600}
  .err{background:#2a1420;border:1px solid #6a2333;color:#ffc7d1;padding:12px;
       border-radius:10px;font:12px/1.55 ui-monospace,Consolas,monospace;
       white-space:pre-wrap;margin-top:12px;display:none}
  .empty{color:var(--dim2);text-align:center;padding:46px 0;font-size:13px}
  .expbar{display:flex;align-items:center;gap:10px;margin-top:18px;padding-top:16px;
          border-top:1px solid var(--line);flex-wrap:wrap}
  .tot{color:var(--dim);font-size:12.5px;font-variant-numeric:tabular-nums}

  .camwrap{display:flex;gap:16px;margin-top:14px;align-items:flex-start;flex-wrap:wrap}
  .camsrc{position:relative;flex:1 1 360px;max-width:520px;border-radius:10px;
          overflow:hidden;background:#000;line-height:0}
  .camsrc img{width:100%;display:block;user-select:none;-webkit-user-drag:none}
  #cambox{position:absolute;border:2px solid #7cd3ff;background:#7cd3ff22;cursor:move;
          box-shadow:0 0 0 9999px #0009;touch-action:none}
  #cambox i{position:absolute;width:13px;height:13px;background:#7cd3ff;border-radius:3px}
  #cambox i[data-h="nw"]{left:-7px;top:-7px;cursor:nwse-resize}
  #cambox i[data-h="ne"]{right:-7px;top:-7px;cursor:nesw-resize}
  #cambox i[data-h="sw"]{left:-7px;bottom:-7px;cursor:nesw-resize}
  #cambox i[data-h="se"]{right:-7px;bottom:-7px;cursor:nwse-resize}
  .camres{flex:0 0 168px}
  .camres img{width:168px;border-radius:9px;display:block;border:1px solid var(--line)}
  .camres .cap{color:var(--dim2);font-size:11px;margin-top:7px;text-align:center}
  .camctl{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:13px;
          font-size:12px;color:var(--dim)}
  .camctl input[type=range]{width:130px}
  .camctl input[type=text]{padding:6px 9px;font-size:12px;width:150px}

  .modal{position:fixed;inset:0;background:#000000c4;backdrop-filter:blur(6px);
         display:none;align-items:center;justify-content:center;padding:20px;z-index:50}
  .modal.on{display:flex}
  .mbox{background:var(--card);border:1px solid var(--line);border-radius:16px;
        padding:22px;width:min(720px,100%);max-height:84vh;overflow:auto;
        box-shadow:0 24px 60px #000a}
  .mbox h3{margin:0 0 5px;font-size:16px;font-weight:650}
  .mbox .hint{color:var(--dim);font-size:12px;margin-bottom:16px;line-height:1.5}
  button.danger{background:#3a1620;border:1px solid var(--danger);color:#ff8fa3}
  button.sm{padding:6px 11px;font-size:12px}
  .vgroup{border:1px solid var(--line);border-radius:11px;margin-bottom:10px;overflow:hidden}
  .vhead{display:flex;justify-content:space-between;gap:10px;align-items:center;
         padding:11px 13px;background:var(--card3);font-size:12.5px}
  .vhead b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vhead .sz{color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap;font-size:12px}
  .vitem{display:flex;align-items:center;gap:10px;padding:8px 13px;
         border-top:1px solid var(--line);font-size:12px}
  .vitem .rot{flex:1;color:var(--dim)}
  .vitem .sz{color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
  .disk{display:flex;justify-content:space-between;font-size:12px;color:var(--dim);
        margin:12px 0;padding-bottom:12px;border-bottom:1px solid var(--line)}
  .vfav{display:flex;gap:8px;padding:12px 13px;border-bottom:1px solid var(--line);
        background:var(--card2);align-items:center}
  .vfav input{flex:1;background:var(--card);border:1px solid var(--line);border-radius:8px;
              color:var(--txt);font:inherit;font-size:12px;padding:8px 11px;min-width:0}
  .vfav input:focus{border-color:var(--acc)}
  .vfalta{display:flex;align-items:center;justify-content:space-between;gap:10px;
          padding:9px 13px;border-bottom:1px solid var(--line);font-size:12px;color:var(--dim)}
  #toasts{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;
          gap:8px;z-index:80;max-width:350px}
  .toast{background:#1a1f2e;border:1px solid var(--line2);border-left:3px solid var(--acc);
         border-radius:10px;padding:11px 15px;font-size:13px;box-shadow:0 10px 28px #0008;
         animation:tin .18s ease-out;transition:opacity .25s}
  .toast.erro{border-left-color:var(--danger);background:#241820}
  .toast.ok{border-left-color:var(--acc2)}
  @keyframes tin{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  .conf{position:fixed;inset:0;background:#000000c4;backdrop-filter:blur(6px);
        display:none;align-items:center;justify-content:center;z-index:90;padding:20px}
  .conf.on{display:flex}
  .confbox{background:var(--card);border:1px solid var(--line);border-radius:14px;
           padding:22px;max-width:420px;width:100%;box-shadow:0 24px 60px #000a}
  .confbox p{margin:0 0 18px;font-size:14px;line-height:1.55;white-space:pre-wrap}
  .confbox .acts{display:flex;gap:8px;justify-content:flex-end}
  @media(max-width:640px){
    body{padding:20px 16px 50px}
    .top{flex-direction:column;align-items:stretch}
    .card{padding:17px}
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>Cortes</h1>
      <div class="sub">Ache os melhores momentos da live, ajuste o corte e exporte em vertical e horizontal.</div>
    </div>
    <div class="topacts">
      <span class="chip" id="chip"><i></i><span id="chiptxt">parado</span></span>
      <button class="ghost" onclick="listarVods()">Arquivos</button>
    </div>
  </header>

  <section class="card">
    <div class="chead"><span class="num">1</span><h2>Fonte</h2></div>
    <div class="row">
      <div class="f grow">
        <label>Link do YouTube ou arquivo local</label>
        <input type="text" id="fonte" placeholder="https://www.youtube.com/live/...">
      </div>
      <div class="f" style="min-width:auto">
        <label>&nbsp;</label>
        <input type="text" id="cookies" placeholder="cookies.txt (opcional)">
      </div>
      <button id="btnAnalisar" onclick="analisar()">Analisar</button>
    </div>
    <div class="row mt">
      <div class="f"><label>Trechos</label><input type="number" id="n" value="12" min="1" max="40"></div>
      <div class="f"><label>Modelo</label>
        <select id="modelo"><option value="tiny">tiny</option>
        <option value="base">base</option><option value="small" selected>small</option>
        <option value="medium">medium</option></select></div>
      <div class="f"><label>Qualidade</label>
        <select id="qualidade"><option>720</option><option selected>1080</option><option>1440</option></select></div>
      <div class="f"><label>Codec</label>
        <select id="codec"><option value="avc" selected>H.264</option><option value="vp9">VP9</option></select></div>
      <div class="f grow"><label>Sensibilidade <span id="sensv" style="color:var(--acc)">5</span></label>
        <input type="range" id="sens" min="1" max="10" value="5"
               oninput="document.getElementById('sensv').textContent=this.value"></div>
    </div>
    <div class="progwrap">
      <div class="bar"><i id="prog"></i></div>
      <div class="st"><span id="status">parado</span><span id="pct"></span></div>
      <div class="logmini" id="logmini"></div>
      <div class="err" id="erro"></div>
    </div>
  </section>

  <section class="card">
    <div class="chead"><span class="num">2</span><h2>Trechos</h2><span class="count" id="cont"></span>
      <span class="grow"></span>
      <button class="ghost sm" onclick="marcarTodos(true)">todos</button>
      <button class="ghost sm" onclick="marcarTodos(false)">nenhum</button>
    </div>
    <div id="grid"><div class="empty">Nada analisado ainda — cole um link acima e clique em Analisar.</div></div>
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
        <button class="ghost sm" onclick="detectarCam()">detectar automático</button>
        <label class="chk"><input type="checkbox" id="camMarca" checked
          onchange="camSalvar()"> marca d'água</label>
        <input type="text" id="camTexto" value="VICEMSHORTS" onchange="camSalvar()">
        <label class="chk">cam = <b id="camTopoV" style="color:var(--acc)">45</b>%</label>
        <input type="range" id="camTopo" min="25" max="65" value="45"
               oninput="camTopoMudou(this.value)">
        <span id="camInfo" style="color:var(--dim2)"></span>
      </div>
    </div>
    <div class="expbar">
      <span class="tot" id="selinfo"></span>
      <span class="grow"></span>
      <button class="ghost" onclick="abrirPasta()">Abrir pasta</button>
      <button id="btnExport" onclick="exportar()" disabled>Exportar</button>
    </div>
  </section>
</div>

<div class="modal" id="modal">
  <div class="mbox">
    <h3>Arquivos no disco</h3>
    <div class="hint">Cada item é apagado por si. Apagar o vídeo não apaga o áudio, os clipes ou a transcrição — só o que você mandar.</div>
    <div class="vfav">
      <input id="reurl" placeholder="link do YouTube pra baixar de novo">
      <button class="ghost sm" onclick="baixarDeNovo()">baixar</button>
    </div>
    <div id="vfalta"></div>
    <div class="disk">
      <span id="dtotal">—</span>
      <span id="dlivre">—</span>
    </div>
    <div id="mlist"></div>
    <div style="margin-top:16px;text-align:right">
      <button class="ghost" onclick="fecharModal()">fechar</button>
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
    modelo: document.getElementById("modelo").value,
    qualidade: +document.getElementById("qualidade").value,
    codec: document.getElementById("codec").value,
    sensibilidade: +document.getElementById("sens").value,
    cookies: document.getElementById("cookies").value.trim() || null
  };
  document.getElementById("btnAnalisar").disabled = true;
  document.getElementById("erro").style.display = "none";
  const r = await fetch("/api/analisar", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); document.getElementById("btnAnalisar").disabled = false; return; }
  iniciarPolling();
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

async function carregarCandidatos(){
  const d = await (await fetch("/api/candidatos")).json();
  cands = d.itens || [];
  chave = d.chave || '';
  selecionados = new Set(cands.map(c => c.id));
  document.getElementById("cont").textContent = d.titulo || "";
  render();
  if(document.getElementById("modo").value === "cam") camInit();
}

function render(){
  const g = document.getElementById("grid");
  if(!cands.length){ g.innerHTML = '<div class="empty">Nada analisado ainda.</div>'; return; }
  g.innerHTML = "";
  cands.forEach(c => {
    const el = document.createElement("div");
    el.className = "clip" + (selecionados.has(c.id) ? " sel" : "");
    el.innerHTML = `
      <div class="thumb" onclick="ver(${c.id}, this)">
        <img src="/thumb/${chave}/${c.id}" loading="lazy">
        <div class="badge">${fmtT(c.inicio)} &middot; ${c.dur}s</div>
        ${c.score ? `<div class="badge r">${Math.round(c.score)}</div>` : ""}
        <div class="play">&#9654;</div>
      </div>
      <div class="body">
        <div class="txt" title="${(c.texto||"").slice(0,400).replace(/"/g,"&quot;")}">${(c.texto||"").slice(0,160)}</div>
        <div class="mini">
          <input type="number" step="0.5" value="${c.inicio}" id="i${c.id}">
          <span>ate</span>
          <input type="number" step="0.5" value="${c.fim}" id="f${c.id}">
          <button class="ghost" style="padding:6px 10px;font-size:11px" onclick="ajustar(${c.id})">ok</button>
        </div>
        <div class="foot">
          <label class="chk"><input type="checkbox" ${selecionados.has(c.id)?"checked":""}
            onchange="marcar(${c.id}, this.checked)"> selecionar</label>
        </div>
      </div>`;
    g.appendChild(el);
  });
  atualizarSel();
}

function ver(id, box){
  const modo = document.getElementById("modo").value;
  box.innerHTML = `<video controls autoplay src="/preview/${chave}/${id}?modo=${modo}"></video>`;
}

function marcarTodos(v){
  selecionados = v ? new Set(cands.map(c => c.id)) : new Set();
  render();
}

function marcar(id, on){
  on ? selecionados.add(id) : selecionados.delete(id);
  const i = cands.findIndex(c => c.id === id);
  const el = document.querySelectorAll(".clip")[i];
  if(el) el.classList.toggle("sel", on);
  atualizarSel();
}

function atualizarSel(){
  const fmtos = document.querySelectorAll("#fmtos .pill.on").length;
  document.getElementById("selinfo").textContent =
    selecionados.size + " de " + cands.length + " selecionados" +
    (selecionados.size && fmtos ? " - " + (selecionados.size * fmtos) + " arquivos" : "");
  const b = document.getElementById("btnExport");
  b.disabled = selecionados.size === 0;
  b.textContent = selecionados.size ? "Exportar " + selecionados.size : "Exportar";
}

async function ajustar(id){
  const inicio = +document.getElementById("i"+id).value;
  const fim = +document.getElementById("f"+id).value;
  const r = await fetch("/api/ajustar", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id, inicio, fim})});
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); return; }
  const c = cands.find(x=>x.id===id);
  c.inicio = inicio; c.fim = fim; c.dur = +(fim-inicio).toFixed(1);
  render();
}

async function exportar(){
  const fmtos = [...document.querySelectorAll("#fmtos .pill.on")].map(p=>p.dataset.f);
  if(!fmtos.length){ toast("Escolha ao menos um formato", "erro"); return; }
  const body = {
    itens: [...selecionados],
    formatos: fmtos,
    modo: document.getElementById("modo").value,
    crf: +document.getElementById("crf").value,
    legenda: document.getElementById("legenda").checked
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
    "Lives ocupando: " + tam(d.total_bytes);
  document.getElementById("dlivre").textContent =
    "Livre no disco E: " + tam(d.livre_bytes);

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
  const r = await fetch("/api/baixar", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({url, qualidade:1440, codec:"vp9",
      cookies: document.getElementById("cookies").value.trim() || null})
  });
  const j = await r.json();
  if(j.erro){ toast(j.erro, "erro"); if(btn) btn.disabled = false; return; }
  toast("baixando — acompanhe a barra", "ok");
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
    "x " + Math.round(CAMCFG.x*100) + "% · y " + Math.round(CAMCFG.y*100) +
    "% · w " + Math.round(CAMCFG.w*100) + "% · h " + Math.round(CAMCFG.h*100) + "%";
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
    toast("cam encontrada — ajuste arrastando se precisar");
  } finally {
    btn.disabled = false; btn.textContent = "detectar automático";
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
    """Deixa o ultimo video pronto pra ver a cam mesmo sem reanalisar."""
    try:
        src = (_ler_ultimo() or {}).get("src")
        if not src or not os.path.exists(src):
            return
        with LOCK:
            PROJETO.update(src=src, dims=N.ffprobe_dims(src),
                           titulo=os.path.splitext(os.path.basename(src))[0],
                           dur=N.ffprobe_dur(src), chave=_chave(src))
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    _restaurar_ultimo()
    url = "http://127.0.0.1:8765"
    print(f"\n  Painel rodando em {url}\n  Ctrl+C para parar\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
