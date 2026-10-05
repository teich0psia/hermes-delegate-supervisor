# 開発・オフライン検証

## 必要なチェックアウト

プラグインの実行時Python依存は空ですが、ホスト結合テストには別途Hermesのソースと、その `venv` の検証用依存（pytest・setuptools・wheelなど）が必要です。確認した環境はPython 3.11の `venv/lib/python3.11/site-packages` を持ちます。現在のrunnerはこの配置を前提とし、全Python環境への可搬性は未確認です。

全テストのうちrouting結合の2件は、別チェックアウトの `hermes_delegate_routing` を読み取ります。`ROUTING_SOURCE` にそのルートを指定してください。未指定の既定パスは本repoと同階層の `hermes-delegate-routing` です。これはテスト専用の依存で、プラグインの実行時依存ではありません。routingソースがない場合は、その2件を除いた選択を使い、全件成功とは報告しないでください。

## focused検証

以下はプラグインrepoのルートで実行します。`HERMES_SOURCE` は監査したHermesのチェックアウト、`TMPDIR` は検証用scratchディレクトリに置き換えます。runnerの第2引数はプラグインのimport元、第3引数以降はpytestの選択です。

```sh
HERMES_SOURCE=/path/to/audited/hermes-agent
export TMPDIR="$HOME/.hermes/cache/scratch"
mkdir -p "$TMPDIR"
python scripts/verify_offline.py "$HERMES_SOURCE" "$PWD" \
  tests/test_core.py tests/test_supervision_command.py tests/test_busy_command.py
```

runnerはcredential-freeな子プロセス、scratchの `HOME`・`HERMES_HOME`、空の有効プラグイン設定を使います。pytestの自動プラグイン読込とlazy installを無効にし、bootstrap・live agentの不要なimportを拒否します。実際に解決したホストとプラグインのmoduleパスを表示し、指定先と一致することをassertします。

これらは認証・モデル推論・実transportを使わないテストです。未知のホスト全体を安全に実行するsandboxではないため、先にホストと対象importの挙動を確認してください。

## 全テストと配布物

routingのチェックアウトがある場合は次を使います。

```sh
export ROUTING_SOURCE=/path/to/reviewed/hermes-delegate-routing
python scripts/verify_offline.py "$HERMES_SOURCE" "$PWD"
python scripts/build_offline.py "$HERMES_SOURCE"
```

`build_offline.py` はネットワークで依存を取得せず、既存ホスト環境のsetuptoolsでwheel・sdistを生成します。展開wheelのfresh-process import、entry point、module・manifestのsource bytes、sdistのREADME・テスト・runnerのbytesを確認し、最後に展開wheelに対して全テストを実行します。環境へのpip installは行いません。生成先はgitignore対象の `dist/`・`build/`・`*.egg-info/` です。

ビルドだけを行う場合は、適切な依存をあらかじめ用意した隔離環境でPEP 517 backendを直接呼び出せます。

```sh
python -c 'from setuptools.build_meta import build_wheel, build_sdist; build_wheel("dist"); build_sdist("dist")'
```

この短いコマンドだけでは、展開wheelの検証やテスト実行まで行った証拠にはなりません。

## 公開用ソースの管理

公開用ブランチは、旧ローカル運用履歴とは独立したroot commitから始まります。製品module・manifest・テストは既存の受入版を維持し、文書と開発スクリプトの個人パスだけを公開向けに整理しています。生の有効化ログ、ホスト・アカウント情報、セッションや独立レビューの識別子は公開ソースに含めません。

今後の公開向け修正は公開用ブランチから分岐してください。旧ローカル履歴のmergeや `push --all`・`push --mirror` は、その履歴も公開するため避けてください。必要な変更だけを差分として移し、ファイルとコミットmetadataを再確認します。公開用文書・テストに実在のトークン・会話ID・個人環境のログを貼らないでください。
