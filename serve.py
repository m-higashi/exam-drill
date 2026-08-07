"""練習アプリの配信と、編集モードのAPI。

待受け先・ポート・配信するフォルダは、隣の `server_config.txt` で決める
（プログラムは触らない）。ip を空欄にすると Tailscale の IP を自動で探し、
見つからなければ 0.0.0.0 で待ち受ける。

  python serve.py            設定ファイルどおりに起動
  python serve.py 9000       ポートだけその場で上書き
  python serve.py 9000 127.0.0.1
"""
import http.server, socketserver, subprocess, sys, os, functools, json, socket
from pathlib import Path

# 編集モード（解説オーサリング）のサーバー側。app/authoring.py が無い環境では
# 読み取り専用の静的サーバーとして動く（配布・公開版はこのモジュールを同梱しない）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import authoring
except Exception as _e:                       # noqa: BLE001
    authoring = None
    _AUTH_ERROR = _e
else:
    _AUTH_ERROR = None

# make console output safe on Japanese (cp932) terminals — otherwise the ▶ / JP
# text below raises UnicodeEncodeError and the server never starts.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================================
#  設定は原則 server_config.txt を編集してください（プログラムは触らない）。
#    ip   … 空欄=自動検出（tailscale ip -4）。固定したい時だけ記入。
#    port … 待受けポート。
#    root … 配信するフォルダ（このファイルからの相対）。
#  優先順位: コマンドライン引数 > 環境変数(EXAM_IP/EXAM_PORT) > server_config.txt > 既定値 > 自動検出
#    例) python serve.py 9000 100.x.y.z   （その場限りの上書き）
# ============================================================
TAILSCALE_IP = ""          # 既定（通常は空＝自動検出。恒久設定は server_config.txt へ）
PORT = 8787                # 既定ポート

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "server_config.txt"
# 配信するフォルダ。既定は serve.py と同じ場所。画像などを1つ上から配りたいときは
# server_config.txt に root=.. と書く（このファイルからの相対）。
ROOT = HERE


def load_config():
    """server_config.txt を読む（key=value、# はコメント）。無ければ空。

    ⚠️**行の途中に書かれた # 以降もコメントとして落とす。** 説明文を値ごと
    読み込むと `ip=127.0.0.1  # …` が丸ごとアドレス扱いになり、起動時に
    socket.gaierror で落ちる（Mac実機の検証で実際に踏んだ）。
    BOM付きで保存されても読めるように utf-8-sig で開く。
    """
    cfg = {}
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.split("#", 1)[0].strip()      # 行内コメントを落とす
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip().lower()] = v.strip()
    except Exception:
        pass
    return cfg


_cfg = load_config()
# ⚠️設定の値は利用者が手で書く。ここで例外を投げると、起動前の traceback になる。
try:
    _root = (HERE / (_cfg.get("root") or ".")).resolve()
    _ok = (_root / "index.html").exists() or (_root / "app" / "index.html").exists()
except Exception:                                  # noqa: BLE001
    _root, _ok = HERE, False
if _ok:
    ROOT = _root
elif _cfg.get("root") not in (None, "", "."):
    print(f"(!) 設定の root={_cfg.get('root')!r} に index.html が見当たりません。"
          f"{HERE.name} フォルダを配信します。")


def _pick_port():
    if len(sys.argv) > 1:
        src = sys.argv[1]
    else:
        src = os.environ.get("EXAM_PORT") or _cfg.get("port") or PORT
    try:
        return int(str(src).strip())
    except (TypeError, ValueError):
        return PORT


PORT = _pick_port()
# IP: CLI arg2 > 環境変数 > 設定ファイル > 既定定数（いずれも空なら後で自動検出）
_IP_OVERRIDE = (sys.argv[2] if len(sys.argv) > 2
                else (os.environ.get("EXAM_IP") or _cfg.get("ip") or TAILSCALE_IP)).strip()

