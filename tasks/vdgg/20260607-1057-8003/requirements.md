## Goal

HBExtra を第三者に使ってもらう前提で、AIB の `10_products/hbextra/security-audit-2026-06-07.md` に記録された脆弱性を修正する。

対象は全 findings:

- `/api/proxy` same-origin arbitrary HTML execution
- `/api/import` stored XSS / shared DB DoS
- open registration
- DNS rebinding / SSRF gap
- authentication and transport hardening
- local secret/DB file permissions
- memory/thread DoS
- unsafe XML parsing
- missing security headers
- unpinned dependencies
- scanner false-positive handling where appropriate

## Constraints

- UI デザインを変更しない。見た目の作り替え、装飾追加、レイアウト刷新は禁止。
- 全面置換しない。既存構造を尊重し、必要箇所だけ外科的に変更する。
- 既存の未コミット変更はユーザー作業として扱い、巻き戻さない。
- 1つのリスクごとに小さく実装し、確認してから次へ進む。
- 大きな新規フレームワークや重い依存は追加しない。
- 公開運用で危険なデフォルトは安全側へ倒す。ただし既存ローカル利用が壊れないよう、互換性を保てる箇所は環境変数で制御する。

## Acceptance criteria

- `/api/proxy` の直接 navigation で外部 HTML が HBExtra origin の通常権限を持たない。
- import された悪意ある URL/count/tags が XSS にならず、API 500 も起こさない。
- 初回ユーザー作成後、無条件の2人目登録が拒否される。
- SSRF 防御が DNS rebinding を考慮した形になる、または proxy の危険面が実質閉じられる。
- login/register/import/proxy/refresh の濫用耐性が改善される。
- `.secret_key` と DB/data dir が private permissions で作成される。
- RSS XML parse が `defusedxml` へ移る。
- 通常レスポンスに基本セキュリティヘッダが付く。
- 依存関係が再現可能な形で pin され、`pip-audit` を通せる。
- 自動テスト、起動確認、Bandit/pip-audit の結果を記録する。
