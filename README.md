# HBExtra

はてなブックマークのホットエントリー・新着エントリーを閲覧するRSSリーダー。

## 必要なもの

- Python 3.9以上
- インターネット接続（RSSフィード取得用）

## インストール

### Mac

```bash
# リポジトリをクローン
git clone https://github.com/tamekuniz/hbextra.git
cd hbextra

# 依存パッケージをインストール
python3 -m pip install -r requirements.txt
```

### Windows

1. Python 3をインストール（https://www.python.org/downloads/）
   - **「Add Python to PATH」にチェックを入れること**

```cmd
# リポジトリをクローン
git clone https://github.com/tamekuniz/hbextra.git
cd hbextra

# 依存パッケージをインストール
python -m pip install -r requirements.txt
```

> `pip` が見つからない場合は `python -m pip install -r requirements.txt` を試してください。

## 起動

### Mac

```bash
python3 hbextra.py
```

### Windows

```cmd
python hbextra.py
```

ブラウザで http://localhost:8000 を開く。初回アクセス時にユーザー登録画面が表示されます。

標準ではこのMac/PCからだけアクセスできます。LAN内の他端末から使う場合は `HBEXTRA_HOST=0.0.0.0 python3 hbextra.py` のように明示して起動してください。

## 公開運用

- 初回ユーザー作成後、追加ユーザー登録には `HBEXTRA_REGISTRATION_TOKEN` が必要です。
- HTTPS 配下で公開する場合は `HBEXTRA_COOKIE_SECURE=1` と `HBEXTRA_HSTS=1` を設定してください。
- 本番起動は `gunicorn --workers 1 --bind 127.0.0.1:8001 wsgi:application` を推奨します。

## 依存パッケージ

| パッケージ | 必須 | 用途 |
|-----------|------|------|
| Flask | Yes | Webサーバー |
| pykakasi | No | 日本語ひらがな変換（検索精度向上） |
| defusedxml | Yes | RSS XMLの安全な解析 |
| gunicorn | Production | 本番WSGIサーバー |

## 機能

- 新着/人気エントリーの一覧表示（10カテゴリ対応）
- キーボード操作（j/k移動、v プレビュー、s スター、d 非表示、b 開く）
- スター・非表示管理（ユーザーごとに独立）
- タグクラウド（期間・並び順変更、ローマ字検索）
- エクスポート・インポート
- マルチユーザー対応（ユーザー登録/ログイン）

## データベース

- `hbextra.db`（SQLite）がプロジェクトディレクトリに自動作成されます
- 記事データは全ユーザーで共有、スター・非表示はユーザーごとに管理されます

## トラブルシューティング

**ポート8000が使用中の場合：**
`hbextra.py` 末尾の `port=8000` を別のポート番号に変更してください。

**pykakasi のインストールに失敗する場合：**
pykakasi なしでも動作します。検索時のひらがな変換が無効になるだけです。

## ライセンス

MIT
