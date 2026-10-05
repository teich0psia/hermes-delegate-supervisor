# hermes-delegate-supervisor

[English](README.md) | [日本語](README.ja.md)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) の `delegate_task` で実行中の子エージェントを、元の親エージェントが定期的に確認するためのプラグインです。プラグイン識別子は `delegate_supervisor`、既定の確認間隔は600秒（10分）です。

タイマーが期限を管理し、親に確認対象を渡します。親は必要に応じて進捗や証拠を調べ、既存の `delegate_task(action="steer", ...)` で追加指示します。別の監督モデルを起動する機能ではありません。タイマーと生存確認はモデルを呼びませんが、親の確認ターンには通常の推論コストが発生します。

このREADMEは **v0.2.0のソース**を説明します。v0.1.0には `/supervision` がありません。v0.2.0はオフライン検証済みの候補であり、実Gateway・Discord・CLI・TUI・Desktopでの配送と親の判断までを実運用確認した版ではありません。

## 監督する作業・しない作業

対象は、プラグインのロード後にトップレベルの会話から `delegate_task` で起動した子エージェントです。同じ親に複数の確認期限が重なった場合、一つの確認にまとめます。ロード前から実行中の子は遡って登録しません。

次の処理は既存のHermesに任せます。

- 子の起動・実行・停止、完了通知、停滞検出。
- 子のモデル・プロバイダー・推論設定・Fast設定の選択。
- 親が行う判断と、実行中の子への追加指示。

一般のバックグラウンドプロセス、cron、任意の作業一覧を監視する機能ではありません。ネストした委譲の親、API server、stateless・one-shotセッション、送信先を特定できない会話は対象外です。既存の `/heartbeat` の登録・変更も行いません。Heartbeatは会話ごとの任意の反復指示、本プラグインは実行中の委譲先に対する期限管理という別の用途です。

## 会話内の `/supervision`

コマンドはモデルを呼ばず、その会話の設定だけを表示・変更します。別会話・別プロファイル・設定ファイルには書き込みません。

| 入力 | 効果 |
|---|---|
| `/supervision` または `/supervision status` | 有効・無効、実効間隔、既定値・上書き、対象ID、親のbusy・idle、保留数、受付済み確認要求の有無を表示 |
| `/supervision 10m` | この会話の間隔を上書きし、自動確認を有効化 |
| `/supervision off` | 自動確認だけを無効化。直前の間隔と子の登録を保持 |
| `/supervision default` | 間隔と無効化の上書きを解除し、ロード時の既定間隔で有効化 |

間隔は `s`・`m`・`h` 付きの整数または小数です。`30s`、`1.5m`、`1h` を指定でき、最小は1秒です。単位なしの値や `1d` は受け付けません。短い間隔は親の確認ターンと推論コストを増やします。

子を起動する前にも設定できます。ただしGatewayに会話のセッションがまだ存在しない場合は、先に通常のメッセージを送る必要があります。プラグインはコマンドのために新規セッションを作りません。

### 間隔変更と停止の違い

間隔・`default` の変更は、子の登録、親の状態、保留中の確認、受付済み要求を保持します。各子の次の期限はコマンド受付時刻から新しい間隔で計算し直します。すでに保留・受付済みの確認は、その新期限より先に届く場合があります。設定反映のためのプラグイン再ロードは不要です。

`off` は子を停止せず、保留中の確認を消し、ターン入口で確認対象を渡さなくします。無効化中に起動した子も登録します。再有効化すると、現在時刻から次の期限を設定します。

Classic CLIでは未消費の確認要求を失効させます。Gateway・TUI・Desktopには受付済み入力の撤回APIがないため、無効化後に一度届く場合があります。その要求だけでは子への追加指示を促しません。ただし、すでに確認対象を受け取って開始した親ターンは取り消しません。

### 操作できる画面とbusy時の扱い

| 画面・経路 | `/supervision` の扱い |
|---|---|
| Classic CLI | ネイティブのコマンド処理を使用 |
| TUI・Desktop | ネイティブのプラグイン呼出しを使用。busy時の制御呼出しはオフライン確認済み |
| アイドル中のMessaging Gateway | 登録済みコマンドとして処理 |
| busy中のMessaging Gateway | 対応するホスト・アダプターでは明示拒否。親がidleになってから操作 |

busy Gatewayではユーザー認可・bot受付・slash権限確認を経て拒否します。通常入力のキュー、モデルへのsteer、interruptには渡さず、設定も変更しません。

この拒否はホスト内部の限定アダプターに依存します。未知の実装では警告付きで無効になり、ネイティブ処理がコマンド文字列を通常入力として扱う可能性があります。対応確認なしにbusy時の安全な制御を保証するものではありません。

## 導入

