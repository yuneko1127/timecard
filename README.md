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

### 環境変数の設定

`.env.example` を参考に、環境変数を設定してください：

**開発環境での例：**
```bash
source .venv/bin/activate
export INITIAL_LOGIN_ID="admin"
export INITIAL_PASSWORD="secure_password"
python app.py
```

または、ターミナルセッション内で直接設定：
```bash
INITIAL_LOGIN_ID="admin" INITIAL_PASSWORD="secure_password" python app.py
```

**本番環境（Render 等）での設定：**
- プラットフォームのダッシュボードで環境変数を設定してください
  - `DATABASE_URL` … 接続文字列（PostgreSQL）
  - `SECRET_KEY` … Flask セッション用のランダム文字列
  - `INITIAL_LOGIN_ID` … 初期ログインID
  - `INITIAL_PASSWORD` … 初期パスワード

既存の `timecard.db` がある場合は、別途ダンプ・リストア（`pgloader` や CSV 経由など）で移行してください。スキーマは `app.py` の `_init_db_postgres` / `_init_db_sqlite` を参照してください。

