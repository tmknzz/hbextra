# hbextra インストール手順

## 必要なもの
- Mac（macOS 12以降推奨）
- Python 3.9以上
- GitHub Desktop（https://desktop.github.com）

---

## 手順

### 1. GitHub Desktop をインストール

https://desktop.github.com からダウンロードしてインストールする。

GitHubアカウント（tamekuniz）でサインインする。

---

### 2. リポジトリをClone

GitHub Desktop を開いて：

`tamekuniz/hbextra` を Clone する。

保存先は任意のフォルダでOK。例：`/Users/（ユーザー名）/ClaudeCode/`

---

### 3. Pythonパッケージをインストール

ターミナルを開いて以下を実行：

```bash
python3 -m pip install -r requirements.txt
```

> `pip3` が見つからない場合は `python3 -m pip install -r requirements.txt` を試す

---

### 4. 起動する

```bash
cd /Users/（ユーザー名）/ClaudeCode/hbextra
python3 hbextra.py
```

起動後、ブラウザで以下にアクセス：

```
http://localhost:8000
```

---

### 5. 停止する

ターミナルで `Ctrl + C` を押す

---

## 最新版に更新するには

GitHub Desktop を開いて「**Fetch origin**」→「**Pull origin**」を押すだけ！

---

## トラブルシューティング

### 「flask が見つからない」エラーが出る
```bash
python3 -m pip install -r requirements.txt
```
を再実行する。それでも出る場合：
```bash
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

### ポート8000が使用中と出る
`hbextra.py` の末尾近くにある以下の行を探して、`8000` を別の番号（例：`8001`）に変更する：
```python
app.run(host=host, port=8000, ...)
```

### LAN内の他端末からアクセスしたい
通常は自分のMacからだけ使えるように起動します。LAN公開する場合だけ、以下のように明示して起動します：
```bash
HBEXTRA_HOST=0.0.0.0 python3 hbextra.py
```

---

## データについて
- 記事データは `hbextra.db`（SQLite）に自動保存される
- スター・非表示の情報も `hbextra.db` に入っている
- データのバックアップはアプリ内の「エクスポート」ボタンからも可能