Pythonパッケージの宣言上の要件はPython 3.10以上です。実際の対応はHermesのAPIと内部実装に依存します。確認したホストは `v0.21.5+4775.g3ebbaf5`、ソースrevisionは `3ebbaf524344f93943169e63854cb952541563f9` です。最新版を含む他revisionへの互換性は未確認です。先に[互換性と制限](docs/compatibility.md)を確認してください。

リポジトリはnative directory pluginとPython entry pointの両方を用意しています。管理ランタイムではHermesの正規プラグイン管理を使います。以下は**リポジトリ公開後**のインストール例で、確認した公開コミットの40桁SHAを `PUBLIC_COMMIT_SHA` に指定します。

```sh
PUBLIC_COMMIT_SHA=REPLACE_WITH_REVIEWED_40_CHARACTER_COMMIT_SHA
hermes plugins install teich0psia/hermes-delegate-supervisor \
  --ref "$PUBLIC_COMMIT_SHA" --no-enable
hermes plugins show delegate_supervisor
```

対象プロファイルは、通常のHermesプロファイル選択または `HERMES_HOME` で明示します。`--no-enable` はインストールのみの指定です。既存の有効な版の置換は、この新規導入例とは別に状態と権限を確認してください。

自動確認をTUI・Desktop・Gatewayへ送る場合、`gateway.inject` 相当の権限が必要です。ロード時の既定間隔と注入許可は次のキーで設定します。

```sh
hermes config set plugins.entries.delegate_supervisor.settings.interval_seconds 600
hermes config set plugins.entries.delegate_supervisor.allow_gateway_injection true
hermes plugins enable delegate_supervisor --no-allow-tool-override
```

組み込みツールの置換権限は不要です。Classic CLIの限定FIFO経路はGateway注入権限を使いません。間隔の設定値は有限の正数に限り、文字列・真偽値・0・負数・NaN・無限大ではプラグインを無効化します。

`enable` はホストによって稼働Gatewayのプラグイン再ロードを要求します。サービス再起動とは別ですが、本プラグインの監督登録・会話上書きは失われます。稼働中の会話がある場合は再ロードの影響を確認してから有効化してください。設定ファイルの既定間隔はロード時に読み取るため、稼働中の変更には会話内の `/supervision` を使います。

## 確認要求の配送と会話の寿命

- 子ごとに単調増加時計で期限を管理します。親がbusyなら確認を保留し、次の自然なターン入口で対象を渡すか、idleになってから確認ターンを予約します。監督のために実行中の親ターンを中断しません。
- 予約には固有トークンを付け、その要求が入口に届くまで追加予約を抑止します。受付成功はモデル実行や返信成功の証明ではありません。無関係なユーザーターンを到着確認には使いません。
- Classic CLIは消費時に会話世代と予約の有効性を再確認し、失効した自プラグインの要求だけを捨てます。通常入力のFIFO順序は維持します。Gatewayでは非同期セッション解決後と実行入口で世代を確認します。
- 子の完了・停止、親のstop・reset・finalize、プラグインのunloadで監督対象を解除します。親のstopは会話設定を保持し、reset・finalizeは設定も解除します。最後の子が終了しても会話の上書きは保持します。
- 確定した圧縮履歴から一意な継続と判断できる場合だけ、設定と監督を引き継ぎます。別セッションのresume・reset、別送信先への履歴再利用では引き継ぎません。履歴が曖昧、DBが読めない、未知の実装の場合も移行を拒否します。
- 状態はプロセス内だけに保持します。unload・reload・restart後の監督再登録と会話上書きの復元は行いません。Hermes本体の永続化された完了配送とは独立です。

## 開発・検証

ソースと展開wheelについて、既存のオフライン受入記録では各99件のテスト成功を確認しています。これは実ホストの処理をfixtureへ結合した検証であり、実LLMの子起動、認証、Discord配送、各画面の実起動までの成功を示しません。公開準備での検証は文書・パッケージ・変更した開発スクリプトに限定し、変更のない機能テスト一式は再実行していません。

環境を変更せずに検証する手順と外部チェックアウト要件は[開発手順](docs/development.md)を参照してください。登録時の `ACTIVE` ログやDoctor成功だけでは、長時間動くGatewayがその版を採用したことや、定期確認の実配送は確認できません。

## ライセンスと由来

[MIT License](LICENSE)です。利用・改変・再配布・商用利用が可能です。コピーまたは主要部分を再配布する際は、著作権表示と許諾文を保持してください。無保証です。

Hermes Agent本体や第三者の依存には、それぞれのライセンスが適用されます。テストは別チェックアウトのホスト処理を読み取り、fixtureで実行します。Hermes本体や別のroutingプラグインを同梱する構成ではありません。

## 参考資料

- [Hermes Plugin開発ガイド](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/)
- [Delegation・実行中の子への追加指示](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation/)
- [Session Heartbeat](https://hermes-agent.nousresearch.com/docs/user-guide/features/heartbeat/)
