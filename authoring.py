"""編集モードのサーバー側。

アプリの編集モードから呼ばれ、科目の定義（`subjects.json`）に従って保存する。
このファイルが無い環境では serve.py は編集APIを持たない静的サーバーとして動く。

保存のしかたは科目ごとに2通り（`subjects.json` の `authoring.mode`）。

  direct   … 問題データのJSONを直接書き換える。**問題そのものも作れる**。
             保存した瞬間に演習側へ反映される。
  pipeline … 解説だけを `enriched/<年>.json` に書き、`rebuild_all.py` で反映する。
             問題文・選択肢・画像は元データ側の持ち物なので触らない。

pipeline の enriched は「パイプライン出力への上書き」なので、保存も
「送られてきたキーだけ」を反映し、null が来たキーは上書きを取り消す。
"""
import json, os, re, shutil, subprocess, sys, time, unicodedata
from pathlib import Path

APPDIR = Path(__file__).resolve().parent
CONFIG = APPDIR / "subjects.json"

COMMON_FIELDS = ("explanation", "answer", "flag", "sources")
DIRECT_FIELDS = COMMON_FIELDS + ("stem", "choices", "images")
KEEP_BACKUPS = 10                    # ファイルごとに残す控えの数
LETTERS = "abcdefgh"
MAX_IMAGE_PATH = 300


class BadRequest(Exception):
    """入力が不正。呼び出し側で 400 にする。"""


# ---------------------------------------------------------------- 科目の定義
_cache = {"mtime": None, "subjects": {}}


def _resolve(rel):
    """subjects.json からの相対パスを解く（絶対パスを設定に書かせないため）。"""
    return (APPDIR / str(rel)).resolve()


def subjects():
    """subjects.json のうち、編集の設定がある科目だけを返す（更新は自動で拾う）。"""
    try:
        mtime = CONFIG.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] == mtime:
        return _cache["subjects"]
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[編集モード] subjects.json を読めません: {e}")
        return {}
    out = {}
    for key, conf in raw.items():
        if key.startswith("_") or not isinstance(conf, dict):
            continue
        a = conf.get("authoring")
        if not isinstance(a, dict):
            continue
        mode = a.get("mode", "pipeline")
        # kind="essay" … 記述式（選択肢のない問題集）。**この宣言があるときだけ**
        # 選択肢0個を通す。既定でエラーのままにしておかないと、取り込みで選択肢を
        # 読み損ねた問題が「記述式」として黙って入ってしまう。
        item = {"label": conf.get("name", key), "mode": mode,
                "essay": conf.get("kind") == "essay"}
        if mode == "pipeline":
            if not a.get("enriched"):
                continue
            item["enriched"] = _resolve(a["enriched"])
            item["rebuild"] = _resolve(a["rebuild"]) if a.get("rebuild") else None
        elif mode == "direct":
            item["data"] = _resolve(a.get("file") or conf.get("file") or "")
        else:
            print(f"[編集モード] 知らない mode です: {key} -> {mode}")
            continue
        out[key] = item
    _cache.update(mtime=mtime, subjects=out)
    return out


def subject_conf(subject):
    conf = subjects().get(subject)
    if conf is None:
        raise BadRequest(f"編集できる科目ではありません: {subject!r}")
    if conf["mode"] == "pipeline" and not conf["enriched"].is_dir():
        raise BadRequest(f"{conf['label']}の解説フォルダがありません: {conf['enriched']}")
    if conf["mode"] == "direct" and not conf["data"].is_file():
        raise BadRequest(f"{conf['label']}のデータがありません: {conf['data']}")
    return conf


def fields_of(mode):
    return DIRECT_FIELDS if mode == "direct" else COMMON_FIELDS


def as_int(value, what):
    """数値以外が来ても 500 にせず、直せる文言で 400 にする。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise BadRequest(f"{what}は数字で指定してください: {value!r}")


def config():
    """アプリ起動時に編集モードが使えるかを返す。"""
    out = {}
    for key, conf in subjects().items():
        try:
            ys = years(key)
        except BadRequest:
            continue
        out[key] = {"label": conf["label"], "mode": conf["mode"], "years": ys,
                    "rebuild": bool(conf["mode"] == "pipeline" and conf.get("rebuild")
                                    and conf["rebuild"].is_file()),
                    "canAddQuestions": conf["mode"] == "direct",
                    "essay": bool(conf.get("essay")),
                    # 画像の置き場所は問題集ごとに違う（データJSONの隣の images/）。
                    # 画面に直書きすると、`data/images/…` と案内して別の場所へ入る。
                    "imageDir": (_img_root(conf)[1] if conf["mode"] == "direct" else None)}
    return {"enabled": bool(out), "subjects": out}


# ---------------------------------------------------------------- 共通の入出力
def _read_json(path, what):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise BadRequest(f"{what} を開けません: {e}")
    except json.JSONDecodeError as e:
        raise BadRequest(f"{what} を読めません（{e.lineno}行目: {e.msg}）")


def backup(path):
    """書き換え前の控えを _backup/ に取り、古いものから間引く。"""
    if not path.exists():
        return None
    bdir = path.parent / "_backup"
    bdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # 連番は必ず付けて桁を揃える。付けたり付けなかったりすると名前順が時刻順から
    # ずれ（"-1.json" が ".json" より前に来る）、古い控えの間引きが逆向きになる。
    n = 0
    while (bdir / f"{path.stem}-{stamp}-{n:03d}.json").exists():
        n += 1
    dest = bdir / f"{path.stem}-{stamp}-{n:03d}.json"
    shutil.copy2(path, dest)
    for p in sorted(bdir.glob(f"{path.stem}-*.json"))[:-KEEP_BACKUPS]:
        try:
            p.unlink()
        except OSError:
            pass
    return dest.name


def write_json(path, data, indent=1):
    """同じフォルダの一時ファイルへ書いてから置換する（途中で落ちても元が残る）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))   # 読み直せることを確認してから置換
    os.replace(tmp, path)