def tailscale_ip():
    # コマンドの場所はOSで違う。macOSのApp Store版はPATHに入っていないことが多い。
    for exe in ("tailscale",
                r"C:\Program Files\Tailscale\tailscale.exe",
                "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
                "/usr/bin/tailscale"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5)
            ip = out.stdout.strip().splitlines()[0].strip()
            if ip and ip.count(".") == 3:
                return ip
        except Exception:
            continue
    return None

MAX_BODY = 4 * 1024 * 1024        # 保存1回あたりの上限（解説はテキストなので十分）
ALLOWED_HOSTS = set()             # main() で組み立てる（編集APIのHost検証用）


def build_allowed_hosts(bind):
    """編集APIを受け付けるHost名。このPC・待受けIP・tailnet名だけ。"""
    hosts = {"localhost", "127.0.0.1", "[::1]", "::1"}
    if bind and bind != "0.0.0.0":
        hosts.add(bind)
    try:
        hosts.add(socket.gethostname().lower())
    except Exception:
        pass
    return hosts


def host_allowed(header):
    """Hostヘッダ（name:port）を照合する。DNSリバインディング対策。"""
    if not header:
        return False
    name = header.rsplit(":", 1)[0].strip().lower() if not header.startswith("[") \
        else header.split("]")[0].lstrip("[").lower()
    return name in ALLOWED_HOSTS or name.endswith(".ts.net")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):            # quieter logs
        pass

    # ⚠️**更新時刻が過去に戻るファイルがある。** バックアップやZIPから戻すと、
    #   ファイルの日付は「作った当時」に戻る。すると If-Modified-Since に 304 を返し、
    #   ブラウザは自分の持っている**新しいキャッシュのほうを使い続ける**
    #   （Mac実機で実測: data/ を戻したのに画面が古いまま）。
    #   `no-cache` は「毎回確かめよ」であって、304 が返れば古い方が使われるので防げない。
    #   → 中身が変わりうるもの（データ・画面・設定）は **no-store ＋ 条件付き要求を無視**。
    #   画像は名前が同じなら中身も同じなので、これまでどおり no-cache（毎回落とすと重い）。
    FRESH_EXTS = {".json", ".csv", ".txt", ".html", ".htm", ".js", ".css", ".md"}

    def _must_be_fresh(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/api/") or path.endswith("/"):
            return True
        return Path(path).suffix.lower() in self.FRESH_EXTS

    def send_head(self):
        if self._must_be_fresh():
            del self.headers["If-Modified-Since"]     # 304 を返さない＝必ず中身を返す
            del self.headers["If-None-Match"]
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control",
                         "no-store" if self._must_be_fresh() else "no-cache")
        super().end_headers()

    # ---------------- 編集API（/api/…） ----------------
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_gate(self, writing):
        """編集APIの入口検査。通れば None、駄目なら理由を返して応答済みにする。"""
        if authoring is None:
            self._send_json(404, {"error": "編集モードはこの環境では使えません。"})
            return False
        if not host_allowed(self.headers.get("Host")):
            self._send_json(403, {"error": "このアドレスからは編集できません。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin.split("//", 1)[-1] != (self.headers.get("Host") or ""):
            self._send_json(403, {"error": "別サイトからの操作は受け付けません。"})
            return False
        if writing:
            # 独自ヘッダを必須にする＝他サイトのフォーム/簡易リクエストでは送れない
            if self.headers.get("X-Authoring") != "1":
                self._send_json(403, {"error": "別サイトからの操作は受け付けません。"})
                return False
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                self._send_json(415, {"error": "JSONで送ってください。"})
                return False
        return True

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            raise authoring.BadRequest("送信内容が大きすぎます。")
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise authoring.BadRequest("送信内容を読めませんでした。")
        # ⚠️JSONは辞書とは限らない（配列・数値・null が来る）。
        #   そのまま .get() を呼ぶと AttributeError で 500 になる。ここで1か所に集約する。
        return body if isinstance(body, dict) else {}

    def _dispatch(self, path, query, body, writing):
        A = authoring
        if path == "/api/authoring/config":
            return A.config()
        if path == "/api/authoring/entry":
            return {"entry": A.read_entry(query.get("subject", [""])[0],
                                          query.get("year", [""])[0],
                                          query.get("no", [""])[0])}
        if path == "/api/authoring/save":
            return A.save_entry(body.get("subject"), body.get("year"),
                                body.get("no"), body.get("entry") or {})
        if path == "/api/authoring/save-many":
            return A.save_many(body.get("subject"), body.get("items") or [])
        if path == "/api/authoring/check":
            src = body.get("sources") or []
            if isinstance(src, str):
                src = [s for s in src.splitlines() if s.strip()]
            return {"notes": A.check_template(body.get("explanation", ""),
                                              body.get("labels") or "abcde",
                                              bool(body.get("flag")), src)}
        if path == "/api/authoring/import":
            return A.import_text(body.get("subject"), body.get("text") or "",
                                 body.get("mode") or "add", bool(body.get("apply")),
                                 body.get("year"))
        if path == "/api/authoring/assign-images":
            return A.assign_images(body.get("subject"), body.get("mode") or "fill",
                                   bool(body.get("apply")))
        if path == "/api/authoring/rename":
            return A.rename_subject(body.get("subject"), body.get("values") or {})
        if path == "/api/authoring/add":
            return A.add_question(body.get("subject"), body.get("year"), body.get("no"))
        if path == "/api/authoring/delete":
            return A.delete_question(body.get("subject"), body.get("year"), body.get("no"))
        if path == "/api/authoring/rebuild":
            return A.rebuild(body.get("subject"))
        raise A.BadRequest("知らない操作です。")

    def _handle_api(self, writing):
        from urllib.parse import urlsplit, parse_qs
        u = urlsplit(self.path)
        try:
            body = self._read_json() if writing else {}
            self._send_json(200, self._dispatch(u.path, parse_qs(u.query), body, writing))
        except authoring.BadRequest as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:                                  # noqa: BLE001
            print(f"[編集API] {u.path} で失敗: {type(e).__name__}: {e}")
            self._send_json(500, {"error": f"サーバー側で失敗しました: {e}"})

    def do_GET(self):
        if self.path.split("?")[0].startswith("/api/"):
            if self._api_gate(writing=False):
                self._handle_api(writing=False)
            return
        super().do_GET()

    def do_HEAD(self):
        if self.path.split("?")[0].startswith("/api/"):
            self.send_error(405)
            return
        super().do_HEAD()

    def do_POST(self):
        if not self.path.split("?")[0].startswith("/api/"):
            self.send_error(405, "POST is not supported here")
            return
        if self._api_gate(writing=True):
            self._handle_api(writing=True)

    def do_OPTIONS(self):                 # CORSプリフライトには応じない
        self.send_error(403)

def bind_error(bind, err):
    """待受けに失敗したとき、tracebackでなく直せる案内を出して終わる。"""
    print("\n" + "=" * 60)
    print(f"サーバーを起動できませんでした（待受け {bind}:{PORT}）")
    print("=" * 60)
    errno_ = getattr(err, "errno", None)
    winerr = getattr(err, "winerror", None)      # Windowsは別の番号を返す
    if isinstance(err, socket.gaierror):
        print(f"\n  ipの値「{bind}」がアドレスとして読めません。")
        print(f"  {CONFIG_FILE} の ip= の行を確かめてください。")
        print("\n  正しい書き方（= の右に値だけ）:")
        print("      ip=127.0.0.1")
        print("      ip=              ← 空欄なら自動で決めます")
    elif errno_ == 13 and PORT < 1024:
        print(f"\n  ポート {PORT} は 1024 未満なので、管理者の権限が要ります。")
        print(f"  {CONFIG_FILE} の port= を 1024 以上（例 8787）にしてください。")
    elif errno_ in (48, 98, 13) or winerr in (10048, 10013):
        print(f"\n  ポート {PORT} はすでに他のプログラムが使っています。")
        print("  すでにこのアプリを起動していないか確かめるか、")
        print(f"  {CONFIG_FILE} の port= を別の番号（例 8788）に変えてください。")
    elif errno_ in (49, 99) or winerr == 10049:
        print(f"\n  アドレス「{bind}」はこのパソコンのものではありません。")
        print(f"  {CONFIG_FILE} の ip= を空欄にするか、127.0.0.1 にしてください。")
        print("  （Tailscale を使う設定なら、Tailscale が動いているか確かめてください）")
    else:
        print(f"\n  {type(err).__name__}: {err}")
        print(f"  {CONFIG_FILE} の設定を確かめてください。")
    print("\n直したら、起動し直してください。")
    sys.exit(1)


def main():
    global ALLOWED_HOSTS
    ip = _IP_OVERRIDE or tailscale_ip()               # 変数/環境変数で指定があれば優先
    bind = ip or "0.0.0.0"
    ALLOWED_HOSTS = build_allowed_hosts(bind)
    handler = functools.partial(Handler, directory=str(ROOT))

    # ⚠️**Windows では SO_REUSEADDR がポートの「横取り」を許す。**
    #   2回ダブルクリックすると2つ目も起動でき、どちらが答えるか分からなくなる
    #   （実測: 1つ目を止めても2つ目が答え続けた）。だから Windows では
    #   SO_EXCLUSIVEADDRUSE にして、こちらが使っている間は誰にも取らせない。
    #   POSIX で SO_REUSEADDR を外すと、Ctrl+C 直後の起動し直しが TIME_WAIT で
    #   弾かれるので、そちらは今までどおり True にする。
    class Server(socketserver.TCPServer):
        allow_reuse_address = (os.name != "nt")

        def server_bind(self):
            if os.name == "nt":
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    try:
                        self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                    except OSError:
                        pass                  # 使えない環境でも起動は続ける
            super().server_bind()

    try:
        httpd = Server((bind, PORT), handler)
    except Exception as e:                                # noqa: BLE001
        bind_error(bind, e)
    with httpd:
        print(f"serving {ROOT}")
        print(f"bind    {bind}:{PORT}")
        if authoring is not None:
            subs = authoring.config()["subjects"]
            if subs:
                print("編集モード  有効: " + "・".join(c["label"] for c in subs.values()))
            else:
                print("編集モード  無効（subjects.json に編集の設定がありません）")
        elif _AUTH_ERROR is not None and not isinstance(_AUTH_ERROR, ImportError):
            print(f"編集モード  無効（authoring.py の読み込みに失敗: {_AUTH_ERROR}）")
        # index.html が配信フォルダのどこにあるかで、開くURLが変わる
        path = "/" if ROOT == HERE else "/" + HERE.name + "/"
        if ip:
            print(f"\n▶ アプリURL:  http://{ip}:{PORT}{path}")
            if ip not in ("127.0.0.1", "localhost", "0.0.0.0"):
                # ⚠️このアドレスだけで待ち受けるので localhost では開けない。
                #   知らせておかないと「開けない」と誤解される（Mac実機で指摘された）
                print(f"   ※ このアドレスでだけ開けます。127.0.0.1 では開けません。")
                if ip.startswith("100."):
                    print("   （同じTailscaleネットワークのスマホ・タブレットからも開けます）")
        else:
            print(f"\n(!) 待受けアドレスを自動で決められませんでした。0.0.0.0 で待ち受けます。")
            print(f"    このPCからは  http://127.0.0.1:{PORT}{path}  で開けます。")
        print(f"\n設定（アドレス・ポート・配信フォルダ）は  {CONFIG_FILE}  を編集 → 保存 → 起動し直し。")
        print("終了するには、この画面で Ctrl+C を押してください。")
        httpd.serve_forever()

if __name__ == "__main__":
    main()
