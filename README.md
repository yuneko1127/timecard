# タイムカードアプリ

Flask で動く日本語タイムカードアプリです。**本番利用では PostgreSQL** を想定しています（環境変数 `DATABASE_URL`）。`DATABASE_URL` が無い場合のみ、開発用にローカル SQLite（`timecard.db`）を使います。

## セットアップ（.venv）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## データベース

### PostgreSQL（推奨・本番）

1. PostgreSQL 16 相当のインスタンスを用意し、接続 URL を取得する。
2. 環境変数を設定する。

```bash
export DATABASE_URL="postgresql://ユーザー:パスワード@ホスト:5432/データベース名"
export SECRET_KEY="ランダムな長い文字列"   # 本番では必須
python app.py
```

初回起動時にテーブル作成と初期ユーザー・デフォルト会社の投入が行われます。

**Docker でローカル Postgres を立ち上げる例**

```bash
docker compose up -d
export DATABASE_URL="postgresql://timecard:timecard@127.0.0.1:5432/timecard"
python app.py
```

### SQLite（開発のみ）

`DATABASE_URL` を設定せずに起動すると、プロジェクト直下の `timecard.db` を使います。

## 起動

```bash
source .venv/bin/activate
python app.py
```

`http://127.0.0.1:5000` にアクセスしてください。

## SQLite から PostgreSQL へデータ移行

既存の `timecard.db` がある場合は、別途ダンプ・リストア（`pgloader` や CSV 経由など）で移行してください。スキーマは `app.py` の `_init_db_postgres` / `_init_db_sqlite` を参照してください。

## 初期ログイン情報

- ログインID: `yuneko`
- パスワード: `timecard123`

（DB が空のときだけ自動作成されます。）

---

## 本番デプロイ例: **Supabase（PostgreSQL）+ Gunicorn（別ホスト）**

このアプリは **DB とアプリを別サービスに分ける**前提で問題なく動きます。

1. **[Supabase](https://supabase.com)** でプロジェクトを作成する。  
2. **Project Settings → Database** などから **Connection string（URI）** をコピーする（**Session mode** / 直接 `5432` 向けが扱いやすいことが多い。接続プーラー `6543` はホストの説明に従う）。  
3. **Render / Railway / Fly.io / Koyeb** など任意のホストで Web サービスを作り、リポジトリを接続する。  
4. 環境変数:
   - `DATABASE_URL` … Supabase の URI（多くは **`?sslmode=require`** 付き）
   - `SECRET_KEY` … 長いランダム文字列（例: `python3 -c "import secrets; print(secrets.token_hex(32))"`）
5. **Build**: `pip install -r requirements.txt`  
   **Start**: `gunicorn app:app --bind 0.0.0.0:$PORT`  
   （`Procfile` と同内容）

初回デプロイ後にテーブル作成・初期ユーザー投入が走ります。Supabase の無料枠の上限は[公式料金](https://supabase.com/pricing)を確認してください。

### 「再起動されない・スリープしない」無料ホストはあるか

**結論: 無料のマネージド PaaS で「常に起動し続け、スリープもしない」を保証するものはほぼありません。**  
手動の「再起動」ではなく、**しばらくアクセスがないとプロセスが止まり、次のアクセスで起動し直す（コールドスタート）**仕組みが、Render・Railway（無料時のサーバーレス）などにあります。利用規約・料金は各公式を確認してください。

| 望む挙動 | 現実的な選択肢 |
|----------|----------------|
| **できるだけ安く** | 上記のような無料枠を使う。**最初の1リクエストだけ遅い**ことは許容する。 |
| **常時応答したい（スリープしたくない）** | そのホストの **有料プラン**（例: スリープ無効化できるプラン）、または **VPS**（[Oracle Cloud Always Free](https://www.oracle.com/cloud/free/)、さくらの VPS 等）に **自分で gunicorn + systemd** を載せる。 |

つまり **Supabase + 「別の gunicorn 用ホスト」** の組み合わせ自体は正しく、**スリープを避けたいなら無料 PaaS ではなく有料 or VPS** になる、という整理です。

### Gunicorn を載せるホストの例（いずれも `DATABASE_URL` に Supabase を指定）

- **[Render](https://render.com)** … Web Service のみ。無料枠はスリープしやすい。  
- **[Railway](https://railway.app)** … クレジット制。無料相当でもサーバーレス（スリープ）の説明がある。  
- **[Fly.io](https://fly.io)** … マシン常駐に近い運用もできるが、無料枠・料金は要確認。  
- **[Koyeb](https://www.koyeb.com)** … 同様に公式の制限を確認。

`postgres://` で始まる URL でも、このアプリは `postgresql://` に正規化して接続します。

### DB を Supabase 以外にする場合

**[Neon](https://neon.tech)** など別のマネージド Postgres にしても同様です（Neon はアイドル後に初回接続が遅くなることがあります）。