# ---------------------------------------------------------------- 入力の検証
# ⚠️制御文字は保存の入口で落とす。NUL などはブラウザが勝手に置き換えるので、
#   保存はできても読み戻すと別物になる（実測した）。改行とタブだけ残す。
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value):
    """保存する文字列の共通の下ごしらえ。改行を揃え、制御文字を落とす。"""
    if not isinstance(value, str):
        return value
    return CTRL_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))


def norm_answer(value, labels=LETTERS):
    """'A, b' や 'a b' を 'a,b' に正規化する。空文字は「解答なし」。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest("解答は文字列で指定してください")
    # ⚠️記号は全角で打たれる（IMEの変換・Excelからの貼り付け）。ここで半角に寄せる。
    #   値は記号と区切りだけなので、行ごと寄せてよい（本文には使わない）。
    value = unicodedata.normalize("NFKC", value)
    ok = set(labels)
    seen = []
    for ch in value.lower():
        if ch in ok and ch not in seen:
            seen.append(ch)
    junk = sorted({c for c in value.lower() if c.isalpha() and c not in ok})
    if junk:
        raise BadRequest(f"解答に使えない文字があります: {''.join(junk)}"
                         f"（選択肢は {'・'.join(labels)}）")
    return ",".join(seen)


def parse_choices(text, allow_empty=False):
    """「a 選択肢の文」を1行ずつ受け取り、[{label,text}] にする。

    ⚠️**空（0個）は既定でエラー。** これは壊れたデータを弾く安全装置でもある。
      無条件に0個を許すと、取り込みで選択肢を読み損ねた問題が「記述式」として
      黙って通ってしまう。`allow_empty` は、記述式と宣言された問題集
      （subjects.json の `kind: "essay"`）か、**もともと選択肢の無い問題を
      直しているとき**だけ立てる（後者は、直せなくなる袋小路を作らないため）。
    """
    if isinstance(text, list):
        # ⚠️リストだからと素通しにしない。中身が想定と違うと、後で
        #   c["label"] のところで落ちて 500 になる（バグ狩りで実際に出た）。
        out = []
        for i, c in enumerate(text, 1):
            if not isinstance(c, dict) or "label" not in c or "text" not in c:
                raise BadRequest(f"選択肢の {i} 個目が読めません"
                                 "（{\"label\":\"a\",\"text\":\"…\"} の形で指定してください）")
            lab = str(c["label"]).strip().lower()
            if lab not in LETTERS:
                raise BadRequest(f"選択肢の記号は {'・'.join(LETTERS)} のいずれかにしてください: {lab}")
            out.append({"label": lab, "text": clean_text(str(c["text"])).strip()})
        if not out and allow_empty:
            return []
        if len(out) < 2:
            raise BadRequest("選択肢は2つ以上必要です。")
        if len({c["label"] for c in out}) != len(out):
            raise BadRequest("選択肢の記号が重複しています。")
        return out
    if not isinstance(text, str):
        raise BadRequest("選択肢は文字列で指定してください")
    out, seen = [], set()
    for i, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-zＡ-Ｚａ-ｚ])[ 　	.．、,)）]\s*(.+)$", line)
        if not m:
            raise BadRequest(f"選択肢の {i} 行目が読めません: 「{line[:24]}」"
                             f"（「a 選択肢の文」のように、記号・空白・本文の順で書いてください）")
        lab = unicodedata.normalize("NFKC", m.group(1)).lower()
        if lab not in LETTERS:
            raise BadRequest(f"選択肢の記号は {'・'.join(LETTERS)} のいずれかにしてください: {lab}")
        if lab in seen:
            raise BadRequest(f"選択肢の記号が重複しています: {lab}")
        seen.add(lab)
        out.append({"label": lab, "text": m.group(2).strip()})
    if not out and allow_empty:
        return []
    if len(out) < 2:
        raise BadRequest("選択肢は2つ以上必要です。")
    return out


def parse_images(text):
    """画像の場所を1行に1件。外部URLと親ディレクトリ参照は受け付けない。"""
    if isinstance(text, list):
        # 同上。str() で何でも通してしまうと、辞書がパスとして保存される
        if any(not isinstance(x, str) for x in text):
            raise BadRequest("画像は文字列のリストで指定してください")
        lines = list(text)
    elif isinstance(text, str):
        lines = text.replace("\r\n", "\n").split("\n")
    else:
        raise BadRequest("画像は文字列で指定してください")
    out = []
    for raw in lines:
        # 濁点の分解（macOS由来のファイル名はNFD）で「同じ名前なのに一致しない」を防ぐ。
        # 区切りは / に寄せる（Windowsで \ を打たれても通るように）。
        p = unicodedata.normalize("NFC", raw.strip()).replace("\\", "/")
        if not p:
            continue
        if len(p) > MAX_IMAGE_PATH:
            raise BadRequest("画像の場所が長すぎます。")
        if re.match(r"^[a-zA-Z][\w+.-]*:", p):
            raise BadRequest(f"外部のURLは指定できません: {p[:40]}"
                             "（画像はこのフォルダの中に置いてください）")
        if ".." in p.split("/"):
            raise BadRequest(f"「..」を含む場所は指定できません: {p[:40]}")
        out.append(p)
    return out


def clean_entry(patch, mode, labels=LETTERS, allow_empty_choices=False):
    """来た差分を検証し、(上書きするキー, 削除するキー) に分ける。"""
    if not isinstance(patch, dict):
        raise BadRequest("保存内容が不正です")
    allowed = fields_of(mode)
    unknown = set(patch) - set(allowed)
    if unknown:
        raise BadRequest(f"扱えない項目です: {', '.join(sorted(unknown))}")
    # 制御文字は保存の入口で落とす（1か所に集約する）
    patch = {k: clean_text(v) if isinstance(v, str) else v for k, v in patch.items()}
    setk, delk = {}, []
    # 選択肢を先に確定させる（解答の記号を選択肢と突き合わせるため）
    if "choices" in patch and patch["choices"] is not None:
        setk["choices"] = parse_choices(patch["choices"], allow_empty_choices)
        labels = "".join(c["label"] for c in setk["choices"]) or LETTERS
    for key, val in patch.items():
        if key == "choices":
            if val is None:
                raise BadRequest("選択肢は空にできません。")
            continue
        if val is None:
            delk.append(key)
            continue
        if key in ("explanation", "stem"):
            if not isinstance(val, str):
                raise BadRequest(f"{'解説' if key=='explanation' else '問題文'}は文字列で指定してください")
            val = val.replace("\r\n", "\n").replace("\r", "\n").rstrip()
            if not val.strip():
                if key == "stem":
                    raise BadRequest("問題文は空にできません。")
                delk.append(key)
                continue
        elif key == "answer":
            val = norm_answer(val, labels)
        elif key == "flag":
            if not isinstance(val, bool):
                raise BadRequest("要確認は true/false で指定してください")
        elif key == "sources":
            if isinstance(val, str):
                val = val.replace("\r\n", "\n").split("\n")
            if not isinstance(val, list) or any(not isinstance(s, str) for s in val):
                raise BadRequest("出典は文字列のリストで指定してください")
            val = [x for x in (clean_text(s).strip() for s in val) if x]
            if not val:
                delk.append(key)
                continue
        elif key == "images":
            val = parse_images(val)
            if not val:
                delk.append(key)
                continue
        setk[key] = val
    return setk, delk


# =================================================================
#  pipeline モード（解説だけを enriched に上書きする）
# =================================================================
def _pl_years(conf):
    ys = []
    for p in conf["enriched"].glob("*.json"):
        if re.fullmatch(r"\d{4}", p.stem):
            ys.append(int(p.stem))
    return sorted(ys)


def _pl_path(conf, subject, year):
    y = as_int(year, "年度")
    if y not in _pl_years(conf):
        raise BadRequest(f"編集できる年度ではありません: {year}")
    return conf["enriched"] / f"{y}.json"


def _pl_load(conf, subject, year):
    p = _pl_path(conf, subject, year)
    if not p.exists():
        return p, {}
    data = _read_json(p, p.name)
    if not isinstance(data, dict):
        raise BadRequest(f"{p.name} の中身が想定と違います（辞書ではありません）")
    return p, data


def _pl_apply(data, qno, setk, delk):
    entry = dict(data.get(str(qno), {}))
    before = dict(entry)
    entry.update(setk)
    for k in delk:
        entry.pop(k, None)
    if entry == before and str(qno) in data:
        return False, entry
    if entry:
        data[str(qno)] = entry
    else:
        data.pop(str(qno), None)
    return True, entry


def _pl_write(path, data):
    write_json(path, {k: data[k] for k in sorted(data, key=int)})


# =================================================================
#  direct モード（問題データのJSONを直接書き換える）
# =================================================================
def _dr_load(conf):
    path = conf["data"]
    data = _read_json(path, path.name)
    if not isinstance(data, list):
        raise BadRequest(f"{path.name} の中身が想定と違います（問題のリストではありません）")
    return path, data


def _dr_years(conf):
    try:
        _, data = _dr_load(conf)
    except BadRequest:
        return []
    return sorted({int(q["year"]) for q in data if isinstance(q, dict) and "year" in q})


def _dr_find(data, year, qno):
    for i, q in enumerate(data):
        if int(q.get("year", -1)) == year and int(q.get("no", -1)) == qno:
            return i
    return -1


def _dr_entry(q):
    """1問を編集画面が扱う形にする。"""
    return {"explanation": q.get("expl", ""),
            "answer": ",".join(q.get("answer") or []),
            "flag": bool(q.get("flag")),
            "sources": list(q.get("sources") or []),
            "stem": q.get("stem", ""),
            "choices": "\n".join(f'{c["label"]} {c["text"]}' for c in (q.get("choices") or [])),
            "images": "\n".join(q.get("images") or [])}


def _dr_apply(q, setk, delk):
    """編集画面の項目を、問題データのキーへ書き戻す。"""
    m = {"explanation": "expl", "stem": "stem", "choices": "choices",
         "images": "images", "flag": "flag", "sources": "sources"}
    before = json.dumps(q, ensure_ascii=False, sort_keys=True)
    for key, val in setk.items():
        if key == "answer":
            q["answer"] = [c for c in val.split(",") if c]
        else:
            q[m[key]] = val
    for key in delk:
        if key == "answer":
            q["answer"] = []
        elif key == "flag":
            q["flag"] = False
        elif key in ("sources", "images"):
            q.pop(m[key], None)
        else:
            q[m[key]] = ""
    return json.dumps(q, ensure_ascii=False, sort_keys=True) != before


def _dr_labels(q):
    return "".join(c["label"] for c in (q.get("choices") or [])) or LETTERS


# =================================================================
#  外から使う入口
# =================================================================
def years(subject):
    conf = subjects().get(subject)
    if conf is None:
        return []
    return _pl_years(conf) if conf["mode"] == "pipeline" else _dr_years(conf)


def read_entry(subject, year, qno):
    conf = subject_conf(subject)
    qno = as_int(qno, "問題番号")
    if conf["mode"] == "pipeline":
        _, data = _pl_load(conf, subject, year)
        return data.get(str(qno), {})
    _, data = _dr_load(conf)
    i = _dr_find(data, as_int(year, "年度"), qno)
    if i < 0:
        raise BadRequest(f"その問題がありません: {year}年 問{qno}")
    return _dr_entry(data[i])


def save_entry(subject, year, qno, patch):
    return save_many(subject, [{"year": year, "no": qno, "entry": patch}], single=True)


def save_many(subject, items, single=False):
    """複数問をまとめて保存する。1件でも不正なら何も書かない。"""
    conf = subject_conf(subject)
    if not isinstance(items, list):
        raise BadRequest("保存内容が不正です")
    for it in items:
        if not isinstance(it, dict):
            raise BadRequest("保存内容が不正です")
        qno = as_int(it.get("no"), "問題番号")
        if not 1 <= qno <= 9999:
            raise BadRequest(f"問題番号が範囲外です: {qno}")

    if conf["mode"] == "pipeline":
        by_year = {}
        for it in items:
            by_year.setdefault(as_int(it.get("year"), "年度"), []).append(it)
        plans = []
        for y, group in by_year.items():                    # ① 先に全部検証する
            path, data = _pl_load(conf, subject, y)
            changed = 0
            for it in group:
                setk, delk = clean_entry(it.get("entry") or {}, "pipeline")
                if _pl_apply(data, as_int(it["no"], "問題番号"), setk, delk)[0]:
                    changed += 1
            plans.append((path, data, y, changed))
        saved, touched, last = 0, [], {}
        for path, data, y, changed in plans:                # ② 通ってから書く
            if changed:
                backup(path)
                _pl_write(path, data)
                touched.append(y)
                saved += changed
        if single:
            it = items[0]
            last = _pl_load(conf, subject, it["year"])[1].get(str(as_int(it["no"], "問題番号")), {})
            return {"changed": bool(saved), "entry": last, "saved": saved,
                    "years": sorted(touched)}
        return {"saved": saved, "years": sorted(touched)}

    # direct: ファイルは1つ。全件を検証してからまとめて書く
    path, data = _dr_load(conf)
    changed = 0
    for it in items:
        y, qno = as_int(it.get("year"), "年度"), as_int(it["no"], "問題番号")
        i = _dr_find(data, y, qno)
        if i < 0:
            raise BadRequest(f"その問題がありません: {y}年 問{qno}")
        # 記述式の問題集か、もともと選択肢の無い問題なら、選択肢0個のまま保存できる
        empty_ok = bool(conf.get("essay")) or not (data[i].get("choices") or [])
        setk, delk = clean_entry(it.get("entry") or {}, "direct",
                                 _dr_labels(data[i]), empty_ok)
        if _dr_apply(data[i], setk, delk):
            changed += 1
    if changed:
        backup(path)
        write_json(path, data, indent=None)
    if single:
        it = items[0]
        i = _dr_find(data, as_int(it["year"], "年度"), as_int(it["no"], "問題番号"))
        return {"changed": bool(changed), "entry": _dr_entry(data[i]),
                "saved": changed, "years": [as_int(it["year"], "年度")]}
    return {"saved": changed, "years": sorted({as_int(i["year"], "年度") for i in items})}


# ---------------------------------------------------------------- 画像の自動割り当て
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
LEADING_NUM = re.compile(r"^\D*?(\d+)")


def _img_root(conf):
    """画像を探すフォルダと、データに書くときの相対の起点を返す。"""
    root = conf["data"].parent / "images"
    try:
        # ⚠️APPDIR は差し替えられることがあるので、両方 resolve() してから比べる
        #   （解決していないパスと比べると、同じ場所でも「外」と判定される）
        rel = root.resolve().relative_to(Path(APPDIR).resolve()).as_posix()
    except ValueError:                     # データがアプリの外にある構成
        rel = None
    return root, rel


# ---------------------------------------------------------------- 画像を受け取る
# ⚠️**ここはアプリで唯一「新しいファイルを作る」経路。名前も中身も信用しない。**
#   ・置き場所は data/images/<年>/ の中だけ。書く直前に、本当にその中かを確かめる
#   ・拡張子は白名簿。さらに**中身の先頭バイト**が拡張子と合っているかを見る
#     （`わるいもの.png` と名乗る別形式を弾く）
#   ・同じ名前があっても**上書きしない**（-2, -3 …を付ける）
MAX_IMAGE_BYTES = 12 * 1024 * 1024
BAD_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
IMG_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", {".png"}),
    (b"\xff\xd8\xff", {".jpg", ".jpeg"}),
    (b"GIF87a", {".gif"}),
    (b"GIF89a", {".gif"}),
]


def _looks_like_image(raw, ext):
    for magic, exts in IMG_MAGIC:
        if raw.startswith(magic):
            return ext in exts
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":       # webp は先頭が2つに分かれる
        return ext == ".webp"
    return False


def save_image(subject, year, filename, data_b64):
    """画像を `images/<年>/` に置いて、データに書く相対パスを返す。

    問題そのものには**紐づけない**。返したパスを画面の「画像」欄に足して、
    利用者が「保存」を押したときに初めて問題へ入る（保存の意味を変えない）。
    """
    import base64                                        # ここでしか使わない

    conf = subject_conf(subject)
    if conf["mode"] != "direct":
        raise BadRequest("この問題集では画像を追加できません。")
    y = as_int(year, "年度")
    root, rel = _img_root(conf)
    if rel is None:
        raise BadRequest("画像フォルダがアプリの外にあるため、追加できません。")

    # --- 名前 ---
    name = unicodedata.normalize("NFC", str(filename or "")).replace("\\", "/")
    name = name.split("/")[-1]                           # パス区切りは全部落とす
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in IMG_EXTS:
        raise BadRequest(f"画像として扱える形式ではありません: {ext or '（拡張子なし）'}"
                         f"（{'・'.join(sorted(IMG_EXTS))}）")
    stem = BAD_NAME_CHARS.sub("_", stem).strip(" .")[:80] or "image"

    # --- 中身 ---
    try:
        raw = base64.b64decode(str(data_b64 or ""), validate=True)
    except Exception:                                     # noqa: BLE001
        raise BadRequest("画像の中身を読めませんでした。")
    if not raw:
        raise BadRequest("画像が空です。")
    if len(raw) > MAX_IMAGE_BYTES:
        raise BadRequest(f"画像が大きすぎます（{len(raw)/1024/1024:.1f}MB）。"
                         f"{MAX_IMAGE_BYTES//1024//1024}MB までにしてください。")
    if not _looks_like_image(raw, ext):
        raise BadRequest("中身が画像として読めません（名前と形式が違うようです）。")

    ydir = root / str(y)
    ydir.mkdir(parents=True, exist_ok=True)
    dest, n = ydir / f"{stem}{ext}", 2
    while dest.exists():                                  # 上書きしない
        dest = ydir / f"{stem}-{n}{ext}"
        n += 1
    # ⚠️最後にもう一度、書き込み先が画像フォルダの中かを確かめる（保険）
    if root.resolve() not in dest.resolve().parents:
        raise BadRequest("画像フォルダの外には書き込めません。")
    dest.write_bytes(raw)
    return {"path": f"{rel}/{y}/{dest.name}", "name": dest.name, "bytes": len(raw)}


def assign_images(subject, mode="fill", apply=False):
    """`images/<年>/` を見て、ファイル名の先頭の数字を問題番号として割り当てる。

    切り出した画像は `001-image01.png` のように **番号が名前に入っている**のがふつう。
    それを使えば、テキストに画像の行を1つも書かなくてよくなる。

    mode = "fill"（既定・画像が入っていない問題だけ） / "replace"（すべて置き換え）
    """
    conf = subject_conf(subject)
    if conf["mode"] != "direct":
        raise BadRequest("この問題集では画像の割り当てはできません。")
    if mode not in ("fill", "replace"):
        raise BadRequest(f"知らない割り当て方です: {mode}")
    root, rel = _img_root(conf)
    if rel is None:
        raise BadRequest("画像フォルダがアプリの外にあるため、割り当てできません。")
    if not root.is_dir():
        raise BadRequest(f"画像フォルダがありません: {rel}/<年>/ を作って画像を入れてください。")

    path, data = _dr_load(conf)
    by_year = {}
    for q in data:
        by_year.setdefault(int(q.get("year", 0)), {})[int(q.get("no", 0))] = q

    plan = {"years": [], "assigned": 0, "questions": 0, "matched": 0, "skipped": [],
            "applied": False, "folder": rel}
    changed = False
    for ydir in sorted(p for p in root.iterdir() if p.is_dir()):
        if not re.fullmatch(r"\d{1,4}", ydir.name):
            plan["skipped"].append(f"{ydir.name}/（年の名前になっていないフォルダ）")
            continue
        y = int(ydir.name)
        qs = by_year.get(y)
        if not qs:
            plan["skipped"].append(f"{ydir.name}/（その年の問題がありません）")
            continue
        found, unmatched = {}, []
        for f in sorted(ydir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in IMG_EXTS:
                continue
            m = LEADING_NUM.match(f.name)
            no = int(m.group(1)) if m else None
            if no is None or no not in qs:
                unmatched.append(f.name)
                continue
            found.setdefault(no, []).append(f"{rel}/{ydir.name}/{f.name}")
        target = [no for no in sorted(found)
                  if mode == "replace" or not (qs[no].get("images") or [])]
        plan["years"].append({"year": y, "questions": len(target),
                              "images": sum(len(found[n]) for n in target),
                              "unmatched": len(unmatched),
                              "examples": unmatched[:5]})
        plan["questions"] += len(target)
        plan["assigned"] += sum(len(found[n]) for n in target)
        plan["matched"] += sum(len(v) for v in found.values())
        plan["skipped"] += [f"{ydir.name}/{u}" for u in unmatched[:20]]
        if apply:
            for no in target:
                if qs[no].get("images") != found[no]:
                    qs[no]["images"] = found[no]
                    changed = True
    if apply:
        # ⚠️「割り当て先が0」と「そもそも1枚も見つからない」を分ける。
        #   2回目に押したときは前者（もう全部入っている）で、失敗ではない。
        if not plan["matched"]:
            raise BadRequest("割り当てられる画像がありませんでした。"
                             f"{rel}/<年>/ にファイルがあるか、名前の先頭が問題番号か確かめてください。")
        if changed:
            backup(path)
            write_json(path, data, indent=None)
        plan["applied"] = True
    return plan


# ---------------------------------------------------------------- 問題集の名前
NAME_FIELDS = {"name": ("名前", 60), "short": ("短い名前", 20), "src": ("出典の説明", 200)}


def rename_subject(subject, values):
    """問題集の名前を変える（direct のみ）。

    自分の問題を入れたのに「サンプル問題集」のままになるのを、
    設定ファイルを開かずに直せるようにするため。
    ⚠️`file` と `pkey` は変えない。pkey を変えると学習記録が読めなくなる。
    """
    conf = subject_conf(subject)
    if conf["mode"] != "direct":
        raise BadRequest("この問題集の名前はここからは変えられません。")
    if not isinstance(values, dict):
        raise BadRequest("入力が不正です")
    unknown = set(values) - set(NAME_FIELDS)
    if unknown:
        raise BadRequest(f"扱えない項目です: {', '.join(sorted(unknown))}")
    clean = {}
    for k, v in values.items():
        label, limit = NAME_FIELDS[k]
        if not isinstance(v, str):
            raise BadRequest(f"{label}は文字列で指定してください")
        v = clean_text(v).replace("\n", " ").strip()
        if k in ("name", "short") and not v:
            raise BadRequest(f"{label}は空にできません。")
        if len(v) > limit:
            raise BadRequest(f"{label}が長すぎます（{limit}文字まで）。")
        clean[k] = v
    if not clean:
        return {"changed": False}

    raw = _read_json(CONFIG, CONFIG.name)
    if subject not in raw or not isinstance(raw[subject], dict):
        raise BadRequest(f"{CONFIG.name} に {subject} がありません。")
    before = dict(raw[subject])
    raw[subject].update(clean)
    if raw[subject] == before:
        return {"changed": False}
    backup(CONFIG)
    write_json(CONFIG, raw, indent=2)
    _cache.update(mtime=None, subjects={})          # 読み直させる
    return {"changed": True, **clean}


# ---------------------------------------------------------------- 問題の追加・削除（direct のみ）
def add_question(subject, year, qno=None):
    conf = subject_conf(subject)
    if conf["mode"] != "direct":
        raise BadRequest("この科目では問題を追加できません（問題は元データ側の持ち物です）。")
    path, data = _dr_load(conf)
    y = as_int(year, "年度")
    same = [int(q["no"]) for q in data if int(q.get("year", -1)) == y]
    n = as_int(qno, "問題番号") if qno not in (None, "") else (max(same) + 1 if same else 1)
    if not 1 <= n <= 9999:
        raise BadRequest(f"問題番号が範囲外です: {n}")
    if n in same:
        raise BadRequest(f"{y}年 問{n} はすでにあります。")
    # ⚠️解答の既定は**空**。編集画面が「解答が空のときは採点しない」と案内している
    #   のに ["a"] を既定にすると、空のつもりで保存した人が a を正解にしてしまう。
    q = {"id": f"{y}-{n}", "year": y, "no": n, "stem": "（ここに問題文を書いてください）",
         "choices": ([] if conf.get("essay") else          # 記述式の問題集は選択肢なしで作る
                     [{"label": l, "text": f"選択肢 {l}"} for l in "abcde"]),
         "images": [], "answer": [], "flag": True, "expl": ""}
    data.append(q)
    data.sort(key=lambda x: (int(x.get("year", 0)), int(x.get("no", 0))))
    backup(path)
    write_json(path, data, indent=None)
    return {"added": True, "year": y, "no": n, "id": q["id"], "entries": len(data)}


def delete_question(subject, year, qno):
    conf = subject_conf(subject)
    if conf["mode"] != "direct":
        raise BadRequest("この科目では問題を削除できません（問題は元データ側の持ち物です）。")
    path, data = _dr_load(conf)
    y, n = as_int(year, "年度"), as_int(qno, "問題番号")
    i = _dr_find(data, y, n)
    if i < 0:
        raise BadRequest(f"その問題がありません: {y}年 問{n}")
    gone = data.pop(i)
    backup(path)
    write_json(path, data, indent=None)
    return {"deleted": True, "year": y, "no": n,
            "stem": (gone.get("stem") or "")[:60], "entries": len(data)}


# =================================================================
#  テキストからの取り込み（direct のみ）
#
#  メモ帳で書ける形から問題データを作る。JSONを手で書かせないため。
#  書式は 同梱の データ形式.md と data/sample.txt を参照。
# =================================================================
RE_YEAR = re.compile(r"^\s*(?:[#＃]+\s*|=+\s*|【)?\s*(?:年|セット)?\s*[:：]?\s*"
                     r"(\d{4})\s*(?:年|回)?\s*(?:】|=+)?\s*$")
RE_QNO = re.compile(r"^\s*(?:第\s*)?(?:問\s*(\d+)|(\d+)\s*[.．、)）:：])\s*(.*)$")
# 「0.035インチ」「1.5テスラ」のような小数。問題番号と区別するため
RE_DECIMAL = re.compile(r"^\s*\d+[.．]\d")
# ⚠️記号は全角で打たれる（IMEやExcelの自動変換）。**記号の位置だけ**半角に寄せる。
#   行ごと NFKC すると本文の半角カナまで変わるので、拾った1文字だけを直す。
RE_CHOICE = re.compile(r"^\s*([A-Za-zＡ-Ｚａ-ｚ])[ 　	.．、,)）:：]\s*(.+)$")


def _ascii(text):
    """全角の英数字だけを半角に寄せる（記号の位置に使う）。"""
    return unicodedata.normalize("NFKC", str(text or ""))
# ⚠️見出し語は「区切り必須」か「中身が記号だけ」に限る。緩くすると本文を食う。
#   実例= 「図は、このアプリの…」という問題文が、画像の指定として飲み込まれた。
RE_ANSWER = re.compile(r"^\s*(?:答え|解答|正解|answer)\s*[:：]?\s*"
                       r"([A-Za-zＡ-Ｚａ-ｚ][A-Za-zＡ-Ｚａ-ｚ,、，・･\s　]*|)\s*$", re.I)
RE_IMAGE = re.compile(r"^\s*(?:画像|図|image)\s*[:：]\s*(.+)$", re.I)
RE_SOURCE = re.compile(r"^\s*(?:出典|参考|source)\s*[:：]\s*(.*)$", re.I)
RE_FLAG = re.compile(r"^\s*(?:要確認|要検討)\s*$")
RE_EXPL = re.compile(r"^\s*(?:解説|説明|explanation)\s*[:：]?\s*(.*)$", re.I)


def _q_finish(q, errors, essay=False):
    """1問ぶんを組み立てて検証する。問題があれば errors に足して None を返す。

    essay=True は「記述式の問題集へ取り込むとき」だけ。選択肢0個を通す。
    """
    line = q["_line"]
    stem = "\n".join(q["_stem"]).strip()
    if not stem:
        errors.append({"line": line, "message": f"{q['year']}年 問{q['no']}: 問題文がありません。"
                                                "問題番号の行の次から書いてください。"})
        return None
    if len(q["_choices"]) < 2 and not (essay and not q["_choices"]):
        errors.append({"line": line, "message": f"{q['year']}年 問{q['no']}: 選択肢が"
                       f"{len(q['_choices'])}個しかありません。「a 選択肢の文」の形で2つ以上。"})
        return None
    labels = [c["label"] for c in q["_choices"]]
    if len(set(labels)) != len(labels):
        errors.append({"line": line, "message": f"{q['year']}年 問{q['no']}: 選択肢の記号が"
                                                "重複しています。"})
        return None
    try:
        answer = norm_answer(q["_answer"], "".join(labels))
    except BadRequest as e:
        errors.append({"line": line, "message": f"{q['year']}年 問{q['no']}: {e}"})
        return None
    try:
        images = parse_images(q["_images"])
    except BadRequest as e:
        errors.append({"line": line, "message": f"{q['year']}年 問{q['no']}: {e}"})
        return None
    out = {"id": f"{q['year']}-{q['no']}", "year": q["year"], "no": q["no"], "stem": stem,
           "choices": q["_choices"], "images": images,
           "answer": [c for c in answer.split(",") if c],
           "flag": q["_flag"], "expl": "\n".join(q["_expl"]).strip()}
    if q["_sources"]:
        out["sources"] = q["_sources"]
    return out


def parse_text(text, default_year=None, essay=False):
    """テキストを問題のリストにする。返り値 (問題のリスト, エラーのリスト)。"""
    if not isinstance(text, str):
        raise BadRequest("取り込む内容が文字列ではありません。")
    text = clean_text(text).lstrip("﻿")
    year = int(default_year) if default_year else None
    questions, errors, q, in_expl = [], [], None, False

    def close():
        nonlocal q, in_expl
        if q is not None:
            built = _q_finish(q, errors, essay)
            if built:
                questions.append(built)
        q, in_expl = None, False

    for i, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip()
        # 年だけの行は、解説の途中であっても必ず区切りとして扱う。
        # （「解説の中だけ除く」にすると、解説で終わる問題の次の年が効かなくなる）
        m = RE_YEAR.match(line)
        if m:
            close()
            year = int(m.group(1))
            continue
        m = RE_QNO.match(line)
        start = bool(m and (m.group(1) or m.group(2)))
        if start and not m.group(1):
            # ここは「問」も「第」も付かない、数字だけで始まる行。
            # 数字で始まる**本文**と見分けがつかないので、次の2つは問題番号にしない。
            n = int(m.group(2))
            # ⚠️①小数と、桁の大きすぎる数。問題番号になりえない。
            #   「0.035インチのガイドワイヤー…」が問0、
            #   問題文が折り返した「2018）はどれか。1つ選べ。」が問2018 として
            #   切り出され、本来の問題が「問題文なし」で落ちた。
            if not 1 <= n <= 999 or RE_DECIMAL.match(line):
                start = False
            # ⚠️②解説の中の「1. …」「2. …」は箇条書きであって次の問題ではない。
            #   問題番号は増えていくので、増えていなければ解説の続きとみなす。
            #   （実データで、番号つきの箇条書きを含む解説が「問1〜問5」として
            #     切り出され、取り込みが止まった）
            elif in_expl and q is not None and n <= q["no"]:
                start = False
        if start:
            close()
            if year is None:
                errors.append({"line": i, "message": "年（またはセットの番号）が決まっていません。"
                               "ファイルの先頭に「2024」のように年だけの行を置いてください。"})
                year = 0
            q = {"year": year, "no": int(m.group(1) or m.group(2)), "_line": i,
                 "_stem": [], "_choices": [], "_images": [], "_sources": [],
                 "_answer": "", "_flag": False, "_expl": []}
            if m.group(3).strip():
                q["_stem"].append(m.group(3).strip())
            continue
        if q is None:
            if line.strip():
                errors.append({"line": i, "message": f"問題の外に文章があります: 「{line.strip()[:24]}」"
                               "（「問1」や「1.」で始まる行から問題が始まります）"})
            continue
        # --- ここから1問の中 ---
        m = RE_SOURCE.match(line)          # 出典は解説の中でも拾う（【出典】とは別）
        if m:
            if m.group(1).strip():
                q["_sources"].append(m.group(1).strip())
            continue
        if in_expl:
            q["_expl"].append(line)
            continue
        m = RE_EXPL.match(line)
        if m:
            in_expl = True
            if m.group(1).strip():
                q["_expl"].append(m.group(1).strip())
            continue
        if RE_FLAG.match(line):
            q["_flag"] = True
            continue
        m = RE_ANSWER.match(line)
        if m:
            q["_answer"] = m.group(1)
            continue
        m = RE_IMAGE.match(line)
        if m:
            q["_images"].append(m.group(1))
            continue
        m = RE_CHOICE.match(line)
        if m:
            q["_choices"].append({"label": _ascii(m.group(1)).lower(),
                                  "text": m.group(2).strip()})
            continue
        if line.strip():
            if q["_choices"]:
                errors.append({"line": i, "message": f"{q['year']}年 問{q['no']}: 選択肢の途中に"
                               f"読めない行があります: 「{line.strip()[:24]}」"})
            else:
                q["_stem"].append(line.strip())
    close()

    # 年が決まらないまま作られた問題は、確認画面に「0年 問1」と出て紛らわしいので落とす
    # （落とした理由はすでにエラーとして出している）
    questions = [x for x in questions if x["year"]]

    seen = {}
    for x in questions:
        key = (x["year"], x["no"])
        if key in seen:
            errors.append({"line": 0, "message": f"{x['year']}年 問{x['no']} が2回出てきます。"})
        seen[key] = True
    return questions, errors


def import_text(subject, text, mode="add", apply=False, default_year=None):
    """テキストを取り込む。apply=False なら「何が起きるか」だけを返す（実行しない）。"""
    conf = subject_conf(subject)
    if conf["mode"] != "direct":
        raise BadRequest("この科目ではテキストからの取り込みはできません。")
    if mode not in ("add", "replace"):
        raise BadRequest(f"知らない取り込み方です: {mode}")
    incoming, errors = parse_text(text, default_year, bool(conf.get("essay")))
    path, data = _dr_load(conf)
    have = {(int(x.get("year", -1)), int(x.get("no", -1))) for x in data}
    new = [x for x in incoming if (x["year"], x["no"]) not in have]
    dup = [x for x in incoming if (x["year"], x["no"]) in have]
    plan = {"parsed": len(incoming), "new": len(new), "overwrite": len(dup),
            # 選択肢なしで入るものは件数を出す（記述式の問題集でだけ起こりうる）
            "noChoices": sum(1 for x in incoming if not x["choices"]),
            "removed": (len(data) if mode == "replace" else 0),
            "years": sorted({x["year"] for x in incoming}),
            "errors": errors,
            "conflicts": [f"{x['year']}年 問{x['no']}" for x in dup[:20]],
            "samples": [{"year": x["year"], "no": x["no"], "stem": x["stem"][:60],
                         "choices": len(x["choices"]),
                         "answer": ",".join(x["answer"]) or "（なし）"} for x in incoming[:5]],
            "applied": False}
    if not apply or errors or not incoming:
        if apply and errors:
            raise BadRequest("直せていない行があるので取り込みませんでした。")
        if apply and not incoming:
            raise BadRequest("取り込める問題が1問もありませんでした。書式を確認してください。")
        return plan

    if mode == "replace":
        data = list(incoming)
    else:
        keep = {(x["year"], x["no"]) for x in incoming}
        data = [x for x in data if (int(x.get("year", -1)), int(x.get("no", -1))) not in keep]
        data += incoming
    data.sort(key=lambda x: (int(x.get("year", 0)), int(x.get("no", 0))))
    backup(path)
    write_json(path, data, indent=None)
    plan["applied"] = True
    plan["total"] = len(data)
    return plan


# ---------------------------------------------------------------- テンプレート検査
SECTION_RE = re.compile(r"【(.+?)】")
CHOICE_RE = re.compile(r"^([a-h])[ 　]")


def check_template(explanation, labels=None, flag=False, sources=None):
    """保存を拒否せず、足りないところを助言として返す。"""
    text = (explanation or "").replace("\r\n", "\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    heads = set(SECTION_RE.findall(text))
    found = {m.group(1) for m in (CHOICE_RE.match(l) for l in lines) if m}
    # ⚠️labels が空文字なら「選択肢が無い問題（記述式）」。None（不明）と区別する。
    #   ここを `labels or "abcde"` にすると、記述式のたびに
    #   「選択肢 a〜e が行頭に無い」と助言してしまう。
    want = list("abcde" if labels is None else labels)
    notes = []
    if not text.strip():
        return ["解説が空です。"]
    if "総論" not in heads and not (not want and "模範解答" in heads):
        notes.append("【総論】の見出しがありません。" if want else
                     "【模範解答】または【総論】の見出しがありません。")
    missing = [l for l in want if l not in found]
    if missing:
        notes.append("選択肢が行頭に無いものがあります: " + "・".join(missing)
                     + "（「a ○○ …×（理由）」の形で1行ずつ）")
    marked = [l for l in lines if CHOICE_RE.match(l) and re.search(r"[○×△]", l)]
    if found and len(marked) < len(found):
        notes.append("○×△が付いていない選択肢の行があります。")
    if flag:
        # 選択肢が無い問題（記述式）は「答えが割れている」型ではないので【結論】は求めない
        if want and "結論" not in heads:
            notes.append("要確認の問題です。【結論】に、どちらを採るか・なぜ割れるかを書いてください。")
        if not (sources or "出典" in heads or "http" in text):
            notes.append("要確認の問題です。出典（URLまたは資料名）を入れてください。")
    return notes


# ------------------------------------------------------------ 他のページに反映
def rebuild(subject, timeout=900):
    """その科目の rebuild_all.py を実行する。引数は受け取らない（固定のスクリプトのみ）。"""
    conf = subject_conf(subject)
    if conf["mode"] != "pipeline":
        raise BadRequest("この科目は保存した時点で反映されています（再生成は要りません）。")
    script = conf.get("rebuild")
    if not script or not script.is_file():
        raise BadRequest(f"再生成スクリプトがありません: {script}")
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, str(script)], cwd=str(script.parent.parent),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1,
                "log": f"再生成が {timeout} 秒で終わりませんでした。コンソールで "
                       f"python {script.name} を直接実行して確認してください。"}
    log = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    if len(log) > 20000:
        log = log[:8000] + "\n……（中略）……\n" + log[-8000:]
    return {"ok": r.returncode == 0, "code": r.returncode, "log": log.strip()}
