# 互換性と制限

## 確認した実装

v0.2.0のセッション制御とbusy Gateway拒否は、Hermes Agent `3ebbaf524344f93943169e63854cb952541563f9` を対象にオフライン検証しています。Pythonパッケージのバージョン要件を満たすだけでは、各画面の互換性は確認できません。

プラグインは `subagent_start`・`subagent_stop`、LLM呼出し前後、停止・reset・finalizeのhookと、`tool_execution` middleware、`register_command` を使用します。委譲のcore関数・schema・既存routing wrapperは置換しません。コマンド登録の正本は実行時の `register_command` です。

一方、配送の世代確認とbusy拒否には公開APIだけでは足りないため、次の限定されたインスタンス処理をラップします。

- Gatewayの確認要求dispatchとstrict消費の確認。
- Classic CLIのFIFO消費入口。
- busy Gatewayの外部 `/supervision` 判定、アダプターのinline返信経路。
- プロファイルmanagerのホスト公開とアダプター配線の同期通知。

圧縮の確定処理、子構築の順序、TUI注入処理もソースの指紋で確認します。検証対象の関数をdedentしたソースのSHA256が一致しない場合、該当機能を無効にします。ホストのファイルは編集しませんが、内部実装に依存する実行時ラップであることは変わりません。

[公式のプラグイン互換性契約](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/#native-plugin-compatibility-contract)は、こうした内部関数・属性の置換をサポートする拡張点として扱っていません。公開リポジトリとして配布できることと、公式catalogの採用要件を満たすことは別です。catalog適合・最新版互換を主張しません。

## 警告と無効状態

現在のmanifestには説明用の `capabilities.commands` mappingがあります。確認したホストはcapabilitiesをlistとして解釈するため、これを無視する警告を出します。コマンドの実登録とdispatchは別途確認済みですが、正式なcapability宣言に成功した意味ではありません。

未知のホスト・アダプター、独自のpublication・配線、第三者が同じ処理を置換した構成は保証外です。特にbusy拒否が無効になった場合はネイティブの通常入力化が残り得ます。警告を確認し、busy中の `/supervision` を使わず、対応版でidle時に操作してください。失敗を別会話への配送で回避しません。

`/supervision` は他プラグインの同名コマンドと競合する可能性があります。コマンド名だけで所有を推定せず、実際の登録handlerとプロファイルで所有を確認します。

## 配送の限界

Gateway・TUIで受付済みの要求を撤回するAPIはありません。子の完了や無効化の後に空の確認要求が届く場合でも、入口では現在の監督対象を再確認します。Classic CLIでは未消費の自プラグインenvelopeを失効させます。すでに開始したモデルターンの取り消しはしません。

注入の受付後にホストが認可・送信先を拒否した場合、プラグインへ確定した配送失敗callbackは戻りません。受付トークンが入口へ届くまで追加予約を抑止するため、ホストログで原因を確認する必要があります。予約の成功を実行・返信の成功として報告しません。

個別の子に外部UIからinterruptを要求した瞬間の公開hookはありません。モデル経由の成功したstop要求は即座に登録解除しますが、外部UI経由ではregistryの消失・終了hookを待ちます。停止要求の直後に必ず解除される保証はありません。

unloadでは自分が追加したインスタンス属性だけを復元し、別ownerの変更を上書きしません。Classic CLIに失効envelopeが残る間は、それを捨てる無効guardが残ります。CLIが消費せずに終了した場合は、インスタンスの寿命まで残り得ます。

## オフラインと実運用の区別

既存の受入ではsourceと展開wheelで各99件が成功しています。busy ingress、通常入力保持、権限確認、プロファイル分離、会話上書き、圧縮後の世代確認、停止・unload・予約失効を検証しています。

実際のホスト処理を使う場合も、constructor・認証解決・transport末端などはfixtureです。実Gatewayの起動全体、実transport reconnect、Discord・CLI・TUI・Desktopでのライブ配送、実LLMの子起動と親の判断、配備後の採用はこの検証に含みません。状態はプロセス内だけにあり、再ロードによる復元を保証しません。
